"""Durable temporary Run Capsule state and exact disposable-body recovery.

Phase 8 composes already-proven Fleet primitives. It deliberately does not mint
principal identity or RunAuthority. ``principal_id`` and ``run_authority_hash``
are opaque, already-verified references that later phases will formalize.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .backend_capabilities import BackendCapabilities
from .execution_backend import (
    BackendExecutionHandle,
    BackendExecutionState,
    ExecutionPlan,
)
from .host_action_broker import HostActionGrant
from .network_isolation import (
    NETWORK_NONE,
    NETWORK_PROVIDER_ONLY,
    NetworkDestination,
    NetworkGrant,
)
from .oci_backend import DockerWorkshopBackend, OciRealizationSpec
from .recipes import ResolvedRecipe
from .workspace_isolation import (
    ArtifactExportGrant,
    DockerWorkspaceIO,
    FilesystemAuthorityScope,
    FilesystemGrant,
    ProjectWorkspaceResolver,
)

_SCHEMA_ID = "fleet.run-capsule.v1"
_SPEC_SCHEMA_ID = "fleet.run-capsule-spec.v2"
_BACKEND_KIND = "fleet.dev/docker-oci"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$")
_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./_-]{0,254}@sha256:[0-9a-f]{64})$"
)
_MAX_JSON_BYTES = 512 * 1024
_MAX_SECRET_REFS = 64
_MAX_HOST_GRANTS = 64
_MAX_TOOLSETS = 32
_MAX_FILESYSTEM_GRANTS = 16
_MAX_ARTIFACT_GRANTS = 16

_STATES = (
    "admitted",
    "agent_ready",
    "body_ready",
    "run_submitting",
    "running",
    "terminal",
    "quiescent",
    "evidence_verified",
    "learning_persisted",
    "grants_revoked",
    "cleanup_pending",
    "cleaned",
    "finalized",
    "timed_out",
    "failed",
    "indeterminate",
)
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "admitted": frozenset({"agent_ready", "failed", "indeterminate"}),
    "agent_ready": frozenset({"body_ready", "failed", "indeterminate"}),
    "body_ready": frozenset(
        {"run_submitting", "failed", "indeterminate", "cleanup_pending"}
    ),
    "run_submitting": frozenset({"running", "failed", "indeterminate"}),
    "running": frozenset({"terminal", "timed_out", "failed", "indeterminate"}),
    "terminal": frozenset({"quiescent", "indeterminate"}),
    "quiescent": frozenset({"evidence_verified", "failed", "indeterminate"}),
    "evidence_verified": frozenset({"learning_persisted", "failed", "indeterminate"}),
    "learning_persisted": frozenset({"grants_revoked", "indeterminate"}),
    "grants_revoked": frozenset({"cleanup_pending", "indeterminate"}),
    "timed_out": frozenset({"quiescent", "indeterminate"}),
    "failed": frozenset({"quiescent", "indeterminate"}),
    "indeterminate": frozenset({"quiescent", "cleanup_pending"}),
    "cleanup_pending": frozenset({"cleaned", "indeterminate"}),
    "cleaned": frozenset({"finalized"}),
    "finalized": frozenset(),
}

_TABLE_SQL = f"""
CREATE TABLE run_capsules (
    execution_id TEXT PRIMARY KEY,
    capsule_hash TEXT NOT NULL,
    spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    state TEXT NOT NULL CHECK(state IN ({",".join(repr(item) for item in _STATES)})),
    container_id TEXT,
    hermes_run_id TEXT,
    evidence_json TEXT CHECK(evidence_json IS NULL OR json_valid(evidence_json)),
    grants_revoked INTEGER NOT NULL CHECK(grants_revoked IN (0,1)),
    learning_persisted INTEGER NOT NULL CHECK(learning_persisted IN (0,1)),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
)
"""
_SCHEMA_SQL = "CREATE TABLE run_capsule_schema (schema_id TEXT PRIMARY KEY)"


class RunCapsuleError(RuntimeError):
    """Run Capsule state is malformed, stale, unsafe, or unrecoverable."""


class RunCapsuleConflict(RunCapsuleError):
    """A generation or execution identity changed concurrently."""


class RunCapsuleIndeterminate(RunCapsuleError):
    """The exact run outcome or disposable body cannot be proven."""


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise RunCapsuleError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise RunCapsuleError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int = 1 << 63) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 < value <= maximum:
        raise RunCapsuleError(f"{label} is invalid")
    return value


def _canonical_json(value: object, label: str, maximum: int = _MAX_JSON_BYTES) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise RunCapsuleError(f"{label} is not canonical JSON") from error
    if len(encoded) > maximum:
        raise RunCapsuleError(f"{label} exceeds its byte bound")
    return encoded.decode("ascii")


def _digest(value: object, label: str) -> str:
    payload = _canonical_json(value, label).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _filesystem_to_dict(grant: FilesystemGrant) -> dict[str, object]:
    if type(grant) is not FilesystemGrant:
        raise RunCapsuleError("filesystem grant is invalid")
    return {
        "project_id": grant.project_id,
        "relative_path": grant.relative_path,
        "target": grant.target,
        "mode": grant.mode,
        "max_bytes": grant.max_bytes,
        "authority_ref": grant.authority_ref,
        "write_authority_ref": grant.write_authority_ref,
    }


def _artifact_to_dict(grant: ArtifactExportGrant) -> dict[str, object]:
    if type(grant) is not ArtifactExportGrant:
        raise RunCapsuleError("artifact grant is invalid")
    return {
        "name": grant.name,
        "path": grant.path,
        "max_bytes": grant.max_bytes,
        "scan_required": grant.scan_required,
    }


@dataclass(frozen=True, slots=True)
class RunCapsuleSpec:
    """Immutable security/lifecycle material for one exact temporary run."""

    execution_id: str
    idempotency_digest: str
    agent_instance_id: str
    principal_id: str
    recipe_hash: str
    resolved_recipe_hash: str
    recipe_compiler_version: str
    requirement_provenance_digest: str
    run_authority_hash: str
    capabilities_hash: str
    target: Mapping[str, Any]
    target_digest: str
    project_scope: tuple[str, ...]
    network_grant: NetworkGrant
    network_mode: str
    network_policy_hash: str
    toolsets: tuple[str, ...]
    approval_budget: int
    secret_refs: tuple[str, ...]
    filesystem_grants: tuple[FilesystemGrant, ...]
    artifact_grants: tuple[ArtifactExportGrant, ...]
    host_broker_grants: tuple[HostActionGrant, ...]
    cpu_millis: int
    memory_bytes: int
    pids_limit: int
    max_iterations: int
    deadline_ms: int
    image: str
    plan_fingerprint: str
    remote_keryx_task_id: str | None = None
    workflow_id: str | None = None
    workflow_revision: int | None = None
    workflow_hash: str | None = None
    workflow_step_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.execution_id, "Run Capsule execution id")
        _hash(self.idempotency_digest, "Run Capsule idempotency digest")
        _hash(self.agent_instance_id, "Run Capsule Agent Instance id")
        _identifier(self.principal_id, "Run Capsule principal id")
        for value, label in (
            (self.recipe_hash, "Run Capsule Recipe hash"),
            (self.resolved_recipe_hash, "Run Capsule ResolvedRecipe hash"),
            (
                self.requirement_provenance_digest,
                "Run Capsule requirement provenance digest",
            ),
            (self.run_authority_hash, "Run Capsule RunAuthority hash"),
            (self.capabilities_hash, "Run Capsule capabilities hash"),
            (self.target_digest, "Run Capsule target digest"),
            (self.network_policy_hash, "Run Capsule network policy hash"),
            (self.plan_fingerprint, "Run Capsule plan fingerprint"),
        ):
            _hash(value, label)
        _identifier(
            self.recipe_compiler_version,
            "Run Capsule Recipe compiler version",
        )
        workflow_values = (
            self.workflow_id,
            self.workflow_revision,
            self.workflow_hash,
            self.workflow_step_id,
        )
        if any(value is not None for value in workflow_values):
            if any(value is None for value in workflow_values):
                raise RunCapsuleError(
                    "Run Capsule Workflow binding must be complete when present"
                )
            _identifier(self.workflow_id, "Run Capsule Workflow id")
            _positive_int(self.workflow_revision, "Run Capsule Workflow revision")
            _hash(self.workflow_hash, "Run Capsule Workflow hash")
            _identifier(self.workflow_step_id, "Run Capsule Workflow step id")
        if not isinstance(self.target, Mapping):
            raise RunCapsuleError("Run Capsule target is invalid")
        target_document = dict(self.target)
        if _digest(target_document, "Run Capsule target") != self.target_digest:
            raise RunCapsuleError("Run Capsule target digest does not match target")
        object.__setattr__(self, "target", target_document)
        if type(self.network_grant) is not NetworkGrant:
            raise RunCapsuleError("Run Capsule network grant is invalid")
        if self.network_grant.mode != self.network_mode:
            raise RunCapsuleError("Run Capsule network mode does not match grant")
        if self.network_grant.authority_ref != self.run_authority_hash:
            raise RunCapsuleError(
                "Run Capsule network grant is bound to another authority"
            )
        if self.network_grant.policy_hash != self.network_policy_hash:
            raise RunCapsuleError("Run Capsule network policy hash changed")
        if self.network_mode not in {NETWORK_NONE, NETWORK_PROVIDER_ONLY}:
            raise RunCapsuleError(
                "Phase 8 local Capsule executor supports only "
                "offline/provider-only bodies"
            )
        if (
            type(self.project_scope) is not tuple
            or len(self.project_scope) > 64
            or len(set(self.project_scope)) != len(self.project_scope)
        ):
            raise RunCapsuleError("Run Capsule project scope is invalid")
        for item in self.project_scope:
            _identifier(item, "Run Capsule project scope")
        if (
            type(self.toolsets) is not tuple
            or not 0 < len(self.toolsets) <= _MAX_TOOLSETS
            or len(set(self.toolsets)) != len(self.toolsets)
            or self.toolsets != ("fleet-terminal",)
        ):
            raise RunCapsuleError("Run Capsule toolsets are invalid")
        if (
            isinstance(self.approval_budget, bool)
            or type(self.approval_budget) is not int
        ):
            raise RunCapsuleError("Run Capsule approval budget is invalid")
        if not 0 <= self.approval_budget <= 32:
            raise RunCapsuleError("Run Capsule approval budget is invalid")
        if (
            type(self.secret_refs) is not tuple
            or len(self.secret_refs) > _MAX_SECRET_REFS
            or len(set(self.secret_refs)) != len(self.secret_refs)
        ):
            raise RunCapsuleError("Run Capsule secret references are invalid")
        for item in self.secret_refs:
            _identifier(item, "Run Capsule secret reference")
        if (
            type(self.host_broker_grants) is not tuple
            or len(self.host_broker_grants) > _MAX_HOST_GRANTS
            or any(
                type(item) is not HostActionGrant for item in self.host_broker_grants
            )
        ):
            raise RunCapsuleError("Run Capsule host-broker grants are invalid")
        host_grant_keys = {
            (
                item.verb,
                item.target,
                item.parameters_digest,
                item.max_calls,
                item.rate_limit_per_minute,
            )
            for item in self.host_broker_grants
        }
        if len(host_grant_keys) != len(self.host_broker_grants):
            raise RunCapsuleError("Run Capsule host-broker grants contain duplicates")
        if (
            type(self.filesystem_grants) is not tuple
            or len(self.filesystem_grants) > _MAX_FILESYSTEM_GRANTS
        ):
            raise RunCapsuleError("Run Capsule filesystem grants are invalid")
        for grant in self.filesystem_grants:
            _filesystem_to_dict(grant)
            if grant.authority_ref != self.run_authority_hash:
                raise RunCapsuleError(
                    "filesystem grant is bound to another RunAuthority"
                )
        if (
            type(self.artifact_grants) is not tuple
            or len(self.artifact_grants) > _MAX_ARTIFACT_GRANTS
        ):
            raise RunCapsuleError("Run Capsule artifact grants are invalid")
        for grant in self.artifact_grants:
            _artifact_to_dict(grant)
        _positive_int(self.cpu_millis, "Run Capsule CPU limit", maximum=1_000_000)
        _positive_int(self.memory_bytes, "Run Capsule memory limit")
        _positive_int(self.pids_limit, "Run Capsule PID limit", maximum=65_535)
        _positive_int(self.max_iterations, "Run Capsule iteration limit", maximum=32)
        _positive_int(self.deadline_ms, "Run Capsule deadline")
        if type(self.image) is not str or _IMAGE_RE.fullmatch(self.image) is None:
            raise RunCapsuleError("Run Capsule image must be digest-pinned")
        if self.remote_keryx_task_id is not None:
            _identifier(self.remote_keryx_task_id, "Run Capsule Keryx task id")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SPEC_SCHEMA_ID,
            "execution_id": self.execution_id,
            "idempotency_digest": self.idempotency_digest,
            "agent_instance_id": self.agent_instance_id,
            "principal_id": self.principal_id,
            "recipe_hash": self.recipe_hash,
            "resolved_recipe_hash": self.resolved_recipe_hash,
            "recipe_compiler_version": self.recipe_compiler_version,
            "requirement_provenance_digest": self.requirement_provenance_digest,
            "workflow_id": self.workflow_id,
            "workflow_revision": self.workflow_revision,
            "workflow_hash": self.workflow_hash,
            "workflow_step_id": self.workflow_step_id,
            "run_authority_hash": self.run_authority_hash,
            "capabilities_hash": self.capabilities_hash,
            "target": dict(self.target),
            "target_digest": self.target_digest,
            "project_scope": list(self.project_scope),
            "network_grant": self.network_grant.to_dict(),
            "network_mode": self.network_mode,
            "network_policy_hash": self.network_policy_hash,
            "toolsets": list(self.toolsets),
            "approval_budget": self.approval_budget,
            "secret_refs": list(self.secret_refs),
            "filesystem_grants": [
                _filesystem_to_dict(item) for item in self.filesystem_grants
            ],
            "artifact_grants": [
                _artifact_to_dict(item) for item in self.artifact_grants
            ],
            "host_broker_grants": [item.to_dict() for item in self.host_broker_grants],
            "resources": {
                "cpu_millis": self.cpu_millis,
                "memory_bytes": self.memory_bytes,
                "pids_limit": self.pids_limit,
                "max_iterations": self.max_iterations,
            },
            "deadline_ms": self.deadline_ms,
            "image": self.image,
            "plan_fingerprint": self.plan_fingerprint,
            "remote_keryx_task_id": self.remote_keryx_task_id,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "Run Capsule spec")


@dataclass(frozen=True, slots=True)
class RunCapsuleRecord:
    spec: RunCapsuleSpec
    generation: int
    state: str
    container_id: str | None
    hermes_run_id: str | None
    evidence: Mapping[str, Any] | None
    grants_revoked: bool
    learning_persisted: bool
    created_at_ms: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        if type(self.spec) is not RunCapsuleSpec:
            raise RunCapsuleError("Run Capsule record spec is invalid")
        _positive_int(self.generation, "Run Capsule generation")
        if self.state not in _STATES:
            raise RunCapsuleError("Run Capsule state is invalid")
        if self.container_id is not None and (
            type(self.container_id) is not str
            or _CONTAINER_ID_RE.fullmatch(self.container_id) is None
        ):
            raise RunCapsuleError("Run Capsule container id is invalid")
        if self.hermes_run_id is not None:
            _identifier(self.hermes_run_id, "Run Capsule Hermes run id")
        if self.evidence is not None:
            _canonical_json(dict(self.evidence), "Run Capsule evidence")
        if (
            type(self.grants_revoked) is not bool
            or type(self.learning_persisted) is not bool
        ):
            raise RunCapsuleError("Run Capsule durable flags are invalid")
        _positive_int(self.created_at_ms, "Run Capsule created timestamp")
        _positive_int(self.updated_at_ms, "Run Capsule updated timestamp")
        if self.updated_at_ms < self.created_at_ms:
            raise RunCapsuleError("Run Capsule timestamps are invalid")
        container_states = {
            "body_ready",
            "run_submitting",
            "running",
            "terminal",
            "quiescent",
            "evidence_verified",
            "learning_persisted",
            "grants_revoked",
            "cleanup_pending",
        }
        if self.state in container_states and self.container_id is None:
            raise RunCapsuleError("Run Capsule state requires an exact container")
        if self.state in {"running", "terminal"} and self.hermes_run_id is None:
            raise RunCapsuleError("Run Capsule state requires an exact Hermes run")
        revoked_states = {"grants_revoked", "cleanup_pending", "cleaned", "finalized"}
        if self.state in revoked_states and not self.grants_revoked:
            raise RunCapsuleError("Run Capsule cleanup state requires revoked grants")
        learning_states = revoked_states | {"learning_persisted"}
        if self.state in learning_states and not self.learning_persisted:
            raise RunCapsuleError(
                "Run Capsule cleanup state requires persisted learning"
            )

    @property
    def capsule_hash(self) -> str:
        return self.spec.content_hash


class RunCapsuleStore:
    """SQLite CAS store for temporary Fleet-owned Capsule recovery state."""

    def __init__(
        self,
        path: str | Path,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.name:
            raise RunCapsuleError("Run Capsule store path must be absolute")
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        if not callable(self._now_ms):
            raise RunCapsuleError("Run Capsule store clock is invalid")
        self._prepare_file()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._initialize(connection)

    def admit(self, spec: RunCapsuleSpec) -> tuple[RunCapsuleRecord, bool]:
        if type(spec) is not RunCapsuleSpec:
            raise RunCapsuleError("Run Capsule spec is invalid")
        now = self._now()
        spec_json = _canonical_json(spec.to_dict(), "Run Capsule spec")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, spec.execution_id)
            if row is not None:
                if row[1] != spec.content_hash:
                    raise RunCapsuleConflict("Run Capsule replay identity changed")
                try:
                    record = self._row_to_record(row, spec=spec)
                except RunCapsuleConflict as error:
                    raise RunCapsuleConflict(
                        "Run Capsule replay identity changed"
                    ) from error
                return record, False
            connection.execute(
                """
                INSERT INTO run_capsules(
                    execution_id, capsule_hash, spec_json, generation, state,
                    container_id, hermes_run_id, evidence_json, grants_revoked,
                    learning_persisted, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, 1, 'admitted', NULL, NULL, NULL, 0, 0, ?, ?)
                """,
                (spec.execution_id, spec.content_hash, spec_json, now, now),
            )
            row = self._select(connection, spec.execution_id)
            if row is None:
                raise RunCapsuleError("Run Capsule admission could not be observed")
            return self._row_to_record(row, spec=spec), True

    def get(self, execution_id: str) -> RunCapsuleRecord | None:
        _identifier(execution_id, "Run Capsule execution id")
        with self._connect() as connection:
            row = self._select(connection, execution_id)
        return None if row is None else self._row_to_record(row)

    def require_exact(self, spec: RunCapsuleSpec) -> RunCapsuleRecord:
        record = self.get(spec.execution_id)
        if record is None:
            raise RunCapsuleError("Run Capsule record is unavailable")
        if record.capsule_hash != spec.content_hash:
            raise RunCapsuleConflict("Run Capsule replay identity changed")
        return record

    def transition(
        self,
        spec: RunCapsuleSpec,
        *,
        expected_generation: int,
        state: str,
        container_id: str | None = None,
        hermes_run_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
        grants_revoked: bool | None = None,
        learning_persisted: bool | None = None,
    ) -> RunCapsuleRecord:
        if state not in _STATES:
            raise RunCapsuleError("Run Capsule transition state is invalid")
        _positive_int(expected_generation, "Run Capsule expected generation")
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, spec.execution_id)
            if row is None:
                raise RunCapsuleError("Run Capsule record is unavailable")
            current = self._row_to_record(row, spec=spec)
            if current.capsule_hash != spec.content_hash:
                raise RunCapsuleConflict("Run Capsule replay identity changed")
            if current.generation != expected_generation:
                raise RunCapsuleConflict("Run Capsule generation changed")
            if state not in _ALLOWED_TRANSITIONS[current.state]:
                raise RunCapsuleError(
                    f"Run Capsule transition {current.state}->{state} is invalid"
                )
            next_record = replace(
                current,
                generation=current.generation + 1,
                state=state,
                container_id=(
                    current.container_id if container_id is None else container_id
                ),
                hermes_run_id=(
                    current.hermes_run_id if hermes_run_id is None else hermes_run_id
                ),
                evidence=(current.evidence if evidence is None else dict(evidence)),
                grants_revoked=(
                    current.grants_revoked if grants_revoked is None else grants_revoked
                ),
                learning_persisted=(
                    current.learning_persisted
                    if learning_persisted is None
                    else learning_persisted
                ),
                updated_at_ms=now,
            )
            evidence_json = (
                None
                if next_record.evidence is None
                else _canonical_json(
                    dict(next_record.evidence),
                    "Run Capsule evidence",
                )
            )
            cursor = connection.execute(
                """
                UPDATE run_capsules
                SET generation = ?, state = ?, container_id = ?, hermes_run_id = ?,
                    evidence_json = ?, grants_revoked = ?, learning_persisted = ?,
                    updated_at_ms = ?
                WHERE execution_id = ? AND generation = ? AND capsule_hash = ?
                """,
                (
                    next_record.generation,
                    next_record.state,
                    next_record.container_id,
                    next_record.hermes_run_id,
                    evidence_json,
                    int(next_record.grants_revoked),
                    int(next_record.learning_persisted),
                    now,
                    spec.execution_id,
                    expected_generation,
                    spec.content_hash,
                ),
            )
            if cursor.rowcount != 1:
                raise RunCapsuleConflict("Run Capsule transition lost its CAS race")
            return next_record

    def list_unfinalized(self) -> tuple[RunCapsuleRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_capsules WHERE state != 'finalized' "
                "ORDER BY created_at_ms, execution_id"
            ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    def _prepare_file(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if parent.is_symlink() or not parent.is_dir():
            raise RunCapsuleError("Run Capsule store parent is unsafe")
        parent_info = parent.stat()
        if (
            parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise RunCapsuleError("Run Capsule store parent permissions are unsafe")
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise RunCapsuleError("Run Capsule store file is unsafe")
            if info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise RunCapsuleError("Run Capsule store ownership is unsafe")
            self.path.chmod(0o600)

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
        expected = {"run_capsule_schema", "run_capsules"}
        if tables and tables != expected:
            raise RunCapsuleError("Run Capsule store schema is not ready")
        if not tables:
            connection.execute(_SCHEMA_SQL)
            connection.execute(_TABLE_SQL)
            connection.execute(
                "INSERT INTO run_capsule_schema(schema_id) VALUES (?)",
                (_SCHEMA_ID,),
            )
        schema = connection.execute(
            "SELECT schema_id FROM run_capsule_schema"
        ).fetchall()
        if schema != [(_SCHEMA_ID,)]:
            raise RunCapsuleError("Run Capsule store schema is not ready")

    @staticmethod
    def _select(
        connection: sqlite3.Connection,
        execution_id: str,
    ) -> tuple[Any, ...] | None:
        return connection.execute(
            """
            SELECT execution_id, capsule_hash, spec_json, generation, state,
                   container_id, hermes_run_id, evidence_json, grants_revoked,
                   learning_persisted, created_at_ms, updated_at_ms
            FROM run_capsules WHERE execution_id = ?
            """,
            (execution_id,),
        ).fetchone()

    def _row_to_record(
        self, row: tuple[Any, ...], *, spec: RunCapsuleSpec | None = None
    ) -> RunCapsuleRecord:
        stored_spec = json.loads(row[2])
        if spec is None:
            spec = self._spec_from_dict(stored_spec)
        elif stored_spec != spec.to_dict():
            raise RunCapsuleConflict("Run Capsule persisted spec changed")
        if row[1] != spec.content_hash:
            raise RunCapsuleConflict("Run Capsule persisted hash changed")
        evidence = None if row[7] is None else json.loads(row[7])
        if evidence is not None and type(evidence) is not dict:
            raise RunCapsuleError("Run Capsule evidence is invalid")
        return RunCapsuleRecord(
            spec=spec,
            generation=row[3],
            state=row[4],
            container_id=row[5],
            hermes_run_id=row[6],
            evidence=evidence,
            grants_revoked=bool(row[8]),
            learning_persisted=bool(row[9]),
            created_at_ms=row[10],
            updated_at_ms=row[11],
        )

    @staticmethod
    def _spec_from_dict(value: object) -> RunCapsuleSpec:
        if type(value) is not dict:
            raise RunCapsuleError("Run Capsule persisted spec is invalid")
        expected_keys = {
            "schema",
            "execution_id",
            "idempotency_digest",
            "agent_instance_id",
            "principal_id",
            "recipe_hash",
            "resolved_recipe_hash",
            "recipe_compiler_version",
            "requirement_provenance_digest",
            "workflow_id",
            "workflow_revision",
            "workflow_hash",
            "workflow_step_id",
            "run_authority_hash",
            "capabilities_hash",
            "target",
            "target_digest",
            "project_scope",
            "network_grant",
            "network_mode",
            "network_policy_hash",
            "toolsets",
            "approval_budget",
            "secret_refs",
            "filesystem_grants",
            "artifact_grants",
            "host_broker_grants",
            "resources",
            "deadline_ms",
            "image",
            "plan_fingerprint",
            "remote_keryx_task_id",
        }
        if set(value) != expected_keys:
            raise RunCapsuleError(
                "Run Capsule persisted spec shape is legacy, incomplete, or unknown"
            )
        if value["schema"] != _SPEC_SCHEMA_ID:
            raise RunCapsuleError("Run Capsule persisted spec schema is unsupported")
        resources = value.get("resources")
        if type(resources) is not dict:
            raise RunCapsuleError("Run Capsule persisted resources are invalid")
        filesystem = value.get("filesystem_grants")
        artifacts = value.get("artifact_grants")
        network = value.get("network_grant")
        host_grants = value.get("host_broker_grants")
        if (
            type(filesystem) is not list
            or type(artifacts) is not list
            or type(network) is not dict
            or type(host_grants) is not list
        ):
            raise RunCapsuleError("Run Capsule persisted grants are invalid")
        destinations = network.get("destinations")
        if type(destinations) is not list:
            raise RunCapsuleError("Run Capsule persisted network grant is invalid")
        network_grant = NetworkGrant(
            mode=network.get("mode"),
            authority_ref=network.get("authority_ref"),
            destinations=tuple(
                NetworkDestination(
                    host=item["host"],
                    resolved_ips=tuple(item["resolved_ips"]),
                    ports=tuple(item["ports"]),
                )
                for item in destinations
            ),
            approval_ref=network.get("approval_ref"),
        )
        return RunCapsuleSpec(
            execution_id=value["execution_id"],
            idempotency_digest=value["idempotency_digest"],
            agent_instance_id=value["agent_instance_id"],
            principal_id=value["principal_id"],
            recipe_hash=value["recipe_hash"],
            resolved_recipe_hash=value["resolved_recipe_hash"],
            recipe_compiler_version=value["recipe_compiler_version"],
            requirement_provenance_digest=value["requirement_provenance_digest"],
            run_authority_hash=value["run_authority_hash"],
            capabilities_hash=value["capabilities_hash"],
            target=value["target"],
            target_digest=value["target_digest"],
            project_scope=tuple(value["project_scope"]),
            network_grant=network_grant,
            network_mode=value["network_mode"],
            network_policy_hash=value["network_policy_hash"],
            toolsets=tuple(value["toolsets"]),
            approval_budget=value["approval_budget"],
            secret_refs=tuple(value["secret_refs"]),
            filesystem_grants=tuple(FilesystemGrant(**item) for item in filesystem),
            artifact_grants=tuple(ArtifactExportGrant(**item) for item in artifacts),
            host_broker_grants=tuple(HostActionGrant(**item) for item in host_grants),
            cpu_millis=resources["cpu_millis"],
            memory_bytes=resources["memory_bytes"],
            pids_limit=resources["pids_limit"],
            max_iterations=resources["max_iterations"],
            deadline_ms=value["deadline_ms"],
            image=value["image"],
            plan_fingerprint=value["plan_fingerprint"],
            remote_keryx_task_id=value["remote_keryx_task_id"],
            workflow_id=value["workflow_id"],
            workflow_revision=value["workflow_revision"],
            workflow_hash=value["workflow_hash"],
            workflow_step_id=value["workflow_step_id"],
        )

    def _now(self) -> int:
        value = self._now_ms()
        return _positive_int(value, "Run Capsule store timestamp")


class DockerRunCapsuleBody:
    """Exact Phase 8 disposable body. Initial creation and recovery are separate."""

    def __init__(
        self,
        *,
        capabilities: BackendCapabilities,
        resolved_recipe: ResolvedRecipe,
        spec: RunCapsuleSpec,
        project_roots: Mapping[str, Path] | None = None,
        forbidden_roots: tuple[Path, ...] = (),
        write_authority_hashes: tuple[str, ...] = (),
        workspace_io: DockerWorkspaceIO | None = None,
        artifact_scanner: Callable[[bytes, ArtifactExportGrant], bool] | None = None,
        backend_factory: Callable[..., DockerWorkshopBackend] = DockerWorkshopBackend,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if type(capabilities) is not BackendCapabilities:
            raise RunCapsuleError("Run Capsule capabilities are invalid")
        if type(resolved_recipe) is not ResolvedRecipe:
            raise RunCapsuleError("Run Capsule Recipe is invalid")
        if type(spec) is not RunCapsuleSpec:
            raise RunCapsuleError("Run Capsule spec is invalid")
        if resolved_recipe.recipe_hash != spec.recipe_hash:
            raise RunCapsuleError("Run Capsule Recipe identity changed")
        if resolved_recipe.content_hash != spec.resolved_recipe_hash:
            raise RunCapsuleError("Run Capsule ResolvedRecipe identity changed")
        if capabilities.content_hash != spec.capabilities_hash:
            raise RunCapsuleError("Run Capsule capabilities changed")
        if not callable(backend_factory):
            raise RunCapsuleError("Run Capsule backend factory is invalid")
        self._capabilities = capabilities
        self._recipe = resolved_recipe
        self._spec = spec
        if spec.filesystem_grants:
            self._resolver: ProjectWorkspaceResolver | None = ProjectWorkspaceResolver(
                project_roots or {}, forbidden_paths=forbidden_roots
            )
        else:
            self._resolver = None
        self._filesystem_authority = FilesystemAuthorityScope(
            run_authority_hash=spec.run_authority_hash,
            write_authority_hashes=write_authority_hashes,
        )
        self._workspace = workspace_io or DockerWorkspaceIO()
        self._scanner = artifact_scanner
        self._backend_factory = backend_factory
        self._now_ms = now_ms

    @property
    def plan(self) -> ExecutionPlan:
        plan = ExecutionPlan(
            execution_id=self._spec.execution_id,
            idempotency_key=self._spec.idempotency_digest,
            resolved_recipe=self._recipe,
            required_capabilities_hash=self._spec.capabilities_hash,
        )
        if plan.fingerprint != self._spec.plan_fingerprint:
            raise RunCapsuleError("Run Capsule plan fingerprint changed")
        return plan

    def create_initial(self) -> BackendExecutionHandle:
        """Create/start only on the initial path.

        Declared filesystem grants are projected only after the exact body is
        observed running.
        """
        backend = self._backend()
        handle = backend.ensure(self.plan)
        if handle.state != BackendExecutionState.RUNNING:
            raise RunCapsuleError("Run Capsule body did not enter running state")
        try:
            if self._spec.filesystem_grants:
                if self._resolver is None:
                    raise RunCapsuleError(
                        "Run Capsule filesystem resolver is unavailable"
                    )
                resolved = self._resolver.resolve(
                    self._spec.filesystem_grants,
                    authority=self._filesystem_authority,
                )
                for grant in resolved:
                    self._workspace.stage(handle.realization_id, grant)
        except Exception:
            try:
                backend.cleanup_plan(self.plan, handle=handle)
            except Exception:
                pass
            raise
        return handle

    def find_existing_by_plan(self) -> BackendExecutionHandle | None:
        """Find an exact plan-owned body without create/ensure fallback."""
        return self._backend().find(self.plan)

    def recover_exact(
        self,
        expected_container_id: str,
    ) -> BackendExecutionHandle:
        """Find the exact existing body without create/ensure fallback."""
        if _CONTAINER_ID_RE.fullmatch(expected_container_id) is None:
            raise RunCapsuleError("Run Capsule recovery container id is invalid")
        handle = self._backend().find(self.plan)
        if handle is None:
            raise RunCapsuleIndeterminate("exact Run Capsule body is missing")
        if handle.realization_id != expected_container_id:
            raise RunCapsuleIndeterminate("Run Capsule body identity changed")
        return handle

    def export_artifacts(self, container_id: str) -> dict[str, bytes]:
        self.recover_exact(container_id)
        return self._workspace.export_declared(
            container_id,
            self._spec.artifact_grants,
            scanner=self._scanner,
        )

    def cleanup_exact(self, container_id: str) -> None:
        handle = self.recover_exact(container_id)
        backend = self._backend()
        backend.cleanup_plan(self.plan, handle=handle)
        if backend.find(self.plan) is not None:
            raise RunCapsuleIndeterminate("Run Capsule body cleanup is unproven")

    def cleanup_if_present(self, container_id: str) -> None:
        """Idempotently clean exact recovery state without replacement."""
        if _CONTAINER_ID_RE.fullmatch(container_id) is None:
            raise RunCapsuleError("Run Capsule cleanup container id is invalid")
        backend = self._backend()
        handle = backend.find(self.plan)
        if handle is None:
            return
        if handle.realization_id != container_id:
            raise RunCapsuleIndeterminate("Run Capsule cleanup identity changed")
        backend.cleanup_plan(self.plan, handle=handle)
        if backend.find(self.plan) is not None:
            raise RunCapsuleIndeterminate("Run Capsule body cleanup is unproven")

    def _backend(self) -> DockerWorkshopBackend:
        realization = OciRealizationSpec(
            image=self._spec.image,
            argv=("sleep", "infinity"),
            network="none",
            cpu_millis=self._spec.cpu_millis,
            memory_bytes=self._spec.memory_bytes,
            pids_limit=self._spec.pids_limit,
        )
        backend = self._backend_factory(
            capabilities=self._capabilities,
            realization=realization,
            deadline_ms=self._spec.deadline_ms,
            now_ms=self._now_ms,
        )
        if not isinstance(backend, DockerWorkshopBackend):
            raise RunCapsuleError(
                "Run Capsule backend factory returned invalid backend"
            )
        return backend
