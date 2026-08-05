"""Hermes Fleet general-plugin registration entry point."""


def register(ctx) -> None:
    """Register Phase-1 operator and model surfaces through public APIs only."""
    from hermes_fleet.cli import handle_fleet_cli, setup_fleet_parser
    from hermes_fleet.tools import FLEET_LIST_NODES_SCHEMA, TOOLSET, fleet_list_nodes

    ctx.register_cli_command(
        name="fleet",
        help="Initialize Hermes Fleet state",
        setup_fn=setup_fleet_parser,
        handler_fn=handle_fleet_cli,
        description="Hermes Fleet operator commands.",
    )
    ctx.register_tool(
        name="fleet_list_nodes",
        toolset=TOOLSET,
        schema=FLEET_LIST_NODES_SCHEMA,
        handler=fleet_list_nodes,
        description=FLEET_LIST_NODES_SCHEMA["description"],
        emoji="🚢",
    )
