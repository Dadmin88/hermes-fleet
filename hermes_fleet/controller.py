"""Exact-node Fleet communication submission over public Keryx primitives."""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .config import FleetConfig
from .envelope import ENVELOPE_VERSION, OPERATIONS, FleetEnvelope, parse_envelope
from .execution_package import EXECUTION_PACKAGE_MEDIA_TYPE
from .models import NodeConfig, RemoteOutput
from .observation import normalize_readiness
from .policy import enforce_request_policy
from .selection import select_nodes

_MAX_RESULT_CHARS = 65_536


class _TaskHandle(Protocol):
    task_id: str
    receipt: object

    async def wait(self, timeout: float | None = None) -> object: ...


class _KeryxNode(Protocol):
    async def send_task(
        self,
        message: dict[str, Any],
        *,
        peer_id: str,
        metadata: dict[str, str],
        deadline_ms: int,
    ) -> _TaskHandle: ...


@dataclass(frozen=True, slots=True)
class FleetSubmission:
    """One Keryx submission plus the daemon's immutable actual-route receipt."""

    target: NodeConfig
    operation: str
    task_id: str
    routed_to: str
    delivery_route: str
    deadline_ms: int
    handle: _TaskHandle


@dataclass(frozen=True, slots=True)
class FleetOperationResult:
    """Terminal Fleet response paired with Keryx's actual route receipt."""

    operation: str
    target: str
    task_id: str
    routed_to: str
    delivery_route: str
    response: dict[str, Any] | str
    untrusted: bool


class FleetController:
    """Shared exact-node controller surface for CLI and Hermes model tools."""

    def __init__(self, *, keryx: _KeryxNode, config: FleetConfig) -> None:
        if type(config) is not FleetConfig:
            raise ValueError("config must be a FleetConfig")
        if not callable(getattr(keryx, "send_task", None)):
            raise ValueError("keryx must provide send_task()")
        self._keryx = keryx
        self._config = config

    async def get_health(
        self, target: str, *, deadline_seconds: int = 30
    ) -> FleetOperationResult:
        return await self._communicate(target, "fleet.health", {}, deadline_seconds)

    async def get_inventory(
        self, target: str, *, deadline_seconds: int = 30
    ) -> FleetOperationResult:
        return await self._communicate(target, "fleet.inventory", {}, deadline_seconds)

    async def send_message(
        self,
        target: str,
        text: str,
        *,
        topic: str = "",
        correlation_id: str = "",
        deadline_seconds: int = 30,
    ) -> FleetOperationResult:
        input_data = {"text": text}
        if topic:
            input_data["topic"] = topic
        if correlation_id:
            input_data["correlation_id"] = correlation_id
        return await self._communicate(
            target, "fleet.message", input_data, deadline_seconds
        )

    async def _communicate(
        self,
        target: str,
        operation: str,
        input_data: dict[str, Any],
        deadline_seconds: int,
    ) -> FleetOperationResult:
        submission = await submit_communication(
            keryx=self._keryx,
            config=self._config,
            target_name=target,
            operation=operation,
            input_data=input_data,
            deadline_seconds=deadline_seconds,
        )
        output = await wait_text_result(
            submission, timeout_seconds=float(deadline_seconds) + 5.0
        )
        response: dict[str, Any] | str = _direct_response(output.text, operation)
        return FleetOperationResult(
            operation=operation,
            target=submission.target.name,
            task_id=submission.task_id,
            routed_to=submission.routed_to,
            delivery_route=submission.delivery_route,
            response=response,
            untrusted=True,
        )


