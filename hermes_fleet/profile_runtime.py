"""Execution-owned Hermes profile runtime for destination FX8 work."""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agency_materialization import materialize_agency_bundle
from .execution_package import ExactExecutionPackage

_EXECUTION_PROFILE = "fleet-execution"
_SLOT_FILE = ".fleet-execution-slot"
_SLOT_CONTENT = "hermes-fleet.execution-slot.v1\n"
_OWNER_FILE = ".fleet-execution-owner"
_ENV_REF_RE = re.compile(r"^secret://worker/env/([A-Z][A-Z0-9_]{0,127})$")
_FILE_REF_RE = re.compile(r"^secret://worker/file/([A-Z][A-Z0-9_]{0,127})$")
_DESTINATION_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_FILE_SECRET_BYTES = 1_048_576
_RESERVED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "HERMES_HOME",
        "HERMES_PROFILE",
        "PYTHONPATH",
    }
)


class EnvironmentSecretResolver:
    """Resolve only explicitly configured execution-scoped environment references."""

    def __init__(self, *, allowed_references: tuple[str, ...]) -> None:
        if type(allowed_references) is not tuple:
            raise ValueError("allowed secret references must be a tuple")
        for reference in allowed_references:
            _environment_name(reference)
        self._allowed = frozenset(allowed_references)

    def resolve(
        self,
        references: list[str],
        *,
        requester: str,
        target: dict[str, Any],
        execution_id: str,
    ) -> dict[str, str]:
        del requester, target, execution_id
        if type(references) is not list or any(
            reference not in self._allowed for reference in references
        ):
            raise ValueError("secret reference is not allowed")
        values: dict[str, str] = {}
        for reference in references:
            name = _environment_name(reference)
            value = os.environ.get(name)
            if not value or "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError("secret reference is unavailable")
            values[reference] = value
        return values


@dataclass(frozen=True, slots=True, repr=False)
class LocalFileSecret:
    """Redacted destination-local file capability; never contains secret bytes."""

    source: Path
    destination_name: str
    device: int
    inode: int
    owner_uid: int
    size: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, Path)
            or not self.source.is_absolute()
            or ".." in self.source.parts
            or type(self.destination_name) is not str
            or _DESTINATION_FILE_RE.fullmatch(self.destination_name) is None
            or type(self.device) is not int
            or self.device < 0
            or type(self.inode) is not int
            or self.inode <= 0
            or type(self.owner_uid) is not int
            or self.owner_uid < 0
            or type(self.size) is not int
            or not 0 < self.size <= _MAX_FILE_SECRET_BYTES
        ):
            raise ValueError("file secret capability is invalid")

    def __repr__(self) -> str:
        return "LocalFileSecret(<redacted>)"


class DestinationSecretResolver:
    """Resolve allowlisted environment and destination-local file references."""

    def __init__(
        self,
        *,
        allowed_references: tuple[str, ...],
        file_sources: dict[str, tuple[Path, str]],
    ) -> None:
        if type(allowed_references) is not tuple or type(file_sources) is not dict:
            raise ValueError("secret resolver configuration is invalid")
        for reference in allowed_references:
            if (
                _ENV_REF_RE.fullmatch(reference) is None
                and _FILE_REF_RE.fullmatch(reference) is None
            ):
                raise ValueError("secret reference is invalid")
        for reference, configured in file_sources.items():
            if (
                reference not in allowed_references
                or _FILE_REF_RE.fullmatch(reference) is None
                or type(configured) is not tuple
                or len(configured) != 2
            ):
                raise ValueError("file secret mapping is invalid")
            source, destination_name = configured
            if (
                not isinstance(source, Path)
                or not source.is_absolute()
                or ".." in source.parts
                or type(destination_name) is not str
                or _DESTINATION_FILE_RE.fullmatch(destination_name) is None
            ):
                raise ValueError("file secret mapping is invalid")
        self._allowed = frozenset(allowed_references)
        self._file_sources = dict(file_sources)

    def __repr__(self) -> str:
        return "DestinationSecretResolver(<redacted>)"

    def resolve(
        self,
        references: list[str],
        *,
        requester: str,
        target: dict[str, Any],
        execution_id: str,
    ) -> dict[str, str | LocalFileSecret]:
        del requester, target, execution_id
        if type(references) is not list or any(
            type(reference) is not str or reference not in self._allowed
            for reference in references
        ):
            raise ValueError("secret reference is not allowed")
        values: dict[str, str | LocalFileSecret] = {}
        for reference in references:
            if _ENV_REF_RE.fullmatch(reference) is not None:
                name = _environment_name(reference)
                value = os.environ.get(name)
                if not value or any(c in value for c in ("\x00", "\n", "\r")):
                    raise ValueError("secret reference is unavailable")
                values[reference] = value
                continue
            configured = self._file_sources.get(reference)
            if configured is None:
                raise ValueError("file secret reference is not configured")
            source, destination_name = configured
            metadata = _safe_file_secret_metadata(source)
            values[reference] = LocalFileSecret(
                source=source,
                destination_name=destination_name,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                owner_uid=metadata.st_uid,
                size=metadata.st_size,
            )
        return values


