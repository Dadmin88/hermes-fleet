"""Operator CLI wiring for Hermes Fleet communication and execution."""

from __future__ import annotations

import asyncio

from .config import get_fleet_dir
from .inventory import initialize_inventory_state


def setup_fleet_parser(parser) -> None:
    """Add the bounded Fleet command tree."""
    subparsers = parser.add_subparsers(dest="fleet_command")
    subparsers.add_parser(
        "init", help="Create Fleet state without overwriting existing files"
    )
    subparsers.add_parser("list", help="List configured nodes with live Keryx state")

    show = subparsers.add_parser("show", help="Request safe inventory from one node")
    show.add_argument("name")
    _deadline(show, 30)

    health = subparsers.add_parser("health", help="Request node capability health")
    health.add_argument("name")
    _deadline(health, 30)

    inventory = subparsers.add_parser("inventory", help="Request safe node inventory")
    inventory.add_argument("name")
    _deadline(inventory, 30)

    message = subparsers.add_parser(
        "message", help="Send text without starting remote Hermes"
    )
    message.add_argument("name")
    message.add_argument("text")
    message.add_argument("--topic", default="")
    message.add_argument("--correlation-id", default="")
    _deadline(message, 30)

    run = subparsers.add_parser("run", help="Deliberately start remote Hermes")
    run.add_argument("name")
    run.add_argument("prompt")
    _deadline(run, 120)

    status = subparsers.add_parser("status", help="Inspect Keryx-backed task status")
    status.add_argument("task_id")
    cancel = subparsers.add_parser(
        "cancel", help="Cancel executable work when supported"
    )
    cancel.add_argument("task_id")


def handle_fleet_cli(args) -> None:
    """Run one Fleet operator command and print stable JSON output."""
    command = getattr(args, "fleet_command", None)
    if command == "init":
        initialize_inventory_state(get_fleet_dir())
        return

    from .tools import (
        fleet_cancel_task,
        fleet_get_health,
        fleet_get_node,
        fleet_get_task,
        fleet_list_nodes,
        fleet_run,
        fleet_send_message,
    )

    if command == "list":
        result = asyncio.run(fleet_list_nodes({}))
    elif command in {"show", "inventory"}:
        result = asyncio.run(
            fleet_get_node(
                {
                    "name": args.name,
                    "deadline_seconds": args.deadline_seconds,
                }
            )
        )
    elif command == "health":
        result = asyncio.run(
            fleet_get_health(
                {
                    "name": args.name,
                    "deadline_seconds": args.deadline_seconds,
                }
            )
        )
    elif command == "message":
        result = asyncio.run(
            fleet_send_message(
                {
                    "name": args.name,
                    "text": args.text,
                    "topic": args.topic,
                    "correlation_id": args.correlation_id,
                    "deadline_seconds": args.deadline_seconds,
                }
            )
        )
    elif command == "run":
        result = asyncio.run(
            fleet_run(
                {
                    "name": args.name,
                    "prompt": args.prompt,
                    "deadline_seconds": args.deadline_seconds,
                }
            )
        )
    elif command == "status":
        result = asyncio.run(fleet_get_task({"task_id": args.task_id}))
    elif command == "cancel":
        result = asyncio.run(fleet_cancel_task({"task_id": args.task_id}))
    else:
        raise ValueError("a Fleet command is required")
    print(result)


def _deadline(parser, default: int) -> None:
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=default,
        choices=range(1, 901),
        metavar="SECONDS",
    )
