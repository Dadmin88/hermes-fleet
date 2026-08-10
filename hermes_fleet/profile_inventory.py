"""Bounded discovery of installed Hermes profile distributions."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Final

_DEFAULT_PROFILES_ROOT: Final[Path] = Path.home() / ".hermes" / "profiles"
_MAX_PROFILE_COUNT: Final[int] = 256
_MAX_MANIFEST_BYTES: Final[int] = 65_536
_MAX_NAME_LENGTH: Final[int] = 128
_MAX_VERSION_LENGTH: Final[int] = 128
_MAX_CONTENT_FILES: Final[int] = 4_096
_MAX_CONTENT_FILE_BYTES: Final[int] = 16 * 1024 * 1024
_MAX_CONTENT_BYTES: Final[int] = 64 * 1024 * 1024
_ALLOWED_NAME_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
PROFILE_CONTENT_DIGEST_SCHEMA: Final[str] = "hermes-agency-profile-content.v1"
_CONTENT_FILES: Final[tuple[str, ...]] = (
    "SOUL.md",
    "config.yaml",
    "mcp.json",
    ".no-bundled-skills",
)
_CONTENT_DIRS: Final[tuple[str, ...]] = ("skills", "cron")


class ProfileInventoryError(ValueError):
    """The local installed-profile inventory cannot be represented safely."""


def scan_profile_distributions(
    root: Path | None = None,
    *,
    max_profiles: int = _MAX_PROFILE_COUNT,
) -> list[dict[str, str]]:
    """Return deterministic unique distribution identities from Hermes profiles.

    Distribution name/version are read from each bounded top-level manifest.
    When the installed profile has the Agency V1 behavior shape, Fleet also
    computes its exact content digest from behavior-bearing files. Generic or
    unsafe distributions remain visible by name/version but omit exact content
    identity. Conflicting installations of one distribution identity fail
    closed rather than making Fleet advertise ambiguous presence.
    """
    profiles_root = _DEFAULT_PROFILES_ROOT if root is None else root
    if not isinstance(profiles_root, Path) or max_profiles <= 0:
        raise ProfileInventoryError("profile inventory configuration is invalid")
    if not profiles_root.exists():
        return []
    if profiles_root.is_symlink() or not profiles_root.is_dir():
        raise ProfileInventoryError("Hermes profiles root is not a safe directory")

    discovered: dict[str, tuple[str, str | None]] = {}
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
        content_digest = _profile_content_digest(entry, name, version)
        candidate = (version, content_digest)
        current = discovered.get(name)
        if current is not None and current != candidate:
            message = (
                "conflicting installed identities for Hermes profile distribution "
                f"{name!r}"
            )
            raise ProfileInventoryError(message)
        discovered[name] = candidate
        if len(discovered) > max_profiles:
            raise ProfileInventoryError(
                "installed Hermes profile inventory exceeds the bound"
            )

    result: list[dict[str, str]] = []
    for name in sorted(discovered):
        version, content_digest = discovered[name]
        item = {"name": name, "version": version}
        if content_digest is not None:
            item["content_digest"] = content_digest
        result.append(item)
    return result


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


def _profile_content_digest(profile_dir: Path, name: str, version: str) -> str | None:
    content = _collect_content_files(profile_dir)
    if content is None:
        return None

    records: list[dict[str, object]] = []
    for path, expected_stat in content:
        file_digest = _hash_stable_file(path, expected_stat)
        if file_digest is None:
            return None
        records.append(
            {
                "path": path.relative_to(profile_dir).as_posix(),
                "sha256": file_digest,
                "executable": bool(expected_stat.st_mode & 0o111),
            }
        )

    material = {
        "schema": PROFILE_CONTENT_DIGEST_SCHEMA,
        "name": name,
        "version": version,
        "files": records,
    }
    payload = json.dumps(
        material,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _collect_content_files(profile_dir: Path) -> list[tuple[Path, object]] | None:
    soul = profile_dir / "SOUL.md"
    skills = profile_dir / "skills"
    try:
        if soul.is_symlink() or not soul.is_file():
            return None
        if skills.is_symlink() or not skills.is_dir():
            return None
    except OSError:
        return None

    paths: list[Path] = []
    try:
        for relative in _CONTENT_FILES:
            path = profile_dir / relative
            if path.is_symlink():
                return None
            if path.exists():
                if not path.is_file():
                    return None
                paths.append(path)

        for relative in _CONTENT_DIRS:
            directory = profile_dir / relative
            if directory.is_symlink():
                return None
            if not directory.exists():
                continue
            if not directory.is_dir():
                return None
            for path in directory.rglob("*"):
                if path.is_symlink():
                    return None
                if path.is_file():
                    paths.append(path)
                elif not path.is_dir():
                    return None
    except OSError:
        return None

    paths.sort(key=lambda path: path.relative_to(profile_dir).as_posix())
    if len(paths) > _MAX_CONTENT_FILES:
        return None

    content: list[tuple[Path, object]] = []
    total_bytes = 0
    for path in paths:
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size < 0 or metadata.st_size > _MAX_CONTENT_FILE_BYTES:
            return None
        total_bytes += metadata.st_size
        if total_bytes > _MAX_CONTENT_BYTES:
            return None
        content.append((path, metadata))
    return content


def _hash_stable_file(path: Path, expected_stat: object) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat(follow_symlinks=False)
    except OSError:
        return None

    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
    if any(
        getattr(expected_stat, field) != getattr(after, field)
        for field in stable_fields
    ):
        return None
    if not stat.S_ISREG(after.st_mode):
        return None
    return digest.hexdigest()


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
        and all(
            32 < ord(character) < 127 and not character.isspace() for character in value
        )
    )
