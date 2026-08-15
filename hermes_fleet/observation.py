"""Lightweight local publisher for Rust Fleet node observations."""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._paths import is_concrete_path

_SCHEMA = "fleet.node-observation.v1"
_MAX_FRAME_BYTES = 32_768
_U32_MAX = (1 << 32) - 1
_U64_MAX = (1 << 64) - 1
_MAX_PROFILE_COUNT = 256
_MAX_PROFILE_NAME_BYTES = 128
_MAX_PROFILE_VERSION_BYTES = 128
_PROFILE_CONTENT_DIGEST_BYTES = 64
_READINESS_KEYS = frozenset(
    {
        "managed_state",
        "admission_generation",
        "alive",
        "fresh",
        "scheduler_ready",
        "observation_age_ms",
        "reasons",
        "last_observation",
        "capacity",
        "profiles",
        "resources",
    }
)
_READINESS_REASONS = frozenset(
    {
        "node_unknown",
        "node_not_active",
        "observation_missing",
        "observation_stale",
        "observation_time_invalid",
        "network_unreachable",
        "keryx_unavailable",
        "hermes_unavailable",
        "worker_unavailable",
        "no_worker_capacity",
    }
)


def normalize_readiness(value: object) -> dict[str, Any]:
    """Return one exact bounded readiness view or reject the nested contract."""
    if type(value) is not dict or set(value) != _READINESS_KEYS:
        raise ValueError("readiness view has invalid fields")
    managed_state = value.get("managed_state")
    admission_generation = value.get("admission_generation")
    alive = value.get("alive")
    fresh = value.get("fresh")
    scheduler_ready = value.get("scheduler_ready")
    observation_age_ms = value.get("observation_age_ms")
    reasons = value.get("reasons")
    if managed_state not in {"unknown", "active", "disabled", "removed"}:
        raise ValueError("readiness managed state is invalid")
    if managed_state == "unknown":
        if admission_generation is not None:
            raise ValueError("unknown node cannot have an admission generation")
    else:
        admission_generation = _bounded_int(
            admission_generation, minimum=1, maximum=_U64_MAX
        )
    if (
        type(alive) is not bool
        or type(fresh) is not bool
        or type(scheduler_ready) is not bool
    ):
        raise ValueError("readiness booleans are invalid")
    if observation_age_ms is not None:
        _bounded_int(observation_age_ms, minimum=0, maximum=_U64_MAX)
    if (
        type(reasons) is not list
        or len(reasons) > len(_READINESS_REASONS)
        or any(
            type(reason) is not str or reason not in _READINESS_REASONS
            for reason in reasons
        )
        or len(reasons) != len(set(reasons))
        or scheduler_ready != (not reasons)
        or alive != fresh
    ):
        raise ValueError("readiness reasons are invalid")

    last_observation = _normalize_last_observation(value.get("last_observation"))
    capacity = _normalize_capacity(value.get("capacity"))
    raw_profiles = value.get("profiles")
    profiles = None if raw_profiles is None else _normalize_profiles(raw_profiles)
    resources = _normalize_resources(value.get("resources"))
    observation_missing = last_observation is None
    if (
        observation_missing != (capacity is None)
        or observation_missing != (profiles is None)
        or observation_missing != (resources is None)
    ):
        raise ValueError("readiness observation fields disagree")
    if last_observation is None and (observation_age_ms is not None or fresh or alive):
        raise ValueError("missing readiness observation cannot be alive")
    if (
        last_observation is not None
        and last_observation["admission_generation"] != admission_generation
    ):
        raise ValueError("last observation admission generation is stale")

    return {
        "managed_state": managed_state,
        "admission_generation": admission_generation,
        "alive": alive,
        "fresh": fresh,
        "scheduler_ready": scheduler_ready,
        "observation_age_ms": observation_age_ms,
        "reasons": list(reasons),
        "last_observation": last_observation,
        "capacity": capacity,
        "profiles": profiles,
        "resources": resources,
    }


