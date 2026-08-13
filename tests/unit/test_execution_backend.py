from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import (
    BackendExecutionHandle,
    BackendExecutionState,
    ExecutionBackend,
    ExecutionBackendError,
    ExecutionBackendErrorCode,
    ExecutionPlan,
)
from hermes_fleet.recipes import ResolvedRecipe


def resolved_recipe() -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": "sha256:" + "1" * 64,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "researcher",
                "version": "1.4.2",
                "content_digest": "sha256:" + "2" * 64,
            },
            "extensions": {},
        }
    )


def capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "example.org/native",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["process"],
            "network": ["restricted"],
            "resources": {"cpu_millis": 1000, "memory_bytes": 1_073_741_824},
            "filesystem": {"ephemeral_root": True, "read_only_inputs": True},
            "materialization": {"agency_profile": True, "artifacts": True},
            "extensions": {},
        }
    )


class RecordingBackend(ExecutionBackend):
    def __init__(self) -> None:
        self.prepares = 0
        self.starts = 0
        self.cleans = 0
        self.handles: dict[str, BackendExecutionHandle] = {}
        self.plans: dict[str, str] = {}

    @property
    def capabilities(self) -> BackendCapabilities:
        return capabilities()

    def _prepare(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        existing = self.handles.get(plan.execution_id)
        if existing is not None:
            return existing
        self.prepares += 1
        handle = BackendExecutionHandle(
            execution_id=plan.execution_id,
            backend_kind=self.capabilities.backend_kind,
            realization_id=f"realization:{plan.execution_id}",
            plan_fingerprint=plan.fingerprint,
            state=BackendExecutionState.PREPARED,
        )
        self.handles[plan.execution_id] = handle
        self.plans[plan.execution_id] = plan.fingerprint
        return handle

    def start(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        current = self.handles[handle.execution_id]
        if current.state in {
            BackendExecutionState.RUNNING,
            BackendExecutionState.COMPLETED,
        }:
            return current
        self.starts += 1
        running = current.with_state(BackendExecutionState.RUNNING)
        self.handles[handle.execution_id] = running
        return running

    def inspect(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        return self.handles[handle.execution_id]

    def stop(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        stopped = self.handles[handle.execution_id].with_state(
            BackendExecutionState.STOPPED
        )
        self.handles[handle.execution_id] = stopped
        return stopped

    def cleanup(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        current = self.handles[handle.execution_id]
        if current.state == BackendExecutionState.CLEANED:
            return current
        self.cleans += 1
        self.handles[handle.execution_id] = current.with_state(
            BackendExecutionState.CLEANED
        )
        return self.handles[handle.execution_id]


def test_execution_plan_binds_recipe_capabilities_and_idempotency() -> None:
    plan = ExecutionPlan(
        execution_id="exec-1",
        idempotency_key="request-1",
        resolved_recipe=resolved_recipe(),
        required_capabilities_hash=capabilities().content_hash,
    )

    assert plan.resolved_recipe_hash == resolved_recipe().content_hash
    with pytest.raises(FrozenInstanceError):
        plan.execution_id = "changed"  # type: ignore[misc]


def test_prepare_start_and_cleanup_are_idempotent_by_contract() -> None:
    backend = RecordingBackend()
    plan = ExecutionPlan(
        execution_id="exec-1",
        idempotency_key="request-1",
        resolved_recipe=resolved_recipe(),
        required_capabilities_hash=capabilities().content_hash,
    )

    first = backend.prepare(plan)
    assert backend.prepare(plan) == first
    running = backend.start(first)
    assert backend.start(running) == running
    backend.cleanup(backend.stop(running))
    backend.cleanup(backend.inspect(running))

    assert (backend.prepares, backend.starts, backend.cleans) == (1, 1, 1)


def test_handle_rejects_illegal_state_regression() -> None:
    handle = BackendExecutionHandle(
        execution_id="exec-1",
        backend_kind="example.org/native",
        realization_id="realization-1",
        plan_fingerprint="sha256:" + "3" * 64,
        state=BackendExecutionState.RUNNING,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        handle.with_state(BackendExecutionState.PREPARED)

    assert raised.value.code == ExecutionBackendErrorCode.INVALID_TRANSITION


def test_contract_has_no_docker_or_scheduler_operations() -> None:
    public = set(ExecutionBackend.__abstractmethods__)

    assert public == {"capabilities", "_prepare", "start", "inspect", "stop", "cleanup"}
    assert not {"schedule", "place", "docker_run", "pull_image"} & public


def test_prepare_rejects_capability_drift_before_provider_side_effect() -> None:
    backend = RecordingBackend()
    plan = ExecutionPlan(
        execution_id="exec-1",
        idempotency_key="request-1",
        resolved_recipe=resolved_recipe(),
        required_capabilities_hash="sha256:" + "9" * 64,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        backend.prepare(plan)

    assert raised.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH
    assert backend.prepares == 0


def test_prepare_rejects_execution_identity_rebound_to_different_plan() -> None:
    backend = RecordingBackend()
    first = ExecutionPlan(
        execution_id="exec-1",
        idempotency_key="request-1",
        resolved_recipe=resolved_recipe(),
        required_capabilities_hash=capabilities().content_hash,
    )
    backend.prepare(first)
    conflict = ExecutionPlan(
        execution_id="exec-1",
        idempotency_key="request-2",
        resolved_recipe=resolved_recipe(),
        required_capabilities_hash=capabilities().content_hash,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        backend.prepare(conflict)

    assert raised.value.code == ExecutionBackendErrorCode.PLAN_CONFLICT
    assert backend.prepares == 1


def test_indeterminate_handle_cannot_claim_cleanup_without_observed_resolution() -> (
    None
):
    handle = BackendExecutionHandle(
        execution_id="exec-1",
        backend_kind="example.org/native",
        realization_id="realization-1",
        plan_fingerprint="sha256:" + "3" * 64,
        state=BackendExecutionState.INDETERMINATE,
    )

    with pytest.raises(ExecutionBackendError) as raised:
        handle.with_state(BackendExecutionState.CLEANED)

    assert raised.value.code == ExecutionBackendErrorCode.INVALID_TRANSITION
