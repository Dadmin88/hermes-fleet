from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from types import ModuleType

import pytest


class _Projection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _ControlServer:
    def __init__(self) -> None:
        self.started = False
        self.served = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    def serve_forever(self) -> None:
        self.served = True

    async def close(self) -> None:
        self.closed = True


def test_managed_service_starts_logs_readiness_and_cleans_up(caplog, tmp_path) -> None:
    from hermes_fleet.managed_service import ManagedServiceConfig, run_managed_service

    projection = _Projection()
    control = _ControlServer()
    created: dict[str, object] = {}

    def projection_factory(*, database_path: Path) -> _Projection:
        created["database_path"] = database_path
        return projection

    def control_factory(
        *,
        socket_path: Path,
        allowed_uid: int,
        socket_gid: int | None,
        managed_projection: _Projection,
    ) -> _ControlServer:
        created["socket_path"] = socket_path
        created["allowed_uid"] = allowed_uid
        created["socket_gid"] = socket_gid
        created["managed_projection"] = managed_projection
        return control

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_managed_service(
            ManagedServiceConfig(
                socket_path=tmp_path / "control.sock",
                database_path=tmp_path / "managed-projection.sqlite3",
                allowed_uid=1000,
                shutdown_timeout_seconds=1,
            ),
            projection_factory=projection_factory,
            control_factory=control_factory,
            shutdown=shutdown,
        )

    with caplog.at_level(logging.INFO):
        asyncio.run(exercise())

    assert created == {
        "database_path": tmp_path / "managed-projection.sqlite3",
        "socket_path": tmp_path / "control.sock",
        "allowed_uid": 1000,
        "socket_gid": None,
        "managed_projection": projection,
    }
    assert control.started is True
    assert control.closed is True
    assert projection.closed is True
    assert "fleet managed projection ready" in caplog.text


def test_managed_service_config_requires_absolute_paths_and_bounded_values(
    tmp_path,
) -> None:
    from hermes_fleet.managed_service import ManagedServiceConfig

    with pytest.raises(ValueError, match="socket_path must be an absolute Path"):
        ManagedServiceConfig(
            socket_path=Path("control.sock"),
            database_path=tmp_path / "managed-projection.sqlite3",
            allowed_uid=1000,
            shutdown_timeout_seconds=20,
        )
    with pytest.raises(ValueError, match="database_path must be an absolute Path"):
        ManagedServiceConfig(
            socket_path=tmp_path / "control.sock",
            database_path=Path("managed-projection.sqlite3"),
            allowed_uid=1000,
            shutdown_timeout_seconds=20,
        )
    with pytest.raises(ValueError, match="allowed_uid must be a nonnegative integer"):
        ManagedServiceConfig(
            socket_path=tmp_path / "control.sock",
            database_path=tmp_path / "managed-projection.sqlite3",
            allowed_uid=True,
            shutdown_timeout_seconds=20,
        )
    with pytest.raises(
        ValueError, match="shutdown_timeout_seconds must be between 1 and 60"
    ):
        ManagedServiceConfig(
            socket_path=tmp_path / "control.sock",
            database_path=tmp_path / "managed-projection.sqlite3",
            allowed_uid=1000,
            shutdown_timeout_seconds=61,
        )
    with pytest.raises(
        ValueError, match="socket_gid must be a nonnegative integer or None"
    ):
        ManagedServiceConfig(
            socket_path=tmp_path / "control.sock",
            database_path=tmp_path / "managed-projection.sqlite3",
            allowed_uid=1000,
            shutdown_timeout_seconds=20,
            socket_gid=True,
        )


def test_managed_service_bounds_slow_control_cleanup_and_closes_projection(
    caplog, tmp_path
) -> None:
    from hermes_fleet.managed_service import ManagedServiceConfig, run_managed_service

    class SlowControl(_ControlServer):
        async def close(self) -> None:
            await asyncio.Event().wait()

    projection = _Projection()
    control = SlowControl()

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await asyncio.wait_for(
            run_managed_service(
                ManagedServiceConfig(
                    socket_path=tmp_path / "control.sock",
                    database_path=tmp_path / "managed-projection.sqlite3",
                    allowed_uid=1000,
                    shutdown_timeout_seconds=1,
                ),
                projection_factory=lambda **_kwargs: projection,
                control_factory=lambda **_kwargs: control,
                shutdown=shutdown,
            ),
            timeout=1.5,
        )

    with caplog.at_level(logging.ERROR):
        asyncio.run(exercise())

    assert projection.closed is True
    assert "timed out closing local control" in caplog.text


