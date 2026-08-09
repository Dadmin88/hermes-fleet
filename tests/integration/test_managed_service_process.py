"""Real process lifecycle coverage for the managed-projection service."""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from hermes_fleet.managed_projection import canonical_content_hash

SCHEMA = "fleet.managed-projection.v1"
_REQUEST_TIMEOUT_SECONDS = 1.0
_STARTUP_TIMEOUT_SECONDS = 10.0
_STOP_TIMEOUT_SECONDS = 5.0


def _read_exact(client: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = client.recv(remaining)
        if not chunk:
            raise EOFError("connection closed before a complete response frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _request(socket_path: Path, request: Mapping[str, object]) -> dict[str, Any]:
    payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_REQUEST_TIMEOUT_SECONDS)
        client.connect(str(socket_path))
        client.sendall(struct.pack(">I", len(payload)) + payload)
        client.shutdown(socket.SHUT_WR)
        length = struct.unpack(">I", _read_exact(client, 4))[0]
        decoded = json.loads(_read_exact(client, length).decode("utf-8"))
    assert type(decoded) is dict
    return decoded


def _apply_request() -> dict[str, object]:
    document: dict[str, object] = {
        "source": "nodescale",
        "network_id": "network-process",
        "device_id": "device-process",
        "projection_generation": "1",
        "membership_generation": "1",
        "binding_generation": "1",
        "operation": "upsert",
        "generated_operations": ["fleet.health", "fleet.inventory"],
        "provenance": {
            "source": "nodescale",
            "network_id": "network-process",
            "device_id": "device-process",
            "snapshot": "1",
        },
    }
    document["content_hash"] = canonical_content_hash(document)
    return {"schema": SCHEMA, "kind": "apply", "document": document}


def _inspect_request() -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "kind": "inspect",
        "selector": {
            "source": "nodescale",
            "network_id": "network-process",
            "device_id": "device-process",
        },
    }


def _service_command(
    socket_path: Path, database_path: Path, *, socket_gid: int | None = None
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "hermes_fleet.managed_service",
        "--socket",
        str(socket_path),
        "--database",
        str(database_path),
        "--allowed-uid",
        str(os.getuid()),
    ]
    if socket_gid is not None:
        command.extend(("--socket-gid", str(socket_gid)))
    command.extend(
        (
            "--shutdown-timeout",
            "2",
            "--log-level",
            "DEBUG",
        )
    )
    return command


def _start_service(
    socket_path: Path, database_path: Path, *, socket_gid: int | None = None
) -> subprocess.Popen[str]:
    repository = Path(__file__).resolve().parents[2]
    return subprocess.Popen(
        _service_command(socket_path, database_path, socket_gid=socket_gid),
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_ready(socket_path: Path, process: subprocess.Popen[str]) -> None:
    request = {"schema": SCHEMA, "kind": "capabilities"}
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    last_error: OSError | EOFError | json.JSONDecodeError | struct.error | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            pytest.fail(
                "managed service exited before UDS readiness "
                f"(returncode={process.returncode}, stdout={stdout!r}, "
                f"stderr={stderr!r})"
            )
        try:
            response = _request(socket_path, request)
        except (OSError, EOFError, json.JSONDecodeError, struct.error) as error:
            last_error = error
            time.sleep(0.02)
            continue
        assert response == {
            "schema": SCHEMA,
            "kind": "capabilities",
            "ok": True,
            "result": {"kinds": ["capabilities", "apply", "inspect"]},
        }
        return
    pytest.fail(f"managed service did not become ready: {last_error!r}")


def _socket_inodes(process_id: int) -> Iterator[str]:
    for descriptor in Path(f"/proc/{process_id}/fd").iterdir():
        try:
            target = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            yield target.removeprefix("socket:[").removesuffix("]")


def _network_listener_inodes(process_id: int) -> set[str]:
    socket_inodes = set(_socket_inodes(process_id))
    listeners: set[str] = set()
    for protocol in ("tcp", "tcp6"):
        table_path = Path(f"/proc/{process_id}/net/{protocol}")
        for line in table_path.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A" and fields[9] in socket_inodes:
                listeners.add(fields[9])
    return listeners


def _stop_service(process: subprocess.Popen[str], socket_path: Path) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=_STOP_TIMEOUT_SECONDS)
    assert process.returncode == 0, (stdout, stderr)
    assert not socket_path.exists()


def _private_parent(path: Path, mode: int) -> Path:
    path.mkdir(mode=mode)
    path.chmod(mode)
    return path