def _normalize_last_observation(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    expected = {
        "admission_generation",
        "observed_at_ms",
        "received_at_ms",
        "network",
        "keryx",
        "hermes",
        "worker",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("last observation has invalid fields")
    admission_generation = _bounded_int(
        value["admission_generation"], minimum=1, maximum=_U64_MAX
    )
    observed_at_ms = _bounded_int(value["observed_at_ms"], minimum=1, maximum=_U64_MAX)
    received_at_ms = _bounded_int(value["received_at_ms"], minimum=1, maximum=_U64_MAX)
    if value["network"] not in {"reachable", "unreachable"} or any(
        value[field] not in {"available", "unavailable"}
        for field in ("keryx", "hermes", "worker")
    ):
        raise ValueError("last observation availability is invalid")
    return {
        "admission_generation": admission_generation,
        "observed_at_ms": observed_at_ms,
        "received_at_ms": received_at_ms,
        "network": value["network"],
        "keryx": value["keryx"],
        "hermes": value["hermes"],
        "worker": value["worker"],
    }


def _normalize_capacity(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    expected = {"active_workers", "max_workers", "available_worker_slots"}
    if type(value) is not dict or set(value) != expected:
        raise ValueError("readiness capacity has invalid fields")
    active = _bounded_int(value["active_workers"], minimum=0, maximum=_U32_MAX)
    maximum = _bounded_int(value["max_workers"], minimum=1, maximum=_U32_MAX)
    available = _bounded_int(
        value["available_worker_slots"], minimum=0, maximum=_U32_MAX
    )
    if active > maximum or available != maximum - active:
        raise ValueError("readiness capacity is inconsistent")
    return {
        "active_workers": active,
        "max_workers": maximum,
        "available_worker_slots": available,
    }


def _normalize_resources(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    expected = {"cpu", "ram", "swap", "disk", "gpu"}
    if type(value) is not dict or set(value) != expected:
        raise ValueError("readiness resources have invalid fields")
    cpu = _normalize_cpu(value["cpu"])
    ram = _normalize_bytes(value["ram"], allow_zero_total=False)
    swap = _normalize_bytes(value["swap"], allow_zero_total=True)
    disk = _normalize_bytes(value["disk"], allow_zero_total=False)
    gpu = _normalize_gpu(value["gpu"])
    return {"cpu": cpu, "ram": ram, "swap": swap, "disk": disk, "gpu": gpu}


def _normalize_cpu(value: object) -> dict[str, int | None] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"logical_cores", "load_basis_points"}:
        raise ValueError("readiness CPU fields are invalid")
    cores = _bounded_int(value["logical_cores"], minimum=1, maximum=65_535)
    load = value["load_basis_points"]
    if load is not None:
        load = _bounded_int(load, minimum=0, maximum=10_000)
    return {"logical_cores": cores, "load_basis_points": load}


def _normalize_bytes(value: object, *, allow_zero_total: bool) -> dict[str, int] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"total_bytes", "available_bytes"}:
        raise ValueError("readiness byte-capacity fields are invalid")
    total = _bounded_int(
        value["total_bytes"], minimum=0 if allow_zero_total else 1, maximum=_U64_MAX
    )
    available = _bounded_int(value["available_bytes"], minimum=0, maximum=_U64_MAX)
    if available > total:
        raise ValueError("readiness byte capacity is inconsistent")
    return {"total_bytes": total, "available_bytes": available}


def _normalize_gpu(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"present", "vram"}:
        raise ValueError("readiness GPU fields are invalid")
    present = value["present"]
    if type(present) is not bool:
        raise ValueError("readiness GPU presence is invalid")
    vram = _normalize_bytes(value["vram"], allow_zero_total=False)
    if not present and vram is not None:
        raise ValueError("absent GPU cannot report VRAM")
    return {"present": present, "vram": vram}


def _normalize_profiles(value: object) -> list[dict[str, str]]:
    if type(value) is not list or len(value) > _MAX_PROFILE_COUNT:
        raise ValueError("profile inventory is invalid")
    normalized: list[dict[str, str]] = []
    previous_name: str | None = None
    for item in value:
        if type(item) is not dict or set(item) not in (
            {"name", "version"},
            {"name", "version", "content_digest"},
        ):
            raise ValueError("profile presence fields are invalid")
        name = item["name"]
        version = item["version"]
        content_digest = item.get("content_digest")
        if (
            type(name) is not str
            or not 0 < len(name) <= _MAX_PROFILE_NAME_BYTES
            or name in {".", ".."}
            or any(
                not (
                    character.isascii() and (character.isalnum() or character in "._-")
                )
                for character in name
            )
            or type(version) is not str
            or not 0 < len(version) <= _MAX_PROFILE_VERSION_BYTES
            or any(
                not character.isascii()
                or character.isspace()
                or not 32 < ord(character) < 127
                for character in version
            )
            or (
                content_digest is not None
                and (
                    type(content_digest) is not str
                    or len(content_digest) != _PROFILE_CONTENT_DIGEST_BYTES
                    or any(
                        character not in "0123456789abcdef"
                        for character in content_digest
                    )
                )
            )
            or (previous_name is not None and name <= previous_name)
        ):
            raise ValueError("profile presence identity is invalid")
        profile = {"name": name, "version": version}
        if content_digest is not None:
            profile["content_digest"] = content_digest
        normalized.append(profile)
        previous_name = name
    return normalized


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError("readiness integer is outside the supported range")
    return value


class ObservationClient:
    """Publish and inspect one managed node through Fleet's local Rust control API."""

    def __init__(
        self,
        *,
        socket_path: Path,
        network_id: str,
        device_id: str,
        timeout_seconds: float = 2.0,
    ) -> None:
        if not is_concrete_path(socket_path) or not socket_path.is_absolute():
            raise ValueError("observation socket must be an absolute Path")
        _identifier(network_id)
        _identifier(device_id)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or timeout_seconds <= 0
            or timeout_seconds > 30
        ):
            raise ValueError("observation timeout must be between 0 and 30 seconds")
        self._socket_path = socket_path
        self._selector = {
            "source": "nodescale",
            "network_id": network_id,
            "device_id": device_id,
        }
        self._timeout_seconds = float(timeout_seconds)
        self._timestamp_lock = threading.Lock()
        self._last_observed_at_ms = 0

    def publish(self, observation: dict[str, Any]) -> str:
        """Publish one typed sample; identity remains outside the telemetry object."""
        if type(observation) is not dict or {
            "source",
            "network_id",
            "device_id",
        } & set(observation):
            raise ValueError("observation must not contain identity fields")
        candidate = dict(observation)
        timestamp = candidate.get("observed_at_ms")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp <= 0
        ):
            raise ValueError("observation timestamp is invalid")
        with self._timestamp_lock:
            if timestamp == self._last_observed_at_ms:
                timestamp += 1
            self._last_observed_at_ms = timestamp
        candidate["observed_at_ms"] = timestamp
        result = self._request(
            "observe",
            {
                "selector": self._selector,
                "observation": candidate,
            },
        )
        outcome = result.get("outcome")
        if outcome not in {"recorded", "already_recorded", "stale", "conflict"}:
            raise RuntimeError("Fleet returned an invalid observation outcome")
        return outcome

    def inspect(self) -> dict[str, Any]:
        """Return the exact Rust-derived local readiness view."""
        try:
            return normalize_readiness(
                self._request("inspect_observation", {"selector": self._selector})
            )
        except ValueError as error:
            raise RuntimeError("Fleet returned an invalid readiness view") from error

    def admission_generation(self) -> int:
        """Capture the active managed admission generation before sampling."""
        readiness = self.inspect()
        generation = readiness["admission_generation"]
        if readiness["managed_state"] != "active" or type(generation) is not int:
            raise RuntimeError("Fleet node is not actively admitted")
        return generation

    def _request(self, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
        request = {"schema": _SCHEMA, "kind": kind, **fields}
        payload = json.dumps(
            request,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not payload or len(payload) > _MAX_FRAME_BYTES:
            raise ValueError("observation request exceeds the Fleet frame bound")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._timeout_seconds)
            connection.connect(str(self._socket_path))
            connection.sendall(struct.pack("!I", len(payload)) + payload)
            connection.shutdown(socket.SHUT_WR)
            length = struct.unpack("!I", _recv_exact(connection, 4))[0]
            if not 1 <= length <= _MAX_FRAME_BYTES:
                raise RuntimeError("Fleet returned an invalid observation frame")
            document = json.loads(_recv_exact(connection, length))
        if (
            type(document) is not dict
            or document.get("schema") != _SCHEMA
            or document.get("kind") != kind
            or document.get("ok") is not True
            or type(document.get("result")) is not dict
        ):
            raise RuntimeError("Fleet rejected or malformed the observation request")
        return document["result"]


def build_observation(
    *,
    admission_generation: int,
    hermes_health: object,
    active_workers: int,
    max_workers: int,
    now_ms: Callable[[], int] | None = None,
    network_reachable: bool,
    keryx_available: bool,
    worker_available: bool,
    profiles: object | None = None,
) -> dict[str, Any]:
    """Build the narrow scheduling sample owned by the Fleet worker."""
    if (
        isinstance(admission_generation, bool)
        or not isinstance(admission_generation, int)
        or not 0 < admission_generation <= _U64_MAX
        or isinstance(active_workers, bool)
        or not isinstance(active_workers, int)
        or isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or max_workers <= 0
        or active_workers < 0
        or active_workers > max_workers
    ):
        raise ValueError("observation generation or worker capacity is invalid")
    if any(
        type(value) is not bool
        for value in (network_reachable, keryx_available, worker_available)
    ):
        raise ValueError("operational availability flags must be booleans")
    timestamp = (now_ms or (lambda: int(time.time() * 1_000)))()
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise ValueError("observation timestamp is invalid")
    hermes_available = (
        type(hermes_health) is dict
        and hermes_health.get("api") == "healthy"
        and all(
            hermes_health.get(field) is True
            for field in (
                "run_submission",
                "run_status",
                "run_stop",
                "run_finalize",
                "run_approval_budget",
                "run_tool_evidence",
                "run_command_evidence",
            )
        )
    )
    sample = {
        "admission_generation": admission_generation,
        "observed_at_ms": timestamp,
        "network": "reachable" if network_reachable else "unreachable",
        "keryx": "available" if keryx_available else "unavailable",
        "hermes": "available" if hermes_available else "unavailable",
        "worker": "available" if worker_available else "unavailable",
        "capacity": {
            "active_workers": active_workers,
            "max_workers": max_workers,
        },
        "resources": linux_resources(),
    }
    if profiles is not None:
        sample["profiles"] = _normalize_profiles(profiles)
    return sample


def linux_resources() -> dict[str, Any]:
    """Collect bounded Linux resources; absent optional telemetry is acceptable."""
    resources: dict[str, Any] = {}
    logical_cores = os.cpu_count()
    if type(logical_cores) is int and 0 < logical_cores <= 65_535:
        cpu: dict[str, int] = {"logical_cores": logical_cores}
        try:
            one_minute_load = os.getloadavg()[0]
        except OSError:
            pass
        else:
            if one_minute_load >= 0:
                cpu["load_basis_points"] = min(
                    10_000, round(one_minute_load * 10_000 / logical_cores)
                )
        resources["cpu"] = cpu

    try:
        memory = _read_meminfo()
        total = memory["MemTotal"] * 1_024
        available = memory["MemAvailable"] * 1_024
        if total > 0 and 0 <= available <= total:
            resources["ram"] = {
                "total_bytes": total,
                "available_bytes": available,
            }
        swap_total = memory.get("SwapTotal")
        swap_free = memory.get("SwapFree")
        if (
            type(swap_total) is int
            and type(swap_free) is int
            and swap_total >= 0
            and 0 <= swap_free <= swap_total
        ):
            resources["swap"] = {
                "total_bytes": swap_total * 1_024,
                "available_bytes": swap_free * 1_024,
            }
    except (KeyError, OSError, ValueError):
        pass

    try:
        resources["disk"] = _disk_capacity()
    except OSError:
        pass
    try:
        gpu = _gpu_observation()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    else:
        if gpu is not None:
            resources["gpu"] = gpu
    return resources


def _read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    with Path("/proc/meminfo").open(encoding="utf-8") as handle:
        for line in handle:
            key, separator, raw = line.partition(":")
            if not separator:
                continue
            fields = raw.split()
            if not fields:
                continue
            values[key] = int(fields[0])
    return values


def _disk_capacity() -> dict[str, int]:
    filesystem = os.statvfs("/")
    total = filesystem.f_blocks * filesystem.f_frsize
    available = filesystem.f_bavail * filesystem.f_frsize
    if total <= 0 or available < 0 or available > total:
        raise OSError("invalid filesystem capacity")
    return {"total_bytes": total, "available_bytes": available}


def _gpu_observation() -> dict[str, Any] | None:
    """Return aggregate NVIDIA VRAM when the bounded system probe is available."""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    completed = subprocess.run(
        [
            executable,
            "--query-gpu=memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout) > 4_096:
        return None
    rows = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not rows or len(rows) > 32:
        return None
    total_mib = 0
    available_mib = 0
    mib = 1_048_576
    max_total_mib = ((1 << 64) - 1) // mib
    for row in rows:
        fields = [field.strip() for field in row.split(",")]
        if len(fields) != 2:
            return None
        total, available = (int(field) for field in fields)
        if (
            total <= 0
            or total > max_total_mib
            or available < 0
            or available > total
            or total_mib + total > max_total_mib
        ):
            return None
        total_mib += total
        available_mib += available
    return {
        "present": True,
        "vram": {
            "total_bytes": total_mib * mib,
            "available_bytes": available_mib * mib,
        },
    }


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise RuntimeError("Fleet closed the observation connection early")
        result.extend(chunk)
    return bytes(result)


def _identifier(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or value.strip() != value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("managed observation identity is invalid")
    return value
