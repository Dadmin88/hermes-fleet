"""Persistent Hermes-native Agent Instance lifecycle for Fleet vNext.

An Agent Instance is durable brain state.  Its stable identity is the stable
Agency profile identity (repository + profile name), deliberately excluding the
pinned base revision/version/content digest.  Runs, containers, approvals,
credentials, network/filesystem grants, and other temporary authority never live
in this profile.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

from .agency_materialization import ImmutableAgencyBundle, materialize_agency_bundle
from .profile_runtime import _load_model_config, _stage_model_config
from .recipes import ResolvedAgencyProfile

_METADATA_FILE = ".fleet-agent-instance.json"
_STATE_FILE = ".fleet-agent-state.json"
_LOCK_FILE = ".fleet-agent-state.lock"
_METADATA_SCHEMA = "fleet.agent-instance.v1"
_STATE_SCHEMA = "fleet.agent-state.v1"
_PROFILE_RE = re.compile(r"^fleet-agent-[0-9a-f]{24}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_METADATA_BYTES = 32 * 1024
_MAX_STATE_BYTES = 16 * 1024
_MAX_CONFIG_BYTES = 64 * 1024
_STATE_COMPONENTS = frozenset({"memory", "skills"})
_RESERVED_PROFILE_NAMES = frozenset(
    {
        ".env",
        ".fleet-execution-owner",
        ".fleet-execution-slot",
        ".fleet-run-authority",
        ".fleet-run-capsule.json",
    }
)
_RESERVED_CONFIG_KEYS = frozenset(
    {
        "approvalbudget",
        "containerid",
        "filesystemgrants",
        "fleetruntime",
        "hostbrokergrants",
        "networkgrants",
        "runauthority",
        "runcapsule",
        "secrethandles",
        "secretrefs",
    }
)


class AgentInstanceError(ValueError):
    """Persistent Agent Instance state is invalid or unsafe."""


class AgentInstanceUpgradeRequired(AgentInstanceError):
    """The immutable Agency base changed and needs an explicit upgrade flow."""


class AgentInstanceConfigurationChanged(AgentInstanceError):
    """The durable model/profile baseline changed outside an explicit update."""


class AgentInstanceConflict(AgentInstanceError):
    """A durable memory/skill generation changed before the requested mutation."""


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise AgentInstanceError(f"{label} is invalid")
    return value


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode()) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AgentInstanceError(f"{label} is invalid")
    return value


def _canonical_digest(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise AgentInstanceError(
            "Agent Instance value is not canonical JSON"
        ) from error
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _stable_identity(agent: ResolvedAgencyProfile) -> tuple[str, str]:
    if type(agent) is not ResolvedAgencyProfile:
        raise AgentInstanceError("resolved Agency profile is invalid")
    identity = {
        "kind": "agency_profile",
        "repository": agent.repository,
        "name": agent.name,
    }
    digest = _canonical_digest(identity)
    return digest, f"fleet-agent-{digest.removeprefix('sha256:')[:24]}"


@dataclass(frozen=True, slots=True)
class AgentInstanceBinding:
    instance_id: str
    profile: str
    agency_repository: str
    agency_name: str
    base_revision: str
    base_version: str
    base_content_digest: str
    model_baseline_digest: str
    profile_config_digest: str

    def __post_init__(self) -> None:
        _hash(self.instance_id, "Agent Instance ID")
        if type(self.profile) is not str or _PROFILE_RE.fullmatch(self.profile) is None:
            raise AgentInstanceError("Agent Instance profile name is invalid")
        for value, label, maximum in (
            (self.agency_repository, "Agency repository", 2048),
            (self.agency_name, "Agency profile name", 128),
            (self.base_revision, "Agency base revision", 128),
            (self.base_version, "Agency base version", 128),
        ):
            _bounded_text(value, label, maximum)
        _hash(self.base_content_digest, "Agency base content digest")
        _hash(self.model_baseline_digest, "Agent Instance model baseline digest")
        _hash(self.profile_config_digest, "Agent Instance profile config digest")
        expected_id, expected_profile = _stable_identity(
            ResolvedAgencyProfile(
                repository=self.agency_repository,
                revision=self.base_revision,
                name=self.agency_name,
                version=self.base_version,
                content_digest=self.base_content_digest,
            )
        )
        if self.instance_id != expected_id or self.profile != expected_profile:
            raise AgentInstanceError("Agent Instance stable identity is inconsistent")

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": _METADATA_SCHEMA,
            "instance_id": self.instance_id,
            "profile": self.profile,
            "agency_repository": self.agency_repository,
            "agency_name": self.agency_name,
            "base_revision": self.base_revision,
            "base_version": self.base_version,
            "base_content_digest": self.base_content_digest,
            "model_baseline_digest": self.model_baseline_digest,
            "profile_config_digest": self.profile_config_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> AgentInstanceBinding:
        if type(value) is not dict or set(value) != {
            "schema",
            "instance_id",
            "profile",
            "agency_repository",
            "agency_name",
            "base_revision",
            "base_version",
            "base_content_digest",
            "model_baseline_digest",
            "profile_config_digest",
        }:
            raise AgentInstanceError("Agent Instance metadata is invalid")
        if value.get("schema") != _METADATA_SCHEMA:
            raise AgentInstanceError("Agent Instance metadata schema is unsupported")
        return cls(
            instance_id=value["instance_id"],
            profile=value["profile"],
            agency_repository=value["agency_repository"],
            agency_name=value["agency_name"],
            base_revision=value["base_revision"],
            base_version=value["base_version"],
            base_content_digest=value["base_content_digest"],
            model_baseline_digest=value["model_baseline_digest"],
            profile_config_digest=value["profile_config_digest"],
        )


@dataclass(frozen=True, slots=True)
class AgentInstanceState:
    memory_generation: int = 0
    skills_generation: int = 0

    def __post_init__(self) -> None:
        for value, label in (
            (self.memory_generation, "memory generation"),
            (self.skills_generation, "skills generation"),
        ):
            if isinstance(value, bool) or type(value) is not int or value < 0:
                raise AgentInstanceError(f"Agent Instance {label} is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _STATE_SCHEMA,
            "memory_generation": self.memory_generation,
            "skills_generation": self.skills_generation,
        }

    @classmethod
    def from_dict(cls, value: object) -> AgentInstanceState:
        if type(value) is not dict or set(value) != {
            "schema",
            "memory_generation",
            "skills_generation",
        }:
            raise AgentInstanceError("Agent Instance state metadata is invalid")
        if value.get("schema") != _STATE_SCHEMA:
            raise AgentInstanceError("Agent Instance state schema is unsupported")
        return cls(
            memory_generation=value["memory_generation"],
            skills_generation=value["skills_generation"],
        )

    def generation(self, component: str) -> int:
        if component == "memory":
            return self.memory_generation
        if component == "skills":
            return self.skills_generation
        raise AgentInstanceError("Agent Instance state component is unsupported")

    def bump(self, component: str) -> AgentInstanceState:
        if component == "memory":
            return AgentInstanceState(
                memory_generation=self.memory_generation + 1,
                skills_generation=self.skills_generation,
            )
        if component == "skills":
            return AgentInstanceState(
                memory_generation=self.memory_generation,
                skills_generation=self.skills_generation + 1,
            )
        raise AgentInstanceError("Agent Instance state component is unsupported")


class AgentInstanceManager:
    """Create once, reopen safely, and never delete a persistent Hermes profile."""

    def __init__(self, *, profiles_root: Path, model_config_path: Path) -> None:
        if not isinstance(profiles_root, Path) or not profiles_root.is_absolute():
            raise AgentInstanceError("profiles root must be an absolute Path")
        if (
            not isinstance(model_config_path, Path)
            or not model_config_path.is_absolute()
        ):
            raise AgentInstanceError("model config path must be an absolute Path")
        self._profiles_root = profiles_root
        self._model_config_path = model_config_path
        self._local_locks_guard = threading.Lock()
        self._local_locks: dict[str, threading.RLock] = {}

    @property
    def profiles_root(self) -> Path:
        return self._profiles_root

    @staticmethod
    def identity_for(agent: ResolvedAgencyProfile) -> tuple[str, str]:
        return _stable_identity(agent)

    def ensure(self, bundle: ImmutableAgencyBundle) -> AgentInstanceBinding:
        if type(bundle) is not ImmutableAgencyBundle:
            raise AgentInstanceError("Agency bundle is invalid")
        model_config = _load_model_config(self._model_config_path)
        model_digest = _canonical_digest(model_config)
        instance_id, profile = _stable_identity(bundle.resolved)
        self._ensure_profiles_root()
        destination = self._profiles_root / profile
        if destination.exists() or destination.is_symlink():
            binding = self._read_binding(destination)
            self._validate_existing(
                destination,
                binding,
                bundle.resolved,
                instance_id=instance_id,
                profile=profile,
                model_digest=model_digest,
            )
            return binding

        staging = self._profiles_root / f".{profile}.creating-{uuid.uuid4().hex}"
        if staging.exists() or staging.is_symlink():
            raise AgentInstanceError("Agent Instance staging path is unavailable")
        try:
            materialize_agency_bundle(bundle, destination=staging)
            self._reject_reserved_profile_state(staging)
            _stage_model_config(staging, model_config)
            self._assert_config_has_no_run_state(staging / "config.yaml")
            config_digest = self._config_digest(staging / "config.yaml")
            binding = AgentInstanceBinding(
                instance_id=instance_id,
                profile=profile,
                agency_repository=bundle.resolved.repository,
                agency_name=bundle.resolved.name,
                base_revision=bundle.resolved.revision,
                base_version=bundle.resolved.version,
                base_content_digest=bundle.resolved.content_digest,
                model_baseline_digest=model_digest,
                profile_config_digest=config_digest,
            )
            self._write_json_file(staging / _METADATA_FILE, binding.to_dict())
            self._write_json_file(
                staging / _STATE_FILE,
                AgentInstanceState().to_dict(),
            )
            self._touch_lock_file(staging / _LOCK_FILE)
            staging.chmod(0o700)
            try:
                staging.replace(destination)
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                shutil.rmtree(staging, ignore_errors=True)
                existing = self._read_binding(destination)
                self._validate_existing(
                    destination,
                    existing,
                    bundle.resolved,
                    instance_id=instance_id,
                    profile=profile,
                    model_digest=model_digest,
                )
                return existing
            return binding
        except BaseException:
            if staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def open(self, agent: ResolvedAgencyProfile) -> AgentInstanceBinding:
        instance_id, profile = _stable_identity(agent)
        model_config = _load_model_config(self._model_config_path)
        model_digest = _canonical_digest(model_config)
        destination = self._profiles_root / profile
        binding = self._read_binding(destination)
        self._validate_existing(
            destination,
            binding,
            agent,
            instance_id=instance_id,
            profile=profile,
            model_digest=model_digest,
        )
        return binding

    def profile_path(self, binding: AgentInstanceBinding) -> Path:
        self._validate_binding_path(binding)
        return self._profiles_root / binding.profile

    def read_state(self, binding: AgentInstanceBinding) -> AgentInstanceState:
        destination = self.profile_path(binding)
        self._validate_binding_matches_disk(destination, binding)
        with self._locked_state(binding.profile):
            return self._read_state_file(destination / _STATE_FILE)

    @contextmanager
    def mutation_guard(
        self,
        binding: AgentInstanceBinding,
        *,
        component: str,
        expected_generation: int,
    ) -> Iterator[AgentInstanceState]:
        """Serialize one native memory/skill mutation and bump its generation.

        The guard stores no memory/skill content.  Callers perform the actual
        Hermes-native mutation while holding the guard.  On normal exit Fleet
        advances only the matching generation.  A conflicting generation fails
        before the caller receives the mutation window.
        """
        if component not in _STATE_COMPONENTS:
            raise AgentInstanceError("Agent Instance state component is unsupported")
        if (
            isinstance(expected_generation, bool)
            or type(expected_generation) is not int
            or expected_generation < 0
        ):
            raise AgentInstanceError("expected Agent Instance generation is invalid")
        destination = self.profile_path(binding)
        self._validate_binding_matches_disk(destination, binding)
        with self._locked_state(binding.profile):
            current = self._read_state_file(destination / _STATE_FILE)
            if current.generation(component) != expected_generation:
                raise AgentInstanceConflict(
                    f"Agent Instance {component} generation changed"
                )
            yield current
            self._write_json_file(
                destination / _STATE_FILE,
                current.bump(component).to_dict(),
            )

    def _ensure_profiles_root(self) -> None:
        self._profiles_root.mkdir(parents=True, exist_ok=True)
        if self._profiles_root.is_symlink() or not self._profiles_root.is_dir():
            raise AgentInstanceError("profiles root is unsafe")

    def _validate_binding_path(self, binding: AgentInstanceBinding) -> None:
        if type(binding) is not AgentInstanceBinding:
            raise AgentInstanceError("Agent Instance binding is invalid")
        expected_id, expected_profile = _stable_identity(
            ResolvedAgencyProfile(
                repository=binding.agency_repository,
                revision=binding.base_revision,
                name=binding.agency_name,
                version=binding.base_version,
                content_digest=binding.base_content_digest,
            )
        )
        if binding.instance_id != expected_id or binding.profile != expected_profile:
            raise AgentInstanceError("Agent Instance binding identity changed")

    def _validate_binding_matches_disk(
        self,
        destination: Path,
        binding: AgentInstanceBinding,
    ) -> None:
        observed = self._read_binding(destination)
        if observed != binding:
            raise AgentInstanceError("Agent Instance binding changed on disk")
        self._validate_profile_files(destination, binding)

    def _validate_existing(
        self,
        destination: Path,
        binding: AgentInstanceBinding,
        requested: ResolvedAgencyProfile,
        *,
        instance_id: str,
        profile: str,
        model_digest: str,
    ) -> None:
        if (
            binding.instance_id != instance_id
            or binding.profile != profile
            or binding.agency_repository != requested.repository
            or binding.agency_name != requested.name
        ):
            raise AgentInstanceError(
                "Agent Instance identity does not match requested Agency profile"
            )
        if (
            binding.base_revision != requested.revision
            or binding.base_version != requested.version
            or binding.base_content_digest != requested.content_digest
        ):
            raise AgentInstanceUpgradeRequired(
                "Agent Instance Agency base changed; explicit upgrade is required"
            )
        if binding.model_baseline_digest != model_digest:
            raise AgentInstanceConfigurationChanged(
                "Agent Instance model baseline changed; explicit update is required"
            )
        self._validate_profile_files(destination, binding)

    def _validate_profile_files(
        self,
        destination: Path,
        binding: AgentInstanceBinding,
    ) -> None:
        if destination.is_symlink() or not destination.is_dir():
            raise AgentInstanceError("Agent Instance profile is invalid")
        self._reject_reserved_profile_state(destination, allow_agent_metadata=True)
        config_path = destination / "config.yaml"
        self._assert_config_has_no_run_state(config_path)
        if self._config_digest(config_path) != binding.profile_config_digest:
            raise AgentInstanceConfigurationChanged(
                "Agent Instance profile config changed outside explicit update"
            )
        self._read_state_file(destination / _STATE_FILE)
        self._validate_lock_file(destination / _LOCK_FILE)

    @staticmethod
    def _reject_reserved_profile_state(
        destination: Path,
        *,
        allow_agent_metadata: bool = False,
    ) -> None:
        for name in _RESERVED_PROFILE_NAMES:
            candidate = destination / name
            if candidate.exists() or candidate.is_symlink():
                raise AgentInstanceError(
                    "persistent Agent Instance contains reserved run/credential state"
                )
        if not allow_agent_metadata:
            for name in (_METADATA_FILE, _STATE_FILE, _LOCK_FILE):
                candidate = destination / name
                if candidate.exists() or candidate.is_symlink():
                    raise AgentInstanceError(
                        "Agency bundle contains reserved Agent Instance metadata"
                    )

    @staticmethod
    def _assert_config_has_no_run_state(path: Path) -> None:
        payload = AgentInstanceManager._read_regular_file(
            path,
            maximum=_MAX_CONFIG_BYTES,
            label="Agent Instance config",
        )
        try:
            config = yaml.safe_load(payload)
        except (UnicodeError, yaml.YAMLError) as error:
            raise AgentInstanceError("Agent Instance config is invalid") from error
        if type(config) is not dict:
            raise AgentInstanceError("Agent Instance config must be an object")

        def walk(value: object) -> None:
            if type(value) is dict:
                for key, item in value.items():
                    if type(key) is not str:
                        raise AgentInstanceError("Agent Instance config key is invalid")
                    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                    if normalized in _RESERVED_CONFIG_KEYS:
                        raise AgentInstanceError(
                            "persistent Agent Instance config contains run-scoped state"
                        )
                    walk(item)
            elif type(value) is list:
                for item in value:
                    walk(item)

        walk(config)

    @staticmethod
    def _config_digest(path: Path) -> str:
        payload = AgentInstanceManager._read_regular_file(
            path,
            maximum=_MAX_CONFIG_BYTES,
            label="Agent Instance config",
        )
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _read_binding(destination: Path) -> AgentInstanceBinding:
        if destination.is_symlink() or not destination.is_dir():
            raise AgentInstanceError("persistent Agent Instance does not exist")
        payload = AgentInstanceManager._read_regular_file(
            destination / _METADATA_FILE,
            maximum=_MAX_METADATA_BYTES,
            label="Agent Instance metadata",
        )
        try:
            value = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise AgentInstanceError("Agent Instance metadata is unreadable") from error
        return AgentInstanceBinding.from_dict(value)

    @staticmethod
    def _read_state_file(path: Path) -> AgentInstanceState:
        payload = AgentInstanceManager._read_regular_file(
            path,
            maximum=_MAX_STATE_BYTES,
            label="Agent Instance state",
        )
        try:
            value = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise AgentInstanceError("Agent Instance state is unreadable") from error
        return AgentInstanceState.from_dict(value)

    @staticmethod
    def _read_regular_file(path: Path, *, maximum: int, label: str) -> bytes:
        try:
            info = path.lstat()
        except OSError as error:
            raise AgentInstanceError(f"{label} is unavailable") from error
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_size > maximum
        ):
            raise AgentInstanceError(f"{label} is unsafe")
        try:
            return path.read_bytes()
        except OSError as error:
            raise AgentInstanceError(f"{label} is unreadable") from error

    @staticmethod
    def _write_json_file(path: Path, value: object) -> None:
        payload = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as error:
            raise AgentInstanceError("Agent Instance metadata write failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _touch_lock_file(path: Path) -> None:
        descriptor = -1
        try:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            AgentInstanceManager._validate_lock_file(path)
        except OSError as error:
            raise AgentInstanceError(
                "Agent Instance lock file is unavailable"
            ) from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _validate_lock_file(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as error:
            raise AgentInstanceError(
                "Agent Instance lock file is unavailable"
            ) from error
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise AgentInstanceError("Agent Instance lock file is unsafe")

    def _local_lock(self, profile: str) -> threading.RLock:
        with self._local_locks_guard:
            lock = self._local_locks.get(profile)
            if lock is None:
                lock = threading.RLock()
                self._local_locks[profile] = lock
            return lock

    @contextmanager
    def _locked_state(self, profile: str) -> Iterator[None]:
        destination = self._profiles_root / profile
        lock_path = destination / _LOCK_FILE
        local = self._local_lock(profile)
        with local:
            descriptor = -1
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR | os.O_NOFOLLOW,
                )
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                ):
                    raise AgentInstanceError("Agent Instance lock file is unsafe")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            except OSError as error:
                raise AgentInstanceError("Agent Instance state lock failed") from error
            finally:
                if descriptor >= 0:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)
