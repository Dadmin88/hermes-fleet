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
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

get_fleet_dir = importlib.import_module("hermes_fleet.config").get_fleet_dir
DesktopApiClient = importlib.import_module("hermes_fleet.desktop_api").DesktopApiClient
NodescaleObservationClient = importlib.import_module(
    "hermes_fleet.nodescale_observations"
).NodescaleObservationClient

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


class WorkflowCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    document: dict[str, object]


class WorkflowUpdateRequest(WorkflowCreateRequest):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    expected_version: int = Field(alias="expectedVersion", ge=1, le=(1 << 64) - 1)


class WorkflowDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    expected_version: int = Field(alias="expectedVersion", ge=1, le=(1 << 64) - 1)


class ExactRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    target: str = Field(min_length=1, max_length=512)
    prompt: str = Field(min_length=1, max_length=65_536)
    deadline_seconds: int = Field(alias="deadlineSeconds", ge=1, le=900)


async def open_operator_context():
    """Open the same operator runtime used by the CLI adapter."""
    context_type = importlib.import_module(
        "hermes_fleet.operator_cli"
    ).OperatorCliContext
    return await context_type.open()


def _enum_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _run_document(
    result: object, *, submission_stages_observed: bool = False
) -> dict[str, Any]:
    terminal_state = getattr(result, "terminal_state", None)
    terminal = terminal_state not in {
        "submitted",
        "pending",
        "working",
        "running",
        "leased",
    }
    error_category = _enum_value(getattr(result, "error_category", None))
    if not terminal:
        completion_state = "pending"
    elif error_category == "TASK_INDETERMINATE":
        completion_state = "indeterminate"
    elif error_category is not None:
        completion_state = "failed"
    else:
        completion_state = "completed"
    stages = []
    if submission_stages_observed:
        stages.extend(
            {"id": stage, "state": "completed"}
            for stage in (
                "operator_request",
                "target_resolution",
                "authorization",
                "readiness",
            )
        )
    stages.extend(
        (
            {"id": "durable_submission", "state": "observed"},
            {"id": "completion", "state": completion_state},
        )
    )
    return {
        "schema": "fleet.desktop-run.v1",
        "taskId": getattr(result, "task_id", None),
        "state": terminal_state,
        "runId": getattr(result, "run_id", None),
        "result": getattr(result, "result", None),
        "errorCategory": error_category,
        "stages": stages,
    }


