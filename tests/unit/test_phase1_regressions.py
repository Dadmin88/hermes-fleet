"""Regression coverage for Phase 1 semantic and local-state safety boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_config_rejects_boolean_schema_version_and_malformed_construction(
    tmp_path,
) -> None:
    """YAML booleans and malformed inputs use the public ValueError API."""
    from hermes_fleet.config import load_fleet_config
    from hermes_fleet.models import NodeConfig

    with pytest.raises(ValueError, match="name"):
        NodeConfig()

    path = tmp_path / "nodes.yaml"
    path.write_text("schema_version: true\ndefaults: {}\nnodes: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_fleet_config(path)

    path.write_text(
        "schema_version: 1\ndefaults: []\nnodes: [not-a-mapping]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_fleet_config(path)

    path.write_text("schema_version: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_fleet_config(path)

    path.write_text(
        """schema_version: 1
extra: true
1: true
defaults: {}
nodes: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown configuration"):
        load_fleet_config(path)


def test_config_rejects_duplicate_top_level_yaml_keys(tmp_path) -> None:
    """A duplicate document key is a configuration error rather than last-key wins."""
    from hermes_fleet.config import FleetConfigError, load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(
        "schema_version: 1\nschema_version: 1\ndefaults: {}\nnodes: []\n",
        encoding="utf-8",
    )

    with pytest.raises(
        FleetConfigError, match="duplicate inventory key 'schema_version'"
    ) as error:
        load_fleet_config(path)
    assert type(error.value) is FleetConfigError


@pytest.mark.parametrize(
    "contents",
    (
        """schema_version: 1
defaults:
  max_prompt_chars: 10
  max_prompt_chars: 20
nodes: []
""",
        """schema_version: 1
defaults: {}
nodes:
  - name: alpha
    peer_id: peer-alpha
    policy:
      max_payload_bytes: 10
      max_payload_bytes: 20
""",
    ),
)
def test_config_rejects_duplicate_nested_yaml_keys(tmp_path, contents: str) -> None:
    """Duplicate defaults and policy keys are rejected at their mapping depth."""
    from hermes_fleet.config import FleetConfigError, load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(FleetConfigError, match="duplicate inventory key") as error:
        load_fleet_config(path)
    assert type(error.value) is FleetConfigError


def test_node_config_rejects_url_like_peer_id_but_preserves_opaque_token() -> None:
    """Peer IDs stay opaque but cannot be endpoint URLs."""
    from hermes_fleet.models import NodeConfig

    assert NodeConfig(name="alpha", peer_id="token-value").peer_id == "token-value"
    with pytest.raises(ValueError, match="peer_id"):
        NodeConfig(name="alpha", peer_id="https://relay.example/path")


def test_envelope_rejects_unhashable_operations_and_strict_invalid_input() -> None:
    """Malformed JSON envelope members consistently surface as ValueError."""
    import json

    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    defaults = FleetDefaults(max_export_paths=1)
    target = NodeConfig(name="alpha", peer_id="peer-alpha")
    valid = {
        "version": 1,
        "operation": "fleet.hermes.run",
        "target": {"name": "alpha", "peer_id": "peer-alpha"},
        "input": {"prompt": "do work", "export_paths": []},
        "limits": {"deadline_seconds": 1},
    }
    invalid_values = (
        ("operation", ["fleet.health"]),
        ("operation", {"name": "fleet.health"}),
        ("operation", "fleet.unknown"),
        ("input", []),
        ("limits", []),
        ("input", {"prompt": "   ", "export_paths": []}),
        ("input", {"prompt": "ok", "export_paths": ["../unsafe"]}),
        ("input", {"prompt": "ok", "export_paths": ["one.txt", "one.txt"]}),
        ("input", {"prompt": "ok", "export_paths": ["x" * 257]}),
    )

    for key, value in invalid_values:
        document = dict(valid)
        document[key] = value
        with pytest.raises(ValueError):
            parse_envelope(json.dumps(document), target=target, defaults=defaults)


def test_envelope_rejects_lone_unicode_surrogate_as_value_error() -> None:
    """Invalid UTF-8 payload text cannot escape the public validation API."""
    import json

    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    target = NodeConfig(name="alpha", peer_id="peer-alpha")
    document = {
        "version": 1,
        "operation": "fleet.hermes.run",
        "target": {"name": "alpha", "peer_id": "peer-alpha"},
        "input": {"prompt": "do work", "export_paths": []},
        "limits": {"deadline_seconds": 1},
    }
    payload = json.dumps(document).replace("do work", "\ud800")

    with pytest.raises(ValueError) as raised:
        parse_envelope(payload, target=target, defaults=FleetDefaults())
    assert type(raised.value) is ValueError
    assert str(raised.value) == "payload must be valid UTF-8"


