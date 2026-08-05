"""Hermes Fleet general-plugin registration entry point."""


def register(ctx) -> None:
    """Register Fleet operator and model surfaces through public APIs only."""
    from .hermes_fleet.cli import handle_fleet_cli, setup_fleet_parser
    from .hermes_fleet.tools import TOOL_DEFINITIONS, TOOLSET

    for schema, handler in TOOL_DEFINITIONS:
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            is_async=True,
            description=schema["description"],
            emoji="🚢",
        )
    ctx.register_cli_command(
        name="fleet",
        help="Communicate with and run Hermes on exact Fleet nodes",
        setup_fn=setup_fleet_parser,
        handler_fn=handle_fleet_cli,
        description="Hermes Fleet operator commands.",
    )
