from __future__ import annotations

import asyncio


class _Node:
    def __init__(self, *, node_token: str, worker_concurrency: int) -> None:
        self.node_token = node_token
        self.worker_concurrency = worker_concurrency
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def test_controller_runtime_starts_authenticates_and_stops(tmp_path) -> None:
    from hermes_fleet.controller_runtime import run_controller_action

    created: list[_Node] = []

    def factory(**kwargs):
        node = _Node(**kwargs)
        created.append(node)
        return node

    async def action(node, config):
        assert node.started is True
        assert config.schema_version == 1
        return "done"

    config_path = tmp_path / "nodes.yaml"
    config_path.write_text(
        "schema_version: 1\nnodes: []\n",
        encoding="utf-8",
    )
    result = asyncio.run(
        run_controller_action(
            action,
            config_path=config_path,
            node_token="test-controller-token",
            node_factory=factory,
        )
    )

    assert result == "done"
    assert created[0].node_token == "test-controller-token"
    assert created[0].worker_concurrency == 1
    assert created[0].stopped is True
