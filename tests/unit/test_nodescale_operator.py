import json
import socket
import struct
import threading
from pathlib import Path

import pytest

VERSION = "nodescale.operator.v1"
NETWORK_ID = "11111111-1111-1111-1111-111111111111"
DEVICE_A = "22222222-2222-2222-2222-222222222222"
DEVICE_B = "33333333-3333-3333-3333-333333333333"
PROVIDER_INSTANCE_ID = "44444444-4444-4444-4444-444444444444"
BINDING_ID = "55555555-5555-5555-5555-555555555555"


def _serve(
    path: Path,
    responses: list[dict | bytes],
    captured: list[dict],
    *,
    trailing: bytes = b"",
) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(len(responses))
            ready.set()
            for response in responses:
                connection, _ = listener.accept()
                with connection:
                    length = struct.unpack("!I", _recv_exact(connection, 4))[0]
                    captured.append(json.loads(_recv_exact(connection, length)))
                    assert connection.recv(1) == b""
                    encoded = (
                        response
                        if isinstance(response, bytes)
                        else json.dumps(response, separators=(",", ":")).encode()
                    )
                    connection.sendall(
                        struct.pack("!I", len(encoded)) + encoded + trailing
                    )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(2)
    return thread


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise RuntimeError("client closed early")
        result.extend(chunk)
    return bytes(result)


def _capabilities() -> dict:
    return {
        "version": VERSION,
        "kind": "capabilities",
        "capabilities": {
            "read_operations": ["capabilities", "devices.list", "devices.inspect"],
            "mutation_operations": [],
            "max_page_size": 32,
            "max_response_bytes": 65_536,
        },
    }


def _device(device_id: str, *, name: str) -> dict:
    return {
        "device_id": device_id,
        "network_id": NETWORK_ID,
        "display_name": name,
        "membership_state": "active",
        "roles": ["node", "worker"],
        "credential_generation": 3,
        "keryx_binding_generation": 4,
        "fleet_projection_generation": 5,
        "fleet_projection_status": "applied",
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "provider_node_id": "provider-node-1",
        "durable_trust_state": "trusted",
        "durable_trust_revision": 7,
        "live_trust_evidence": "not_reconciled_by_operator_read",
        "provider_binding_state": "active",
        "provider_binding_revision": 8,
        "keryx_binding_id": BINDING_ID,
        "keryx_binding_state": "active",
        "verified_keryx_peer_id": "peer-a",
        "keryx_binding_revision": 9,
        "live_keryx_binding_health": "not_exposed",
        "created_at": "2026-08-11T00:00:00+00:00",
        "updated_at": "2026-08-11T00:01:00+00:00",
        "revoked_at": None,
    }


def _page(devices: list[dict], *, next_cursor: str | None = None) -> dict:
    response = {
        "version": VERSION,
        "kind": "devices.list",
        "network_id": NETWORK_ID,
        "devices": devices,
    }
    if next_cursor is not None:
        response["next_cursor"] = next_cursor
    return response


def _inspection(device: dict) -> dict:
    return {
        "version": VERSION,
        "kind": "devices.inspect",
        "network_id": NETWORK_ID,
        "device": device,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_page_size", 32.0),
        ("max_page_size", True),
        ("max_response_bytes", 65_536.0),
        ("max_response_bytes", False),
    ],
)
def test_operator_client_rejects_non_integer_capability_bounds(
    tmp_path, monkeypatch, field, value
) -> None:
    from hermes_fleet.nodescale_operator import NodescaleOperatorClient

    response = _capabilities()
    response["capabilities"][field] = value
    client = NodescaleOperatorClient(
        socket_path=tmp_path / "operator.sock", network_id=NETWORK_ID
    )
    monkeypatch.setattr(client, "_request", lambda payload, *, deadline: response)

    with pytest.raises(RuntimeError, match="invalid operator capabilities"):
        client._capabilities(0)


def test_operator_client_lists_and_inspects_exact_authoritative_device(
    tmp_path,
) -> None:
    from hermes_fleet.nodescale_operator import NodescaleOperatorClient

    socket_path = tmp_path / "operator.sock"
    captured: list[dict] = []
    first = _device(DEVICE_A, name="compute-a")
    second = _device(DEVICE_B, name="compute-b")
    thread = _serve(
        socket_path,
        [
            _capabilities(),
            _page([first], next_cursor=DEVICE_A),
            _page([second]),
            _inspection(first),
        ],
        captured,
    )
    client = NodescaleOperatorClient(socket_path=socket_path, network_id=NETWORK_ID)

    assert client.list_devices() == {
        "schema": VERSION,
        "network_id": NETWORK_ID,
        "devices": [first, second],
        "truncated": False,
    }
    assert client.inspect_device(DEVICE_A) == first

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert captured == [
        {"version": VERSION, "kind": "capabilities"},
        {
            "version": VERSION,
            "kind": "devices.list",
            "network_id": NETWORK_ID,
            "limit": 32,
        },
        {
            "version": VERSION,
            "kind": "devices.list",
            "network_id": NETWORK_ID,
            "limit": 32,
            "cursor": DEVICE_A,
        },
        {
            "version": VERSION,
            "kind": "devices.inspect",
            "network_id": NETWORK_ID,
            "device_id": DEVICE_A,
        },
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda device: device.update({"scheduler_ready": True}),
        lambda device: device.update({"live_trust_evidence": "trusted"}),
        lambda device: device.update({"live_keryx_binding_health": "healthy"}),
        lambda device: device.update({"network_id": DEVICE_A}),
        lambda device: device.update({"roles": ["admin", "node"]}),
        lambda device: device.update({"durable_trust_revision": None}),
        lambda device: device.update(
            {"durable_trust_state": None, "durable_trust_revision": None}
        ),
        lambda device: device.update({"verified_keryx_peer_id": "peer-a\nsecret"}),
        lambda device: device.update({"provider_node_id": "a" * 256}),
    ],
)
def test_operator_client_rejects_unknown_authority_and_incoherent_evidence(
    tmp_path, mutate
) -> None:
    from hermes_fleet.nodescale_operator import NodescaleOperatorClient

    socket_path = tmp_path / "operator.sock"
    device = _device(DEVICE_A, name="compute-a")
    mutate(device)
    thread = _serve(socket_path, [_inspection(device)], [])

    with pytest.raises(RuntimeError, match="invalid Nodescale operator device"):
        NodescaleOperatorClient(
            socket_path=socket_path, network_id=NETWORK_ID
        ).inspect_device(DEVICE_A)
    thread.join(timeout=2)


