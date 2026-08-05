"""Tests for pure default-deny Fleet policy enforcement."""

from __future__ import annotations

import pytest


def test_policy_defaults_to_deny_and_enforces_explicit_bounds() -> None:
    """No operation is permitted until an operator lists it for the peer."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    defaults = FleetDefaults(
        max_deadline_seconds=100, max_payload_bytes=1000, max_prompt_chars=100
    )
    allowed = NodePolicy(
        allowed_operations=("fleet.hermes.run",),
        max_deadline_seconds=20,
        max_payload_bytes=200,
        max_prompt_chars=20,
        max_export_paths=1,
    )
    enforce_request_policy(
        allowed,
        defaults=defaults,
        operation="fleet.hermes.run",
        deadline_seconds=20,
        payload_bytes=200,
        prompt_chars=20,
        export_path_count=1,
    )

    with pytest.raises(ValueError, match="not allowed"):
        enforce_request_policy(
            NodePolicy(),
            defaults=defaults,
            operation="fleet.hermes.run",
            deadline_seconds=1,
            payload_bytes=1,
            prompt_chars=1,
            export_path_count=0,
        )
    with pytest.raises(ValueError, match="deadline_seconds"):
        enforce_request_policy(
            allowed,
            defaults=defaults,
            operation="fleet.hermes.run",
            deadline_seconds=True,
            payload_bytes=1,
            prompt_chars=1,
            export_path_count=0,
        )


@pytest.mark.parametrize(
    ("field", "error_label"),
    (
        ("max_deadline_seconds", "deadline_seconds"),
        ("max_payload_bytes", "payload_bytes"),
        ("max_prompt_chars", "prompt_chars"),
        ("max_export_paths", "export_path_count"),
    ),
)
@pytest.mark.parametrize("lower_bound_source", ("global", "node"))
def test_policy_uses_minimum_of_global_and_node_bounds(
    field: str, error_label: str, lower_bound_source: str
) -> None:
    """Neither a permissive global nor node value can widen the tighter bound."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    defaults_values = {
        "max_deadline_seconds": 20,
        "max_payload_bytes": 20,
        "max_prompt_chars": 20,
        "max_export_paths": 20,
    }
    policy_values = dict(defaults_values)
    if lower_bound_source == "global":
        defaults_values[field] = 10
    else:
        policy_values[field] = 10
    request = {
        "deadline_seconds": 1,
        "payload_bytes": 1,
        "prompt_chars": 0,
        "export_path_count": 0,
    }
    request[error_label] = 11

    with pytest.raises(ValueError, match=error_label):
        enforce_request_policy(
            NodePolicy(allowed_operations=("fleet.hermes.run",), **policy_values),
            defaults=FleetDefaults(**defaults_values),
            operation="fleet.hermes.run",
            **request,
        )
