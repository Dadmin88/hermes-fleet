import asyncio
import importlib.util
import shutil
import subprocess
import sys
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


def test_overview_route_uses_authoritative_desktop_client(
    monkeypatch, tmp_path
) -> None:
    module = _load_api()
    socket_path = tmp_path / "fleet.sock"
    monkeypatch.setenv("FLEET_MANAGED_PROJECTION_SOCKET", str(socket_path))
    expected = {
        "schema": "fleet.desktop.v1",
        "summary": {"managed": 0, "active": 0, "alive": 0, "ready": 0, "not_ready": 0},
        "nodes": [],
    }
    captured: list[Path] = []

    class FakeClient:
        def __init__(self, *, socket_path: Path) -> None:
            captured.append(socket_path)

        def overview(self) -> dict:
            return expected

    monkeypatch.setattr(module, "DesktopApiClient", FakeClient)

    assert asyncio.run(module.overview()) == expected
    assert captured == [socket_path]


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
