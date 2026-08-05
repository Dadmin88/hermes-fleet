"""Tests for atomic, owner-safe Fleet inventory state."""

from __future__ import annotations

import os

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
