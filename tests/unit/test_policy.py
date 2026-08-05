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
