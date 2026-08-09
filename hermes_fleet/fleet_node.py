"""One explicit Fleet dispatcher for Keryx-delivered node communications."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from .envelope import OPERATIONS, FleetEnvelope, parse_envelope
from .hermes_runs import HermesRunDeadlineExceeded, HermesRunError, HermesRunResult
from .models import FleetDefaults, NodeConfig, _require_exact_type
from .observation import normalize_readiness
from .policy import enforce_request_policy
from .run_binding import RunBinding, RunBindingStore, recovery_action

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


class FleetNodeWorker:
    """Bind one local Fleet target to a Keryx-compatible worker node."""

    def __init__(
        self,
        *,
        target: NodeConfig,
        defaults: FleetDefaults,
        hermes: _HermesRunner,
        bindings: RunBindingStore,
        controller_peer_ids: tuple[str, ...],
        advertised_operations: tuple[str, ...] | None = None,
        now_ms: Callable[[], int] | None = None,
        readiness_inspector: Callable[[], dict[str, Any]] | None = None,
        capacity_observer: Callable[[int], Awaitable[None]] | None = None,
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
        self._bindings = _require_exact_type(
            bindings, RunBindingStore, "bindings must be a RunBindingStore"
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
        operations = advertised_operations or tuple(sorted(OPERATIONS))
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
        if capacity_observer is not None and not callable(capacity_observer):
            raise ValueError("capacity_observer must be callable")
        self._capacity_observer = capacity_observer
        self._active_worker_count = 0

    @property
    def active_worker_count(self) -> int:
        """Return Keryx tasks currently occupying this worker's single slot."""
        return self._active_worker_count

    @property
    def observed_active_worker_count(self) -> int:
        """Fail closed when durable bindings may still consume the worker slot."""
        unresolved = self._bindings.unresolved_count()
        return max(self._active_worker_count, min(unresolved, 1))

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

    async def _reply_from_terminal_binding(
        self,
        incoming: object,
        *,
        task_id: str,
        run_id: str,
    ) -> bool:
        """Replay the durable winner when two deliveries race to terminal state."""
        binding = self._bindings.get(task_id)
        if binding is None or binding.run_id != run_id:
            return False
        if binding.state == "completed" and binding.result_text is not None:
            await _complete_text(
                incoming,
                name="hermes-result.txt",
                text=binding.result_text,
                metadata={"hermes_run_id": run_id},
            )
            return True
        if binding.state == "cancelled":
            await _fail(incoming, "Fleet Hermes execution was cancelled")
            return True
        if binding.state == "indeterminate":
            await _fail(incoming, "Fleet Hermes execution is indeterminate")
            return True
        return False

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

        try:
            payload = _incoming_text(incoming)
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

        metadata = getattr(incoming, "metadata", None)
        if not _metadata_matches(metadata, envelope, self._target):
            await _fail(incoming, "Fleet delivery metadata does not match envelope")
            logger.info(
                "fleet communication rejected task_id=%s peer_id=%s "
                "status=metadata-mismatch",
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

        try:
            binding, created = self._bindings.reserve_execution_if_available(
                task_id, max_unresolved=1
            )
        except ValueError:
            await _fail(incoming, "Fleet execution binding is invalid")
            return
        if binding is None:
            await _fail(incoming, "Fleet worker has no available execution slot")
            return

        action = recovery_action(binding, created=created)
        if action == "replay_completed":
            assert binding.run_id is not None
            assert binding.result_text is not None
            await _complete_text(
                incoming,
                name="hermes-result.txt",
                text=binding.result_text,
                metadata={"hermes_run_id": binding.run_id},
            )
            logger.info(
                "fleet execution replayed task_id=%s peer_id=%s "
                "hermes_run_id=%s status=completed",
                task_id,
                peer_id,
                _bounded_label(binding.run_id, "unknown"),
            )
            return

        if action == "fail_closed_indeterminate":
            if binding.state in {"creating", "running"}:
                self._bindings.mark_indeterminate(task_id)
            await _fail(incoming, "Fleet execution binding is indeterminate")
            return
        if action == "fail_cancelled":
            await _fail(incoming, "Fleet Hermes execution was cancelled")
            return

        self._active_worker_count += 1
        await self._notify_capacity()
        try:
            await self._execute_hermes_binding(
                incoming,
                envelope=envelope,
                metadata=metadata,
                task_id=task_id,
                peer_id=peer_id,
                binding=binding,
                action=action,
            )
        finally:
            self._active_worker_count -= 1
            await self._notify_capacity()

    async def _execute_hermes_binding(
        self,
        incoming: object,
        *,
        envelope: FleetEnvelope,
        metadata: object,
        task_id: str,
        peer_id: str,
        binding: RunBinding,
        action: str,
    ) -> None:
        run_id = binding.run_id
        if action == "start_new":
            try:
                run_id = await asyncio.to_thread(
                    self._hermes.start,
                    prompt=envelope.input["prompt"],
                    session_id=f"fleet:{self._target.name}:{task_id}",
                    timeout_seconds=_remaining_timeout(
                        envelope,
                        metadata,
                        now_ms=self._now_ms(),
                    ),
                )
                self._bindings.bind_run(task_id, run_id)
            except (HermesRunError, ValueError):
                self._bindings.mark_indeterminate(task_id)
                await _fail(incoming, "Fleet Hermes submission is indeterminate")
                return

        assert run_id is not None

        try:
            timeout_seconds = _remaining_timeout(
                envelope,
                metadata,
                now_ms=self._now_ms(),
            )
        except ValueError:
            try:
                await asyncio.to_thread(self._hermes.stop, run_id)
            except HermesRunError:
                try:
                    self._bindings.mark_indeterminate(task_id)
                except ValueError:
                    if await self._reply_from_terminal_binding(
                        incoming, task_id=task_id, run_id=run_id
                    ):
                        return
            else:
                try:
                    self._bindings.mark_cancelled(task_id, run_id)
                except ValueError:
                    if await self._reply_from_terminal_binding(
                        incoming, task_id=task_id, run_id=run_id
                    ):
                        return
            await _fail(incoming, "Fleet task deadline has expired")
            return

        try:
            result = await asyncio.to_thread(
                self._hermes.wait,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
            )
            if result.run_id != run_id:
                raise ValueError("Hermes returned a mismatched run ID")
            completed = self._bindings.complete(task_id, run_id, result.text)
        except HermesRunDeadlineExceeded:
            try:
                self._bindings.mark_cancelled(task_id, run_id)
            except ValueError:
                if await self._reply_from_terminal_binding(
                    incoming, task_id=task_id, run_id=run_id
                ):
                    return
            await _fail(incoming, "Fleet Hermes execution exceeded deadline")
            logger.info(
                "fleet execution cancelled task_id=%s peer_id=%s "
                "hermes_run_id=%s status=deadline-exceeded",
                task_id,
                peer_id,
                _bounded_label(run_id, "unknown"),
            )
            return
        except (HermesRunError, ValueError):
            try:
                self._bindings.mark_indeterminate(task_id)
            except ValueError:
                if await self._reply_from_terminal_binding(
                    incoming, task_id=task_id, run_id=run_id
                ):
                    return
            await _fail(incoming, "Fleet Hermes execution is indeterminate")
            logger.info(
                "fleet execution failed task_id=%s peer_id=%s status=indeterminate",
                task_id,
                peer_id,
            )
            return

        await _complete_text(
            incoming,
            name="hermes-result.txt",
            text=completed.result_text or "",
            metadata={"hermes_run_id": run_id},
        )
        logger.info(
            "fleet execution completed task_id=%s peer_id=%s "
            "hermes_run_id=%s status=completed",
            task_id,
            peer_id,
            _bounded_label(run_id, "unknown"),
        )


def _health_response(
    target: NodeConfig,
    envelope: FleetEnvelope,
    sender_peer_id: str,
    hermes_health: dict[str, object] | None,
    advertised_operations: tuple[str, ...],
    readiness: dict[str, Any] | None,
) -> dict[str, Any]:
    del target, envelope, sender_peer_id, advertised_operations
    health = hermes_health or {
        "api": "unavailable",
        "run_submission": False,
        "run_status": False,
        "run_stop": False,
    }
    healthy = health.get("api") == "healthy" and all(
        health.get(field) is True
        for field in ("run_submission", "run_status", "run_stop")
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
    return response


def _message_response(
    target: NodeConfig,
    envelope: FleetEnvelope,
    sender_peer_id: str,
    hermes_health: dict[str, object] | None,
    advertised_operations: tuple[str, ...],
    readiness: dict[str, Any] | None,
) -> dict[str, Any]:
    del hermes_health, advertised_operations, readiness
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


def _incoming_text(incoming: object) -> str:
    messages = getattr(incoming, "messages", None)
    if type(messages) is not list or len(messages) != 1:
        raise ValueError("Fleet task must contain one text message")
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
