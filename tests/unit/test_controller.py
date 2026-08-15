from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest


@dataclass(frozen=True)
class _Receipt:
    task_id: str = "task-keryx-1"
    status: str = "submitted"
    routed_to: str = "peer-vps"
    delivery_route: str = "relay"


class _Handle:
    def __init__(self, *, text: str = "result text") -> None:
        self.task_id = "task-keryx-1"
        self.receipt = _Receipt()
        self._text = text

    async def wait(self, timeout: float | None = None):
        del timeout
        return SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            artifacts=[
                SimpleNamespace(
                    parts=[SimpleNamespace(text=self._text, media_type="text/plain")]
                )
            ],
        )


class _Keryx:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.handle = _Handle()

    async def send_task(self, message, **kwargs):
        self.calls.append({"message": message, **kwargs})
        return self.handle

    def task_handle(self, task_id: str):
        assert task_id == "execution-1"
        return self.handle


def _config():
    from hermes_fleet.config import FleetConfig
    from hermes_fleet.models import FleetDefaults, NodeConfig, NodePolicy

    operations = (
        "fleet.health",
        "fleet.inventory",
        "fleet.message",
        "fleet.hermes.run",
    )
    policy = NodePolicy(allowed_operations=operations)
    return FleetConfig(
        schema_version=1,
        defaults=FleetDefaults(),
        nodes=(
            NodeConfig(name="other", peer_id="peer-other", policy=policy),
            NodeConfig(name="vps", peer_id="peer-vps", policy=policy),
        ),
    )


def test_controller_submits_direct_message_to_one_exact_configured_peer() -> None:
    from hermes_fleet.controller import submit_communication

    keryx = _Keryx()
    submission = asyncio.run(
        submit_communication(
            keryx=keryx,
            config=_config(),
            target_name="vps",
            operation="fleet.message",
            input_data={
                "text": "FLEET_MESSAGE_OK",
                "topic": "smoke-test",
                "correlation_id": "corr-1",
            },
            deadline_seconds=30,
            now_ms=lambda: 10_000,
        )
    )

    assert len(keryx.calls) == 1
    call = keryx.calls[0]
    assert call["peer_id"] == "peer-vps"
    assert call["deadline_ms"] == 40_000
    assert call["metadata"] == {
        "fleet.envelope_version": "1",
        "fleet.operation": "fleet.message",
        "fleet.target_peer_id": "peer-vps",
        "fleet_deadline_ms": "40000",
        "skill": "fleet.message",
    }
    payload = json.loads(call["message"]["parts"][0]["text"])
    assert payload["operation"] == "fleet.message"
    assert payload["input"] == {
        "correlation_id": "corr-1",
        "text": "FLEET_MESSAGE_OK",
        "topic": "smoke-test",
    }
    assert submission.target.name == "vps"
    assert submission.task_id == "task-keryx-1"
    assert submission.routed_to == "peer-vps"
    assert submission.delivery_route == "relay"


def test_controller_submits_one_exact_execution_package_with_preselected_identity() -> (
    None
):
    from hermes_fleet.controller import submit_execution_package
    from hermes_fleet.execution_package import EXECUTION_PACKAGE_MEDIA_TYPE

    keryx = _Keryx()
    keryx.handle.task_id = "execution-1"
    keryx.handle.receipt = _Receipt(task_id="execution-1")
    payload = b"immutable execution package"
    submission = asyncio.run(
        submit_execution_package(
            keryx=keryx,
            peer_id="peer-vps",
            task_id="execution-1",
            idempotency_key="execution-1",
            package_payload=payload,
            package_hash="sha256:" + "1" * 64,
            deadline_ms=40_000,
        )
    )

    assert len(keryx.calls) == 1
    call = keryx.calls[0]
    assert call["peer_id"] == "peer-vps"
    assert call["task_id"] == "execution-1"
    assert call["idempotency_key"] == "execution-1"
    assert call["message"]["parts"] == [
        {
            "text": "",
            "raw": payload,
            "media_type": EXECUTION_PACKAGE_MEDIA_TYPE,
        }
    ]
    assert call["metadata"]["fleet.execution_package_hash"] == "sha256:" + "1" * 64
    assert submission.task_id == "execution-1"


def test_uncertain_execution_submission_retries_same_identity_before_reopen() -> None:
    from hermes_fleet.controller import submit_execution_package

    class Uncertain(_Keryx):
        async def send_task(self, message, **kwargs):
            self.calls.append({"message": message, **kwargs})
            raise TimeoutError("response lost")

    keryx = Uncertain()
    keryx.handle.task_id = "execution-1"
    keryx.handle.receipt = None
    submission = asyncio.run(
        submit_execution_package(
            keryx=keryx,
            peer_id="peer-vps",
            task_id="execution-1",
            idempotency_key="execution-1",
            package_payload=b"immutable execution package",
            package_hash="sha256:" + "1" * 64,
            deadline_ms=40_000,
        )
    )

    assert len(keryx.calls) == 3
    assert {call["task_id"] for call in keryx.calls} == {"execution-1"}
    assert {call["idempotency_key"] for call in keryx.calls} == {"execution-1"}
    assert submission.task_id == "execution-1"
    assert submission.delivery_route == "reconciled"


