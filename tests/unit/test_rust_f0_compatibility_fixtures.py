"""Python-oracle checks for the language-neutral Rust F0 fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "f0"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_domain_fixture_matches_python_operation_and_selection_truth() -> None:
    from hermes_fleet.envelope import OPERATIONS
    from hermes_fleet.models import NodeConfig, NodePolicy
    from hermes_fleet.selection import select_nodes

    fixture = _load("domain-v1.json")
    for case in fixture["operations"]:
        assert (case["value"] in OPERATIONS) is case["valid"]
        assert (case["value"] == "fleet.hermes.run") is case["executable"]

    selection = fixture["selection"]
    nodes = tuple(
        NodeConfig(
            name=node["name"],
            peer_id=node["peer_id"],
            enabled=node["enabled"],
            priority=node["priority"],
            tags=tuple(node["tags"]),
            policy=NodePolicy(allowed_operations=tuple(node["allowed"])),
        )
        for node in selection["nodes"]
    )
    for case in selection["cases"]:
        if expected := case.get("expected_error"):
            message = {
                "unknown_node": "unknown node names",
                "unknown_tag": "unknown tags",
                "mixed_selectors": "names and tags cannot be selected together",
            }[expected]
            with pytest.raises(ValueError, match=message):
                select_nodes(nodes, names=case["names"], tags=case["tags"])
            continue
        selected = select_nodes(nodes, names=case["names"], tags=case["tags"])
        assert [node.name for node in selected] == case["expected"]


def test_managed_projection_fixture_matches_python_outcomes(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    fixture = _load("managed-projection-v1.json")
    store = ManagedProjectionStore(tmp_path / "managed-projections.sqlite3")
    for step in fixture["steps"]:
        document = step["document"]
        result = store.apply(
            **{
                **document,
                "generated_operations": tuple(document["generated_operations"]),
            }
        )
        assert result.outcome == step["expected_outcome"]
        generated = store.inspect(
            source=document["source"],
            network_id=document["network_id"],
            device_id=document["device_id"],
        )["generated"]
        assert generated["projection_generation"] == step["expected_generation"]
