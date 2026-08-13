"""Top-level Fleet Operator CLI V1 over the shared application boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .config import get_fleet_dir, load_fleet_config
from .desktop_api import DesktopApiClient
from .operator import OperatorError, OperatorErrorCode, OperatorService
from .operator_doctor import OperatorDoctor, OperatorDoctorReport

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_DENIED = 4
EXIT_NOT_READY = 5
EXIT_UNAVAILABLE = 6
EXIT_TASK_FAILED = 7
EXIT_INDETERMINATE = 8

_ERROR_EXITS = {
    OperatorErrorCode.UNKNOWN_TARGET: EXIT_NOT_FOUND,
    OperatorErrorCode.AMBIGUOUS_TARGET: EXIT_NOT_FOUND,
    OperatorErrorCode.NOT_MANAGED: EXIT_NOT_FOUND,
    OperatorErrorCode.POLICY_DENIED: EXIT_DENIED,
    OperatorErrorCode.STALE_STATE: EXIT_NOT_READY,
    OperatorErrorCode.NOT_READY: EXIT_NOT_READY,
    OperatorErrorCode.NO_CAPACITY: EXIT_NOT_READY,
    OperatorErrorCode.NO_BINDING: EXIT_UNAVAILABLE,
    OperatorErrorCode.OPERATION_UNAVAILABLE: EXIT_UNAVAILABLE,
    OperatorErrorCode.TRANSPORT_UNAVAILABLE: EXIT_UNAVAILABLE,
    OperatorErrorCode.REMOTE_REJECTED: EXIT_TASK_FAILED,
    OperatorErrorCode.HERMES_UNAVAILABLE: EXIT_UNAVAILABLE,
    OperatorErrorCode.HERMES_AUTH_FAILURE: EXIT_DENIED,
    OperatorErrorCode.DEADLINE_EXCEEDED: EXIT_TASK_FAILED,
    OperatorErrorCode.TASK_FAILED: EXIT_TASK_FAILED,
    OperatorErrorCode.TASK_INDETERMINATE: EXIT_INDETERMINATE,
}


def _socket_path() -> Path:
    configured = os.environ.get("FLEET_MANAGED_PROJECTION_SOCKET")
    return (
        Path(configured).expanduser()
        if configured
        else get_fleet_dir() / "managed-projection.sock"
    )


@dataclass
class OperatorCliContext:
    operator: OperatorService
    config_path: Path
    config: Any
    state: DesktopApiClient
    keryx: Any
    _owns_keryx: bool = True

    @classmethod
    def open(cls) -> OperatorCliContext:
        token = os.environ.get("KERYX_NODE_TOKEN", "")
        if not token:
            raise OperatorError(
                OperatorErrorCode.TRANSPORT_UNAVAILABLE,
                "Authenticated Keryx controller configuration is unavailable.",
            )
        config_path = get_fleet_dir() / "nodes.yaml"
        try:
            config = load_fleet_config(config_path)
            state = DesktopApiClient(socket_path=_socket_path())
            from keryx.node import KeryxNode

            keryx = KeryxNode(node_token=token, worker_concurrency=1)
        except OperatorError:
            raise
        except Exception as error:
            raise OperatorError(
                OperatorErrorCode.OPERATION_UNAVAILABLE,
                "Fleet operator runtime is unavailable.",
                detail=error,
            ) from error
        asyncio.run(keryx.start())
        return cls(
            operator=OperatorService(state=state, config=config, keryx=keryx),
            config_path=config_path,
            config=config,
            state=state,
            keryx=keryx,
        )

    def doctor(self) -> OperatorDoctorReport:
        try:
            nodes = self.operator.list_nodes()
        except OperatorError:
            nodes = ()
        return OperatorDoctor().run(
            config_path=self.config_path, config=self.config, nodes=nodes
        )

    def close(self) -> None:
        if self._owns_keryx:
            asyncio.run(self.keryx.stop())


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.description = "Hermes Fleet operator CLI"
    commands = parser.add_subparsers(dest="command", required=True)

    nodes = commands.add_parser("nodes", help="List authoritative managed nodes")
    _json(nodes)

    node = commands.add_parser("node", help="Inspect one managed node")
    node_commands = node.add_subparsers(dest="node_command", required=True)
    show = node_commands.add_parser("show", help="Show one managed node")
    show.add_argument("target")
    _json(show)

    readiness = commands.add_parser("readiness", help="Inspect target readiness")
    readiness.add_argument("target")
    _json(readiness)

    execute = commands.add_parser("run", help="Run exact-target Hermes work")
    execute.add_argument("target")
    execute.add_argument("prompt")
    mode = execute.add_mutually_exclusive_group()
    mode.add_argument("--wait", action="store_true", default=True)
    mode.add_argument("--detach", dest="wait", action="store_false")
    execute.add_argument("--deadline", type=int, default=120, choices=range(1, 901))
    _json(execute)

    task = commands.add_parser("task", help="Inspect durable task state")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_show = task_commands.add_parser("show", help="Show one task")
    task_show.add_argument("task_id")
    _json(task_show)

    doctor = commands.add_parser("doctor", help="Run read-only local diagnostics")
    _json(doctor)


def _json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="json_output")


def run(
    args: argparse.Namespace,
    *,
    context_factory: Callable[[], OperatorCliContext] = OperatorCliContext.open,
    stdout: Callable[[str], None] = print,
    stderr: Callable[[str], None] = print,
) -> int:
    context: OperatorCliContext | None = None
    try:
        context = context_factory()
        result = _dispatch(args, context)
        stdout(_encode(result) if args.json_output else _human(args, result))
        if args.command == "doctor" and not result.healthy:
            return EXIT_UNAVAILABLE
        if hasattr(result, "error_category") and result.error_category is not None:
            return _ERROR_EXITS[result.error_category]
        return EXIT_OK
    except OperatorError as error:
        payload = {"error": {"code": error.code.value, "message": error.public_message}}
        stderr(
            _encode(payload)
            if getattr(args, "json_output", False)
            else f"{error.code.value}: {error.public_message}"
        )
        return _ERROR_EXITS[error.code]
    except (ValueError, OSError, RuntimeError) as error:
        public = OperatorError(
            OperatorErrorCode.OPERATION_UNAVAILABLE,
            "Fleet operator command is unavailable.",
            detail=error,
        )
        payload = {
            "error": {"code": public.code.value, "message": public.public_message}
        }
        stderr(
            _encode(payload)
            if getattr(args, "json_output", False)
            else f"{public.code.value}: {public.public_message}"
        )
        return EXIT_UNAVAILABLE
    finally:
        if context is not None:
            context.close()


def _dispatch(args: argparse.Namespace, context: OperatorCliContext) -> Any:
    if args.command == "nodes":
        return {"nodes": context.operator.list_nodes()}
    if args.command == "node":
        return context.operator.inspect_node(args.target)
    if args.command == "readiness":
        return context.operator.inspect_readiness(args.target)
    if args.command == "run":
        action = (
            context.operator.run_exact if args.wait else context.operator.submit_exact
        )
        return asyncio.run(
            action(args.target, args.prompt, deadline_seconds=args.deadline)
        )
    if args.command == "task":
        return asyncio.run(context.operator.inspect_task(args.task_id))
    if args.command == "doctor":
        return context.doctor()
    raise ValueError("unknown Fleet command")


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _encode(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"))


def _human(args: argparse.Namespace, value: Any) -> str:
    data = _plain(value)
    if args.command == "nodes":
        rows = data["nodes"]
        if not rows:
            return "No managed Fleet nodes."
        return "\n".join(
            f"{row['identity']['display_name']}\t{row['managed_state']}\t"
            f"{'ready' if row['readiness']['scheduler_ready'] else 'not-ready'}"
            for row in rows
        )
    if args.command == "readiness":
        reasons = ",".join(data["reasons"]) or "none"
        ready = str(data["scheduler_ready"]).lower()
        fresh = str(data["fresh"]).lower()
        return f"ready={ready} fresh={fresh} reasons={reasons}"
    if args.command == "doctor":
        if data["healthy"]:
            return "Fleet doctor: healthy"
        return "\n".join(
            f"{item['status'].upper()} {item['code']}: {item['message']}"
            for item in data["findings"]
        )
    if "task_id" in data:
        return f"{data['task_id']}\t{data['terminal_state']}"
    return json.dumps(data, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet")
    setup_parser(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