def test_managed_service_parser_requires_explicit_safe_arguments(tmp_path) -> None:
    from hermes_fleet.managed_service import _config_from_args, _parser

    parser = _parser()
    args = parser.parse_args(
        [
            "--socket",
            str(tmp_path / "control.sock"),
            "--database",
            str(tmp_path / "managed-projection.sqlite3"),
            "--allowed-uid",
            "1000",
            "--socket-gid",
            "1001",
            "--log-level",
            "INFO",
        ]
    )

    config = _config_from_args(args)
    assert config.socket_path == tmp_path / "control.sock"
    assert config.database_path == tmp_path / "managed-projection.sqlite3"
    assert config.allowed_uid == 1000
    assert config.socket_gid == 1001
    assert config.shutdown_timeout_seconds == 20
    with pytest.raises(SystemExit):
        parser.parse_args(["--socket", str(tmp_path / "control.sock")])
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--socket",
                str(tmp_path / "control.sock"),
                "--database",
                str(tmp_path / "managed-projection.sqlite3"),
                "--allowed-uid",
                "1000",
                "--log-level",
                "NOTSET",
            ]
        )


def test_default_factories_wire_managed_projection_and_local_control(
    monkeypatch, tmp_path
) -> None:
    from hermes_fleet.managed_service import _control_factory, _projection_factory

    projection_module = ModuleType("hermes_fleet.managed_projection")
    control_module = ModuleType("hermes_fleet.local_control")
    created: dict[str, object] = {}

    class ManagedProjectionStore:
        def __init__(self, path: Path) -> None:
            created["database_path"] = path

    class LocalControlServer:
        def __init__(
            self,
            *,
            socket_path: Path,
            allowed_uid: int,
            socket_gid: int | None,
            managed_projection: object,
        ) -> None:
            created["socket_path"] = socket_path
            created["allowed_uid"] = allowed_uid
            created["socket_gid"] = socket_gid
            created["managed_projection"] = managed_projection

    projection_module.ManagedProjectionStore = ManagedProjectionStore
    control_module.LocalControlServer = LocalControlServer
    monkeypatch.setitem(
        sys.modules, "hermes_fleet.managed_projection", projection_module
    )
    monkeypatch.setitem(sys.modules, "hermes_fleet.local_control", control_module)

    projection = _projection_factory(database_path=tmp_path / "managed.sqlite3")
    control = _control_factory(
        socket_path=tmp_path / "control.sock",
        allowed_uid=1000,
        socket_gid=1001,
        managed_projection=projection,
    )

    assert isinstance(control, LocalControlServer)
    assert created == {
        "database_path": tmp_path / "managed.sqlite3",
        "socket_path": tmp_path / "control.sock",
        "allowed_uid": 1000,
        "socket_gid": 1001,
        "managed_projection": projection,
    }


def test_managed_service_installs_sigint_and_sigterm_shutdown_handlers() -> None:
    from hermes_fleet.managed_service import _install_shutdown_handlers

    class Loop:
        def __init__(self) -> None:
            self.handlers: dict[int, object] = {}

        def add_signal_handler(self, signum: int, callback) -> None:
            self.handlers[signum] = callback

    shutdown = asyncio.Event()
    loop = Loop()

    _install_shutdown_handlers(loop, shutdown)

    assert set(loop.handlers) == {signal.SIGINT, signal.SIGTERM}
    for callback in loop.handlers.values():
        callback()
    assert shutdown.is_set()


def test_managed_projection_systemd_unit_uses_explicit_bounded_service_arguments() -> (
    None
):
    unit_path = (
        Path(__file__).resolve().parents[2]
        / "ops"
        / "systemd"
        / "fleet-managed-projection.service"
    )
    unit = unit_path.read_text(encoding="utf-8")

    assert "Type=simple" in unit
    assert (
        "EnvironmentFile=%h/.config/hermes-fleet/fleet-managed-projection.env" in unit
    )
    assert "RuntimeDirectory=" not in unit
    assert "--socket ${FLEET_MANAGED_PROJECTION_SOCKET}" in unit
    assert "--database ${FLEET_MANAGED_PROJECTION_DATABASE}" in unit
    assert "--allowed-uid ${FLEET_MANAGED_PROJECTION_ALLOWED_UID}" in unit
    # The shipped unit must launch in the documented same-UID mode when no
    # optional cross-UID group is configured. Cross-UID deployments add the
    # socket GID through the documented ExecStart drop-in.
    assert "%h/.local/bin/fleet-managed-control" in unit
    assert "--socket-gid ${FLEET_MANAGED_PROJECTION_SOCKET_GID}" not in unit
    assert "--shutdown-timeout" not in unit
    assert "--log-level" not in unit
    assert "UMask=0077" in unit
    assert "Provision the exact socket and database parent directories" in unit
    assert "/bin/sh" not in unit
    assert "KillMode=mixed" in unit
    assert "KillSignal=SIGTERM" in unit
    assert "TimeoutStopSec=30" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
