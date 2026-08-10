"""Authenticated Fleet Desktop backend routes.

Hermes mounts this router at ``/api/plugins/hermes-fleet``. The handlers remain
thin and consume Fleet's authoritative local-control projection rather than
reading Fleet SQLite state.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

get_fleet_dir = importlib.import_module("hermes_fleet.config").get_fleet_dir
DesktopApiClient = importlib.import_module("hermes_fleet.desktop_api").DesktopApiClient

router = APIRouter()


class AliasClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: str = Field(min_length=1, max_length=256)
    network_id: str = Field(min_length=1, max_length=256)
    device_id: str = Field(min_length=1, max_length=256)
    binding_generation: str = Field(
        min_length=1, max_length=20, pattern=r"^[1-9][0-9]*$"
    )


class AliasSetRequest(AliasClearRequest):
    alias: str = Field(min_length=1, max_length=128)


def _expected_stable_id(request: AliasClearRequest) -> str:
    material = json.dumps(
        [request.source, request.network_id, request.device_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"fleet-node-{hashlib.sha256(material).hexdigest()}"


def _overview_digest(overview: dict[str, Any]) -> str:
    canonical = json.dumps(
        overview, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_event(
    kind: str, sequence: int, overview: dict[str, Any] | None = None
) -> dict[str, Any]:
    if kind not in {
        "snapshot",
        "overview_changed",
        "unavailable",
        "recovered",
        "heartbeat",
    }:
        raise ValueError("unsupported Fleet event kind")
    event: dict[str, Any] = {
        "schema": "fleet.desktop-events.v1",
        "kind": kind,
        "sequence": sequence,
    }
    if overview is not None:
        event["overview"] = overview
    return event


def _socket_path() -> Path:
    configured = os.environ.get("FLEET_MANAGED_PROJECTION_SOCKET")
    if configured:
        return Path(configured).expanduser()
    return get_fleet_dir() / "managed-projection.sock"


@router.websocket("/events")
async def events(websocket: WebSocket) -> None:
    """Stream changes derived only from validated Fleet overview snapshots."""
    await websocket.accept()
    previous_digest: str | None = None
    unavailable = False
    sequence = 0
    unchanged_cycles = 0
    retry_seconds = 1.0
    try:
        while True:
            try:
                current = await asyncio.to_thread(
                    DesktopApiClient(socket_path=_socket_path()).overview
                )
            except Exception:
                if not unavailable:
                    sequence += 1
                    await websocket.send_json(build_event("unavailable", sequence))
                unavailable = True
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2.0, 15.0)
                continue

            digest = _overview_digest(current)
            kind = None
            if previous_digest is None:
                kind = "recovered" if unavailable else "snapshot"
            elif unavailable:
                kind = "recovered"
            elif digest != previous_digest:
                kind = "overview_changed"
            if kind is None:
                unchanged_cycles += 1
                if unchanged_cycles >= 10:
                    kind = "heartbeat"
                    unchanged_cycles = 0
            else:
                unchanged_cycles = 0
            if kind is not None:
                sequence += 1
                await websocket.send_json(
                    build_event(
                        kind,
                        sequence,
                        None if kind == "heartbeat" else current,
                    )
                )
            previous_digest = digest
            unavailable = False
            retry_seconds = 1.0
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        return


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


@router.put("/nodes/{stable_id}/alias")
async def set_alias(stable_id: str, request: AliasSetRequest) -> dict[str, str]:
    """Set a presentation-only alias for the exact selected managed binding."""
    if stable_id != _expected_stable_id(request):
        raise HTTPException(status_code=400, detail="Invalid Fleet node identity.")
    try:
        outcome = await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).set_alias,
            **request.model_dump(),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid alias request.") from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=409, detail="Fleet rejected the alias update."
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Desktop state is unavailable."
        ) from error
    return {"outcome": outcome}


@router.delete("/nodes/{stable_id}/alias")
async def clear_alias(stable_id: str, request: AliasClearRequest) -> dict[str, str]:
    """Clear a presentation-only alias for the exact selected managed binding."""
    if stable_id != _expected_stable_id(request):
        raise HTTPException(status_code=400, detail="Invalid Fleet node identity.")
    try:
        outcome = await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).clear_alias,
            **request.model_dump(),
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid alias request.") from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=409, detail="Fleet rejected the alias update."
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Desktop state is unavailable."
        ) from error
    return {"outcome": outcome}