def test_transient_execution_submission_recovers_by_same_identity_redelivery() -> None:
    from hermes_fleet.controller import submit_execution_package

    class Flaky(_Keryx):
        async def send_task(self, message, **kwargs):
            self.calls.append({"message": message, **kwargs})
            if len(self.calls) < 3:
                raise TimeoutError("relay acknowledgement lost")
            return self.handle

    keryx = Flaky()
    keryx.handle.task_id = "execution-1"
    keryx.handle.receipt = _Receipt(task_id="execution-1")
    submission = asyncio.run(
        submit_execution_package(
            keryx=keryx,
            peer_id="peer-vps",
            task_id="execution-1",
            idempotency_key="execution-1",
            package_payload=b"immutable execution package",
            package_hash="sha256:" + "1" * 64,
            deadline_ms=40_000,
        )
    )

    assert len(keryx.calls) == 3
    assert all(call["task_id"] == "execution-1" for call in keryx.calls)
    assert all(call["idempotency_key"] == "execution-1" for call in keryx.calls)
    assert submission.task_id == "execution-1"
    assert submission.delivery_route == "relay"


def test_controller_builds_each_initial_operation_without_transport_branching() -> None:
    from hermes_fleet.controller import submit_communication

    cases = (
        ("fleet.health", {}),
        ("fleet.inventory", {}),
        (
            "fleet.hermes.run",
            {"prompt": "Return exactly FLEET_OK", "export_paths": []},
        ),
    )
    for operation, input_data in cases:
        keryx = _Keryx()
        asyncio.run(
            submit_communication(
                keryx=keryx,
                config=_config(),
                target_name="vps",
                operation=operation,
                input_data=input_data,
                deadline_seconds=30,
                now_ms=lambda: 10_000,
            )
        )
        payload = json.loads(keryx.calls[0]["message"]["parts"][0]["text"])
        assert payload["operation"] == operation
        assert payload["input"] == input_data


def test_controller_returns_untrusted_terminal_text_from_keryx() -> None:
    from hermes_fleet.controller import submit_communication, wait_text_result

    keryx = _Keryx()
    submission = asyncio.run(
        submit_communication(
            keryx=keryx,
            config=_config(),
            target_name="vps",
            operation="fleet.health",
            input_data={},
            deadline_seconds=30,
            now_ms=lambda: 10_000,
        )
    )

    output = asyncio.run(wait_text_result(submission, timeout_seconds=30))

    assert output.text == "result text"
    assert output.untrusted is True


def test_controller_rejects_unknown_or_disabled_exact_targets() -> None:
    import pytest

    from hermes_fleet.config import FleetConfig
    from hermes_fleet.controller import submit_communication
    from hermes_fleet.models import FleetDefaults, NodeConfig

    config = FleetConfig(
        schema_version=1,
        defaults=FleetDefaults(),
        nodes=(NodeConfig(name="vps", peer_id="peer-vps", enabled=False),),
    )

    with pytest.raises(ValueError, match="enabled"):
        asyncio.run(
            submit_communication(
                keryx=_Keryx(),
                config=config,
                target_name="vps",
                operation="fleet.health",
                input_data={},
                deadline_seconds=30,
                now_ms=lambda: 10_000,
            )
        )


def test_high_level_controller_returns_message_ack_and_actual_route() -> None:
    from hermes_fleet.controller import FleetController

    keryx = _Keryx()
    keryx.handle = _Handle(
        text=json.dumps(
            {
                "operation": "fleet.message",
                "status": "received",
                "received_by": "peer-vps",
            }
        )
    )

    result = asyncio.run(
        FleetController(keryx=keryx, config=_config()).send_message(
            "vps",
            "FLEET_MESSAGE_OK",
            topic="smoke-test",
            correlation_id="corr-1",
            deadline_seconds=30,
        )
    )

    assert result.task_id == "task-keryx-1"
    assert result.routed_to == "peer-vps"
    assert result.delivery_route == "relay"
    assert result.untrusted is True
    assert result.response == {
        "operation": "fleet.message",
        "received_by": "peer-vps",
        "status": "received",
    }


def test_high_level_controller_accepts_worker_inventory_response() -> None:
    from hermes_fleet.controller import FleetController

    keryx = _Keryx()
    expected = {
        "operation": "fleet.inventory",
        "status": "ok",
        "node": {"name": "vps", "peer_id": "peer-vps", "version": "0.1.0"},
        "capabilities": [
            "fleet.health",
            "fleet.hermes.run",
            "fleet.inventory",
            "fleet.message",
        ],
        "readiness": {
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
        },
    }
    keryx.handle = _Handle(text=json.dumps(expected))

    result = asyncio.run(
        FleetController(keryx=keryx, config=_config()).get_inventory(
            "vps",
            deadline_seconds=30,
        )
    )

    assert result.response == expected
    assert result.routed_to == "peer-vps"
    assert result.untrusted is True

    expected["readiness"]["extra"] = "not-owned"
    keryx.handle = _Handle(text=json.dumps(expected))
    with pytest.raises(RuntimeError, match="invalid direct response"):
        asyncio.run(
            FleetController(keryx=keryx, config=_config()).get_inventory(
                "vps",
                deadline_seconds=30,
            )
        )
