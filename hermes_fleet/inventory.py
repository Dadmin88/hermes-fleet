"""Atomic local state helpers for Fleet operator inventory and recoverable cache."""

from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

import yaml

from ._paths import is_concrete_path

_INITIAL_INVENTORY = {"schema_version": 1, "defaults": {}, "nodes": []}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
_TEMPORARY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_TEMPORARY_ATTEMPTS = 32


def _verify_owner_directory(descriptor: int) -> None:
    """Verify an opened directory without changing an existing ancestor's mode."""
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("state directory must be an owner-owned directory")
    if metadata.st_uid != os.getuid():
        raise ValueError("state directory must be owned by the current user")


def _tighten_owner_directory(descriptor: int) -> None:
    """Apply the state-directory mode after ownership and type verification."""
    _verify_owner_directory(descriptor)
    os.fchmod(descriptor, 0o700)


def _require_absolute_state_path(path: Path) -> Path:
    """Reject ambiguous state paths before any directory descriptor is opened."""
    if not is_concrete_path(path):
        raise ValueError("state directory path must be a Path")
    if not path.is_absolute():
        raise ValueError("state directory path must be absolute")
    if ".." in path.parts:
        raise ValueError("state directory path must not contain parent traversal")
    return path


def _open_owner_directory(path: Path) -> int:
    """Open and verify an owner-owned directory without following its path."""
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise ValueError("state directory must be an owner-owned directory") from error
    try:
        _verify_owner_directory(descriptor)
    except (OSError, ValueError) as error:
        os.close(descriptor)
        if isinstance(error, ValueError):
            raise
        raise ValueError("state directory must be an owner-owned directory") from error
    return descriptor


def _open_or_create_owner_directory(path: Path) -> int:
    """Create missing directory components below a verified owner-owned ancestor."""
    missing_components: list[str] = []
    current = path
    descriptor = -1
    try:
        while True:
            try:
                descriptor = os.open(current, _DIRECTORY_FLAGS)
            except FileNotFoundError:
                if current.name in {"", ".", ".."}:
                    raise ValueError(
                        "state directory must be an owner-owned directory"
                    ) from None
                missing_components.append(current.name)
                current = current.parent
                continue
            _verify_owner_directory(descriptor)
            break

        while missing_components:
            name = missing_components.pop()
            created = False
            try:
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                created = True
            except FileExistsError:
                pass
            child_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                _verify_owner_directory(child_descriptor)
                if created:
                    os.fchmod(child_descriptor, 0o700)
            except (OSError, ValueError):
                os.close(child_descriptor)
                raise
            os.close(descriptor)
            descriptor = child_descriptor
        result = descriptor
        descriptor = -1
        return result
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("state directory must be an owner-owned directory") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _open_state_directory(state_dir: Path) -> int:
    """Open or initialize ``state_dir`` relative to its verified parent descriptor."""
    state_dir = _require_absolute_state_path(state_dir)
    parent_descriptor = _open_or_create_owner_directory(state_dir.parent)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                state_dir.name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
            )
        except FileNotFoundError:
            try:
                os.mkdir(state_dir.name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(
                state_dir.name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
            )
        _tighten_owner_directory(descriptor)
        result = descriptor
        descriptor = -1
        return result
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("state directory must be an owner-owned directory") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(parent_descriptor)


def _safe_existing_target_at(directory_descriptor: int, name: str) -> bool:
    """Validate an existing child by descriptor without following it."""
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValueError("state target must be an owner-owned regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("state target must be an owner-owned regular file")
        if metadata.st_uid != os.getuid():
            raise ValueError("state target must be owned by the current user")
        os.fchmod(descriptor, 0o600)
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError("state target must be an owner-owned regular file") from error
    finally:
        os.close(descriptor)
    return True


def _new_temporary_file(directory_descriptor: int, name: str) -> tuple[int, str]:
    for _ in range(_TEMPORARY_ATTEMPTS):
        temporary_name = f".{name}.{secrets.token_hex(16)}"
        try:
            return (
                os.open(
                    temporary_name,
                    _TEMPORARY_FLAGS,
                    0o600,
                    dir_fd=directory_descriptor,
                ),
                temporary_name,
            )
        except FileExistsError:
            continue
        except OSError as error:
            raise ValueError("unable to create atomic state temporary") from error
    raise ValueError("unable to create unique atomic state temporary")


def _atomic_write_at(directory_descriptor: int, name: str, content: str) -> None:
    """Atomically replace ``name`` within an already-verified directory."""
    _safe_existing_target_at(directory_descriptor, name)
    descriptor, temporary_name = _new_temporary_file(directory_descriptor, name)
    replaced = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        replaced = True
        os.fsync(directory_descriptor)
    except OSError as error:
        raise ValueError("unable to atomically write state") from error
    finally:
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace a current-user regular file without following symlinks."""
    directory_descriptor = _open_owner_directory(path.parent)
    try:
        _atomic_write_at(directory_descriptor, path.name, content)
    finally:
        os.close(directory_descriptor)


def write_json_atomic(path: Path, value: Any) -> None:
    """Replace recoverable cache JSON only after full serialization."""
    if not is_concrete_path(path):
        raise ValueError("state file path must be a Path")
    _atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_yaml_atomic(path: Path, value: Any) -> None:
    """Replace operator inventory YAML atomically and owner-safely."""
    if not is_concrete_path(path):
        raise ValueError("state file path must be a Path")
    _atomic_write(path, yaml.safe_dump(value, sort_keys=False))


def _read_text_at(directory_descriptor: int, name: str) -> str:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("state target must be an owner-owned regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _valid_cache_at(directory_descriptor: int, name: str) -> bool:
    try:
        value = json.loads(_read_text_at(directory_descriptor, name))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(value, dict)


def load_cache(path: Path) -> dict[str, Any]:
    """Read cache data or return a recoverable empty mapping for malformed input."""
    if not is_concrete_path(path):
        return {}
    try:
        directory_descriptor = _open_owner_directory(path.parent)
    except ValueError:
        return {}
    try:
        value = json.loads(_read_text_at(directory_descriptor, path.name))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    finally:
        os.close(directory_descriptor)
    return value if isinstance(value, dict) else {}


def initialize_inventory_state(state_dir: Path) -> None:
    """Create missing inventory/cache files; preserve valid operator state exactly."""
    directory_descriptor = _open_state_directory(state_dir)
    try:
        if not _safe_existing_target_at(directory_descriptor, "nodes.yaml"):
            _atomic_write_at(
                directory_descriptor,
                "nodes.yaml",
                yaml.safe_dump(_INITIAL_INVENTORY, sort_keys=False),
            )
        _safe_existing_target_at(directory_descriptor, "cache.json")
        if not _valid_cache_at(directory_descriptor, "cache.json"):
            _atomic_write_at(directory_descriptor, "cache.json", "{}\n")
    finally:
        os.close(directory_descriptor)
