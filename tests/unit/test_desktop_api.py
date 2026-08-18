import hashlib
import json
import socket
import struct
import threading
from pathlib import Path

import pytest


def _serve_once(
    path: Path,
    response: dict | bytes,
    captured: list[dict],
    *,
    trailing: bytes = b"",
) -> threading.Thread:
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
                encoded = (
                    response
                    if isinstance(response, bytes)
                    else json.dumps(response, separators=(",", ":")).encode()
                )
                connection.sendall(struct.pack("!I", len(encoded)) + encoded + trailing)

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(2)
    return thread


def _node(*, active: bool, alive: bool, ready: bool, device_id: str) -> dict:
    state = "active" if active else "disabled"
    last_observation = (
        {
            "admission_generation": 1,
            "observed_at_ms": 1_000,
            "received_at_ms": 1_001,
            "network": "reachable",
            "keryx": "available",
            "hermes": "available",
            "worker": "available",
        }
        if active
        else None
    )
    capacity = (
        {"active_workers": 0, "max_workers": 1, "available_worker_slots": 1}
        if active
        else None
    )
    resources = (
        {"cpu": None, "ram": None, "swap": None, "disk": None, "gpu": None}
        if active
        else None
    )
    stable_material = json.dumps(
        ["nodescale", "network-1", device_id], separators=(",", ":")
    ).encode()
    return {
        "stable_id": f"fleet-node-{hashlib.sha256(stable_material).hexdigest()}",
        "identity": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": device_id,
        },
        "naming": {
            "display_name": device_id,
            "provider_name": None,
            "alias": None,
            "has_alias": False,
        },
        "managed": {
            "state": state,
            "active": active,
            "projection_generation": "1",
            "membership_generation": "1",
            "binding_generation": "1",
        },
        "readiness": {
            "managed_state": state,
            "admission_generation": 1,
            "alive": alive,
            "fresh": alive,
            "scheduler_ready": ready,
            "observation_age_ms": 10 if alive else (10_000 if active else None),
            "reasons": (
                []
                if ready
                else (
                    ["node_not_active", "observation_missing"]
                    if not active
                    else ["observation_stale"]
                )
            ),
            "last_observation": last_observation,
            "capacity": capacity,
            "profiles": [] if active else None,
            "resources": resources,
        },
        "operations": ["fleet.health", "fleet.inventory"],
    }


def _response(nodes: list[dict]) -> dict:
    return {
        "schema": "fleet.desktop.v1",
        "kind": "overview",
        "ok": True,
        "result": {"nodes": nodes},
    }


def test_desktop_client_projects_authoritative_nodes_into_bounded_summary(
    tmp_path,
) -> None:
    from hermes_fleet.desktop_api import DesktopApiClient

    path = tmp_path / "fleet.sock"
    captured: list[dict] = []
    nodes = [
        _node(active=True, alive=True, ready=True, device_id="node-a"),
        _node(active=True, alive=False, ready=False, device_id="node-b"),
        _node(active=False, alive=False, ready=False, device_id="node-c"),
    ]
    nodes[0]["naming"] = {
        "display_name": "compute-a",
        "provider_name": "provider-node-a",
        "alias": "compute-a",
        "has_alias": True,
    }
    thread = _serve_once(
        path,
        {
            "schema": "fleet.desktop.v1",
            "kind": "overview",
            "ok": True,
            "result": {"nodes": nodes},
        },
        captured,
    )

    overview = DesktopApiClient(socket_path=path).overview()

    thread.join(2)
    assert captured == [{"schema": "fleet.desktop.v1", "kind": "overview"}]
    assert overview == {
        "schema": "fleet.desktop.v1",
        "summary": {
            "managed": 3,
            "active": 2,
            "alive": 1,
            "ready": 1,
            "not_ready": 1,
        },
        "nodes": nodes,
    }


def test_desktop_client_rejects_duplicate_stable_ids(tmp_path):
    from hermes_fleet.desktop_api import DesktopApiClient

    socket_path = tmp_path / "fleet.sock"
    nodes = [
        _node(active=True, alive=True, ready=True, device_id="node-a"),
        _node(active=True, alive=True, ready=True, device_id="node-a"),
    ]
    thread = _serve_once(socket_path, _response(nodes), [])

    with pytest.raises(RuntimeError, match="duplicate Desktop node identities"):
        DesktopApiClient(socket_path=socket_path).overview()
    thread.join(timeout=2)


