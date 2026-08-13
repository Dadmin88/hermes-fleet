"""Behavior tests for Fleet configuration and profile-scoped state paths."""

from __future__ import annotations

from typing import Any, cast

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


def test_config_path_boundaries_reject_wrong_runtime_types() -> None:
    """Public path inputs fail as Fleet configuration errors, not attribute errors."""
    from hermes_fleet.config import FleetConfigError, get_fleet_dir, load_fleet_config

    with pytest.raises(FleetConfigError, match="path"):
        load_fleet_config(cast(Any, "nodes.yaml"))
    with pytest.raises(FleetConfigError, match="state root"):
        get_fleet_dir(standalone_home=cast(Any, "/tmp/fleet"))


@pytest.mark.parametrize("boundary", ("load", "state-root"))
def test_config_path_boundaries_reject_path_subclasses(tmp_path, boundary: str) -> None:
    """Path subclasses cannot invoke filesystem or normalization hooks."""
    from hermes_fleet.config import FleetConfigError, get_fleet_dir, load_fleet_config

    class HostilePath(type(tmp_path)):
        def read_text(self, *args, **kwargs):
            raise RuntimeError("read hook ran")

        def is_absolute(self):
            raise RuntimeError("path hook ran")

    path = HostilePath(tmp_path / "nodes.yaml")
    with pytest.raises(
        FleetConfigError, match="path" if boundary == "load" else "state root"
    ):
        if boundary == "load":
            load_fleet_config(path)
        else:
            get_fleet_dir(standalone_home=path)


def test_config_schema_failures_use_fleet_config_error(tmp_path) -> None:
    """Schema validation uses one Fleet-owned ValueError subtype."""
    from hermes_fleet.config import FleetConfigError, load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text("schema_version: true\ndefaults: {}\nnodes: []\n", encoding="utf-8")
    with pytest.raises(FleetConfigError, match="schema_version") as error:
        load_fleet_config(path)
    assert type(error.value) is FleetConfigError


def test_config_converts_oversized_integer_parser_error(tmp_path) -> None:
    """Python's YAML integer digit limit cannot leak parser-specific text."""
    from hermes_fleet.config import FleetConfigError, load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        "schema_version: " + "9" * 5_000 + "\ndefaults: {}\nnodes: []\n",
        encoding="utf-8",
    )
    with pytest.raises(FleetConfigError) as error:
        load_fleet_config(path)
    assert str(error.value) == "schema_version 1 configuration file is required"


def test_config_converts_parser_recursion_to_fleet_error(tmp_path) -> None:
    """Deep YAML cannot leak a parser recursion exception."""
    from hermes_fleet.config import FleetConfigError, load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text("{a:" * 10_000 + "1" + "}" * 10_000, encoding="utf-8")
    with pytest.raises(FleetConfigError) as error:
        load_fleet_config(path)
    assert str(error.value) == "schema_version 1 configuration file is required"


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


def test_schema_v2_keys_explicit_policy_by_managed_identity_without_peer_ids(
    tmp_path,
) -> None:
    from hermes_fleet.config import load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        """schema_version: 2
defaults: {}
nodes: []
managed_targets:
  - source: nodescale
    network_id: network-test
    device_id: device-a
    target_name: worker
    policy:
      allowed_operations:
        - fleet.hermes.run
""",
        encoding="utf-8",
    )

    config = load_fleet_config(path)
    assert config.schema_version == 2
    assert config.nodes == ()
    assert config.managed_targets[0].device_id == "device-a"
    assert config.managed_targets[0].target_name == "worker"
    assert config.managed_targets[0].policy.allowed_operations == ("fleet.hermes.run",)


def test_schema_v1_rejects_managed_targets_and_schema_v2_rejects_duplicate_identity(
    tmp_path,
) -> None:
    from hermes_fleet.config import FleetConfigError, load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        "schema_version: 1\ndefaults: {}\nnodes: []\nmanaged_targets: []\n",
        encoding="utf-8",
    )
    with pytest.raises(FleetConfigError, match="unknown configuration keys"):
        load_fleet_config(path)

    path.write_text(
        """schema_version: 2
defaults: {}
nodes: []
managed_targets:
  - source: nodescale
    network_id: network-test
    device_id: device-a
    target_name: worker
    policy: {}
  - source: nodescale
    network_id: network-test
    device_id: device-a
    target_name: worker
    policy: {}
""",
        encoding="utf-8",
    )
    with pytest.raises(FleetConfigError, match="identities must be unique"):
        load_fleet_config(path)


def test_schema_v2_rejects_target_name_that_exact_node_envelopes_cannot_use(
    tmp_path,
) -> None:
    from hermes_fleet.config import FleetConfigError, load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        """schema_version: 2
defaults: {}
nodes: []
managed_targets:
  - source: nodescale
    network_id: network-test
    device_id: device-a
    target_name: Not Valid
    policy: {}
""",
        encoding="utf-8",
    )
    with pytest.raises(FleetConfigError, match="target_name"):
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
