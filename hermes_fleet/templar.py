"""Phase 20 low-authority Templar evaluation core.

Templar consumes only the immutable Phase 19 security-event facts and returns
one bounded advisory decision: ALLOW, DENY, or REVIEW. A verdict never grants
execution authority. Deterministic Fleet hard denies retain precedence.

The Phase 21 disposable evaluator sandbox and the Phase 22/23 execution and
learning gate orchestration remain separate modules. This core only validates
and evaluates exact supported immutable Fleet event schemas.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Protocol

from .security_event import (
    DeterministicHardDeny,
    SecurityEvent,
    validate_hard_denies,
)

TEMPLAR_REQUEST_SCHEMA: Final[str] = "fleet.templar-evaluation-request.v1"
TEMPLAR_BACKEND_RESPONSE_SCHEMA: Final[str] = "fleet.templar-backend-response.v1"
TEMPLAR_VERDICT_SCHEMA: Final[str] = "fleet.templar-verdict.v1"

ALLOW: Final[str] = "ALLOW"
DENY: Final[str] = "DENY"
REVIEW: Final[str] = "REVIEW"
DECISIONS: Final[frozenset[str]] = frozenset({ALLOW, DENY, REVIEW})

ORIGIN_EVALUATOR: Final[str] = "evaluator"
ORIGIN_FAIL_CLOSED: Final[str] = "core-fail-closed"
_ORIGINS = frozenset({ORIGIN_EVALUATOR, ORIGIN_FAIL_CLOSED})
_FAIL_CLOSED_REASONS = frozenset(
    {
        "evaluator-timeout",
        "evaluator-failure",
        "response-binding-mismatch",
        "malformed-response",
    }
)

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,511}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_CONTEXT_BYTES = 512 * 1024
_MAX_DOCUMENT_BYTES = 768 * 1024
_MAX_REASON_CODES = 16
_MAX_TIMEOUT_MS = 120_000
_MAX_VERDICT_TTL_MS = 10 * 60_000
_SUPPORTED_EVENT_SCHEMAS = frozenset(
    {"fleet.security-event.v1", "fleet.learning-promotion-event.v1"}
)


class TemplarError(RuntimeError):
    """Templar input, response, or verdict cannot be trusted safely."""


class TemplarMalformedResponse(TemplarError):
    """The evaluator returned an unsupported or ambiguous response."""


class TemplarBindingError(TemplarError):
    """A response or verdict does not bind the exact evaluation request."""


class TemplarStaleVerdict(TemplarError):
    """A Templar verdict is expired or no longer matches current facts."""


def _canonical(
    value: object,
    label: str,
    *,
    maximum: int = _MAX_DOCUMENT_BYTES,
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
        raise TemplarError(f"{label} is not canonical JSON") from error
    if len(payload) > maximum:
        raise TemplarError(f"{label} exceeds the supported bound")
    return payload


def _digest(value: object, label: str) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value, label)).hexdigest()


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise TemplarError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise TemplarError(f"{label} is invalid")
    return value


def _code(value: object, label: str) -> str:
    if type(value) is not str or _CODE_RE.fullmatch(value) is None:
        raise TemplarError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= maximum:
        raise TemplarError(f"{label} is invalid")
    return value


def _reason_codes(value: object) -> tuple[str, ...]:
    if type(value) not in {list, tuple} or len(value) > _MAX_REASON_CODES:
        raise TemplarMalformedResponse("Templar reason codes are invalid")
    normalized = tuple(_code(item, "Templar reason code") for item in value)
    if len(normalized) != len(set(normalized)):
        raise TemplarMalformedResponse("Templar reason codes contain duplicates")
    return tuple(sorted(normalized))


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise TemplarMalformedResponse(f"{label} has an invalid closed schema")
    return value


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain_json(item) for item in value]
    return value


def _event_binding(event: object) -> tuple[str, str, str, dict[str, Any]]:
    """Validate one immutable event against Templar's closed event contract."""

    to_dict = getattr(event, "to_dict", None)
    request_hash = getattr(event, "request_hash", None)
    event_hash = getattr(event, "content_hash", None)
    policy_digest = getattr(event, "policy_digest", None)
    if not callable(to_dict):
        raise TemplarError("Templar event does not expose a canonical document")
    _hash(request_hash, "Templar event request hash")
    _hash(event_hash, "Templar event content hash")
    _hash(policy_digest, "Templar event policy digest")
    document = to_dict()
    if (
        type(document) is not dict
        or document.get("schema") not in _SUPPORTED_EVENT_SCHEMAS
    ):
        raise TemplarError("Templar event schema is unsupported")
    if document.get("request_hash") != request_hash:
        raise TemplarBindingError("Templar event request hash is inconsistent")
    if _digest(document, "Templar event") != event_hash:
        raise TemplarBindingError("Templar event content hash is inconsistent")
    request = document.get("request")
    if type(request) is not dict or request.get("policy_digest") != policy_digest:
        raise TemplarBindingError("Templar event Fleet policy is inconsistent")
    return request_hash, event_hash, policy_digest, document


