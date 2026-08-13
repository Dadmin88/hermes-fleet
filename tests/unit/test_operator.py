from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from hermes_fleet.config import FleetConfig, ManagedTargetPolicy
from hermes_fleet.models import FleetDefaults, NodePolicy
from hermes_fleet.operator import OperatorError, OperatorErrorCode, OperatorService

IDENTITY = {
    "source": "nodescale",
    "network_id": "network-test",
    "device_id": "device-a",
}


def _managed_node(
    *,
    device_id: str = "device-a",
    alias: str | None = "worker-a",
    peer_id: str | None = "peer-current",
    ready: bool = True,
    binding_generation: str = "3",
) -> dict[str, Any]:
    identity = {**IDENTITY, "device_id": device_id}
    return {
        "stable_id": f"fleet-node-{'a' * 64}",
        "identity": identity,
        "naming": {
            "display_name": alias or device_id,
            "provider_name": None,
            "alias": alias,
            "has_alias": alias is not None,
        },
        "managed": {
            "state": "active",
            "active": True,
            "projection_generation": "5",
            "membership_generation": "4",
            "binding_generation": binding_generation,
        },
        "readiness": {
            "managed_state": "active",
            "admission_generation": 5,
            "alive": ready,
            "fresh": ready,
            "scheduler_ready": ready,
            "observation_age_ms": 10 if ready else 10_000,
            "reasons": [] if ready else ["observation_stale"],
            "last_observation": None,
            "capacity": {
                "active_workers": 0,
                "max_workers": 1,
                "available_worker_slots": 1,
            },
            "profiles": [],
            "resources": {
                "cpu": None,
                "ram": None,
                "swap": None,
                "disk": None,
                "gpu": None,
            },
        },
        "operations": ["fleet.health", "fleet.inventory", "fleet.message"],
        "_peer_id": peer_id,
    }


class FakeState:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = nodes
        self.overview_calls = 0
        self.inspect_calls: list[tuple[str, str, str]] = []

    def overview(self) -> dict[str, Any]:
        self.overview_calls += 1
        public_nodes = [
            {key: value for key, value in node.items() if key != "_peer_id"}
            for node in self.nodes
        ]
        return {"schema": "fleet.desktop.v1", "summary": {}, "nodes": public_nodes}

    def inspect_projection(
        self, *, source: str, network_id: str, device_id: str
    ) -> dict[str, Any]:
        self.inspect_calls.append((source, network_id, device_id))
        node = next(
            item
            for item in self.nodes
            if item["identity"]
            == {"source": source, "network_id": network_id, "device_id": device_id}
        )
        peer_id = node["_peer_id"]
        provenance = {
            "source": source,
            "network_id": network_id,
            "device_id": device_id,
            "snapshot": "5",
        }
        if peer_id is not None:
            provenance.update(
                {"binding_id": "binding-test", "authenticated_peer_id": peer_id}
            )
        generated = {
            "state": node["managed"]["state"],
            "projection_generation": node["managed"]["projection_generation"],
            "membership_generation": node["managed"]["membership_generation"],
            "binding_generation": node["managed"]["binding_generation"],
            "content_hash": "b" * 64,
            "allowed_operations": ("fleet.health", "fleet.inventory", "fleet.message"),
            "provenance": provenance,
        }
        return {
            "generated": generated,
            "effective": {
                "state": "active",
                "allowed_operations": generated["allowed_operations"],
                "operator_denied_operations": (),
            },
        }


class Receipt:
    task_id = "task-test"
    routed_to = "peer-current"
    delivery_route = "relay"


class Task:
    class Status:
        value = "completed"

    status = Status()
    metadata = {"result_text": "done", "run_id": "run-test"}
    artifacts: list[Any] = []


class Handle:
    task_id = "task-test"
    receipt = Receipt()

    async def wait(self, timeout: float | None = None) -> Task:
        return Task()

    async def refresh(self) -> Task:
        return Task()


class FakeKeryx:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_task(self, message, *, peer_id, metadata, deadline_ms):
        self.sent.append(
            {
                "message": message,
                "peer_id": peer_id,
                "metadata": metadata,
                "deadline_ms": deadline_ms,
            }
        )
        return Handle()

    def task_handle(self, task_id: str) -> Handle:
        assert task_id == "task-test"
        return Handle()


def _config(*, operations: tuple[str, ...] = ("fleet.hermes.run",)) -> FleetConfig:
    return FleetConfig(
        schema_version=2,
        defaults=FleetDefaults(),
        nodes=(),
        managed_targets=(
            ManagedTargetPolicy(
                source="nodescale",
                network_id="network-test",
                device_id="device-a",
                target_name="worker",
                policy=NodePolicy(allowed_operations=operations),
            ),
        ),
    )


def _service(
    state: FakeState,
    *,
    config: FleetConfig | None = None,
    keryx: Any | None = None,
) -> OperatorService:
    return OperatorService(
        state=state, config=config or _config(), keryx=keryx or FakeKeryx()
    )


def test_list_and_inspect_use_authoritative_state_and_readiness() -> None:
    state = FakeState([_managed_node(ready=False)])
    service = _service(state)

    listed = service.list_nodes()
    inspected = service.inspect_node("worker-a")

    assert state.overview_calls == 2
    assert state.inspect_calls == [
        ("nodescale", "network-test", "device-a"),
        ("nodescale", "network-test", "device-a"),
    ]
    assert listed[0].readiness.reasons == ("observation_stale",)
    assert inspected.current_peer_id == "peer-current"
    assert inspected.readiness.reasons == ("observation_stale",)
    assert inspected.explicit_operations == ("fleet.hermes.run",)
    assert "fleet.hermes.run" not in inspected.managed_operations