def test_operator_client_rejects_identity_mismatch_duplicate_and_bad_cursor(
    tmp_path,
) -> None:
    from hermes_fleet.nodescale_operator import NodescaleOperatorClient

    mismatch_path = tmp_path / "mismatch.sock"
    mismatch = _device(DEVICE_B, name="compute-b")
    thread = _serve(mismatch_path, [_inspection(mismatch)], [])
    with pytest.raises(RuntimeError, match="invalid Nodescale operator inspection"):
        NodescaleOperatorClient(
            socket_path=mismatch_path, network_id=NETWORK_ID
        ).inspect_device(DEVICE_A)
    thread.join(timeout=2)

    duplicate_path = tmp_path / "duplicate.sock"
    duplicate = _device(DEVICE_A, name="compute-a")
    thread = _serve(
        duplicate_path, [_capabilities(), _page([duplicate, duplicate])], []
    )
    with pytest.raises(RuntimeError, match="duplicate Nodescale operator device"):
        NodescaleOperatorClient(
            socket_path=duplicate_path, network_id=NETWORK_ID
        ).list_devices()
    thread.join(timeout=2)

    cursor_path = tmp_path / "cursor.sock"
    thread = _serve(
        cursor_path,
        [_capabilities(), _page([duplicate], next_cursor=DEVICE_B)],
        [],
    )
    with pytest.raises(RuntimeError, match="invalid Nodescale operator cursor"):
        NodescaleOperatorClient(
            socket_path=cursor_path, network_id=NETWORK_ID
        ).list_devices()
    thread.join(timeout=2)


def test_operator_client_rejects_duplicate_json_trailing_bytes_and_errors(
    tmp_path,
) -> None:
    from hermes_fleet.nodescale_operator import NodescaleOperatorClient

    duplicate_path = tmp_path / "duplicate-json.sock"
    duplicate = (
        b'{"version":"nodescale.operator.v1","kind":"capabilities",'
        b'"kind":"capabilities","capabilities":{}}'
    )
    thread = _serve(duplicate_path, [duplicate], [])
    with pytest.raises(RuntimeError, match="malformed Nodescale operator JSON"):
        NodescaleOperatorClient(
            socket_path=duplicate_path, network_id=NETWORK_ID
        ).list_devices()
    thread.join(timeout=2)

    trailing_path = tmp_path / "trailing.sock"
    thread = _serve(trailing_path, [_capabilities()], [], trailing=b"JUNK")
    with pytest.raises(RuntimeError, match="trailing Nodescale operator frame bytes"):
        NodescaleOperatorClient(
            socket_path=trailing_path, network_id=NETWORK_ID
        ).list_devices()
    thread.join(timeout=2)

    error_path = tmp_path / "error.sock"
    thread = _serve(
        error_path,
        [{"version": VERSION, "kind": "error", "error": "unavailable"}],
        [],
    )
    with pytest.raises(RuntimeError, match="Nodescale operator state is unavailable"):
        NodescaleOperatorClient(
            socket_path=error_path, network_id=NETWORK_ID
        ).list_devices()
    thread.join(timeout=2)


def test_operator_client_bounds_configuration_and_result_count(tmp_path) -> None:
    from hermes_fleet.nodescale_operator import NodescaleOperatorClient

    with pytest.raises(ValueError, match="absolute Path"):
        NodescaleOperatorClient(
            socket_path=Path("operator.sock"), network_id=NETWORK_ID
        )
    with pytest.raises(ValueError, match="network ID"):
        NodescaleOperatorClient(
            socket_path=tmp_path / "operator.sock", network_id="not-a-uuid"
        )
    with pytest.raises(ValueError, match="device cap"):
        NodescaleOperatorClient(
            socket_path=tmp_path / "operator.sock",
            network_id=NETWORK_ID,
            max_devices=257,
        )

    socket_path = tmp_path / "bounded.sock"
    device = _device(DEVICE_A, name="compute-a")
    thread = _serve(
        socket_path,
        [_capabilities(), _page([device], next_cursor=DEVICE_A)],
        [],
    )
    result = NodescaleOperatorClient(
        socket_path=socket_path, network_id=NETWORK_ID, max_devices=1
    ).list_devices()
    assert result["devices"] == [device]
    assert result["truncated"] is True
    thread.join(timeout=2)
