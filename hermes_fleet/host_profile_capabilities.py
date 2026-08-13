"""Truthful capabilities for the destination host-profile Runs backend."""

from __future__ import annotations

from .backend_capabilities import BackendCapabilities


def host_profile_capabilities(
    *,
    logical_cpus: int,
    memory_bytes: int,
    operating_system: str,
    architecture: str,
) -> BackendCapabilities:
    """Describe host eligibility without claiming unenforced resource quotas."""
    if type(logical_cpus) is not int or not 0 < logical_cpus <= 65_535:
        raise ValueError("logical CPU observation is invalid")
    if type(memory_bytes) is not int or not 0 < memory_bytes <= (1 << 63) - 1:
        raise ValueError("memory observation is invalid")
    if type(operating_system) is not str or not operating_system:
        raise ValueError("operating system observation is invalid")
    if type(architecture) is not str or not architecture:
        raise ValueError("architecture observation is invalid")
    return BackendCapabilities(
        backend_kind="fleet.dev/profile-runs",
        os=operating_system,
        architecture=architecture,
        isolation=("process",),
        network=("provider",),
        cpu_millis=logical_cpus * 1_000,
        memory_bytes=memory_bytes,
        ephemeral_root=False,
        read_only_inputs=True,
        agency_profile=True,
        artifacts=True,
        extensions={
            "fleet.dev/profile-runs": {
                "resource_capacity_only": True,
                "resource_limits_enforced": False,
            }
        },
    )
