"""Behavior tests for Fleet configuration and profile-scoped state paths."""

from __future__ import annotations

import pytest


def test_config_uses_hermes_home_or_an_explicit_standalone_override(
    tmp_path, monkeypatch
) -> None:
    """Fleet state is always beneath active HERMES_HOME unless a test overrides it."""
    from hermes_fleet.config import get_fleet_dir

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "active-profile"))

    assert get_fleet_dir() == tmp_path / "active-profile" / "fleet"
    assert (
        get_fleet_dir(standalone_home=tmp_path / "standalone")
        == tmp_path / "standalone" / "fleet"
    )


def test_config_rejects_duplicate_names_peer_ids_and_non_integer_defaults(
    tmp_path,
) -> None:
    """Inventory config is strict and never silently merges peer identities."""
    from hermes_fleet.config import load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        """schema_version: 1
defaults:
  max_deadline_seconds: 60
  max_payload_bytes: 1000
  max_export_paths: 2
nodes:
  - name: alpha
    peer_id: peer-alpha
  - name: beta
    peer_id: peer-alpha
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="peer IDs"):
        load_fleet_config(path)

    path.write_text(
        """schema_version: 1
defaults:
  max_deadline_seconds: \"60\"
nodes: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="max_deadline_seconds"):
        load_fleet_config(path)


def test_config_rejects_duplicate_normalized_names(tmp_path) -> None:
    """Friendly names remain unique after case normalization."""
    from hermes_fleet.config import load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        """schema_version: 1
defaults: {}
nodes:
  - name: Alpha
    peer_id: peer-alpha
  - name: alpha
    peer_id: peer-beta
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="node names"):
        load_fleet_config(path)


@pytest.mark.parametrize(
    "field",
    (
        "max_deadline_seconds",
        "max_payload_bytes",
        "max_prompt_chars",
        "max_export_paths",
    ),
)
def test_config_rejects_boolean_default_bounds(tmp_path, field: str) -> None:
    """YAML booleans cannot masquerade as integer resource bounds."""
    from hermes_fleet.config import load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        f"schema_version: 1\ndefaults:\n  {field}: true\nnodes: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field):
        load_fleet_config(path)
