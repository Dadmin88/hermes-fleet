"""Hermes model tools for live Fleet communication and execution."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any, cast

from .controller import FleetController, FleetOperationResult
from .controller_runtime import run_controller_action
from .live_inventory import list_live_nodes

TOOLSET = "fleet"


def _schema(name: str, description: str, properties: dict[str, Any], required=()):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }


_NAME = {"type": "string", "minLength": 1, "maxLength": 63}
_DEADLINE_30 = {"type": "integer", "minimum": 1, "maximum": 300, "default": 30}

FLEET_LIST_NODES_SCHEMA = _schema(
    "fleet_list_nodes",
    "List configured Fleet nodes with truthful live Keryx reachability.",
    {},
)
FLEET_GET_NODE_SCHEMA = _schema(
    "fleet_get_node",
    "Request safe inventory from one exact Fleet node.",
    {"name": _NAME, "deadline_seconds": _DEADLINE_30},
    ("name",),
)
FLEET_GET_HEALTH_SCHEMA = _schema(
    "fleet_get_health",
    "Request bounded adapter, Keryx, and Hermes capability health.",
    {"name": _NAME, "deadline_seconds": _DEADLINE_30},
    ("name",),
)
FLEET_SEND_MESSAGE_SCHEMA = _schema(
    "fleet_send_message",
    "Send bounded text to one exact Fleet node without starting Hermes.",
    {
        "name": _NAME,
        "text": {"type": "string", "minLength": 1, "maxLength": 4096},
        "topic": {"type": "string", "maxLength": 64, "default": ""},
        "correlation_id": {
            "type": "string",
            "maxLength": 128,
            "default": "",
        },
        "deadline_seconds": _DEADLINE_30,
    },
    ("name", "text"),
)
FLEET_RUN_SCHEMA = _schema(
    "fleet_run",
    "Deliberately run one bounded Hermes prompt on one exact Fleet node.",
    {
        "name": _NAME,
        "prompt": {"type": "string", "minLength": 1, "maxLength": 16000},
        "deadline_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 900,
            "default": 120,
        },
    },
    ("name", "prompt"),
)
FLEET_GET_TASK_SCHEMA = _schema(
    "fleet_get_task",
    "Inspect a Keryx-backed Fleet communication by task ID when supported.",
    {"task_id": {"type": "string", "minLength": 1, "maxLength": 512}},
    ("task_id",),
)
FLEET_CANCEL_TASK_SCHEMA = _schema(
    "fleet_cancel_task",
    "Cancel executable Fleet work by Keryx task ID when supported.",
    {"task_id": {"type": "string", "minLength": 1, "maxLength": 256}},
    ("task_id",),
)


def _arguments(args: dict[str, Any] | None) -> dict[str, Any]:
    if type(args) is not dict:
        raise ValueError("tool arguments must be a JSON object")
    return args


def _text_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _success(data: Any, *, warnings: list[dict[str, str]] | None = None) -> str:
    return _text_result(
        {
            "success": True,
            "data": data,
            "errors": [],
            "warnings": warnings or [],
        }
    )


def _failure(code: str, message: str) -> str:
    return _text_result(
        {
            "success": False,
            "data": None,
            "errors": [{"code": code, "message": message}],
            "warnings": [],
        }
    )


async def _run_action(action):
    return await run_controller_action(
        action,
        node_token=os.environ.get("KERYX_NODE_TOKEN", ""),
    )


def _operation_data(result: FleetOperationResult) -> dict[str, Any]:
    return {
        "operation": result.operation,
        "target": result.target,
        "task_id": result.task_id,
        "routed_to": result.routed_to,
        "delivery_route": result.delivery_route,
        "response": result.response,
        "untrusted": result.untrusted,
    }


async def fleet_list_nodes(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    del kwargs
    try:
        if _arguments(args):
            raise ValueError("fleet_list_nodes accepts no arguments")

        async def action(node, config):
            views = await list_live_nodes(cast(Any, node), config)
            return [asdict(view) for view in views]

        return _success(await _run_action(action))
    except ValueError:
        return _failure("INVALID_REQUEST", "Fleet list request is invalid.")
    except Exception:
        return _failure("FLEET_UNAVAILABLE", "Live Fleet inventory is unavailable.")


async def fleet_get_node(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    del kwargs
    try:
        values = _arguments(args)
        name = values["name"]
        deadline = values.get("deadline_seconds", 30)

        async def action(node, config):
            return await FleetController(
                keryx=cast(Any, node), config=config
            ).get_inventory(name, deadline_seconds=deadline)

        return _success(_operation_data(await _run_action(action)))
    except (KeyError, TypeError, ValueError):
        return _failure("INVALID_REQUEST", "Fleet node request is invalid.")
    except Exception:
        return _failure("FLEET_UNAVAILABLE", "Fleet node inventory is unavailable.")


async def fleet_get_health(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    del kwargs
    try:
        values = _arguments(args)
        name = values["name"]
        deadline = values.get("deadline_seconds", 30)

        async def action(node, config):
            return await FleetController(
                keryx=cast(Any, node), config=config
            ).get_health(name, deadline_seconds=deadline)

        return _success(_operation_data(await _run_action(action)))
    except (KeyError, TypeError, ValueError):
        return _failure("INVALID_REQUEST", "Fleet health request is invalid.")
    except Exception:
        return _failure("FLEET_UNAVAILABLE", "Fleet node health is unavailable.")


async def fleet_send_message(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    del kwargs
    try:
        values = _arguments(args)
        name = values["name"]
        text = values["text"]
        topic = values.get("topic", "")
        correlation_id = values.get("correlation_id", "")
        deadline = values.get("deadline_seconds", 30)

        async def action(node, config):
            return await FleetController(
                keryx=cast(Any, node), config=config
            ).send_message(
                name,
                text,
                topic=topic,
                correlation_id=correlation_id,
                deadline_seconds=deadline,
            )

        return _success(_operation_data(await _run_action(action)))
    except (KeyError, TypeError, ValueError):
        return _failure("INVALID_REQUEST", "Fleet message request is invalid.")
    except Exception:
        return _failure("FLEET_UNAVAILABLE", "Fleet message could not be delivered.")


async def fleet_run(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    del kwargs
    try:
        values = _arguments(args)
        name = values["name"]
        prompt = values["prompt"]
        deadline = values.get("deadline_seconds", 120)

        async def action(node, config):
            return await FleetController(
                keryx=cast(Any, node), config=config
            ).run_hermes(name, prompt, deadline_seconds=deadline)

        return _success(_operation_data(await _run_action(action)))
    except (KeyError, TypeError, ValueError):
        return _failure("INVALID_REQUEST", "Fleet run request is invalid.")
    except Exception:
        return _failure("FLEET_UNAVAILABLE", "Fleet Hermes execution is unavailable.")


async def fleet_get_task(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    del kwargs
    try:
        values = _arguments(args)
        task_id = values["task_id"]
        if type(task_id) is not str or not task_id or len(task_id) > 512:
            raise ValueError("invalid task ID")
    except (KeyError, TypeError, ValueError):
        return _failure("INVALID_REQUEST", "Fleet task request is invalid.")

    try:

        async def action(node, _config):
            task_handle = getattr(node, "task_handle", None)
            if not callable(task_handle):
                raise RuntimeError("pinned Keryx SDK cannot reopen tasks")
            handle = cast(Any, task_handle(task_id))
            task = await handle.refresh()
            status = getattr(getattr(task, "status", None), "value", None)
            if type(status) is not str or not status:
                raise RuntimeError("Keryx returned invalid task status")
            metadata = getattr(task, "metadata", None)
            safe_metadata = {}
            if type(metadata) is dict:
                for key in (
                    "result_text",
                    "executor_peer_id",
                    "duration_ms",
                    "error_reason",
                ):
                    value = metadata.get(key)
                    if type(value) is str and len(value) <= 65_536:
                        safe_metadata[key] = value
            return {
                "task_id": task_id,
                "status": status,
                "result": safe_metadata,
                "untrusted": "result_text" in safe_metadata,
            }

        return _success(await _run_action(action))
    except Exception:
        return _failure("FLEET_TASK_UNAVAILABLE", "Fleet task status is unavailable.")


async def fleet_cancel_task(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    del kwargs
    try:
        values = _arguments(args)
        if type(values["task_id"]) is not str or not values["task_id"]:
            raise ValueError("invalid task ID")
    except (KeyError, TypeError, ValueError):
        return _failure("INVALID_REQUEST", "Fleet cancellation request is invalid.")
    return _failure(
        "KERYX_REMOTE_CANCEL_UNAVAILABLE",
        "Remote cancellation cannot yet prove that the Fleet worker stopped Hermes.",
    )


TOOL_DEFINITIONS = (
    (FLEET_LIST_NODES_SCHEMA, fleet_list_nodes),
    (FLEET_GET_NODE_SCHEMA, fleet_get_node),
    (FLEET_GET_HEALTH_SCHEMA, fleet_get_health),
    (FLEET_SEND_MESSAGE_SCHEMA, fleet_send_message),
    (FLEET_GET_TASK_SCHEMA, fleet_get_task),
    (FLEET_CANCEL_TASK_SCHEMA, fleet_cancel_task),
)
