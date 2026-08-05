"""Strict Fleet inventory configuration and profile-aware state paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.resolver import BaseResolver

from .models import FleetDefaults, NodeConfig, NodePolicy

_CONCRETE_PATH_TYPE = type(Path())


class FleetConfigError(ValueError):
    """Stable public configuration error for Fleet-owned parser boundaries."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate mapping keys at every depth."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise FleetConfigError("inventory mapping keys must be hashable") from error
        if duplicate:
            raise FleetConfigError(f"duplicate inventory key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True, slots=True)
class FleetConfig:
    """The versioned, credential-free Fleet inventory document."""

    schema_version: int
    defaults: FleetDefaults
    nodes: tuple[NodeConfig, ...]


def _require_absolute_state_root(path: Path) -> Path:
    """Reject ambiguous roots before callers derive local Fleet state paths."""
    if type(path) is not _CONCRETE_PATH_TYPE:
        raise FleetConfigError("state root must be a Path")
    if not path.is_absolute():
        raise FleetConfigError("state root must be absolute")
    if ".." in path.parts:
        raise FleetConfigError("state root must not contain parent traversal")
    return path


def get_hermes_home() -> Path:
    """Resolve the active Hermes state root without creating global state."""
    configured_home = os.environ.get("HERMES_HOME")
    if configured_home:
        return _require_absolute_state_root(Path(configured_home).expanduser())
    try:
        from hermes_constants import get_hermes_home as runtime_home
    except ImportError:
        return Path.home() / ".hermes"
    return Path(runtime_home())


def get_fleet_dir(*, standalone_home: Path | None = None) -> Path:
    """Return active ``HERMES_HOME/fleet`` or an explicit standalone test root."""
    home = standalone_home if standalone_home is not None else get_hermes_home()
    return _require_absolute_state_root(home) / "fleet"


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FleetConfigError(f"{label} must be a mapping")
    return value


def _node(value: Any) -> NodeConfig:
    raw = _mapping(value, "node")
    allowed = {"name", "peer_id", "tags", "enabled", "priority", "policy"}
    unknown = set(raw).difference(allowed)
    if unknown:
        raise FleetConfigError(
            f"node contains unknown keys: {sorted(unknown, key=repr)}"
        )
    policy_raw = _mapping(raw.get("policy", {}), "policy")
    allowed_policy = {
        "allowed_operations",
        "max_deadline_seconds",
        "max_payload_bytes",
        "max_prompt_chars",
        "max_export_paths",
    }
    unknown_policy = set(policy_raw).difference(allowed_policy)
    if unknown_policy:
        raise FleetConfigError(
            f"policy contains unknown keys: {sorted(unknown_policy, key=repr)}"
        )
    values = dict(raw)
    try:
        values["policy"] = NodePolicy(**policy_raw)
        return NodeConfig(**values)
    except (TypeError, ValueError) as error:
        raise FleetConfigError(str(error)) from error


def load_fleet_config(path: Path) -> FleetConfig:
    """Load strict schema-v1 inventory; no URL, secret, or transport fields exist."""
    if type(path) is not _CONCRETE_PATH_TYPE:
        raise FleetConfigError("configuration path must be a Path")
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeySafeLoader)
    except FleetConfigError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError) as error:
        raise FleetConfigError(
            "schema_version 1 configuration file is required"
        ) from error
    document = _mapping(raw, "configuration")
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise FleetConfigError("schema_version must be 1")
    if schema_version != 1:
        raise FleetConfigError("schema_version must be 1")
    allowed_document = {"schema_version", "defaults", "nodes"}
    unknown_document = set(document).difference(allowed_document)
    if unknown_document:
        raise FleetConfigError(
            f"unknown configuration keys: {sorted(unknown_document, key=repr)}"
        )

    defaults_raw = _mapping(document.get("defaults", {}), "defaults")
    allowed_defaults = {
        "max_deadline_seconds",
        "max_payload_bytes",
        "max_prompt_chars",
        "max_export_paths",
    }
    if set(defaults_raw).difference(allowed_defaults):
        raise FleetConfigError("defaults contains unknown keys")
    try:
        defaults = FleetDefaults(**defaults_raw)
    except (TypeError, ValueError) as error:
        raise FleetConfigError(str(error)) from error

    nodes_raw = document.get("nodes", [])
    if not isinstance(nodes_raw, list):
        raise FleetConfigError("nodes must be a list")
    nodes = tuple(_node(node) for node in nodes_raw)
    names = [node.name for node in nodes]
    peer_ids = [node.peer_id for node in nodes]
    if len(set(names)) != len(names):
        raise FleetConfigError("node names must be unique")
    if len(set(peer_ids)) != len(peer_ids):
        raise FleetConfigError("node peer IDs must be unique")
    return FleetConfig(schema_version=1, defaults=defaults, nodes=nodes)
