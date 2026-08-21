"""Phase 23 Templar gate for durable learning promotion.

The gate consumes only the exact sanitized material prepared by Hermes Phase 18,
binds Templar to the exact final candidate hash, preserves deterministic skill
verification evidence, and calls Fleet promotion policy only after an evaluator
ALLOW. Templar itself never receives a mutation callback or promotion authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from .principal_identity import PrincipalRecord, PrincipalReference
from .promotion import (
    PromotionAuthorization,
    PromotionError,
    PromotionScopeRef,
    authorize_promotion,
    validate_promotion_policy,
)
from .templar import ALLOW, DENY, REVIEW, TemplarCore, TemplarVerdict

LEARNING_PROMOTION_REQUEST_SCHEMA: Final[str] = "fleet.learning-promotion-request.v1"
LEARNING_PROMOTION_EVENT_SCHEMA: Final[str] = "fleet.learning-promotion-event.v1"
PROMOTION_EVALUATION_MATERIAL_SCHEMA: Final[str] = (
    "fleet.promotion-evaluation-material.v1"
)
READY: Final[str] = "ready"

LEARNING_EVALUATION_CATEGORIES: Final[tuple[str, ...]] = (
    "hidden-instructions",
    "social-engineering",
    "exfiltration-intent",
    "disguised-privilege-escalation",
    "dangerous-combinations",
    "suspicious-secret-handling",
)

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_MATERIAL_BYTES = 320 * 1024
_MAX_FILES = 128
_MAX_REASON_CODES = 16

_UNREDACTED_CREDENTIAL_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(
        r"\bBearer\s+eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        re.I,
    ),
)

_SIGNAL_PATTERNS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "hidden-instructions": (
        re.compile(
            r"\bignore\b.{0,80}\b(?:previous|prior|system|developer)\b"
            r".{0,40}\binstruction",
            re.I | re.S,
        ),
        re.compile(r"\b(?:hidden|secret)\s+instruction", re.I),
        re.compile(r"\bsystem\s+prompt\b", re.I),
        re.compile(r"\bdo\s+not\s+(?:tell|reveal|show)\b", re.I),
    ),
    "social-engineering": (
        re.compile(r"\bimpersonat(?:e|es|ing|ion)\b", re.I),
        re.compile(r"\bpretend\s+to\s+be\b", re.I),
        re.compile(r"\bsocial\s+engineer", re.I),
        re.compile(
            r"\bconvince\b.{0,80}\b(?:user|operator|admin(?:istrator)?)\b",
            re.I | re.S,
        ),
    ),
    "exfiltration-intent": (
        re.compile(r"\bexfiltrat(?:e|es|ing|ion)\b", re.I),
        re.compile(
            r"\b(?:send|upload|post|transmit|forward)\b.{0,100}"
            r"\b(?:secret|token|password|credential|private\s+key|api\s+key|cookie)\b",
            re.I | re.S,
        ),
    ),
    "disguised-privilege-escalation": (
        re.compile(r"\bprivilege\s+escalat", re.I),
        re.compile(
            r"\bbypass\b.{0,60}\b(?:fleet|templar|approval|policy|sandbox|authoriz)",
            re.I | re.S,
        ),
        re.compile(r"/var/run/docker\.sock", re.I),
        re.compile(r"\bsetuid\b|\bchmod\s+[+]?[us]\b", re.I),
        re.compile(r"\bmount\b.{0,50}\bhost\b", re.I | re.S),
    ),
    "suspicious-secret-handling": (
        re.compile(
            r"\b(?:read|collect|dump|copy|capture|extract|store)\b.{0,100}"
            r"\b(?:secret|token|password|credential|private\s+key|api\s+key|cookie)\b",
            re.I | re.S,
        ),
    ),
}


class LearningPromotionGateError(RuntimeError):
    """Phase 23 learning material, binding, or gate state is unsafe."""


def _canonical(value: object, label: str, *, maximum: int = 768 * 1024) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise LearningPromotionGateError(f"{label} is not canonical JSON") from error
    if len(payload) > maximum:
        raise LearningPromotionGateError(f"{label} exceeds the supported bound")
    return payload


def _digest(value: object, label: str) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value, label)).hexdigest()


def _raw_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise LearningPromotionGateError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise LearningPromotionGateError(f"{label} is invalid")
    return value


def _reason_codes(value: object) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or len(value) > _MAX_REASON_CODES:
        raise LearningPromotionGateError("learning gate reason codes are invalid")
    items = tuple(value)
    if any(type(item) is not str or _CODE_RE.fullmatch(item) is None for item in items):
        raise LearningPromotionGateError("learning gate reason code is invalid")
    if len(set(items)) != len(items):
        raise LearningPromotionGateError(
            "learning gate reason codes contain duplicates"
        )
    return tuple(sorted(items))


@dataclass(frozen=True, slots=True)
class PromotionEvaluationFile:
    path: str
    sha256: str
    bytes: int
    text: str

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path or len(self.path) > 512:
            raise LearningPromotionGateError("skill evaluation path is invalid")
        parsed = PurePosixPath(self.path)
        if parsed.is_absolute() or ".." in parsed.parts or "." in parsed.parts:
            raise LearningPromotionGateError("skill evaluation path is unsafe")
        _hash(self.sha256, "skill evaluation file hash")
        if (
            isinstance(self.bytes, bool)
            or type(self.bytes) is not int
            or self.bytes < 0
        ):
            raise LearningPromotionGateError("skill evaluation file size is invalid")
        if type(self.text) is not str:
            raise LearningPromotionGateError("skill evaluation text is invalid")
        encoded = self.text.encode("utf-8")
        if len(encoded) != self.bytes or _raw_digest(encoded) != self.sha256:
            raise LearningPromotionGateError(
                "skill evaluation file does not match its hash"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "text": self.text,
        }

    def manifest_entry(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class PromotionEvaluationMaterial:
    kind: str
    content_hash: str
    text: str | None = None
    files: tuple[PromotionEvaluationFile, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {"memory", "skill"}:
            raise LearningPromotionGateError("promotion evaluation kind is invalid")
        _hash(self.content_hash, "promotion evaluation content hash")
        if self.kind == "memory":
            if type(self.text) is not str or self.files:
                raise LearningPromotionGateError(
                    "memory evaluation material is invalid"
                )
            if _raw_digest(self.text.encode("utf-8")) != self.content_hash:
                raise LearningPromotionGateError(
                    "memory evaluation material does not match candidate hash"
                )
        else:
            if self.text is not None or not self.files or len(self.files) > _MAX_FILES:
                raise LearningPromotionGateError("skill evaluation material is invalid")
            paths = tuple(item.path for item in self.files)
            if len(paths) != len(set(paths)) or paths != tuple(sorted(paths)):
                raise LearningPromotionGateError(
                    "skill evaluation file order is invalid"
                )
            if "SKILL.md" not in paths:
                raise LearningPromotionGateError(
                    "skill evaluation material lacks SKILL.md"
                )
            manifest = [item.manifest_entry() for item in self.files]
            if _digest(manifest, "skill evaluation manifest") != self.content_hash:
                raise LearningPromotionGateError(
                    "skill evaluation material does not match candidate hash"
                )
        _canonical(
            self.to_dict(),
            "promotion evaluation material",
            maximum=_MAX_MATERIAL_BYTES,
        )

    @classmethod
    def from_document(
        cls, value: object, *, expected_kind: str, candidate_hash: str
    ) -> PromotionEvaluationMaterial:
        if type(value) is not dict:
            raise LearningPromotionGateError("promotion evaluation material is invalid")
        if value.get("schema") != PROMOTION_EVALUATION_MATERIAL_SCHEMA:
            raise LearningPromotionGateError(
                "promotion evaluation material schema is unsupported"
            )
        if (
            value.get("kind") != expected_kind
            or value.get("content_hash") != candidate_hash
        ):
            raise LearningPromotionGateError(
                "promotion evaluation material binding changed"
            )
        if expected_kind == "memory":
            if set(value) != {"schema", "kind", "content_hash", "bytes", "text"}:
                raise LearningPromotionGateError(
                    "memory evaluation material shape is invalid"
                )
            text = value["text"]
            encoded = text.encode("utf-8") if type(text) is str else b""
            if (
                isinstance(value["bytes"], bool)
                or type(value["bytes"]) is not int
                or value["bytes"] != len(encoded)
            ):
                raise LearningPromotionGateError(
                    "memory evaluation material size is invalid"
                )
            return cls(kind="memory", content_hash=candidate_hash, text=text)
        if expected_kind != "skill" or set(value) != {
            "schema",
            "kind",
            "content_hash",
            "files",
        }:
            raise LearningPromotionGateError(
                "skill evaluation material shape is invalid"
            )
        raw_files = value["files"]
        if type(raw_files) is not list:
            raise LearningPromotionGateError("skill evaluation files are invalid")
        files: list[PromotionEvaluationFile] = []
        for item in raw_files:
            if type(item) is not dict or set(item) != {
                "path",
                "sha256",
                "bytes",
                "text",
            }:
                raise LearningPromotionGateError(
                    "skill evaluation file shape is invalid"
                )
            files.append(
                PromotionEvaluationFile(
                    path=item["path"],
                    sha256=item["sha256"],
                    bytes=item["bytes"],
                    text=item["text"],
                )
            )
        return cls(kind="skill", content_hash=candidate_hash, files=tuple(files))

    def all_text(self) -> str:
        if self.kind == "memory":
            return self.text or ""
        return "\n\n".join(f"[{item.path}]\n{item.text}" for item in self.files)

    def has_unredacted_credential(self) -> bool:
        text = self.all_text()
        return any(
            pattern.search(text) is not None
            for pattern in _UNREDACTED_CREDENTIAL_PATTERNS
        )

    def risk_signals(self) -> tuple[str, ...]:
        text = self.all_text()
        signals = {
            code
            for code, patterns in _SIGNAL_PATTERNS.items()
            if any(pattern.search(text) is not None for pattern in patterns)
        }
        if len(signals) >= 2:
            signals.add("dangerous-combinations")
        return tuple(sorted(signals))

    def to_dict(self) -> dict[str, object]:
        if self.kind == "memory":
            assert self.text is not None
            encoded = self.text.encode("utf-8")
            return {
                "schema": PROMOTION_EVALUATION_MATERIAL_SCHEMA,
                "kind": self.kind,
                "content_hash": self.content_hash,
                "bytes": len(encoded),
                "text": self.text,
            }
        return {
            "schema": PROMOTION_EVALUATION_MATERIAL_SCHEMA,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "files": [item.to_dict() for item in self.files],
        }


@dataclass(frozen=True, slots=True)
class LearningPromotionRequest:
    subject_kind: str
    subject_key: str
    source_owner_principal_id: str
    agent_instance_id: str
    source_scope: PromotionScopeRef
    target_scope: PromotionScopeRef
    source_content_hash: str
    approved_content_hash: str
    administrator: PrincipalReference
    policy_digest: str
    sanitized: bool
    evaluation_material: PromotionEvaluationMaterial
    verification_digest: str | None = None
    expected_current_promotion_id: str | None = None

    def __post_init__(self) -> None:
        if self.subject_kind not in {"memory", "skill"}:
            raise LearningPromotionGateError(
                "learning promotion subject kind is invalid"
            )
        _identifier(self.subject_key, "learning promotion subject key")
        for value, label in (
            (self.source_owner_principal_id, "source owner principal"),
            (self.agent_instance_id, "Agent Instance"),
            (self.source_content_hash, "source content hash"),
            (self.approved_content_hash, "candidate hash"),
            (self.policy_digest, "policy digest"),
        ):
            _hash(value, f"learning promotion {label}")
        if (
            type(self.source_scope) is not PromotionScopeRef
            or type(self.target_scope) is not PromotionScopeRef
        ):
            raise LearningPromotionGateError("learning promotion scope is invalid")
        if type(self.administrator) is not PrincipalReference:
            raise LearningPromotionGateError(
                "learning promotion administrator is invalid"
            )
        if type(self.sanitized) is not bool:
            raise LearningPromotionGateError(
                "learning promotion sanitation flag is invalid"
            )
        if type(self.evaluation_material) is not PromotionEvaluationMaterial:
            raise LearningPromotionGateError(
                "learning promotion evaluation material is invalid"
            )
        if (
            self.evaluation_material.kind != self.subject_kind
            or self.evaluation_material.content_hash != self.approved_content_hash
        ):
            raise LearningPromotionGateError(
                "learning promotion candidate material is not exact"
            )
        if self.subject_kind == "skill":
            _hash(
                self.verification_digest,
                "learning promotion verification digest",
            )
        elif self.verification_digest is not None:
            raise LearningPromotionGateError(
                "memory promotion cannot carry skill verification"
            )
        if self.expected_current_promotion_id is not None:
            _hash(self.expected_current_promotion_id, "current promotion id")

    @classmethod
    def from_prepared(
        cls,
        prepared: object,
        *,
        source_owner_principal_id: str,
        agent_instance_id: str,
        source_scope: PromotionScopeRef,
        target_scope: PromotionScopeRef,
        administrator: PrincipalReference,
        policy_digest: str,
        expected_current_promotion_id: str | None = None,
    ) -> LearningPromotionRequest:
        expected = {
            "subject_kind",
            "subject_key",
            "source_content_hash",
            "approved_content_hash",
            "sanitized",
            "evaluation_material",
            "verification_digest",
            "authority",
        }
        if (
            type(prepared) is not dict
            or set(prepared) != expected
            or prepared.get("authority") != "none"
        ):
            raise LearningPromotionGateError(
                "prepared promotion has an invalid closed shape"
            )
        subject_kind = prepared["subject_kind"]
        if subject_kind not in {"memory", "skill"}:
            raise LearningPromotionGateError("prepared promotion kind is invalid")
        candidate_hash = _hash(
            prepared["approved_content_hash"], "prepared candidate hash"
        )
        material = PromotionEvaluationMaterial.from_document(
            prepared["evaluation_material"],
            expected_kind=subject_kind,
            candidate_hash=candidate_hash,
        )
        return cls(
            subject_kind=subject_kind,
            subject_key=prepared["subject_key"],
            source_owner_principal_id=source_owner_principal_id,
            agent_instance_id=agent_instance_id,
            source_scope=source_scope,
            target_scope=target_scope,
            source_content_hash=prepared["source_content_hash"],
            approved_content_hash=candidate_hash,
            administrator=administrator,
            policy_digest=policy_digest,
            sanitized=prepared["sanitized"],
            evaluation_material=material,
            verification_digest=prepared["verification_digest"],
            expected_current_promotion_id=expected_current_promotion_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": LEARNING_PROMOTION_REQUEST_SCHEMA,
            "subject_kind": self.subject_kind,
            "subject_key": self.subject_key,
            "source_owner_principal_id": self.source_owner_principal_id,
            "agent_instance_id": self.agent_instance_id,
            "source_scope": self.source_scope.to_request(),
            "target_scope": self.target_scope.to_request(),
            "source_content_hash": self.source_content_hash,
            "approved_content_hash": self.approved_content_hash,
            "candidate_hash": self.approved_content_hash,
            "administrator": {
                "principal_id": self.administrator.principal_id,
                "kind": self.administrator.kind,
                "generation": self.administrator.generation,
                "binding_hash": self.administrator.binding_hash,
            },
            "policy_digest": self.policy_digest,
            "sanitized": self.sanitized,
            "evaluation_material": self.evaluation_material.to_dict(),
            "verification_digest": self.verification_digest,
            "expected_current_promotion_id": self.expected_current_promotion_id,
            "authority": "none",
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "learning promotion request")

    def event(self) -> LearningPromotionEvent:
        return LearningPromotionEvent(
            request=self,
            risk_signals=self.evaluation_material.risk_signals(),
        )


@dataclass(frozen=True, slots=True)
class LearningPromotionEvent:
    request: LearningPromotionRequest
    risk_signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.request) is not LearningPromotionRequest:
            raise LearningPromotionGateError(
                "learning promotion event request is invalid"
            )
        signals = _reason_codes(self.risk_signals)
        if any(signal not in LEARNING_EVALUATION_CATEGORIES for signal in signals):
            raise LearningPromotionGateError(
                "learning promotion risk signal is unsupported"
            )
        object.__setattr__(self, "risk_signals", signals)

    @property
    def request_hash(self) -> str:
        return self.request.content_hash

    @property
    def policy_digest(self) -> str:
        return self.request.policy_digest

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": LEARNING_PROMOTION_EVENT_SCHEMA,
            "request_hash": self.request_hash,
            "request": self.request.to_dict(),
            "evaluation_categories": list(LEARNING_EVALUATION_CATEGORIES),
            "risk_signals": list(self.risk_signals),
            "authority": "none",
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "learning promotion event")


@dataclass(frozen=True, slots=True)
class LearningPromotionOutcome:
    status: str
    request_hash: str
    event_hash: str
    candidate_hash: str
    reason_codes: tuple[str, ...] = ()
    risk_signals: tuple[str, ...] = ()
    review_reference: str | None = None
    verdict: TemplarVerdict | None = None
    authorization: PromotionAuthorization | None = None

    def __post_init__(self) -> None:
        if self.status not in {READY, DENY, REVIEW}:
            raise LearningPromotionGateError(
                "learning promotion outcome status is invalid"
            )
        _hash(self.request_hash, "learning outcome request hash")
        _hash(self.event_hash, "learning outcome event hash")
        _hash(self.candidate_hash, "learning outcome candidate hash")
        object.__setattr__(self, "reason_codes", _reason_codes(self.reason_codes))
        object.__setattr__(self, "risk_signals", _reason_codes(self.risk_signals))
        if self.status == READY:
            if (
                self.authorization is None
                or self.verdict is None
                or self.verdict.decision != ALLOW
            ):
                raise LearningPromotionGateError(
                    "ready learning outcome lacks ALLOW authorization evidence"
                )
            if self.review_reference is not None:
                raise LearningPromotionGateError(
                    "ready learning outcome cannot carry review state"
                )
        elif self.authorization is not None:
            raise LearningPromotionGateError(
                "non-ready learning outcome cannot carry promotion authorization"
            )
        if self.status == REVIEW:
            _identifier(
                self.review_reference,
                "learning promotion review reference",
            )


class LearningPromotionGate:
    """Mandatory Phase 23 advisory gate before Fleet emits promotion authorization."""

    def __init__(
        self,
        *,
        policy_digest: str,
        templar: TemplarCore | None,
        review_router=None,
        now_ms=None,
        authorization_ttl_ms: int = 5 * 60 * 1000,
    ) -> None:
        _hash(policy_digest, "learning gate policy digest")
        if templar is not None and type(templar) is not TemplarCore:
            raise LearningPromotionGateError("learning gate Templar core is invalid")
        if review_router is not None and not callable(review_router):
            raise LearningPromotionGateError("learning gate review router is invalid")
        if (
            isinstance(authorization_ttl_ms, bool)
            or type(authorization_ttl_ms) is not int
            or not 0 < authorization_ttl_ms <= 15 * 60 * 1000
        ):
            raise LearningPromotionGateError(
                "learning gate authorization TTL is invalid"
            )
        self.policy_digest = policy_digest
        self.templar = templar
        self.review_router = review_router
        self.now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self.authorization_ttl_ms = authorization_ttl_ms

    def _outcome(
        self,
        request: LearningPromotionRequest,
        event: LearningPromotionEvent,
        *,
        status: str,
        reason_codes: tuple[str, ...] = (),
        review_reference: str | None = None,
        verdict: TemplarVerdict | None = None,
        authorization: PromotionAuthorization | None = None,
    ) -> LearningPromotionOutcome:
        return LearningPromotionOutcome(
            status=status,
            request_hash=request.content_hash,
            event_hash=event.content_hash,
            candidate_hash=request.approved_content_hash,
            reason_codes=reason_codes,
            risk_signals=event.risk_signals,
            review_reference=review_reference,
            verdict=verdict,
            authorization=authorization,
        )

    def authorize(
        self,
        request: LearningPromotionRequest,
        *,
        administrator: PrincipalRecord,
    ) -> LearningPromotionOutcome:
        if type(request) is not LearningPromotionRequest:
            raise LearningPromotionGateError("learning promotion request is invalid")
        if type(administrator) is not PrincipalRecord:
            raise LearningPromotionGateError(
                "learning promotion administrator is invalid"
            )
        event = request.event()
        if request.administrator != administrator.reference:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("authenticated-administrator-mismatch",),
            )
        if request.policy_digest != self.policy_digest:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("stale-learning-policy",),
            )
        if request.evaluation_material.has_unredacted_credential():
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("unredacted-secret-material",),
            )
        try:
            validate_promotion_policy(
                subject_kind=request.subject_kind,
                subject_key=request.subject_key,
                source_owner_principal_id=request.source_owner_principal_id,
                agent_instance_id=request.agent_instance_id,
                source_scope=request.source_scope,
                target_scope=request.target_scope,
                source_content_hash=request.source_content_hash,
                approved_content_hash=request.approved_content_hash,
                administrator=administrator,
                verification_digest=request.verification_digest,
                expected_current_promotion_id=request.expected_current_promotion_id,
                ttl_ms=self.authorization_ttl_ms,
            )
        except PromotionError:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("fleet-promotion-policy-deny",),
            )
        if self.templar is None:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("templar-unavailable",),
            )

        verdict = self.templar.evaluate(event)
        try:
            verdict.validate_for(
                event,
                templar_policy=self.templar.policy,
                evaluator=self.templar.evaluator,
                now_ms=self.now_ms(),
            )
        except Exception:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("templar-verdict-stale",),
                verdict=verdict,
            )
        if verdict.decision == DENY:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=verdict.reason_codes or ("templar-deny",),
                verdict=verdict,
            )
        if verdict.decision == REVIEW:
            if self.review_router is None:
                return self._outcome(
                    request,
                    event,
                    status=DENY,
                    reason_codes=("review-routing-unavailable",),
                    verdict=verdict,
                )
            try:
                review_reference = self.review_router(request, verdict)
                _identifier(
                    review_reference,
                    "learning promotion review reference",
                )
            except Exception:
                return self._outcome(
                    request,
                    event,
                    status=DENY,
                    reason_codes=("review-routing-failure",),
                    verdict=verdict,
                )
            return self._outcome(
                request,
                event,
                status=REVIEW,
                reason_codes=verdict.reason_codes,
                review_reference=review_reference,
                verdict=verdict,
            )
        if verdict.decision != ALLOW:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("templar-verdict-invalid",),
                verdict=verdict,
            )

        try:
            authorization = authorize_promotion(
                subject_kind=request.subject_kind,
                subject_key=request.subject_key,
                source_owner_principal_id=request.source_owner_principal_id,
                agent_instance_id=request.agent_instance_id,
                source_scope=request.source_scope,
                target_scope=request.target_scope,
                source_content_hash=request.source_content_hash,
                approved_content_hash=request.approved_content_hash,
                administrator=administrator,
                verification_digest=request.verification_digest,
                expected_current_promotion_id=request.expected_current_promotion_id,
                now_ms=self.now_ms(),
                ttl_ms=self.authorization_ttl_ms,
            )
        except PromotionError:
            return self._outcome(
                request,
                event,
                status=DENY,
                reason_codes=("fleet-promotion-policy-deny",),
                verdict=verdict,
            )
        return self._outcome(
            request,
            event,
            status=READY,
            verdict=verdict,
            authorization=authorization,
        )


__all__ = [
    "LEARNING_EVALUATION_CATEGORIES",
    "LEARNING_PROMOTION_EVENT_SCHEMA",
    "LEARNING_PROMOTION_REQUEST_SCHEMA",
    "PROMOTION_EVALUATION_MATERIAL_SCHEMA",
    "READY",
    "LearningPromotionEvent",
    "LearningPromotionGate",
    "LearningPromotionGateError",
    "LearningPromotionOutcome",
    "LearningPromotionRequest",
    "PromotionEvaluationFile",
    "PromotionEvaluationMaterial",
]
