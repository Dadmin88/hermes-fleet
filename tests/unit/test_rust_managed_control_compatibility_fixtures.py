"""Independent Python oracle for the shared Rust managed-control fixture."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_fleet.local_control import parse_request
from hermes_fleet.managed_projection import (
    ManagedProjectionStore,
    canonical_content_hash,
    verify_canonical_content_hash,
)

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "f0" / "managed-control-v1.json"


def test_shared_managed_control_fixture_matches_python_oracle(tmp_path: Path) -> None:
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["schema"] == "hermes-fleet.managed-control-compat.v1"

    capabilities = json.dumps(
        fixture["capabilities_request"], separators=(",", ":")
    ).encode()
    assert parse_request(capabilities) == ("capabilities", None)

    document = fixture["document"]
    assert verify_canonical_content_hash(document)
    material = {key: value for key, value in document.items() if key != "content_hash"}
    assert canonical_content_hash(material) == document["content_hash"]

    apply_request = {
        "schema": "fleet.managed-projection.v1",
        "kind": "apply",
        "document": document,
    }
    kind, parsed = parse_request(
        json.dumps(apply_request, separators=(",", ":")).encode()
    )
    assert kind == "apply"
    assert parsed == document

    store = ManagedProjectionStore(tmp_path / "managed.db")
    outcomes = []
    for _expected in fixture["apply_outcomes"]:
        outcomes.append(store.apply(**document, wire_document=dict(document)).outcome)
    assert outcomes == fixture["apply_outcomes"]
    inspected = store.inspect(**fixture["inspect_selector"])
    assert inspected["generated"]["state"] == fixture["expected_state"]
    assert (
        list(inspected["effective"]["allowed_operations"])
        == fixture["expected_effective_operations"]
    )
