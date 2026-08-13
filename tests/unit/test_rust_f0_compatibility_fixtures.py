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


def test_domain_fixture_matches_python_authority_truth(tmp_path) -> None:
    from hermes_fleet.managed_projection import ManagedProjectionStore

    fixture = _load("domain-v1.json")
    store = ManagedProjectionStore(tmp_path / "managed-authority.sqlite3")
    for index, case in enumerate(fixture["managed_authority"], start=1):
        device_id = f"authority-{index}"
        key = {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": device_id,
        }
        assert (
            store.apply(
                source=key["source"],
                network_id=key["network_id"],
                device_id=key["device_id"],
                projection_generation="1",
                membership_generation="1",
                binding_generation="1",
                content_hash=f"{index:064x}",
                operation="upsert",
                generated_operations=tuple(case["generated"]),
                provenance={**key, "snapshot": "1"},
            ).outcome
            == "applied"
        )
        for operation in case["denied"]:
            store.set_operator_deny(**key, operation=operation, denied=True)
        inspected = store.inspect(**key)
        assert list(inspected["generated"]["allowed_operations"]) == sorted(
            case["generated"]
        )
        assert list(inspected["effective"]["allowed_operations"]) == case["expected"]


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
        expected = fixture["steps"][step["expected_record_from_step"]]["document"]
        expected_state = {
            "upsert": "active",
            "disable": "disabled",
            "remove": "removed",
        }[expected["operation"]]
        assert generated == {
            "state": expected_state,
            "projection_generation": expected["projection_generation"],
            "membership_generation": expected["membership_generation"],
            "binding_generation": expected["binding_generation"],
            "content_hash": expected["content_hash"],
            "allowed_operations": tuple(
                sorted(expected["generated_operations"])
                if expected_state == "active"
                else ()
            ),
            "provenance": expected["provenance"],
        }
