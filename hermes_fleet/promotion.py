"""Phase 18 deterministic memory/skill promotion policy.

Fleet owns the decision to widen durable learning visibility. Promotion never
creates execution authority: this module only authorizes an exact content hash
to move between durable learning scopes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Final

from .principal_identity import (
    SOURCE_SCOPED_PARENT,
    PrincipalRecord,
    PrincipalReference,
)

PROMOTION_VERSION: Final[str] = "fleet-promotion-v1"
PROMOTION_POLICY_VERSION: Final[str] = "phase18-v1"
PROMOTABLE_KINDS: Final[frozenset[str]] = frozenset({"memory", "skill"})
PROMOTION_SCOPE_KINDS: Final[frozenset[str]] = frozenset(
    {"principal", "project", "network", "owner"}
)
_SCOPE_RANK: Final[dict[str, int]] = {
    "principal": 0,
    "project": 1,
    "network": 2,
    "owner": 3,
}
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$")
_MAX_TTL_MS = 15 * 60 * 1000


class PromotionError(RuntimeError):
    """A durable-learning promotion cannot be authorized safely."""


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise PromotionError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise PromotionError(f"{label} is invalid")
    return value


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PromotionError("promotion document is not canonical JSON") from error


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class PromotionScopeRef:
    kind: str
    scope_id: str

    def __post_init__(self) -> None:
        if self.kind not in PROMOTION_SCOPE_KINDS:
            raise PromotionError("promotion scope kind is invalid")
        if self.kind == "principal":
            _hash(self.scope_id, "promotion principal scope")
        else:
            _identifier(self.scope_id, "promotion scope id")

    def to_request(self) -> dict[str, str]:
        return {"kind": self.kind, "scope_id": self.scope_id}


def _validate_promotion_fields(
    *,
    subject_kind: str,
    subject_key: str,
    source_owner_principal_id: str,
    agent_instance_id: str,
    source_scope: PromotionScopeRef,
    target_scope: PromotionScopeRef,
    source_content_hash: str,
    approved_content_hash: str,
    administrator: PrincipalReference,
    verification_digest: str | None,
    expected_current_promotion_id: str | None,
    rollback_to_promotion_id: str | None,
    operation: str,
) -> None:
    if subject_kind not in PROMOTABLE_KINDS:
        raise PromotionError("promotion subject kind is invalid")
    _identifier(subject_key, "promotion subject key")
    if operation not in {"promote", "rollback"}:
        raise PromotionError("promotion operation is invalid")
    if expected_current_promotion_id is not None:
        _hash(expected_current_promotion_id, "expected current promotion ID")
    if operation == "rollback":
        _hash(rollback_to_promotion_id, "rollback promotion ID")
        if expected_current_promotion_id is None:
            raise PromotionError("rollback requires the exact current promotion ID")
        if rollback_to_promotion_id == expected_current_promotion_id:
            raise PromotionError("rollback target is already current")
    elif rollback_to_promotion_id is not None:
        raise PromotionError("normal promotion cannot carry a rollback target")
    _hash(source_owner_principal_id, "promotion source owner principal")
    _hash(agent_instance_id, "promotion Agent Instance")
    _hash(source_content_hash, "promotion source content hash")
    _hash(approved_content_hash, "promotion approved content hash")
    if (
        type(source_scope) is not PromotionScopeRef
        or type(target_scope) is not PromotionScopeRef
    ):
        raise PromotionError("promotion scope reference is invalid")
    if type(administrator) is not PrincipalReference:
        raise PromotionError("promotion administrator reference is invalid")
    if source_scope.kind == "principal" and (
        source_scope.scope_id != source_owner_principal_id
    ):
        raise PromotionError("promotion private source scope does not match its owner")
    if _SCOPE_RANK[target_scope.kind] <= _SCOPE_RANK[source_scope.kind]:
        raise PromotionError("promotion target must be broader than the source scope")
    if subject_kind == "skill":
        _hash(verification_digest, "promotion skill verification digest")
    elif verification_digest is not None:
        raise PromotionError(
            "memory promotion cannot carry skill verification evidence"
        )


@dataclass(frozen=True, slots=True)
class PromotionAuthorization:
    subject_kind: str
    subject_key: str
    source_owner_principal_id: str
    agent_instance_id: str
    source_scope: PromotionScopeRef
    target_scope: PromotionScopeRef
    source_content_hash: str
    approved_content_hash: str
    administrator: PrincipalReference
    issued_at_ms: int
    expires_at_ms: int
    verification_digest: str | None = None
    expected_current_promotion_id: str | None = None
    rollback_to_promotion_id: str | None = None
    operation: str = "promote"
    policy_version: str = PROMOTION_POLICY_VERSION
    version: str = PROMOTION_VERSION

    def __post_init__(self) -> None:
        if self.version != PROMOTION_VERSION:
            raise PromotionError("promotion version is unsupported")
        if self.policy_version != PROMOTION_POLICY_VERSION:
            raise PromotionError("promotion policy version is unsupported")
        _validate_promotion_fields(
            subject_kind=self.subject_kind,
            subject_key=self.subject_key,
            source_owner_principal_id=self.source_owner_principal_id,
            agent_instance_id=self.agent_instance_id,
            source_scope=self.source_scope,
            target_scope=self.target_scope,
            source_content_hash=self.source_content_hash,
            approved_content_hash=self.approved_content_hash,
            administrator=self.administrator,
            verification_digest=self.verification_digest,
            expected_current_promotion_id=self.expected_current_promotion_id,
            rollback_to_promotion_id=self.rollback_to_promotion_id,
            operation=self.operation,
        )
        for value, label in (
            (self.issued_at_ms, "promotion issue time"),
            (self.expires_at_ms, "promotion expiry"),
        ):
            if isinstance(value, bool) or type(value) is not int or value < 1:
                raise PromotionError(f"{label} is invalid")
        if not (
            self.issued_at_ms < self.expires_at_ms <= self.issued_at_ms + _MAX_TTL_MS
        ):
            raise PromotionError("promotion lifetime is invalid")

    def unsigned_document(self) -> dict[str, object]:
        return {
            "version": self.version,
            "policy_version": self.policy_version,
            "subject_kind": self.subject_kind,
            "subject_key": self.subject_key,
            "source_owner_principal_id": self.source_owner_principal_id,
            "agent_instance_id": self.agent_instance_id,
            "source_scope": self.source_scope.to_request(),
            "target_scope": self.target_scope.to_request(),
            "source_content_hash": self.source_content_hash,
            "approved_content_hash": self.approved_content_hash,
            "administrator": {
                "principal_id": self.administrator.principal_id,
                "kind": self.administrator.kind,
                "generation": self.administrator.generation,
                "binding_hash": self.administrator.binding_hash,
            },
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "verification_digest": self.verification_digest,
            "expected_current_promotion_id": self.expected_current_promotion_id,
            "rollback_to_promotion_id": self.rollback_to_promotion_id,
            "operation": self.operation,
            "authority": "none",
        }

    @property
    def promotion_id(self) -> str:
        return _digest(self.unsigned_document())

    def to_request(self) -> dict[str, object]:
        return {**self.unsigned_document(), "promotion_id": self.promotion_id}


def _administrator_controls_scope(
    administrator: PrincipalRecord,
    target: PromotionScopeRef,
) -> bool:
    """Return whether the exact authenticated principal administers target.

    Phase 18 intentionally avoids inventing an ambient RBAC database. The
    administering principal must itself be the exact principal for the target
    scope: project administrator for that project, network administrator for
    that network, or owner principal for that owner scope. Future delegated
    administrator roles can narrow/extend this predicate explicitly.
    """
    expected_kind = target.kind
    if expected_kind == "principal":
        return False
    if administrator.definition.kind != expected_kind:
        return False
    return administrator.definition.scope.get(expected_kind) == target.scope_id


def _administrator_controls_private_source(
    administrator: PrincipalRecord,
    *,
    source_owner_principal_id: str,
    target: PromotionScopeRef,
) -> bool:
    """Require owner consent without collapsing distinct scoped identities.

    Owner-scope promotion is administered directly by the source owner. For a
    project/network target, the administering principal must be an exact
    scoped child whose immutable binding names the private source principal as
    its parent. This uses the Phase 9 identity graph rather than inventing a
    second promotion-specific delegation database.
    """
    if target.kind == "owner":
        return administrator.reference.principal_id == source_owner_principal_id
    binding = administrator.binding
    if binding.source != SOURCE_SCOPED_PARENT:
        return False
    return binding.evidence.get("parent_principal_id") == source_owner_principal_id


def validate_promotion_policy(
    *,
    subject_kind: str,
    subject_key: str,
    source_owner_principal_id: str,
    agent_instance_id: str,
    source_scope: PromotionScopeRef,
    target_scope: PromotionScopeRef,
    source_content_hash: str,
    approved_content_hash: str,
    administrator: PrincipalRecord,
    verification_digest: str | None = None,
    expected_current_promotion_id: str | None = None,
    rollback_to_promotion_id: str | None = None,
    operation: str = "promote",
    ttl_ms: int = 5 * 60 * 1000,
) -> None:
    """Validate deterministic promotion policy without minting authorization."""

    if type(administrator) is not PrincipalRecord:
        raise PromotionError("promotion administrator is invalid")
    if (
        isinstance(ttl_ms, bool)
        or type(ttl_ms) is not int
        or not 0 < ttl_ms <= _MAX_TTL_MS
    ):
        raise PromotionError("promotion TTL is invalid")
    _validate_promotion_fields(
        subject_kind=subject_kind,
        subject_key=subject_key,
        source_owner_principal_id=source_owner_principal_id,
        agent_instance_id=agent_instance_id,
        source_scope=source_scope,
        target_scope=target_scope,
        source_content_hash=source_content_hash,
        approved_content_hash=approved_content_hash,
        administrator=administrator.reference,
        verification_digest=verification_digest,
        expected_current_promotion_id=expected_current_promotion_id,
        rollback_to_promotion_id=rollback_to_promotion_id,
        operation=operation,
    )
    if not _administrator_controls_scope(administrator, target_scope):
        raise PromotionError("principal is not an administrator of the target scope")
    if source_scope.kind == "principal" and not _administrator_controls_private_source(
        administrator,
        source_owner_principal_id=source_owner_principal_id,
        target=target_scope,
    ):
        raise PromotionError(
            "private promotion administrator is not derived from the source principal"
        )


def authorize_promotion(
    *,
    subject_kind: str,
    subject_key: str,
    source_owner_principal_id: str,
    agent_instance_id: str,
    source_scope: PromotionScopeRef,
    target_scope: PromotionScopeRef,
    source_content_hash: str,
    approved_content_hash: str,
    administrator: PrincipalRecord,
    verification_digest: str | None = None,
    expected_current_promotion_id: str | None = None,
    rollback_to_promotion_id: str | None = None,
    operation: str = "promote",
    now_ms: int | None = None,
    ttl_ms: int = 5 * 60 * 1000,
) -> PromotionAuthorization:
    """Authorize one exact sanitized promotion without granting authority."""
    validate_promotion_policy(
        subject_kind=subject_kind,
        subject_key=subject_key,
        source_owner_principal_id=source_owner_principal_id,
        agent_instance_id=agent_instance_id,
        source_scope=source_scope,
        target_scope=target_scope,
        source_content_hash=source_content_hash,
        approved_content_hash=approved_content_hash,
        administrator=administrator,
        verification_digest=verification_digest,
        expected_current_promotion_id=expected_current_promotion_id,
        rollback_to_promotion_id=rollback_to_promotion_id,
        operation=operation,
        ttl_ms=ttl_ms,
    )
    issued = int(time.time() * 1000) if now_ms is None else now_ms
    return PromotionAuthorization(
        subject_kind=subject_kind,
        subject_key=subject_key,
        source_owner_principal_id=source_owner_principal_id,
        agent_instance_id=agent_instance_id,
        source_scope=source_scope,
        target_scope=target_scope,
        source_content_hash=source_content_hash,
        approved_content_hash=approved_content_hash,
        administrator=administrator.reference,
        issued_at_ms=issued,
        expires_at_ms=issued + ttl_ms,
        verification_digest=verification_digest,
        expected_current_promotion_id=expected_current_promotion_id,
        rollback_to_promotion_id=rollback_to_promotion_id,
        operation=operation,
    )


__all__ = [
    "PROMOTION_POLICY_VERSION",
    "PROMOTION_VERSION",
    "PromotionAuthorization",
    "PromotionError",
    "PromotionScopeRef",
    "authorize_promotion",
    "validate_promotion_policy",
]
