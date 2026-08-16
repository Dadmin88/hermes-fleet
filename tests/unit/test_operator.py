from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_fleet import operator as operator_mod
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
        self.peer_id = "peer-controller-1"

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
    recipe_submission: Any | None = None,
) -> OperatorService:
    return OperatorService(
        state=state,
        config=config or _config(),
        keryx=keryx or FakeKeryx(),
        recipe_submission=recipe_submission,
        execution_id_factory=lambda: "execution-1",
    )


def _exact_request(*, deadline_seconds: int = 30):
    from hermes_fleet.agency_snapshot import AgencySource
    from hermes_fleet.operator import ExactRecipeRequest
    from hermes_fleet.recipes import FleetRecipe

    recipe = FleetRecipe.from_dict(
        {
            "schema": "fleet.recipe.v1",
            "agent": {
                "kind": "agency_profile",
                "name": "acceptance",
                "version": "1.0.0",
            },
            "environment": {"os": ["linux"], "architecture": ["x86_64"]},
            "resources": {"cpu_millis": 1000, "memory_bytes": 1000},
            "security": {"isolation": "process", "network": "provider"},
            "extensions": {},
        }
    )
    return ExactRecipeRequest(
        target="worker-a",
        prompt="Return FX8_OK.",
        recipe=recipe,
        agency_source=AgencySource("https://example.invalid/agency.git", "a" * 40),
        deadline_seconds=deadline_seconds,
    )


def _run_exact_with_wait_error(
    error: Exception,
    *,
    status: str,
    deadline_ms: int,
):
    service = _service(FakeState([_managed_node()]))
    resolved = service.resolve_target("worker-a")

    class Status:
        value = status

    class FailingHandle:
        def __init__(self) -> None:
            self.status = Status()

        async def wait(self, _timeout: float):
            raise error

    submission = SimpleNamespace(
        task_id="task-deadline",
        routed_to="peer-current",
        delivery_route="relay",
        deadline_ms=deadline_ms,
        handle=FailingHandle(),
    )

    async def submit_exact(_request):
        return submission, resolved

    service._submit_exact = submit_exact  # type: ignore[method-assign]
    return service


def test_inventory_probe_retries_transient_failures() -> None:
    class Controller:
        def __init__(self) -> None:
            self.deadlines: list[int] = []

        async def get_inventory(self, target: str, *, deadline_seconds: int):
            assert target == "worker"
            self.deadlines.append(deadline_seconds)
            if len(self.deadlines) < 3:
                raise RuntimeError("transient inventory transport failure")
            return "inventory-ok"

    controller = Controller()
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = asyncio.run(
        operator_mod._inventory_probe_with_retry(
            controller,  # type: ignore[arg-type]
            "worker",
            deadline_seconds=30,
            sleep=sleep,
        )
    )

    assert result == "inventory-ok"
    assert controller.deadlines == [10, 10, 10]
    assert delays == [0.25, 0.75]


def test_inventory_probe_exhaustion_is_typed_transport_failure() -> None:
    class Controller:
        def __init__(self) -> None:
            self.calls = 0

        async def get_inventory(self, target: str, *, deadline_seconds: int):
            assert target == "worker"
            assert deadline_seconds == 10
            self.calls += 1
            raise RuntimeError(f"inventory transport failure {self.calls}")

    controller = Controller()

    async def sleep(_delay: float) -> None:
        return None

    with pytest.raises(OperatorError) as caught:
        asyncio.run(
            operator_mod._inventory_probe_with_retry(
                controller,  # type: ignore[arg-type]
                "worker",
                deadline_seconds=90,
                sleep=sleep,
            )
        )

    assert caught.value.code is OperatorErrorCode.TRANSPORT_UNAVAILABLE
    assert caught.value.public_message == "Destination inventory probe is unavailable."
    assert "inventory transport failure 3" in caught.value.debug_detail
    assert controller.calls == 3


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


def test_prompt_only_execution_is_unavailable_without_recipe_authority() -> None:
    service = _service(FakeState([_managed_node()]), keryx=FakeKeryx())
    with pytest.raises(OperatorError) as unavailable:
        asyncio.run(service.run_exact("worker-a"))  # type: ignore[arg-type]
    assert unavailable.value.code is OperatorErrorCode.OPERATION_UNAVAILABLE


