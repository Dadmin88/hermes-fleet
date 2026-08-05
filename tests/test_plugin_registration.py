"""Public Hermes-plugin registration behavior tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest


class PublicContext:
    """Minimal public registration surface; no private Hermes internals exist here."""

    def __init__(self) -> None:
        self.cli: list[dict] = []
        self.tools: list[dict] = []

    def register_cli_command(self, **kwargs) -> None:
        self.cli.append(kwargs)

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)


def test_fleet_init_is_profile_scoped_and_idempotent_for_valid_empty_cache(
    tmp_path, monkeypatch
) -> None:
    """The placeholder command initializes only its active profile state root."""
    from hermes_fleet import cli

    state_dir = tmp_path / "profile" / "fleet"
    monkeypatch.setattr(cli, "get_fleet_dir", lambda: state_dir)
    parser = argparse.ArgumentParser()
    cli.setup_fleet_parser(parser)
    args = parser.parse_args(["init"])

    cli.handle_fleet_cli(args)
    cache_path = state_dir / "cache.json"
    os.utime(cache_path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    expected_mtime = cache_path.stat().st_mtime_ns
    cli.handle_fleet_cli(args)

    assert cache_path.stat().st_mtime_ns == expected_mtime


def test_plugin_uses_public_registration_and_returns_stable_json_tool_result() -> None:
    """Phase 1 exposes only ``fleet init`` and its non-networking list placeholder."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("fleet_plugin", root / "__init__.py")
    assert spec and spec.loader
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    context = PublicContext()

    plugin.register(context)

    assert len(context.cli) == 1
    assert len(context.tools) == 1
    command = context.cli[0]
    assert set(command) == {
        "name",
        "help",
        "setup_fn",
        "handler_fn",
        "description",
    }
    assert command["name"] == "fleet"
    assert callable(command["setup_fn"])
    assert callable(command["handler_fn"])
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    assert vars(parser.parse_args(["init"])) == {"fleet_command": "init"}
    with pytest.raises(SystemExit):
        parser.parse_args(["list"])

    tool = context.tools[0]
    assert tool["name"] == "fleet_list_nodes"
    assert tool["toolset"] == "fleet"
    assert callable(tool["handler"])
    assert set(tool["schema"]) == {"name", "description", "parameters"}
    assert tool["schema"]["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    handler = cast(Callable[[dict[str, Any]], str], tool["handler"])
    assert json.loads(handler({})) == {
        "success": True,
        "data": [],
        "errors": [],
        "warnings": ["Fleet dispatch is not available in Phase 1."],
    }
