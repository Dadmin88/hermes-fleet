"""Presentation-neutral operator application boundary for Hermes Fleet."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from keryx.task import TaskResultUnavailableError

from .agency_snapshot import AgencySource
from .backend_capabilities import BackendCapabilities
from .config import FleetConfig, ManagedTargetPolicy
from .controller import FleetController
from .models import NodeConfig, NodePolicy
from .recipe_execution import ExactRecipeSubmissionService
from .recipes import FleetRecipe

_SECRET = re.compile(r"(?i)(bearer|token|key|secret|password|credential)\s*[=:]\s*\S+")
_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[^\s/]+/)+[^\s]*")
_MAX_RESULT_CHARS = 65_536
_EXECUTION_OUTCOME_ARTIFACT = "fleet-execution-outcome.v1.json"
_EXECUTION_OUTCOME_SCHEMA = "fleet.execution-outcome.v1"
_EXECUTION_OUTCOME_STATUS = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_INVENTORY_PROBE_ATTEMPTS = 3
_INVENTORY_PROBE_DEADLINE_SECONDS = 10
_INVENTORY_RETRY_DELAYS = (0.0, 0.25, 0.75)


class OperatorErrorCode(StrEnum):
    UNKNOWN_TARGET = "UNKNOWN_TARGET"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    NOT_MANAGED = "NOT_MANAGED"
    NO_BINDING = "NO_BINDING"
    STALE_STATE = "STALE_STATE"
    NOT_READY = "NOT_READY"
    NO_CAPACITY = "NO_CAPACITY"
    POLICY_DENIED = "POLICY_DENIED"
    OPERATION_UNAVAILABLE = "OPERATION_UNAVAILABLE"
    TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"
    REMOTE_REJECTED = "REMOTE_REJECTED"
    HERMES_UNAVAILABLE = "HERMES_UNAVAILABLE"
    HERMES_AUTH_FAILURE = "HERMES_AUTH_FAILURE"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    TASK_FAILED = "TASK_FAILED"
    TASK_INDETERMINATE = "TASK_INDETERMINATE"


def _safe_message(value: object) -> str:
    text = str(value).replace("\n", " ").strip()
    text = _SECRET.sub(r"\1=<redacted>", text)
    text = _PATH.sub("<private-path>", text)
    return text[:300]


class OperatorError(RuntimeError):
    """Stable operator failure with separate sanitized and debugging details."""

    def __init__(
        self, code: OperatorErrorCode, message: str, *, detail: object | None = None
    ) -> None:
        self.code = code
        self.public_message = _safe_message(message)
        self.debug_detail = str(detail if detail is not None else message)
        super().__init__(self.public_message)


def _execution_outcome(task: object, *, task_id: str) -> tuple[str, str] | None:
    artifacts = getattr(task, "artifacts", None)
    if not isinstance(artifacts, list):
        return None
    matches = [
        artifact
        for artifact in artifacts
        if getattr(artifact, "name", None) == _EXECUTION_OUTCOME_ARTIFACT
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise OperatorError(
            OperatorErrorCode.TASK_INDETERMINATE,
            "Fleet execution returned ambiguous outcome evidence.",
        )
    parts = getattr(matches[0], "parts", None)
    if not isinstance(parts, list) or len(parts) != 1:
        raise OperatorError(
            OperatorErrorCode.TASK_INDETERMINATE,
            "Fleet execution returned invalid outcome evidence.",
        )
    text = getattr(parts[0], "text", None)
    if type(text) is not str or not 0 < len(text) <= 4096:
        raise OperatorError(
            OperatorErrorCode.TASK_INDETERMINATE,
            "Fleet execution returned invalid outcome evidence.",
        )
    try:
        document = json.loads(text)
    except (ValueError, TypeError, RecursionError) as error:
        raise OperatorError(
            OperatorErrorCode.TASK_INDETERMINATE,
            "Fleet execution returned invalid outcome evidence.",
            detail=error,
        ) from error
    if type(document) is not dict or set(document) != {
        "schema",
        "execution_id",
        "status",
        "reason",
    }:
        raise OperatorError(
            OperatorErrorCode.TASK_INDETERMINATE,
            "Fleet execution returned invalid outcome evidence.",
        )
    status = document.get("status")
    reason = document.get("reason")
    if (
        document.get("schema") != _EXECUTION_OUTCOME_SCHEMA
        or document.get("execution_id") != task_id
        or type(status) is not str
        or _EXECUTION_OUTCOME_STATUS.fullmatch(status) is None
        or type(reason) is not str
        or not 0 < len(reason) <= 1024
    ):
        raise OperatorError(
            OperatorErrorCode.TASK_INDETERMINATE,
            "Fleet execution returned invalid outcome evidence.",
        )
    return status, _safe_message(reason)


async def _inventory_probe_with_retry(
    controller: FleetController,
    target: str,
    *,
    deadline_seconds: int,
    sleep=asyncio.sleep,
):
    """Retry only the read-only inventory probe.

    Execution submission stays single-shot.
    """
    last_error: Exception | None = None
    for attempt in range(_INVENTORY_PROBE_ATTEMPTS):
        delay = _INVENTORY_RETRY_DELAYS[attempt]
        if delay:
            await sleep(delay)
        try:
            return await controller.get_inventory(
                target,
                deadline_seconds=min(
                    deadline_seconds,
                    _INVENTORY_PROBE_DEADLINE_SECONDS,
                ),
            )
        except Exception as error:
            last_error = error
    raise OperatorError(
        OperatorErrorCode.TRANSPORT_UNAVAILABLE,
        "Destination inventory probe is unavailable.",
        detail=last_error,
    )


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    source: str
    network_id: str
    device_id: str
    stable_id: str
    display_name: str
    alias: str | None


@dataclass(frozen=True, slots=True)
class OperatorReadiness:
    alive: bool
    fresh: bool
    scheduler_ready: bool
    observation_age_ms: int | None
    reasons: tuple[str, ...]
    capacity: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class OperatorNodeResult:
    identity: OperatorIdentity
    managed_state: str
    binding_generation: str
    current_peer_id: str | None
    provenance: dict[str, str]
    readiness: OperatorReadiness
    managed_operations: tuple[str, ...]
    explicit_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedOperatorTarget:
    requested_target: str
    identity: OperatorIdentity
    current_peer_id: str
    binding_generation: str
    policy: ManagedTargetPolicy


@dataclass(frozen=True, slots=True)
class OperatorCompletionResult:
    task_id: str
    terminal_state: str
    requested_target: str | None = None
    resolved_target: ResolvedOperatorTarget | None = None
    routed_to: str | None = None
    delivery_route: str | None = None
    operation: str | None = None
    deadline_ms: int | None = None
    run_id: str | None = None
    result: str | None = None
    error_category: OperatorErrorCode | None = None
    transport_status: str | None = None
    execution_status: str | None = None


@dataclass(frozen=True, slots=True)
class ExactRecipeRequest:
    target: str
    prompt: str
    recipe: FleetRecipe
    agency_source: AgencySource
    secret_refs: tuple[str, ...] = ()
    deadline_seconds: int = 120

    def __post_init__(self) -> None:
        if (
            type(self.target) is not str
            or not self.target
            or self.target != self.target.strip()
        ):
            raise ValueError("target is invalid")
        if type(self.prompt) is not str or not self.prompt or len(self.prompt) > 16_000:
            raise ValueError("prompt is invalid")
        if type(self.recipe) is not FleetRecipe:
            raise ValueError("recipe is invalid")
        if type(self.agency_source) is not AgencySource:
            raise ValueError("Agency source is invalid")
        if type(self.secret_refs) not in (tuple, list) or any(
            type(reference) is not str for reference in self.secret_refs
        ):
            raise ValueError("secret references are invalid")
        object.__setattr__(self, "secret_refs", tuple(self.secret_refs))
        if (
            type(self.deadline_seconds) is not int
            or not 0 < self.deadline_seconds <= 900
        ):
            raise ValueError("deadline is invalid")


class OperatorState(Protocol):
    def overview(self) -> dict[str, Any]: ...

    def inspect_projection(
        self, *, source: str, network_id: str, device_id: str
    ) -> dict[str, Any]: ...


class OperatorService:
    """Reusable application logic for future CLI and Desktop adapters."""

    def __init__(
        self,
        *,
        state: OperatorState,
        config: FleetConfig,
        keryx: Any,
        recipe_submission: ExactRecipeSubmissionService | None = None,
        execution_id_factory: Any | None = None,
    ) -> None:
        if not callable(getattr(state, "overview", None)) or not callable(
            getattr(state, "inspect_projection", None)
        ):
            raise ValueError("state must provide authoritative overview and inspection")
        if type(config) is not FleetConfig:
            raise ValueError("config must be a FleetConfig")
        if not callable(getattr(keryx, "send_task", None)):
            raise ValueError("keryx must provide send_task()")
        self._state = state
        self._config = config
        self._keryx = keryx
        self._recipe_submission = recipe_submission or ExactRecipeSubmissionService()
        self._execution_id_factory = execution_id_factory or (lambda: str(uuid.uuid4()))

    def list_nodes(self) -> tuple[OperatorNodeResult, ...]:
        overview = self._state.overview()
        nodes = overview.get("nodes")
        if type(nodes) is not list:
            raise OperatorError(
                OperatorErrorCode.OPERATION_UNAVAILABLE,
                "Authoritative managed node inventory is unavailable.",
            )
        return tuple(self._node_result(node) for node in nodes)

    def inspect_node(self, target: str) -> OperatorNodeResult:
        resolved, row, projection = self._resolve(target)
        return self._node_result(row, projection=projection, policy=resolved.policy)

    def inspect_readiness(self, target: str) -> OperatorReadiness:
        return self.inspect_node(target).readiness

    def resolve_target(
        self, target: str, *, operation: str | None = None
    ) -> ResolvedOperatorTarget:
        resolved, _row, _projection = self._resolve(target)
        if (
            operation is not None
            and operation not in resolved.policy.policy.allowed_operations
        ):
            raise OperatorError(
                OperatorErrorCode.POLICY_DENIED,
                f"Operation {operation} is not explicitly allowed for this target.",
            )
        return resolved

    async def run_exact(self, request: ExactRecipeRequest) -> OperatorCompletionResult:
        submission, resolved = await self._submit_exact(request)
        try:
            task = await submission.handle.wait(float(request.deadline_seconds) + 5.0)
        except TimeoutError:
            return self._deadline_completion(
                submission=submission,
                request=request,
                resolved=resolved,
            )
        except TaskResultUnavailableError as error:
            status = getattr(getattr(submission.handle, "status", None), "value", None)
            deadline_elapsed = int(time.time() * 1_000) >= submission.deadline_ms
            if status == "failed" and deadline_elapsed:
                return self._deadline_completion(
                    submission=submission,
                    request=request,
                    resolved=resolved,
                )
            raise OperatorError(
                OperatorErrorCode.TRANSPORT_UNAVAILABLE,
                "Fleet transport is unavailable.",
                detail=error,
            ) from error
        except Exception as error:
            raise OperatorError(
                OperatorErrorCode.TRANSPORT_UNAVAILABLE,
                "Fleet transport is unavailable.",
                detail=error,
            ) from error
        return self._completion(
            task,
            task_id=submission.task_id,
            requested_target=request.target,
            resolved_target=resolved,
            routed_to=submission.routed_to,
            delivery_route=submission.delivery_route,
            operation="fleet.hermes.run",
            deadline_ms=submission.deadline_ms,
        )

    @staticmethod
    def _deadline_completion(
        *,
        submission: Any,
        request: ExactRecipeRequest,
        resolved: ResolvedOperatorTarget,
    ) -> OperatorCompletionResult:
        return OperatorCompletionResult(
            task_id=submission.task_id,
            terminal_state="timed_out",
            requested_target=request.target,
            resolved_target=resolved,
            routed_to=submission.routed_to,
            delivery_route=submission.delivery_route,
            operation="fleet.hermes.run",
            deadline_ms=submission.deadline_ms,
            run_id=None,
            result="Fleet execution deadline exceeded.",
            error_category=OperatorErrorCode.DEADLINE_EXCEEDED,
            transport_status="indeterminate",
            execution_status="timed_out",
        )

    async def submit_exact(
        self, request: ExactRecipeRequest
    ) -> OperatorCompletionResult:
        """Submit exact Recipe work and return its durable identity without waiting."""
        submission, resolved = await self._submit_exact(request)
        return OperatorCompletionResult(
            task_id=submission.task_id,
            terminal_state="submitted",
            requested_target=request.target,
            resolved_target=resolved,
            routed_to=submission.routed_to,
            delivery_route=submission.delivery_route,
            operation="fleet.hermes.run",
            deadline_ms=submission.deadline_ms,
            transport_status="submitted",
            execution_status=None,
        )

    async def _submit_exact(self, request: ExactRecipeRequest):
        if type(request) is not ExactRecipeRequest:
            raise OperatorError(
                OperatorErrorCode.OPERATION_UNAVAILABLE,
                (
                    "Exact execution requires an immutable Fleet Recipe "
                    "and pinned Agency source."
                ),
            )
        operation = "fleet.hermes.run"
        resolved, row, _projection = self._resolve(request.target)
        if operation not in resolved.policy.policy.allowed_operations:
            raise OperatorError(
                OperatorErrorCode.POLICY_DENIED,
                "Hermes execution is not explicitly allowed for this target.",
            )
        readiness = self._readiness(row)
        if not readiness.fresh:
            raise OperatorError(
                OperatorErrorCode.STALE_STATE,
                "Target readiness evidence is stale or missing.",
            )
        if not readiness.scheduler_ready:
            code = (
                OperatorErrorCode.NO_CAPACITY
                if "no_worker_capacity" in readiness.reasons
                else OperatorErrorCode.NOT_READY
            )
            raise OperatorError(code, "Target is not ready for Hermes execution.")
        policy = resolved.policy.policy
        transport_policy = NodePolicy(
            max_deadline_seconds=policy.max_deadline_seconds,
            max_payload_bytes=policy.max_payload_bytes,
            max_prompt_chars=policy.max_prompt_chars,
            max_export_paths=policy.max_export_paths,
            allowed_operations=tuple(
                sorted(set(policy.allowed_operations) | {"fleet.inventory"})
            ),
            allowed_secret_references=policy.allowed_secret_references,
        )
        node = NodeConfig(
            name=resolved.policy.target_name,
            peer_id=resolved.current_peer_id,
            policy=transport_policy,
        )
        submission_config = FleetConfig(
            schema_version=1,
            defaults=self._config.defaults,
            nodes=(node,),
        )
        try:
            inventory = await _inventory_probe_with_retry(
                FleetController(keryx=self._keryx, config=submission_config),
                node.name,
                deadline_seconds=request.deadline_seconds,
            )
            response = inventory.response
            backend = (
                response.get("execution_backend") if type(response) is dict else None
            )
            if type(backend) is not dict or set(backend) != {
                "content_hash",
                "document",
            }:
                raise OperatorError(
                    OperatorErrorCode.OPERATION_UNAVAILABLE,
                    "Target does not publish an exact Recipe execution backend.",
                )
            capabilities = BackendCapabilities.from_dict(backend["document"])
            if capabilities.content_hash != backend["content_hash"]:
                raise OperatorError(
                    OperatorErrorCode.STALE_STATE,
                    "Target execution capabilities are inconsistent.",
                )
            execution_id = self._execution_id_factory()
            requester = getattr(self._keryx, "peer_id", None)
            if (
                type(execution_id) is not str
                or not execution_id
                or type(requester) is not str
                or not requester
            ):
                raise OperatorError(
                    OperatorErrorCode.TRANSPORT_UNAVAILABLE,
                    "Authenticated controller identity is unavailable.",
                )
            submission = await self._recipe_submission.submit(
                keryx=self._keryx,
                requester=requester,
                peer_id=resolved.current_peer_id,
                execution_id=execution_id,
                recipe=request.recipe,
                capabilities=capabilities,
                agency_source=request.agency_source,
                target={
                    "source": resolved.identity.source,
                    "network_id": resolved.identity.network_id,
                    "device_id": resolved.identity.device_id,
                    "binding_generation": int(resolved.binding_generation),
                    "admission_generation": int(
                        row["readiness"]["admission_generation"]
                    ),
                },
                policy_digest=resolved.policy.policy.content_hash,
                prompt=request.prompt,
                secret_refs=list(request.secret_refs),
                deadline_seconds=request.deadline_seconds,
            )
        except OperatorError:
            raise
        except Exception as error:
            raise OperatorError(
                OperatorErrorCode.TRANSPORT_UNAVAILABLE,
                "Fleet transport is unavailable.",
                detail=error,
            ) from error
        return submission, resolved

    @staticmethod
    def _completion(
        task: object,
        *,
        task_id: str,
        requested_target: str | None = None,
        resolved_target: ResolvedOperatorTarget | None = None,
        routed_to: str | None = None,
        delivery_route: str | None = None,
        operation: str | None = None,
        deadline_ms: int | None = None,
    ) -> OperatorCompletionResult:
        status = getattr(getattr(task, "status", None), "value", None)
        if type(status) is not str or not status:
            raise OperatorError(
                OperatorErrorCode.TASK_INDETERMINATE,
                "Fleet task returned no trustworthy terminal state.",
            )
        outcome = _execution_outcome(task, task_id=task_id)
        if outcome is not None:
            outcome_status, reason = outcome
            outcome_categories = {
                "failed": OperatorErrorCode.TASK_FAILED,
                "indeterminate": OperatorErrorCode.TASK_INDETERMINATE,
                "policy_denied": OperatorErrorCode.POLICY_DENIED,
                "not_ready": OperatorErrorCode.NOT_READY,
                "no_capacity": OperatorErrorCode.NO_CAPACITY,
                "deadline_exceeded": OperatorErrorCode.DEADLINE_EXCEEDED,
                "expired": OperatorErrorCode.DEADLINE_EXCEEDED,
                "stale_state": OperatorErrorCode.STALE_STATE,
            }
            return OperatorCompletionResult(
                task_id=task_id,
                terminal_state=outcome_status,
                requested_target=requested_target,
                resolved_target=resolved_target,
                routed_to=routed_to,
                delivery_route=delivery_route,
                operation=operation,
                deadline_ms=deadline_ms,
                run_id=None,
                result=reason,
                error_category=outcome_categories.get(
                    outcome_status, OperatorErrorCode.REMOTE_REJECTED
                ),
                transport_status=status,
                execution_status=(
                    "indeterminate" if outcome_status == "indeterminate" else "failed"
                ),
            )
        artifacts = getattr(task, "artifacts", None)
        has_hermes_result = isinstance(artifacts, list) and any(
            getattr(artifact, "name", None) == "hermes-result.txt"
            for artifact in artifacts
        )
        metadata = getattr(task, "metadata", None)
        metadata = metadata if type(metadata) is dict else {}
        result = metadata.get("result_text")
        if type(result) is not str or len(result) > _MAX_RESULT_CHARS:
            result = None
        run_id = metadata.get("run_id")
        if type(run_id) is not str or not run_id:
            run_id = None
        error_categories = {
            "failed": OperatorErrorCode.TASK_FAILED,
            "canceled": OperatorErrorCode.TASK_FAILED,
            "cancelled": OperatorErrorCode.TASK_FAILED,
            "rejected": OperatorErrorCode.REMOTE_REJECTED,
            "expired": OperatorErrorCode.DEADLINE_EXCEEDED,
            "deadline_exceeded": OperatorErrorCode.DEADLINE_EXCEEDED,
            "timed_out": OperatorErrorCode.DEADLINE_EXCEEDED,
        }
        known_nonterminal = {"submitted", "pending", "working", "running", "leased"}
        if status == "completed" or status in known_nonterminal:
            error_category = None
        else:
            error_category = error_categories.get(
                status, OperatorErrorCode.TASK_INDETERMINATE
            )
        execution_evidence = (
            operation == "fleet.hermes.run" or has_hermes_result or run_id is not None
        )
        execution_status: str | None = None
        if execution_evidence:
            if status == "completed":
                execution_status = (
                    "succeeded" if result is not None else "indeterminate"
                )
            elif status not in known_nonterminal:
                execution_status = "indeterminate"
        return OperatorCompletionResult(
            task_id=task_id,
            terminal_state=status,
            requested_target=requested_target,
            resolved_target=resolved_target,
            routed_to=routed_to,
            delivery_route=delivery_route,
            operation=operation,
            deadline_ms=deadline_ms,
            run_id=run_id,
            result=result,
            error_category=error_category,
            transport_status=status,
            execution_status=execution_status,
        )

    async def inspect_task(self, task_id: str) -> OperatorCompletionResult:
        if type(task_id) is not str or not task_id or len(task_id) > 512:
            raise ValueError("task_id must be bounded text")
        handle_factory = getattr(self._keryx, "task_handle", None)
        if not callable(handle_factory):
            raise OperatorError(
                OperatorErrorCode.OPERATION_UNAVAILABLE,
                "Task reattachment is unavailable.",
            )
        try:
            task = await handle_factory(task_id).refresh()
        except Exception as error:
            raise OperatorError(
                OperatorErrorCode.TRANSPORT_UNAVAILABLE,
                "Fleet task status is unavailable.",
                detail=error,
            ) from error
        return self._completion(task, task_id=task_id)

    def _resolve(
        self, target: str
    ) -> tuple[ResolvedOperatorTarget, dict[str, Any], dict[str, Any]]:
        if type(target) is not str or not target or target != target.strip():
            raise OperatorError(OperatorErrorCode.UNKNOWN_TARGET, "Target is unknown.")
        overview = self._state.overview()
        rows = overview.get("nodes")
        if type(rows) is not list:
            raise OperatorError(
                OperatorErrorCode.OPERATION_UNAVAILABLE,
                "Authoritative managed node inventory is unavailable.",
            )
        matches = [row for row in rows if target in self._target_names(row)]
        if not matches:
            raise OperatorError(OperatorErrorCode.UNKNOWN_TARGET, "Target is unknown.")
        if len(matches) != 1:
            raise OperatorError(
                OperatorErrorCode.AMBIGUOUS_TARGET,
                "Target matches more than one managed node.",
            )
        row = matches[0]
        identity = self._identity(row)
        managed = row.get("managed")
        if type(managed) is not dict or managed.get("state") != "active":
            raise OperatorError(OperatorErrorCode.NOT_MANAGED, "Target is not active.")
        projection = self._state.inspect_projection(
            source=identity.source,
            network_id=identity.network_id,
            device_id=identity.device_id,
        )
        generated = projection.get("generated")
        if type(generated) is not dict:
            raise OperatorError(OperatorErrorCode.NOT_MANAGED, "Target is not managed.")
        if generated.get("binding_generation") != managed.get("binding_generation"):
            raise OperatorError(
                OperatorErrorCode.STALE_STATE,
                "Managed binding state changed during target resolution.",
            )
        provenance = generated.get("provenance")
        peer_id = (
            provenance.get("authenticated_peer_id")
            if type(provenance) is dict
            else None
        )
        if type(peer_id) is not str or not peer_id:
            raise OperatorError(
                OperatorErrorCode.NO_BINDING,
                "Target has no current authenticated Keryx binding.",
            )
        policies = [
            item
            for item in self._config.managed_targets
            if (item.source, item.network_id, item.device_id)
            == (identity.source, identity.network_id, identity.device_id)
        ]
        if len(policies) != 1:
            raise OperatorError(
                OperatorErrorCode.POLICY_DENIED,
                "Target has no unique explicit local operator policy.",
            )
        return (
            ResolvedOperatorTarget(
                requested_target=target,
                identity=identity,
                current_peer_id=peer_id,
                binding_generation=generated["binding_generation"],
                policy=policies[0],
            ),
            row,
            projection,
        )

    def _node_result(
        self,
        row: dict[str, Any],
        *,
        projection: dict[str, Any] | None = None,
        policy: ManagedTargetPolicy | None = None,
    ) -> OperatorNodeResult:
        identity = self._identity(row)
        if projection is None:
            projection = self._state.inspect_projection(
                source=identity.source,
                network_id=identity.network_id,
                device_id=identity.device_id,
            )
        generated = projection.get("generated")
        generated = generated if type(generated) is dict else {}
        provenance = generated.get("provenance")
        provenance = provenance if type(provenance) is dict else {}
        if policy is None:
            policy = next(
                (
                    item
                    for item in self._config.managed_targets
                    if (item.source, item.network_id, item.device_id)
                    == (identity.source, identity.network_id, identity.device_id)
                ),
                None,
            )
        peer_id = provenance.get("authenticated_peer_id")
        return OperatorNodeResult(
            identity=identity,
            managed_state=str(row["managed"]["state"]),
            binding_generation=str(row["managed"]["binding_generation"]),
            current_peer_id=peer_id if type(peer_id) is str else None,
            provenance={
                key: value
                for key, value in provenance.items()
                if type(key) is str and type(value) is str
            },
            readiness=self._readiness(row),
            managed_operations=tuple(row.get("operations", ())),
            explicit_operations=(
                policy.policy.allowed_operations if policy is not None else ()
            ),
        )

    @staticmethod
    def _identity(row: dict[str, Any]) -> OperatorIdentity:
        raw = row["identity"]
        naming = row["naming"]
        return OperatorIdentity(
            source=raw["source"],
            network_id=raw["network_id"],
            device_id=raw["device_id"],
            stable_id=row["stable_id"],
            display_name=naming["display_name"],
            alias=naming["alias"],
        )

    @staticmethod
    def _target_names(row: dict[str, Any]) -> set[str]:
        identity = row.get("identity", {})
        naming = row.get("naming", {})
        values = {
            row.get("stable_id"),
            identity.get("device_id"),
            naming.get("alias"),
        }
        return {value for value in values if type(value) is str and value}

    @staticmethod
    def _readiness(row: dict[str, Any]) -> OperatorReadiness:
        value = row["readiness"]
        return OperatorReadiness(
            alive=value["alive"],
            fresh=value["fresh"],
            scheduler_ready=value["scheduler_ready"],
            observation_age_ms=value["observation_age_ms"],
            reasons=tuple(value["reasons"]),
            capacity=value["capacity"],
        )