@dataclass(frozen=True, slots=True)
class TemplarPolicyRef:
    policy_id: str
    policy_version: str
    policy_digest: str

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "Templar policy id")
        _identifier(self.policy_version, "Templar policy version")
        _hash(self.policy_digest, "Templar policy digest")

    def to_dict(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> TemplarPolicyRef:
        item = _exact_object(
            value,
            {"policy_id", "policy_version", "policy_digest"},
            "Templar policy",
        )
        return cls(
            policy_id=item["policy_id"],
            policy_version=item["policy_version"],
            policy_digest=item["policy_digest"],
        )


@dataclass(frozen=True, slots=True)
class TemplarEvaluatorIdentity:
    evaluator_id: str
    implementation_version: str
    model_provider: str
    model_name: str
    model_version: str

    def __post_init__(self) -> None:
        _identifier(self.evaluator_id, "Templar evaluator id")
        _identifier(self.implementation_version, "Templar implementation version")
        _identifier(self.model_provider, "Templar model provider")
        _identifier(self.model_name, "Templar model name")
        _identifier(self.model_version, "Templar model version")

    def to_dict(self) -> dict[str, str]:
        return {
            "evaluator_id": self.evaluator_id,
            "implementation_version": self.implementation_version,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "model_version": self.model_version,
        }

    @classmethod
    def from_dict(cls, value: object) -> TemplarEvaluatorIdentity:
        item = _exact_object(
            value,
            {
                "evaluator_id",
                "implementation_version",
                "model_provider",
                "model_name",
                "model_version",
            },
            "Templar evaluator identity",
        )
        return cls(
            evaluator_id=item["evaluator_id"],
            implementation_version=item["implementation_version"],
            model_provider=item["model_provider"],
            model_name=item["model_name"],
            model_version=item["model_version"],
        )


@dataclass(frozen=True, slots=True)
class TemplarEvaluationRequest:
    """Bounded sanitized facts supplied to one evaluator invocation."""

    request_hash: str
    event_hash: str
    fleet_policy_digest: str
    templar_policy: TemplarPolicyRef
    evaluator: TemplarEvaluatorIdentity
    issued_at_ms: int
    deadline_ms: int
    event: Mapping[str, Any]

    def __post_init__(self) -> None:
        _hash(self.request_hash, "Templar request hash")
        _hash(self.event_hash, "Templar event hash")
        _hash(self.fleet_policy_digest, "Fleet policy digest")
        if type(self.templar_policy) is not TemplarPolicyRef:
            raise TemplarError("Templar policy is invalid")
        if type(self.evaluator) is not TemplarEvaluatorIdentity:
            raise TemplarError("Templar evaluator identity is invalid")
        _positive_int(self.issued_at_ms, "Templar request issue time")
        _positive_int(self.deadline_ms, "Templar request deadline")
        if self.deadline_ms <= self.issued_at_ms:
            raise TemplarError("Templar request deadline must follow issuance")
        if not isinstance(self.event, Mapping):
            raise TemplarError("Templar event document is invalid")
        event = json.loads(
            _canonical(
                _plain_json(self.event),
                "Templar security-event context",
                maximum=_MAX_CONTEXT_BYTES,
            ).decode("utf-8")
        )
        object.__setattr__(self, "event", _freeze_json(event))
        if event.get("schema") not in _SUPPORTED_EVENT_SCHEMAS:
            raise TemplarError("Templar context event schema is unsupported")
        if event.get("request_hash") != self.request_hash:
            raise TemplarBindingError("Templar context request hash does not match")
        if _digest(event, "Templar security-event context") != self.event_hash:
            raise TemplarBindingError("Templar context event hash does not match")
        request = event.get("request")
        if (
            type(request) is not dict
            or request.get("policy_digest") != self.fleet_policy_digest
        ):
            raise TemplarBindingError("Templar context Fleet policy does not match")

    @classmethod
    def from_event(
        cls,
        event: object,
        *,
        templar_policy: TemplarPolicyRef,
        evaluator: TemplarEvaluatorIdentity,
        issued_at_ms: int,
        deadline_ms: int,
    ) -> TemplarEvaluationRequest:
        request_hash, event_hash, policy_digest, document = _event_binding(event)
        return cls(
            request_hash=request_hash,
            event_hash=event_hash,
            fleet_policy_digest=policy_digest,
            templar_policy=templar_policy,
            evaluator=evaluator,
            issued_at_ms=issued_at_ms,
            deadline_ms=deadline_ms,
            event=document,
        )

    def request_document(self) -> dict[str, object]:
        return {
            "schema": TEMPLAR_REQUEST_SCHEMA,
            "request_hash": self.request_hash,
            "event_hash": self.event_hash,
            "fleet_policy_digest": self.fleet_policy_digest,
            "templar_policy": self.templar_policy.to_dict(),
            "evaluator": self.evaluator.to_dict(),
            "issued_at_ms": self.issued_at_ms,
            "deadline_ms": self.deadline_ms,
            "event": _plain_json(self.event),
        }

    @property
    def evaluation_id(self) -> str:
        return _digest(self.request_document(), "Templar evaluation request")

    def to_dict(self) -> dict[str, object]:
        return {
            **self.request_document(),
            "evaluation_id": self.evaluation_id,
        }


@dataclass(frozen=True, slots=True)
class TemplarBackendResponse:
    evaluation_id: str
    request_hash: str
    event_hash: str
    decision: str
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _hash(self.evaluation_id, "Templar evaluation id")
        _hash(self.request_hash, "Templar response request hash")
        _hash(self.event_hash, "Templar response event hash")
        if self.decision not in DECISIONS:
            raise TemplarMalformedResponse("Templar decision is unsupported")
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        if self.decision in {DENY, REVIEW} and not self.reason_codes:
            raise TemplarMalformedResponse(
                "Templar DENY/REVIEW requires at least one bounded reason code"
            )

    @classmethod
    def from_dict(cls, value: object) -> TemplarBackendResponse:
        item = _exact_object(
            value,
            {
                "schema",
                "evaluation_id",
                "request_hash",
                "event_hash",
                "decision",
                "reason_codes",
            },
            "Templar backend response",
        )
        if item["schema"] != TEMPLAR_BACKEND_RESPONSE_SCHEMA:
            raise TemplarMalformedResponse(
                "Templar backend response schema is unsupported"
            )
        return cls(
            evaluation_id=item["evaluation_id"],
            request_hash=item["request_hash"],
            event_hash=item["event_hash"],
            decision=item["decision"],
            reason_codes=item["reason_codes"],
        )

    def validate_request(self, request: TemplarEvaluationRequest) -> None:
        if type(request) is not TemplarEvaluationRequest:
            raise TemplarBindingError("Templar response request is invalid")
        if (
            self.evaluation_id != request.evaluation_id
            or self.request_hash != request.request_hash
            or self.event_hash != request.event_hash
        ):
            raise TemplarBindingError(
                "Templar response is request-substituted or stale"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": TEMPLAR_BACKEND_RESPONSE_SCHEMA,
            "evaluation_id": self.evaluation_id,
            "request_hash": self.request_hash,
            "event_hash": self.event_hash,
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class TemplarVerdict:
    evaluation_id: str
    request_hash: str
    event_hash: str
    fleet_policy_digest: str
    templar_policy: TemplarPolicyRef
    evaluator: TemplarEvaluatorIdentity
    decision: str
    reason_codes: tuple[str, ...]
    origin: str
    evaluation_issued_at_ms: int
    evaluation_deadline_ms: int
    issued_at_ms: int
    valid_until_ms: int
    authority: str = field(default="none")

    def __post_init__(self) -> None:
        _hash(self.evaluation_id, "Templar verdict evaluation id")
        _hash(self.request_hash, "Templar verdict request hash")
        _hash(self.event_hash, "Templar verdict event hash")
        _hash(self.fleet_policy_digest, "Templar verdict Fleet policy digest")
        if type(self.templar_policy) is not TemplarPolicyRef:
            raise TemplarError("Templar verdict policy is invalid")
        if type(self.evaluator) is not TemplarEvaluatorIdentity:
            raise TemplarError("Templar verdict evaluator is invalid")
        if self.decision not in DECISIONS:
            raise TemplarError("Templar verdict decision is unsupported")
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        if self.decision in {DENY, REVIEW} and not self.reason_codes:
            raise TemplarError("Templar DENY/REVIEW verdict requires a reason code")
        if self.origin not in _ORIGINS:
            raise TemplarError("Templar verdict origin is invalid")
        if self.origin == ORIGIN_FAIL_CLOSED:
            if self.decision != DENY:
                raise TemplarError("fail-closed Templar verdict must be DENY")
            if (
                len(self.reason_codes) != 1
                or self.reason_codes[0] not in _FAIL_CLOSED_REASONS
            ):
                raise TemplarError("fail-closed Templar reason is invalid")
        _positive_int(
            self.evaluation_issued_at_ms,
            "Templar evaluation issue time",
        )
        _positive_int(
            self.evaluation_deadline_ms,
            "Templar evaluation deadline",
        )
        if self.evaluation_deadline_ms <= self.evaluation_issued_at_ms:
            raise TemplarError("Templar evaluation deadline must follow issuance")
        _positive_int(self.issued_at_ms, "Templar verdict issue time")
        if self.issued_at_ms < self.evaluation_issued_at_ms:
            raise TemplarError("Templar verdict predates its evaluation")
        if (
            self.origin == ORIGIN_EVALUATOR
            and self.issued_at_ms >= self.evaluation_deadline_ms
        ):
            raise TemplarError("evaluator verdict was issued after its deadline")
        _positive_int(self.valid_until_ms, "Templar verdict expiry")
        if self.valid_until_ms <= self.issued_at_ms:
            raise TemplarError("Templar verdict expiry must follow issuance")
        if self.authority != "none":
            raise TemplarError("Templar verdict cannot carry authority")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": TEMPLAR_VERDICT_SCHEMA,
            "evaluation_id": self.evaluation_id,
            "request_hash": self.request_hash,
            "event_hash": self.event_hash,
            "fleet_policy_digest": self.fleet_policy_digest,
            "templar_policy": self.templar_policy.to_dict(),
            "evaluator": self.evaluator.to_dict(),
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
            "origin": self.origin,
            "evaluation_issued_at_ms": self.evaluation_issued_at_ms,
            "evaluation_deadline_ms": self.evaluation_deadline_ms,
            "issued_at_ms": self.issued_at_ms,
            "valid_until_ms": self.valid_until_ms,
            "authority": self.authority,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "Templar verdict")

    @classmethod
    def from_dict(cls, value: object) -> TemplarVerdict:
        item = _exact_object(
            value,
            {
                "schema",
                "evaluation_id",
                "request_hash",
                "event_hash",
                "fleet_policy_digest",
                "templar_policy",
                "evaluator",
                "decision",
                "reason_codes",
                "origin",
                "evaluation_issued_at_ms",
                "evaluation_deadline_ms",
                "issued_at_ms",
                "valid_until_ms",
                "authority",
            },
            "Templar verdict",
        )
        if item["schema"] != TEMPLAR_VERDICT_SCHEMA:
            raise TemplarError("Templar verdict schema is unsupported")
        return cls(
            evaluation_id=item["evaluation_id"],
            request_hash=item["request_hash"],
            event_hash=item["event_hash"],
            fleet_policy_digest=item["fleet_policy_digest"],
            templar_policy=TemplarPolicyRef.from_dict(item["templar_policy"]),
            evaluator=TemplarEvaluatorIdentity.from_dict(item["evaluator"]),
            decision=item["decision"],
            reason_codes=item["reason_codes"],
            origin=item["origin"],
            evaluation_issued_at_ms=item["evaluation_issued_at_ms"],
            evaluation_deadline_ms=item["evaluation_deadline_ms"],
            issued_at_ms=item["issued_at_ms"],
            valid_until_ms=item["valid_until_ms"],
            authority=item["authority"],
        )

    def validate_for(
        self,
        event: object,
        *,
        templar_policy: TemplarPolicyRef,
        evaluator: TemplarEvaluatorIdentity,
        now_ms: int,
    ) -> None:
        try:
            request_hash, event_hash, policy_digest, _document = _event_binding(event)
        except TemplarError as error:
            raise TemplarStaleVerdict("Templar verdict event is invalid") from error
        _positive_int(now_ms, "Templar verdict validation time")
        if (
            self.request_hash != request_hash
            or self.event_hash != event_hash
            or self.fleet_policy_digest != policy_digest
        ):
            raise TemplarStaleVerdict(
                "Templar verdict no longer matches security event or evaluated "
                "learning event"
            )
        if self.templar_policy != templar_policy:
            raise TemplarStaleVerdict("Templar verdict policy is stale")
        if self.evaluator != evaluator:
            raise TemplarStaleVerdict("Templar verdict evaluator identity is stale")
        expected_request = TemplarEvaluationRequest.from_event(
            event,
            templar_policy=templar_policy,
            evaluator=evaluator,
            issued_at_ms=self.evaluation_issued_at_ms,
            deadline_ms=self.evaluation_deadline_ms,
        )
        if self.evaluation_id != expected_request.evaluation_id:
            raise TemplarStaleVerdict("Templar verdict evaluation binding is stale")
        if now_ms < self.issued_at_ms or now_ms >= self.valid_until_ms:
            raise TemplarStaleVerdict("Templar verdict is stale")


class TemplarBackend(Protocol):
    """One bounded evaluator call; Phase 21 supplies the disposable sandbox."""

    def evaluate(
        self,
        request: Mapping[str, Any],
        *,
        timeout_ms: int,
    ) -> Mapping[str, Any]: ...


class TemplarCore:
    """Low-authority evaluator contract around one exact supported Fleet event."""

    def __init__(
        self,
        *,
        backend: TemplarBackend,
        policy: TemplarPolicyRef,
        evaluator: TemplarEvaluatorIdentity,
        timeout_ms: int = 10_000,
        verdict_ttl_ms: int = 60_000,
        wall_clock_ms: Callable[[], int] | None = None,
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        if not hasattr(backend, "evaluate"):
            raise TemplarError("Templar backend is invalid")
        if type(policy) is not TemplarPolicyRef:
            raise TemplarError("Templar policy is invalid")
        if type(evaluator) is not TemplarEvaluatorIdentity:
            raise TemplarError("Templar evaluator identity is invalid")
        _positive_int(timeout_ms, "Templar timeout", maximum=_MAX_TIMEOUT_MS)
        _positive_int(
            verdict_ttl_ms,
            "Templar verdict TTL",
            maximum=_MAX_VERDICT_TTL_MS,
        )
        self._backend = backend
        self.policy = policy
        self.evaluator = evaluator
        self.timeout_ms = timeout_ms
        self.verdict_ttl_ms = verdict_ttl_ms
        self._wall_clock_ms = wall_clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic_ms = monotonic_ms or (lambda: time.monotonic_ns() // 1_000_000)

    def evaluate(self, event: object) -> TemplarVerdict:
        issued_at_ms = self._wall_clock_ms()
        _positive_int(issued_at_ms, "Templar current time")
        request = TemplarEvaluationRequest.from_event(
            event,
            templar_policy=self.policy,
            evaluator=self.evaluator,
            issued_at_ms=issued_at_ms,
            deadline_ms=issued_at_ms + self.timeout_ms,
        )
        started_ms = self._monotonic_ms()
        try:
            raw_response = self._backend.evaluate(
                request.to_dict(),
                timeout_ms=self.timeout_ms,
            )
        except TimeoutError:
            return self._fail_closed(request, "evaluator-timeout")
        except Exception:
            return self._fail_closed(request, "evaluator-failure")

        elapsed_ms = self._monotonic_ms() - started_ms
        if elapsed_ms < 0 or elapsed_ms >= self.timeout_ms:
            return self._fail_closed(request, "evaluator-timeout")
        try:
            response = TemplarBackendResponse.from_dict(raw_response)
            response.validate_request(request)
        except TemplarBindingError:
            return self._fail_closed(request, "response-binding-mismatch")
        except (TemplarMalformedResponse, TemplarError, TypeError, ValueError):
            return self._fail_closed(request, "malformed-response")

        completed_at_ms = self._wall_clock_ms()
        _positive_int(completed_at_ms, "Templar completion time")
        if completed_at_ms >= request.deadline_ms:
            return self._fail_closed(request, "evaluator-timeout")
        return TemplarVerdict(
            evaluation_id=request.evaluation_id,
            request_hash=request.request_hash,
            event_hash=request.event_hash,
            fleet_policy_digest=request.fleet_policy_digest,
            templar_policy=request.templar_policy,
            evaluator=request.evaluator,
            decision=response.decision,
            reason_codes=response.reason_codes,
            origin=ORIGIN_EVALUATOR,
            evaluation_issued_at_ms=request.issued_at_ms,
            evaluation_deadline_ms=request.deadline_ms,
            issued_at_ms=completed_at_ms,
            valid_until_ms=completed_at_ms + self.verdict_ttl_ms,
        )

    def _fail_closed(
        self,
        request: TemplarEvaluationRequest,
        reason_code: str,
    ) -> TemplarVerdict:
        completed_at_ms = self._wall_clock_ms()
        _positive_int(completed_at_ms, "Templar fail-closed time")
        return TemplarVerdict(
            evaluation_id=request.evaluation_id,
            request_hash=request.request_hash,
            event_hash=request.event_hash,
            fleet_policy_digest=request.fleet_policy_digest,
            templar_policy=request.templar_policy,
            evaluator=request.evaluator,
            decision=DENY,
            reason_codes=(_code(reason_code, "Templar fail-closed reason"),),
            origin=ORIGIN_FAIL_CLOSED,
            evaluation_issued_at_ms=request.issued_at_ms,
            evaluation_deadline_ms=request.deadline_ms,
            issued_at_ms=completed_at_ms,
            valid_until_ms=completed_at_ms + self.verdict_ttl_ms,
        )


def resolve_templar_disposition(
    event: SecurityEvent,
    verdict: TemplarVerdict,
    *,
    hard_denies: tuple[DeterministicHardDeny, ...] | list[DeterministicHardDeny] = (),
    templar_policy: TemplarPolicyRef,
    evaluator: TemplarEvaluatorIdentity,
    now_ms: int,
) -> str:
    """Return advisory security disposition; never execution authorization.

    A valid deterministic Fleet hard deny always wins over Templar, including
    over an evaluator ALLOW. Phase 22 still owns pre-execution gate ordering and
    Fleet's final authorization decision.
    """

    validated_denies = validate_hard_denies(event, hard_denies)
    if validated_denies:
        return DENY
    if type(verdict) is not TemplarVerdict:
        raise TemplarError("Templar verdict is invalid")
    verdict.validate_for(
        event,
        templar_policy=templar_policy,
        evaluator=evaluator,
        now_ms=now_ms,
    )
    return verdict.decision


__all__ = [
    "ALLOW",
    "DENY",
    "REVIEW",
    "DECISIONS",
    "ORIGIN_EVALUATOR",
    "ORIGIN_FAIL_CLOSED",
    "TEMPLAR_BACKEND_RESPONSE_SCHEMA",
    "TEMPLAR_REQUEST_SCHEMA",
    "TEMPLAR_VERDICT_SCHEMA",
    "TemplarBackend",
    "TemplarBackendResponse",
    "TemplarBindingError",
    "TemplarCore",
    "TemplarError",
    "TemplarEvaluationRequest",
    "TemplarEvaluatorIdentity",
    "TemplarMalformedResponse",
    "TemplarPolicyRef",
    "TemplarStaleVerdict",
    "TemplarVerdict",
    "resolve_templar_disposition",
]
