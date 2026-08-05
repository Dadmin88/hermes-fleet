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


def test_policy_denial_does_not_reflect_untrusted_operation_text() -> None:
    """Authorization failures cannot inject caller text into errors or logs."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    operation = "TOP-SECRET\x1b[2J\nFORGED"
    with pytest.raises(ValueError) as error:
        enforce_request_policy(
            NodePolicy(),
            defaults=FleetDefaults(),
            operation=operation,
            deadline_seconds=1,
            payload_bytes=1,
            prompt_chars=0,
            export_path_count=0,
        )

    assert str(error.value) == "operation is not allowed"
    assert operation not in str(error.value)


def test_policy_validates_malformed_metrics_before_authorization() -> None:
    """A denied operation cannot hide a structurally invalid request metric."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    with pytest.raises(ValueError, match="deadline_seconds"):
        enforce_request_policy(
            NodePolicy(allowed_operations=("fleet.health",)),
            defaults=FleetDefaults(),
            operation="fleet.inventory",
            deadline_seconds=[],
            payload_bytes=1,
            prompt_chars=0,
            export_path_count=0,
        )


@pytest.mark.parametrize(
    ("policy", "defaults", "error_label"),
    (
        ({}, None, "policy"),
        (None, {}, "defaults"),
    ),
)
def test_policy_rejects_invalid_domain_collaborators(
    policy, defaults, error_label: str
) -> None:
    """Wrong collaborator types cannot escape as attribute errors."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    actual_policy = policy if policy is not None else NodePolicy()
    actual_defaults = defaults if defaults is not None else FleetDefaults()
    with pytest.raises(ValueError, match=error_label):
        enforce_request_policy(
            actual_policy,
            defaults=actual_defaults,
            operation="fleet.health",
            deadline_seconds=1,
            payload_bytes=1,
            prompt_chars=0,
            export_path_count=0,
        )


@pytest.mark.parametrize("field", ("policy", "defaults"))
@pytest.mark.parametrize("behavior", ("plain-subclass", "hostile-subclass"))
def test_policy_rejects_domain_collaborator_subclasses(
    field: str, behavior: str
) -> None:
    """Domain subclasses cannot carry hooks across authorization boundaries."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    class PolicySubclass(NodePolicy):
        armed = False

        def __getattribute__(self, name):
            if type(self).armed and name == "allowed_operations":
                raise RuntimeError("policy hook ran")
            return object.__getattribute__(self, name)

    class DefaultsSubclass(FleetDefaults):
        armed = False

        def __getattribute__(self, name):
            if type(self).armed and name == "max_deadline_seconds":
                raise RuntimeError("defaults hook ran")
            return object.__getattribute__(self, name)

    policy = PolicySubclass(allowed_operations=("fleet.health",))
    defaults = DefaultsSubclass()
    if behavior == "hostile-subclass":
        PolicySubclass.armed = True
        DefaultsSubclass.armed = True

    with pytest.raises(ValueError, match=f"{field} must be"):
        enforce_request_policy(
            policy
            if field == "policy"
            else NodePolicy(allowed_operations=("fleet.health",)),
            defaults=defaults if field == "defaults" else FleetDefaults(),
            operation="fleet.health",
            deadline_seconds=1,
            payload_bytes=1,
            prompt_chars=0,
            export_path_count=0,
        )


@pytest.mark.parametrize("behavior", ("always-equal", "explosive"))
def test_policy_requires_exact_primitive_operation_strings(behavior: str) -> None:
    """Hostile string subclasses cannot bypass or crash authorization."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    if behavior == "always-equal":
        Operation = type(
            "AlwaysAllowed",
            (str,),
            {"__eq__": lambda self, other: True, "__hash__": str.__hash__},
        )
    else:

        def explode(self, other):
            raise RuntimeError("comparison hook ran")

        Operation = type(
            "ExplosiveOperation",
            (str,),
            {"__eq__": explode, "__hash__": str.__hash__},
        )

    with pytest.raises(ValueError, match="operation must be a string"):
        enforce_request_policy(
            NodePolicy(allowed_operations=("fleet.health",)),
            defaults=FleetDefaults(),
            operation=Operation("fleet.unlisted.operation"),
            deadline_seconds=1,
            payload_bytes=1,
            prompt_chars=0,
            export_path_count=0,
        )


@pytest.mark.parametrize(
    "field",
    ("deadline_seconds", "payload_bytes", "prompt_chars", "export_path_count"),
)
def test_policy_requires_exact_primitive_request_metrics(field: str) -> None:
    """Numeric subclasses cannot invoke comparison hooks during validation."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    def explode(self, other):
        raise RuntimeError("numeric comparison hook ran")

    Metric = type(
        "Metric",
        (int,),
        {"__le__": explode, "__lt__": explode, "__gt__": explode},
    )
    request = {
        "deadline_seconds": 1,
        "payload_bytes": 1,
        "prompt_chars": 0,
        "export_path_count": 0,
    }
    request[field] = Metric(1)

    with pytest.raises(ValueError, match=field):
        enforce_request_policy(
            NodePolicy(allowed_operations=("fleet.health",)),
            defaults=FleetDefaults(),
            operation="fleet.health",
            **request,
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