class ProfileHermesRuntime:
    """Materialize, invoke, and delete one immutable execution-owned profile."""

    def __init__(
        self,
        *,
        profiles_root: Path,
        runs_factory: Callable[[str], Any],
        api_server_key: str,
    ) -> None:
        if not isinstance(profiles_root, Path) or not profiles_root.is_absolute():
            raise ValueError("profiles root must be an absolute Path")
        if not callable(runs_factory):
            raise ValueError("runs_factory must be callable")
        if (
            type(api_server_key) is not str
            or not api_server_key
            or any(character in api_server_key for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("API server key must be nonempty bounded text")
        self._profiles_root = profiles_root
        self._runs_factory = runs_factory
        self._api_server_key = api_server_key
        self._runs: dict[str, Any] = {}

    def materialize(
        self,
        package: ExactExecutionPackage,
        *,
        secrets: dict[str, str | LocalFileSecret],
    ) -> str:
        if type(package) is not ExactExecutionPackage:
            raise ValueError("execution package is invalid")
        profile = _EXECUTION_PROFILE
        destination = self._profiles_root / profile
        environment: dict[str, str] = {}
        file_secrets: list[LocalFileSecret] = []
        if type(secrets) is not dict:
            raise ValueError("execution secrets are invalid")
        for reference, value in secrets.items():
            if _ENV_REF_RE.fullmatch(reference) is not None:
                name = _environment_name(reference)
                if (
                    type(value) is not str
                    or not value
                    or any(c in value for c in ("\x00", "\n", "\r"))
                ):
                    raise ValueError("execution secret material is invalid")
                environment[name] = value
            elif (
                _FILE_REF_RE.fullmatch(reference) is not None
                and type(value) is LocalFileSecret
            ):
                file_secrets.append(value)
            else:
                raise ValueError("secret reference is invalid")
        if "API_SERVER_KEY" in environment:
            raise ValueError("API server key is reserved execution state")
        environment["API_SERVER_KEY"] = self._api_server_key
        _prepare_owned_slot(destination)
        staging = destination / ".materializing"
        if staging.exists() or staging.is_symlink():
            raise ValueError("execution profile staging path is unavailable")
        reclaimed = False
        try:
            materialize_agency_bundle(package.agency_bundle, destination=staging)
            if any(
                (staging / name).exists() or (staging / name).is_symlink()
                for name in (_SLOT_FILE, _OWNER_FILE)
            ):
                raise ValueError("Agency profile contains reserved Fleet state")
            owner = staging / _OWNER_FILE
            owner.write_text(package.execution_id + "\n", encoding="utf-8")
            owner.chmod(0o600)
            environment_path = staging / ".env"
            content = "".join(
                f"{name}={environment[name]}\n" for name in sorted(environment)
            )
            environment_path.write_text(content, encoding="utf-8")
            environment_path.chmod(0o600)
            for file_secret in file_secrets:
                _copy_local_file_secret(file_secret, staging)
            client = self._runs_factory(profile)
            _clear_owned_slot(destination, preserve_names={staging.name})
            reclaimed = True
            for item in tuple(staging.iterdir()):
                item.replace(destination / item.name)
            staging.rmdir()
            self._runs[profile] = client
            return profile
        except BaseException:
            if reclaimed:
                _clear_owned_slot(destination)
            elif staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging)
            self._runs.pop(profile, None)
            raise
        finally:
            environment.clear()
            file_secrets.clear()

    def start(
        self,
        profile: str,
        *,
        prompt: str,
        session_id: str,
        timeout_seconds: float,
    ) -> str:
        client = self._client(profile)
        return client.start(
            prompt=prompt,
            session_id=session_id,
            timeout_seconds=timeout_seconds,
        )

    def wait(self, profile: str, *, run_id: str, timeout_seconds: float) -> Any:
        return self._client(profile).wait(
            run_id=run_id, timeout_seconds=timeout_seconds
        )

    def stop(
        self, profile: str, run_id: str, *, timeout_seconds: float | None = None
    ) -> None:
        self._client(profile).stop(run_id, timeout_seconds=timeout_seconds)

    def cleanup(self, profile: str, *, expected_owner: str) -> None:
        if profile != _EXECUTION_PROFILE:
            raise ValueError("execution profile is invalid")
        if (
            type(expected_owner) is not str
            or not expected_owner
            or "\n" in expected_owner
            or "\r" in expected_owner
        ):
            raise ValueError("expected execution owner is invalid")
        destination = self._profiles_root / profile
        if destination.parent != self._profiles_root:
            raise ValueError("execution profile path is invalid")
        slot = destination / _SLOT_FILE
        if (
            not slot.is_file()
            or slot.is_symlink()
            or slot.read_text(encoding="utf-8") != _SLOT_CONTENT
        ):
            raise ValueError("execution profile is not owned by Fleet")
        owner = destination / _OWNER_FILE
        if not owner.is_file() or owner.is_symlink():
            raise ValueError("execution profile is not owned by Fleet")
        try:
            execution_id = owner.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise ValueError("execution profile ownership is invalid") from error
        if not execution_id or "\n" in execution_id or "\r" in execution_id:
            raise ValueError("execution profile ownership is invalid")
        if execution_id != expected_owner:
            raise ValueError("execution profile ownership changed")
        _clear_owned_slot(destination)
        if set(item.name for item in destination.iterdir()) != {_SLOT_FILE}:
            raise RuntimeError("execution profile cleanup is unproven")
        self._runs.pop(profile, None)

    def owner(self, profile: str) -> str | None:
        if profile != _EXECUTION_PROFILE:
            raise ValueError("execution profile is invalid")
        destination = self._profiles_root / profile
        slot = destination / _SLOT_FILE
        if (
            not slot.is_file()
            or slot.is_symlink()
            or slot.read_text(encoding="utf-8") != _SLOT_CONTENT
        ):
            raise ValueError("execution profile is not owned by Fleet")
        owner = destination / _OWNER_FILE
        if not owner.exists():
            return None
        if not owner.is_file() or owner.is_symlink():
            raise ValueError("execution profile ownership is invalid")
        value = owner.read_text(encoding="utf-8")
        if not value.endswith("\n") or not value[:-1] or "\n" in value[:-1]:
            raise ValueError("execution profile ownership is invalid")
        return value[:-1]

    def status(self, profile: str, *, run_id: str) -> str:
        if self.owner(profile) is None:
            raise ValueError("execution profile is not owned")
        client = self._runs.get(profile)
        if client is None:
            client = self._runs_factory(profile)
            self._runs[profile] = client
        return client.status(run_id)

    def _client(self, profile: str) -> Any:
        if profile != _EXECUTION_PROFILE or profile not in self._runs:
            raise ValueError("execution profile is unavailable")
        return self._runs[profile]


def _environment_name(reference: object) -> str:
    if type(reference) is not str:
        raise ValueError("secret reference is invalid")
    match = _ENV_REF_RE.fullmatch(reference)
    if match is None or match.group(1) in _RESERVED_ENV:
        raise ValueError("secret reference is invalid")
    return match.group(1)


def _safe_file_secret_metadata(source: Path) -> os.stat_result:
    try:
        metadata = source.lstat()
    except OSError as error:
        raise ValueError("file secret source is unsafe") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or not 0 < metadata.st_size <= _MAX_FILE_SECRET_BYTES
    ):
        raise ValueError("file secret source is unsafe")
    return metadata