def test_desktop_client_rejects_unknown_backend_fields(tmp_path):
    from hermes_fleet.desktop_api import DesktopApiClient

    socket_path = tmp_path / "fleet.sock"
    response = _response([])
    response["unexpected"] = True
    thread = _serve_once(socket_path, response, [])

    with pytest.raises(RuntimeError, match="malformed"):
        DesktopApiClient(socket_path=socket_path).overview()
    thread.join(timeout=2)


def test_desktop_client_rejects_trailing_frame_bytes(tmp_path):
    from hermes_fleet.desktop_api import DesktopApiClient

    socket_path = tmp_path / "fleet.sock"
    thread = _serve_once(socket_path, _response([]), [], trailing=b"JUNK")

    with pytest.raises(RuntimeError, match="trailing Desktop frame bytes"):
        DesktopApiClient(socket_path=socket_path).overview()
    thread.join(timeout=2)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"fleet.desktop.v1","kind":"overview","ok":true,'
        b'"ok":true,"result":{"nodes":[]}}',
        b'{"schema":"fleet.desktop.v1","kind":"overview","ok":true,'
        b'"result":{"nodes":NaN}}',
    ],
)
def test_desktop_client_rejects_noncanonical_json(tmp_path, payload):
    from hermes_fleet.desktop_api import DesktopApiClient

    socket_path = tmp_path / "fleet.sock"
    thread = _serve_once(socket_path, payload, [])

    with pytest.raises(RuntimeError, match="malformed"):
        DesktopApiClient(socket_path=socket_path).overview()
    thread.join(timeout=2)


def test_desktop_client_rejects_lone_surrogate_display_text(tmp_path):
    from hermes_fleet.desktop_api import DesktopApiClient

    socket_path = tmp_path / "fleet.sock"
    node = _node(active=True, alive=True, ready=True, device_id="node-a")
    node["naming"]["display_name"] = "\ud800"
    thread = _serve_once(socket_path, _response([node]), [])

    with pytest.raises(RuntimeError, match="invalid Desktop node"):
        DesktopApiClient(socket_path=socket_path).overview()
    thread.join(timeout=2)


def test_workflow_client_round_trips_versioned_backend_documents(tmp_path) -> None:
    from hermes_fleet.desktop_api import DesktopApiClient

    document = {
        "schema": "fleet.workflow-editor.v2",
        "id": "workflow-1",
        "name": "Deploy safely",
        "nodes": [],
        "connections": [],
        "metadata": {"executionAvailable": False},
    }
    revision = {
        "workflowId": "workflow-1",
        "version": 1,
        "contentHash": "a" * 64,
        "document": document,
        "createdAtMs": 1_000,
    }
    captured: list[dict] = []
    socket_path = tmp_path / "workflow-create.sock"
    thread = _serve_once(
        socket_path,
        {
            "schema": "fleet.workflow.v1",
            "kind": "create",
            "ok": True,
            "result": {"outcome": "created", "revision": revision},
        },
        captured,
    )
    client = DesktopApiClient(socket_path=socket_path)
    assert client.create_workflow(document) == {
        "outcome": "created",
        "revision": revision,
    }
    thread.join(timeout=2)
    assert captured == [
        {"schema": "fleet.workflow.v1", "kind": "create", "document": document}
    ]

    captured = []
    list_socket_path = tmp_path / "workflow-list.sock"
    thread = _serve_once(
        list_socket_path,
        {
            "schema": "fleet.workflow.v1",
            "kind": "list",
            "ok": True,
            "result": {
                "workflows": [
                    {
                        "workflowId": "workflow-1",
                        "latestVersion": 1,
                        "createdAtMs": 1_000,
                        "updatedAtMs": 1_000,
                    }
                ]
            },
        },
        captured,
    )
    workflows = DesktopApiClient(socket_path=list_socket_path).list_workflows()
    thread.join(timeout=2)
    assert workflows[0]["latestVersion"] == 1
    assert captured == [{"schema": "fleet.workflow.v1", "kind": "list"}]


