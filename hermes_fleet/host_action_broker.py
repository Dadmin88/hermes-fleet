"""Structured, authority-bound host effects for disposable Fleet runs.

Phase 5 deliberately exposes no generic host shell, Docker, SSH, systemd, or
filesystem API.  Operator code registers fixed adapters behind logical
``(verb, target)`` identities.  A verified authority slice may only select an
exact adapter with exact, schema-bounded parameters and budgets.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PARAMETER_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

DEPLOY_APPROVED_ARTIFACT = "deploy-approved-artifact"
RESTART_APPROVED_SERVICE = "restart-approved-service"
PUBLISH_APPROVED_BUILD = "publish-approved-build"
REPLACE_APPROVED_TREE = "replace-approved-tree"
QUERY_APPROVED_HEALTH = "query-approved-health"

HOST_ACTION_VERBS = frozenset(
    {
        DEPLOY_APPROVED_ARTIFACT,
        RESTART_APPROVED_SERVICE,
        PUBLISH_APPROVED_BUILD,
        REPLACE_APPROVED_TREE,
        QUERY_APPROVED_HEALTH,
    }
)

_MAX_PARAMETERS_BYTES = 32 * 1024
_MAX_RESULT_BYTES = 64 * 1024
_MAX_IDEMPOTENCY_ENTRIES = 4096
_MAX_ADAPTERS = 512
_MAX_SCHEMA_KEYS = 32
_MAX_COLLECTION_ITEMS = 128
_MAX_VALUE_DEPTH = 8

_FORBIDDEN_PARAMETER_KEY_PARTS = frozenset(
    {
        "argv",
        "cmd",
        "command",
        "cwd",
        "docker",
        "env",
        "environment",
        "hostpath",
        "host_path",
        "path",
        "shell",
        "socket",
        "ssh",
        "systemd",
        "unit",
    }
)
_SECRET_KEY_PARTS = frozenset(
    {"credential", "password", "secret", "token", "private_key", "api_key"}
)
_FORBIDDEN_STRING_PREFIXES = (
    "/",
    "~/",
    "file:",
    "ssh:",
    "unix:",
    "docker:",
)
_ADVISORIES = frozenset({"allow", "deny", "review"})
_EVIDENCE_STATUSES = frozenset({"succeeded", "indeterminate"})


class HostActionBrokerError(RuntimeError):
    """A host effect is unauthorized, malformed, stale, or cannot be proven."""


class HostActionIndeterminateError(HostActionBrokerError):
    """A host effect may have happened but safe success cannot be proven."""

    def __init__(self, message: str, evidence: HostActionEvidence) -> None:
        self.evidence = evidence
        super().__init__(message)


def canonical_digest(value: object) -> str:
    payload = _canonical_json(value, "host-action value", 1 << 20)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise HostActionBrokerError(f"{label} is invalid")
    return value


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise HostActionBrokerError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 < value <= maximum:
        raise HostActionBrokerError(f"{label} is invalid")
    return value


def _canonical_json(value: object, label: str, maximum: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise HostActionBrokerError(f"{label} is not canonical JSON") from error
    if len(encoded) > maximum:
        raise HostActionBrokerError(f"{label} exceeds its byte bound")
    return encoded


def _canonical_object(value: object, label: str, maximum: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HostActionBrokerError(f"{label} must be an object")
    materialized = dict(value)
    _canonical_json(materialized, label, maximum)
    return materialized


def _parameter_key(value: object) -> str:
    if type(value) is not str or _PARAMETER_KEY_RE.fullmatch(value) is None:
        raise HostActionBrokerError("host-action parameter key is invalid")
    if value in _FORBIDDEN_PARAMETER_KEY_PARTS or any(
        part in _FORBIDDEN_PARAMETER_KEY_PARTS for part in value.split("_")
    ):
        raise HostActionBrokerError("host-action parameter exposes generic host power")
    if value in _SECRET_KEY_PARTS or any(
        part in _SECRET_KEY_PARTS for part in value.split("_")
    ):
        raise HostActionBrokerError(
            "host-action parameter may not carry secret material"
        )
    return value


def _validate_logical_value(value: object, *, depth: int = 0) -> None:
    if depth > _MAX_VALUE_DEPTH:
        raise HostActionBrokerError("host-action parameter structure is too deep")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}:
            raise HostActionBrokerError("host-action parameter number is invalid")
        return
    if type(value) is str:
        if (
            not value
            or len(value.encode()) > 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise HostActionBrokerError("host-action parameter text is invalid")
        lowered = value.lower()
        if (
            lowered.startswith(_FORBIDDEN_STRING_PREFIXES)
            or _WINDOWS_PATH_RE.match(value) is not None
            or "://" in value
        ):
            raise HostActionBrokerError(
                "host-action parameter may not contain a host path or transport URI"
            )
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise HostActionBrokerError("host-action parameter object is too large")
        for key, item in value.items():
            _parameter_key(key)
            _validate_logical_value(item, depth=depth + 1)
        return
    if type(value) in {list, tuple}:
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise HostActionBrokerError("host-action parameter list is too large")
        for item in value:
            _validate_logical_value(item, depth=depth + 1)
        return
    raise HostActionBrokerError("host-action parameter contains an unsupported value")


def _validate_result(value: object) -> dict[str, Any]:
    result = _canonical_object(value, "host-action result", _MAX_RESULT_BYTES)
    for key, item in result.items():
        if type(key) is not str or not key:
            raise HostActionBrokerError("host-action result key is invalid")
        lowered = key.lower()
        if any(part in lowered for part in _SECRET_KEY_PARTS):
            raise HostActionBrokerError(
                "host-action result may not contain secret material"
            )
        _validate_result_value(item, depth=0)
    return result


def _validate_result_value(value: object, *, depth: int) -> None:
    if depth > _MAX_VALUE_DEPTH:
        raise HostActionBrokerError("host-action result structure is too deep")
    if value is None or type(value) in {bool, int, float}:
        return
    if type(value) is str:
        if len(value.encode()) > 8192 or any(
            ord(character) < 32 and character not in "\t\n\r" for character in value
        ):
            raise HostActionBrokerError("host-action result text is invalid")
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise HostActionBrokerError("host-action result object is too large")
        for key, item in value.items():
            if type(key) is not str or not key:
                raise HostActionBrokerError("host-action result key is invalid")
            if any(part in key.lower() for part in _SECRET_KEY_PARTS):
                raise HostActionBrokerError(
                    "host-action result may not contain secret material"
                )
            _validate_result_value(item, depth=depth + 1)
        return
    if type(value) in {list, tuple}:
        if len(value) > _MAX_COLLECTION_ITEMS:
            raise HostActionBrokerError("host-action result list is too large")
        for item in value:
            _validate_result_value(item, depth=depth + 1)
        return
    raise HostActionBrokerError("host-action result contains an unsupported value")


@dataclass(frozen=True, slots=True)
class HostActionGrant:
    verb: str
    target: str
    parameters_digest: str
    max_calls: int
    rate_limit_per_minute: int = 60

    def __post_init__(self) -> None:
        if self.verb not in HOST_ACTION_VERBS:
            raise HostActionBrokerError("host-action verb is unsupported")
        _identifier(self.target, "host-action target")
        _hash(self.parameters_digest, "host-action parameter digest")
        _positive_int(self.max_calls, "host-action call limit", maximum=1000)
        _positive_int(
            self.rate_limit_per_minute,
            "host-action rate limit",
            maximum=1000,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "verb": self.verb,
            "target": self.target,
            "parameters_digest": self.parameters_digest,
            "max_calls": self.max_calls,
            "rate_limit_per_minute": self.rate_limit_per_minute,
        }


@dataclass(frozen=True, slots=True)
class HostActionAuthorityScope:
    """Verified Phase 5 slice projected from a future immutable RunAuthority."""

    principal_id: str
    execution_id: str
    run_authority_hash: str
    resolved_recipe_hash: str
    policy_digest: str
    target_digest: str
    deadline_ms: int
    grants: tuple[HostActionGrant, ...]

    def __post_init__(self) -> None:
        _identifier(self.principal_id, "host-action principal")
        _identifier(self.execution_id, "host-action execution ID")
        _hash(self.run_authority_hash, "host-action RunAuthority hash")
        _hash(self.resolved_recipe_hash, "host-action Recipe hash")
        _hash(self.policy_digest, "host-action policy digest")
        _hash(self.target_digest, "host-action destination digest")
        _positive_int(
            self.deadline_ms,
            "host-action authority deadline",
            maximum=1 << 63,
        )
        if type(self.grants) is not tuple or len(self.grants) > 128:
            raise HostActionBrokerError("host-action grant collection is invalid")
        if any(type(item) is not HostActionGrant for item in self.grants):
            raise HostActionBrokerError("host-action grant is invalid")
        identities = [
            (item.verb, item.target, item.parameters_digest) for item in self.grants
        ]
        if len(identities) != len(set(identities)):
            raise HostActionBrokerError("host-action grants contain duplicates")

    def match(self, request: HostActionRequest) -> HostActionGrant:
        candidates = [
            grant
            for grant in self.grants
            if grant.verb == request.verb
            and grant.target == request.target
            and grant.parameters_digest == request.parameters_digest
        ]
        if len(candidates) != 1:
            raise HostActionBrokerError("host action is not granted by RunAuthority")
        return candidates[0]


@dataclass(frozen=True, slots=True)
class HostActionRequest:
    principal_id: str
    execution_id: str
    run_authority_hash: str
    resolved_recipe_hash: str
    verb: str
    target: str
    parameters: Mapping[str, Any]
    idempotency_key: str
    deadline_ms: int

    def __post_init__(self) -> None:
        _identifier(self.principal_id, "host-action principal")
        _identifier(self.execution_id, "host-action execution ID")
        _hash(self.run_authority_hash, "host-action RunAuthority hash")
        _hash(self.resolved_recipe_hash, "host-action Recipe hash")
        if self.verb not in HOST_ACTION_VERBS:
            raise HostActionBrokerError("host-action verb is unsupported")
        _identifier(self.target, "host-action target")
        _identifier(self.idempotency_key, "host-action idempotency key")
        _positive_int(self.deadline_ms, "host-action deadline", maximum=1 << 63)
        parameters = _canonical_object(
            self.parameters,
            "host-action parameters",
            _MAX_PARAMETERS_BYTES,
        )
        for key, value in parameters.items():
            _parameter_key(key)
            _validate_logical_value(value)
        object.__setattr__(self, "parameters", parameters)

    @property
    def parameters_digest(self) -> str:
        return canonical_digest(dict(self.parameters))

    @property
    def request_hash(self) -> str:
        return canonical_digest(
            {
                "principal_id": self.principal_id,
                "execution_id": self.execution_id,
                "run_authority_hash": self.run_authority_hash,
                "resolved_recipe_hash": self.resolved_recipe_hash,
                "verb": self.verb,
                "target": self.target,
                "parameters": dict(self.parameters),
                "idempotency_key": self.idempotency_key,
                "deadline_ms": self.deadline_ms,
            }
        )


Adapter = Callable[[Mapping[str, Any]], Mapping[str, Any]]
NodePolicyCheck = Callable[[HostActionAuthorityScope, HostActionRequest], bool]
AuditSink = Callable[["HostActionEvidence"], None]


@dataclass(frozen=True, slots=True)
class HostActionAdapterSpec:
    verb: str
    target: str
    handler: Adapter
    required_parameters: tuple[str, ...] = ()
    optional_parameters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.verb not in HOST_ACTION_VERBS:
            raise HostActionBrokerError("host-action adapter verb is unsupported")
        _identifier(self.target, "host-action adapter target")
        if not callable(self.handler):
            raise HostActionBrokerError("host-action adapter handler is invalid")
        for collection, label in (
            (self.required_parameters, "required"),
            (self.optional_parameters, "optional"),
        ):
            if type(collection) is not tuple or len(collection) > _MAX_SCHEMA_KEYS:
                raise HostActionBrokerError(
                    f"host-action adapter {label} parameter schema is invalid"
                )
            for key in collection:
                _parameter_key(key)
        if len(self.required_parameters) != len(set(self.required_parameters)):
            raise HostActionBrokerError(
                "host-action adapter required parameters contain duplicates"
            )
        if len(self.optional_parameters) != len(set(self.optional_parameters)):
            raise HostActionBrokerError(
                "host-action adapter optional parameters contain duplicates"
            )
        if set(self.required_parameters) & set(self.optional_parameters):
            raise HostActionBrokerError("host-action adapter parameter schemas overlap")

    def validate_parameters(self, parameters: Mapping[str, Any]) -> None:
        keys = set(parameters)
        required = set(self.required_parameters)
        allowed = required | set(self.optional_parameters)
        if not required.issubset(keys) or not keys.issubset(allowed):
            raise HostActionBrokerError(
                "host-action parameters do not match the registered adapter schema"
            )


@dataclass(frozen=True, slots=True)
class HostActionEvidence:
    status: str
    request_hash: str
    run_authority_hash: str
    principal_id: str
    execution_id: str
    resolved_recipe_hash: str
    verb: str
    target: str
    idempotency_key: str
    started_at_ms: int
    completed_at_ms: int
    result_hash: str
    result: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.status not in _EVIDENCE_STATUSES:
            raise HostActionBrokerError("host-action evidence status is invalid")
        for value, label in (
            (self.request_hash, "request hash"),
            (self.run_authority_hash, "RunAuthority hash"),
            (self.resolved_recipe_hash, "Recipe hash"),
            (self.result_hash, "result hash"),
        ):
            _hash(value, f"host-action evidence {label}")
        _identifier(self.principal_id, "host-action evidence principal")
        _identifier(self.execution_id, "host-action evidence execution ID")
        _identifier(self.target, "host-action evidence target")
        _identifier(self.idempotency_key, "host-action evidence idempotency key")
        if self.verb not in HOST_ACTION_VERBS:
            raise HostActionBrokerError("host-action evidence verb is invalid")
        if (
            type(self.started_at_ms) is not int
            or type(self.completed_at_ms) is not int
            or self.started_at_ms <= 0
            or self.completed_at_ms < self.started_at_ms
        ):
            raise HostActionBrokerError("host-action evidence timestamps are invalid")
        result = _validate_result(self.result)
        if canonical_digest(result) != self.result_hash:
            raise HostActionBrokerError("host-action evidence result hash changed")
        object.__setattr__(self, "result", result)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "request_hash": self.request_hash,
            "run_authority_hash": self.run_authority_hash,
            "principal_id": self.principal_id,
            "execution_id": self.execution_id,
            "resolved_recipe_hash": self.resolved_recipe_hash,
            "verb": self.verb,
            "target": self.target,
            "idempotency_key": self.idempotency_key,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "result_hash": self.result_hash,
            "result": dict(self.result),
        }


class HostActionBroker:
    """Invoke fixed operator adapters under an exact verified authority slice."""

    def __init__(
        self,
        *,
        adapters: tuple[HostActionAdapterSpec, ...],
        node_policy: NodePolicyCheck,
        now_ms: Callable[[], int],
        audit_sink: AuditSink | None = None,
    ) -> None:
        if type(adapters) is not tuple or len(adapters) > _MAX_ADAPTERS:
            raise HostActionBrokerError("host-action adapter registry is invalid")
        registry: dict[tuple[str, str], HostActionAdapterSpec] = {}
        for adapter in adapters:
            if type(adapter) is not HostActionAdapterSpec:
                raise HostActionBrokerError("host-action adapter registry is invalid")
            key = (adapter.verb, adapter.target)
            if key in registry:
                raise HostActionBrokerError(
                    "host-action adapter registry has duplicates"
                )
            registry[key] = adapter
        if not callable(node_policy) or not callable(now_ms):
            raise HostActionBrokerError("host-action broker dependencies are invalid")
        if audit_sink is not None and not callable(audit_sink):
            raise HostActionBrokerError("host-action audit sink is invalid")
        self._adapters = registry
        self._node_policy = node_policy
        self._now_ms = now_ms
        self._audit_sink = audit_sink
        self._lock = threading.RLock()
        self._completed: dict[tuple[str, str], tuple[str, HostActionEvidence]] = {}
        self._in_flight: set[tuple[str, str]] = set()
        self._attempt_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        self._rate_windows: dict[tuple[str, str, str, str], deque[int]] = defaultdict(
            deque
        )

    def invoke(
        self,
        *,
        authority: HostActionAuthorityScope,
        request: HostActionRequest,
        current_policy_digest: str,
        current_resolved_recipe_hash: str,
        current_target: Mapping[str, Any],
        advisory: str | None = None,
    ) -> HostActionEvidence:
        if type(authority) is not HostActionAuthorityScope:
            raise HostActionBrokerError("host action requires verified authority")
        if type(request) is not HostActionRequest:
            raise HostActionBrokerError("host-action request is invalid")
        now = self._validated_now()
        if request.principal_id != authority.principal_id:
            raise HostActionBrokerError("host-action principal changed")
        if request.execution_id != authority.execution_id:
            raise HostActionBrokerError("host-action execution identity changed")
        if request.run_authority_hash != authority.run_authority_hash:
            raise HostActionBrokerError("host-action authority binding changed")
        if request.resolved_recipe_hash != authority.resolved_recipe_hash:
            raise HostActionBrokerError("host-action Recipe binding changed")
        _hash(current_policy_digest, "current policy digest")
        _hash(current_resolved_recipe_hash, "current Recipe hash")
        if current_policy_digest != authority.policy_digest:
            raise HostActionBrokerError(
                "host-action node policy authorization is stale"
            )
        if current_resolved_recipe_hash != authority.resolved_recipe_hash:
            raise HostActionBrokerError("host-action Recipe authorization is stale")
        current_target_digest = canonical_digest(
            _canonical_object(current_target, "current host-action target", 32 * 1024)
        )
        if current_target_digest != authority.target_digest:
            raise HostActionBrokerError(
                "host-action destination authorization is stale"
            )

        idempotency = (authority.run_authority_hash, request.idempotency_key)
        with self._lock:
            completed = self._completed.get(idempotency)
            if completed is not None:
                existing_hash, evidence = completed
                if existing_hash != request.request_hash:
                    raise HostActionBrokerError(
                        "host-action idempotency key was reused for a changed request"
                    )
                if evidence.status == "indeterminate":
                    raise HostActionIndeterminateError(
                        "host-action outcome remains indeterminate",
                        evidence,
                    )
                return evidence
            if idempotency in self._in_flight:
                raise HostActionBrokerError(
                    "host-action idempotency key is already in flight"
                )

        if now > authority.deadline_ms:
            raise HostActionBrokerError("host-action authority has expired")
        if now > request.deadline_ms:
            raise HostActionBrokerError("host-action request has expired")
        if request.deadline_ms > authority.deadline_ms:
            raise HostActionBrokerError("host-action deadline widens RunAuthority")

        adapter = self._adapters.get((request.verb, request.target))
        if adapter is None:
            raise HostActionBrokerError("host-action target has no registered adapter")
        adapter.validate_parameters(request.parameters)
        grant = authority.match(request)

        try:
            permitted = self._node_policy(authority, request)
        except Exception as error:
            raise HostActionBrokerError(
                "host-action node policy is unavailable"
            ) from error
        if permitted is not True:
            raise HostActionBrokerError("host action is denied by node policy")

        if advisory is not None:
            if advisory not in _ADVISORIES:
                raise HostActionBrokerError("host-action security advisory is invalid")
            if advisory == "deny":
                raise HostActionBrokerError(
                    "host action is denied by security advisory"
                )
            if advisory == "review":
                raise HostActionBrokerError("host action requires operator review")
        # advisory == "allow" deliberately grants nothing. Every deterministic
        # authority/policy/adapter check above already had to pass.

        grant_key = (
            authority.run_authority_hash,
            grant.verb,
            grant.target,
            grant.parameters_digest,
        )
        with self._lock:
            if len(self._completed) >= _MAX_IDEMPOTENCY_ENTRIES:
                raise HostActionBrokerError("host-action idempotency cache is full")
            self._reserve_budget(grant_key, grant, now)
            self._in_flight.add(idempotency)

        started = now
        try:
            try:
                raw_result = adapter.handler(request.parameters)
            except Exception as error:
                evidence = self._indeterminate_evidence(
                    authority=authority,
                    request=request,
                    started=started,
                    reason="adapter_failed",
                )
                self._remember(idempotency, request.request_hash, evidence)
                raise HostActionIndeterminateError(
                    "host-action adapter outcome is indeterminate",
                    evidence,
                ) from error

            completed_at = self._validated_now()
            if (
                completed_at > authority.deadline_ms
                or completed_at > request.deadline_ms
            ):
                evidence = self._indeterminate_evidence(
                    authority=authority,
                    request=request,
                    started=started,
                    reason="completed_after_deadline",
                    completed_at=completed_at,
                )
                self._remember(idempotency, request.request_hash, evidence)
                raise HostActionIndeterminateError(
                    "host action completed outside its authorized deadline",
                    evidence,
                )
            try:
                result = _validate_result(raw_result)
            except HostActionBrokerError as error:
                evidence = self._indeterminate_evidence(
                    authority=authority,
                    request=request,
                    started=started,
                    reason="invalid_adapter_evidence",
                    completed_at=completed_at,
                )
                self._remember(idempotency, request.request_hash, evidence)
                raise HostActionIndeterminateError(
                    "host action returned unverifiable evidence",
                    evidence,
                ) from error

            evidence = HostActionEvidence(
                status="succeeded",
                request_hash=request.request_hash,
                run_authority_hash=authority.run_authority_hash,
                principal_id=authority.principal_id,
                execution_id=authority.execution_id,
                resolved_recipe_hash=authority.resolved_recipe_hash,
                verb=request.verb,
                target=request.target,
                idempotency_key=request.idempotency_key,
                started_at_ms=started,
                completed_at_ms=completed_at,
                result_hash=canonical_digest(result),
                result=result,
            )
            self._remember(idempotency, request.request_hash, evidence)
        finally:
            with self._lock:
                self._in_flight.discard(idempotency)

        if self._audit_sink is not None:
            try:
                self._audit_sink(evidence)
            except Exception as error:
                raise HostActionBrokerError(
                    "host action completed but audit evidence could not be persisted"
                ) from error
        return evidence

    def _reserve_budget(
        self,
        key: tuple[str, str, str, str],
        grant: HostActionGrant,
        now_ms: int,
    ) -> None:
        if self._attempt_counts[key] >= grant.max_calls:
            raise HostActionBrokerError("host-action call budget is exhausted")
        window = self._rate_windows[key]
        threshold = now_ms - 60_000
        while window and window[0] <= threshold:
            window.popleft()
        if len(window) >= grant.rate_limit_per_minute:
            raise HostActionBrokerError("host-action rate limit is exhausted")
        self._attempt_counts[key] += 1
        window.append(now_ms)

    def _remember(
        self,
        idempotency: tuple[str, str],
        request_hash: str,
        evidence: HostActionEvidence,
    ) -> None:
        with self._lock:
            self._completed[idempotency] = (request_hash, evidence)

    def _indeterminate_evidence(
        self,
        *,
        authority: HostActionAuthorityScope,
        request: HostActionRequest,
        started: int,
        reason: str,
        completed_at: int | None = None,
    ) -> HostActionEvidence:
        completed = completed_at if completed_at is not None else self._validated_now()
        result = {"reason": reason}
        return HostActionEvidence(
            status="indeterminate",
            request_hash=request.request_hash,
            run_authority_hash=authority.run_authority_hash,
            principal_id=authority.principal_id,
            execution_id=authority.execution_id,
            resolved_recipe_hash=authority.resolved_recipe_hash,
            verb=request.verb,
            target=request.target,
            idempotency_key=request.idempotency_key,
            started_at_ms=started,
            completed_at_ms=max(started, completed),
            result_hash=canonical_digest(result),
            result=result,
        )

    def _validated_now(self) -> int:
        value = self._now_ms()
        if isinstance(value, bool) or type(value) is not int or value <= 0:
            raise HostActionBrokerError("host-action broker clock is invalid")
        return value