@pytest.mark.parametrize(
    "unsafe_parent",
    ("world_socket", "world_database", "missing_database", "symlink_socket"),
)
def test_managed_service_rejects_unsafe_preprovisioned_parent_before_creation(
    tmp_path: Path, unsafe_parent: str
) -> None:
    socket_parent = _private_parent(tmp_path / "socket-parent", 0o700)
    database_parent = tmp_path / "database-parent"
    if unsafe_parent != "missing_database":
        _private_parent(database_parent, 0o700)
    if unsafe_parent == "world_socket":
        socket_parent.chmod(0o777)
    elif unsafe_parent == "world_database":
        database_parent.chmod(0o755)
    else:
        socket_target = _private_parent(tmp_path / "socket-target", 0o700)
        socket_link = tmp_path / "socket-link"
        socket_link.symlink_to(socket_target, target_is_directory=True)
        socket_parent = socket_link
    socket_path = socket_parent / "managed-control.sock"
    database_path = database_parent / "managed-projection.sqlite3"

    process = _start_service(socket_path, database_path)
    stdout, stderr = process.communicate(timeout=_STOP_TIMEOUT_SECONDS)

    assert process.returncode == 1, (stdout, stderr)
    assert not socket_path.exists()
    assert not database_path.exists()
    if unsafe_parent == "missing_database":
        assert not database_parent.exists()


def test_managed_service_accepts_explicit_group_socket_gid(tmp_path: Path) -> None:
    socket_parent = _private_parent(tmp_path / "socket-group-parent", 0o750)
    database_parent = _private_parent(tmp_path / "database-parent", 0o700)
    socket_path = socket_parent / "managed-control.sock"
    database_path = database_parent / "managed-projection.sqlite3"

    process = _start_service(socket_path, database_path, socket_gid=os.getgid())
    try:
        _wait_for_ready(socket_path, process)
        socket_identity = socket_path.lstat()
        database_identity = database_path.lstat()
        assert stat.S_ISSOCK(socket_identity.st_mode)
        assert stat.S_IMODE(socket_identity.st_mode) == 0o660
        assert socket_identity.st_uid == os.geteuid()
        assert socket_identity.st_gid == os.getgid()
        assert stat.S_IMODE(database_identity.st_mode) == 0o600
        assert database_identity.st_uid == os.geteuid()
    finally:
        _stop_service(process, socket_path)


def test_systemd_user_unit_defaults_same_uid_and_documents_cross_uid_drop_in() -> None:
    repository = Path(__file__).resolve().parents[2]
    unit = (
        repository / "ops" / "systemd" / "fleet-managed-projection.service"
    ).read_text(encoding="utf-8")
    documentation = (repository / "docs" / "managed-projection-v1.md").read_text(
        encoding="utf-8"
    )

    assert "FLEET_MANAGED_PROJECTION_SOCKET_GID" not in unit
    assert "--socket-gid" not in unit
    assert "fleet-managed-projection.service.d/cross-uid.conf" in documentation
    assert "ExecStart=" in documentation
    assert "--socket-gid ${FLEET_MANAGED_PROJECTION_SOCKET_GID}" in documentation


def test_managed_service_cli_serves_durably_over_uds_and_stops_on_sigterm(
    tmp_path: Path,
) -> None:
    """The executable is UDS-only, durable across restart, and SIGTERM-clean."""
    socket_path = (
        _private_parent(tmp_path / "socket-parent", 0o700) / "managed-control.sock"
    )
    database_path = (
        _private_parent(tmp_path / "database-parent", 0o700)
        / "managed-projection.sqlite3"
    )
    assert socket_path.is_absolute()
    assert database_path.is_absolute()
    first_process = _start_service(socket_path, database_path)
    try:
        _wait_for_ready(socket_path, first_process)
        socket_identity = socket_path.stat()
        assert stat.S_ISSOCK(socket_identity.st_mode)
        assert stat.S_IMODE(socket_identity.st_mode) == 0o600
        assert socket_identity.st_gid == os.getgid()
        assert not _network_listener_inodes(first_process.pid)

        apply_request = _apply_request()
        applied = _request(socket_path, apply_request)
        inspected = _request(socket_path, _inspect_request())
    finally:
        _stop_service(first_process, socket_path)

    second_process = _start_service(socket_path, database_path)
    try:
        _wait_for_ready(socket_path, second_process)
        restarted = _request(socket_path, _inspect_request())
    finally:
        _stop_service(second_process, socket_path)

    document = apply_request["document"]
    assert type(document) is dict
    content_hash = document["content_hash"]
    assert type(content_hash) is str
    expected_inspected_result = {
        "generated": {
            "state": "active",
            "projection_generation": "1",
            "membership_generation": "1",
            "binding_generation": "1",
            "content_hash": content_hash,
            "allowed_operations": ["fleet.health", "fleet.inventory"],
            "provenance": {
                "source": "nodescale",
                "network_id": "network-process",
                "device_id": "device-process",
                "snapshot": "1",
            },
        },
        "effective": {
            "state": "active",
            "allowed_operations": ["fleet.health", "fleet.inventory"],
            "operator_denied_operations": [],
        },
    }
    assert applied == {
        "schema": SCHEMA,
        "kind": "apply",
        "ok": True,
        "result": {"outcome": "applied"},
    }
    assert inspected == {
        "schema": SCHEMA,
        "kind": "inspect",
        "ok": True,
        "result": expected_inspected_result,
    }
    assert restarted == inspected
