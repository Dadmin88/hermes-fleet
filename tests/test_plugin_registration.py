"""Public Hermes-plugin registration behavior tests."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


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


def test_plugin_registers_bounded_async_fleet_surfaces() -> None:
    root = Path(__file__).resolve().parents[1]
    module_name = "fleet_plugin"
    spec = importlib.util.spec_from_file_location(
        module_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    assert spec and spec.loader
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = plugin
    try:
        spec.loader.exec_module(plugin)
        context = PublicContext()
        plugin.register(context)
    finally:
        for loaded_name in tuple(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(f"{module_name}."):
                sys.modules.pop(loaded_name, None)

    assert len(context.cli) == 1
    assert {tool["name"] for tool in context.tools} == {
        "fleet_list_nodes",
        "fleet_get_node",
        "fleet_get_health",
        "fleet_send_message",
        "fleet_get_task",
        "fleet_cancel_task",
    }
    assert "fleet_run" not in {tool["name"] for tool in context.tools}
    assert all(tool["toolset"] == "fleet" for tool in context.tools)
    assert all(tool["is_async"] is True for tool in context.tools)
    assert all(callable(tool["handler"]) for tool in context.tools)
    assert all(
        set(tool["schema"]) == {"name", "description", "parameters"}
        for tool in context.tools
    )
    assert all(
        set(tool["schema"]["parameters"])
        == {"type", "properties", "required", "additionalProperties"}
        for tool in context.tools
    )
    assert all(
        tool["schema"]["parameters"]["type"] == "object"
        and tool["schema"]["parameters"]["additionalProperties"] is False
        for tool in context.tools
    )

    command = context.cli[0]
    assert command["name"] == "fleet"
    assert callable(command["setup_fn"])
    assert callable(command["handler_fn"])
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    assert vars(parser.parse_args(["init"])) == {"fleet_command": "init"}
    parsed = vars(parser.parse_args(["message", "vps", "hello", "--topic", "smoke"]))
    assert parsed == {
        "fleet_command": "message",
        "name": "vps",
        "text": "hello",
        "topic": "smoke",
        "correlation_id": "",
        "deadline_seconds": 30,
    }

    task_tool = next(tool for tool in context.tools if tool["name"] == "fleet_get_task")
    handler = cast(Callable[[dict[str, Any]], Any], task_tool["handler"])
    result = json.loads(asyncio.run(handler({"task_id": "task-1"})))
    assert result["success"] is False
    assert result["errors"][0]["code"] == "FLEET_TASK_UNAVAILABLE"
