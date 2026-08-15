"""One explicit Fleet dispatcher for Keryx-delivered node communications."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from .backend_capabilities import BackendCapabilities
from .envelope import OPERATIONS, FleetEnvelope, parse_envelope
from .execution_package import MEDIA_TYPE as EXECUTION_PACKAGE_MEDIA_TYPE
from .execution_package import (
    ExactExecutionPackage,
    ExecutionPackageError,
    parse_execution_package,
)
from .hermes_runs import HermesRunError, HermesRunResult
from .models import FleetDefaults, NodeConfig, _require_exact_type
from .observation import normalize_readiness
from .policy import enforce_request_policy

logger = logging.getLogger(__name__)

_FLEET_VERSION = "0.1.0"
_EXECUTABLE_OPERATION = "fleet.hermes.run"


class _HermesRunner(Protocol):
    def health(self, *, timeout_seconds: float | None = None) -> dict[str, object]: ...

    def start(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str: ...

    def wait(self, *, run_id: str, timeout_seconds: float) -> HermesRunResult: ...

    def stop(self, run_id: str, *, timeout_seconds: float | None = None) -> None: ...


class _RecipeExecutor(Protocol):
    async def execute(
        self,
        *,
        package: ExactExecutionPackage,
        authenticated_sender: str,
        incoming: object,
    ) -> None: ...


class FleetNodeWorker:
    """Bind one local Fleet target to a Keryx-compatible worker node."""

    def __init__(
        self,
        *,
        target: NodeConfig,
        defaults: FleetDefaults,
        hermes: _HermesRunner,
        controller_peer_ids: tuple[str, ...],
        advertised_operations: tuple[str, ...] | None = None,
        now_ms: Callable[[], int] | None = None,
        readiness_inspector: Callable[[], dict[str, Any]] | None = None,
        admission_generation_inspector: Callable[[], int] | None = None,
        managed_network_id: str | None = None,
        managed_device_id: str | None = None,
        capacity_observer: Callable[[int], Awaitable[None]] | None = None,
        recipe_executor: _RecipeExecutor | None = None,
        backend_capabilities: BackendCapabilities | None = None,
    ) -> None:
        self._target = _require_exact_type(
            target, NodeConfig, "target must be a NodeConfig"
        )
        self._defaults = _require_exact_type(
            defaults, FleetDefaults, "defaults must be FleetDefaults"
        )
        if any(
            not callable(getattr(hermes, method, None))
            for method in ("health", "start", "wait", "stop")
        ):
            raise ValueError(
                "hermes must provide health(), start(), wait(), and stop()"
            )

        if type(controller_peer_ids) is not tuple or not controller_peer_ids:
            raise ValueError("controller_peer_ids must be a nonempty tuple")
        if any(
            type(peer_id) is not str
            or not peer_id
            or len(peer_id) > 256
            or peer_id != peer_id.strip()
            for peer_id in controller_peer_ids
        ):
            raise ValueError("controller_peer_ids contains an invalid peer ID")
        self._hermes = hermes
        self._controller_peer_ids = frozenset(controller_peer_ids)
        operations = (
            tuple(sorted(OPERATIONS))
            if advertised_operations is None
            else advertised_operations
        )
        if (
            type(operations) is not tuple
            or not operations
            or any(operation not in OPERATIONS for operation in operations)
        ):
            raise ValueError("advertised_operations must be known Fleet operations")
        self._advertised_operations = operations
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))
        if readiness_inspector is not None and not callable(readiness_inspector):
            raise ValueError("readiness_inspector must be callable")
        self._readiness_inspector = readiness_inspector
        managed_identity = (managed_network_id, managed_device_id)
        if any(value is not None for value in managed_identity) != all(
            value is not None for value in managed_identity
        ):
            raise ValueError("managed execution identity must be complete")
        if admission_generation_inspector is not None and not callable(
            admission_generation_inspector
        ):
            raise ValueError("admission_generation_inspector must be callable")
        if admission_generation_inspector is not None and managed_network_id is None:
            raise ValueError("managed execution identity is required for admission")
        self._admission_generation_inspector = admission_generation_inspector
        self._managed_network_id = managed_network_id
        self._managed_device_id = managed_device_id
        if capacity_observer is not None and not callable(capacity_observer):
            raise ValueError("capacity_observer must be callable")
        self._capacity_observer = capacity_observer
        if recipe_executor is not None and not callable(
            getattr(recipe_executor, "execute", None)
        ):
            raise ValueError("recipe_executor must provide execute()")
        self._recipe_executor = recipe_executor
        if (
            backend_capabilities is not None
            and type(backend_capabilities) is not BackendCapabilities
        ):
            raise ValueError("backend_capabilities must be BackendCapabilities")
        self._backend_capabilities = backend_capabilities
        self._active_worker_count = 0

    @property
    def active_worker_count(self) -> int:
        """Return Keryx tasks currently occupying this worker's single slot."""
        return self._active_worker_count

    @property
    def observed_active_worker_count(self) -> int:
        """Return destination executions currently occupying the worker slot."""
        return self._active_worker_count

    def bind(self, node: object) -> None:
        """Register this dispatcher once with public Keryx ``on_task``."""
        on_task = getattr(node, "on_task", None)
        if not callable(on_task):
            raise ValueError("node must provide on_task()")
        on_task(self.handle_task)

    async def _notify_capacity(self) -> None:
        if self._capacity_observer is None:
            return
        try:
            await self._capacity_observer(self._active_worker_count)
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("fleet worker capacity observation failed: %s", error)

    async def handle_task(self, incoming: object) -> None:
        """Validate and route one communication to a direct or executable handler."""
        task_id = _task_id(getattr(incoming, "task_id", ""))
        if task_id is None:
            await _fail(incoming, "Fleet delivery has invalid Keryx task identity")
            return
        peer_id = _bounded_label(getattr(incoming, "peer_id", ""), "unknown")

        if peer_id not in self._controller_peer_ids:
            await _fail(incoming, "Fleet sender is not authorized")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=sender-denied",
                task_id,
                peer_id,
            )
            return

        metadata = getattr(incoming, "metadata", None)
        try:
            package_payload = _incoming_execution_package(incoming)
        except ValueError:
            await _fail(incoming, "Fleet task payload is invalid")
            return
        if package_payload is not None:
            try:
                execution_package = self._admit_execution_package(
                    package_payload,
                    metadata=metadata,
                    task_id=task_id,
                )
            except (ExecutionPackageError, OSError, RuntimeError, ValueError):
                await _fail(incoming, "Fleet execution package is not admitted")
                return
            if self._recipe_executor is None:
                await _fail(incoming, "Fleet Recipe execution is unavailable")
                return
            self._active_worker_count += 1
            await self._notify_capacity()
            try:
                await self._recipe_executor.execute(
                    package=execution_package,
                    authenticated_sender=peer_id,
                    incoming=incoming,
                )
            except (OSError, RuntimeError, ValueError):
                await _fail(incoming, "Fleet Recipe execution failed")
            finally:
                self._active_worker_count -= 1
                await self._notify_capacity()
            return

        try:
            payload = _incoming_text_payload(incoming)
            envelope = parse_envelope(
                payload, target=self._target, defaults=self._defaults
            )
        except ValueError:
            await _fail(incoming, "Fleet task envelope is invalid")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=invalid-envelope",
                task_id,
                peer_id,
            )
            return

        if not _metadata_matches(metadata, envelope, self._target):
            await _fail(incoming, "Fleet delivery metadata does not match envelope")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=metadata-mismatch",
                task_id,
                peer_id,
            )
            return

        if envelope.operation not in self._advertised_operations:
            await _fail(incoming, "Fleet operation is not currently available")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=operation-unavailable",
                task_id,
                peer_id,
            )
            return

        export_paths = envelope.input.get("export_paths", ())
        if export_paths:
            await _fail(incoming, "Fleet artifact exports are not available")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=exports-deferred",
                task_id,
                peer_id,
            )
            return

        prompt_chars = len(envelope.input.get("prompt", envelope.input.get("text", "")))
        try:
            enforce_request_policy(
                self._target.policy,
                defaults=self._defaults,
                operation=envelope.operation,
                deadline_seconds=envelope.deadline_seconds,
                payload_bytes=len(payload.encode("utf-8")),
                prompt_chars=prompt_chars,
                export_path_count=0,
            )
        except ValueError:
            await _fail(incoming, "Fleet task is not authorized")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=policy-denied",
                task_id,
                peer_id,
            )
            return

        try:
            timeout_seconds = _remaining_timeout(
                envelope, metadata, now_ms=self._now_ms()
            )
        except ValueError:
            await _fail(incoming, "Fleet task deadline has expired")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=deadline-expired",
                task_id,
                peer_id,
            )
            return

        direct_handler = _DIRECT_HANDLERS.get(envelope.operation)
        if direct_handler is not None:
            hermes_health = None
            if envelope.operation == "fleet.health":
                try:
                    hermes_health = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._hermes.health,
                            timeout_seconds=timeout_seconds,
                        ),
                        timeout=timeout_seconds,
                    )
                except (TimeoutError, HermesRunError, ValueError):
                    await _fail(incoming, "Fleet task deadline has expired")
                    return
            readiness = None
            if (
                envelope.operation in {"fleet.health", "fleet.inventory"}
                and self._readiness_inspector is not None
            ):
                try:
                    readiness_timeout = _remaining_timeout(
                        envelope,
                        metadata,
                        now_ms=self._now_ms(),
                    )
                    readiness = await asyncio.wait_for(
                        asyncio.to_thread(self._readiness_inspector),
                        timeout=readiness_timeout,
                    )
                    readiness = normalize_readiness(readiness)
                except (TimeoutError, OSError, RuntimeError, ValueError):
                    readiness = None
            response = direct_handler(
                self._target,
                envelope,
                peer_id,
                hermes_health,
                self._advertised_operations,
                readiness,
                self._backend_capabilities,
            )
            await _complete_text(
                incoming,
                name="fleet-response.json",
                text=json.dumps(response, separators=(",", ":"), sort_keys=True),
            )
            logger.info(
                "fleet communication completed task_id=%s peer_id=%s operation=%s "
                "status=completed",
                task_id,
                peer_id,
                envelope.operation,
            )
            return

        if envelope.operation != _EXECUTABLE_OPERATION:
            await _fail(incoming, "Fleet operation is not supported")
            return

        await _fail(
            incoming, "Fleet Hermes execution requires an immutable Recipe package"
        )
        return

    def _admit_execution_package(
        self,
        payload: bytes,
        *,
        metadata: object,
        task_id: str,
    ) -> ExactExecutionPackage:
        if (
            self._admission_generation_inspector is None
            or self._managed_network_id is None
            or self._managed_device_id is None
        ):
            raise ExecutionPackageError("destination admission is unavailable")
        package = parse_execution_package(payload)
        if package.execution_id != task_id:
            raise ExecutionPackageError("execution identity conflicts with delivery")
        if type(metadata) is not dict:
            raise ExecutionPackageError("execution metadata is invalid")
        expected_metadata = {
            "fleet.operation": "fleet.hermes.run",
            "fleet.execution_package_hash": package.content_hash,
            "fleet_deadline_ms": str(package.authorization["deadline_ms"]),
            "skill": "fleet.hermes.run",
        }
        if not _execution_metadata_matches(
            metadata, expected_metadata, target_peer_id=self._target.peer_id
        ):
            raise ExecutionPackageError("execution metadata conflicts with package")
        if package.target != {
            "source": "nodescale",
            "network_id": self._managed_network_id,
            "device_id": self._managed_device_id,
            "binding_generation": package.target["binding_generation"],
            "admission_generation": package.target["admission_generation"],
        }:
            raise ExecutionPackageError("execution package targets another destination")
        generation = self._admission_generation_inspector()
        if (
            type(generation) is not int
            or generation != package.target["admission_generation"]
        ):
            raise ExecutionPackageError("destination admission generation is stale")
        return package


