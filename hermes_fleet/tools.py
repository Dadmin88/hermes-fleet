"""Phase-one placeholder model tool definitions."""

from __future__ import annotations

import json
from typing import Any

TOOLSET = "fleet"
FLEET_LIST_NODES_SCHEMA = {
    "name": "fleet_list_nodes",
    "description": "Report that live Hermes Fleet inventory is unavailable in Phase 1.",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}


def fleet_list_nodes(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Return a stable, non-networking Phase-1 placeholder response."""
    del args, kwargs
    return json.dumps(
        {
            "success": False,
            "data": None,
            "errors": [
                {
                    "code": "FEATURE_NOT_IMPLEMENTED",
                    "message": "Live Fleet inventory is not available in Phase 1.",
                }
            ],
            "warnings": [],
        }
    )
