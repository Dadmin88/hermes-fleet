"""Provider-neutral lifecycle contract for realized exact Recipe execution."""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from .backend_capabilities import BackendCapabilities
from .recipes import ResolvedRecipe

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ExecutionBackendErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_TRANSITION = "invalid_transition"
    CAPABILITY_MISMATCH = "capability_mismatch"
    PREPARE_FAILED = "prepare_failed"
    START_FAILED = "start_failed"
    INSPECTION_UNAVAILABLE = "inspection_unavailable"
    STOP_INDETERMINATE = "stop_indeterminate"
    CLEANUP_FAILED = "cleanup_failed"
    PLAN_CONFLICT = "plan_conflict"


class ExecutionBackendError(RuntimeError):
    def __init__(self, code: ExecutionBackendErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class BackendExecutionState(StrEnum):
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    INDETERMINATE = "indeterminate"
    CLEANED = "cleaned"


_ALLOWED_TRANSITIONS = {
    BackendExecutionState.PREPARED: frozenset(
        {
            BackendExecutionState.RUNNING,
            BackendExecutionState.FAILED,
            BackendExecutionState.STOPPED,
            BackendExecutionState.CLEANED,
        }
    ),
    BackendExecutionState.RUNNING: frozenset(
        {
            BackendExecutionState.COMPLETED,
            BackendExecutionState.FAILED,
            BackendExecutionState.STOPPED,
            BackendExecutionState.INDETERMINATE,
        }
    ),
    BackendExecutionState.INDETERMINATE: frozenset(
        {
            BackendExecutionState.RUNNING,
            BackendExecutionState.COMPLETED,
            BackendExecutionState.FAILED,
            BackendExecutionState.STOPPED,
        }
    ),
    BackendExecutionState.COMPLETED: frozenset({BackendExecutionState.CLEANED}),
    BackendExecutionState.FAILED: frozenset({BackendExecutionState.CLEANED}),
    BackendExecutionState.STOPPED: frozenset({BackendExecutionState.CLEANED}),
    BackendExecutionState.CLEANED: frozenset(),
}


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ExecutionBackendError(
            ExecutionBackendErrorCode.INVALID_INPUT, f"{label} is invalid"
        )
    return value


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Exact logical ingredients plus an idempotent execution identity."""

    execution_id: str
    idempotency_key: str
    resolved_recipe: ResolvedRecipe
    required_capabilities_hash: str

    def __post_init__(self) -> None:
        _identifier(self.execution_id, "execution ID")
        _identifier(self.idempotency_key, "idempotency key")
        if type(self.resolved_recipe) is not ResolvedRecipe:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_INPUT,
                "resolved Recipe is invalid",
            )
        if _HASH_RE.fullmatch(self.required_capabilities_hash) is None:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_INPUT,
                "required capability hash is invalid",
            )

    @property
    def resolved_recipe_hash(self) -> str:
        return self.resolved_recipe.content_hash

    @property
    def fingerprint(self) -> str:
        """Return the exact semantic identity for idempotent realization."""
        document = json.dumps(
            {
                "capabilities": self.required_capabilities_hash,
                "execution": self.execution_id,
                "idempotency": hashlib.sha256(
                    self.idempotency_key.encode("utf-8")
                ).hexdigest(),
                "recipe": self.resolved_recipe_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(document).hexdigest()


@dataclass(frozen=True, slots=True)
class BackendExecutionHandle:
    """Opaque durable backend realization identity and observed lifecycle state."""

    execution_id: str
    backend_kind: str
    realization_id: str
    plan_fingerprint: str
    state: BackendExecutionState

    def __post_init__(self) -> None:
        _identifier(self.execution_id, "execution ID")
        _identifier(self.realization_id, "realization ID")
        if _HASH_RE.fullmatch(self.plan_fingerprint) is None:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_INPUT,
                "plan fingerprint is invalid",
            )
        if type(self.backend_kind) is not str or "/" not in self.backend_kind:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_INPUT, "backend kind is invalid"
            )
        if type(self.state) is not BackendExecutionState:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_INPUT, "backend state is invalid"
            )

    def with_state(self, state: BackendExecutionState) -> BackendExecutionHandle:
        if type(state) is not BackendExecutionState:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_INPUT, "backend state is invalid"
            )
        if state == self.state:
            return self
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_TRANSITION,
                f"cannot transition backend execution from {self.state} to {state}",
            )
        return BackendExecutionHandle(
            execution_id=self.execution_id,
            backend_kind=self.backend_kind,
            realization_id=self.realization_id,
            plan_fingerprint=self.plan_fingerprint,
            state=state,
        )


class ExecutionBackend(ABC):
    """Lifecycle boundary implemented by mature execution runtimes."""

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Return current hard guarantees for this backend instance."""

    def prepare(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        """Validate and idempotently materialize one exact plan without starting it."""
        if type(plan) is not ExecutionPlan:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_INPUT, "execution plan is invalid"
            )
        if plan.required_capabilities_hash != self.capabilities.content_hash:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.CAPABILITY_MISMATCH,
                "execution plan capability hash does not match this backend",
            )
        handle = self._prepare(plan)
        if (
            type(handle) is not BackendExecutionHandle
            or handle.execution_id != plan.execution_id
            or handle.backend_kind != self.capabilities.backend_kind
        ):
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.PREPARE_FAILED,
                "backend returned an invalid execution handle",
            )
        if handle.plan_fingerprint != plan.fingerprint:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.PLAN_CONFLICT,
                "execution identity is already bound to a different plan",
            )
        return handle

    @abstractmethod
    def _prepare(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        """Provider implementation for a plan validated by :meth:`prepare`."""

    @abstractmethod
    def start(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        """Idempotently start a prepared realization without duplicate work."""

    @abstractmethod
    def inspect(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        """Observe authoritative lifecycle state without mutation."""

    @abstractmethod
    def stop(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        """Request stop and return observed state; uncertainty is indeterminate."""

    @abstractmethod
    def cleanup(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        """Remove owned resources and return CLEANED only after proving absence."""
