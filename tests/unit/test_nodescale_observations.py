import hashlib
import json
import socket
import struct
import threading
from pathlib import Path

import pytest

VERSION = "nodescale.observations.v1"
NETWORK_ID = "11111111-1111-1111-1111-111111111111"
INSTANCE_ID = "22222222-2222-2222-2222-222222222222"


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


def _capabilities(*, max_page_size: int = 100) -> dict:
    return {
        "version": VERSION,
        "kind": "capabilities",
        "capabilities": {
            "max_page_size": max_page_size,
            "max_response_bytes": 65_536,
        },
    }


def _reconciliation(*, count: int = 2) -> dict:
    return {
        "state": "healthy",
        "last_attempted_at": "2026-08-10T00:00:01+00:00",
        "last_successful_at": "2026-08-10T00:00:00+00:00",
        "observed_count": count,
    }


def _summary(*, count: int = 2) -> dict:
    return {
        "version": VERSION,
        "kind": "summary",
        "network_id": NETWORK_ID,
        "reconciliation": _reconciliation(count=count),
    }


def _observation(node_id: str, *, name: str) -> dict:
    suffix = node_id.removeprefix("node-")
    return {
        "observed_id": f"sha256:{hashlib.sha256(node_id.encode()).hexdigest()}",
        "network_id": NETWORK_ID,
        "provider_kind": "headscale",
        "provider_instance_id": INSTANCE_ID,
        "provider_node_id": node_id,
        "hostname": name,
        "given_name": name,
        "addresses": [f"100.64.0.{int(suffix)}"],
        "tags": ["tag:fleet-acceptance"],
        "registered_at": "2026-08-10T00:00:00+00:00",
        "last_seen_at": "2026-08-10T00:00:01+00:00",
        "expires_at": None,
        "online": True,
        "expired": False,
        "classification": "discovered_unmanaged",
        "first_observed_at": "2026-08-10T00:00:00+00:00",
        "last_observed_at": "2026-08-10T00:00:01+00:00",
        "snapshot_at": "2026-08-10T00:00:01+00:00",
    }


def _list(observations: list[dict], *, next_cursor: str | None = None) -> dict:
    response = {
        "version": VERSION,
        "kind": "list",
        "network_id": NETWORK_ID,
        "reconciliation": _reconciliation(),
        "observations": observations,
    }
    if next_cursor is not None:
        response["next_cursor"] = next_cursor
    return response


def test_nodescale_client_reads_strict_paginated_observed_inventory(tmp_path) -> None:
    from hermes_fleet.nodescale_observations import NodescaleObservationClient

    path = tmp_path / "observations.sock"
    captured: list[dict] = []
    first = _observation("node-1", name="fleet-accept-a")
    second = _observation("node-2", name="fleet-accept-b")
    thread = _serve(
        path,
        [
            _capabilities(max_page_size=1),
            _summary(),
            _list([first], next_cursor="node-1"),
            _list([second]),
        ],
        captured,
    )

    result = NodescaleObservationClient(
        socket_path=path, network_id=NETWORK_ID
    ).overview()

    thread.join(timeout=2)
    assert not thread.is_alive()
    assert captured == [
        {"version": VERSION, "kind": "capabilities"},
        {"version": VERSION, "kind": "summary", "network_id": NETWORK_ID},
        {
            "version": VERSION,
            "kind": "list",
            "network_id": NETWORK_ID,
            "limit": 1,
        },
        {
            "version": VERSION,
            "kind": "list",
            "network_id": NETWORK_ID,
            "limit": 1,
            "cursor": "node-1",
        },
    ]
    assert result == {
        "schema": VERSION,
        "network_id": NETWORK_ID,
        "reconciliation": _reconciliation(),
        "observations": [first, second],
        "truncated": False,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update({"device_id": "forbidden"}),
        lambda row: row.update({"readiness": {"scheduler_ready": True}}),
        lambda row: row.update({"operations": ["fleet.hermes.run"]}),
        lambda row: row.update({"network_id": "33333333-3333-3333-3333-333333333333"}),
        lambda row: row.update({"observed_id": "not-a-sha256"}),
    ],
)
def test_nodescale_client_rejects_authority_unknown_and_identity_mismatch(
    tmp_path, mutate
) -> None:
    from hermes_fleet.nodescale_observations import NodescaleObservationClient

    path = tmp_path / "observations.sock"
    row = _observation("node-1", name="fleet-accept-a")
    mutate(row)
    thread = _serve(path, [_capabilities(), _summary(count=1), _list([row])], [])

    with pytest.raises(RuntimeError, match="invalid Nodescale observation"):
        NodescaleObservationClient(socket_path=path, network_id=NETWORK_ID).overview()
    thread.join(timeout=2)