def _copy_local_file_secret(secret: LocalFileSecret, destination: Path) -> None:
    if type(secret) is not LocalFileSecret:
        raise ValueError("file secret capability is invalid")
    secret.__post_init__()
    source_fd = destination_fd = -1
    target = destination / secret.destination_name
    try:
        source_fd = os.open(
            secret.source,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or (
                before.st_dev,
                before.st_ino,
                before.st_uid,
                before.st_size,
            )
            != (secret.device, secret.inode, secret.owner_uid, secret.size)
        ):
            raise ValueError("file secret source changed before use")
        destination_fd = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        remaining = secret.size
        while remaining:
            chunk = os.read(source_fd, min(65_536, remaining))
            if not chunk:
                raise ValueError("file secret source changed during use")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
            remaining -= len(chunk)
        if os.read(source_fd, 1):
            raise ValueError("file secret source changed during use")
        after = os.fstat(source_fd)
        if (after.st_dev, after.st_ino, after.st_uid, after.st_size) != (
            secret.device,
            secret.inode,
            secret.owner_uid,
            secret.size,
        ):
            raise ValueError("file secret source changed during use")
        os.fsync(destination_fd)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _prepare_owned_slot(destination: Path) -> None:
    if not destination.is_dir() or destination.is_symlink():
        raise ValueError("execution profile is not an empty owned slot")
    try:
        directory_metadata = destination.stat()
        marker = destination / _SLOT_FILE
        marker_metadata = marker.lstat()
        owner = destination / _OWNER_FILE
        valid = (
            directory_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(directory_metadata.st_mode) == 0o700
            and stat.S_ISREG(marker_metadata.st_mode)
            and marker_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(marker_metadata.st_mode) == 0o600
            and marker_metadata.st_nlink == 1
            and marker.read_text(encoding="utf-8") == _SLOT_CONTENT
            and not owner.exists()
            and not owner.is_symlink()
        )
    except (OSError, UnicodeError):
        valid = False
    if not valid:
        raise ValueError("execution profile is not an empty owned slot")


def _clear_owned_slot(
    destination: Path, *, preserve_names: set[str] | None = None
) -> None:
    if not destination.is_dir() or destination.is_symlink():
        return
    preserved = preserve_names or set()
    for item in tuple(destination.iterdir()):
        if item.name == _SLOT_FILE or item.name in preserved:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