def test_run_exact_timeout_returns_typed_deadline_result() -> None:
    service = _run_exact_with_wait_error(
        TimeoutError("wait expired"),
        status="working",
        deadline_ms=1_000,
    )

    result = asyncio.run(service.run_exact(_exact_request(deadline_seconds=1)))

    assert result.task_id == "task-deadline"
    assert result.terminal_state == "timed_out"
    assert result.transport_status == "indeterminate"
    assert result.execution_status == "timed_out"
    assert result.error_category is OperatorErrorCode.DEADLINE_EXCEEDED
    assert result.result == "Fleet execution deadline exceeded."


def test_terminal_result_unavailable_after_deadline_is_typed_timeout(
    monkeypatch,
) -> None:
    from keryx.task import TaskResultUnavailableError

    monkeypatch.setattr(operator_mod.time, "time", lambda: 2.0)
    service = _run_exact_with_wait_error(
        TaskResultUnavailableError("terminal_result_unavailable"),
        status="failed",
        deadline_ms=1_000,
    )

    result = asyncio.run(service.run_exact(_exact_request(deadline_seconds=1)))

    assert result.terminal_state == "timed_out"
    assert result.transport_status == "indeterminate"
    assert result.execution_status == "timed_out"
    assert result.error_category is OperatorErrorCode.DEADLINE_EXCEEDED


def test_terminal_result_unavailable_before_deadline_stays_transport_failure(
    monkeypatch,
) -> None:
    from keryx.task import TaskResultUnavailableError

    monkeypatch.setattr(operator_mod.time, "time", lambda: 1.0)
    service = _run_exact_with_wait_error(
        TaskResultUnavailableError("terminal_result_unavailable"),
        status="failed",
        deadline_ms=2_000,
    )

    with pytest.raises(OperatorError) as caught:
        asyncio.run(service.run_exact(_exact_request(deadline_seconds=1)))

    assert caught.value.code is OperatorErrorCode.TRANSPORT_UNAVAILABLE


def test_exact_recipe_request_uses_destination_capabilities_and_shared_submission() -> (
    None
):
    from types import SimpleNamespace

    from hermes_fleet.agency_snapshot import AgencySource
    from hermes_fleet.host_profile_capabilities import host_profile_capabilities
    from hermes_fleet.operator import ExactRecipeRequest
    from hermes_fleet.recipes import FleetRecipe

    capabilities = host_profile_capabilities(
        logical_cpus=2,
        memory_bytes=1_000_000,
        operating_system="linux",
        architecture="x86_64",
    )
    inventory_text = json.dumps(
        {
            "operation": "fleet.inventory",
            "status": "ok",
            "node": {"name": "worker", "peer_id": "peer-current", "version": "0.1.0"},
            "capabilities": ["fleet.hermes.run"],
            "execution_backend": {
                "content_hash": capabilities.content_hash,
                "document": capabilities.to_dict(),
            },
        }
    )
    keryx = FakeKeryx()
    keryx.task_handle = lambda task_id: Handle()
    original_send = keryx.send_task

    async def send_task(message, **kwargs):
        if kwargs.get("metadata", {}).get("fleet.operation") == "fleet.inventory":
            handle = Handle()
            handle._inventory_text = inventory_text  # type: ignore[attr-defined]

            async def wait(timeout=None):
                return SimpleNamespace(
                    status=SimpleNamespace(value="completed"),
                    artifacts=[
                        SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    text=inventory_text, media_type="text/plain"
                                )
                            ]
                        )
                    ],
                )

            handle.wait = wait  # type: ignore[method-assign]
            return handle
        return await original_send(message, **kwargs)

    keryx.send_task = send_task  # type: ignore[method-assign]
    submitted: list[dict[str, Any]] = []

    class Submission:
        async def submit(self, **kwargs):
            submitted.append(kwargs)
            return SimpleNamespace(
                task_id="task-test",
                routed_to="peer-current",
                delivery_route="relay",
                deadline_ms=40_000,
                handle=Handle(),
            )

    recipe = FleetRecipe.from_dict(
        {
            "schema": "fleet.recipe.v1",
            "agent": {
                "kind": "agency_profile",
                "name": "acceptance",
                "version": "1.0.0",
            },
            "environment": {"os": ["linux"], "architecture": ["x86_64"]},
            "resources": {"cpu_millis": 1000, "memory_bytes": 1000},
            "security": {"isolation": "process", "network": "provider"},
            "extensions": {},
        }
    )
    request = ExactRecipeRequest(
        target="worker-a",
        prompt="Return FX8_OK.",
        recipe=recipe,
        agency_source=AgencySource("https://example.invalid/agency.git", "a" * 40),
        deadline_seconds=30,
    )

    result = asyncio.run(
        _service(
            FakeState([_managed_node()]),
            keryx=keryx,
            recipe_submission=Submission(),
        ).submit_exact(request)
    )

    assert result.task_id == "task-test"
    assert len(submitted) == 1
    assert submitted[0]["requester"] == "peer-controller-1"
    assert submitted[0]["peer_id"] == "peer-current"
    assert submitted[0]["execution_id"] == "execution-1"
    assert submitted[0]["capabilities"] == capabilities
    assert submitted[0]["target"] == {
        "source": "nodescale",
        "network_id": "network-test",
        "device_id": "device-a",
        "binding_generation": 3,
        "admission_generation": 5,
    }
    assert (
        submitted[0]["policy_digest"]
        == _config().managed_targets[0].policy.content_hash
    )


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
    assert result.transport_status == status
    assert result.execution_status is None
    assert result.error_category is None


