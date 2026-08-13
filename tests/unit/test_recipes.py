from __future__ import annotations

import json

import pytest

from hermes_fleet.recipes import (
    FleetRecipe,
    RecipeError,
    ResolvedAgencyProfile,
    ResolvedRecipe,
)


def recipe_document() -> dict:
    return {
        "schema": "fleet.recipe.v1",
        "agent": {
            "kind": "agency_profile",
            "name": "researcher",
            "version": ">=1,<2",
        },
        "environment": {
            "os": ["linux"],
            "architecture": ["x86_64", "aarch64"],
        },
        "resources": {"cpu_millis": 500, "memory_bytes": 536_870_912},
        "security": {"isolation": "process", "network": "restricted"},
        "extensions": {"example.org/feature": {"mode": "safe", "weight": 1}},
    }


def test_fleet_recipe_round_trips_and_hashes_canonically() -> None:
    recipe = FleetRecipe.from_dict(recipe_document())
    reordered = dict(reversed(list(recipe_document().items())))

    assert recipe.to_dict() == recipe_document()
    assert (
        FleetRecipe.from_json(json.dumps(reordered)).content_hash == recipe.content_hash
    )
    assert recipe.content_hash.startswith("sha256:")


def test_recipe_preserves_namespaced_extensions_without_interpreting_them() -> None:
    recipe = FleetRecipe.from_dict(recipe_document())

    assert recipe.extensions == {"example.org/feature": {"mode": "safe", "weight": 1}}
    assert recipe.to_dict()["extensions"] == recipe.extensions
    with pytest.raises(TypeError):
        recipe.extensions["example.org/new"] = {}  # type: ignore[index]


def test_recipe_rejects_backend_and_mutable_host_fields() -> None:
    for forbidden in ("backend", "docker", "node_id", "peer_id", "host_path"):
        document = recipe_document()
        document[forbidden] = "forbidden"
        with pytest.raises(RecipeError):
            FleetRecipe.from_dict(document)


def test_recipe_rejects_unnamespaced_or_non_json_extensions() -> None:
    document = recipe_document()
    document["extensions"] = {"feature": {"enabled": True}}
    with pytest.raises(RecipeError):
        FleetRecipe.from_dict(document)

    document["extensions"] = {"example.org/feature": {"value": float("nan")}}
    with pytest.raises(RecipeError):
        FleetRecipe.from_dict(document)


def test_resolved_recipe_binds_exact_agency_source_and_content() -> None:
    recipe = FleetRecipe.from_dict(recipe_document())
    resolved = ResolvedRecipe(
        recipe_hash=recipe.content_hash,
        agent=ResolvedAgencyProfile(
            repository="https://example.org/agency.git",
            revision="a" * 40,
            name="researcher",
            version="1.4.2",
            content_digest="sha256:" + "b" * 64,
        ),
        extensions={"example.org/resolver": {"catalog_version": 2}},
    )

    reparsed = ResolvedRecipe.from_json(resolved.to_json())

    assert reparsed == resolved
    assert reparsed.content_hash == resolved.content_hash
    assert reparsed.to_dict()["agent"]["revision"] == "a" * 40


def test_resolved_recipe_rejects_ranges_backend_choices_and_invalid_digests() -> None:
    recipe = FleetRecipe.from_dict(recipe_document())
    base = {
        "schema": "fleet.resolved-recipe.v1",
        "recipe_hash": recipe.content_hash,
        "agent": {
            "kind": "agency_profile",
            "repository": "https://example.org/agency.git",
            "revision": "a" * 40,
            "name": "researcher",
            "version": "1.4.2",
            "content_digest": "sha256:" + "b" * 64,
        },
        "extensions": {},
    }

    for field, value in (
        ("backend", "docker"),
        ("node_id", "node-a"),
        ("execution_plan", {}),
    ):
        document = {**base, field: value}
        with pytest.raises(RecipeError):
            ResolvedRecipe.from_dict(document)

    base["agent"]["version"] = ">=1"
    with pytest.raises(RecipeError):
        ResolvedRecipe.from_dict(base)


def test_contracts_are_bounded() -> None:
    document = recipe_document()
    document["extensions"] = {
        f"example.org/key-{index}": {"value": index} for index in range(65)
    }
    with pytest.raises(RecipeError):
        FleetRecipe.from_dict(document)

    with pytest.raises(RecipeError):
        FleetRecipe.from_json("{" + " " * 1_000_000 + "}")
