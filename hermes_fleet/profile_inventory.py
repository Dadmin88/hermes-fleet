"""Bounded discovery of installed Hermes profile distributions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

_DEFAULT_PROFILES_ROOT: Final[Path] = Path.home() / ".hermes" / "profiles"
_MAX_PROFILE_COUNT: Final[int] = 256
_MAX_MANIFEST_BYTES: Final[int] = 65_536
_MAX_NAME_LENGTH: Final[int] = 128
_MAX_VERSION_LENGTH: Final[int] = 128
_ALLOWED_NAME_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class ProfileInventoryError(ValueError):
    """The local installed-profile inventory cannot be represented safely."""


def scan_profile_distributions(
    root: Path | None = None,
    *,
    max_profiles: int = _MAX_PROFILE_COUNT,
) -> list[dict[str, str]]:
    """Return deterministic unique distribution identities from Hermes profiles.

    Only top-level ``distribution.yaml`` metadata is read. User-owned profile
    state, skills, config, secrets, and runtime files are deliberately ignored.
    Non-distribution and malformed profile directories are omitted. Conflicting
    installations of the same distribution name fail closed rather than making
    Fleet advertise an ambiguous version.
    """
    profiles_root = _DEFAULT_PROFILES_ROOT if root is None else root
    if not isinstance(profiles_root, Path) or max_profiles <= 0:
        raise ProfileInventoryError("profile inventory configuration is invalid")
    if not profiles_root.exists():
        return []
    if profiles_root.is_symlink() or not profiles_root.is_dir():
        raise ProfileInventoryError("Hermes profiles root is not a safe directory")

    discovered: dict[str, str] = {}
    try:
        entries = sorted(profiles_root.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise ProfileInventoryError("Hermes profiles root cannot be read") from error

    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
        except OSError:
            continue
        manifest = entry / "distribution.yaml"
        try:
            if manifest.is_symlink() or not manifest.is_file():
                continue
            if manifest.stat().st_size > _MAX_MANIFEST_BYTES:
                continue
            identity = _read_distribution_identity(manifest)
        except (OSError, UnicodeError, ValueError):
            continue
        if identity is None:
            continue
        name, version = identity
        current = discovered.get(name)
        if current is not None and current != version:
            raise ProfileInventoryError(
                f"conflicting installed versions for Hermes profile distribution {name!r}"
            )
        discovered[name] = version
        if len(discovered) > max_profiles:
            raise ProfileInventoryError("installed Hermes profile inventory exceeds the bound")

    return [
        {"name": name, "version": discovered[name]}
        for name in sorted(discovered)
    ]


def _read_distribution_identity(path: Path) -> tuple[str, str] | None:
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw_line in text.splitlines():
        if not raw_line or raw_line[0].isspace() or ":" not in raw_line:
            continue
        key, raw_value = raw_line.split(":", 1)
        if key not in {"name", "version"}:
            continue
        if key in values:
            return None
        values[key] = _yaml_scalar(raw_value)
    if set(values) != {"name", "version"}:
        return None
    name = values["name"]
    version = values["version"]
    if not _valid_name(name) or not _valid_version(version):
        return None
    return name, version


def _yaml_scalar(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        raise ValueError("empty distribution scalar")
    if value.startswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("distribution scalar must be a string")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError("invalid single-quoted distribution scalar")
        return value[1:-1].replace("''", "'")
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if not value:
        raise ValueError("empty distribution scalar")
    return value


def _valid_name(value: str) -> bool:
    return (
        0 < len(value) <= _MAX_NAME_LENGTH
        and value.strip() == value
        and all(character in _ALLOWED_NAME_CHARS for character in value)
        and value not in {".", ".."}
    )


def _valid_version(value: str) -> bool:
    return (
        0 < len(value) <= _MAX_VERSION_LENGTH
        and value.strip() == value
        and all(32 < ord(character) < 127 and not character.isspace() for character in value)
    )