def test_nodescale_client_rejects_duplicate_observed_ids(tmp_path) -> None:
    from hermes_fleet.nodescale_observations import NodescaleObservationClient

    path = tmp_path / "observations.sock"
    first = _observation("node-1", name="fleet-accept-a")
    second = _observation("node-2", name="fleet-accept-b")
    second["observed_id"] = first["observed_id"]
    thread = _serve(path, [_capabilities(), _summary(), _list([first, second])], [])

    with pytest.raises(RuntimeError, match="duplicate Nodescale observation"):
        NodescaleObservationClient(socket_path=path, network_id=NETWORK_ID).overview()
    thread.join(timeout=2)


def test_nodescale_client_rejects_duplicate_json_and_trailing_frame_bytes(
    tmp_path,
) -> None:
    from hermes_fleet.nodescale_observations import NodescaleObservationClient

    path = tmp_path / "duplicate.sock"
    duplicate = (
        b'{"version":"nodescale.observations.v1","kind":"capabilities",'
        b'"kind":"capabilities","capabilities":{"max_page_size":100,'
        b'"max_response_bytes":65536}}'
    )
    thread = _serve(path, [duplicate], [])
    with pytest.raises(RuntimeError, match="malformed Nodescale JSON"):
        NodescaleObservationClient(socket_path=path, network_id=NETWORK_ID).overview()
    thread.join(timeout=2)

    trailing_path = tmp_path / "trailing.sock"
    thread = _serve(trailing_path, [_capabilities()], [], trailing=b"JUNK")
    with pytest.raises(RuntimeError, match="trailing Nodescale frame bytes"):
        NodescaleObservationClient(
            socket_path=trailing_path, network_id=NETWORK_ID
        ).overview()
    thread.join(timeout=2)


def test_nodescale_client_caps_inventory_and_reports_truncation(tmp_path) -> None:
    from hermes_fleet.nodescale_observations import NodescaleObservationClient

    path = tmp_path / "observations.sock"
    all_rows = sorted(
        [
            _observation(f"node-{index}", name=f"node-{index}")
            for index in range(1, 257)
        ],
        key=lambda row: row["provider_node_id"],
    )
    pages = [all_rows[index : index + 32] for index in range(0, 256, 32)]
    thread = _serve(
        path,
        [
            _capabilities(max_page_size=32),
            _summary(count=300),
            *[_list(page, next_cursor=page[-1]["provider_node_id"]) for page in pages],
        ],
        [],
    )

    result = NodescaleObservationClient(
        socket_path=path, network_id=NETWORK_ID, max_observations=256
    ).overview()

    thread.join(timeout=2)
    assert len(result["observations"]) == 256
    assert result["truncated"] is True


def test_nodescale_client_caps_page_count_for_tiny_provider_pages(tmp_path) -> None:
    from hermes_fleet.nodescale_observations import NodescaleObservationClient

    path = tmp_path / "observations.sock"
    rows = [
        _observation(f"node-{index:03d}", name=f"node-{index:03d}")
        for index in range(32)
    ]
    captured: list[dict] = []
    thread = _serve(
        path,
        [
            _capabilities(max_page_size=1),
            _summary(count=300),
            *[_list([row], next_cursor=row["provider_node_id"]) for row in rows],
        ],
        captured,
    )

    result = NodescaleObservationClient(
        socket_path=path, network_id=NETWORK_ID, max_observations=256
    ).overview()

    thread.join(timeout=2)
    assert len(result["observations"]) == 32
    assert result["truncated"] is True
    assert len([request for request in captured if request["kind"] == "list"]) == 32
