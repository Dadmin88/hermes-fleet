"""Operator CLI wiring for Hermes Fleet Phase 1."""

from __future__ import annotations

from .config import get_fleet_dir
from .inventory import initialize_inventory_state


def setup_fleet_parser(parser) -> None:
    """Add the intentionally small Phase-1 Fleet command tree."""
    subparsers = parser.add_subparsers(dest="fleet_command")
    subparsers.add_parser(
        "init", help="Create Fleet state without overwriting existing files"
    )


def handle_fleet_cli(args) -> None:
    """Initialize profile-scoped Fleet state for ``hermes fleet init``."""
    if getattr(args, "fleet_command", None) == "init":
        initialize_inventory_state(get_fleet_dir())
