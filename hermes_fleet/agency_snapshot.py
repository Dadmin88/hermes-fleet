"""Pinned Hermes Agency source and runtime-catalog validation for Fleet placement."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from . import profile_inventory

_CATALOG_SCHEMA_VERSION: Final[int] = 2
_MAX_CATALOG_BYTES: Final[int] = 1_048_576
_MAX_PROFILE_COUNT: Final[int] = 256
_MAX_PROFILE_NAME_BYTES: Final[int] = 128
_MAX_PROFILE_VERSION_BYTES: Final[int] = 128
_MAX_DESCRIPTION_CHARS: Final[int] = 4_096
_MAX_CATEGORY_BYTES: Final[int] = 64
_MAX_CAPABILITIES: Final[int] = 128
_MAX_CAPABILITY_BYTES: Final[int] = 128
_MAX_REPOSITORY_CHARS: Final[int] = 2_048
_MAX_GIT_OUTPUT_BYTES: Final[int] = 512
_DEFAULT_GIT_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_CATALOG_TIMEOUT_SECONDS: Final[float] = 10.0
_AGENCY_CACHE_ROOT_ENV: Final[str] = "FLEET_AGENCY_CACHE_ROOT"
_SUPPORTED_DIGEST_SCHEMA: Final[str] = profile_inventory.PROFILE_CONTENT_DIGEST_SCHEMA


class AgencySnapshotError(ValueError):
    """A pinned Agency source or catalog cannot be trusted for placement."""


class _DuplicateJsonObjectKey(ValueError):
    pass


class _NonStandardJsonValue(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgencySource:
    """One approved repository bound to one exact immutable git object ID."""

    repository: str
    revision: str

    def __post_init__(self) -> None:
        if (
            type(self.repository) is not str
            or not self.repository
            or len(self.repository) > _MAX_REPOSITORY_CHARS
            or self.repository != self.repository.strip()
            or self.repository.startswith("-")
            or any(
                character.isspace() or ord(character) < 32
                for character in self.repository
            )
        ):
            raise AgencySnapshotError("Agency repository identity is invalid")
        if not _valid_git_object_id(self.revision):
            raise AgencySnapshotError(
                "Agency revision must be an exact full git object ID"
            )


@dataclass(frozen=True, slots=True)
class _CatalogProfile:
    name: str
    version: str
    category: str
    priority: str
    description: str
    distribution_path: str
    content_digest: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgencyProfilePackage:
    """One independently verified profile package inside a pinned Agency checkout."""

    source: AgencySource
    name: str
    version: str
    content_digest: str
    category: str
    priority: str
    capabilities: tuple[str, ...]
    distribution_path: str
    local_path: Path


@dataclass(frozen=True, slots=True)
class AgencySnapshot:
    """Validated runtime catalog tied to the lifetime of one exact checkout."""

    source: AgencySource
    checkout_root: Path
    agency_version: str
    orchestrator: str
    profiles: tuple[_CatalogProfile, ...]

    def resolve_profile(self, name: str) -> AgencyProfilePackage:
        """Verify one catalog entry against the exact checked-out package bytes."""
        if not _valid_profile_name(name):
            raise AgencySnapshotError("requested Agency profile name is invalid")
        profile = next((item for item in self.profiles if item.name == name), None)
        if profile is None:
            raise AgencySnapshotError("requested Agency profile is not in the snapshot")

        local_path = _safe_distribution_directory(
            self.checkout_root, profile.distribution_path
        )
        manifest = local_path / "distribution.yaml"
        try:
            if manifest.is_symlink() or not manifest.is_file():
                raise AgencySnapshotError(
                    "Agency profile distribution manifest is invalid"
                )
            identity = profile_inventory._read_distribution_identity(manifest)
        except (OSError, UnicodeError, ValueError) as error:
            raise AgencySnapshotError(
                "Agency profile distribution manifest cannot be trusted"
            ) from error
        if identity != (profile.name, profile.version):
            raise AgencySnapshotError(
                "Agency profile distribution identity does not match catalog"
            )

        digest = profile_inventory._profile_content_digest(
            local_path, profile.name, profile.version
        )
        if digest is None or digest != profile.content_digest:
            raise AgencySnapshotError(
                "Agency profile content does not match catalog identity"
            )
        return AgencyProfilePackage(
            source=self.source,
            name=profile.name,
            version=profile.version,
            content_digest=profile.content_digest,
            category=profile.category,
            priority=profile.priority,
            capabilities=profile.capabilities,
            distribution_path=profile.distribution_path,
            local_path=local_path,
        )


def _resolved_cache_root(explicit: Path | None) -> Path | None:
    candidate = explicit
    if candidate is None:
        raw = os.environ.get(_AGENCY_CACHE_ROOT_ENV, "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
    if not isinstance(candidate, Path) or not candidate.is_absolute():
        raise AgencySnapshotError("Agency cache root must be an absolute path")
    try:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        if candidate.is_symlink() or not candidate.is_dir():
            raise AgencySnapshotError("Agency cache root is invalid")
        candidate.chmod(0o700)
        return candidate.resolve(strict=True)
    except OSError as error:
        raise AgencySnapshotError("Agency cache root cannot be secured") from error


def _git_unvalidated_text(
    command: list[str], *, timeout_seconds: float, environment: dict[str, str]
) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AgencySnapshotError("cached Agency git verification failed") from error
    if completed.returncode != 0 or len(completed.stdout) > 4096:
        raise AgencySnapshotError("cached Agency git verification failed")
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeError as error:
        raise AgencySnapshotError("cached Agency git verification failed") from error


def _cached_commit_exists(
    git: str,
    repository: Path,
    revision: str,
    *,
    timeout_seconds: float,
    environment: dict[str, str],
) -> bool:
    try:
        completed = subprocess.run(
            [
                git,
                "--git-dir",
                str(repository),
                "cat-file",
                "-e",
                f"{revision}^{{commit}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AgencySnapshotError("cached Agency git verification failed") from error
    return completed.returncode == 0


def _ensure_cached_repository(
    source: AgencySource,
    *,
    cache_root: Path,
    git: str,
    timeout_seconds: float,
    environment: dict[str, str],
) -> Path:
    repositories = cache_root / "repositories"
    try:
        repositories.mkdir(parents=True, exist_ok=True, mode=0o700)
        repositories.chmod(0o700)
    except OSError as error:
        raise AgencySnapshotError(
            "Agency repository cache cannot be secured"
        ) from error

    identity = hashlib.sha256(source.repository.encode("utf-8")).hexdigest()
    repository = repositories / f"{identity}.git"
    lock_path = repositories / f".{identity}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise AgencySnapshotError(
            "Agency repository cache lock is unavailable"
        ) from error
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if repository.exists():
            if repository.is_symlink() or not repository.is_dir():
                raise AgencySnapshotError("cached Agency repository is invalid")
        else:
            with tempfile.TemporaryDirectory(
                prefix=f".{identity}-", dir=repositories
            ) as temporary:
                staged = Path(temporary) / "repository.git"
                _run_git_quiet(
                    [
                        git,
                        "-c",
                        "core.hooksPath=/dev/null",
                        "clone",
                        "--bare",
                        "--no-tags",
                        "--quiet",
                        source.repository,
                        str(staged),
                    ],
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                )
                staged.rename(repository)

        origin = _git_unvalidated_text(
            [
                git,
                "--git-dir",
                str(repository),
                "config",
                "--get",
                "remote.origin.url",
            ],
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        if origin != source.repository:
            raise AgencySnapshotError("cached Agency repository origin is invalid")
        bare = _git_unvalidated_text(
            [git, "--git-dir", str(repository), "rev-parse", "--is-bare-repository"],
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        if bare != "true":
            raise AgencySnapshotError("cached Agency repository is not bare")

        if not _cached_commit_exists(
            git,
            repository,
            source.revision,
            timeout_seconds=timeout_seconds,
            environment=environment,
        ):
            _run_git_quiet(
                [
                    git,
                    "--git-dir",
                    str(repository),
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "origin",
                ],
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
        if not _cached_commit_exists(
            git,
            repository,
            source.revision,
            timeout_seconds=timeout_seconds,
            environment=environment,
        ):
            raise AgencySnapshotError("pinned Agency revision is unavailable in cache")
        return repository
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


@contextmanager
def acquire_agency_snapshot(
    source: AgencySource,
    *,
    git_executable: str | None = None,
    git_timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
    catalog_timeout_seconds: float = _DEFAULT_CATALOG_TIMEOUT_SECONDS,
    cache_root: Path | None = None,
) -> Iterator[AgencySnapshot]:
    """Acquire, verify, and yield one temporary immutable Agency checkout.

    When an Agency cache root is configured, the exact pinned Git object is
    retained in a local bare repository and each execution checks out from that
    local object store. Network access is then needed only when the requested
    immutable object is not already present in the cache.
    """
    if type(source) is not AgencySource:
        raise AgencySnapshotError("source must be an AgencySource")
    _bounded_timeout(git_timeout_seconds, "git timeout")
    _bounded_timeout(catalog_timeout_seconds, "catalog timeout")
    git = git_executable or shutil.which("git")
    if type(git) is not str or not git:
        raise AgencySnapshotError("git executable is unavailable")

    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    resolved_cache = _resolved_cache_root(cache_root)
    clone_source = source.repository
    clone_options: list[str] = []
    temporary_parent: Path | None = None
    if resolved_cache is not None:
        cached_repository = _ensure_cached_repository(
            source,
            cache_root=resolved_cache,
            git=git,
            timeout_seconds=git_timeout_seconds,
            environment=environment,
        )
        checkouts = resolved_cache / "checkouts"
        try:
            checkouts.mkdir(parents=True, exist_ok=True, mode=0o700)
            checkouts.chmod(0o700)
        except OSError as error:
            raise AgencySnapshotError(
                "Agency checkout cache cannot be secured"
            ) from error
        clone_source = str(cached_repository)
        clone_options.append("--shared")
        temporary_parent = checkouts

    with tempfile.TemporaryDirectory(
        prefix="hermes-fleet-agency-",
        dir=temporary_parent,
    ) as temporary:
        temporary_root = Path(temporary)
        try:
            temporary_root.chmod(0o700)
        except OSError as error:
            raise AgencySnapshotError(
                "temporary Agency checkout cannot be secured"
            ) from error
        checkout = temporary_root / "checkout"

        _run_git_quiet(
            [
                git,
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                *clone_options,
                "--no-checkout",
                "--no-tags",
                "--quiet",
                clone_source,
                str(checkout),
            ],
            timeout_seconds=git_timeout_seconds,
            environment=environment,
        )
        _run_git_quiet(
            [
                git,
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(checkout),
                "checkout",
                "--detach",
                "--quiet",
                source.revision,
            ],
            timeout_seconds=git_timeout_seconds,
            environment=environment,
        )
        actual_revision = _run_git_text(
            [
                git,
                "-C",
                str(checkout),
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            timeout_seconds=git_timeout_seconds,
            environment=environment,
        )
        if actual_revision != source.revision:
            raise AgencySnapshotError(
                "Agency checkout revision does not match requested pin"
            )

        catalog_bytes = _run_catalog(checkout, timeout_seconds=catalog_timeout_seconds)
        snapshot = _parse_catalog(catalog_bytes, source=source, checkout_root=checkout)
        yield snapshot


def _parse_catalog(
    payload: bytes,
    *,
    source: AgencySource,
    checkout_root: Path,
) -> AgencySnapshot:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_CATALOG_BYTES:
        raise AgencySnapshotError("Agency runtime catalog exceeds the supported bound")
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonstandard_json_value,
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateJsonObjectKey,
        _NonStandardJsonValue,
        RecursionError,
    ) as error:
        raise AgencySnapshotError("Agency runtime catalog is invalid JSON") from error
    if type(document) is not dict or set(document) != {
        "schema_version",
        "content_digest_schema",
        "agency",
        "distribution",
        "routing",
        "profiles",
    }:
        raise AgencySnapshotError("Agency runtime catalog has an unsupported shape")
    if document["schema_version"] != _CATALOG_SCHEMA_VERSION:
        raise AgencySnapshotError("Agency runtime catalog schema is unsupported")
    if document["content_digest_schema"] != _SUPPORTED_DIGEST_SCHEMA:
        raise AgencySnapshotError("Agency content digest schema is unsupported")

    agency = _exact_mapping(
        document["agency"],
        {"name", "version", "profile_count", "orchestrator"},
        "Agency catalog metadata",
    )
    if agency["name"] != "hermes-agency":
        raise AgencySnapshotError("Agency catalog name is unsupported")
    version = _version(agency["version"])
    profile_count = agency["profile_count"]
    if (
        type(profile_count) is not int
        or isinstance(profile_count, bool)
        or not 0 < profile_count <= _MAX_PROFILE_COUNT
    ):
        raise AgencySnapshotError("Agency profile count is invalid")
    orchestrator = agency["orchestrator"]
    if not _valid_profile_name(orchestrator):
        raise AgencySnapshotError("Agency orchestrator identity is invalid")

    distribution = _exact_mapping(
        document["distribution"],
        {"format", "profile_identity_field", "profile_path_template"},
        "Agency distribution metadata",
    )
    if distribution != {
        "format": "hermes-profile-distribution",
        "profile_identity_field": "name",
        "profile_path_template": "profiles/{name}",
    }:
        raise AgencySnapshotError("Agency distribution contract is unsupported")

    routing = _exact_mapping(
        document["routing"],
        {"selection_order", "live_presence_owner", "missing_presence_behavior"},
        "Agency routing metadata",
    )
    if (
        routing["selection_order"] != ["professional-profile", "eligible-node"]
        or routing["live_presence_owner"] != "hermes-fleet"
        or routing["missing_presence_behavior"] != "fleet-locate-or-place"
    ):
        raise AgencySnapshotError("Agency routing contract is unsupported")

    raw_profiles = document["profiles"]
    if type(raw_profiles) is not list or len(raw_profiles) != profile_count:
        raise AgencySnapshotError("Agency profile roster is inconsistent")
    profiles = tuple(
        _catalog_profile(item, agency_version=version) for item in raw_profiles
    )
    names = [profile.name for profile in profiles]
    if names != sorted(names) or len(names) != len(set(names)):
        raise AgencySnapshotError(
            "Agency profile roster is not deterministic and unique"
        )
    if orchestrator not in set(names):
        raise AgencySnapshotError("Agency orchestrator is absent from the roster")

    root = _safe_checkout_root(checkout_root)
    return AgencySnapshot(
        source=source,
        checkout_root=root,
        agency_version=version,
        orchestrator=orchestrator,
        profiles=profiles,
    )


def _catalog_profile(value: object, *, agency_version: str) -> _CatalogProfile:
    item = _exact_mapping(
        value,
        {
            "name",
            "version",
            "category",
            "priority",
            "description",
            "distribution_path",
            "content_digest",
            "capabilities",
        },
        "Agency profile",
    )
    name = item["name"]
    if not _valid_profile_name(name):
        raise AgencySnapshotError("Agency profile name is invalid")
    version = _version(item["version"])
    if version != agency_version:
        raise AgencySnapshotError(
            "Agency profile version disagrees with catalog version"
        )
    category = _safe_token(item["category"], _MAX_CATEGORY_BYTES, "profile category")
    priority = item["priority"]
    if priority not in {"standard", "backbone"}:
        raise AgencySnapshotError("Agency profile priority is invalid")
    description = item["description"]
    if (
        type(description) is not str
        or not description.strip()
        or len(description) > _MAX_DESCRIPTION_CHARS
    ):
        raise AgencySnapshotError("Agency profile description is invalid")
    distribution_path = item["distribution_path"]
    if distribution_path != f"profiles/{name}" or not _valid_relative_path(
        distribution_path
    ):
        raise AgencySnapshotError("Agency profile distribution path is invalid")
    digest = item["content_digest"]
    if not _valid_digest(digest):
        raise AgencySnapshotError("Agency profile content digest is invalid")
    capabilities = item["capabilities"]
    if (
        type(capabilities) is not list
        or not capabilities
        or len(capabilities) > _MAX_CAPABILITIES
    ):
        raise AgencySnapshotError("Agency profile capabilities are invalid")
    normalized_capabilities = tuple(
        _safe_token(value, _MAX_CAPABILITY_BYTES, "profile capability")
        for value in capabilities
    )
    if list(normalized_capabilities) != sorted(set(normalized_capabilities)):
        raise AgencySnapshotError("Agency profile capabilities are not canonical")
    return _CatalogProfile(
        name=name,
        version=version,
        category=category,
        priority=priority,
        description=description,
        distribution_path=distribution_path,
        content_digest=digest,
        capabilities=normalized_capabilities,
    )


def _run_catalog(checkout: Path, *, timeout_seconds: float) -> bytes:
    script = checkout / "catalog.py"
    try:
        if script.is_symlink() or not script.is_file():
            raise AgencySnapshotError("Agency runtime catalog generator is missing")
        completed = subprocess.run(
            [sys.executable, "-I", str(script), "--compact"],
            cwd=checkout,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AgencySnapshotError("Agency runtime catalog generation failed") from error
    if completed.returncode != 0:
        raise AgencySnapshotError("Agency runtime catalog generation failed")
    if not completed.stdout or len(completed.stdout) > _MAX_CATALOG_BYTES:
        raise AgencySnapshotError("Agency runtime catalog exceeds the supported bound")
    return completed.stdout


def _run_git_quiet(
    command: list[str], *, timeout_seconds: float, environment: dict[str, str]
) -> None:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AgencySnapshotError("pinned Agency git operation failed") from error
    if completed.returncode != 0:
        raise AgencySnapshotError("pinned Agency git operation failed")


def _run_git_text(
    command: list[str], *, timeout_seconds: float, environment: dict[str, str]
) -> str:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AgencySnapshotError("pinned Agency git verification failed") from error
    if completed.returncode != 0 or len(completed.stdout) > _MAX_GIT_OUTPUT_BYTES:
        raise AgencySnapshotError("pinned Agency git verification failed")
    try:
        value = completed.stdout.decode("ascii").strip()
    except UnicodeError as error:
        raise AgencySnapshotError("pinned Agency git verification failed") from error
    if not _valid_git_object_id(value):
        raise AgencySnapshotError(
            "pinned Agency git verification returned invalid identity"
        )
    return value


def _safe_checkout_root(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise AgencySnapshotError("Agency checkout root is invalid")
    try:
        if path.is_symlink() or not path.is_dir():
            raise AgencySnapshotError("Agency checkout root is invalid")
        return path.resolve(strict=True)
    except OSError as error:
        raise AgencySnapshotError("Agency checkout root is invalid") from error


def _safe_distribution_directory(root: Path, relative: str) -> Path:
    if not _valid_relative_path(relative):
        raise AgencySnapshotError("Agency profile distribution path is invalid")
    root = _safe_checkout_root(root)
    candidate = root
    try:
        for part in PurePosixPath(relative).parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise AgencySnapshotError(
                    "Agency profile distribution path contains a symlink"
                )
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise AgencySnapshotError(
            "Agency profile distribution path cannot be resolved"
        ) from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise AgencySnapshotError(
            "Agency profile distribution path escapes checkout"
        ) from error
    if not resolved.is_dir():
        raise AgencySnapshotError("Agency profile distribution path is not a directory")
    return resolved


def _exact_mapping(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise AgencySnapshotError(f"{label} has an invalid shape")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKey
        result[key] = value
    return result


def _reject_nonstandard_json_value(_value: str) -> None:
    raise _NonStandardJsonValue


def _valid_git_object_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_profile_name(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= _MAX_PROFILE_NAME_BYTES
        and value not in {".", ".."}
        and all(
            character.isascii() and (character.isalnum() or character in "._-")
            for character in value
        )
    )


def _version(value: object) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= _MAX_PROFILE_VERSION_BYTES
        or value != value.strip()
        or any(
            not character.isascii()
            or character.isspace()
            or not 32 < ord(character) < 127
            for character in value
        )
    ):
        raise AgencySnapshotError("Agency version is invalid")
    return value


def _safe_token(value: object, maximum: int, label: str) -> str:
    if (
        type(value) is not str
        or not 0 < len(value) <= maximum
        or value != value.strip()
        or any(
            not character.isascii() or not (character.isalnum() or character in "._-")
            for character in value
        )
    ):
        raise AgencySnapshotError(f"Agency {label} is invalid")
    return value


def _valid_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_relative_path(value: object) -> bool:
    if type(value) is not str or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _bounded_timeout(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value <= 120
    ):
        raise AgencySnapshotError(f"{label} must be between 0 and 120 seconds")
    return float(value)
