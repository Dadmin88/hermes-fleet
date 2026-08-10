from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _payload(operation: str, input_data: dict[str, Any] | None = None) -> str:
    if input_data is None:
        if operation == "fleet.hermes.run":
            input_data = {
                "prompt": "Return a short text result.",
                "export_paths": [],
            }
        elif operation == "fleet.message":
            input_data = {
                "text": "FLEET_MESSAGE_OK",
                "topic": "smoke-test",
                "correlation_id": "corr-1",
            }
        else:
            input_data = {}
    return json.dumps(
        {
            "version": 1,
            "operation": operation,
            "target": {"name": "vps", "peer_id": "peer-vps"},
            "input": input_data,
            "limits": {"deadline_seconds": 30},
        }
    )


def _metadata(operation: str, **overrides: str) -> dict[str, str]:
    return {
        "fleet.envelope_version": "1",
        "fleet.operation": operation,
        "fleet.target_peer_id": "peer-vps",
        "fleet_deadline_ms": "14000",
        **overrides,
    }


@dataclass
class _IncomingTask:
    payload: str
    operation: str
    task_id: str = "task-1"
    peer_id: str = "peer-katana"
    metadata: dict[str, str] = field(default_factory=dict)
    completed: list[Any] | None = None
    failed: str | None = None

    def __post_init__(self) -> None:
        if not self.metadata:
            self.metadata = _metadata(self.operation)

    @property
    def messages(self) -> list[Any]:
        return [
            SimpleNamespace(
                parts=[
                    SimpleNamespace(text=self.payload, raw=b"", media_type="text/plain")
                ]
            )
        ]

    async def complete(self, artifacts: list[Any]) -> None:
        self.completed = artifacts

    async def fail(self, error: str) -> None:
        self.failed = error


