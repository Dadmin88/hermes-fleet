import json
import socket
import struct
import threading
from pathlib import Path

import pytest


def _serve_once(path: Path, response: dict, captured: list[dict]) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            ready.set()
            connection, _ = listener.accept()
            with connection:
                length = struct.unpack("!I", connection.recv(4))[0]
                payload = b""
                while len(payload) < length:
                    payload += connection.recv(length - len(payload))
                captured.append(json.loads(payload))
                encoded = json.dumps(response, separators=(",", ":")).encode()
                connection.sendall(struct.pack("!I", len(encoded)) + encoded)

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(2)
    return thread


def test_observation_client_keeps_identity_outside_typed_sample(tmp_path) -> None:
    from hermes_fleet.observation import ObservationClient

    path = tmp_path / "fleet.sock"
    captured: list[dict] = []
    response = {
        "schema": "fleet.node-observation.v1",
        "kind": "observe",
        "ok": True,
        "result": {"outcome": "recorded"},
    }
    thread = _serve_once(path, response, captured)
    client = ObservationClient(
        socket_path=path,
        network_id="network-1",
        device_id="device-1",
    )
    sample = {
        "admission_generation": 1,
        "observed_at_ms": 1_000,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 1},
        "resources": {},
    }

    assert client.publish(sample) == "recorded"
    thread.join(2)
    request = captured[0]
    assert request["selector"] == {
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": "device-1",
    }
    assert request["observation"] == sample
    assert not ({"source", "network_id", "device_id"} & set(request["observation"]))


def test_observation_client_returns_bounded_readiness_inspection(tmp_path) -> None:
    from hermes_fleet.observation import ObservationClient

    path = tmp_path / "fleet.sock"
    captured: list[dict] = []
    readiness = {
        "managed_state": "active",
        "admission_generation": 7,
        "alive": True,
        "fresh": True,
        "scheduler_ready": True,
        "observation_age_ms": 10,
        "reasons": [],
        "last_observation": {
            "admission_generation": 7,
            "observed_at_ms": 1_000,
            "received_at_ms": 1_001,
            "network": "reachable",
            "keryx": "available",
            "hermes": "available",
            "worker": "available",
        },
        "capacity": {
            "active_workers": 0,
            "max_workers": 1,
            "available_worker_slots": 1,
        },
        "profiles": [{"name": "agency-backend-engineer", "version": "0.1.0"}],
        "resources": {
            "cpu": None,
            "ram": None,
            "swap": None,
            "disk": None,
            "gpu": None,
        },
    }
    thread = _serve_once(
        path,
        {
            "schema": "fleet.node-observation.v1",
            "kind": "inspect_observation",
            "ok": True,
            "result": readiness,
        },
        captured,
    )
    client = ObservationClient(
        socket_path=path,
        network_id="network-1",
        device_id="device-1",
    )

    assert client.inspect() == readiness
    thread.join(2)
    assert captured[0]["kind"] == "inspect_observation"


def test_readiness_profiles_distinguish_unknown_from_observed_empty() -> None:
    from hermes_fleet.observation import normalize_readiness

    missing = normalize_readiness(
        {
            "managed_state": "active",
            "admission_generation": 7,
            "alive": False,
            "fresh": False,
            "scheduler_ready": False,
            "observation_age_ms": None,
            "reasons": ["observation_missing"],
            "last_observation": None,
            "capacity": None,
            "profiles": None,
            "resources": None,
        }
    )
    assert missing["profiles"] is None

    observed = normalize_readiness(
        {
            "managed_state": "active",
            "admission_generation": 7,
            "alive": True,
            "fresh": True,
            "scheduler_ready": True,
            "observation_age_ms": 0,
            "reasons": [],
            "last_observation": {
                "admission_generation": 7,
                "observed_at_ms": 1_000,
                "received_at_ms": 1_001,
                "network": "reachable",
                "keryx": "available",
                "hermes": "available",
                "worker": "available",
            },
            "capacity": {
                "active_workers": 0,
                "max_workers": 1,
                "available_worker_slots": 1,
            },
            "profiles": [],
            "resources": {
                "cpu": None,
                "ram": None,
                "swap": None,
                "disk": None,
                "gpu": None,
            },
        }
    )
    assert observed["profiles"] == []


def test_readiness_normalizer_rejects_profile_presence_without_observation() -> None:
    from hermes_fleet.observation import normalize_readiness

    with pytest.raises(ValueError, match="observation fields disagree"):
        normalize_readiness(
            {
                "managed_state": "active",
                "admission_generation": 7,
                "alive": False,
                "fresh": False,
                "scheduler_ready": False,
                "observation_age_ms": None,
                "reasons": ["observation_missing"],
                "last_observation": None,
                "capacity": None,
                "profiles": [],
                "resources": None,
            }
        )


