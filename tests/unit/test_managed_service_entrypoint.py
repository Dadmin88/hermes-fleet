"""Packaging-level smoke coverage for the managed-projection service CLI."""

from __future__ import annotations

import tomllib
from importlib import import_module
from pathlib import Path


def test_managed_projection_console_script_targets_a_parseable_service_main() -> None:
    repository = Path(__file__).resolve().parents[2]
    pyproject = (repository / "pyproject.toml").read_text(encoding="utf-8")
    metadata = tomllib.loads(pyproject)

    target = metadata["project"]["scripts"]["fleet-managed-projection"]

    assert target == "hermes_fleet.managed_service:main"
    module_name, function_name = target.split(":", maxsplit=1)
    service = import_module(module_name)
    args = service._parser().parse_args(
        [
            "--socket",
            "/run/fleet/managed-projection.sock",
            "--database",
            "/var/lib/fleet/managed-projection.sqlite3",
            "--allowed-uid",
            "1000",
            "--shutdown-timeout",
            "30",
            "--log-level",
            "DEBUG",
        ]
    )
    assert vars(args) == {
        "socket": Path("/run/fleet/managed-projection.sock"),
        "database": Path("/var/lib/fleet/managed-projection.sqlite3"),
        "allowed_uid": 1000,
        "shutdown_timeout": 30,
        "log_level": "DEBUG",
    }
    assert callable(getattr(service, function_name))