async def submit_communication(
    *,
    keryx: _KeryxNode,
    config: FleetConfig,
    target_name: str,
    operation: str,
    input_data: dict[str, Any],
    deadline_seconds: int,
    now_ms: Callable[[], int] | None = None,
) -> FleetSubmission:
    """Validate and submit one direct or executable communication to an exact node."""
    if type(config) is not FleetConfig:
        raise ValueError("config must be a FleetConfig")
    if type(operation) is not str or operation not in OPERATIONS:
        raise ValueError("unsupported Fleet operation")
    if type(input_data) is not dict:
        raise ValueError("input_data must be a JSON object")
    if not callable(getattr(keryx, "send_task", None)):
        raise ValueError("keryx must provide send_task()")

    selected = select_nodes(config.nodes, names=(target_name,))
    if len(selected) != 1:
        raise ValueError("exactly one enabled Fleet node is required")
    target = selected[0]

    candidate = FleetEnvelope(
        version=ENVELOPE_VERSION,
        operation=operation,
        target_name=target.name,
        target_peer_id=target.peer_id,
        input=input_data,
        deadline_seconds=deadline_seconds,
    )
    payload = candidate.to_json()
    envelope = parse_envelope(payload, target=target, defaults=config.defaults)
    prompt_chars = len(envelope.input.get("prompt", envelope.input.get("text", "")))
    export_paths = envelope.input.get("export_paths", ())
    enforce_request_policy(
        target.policy,
        defaults=config.defaults,
        operation=envelope.operation,
        deadline_seconds=envelope.deadline_seconds,
        payload_bytes=len(payload.encode("utf-8")),
        prompt_chars=prompt_chars,
        export_path_count=len(export_paths),
    )

    clock = now_ms or (lambda: int(time.time() * 1_000))
    current_ms = clock()
    if type(current_ms) is not int or current_ms < 0:
        raise ValueError("now_ms must return a nonnegative integer")
    deadline_ms = current_ms + envelope.deadline_seconds * 1_000
    metadata = {
        "fleet.envelope_version": str(envelope.version),
        "fleet.operation": envelope.operation,
        "fleet.target_peer_id": target.peer_id,
        "fleet_deadline_ms": str(deadline_ms),
        "skill": envelope.operation,
    }
    handle = await keryx.send_task(
        {"role": "user", "parts": [{"text": payload, "media_type": "text/plain"}]},
        peer_id=target.peer_id,
        metadata=metadata,
        deadline_ms=deadline_ms,
    )
    receipt = getattr(handle, "receipt", None)
    task_id = getattr(handle, "task_id", None)
    receipt_task_id = getattr(receipt, "task_id", None)
    routed_to = getattr(receipt, "routed_to", None)
    delivery_route = getattr(receipt, "delivery_route", None)
    if (
        type(task_id) is not str
        or not task_id
        or task_id != receipt_task_id
        or type(routed_to) is not str
        or not routed_to
        or type(delivery_route) is not str
        or not delivery_route
    ):
        raise RuntimeError("Keryx returned an invalid submission receipt")
    return FleetSubmission(
        target=target,
        operation=envelope.operation,
        task_id=task_id,
        routed_to=routed_to,
        delivery_route=delivery_route,
        deadline_ms=deadline_ms,
        handle=handle,
    )


async def submit_execution_package(
    *,
    keryx: Any,
    peer_id: str,
    task_id: str,
    idempotency_key: str,
    package_payload: bytes,
    package_hash: str,
    deadline_ms: int,
) -> FleetSubmission:
    """Submit immutable FX8 bytes once and reconcile uncertainty by task identity."""
    for value, label in (
        (peer_id, "peer ID"),
        (task_id, "task ID"),
        (idempotency_key, "idempotency key"),
    ):
        if type(value) is not str or not value or len(value) > 256:
            raise ValueError(f"execution {label} is invalid")
    if type(package_payload) is not bytes or not package_payload:
        raise ValueError("execution package payload is invalid")
    if (
        type(package_hash) is not str
        or not package_hash.startswith("sha256:")
        or len(package_hash) != 71
        or any(character not in "0123456789abcdef" for character in package_hash[7:])
    ):
        raise ValueError("execution package hash is invalid")
    if type(deadline_ms) is not int or deadline_ms <= 0:
        raise ValueError("execution deadline is invalid")
    message = {
        "role": "user",
        "parts": [
            {
                "text": "",
                "raw": package_payload,
                "media_type": EXECUTION_PACKAGE_MEDIA_TYPE,
            }
        ],
    }
    metadata = {
        "fleet.operation": "fleet.hermes.run",
        "fleet.execution_package_hash": package_hash,
        "fleet_deadline_ms": str(deadline_ms),
        "skill": "fleet.hermes.run",
    }
    try:
        handle = await keryx.send_task(
            message,
            peer_id=peer_id,
            task_id=task_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
            deadline_ms=deadline_ms,
        )
    except Exception:
        reopen = getattr(keryx, "task_handle", None)
        if not callable(reopen):
            raise
        handle = reopen(task_id)
    if getattr(handle, "task_id", None) != task_id:
        raise RuntimeError("Keryx returned a mismatched execution task identity")
    handle = cast(_TaskHandle, handle)
    receipt = getattr(handle, "receipt", None)
    if receipt is None:
        return FleetSubmission(
            target=NodeConfig(name=task_id, peer_id=peer_id),
            operation="fleet.hermes.run",
            task_id=task_id,
            routed_to=peer_id,
            delivery_route="reconciled",
            deadline_ms=deadline_ms,
            handle=handle,
        )
    if getattr(receipt, "task_id", None) != task_id:
        raise RuntimeError("Keryx returned a mismatched execution receipt identity")
    routed_to = getattr(receipt, "routed_to", None)
    delivery_route = getattr(receipt, "delivery_route", None)
    if (
        type(routed_to) is not str
        or not routed_to
        or type(delivery_route) is not str
        or not delivery_route
    ):
        raise RuntimeError("Keryx returned an invalid execution submission receipt")
    return FleetSubmission(
        target=NodeConfig(name=task_id, peer_id=peer_id),
        operation="fleet.hermes.run",
        task_id=task_id,
        routed_to=routed_to,
        delivery_route=delivery_route,
        deadline_ms=deadline_ms,
        handle=handle,
    )