def test_typed_outcome_separates_transport_and_execution_status() -> None:
    artifact = SimpleNamespace(
        name="fleet-execution-outcome.v1.json",
        parts=[
            SimpleNamespace(
                text=json.dumps(
                    {
                        "schema": "fleet.execution-outcome.v1",
                        "execution_id": "execution-1",
                        "status": "failed",
                        "reason": "Hermes command outcome verification failed",
                    }
                )
            )
        ],
    )

    task = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        artifacts=[artifact],
        metadata={},
    )
    result = OperatorService._completion(
        task,
        task_id="execution-1",
        operation="fleet.hermes.run",
    )

    assert result.terminal_state == "failed"
    assert result.transport_status == "completed"
    assert result.execution_status == "failed"
    assert result.error_category is OperatorErrorCode.TASK_FAILED
    assert result.result == "Hermes command outcome verification failed"


def test_typed_execution_outcome_is_bound_to_exact_execution_identity() -> None:
    artifact = SimpleNamespace(
        name="fleet-execution-outcome.v1.json",
        parts=[
            SimpleNamespace(
                text=json.dumps(
                    {
                        "schema": "fleet.execution-outcome.v1",
                        "execution_id": "other-execution",
                        "status": "failed",
                        "reason": "wrong execution",
                    }
                )
            )
        ],
    )
    task = SimpleNamespace(
        status=SimpleNamespace(value="completed"),
        artifacts=[artifact],
        metadata={},
    )

    with pytest.raises(OperatorError) as caught:
        OperatorService._completion(
            task,
            task_id="execution-1",
            operation="fleet.hermes.run",
        )

    assert caught.value.code is OperatorErrorCode.TASK_INDETERMINATE


def test_hermes_execution_status_is_separate_from_transport_status() -> None:
    class Task:
        class Status:
            value = "completed"

        status = Status()
        metadata = {"result_text": "done", "run_id": "run-test"}
        artifacts: list[object] = []

    result = OperatorService._completion(
        Task(),
        task_id="task-test",
        operation="fleet.hermes.run",
    )

    assert result.terminal_state == "completed"
    assert result.transport_status == "completed"
    assert result.execution_status == "succeeded"
    assert result.result == "done"


def test_completed_hermes_transport_without_result_is_execution_indeterminate() -> None:
    class Task:
        class Status:
            value = "completed"

        status = Status()
        metadata: dict[str, str] = {}
        artifacts: list[object] = []

    result = OperatorService._completion(
        Task(),
        task_id="task-test",
        operation="fleet.hermes.run",
    )

    assert result.transport_status == "completed"
    assert result.execution_status == "indeterminate"


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
