"""Destination-owned FX8 exact-Recipe execution lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .execution_package import ExactExecutionPackage
from .hermes_runs import HermesRunSubmissionUnknown
from .profile_runtime import LocalFileSecret

_BACKEND_KIND = "hermes.local/profile-runs"
_OUTCOME_ARTIFACT_NAME = "fleet-execution-outcome.v1.json"
_OUTCOME_SCHEMA = "fleet.execution-outcome.v1"
_APPROVAL_EXTENSION = "fleet.hermes/approvals.v1"
_OUTCOME_EXTENSION = "fleet.hermes/outcome.v1"
_FINALIZE_TIMEOUT_SECONDS = 5.0


class DestinationExecutionError(RuntimeError):
    """The destination cannot safely execute or reconcile an exact package."""


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    mode: str
    max_requests: int


@dataclass(frozen=True, slots=True)
class OutcomePolicy:
    min_successful_commands: int
    require_last_command_success: bool
    require_no_pending_processes: bool


class DestinationRuntime(Protocol):
    def materialize(
        self, package: ExactExecutionPackage, *, secrets: dict[str, str]
    ) -> str: ...

    def start(
        self,
        profile: str,
        *,
        prompt: str,
        session_id: str,
        timeout_seconds: float,
        approval_budget: int | None = None,
    ) -> str: ...

    def wait(
        self,
        profile: str,
        *,
        run_id: str,
        timeout_seconds: float,
        approval_mode: str | None = None,
        approval_budget: int | None = None,
    ) -> Any: ...

    def finalize(
        self,
        profile: str,
        *,
        run_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...

    def cleanup(self, profile: str, *, expected_owner: str) -> None: ...

    def inspect_owner(self, profile: str) -> str | None: ...

    def inspect(self, profile: str, *, run_id: str) -> Any: ...


class DestinationSecretResolver(Protocol):
    def resolve(
        self,
        references: list[str],
        *,
        requester: str,
        target: dict[str, Any],
        execution_id: str,
    ) -> dict[str, str]: ...


class DestinationRecipeExecutor:
    """Admit first, then materialize and run one execution-owned Hermes profile."""

    def __init__(
        self,
        *,
        execution_control: Any,
        runtime: DestinationRuntime,
        secret_resolver: DestinationSecretResolver,
        current_policy_digest: Callable[[], str],
        current_capabilities_hash: Callable[[], str],
        now_ms: Callable[[], int],
    ) -> None:
        self._control = execution_control
        self._runtime = runtime
        self._secrets = secret_resolver
        self._policy_digest = current_policy_digest
        self._capabilities_hash = current_capabilities_hash
        self._now_ms = now_ms

    async def execute(
        self,
        *,
        package: ExactExecutionPackage,
        authenticated_sender: str,
        incoming: object,
    ) -> str:
        if type(package) is not ExactExecutionPackage:
            raise DestinationExecutionError("execution package is invalid")
        authorization = package.authorization
        if authenticated_sender != authorization["requester"]:
            raise DestinationExecutionError("authenticated sender is not the requester")
        now = self._now_ms()
        if type(now) is not int or now > authorization["deadline_ms"]:
            raise DestinationExecutionError("execution authorization has expired")
        policy_digest = self._policy_digest()
        capabilities_hash = self._capabilities_hash()
        if authorization["policy_digest"] != policy_digest:
            raise DestinationExecutionError("execution policy authorization is stale")
        if package.capabilities_hash != capabilities_hash:
            raise DestinationExecutionError("execution capabilities are stale")
        approval_policy = _approval_policy(package)
        approval_mode = approval_policy.mode if approval_policy is not None else None
        approval_budget = (
            approval_policy.max_requests if approval_policy is not None else None
        )
        outcome_policy = _outcome_policy(package)

        instance = {
            "instance_id": package.execution_id,
            "idempotency_key": package.idempotency_key,
            "recipe_hash": package.resolved_recipe.content_hash,
            "capabilities_hash": package.capabilities_hash,
            "target": package.target,
            "generation": 1,
            "phase": {"kind": "reserved"},
            "created_at_ms": now,
            "updated_at_ms": now,
        }
        admission = self._control.reserve_admit(
            instance,
            authorization={
                "authenticated_sender": authenticated_sender,
                "requester": authorization["requester"],
                "operation": authorization["operation"],
                "recipe_hash": authorization["resolved_recipe_hash"],
                "policy_digest": authorization["policy_digest"],
                "deadline_ms": authorization["deadline_ms"],
                "secret_refs_digest": _secret_refs_digest(authorization["secret_refs"]),
            },
            current_policy_digest=policy_digest,
            current_capabilities_hash=capabilities_hash,
            deadline_ms=authorization["deadline_ms"],
        )
        decision = admission.get("decision", {}).get("status")
        if decision != "admitted":
            return str(decision or "invalid_request")
        returned = admission.get("instance")
        generation = returned.get("generation") if type(returned) is dict else None
        if type(generation) is not int:
            raise DestinationExecutionError("admission omitted execution instance")
        if admission.get("created") is False:
            if returned["phase"]["kind"] == "reserved":
                reason = "reserved execution ownership is uncertain"
                await _fail_incoming(incoming, reason)
                raise DestinationExecutionError(reason)
            return await self._reconcile(
                package=package,
                instance=returned,
                incoming=incoming,
            )

        remaining = (authorization["deadline_ms"] - self._now_ms()) / 1_000
        if remaining <= 0:
            raise DestinationExecutionError("execution deadline has expired")
        secrets = self._secrets.resolve(
            authorization["secret_refs"],
            requester=authorization["requester"],
            target=package.target,
            execution_id=package.execution_id,
        )
        if type(secrets) is not dict or any(
            type(key) is not str
            or not (
                (type(value) is str and bool(value)) or type(value) is LocalFileSecret
            )
            for key, value in secrets.items()
        ):
            raise DestinationExecutionError(
                "secret resolution returned invalid material"
            )
        if set(secrets) != set(authorization["secret_refs"]):
            raise DestinationExecutionError(
                "secret resolution did not match authorization"
            )

        profile: str | None = None
        run_id: str | None = None
        try:
            profile = self._runtime.materialize(package, secrets=secrets)
            secrets.clear()
            generation = self._transition(
                package.execution_id,
                generation,
                {
                    "kind": "prepared",
                    "backend_kind": _BACKEND_KIND,
                    "realization_id": profile,
                },
            )
            try:
                run_id = self._runtime.start(
                    profile,
                    prompt=package.prompt,
                    session_id=f"fleet:{package.execution_id}",
                    approval_budget=approval_budget,
                    timeout_seconds=remaining,
                )
            except HermesRunSubmissionUnknown:
                self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "indeterminate",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": None,
                        "hermes_run_id": None,
                        "reason": "Hermes start response is uncertain",
                    },
                )
                await _fail_incoming(
                    incoming, "Hermes execution outcome is indeterminate"
                )
                return "indeterminate"
            except Exception:
                generation = self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "failed",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": None,
                    },
                )
                generation = self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "cleanup_pending",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": None,
                        "reason": "failed Hermes start requires profile cleanup",
                    },
                )
                self._runtime.cleanup(profile, expected_owner=package.execution_id)
                self._transition(package.execution_id, generation, {"kind": "cleaned"})
                await _fail_incoming(incoming, "Hermes execution failed")
                return "failed"
            generation = self._transition(
                package.execution_id,
                generation,
                {
                    "kind": "running",
                    "backend_kind": _BACKEND_KIND,
                    "realization_id": profile,
                    "keryx_task_id": package.execution_id,
                    "hermes_run_id": run_id,
                },
            )
            try:
                result = self._runtime.wait(
                    profile,
                    run_id=run_id,
                    timeout_seconds=remaining,
                    approval_mode=approval_mode,
                    approval_budget=approval_budget,
                )
            except TimeoutError:
                self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "indeterminate",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": run_id,
                        "reason": "Hermes terminal response is uncertain",
                    },
                )
                await _fail_incoming(
                    incoming, "Hermes execution outcome is indeterminate"
                )
                return "indeterminate"
            except Exception:
                generation = self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "failed",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": run_id,
                    },
                )
                generation = self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "cleanup_pending",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": run_id,
                        "reason": (
                            "failed Hermes run requires quiescent profile cleanup"
                        ),
                    },
                )
                try:
                    await self._finalize_profile(profile, run_id)
                except Exception:
                    await _fail_incoming(
                        incoming, "Hermes execution outcome is indeterminate"
                    )
                    return "indeterminate"
                self._runtime.cleanup(profile, expected_owner=package.execution_id)
                self._transition(package.execution_id, generation, {"kind": "cleaned"})
                await _fail_incoming(incoming, "Hermes execution failed")
                return "failed"
            if (
                getattr(result, "run_id", None) != run_id
                or type(getattr(result, "text", None)) is not str
            ):
                raise DestinationExecutionError("Hermes returned an invalid result")
            try:
                finalization = await self._finalize_profile(profile, run_id)
            except Exception:
                self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "indeterminate",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": run_id,
                        "reason": (
                            "Hermes execution completed but profile "
                            "quiescence is unproven"
                        ),
                    },
                )
                await _complete_outcome(
                    incoming,
                    execution_id=package.execution_id,
                    status="indeterminate",
                    reason=(
                        "Hermes execution completed but profile quiescence is unproven"
                    ),
                )
                return "indeterminate"

            verification = _verify_outcome_policy(outcome_policy, finalization)
            if verification is not None:
                verification_status, reason = verification
                transition = {"kind": verification_status}
                transition.update(
                    {
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": run_id,
                    }
                )
                if verification_status == "indeterminate":
                    transition["reason"] = reason
                generation = self._transition(
                    package.execution_id,
                    generation,
                    transition,
                )
                generation = self._transition(
                    package.execution_id,
                    generation,
                    {
                        "kind": "cleanup_pending",
                        "backend_kind": _BACKEND_KIND,
                        "realization_id": profile,
                        "keryx_task_id": package.execution_id,
                        "hermes_run_id": run_id,
                        "reason": (
                            "verified terminal Hermes run requires profile cleanup"
                        ),
                    },
                )
                self._runtime.cleanup(profile, expected_owner=package.execution_id)
                self._transition(package.execution_id, generation, {"kind": "cleaned"})
                await _complete_outcome(
                    incoming,
                    execution_id=package.execution_id,
                    status=verification_status,
                    reason=reason,
                )
                return verification_status

            generation = self._transition(
                package.execution_id,
                generation,
                {
                    "kind": "completed",
                    "backend_kind": _BACKEND_KIND,
                    "realization_id": profile,
                    "keryx_task_id": package.execution_id,
                    "hermes_run_id": run_id,
                },
            )
            generation = self._transition(
                package.execution_id,
                generation,
                {
                    "kind": "cleanup_pending",
                    "backend_kind": _BACKEND_KIND,
                    "realization_id": profile,
                    "keryx_task_id": package.execution_id,
                    "hermes_run_id": run_id,
                    "reason": "verified Hermes run requires quiescent profile cleanup",
                },
            )
            self._runtime.cleanup(profile, expected_owner=package.execution_id)
            self._transition(package.execution_id, generation, {"kind": "cleaned"})
            complete = getattr(incoming, "complete", None)
            if not callable(complete):
                raise DestinationExecutionError("Keryx completion is unavailable")
            await cast(Callable[[list[Any]], Awaitable[None]], complete)(
                _result_artifact(
                    text=result.text,
                    run_id=run_id,
                    execution_id=package.execution_id,
                )
            )
            return "completed"
        finally:
            secrets.clear()

    async def _reconcile(
        self,
        *,
        package: ExactExecutionPackage,
        instance: dict[str, Any],
        incoming: object,
    ) -> str:
        phase = instance["phase"]
        if phase.get("backend_kind") != _BACKEND_KIND:
            raise DestinationExecutionError("execution backend provenance changed")
        profile = phase.get("realization_id")
        run_id = phase.get("hermes_run_id")
        if type(profile) is not str or type(run_id) is not str:
            raise DestinationExecutionError("execution recovery identity is incomplete")
        if phase.get("keryx_task_id") not in {None, package.execution_id}:
            raise DestinationExecutionError("execution task provenance changed")
        if self._runtime.inspect_owner(profile) != package.execution_id:
            raise DestinationExecutionError("execution profile ownership is unproven")
        inspection = self._runtime.inspect(profile, run_id=run_id)
        if getattr(inspection, "run_id", None) != run_id:
            raise DestinationExecutionError("Hermes recovery identity changed")
        status = getattr(inspection, "status", None)
        if status in {"queued", "running", "waiting_for_approval"}:
            await _fail_incoming(incoming, "Hermes execution remains in progress")
            return "indeterminate"
        if status != "completed" or type(getattr(inspection, "text", None)) is not str:
            await _fail_incoming(incoming, "Hermes execution failed during recovery")
            return "failed"
        generation = instance["generation"]
        try:
            finalization = await self._finalize_profile(profile, run_id)
        except Exception:
            self._transition(
                package.execution_id,
                generation,
                {
                    "kind": "indeterminate",
                    "backend_kind": _BACKEND_KIND,
                    "realization_id": profile,
                    "keryx_task_id": package.execution_id,
                    "hermes_run_id": run_id,
                    "reason": "Reconciled Hermes run quiescence is unproven",
                },
            )
            await _complete_outcome(
                incoming,
                execution_id=package.execution_id,
                status="indeterminate",
                reason="Reconciled Hermes run quiescence is unproven",
            )
            return "indeterminate"

        verification = _verify_outcome_policy(_outcome_policy(package), finalization)
        if verification is not None:
            verification_status, reason = verification
            transition = {"kind": verification_status}
            transition.update(
                {
                    "backend_kind": _BACKEND_KIND,
                    "realization_id": profile,
                    "keryx_task_id": package.execution_id,
                    "hermes_run_id": run_id,
                }
            )
            if verification_status == "indeterminate":
                transition["reason"] = reason
            generation = self._transition(
                package.execution_id,
                generation,
                transition,
            )
            generation = self._transition(
                package.execution_id,
                generation,
                {
                    "kind": "cleanup_pending",
                    "backend_kind": _BACKEND_KIND,
                    "realization_id": profile,
                    "keryx_task_id": package.execution_id,
                    "hermes_run_id": run_id,
                    "reason": "reconciled verified Hermes run requires profile cleanup",
                },
            )
            self._runtime.cleanup(profile, expected_owner=package.execution_id)
            self._transition(package.execution_id, generation, {"kind": "cleaned"})
            await _complete_outcome(
                incoming,
                execution_id=package.execution_id,
                status=verification_status,
                reason=reason,
            )
            return verification_status

        generation = self._transition(
            package.execution_id,
            generation,
            {
                "kind": "completed",
                "backend_kind": _BACKEND_KIND,
                "realization_id": profile,
                "keryx_task_id": package.execution_id,
                "hermes_run_id": run_id,
            },
        )
        generation = self._transition(
            package.execution_id,
            generation,
            {
                "kind": "cleanup_pending",
                "backend_kind": _BACKEND_KIND,
                "realization_id": profile,
                "keryx_task_id": package.execution_id,
                "hermes_run_id": run_id,
                "reason": "reconciled verified Hermes run requires profile cleanup",
            },
        )
        self._runtime.cleanup(profile, expected_owner=package.execution_id)
        self._transition(package.execution_id, generation, {"kind": "cleaned"})
        complete = getattr(incoming, "complete", None)
        if not callable(complete):
            raise DestinationExecutionError("Keryx completion is unavailable")
        await cast(Callable[[list[Any]], Awaitable[None]], complete)(
            _result_artifact(
                text=inspection.text,
                run_id=run_id,
                execution_id=package.execution_id,
            )
        )
        return "completed"

    async def _finalize_profile(self, profile: str, run_id: str) -> dict[str, Any]:
        """Require Hermes quiescence before deleting execution-owned state."""
        document = await asyncio.to_thread(
            self._runtime.finalize,
            profile,
            run_id=run_id,
            timeout_seconds=_FINALIZE_TIMEOUT_SECONDS,
        )
        if (
            type(document) is not dict
            or document.get("run_id") != run_id
            or document.get("quiescent") is not True
            or document.get("status") not in {"completed", "failed", "cancelled"}
        ):
            raise DestinationExecutionError("Hermes profile quiescence is unproven")
        return document

    def _transition(
        self, execution_id: str, generation: int, phase: dict[str, Any]
    ) -> int:
        returned = self._control.transition(
            execution_id, expected_generation=generation, phase=phase
        )
        if type(returned) is dict and type(returned.get("instance")) is dict:
            returned = returned["instance"]
        next_generation = returned.get("generation") if type(returned) is dict else None
        if type(next_generation) is not int or next_generation != generation + 1:
            raise DestinationExecutionError(
                "execution transition returned invalid state"
            )
        return next_generation


def _approval_policy(package: ExactExecutionPackage) -> ApprovalPolicy | None:
    approval = package.resolved_recipe.extensions.get(_APPROVAL_EXTENSION)
    if approval is None:
        return None
    if (
        not isinstance(approval, Mapping)
        or set(approval) != {"mode", "max_requests"}
        or approval.get("mode") != "once"
        or type(approval.get("max_requests")) is not int
        or not 1 <= approval["max_requests"] <= 32
    ):
        raise DestinationExecutionError("execution approval extension is invalid")
    return ApprovalPolicy(mode="once", max_requests=approval["max_requests"])


def _outcome_policy(package: ExactExecutionPackage) -> OutcomePolicy | None:
    outcome = package.resolved_recipe.extensions.get(_OUTCOME_EXTENSION)
    if outcome is None:
        return None
    expected = {
        "min_successful_commands",
        "require_last_command_success",
        "require_no_pending_processes",
    }
    if (
        not isinstance(outcome, Mapping)
        or set(outcome) != expected
        or type(outcome.get("min_successful_commands")) is not int
        or not 0 <= outcome["min_successful_commands"] <= 32
        or type(outcome.get("require_last_command_success")) is not bool
        or type(outcome.get("require_no_pending_processes")) is not bool
    ):
        raise DestinationExecutionError("execution outcome extension is invalid")
    return OutcomePolicy(
        min_successful_commands=outcome["min_successful_commands"],
        require_last_command_success=outcome["require_last_command_success"],
        require_no_pending_processes=outcome["require_no_pending_processes"],
    )


def _verify_outcome_policy(
    policy: OutcomePolicy | None,
    finalization: dict[str, Any],
) -> tuple[str, str] | None:
    if policy is None:
        return None
    command_calls = finalization.get("command_calls")
    command_errors = finalization.get("command_errors")
    last_command_error = finalization.get("last_command_error")
    pending_processes = finalization.get("pending_processes")
    evidence_invalid = finalization.get("command_evidence_invalid")
    if (
        type(command_calls) is not int
        or type(command_errors) is not int
        or type(pending_processes) is not int
        or type(evidence_invalid) is not bool
        or command_calls < 0
        or command_errors < 0
        or command_errors > command_calls
        or pending_processes < 0
        or last_command_error not in {None, False, True}
        or (command_calls == 0 and last_command_error is not None)
        or (command_calls > 0 and type(last_command_error) is not bool)
        or evidence_invalid
    ):
        return "indeterminate", "Hermes command outcome evidence is invalid"
    if policy.require_no_pending_processes and pending_processes != 0:
        return "indeterminate", "Hermes command outcome remains pending"
    successful_commands = command_calls - command_errors
    if successful_commands < policy.min_successful_commands:
        return "failed", "Hermes command outcome verification failed"
    if policy.require_last_command_success and (
        command_calls == 0 or last_command_error is not False
    ):
        return "failed", "Hermes command outcome verification failed"
    return None


def _secret_refs_digest(references: list[str]) -> str:
    payload = json.dumps(
        references, ensure_ascii=True, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _result_artifact(*, text: str, run_id: str, execution_id: str) -> list[Any]:
    return [
        {
            "name": "hermes-result.txt",
            "parts": [
                {
                    "text": text,
                    "media_type": "text/plain",
                    "metadata": {
                        "hermes_run_id": run_id,
                        "execution_instance_id": execution_id,
                    },
                }
            ],
        }
    ]


def _outcome_artifact(*, execution_id: str, status: str, reason: str) -> list[Any]:
    document = json.dumps(
        {
            "schema": _OUTCOME_SCHEMA,
            "execution_id": execution_id,
            "status": status,
            "reason": reason,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return [
        {
            "name": _OUTCOME_ARTIFACT_NAME,
            "parts": [{"text": document, "media_type": "application/json"}],
        }
    ]


async def _complete_outcome(
    incoming: object,
    *,
    execution_id: str,
    status: str,
    reason: str,
) -> None:
    complete = getattr(incoming, "complete", None)
    if not callable(complete):
        raise DestinationExecutionError("Keryx completion is unavailable")
    await cast(Callable[[list[Any]], Awaitable[None]], complete)(
        _outcome_artifact(
            execution_id=execution_id,
            status=status,
            reason=reason,
        )
    )


async def _fail_incoming(incoming: object, reason: str) -> None:
    fail = getattr(incoming, "fail", None)
    if not callable(fail):
        raise DestinationExecutionError("Keryx failure reporting is unavailable")
    await cast(Callable[[str], Awaitable[None]], fail)(reason)