def test_workflow_document_accepts_legacy_v1_and_current_v2_without_execution() -> None:
    from hermes_fleet.desktop_api import _workflow_document

    base = {
        "id": "workflow-compat",
        "name": "Compatibility",
        "nodes": [],
        "connections": [],
        "metadata": {"executionAvailable": False},
    }
    for schema in ("fleet.workflow-editor.v1", "fleet.workflow-editor.v2"):
        document = {"schema": schema, **base}
        assert _workflow_document(document) == document

    with pytest.raises(ValueError, match="envelope"):
        _workflow_document(
            {
                "schema": "fleet.workflow-editor.v2",
                **{**base, "metadata": {"executionAvailable": True}},
            }
        )


def test_workflow_client_keeps_execution_unavailable_and_uses_version_fences(
    tmp_path,
) -> None:
    from hermes_fleet.desktop_api import DesktopApiClient

    captured: list[dict] = []
    socket_path = tmp_path / "workflow-delete.sock"
    thread = _serve_once(
        socket_path,
        {
            "schema": "fleet.workflow.v1",
            "kind": "delete",
            "ok": True,
            "result": {"outcome": "deleted"},
        },
        captured,
    )
    assert (
        DesktopApiClient(socket_path=socket_path).delete_workflow(
            "workflow-1", expected_version=7
        )
        == "deleted"
    )
    thread.join(timeout=2)
    assert captured == [
        {
            "schema": "fleet.workflow.v1",
            "kind": "delete",
            "workflowId": "workflow-1",
            "expectedVersion": 7,
        }
    ]

    capabilities_socket = tmp_path / "workflow-capabilities.sock"
    thread = _serve_once(
        capabilities_socket,
        {
            "schema": "fleet.workflow.v1",
            "kind": "capabilities",
            "ok": True,
            "result": {
                "kinds": [
                    "capabilities",
                    "create",
                    "read",
                    "read_version",
                    "update",
                    "list",
                    "delete",
                ],
                "executionAvailable": False,
            },
        },
        [],
    )
    assert (
        DesktopApiClient(socket_path=capabilities_socket).workflow_capabilities()[
            "executionAvailable"
        ]
        is False
    )
    thread.join(timeout=2)


def test_desktop_alias_client_sends_generation_fenced_set_and_clear(tmp_path):
    from hermes_fleet.desktop_api import DesktopApiClient

    socket_path = tmp_path / "fleet.sock"
    captured: list[dict] = []
    thread = _serve_once(
        socket_path,
        {
            "schema": "fleet.desktop-alias.v1",
            "kind": "set_alias",
            "ok": True,
            "result": {"outcome": "created"},
        },
        captured,
    )
    client = DesktopApiClient(socket_path=socket_path)
    assert (
        client.set_alias(
            source="nodescale",
            network_id="network-1",
            device_id="node-a",
            binding_generation="7",
            alias="compute-a",
        )
        == "created"
    )
    thread.join(timeout=2)
    assert captured == [
        {
            "schema": "fleet.desktop-alias.v1",
            "kind": "set_alias",
            "selector": {
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "node-a",
            },
            "binding_generation": "7",
            "alias": "compute-a",
        }
    ]

    captured = []
    clear_socket_path = tmp_path / "fleet-clear.sock"
    thread = _serve_once(
        clear_socket_path,
        {
            "schema": "fleet.desktop-alias.v1",
            "kind": "clear_alias",
            "ok": True,
            "result": {"outcome": "cleared"},
        },
        captured,
    )
    clear_client = DesktopApiClient(socket_path=clear_socket_path)
    assert (
        clear_client.clear_alias(
            source="nodescale",
            network_id="network-1",
            device_id="node-a",
            binding_generation="7",
        )
        == "cleared"
    )
    thread.join(timeout=2)
    assert captured[0]["kind"] == "clear_alias"
    assert "alias" not in captured[0]


@pytest.mark.parametrize(
    "alias", ["", " padded", "padded ", "line\nbreak", "zero\u200bwidth"]
)
def test_desktop_alias_client_rejects_invalid_display_text_without_connecting(
    tmp_path, alias
):
    from hermes_fleet.desktop_api import DesktopApiClient

    client = DesktopApiClient(socket_path=tmp_path / "missing.sock")
    with pytest.raises(ValueError, match="alias"):
        client.set_alias(
            source="nodescale",
            network_id="network-1",
            device_id="node-a",
            binding_generation="1",
            alias=alias,
        )