def _health_response(
    target: NodeConfig,
    envelope: FleetEnvelope,
    sender_peer_id: str,
    hermes_health: dict[str, object] | None,
    advertised_operations: tuple[str, ...],
    readiness: dict[str, Any] | None,
    backend_capabilities: BackendCapabilities | None,
) -> dict[str, Any]:
    del target, envelope, sender_peer_id, advertised_operations, backend_capabilities
    health = hermes_health or {
        "api": "unavailable",
        "run_submission": False,
        "run_status": False,
        "run_stop": False,
        "run_finalize": False,
        "run_approval_budget": False,
        "run_tool_evidence": False,
    }
    healthy = health.get("api") == "healthy" and all(
        health.get(field) is True
        for field in (
            "run_submission",
            "run_status",
            "run_stop",
            "run_finalize",
            "run_approval_budget",
            "run_tool_evidence",
        )
    )
    response = {
        "operation": "fleet.health",
        "status": "ok" if healthy else "degraded",
        "adapter": "ok",
        "keryx_delivery": "received",
        "hermes": health,
    }
    if readiness is not None:
        response["readiness"] = readiness
    return response


def _inventory_response(
    target: NodeConfig,
    envelope: FleetEnvelope,
    sender_peer_id: str,
    hermes_health: dict[str, object] | None,
    advertised_operations: tuple[str, ...],
    readiness: dict[str, Any] | None,
    backend_capabilities: BackendCapabilities | None,
) -> dict[str, Any]:
    del envelope, sender_peer_id, hermes_health
    response = {
        "operation": "fleet.inventory",
        "status": "ok",
        "node": {
            "name": target.name,
            "peer_id": target.peer_id,
            "version": _FLEET_VERSION,
        },
        "capabilities": list(advertised_operations),
    }
    if readiness is not None:
        response["readiness"] = readiness
    if backend_capabilities is not None:
        response["execution_backend"] = {
            "content_hash": backend_capabilities.content_hash,
            "document": backend_capabilities.to_dict(),
        }
    return response


