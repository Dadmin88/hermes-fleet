from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass(frozen=True)
class _Receipt:
    task_id: str = "task-1"
    routed_to: str = "peer-vps"
    delivery_route: str = "relay"


class _Handle:
    task_id = "task-1"
    receipt = _Receipt()

    async def wait(self, timeout=None):
        del timeout
        text = json.dumps(
            {
                "operation": "fleet.message",
                "status": "received",
                "received_by": "peer-vps",
            }
        )
        return SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            artifacts=[
                SimpleNamespace(
                    parts=[SimpleNamespace(text=text, media_type="text/plain")]
                )
            ],
        )

    async def refresh(self):
        return SimpleNamespace(
            status=SimpleNamespace(value="completed"),
            metadata={
                "result_text": "FLEET_OK",
                "executor_peer_id": "peer-vps",
            },
        )


class _Keryx:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def list_peers(self):
        return [{"peer_id": "peer-vps", "connected": True, "local": False}]

    async def discover(self, operation: str, *, limit: int):
        del limit
        return [{"peer_id": "peer-vps", "skill_id": operation}]

    async def send_task(self, message, **kwargs):
        self.sent.append({"message": message, **kwargs})
        return _Handle()

    def task_handle(self, task_id: str):
        assert task_id == "task-1"
        return _Handle()


def _config():
    from hermes_fleet.config import FleetConfig
    from hermes_fleet.models import FleetDefaults, NodeConfig, NodePolicy

    return FleetConfig(
        schema_version=1,
        defaults=FleetDefaults(),
        nodes=(
            NodeConfig(
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
            ),
        ),
    )


def test_list_nodes_tool_reports_live_keryx_observations(monkeypatch) -> None:
    from hermes_fleet import tools

    keryx = _Keryx()

    async def run_action(action):
        return await action(keryx, _config())

    monkeypatch.setattr(tools, "_run_action", run_action)
    result = json.loads(asyncio.run(tools.fleet_list_nodes({})))

    assert result["success"] is True
    assert result["data"][0]["name"] == "vps"
    assert result["data"][0]["reachability"] == "direct"
    assert result["data"][0]["registry_state"] == "visible"


def test_message_tool_returns_ack_and_actual_route(monkeypatch) -> None:
    from hermes_fleet import tools

    keryx = _Keryx()

    async def run_action(action):
        return await action(keryx, _config())

    monkeypatch.setattr(tools, "_run_action", run_action)
    result = json.loads(
        asyncio.run(
            tools.fleet_send_message(
                {
                    "name": "vps",
                    "text": "FLEET_MESSAGE_OK",
                    "topic": "smoke",
                    "correlation_id": "corr-1",
                    "deadline_seconds": 30,
                }
            )
        )
    )

    assert result["success"] is True
    assert result["data"]["task_id"] == "task-1"
    assert result["data"]["routed_to"] == "peer-vps"
    assert result["data"]["delivery_route"] == "relay"
    assert result["data"]["response"]["status"] == "received"


def test_task_tool_reopens_durable_keryx_result(monkeypatch) -> None:
    from hermes_fleet import tools

    keryx = _Keryx()

    async def run_action(action):
        return await action(keryx, _config())

    monkeypatch.setattr(tools, "_run_action", run_action)
    result = json.loads(asyncio.run(tools.fleet_get_task({"task_id": "task-1"})))

    assert result["success"] is True
    assert result["data"] == {
        "result": {
            "executor_peer_id": "peer-vps",
            "result_text": "FLEET_OK",
        },
        "status": "completed",
        "task_id": "task-1",
        "untrusted": True,
    }
