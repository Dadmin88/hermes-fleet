"""Tests for atomic, owner-safe Fleet inventory state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest


def test_initialize_inventory_preserves_a_valid_empty_cache_without_rewriting(
    tmp_path,
) -> None:
    """Idempotent init leaves a valid empty cache untouched."""
    from hermes_fleet.inventory import initialize_inventory_state

    state_dir = tmp_path / "fleet"
    initialize_inventory_state(state_dir)
    cache_path = state_dir / "cache.json"
    os.utime(cache_path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    expected_mtime = cache_path.stat().st_mtime_ns

    initialize_inventory_state(state_dir)

    assert cache_path.read_text(encoding="utf-8") == "{}\n"
    assert cache_path.stat().st_mtime_ns == expected_mtime


def test_inventory_write_refuses_a_symlink_target(tmp_path) -> None:
    """Atomic inventory writes never follow an operator-state symlink."""
    from hermes_fleet.inventory import write_yaml_atomic

    target = tmp_path / "nodes.yaml"
    target.symlink_to(tmp_path / "elsewhere.yaml")

    with pytest.raises(ValueError, match="regular file"):
        write_yaml_atomic(target, {"schema_version": 1})


@pytest.mark.parametrize("cache_contents", ("{broken", "[]\n"))
def test_initialize_inventory_replaces_invalid_cache_without_touching_nodes(
    tmp_path, cache_contents: str
) -> None:
    """Recover invalid cache while preserving operator inventory."""
    from hermes_fleet.inventory import initialize_inventory_state

    state_dir = tmp_path / "fleet"
    state_dir.mkdir(mode=0o700)
    nodes_path = state_dir / "nodes.yaml"
    cache_path = state_dir / "cache.json"
    nodes_contents = "schema_version: 1\ndefaults: {}\nnodes: []\n"
    nodes_path.write_text(nodes_contents, encoding="utf-8")
    cache_path.write_text(cache_contents, encoding="utf-8")

    initialize_inventory_state(state_dir)

    assert nodes_path.read_text(encoding="utf-8") == nodes_contents
    assert cache_path.read_text(encoding="utf-8") == "{}\n"


@pytest.mark.parametrize("cache_contents", ("{broken", "[]\n", "null\n"))
def test_load_cache_recovers_invalid_or_non_mapping_json(
    tmp_path, cache_contents: str
) -> None:
    """The public cache loader exposes recoverable corruption as an empty mapping."""
    from hermes_fleet.inventory import load_cache

    state_dir = tmp_path / "fleet"
    state_dir.mkdir(mode=0o700)
    cache_path = state_dir / "cache.json"
    cache_path.write_text(cache_contents, encoding="utf-8")

    assert load_cache(cache_path) == {}


def _untrusted_path(tmp_path, behavior: str) -> tuple[Any, Any]:
    if behavior == "wrong-type":
        return object(), None

    class ProbePath(type(Path())):
        touched = False

        def __getattribute__(self, name: str):
            if name in {"is_absolute", "parts", "parent", "name"}:
                type(self).touched = True
                if behavior == "hostile-subclass":
                    raise RuntimeError("path hook ran")
            return super().__getattribute__(name)

    return ProbePath(tmp_path / "state.json"), ProbePath


@pytest.mark.parametrize(
    ("operation", "error_message"),
    (
        ("initialize", "state directory path must be a Path"),
        ("write-json", "state file path must be a Path"),
        ("write-yaml", "state file path must be a Path"),
        ("load-cache", None),
    ),
)
@pytest.mark.parametrize(
    "behavior", ("wrong-type", "benign-subclass", "hostile-subclass")
)
def test_inventory_public_paths_reject_untrusted_runtime_types(
    tmp_path, operation: str, error_message: str | None, behavior: str
) -> None:
    """State APIs reject path hooks while cache loading remains recoverable."""
    from hermes_fleet import inventory

    path, probe_type = _untrusted_path(tmp_path, behavior)

    if operation == "load-cache":
        assert inventory.load_cache(path) == {}
    else:
        with pytest.raises(ValueError) as error:
            if operation == "initialize":
                inventory.initialize_inventory_state(path)
            elif operation == "write-json":
                inventory.write_json_atomic(path, {"safe": True})
            else:
                inventory.write_yaml_atomic(path, {"safe": True})
        assert type(error.value) is ValueError
        assert str(error.value) == error_message

    if probe_type is not None:
        assert probe_type.touched is False


def test_atomic_json_write_accepts_concrete_path(tmp_path) -> None:
    """The exact platform Path remains valid after the public type gate."""
    from hermes_fleet.inventory import load_cache, write_json_atomic

    target = tmp_path / "cache.json"
    write_json_atomic(target, {"safe": True})

    assert load_cache(target) == {"safe": True}
