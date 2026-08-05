"""Tests for strict, versioned and transport-independent Fleet envelopes."""

from __future__ import annotations

import json

import pytest


def test_envelope_accepts_a_bounded_hermes_run_for_its_configured_peer() -> None:
    """The only Phase-1 work envelope is schema-checked before dispatch exists."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    node = NodeConfig(name="alpha", peer_id="peer-alpha")
    message = json.dumps(
        {
            "version": 1,
            "operation": "fleet.hermes.run",
            "target": {"name": "alpha", "peer_id": "peer-alpha"},
            "input": {
                "prompt": "Run focused tests.",
                "export_paths": ["reports/out.txt"],
            },
            "limits": {"deadline_seconds": 60},
        }
    )

    envelope = parse_envelope(message, target=node, defaults=FleetDefaults())

    assert envelope.operation == "fleet.hermes.run"
    assert envelope.input["export_paths"] == ("reports/out.txt",)


def test_envelope_rejects_target_mismatches_bad_json_and_unsafe_bounds() -> None:
    """Inputs are not routing guesses and size/path limits are checked locally."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    node = NodeConfig(name="alpha", peer_id="peer-alpha")
    defaults = FleetDefaults(
        max_deadline_seconds=60, max_prompt_chars=8, max_export_paths=1
    )
    base = {
        "version": 1,
        "operation": "fleet.hermes.run",
        "target": {"name": "alpha", "peer_id": "wrong-peer"},
        "input": {
            "prompt": "too-long-prompt",
            "export_paths": ["../unsafe", "again.txt"],
        },
        "limits": {"deadline_seconds": True},
    }

    with pytest.raises(ValueError, match="target"):
        parse_envelope(json.dumps(base), target=node, defaults=defaults)
    with pytest.raises(ValueError, match="JSON object"):
        parse_envelope("[]", target=node, defaults=defaults)

    base["target"]["peer_id"] = "peer-alpha"
    base["version"] = True
    with pytest.raises(ValueError, match="version"):
        parse_envelope(json.dumps(base), target=node, defaults=defaults)
    base["version"] = 1
    with pytest.raises(ValueError, match="deadline_seconds"):
        parse_envelope(json.dumps(base), target=node, defaults=defaults)
