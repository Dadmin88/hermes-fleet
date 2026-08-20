"""Phase 19 deterministic, authority-free security event facts.

Security events are immutable/versioned inputs for later Templar evaluation.
They do not make an authorization decision. Deterministic hard denies are
represented by a separate object so a fact record can never masquerade as
execution authority or as a Templar verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Final

from .network_isolation import (
    NETWORK_EXPLICIT_INTERNET,
    NETWORK_NONE,
    NETWORK_PROJECT_ALLOWLIST,
    NETWORK_PROVIDER_ONLY,
    NetworkDestination,
    NetworkGrant,
)
from .principal_identity import PrincipalReference
from .run_authority import RunAuthority

SECURITY_REQUEST_SCHEMA: Final[str] = "fleet.security-request.v1"
SECURITY_EVENT_SCHEMA: Final[str] = "fleet.security-event.v1"
HARD_DENY_SCHEMA: Final[str] = "fleet.security-hard-deny.v1"

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_JSON_BYTES = 512 * 1024
_MAX_ITEMS = 128
_MAX_REASON_CODES = 64
_MAX_DETECTED_KINDS = 32
_MAX_COUNT = 1_000_000

_RISK_LEVELS = frozenset({"info", "low", "medium", "high", "critical"})
_SECRET_ACTIONS = frozenset(
    {"none", "redacted", "blocked", "vault-referenced", "failed-closed"}
)
_QUARANTINE_STATES = frozenset({"rejected", "needs-review", "verification-ready"})
_VERIFICATION_STATES = frozenset({"not-run", "verified", "failed"})


class SecurityEventError(RuntimeError):
    """A security event fact set is malformed, ambiguous, or not safely bound."""


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
        raise SecurityEventError(f"{label} is not canonical JSON") from error
    if len(payload) > _MAX_JSON_BYTES:
        raise SecurityEventError(f"{label} exceeds the supported bound")
    return payload


def _digest(value: object, label: str) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value, label)).hexdigest()


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise SecurityEventError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise SecurityEventError(f"{label} is invalid")
    return value


def _code(value: object, label: str) -> str:
    if type(value) is not str or _CODE_RE.fullmatch(value) is None:
        raise SecurityEventError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= maximum:
        raise SecurityEventError(f"{label} is invalid")
    return value


def _nonnegative_int(value: object, label: str, *, maximum: int = _MAX_COUNT) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 <= value <= maximum:
        raise SecurityEventError(f"{label} is invalid")
    return value


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise SecurityEventError(f"{label} has an invalid closed schema")
    return value


def _codes(
    value: object,
    label: str,
    *,
    maximum: int = _MAX_REASON_CODES,
) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or len(value) > maximum:
        raise SecurityEventError(f"{label} is invalid")
    normalized = tuple(_code(item, label) for item in value)
    if len(normalized) != len(set(normalized)):
        raise SecurityEventError(f"{label} contains duplicates")
    return tuple(sorted(normalized))


def _identifiers(
    value: object,
    label: str,
    *,
    maximum: int = _MAX_ITEMS,
) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or len(value) > maximum:
        raise SecurityEventError(f"{label} is invalid")
    normalized = tuple(_identifier(item, label) for item in value)
    if len(normalized) != len(set(normalized)):
        raise SecurityEventError(f"{label} contains duplicates")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class SecurityRecipeBinding:
    """Exact Recipe/ResolvedRecipe identity projected from RunAuthority."""

    recipe_hash: str
    resolved_recipe_hash: str
    compiler_version: str
    provenance_digest: str
    workflow_hash: str | None = None
    workflow_step_id: str | None = None

    def __post_init__(self) -> None:
        _hash(self.recipe_hash, "security Recipe hash")
        _hash(self.resolved_recipe_hash, "security ResolvedRecipe hash")
        _identifier(self.compiler_version, "security Recipe compiler version")
        _hash(self.provenance_digest, "security Recipe provenance digest")
        if (self.workflow_hash is None) != (self.workflow_step_id is None):
            raise SecurityEventError("security Workflow binding must be complete")
        if self.workflow_hash is not None:
            _hash(self.workflow_hash, "security Workflow hash")
            _identifier(self.workflow_step_id, "security Workflow step id")

    def to_dict(self) -> dict[str, object]:
        return {
            "recipe_hash": self.recipe_hash,
            "resolved_recipe_hash": self.resolved_recipe_hash,
            "compiler_version": self.compiler_version,
            "provenance_digest": self.provenance_digest,
            "workflow_hash": self.workflow_hash,
            "workflow_step_id": self.workflow_step_id,
        }


@dataclass(frozen=True, slots=True)
class SecurityTargetBinding:
    """Exact immutable RunAuthority target document plus its authoritative digest."""

    target_digest: str
    target_canonical: bytes

    def __post_init__(self) -> None:
        _hash(self.target_digest, "security target digest")
        if type(self.target_canonical) is not bytes:
            raise SecurityEventError("security target document is invalid")
        try:
            target = json.loads(self.target_canonical)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SecurityEventError("security target document is invalid") from error
        if type(target) is not dict:
            raise SecurityEventError("security target document is invalid")
        canonical = _canonical(target, "security target")
        if canonical != self.target_canonical:
            raise SecurityEventError("security target document is not canonical")
        if _digest(target, "security target") != self.target_digest:
            raise SecurityEventError("security target digest does not match target")
        _identifier(target.get("source"), "security target source")
        _identifier(target.get("node_id"), "security target node id")
        _positive_int(target.get("generation"), "security target generation")

    @classmethod
    def from_target(
        cls,
        *,
        target_digest: str,
        target: object,
    ) -> SecurityTargetBinding:
        if type(target) is not dict:
            raise SecurityEventError("security target document is invalid")
        return cls(
            target_digest=target_digest,
            target_canonical=_canonical(target, "security target"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "target_digest": self.target_digest,
            "target": json.loads(self.target_canonical),
        }


@dataclass(frozen=True, slots=True)
class SecurityResourceFacts:
    cpu_millis: int
    memory_bytes: int
    pids_limit: int
    max_iterations: int
    deadline_ms: int

    def __post_init__(self) -> None:
        _positive_int(self.cpu_millis, "security CPU limit", maximum=1_000_000)
        _positive_int(self.memory_bytes, "security memory limit")
        _positive_int(self.pids_limit, "security PID limit", maximum=65_535)
        _positive_int(self.max_iterations, "security iteration limit", maximum=32)
        _positive_int(self.deadline_ms, "security deadline")

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_millis": self.cpu_millis,
            "memory_bytes": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "max_iterations": self.max_iterations,
            "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True, slots=True)
class SecurityNetworkDestination:
    host: str
    resolved_ips: tuple[str, ...]
    ports: tuple[int, ...]

    def __post_init__(self) -> None:
        try:
            destination = NetworkDestination(
                host=self.host,
                resolved_ips=tuple(self.resolved_ips),
                ports=tuple(self.ports),
            )
        except Exception as error:
            raise SecurityEventError(
                "security network destination is invalid"
            ) from error
        object.__setattr__(self, "host", destination.host)
        object.__setattr__(self, "resolved_ips", destination.resolved_ips)
        object.__setattr__(self, "ports", destination.ports)

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "resolved_ips": list(self.resolved_ips),
            "ports": list(self.ports),
        }


@dataclass(frozen=True, slots=True)
class SecurityNetworkFacts:
    mode: str
    policy_hash: str
    destinations: tuple[SecurityNetworkDestination, ...] = ()
    approval_ref: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {
            NETWORK_NONE,
            NETWORK_PROVIDER_ONLY,
            NETWORK_PROJECT_ALLOWLIST,
            NETWORK_EXPLICIT_INTERNET,
        }:
            raise SecurityEventError("security network mode is invalid")
        _hash(self.policy_hash, "security network policy hash")
        destinations = tuple(self.destinations)
        if len(destinations) > 32 or any(
            type(item) is not SecurityNetworkDestination for item in destinations
        ):
            raise SecurityEventError("security network destinations are invalid")
        keys = [(item.host, item.resolved_ips, item.ports) for item in destinations]
        if len(keys) != len(set(keys)):
            raise SecurityEventError("security network destinations contain duplicates")
        object.__setattr__(
            self,
            "destinations",
            tuple(sorted(destinations, key=lambda item: (item.host, item.ports))),
        )
        if self.approval_ref is not None:
            _hash(self.approval_ref, "security network approval reference")
        if self.mode in {NETWORK_NONE, NETWORK_PROVIDER_ONLY}:
            if destinations or self.approval_ref is not None:
                raise SecurityEventError(
                    "offline security network facts are inconsistent"
                )
        elif self.mode == NETWORK_PROJECT_ALLOWLIST:
            if not destinations or self.approval_ref is not None:
                raise SecurityEventError(
                    "project allowlist security network facts are inconsistent"
                )
        elif not destinations or self.approval_ref is None:
            raise SecurityEventError(
                "explicit internet security network facts are inconsistent"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "policy_hash": self.policy_hash,
            "destinations": [item.to_dict() for item in self.destinations],
            "approval_ref": self.approval_ref,
        }


@dataclass(frozen=True, slots=True)
class MemorySkillRisk:
    """Bounded deterministic risk facts with no persisted content body."""

    subject_kind: str
    subject_hash: str
    scope_kind: str
    risk_level: str
    signal_codes: tuple[str, ...]
    evidence_hash: str

    def __post_init__(self) -> None:
        if self.subject_kind not in {"memory", "skill"}:
            raise SecurityEventError("memory/skill risk subject kind is invalid")
        _hash(self.subject_hash, "memory/skill subject hash")
        _identifier(self.scope_kind, "memory/skill scope kind")
        if self.risk_level not in _RISK_LEVELS:
            raise SecurityEventError("memory/skill risk level is invalid")
        object.__setattr__(
            self,
            "signal_codes",
            _codes(self.signal_codes, "memory/skill risk signal"),
        )
        _hash(self.evidence_hash, "memory/skill risk evidence hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "subject_kind": self.subject_kind,
            "subject_hash": self.subject_hash,
            "scope_kind": self.scope_kind,
            "risk_level": self.risk_level,
            "signal_codes": list(self.signal_codes),
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class SecretInterceptionFact:
    """Only classification/count/action evidence; never the intercepted value."""

    source_kind: str
    detected_kinds: tuple[str, ...]
    detected_count: int
    action: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _identifier(self.source_kind, "interception source kind")
        object.__setattr__(
            self,
            "detected_kinds",
            _codes(
                self.detected_kinds,
                "interception detected kind",
                maximum=_MAX_DETECTED_KINDS,
            ),
        )
        _nonnegative_int(self.detected_count, "interception detected count")
        if self.action not in _SECRET_ACTIONS:
            raise SecurityEventError("interception action is invalid")
        if self.detected_count == 0:
            if self.detected_kinds or self.action != "none":
                raise SecurityEventError(
                    "zero-detection interception facts must use action none"
                )
        elif not self.detected_kinds or self.action == "none":
            raise SecurityEventError(
                "interception detections require classification and an action"
            )
        _hash(self.evidence_hash, "interception evidence hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_kind": self.source_kind,
            "detected_kinds": list(self.detected_kinds),
            "detected_count": self.detected_count,
            "action": self.action,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class PolicyMismatch:
    """A deterministic policy fact, deliberately not a deny/verdict object."""

    code: str
    subject: str
    expected_hash: str | None
    observed_hash: str | None
    evidence_hash: str

    def __post_init__(self) -> None:
        _code(self.code, "policy mismatch code")
        _identifier(self.subject, "policy mismatch subject")
        if self.expected_hash is not None:
            _hash(self.expected_hash, "policy mismatch expected hash")
        if self.observed_hash is not None:
            _hash(self.observed_hash, "policy mismatch observed hash")
        _hash(self.evidence_hash, "policy mismatch evidence hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "subject": self.subject,
            "expected_hash": self.expected_hash,
            "observed_hash": self.observed_hash,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class QuarantineSignal:
    """Phase 16/17 quarantine evidence without skill content."""

    candidate_hash: str
    quarantine_digest: str
    state: str
    reason_digest: str
    reason_codes: tuple[str, ...]
    verification_state: str = "not-run"
    verification_digest: str | None = None

    def __post_init__(self) -> None:
        _hash(self.candidate_hash, "quarantine candidate hash")
        _hash(self.quarantine_digest, "quarantine digest")
        if self.state not in _QUARANTINE_STATES:
            raise SecurityEventError("quarantine state is invalid")
        _hash(self.reason_digest, "quarantine reason digest")
        object.__setattr__(
            self,
            "reason_codes",
            _codes(self.reason_codes, "quarantine reason code"),
        )
        if self.verification_state not in _VERIFICATION_STATES:
            raise SecurityEventError("quarantine verification state is invalid")
        if self.verification_state == "not-run":
            if self.verification_digest is not None:
                raise SecurityEventError(
                    "unverified quarantine signal cannot carry verification digest"
                )
        else:
            _hash(self.verification_digest, "quarantine verification digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_hash": self.candidate_hash,
            "quarantine_digest": self.quarantine_digest,
            "state": self.state,
            "reason_digest": self.reason_digest,
            "reason_codes": list(self.reason_codes),
            "verification_state": self.verification_state,
            "verification_digest": self.verification_digest,
        }


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """Immutable security facts for one exact execution request.

    The request hash binds requested execution semantics. ``content_hash`` also
    binds derived risk/interception/mismatch/quarantine facts. Neither hash is
    execution permission.
    """

    principal: PrincipalReference
    recipe: SecurityRecipeBinding
    run_authority_hash: str
    target: SecurityTargetBinding
    requested_tools: tuple[str, ...]
    authorized_toolsets: tuple[str, ...]
    resources: SecurityResourceFacts
    network: SecurityNetworkFacts
    policy_digest: str
    capabilities_hash: str
    memory_skill_risks: tuple[MemorySkillRisk, ...] = ()
    secret_interceptions: tuple[SecretInterceptionFact, ...] = ()
    policy_mismatches: tuple[PolicyMismatch, ...] = ()
    quarantine_signals: tuple[QuarantineSignal, ...] = ()

    def __post_init__(self) -> None:
        if type(self.principal) is not PrincipalReference:
            raise SecurityEventError("security event principal is invalid")
        if type(self.recipe) is not SecurityRecipeBinding:
            raise SecurityEventError("security event Recipe binding is invalid")
        _hash(self.run_authority_hash, "security RunAuthority hash")
        if type(self.target) is not SecurityTargetBinding:
            raise SecurityEventError("security event target is invalid")
        object.__setattr__(
            self,
            "requested_tools",
            _identifiers(self.requested_tools, "requested tool"),
        )
        object.__setattr__(
            self,
            "authorized_toolsets",
            _identifiers(self.authorized_toolsets, "authorized toolset"),
        )
        if type(self.resources) is not SecurityResourceFacts:
            raise SecurityEventError("security event resources are invalid")
        if type(self.network) is not SecurityNetworkFacts:
            raise SecurityEventError("security event network facts are invalid")
        try:
            bound_network = NetworkGrant(
                mode=self.network.mode,
                authority_ref=self.run_authority_hash,
                destinations=tuple(
                    NetworkDestination(
                        host=item.host,
                        resolved_ips=item.resolved_ips,
                        ports=item.ports,
                    )
                    for item in self.network.destinations
                ),
                approval_ref=self.network.approval_ref,
            )
        except Exception as error:
            raise SecurityEventError(
                "security event network facts do not bind exact RunAuthority"
            ) from error
        if bound_network.policy_hash != self.network.policy_hash:
            raise SecurityEventError(
                "security event network policy hash does not match exact facts"
            )
        _hash(self.policy_digest, "security policy digest")
        _hash(self.capabilities_hash, "security capabilities hash")
        object.__setattr__(
            self,
            "memory_skill_risks",
            _sorted_unique_facts(
                self.memory_skill_risks,
                MemorySkillRisk,
                "memory/skill risks",
            ),
        )
        object.__setattr__(
            self,
            "secret_interceptions",
            _sorted_unique_facts(
                self.secret_interceptions,
                SecretInterceptionFact,
                "secret interceptions",
            ),
        )
        object.__setattr__(
            self,
            "policy_mismatches",
            _sorted_unique_facts(
                self.policy_mismatches,
                PolicyMismatch,
                "policy mismatches",
            ),
        )
        object.__setattr__(
            self,
            "quarantine_signals",
            _sorted_unique_facts(
                self.quarantine_signals,
                QuarantineSignal,
                "quarantine signals",
            ),
        )
        _canonical(self.to_dict(), "security event")

    @classmethod
    def from_run_authority(
        cls,
        authority: RunAuthority,
        *,
        requested_tools: tuple[str, ...] = (),
        memory_skill_risks: tuple[MemorySkillRisk, ...] = (),
        secret_interceptions: tuple[SecretInterceptionFact, ...] = (),
        policy_mismatches: tuple[PolicyMismatch, ...] = (),
        quarantine_signals: tuple[QuarantineSignal, ...] = (),
    ) -> SecurityEvent:
        if type(authority) is not RunAuthority:
            raise SecurityEventError("security event requires exact RunAuthority")
        target = dict(authority.target)
        source = target.get("source")
        node_id = target.get("node_id")
        generation = target.get("generation")
        if source is None or node_id is None or generation is None:
            raise SecurityEventError(
                "RunAuthority target lacks deterministic source/node/generation facts"
            )
        grant = authority.network_grant()
        network = SecurityNetworkFacts(
            mode=grant.mode,
            policy_hash=grant.policy_hash,
            destinations=tuple(
                SecurityNetworkDestination(
                    host=item.host,
                    resolved_ips=item.resolved_ips,
                    ports=item.ports,
                )
                for item in grant.destinations
            ),
            approval_ref=grant.approval_ref,
        )
        recipe = authority.recipe
        return cls(
            principal=authority.principal,
            recipe=SecurityRecipeBinding(
                recipe_hash=recipe.recipe_hash,
                resolved_recipe_hash=recipe.resolved_recipe_hash,
                compiler_version=recipe.compiler_version,
                provenance_digest=recipe.provenance_digest,
                workflow_hash=recipe.workflow_hash,
                workflow_step_id=recipe.workflow_step_id,
            ),
            run_authority_hash=authority.content_hash,
            target=SecurityTargetBinding.from_target(
                target_digest=authority.target_digest,
                target=dict(authority.target),
            ),
            requested_tools=requested_tools,
            authorized_toolsets=authority.toolsets,
            resources=SecurityResourceFacts(
                cpu_millis=authority.resources.cpu_millis,
                memory_bytes=authority.resources.memory_bytes,
                pids_limit=authority.resources.pids_limit,
                max_iterations=authority.resources.max_iterations,
                deadline_ms=authority.deadline_ms,
            ),
            network=network,
            policy_digest=authority.policy_digest,
            capabilities_hash=authority.capabilities_hash,
            memory_skill_risks=memory_skill_risks,
            secret_interceptions=secret_interceptions,
            policy_mismatches=policy_mismatches,
            quarantine_signals=quarantine_signals,
        )

    def request_document(self) -> dict[str, object]:
        return {
            "schema": SECURITY_REQUEST_SCHEMA,
            "principal": self.principal.to_dict(),
            "recipe": self.recipe.to_dict(),
            "run_authority_hash": self.run_authority_hash,
            "target": self.target.to_dict(),
            "requested_tools": list(self.requested_tools),
            "authorized_toolsets": list(self.authorized_toolsets),
            "resources": self.resources.to_dict(),
            "network": self.network.to_dict(),
            "policy_digest": self.policy_digest,
            "capabilities_hash": self.capabilities_hash,
        }

    @property
    def request_hash(self) -> str:
        return _digest(self.request_document(), "security request")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SECURITY_EVENT_SCHEMA,
            "request_hash": self.request_hash,
            "request": self.request_document(),
            "memory_skill_risks": [item.to_dict() for item in self.memory_skill_risks],
            "secret_interceptions": [
                item.to_dict() for item in self.secret_interceptions
            ],
            "policy_mismatches": [item.to_dict() for item in self.policy_mismatches],
            "quarantine_signals": [item.to_dict() for item in self.quarantine_signals],
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "security event")


@dataclass(frozen=True, slots=True)
class DeterministicHardDeny:
    """A Fleet deterministic deny fact, separate from the security event itself."""

    request_hash: str
    event_hash: str
    policy_digest: str
    code: str
    subject: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _hash(self.request_hash, "hard-deny request hash")
        _hash(self.event_hash, "hard-deny event hash")
        _hash(self.policy_digest, "hard-deny policy digest")
        _code(self.code, "hard-deny code")
        _identifier(self.subject, "hard-deny subject")
        _hash(self.evidence_hash, "hard-deny evidence hash")

    @classmethod
    def from_event(
        cls,
        event: SecurityEvent,
        *,
        code: str,
        subject: str,
        evidence_hash: str,
    ) -> DeterministicHardDeny:
        if type(event) is not SecurityEvent:
            raise SecurityEventError("hard deny requires an exact security event")
        return cls(
            request_hash=event.request_hash,
            event_hash=event.content_hash,
            policy_digest=event.policy_digest,
            code=code,
            subject=subject,
            evidence_hash=evidence_hash,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": HARD_DENY_SCHEMA,
            "request_hash": self.request_hash,
            "event_hash": self.event_hash,
            "policy_digest": self.policy_digest,
            "code": self.code,
            "subject": self.subject,
            "evidence_hash": self.evidence_hash,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "deterministic hard deny")

    def validate_event(self, event: SecurityEvent) -> None:
        if type(event) is not SecurityEvent:
            raise SecurityEventError("hard deny validation requires security event")
        if (
            self.request_hash != event.request_hash
            or self.event_hash != event.content_hash
            or self.policy_digest != event.policy_digest
        ):
            raise SecurityEventError("hard deny is stale or request-substituted")


def _sorted_unique_facts(value: object, item_type: type, label: str) -> tuple:
    if type(value) not in {tuple, list} or len(value) > _MAX_ITEMS:
        raise SecurityEventError(f"{label} are invalid")
    items = tuple(value)
    if any(type(item) is not item_type for item in items):
        raise SecurityEventError(f"{label} are invalid")
    keyed = [(_canonical(item.to_dict(), label), item) for item in items]
    if len({key for key, _item in keyed}) != len(keyed):
        raise SecurityEventError(f"{label} contain duplicates")
    return tuple(item for _key, item in sorted(keyed, key=lambda pair: pair[0]))


def validate_hard_denies(
    event: SecurityEvent,
    hard_denies: tuple[DeterministicHardDeny, ...] | list[DeterministicHardDeny],
) -> tuple[DeterministicHardDeny, ...]:
    """Validate a separate deterministic-deny set against one exact event."""
    if type(event) is not SecurityEvent:
        raise SecurityEventError("hard-deny set requires security event")
    if type(hard_denies) not in {tuple, list} or len(hard_denies) > _MAX_ITEMS:
        raise SecurityEventError("hard-deny set is invalid")
    items = tuple(hard_denies)
    if any(type(item) is not DeterministicHardDeny for item in items):
        raise SecurityEventError("hard-deny set is invalid")
    for item in items:
        item.validate_event(event)
    keyed = [(_canonical(item.to_dict(), "hard deny"), item) for item in items]
    if len({key for key, _item in keyed}) != len(keyed):
        raise SecurityEventError("hard-deny set contains duplicates")
    return tuple(item for _key, item in sorted(keyed, key=lambda pair: pair[0]))


def security_event_from_dict(value: object) -> SecurityEvent:
    item = _exact_object(
        value,
        {
            "schema",
            "request_hash",
            "request",
            "memory_skill_risks",
            "secret_interceptions",
            "policy_mismatches",
            "quarantine_signals",
        },
        "security event",
    )
    if item["schema"] != SECURITY_EVENT_SCHEMA:
        raise SecurityEventError("security event schema is unsupported")
    request = _exact_object(
        item["request"],
        {
            "schema",
            "principal",
            "recipe",
            "run_authority_hash",
            "target",
            "requested_tools",
            "authorized_toolsets",
            "resources",
            "network",
            "policy_digest",
            "capabilities_hash",
        },
        "security request",
    )
    if request["schema"] != SECURITY_REQUEST_SCHEMA:
        raise SecurityEventError("security request schema is unsupported")
    recipe = _exact_object(
        request["recipe"],
        {
            "recipe_hash",
            "resolved_recipe_hash",
            "compiler_version",
            "provenance_digest",
            "workflow_hash",
            "workflow_step_id",
        },
        "security Recipe binding",
    )
    target = _exact_object(
        request["target"],
        {"target_digest", "target"},
        "security target",
    )
    resources = _exact_object(
        request["resources"],
        {"cpu_millis", "memory_bytes", "pids_limit", "max_iterations", "deadline_ms"},
        "security resources",
    )
    network = _exact_object(
        request["network"],
        {"mode", "policy_hash", "destinations", "approval_ref"},
        "security network",
    )
    destinations_value = network["destinations"]
    if type(destinations_value) is not list:
        raise SecurityEventError("security network destinations are invalid")
    destinations: list[SecurityNetworkDestination] = []
    for value_item in destinations_value:
        destination = _exact_object(
            value_item,
            {"host", "resolved_ips", "ports"},
            "security network destination",
        )
        destinations.append(
            SecurityNetworkDestination(
                host=destination["host"],
                resolved_ips=destination["resolved_ips"],
                ports=destination["ports"],
            )
        )

    risks_value = item["memory_skill_risks"]
    if type(risks_value) is not list:
        raise SecurityEventError("memory/skill risks are invalid")
    risks: list[MemorySkillRisk] = []
    for value_item in risks_value:
        risk = _exact_object(
            value_item,
            {
                "subject_kind",
                "subject_hash",
                "scope_kind",
                "risk_level",
                "signal_codes",
                "evidence_hash",
            },
            "memory/skill risk",
        )
        risks.append(
            MemorySkillRisk(
                subject_kind=risk["subject_kind"],
                subject_hash=risk["subject_hash"],
                scope_kind=risk["scope_kind"],
                risk_level=risk["risk_level"],
                signal_codes=risk["signal_codes"],
                evidence_hash=risk["evidence_hash"],
            )
        )

    interceptions_value = item["secret_interceptions"]
    if type(interceptions_value) is not list:
        raise SecurityEventError("secret interceptions are invalid")
    interceptions: list[SecretInterceptionFact] = []
    for value_item in interceptions_value:
        fact = _exact_object(
            value_item,
            {
                "source_kind",
                "detected_kinds",
                "detected_count",
                "action",
                "evidence_hash",
            },
            "secret interception",
        )
        interceptions.append(
            SecretInterceptionFact(
                source_kind=fact["source_kind"],
                detected_kinds=fact["detected_kinds"],
                detected_count=fact["detected_count"],
                action=fact["action"],
                evidence_hash=fact["evidence_hash"],
            )
        )

    mismatches_value = item["policy_mismatches"]
    if type(mismatches_value) is not list:
        raise SecurityEventError("policy mismatches are invalid")
    mismatches: list[PolicyMismatch] = []
    for value_item in mismatches_value:
        mismatch = _exact_object(
            value_item,
            {"code", "subject", "expected_hash", "observed_hash", "evidence_hash"},
            "policy mismatch",
        )
        mismatches.append(
            PolicyMismatch(
                code=mismatch["code"],
                subject=mismatch["subject"],
                expected_hash=mismatch["expected_hash"],
                observed_hash=mismatch["observed_hash"],
                evidence_hash=mismatch["evidence_hash"],
            )
        )

    quarantine_value = item["quarantine_signals"]
    if type(quarantine_value) is not list:
        raise SecurityEventError("quarantine signals are invalid")
    quarantine: list[QuarantineSignal] = []
    for value_item in quarantine_value:
        signal = _exact_object(
            value_item,
            {
                "candidate_hash",
                "quarantine_digest",
                "state",
                "reason_digest",
                "reason_codes",
                "verification_state",
                "verification_digest",
            },
            "quarantine signal",
        )
        quarantine.append(
            QuarantineSignal(
                candidate_hash=signal["candidate_hash"],
                quarantine_digest=signal["quarantine_digest"],
                state=signal["state"],
                reason_digest=signal["reason_digest"],
                reason_codes=signal["reason_codes"],
                verification_state=signal["verification_state"],
                verification_digest=signal["verification_digest"],
            )
        )

    event = SecurityEvent(
        principal=PrincipalReference.from_dict(request["principal"]),
        recipe=SecurityRecipeBinding(
            recipe_hash=recipe["recipe_hash"],
            resolved_recipe_hash=recipe["resolved_recipe_hash"],
            compiler_version=recipe["compiler_version"],
            provenance_digest=recipe["provenance_digest"],
            workflow_hash=recipe["workflow_hash"],
            workflow_step_id=recipe["workflow_step_id"],
        ),
        run_authority_hash=request["run_authority_hash"],
        target=SecurityTargetBinding.from_target(
            target_digest=target["target_digest"],
            target=target["target"],
        ),
        requested_tools=request["requested_tools"],
        authorized_toolsets=request["authorized_toolsets"],
        resources=SecurityResourceFacts(
            cpu_millis=resources["cpu_millis"],
            memory_bytes=resources["memory_bytes"],
            pids_limit=resources["pids_limit"],
            max_iterations=resources["max_iterations"],
            deadline_ms=resources["deadline_ms"],
        ),
        network=SecurityNetworkFacts(
            mode=network["mode"],
            policy_hash=network["policy_hash"],
            destinations=tuple(destinations),
            approval_ref=network["approval_ref"],
        ),
        policy_digest=request["policy_digest"],
        capabilities_hash=request["capabilities_hash"],
        memory_skill_risks=tuple(risks),
        secret_interceptions=tuple(interceptions),
        policy_mismatches=tuple(mismatches),
        quarantine_signals=tuple(quarantine),
    )
    supplied_request_hash = _hash(item["request_hash"], "security request hash")
    if supplied_request_hash != event.request_hash:
        raise SecurityEventError("security event request hash does not match request")
    return event


def hard_deny_from_dict(value: object) -> DeterministicHardDeny:
    item = _exact_object(
        value,
        {
            "schema",
            "request_hash",
            "event_hash",
            "policy_digest",
            "code",
            "subject",
            "evidence_hash",
        },
        "deterministic hard deny",
    )
    if item["schema"] != HARD_DENY_SCHEMA:
        raise SecurityEventError("hard-deny schema is unsupported")
    return DeterministicHardDeny(
        request_hash=item["request_hash"],
        event_hash=item["event_hash"],
        policy_digest=item["policy_digest"],
        code=item["code"],
        subject=item["subject"],
        evidence_hash=item["evidence_hash"],
    )


__all__ = [
    "HARD_DENY_SCHEMA",
    "SECURITY_EVENT_SCHEMA",
    "SECURITY_REQUEST_SCHEMA",
    "DeterministicHardDeny",
    "MemorySkillRisk",
    "PolicyMismatch",
    "QuarantineSignal",
    "SecretInterceptionFact",
    "SecurityEvent",
    "SecurityEventError",
    "SecurityNetworkDestination",
    "SecurityNetworkFacts",
    "SecurityRecipeBinding",
    "SecurityResourceFacts",
    "SecurityTargetBinding",
    "hard_deny_from_dict",
    "security_event_from_dict",
    "validate_hard_denies",
]
