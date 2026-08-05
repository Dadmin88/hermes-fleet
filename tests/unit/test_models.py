"""Behavior tests for Fleet's transport-independent domain models."""

from __future__ import annotations

import pytest


def test_node_config_maps_friendly_name_to_opaque_keryx_peer_id() -> None:
    """A node uses a friendly name and opaque Keryx identity, never a URL."""
    from hermes_fleet.models import NodeConfig, NodePolicy

    node = NodeConfig(
        name="alpha-node",
        peer_id="12D3KooWExamplePeer",
        tags=("linux", "gpu"),
        enabled=True,
        priority=10,
        policy=NodePolicy(allowed_operations=("fleet.health",)),
    )

    assert node.name == "alpha-node"
    assert node.peer_id == "12D3KooWExamplePeer"
    assert node.tags == ("gpu", "linux")
    assert node.policy.allowed_operations == ("fleet.health",)
    with pytest.raises(ValueError, match="peer_id"):
        NodeConfig(name="alpha", peer_id="has whitespace")
    with pytest.raises(ValueError, match="peer_id"):
        NodeConfig(name="alpha", peer_id="\u0000control")
    with pytest.raises(ValueError, match="name"):
        NodeConfig(name="not valid", peer_id="peer")
    with pytest.raises(ValueError, match="tag"):
        NodeConfig(name="alpha", peer_id="peer", tags=("not valid",))
