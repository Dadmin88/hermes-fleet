from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_fleet.agency_materialization import ImmutableAgencyBundle
from hermes_fleet.execution_package import MEDIA_TYPE as EXECUTION_PACKAGE_MEDIA_TYPE
from hermes_fleet.execution_package import (
    ExactExecutionPackage,
    serialize_execution_package,
)
from hermes_fleet.recipes import ResolvedRecipe


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
    peer_id: str = "peer-controller-1"
    metadata: dict[str, str] = field(default_factory=dict)
    completed: list[Any] | None = None
    failed: str | None = None
    package_payload: bytes | None = None
    package_media_type: str | None = None

    def __post_init__(self) -> None:
        if not self.metadata:
            self.metadata = _metadata(self.operation)

    @property
    def messages(self) -> list[Any]:
        if self.package_payload is not None:
            return [
                SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text="",
                            raw=self.package_payload,
                            media_type=self.package_media_type,
                        )
                    ]
                )
            ]
        parts = [SimpleNamespace(text=self.payload, raw=b"", media_type="text/plain")]
        return [SimpleNamespace(parts=parts)]

    async def complete(self, artifacts: list[Any]) -> None:
        self.completed = artifacts

    async def fail(self, error: str) -> None:
        self.failed = error


def _execution_package(*, admission_generation: int = 9) -> ExactExecutionPackage:
    agency_payload = b"exact immutable Agency package"
    recipe = ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": "sha256:" + "1" * 64,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "acceptance",
                "version": "1.0.0",
                "content_digest": "sha256:" + "2" * 64,
            },
            "extensions": {},
        }
    )
    return ExactExecutionPackage(
        execution_id="task-1",
        idempotency_key="operator-request-1",
        resolved_recipe=recipe,
        capabilities_hash="sha256:" + "3" * 64,
        target={
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "binding_generation": 7,
            "admission_generation": admission_generation,
        },
        authorization={
            "requester": "peer-controller-1",
            "operation": "fleet.hermes.run",
            "resolved_recipe_hash": recipe.content_hash,
            "policy_digest": "sha256:" + "4" * 64,
            "deadline_ms": 2_000_000_000_000,
            "secret_refs": [],
        },
        prompt="Return a short text result.",
        agency_bundle=ImmutableAgencyBundle(
            resolved=recipe.agent,
            archive_sha256="sha256:" + hashlib.sha256(agency_payload).hexdigest(),
            payload=agency_payload,
        ),
    )


def _packaged_incoming(
    package: ExactExecutionPackage, *, metadata_overrides: dict[str, str] | None = None
) -> _IncomingTask:
    metadata = {
        "fleet.operation": "fleet.hermes.run",
        "fleet.execution_package_hash": package.content_hash,
        "fleet_deadline_ms": str(package.authorization["deadline_ms"]),
        "skill": "fleet.hermes.run",
        **(metadata_overrides or {}),
    }
    return _IncomingTask(
        _payload("fleet.hermes.run"),
        "fleet.hermes.run",
        metadata=metadata,
        package_payload=serialize_execution_package(package),
        package_media_type=EXECUTION_PACKAGE_MEDIA_TYPE,
    )


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
            "run_finalize": True,
            "run_approval_budget": True,
            "run_tool_evidence": True,
            "run_command_evidence": True,
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
    admission_generation_inspector=None,
    managed_network_id=None,
    managed_device_id=None,
    capacity_observer=None,
    recipe_executor=None,
    backend_capabilities=None,
):
    from hermes_fleet.fleet_node import FleetNodeWorker
    from hermes_fleet.models import FleetDefaults, NodeConfig, NodePolicy

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
        controller_peer_ids=("peer-controller-1",),
        advertised_operations=advertised_operations,
        now_ms=now_ms or (lambda: 10_000),
        readiness_inspector=readiness_inspector,
        admission_generation_inspector=admission_generation_inspector,
        managed_network_id=managed_network_id,
        managed_device_id=managed_device_id,
        capacity_observer=capacity_observer,
        recipe_executor=recipe_executor,
        backend_capabilities=backend_capabilities,
    )


def _completed_text(incoming: _IncomingTask) -> str:
    assert incoming.completed is not None
    return incoming.completed[0]["parts"][0]["text"]


def test_packaged_execution_delegates_to_destination_executor_before_legacy_binding(
    tmp_path,
) -> None:
    package = _execution_package()
    incoming = _packaged_incoming(package)
    events: list[str] = []

    class Executor:
        async def execute(self, *, package, authenticated_sender, incoming):
            events.append("destination_admission")
            assert package == _execution_package()
            assert authenticated_sender == "peer-controller-1"
            await incoming.complete(
                [
                    {
                        "name": "hermes-result.txt",
                        "parts": [{"text": "fx8-result", "media_type": "text/plain"}],
                    }
                ]
            )

    hermes = _Hermes()
    worker = _worker(
        hermes,
        tmp_path / "bindings.db",
        admission_generation_inspector=lambda: 9,
        managed_network_id="network-1",
        managed_device_id="device-1",
        recipe_executor=Executor(),
    )

    asyncio.run(worker.handle_task(incoming))

    assert events == ["destination_admission"]
    assert _completed_text(incoming) == "fx8-result"
    assert hermes.start_calls == []