async def wait_text_result(
    submission: FleetSubmission, *, timeout_seconds: float
) -> RemoteOutput:
    """Wait through Keryx and return bounded terminal text as untrusted output."""
    if type(submission) is not FleetSubmission:
        raise ValueError("submission must be a FleetSubmission")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int | float)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive")
    wait = getattr(submission.handle, "wait", None)
    if not callable(wait):
        raise RuntimeError("Keryx task handle cannot wait for a result")
    task = await cast(Callable[[float | None], Awaitable[object]], wait)(
        float(timeout_seconds)
    )
    status = getattr(getattr(task, "status", None), "value", None)
    if status != "completed":
        raise RuntimeError("Keryx communication did not complete successfully")

    task_metadata = getattr(task, "metadata", None)
    text = task_metadata.get("result_text") if type(task_metadata) is dict else None
    if type(text) is not str:
        text = _artifact_text(getattr(task, "artifacts", None))
    if type(text) is not str or len(text) > _MAX_RESULT_CHARS:
        raise RuntimeError("Keryx result does not contain bounded terminal text")
    return RemoteOutput(text=text)


def _artifact_text(artifacts: object) -> str | None:
    if type(artifacts) is not list or len(artifacts) != 1:
        return None
    parts = getattr(artifacts[0], "parts", None)
    if type(parts) is not list or len(parts) != 1:
        return None
    text = getattr(parts[0], "text", None)
    media_type = getattr(parts[0], "media_type", "text/plain")
    if type(text) is not str or media_type != "text/plain":
        return None
    return text


_DIRECT_RESPONSE_KEYS = {
    "fleet.health": frozenset(
        {"operation", "status", "adapter", "keryx_delivery", "hermes", "readiness"}
    ),
    "fleet.inventory": frozenset(
        {
            "operation",
            "status",
            "node",
            "capabilities",
            "readiness",
            "execution_backend",
        }
    ),
    "fleet.message": frozenset(
        {
            "operation",
            "status",
            "received_by",
            "sender_peer_id",
            "topic",
            "correlation_id",
        }
    ),
}


def _direct_response(text: str, operation: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite numeric constant")

    try:
        response = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError, RecursionError):
        raise RuntimeError("Fleet node returned an invalid direct response") from None
    allowed_keys = _DIRECT_RESPONSE_KEYS.get(operation)
    if (
        type(response) is not dict
        or allowed_keys is None
        or not set(response).issubset(allowed_keys)
        or response.get("operation") != operation
        or type(response.get("status")) is not str
    ):
        raise RuntimeError("Fleet node returned an invalid direct response")
    if "readiness" in response:
        try:
            response["readiness"] = normalize_readiness(response["readiness"])
        except ValueError:
            raise RuntimeError(
                "Fleet node returned an invalid direct response"
            ) from None
    return response