def _message_response(
    target: NodeConfig,
    envelope: FleetEnvelope,
    sender_peer_id: str,
    hermes_health: dict[str, object] | None,
    advertised_operations: tuple[str, ...],
    readiness: dict[str, Any] | None,
    backend_capabilities: BackendCapabilities | None,
) -> dict[str, Any]:
    del hermes_health, advertised_operations, readiness, backend_capabilities
    response = {
        "operation": "fleet.message",
        "status": "received",
        "received_by": target.peer_id,
        "sender_peer_id": sender_peer_id,
    }
    for key in ("topic", "correlation_id"):
        value = envelope.input[key]
        if value:
            response[key] = value
    return response


_DirectHandler = Callable[
    [
        NodeConfig,
        FleetEnvelope,
        str,
        dict[str, object] | None,
        tuple[str, ...],
        dict[str, Any] | None,
        BackendCapabilities | None,
    ],
    dict[str, Any],
]
_DIRECT_HANDLERS: dict[str, _DirectHandler] = {
    "fleet.health": _health_response,
    "fleet.inventory": _inventory_response,
    "fleet.message": _message_response,
}


def _metadata_matches(
    metadata: object, envelope: FleetEnvelope, target: NodeConfig
) -> bool:
    if type(metadata) is not dict:
        return False
    expected = {
        "fleet.envelope_version": str(envelope.version),
        "fleet.operation": envelope.operation,
        "fleet.target_peer_id": target.peer_id,
    }
    return all(metadata.get(key) == value for key, value in expected.items()) and (
        _deadline_ms(metadata) is not None
    )


