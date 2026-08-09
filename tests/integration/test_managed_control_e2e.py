"""Real UDS acceptance coverage for Fleet's durable managed-control boundary."""

from __future__ import annotations

import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

SCHEMA = "fleet.managed-projection.v1"
_TIMEOUT_SECONDS = 0.5


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


def _request(socket_path: Path, request: dict[str, object]) -> dict[str, Any]:
    payload = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_TIMEOUT_SECONDS)
        client.connect(str(socket_path))
        client.sendall(struct.pack(">I", len(payload)) + payload)
        client.shutdown(socket.SHUT_WR)
        length = struct.unpack(">I", _read_exact(client, 4))[0]
        decoded = json.loads(_read_exact(client, length).decode("utf-8"))
    assert type(decoded) is dict
    return decoded


def _apply_document(
    *, operation: str, generation: str, digest: str
) -> dict[str, object]:
    document: dict[str, object] = {
        "source": "nodescale",
        "network_id": "network-e2e",
        "device_id": "device-e2e",
        "projection_generation": generation,
        "membership_generation": generation,
        "binding_generation": generation,
        "operation": operation,
        "generated_operations": ["fleet.health", "fleet.inventory"],
        "provenance": {
            "source": "nodescale",
            "network_id": "network-e2e",
            "device_id": "device-e2e",
            "snapshot": generation,
        },
    }
    if digest == "canonical":
        from hermes_fleet.managed_projection import canonical_content_hash

        document["content_hash"] = canonical_content_hash(document)
    else:
        document["content_hash"] = digest
    return document


def _apply(document: dict[str, object]) -> dict[str, object]:
    return {"schema": SCHEMA, "kind": "apply", "document": document}


def _inspect() -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "kind": "inspect",
        "selector": {
            "source": "nodescale",
            "network_id": "network-e2e",
            "device_id": "device-e2e",
        },
    }


def _start_server(tmp_path: Path, database_path: Path) -> tuple[Any, Path]:
    from hermes_fleet.local_control import LocalControlServer
    from hermes_fleet.managed_projection import ManagedProjectionStore

    socket_path = tmp_path / "managed-control.sock"
    server = LocalControlServer(
        socket_path=socket_path,
        allowed_uid=os.getuid(),
        managed_projection=ManagedProjectionStore(database_path),
        io_timeout_seconds=_TIMEOUT_SECONDS,
    )
    server.start()
    return server, socket_path


def _stop_server(server: Any, socket_path: Path) -> None:
    server.close()
    assert not socket_path.exists()


def test_managed_control_durably_replays_conflicts_restarts_and_tombstones(
    tmp_path,
) -> None:
    """The production UDS/server/store path is durable and restart-safe."""
    database_path = tmp_path / "managed-projection.sqlite3"
    upsert = _apply_document(operation="upsert", generation="1", digest="canonical")
    conflict = _apply_document(operation="upsert", generation="1", digest="canonical")
    conflict["generated_operations"] = ["fleet.health"]
    from hermes_fleet.managed_projection import canonical_content_hash

    conflict["content_hash"] = canonical_content_hash(conflict)
    disable = _apply_document(operation="disable", generation="2", digest="canonical")
    remove = _apply_document(operation="remove", generation="3", digest="canonical")

    server, socket_path = _start_server(tmp_path, database_path)
    try:
        capabilities = _request(socket_path, {"schema": SCHEMA, "kind": "capabilities"})
        applied = _request(socket_path, _apply(upsert))
        inspected = _request(socket_path, _inspect())
        replayed = _request(socket_path, _apply(upsert))
        conflicted = _request(socket_path, _apply(conflict))
    finally:
        _stop_server(server, socket_path)

    server, socket_path = _start_server(tmp_path, database_path)
    try:
        restarted = _request(socket_path, _inspect())
        disabled = _request(socket_path, _apply(disable))
        disabled_view = _request(socket_path, _inspect())
        removed = _request(socket_path, _apply(remove))
        tombstoned = _request(socket_path, _inspect())
    finally:
        _stop_server(server, socket_path)

    assert applied["ok"] is True, applied
    assert capabilities == {
        "schema": SCHEMA,
        "kind": "capabilities",
        "ok": True,
        "result": {"kinds": ["capabilities", "apply", "inspect"]},
    }
    assert applied["result"] == {"outcome": "applied"}
    assert inspected["result"] == {
        "generated": {
            "state": "active",
            "projection_generation": "1",
            "membership_generation": "1",
            "binding_generation": "1",
            "content_hash": upsert["content_hash"],
            "allowed_operations": ["fleet.health", "fleet.inventory"],
            "provenance": {
                "source": "nodescale",
                "network_id": "network-e2e",
                "device_id": "device-e2e",
                "snapshot": "1",
            },
        },
        "effective": {
            "state": "active",
            "allowed_operations": ["fleet.health", "fleet.inventory"],
            "operator_denied_operations": [],
        },
    }
    assert replayed["result"] == {"outcome": "already_applied"}
    assert conflicted["result"] == {"outcome": "conflict"}
    assert restarted == inspected
    assert disabled["result"] == {"outcome": "applied"}
    assert disabled_view["result"]["generated"]["state"] == "disabled"
    assert removed["result"] == {"outcome": "applied"}
    assert tombstoned["result"]["generated"]["state"] == "removed"
    assert tombstoned["result"]["generated"]["allowed_operations"] == []


def test_managed_control_rejects_bad_canonical_hash_without_durable_write(
    tmp_path,
) -> None:
    database_path = tmp_path / "managed-projection.sqlite3"
    document = _apply_document(operation="upsert", generation="1", digest="canonical")
    document["content_hash"] = (
        "0" * 64 if document["content_hash"] != "0" * 64 else "1" * 64
    )

    server, socket_path = _start_server(tmp_path, database_path)
    try:
        response = _request(socket_path, _apply(document))
        inspected = _request(socket_path, _inspect())
    finally:
        _stop_server(server, socket_path)

    assert response == {
        "schema": SCHEMA,
        "kind": "error",
        "ok": False,
        "error": "invalid_request",
    }
    assert inspected == {
        "schema": SCHEMA,
        "kind": "inspect",
        "ok": True,
        "result": {"generated": None, "effective": None},
    }
