from __future__ import annotations

import json
import os
import socket
import struct
from pathlib import Path

import pytest

from hermes_fleet.local_control import LocalControlServer
from hermes_fleet.principal_identity import get_local_peer_context

SCHEMA = "fleet.managed-projection.v1"


class _Projection:
    def __init__(self) -> None:
        self.peer_uids: list[int | None] = []

    def inspect(self, **_selector: object) -> dict[str, object]:
        context = get_local_peer_context()
        self.peer_uids.append(None if context is None else context.uid)
        return {"generated": None, "effective": None}

    def apply(self, **_document: object):  # pragma: no cover - not used here
        raise AssertionError("apply was not expected")


def _read_exact(client: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = client.recv(size - len(payload))
        if not chunk:
            raise EOFError("local-control response ended early")
        payload.extend(chunk)
    return bytes(payload)


def _inspect(path: Path) -> dict[str, object]:
    document = {
        "schema": SCHEMA,
        "kind": "inspect",
        "selector": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
        },
    }
    payload = json.dumps(document, separators=(",", ":")).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(struct.pack(">I", len(payload)) + payload)
        client.shutdown(socket.SHUT_WR)
        header = _read_exact(client, 4)
        length = struct.unpack(">I", header)[0]
        return json.loads(_read_exact(client, length).decode())


def test_local_control_dispatch_exposes_kernel_peer_uid_only_inside_request(
    tmp_path: Path,
) -> None:
    projection = _Projection()
    path = tmp_path / "fleet-control.sock"
    server = LocalControlServer(
        socket_path=path,
        allowed_uid=os.getuid(),
        managed_projection=projection,
    )
    server.start()
    try:
        response = _inspect(path)
    finally:
        server.close()

    assert response["ok"] is True
    assert projection.peer_uids == [os.getuid()]
    assert get_local_peer_context() is None


def test_local_control_wrong_uid_never_reaches_dispatch(tmp_path: Path) -> None:
    if os.getuid() == (1 << 31) - 1:
        pytest.skip("cannot construct a distinct uid")
    projection = _Projection()
    path = tmp_path / "fleet-control.sock"
    server = LocalControlServer(
        socket_path=path,
        allowed_uid=os.getuid() + 1,
        managed_projection=projection,
    )
    server.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            payload = b'{"schema":"fleet.managed-projection.v1","kind":"capabilities"}'
            client.sendall(struct.pack(">I", len(payload)) + payload)
            client.shutdown(socket.SHUT_WR)
            try:
                data = client.recv(1)
            except (ConnectionResetError, TimeoutError):
                data = b""
    finally:
        server.close()

    assert data == b""
    assert projection.peer_uids == []
    assert get_local_peer_context() is None
