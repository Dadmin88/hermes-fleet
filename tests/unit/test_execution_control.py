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
                header = connection.recv(4)
                length = struct.unpack("!I", header)[0]
                payload = b""
                while len(payload) < length:
                    payload += connection.recv(length - len(payload))
                captured.append(json.loads(payload))
                encoded = json.dumps(response, separators=(",", ":")).encode()
                connection.sendall(struct.pack("!I", len(encoded)) + encoded)
        path.unlink(missing_ok=True)

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(2)
    return thread


def instance() -> dict:
    return {
        "instance_id": "instance-1",
        "idempotency_key": "request-1",
        "recipe_hash": "sha256:" + "1" * 64,
        "capabilities_hash": "sha256:" + "2" * 64,
        "target": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "binding_generation": 7,
            "admission_generation": 9,
        },
        "generation": 1,
        "phase": {"kind": "reserved"},
        "created_at_ms": 1000,
        "updated_at_ms": 1000,
    }


def authorization() -> dict:
    return {
        "authenticated_sender": "requester-1",
        "requester": "requester-1",
        "operation": "fleet.hermes.run",
        "recipe_hash": "sha256:" + "1" * 64,
        "policy_digest": "sha256:" + "3" * 64,
        "deadline_ms": 2000,
        "secret_refs_digest": "sha256:" + "4" * 64,
    }


def test_client_reserves_admits_reads_and_transitions(tmp_path: Path) -> None:
    from hermes_fleet.execution_control import ExecutionControlClient

    path = tmp_path / "fleet.sock"
    captured: list[dict] = []
    admitted = {
        "schema": "fleet.execution-control.v1",
        "kind": "reserve_admit",
        "ok": True,
        "result": {
            "created": True,
            "instance": instance(),
            "decision": {
                "status": "admitted",
                "instance_id": "instance-1",
                "target": instance()["target"],
                "recipe_hash": "sha256:" + "1" * 64,
                "capabilities_hash": "sha256:" + "2" * 64,
                "operation": "fleet.hermes.run",
                "evaluated_at_ms": 1100,
            },
        },
    }
    thread = _serve_once(path, admitted, captured)
    client = ExecutionControlClient(socket_path=path)
    result = client.reserve_admit(
        instance(),
        authorization=authorization(),
        current_policy_digest="sha256:" + "3" * 64,
        current_capabilities_hash="sha256:" + "2" * 64,
        deadline_ms=2000,
    )
    thread.join(2)
    assert result == admitted["result"]
    assert captured[0]["kind"] == "reserve_admit"
    assert captured[0]["authorization"] == authorization()
    assert captured[0]["current_policy_digest"] == "sha256:" + "3" * 64
    assert captured[0]["current_capabilities_hash"] == "sha256:" + "2" * 64

    captured.clear()
    response = {
        "schema": "fleet.execution-control.v1",
        "kind": "get",
        "ok": True,
        "result": {"instance": instance()},
    }
    thread = _serve_once(path, response, captured)
    assert client.get("instance-1") == instance()
    thread.join(2)

    prepared = instance()
    prepared["generation"] = 2
    prepared["phase"] = {
        "kind": "prepared",
        "backend_kind": "fleet.dev/docker-oci",
        "realization_id": "container-1",
    }
    response = {
        "schema": "fleet.execution-control.v1",
        "kind": "transition",
        "ok": True,
        "result": {"instance": prepared},
    }
    thread = _serve_once(path, response, captured)
    assert (
        client.transition(
            "instance-1",
            expected_generation=1,
            phase=prepared["phase"],
        )
        == prepared
    )
    thread.join(2)


def test_client_preserves_typed_denial_without_treating_it_as_transport_failure(
    tmp_path: Path,
) -> None:
    from hermes_fleet.execution_control import ExecutionControlClient

    path = tmp_path / "fleet.sock"
    response = {
        "schema": "fleet.execution-control.v1",
        "kind": "reserve_admit",
        "ok": True,
        "result": {"decision": {"status": "stale_target"}},
    }
    thread = _serve_once(path, response, [])
    result = ExecutionControlClient(socket_path=path).reserve_admit(
        instance(),
        authorization=authorization(),
        current_policy_digest="sha256:" + "3" * 64,
        current_capabilities_hash="sha256:" + "2" * 64,
        deadline_ms=2000,
    )
    thread.join(2)
    assert result == {"decision": {"status": "stale_target"}}


