from __future__ import annotations

import json
import os
import socket
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

SCHEMA = "fleet.managed-projection.v1"
HASH = "a" * 64


class _Projection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def apply(self, **document: object) -> SimpleNamespace:
        self.calls.append(("apply", document))
        return SimpleNamespace(outcome="applied")

    def inspect(self, **selector: object) -> dict[str, object]:
        self.calls.append(("inspect", selector))
        return {"generated": None, "effective": None}


def _request(
    client: socket.socket,
    document: str,
    *,
    trailing: bytes = b"",
    write_half_closed: bool = True,
) -> dict[str, object]:
    payload = document.encode("utf-8")
    client.sendall(struct.pack(">I", len(payload)) + payload + trailing)
    if write_half_closed:
        client.shutdown(socket.SHUT_WR)
    header = _read_exact(client, 4)
    length = struct.unpack(">I", header)[0]
    return json.loads(_read_exact(client, length).decode("utf-8"))


def _read_exact(client: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = client.recv(remaining)
        if not chunk:
            raise EOFError("connection closed before complete frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _apply_document() -> dict[str, object]:
    return {
        "source": "nodescale",
        "network_id": "net-a",
        "device_id": "device-a",
        "projection_generation": "7",
        "membership_generation": "7",
        "binding_generation": "7",
        "content_hash": HASH,
        "operation": "upsert",
        "generated_operations": ["fleet.health"],
        "provenance": {
            "source": "nodescale",
            "network_id": "net-a",
            "device_id": "device-a",
            "snapshot": "7",
        },
    }


def _running_server(
    tmp_path: Path,
    projection: _Projection,
    *,
    allowed_uid: int | None = None,
    socket_gid: int | None = None,
    io_timeout_seconds: float = 2.0,
):
    from hermes_fleet.local_control import LocalControlServer

    path = tmp_path / "fleet-control.sock"
    server = LocalControlServer(
        socket_path=path,
        allowed_uid=os.getuid() if allowed_uid is None else allowed_uid,
        managed_projection=projection,
        socket_gid=socket_gid,
        io_timeout_seconds=io_timeout_seconds,
    )
    server.start()
    return server, path


def test_local_control_round_trips_closed_capabilities_request(tmp_path) -> None:
    projection = _Projection()
    server, path = _running_server(tmp_path, projection)
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            response = _request(
                client,
                '{"schema":"fleet.managed-projection.v1","kind":"capabilities"}',
            )
    finally:
        server.close()

    assert response == {
        "schema": SCHEMA,
        "kind": "capabilities",
        "ok": True,
        "result": {"kinds": ["capabilities", "apply", "inspect"]},
    }
    assert projection.calls == []
    assert not path.exists()


def test_local_control_dispatches_typed_apply_and_inspect_requests(tmp_path) -> None:
    projection = _Projection()
    server, path = _running_server(tmp_path, projection)
    document = _apply_document()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            apply_response = _request(
                client,
                json.dumps({"schema": SCHEMA, "kind": "apply", "document": document}),
            )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            inspect_response = _request(
                client,
                """{
                    "schema":"fleet.managed-projection.v1",
                    "kind":"inspect",
                    "selector":{
                        "source":"nodescale",
                        "network_id":"net-a",
                        "device_id":"device-a"
                    }
                }""",
            )
    finally:
        server.close()

    assert apply_response == {
        "schema": SCHEMA,
        "kind": "apply",
        "ok": True,
        "result": {"outcome": "applied"},
    }
    assert inspect_response == {
        "schema": SCHEMA,
        "kind": "inspect",
        "ok": True,
        "result": {"generated": None, "effective": None},
    }
    assert projection.calls == [
        ("apply", document),
        (
            "inspect",
            {"source": "nodescale", "network_id": "net-a", "device_id": "device-a"},
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema":"fleet.managed-projection.v1","kind":"capabilities","extra":true}',
        '{"schema":"fleet.managed-projection.v1","kind":"apply","document":{"source":"nodescale","source":"other"}}',
        '{"schema":"fleet.managed-projection.v1","kind":"apply","document":{"source":"nodescale","network_id":"net-a","device_id":"device-a","projection_generation":7,"membership_generation":"7","binding_generation":"7","content_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","operation":"upsert","generated_operations":[],"provenance":{}}}',
        '{"schema":"fleet.managed-projection.v1","kind":"inspect","selector":{"source":"nodescale","network_id":"net-a","device_id":"device-a","extra":"x"}}',
    ],
)
def test_local_control_rejects_noncanonical_closed_request_schemas(payload) -> None:
    from hermes_fleet.local_control import LocalControlProtocolError, parse_request

    with pytest.raises(LocalControlProtocolError):
        parse_request(payload.encode("utf-8"))


def test_local_control_rejects_unknown_nested_provenance_field() -> None:
    from hermes_fleet.local_control import LocalControlProtocolError, parse_request

    document = _apply_document()
    document["provenance"] = {
        "source": "nodescale",
        "network_id": "net-a",
        "device_id": "device-a",
        "snapshot": "7",
        "extra": "forbidden",
    }

    with pytest.raises(LocalControlProtocolError):
        parse_request(
            json.dumps(
                {"schema": SCHEMA, "kind": "apply", "document": document}
            ).encode()
        )


def test_local_control_rejects_non_matching_peer_before_reading_json(tmp_path) -> None:
    from hermes_fleet.local_control import LocalControlServer

    path = tmp_path / "fleet-control.sock"
    projection = _Projection()
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
            client.sendall(b"{not a framed JSON request}")
            try:
                assert client.recv(1) == b""
            except ConnectionResetError:
                pass
    finally:
        server.close()

    assert projection.calls == []


def test_local_control_rejects_trailing_bytes_before_dispatch(tmp_path) -> None:
    projection = _Projection()
    server, path = _running_server(tmp_path, projection)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            response = _request(
                client,
                '{"schema":"fleet.managed-projection.v1","kind":"capabilities"}',
                trailing=b"unexpected",
            )
    finally:
        server.close()

    assert response == {
        "schema": SCHEMA,
        "kind": "error",
        "ok": False,
        "outcome": "rejected",
        "reason": "invalid_request",
        "error": "invalid_request",
    }
    assert projection.calls == []


def test_local_control_requires_client_write_half_close_before_dispatch(
    tmp_path,
) -> None:
    projection = _Projection()
    server, path = _running_server(tmp_path, projection, io_timeout_seconds=0.05)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
            response = _request(
                client,
                '{"schema":"fleet.managed-projection.v1","kind":"capabilities"}',
                write_half_closed=False,
            )
    finally:
        server.close()

    assert response["outcome"] == "rejected"
    assert response["reason"] == "invalid_request"
    assert projection.calls == []


def test_local_control_socket_group_seam_preserves_exact_uid_authentication(
    tmp_path,
) -> None:
    projection = _Projection()
    server, path = _running_server(
        tmp_path,
        projection,
        allowed_uid=os.getuid(),
        socket_gid=os.getgid(),
    )
    try:
        identity = path.lstat()
        assert stat.S_ISSOCK(identity.st_mode)
        assert stat.S_IMODE(identity.st_mode) == 0o660
        assert identity.st_gid == os.getgid()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            response = _request(
                client,
                '{"schema":"fleet.managed-projection.v1","kind":"capabilities"}',
            )
    finally:
        server.close()

    assert response["ok"] is True
    assert projection.calls == []


def test_local_control_fails_closed_for_unsafe_existing_socket_path(tmp_path) -> None:
    from hermes_fleet.local_control import LocalControlServer

    regular_path = tmp_path / "regular"
    regular_path.write_text("must not be removed", encoding="utf-8")
    symlink_path = tmp_path / "symlink"
    symlink_path.symlink_to(regular_path)

    for path in (regular_path, symlink_path):
        with pytest.raises(ValueError, match="unsafe"):
            LocalControlServer(
                socket_path=path,
                allowed_uid=os.getuid(),
                managed_projection=_Projection(),
            ).start()

    assert regular_path.read_text(encoding="utf-8") == "must not be removed"
    assert stat.S_ISLNK(symlink_path.lstat().st_mode)


def test_local_control_frame_limit_is_32768_bytes() -> None:
    from hermes_fleet.local_control import LocalControlProtocolError, parse_request

    oversized = b"{" + (b" " * 32768) + b"}"
    with pytest.raises(LocalControlProtocolError, match="frame"):
        parse_request(oversized)