def _expected_stable_id(request: AliasClearRequest) -> str:
    material = json.dumps(
        [request.source, request.network_id, request.device_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"fleet-node-{hashlib.sha256(material).hexdigest()}"


def _overview_digest(overview: dict[str, Any]) -> str:
    def stable(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: stable(item)
                for key, item in value.items()
                if key != "observation_age_ms"
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    canonical = json.dumps(
        stable(overview), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_event(kind: str, sequence: int) -> dict[str, Any]:
    if kind not in {
        "snapshot",
        "overview_changed",
        "unavailable",
        "recovered",
        "heartbeat",
    }:
        raise ValueError("unsupported Fleet event kind")
    return {
        "schema": "fleet.desktop-events.v1",
        "kind": kind,
        "sequence": sequence,
    }


def _socket_path() -> Path:
    configured = os.environ.get("FLEET_MANAGED_PROJECTION_SOCKET")
    if configured:
        return Path(configured).expanduser()
    return get_fleet_dir() / "managed-projection.sock"


def _observation_config() -> tuple[Path, str] | None:
    socket_value = os.environ.get("NODESCALE_OBSERVATION_SOCKET")
    network_id = os.environ.get("NODESCALE_OBSERVATION_NETWORK_ID")
    if socket_value is not None or network_id is not None:
        if not socket_value or not network_id:
            raise ValueError("incomplete Nodescale observation configuration")
        return Path(socket_value).expanduser(), network_id

    config_path = get_fleet_dir() / "nodescale-observations.json"
    try:
        metadata = config_path.lstat()
    except FileNotFoundError:
        return None
    if config_path.is_symlink() or not config_path.is_file() or metadata.st_size > 4096:
        raise ValueError("invalid Nodescale observation configuration")
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Nodescale observation configuration") from exc
    if type(payload) is not dict or set(payload) != {
        "schema",
        "socket_path",
        "network_id",
    }:
        raise ValueError("invalid Nodescale observation configuration")
    socket_value = payload.get("socket_path")
    network_id = payload.get("network_id")
    if (
        payload.get("schema") != "fleet.nodescale-observations.v1"
        or type(socket_value) is not str
        or not socket_value
        or len(socket_value) > 1024
        or not Path(socket_value).is_absolute()
        or type(network_id) is not str
        or not network_id
        or len(network_id) > 256
    ):
        raise ValueError("invalid Nodescale observation configuration")
    return Path(socket_value), network_id


def _compose_overview(
    managed: dict[str, Any], observed: dict[str, Any] | None, *, observation_state: str
) -> dict[str, Any]:
    if (
        type(managed) is not dict
        or managed.get("schema") != "fleet.desktop.v1"
        or set(managed) != {"schema", "summary", "nodes"}
        or type(managed.get("summary")) is not dict
        or type(managed.get("nodes")) is not list
    ):
        raise RuntimeError("invalid managed Fleet Desktop overview")
    if observation_state == "available":
        if (
            type(observed) is not dict
            or set(observed)
            != {
                "schema",
                "network_id",
                "reconciliation",
                "observations",
                "truncated",
            }
            or observed.get("schema") != "nodescale.observations.v1"
            or type(observed.get("observations")) is not list
            or type(observed.get("truncated")) is not bool
        ):
            raise RuntimeError("invalid Nodescale observation overview")
        observed_nodes = observed["observations"]
        observation_status = {
            "state": "available",
            "network_id": observed["network_id"],
            "reconciliation": observed["reconciliation"],
            "truncated": observed["truncated"],
        }
    else:
        observed_nodes = []
        observation_status = {
            "state": observation_state,
            "network_id": observed.get("network_id") if observed else None,
            "reconciliation": None,
            "truncated": False,
        }
    return {
        "schema": "fleet.desktop.v2",
        "summary": {
            **managed["summary"],
            "observed_unmanaged": len(observed_nodes),
        },
        "nodes": managed["nodes"],
        "observed_nodes": observed_nodes,
        "observations": observation_status,
    }


async def _overview_document() -> dict[str, Any]:
    managed = await asyncio.to_thread(
        DesktopApiClient(socket_path=_socket_path()).overview
    )
    try:
        observation_config = _observation_config()
    except ValueError:
        return _compose_overview(
            managed,
            {"network_id": os.environ.get("NODESCALE_OBSERVATION_NETWORK_ID")},
            observation_state="unavailable",
        )
    if observation_config is None:
        return _compose_overview(managed, None, observation_state="disabled")
    socket_path, network_id = observation_config
    try:
        observed = await asyncio.to_thread(
            NodescaleObservationClient(
                socket_path=socket_path, network_id=network_id
            ).overview
        )
    except Exception:
        return _compose_overview(
            managed,
            {"network_id": network_id},
            observation_state="unavailable",
        )
    return _compose_overview(managed, observed, observation_state="available")


def _websocket_rejection_code(websocket: WebSocket) -> int | None:
    try:
        module = importlib.import_module("hermes_cli.web_server")
        auth_ok = getattr(module, "_ws_auth_ok")
        request_is_allowed = getattr(module, "_ws_request_is_allowed")
    except (ImportError, AttributeError):
        return 1011
    if not auth_ok(websocket):
        return 4401
    if not request_is_allowed(websocket):
        return 4403
    return None


@router.websocket("/events")
async def events(websocket: WebSocket) -> None:
    """Signal changes derived only from validated Fleet overview snapshots."""
    rejection_code = _websocket_rejection_code(websocket)
    if rejection_code is not None:
        await websocket.close(code=rejection_code)
        return
    await websocket.accept()
    previous_digest: str | None = None
    unavailable = False
    sequence = 0
    unchanged_cycles = 0
    retry_seconds = 1.0
    try:
        while True:
            try:
                current = await _overview_document()
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
                await websocket.send_json(build_event(kind, sequence))
            previous_digest = digest
            unavailable = False
            retry_seconds = 1.0
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        return


@router.get("/overview")
async def overview() -> dict[str, Any]:
    """Return managed Fleet authority plus distinct observed provider evidence."""
    try:
        return await _overview_document()
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Desktop state is unavailable."
        ) from error


@router.post("/runs")
async def submit_exact_run(request: ExactRunRequest) -> dict[str, Any]:
    """Submit exact-target Hermes work through the shared operator service."""
    context = None
    try:
        context = await open_operator_context()
        result = await context.operator.submit_exact(
            request.target,
            request.prompt,
            deadline_seconds=request.deadline_seconds,
        )
        response = _run_document(result, submission_stages_observed=True)
        return response
    except Exception as error:
        operator_module = importlib.import_module("hermes_fleet.operator")
        operator_error = getattr(operator_module, "OperatorError")
        if isinstance(error, operator_error):
            public_error: Any = error
            code = _enum_value(public_error.code)
            status = 403 if code == "POLICY_DENIED" else 409
            raise HTTPException(
                status_code=status,
                detail={"code": code, "message": public_error.public_message},
            ) from error
        raise HTTPException(
            status_code=503,
            detail={
                "code": "operation_unavailable",
                "message": "Fleet execution is unavailable.",
            },
        ) from error
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                # Cleanup is secondary to the route's response. Never hide a
                # durable task identity or a truthful operator error with it.
                pass


@router.get("/tasks/{task_id}")
async def inspect_exact_task(task_id: str) -> dict[str, Any]:
    """Reattach to one durable Keryx task without resubmitting work."""
    if not task_id or len(task_id) > 512:
        raise HTTPException(status_code=400, detail="Invalid Fleet task identity.")
    context = None
    try:
        context = await open_operator_context()
        result = await context.operator.inspect_task(task_id)
        return _run_document(result)
    except HTTPException:
        raise
    except Exception as error:
        operator_module = importlib.import_module("hermes_fleet.operator")
        operator_error = getattr(operator_module, "OperatorError")
        if isinstance(error, operator_error):
            public_error: Any = error
            raise HTTPException(
                status_code=409,
                detail={
                    "code": _enum_value(public_error.code),
                    "message": public_error.public_message,
                },
            ) from error
        raise HTTPException(
            status_code=503,
            detail={
                "code": "operation_unavailable",
                "message": "Fleet task status is unavailable.",
            },
        ) from error
    finally:
        if context is not None:
            await context.close()


@router.get("/workflows")
async def list_workflows() -> dict[str, Any]:
    """List active backend-owned Workflow definitions without run state."""
    try:
        workflows = await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).list_workflows
        )
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Workflow state is unavailable."
        ) from error
    return {"workflows": workflows, "executionAvailable": False}


