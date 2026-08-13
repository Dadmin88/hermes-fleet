from __future__ import annotations


def test_host_profile_capabilities_are_truthful_and_deterministic() -> None:
    from hermes_fleet.host_profile_capabilities import host_profile_capabilities

    first = host_profile_capabilities(
        logical_cpus=8,
        memory_bytes=16 * 1024**3,
        operating_system="linux",
        architecture="x86_64",
    )
    equal = host_profile_capabilities(
        logical_cpus=8,
        memory_bytes=16 * 1024**3,
        operating_system="linux",
        architecture="x86_64",
    )

    assert first == equal
    assert first.content_hash == equal.content_hash
    assert first.backend_kind == "fleet.dev/profile-runs"
    assert first.isolation == ("process",)
    assert first.network == ("provider",)
    assert first.ephemeral_root is False
    assert first.read_only_inputs is True
    assert first.agency_profile is True
    assert first.artifacts is True
    assert first.extensions == {
        "fleet.dev/profile-runs": {
            "resource_capacity_only": True,
            "resource_limits_enforced": False,
        }
    }


def test_host_profile_capabilities_reject_invalid_observations() -> None:
    import pytest

    from hermes_fleet.host_profile_capabilities import host_profile_capabilities

    for kwargs in (
        {"logical_cpus": 0, "memory_bytes": 1024},
        {"logical_cpus": 1, "memory_bytes": 0},
        {"logical_cpus": True, "memory_bytes": 1024},
    ):
        with pytest.raises(ValueError):
            host_profile_capabilities(
                operating_system="linux",
                architecture="x86_64",
                **kwargs,
            )
