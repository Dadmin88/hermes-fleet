from __future__ import annotations

import asyncio


class _Keryx:
    def __init__(self, *, fail_discovery: bool = False) -> None:
        self.fail_discovery = fail_discovery

    async def list_peers(self):
        return [
            {"peer_id": "peer-katana", "connected": True, "local": True},
            {"peer_id": "peer-vps", "connected": False, "local": False},
        ]

    async def discover(self, operation: str, *, limit: int):
        assert limit == 100
        if self.fail_discovery:
            raise RuntimeError("registry unavailable")
        if operation in {"fleet.health", "fleet.message", "fleet.hermes.run"}:
            return [{"peer_id": "peer-vps", "skill_id": operation}]
        return []


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
                tags=("worker",),
                policy=NodePolicy(
                    allowed_operations=(
                        "fleet.health",
                        "fleet.inventory",
                        "fleet.message",
                        "fleet.hermes.run",
                    )
                ),
            ),
            NodeConfig(
                name="offline",
                peer_id="peer-offline",
                policy=NodePolicy(allowed_operations=("fleet.health",)),
            ),
        ),
    )


def test_live_inventory_distinguishes_direct_registry_and_offline_state() -> None:
    from hermes_fleet.live_inventory import list_live_nodes

    views = asyncio.run(list_live_nodes(_Keryx(), _config()))

    assert [view.name for view in views] == ["offline", "vps"]
    offline, vps = views
    assert offline.reachability == "not_visible"
    assert offline.registry_state == "not_visible"
    assert offline.capabilities == ()
    assert vps.reachability == "registry_visible"
    assert vps.direct_connected is False
    assert vps.registry_state == "visible"
    assert vps.capabilities == (
        "fleet.health",
        "fleet.hermes.run",
        "fleet.message",
    )


def test_live_inventory_reports_registry_unknown_without_fabricating_offline() -> None:
    from hermes_fleet.live_inventory import list_live_nodes

    views = asyncio.run(list_live_nodes(_Keryx(fail_discovery=True), _config()))

    assert all(view.registry_state == "unknown" for view in views)
    assert all(view.reachability == "unknown" for view in views)