@router.post("/workflows")
async def create_workflow(request: WorkflowCreateRequest) -> dict[str, Any]:
    """Create durable immutable Workflow version 1."""
    try:
        return await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).create_workflow,
            request.document,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="Invalid Workflow document."
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=409, detail="Fleet rejected Workflow creation."
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Workflow state is unavailable."
        ) from error


@router.get("/workflows/{workflow_id}")
async def read_workflow(workflow_id: str) -> dict[str, Any]:
    try:
        revision = await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).read_workflow,
            workflow_id,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="Invalid Workflow identity."
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Workflow state is unavailable."
        ) from error
    if revision is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return {"revision": revision, "executionAvailable": False}


@router.get("/workflows/{workflow_id}/versions/{version}")
async def read_workflow_version(workflow_id: str, version: int) -> dict[str, Any]:
    try:
        revision = await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).read_workflow_version,
            workflow_id,
            version=version,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="Invalid Workflow version."
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Workflow state is unavailable."
        ) from error
    if revision is None:
        raise HTTPException(status_code=404, detail="Workflow version not found.")
    return {"revision": revision, "executionAvailable": False}


@router.put("/workflows/{workflow_id}")
async def update_workflow(
    workflow_id: str, request: WorkflowUpdateRequest
) -> dict[str, Any]:
    if request.document.get("id") != workflow_id:
        raise HTTPException(status_code=400, detail="Workflow identity mismatch.")
    try:
        return await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).update_workflow,
            request.document,
            expected_version=request.expected_version,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="Invalid Workflow document."
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=409, detail="Fleet rejected Workflow update."
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Workflow state is unavailable."
        ) from error


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(
    workflow_id: str, request: WorkflowDeleteRequest
) -> dict[str, str]:
    try:
        outcome = await asyncio.to_thread(
            DesktopApiClient(socket_path=_socket_path()).delete_workflow,
            workflow_id,
            expected_version=request.expected_version,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400, detail="Invalid Workflow identity."
        ) from error
    except RuntimeError as error:
        raise HTTPException(
            status_code=409, detail="Fleet rejected Workflow deletion."
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=503, detail="Fleet Workflow state is unavailable."
        ) from error
    return {"outcome": outcome}


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
