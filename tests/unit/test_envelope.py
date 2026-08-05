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


def test_envelope_rejects_oversize_bytes_before_json_parsing() -> None:
    """The byte ceiling wins even when an oversized payload is malformed JSON."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    with pytest.raises(ValueError, match="size limit"):
        parse_envelope(
            "not-json-at-all",
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(max_payload_bytes=8),
        )


@pytest.mark.parametrize(
    "mutation", ("extra-top-level", "missing-target", "extra-limit")
)
def test_envelope_requires_exact_top_level_and_limits_shapes(mutation: str) -> None:
    """Envelope and limits objects reject missing or additional members."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    document = {
        "version": 1,
        "operation": "fleet.health",
        "target": {"name": "alpha", "peer_id": "peer-alpha"},
        "input": {},
        "limits": {"deadline_seconds": 1},
    }
    if mutation == "extra-top-level":
        document["extra"] = True
    elif mutation == "missing-target":
        del document["target"]
    else:
        document["limits"]["extra"] = True

    with pytest.raises(ValueError, match="shape"):
        parse_envelope(
            json.dumps(document),
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )


@pytest.mark.parametrize("operation", ("fleet.health", "fleet.inventory"))
def test_health_and_inventory_envelopes_reject_nonempty_input(operation: str) -> None:
    """Read-only operations accept no caller-controlled input fields."""
    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    document = {
        "version": 1,
        "operation": operation,
        "target": {"name": "alpha", "peer_id": "peer-alpha"},
        "input": {"unexpected": True},
        "limits": {"deadline_seconds": 1},
    }

    with pytest.raises(ValueError, match="input must be empty"):
        parse_envelope(
            json.dumps(document),
            target=NodeConfig(name="alpha", peer_id="peer-alpha"),
            defaults=FleetDefaults(),
        )
