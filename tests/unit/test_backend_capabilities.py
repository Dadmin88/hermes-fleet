from __future__ import annotations

import pytest

from hermes_fleet.backend_capabilities import (
    BackendCapabilities,
    CapabilityError,
    CapabilityMatch,
    evaluate_capabilities,
)
from hermes_fleet.recipes import FleetRecipe


def recipe(*, isolation: str = "process", network: str = "restricted") -> FleetRecipe:
    return FleetRecipe.from_dict(
        {
            "schema": "fleet.recipe.v1",
            "agent": {
                "kind": "agency_profile",
                "name": "researcher",
                "version": ">=1,<2",
            },
            "environment": {"os": ["linux"], "architecture": ["x86_64"]},
            "resources": {"cpu_millis": 500, "memory_bytes": 536_870_912},
            "security": {"isolation": isolation, "network": network},
            "extensions": {},
        }
    )


def capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "example.org/runtime",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["none", "process"],
            "network": ["none", "restricted"],
            "resources": {"cpu_millis": 2000, "memory_bytes": 2_147_483_648},
            "filesystem": {"ephemeral_root": True, "read_only_inputs": True},
            "materialization": {"agency_profile": True, "artifacts": True},
            "extensions": {"example.org/runtime": {"version": 1}},
        }
    )


def test_capabilities_round_trip_without_backend_specific_config() -> None:
    value = capabilities()

    assert BackendCapabilities.from_json(value.to_json()) == value
    assert value.content_hash.startswith("sha256:")
    assert "docker" not in value.to_json().lower()


def test_matching_accepts_satisfied_recipe_requirements() -> None:
    match = evaluate_capabilities(recipe(), capabilities())

    assert match == CapabilityMatch(eligible=True, reasons=())


def test_matching_reports_every_hard_incompatibility_deterministically() -> None:
    candidate = BackendCapabilities.from_dict(
        {
            **capabilities().to_dict(),
            "platform": {"os": "android", "architecture": "aarch64"},
            "isolation": ["none"],
            "network": ["none"],
            "resources": {"cpu_millis": 100, "memory_bytes": 1024},
            "materialization": {"agency_profile": False, "artifacts": True},
        }
    )

    match = evaluate_capabilities(recipe(), candidate)

    assert match.eligible is False
    assert match.reasons == (
        "architecture_unsupported",
        "cpu_insufficient",
        "isolation_unsupported",
        "memory_insufficient",
        "network_unsupported",
        "os_unsupported",
        "profile_materialization_unsupported",
    )


def test_weaker_backend_cannot_satisfy_stronger_isolation() -> None:
    match = evaluate_capabilities(recipe(isolation="container"), capabilities())

    assert match.eligible is False
    assert "isolation_unsupported" in match.reasons


def test_contract_rejects_backend_configuration_and_unbounded_extensions() -> None:
    document = capabilities().to_dict()
    document["command"] = ["docker", "run"]
    with pytest.raises(CapabilityError):
        BackendCapabilities.from_dict(document)

    document = capabilities().to_dict()
    document["extensions"] = {"feature": {"enabled": True}}
    with pytest.raises(CapabilityError):
        BackendCapabilities.from_dict(document)


def test_matching_does_not_select_or_rank_a_node() -> None:
    match = evaluate_capabilities(recipe(), capabilities())

    assert set(match.__dataclass_fields__) == {"eligible", "reasons"}


def test_direct_construction_normalizes_mutable_guarantee_lists() -> None:
    isolation = ["process"]
    network = ["restricted"]
    value = BackendCapabilities(
        backend_kind="example.org/runtime",
        os="linux",
        architecture="x86_64",
        isolation=isolation,  # type: ignore[arg-type]
        network=network,  # type: ignore[arg-type]
        cpu_millis=1000,
        memory_bytes=1024,
        ephemeral_root=True,
        read_only_inputs=True,
        agency_profile=True,
        artifacts=True,
        extensions={},
    )

    isolation.append("container")
    network.append("none")

    assert value.isolation == ("process",)
    assert value.network == ("restricted",)
