"""Authenticated Fleet Desktop backend routes.

Hermes mounts this router at ``/api/plugins/hermes-fleet``. The handlers remain
thin and consume Fleet's authoritative local-control projection rather than
reading Fleet SQLite state.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

get_fleet_dir = importlib.import_module("hermes_fleet.config").get_fleet_dir
DesktopApiClient = importlib.import_module("hermes_fleet.desktop_api").DesktopApiClient

router = APIRouter()


def _socket_path() -> Path:
    configured = os.environ.get("FLEET_MANAGED_PROJECTION_SOCKET")
    if configured:
        return Path(configured).expanduser()
    return get_fleet_dir() / "managed-projection.sock"


@router.get("/overview")
async def overview() -> dict[str, Any]:
    """Return current Fleet Desktop V1 state or a bounded unavailable response."""
    try:
        return await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).overview
        )
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Desktop state is unavailable."
        ) from error
