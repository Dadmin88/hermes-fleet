"""Typed, transport-independent domain models for Hermes Fleet."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PEER_ID_LIMIT = 256
_OPERATIONS = frozenset({"fleet.health", "fleet.inventory", "fleet.hermes.run"})


def _identifier(value: str, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be a string")
    normalized = value.strip().lower()
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{label} must use lowercase letters, digits, and hyphens")
    return normalized


def _peer_id(value: str) -> str:
    if type(value) is not str:
        raise ValueError("peer_id must be a string")
    if not value or value != value.strip() or len(value) > _PEER_ID_LIMIT:
        raise ValueError("peer_id must be a nonempty trimmed bounded identifier")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("peer_id must not contain whitespace or control characters")
    if "://" in value:
        raise ValueError("peer_id must not use URL syntax")
    return value


def _positive_int(value: object, label: str, maximum: int) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be a positive integer")
    if not 0 < value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class FleetDefaults:
    """Global upper bounds for operator-authored Fleet inventory."""

    max_deadline_seconds: int = 900
    max_payload_bytes: int = 65_536
    max_prompt_chars: int = 16_000
    max_export_paths: int = 8

    def __post_init__(self) -> None:
        _positive_int(self.max_deadline_seconds, "max_deadline_seconds", 86_400)
        _positive_int(self.max_payload_bytes, "max_payload_bytes", 1_048_576)
        _positive_int(self.max_prompt_chars, "max_prompt_chars", 65_536)
        _positive_int(self.max_export_paths, "max_export_paths", 32)


@dataclass(frozen=True, slots=True)
class NodePolicy(FleetDefaults):
    """Default-deny operation and resource limits for one configured Keryx peer."""

    allowed_operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super(NodePolicy, self).__post_init__()
        if type(self.allowed_operations) not in (list, tuple) or not all(
            type(operation) is str for operation in self.allowed_operations
        ):
            raise ValueError("allowed_operations must be a list or tuple of strings")
        normalized = tuple(sorted(set(self.allowed_operations)))
        if any(operation not in _OPERATIONS for operation in normalized):
            raise ValueError("allowed_operations contains an unsupported operation")
        object.__setattr__(self, "allowed_operations", normalized)


@dataclass(frozen=True, slots=True)
class NodeConfig:
    """Friendly operator metadata for an immutable, opaque Keryx peer ID."""

    name: str = ""
    peer_id: str = ""
    tags: tuple[str, ...] = ()
    enabled: bool = True
    priority: int = 0
    policy: NodePolicy = field(default_factory=NodePolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "name"))
        object.__setattr__(self, "peer_id", _peer_id(self.peer_id))
        if type(self.tags) not in (list, tuple) or not all(
            type(tag) is str for tag in self.tags
        ):
            raise ValueError("tags must be a list or tuple of strings")
        object.__setattr__(
            self,
            "tags",
            tuple(sorted({_identifier(tag, "tag") for tag in self.tags})),
        )
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if type(self.priority) is not int:
            raise ValueError("priority must be an integer")
        if type(self.policy) is not NodePolicy:
            raise ValueError("policy must be a NodePolicy")


@dataclass(frozen=True, slots=True)
class RemoteOutput:
    """Remote data retained for display but never trusted as local instructions."""

    text: str
    untrusted: bool = field(default=True, init=False)

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise ValueError("remote output must be text")