def _execution_metadata_matches(
    metadata: object,
    expected: dict[str, str],
    *,
    target_peer_id: str,
) -> bool:
    if type(metadata) is not dict or not all(
        metadata.get(key) == value for key, value in expected.items()
    ):
        return False
    transport_keys = {
        "target_node_id",
        "keryx.authenticated_source_protocol_features",
    }
    if not set(metadata).issubset(set(expected) | transport_keys):
        return False
    target_node_id = metadata.get("target_node_id")
    if target_node_id is not None and target_node_id != target_peer_id:
        return False
    features = metadata.get("keryx.authenticated_source_protocol_features")
    return features is None or (
        type(features) is str
        and 0 < len(features) <= 2_048
        and all(character.isprintable() for character in features)
    )


def _incoming_execution_package(incoming: object) -> bytes | None:
    messages = getattr(incoming, "messages", None)
    if type(messages) is not list or len(messages) != 1:
        raise ValueError("Fleet task must contain one message")
    parts = getattr(messages[0], "parts", None)
    if type(parts) is not list or len(parts) != 1:
        raise ValueError("Fleet task must contain one part")
    part = parts[0]
    media_type = getattr(part, "media_type", "")
    if media_type == "text/plain":
        return None
    text = getattr(part, "text", "")
    raw = getattr(part, "raw", None)
    if (
        text not in ("", None)
        or type(raw) is not bytes
        or not raw
        or media_type != EXECUTION_PACKAGE_MEDIA_TYPE
    ):
        raise ValueError("Fleet execution package part is invalid")
    return raw


