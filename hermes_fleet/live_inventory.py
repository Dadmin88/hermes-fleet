"""Truthful live Fleet inventory projected from config and public Keryx state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from .config import FleetConfig
from .envelope import OPERATIONS


class _KeryxInventory(Protocol):
    async def list_peers(self) -> list[dict[str, Any]]: ...

    async def discover(self, operation: str, *, limit: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class LiveNodeView:
    """Configured identity plus distinct Keryx reachability observations."""

    name: str
    peer_id: str
    tags: tuple[str, ...]
    enabled: bool
    priority: int
    direct_connected: bool
    local: bool
    registry_state: str
    reachability: str
    capabilities: tuple[str, ...]


async def list_live_nodes(
    keryx: _KeryxInventory, config: FleetConfig
) -> tuple[LiveNodeView, ...]:
    """Merge configured Fleet names with truthful Keryx peer/registry observations."""
    if type(config) is not FleetConfig:
        raise ValueError("config must be a FleetConfig")
    if not callable(getattr(keryx, "list_peers", None)) or not callable(
        getattr(keryx, "discover", None)
    ):
        raise ValueError("keryx must provide list_peers() and discover()")

    peer_rows = await keryx.list_peers()
    connected: set[str] = set()
    local: set[str] = set()
    if type(peer_rows) is not list:
        raise RuntimeError("Keryx returned invalid peer inventory")
    for row in peer_rows:
        if type(row) is not dict:
            raise RuntimeError("Keryx returned invalid peer inventory")
        peer_id = row.get("peer_id")
        if type(peer_id) is not str or not peer_id:
            raise RuntimeError("Keryx returned invalid peer inventory")
        if row.get("connected") is True:
            connected.add(peer_id)
        if row.get("local") is True:
            local.add(peer_id)

    operations = tuple(sorted(OPERATIONS))
    discoveries = await asyncio.gather(
        *(keryx.discover(operation, limit=100) for operation in operations),
        return_exceptions=True,
    )
    registry_known = not any(isinstance(item, BaseException) for item in discoveries)
    capabilities: dict[str, set[str]] = {}
    if registry_known:
        for operation, rows in zip(operations, discoveries, strict=True):
            if type(rows) is not list:
                registry_known = False
                capabilities.clear()
                break
            for row in rows:
                if type(row) is not dict:
                    registry_known = False
                    capabilities.clear()
                    break
                peer_id = row.get("peer_id")
                if type(peer_id) is not str or not peer_id:
                    registry_known = False
                    capabilities.clear()
                    break
                capabilities.setdefault(peer_id, set()).add(operation)
            if not registry_known:
                break

    views: list[LiveNodeView] = []
    for node in sorted(config.nodes, key=lambda item: item.name):
        is_direct = node.peer_id in connected
        registered_capabilities = tuple(sorted(capabilities.get(node.peer_id, ())))
        if not registry_known:
            registry_state = "unknown"
        elif registered_capabilities:
            registry_state = "visible"
        else:
            registry_state = "not_visible"
        if is_direct:
            reachability = "direct"
        elif registry_state == "visible":
            reachability = "registry_visible"
        elif registry_state == "not_visible":
            reachability = "not_visible"
        else:
            reachability = "unknown"
        views.append(
            LiveNodeView(
                name=node.name,
                peer_id=node.peer_id,
                tags=node.tags,
                enabled=node.enabled,
                priority=node.priority,
                direct_connected=is_direct,
                local=node.peer_id in local,
                registry_state=registry_state,
                reachability=reachability,
                capabilities=registered_capabilities,
            )
        )
    return tuple(views)
