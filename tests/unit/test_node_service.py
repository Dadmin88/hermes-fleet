from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _runtime(tmp_path):
    from hermes_fleet.models import FleetDefaults, NodeConfig, NodePolicy
    from hermes_fleet.node_service import NodeRuntimeConfig

    return NodeRuntimeConfig(
        target=NodeConfig(
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
        defaults=FleetDefaults(),
        controller_peer_ids=("peer-katana",),
        hermes_endpoint="http://127.0.0.1:8642",
        hermes_api_key="test-api-key",
        binding_path=tmp_path / "run-bindings.sqlite3",
        keryx_node_token="test-node-token",
    )


class _Node:
    def __init__(self, *, card, node_token: str, worker_concurrency: int) -> None:
        self.card = card
        self.node_token = node_token
        self.worker_concurrency = worker_concurrency
        self.peer_id = "peer-vps"
        self.handler = None
        self.started = False
        self.registration = None
        self.served = False
        self.stopped = False
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    def on_task(self, handler) -> None:
        self.handler = handler

    async def start_registration(self, *, ttl_seconds: int) -> dict[str, object]:
        self.registration = ttl_seconds
        return {"accepted": True, "peer_id": self.peer_id}

    async def serve_forever(self) -> None:
        self.served = True
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()


def test_fleet_node_service_registers_four_operations_and_stops_cleanly(
    tmp_path,
) -> None:
    from hermes_fleet.node_service import operation_specs, run_node_service

    card = SimpleNamespace(
        name="fleet-node:vps",
        skills=[SimpleNamespace(id=operation) for operation, _ in operation_specs()],
    )
    created: list[_Node] = []

    def factory(**kwargs):
        node = _Node(**kwargs)
        created.append(node)
        return node

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            _runtime(tmp_path),
            card=card,
            node_factory=factory,
            shutdown=shutdown,
        )

    asyncio.run(exercise())

    assert [skill.id for skill in card.skills] == [
        "fleet.health",
        "fleet.inventory",
        "fleet.message",
        "fleet.hermes.run",
    ]
    node = created[0]
    assert node.started is True
    assert node.handler is not None
    assert node.registration == 300
    assert node.served is True
    assert node.stopped is True
    assert node.worker_concurrency == 1
    assert node.node_token == "test-node-token"


def test_fleet_node_service_rejects_wrong_local_keryx_identity(tmp_path) -> None:
    from hermes_fleet.node_service import run_node_service

    class WrongNode(_Node):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.peer_id = "wrong-peer"

    node = WrongNode(
        card=SimpleNamespace(
            skills=[
                SimpleNamespace(id=operation)
                for operation in (
                    "fleet.health",
                    "fleet.inventory",
                    "fleet.message",
                    "fleet.hermes.run",
                )
            ]
        ),
        node_token="test-node-token",
        worker_concurrency=1,
    )

    with pytest.raises(RuntimeError, match="does not match configured Fleet peer"):
        asyncio.run(
            run_node_service(
                _runtime(tmp_path),
                card=node.card,
                node_factory=lambda **_kwargs: node,
                shutdown=asyncio.Event(),
            )
        )

    assert node.stopped is True
    assert node.registration is None


def test_node_runtime_config_redacts_secrets_from_repr(tmp_path) -> None:
    rendered = repr(_runtime(tmp_path))

    assert "test-api-key" not in rendered
    assert "test-node-token" not in rendered