def test_packaged_execution_updates_worker_capacity_without_binding_ledger(
    tmp_path,
) -> None:
    incoming = _packaged_incoming(_execution_package())
    capacity: list[int] = []

    class Executor:
        async def execute(self, *, package, authenticated_sender, incoming):
            assert worker.active_worker_count == 1
            await incoming.complete([])

    async def observe(active: int) -> None:
        capacity.append(active)

    worker = _worker(
        _Hermes(),
        tmp_path / "unused.db",
        recipe_executor=Executor(),
        admission_generation_inspector=lambda: 9,
        managed_network_id="network-1",
        managed_device_id="device-1",
        capacity_observer=observe,
    )
    asyncio.run(worker.handle_task(incoming))

    assert capacity == [1, 0]
    assert worker.active_worker_count == 0


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
        "sender_peer_id": "peer-controller-1",
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
            "run_finalize": True,
            "run_approval_budget": True,
            "run_tool_evidence": True,
            "run_command_evidence": True,
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


def test_inventory_publishes_exact_backend_capabilities_only_when_enabled(
    tmp_path,
) -> None:
    from hermes_fleet.host_profile_capabilities import host_profile_capabilities

    capabilities = host_profile_capabilities(
        logical_cpus=2,
        memory_bytes=1_000_000,
        operating_system="linux",
        architecture="x86_64",
    )
    worker = _worker(
        _Hermes(),
        tmp_path / "bindings.db",
        backend_capabilities=capabilities,
    )
    inventory = _IncomingTask(_payload("fleet.inventory"), "fleet.inventory")

    asyncio.run(worker.handle_task(inventory))

    response = json.loads(_completed_text(inventory))
    assert response["execution_backend"] == {
        "content_hash": capabilities.content_hash,
        "document": capabilities.to_dict(),
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
                "run_finalize": True,
                "run_approval_budget": True,
                "run_tool_evidence": True,
                "run_command_evidence": True,
            }

    operation = "fleet.health"
    incoming = _IncomingTask(
        _payload(operation),
        operation,
        metadata=_metadata(operation, fleet_deadline_ms="10020"),
    )
    hermes = SlowHermes()

    asyncio.run(_worker(hermes, tmp_path / "bindings.db").handle_task(incoming))

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


def test_fleet_node_admits_exact_immutable_execution_package_before_binding(
    tmp_path,
) -> None:
    package = _execution_package()
    incoming = _packaged_incoming(
        package,
        metadata_overrides={
            "target_node_id": "peer-vps",
            "keryx.authenticated_source_protocol_features": (
                "absolute_deadlines_v1,result_artifact_bytes_v1"
            ),
        },
    )
    hermes = _Hermes()

    class Executor:
        def __init__(self) -> None:
            self.package = None

        async def execute(self, *, package, authenticated_sender, incoming):
            self.package = package
            assert authenticated_sender == "peer-controller-1"
            await incoming.complete([])

    executor = Executor()

    asyncio.run(
        _worker(
            hermes,
            tmp_path / "bindings.db",
            admission_generation_inspector=lambda: 9,
            managed_network_id="network-1",
            managed_device_id="device-1",
            recipe_executor=executor,
        ).handle_task(incoming)
    )

    assert incoming.failed is None
    assert executor.package == package
    assert hermes.start_calls == []


@pytest.mark.parametrize(
    "incoming, generation, network_id, device_id",
    [
        (
            _packaged_incoming(
                _execution_package(),
                metadata_overrides={
                    "fleet.execution_package_hash": "sha256:" + "0" * 64
                },
            ),
            9,
            "network-1",
            "device-1",
        ),
        (
            _packaged_incoming(
                _execution_package(), metadata_overrides={"unexpected": "value"}
            ),
            9,
            "network-1",
            "device-1",
        ),
        (_packaged_incoming(_execution_package()), 8, "network-1", "device-1"),
        (_packaged_incoming(_execution_package()), 9, "network-other", "device-1"),
        (_packaged_incoming(_execution_package()), 9, "network-1", "device-other"),
    ],
)
def test_fleet_node_rejects_package_authority_mismatch_before_binding(
    tmp_path, incoming, generation, network_id, device_id
) -> None:
    hermes = _Hermes()
    state_path = tmp_path / "bindings.db"

    asyncio.run(
        _worker(
            hermes,
            state_path,
            admission_generation_inspector=lambda: generation,
            managed_network_id=network_id,
            managed_device_id=device_id,
        ).handle_task(incoming)
    )

    assert incoming.failed == "Fleet execution package is not admitted"
    assert hermes.start_calls == []
    assert not state_path.exists()


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
    operation = "fleet.hermes.run"
    state_path = tmp_path / "bindings.db"
    incoming = _IncomingTask(_payload(operation), operation, task_id=task_id)
    hermes = _Hermes()

    asyncio.run(_worker(hermes, state_path).handle_task(incoming))

    assert hermes.start_calls == []
    assert incoming.failed == "Fleet delivery has invalid Keryx task identity"
    assert not state_path.exists()


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
