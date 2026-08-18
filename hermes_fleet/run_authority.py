"""Phase 10 immutable RunAuthority and durable revocation/replay state.

The authority document is immutable and content-addressed. Operational state
(active/cancelled/revoked and the one exact Run Capsule claim) is stored
separately so revocation never rewrites the authorized request.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from .host_action_broker import HostActionAuthorityScope, HostActionGrant
from .network_isolation import (
    NETWORK_NONE,
    NetworkAuthorityScope,
    NetworkDestination,
    NetworkGrant,
)
from .principal_identity import PrincipalReference
from .workspace_isolation import (
    ArtifactExportGrant,
    FilesystemAuthorityScope,
    FilesystemGrant,
)

AUTHORITY_SCHEMA: Final[str] = "fleet.run-authority.v1"
STORE_SCHEMA: Final[str] = "fleet.run-authority-store.v1"
SIGNATURE_SCHEMA: Final[str] = "fleet.run-authority-attestation.v1"
DUMMY_AUTHORITY_HASH: Final[str] = "sha256:" + "0" * 64

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./_-]{0,254}@sha256:[0-9a-f]{64})$"
)
_MAX_JSON_BYTES = 512 * 1024
_MAX_ITEMS = 128
_U64_MAX = (1 << 64) - 1


class RunAuthorityError(RuntimeError):
    """RunAuthority is malformed, stale, broadened, replayed, or inactive."""


class RunAuthorityConflict(RunAuthorityError):
    """An execution/idempotency identity conflicts with durable authority state."""


class RunAuthorityInactive(RunAuthorityError):
    """RunAuthority is cancelled, revoked, or expired."""


class RunAuthorityStale(RunAuthorityError):
    """RunAuthority no longer matches current policy/capabilities/destination."""


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise RunAuthorityError(f"{label} is invalid")
    return value


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise RunAuthorityError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int = _U64_MAX) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= maximum:
        raise RunAuthorityError(f"{label} is invalid")
    return value


def _bounded_nonnegative_int(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 <= value <= maximum:
        raise RunAuthorityError(f"{label} is invalid")
    return value


def _canonical(value: object, label: str) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise RunAuthorityError(f"{label} is not canonical JSON") from error
    if len(payload) > _MAX_JSON_BYTES:
        raise RunAuthorityError(f"{label} exceeds the supported bound")
    return payload


def _digest(value: object, label: str) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value, label)).hexdigest()


def _strings(
    value: object, label: str, *, maximum: int = _MAX_ITEMS
) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or len(value) > maximum:
        raise RunAuthorityError(f"{label} is invalid")
    normalized = tuple(_identifier(item, label) for item in value)
    if len(normalized) != len(set(normalized)):
        raise RunAuthorityError(f"{label} contains duplicates")
    return tuple(sorted(normalized))


def _plain_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RunAuthorityError(f"{label} is invalid")
    return MappingProxyType(json.loads(_canonical(dict(value), label).decode("utf-8")))


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise RunAuthorityError(f"{label} has an invalid closed schema")
    return value


def _normalized_sql(value: object) -> str:
    if type(value) is not str:
        return ""
    return "".join(value.lower().split())


def _require_nonsymlink_directory_components(
    path: Path,
    label: str,
) -> os.stat_result:
    if not path.is_absolute() or ".." in path.parts:
        raise RunAuthorityError(f"{label} is unsafe")
    normalized = Path(os.path.abspath(os.fspath(path)))
    current = Path(normalized.anchor)
    try:
        identity = current.lstat()
        for component in normalized.parts[1:]:
            current /= component
            identity = current.lstat()
            if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
                raise RunAuthorityError(f"{label} is unsafe")
    except FileNotFoundError as error:
        raise RunAuthorityError(f"{label} must exist") from error
    except OSError as error:
        raise RunAuthorityError(f"{label} is unsafe") from error
    return identity


@dataclass(frozen=True, slots=True)
class ResourceAuthority:
    cpu_millis: int
    memory_bytes: int
    pids_limit: int
    max_iterations: int

    def __post_init__(self) -> None:
        _positive_int(self.cpu_millis, "authority CPU limit", maximum=1_000_000)
        _positive_int(self.memory_bytes, "authority memory limit")
        _positive_int(self.pids_limit, "authority PID limit", maximum=65_535)
        _positive_int(self.max_iterations, "authority iteration limit", maximum=32)

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_millis": self.cpu_millis,
            "memory_bytes": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "max_iterations": self.max_iterations,
        }


@dataclass(frozen=True, slots=True)
class IsolationAuthority:
    backend_kind: str = "fleet.dev/docker-oci"
    non_root: bool = True
    read_only_root: bool = True
    cap_drop_all: bool = True
    no_new_privileges: bool = True
    docker_socket: bool = False
    management_network: bool = False

    def __post_init__(self) -> None:
        _identifier(self.backend_kind, "authority backend kind")
        if self.backend_kind != "fleet.dev/docker-oci":
            raise RunAuthorityError("authority backend kind is unsupported")
        if (
            self.non_root is not True
            or self.read_only_root is not True
            or self.cap_drop_all is not True
            or self.no_new_privileges is not True
            or self.docker_socket is not False
            or self.management_network is not False
        ):
            raise RunAuthorityError(
                "authority isolation posture may not weaken Fleet hardening"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "backend_kind": self.backend_kind,
            "non_root": self.non_root,
            "read_only_root": self.read_only_root,
            "cap_drop_all": self.cap_drop_all,
            "no_new_privileges": self.no_new_privileges,
            "docker_socket": self.docker_socket,
            "management_network": self.management_network,
        }


@dataclass(frozen=True, slots=True)
class ModelProviderAuthority:
    providers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "providers", _strings(self.providers, "provider constraint")
        )
        object.__setattr__(self, "models", _strings(self.models, "model constraint"))

    def to_dict(self) -> dict[str, object]:
        return {"providers": list(self.providers), "models": list(self.models)}


@dataclass(frozen=True, slots=True)
class RecipeAuthorityBinding:
    recipe_hash: str
    resolved_recipe_hash: str
    compiler_version: str
    provenance_digest: str
    image: str
    workflow_id: str | None = None
    workflow_revision: int | None = None
    workflow_hash: str | None = None
    workflow_step_id: str | None = None

    def __post_init__(self) -> None:
        _hash(self.recipe_hash, "authority Recipe hash")
        _hash(self.resolved_recipe_hash, "authority ResolvedRecipe hash")
        _identifier(self.compiler_version, "authority Recipe compiler version")
        _hash(self.provenance_digest, "authority Recipe provenance digest")
        if type(self.image) is not str or _IMAGE_RE.fullmatch(self.image) is None:
            raise RunAuthorityError("authority runtime image must be digest-pinned")
        values = (
            self.workflow_id,
            self.workflow_revision,
            self.workflow_hash,
            self.workflow_step_id,
        )
        if any(item is not None for item in values):
            if any(item is None for item in values):
                raise RunAuthorityError("authority Workflow binding must be complete")
            _identifier(self.workflow_id, "authority Workflow id")
            _positive_int(self.workflow_revision, "authority Workflow revision")
            _hash(self.workflow_hash, "authority Workflow hash")
            _identifier(self.workflow_step_id, "authority Workflow step id")

    def to_dict(self) -> dict[str, object]:
        return {
            "recipe_hash": self.recipe_hash,
            "resolved_recipe_hash": self.resolved_recipe_hash,
            "compiler_version": self.compiler_version,
            "provenance_digest": self.provenance_digest,
            "image": self.image,
            "workflow_id": self.workflow_id,
            "workflow_revision": self.workflow_revision,
            "workflow_hash": self.workflow_hash,
            "workflow_step_id": self.workflow_step_id,
        }


@dataclass(frozen=True, slots=True)
class NetworkAuthorityIntent:
    mode: str
    destinations: tuple[NetworkDestination, ...] = ()
    approval_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.destinations) not in {tuple, list}:
            raise RunAuthorityError("authority network destinations are invalid")
        destinations = tuple(self.destinations)
        try:
            grant = NetworkGrant(
                mode=self.mode,
                authority_ref=DUMMY_AUTHORITY_HASH,
                destinations=destinations,
                approval_ref=self.approval_ref,
            )
        except Exception as error:
            raise RunAuthorityError("authority network intent is invalid") from error
        object.__setattr__(self, "mode", grant.mode)
        object.__setattr__(self, "destinations", grant.destinations)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "destinations": [item.to_dict() for item in self.destinations],
            "approval_ref": self.approval_ref,
        }

    def materialize(self, authority_hash: str) -> NetworkGrant:
        return NetworkGrant(
            mode=self.mode,
            authority_ref=_hash(authority_hash, "RunAuthority hash"),
            destinations=self.destinations,
            approval_ref=self.approval_ref,
        )


@dataclass(frozen=True, slots=True)
class FilesystemAuthorityIntent:
    project_id: str
    relative_path: str
    target: str
    mode: str = "read"
    max_bytes: int = 64 * 1024 * 1024
    write_authority_ref: str | None = None

    def __post_init__(self) -> None:
        try:
            grant = FilesystemGrant(
                project_id=self.project_id,
                relative_path=self.relative_path,
                target=self.target,
                mode=self.mode,
                max_bytes=self.max_bytes,
                authority_ref=DUMMY_AUTHORITY_HASH,
                write_authority_ref=self.write_authority_ref,
            )
        except Exception as error:
            raise RunAuthorityError("authority filesystem intent is invalid") from error
        object.__setattr__(self, "project_id", grant.project_id)
        object.__setattr__(self, "relative_path", grant.relative_path)
        object.__setattr__(self, "target", grant.target)
        object.__setattr__(self, "mode", grant.mode)
        object.__setattr__(self, "max_bytes", grant.max_bytes)

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "relative_path": self.relative_path,
            "target": self.target,
            "mode": self.mode,
            "max_bytes": self.max_bytes,
            "write_authority_ref": self.write_authority_ref,
        }

    def materialize(self, authority_hash: str) -> FilesystemGrant:
        return FilesystemGrant(
            project_id=self.project_id,
            relative_path=self.relative_path,
            target=self.target,
            mode=self.mode,
            max_bytes=self.max_bytes,
            authority_ref=_hash(authority_hash, "RunAuthority hash"),
            write_authority_ref=self.write_authority_ref,
        )


@dataclass(frozen=True, slots=True)
class RunAuthority:
    execution_id: str
    idempotency_digest: str
    principal: PrincipalReference
    agent_instance_id: str
    recipe: RecipeAuthorityBinding
    policy_digest: str
    capabilities_hash: str
    target: Mapping[str, Any]
    target_digest: str
    plan_fingerprint: str
    issued_at_ms: int
    deadline_ms: int
    resources: ResourceAuthority
    isolation: IsolationAuthority
    network: NetworkAuthorityIntent
    filesystem: tuple[FilesystemAuthorityIntent, ...] = ()
    artifacts: tuple[ArtifactExportGrant, ...] = ()
    toolsets: tuple[str, ...] = ()
    approval_budget: int = 0
    secret_refs: tuple[str, ...] = ()
    host_grants: tuple[HostActionGrant, ...] = ()
    model_provider: ModelProviderAuthority = field(
        default_factory=ModelProviderAuthority
    )
    project_scope: tuple[str, ...] = ()
    remote_keryx_task_id: str | None = None
    parent_authority_hash: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.execution_id, "RunAuthority execution id")
        _hash(self.idempotency_digest, "RunAuthority idempotency digest")
        if type(self.principal) is not PrincipalReference:
            raise RunAuthorityError("RunAuthority principal is invalid")
        _hash(self.agent_instance_id, "RunAuthority Agent Instance id")
        if type(self.recipe) is not RecipeAuthorityBinding:
            raise RunAuthorityError("RunAuthority Recipe binding is invalid")
        _hash(self.policy_digest, "RunAuthority policy digest")
        _hash(self.capabilities_hash, "RunAuthority capabilities hash")
        if not isinstance(self.target, Mapping):
            raise RunAuthorityError("RunAuthority target is invalid")
        target = _plain_mapping(self.target, "RunAuthority target")
        if _digest(dict(target), "RunAuthority target") != self.target_digest:
            raise RunAuthorityError("RunAuthority target digest does not match target")
        object.__setattr__(self, "target", target)
        _hash(self.target_digest, "RunAuthority target digest")
        _hash(self.plan_fingerprint, "RunAuthority plan fingerprint")
        _positive_int(self.issued_at_ms, "RunAuthority issue timestamp")
        _positive_int(self.deadline_ms, "RunAuthority deadline")
        if self.deadline_ms <= self.issued_at_ms:
            raise RunAuthorityError("RunAuthority deadline must follow issuance")
        if type(self.resources) is not ResourceAuthority:
            raise RunAuthorityError("RunAuthority resources are invalid")
        if type(self.isolation) is not IsolationAuthority:
            raise RunAuthorityError("RunAuthority isolation is invalid")
        if type(self.network) is not NetworkAuthorityIntent:
            raise RunAuthorityError("RunAuthority network intent is invalid")

        filesystem = tuple(self.filesystem)
        if len(filesystem) > _MAX_ITEMS or any(
            type(item) is not FilesystemAuthorityIntent for item in filesystem
        ):
            raise RunAuthorityError("RunAuthority filesystem intents are invalid")
        filesystem_keys = [
            (item.project_id, item.relative_path, item.target, item.mode)
            for item in filesystem
        ]
        if len(filesystem_keys) != len(set(filesystem_keys)):
            raise RunAuthorityError(
                "RunAuthority filesystem intents contain duplicates"
            )
        object.__setattr__(
            self,
            "filesystem",
            tuple(
                sorted(
                    filesystem,
                    key=lambda item: (
                        item.project_id,
                        item.relative_path,
                        item.target,
                        item.mode,
                        item.max_bytes,
                        item.write_authority_ref or "",
                    ),
                )
            ),
        )

        artifacts = tuple(self.artifacts)
        if len(artifacts) > _MAX_ITEMS or any(
            type(item) is not ArtifactExportGrant for item in artifacts
        ):
            raise RunAuthorityError("RunAuthority artifact grants are invalid")
        artifact_names = [item.name for item in artifacts]
        if len(artifact_names) != len(set(artifact_names)):
            raise RunAuthorityError("RunAuthority artifact grants contain duplicates")
        object.__setattr__(
            self, "artifacts", tuple(sorted(artifacts, key=lambda item: item.name))
        )

        object.__setattr__(
            self, "toolsets", _strings(self.toolsets, "RunAuthority toolset")
        )
        _bounded_nonnegative_int(
            self.approval_budget, "RunAuthority approval budget", maximum=32
        )
        object.__setattr__(
            self,
            "secret_refs",
            _strings(self.secret_refs, "RunAuthority secret reference", maximum=64),
        )
        host_grants = tuple(self.host_grants)
        if len(host_grants) > _MAX_ITEMS or any(
            type(item) is not HostActionGrant for item in host_grants
        ):
            raise RunAuthorityError("RunAuthority host grants are invalid")
        host_keys = [
            (item.verb, item.target, item.parameters_digest) for item in host_grants
        ]
        if len(host_keys) != len(set(host_keys)):
            raise RunAuthorityError("RunAuthority host grants contain duplicates")
        object.__setattr__(
            self,
            "host_grants",
            tuple(
                sorted(
                    host_grants,
                    key=lambda item: (item.verb, item.target, item.parameters_digest),
                )
            ),
        )
        if type(self.model_provider) is not ModelProviderAuthority:
            raise RunAuthorityError(
                "RunAuthority model/provider constraints are invalid"
            )
        object.__setattr__(
            self,
            "project_scope",
            _strings(self.project_scope, "RunAuthority project scope"),
        )
        if self.remote_keryx_task_id is not None:
            _identifier(self.remote_keryx_task_id, "RunAuthority Keryx task id")
        if self.parent_authority_hash is not None:
            _hash(self.parent_authority_hash, "parent RunAuthority hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": AUTHORITY_SCHEMA,
            "execution_id": self.execution_id,
            "idempotency_digest": self.idempotency_digest,
            "principal": self.principal.to_dict(),
            "agent_instance_id": self.agent_instance_id,
            "recipe": self.recipe.to_dict(),
            "policy_digest": self.policy_digest,
            "capabilities_hash": self.capabilities_hash,
            "target": dict(self.target),
            "target_digest": self.target_digest,
            "plan_fingerprint": self.plan_fingerprint,
            "issued_at_ms": self.issued_at_ms,
            "deadline_ms": self.deadline_ms,
            "resources": self.resources.to_dict(),
            "isolation": self.isolation.to_dict(),
            "network": self.network.to_dict(),
            "filesystem": [item.to_dict() for item in self.filesystem],
            "artifacts": [
                {
                    "name": item.name,
                    "path": item.path,
                    "max_bytes": item.max_bytes,
                    "scan_required": item.scan_required,
                }
                for item in self.artifacts
            ],
            "toolsets": list(self.toolsets),
            "approval_budget": self.approval_budget,
            "secret_refs": list(self.secret_refs),
            "host_grants": [item.to_dict() for item in self.host_grants],
            "model_provider": self.model_provider.to_dict(),
            "project_scope": list(self.project_scope),
            "remote_keryx_task_id": self.remote_keryx_task_id,
            "parent_authority_hash": self.parent_authority_hash,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "RunAuthority")

    @property
    def audit_hash(self) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                b"fleet.run-authority.audit.v1\0"
                + _canonical(self.to_dict(), "RunAuthority audit")
            ).hexdigest()
        )

    def network_grant(self) -> NetworkGrant:
        return self.network.materialize(self.content_hash)

    def filesystem_grants(self) -> tuple[FilesystemGrant, ...]:
        return tuple(item.materialize(self.content_hash) for item in self.filesystem)

    def network_scope(self) -> NetworkAuthorityScope:
        approvals = (
            () if self.network.approval_ref is None else (self.network.approval_ref,)
        )
        return NetworkAuthorityScope(self.content_hash, approvals)

    def filesystem_scope(self) -> FilesystemAuthorityScope:
        write_refs = tuple(
            sorted(
                {
                    item.write_authority_ref
                    for item in self.filesystem
                    if item.write_authority_ref is not None
                }
            )
        )
        return FilesystemAuthorityScope(self.content_hash, write_refs)

    def host_scope(self) -> HostActionAuthorityScope:
        return HostActionAuthorityScope(
            principal_id=self.principal.principal_id,
            execution_id=self.execution_id,
            run_authority_hash=self.content_hash,
            resolved_recipe_hash=self.recipe.resolved_recipe_hash,
            policy_digest=self.policy_digest,
            target_digest=self.target_digest,
            deadline_ms=self.deadline_ms,
            grants=self.host_grants,
        )

    def validate_context(
        self,
        *,
        principal: PrincipalReference,
        agent_instance_id: str,
        recipe_hash: str,
        resolved_recipe_hash: str,
        policy_digest: str,
        capabilities_hash: str,
        target_digest: str,
        now_ms: int,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        if principal != self.principal:
            raise RunAuthorityStale("RunAuthority principal changed")
        if agent_instance_id != self.agent_instance_id:
            raise RunAuthorityStale("RunAuthority Agent Instance changed")
        if (
            recipe_hash != self.recipe.recipe_hash
            or resolved_recipe_hash != self.recipe.resolved_recipe_hash
        ):
            raise RunAuthorityStale("RunAuthority Recipe changed")
        if policy_digest != self.policy_digest:
            raise RunAuthorityStale("RunAuthority policy is stale")
        if capabilities_hash != self.capabilities_hash:
            raise RunAuthorityStale("RunAuthority capabilities are stale")
        if target_digest != self.target_digest:
            raise RunAuthorityStale("RunAuthority target is stale")
        if self.model_provider.providers:
            if provider is None or provider not in self.model_provider.providers:
                raise RunAuthorityStale("RunAuthority provider constraint is stale")
        elif provider is not None:
            _identifier(provider, "current model provider")
        if self.model_provider.models:
            if model is None or model not in self.model_provider.models:
                raise RunAuthorityStale("RunAuthority model constraint is stale")
        elif model is not None:
            _identifier(model, "current model")
        _positive_int(now_ms, "RunAuthority validation timestamp")
        if now_ms >= self.deadline_ms:
            raise RunAuthorityInactive("RunAuthority is expired")

    def to_capsule_spec(self):
        from .run_capsule import RunCapsuleSpec

        network = self.network_grant()
        return RunCapsuleSpec(
            execution_id=self.execution_id,
            idempotency_digest=self.idempotency_digest,
            agent_instance_id=self.agent_instance_id,
            principal=self.principal,
            recipe_hash=self.recipe.recipe_hash,
            resolved_recipe_hash=self.recipe.resolved_recipe_hash,
            recipe_compiler_version=self.recipe.compiler_version,
            requirement_provenance_digest=self.recipe.provenance_digest,
            run_authority_hash=self.content_hash,
            capabilities_hash=self.capabilities_hash,
            target=dict(self.target),
            target_digest=self.target_digest,
            project_scope=self.project_scope,
            network_grant=network,
            network_mode=network.mode,
            network_policy_hash=network.policy_hash,
            toolsets=self.toolsets,
            approval_budget=self.approval_budget,
            secret_refs=self.secret_refs,
            filesystem_grants=self.filesystem_grants(),
            artifact_grants=self.artifacts,
            host_broker_grants=self.host_grants,
            cpu_millis=self.resources.cpu_millis,
            memory_bytes=self.resources.memory_bytes,
            pids_limit=self.resources.pids_limit,
            max_iterations=self.resources.max_iterations,
            deadline_ms=self.deadline_ms,
            image=self.recipe.image,
            plan_fingerprint=self.plan_fingerprint,
            remote_keryx_task_id=self.remote_keryx_task_id,
            workflow_id=self.recipe.workflow_id,
            workflow_revision=self.recipe.workflow_revision,
            workflow_hash=self.recipe.workflow_hash,
            workflow_step_id=self.recipe.workflow_step_id,
        )

    def validate_capsule(self, spec: object) -> None:
        expected = self.to_capsule_spec()
        if spec != expected:
            raise RunAuthorityConflict(
                "Run Capsule is not an exact projection of RunAuthority"
            )

    def narrow(
        self,
        *,
        plan_fingerprint: str,
        deadline_ms: int | None = None,
        resources: ResourceAuthority | None = None,
        network: NetworkAuthorityIntent | None = None,
        filesystem: tuple[FilesystemAuthorityIntent, ...] | None = None,
        artifacts: tuple[ArtifactExportGrant, ...] | None = None,
        toolsets: tuple[str, ...] | None = None,
        approval_budget: int | None = None,
        secret_refs: tuple[str, ...] | None = None,
        host_grants: tuple[HostActionGrant, ...] | None = None,
        model_provider: ModelProviderAuthority | None = None,
        project_scope: tuple[str, ...] | None = None,
    ) -> RunAuthority:
        child = replace(
            self,
            plan_fingerprint=_hash(plan_fingerprint, "narrowed plan fingerprint"),
            deadline_ms=self.deadline_ms if deadline_ms is None else deadline_ms,
            resources=self.resources if resources is None else resources,
            network=self.network if network is None else network,
            filesystem=self.filesystem if filesystem is None else filesystem,
            artifacts=self.artifacts if artifacts is None else artifacts,
            toolsets=self.toolsets if toolsets is None else toolsets,
            approval_budget=(
                self.approval_budget if approval_budget is None else approval_budget
            ),
            secret_refs=self.secret_refs if secret_refs is None else secret_refs,
            host_grants=self.host_grants if host_grants is None else host_grants,
            model_provider=(
                self.model_provider if model_provider is None else model_provider
            ),
            project_scope=(
                self.project_scope if project_scope is None else project_scope
            ),
            parent_authority_hash=self.content_hash,
        )
        _require_narrower(self, child)
        return child


@dataclass(frozen=True, slots=True)
class RunAuthorityAttestation:
    key_id: str
    authority_hash: str
    mac_hex: str
    algorithm: str = "hmac-sha256"

    def __post_init__(self) -> None:
        _identifier(self.key_id, "RunAuthority attestation key id")
        _hash(self.authority_hash, "RunAuthority attested hash")
        if (
            self.algorithm != "hmac-sha256"
            or type(self.mac_hex) is not str
            or _HEX_RE.fullmatch(self.mac_hex) is None
        ):
            raise RunAuthorityError("RunAuthority attestation is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": SIGNATURE_SCHEMA,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "authority_hash": self.authority_hash,
            "mac_hex": self.mac_hex,
        }


class RunAuthoritySigner:
    """Host-supplied symmetric attestation; the key is never persisted here."""

    def __init__(self, *, key_id: str, key: bytes) -> None:
        self.key_id = _identifier(key_id, "RunAuthority signer key id")
        if type(key) is not bytes or len(key) < 32:
            raise RunAuthorityError("RunAuthority signer key is invalid")
        self._key = key

    def sign(self, authority: RunAuthority) -> RunAuthorityAttestation:
        if type(authority) is not RunAuthority:
            raise RunAuthorityError("RunAuthority signing input is invalid")
        mac = hmac.new(
            self._key,
            b"fleet.run-authority.attestation.v1\0" + authority.content_hash.encode(),
            hashlib.sha256,
        ).hexdigest()
        return RunAuthorityAttestation(self.key_id, authority.content_hash, mac)

    def verify(
        self, authority: RunAuthority, attestation: RunAuthorityAttestation
    ) -> bool:
        if (
            type(authority) is not RunAuthority
            or type(attestation) is not RunAuthorityAttestation
        ):
            return False
        if (
            attestation.key_id != self.key_id
            or attestation.authority_hash != authority.content_hash
        ):
            return False
        expected = self.sign(authority)
        return hmac.compare_digest(expected.mac_hex, attestation.mac_hex)


@dataclass(frozen=True, slots=True)
class RunAuthorityRecord:
    authority: RunAuthority
    state: str
    state_generation: int
    claimed_capsule_hash: str | None
    created_at_ms: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        if type(self.authority) is not RunAuthority:
            raise RunAuthorityError("RunAuthority record authority is invalid")
        if self.state not in {"active", "superseded", "cancelled", "revoked"}:
            raise RunAuthorityError("RunAuthority record state is invalid")
        _positive_int(self.state_generation, "RunAuthority state generation")
        if self.claimed_capsule_hash is not None:
            _hash(self.claimed_capsule_hash, "RunAuthority claimed Capsule hash")
        _positive_int(self.created_at_ms, "RunAuthority created timestamp")
        _positive_int(self.updated_at_ms, "RunAuthority updated timestamp")
        if self.updated_at_ms < self.created_at_ms:
            raise RunAuthorityError("RunAuthority record timestamps are invalid")


_AUTHORITY_TABLE = """
CREATE TABLE run_authorities (
    authority_hash TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    idempotency_digest TEXT NOT NULL,
    authority_json TEXT NOT NULL CHECK(json_valid(authority_json)),
    audit_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('active','superseded','cancelled','revoked')),
    state_generation INTEGER NOT NULL CHECK(state_generation >= 1),
    claimed_capsule_hash TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
)
"""
_SCHEMA_TABLE = "CREATE TABLE run_authority_schema (schema_id TEXT PRIMARY KEY)"


class RunAuthorityStore:
    """Durable replay/revocation state for immutable RunAuthority documents."""

    def __init__(
        self,
        path: str | Path,
        *,
        now_ms: Callable[[], int] | None = None,
        principal_state_check: Callable[[PrincipalReference], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.name:
            raise RunAuthorityError("RunAuthority store path must be absolute")
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        if not callable(self._now_ms):
            raise RunAuthorityError("RunAuthority store clock is invalid")
        if principal_state_check is not None and not callable(principal_state_check):
            raise RunAuthorityError("RunAuthority principal state check is invalid")
        self._principal_state_check = principal_state_check
        self._prepare_file()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._initialize(connection)

    def _require_principal_current(self, principal: PrincipalReference) -> None:
        if self._principal_state_check is None:
            return
        try:
            current = self._principal_state_check(principal)
        except Exception as error:
            raise RunAuthorityStale(
                "RunAuthority principal state is unavailable"
            ) from error
        if current is not True:
            raise RunAuthorityStale("RunAuthority principal is stale")

    def admit(self, authority: RunAuthority) -> tuple[RunAuthorityRecord, bool]:
        if type(authority) is not RunAuthority:
            raise RunAuthorityError("RunAuthority admission input is invalid")
        self._require_principal_current(authority.principal)
        now = self._now()
        if now < authority.issued_at_ms:
            raise RunAuthorityInactive("RunAuthority is not yet valid")
        if now >= authority.deadline_ms:
            raise RunAuthorityInactive("RunAuthority is expired")
        encoded = _canonical(authority.to_dict(), "RunAuthority").decode("utf-8")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            by_hash = self._select(connection, authority.content_hash)
            if by_hash is not None:
                record = self._row_to_record(by_hash)
                if record.authority != authority:
                    raise RunAuthorityConflict("RunAuthority hash collision/conflict")
                return record, False
            collision = connection.execute(
                (
                    "SELECT authority_hash FROM run_authorities "
                    "WHERE execution_id = ? OR idempotency_digest = ?"
                ),
                (authority.execution_id, authority.idempotency_digest),
            ).fetchone()
            if collision is not None:
                raise RunAuthorityConflict(
                    "execution or idempotency identity is already bound to "
                    "another RunAuthority"
                )
            connection.execute(
                """
                INSERT INTO run_authorities(
                    authority_hash, execution_id, idempotency_digest, authority_json,
                    audit_hash, state, state_generation, claimed_capsule_hash,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'active', 1, NULL, ?, ?)
                """,
                (
                    authority.content_hash,
                    authority.execution_id,
                    authority.idempotency_digest,
                    encoded,
                    authority.audit_hash,
                    now,
                    now,
                ),
            )
            row = self._select(connection, authority.content_hash)
            if row is None:
                raise RunAuthorityError("RunAuthority admission could not be observed")
            return self._row_to_record(row), True

    def get(self, authority_hash: str) -> RunAuthorityRecord | None:
        _hash(authority_hash, "RunAuthority hash")
        with self._connect() as connection:
            row = self._select(connection, authority_hash)
        return None if row is None else self._row_to_record(row)

    def require_active(
        self,
        authority_hash: str,
        *,
        policy_digest: str,
        capabilities_hash: str,
        target_digest: str,
    ) -> RunAuthorityRecord:
        record = self.get(authority_hash)
        if record is None:
            raise RunAuthorityError("RunAuthority is unavailable")
        if record.state != "active":
            raise RunAuthorityInactive(f"RunAuthority is {record.state}")
        self._require_principal_current(record.authority.principal)
        now = self._now()
        if now < record.authority.issued_at_ms:
            raise RunAuthorityInactive("RunAuthority is not yet valid")
        if now >= record.authority.deadline_ms:
            raise RunAuthorityInactive("RunAuthority is expired")
        if policy_digest != record.authority.policy_digest:
            raise RunAuthorityStale("RunAuthority policy is stale")
        if capabilities_hash != record.authority.capabilities_hash:
            raise RunAuthorityStale("RunAuthority capabilities are stale")
        if target_digest != record.authority.target_digest:
            raise RunAuthorityStale("RunAuthority target is stale")
        return record

    def effect_active(self, authority_hash: str) -> bool:
        """Return whether the exact authority may still authorize a new effect."""
        try:
            record = self.get(authority_hash)
            if record is None or record.state != "active":
                return False
            self._require_principal_current(record.authority.principal)
            now = self._now()
            return record.authority.issued_at_ms <= now < record.authority.deadline_ms
        except RunAuthorityError:
            return False

    def claim_capsule(self, authority_hash: str, spec: object) -> RunAuthorityRecord:
        record = self.get(authority_hash)
        if record is None:
            raise RunAuthorityError("RunAuthority is unavailable")
        self._require_principal_current(record.authority.principal)
        record.authority.validate_capsule(spec)
        capsule_hash = _digest(spec.to_dict(), "Run Capsule claim")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, authority_hash)
            if row is None:
                raise RunAuthorityError("RunAuthority is unavailable")
            current = self._row_to_record(row)
            if current.state != "active":
                raise RunAuthorityInactive(f"RunAuthority is {current.state}")
            self._require_principal_current(current.authority.principal)
            if now < current.authority.issued_at_ms:
                raise RunAuthorityInactive("RunAuthority is not yet valid")
            if now >= current.authority.deadline_ms:
                raise RunAuthorityInactive("RunAuthority is expired")
            if current.claimed_capsule_hash is not None:
                if current.claimed_capsule_hash != capsule_hash:
                    raise RunAuthorityConflict(
                        "RunAuthority was replayed for another Capsule"
                    )
                return current
            connection.execute(
                (
                    "UPDATE run_authorities SET claimed_capsule_hash = ?, "
                    "updated_at_ms = ? WHERE authority_hash = ? "
                    "AND claimed_capsule_hash IS NULL"
                ),
                (capsule_hash, now, authority_hash),
            )
            row = self._select(connection, authority_hash)
            if row is None:
                raise RunAuthorityError(
                    "RunAuthority Capsule claim could not be observed"
                )
            return self._row_to_record(row)

    def narrow(
        self,
        parent_hash: str,
        child: RunAuthority,
    ) -> tuple[RunAuthorityRecord, bool]:
        """Atomically supersede one unclaimed active authority with a narrower child."""
        _hash(parent_hash, "parent RunAuthority hash")
        if type(child) is not RunAuthority:
            raise RunAuthorityError("narrowed RunAuthority is invalid")
        self._require_principal_current(child.principal)
        if child.parent_authority_hash != parent_hash:
            raise RunAuthorityConflict("narrowed RunAuthority parent binding changed")
        now = self._now()
        if now < child.issued_at_ms:
            raise RunAuthorityInactive("narrowed RunAuthority is not yet valid")
        if now >= child.deadline_ms:
            raise RunAuthorityInactive("narrowed RunAuthority is expired")

        encoded = _canonical(child.to_dict(), "narrowed RunAuthority").decode("utf-8")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent_row = self._select(connection, parent_hash)
            if parent_row is None:
                raise RunAuthorityError("parent RunAuthority is unavailable")
            parent = self._row_to_record(parent_row)

            existing_child_row = self._select(connection, child.content_hash)
            if existing_child_row is not None:
                existing_child = self._row_to_record(existing_child_row)
                if existing_child.authority != child:
                    raise RunAuthorityConflict("narrowed RunAuthority hash conflict")
                if parent.state != "superseded" or existing_child.state != "active":
                    raise RunAuthorityConflict(
                        "narrowed RunAuthority lineage state changed"
                    )
                return existing_child, False

            if parent.state != "active":
                raise RunAuthorityInactive(f"parent RunAuthority is {parent.state}")
            self._require_principal_current(parent.authority.principal)
            if parent.claimed_capsule_hash is not None:
                raise RunAuthorityConflict("claimed RunAuthority cannot be narrowed")
            _require_narrower(parent.authority, child)
            lineage_rows = connection.execute(
                """
                SELECT authority_hash, state FROM run_authorities
                WHERE (execution_id = ? OR idempotency_digest = ?)
                  AND authority_hash != ?
                """,
                (child.execution_id, child.idempotency_digest, parent_hash),
            ).fetchall()
            if any(state != "superseded" for _authority_hash, state in lineage_rows):
                raise RunAuthorityConflict(
                    "narrowed execution/idempotency identity conflicts with "
                    "authority lineage"
                )

            result = connection.execute(
                """
                UPDATE run_authorities
                SET state = 'superseded', state_generation = ?, updated_at_ms = ?
                WHERE authority_hash = ? AND state = 'active'
                  AND state_generation = ? AND claimed_capsule_hash IS NULL
                """,
                (
                    parent.state_generation + 1,
                    now,
                    parent_hash,
                    parent.state_generation,
                ),
            )
            if result.rowcount != 1:
                raise RunAuthorityConflict(
                    "parent RunAuthority changed while narrowing"
                )
            connection.execute(
                """
                INSERT INTO run_authorities(
                    authority_hash, execution_id, idempotency_digest, authority_json,
                    audit_hash, state, state_generation, claimed_capsule_hash,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, 'active', 1, NULL, ?, ?)
                """,
                (
                    child.content_hash,
                    child.execution_id,
                    child.idempotency_digest,
                    encoded,
                    child.audit_hash,
                    now,
                    now,
                ),
            )
            child_row = self._select(connection, child.content_hash)
            if child_row is None:
                raise RunAuthorityError("narrowed RunAuthority could not be observed")
            return self._row_to_record(child_row), True

    def cancel(self, authority_hash: str) -> RunAuthorityRecord:
        return self._transition(authority_hash, "cancelled")

    def revoke(self, authority_hash: str) -> RunAuthorityRecord:
        return self._transition(authority_hash, "revoked")

    def _transition(self, authority_hash: str, state: str) -> RunAuthorityRecord:
        _hash(authority_hash, "RunAuthority hash")
        if state not in {"cancelled", "revoked"}:
            raise RunAuthorityError("RunAuthority transition is invalid")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, authority_hash)
            if row is None:
                raise RunAuthorityError("RunAuthority is unavailable")
            current = self._row_to_record(row)
            if current.state == state:
                return current
            if current.state == "superseded":
                raise RunAuthorityInactive("superseded RunAuthority cannot transition")
            if current.state == "revoked":
                raise RunAuthorityInactive("revoked RunAuthority cannot transition")
            generation = current.state_generation + 1
            connection.execute(
                (
                    "UPDATE run_authorities SET state = ?, state_generation = ?, "
                    "updated_at_ms = ? WHERE authority_hash = ? "
                    "AND state_generation = ?"
                ),
                (state, generation, now, authority_hash, current.state_generation),
            )
            row = self._select(connection, authority_hash)
            if row is None:
                raise RunAuthorityError("RunAuthority transition could not be observed")
            return self._row_to_record(row)

    def _now(self) -> int:
        value = self._now_ms()
        return _positive_int(value, "RunAuthority store timestamp")

    def _prepare_file(self) -> None:
        parent_info = _require_nonsymlink_directory_components(
            self.path.parent,
            "RunAuthority store parent",
        )
        mode = stat.S_IMODE(parent_info.st_mode)
        if parent_info.st_uid != os.geteuid() or mode & 0o077 or mode & 0o700 != 0o700:
            raise RunAuthorityError("RunAuthority store parent permissions are unsafe")
        try:
            file_info = self.path.lstat()
        except FileNotFoundError:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError:
                file_info = self.path.lstat()
            else:
                try:
                    file_info = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as error:
            raise RunAuthorityError("RunAuthority store file is unsafe") from error
        if (
            stat.S_ISLNK(file_info.st_mode)
            or not stat.S_ISREG(file_info.st_mode)
            or file_info.st_uid != os.geteuid()
            or file_info.st_nlink != 1
        ):
            raise RunAuthorityError("RunAuthority store file is unsafe")
        os.chmod(self.path, 0o600, follow_symlinks=False)
        verified = self.path.lstat()
        if (
            stat.S_ISLNK(verified.st_mode)
            or not stat.S_ISREG(verified.st_mode)
            or verified.st_uid != os.geteuid()
            or verified.st_nlink != 1
            or stat.S_IMODE(verified.st_mode) != 0o600
        ):
            raise RunAuthorityError("RunAuthority store file is unsafe")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        self.path.chmod(0o600)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected = {"run_authority_schema", "run_authorities"}
        if tables and tables != expected:
            raise RunAuthorityError("RunAuthority store schema is not ready")
        if not tables:
            connection.execute(_SCHEMA_TABLE)
            connection.execute(_AUTHORITY_TABLE)
            connection.execute(
                "INSERT INTO run_authority_schema(schema_id) VALUES (?)",
                (STORE_SCHEMA,),
            )
        schema = connection.execute(
            "SELECT schema_id FROM run_authority_schema"
        ).fetchall()
        actual_sql = dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name IN ('run_authority_schema','run_authorities')"
            ).fetchall()
        )
        if (
            schema != [(STORE_SCHEMA,)]
            or set(actual_sql) != expected
            or _normalized_sql(actual_sql.get("run_authority_schema"))
            != _normalized_sql(_SCHEMA_TABLE)
            or _normalized_sql(actual_sql.get("run_authorities"))
            != _normalized_sql(_AUTHORITY_TABLE)
        ):
            raise RunAuthorityError("RunAuthority store schema is not ready")

    @staticmethod
    def _select(connection: sqlite3.Connection, authority_hash: str):
        return connection.execute(
            """
            SELECT authority_hash, execution_id, idempotency_digest, authority_json,
                   audit_hash, state, state_generation, claimed_capsule_hash,
                   created_at_ms, updated_at_ms
            FROM run_authorities WHERE authority_hash = ?
            """,
            (authority_hash,),
        ).fetchone()

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> RunAuthorityRecord:
        try:
            document = json.loads(row[3])
            authority = authority_from_dict(document)
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
            RunAuthorityError,
        ) as error:
            raise RunAuthorityError("persisted RunAuthority is invalid") from error
        if (
            authority.content_hash != row[0]
            or authority.execution_id != row[1]
            or authority.idempotency_digest != row[2]
        ):
            raise RunAuthorityError("persisted RunAuthority identity changed")
        if authority.audit_hash != row[4]:
            raise RunAuthorityError("persisted RunAuthority audit hash changed")
        return RunAuthorityRecord(
            authority=authority,
            state=row[5],
            state_generation=row[6],
            claimed_capsule_hash=row[7],
            created_at_ms=row[8],
            updated_at_ms=row[9],
        )


def _require_narrower(parent: RunAuthority, child: RunAuthority) -> None:
    fixed = (
        "execution_id",
        "idempotency_digest",
        "principal",
        "agent_instance_id",
        "recipe",
        "policy_digest",
        "capabilities_hash",
        "target",
        "target_digest",
        "issued_at_ms",
        "isolation",
        "remote_keryx_task_id",
    )
    if any(getattr(parent, name) != getattr(child, name) for name in fixed):
        raise RunAuthorityError("RunAuthority narrowing changed immutable identity")
    if child.deadline_ms > parent.deadline_ms:
        raise RunAuthorityError("RunAuthority narrowing widened deadline")
    for name in ("cpu_millis", "memory_bytes", "pids_limit", "max_iterations"):
        if getattr(child.resources, name) > getattr(parent.resources, name):
            raise RunAuthorityError("RunAuthority narrowing widened resources")
    if not set(child.toolsets).issubset(parent.toolsets):
        raise RunAuthorityError("RunAuthority narrowing widened toolsets")
    if child.approval_budget > parent.approval_budget:
        raise RunAuthorityError("RunAuthority narrowing widened approval budget")
    if not set(child.secret_refs).issubset(parent.secret_refs):
        raise RunAuthorityError("RunAuthority narrowing widened secret references")
    if not set(child.project_scope).issubset(parent.project_scope):
        raise RunAuthorityError("RunAuthority narrowing widened project scope")

    if child.network.mode != parent.network.mode:
        if child.network.mode != NETWORK_NONE:
            raise RunAuthorityError(
                "RunAuthority narrowing changed network mode unsafely"
            )
    else:
        parent_destinations = {
            json.dumps(item.to_dict(), sort_keys=True)
            for item in parent.network.destinations
        }
        child_destinations = {
            json.dumps(item.to_dict(), sort_keys=True)
            for item in child.network.destinations
        }
        if not child_destinations.issubset(parent_destinations):
            raise RunAuthorityError(
                "RunAuthority narrowing widened network destinations"
            )
        if child.network.approval_ref != parent.network.approval_ref:
            raise RunAuthorityError("RunAuthority narrowing changed network approval")

    parent_fs = {
        (
            item.project_id,
            item.relative_path,
            item.target,
            item.mode,
            item.write_authority_ref,
        ): item.max_bytes
        for item in parent.filesystem
    }
    for item in child.filesystem:
        key = (
            item.project_id,
            item.relative_path,
            item.target,
            item.mode,
            item.write_authority_ref,
        )
        if key not in parent_fs or item.max_bytes > parent_fs[key]:
            raise RunAuthorityError(
                "RunAuthority narrowing widened filesystem authority"
            )

    parent_artifacts = {
        (item.name, item.path): (item.max_bytes, item.scan_required)
        for item in parent.artifacts
    }
    for item in child.artifacts:
        previous = parent_artifacts.get((item.name, item.path))
        if (
            previous is None
            or item.max_bytes > previous[0]
            or (previous[1] and not item.scan_required)
        ):
            raise RunAuthorityError("RunAuthority narrowing widened artifact authority")

    parent_host = {
        (item.verb, item.target, item.parameters_digest): (
            item.max_calls,
            item.rate_limit_per_minute,
        )
        for item in parent.host_grants
    }
    for item in child.host_grants:
        previous = parent_host.get((item.verb, item.target, item.parameters_digest))
        if (
            previous is None
            or item.max_calls > previous[0]
            or item.rate_limit_per_minute > previous[1]
        ):
            raise RunAuthorityError("RunAuthority narrowing widened host authority")

    for child_values, parent_values, label in (
        (child.model_provider.providers, parent.model_provider.providers, "providers"),
        (child.model_provider.models, parent.model_provider.models, "models"),
    ):
        if parent_values and not child_values:
            raise RunAuthorityError(f"RunAuthority narrowing widened {label}")
        if parent_values and not set(child_values).issubset(parent_values):
            raise RunAuthorityError(f"RunAuthority narrowing widened {label}")


def _authority_from_dict(value: object) -> RunAuthority:
    if type(value) is not dict or value.get("schema") != AUTHORITY_SCHEMA:
        raise RunAuthorityError("RunAuthority document schema is invalid")
    required = {
        "schema",
        "execution_id",
        "idempotency_digest",
        "principal",
        "agent_instance_id",
        "recipe",
        "policy_digest",
        "capabilities_hash",
        "target",
        "target_digest",
        "plan_fingerprint",
        "issued_at_ms",
        "deadline_ms",
        "resources",
        "isolation",
        "network",
        "filesystem",
        "artifacts",
        "toolsets",
        "approval_budget",
        "secret_refs",
        "host_grants",
        "model_provider",
        "project_scope",
        "remote_keryx_task_id",
        "parent_authority_hash",
    }
    if set(value) != required:
        raise RunAuthorityError("RunAuthority document shape is invalid")
    recipe = _exact_object(
        value["recipe"],
        set(RecipeAuthorityBinding.__dataclass_fields__),
        "RunAuthority Recipe binding",
    )
    resources = _exact_object(
        value["resources"],
        set(ResourceAuthority.__dataclass_fields__),
        "RunAuthority resources",
    )
    isolation = _exact_object(
        value["isolation"],
        set(IsolationAuthority.__dataclass_fields__),
        "RunAuthority isolation",
    )
    network = _exact_object(
        value["network"],
        {"mode", "destinations", "approval_ref"},
        "RunAuthority network",
    )
    model_provider = _exact_object(
        value["model_provider"],
        set(ModelProviderAuthority.__dataclass_fields__),
        "RunAuthority model/provider constraints",
    )
    destinations = network["destinations"]
    filesystem = value["filesystem"]
    artifacts = value["artifacts"]
    host_grants = value["host_grants"]
    if (
        type(destinations) is not list
        or type(filesystem) is not list
        or type(artifacts) is not list
        or type(host_grants) is not list
        or type(value["toolsets"]) is not list
        or type(value["secret_refs"]) is not list
        or type(value["project_scope"]) is not list
    ):
        raise RunAuthorityError("RunAuthority collection is invalid")
    destinations = [
        _exact_object(
            item,
            {"host", "resolved_ips", "ports"},
            "RunAuthority network destination",
        )
        for item in destinations
    ]
    filesystem = [
        _exact_object(
            item,
            set(FilesystemAuthorityIntent.__dataclass_fields__),
            "RunAuthority filesystem intent",
        )
        for item in filesystem
    ]
    artifacts = [
        _exact_object(
            item,
            {"name", "path", "max_bytes", "scan_required"},
            "RunAuthority artifact grant",
        )
        for item in artifacts
    ]
    host_grants = [
        _exact_object(
            item,
            {
                "verb",
                "target",
                "parameters_digest",
                "max_calls",
                "rate_limit_per_minute",
            },
            "RunAuthority host grant",
        )
        for item in host_grants
    ]
    return RunAuthority(
        execution_id=value["execution_id"],
        idempotency_digest=value["idempotency_digest"],
        principal=PrincipalReference.from_dict(value["principal"]),
        agent_instance_id=value["agent_instance_id"],
        recipe=RecipeAuthorityBinding(
            **{key: recipe[key] for key in RecipeAuthorityBinding.__dataclass_fields__}
        ),
        policy_digest=value["policy_digest"],
        capabilities_hash=value["capabilities_hash"],
        target=value["target"],
        target_digest=value["target_digest"],
        plan_fingerprint=value["plan_fingerprint"],
        issued_at_ms=value["issued_at_ms"],
        deadline_ms=value["deadline_ms"],
        resources=ResourceAuthority(**resources),
        isolation=IsolationAuthority(**isolation),
        network=NetworkAuthorityIntent(
            mode=network.get("mode"),
            destinations=tuple(NetworkDestination(**item) for item in destinations),
            approval_ref=network.get("approval_ref"),
        ),
        filesystem=tuple(FilesystemAuthorityIntent(**item) for item in filesystem),
        artifacts=tuple(ArtifactExportGrant(**item) for item in artifacts),
        toolsets=tuple(value["toolsets"]),
        approval_budget=value["approval_budget"],
        secret_refs=tuple(value["secret_refs"]),
        host_grants=tuple(HostActionGrant(**item) for item in host_grants),
        model_provider=ModelProviderAuthority(**model_provider),
        project_scope=tuple(value["project_scope"]),
        remote_keryx_task_id=value["remote_keryx_task_id"],
        parent_authority_hash=value["parent_authority_hash"],
    )


def authority_from_dict(value: object) -> RunAuthority:
    """Decode one closed RunAuthority document behind Fleet-owned errors."""
    try:
        return _authority_from_dict(value)
    except RunAuthorityError:
        raise
    except Exception as error:
        raise RunAuthorityError("RunAuthority document is invalid") from error