def test_readiness_normalizer_rejects_unknown_nested_fields() -> None:
    from hermes_fleet.observation import normalize_readiness

    readiness = {
        "managed_state": "unknown",
        "alive": False,
        "fresh": False,
        "scheduler_ready": False,
        "observation_age_ms": None,
        "reasons": ["node_unknown"],
        "last_observation": None,
        "capacity": None,
        "profiles": None,
        "resources": None,
        "extra": "not-owned",
    }

    with pytest.raises(ValueError, match="invalid fields"):
        normalize_readiness(readiness)


def test_build_observation_reports_worker_capacity_and_optional_linux_resources(
    monkeypatch,
) -> None:
    from hermes_fleet import observation

    monkeypatch.setattr(
        observation,
        "linux_resources",
        lambda: {
            "cpu": {"logical_cores": 8, "load_basis_points": 2500},
            "ram": {"total_bytes": 16000, "available_bytes": 8000},
        },
    )
    sample = observation.build_observation(
        admission_generation=7,
        hermes_health={
            "api": "healthy",
            "run_submission": True,
            "run_status": True,
            "run_stop": True,
            "run_finalize": True,
            "run_approval_budget": True,
            "run_tool_evidence": True,
        },
        active_workers=1,
        max_workers=2,
        now_ms=lambda: 5_000,
        network_reachable=True,
        keryx_available=True,
        worker_available=True,
    )

    assert sample == {
        "admission_generation": 7,
        "observed_at_ms": 5_000,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 1, "max_workers": 2},
        "resources": {
            "cpu": {"logical_cores": 8, "load_basis_points": 2500},
            "ram": {"total_bytes": 16000, "available_bytes": 8000},
        },
    }


def test_linux_resource_parser_tolerates_missing_gpu_and_zero_swap(monkeypatch) -> None:
    from hermes_fleet import observation

    monkeypatch.setattr(observation.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(observation.os, "getloadavg", lambda: (1.0, 0.5, 0.25))
    monkeypatch.setattr(
        observation,
        "_read_meminfo",
        lambda: {
            "MemTotal": 16,
            "MemAvailable": 8,
            "SwapTotal": 0,
            "SwapFree": 0,
        },
    )
    monkeypatch.setattr(
        observation,
        "_disk_capacity",
        lambda: {"total_bytes": 100, "available_bytes": 40},
    )
    monkeypatch.setattr(observation, "_gpu_observation", lambda: None)

    resources = observation.linux_resources()
    assert resources["cpu"] == {"logical_cores": 4, "load_basis_points": 2500}
    assert resources["ram"] == {"total_bytes": 16 * 1024, "available_bytes": 8 * 1024}
    assert resources["swap"] == {"total_bytes": 0, "available_bytes": 0}
    assert resources["disk"] == {"total_bytes": 100, "available_bytes": 40}
    assert "gpu" not in resources


def test_gpu_probe_reports_bounded_aggregate_vram(monkeypatch) -> None:
    import subprocess

    from hermes_fleet import observation

    monkeypatch.setattr(
        observation.shutil, "which", lambda _name: "/usr/bin/nvidia-smi"
    )

    def run(command, **kwargs):
        assert command == [
            "/usr/bin/nvidia-smi",
            "--query-gpu=memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ]
        assert kwargs["timeout"] == 2
        return subprocess.CompletedProcess(command, 0, "8192, 4096\n4096, 2048\n", "")

    monkeypatch.setattr(observation.subprocess, "run", run)

    assert observation._gpu_observation() == {
        "present": True,
        "vram": {
            "total_bytes": 12_288 * 1_048_576,
            "available_bytes": 6_144 * 1_048_576,
        },
    }


def test_observation_client_resets_timestamp_watermark_after_clock_regression(
    tmp_path, monkeypatch
) -> None:
    from hermes_fleet.observation import ObservationClient

    client = ObservationClient(
        socket_path=tmp_path / "fleet.sock",
        network_id="network-1",
        device_id="device-1",
    )
    observed: list[int] = []

    def request(_kind, fields):
        observed.append(fields["observation"]["observed_at_ms"])
        return {"outcome": "recorded"}

    monkeypatch.setattr(client, "_request", request)
    sample = {
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 1},
        "resources": {},
    }
    client.publish({**sample, "observed_at_ms": 100_000})
    client.publish({**sample, "observed_at_ms": 100_000})
    client.publish({**sample, "observed_at_ms": 10_000})

    assert observed == [100_000, 100_001, 10_000]