class _Hermes:
    def __init__(self) -> None:
        self.health_calls = 0
        self.health_timeouts: list[float | None] = []
        self.start_calls: list[tuple[str, str | None]] = []
        self.start_timeouts: list[float | None] = []
        self.wait_calls: list[tuple[str, float]] = []
        self.stop_calls: list[str] = []
        self.stop_timeouts: list[float | None] = []

    def health(self, *, timeout_seconds: float | None = None) -> dict[str, object]:
        self.health_calls += 1
        self.health_timeouts.append(timeout_seconds)
        return {
            "api": "healthy",
            "run_submission": True,
            "run_status": True,
            "run_stop": True,
        }

    def start(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        self.start_calls.append((prompt, session_id))
        self.start_timeouts.append(timeout_seconds)
        return "run-vps-1"

    def wait(self, *, run_id: str, timeout_seconds: float):
        from hermes_fleet.hermes_runs import HermesRunResult

        self.wait_calls.append((run_id, timeout_seconds))
        return HermesRunResult(run_id=run_id, text="terminal answer")

    def stop(self, run_id: str, *, timeout_seconds: float | None = None) -> None:
        self.stop_calls.append(run_id)
        self.stop_timeouts.append(timeout_seconds)


def _worker(
    hermes: Any,
    state_path: Path,
    *,
    now_ms=None,
    advertised_operations=None,
    readiness_inspector=None,
    capacity_observer=None,
):
    from hermes_fleet.fleet_node import FleetNodeWorker
    from hermes_fleet.models import FleetDefaults, NodeConfig, NodePolicy
    from hermes_fleet.run_binding import RunBindingStore

    target = NodeConfig(
        name="vps",
        peer_id="peer-vps",
        policy=NodePolicy(
            allowed_operations=(
                "fleet.health",
                "fleet.inventory",
                "fleet.message",
                "fleet.hermes.run",
            )
        ),
    )
    return FleetNodeWorker(
        target=target,
        defaults=FleetDefaults(),
        hermes=hermes,
        bindings=RunBindingStore(state_path),
        controller_peer_ids=("peer-katana",),
        advertised_operations=advertised_operations,
        now_ms=now_ms or (lambda: 10_000),
        readiness_inspector=readiness_inspector,
        capacity_observer=capacity_observer,
    )


def _completed_text(incoming: _IncomingTask) -> str:
    assert incoming.completed is not None
    return incoming.completed[0]["parts"][0]["text"]


def test_fleet_node_binds_one_keryx_task_to_one_hermes_run(tmp_path) -> None:
    operation = "fleet.hermes.run"
    incoming = _IncomingTask(_payload(operation), operation)
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == [("Return a short text result.", "fleet:vps:task-1")]
    assert hermes.start_timeouts == [4.0]
    assert hermes.wait_calls == [("run-vps-1", 4.0)]
    assert incoming.failed is None
    assert _completed_text(incoming) == "terminal answer"
    assert incoming.completed is not None
    assert incoming.completed[0]["name"] == "hermes-result.txt"


def test_fleet_node_rejects_policy_allowed_operation_when_not_advertised(
    tmp_path,
) -> None:
    operation = "fleet.hermes.run"
    incoming = _IncomingTask(_payload(operation), operation)
    hermes = _Hermes()
    worker = _worker(
        hermes,
        tmp_path / "bindings.db",
        advertised_operations=("fleet.health", "fleet.inventory", "fleet.message"),
    )

    asyncio.run(worker.handle_task(incoming))

    assert incoming.completed is None
    assert incoming.failed == "Fleet operation is not currently available"
    assert hermes.start_calls == []


def test_fleet_node_deadline_cancellation_is_terminal_for_exact_binding(
    tmp_path,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunDeadlineExceeded
    from hermes_fleet.run_binding import RunBindingStore

    class DeadlineHermes(_Hermes):
        def wait(self, *, run_id: str, timeout_seconds: float):
            self.wait_calls.append((run_id, timeout_seconds))
            raise HermesRunDeadlineExceeded("Hermes run exceeded Fleet deadline")

    operation = "fleet.hermes.run"
    state_path = tmp_path / "bindings.db"
    incoming = _IncomingTask(_payload(operation), operation)
    hermes = DeadlineHermes()

    asyncio.run(_worker(hermes, state_path).handle_task(incoming))

    binding = RunBindingStore(state_path).get("task-1")
    assert binding is not None
    assert binding.state == "cancelled"
    assert binding.run_id == "run-vps-1"
    assert incoming.completed is None
    assert incoming.failed == "Fleet Hermes execution exceeded deadline"

    duplicate = _IncomingTask(_payload(operation), operation)
    asyncio.run(_worker(hermes, state_path).handle_task(duplicate))
    assert len(hermes.start_calls) == 1
    assert duplicate.failed == "Fleet Hermes execution was cancelled"


def test_fleet_node_replays_completion_when_deadline_transition_loses(
    tmp_path,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunDeadlineExceeded
    from hermes_fleet.run_binding import RunBindingStore

    state_path = tmp_path / "bindings.db"

    class CompletingDeadlineHermes(_Hermes):
        def wait(self, *, run_id: str, timeout_seconds: float):
            self.wait_calls.append((run_id, timeout_seconds))
            RunBindingStore(state_path).complete("task-1", run_id, "winner")
            raise HermesRunDeadlineExceeded("Hermes run exceeded Fleet deadline")

    operation = "fleet.hermes.run"
    incoming = _IncomingTask(_payload(operation), operation)

    asyncio.run(_worker(CompletingDeadlineHermes(), state_path).handle_task(incoming))

    assert incoming.failed is None
    assert _completed_text(incoming) == "winner"
    binding = RunBindingStore(state_path).get("task-1")
    assert binding is not None
    assert binding.state == "completed"


def test_fleet_node_fails_cancelled_when_late_completion_transition_loses(
    tmp_path,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunResult
    from hermes_fleet.run_binding import RunBindingStore

    state_path = tmp_path / "bindings.db"

    class CancellingResultHermes(_Hermes):
        def wait(self, *, run_id: str, timeout_seconds: float):
            self.wait_calls.append((run_id, timeout_seconds))
            RunBindingStore(state_path).mark_cancelled("task-1", run_id)
            return HermesRunResult(run_id=run_id, text="late result")

    operation = "fleet.hermes.run"
    incoming = _IncomingTask(_payload(operation), operation)

    asyncio.run(_worker(CancellingResultHermes(), state_path).handle_task(incoming))

    assert incoming.completed is None
    assert incoming.failed == "Fleet Hermes execution was cancelled"
    binding = RunBindingStore(state_path).get("task-1")
    assert binding is not None
    assert binding.state == "cancelled"


def test_fleet_node_replays_completion_when_indeterminate_transition_loses(
    tmp_path,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunError
    from hermes_fleet.run_binding import RunBindingStore

    state_path = tmp_path / "bindings.db"

    class CompletingErrorHermes(_Hermes):
        def wait(self, *, run_id: str, timeout_seconds: float):
            self.wait_calls.append((run_id, timeout_seconds))
            RunBindingStore(state_path).complete("task-1", run_id, "winner")
            raise HermesRunError("observation failed after completion")

    operation = "fleet.hermes.run"
    incoming = _IncomingTask(_payload(operation), operation)

    asyncio.run(_worker(CompletingErrorHermes(), state_path).handle_task(incoming))

    assert incoming.failed is None
    assert _completed_text(incoming) == "winner"
    binding = RunBindingStore(state_path).get("task-1")
    assert binding is not None
    assert binding.state == "completed"


def test_fleet_node_replays_completed_binding_without_second_run(tmp_path) -> None:
    operation = "fleet.hermes.run"
    state_path = tmp_path / "bindings.db"
    hermes = _Hermes()
    worker = _worker(hermes, state_path)
    first = _IncomingTask(_payload(operation), operation)
    reclaimed = _IncomingTask(_payload(operation), operation)

    asyncio.run(worker.handle_task(first))
    asyncio.run(worker.handle_task(reclaimed))

    assert len(hermes.start_calls) == 1
    assert len(hermes.wait_calls) == 1
    assert _completed_text(reclaimed) == "terminal answer"


def test_fleet_node_resumes_known_run_without_second_start(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    operation = "fleet.hermes.run"
    state_path = tmp_path / "bindings.db"
    bindings = RunBindingStore(state_path)
    bindings.reserve("task-1")
    bindings.bind_run("task-1", "run-vps-1")
    hermes = _Hermes()
    incoming = _IncomingTask(_payload(operation), operation)

    asyncio.run(_worker(hermes, state_path).handle_task(incoming))

    assert hermes.start_calls == []
    assert hermes.wait_calls == [("run-vps-1", 4.0)]
    assert _completed_text(incoming) == "terminal answer"


def test_fleet_node_fails_closed_on_uncertain_creating_binding(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    operation = "fleet.hermes.run"
    state_path = tmp_path / "bindings.db"
    RunBindingStore(state_path).reserve("task-1")
    hermes = _Hermes()
    incoming = _IncomingTask(_payload(operation), operation)

    asyncio.run(_worker(hermes, state_path).handle_task(incoming))

    assert hermes.start_calls == []
    assert hermes.wait_calls == []
    assert incoming.failed == "Fleet execution binding is indeterminate"
    assert RunBindingStore(state_path).get("task-1").state == "indeterminate"


def test_fleet_node_message_returns_safe_ack_without_calling_hermes(tmp_path) -> None:
    operation = "fleet.message"
    incoming = _IncomingTask(_payload(operation), operation)
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == []
    assert hermes.wait_calls == []
    assert hermes.health_calls == 0
    assert incoming.failed is None
    assert json.loads(_completed_text(incoming)) == {
        "correlation_id": "corr-1",
        "operation": "fleet.message",
        "received_by": "peer-vps",
        "sender_peer_id": "peer-katana",
        "status": "received",
        "topic": "smoke-test",
    }


def test_fleet_node_health_and_inventory_never_start_hermes(tmp_path) -> None:
    hermes = _Hermes()
    worker = _worker(hermes, tmp_path / "bindings.db")

    health = _IncomingTask(_payload("fleet.health"), "fleet.health")
    inventory = _IncomingTask(_payload("fleet.inventory"), "fleet.inventory")
    asyncio.run(worker.handle_task(health))
    asyncio.run(worker.handle_task(inventory))

    assert hermes.start_calls == []
    assert hermes.wait_calls == []
    assert hermes.health_calls == 1
    assert json.loads(_completed_text(health)) == {
        "adapter": "ok",
        "hermes": {
            "api": "healthy",
            "run_status": True,
            "run_stop": True,
            "run_submission": True,
        },
        "keryx_delivery": "received",
        "operation": "fleet.health",
        "status": "ok",
    }
    assert json.loads(_completed_text(inventory)) == {
        "capabilities": [
            "fleet.health",
            "fleet.hermes.run",
            "fleet.inventory",
            "fleet.message",
        ],
        "node": {"name": "vps", "peer_id": "peer-vps", "version": "0.1.0"},
        "operation": "fleet.inventory",
        "status": "ok",
    }


def test_fleet_node_health_fails_within_remaining_absolute_deadline(tmp_path) -> None:
    class SlowHermes(_Hermes):
        def health(self, *, timeout_seconds: float | None = None) -> dict[str, object]:
            self.health_calls += 1
            self.health_timeouts.append(timeout_seconds)
            time.sleep(0.3)
            return {
                "api": "healthy",
                "run_submission": True,
                "run_status": True,
                "run_stop": True,
            }

    operation = "fleet.health"
    incoming = _IncomingTask(
        _payload(operation),
        operation,
        metadata=_metadata(operation, fleet_deadline_ms="10020"),
    )
    hermes = SlowHermes()

    async def exercise() -> float:
        started = time.monotonic()
        await _worker(hermes, tmp_path / "bindings.db").handle_task(incoming)
        return time.monotonic() - started

    elapsed = asyncio.run(exercise())

    assert elapsed < 0.15
    assert incoming.completed is None
    assert incoming.failed == "Fleet task deadline has expired"
    assert hermes.health_timeouts == [pytest.approx(0.02)]


def test_fleet_node_inventory_reports_only_advertised_operations(tmp_path) -> None:
    direct_operations = ("fleet.health", "fleet.inventory", "fleet.message")
    inventory = _IncomingTask(_payload("fleet.inventory"), "fleet.inventory")

    asyncio.run(
        _worker(
            _Hermes(),
            tmp_path / "bindings.db",
            advertised_operations=direct_operations,
        ).handle_task(inventory)
    )

    assert json.loads(_completed_text(inventory))["capabilities"] == list(
        direct_operations
    )


def test_fleet_node_rejects_untrusted_sender_without_calling_hermes(tmp_path) -> None:
    operation = "fleet.hermes.run"
    incoming = _IncomingTask(
        _payload(operation), operation, peer_id="peer-not-controller"
    )
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet sender is not authorized"


def test_fleet_node_rejects_keryx_metadata_mismatch_without_calling_hermes(
    tmp_path,
) -> None:
    operation = "fleet.message"
    incoming = _IncomingTask(
        _payload(operation),
        operation,
        metadata=_metadata(operation, **{"fleet.operation": "fleet.hermes.run"}),
    )
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet delivery metadata does not match envelope"


@pytest.mark.parametrize("deadline", (None, "not-a-deadline", "9223372036854775808"))
def test_fleet_node_rejects_missing_or_malformed_absolute_deadline(
    tmp_path,
    deadline,
) -> None:
    operation = "fleet.message"
    metadata = {
        "fleet.envelope_version": "1",
        "fleet.operation": operation,
        "fleet.target_peer_id": "peer-vps",
    }
    if deadline is not None:
        metadata["fleet_deadline_ms"] = deadline
    incoming = _IncomingTask(_payload(operation), operation, metadata=metadata)
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet delivery metadata does not match envelope"


@pytest.mark.parametrize("task_id", ("", " task-1", "task\n1"))
def test_fleet_node_rejects_invalid_task_identity_before_binding(
    tmp_path,
    task_id,
) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    operation = "fleet.hermes.run"
    state_path = tmp_path / "bindings.db"
    incoming = _IncomingTask(_payload(operation), operation, task_id=task_id)
    hermes = _Hermes()

    asyncio.run(_worker(hermes, state_path).handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet delivery has invalid Keryx task identity"
    assert RunBindingStore(state_path).get("task-1") is None


def test_fleet_node_rejects_deferred_export_paths_without_calling_hermes(
    tmp_path,
) -> None:
    operation = "fleet.hermes.run"
    incoming = _IncomingTask(
        _payload(
            operation,
            {
                "prompt": "Return a short text result.",
                "export_paths": ["reports/out.txt"],
            },
        ),
        operation,
    )
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet artifact exports are not available"


def test_fleet_node_rejects_expired_absolute_deadline_without_calling_hermes(
    tmp_path,
) -> None:
    operation = "fleet.hermes.run"
    incoming = _IncomingTask(
        _payload(operation),
        operation,
        metadata=_metadata(operation, fleet_deadline_ms="9999"),
    )
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet task deadline has expired"


def test_fleet_node_rechecks_deadline_immediately_before_run_start(tmp_path) -> None:
    operation = "fleet.hermes.run"
    incoming = _IncomingTask(_payload(operation), operation)
    hermes = _Hermes()
    clock = iter((10_000, 14_000))

    asyncio.run(
        _worker(
            hermes,
            tmp_path / "bindings.db",
            now_ms=lambda: next(clock),
        ).handle_task(incoming)
    )

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet Hermes submission is indeterminate"


def test_fleet_node_bounds_stop_after_bound_run_deadline(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    binding_path = tmp_path / "bindings.db"
    store = RunBindingStore(binding_path)
    store.reserve("task-bound")
    store.bind_run("task-bound", "run-bound")
    operation = "fleet.hermes.run"
    incoming = _IncomingTask(
        _payload(operation),
        operation,
        task_id="task-bound",
        metadata=_metadata(operation, fleet_deadline_ms="10020"),
    )
    hermes = _Hermes()
    clock = iter((10_000, 10_021))

    asyncio.run(
        _worker(
            hermes,
            binding_path,
            now_ms=lambda: next(clock),
        ).handle_task(incoming)
    )

    assert incoming.failed == "Fleet task deadline has expired"
    assert hermes.stop_calls == ["run-bound"]
    assert hermes.stop_timeouts == [0.25]


def test_fleet_node_normalizes_invalid_remote_envelopes(tmp_path) -> None:
    operation = "fleet.message"
    incoming = _IncomingTask("not-json", operation)
    hermes = _Hermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet task envelope is invalid"


def test_fleet_node_binds_one_dispatcher_to_a_keryx_compatible_node(tmp_path) -> None:
    handlers: list[Any] = []
    node = SimpleNamespace(on_task=handlers.append)
    worker = _worker(_Hermes(), tmp_path / "bindings.db")

    worker.bind(node)

    assert len(handlers) == 1
    assert callable(handlers[0])


def test_fleet_node_health_and_inventory_add_readiness_when_observation_is_configured(
    tmp_path,
) -> None:
    readiness = {
        "managed_state": "active",
        "admission_generation": 1,
        "alive": True,
        "fresh": True,
        "scheduler_ready": True,
        "observation_age_ms": 10,
        "reasons": [],
        "last_observation": {
            "admission_generation": 1,
            "observed_at_ms": 1000,
            "received_at_ms": 1001,
            "network": "reachable",
            "keryx": "available",
            "hermes": "available",
            "worker": "available",
        },
        "capacity": {
            "active_workers": 0,
            "max_workers": 1,
            "available_worker_slots": 1,
        },
        "profiles": [{"name": "agency-backend-engineer", "version": "0.1.0"}],
        "resources": {
            "cpu": None,
            "ram": None,
            "swap": None,
            "disk": None,
            "gpu": None,
        },
    }
    worker = _worker(
        _Hermes(),
        tmp_path / "bindings.db",
        readiness_inspector=lambda: readiness,
    )
    health = _IncomingTask(_payload("fleet.health"), "fleet.health")
    inventory = _IncomingTask(_payload("fleet.inventory"), "fleet.inventory")

    asyncio.run(worker.handle_task(health))
    asyncio.run(worker.handle_task(inventory))

    assert json.loads(_completed_text(health))["readiness"] == readiness
    assert json.loads(_completed_text(inventory))["readiness"] == readiness

    readiness["extra"] = "not-owned"
    malformed_worker = _worker(
        _Hermes(),
        tmp_path / "malformed-bindings.db",
        readiness_inspector=lambda: readiness,
    )
    malformed = _IncomingTask(_payload("fleet.inventory"), "fleet.inventory")
    asyncio.run(malformed_worker.handle_task(malformed))
    assert "readiness" not in json.loads(_completed_text(malformed))


def test_restart_capacity_fails_closed_for_unresolved_durable_binding(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    binding_path = tmp_path / "bindings.db"
    store = RunBindingStore(binding_path)
    store.reserve("task-restart")
    store.bind_run("task-restart", "run-restart")
    worker = _worker(_Hermes(), binding_path)

    assert worker.active_worker_count == 0
    assert worker.observed_active_worker_count == 1

    store.complete("task-restart", "run-restart", "done")
    assert worker.observed_active_worker_count == 0


def test_restart_unresolved_binding_blocks_a_distinct_new_execution(tmp_path) -> None:
    from hermes_fleet.run_binding import RunBindingStore

    binding_path = tmp_path / "bindings.db"
    store = RunBindingStore(binding_path)
    store.reserve("task-restart")
    store.bind_run("task-restart", "run-restart")
    hermes = _Hermes()
    incoming = _IncomingTask(
        _payload("fleet.hermes.run"),
        "fleet.hermes.run",
        task_id="task-new",
    )

    asyncio.run(_worker(hermes, binding_path).handle_task(incoming))

    assert incoming.failed == "Fleet worker has no available execution slot"
    assert hermes.start_calls == []
    assert store.get("task-new") is None


def test_bound_hermes_runs_are_counted_for_worker_capacity(tmp_path) -> None:
    class Node:
        handler = None

        def on_task(self, handler) -> None:
            self.handler = handler

    class BlockingHermes(_Hermes):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def wait(self, *, run_id: str, timeout_seconds: float):
            self.wait_calls.append((run_id, timeout_seconds))
            self.entered.set()
            assert self.release.wait(1)
            from hermes_fleet.hermes_runs import HermesRunResult

            return HermesRunResult(run_id=run_id, text="terminal answer")

    hermes = BlockingHermes()
    capacity = []

    async def observe(active_workers: int) -> None:
        capacity.append(active_workers)

    worker = _worker(
        hermes,
        tmp_path / "bindings.db",
        capacity_observer=observe,
    )
    node = Node()
    worker.bind(node)
    incoming = _IncomingTask(_payload("fleet.hermes.run"), "fleet.hermes.run")

    async def exercise() -> None:
        assert node.handler is not None
        task = asyncio.create_task(node.handler(incoming))
        assert await asyncio.to_thread(hermes.entered.wait, 1)
        assert worker.active_worker_count == 1
        hermes.release.set()
        await task
        assert worker.active_worker_count == 0

    asyncio.run(exercise())
    assert capacity == [1, 0]