@pytest.mark.parametrize(
    "decision",
    [
        {"status": "admitted"},
        {"status": "stale_target", "unexpected": True},
        {"status": []},
        {"status": {}},
    ],
)
def test_client_rejects_incomplete_or_extended_admission_decisions(
    tmp_path: Path, decision: dict
) -> None:
    from hermes_fleet.execution_control import ExecutionControlClient

    path = tmp_path / "fleet.sock"
    result = (
        {"created": True, "instance": instance(), "decision": decision}
        if decision["status"] == "admitted"
        else {"decision": decision}
    )
    response = {
        "schema": "fleet.execution-control.v1",
        "kind": "reserve_admit",
        "ok": True,
        "result": result,
    }
    thread = _serve_once(path, response, [])
    with pytest.raises(RuntimeError, match="invalid admission decision"):
        ExecutionControlClient(socket_path=path).reserve_admit(
            instance(),
            authorization=authorization(),
            current_policy_digest="sha256:" + "3" * 64,
            current_capabilities_hash="sha256:" + "2" * 64,
            deadline_ms=2000,
        )
    thread.join(2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_id", "instance-2"),
        ("target", {**instance()["target"], "admission_generation": 10}),
        ("recipe_hash", "sha256:" + "3" * 64),
        ("capabilities_hash", "sha256:" + "3" * 64),
        ("evaluated_at_ms", 2001),
    ],
)
def test_client_rejects_admission_decision_not_bound_to_request(
    tmp_path: Path, field: str, value: object
) -> None:
    from hermes_fleet.execution_control import ExecutionControlClient

    path = tmp_path / "fleet.sock"
    decision = {
        "status": "admitted",
        "instance_id": "instance-1",
        "target": instance()["target"],
        "recipe_hash": "sha256:" + "1" * 64,
        "capabilities_hash": "sha256:" + "2" * 64,
        "operation": "fleet.hermes.run",
        "evaluated_at_ms": 1100,
    }
    decision[field] = value
    response = {
        "schema": "fleet.execution-control.v1",
        "kind": "reserve_admit",
        "ok": True,
        "result": {
            "created": True,
            "instance": instance(),
            "decision": decision,
        },
    }
    thread = _serve_once(path, response, [])
    with pytest.raises(RuntimeError, match="invalid admission decision"):
        ExecutionControlClient(socket_path=path).reserve_admit(
            instance(),
            authorization=authorization(),
            current_policy_digest="sha256:" + "3" * 64,
            current_capabilities_hash="sha256:" + "2" * 64,
            deadline_ms=2000,
        )
    thread.join(2)


def test_client_rejects_invalid_inputs_and_backend_documents(tmp_path: Path) -> None:
    from hermes_fleet.execution_control import ExecutionControlClient

    path = tmp_path / "fleet.sock"
    client = ExecutionControlClient(socket_path=path)
    with pytest.raises(ValueError):
        client.get("bad id")
    with pytest.raises(ValueError):
        client.reserve_admit(
            instance(),
            authorization=authorization(),
            current_policy_digest="sha256:" + "3" * 64,
            current_capabilities_hash="not-a-hash",
            deadline_ms=2000,
        )

    response = {
        "schema": "fleet.execution-control.v1",
        "kind": "get",
        "ok": True,
        "result": {"instance": instance(), "extra": True},
    }
    thread = _serve_once(path, response, [])
    with pytest.raises(RuntimeError, match="invalid execution read"):
        client.get("instance-1")
    thread.join(2)


@pytest.mark.parametrize(
    "phase",
    [
        {"kind": "prepared"},
        {
            "kind": "running",
            "backend_kind": "fleet.dev/docker-oci",
            "realization_id": "container-1",
        },
        {"kind": "cleaned", "extra": True},
        {
            "kind": "indeterminate",
            "backend_kind": "docker-oci",
            "realization_id": "container-1",
            "keryx_task_id": None,
            "reason": "provider response unavailable",
        },
    ],
)
def test_client_rejects_structurally_invalid_phase_documents(
    tmp_path: Path, phase: dict
) -> None:
    from hermes_fleet.execution_control import ExecutionControlClient

    with pytest.raises(ValueError, match="phase"):
        ExecutionControlClient(socket_path=tmp_path / "fleet.sock").transition(
            "instance-1", expected_generation=1, phase=phase
        )
