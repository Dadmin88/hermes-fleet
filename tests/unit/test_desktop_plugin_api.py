import asyncio
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
API_PATH = ROOT / "dashboard" / "plugin_api.py"


def _load_api():
    spec = importlib.util.spec_from_file_location(
        "hermes_fleet_dashboard_api", API_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_observation_config_reads_strict_profile_file(monkeypatch, tmp_path) -> None:
    module = _load_api()
    socket_path = tmp_path / "nodescale.sock"
    network_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.delenv("NODESCALE_OBSERVATION_SOCKET", raising=False)
    monkeypatch.delenv("NODESCALE_OBSERVATION_NETWORK_ID", raising=False)
    monkeypatch.setattr(module, "get_fleet_dir", lambda: tmp_path)
    (tmp_path / "nodescale-observations.json").write_text(
        json.dumps(
            {
                "schema": "fleet.nodescale-observations.v1",
                "socket_path": str(socket_path),
                "network_id": network_id,
            }
        ),
        encoding="utf-8",
    )

    assert module._observation_config() == (socket_path, network_id)


def test_observation_config_rejects_unknown_profile_fields(
    monkeypatch, tmp_path
) -> None:
    module = _load_api()
    monkeypatch.delenv("NODESCALE_OBSERVATION_SOCKET", raising=False)
    monkeypatch.delenv("NODESCALE_OBSERVATION_NETWORK_ID", raising=False)
    monkeypatch.setattr(module, "get_fleet_dir", lambda: tmp_path)
    (tmp_path / "nodescale-observations.json").write_text(
        json.dumps(
            {
                "schema": "fleet.nodescale-observations.v1",
                "socket_path": "/run/user/1000/nodescale.sock",
                "network_id": "11111111-1111-1111-1111-111111111111",
                "authority": "managed",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid Nodescale observation configuration"):
        module._observation_config()


def test_overview_route_uses_authoritative_desktop_client(
    monkeypatch, tmp_path
) -> None:
    module = _load_api()
    socket_path = tmp_path / "fleet.sock"
    monkeypatch.setenv("FLEET_MANAGED_PROJECTION_SOCKET", str(socket_path))
    monkeypatch.delenv("NODESCALE_OBSERVATION_SOCKET", raising=False)
    monkeypatch.delenv("NODESCALE_OBSERVATION_NETWORK_ID", raising=False)
    monkeypatch.setattr(module, "get_fleet_dir", lambda: tmp_path)
    managed = {
        "schema": "fleet.desktop.v1",
        "summary": {
            "managed": 0,
            "active": 0,
            "alive": 0,
            "ready": 0,
            "not_ready": 0,
        },
        "nodes": [],
    }
    expected = {
        "schema": "fleet.desktop.v2",
        "summary": {
            "managed": 0,
            "active": 0,
            "alive": 0,
            "ready": 0,
            "not_ready": 0,
            "observed_unmanaged": 0,
        },
        "nodes": [],
        "observed_nodes": [],
        "observations": {
            "state": "disabled",
            "network_id": None,
            "reconciliation": None,
            "truncated": False,
        },
    }
    captured: list[Path] = []

    class FakeClient:
        def __init__(self, *, socket_path: Path) -> None:
            captured.append(socket_path)

        def overview(self) -> dict:
            return managed

    monkeypatch.setattr(module, "DesktopApiClient", FakeClient)

    assert asyncio.run(module.overview()) == expected
    assert captured == [socket_path]


def test_overview_route_adds_observed_evidence_without_managed_authority(
    monkeypatch, tmp_path
) -> None:
    module = _load_api()
    managed_path = tmp_path / "fleet.sock"
    observed_path = tmp_path / "observed.sock"
    network_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv("FLEET_MANAGED_PROJECTION_SOCKET", str(managed_path))
    monkeypatch.setenv("NODESCALE_OBSERVATION_SOCKET", str(observed_path))
    monkeypatch.setenv("NODESCALE_OBSERVATION_NETWORK_ID", network_id)
    observation = {
        "observed_id": "sha256:" + "a" * 64,
        "network_id": network_id,
        "provider_kind": "headscale",
        "provider_instance_id": "22222222-2222-2222-2222-222222222222",
        "provider_node_id": "node-1",
        "hostname": "fleet-accept-a",
        "given_name": "fleet-accept-a",
        "addresses": ["203.0.113.1"],
        "tags": [],
        "registered_at": None,
        "last_seen_at": "2026-08-10T00:00:01+00:00",
        "expires_at": None,
        "online": True,
        "expired": False,
        "classification": "discovered_unmanaged",
        "first_observed_at": "2026-08-10T00:00:00+00:00",
        "last_observed_at": "2026-08-10T00:00:01+00:00",
        "snapshot_at": "2026-08-10T00:00:01+00:00",
    }

    class ManagedClient:
        def __init__(self, *, socket_path: Path) -> None:
            assert socket_path == managed_path

        def overview(self) -> dict:
            return {
                "schema": "fleet.desktop.v1",
                "summary": {
                    "managed": 0,
                    "active": 0,
                    "alive": 0,
                    "ready": 0,
                    "not_ready": 0,
                },
                "nodes": [],
            }

    class ObservedClient:
        def __init__(self, *, socket_path: Path, network_id: str) -> None:
            assert socket_path == observed_path
            assert network_id == "11111111-1111-1111-1111-111111111111"

        def overview(self) -> dict:
            return {
                "schema": "nodescale.observations.v1",
                "network_id": network_id,
                "reconciliation": {
                    "state": "healthy",
                    "last_attempted_at": "2026-08-10T00:00:01+00:00",
                    "last_successful_at": "2026-08-10T00:00:01+00:00",
                    "observed_count": 1,
                },
                "observations": [observation],
                "truncated": False,
            }

    monkeypatch.setattr(module, "DesktopApiClient", ManagedClient)
    monkeypatch.setattr(module, "NodescaleObservationClient", ObservedClient)

    result = asyncio.run(module.overview())
    assert result["schema"] == "fleet.desktop.v2"
    assert result["summary"]["managed"] == 0
    assert result["summary"]["observed_unmanaged"] == 1
    assert result["nodes"] == []
    assert result["observed_nodes"] == [observation]
    assert result["observations"] == {
        "state": "available",
        "network_id": network_id,
        "reconciliation": {
            "state": "healthy",
            "last_attempted_at": "2026-08-10T00:00:01+00:00",
            "last_successful_at": "2026-08-10T00:00:01+00:00",
            "observed_count": 1,
        },
        "truncated": False,
    }
    assert "operations" not in result["observed_nodes"][0]
    assert "readiness" not in result["observed_nodes"][0]
    assert "managed" not in result["observed_nodes"][0]


def test_observation_outage_does_not_hide_managed_fleet_state(
    monkeypatch, tmp_path
) -> None:
    module = _load_api()
    network_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv("NODESCALE_OBSERVATION_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("NODESCALE_OBSERVATION_NETWORK_ID", network_id)

    class ManagedClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def overview(self) -> dict:
            return {
                "schema": "fleet.desktop.v1",
                "summary": {
                    "managed": 1,
                    "active": 1,
                    "alive": 1,
                    "ready": 1,
                    "not_ready": 0,
                },
                "nodes": [{"stable_id": "managed-node"}],
            }

    class UnavailableObservedClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def overview(self) -> dict:
            raise OSError("private observed socket detail")

    monkeypatch.setattr(module, "DesktopApiClient", ManagedClient)
    monkeypatch.setattr(module, "NodescaleObservationClient", UnavailableObservedClient)

    result = asyncio.run(module.overview())
    assert result["summary"]["managed"] == 1
    assert result["summary"]["observed_unmanaged"] == 0
    assert result["nodes"] == [{"stable_id": "managed-node"}]
    assert result["observed_nodes"] == []
    assert result["observations"] == {
        "state": "unavailable",
        "network_id": network_id,
        "reconciliation": None,
        "truncated": False,
    }


def test_overview_route_returns_service_unavailable_without_leaking_details(
    monkeypatch, tmp_path
) -> None:
    module = _load_api()
    monkeypatch.setenv(
        "FLEET_MANAGED_PROJECTION_SOCKET", str(tmp_path / "missing.sock")
    )

    class UnavailableClient:
        def __init__(self, *, socket_path: Path) -> None:
            del socket_path

        def overview(self) -> dict:
            raise OSError("private local path and implementation detail")

    monkeypatch.setattr(module, "DesktopApiClient", UnavailableClient)

    with pytest.raises(HTTPException) as error:
        asyncio.run(module.overview())
    assert error.value.status_code == 503
    assert error.value.detail == "Fleet Desktop state is unavailable."


def test_typed_event_contract_is_stable_and_router_exposes_websocket():
    module = _load_api()
    assert module.build_event("snapshot", 1) == {
        "schema": "fleet.desktop-events.v1",
        "kind": "snapshot",
        "sequence": 1,
    }
    assert module.build_event("unavailable", 2) == {
        "schema": "fleet.desktop-events.v1",
        "kind": "unavailable",
        "sequence": 2,
    }
    assert module._overview_digest({"b": 2, "a": 1}) == module._overview_digest(
        {"a": 1, "b": 2}
    )
    assert module._overview_digest(
        {"nodes": [{"readiness": {"observation_age_ms": 1, "fresh": True}}]}
    ) == module._overview_digest(
        {"nodes": [{"readiness": {"observation_age_ms": 999, "fresh": True}}]}
    )
    assert module._overview_digest({"observed_nodes": []}) != module._overview_digest(
        {"observed_nodes": [{"observed_id": "sha256:" + "a" * 64}]}
    )
    assert any(
        getattr(route, "path", None) == "/events" for route in module.router.routes
    )


def test_websocket_authorization_delegates_to_hermes_canonical_gate(monkeypatch):
    module = _load_api()
    websocket = object()
    canonical = types.SimpleNamespace(
        _ws_auth_ok=lambda value: value is websocket,
        _ws_request_is_allowed=lambda value: value is websocket,
    )
    monkeypatch.setattr(module.importlib, "import_module", lambda _name: canonical)
    assert module._websocket_rejection_code(websocket) is None

    canonical._ws_auth_ok = lambda _value: False
    assert module._websocket_rejection_code(websocket) == 4401
    canonical._ws_auth_ok = lambda _value: True
    canonical._ws_request_is_allowed = lambda _value: False
    assert module._websocket_rejection_code(websocket) == 4403


def test_dashboard_api_loads_from_an_installed_plugin_without_repo_pythonpath(
    tmp_path,
) -> None:
    installed = tmp_path / "plugins" / "hermes-fleet"
    shutil.copytree(ROOT / "dashboard", installed / "dashboard")
    shutil.copytree(ROOT / "hermes_fleet", installed / "hermes_fleet")
    script = f"""
import importlib.util
from pathlib import Path
path = Path({str(installed / "dashboard" / "plugin_api.py")!r})
spec = importlib.util.spec_from_file_location('installed_fleet_dashboard', path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.router is not None
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _stable_id(source: str, network_id: str, device_id: str) -> str:
    material = json.dumps(
        [source, network_id, device_id], separators=(",", ":")
    ).encode()
    return f"fleet-node-{hashlib.sha256(material).hexdigest()}"


def test_alias_routes_bind_stable_id_and_call_generation_fenced_client(
    monkeypatch, tmp_path
) -> None:
    module = _load_api()
    socket_path = tmp_path / "fleet.sock"
    monkeypatch.setenv("FLEET_MANAGED_PROJECTION_SOCKET", str(socket_path))
    captured: list[tuple] = []

    class FakeClient:
        def __init__(self, *, socket_path: Path) -> None:
            captured.append(("socket", socket_path))

        def set_alias(self, **request) -> str:
            captured.append(("set", request))
            return "created"

        def clear_alias(self, **request) -> str:
            captured.append(("clear", request))
            return "cleared"

    monkeypatch.setattr(module, "DesktopApiClient", FakeClient)
    stable_id = _stable_id("nodescale", "network-1", "node-a")
    set_request = module.AliasSetRequest(
        source="nodescale",
        network_id="network-1",
        device_id="node-a",
        binding_generation="7",
        alias="Workstation",
    )
    assert asyncio.run(module.set_alias(stable_id, set_request)) == {
        "outcome": "created"
    }
    clear_request = module.AliasClearRequest(
        source="nodescale",
        network_id="network-1",
        device_id="node-a",
        binding_generation="7",
    )
    assert asyncio.run(module.clear_alias(stable_id, clear_request)) == {
        "outcome": "cleared"
    }
    assert captured == [
        ("socket", socket_path),
        (
            "set",
            {
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "node-a",
                "binding_generation": "7",
                "alias": "Workstation",
            },
        ),
        ("socket", socket_path),
        (
            "clear",
            {
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "node-a",
                "binding_generation": "7",
            },
        ),
    ]


def test_alias_route_rejects_stable_id_mismatch_before_control_call(
    monkeypatch,
) -> None:
    module = _load_api()

    class ForbiddenClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("mismatched identity reached Fleet control")

    monkeypatch.setattr(module, "DesktopApiClient", ForbiddenClient)
    request = module.AliasSetRequest(
        source="nodescale",
        network_id="network-1",
        device_id="node-a",
        binding_generation="1",
        alias="Workstation",
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.set_alias("fleet-node-" + "0" * 64, request))
    assert error.value.status_code == 400
    assert error.value.detail == "Invalid Fleet node identity."


def test_alias_route_reports_generation_conflict_without_leaking_details(
    monkeypatch,
) -> None:
    module = _load_api()

    class RejectingClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def set_alias(self, **_request) -> str:
            raise RuntimeError("private control protocol details")

    monkeypatch.setattr(module, "DesktopApiClient", RejectingClient)
    request = module.AliasSetRequest(
        source="nodescale",
        network_id="network-1",
        device_id="node-a",
        binding_generation="1",
        alias="Workstation",
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            module.set_alias(_stable_id("nodescale", "network-1", "node-a"), request)
        )
    assert error.value.status_code == 409
    assert error.value.detail == "Fleet rejected the alias update."