@pytest.mark.parametrize("target", ["worker-a", "device-a", f"fleet-node-{'a' * 64}"])
def test_unique_alias_managed_identity_and_stable_id_resolve_to_current_binding(
    target: str,
) -> None:
    state = FakeState([_managed_node()])
    resolved = _service(state).resolve_target(target)
    assert resolved.requested_target == target
    assert resolved.current_peer_id == "peer-current"
    assert resolved.identity.device_id == "device-a"


def test_unknown_and_ambiguous_targets_fail_deterministically() -> None:
    state = FakeState(
        [_managed_node(), _managed_node(device_id="device-b", alias="worker-a")]
    )
    service = _service(state)

    with pytest.raises(OperatorError) as unknown:
        service.resolve_target("missing")
    assert unknown.value.code is OperatorErrorCode.UNKNOWN_TARGET

    with pytest.raises(OperatorError) as ambiguous:
        service.resolve_target("worker-a")
    assert ambiguous.value.code is OperatorErrorCode.AMBIGUOUS_TARGET


def test_missing_or_generation_mismatched_binding_fails_closed() -> None:
    missing = _service(FakeState([_managed_node(peer_id=None)]))
    with pytest.raises(OperatorError) as no_binding:
        missing.resolve_target("worker-a")
    assert no_binding.value.code is OperatorErrorCode.NO_BINDING

    state = FakeState([_managed_node(binding_generation="3")])
    original = state.inspect_projection

    def stale(**selector):
        value = original(**selector)
        value["generated"]["binding_generation"] = "2"
        return value

    state.inspect_projection = stale  # type: ignore[method-assign]
    with pytest.raises(OperatorError) as mismatch:
        _service(state).resolve_target("worker-a")
    assert mismatch.value.code is OperatorErrorCode.STALE_STATE


def test_managed_state_never_implies_execution_authority() -> None:
    state = FakeState([_managed_node()])
    service = _service(state, config=_config(operations=()))
    with pytest.raises(OperatorError) as denied:
        service.resolve_target("worker-a", operation="fleet.hermes.run")
    assert denied.value.code is OperatorErrorCode.POLICY_DENIED


def test_run_exact_preserves_exact_peer_and_returns_structured_completion() -> None:
    state = FakeState([_managed_node()])
    keryx = FakeKeryx()
    result = asyncio.run(
        _service(state, keryx=keryx).run_exact(
            "worker-a", "do bounded work", deadline_seconds=30
        )
    )

    assert keryx.sent[0]["peer_id"] == "peer-current"
    envelope = json.loads(keryx.sent[0]["message"]["parts"][0]["text"])
    assert envelope["target"]["name"] == "worker"
    assert result.task_id == "task-test"
    assert result.requested_target == "worker-a"
    assert result.resolved_target.identity.device_id == "device-a"
    assert result.routed_to == "peer-current"
    assert result.delivery_route == "relay"
    assert result.operation == "fleet.hermes.run"
    assert result.deadline_ms > 0
    assert result.terminal_state == "completed"
    assert result.run_id == "run-test"
    assert result.result == "done"


def test_submit_exact_preserves_exact_peer_without_waiting() -> None:
    state = FakeState([_managed_node()])
    keryx = FakeKeryx()

    result = asyncio.run(
        _service(state, keryx=keryx).submit_exact(
            "worker-a", "do bounded work", deadline_seconds=30
        )
    )

    assert keryx.sent[0]["peer_id"] == "peer-current"
    assert result.task_id == "task-test"
    assert result.terminal_state == "submitted"
    assert result.resolved_target is not None
    assert result.resolved_target.identity.device_id == "device-a"
    assert result.routed_to == "peer-current"


@pytest.mark.parametrize("status", ["submitted", "working"])
def test_task_inspection_preserves_nonterminal_state(status: str) -> None:
    class NonterminalTask:
        class Status:
            value = status

        status = Status()
        metadata: dict[str, str] = {}

    class NonterminalHandle:
        async def refresh(self) -> NonterminalTask:
            return NonterminalTask()

    class NonterminalKeryx:
        async def send_task(self, *_args, **_kwargs):
            raise AssertionError("task inspection must not submit work")

        def task_handle(self, task_id: str) -> NonterminalHandle:
            assert task_id == "task-test"
            return NonterminalHandle()

    result = asyncio.run(
        _service(FakeState([_managed_node()]), keryx=NonterminalKeryx()).inspect_task(
            "task-test"
        )
    )
    assert result.terminal_state == status
    assert result.error_category is None


def test_unknown_task_status_is_indeterminate() -> None:
    class UnknownTask:
        class Status:
            value = "future_status"

        status = Status()
        metadata: dict[str, str] = {}

    result = OperatorService._completion(UnknownTask(), task_id="task-test")
    assert result.error_category is OperatorErrorCode.TASK_INDETERMINATE


def test_error_details_are_debuggable_but_public_message_redacts_secrets() -> None:
    error = OperatorError(
        OperatorErrorCode.TRANSPORT_UNAVAILABLE,
        "Transport failed token=top-secret at /private/controller/state",
        detail="Bearer hidden-secret",
    )
    assert "top-secret" not in error.public_message
    assert "hidden-secret" not in error.public_message
    assert "hidden-secret" in error.debug_detail