def _incoming_text_payload(incoming: object) -> str:
    messages = getattr(incoming, "messages", None)
    if type(messages) is not list or len(messages) != 1:
        raise ValueError("Fleet task must contain one message")
    parts = getattr(messages[0], "parts", None)
    if type(parts) is not list or len(parts) != 1:
        raise ValueError("Fleet task must contain one text part")
    part = parts[0]
    text = getattr(part, "text", None)
    raw = getattr(part, "raw", b"")
    media_type = getattr(part, "media_type", "text/plain")
    if type(text) is not str or raw not in (b"", None) or media_type != "text/plain":
        raise ValueError("Fleet task must contain one text part")
    return text


def _remaining_timeout(
    envelope: FleetEnvelope, metadata: object, *, now_ms: int
) -> float:
    timeout = float(envelope.deadline_seconds)
    absolute_deadline = _deadline_ms(metadata)
    if absolute_deadline is None:
        raise ValueError("invalid Fleet deadline")
    remaining = (absolute_deadline - now_ms) / 1_000
    if remaining <= 0:
        raise ValueError("expired Fleet deadline")
    return min(timeout, remaining)


def _deadline_ms(metadata: object) -> int | None:
    if type(metadata) is not dict:
        return None
    raw_deadline = metadata.get("fleet_deadline_ms")
    if (
        type(raw_deadline) is not str
        or not raw_deadline.isascii()
        or not raw_deadline.isdigit()
        or len(raw_deadline) > 19
    ):
        return None
    deadline = int(raw_deadline)
    return deadline if deadline <= 2**63 - 1 else None


async def _complete_text(
    incoming: object,
    *,
    name: str,
    text: str,
    metadata: dict[str, str] | None = None,
) -> None:
    complete = getattr(incoming, "complete", None)
    if not callable(complete):
        raise ValueError("incoming task must provide complete()")
    part: dict[str, Any] = {"text": text, "media_type": "text/plain"}
    if metadata:
        part["metadata"] = metadata
    await cast(Callable[[list[Any]], Awaitable[None]], complete)(
        [{"name": name, "parts": [part]}]
    )


async def _fail(incoming: object, message: str) -> None:
    fail = getattr(incoming, "fail", None)
    if not callable(fail):
        raise ValueError("incoming task must provide fail()")
    await cast(Callable[[str], Awaitable[None]], fail)(message)


def _bounded_label(value: object, fallback: str) -> str:
    if type(value) is not str or not value or len(value) > 256:
        return fallback
    return value


def _task_id(value: object) -> str | None:
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    return value
