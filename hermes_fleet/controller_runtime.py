"""Short-lived authenticated Keryx runtime shared by Fleet controller surfaces."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from ._paths import is_concrete_path
from .config import FleetConfig, get_fleet_dir, load_fleet_config

_Result = TypeVar("_Result")


class _ControllerNode(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


async def run_controller_action(
    action: Callable[[_ControllerNode, FleetConfig], Awaitable[_Result]],
    *,
    config_path: Path | None = None,
    node_token: str,
    node_factory: Callable[..., _ControllerNode] | None = None,
) -> _Result:
    """Run one action against one authenticated local Keryx SDK node."""
    if not callable(action):
        raise ValueError("action must be callable")
    path = config_path or (get_fleet_dir() / "nodes.yaml")
    if not is_concrete_path(path) or not path.is_absolute():
        raise ValueError("config_path must be an absolute Path")
    if type(node_token) is not str or not node_token:
        raise ValueError("KERYX_NODE_TOKEN is required")
    factory = node_factory or _node_factory
    node = factory(node_token=node_token, worker_concurrency=1)
    started = False
    try:
        await node.start()
        started = True
        return await action(node, load_fleet_config(path))
    finally:
        if started:
            await node.stop()


def _node_factory(**kwargs: Any) -> _ControllerNode:
    try:
        from keryx.node import KeryxNode
    except ImportError as error:
        raise RuntimeError(
            "Fleet controller requires the pinned Keryx Python SDK"
        ) from error
    return cast(_ControllerNode, KeryxNode(**kwargs))