def test_selection_materializes_generator_requests_once() -> None:
    """Generator name requests survive validation instead of being consumed."""
    from hermes_fleet.models import NodeConfig
    from hermes_fleet.selection import select_nodes

    nodes = (
        NodeConfig(name="alpha", peer_id="peer-alpha"),
        NodeConfig(name="beta", peer_id="peer-beta"),
    )

    selected = select_nodes(nodes, names=(name for name in ("alpha",)))

    assert [node.name for node in selected] == ["alpha"]


def test_write_yaml_atomic_rejects_symlink_parent_directory(tmp_path) -> None:
    """Direct atomic writes never follow a parent-directory symlink."""
    from hermes_fleet.inventory import write_yaml_atomic

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="state directory"):
        write_yaml_atomic(linked_parent / "nodes.yaml", {"safe": True})

    assert not (outside / "nodes.yaml").exists()


def test_write_yaml_atomic_stays_bound_to_open_directory_after_rename(
    tmp_path, monkeypatch
) -> None:
    """Replacing after a directory-path swap cannot redirect state outside its FD."""
    import hermes_fleet.inventory as inventory

    state_dir = tmp_path / "fleet"
    state_dir.mkdir()
    moved_state_dir = tmp_path / "fleet-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_replace = inventory.os.replace
    swapped = False

    def replace_after_parent_swap(source, destination, **kwargs) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            state_dir.rename(moved_state_dir)
            state_dir.symlink_to(outside, target_is_directory=True)
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(inventory.os, "replace", replace_after_parent_swap)

    inventory.write_yaml_atomic(state_dir / "nodes.yaml", {"safe": True})

    assert (moved_state_dir / "nodes.yaml").read_text(
        encoding="utf-8"
    ) == "safe: true\n"
    assert not (outside / "nodes.yaml").exists()


def test_state_initialization_rejects_unsafe_directory_and_existing_targets(
    tmp_path, monkeypatch
) -> None:
    """Init refuses unsafe state before it can preserve or write it."""
    import hermes_fleet.inventory as inventory

    outside = tmp_path / "outside"
    outside.mkdir()
    linked_state = tmp_path / "linked-state"
    linked_state.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="state directory"):
        inventory.initialize_inventory_state(linked_state)

    state_dir = tmp_path / "fleet"
    state_dir.mkdir()
    (state_dir / "nodes.yaml").symlink_to(tmp_path / "outside-nodes.yaml")
    with pytest.raises(ValueError, match="regular file"):
        inventory.initialize_inventory_state(state_dir)

    (state_dir / "nodes.yaml").unlink()
    (state_dir / "nodes.yaml").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        inventory.initialize_inventory_state(state_dir)

    (state_dir / "nodes.yaml").rmdir()
    (state_dir / "cache.json").symlink_to(tmp_path / "outside-cache.json")
    with pytest.raises(ValueError, match="regular file"):
        inventory.initialize_inventory_state(state_dir)

    (state_dir / "cache.json").unlink()
    current_uid = os.getuid()
    with pytest.raises(ValueError, match="state directory"):
        monkeypatch.setattr(inventory.os, "getuid", lambda: current_uid + 1)
        inventory.initialize_inventory_state(state_dir)


def test_state_initialization_tightens_existing_valid_file_modes_without_rewrite(
    tmp_path,
) -> None:
    """Keep valid state byte-for-byte intact while tightening permissions."""
    from hermes_fleet.inventory import initialize_inventory_state

    state_dir = tmp_path / "fleet"
    state_dir.mkdir(mode=0o700)
    inventory_path = state_dir / "nodes.yaml"
    cache_path = state_dir / "cache.json"
    inventory_contents = "schema_version: 1\ndefaults: {}\nnodes: []\n"
    cache_contents = "{}\n"
    inventory_path.write_text(inventory_contents, encoding="utf-8")
    cache_path.write_text(cache_contents, encoding="utf-8")
    inventory_path.chmod(0o644)
    cache_path.chmod(0o644)
    cache_mtime_ns = cache_path.stat().st_mtime_ns

    initialize_inventory_state(state_dir)

    assert inventory_path.read_text(encoding="utf-8") == inventory_contents
    assert cache_path.read_text(encoding="utf-8") == cache_contents
    assert inventory_path.stat().st_mode & 0o777 == 0o600
    assert cache_path.stat().st_mode & 0o777 == 0o600
    assert cache_path.stat().st_mtime_ns == cache_mtime_ns
    assert state_dir.stat().st_mode & 0o777 == 0o700


def test_remote_output_cannot_disable_the_untrusted_marker() -> None:
    """Remote output is structurally untrusted rather than caller-configurable."""
    from hermes_fleet.models import RemoteOutput

    assert RemoteOutput("remote").untrusted is True
    with pytest.raises(TypeError):
        RemoteOutput("remote", untrusted=False)


def test_policy_allows_zero_prompt_characters_for_non_run_operations() -> None:
    """Health/inventory use empty input; run validates a nonempty prompt."""
    from hermes_fleet.models import FleetDefaults, NodePolicy
    from hermes_fleet.policy import enforce_request_policy

    enforce_request_policy(
        NodePolicy(allowed_operations=("fleet.health",)),
        defaults=FleetDefaults(),
        operation="fleet.health",
        deadline_seconds=1,
        payload_bytes=1,
        prompt_chars=0,
        export_path_count=0,
    )


