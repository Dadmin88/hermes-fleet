"""Phase 22 mandatory pre-execution Templar gate.

The gate composes authenticated principal binding, deterministic Fleet policy,
destination admission, the Phase 20/21 Templar evaluator, Fleet's final
advisory decision, RunAuthority activation, and a content-bound permit for the
exact derived Run Capsule. The permit is evidence only: ``RunAuthority``
remains Fleet's single root of temporary execution power.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .principal_identity import PrincipalReference
from .run_authority import RunAuthority, RunAuthorityError, RunAuthorityStore
from .run_capsule import RunCapsuleSpec
from .security_event import (
    DeterministicHardDeny,
    MemorySkillRisk,
    PolicyMismatch,
    QuarantineSignal,
    SecretInterceptionFact,
    SecurityEvent,
    SecurityEventError,
    validate_hard_denies,
)
from .templar import ALLOW, DENY, REVIEW, TemplarCore, TemplarVerdict

PRE_EXECUTION_REQUEST_SCHEMA: Final[str] = "fleet.pre-execution-request.v1"
DETERMINISTIC_POLICY_SCHEMA: Final[str] = "fleet.pre-execution-policy.v1"
DESTINATION_ADMISSION_SCHEMA: Final[str] = "fleet.destination-admission.v1"
FLEET_FINAL_DECISION_SCHEMA: Final[str] = "fleet.pre-execution-final-decision.v1"
PRE_EXECUTION_PERMIT_SCHEMA: Final[str] = "fleet.pre-execution-permit.v1"

OPERATION_HERMES_RUN: Final[str] = "fleet.hermes.run"
ADMITTED: Final[str] = "admitted"
READY: Final[str] = "ready"

_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_REASON_CODES = 32
_MAX_PERMIT_TTL_MS = 300_000
_HASH_PREFIX = "sha256:"
_PERMIT_SEAL_PREFIX = "hmac-sha256:"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")


class PreExecutionGateError(RuntimeError):
    """Phase 22 input, binding, or orchestration state is unsafe."""


class PreExecutionGateStale(PreExecutionGateError):
    """The exact request context changed before authority activation."""


def _canonical(
    value: object, label: str, *, maximum: int = _MAX_DOCUMENT_BYTES
) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PreExecutionGateError(f"{label} is not canonical JSON") from error
    if len(payload) > maximum:
        raise PreExecutionGateError(f"{label} exceeds the supported bound")
    return payload


def _digest(value: object, label: str) -> str:
    return _HASH_PREFIX + hashlib.sha256(_canonical(value, label)).hexdigest()


def _hash(value: object, label: str) -> str:
    if type(value) is not str or not value.startswith(_HASH_PREFIX):
        raise PreExecutionGateError(f"{label} is invalid")
    suffix = value[len(_HASH_PREFIX) :]
    if len(suffix) != 64 or any(
        character not in "0123456789abcdef" for character in suffix
    ):
        raise PreExecutionGateError(f"{label} is invalid")
    return value


def _permit_seal(value: object, key: bytes) -> str:
    digest = hmac.new(
        key, _canonical(value, "pre-execution permit seal"), hashlib.sha256
    )
    return _PERMIT_SEAL_PREFIX + digest.hexdigest()


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or not _IDENTIFIER_RE.fullmatch(value):
        raise PreExecutionGateError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 < value <= maximum:
        raise PreExecutionGateError(f"{label} is invalid")
    return value


def _reason_codes(value: object) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or len(value) > _MAX_REASON_CODES:
        raise PreExecutionGateError("pre-execution reason codes are invalid")
    items = tuple(value)
    if any(type(item) is not str or not _CODE_RE.fullmatch(item) for item in items):
        raise PreExecutionGateError("pre-execution reason code is invalid")
    if len(set(items)) != len(items):
        raise PreExecutionGateError("pre-execution reason codes contain duplicates")
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True)
class PreExecutionContext:
    """Current mutable Fleet facts that must still match the proposed authority."""

    principal: PrincipalReference
    agent_instance_id: str
    recipe_hash: str
    resolved_recipe_hash: str
    policy_digest: str
    capabilities_hash: str
    target_digest: str
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if type(self.principal) is not PrincipalReference:
            raise PreExecutionGateError("pre-execution principal context is invalid")
        for value, label in (
            (self.agent_instance_id, "Agent Instance id"),
            (self.recipe_hash, "Recipe hash"),
            (self.resolved_recipe_hash, "ResolvedRecipe hash"),
            (self.policy_digest, "policy digest"),
            (self.capabilities_hash, "capabilities hash"),
            (self.target_digest, "target digest"),
        ):
            _hash(value, f"pre-execution {label}")
        if self.provider is not None:
            _identifier(self.provider, "pre-execution provider")
        if self.model is not None:
            _identifier(self.model, "pre-execution model")

    @classmethod
    def from_authority(
        cls,
        authority: RunAuthority,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> PreExecutionContext:
        if type(authority) is not RunAuthority:
            raise PreExecutionGateError("pre-execution context requires RunAuthority")
        return cls(
            principal=authority.principal,
            agent_instance_id=authority.agent_instance_id,
            recipe_hash=authority.recipe.recipe_hash,
            resolved_recipe_hash=authority.recipe.resolved_recipe_hash,
            policy_digest=authority.policy_digest,
            capabilities_hash=authority.capabilities_hash,
            target_digest=authority.target_digest,
            provider=provider,
            model=model,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "principal": self.principal.to_dict(),
            "agent_instance_id": self.agent_instance_id,
            "recipe_hash": self.recipe_hash,
            "resolved_recipe_hash": self.resolved_recipe_hash,
            "policy_digest": self.policy_digest,
            "capabilities_hash": self.capabilities_hash,
            "target_digest": self.target_digest,
            "provider": self.provider,
            "model": self.model,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "pre-execution context")

    def validate_authority(self, authority: RunAuthority, *, now_ms: int) -> None:
        if type(authority) is not RunAuthority:
            raise PreExecutionGateError("pre-execution authority is invalid")
        try:
            authority.validate_context(
                principal=self.principal,
                agent_instance_id=self.agent_instance_id,
                recipe_hash=self.recipe_hash,
                resolved_recipe_hash=self.resolved_recipe_hash,
                policy_digest=self.policy_digest,
                capabilities_hash=self.capabilities_hash,
                target_digest=self.target_digest,
                now_ms=now_ms,
                provider=self.provider,
                model=self.model,
            )
        except RunAuthorityError as error:
            raise PreExecutionGateStale("pre-execution context is stale") from error


@dataclass(frozen=True, slots=True)
class PreExecutionRequest:
    """One authenticated exact execution intent before RunAuthority activation."""

    operation: str
    authenticated_principal: PrincipalReference
    authority: RunAuthority
    event: SecurityEvent

    def __post_init__(self) -> None:
        if self.operation != OPERATION_HERMES_RUN:
            raise PreExecutionGateError("pre-execution operation is unsupported")
        if type(self.authenticated_principal) is not PrincipalReference:
            raise PreExecutionGateError("authenticated principal is invalid")
        if type(self.authority) is not RunAuthority:
            raise PreExecutionGateError("proposed RunAuthority is invalid")
        if type(self.event) is not SecurityEvent:
            raise PreExecutionGateError("pre-execution security event is invalid")
        if self.authenticated_principal != self.authority.principal:
            raise PreExecutionGateError(
                "authenticated principal does not match RunAuthority"
            )
        if self.event.principal != self.authenticated_principal:
            raise PreExecutionGateError(
                "security event principal does not match authentication"
            )
        if self.event.run_authority_hash != self.authority.content_hash:
            raise PreExecutionGateError(
                "security event does not bind proposed RunAuthority"
            )
        if self.event.policy_digest != self.authority.policy_digest:
            raise PreExecutionGateError(
                "security event policy does not match RunAuthority"
            )
        if self.event.capabilities_hash != self.authority.capabilities_hash:
            raise PreExecutionGateError(
                "security event capabilities do not match RunAuthority"
            )
        if self.event.target.target_digest != self.authority.target_digest:
            raise PreExecutionGateError(
                "security event target does not match RunAuthority"
            )

    @classmethod
    def from_authority(
        cls,
        authority: RunAuthority,
        *,
        authenticated_principal: PrincipalReference,
        requested_tools: tuple[str, ...] = (),
        memory_skill_risks: tuple[MemorySkillRisk, ...] = (),
        secret_interceptions: tuple[SecretInterceptionFact, ...] = (),
        policy_mismatches: tuple[PolicyMismatch, ...] = (),
        quarantine_signals: tuple[QuarantineSignal, ...] = (),
    ) -> PreExecutionRequest:
        if type(authority) is not RunAuthority:
            raise PreExecutionGateError("pre-execution request requires RunAuthority")
        try:
            event = SecurityEvent.from_run_authority(
                authority,
                requested_tools=requested_tools,
                memory_skill_risks=memory_skill_risks,
                secret_interceptions=secret_interceptions,
                policy_mismatches=policy_mismatches,
                quarantine_signals=quarantine_signals,
            )
        except SecurityEventError as error:
            raise PreExecutionGateError(
                "pre-execution security event is invalid"
            ) from error
        return cls(
            operation=OPERATION_HERMES_RUN,
            authenticated_principal=authenticated_principal,
            authority=authority,
            event=event,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PRE_EXECUTION_REQUEST_SCHEMA,
            "operation": self.operation,
            "authenticated_principal": self.authenticated_principal.to_dict(),
            "proposed_run_authority_hash": self.authority.content_hash,
            "security_event": self.event.to_dict(),
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "pre-execution request")


@dataclass(frozen=True, slots=True)
class DeterministicPolicyDecision:
    gate_request_hash: str
    security_request_hash: str
    event_hash: str
    policy_digest: str
    templar_required: bool
    hard_denies: tuple[DeterministicHardDeny, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.gate_request_hash, "gate request hash"),
            (self.security_request_hash, "security request hash"),
            (self.event_hash, "event hash"),
            (self.policy_digest, "policy digest"),
        ):
            _hash(value, f"deterministic policy {label}")
        if type(self.templar_required) is not bool:
            raise PreExecutionGateError("Templar requirement is invalid")
        if type(self.hard_denies) not in {tuple, list}:
            raise PreExecutionGateError("deterministic hard denies are invalid")
        object.__setattr__(self, "hard_denies", tuple(self.hard_denies))
        if any(type(item) is not DeterministicHardDeny for item in self.hard_denies):
            raise PreExecutionGateError("deterministic hard deny is invalid")

    @classmethod
    def from_request(
        cls,
        request: PreExecutionRequest,
        *,
        templar_required: bool,
        hard_denies: tuple[DeterministicHardDeny, ...] = (),
    ) -> DeterministicPolicyDecision:
        return cls(
            gate_request_hash=request.content_hash,
            security_request_hash=request.event.request_hash,
            event_hash=request.event.content_hash,
            policy_digest=request.event.policy_digest,
            templar_required=templar_required,
            hard_denies=hard_denies,
        )

    def validate_for(
        self, request: PreExecutionRequest
    ) -> tuple[DeterministicHardDeny, ...]:
        if type(request) is not PreExecutionRequest:
            raise PreExecutionGateError("policy validation request is invalid")
        if (
            self.gate_request_hash != request.content_hash
            or self.security_request_hash != request.event.request_hash
            or self.event_hash != request.event.content_hash
            or self.policy_digest != request.event.policy_digest
        ):
            raise PreExecutionGateStale("deterministic policy decision is stale")
        try:
            return validate_hard_denies(request.event, self.hard_denies)
        except SecurityEventError as error:
            raise PreExecutionGateStale("deterministic hard deny is stale") from error

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": DETERMINISTIC_POLICY_SCHEMA,
            "gate_request_hash": self.gate_request_hash,
            "security_request_hash": self.security_request_hash,
            "event_hash": self.event_hash,
            "policy_digest": self.policy_digest,
            "templar_required": self.templar_required,
            "hard_denies": [item.to_dict() for item in self.hard_denies],
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "deterministic pre-execution policy")


@dataclass(frozen=True, slots=True)
class DestinationAdmissionDecision:
    gate_request_hash: str
    security_request_hash: str
    event_hash: str
    target_digest: str
    status: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.gate_request_hash, "gate request hash"),
            (self.security_request_hash, "security request hash"),
            (self.event_hash, "event hash"),
            (self.target_digest, "target digest"),
        ):
            _hash(value, f"destination admission {label}")
        if self.status not in {ADMITTED, DENY}:
            raise PreExecutionGateError("destination admission status is invalid")
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        if self.status == DENY and not self.reason_codes:
            raise PreExecutionGateError(
                "denied destination admission requires a reason"
            )

    @classmethod
    def from_request(
        cls,
        request: PreExecutionRequest,
        *,
        status: str,
        reason_codes: tuple[str, ...] = (),
    ) -> DestinationAdmissionDecision:
        return cls(
            gate_request_hash=request.content_hash,
            security_request_hash=request.event.request_hash,
            event_hash=request.event.content_hash,
            target_digest=request.authority.target_digest,
            status=status,
            reason_codes=reason_codes,
        )

    def validate_for(self, request: PreExecutionRequest) -> None:
        if (
            self.gate_request_hash != request.content_hash
            or self.security_request_hash != request.event.request_hash
            or self.event_hash != request.event.content_hash
            or self.target_digest != request.authority.target_digest
        ):
            raise PreExecutionGateStale("destination admission is stale")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": DESTINATION_ADMISSION_SCHEMA,
            "gate_request_hash": self.gate_request_hash,
            "security_request_hash": self.security_request_hash,
            "event_hash": self.event_hash,
            "target_digest": self.target_digest,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "destination admission")


@dataclass(frozen=True, slots=True)
class FleetFinalDecision:
    gate_request_hash: str
    security_request_hash: str
    event_hash: str
    policy_digest: str
    destination_admission_hash: str
    decision: str
    reason_codes: tuple[str, ...] = ()
    templar_verdict_hash: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.gate_request_hash, "gate request hash"),
            (self.security_request_hash, "security request hash"),
            (self.event_hash, "event hash"),
            (self.policy_digest, "policy digest"),
            (self.destination_admission_hash, "destination admission hash"),
        ):
            _hash(value, f"Fleet final decision {label}")
        if self.templar_verdict_hash is not None:
            _hash(
                self.templar_verdict_hash, "Fleet final decision Templar verdict hash"
            )
        if self.decision not in {ALLOW, DENY}:
            raise PreExecutionGateError("Fleet final decision is invalid")
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        if self.decision == DENY and not self.reason_codes:
            raise PreExecutionGateError("Fleet final deny requires a reason")

    @classmethod
    def from_request(
        cls,
        request: PreExecutionRequest,
        admission: DestinationAdmissionDecision,
        *,
        decision: str,
        reason_codes: tuple[str, ...] = (),
        verdict: TemplarVerdict | None = None,
    ) -> FleetFinalDecision:
        return cls(
            gate_request_hash=request.content_hash,
            security_request_hash=request.event.request_hash,
            event_hash=request.event.content_hash,
            policy_digest=request.event.policy_digest,
            destination_admission_hash=admission.content_hash,
            decision=decision,
            reason_codes=reason_codes,
            templar_verdict_hash=None if verdict is None else verdict.content_hash,
        )

    def validate_for(
        self,
        request: PreExecutionRequest,
        admission: DestinationAdmissionDecision,
        *,
        verdict: TemplarVerdict | None,
    ) -> None:
        expected_verdict_hash = None if verdict is None else verdict.content_hash
        if (
            self.gate_request_hash != request.content_hash
            or self.security_request_hash != request.event.request_hash
            or self.event_hash != request.event.content_hash
            or self.policy_digest != request.event.policy_digest
            or self.destination_admission_hash != admission.content_hash
            or self.templar_verdict_hash != expected_verdict_hash
        ):
            raise PreExecutionGateStale("Fleet final decision is stale")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": FLEET_FINAL_DECISION_SCHEMA,
            "gate_request_hash": self.gate_request_hash,
            "security_request_hash": self.security_request_hash,
            "event_hash": self.event_hash,
            "policy_digest": self.policy_digest,
            "destination_admission_hash": self.destination_admission_hash,
            "templar_verdict_hash": self.templar_verdict_hash,
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "Fleet final pre-execution decision")


@dataclass(frozen=True, slots=True)
class PreExecutionPermit:
    """Authority-free, Fleet-sealed proof that the exact request passed Phase 22."""

    gate_request_hash: str
    security_request_hash: str
    event_hash: str
    policy_digest: str
    run_authority_hash: str
    capsule_hash: str
    final_decision_hash: str
    issued_at_ms: int
    valid_until_ms: int
    seal: str
    templar_verdict_hash: str | None = None
    authority: str = "none"

    def __post_init__(self) -> None:
        for value, label in (
            (self.gate_request_hash, "gate request hash"),
            (self.security_request_hash, "security request hash"),
            (self.event_hash, "event hash"),
            (self.policy_digest, "policy digest"),
            (self.run_authority_hash, "RunAuthority hash"),
            (self.capsule_hash, "Run Capsule hash"),
            (self.final_decision_hash, "final decision hash"),
        ):
            _hash(value, f"pre-execution permit {label}")
        if self.templar_verdict_hash is not None:
            _hash(
                self.templar_verdict_hash, "pre-execution permit Templar verdict hash"
            )
        if type(self.seal) is not str or not self.seal.startswith(_PERMIT_SEAL_PREFIX):
            raise PreExecutionGateError("pre-execution permit seal is invalid")
        seal_suffix = self.seal[len(_PERMIT_SEAL_PREFIX) :]
        if len(seal_suffix) != 64 or any(
            character not in "0123456789abcdef" for character in seal_suffix
        ):
            raise PreExecutionGateError("pre-execution permit seal is invalid")
        _positive_int(self.issued_at_ms, "pre-execution permit issue time")
        _positive_int(self.valid_until_ms, "pre-execution permit expiry")
        if self.valid_until_ms <= self.issued_at_ms:
            raise PreExecutionGateError("pre-execution permit expiry is invalid")
        if self.authority != "none":
            raise PreExecutionGateError(
                "pre-execution permit must carry authority:none"
            )

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema": PRE_EXECUTION_PERMIT_SCHEMA,
            "gate_request_hash": self.gate_request_hash,
            "security_request_hash": self.security_request_hash,
            "event_hash": self.event_hash,
            "policy_digest": self.policy_digest,
            "run_authority_hash": self.run_authority_hash,
            "capsule_hash": self.capsule_hash,
            "final_decision_hash": self.final_decision_hash,
            "templar_verdict_hash": self.templar_verdict_hash,
            "issued_at_ms": self.issued_at_ms,
            "valid_until_ms": self.valid_until_ms,
            "authority": self.authority,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "seal": self.seal}

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "pre-execution permit")

    def validate_for(self, spec: RunCapsuleSpec, *, now_ms: int) -> None:
        if type(spec) is not RunCapsuleSpec:
            raise PreExecutionGateError("pre-execution permit Capsule is invalid")
        _positive_int(now_ms, "pre-execution permit validation time")
        if now_ms < self.issued_at_ms or now_ms >= self.valid_until_ms:
            raise PreExecutionGateStale("pre-execution permit is stale")
        if self.valid_until_ms > spec.deadline_ms:
            raise PreExecutionGateStale("pre-execution permit exceeds Capsule deadline")
        if self.run_authority_hash != spec.run_authority_hash:
            raise PreExecutionGateStale("pre-execution permit RunAuthority changed")
        if self.capsule_hash != spec.content_hash:
            raise PreExecutionGateStale("pre-execution permit Run Capsule changed")


class PreExecutionPermitSealer:
    """Process-local Fleet proof that a permit was actually issued by the gate."""

    def __init__(self, key: bytes | None = None) -> None:
        if key is None:
            key = os.urandom(32)
        if type(key) is not bytes or len(key) != 32:
            raise PreExecutionGateError("pre-execution permit sealing key is invalid")
        self.__key = bytes(key)

    def issue(
        self,
        *,
        request: PreExecutionRequest,
        final_decision: FleetFinalDecision,
        capsule_spec: RunCapsuleSpec,
        verdict: TemplarVerdict | None,
        issued_at_ms: int,
        valid_until_ms: int,
    ) -> PreExecutionPermit:
        if type(request) is not PreExecutionRequest:
            raise PreExecutionGateError("permit request is invalid")
        if type(final_decision) is not FleetFinalDecision:
            raise PreExecutionGateError("permit final decision is invalid")
        if final_decision.decision != ALLOW:
            raise PreExecutionGateError("permit requires Fleet final allow")
        if type(capsule_spec) is not RunCapsuleSpec:
            raise PreExecutionGateError("permit Run Capsule is invalid")
        request.authority.validate_capsule(capsule_spec)
        values = {
            "gate_request_hash": request.content_hash,
            "security_request_hash": request.event.request_hash,
            "event_hash": request.event.content_hash,
            "policy_digest": request.event.policy_digest,
            "run_authority_hash": request.authority.content_hash,
            "capsule_hash": capsule_spec.content_hash,
            "final_decision_hash": final_decision.content_hash,
            "templar_verdict_hash": None if verdict is None else verdict.content_hash,
            "issued_at_ms": issued_at_ms,
            "valid_until_ms": valid_until_ms,
            "authority": "none",
        }
        unsigned = {"schema": PRE_EXECUTION_PERMIT_SCHEMA, **values}
        return PreExecutionPermit(
            **values,
            seal=_permit_seal(unsigned, self.__key),
        )

    def verify(
        self,
        permit: PreExecutionPermit,
        *,
        spec: RunCapsuleSpec,
        now_ms: int,
    ) -> None:
        if type(permit) is not PreExecutionPermit:
            raise PreExecutionGateError("pre-execution permit is invalid")
        permit.validate_for(spec, now_ms=now_ms)
        expected = _permit_seal(permit.unsigned_dict(), self.__key)
        if not hmac.compare_digest(permit.seal, expected):
            raise PreExecutionGateStale("pre-execution permit was not issued by Fleet")


@dataclass(frozen=True, slots=True)
class PreExecutionOutcome:
    status: str
    gate_request_hash: str
    security_request_hash: str
    event_hash: str
    reason_codes: tuple[str, ...] = ()
    review_reference: str | None = None
    verdict: TemplarVerdict | None = None
    final_decision: FleetFinalDecision | None = None
    permit: PreExecutionPermit | None = None
    capsule_spec: RunCapsuleSpec | None = None

    def __post_init__(self) -> None:
        if self.status not in {READY, DENY, REVIEW}:
            raise PreExecutionGateError("pre-execution outcome status is invalid")
        for value, label in (
            (self.gate_request_hash, "gate request hash"),
            (self.security_request_hash, "security request hash"),
            (self.event_hash, "event hash"),
        ):
            _hash(value, f"pre-execution outcome {label}")
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        if self.status == READY:
            if (
                self.permit is None
                or self.capsule_spec is None
                or self.final_decision is None
            ):
                raise PreExecutionGateError("ready outcome lacks gate permit material")
            if self.review_reference is not None:
                raise PreExecutionGateError(
                    "ready outcome cannot contain review reference"
                )
        else:
            if self.permit is not None or self.capsule_spec is not None:
                raise PreExecutionGateError(
                    "non-ready outcome cannot carry execution material"
                )
        if self.status == REVIEW:
            _identifier(self.review_reference, "pre-execution review reference")


class PreExecutionGate:
    """Compose the mandatory Phase 22 ordering before temporary execution power."""

    def __init__(
        self,
        *,
        authority_store: RunAuthorityStore,
        deterministic_policy: Callable[
            [PreExecutionRequest], DeterministicPolicyDecision
        ],
        destination_admission: Callable[
            [PreExecutionRequest], DestinationAdmissionDecision
        ],
        final_decider: Callable[
            [PreExecutionRequest, DestinationAdmissionDecision, TemplarVerdict | None],
            FleetFinalDecision,
        ],
        context_inspector: Callable[[PreExecutionRequest], PreExecutionContext],
        permit_sealer: PreExecutionPermitSealer,
        templar: TemplarCore | None = None,
        review_router: (
            Callable[[PreExecutionRequest, TemplarVerdict], str] | None
        ) = None,
        now_ms: Callable[[], int] | None = None,
        permit_ttl_ms: int = 60_000,
    ) -> None:
        if type(authority_store) is not RunAuthorityStore:
            raise PreExecutionGateError("RunAuthority store is invalid")
        if type(permit_sealer) is not PreExecutionPermitSealer:
            raise PreExecutionGateError("pre-execution permit sealer is invalid")
        for callback, label in (
            (deterministic_policy, "deterministic policy evaluator"),
            (destination_admission, "destination admission evaluator"),
            (final_decider, "Fleet final decider"),
            (context_inspector, "pre-execution context inspector"),
        ):
            if not callable(callback):
                raise PreExecutionGateError(f"{label} is invalid")
        if templar is not None and type(templar) is not TemplarCore:
            raise PreExecutionGateError("Templar core is invalid")
        if review_router is not None and not callable(review_router):
            raise PreExecutionGateError("review router is invalid")
        _positive_int(
            permit_ttl_ms, "pre-execution permit TTL", maximum=_MAX_PERMIT_TTL_MS
        )
        self._authority_store = authority_store
        self._permit_sealer = permit_sealer
        self._policy = deterministic_policy
        self._admission = destination_admission
        self._final = final_decider
        self._context = context_inspector
        self._templar = templar
        self._review = review_router
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._permit_ttl_ms = permit_ttl_ms

    def _base_outcome(
        self,
        request: PreExecutionRequest,
        *,
        status: str,
        reason_codes: tuple[str, ...] = (),
        review_reference: str | None = None,
        verdict: TemplarVerdict | None = None,
        final_decision: FleetFinalDecision | None = None,
    ) -> PreExecutionOutcome:
        return PreExecutionOutcome(
            status=status,
            gate_request_hash=request.content_hash,
            security_request_hash=request.event.request_hash,
            event_hash=request.event.content_hash,
            reason_codes=reason_codes,
            review_reference=review_reference,
            verdict=verdict,
            final_decision=final_decision,
        )

    def authorize(self, request: PreExecutionRequest) -> PreExecutionOutcome:
        if type(request) is not PreExecutionRequest:
            raise PreExecutionGateError("pre-execution request is invalid")

        initial_now = self._now_ms()
        _positive_int(initial_now, "pre-execution current time")
        initial_context = self._context(request)
        if type(initial_context) is not PreExecutionContext:
            raise PreExecutionGateError("pre-execution context evidence is invalid")
        if initial_context.principal != request.authenticated_principal:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("authenticated-principal-stale",),
            )
        try:
            initial_context.validate_authority(request.authority, now_ms=initial_now)
        except PreExecutionGateStale:
            return self._base_outcome(
                request, status=DENY, reason_codes=("stale-context",)
            )

        try:
            policy = self._policy(request)
        except Exception:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("deterministic-policy-failure",),
            )
        if type(policy) is not DeterministicPolicyDecision:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("deterministic-policy-invalid",),
            )
        try:
            hard_denies = policy.validate_for(request)
        except PreExecutionGateError:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("deterministic-policy-stale",),
            )
        if hard_denies:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=tuple(sorted({item.code for item in hard_denies})),
            )

        try:
            admission = self._admission(request)
        except Exception:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("destination-admission-failure",),
            )
        if type(admission) is not DestinationAdmissionDecision:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("destination-admission-invalid",),
            )
        try:
            admission.validate_for(request)
        except PreExecutionGateError:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("destination-admission-stale",),
            )
        if admission.status != ADMITTED:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=admission.reason_codes,
            )

        verdict: TemplarVerdict | None = None
        if policy.templar_required:
            if self._templar is None:
                return self._base_outcome(
                    request,
                    status=DENY,
                    reason_codes=("templar-unavailable",),
                )
            verdict = self._templar.evaluate(request.event)
            try:
                verdict.validate_for(
                    request.event,
                    templar_policy=self._templar.policy,
                    evaluator=self._templar.evaluator,
                    now_ms=self._now_ms(),
                )
            except Exception:
                return self._base_outcome(
                    request,
                    status=DENY,
                    reason_codes=("templar-verdict-stale",),
                    verdict=verdict,
                )
            if verdict.decision == DENY:
                return self._base_outcome(
                    request,
                    status=DENY,
                    reason_codes=verdict.reason_codes or ("templar-deny",),
                    verdict=verdict,
                )
            if verdict.decision == REVIEW:
                if self._review is None:
                    return self._base_outcome(
                        request,
                        status=DENY,
                        reason_codes=("review-routing-unavailable",),
                        verdict=verdict,
                    )
                try:
                    review_reference = self._review(request, verdict)
                    _identifier(review_reference, "pre-execution review reference")
                except Exception:
                    return self._base_outcome(
                        request,
                        status=DENY,
                        reason_codes=("review-routing-failure",),
                        verdict=verdict,
                    )
                return self._base_outcome(
                    request,
                    status=REVIEW,
                    reason_codes=verdict.reason_codes,
                    review_reference=review_reference,
                    verdict=verdict,
                )
            if verdict.decision != ALLOW:
                return self._base_outcome(
                    request,
                    status=DENY,
                    reason_codes=("templar-verdict-invalid",),
                    verdict=verdict,
                )

        try:
            final_decision = self._final(request, admission, verdict)
        except Exception:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("fleet-final-decision-failure",),
                verdict=verdict,
            )
        if type(final_decision) is not FleetFinalDecision:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("fleet-final-decision-invalid",),
                verdict=verdict,
            )
        try:
            final_decision.validate_for(request, admission, verdict=verdict)
        except PreExecutionGateError:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("fleet-final-decision-stale",),
                verdict=verdict,
            )
        if final_decision.decision != ALLOW:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=final_decision.reason_codes,
                verdict=verdict,
                final_decision=final_decision,
            )

        final_now = self._now_ms()
        _positive_int(final_now, "pre-execution final time")
        try:
            current_context = self._context(request)
            if type(current_context) is not PreExecutionContext:
                raise PreExecutionGateStale("pre-execution context evidence is invalid")
            if current_context != initial_context:
                raise PreExecutionGateStale("pre-execution context changed")
            current_context.validate_authority(request.authority, now_ms=final_now)
        except Exception:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("stale-context",),
                verdict=verdict,
                final_decision=final_decision,
            )

        try:
            self._authority_store.admit(request.authority)
        except RunAuthorityError:
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("authority-admission-failed",),
                verdict=verdict,
                final_decision=final_decision,
            )

        post_admission_now = self._now_ms()
        try:
            post_admission_context = self._context(request)
            if type(post_admission_context) is not PreExecutionContext:
                raise PreExecutionGateStale(
                    "post-admission context evidence is invalid"
                )
            if post_admission_context != initial_context:
                raise PreExecutionGateStale("post-admission context changed")
            post_admission_context.validate_authority(
                request.authority,
                now_ms=post_admission_now,
            )
        except Exception:
            try:
                self._authority_store.cancel(request.authority.content_hash)
            except RunAuthorityError:
                pass
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("stale-context",),
                verdict=verdict,
                final_decision=final_decision,
            )

        try:
            capsule_spec = request.authority.to_capsule_spec()
        except Exception:
            try:
                self._authority_store.cancel(request.authority.content_hash)
            except RunAuthorityError:
                pass
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("permit-issuance-failed",),
                verdict=verdict,
                final_decision=final_decision,
            )
        valid_until_ms = min(
            request.authority.deadline_ms,
            post_admission_now + self._permit_ttl_ms,
        )
        if valid_until_ms <= post_admission_now:
            try:
                self._authority_store.cancel(request.authority.content_hash)
            except RunAuthorityError:
                pass
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("permit-window-expired",),
                verdict=verdict,
                final_decision=final_decision,
            )
        try:
            permit = self._permit_sealer.issue(
                request=request,
                final_decision=final_decision,
                capsule_spec=capsule_spec,
                verdict=verdict,
                issued_at_ms=post_admission_now,
                valid_until_ms=valid_until_ms,
            )
        except Exception:
            try:
                self._authority_store.cancel(request.authority.content_hash)
            except RunAuthorityError:
                pass
            return self._base_outcome(
                request,
                status=DENY,
                reason_codes=("permit-issuance-failed",),
                verdict=verdict,
                final_decision=final_decision,
            )
        return PreExecutionOutcome(
            status=READY,
            gate_request_hash=request.content_hash,
            security_request_hash=request.event.request_hash,
            event_hash=request.event.content_hash,
            verdict=verdict,
            final_decision=final_decision,
            permit=permit,
            capsule_spec=capsule_spec,
        )


__all__ = [
    "ADMITTED",
    "DESTINATION_ADMISSION_SCHEMA",
    "DETERMINISTIC_POLICY_SCHEMA",
    "FLEET_FINAL_DECISION_SCHEMA",
    "OPERATION_HERMES_RUN",
    "PRE_EXECUTION_PERMIT_SCHEMA",
    "PRE_EXECUTION_REQUEST_SCHEMA",
    "READY",
    "DestinationAdmissionDecision",
    "DeterministicPolicyDecision",
    "FleetFinalDecision",
    "PreExecutionContext",
    "PreExecutionGate",
    "PreExecutionGateError",
    "PreExecutionGateStale",
    "PreExecutionOutcome",
    "PreExecutionPermit",
    "PreExecutionPermitSealer",
    "PreExecutionRequest",
]