def test_initialize_inventory_state_preserves_existing_ancestor_mode(tmp_path) -> None:
    """Only new state components and the Fleet directory receive restrictive modes."""
    from hermes_fleet.inventory import initialize_inventory_state

    ancestor = tmp_path / "ancestor"
    ancestor.mkdir(mode=0o755)
    ancestor.chmod(0o755)
    state_dir = ancestor / "missing-home" / "fleet"

    initialize_inventory_state(state_dir)

    assert ancestor.stat().st_mode & 0o777 == 0o755
    assert (ancestor / "missing-home").stat().st_mode & 0o777 == 0o700
    assert state_dir.stat().st_mode & 0o777 == 0o700
    assert (state_dir / "nodes.yaml").stat().st_mode & 0o777 == 0o600
    assert (state_dir / "cache.json").stat().st_mode & 0o777 == 0o600


def test_relative_state_roots_are_rejected_without_cwd_side_effects(
    tmp_path, monkeypatch
) -> None:
    """All public roots reject relative paths before creating anything in CWD."""
    from hermes_fleet.config import get_fleet_dir, get_hermes_home
    from hermes_fleet.inventory import initialize_inventory_state

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", "relative-home")
    with pytest.raises(ValueError, match="absolute"):
        get_hermes_home()
    with pytest.raises(ValueError, match="absolute"):
        get_fleet_dir()
    with pytest.raises(ValueError, match="absolute"):
        get_fleet_dir(standalone_home=Path("relative-standalone"))
    with pytest.raises(ValueError, match="absolute"):
        initialize_inventory_state(Path("relative/fleet"))

    assert not (tmp_path / "relative-home").exists()
    assert not (tmp_path / "relative-standalone").exists()
    assert not (tmp_path / "relative").exists()


def test_state_paths_with_explicit_parent_traversal_are_rejected(tmp_path) -> None:
    """State roots cannot retain explicit parent traversal components."""
    from hermes_fleet.config import get_fleet_dir
    from hermes_fleet.inventory import initialize_inventory_state

    traversal_home = tmp_path / "safe" / ".." / "outside"
    with pytest.raises(ValueError, match="parent traversal"):
        get_fleet_dir(standalone_home=traversal_home)
    with pytest.raises(ValueError, match="parent traversal"):
        initialize_inventory_state(tmp_path / "safe" / ".." / "fleet")

    assert not (tmp_path / "safe").exists()


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(), reason="requires procfs FD accounting"
)
@pytest.mark.parametrize("missing_ancestor", (True, False))
def test_state_directory_mkdir_failures_are_normalized_without_fd_leaks(
    tmp_path, monkeypatch, missing_ancestor: bool
) -> None:
    """Both creation sites close retained directory FDs when mkdir is denied."""
    import hermes_fleet.inventory as inventory

    parent = tmp_path / "parent"
    parent.mkdir()
    state_dir = (
        parent / "missing-ancestor" / "fleet" if missing_ancestor else parent / "fleet"
    )
    refused_name = "missing-ancestor" if missing_ancestor else "fleet"
    original_mkdir = inventory.os.mkdir

    def denied_mkdir(name, mode=0o777, *, dir_fd=None) -> None:
        if name == refused_name:
            raise PermissionError("denied")
        original_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(inventory.os, "mkdir", denied_mkdir)
    before = len(list(Path("/proc/self/fd").iterdir()))
    for _ in range(8):
        with pytest.raises(
            ValueError, match="state directory must be an owner-owned directory"
        ):
            inventory.initialize_inventory_state(state_dir)
    after = len(list(Path("/proc/self/fd").iterdir()))

    assert after == before


def test_load_fleet_config_normalizes_non_utf8_bytes_to_public_value_error(
    tmp_path,
) -> None:
    """Undecodable inventory bytes cannot leak UnicodeDecodeError from config."""
    from hermes_fleet.config import load_fleet_config

    path = tmp_path / "nodes.yaml"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(
        ValueError, match="schema_version 1 configuration file is required"
    ):
        load_fleet_config(path)


def test_envelope_rejects_float_version() -> None:
    """Envelope schema versions are exact integers, not numerically equal floats."""
    import json

    import pytest

    from hermes_fleet.envelope import parse_envelope
    from hermes_fleet.models import FleetDefaults, NodeConfig

    target = NodeConfig(name="alpha", peer_id="peer-alpha")
    payload = json.dumps(
        {
            "version": 1.0,
            "operation": "fleet.health",
            "target": {"name": "alpha", "peer_id": "peer-alpha"},
            "input": {},
            "limits": {"deadline_seconds": 1},
        }
    )

    with pytest.raises(ValueError, match="unsupported envelope version"):
        parse_envelope(payload, target=target, defaults=FleetDefaults())
