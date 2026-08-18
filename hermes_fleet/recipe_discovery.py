"""Low-authority disposable discovery probes for Phase 8A Recipe resolution."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from .backend_capabilities import BackendCapabilities
from .execution_backend import BackendExecutionState, ExecutionPlan
from .oci_backend import DockerWorkshopBackend, OciRealizationSpec
from .recipe_requirements import CandidateRecipe, RequirementEvidence
from .recipes import ResolvedAgencyProfile, ResolvedRecipe
from .workflow_recipe_compiler import DiscoveryObservation, DiscoveryProbePolicy

_MAX_OUTPUT_BYTES: Final[int] = 256 * 1024
_MAX_ARGV: Final[int] = 32
_MAX_ARG_BYTES: Final[int] = 512
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,~-]+$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:token|secret|password|passwd|api[_-]?key|private[_-]?key)="
)


class RecipeDiscoveryError(RuntimeError):
    """A disposable discovery probe could not be proven safe or exact."""


def _safe_argv(value: object) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or not 0 < len(value) <= _MAX_ARGV:
        raise RecipeDiscoveryError("discovery probe argv is invalid")
    normalized: list[str] = []
    for argument in value:
        if (
            type(argument) is not str
            or not argument
            or len(argument.encode("utf-8")) > _MAX_ARG_BYTES
            or _SAFE_ARG_RE.fullmatch(argument) is None
            or _SECRET_ASSIGNMENT_RE.search(argument) is not None
        ):
            raise RecipeDiscoveryError("discovery probe argv contains an unsafe value")
        normalized.append(argument)
    return tuple(normalized)


def _run_probe(argv: list[str], *, timeout_seconds: float) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            text=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RecipeDiscoveryError(
            "discovery probe command could not be observed"
        ) from error
    if (
        len(completed.stdout) > _MAX_OUTPUT_BYTES
        or len(completed.stderr) > _MAX_OUTPUT_BYTES
    ):
        raise RecipeDiscoveryError("discovery probe output exceeds bound")
    if completed.returncode != 0:
        raise RecipeDiscoveryError("discovery probe command failed")
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecipeDiscoveryError("discovery probe output is not UTF-8") from error


@dataclass(frozen=True, slots=True)
class DiscoveryProbeResult:
    candidate_hash: str
    policy_hash: str
    plan_fingerprint: str
    container_id: str
    argv: tuple[str, ...]
    stdout_hash: str
    stdout: str

    def evidence(self, *, source: str = "discovery-probe") -> RequirementEvidence:
        return RequirementEvidence(
            kind="probe",
            source=source,
            digest=self.stdout_hash,
        )

    def observation(
        self,
        requirement_key: str,
        value: object,
        *,
        source: str = "discovery-probe",
    ) -> DiscoveryObservation:
        return DiscoveryObservation(
            requirement_key=requirement_key,
            value=value,
            evidence_digest=self.stdout_hash,
            source=source,
        )


BackendFactory = Callable[..., DockerWorkshopBackend]
ProbeCommand = Callable[..., str]


class DockerRecipeDiscoveryProbe:
    """Create, inspect, use, and destroy one low-authority Fleet workshop.

    No Hermes Agent is started. The resolved Agency identity is used only to make
    the backend plan identity exact and auditable for the Candidate Recipe.
    """

    def __init__(
        self,
        *,
        capabilities: BackendCapabilities,
        policy: DiscoveryProbePolicy,
        now_ms: Callable[[], int] | None = None,
        backend_factory: BackendFactory = DockerWorkshopBackend,
        command: ProbeCommand = _run_probe,
    ) -> None:
        if type(capabilities) is not BackendCapabilities:
            raise RecipeDiscoveryError("discovery backend capabilities are invalid")
        if type(policy) is not DiscoveryProbePolicy:
            raise RecipeDiscoveryError("discovery probe policy is invalid")
        if policy.network != "none":
            raise RecipeDiscoveryError("discovery probe network must remain disabled")
        if policy.cpu_millis > capabilities.cpu_millis:
            raise RecipeDiscoveryError("discovery probe CPU exceeds backend capacity")
        if policy.memory_bytes > capabilities.memory_bytes:
            raise RecipeDiscoveryError(
                "discovery probe memory exceeds backend capacity"
            )
        if (
            "container" not in capabilities.isolation
            or "none" not in capabilities.network
        ):
            raise RecipeDiscoveryError("discovery backend lacks required isolation")
        if now_ms is not None and not callable(now_ms):
            raise RecipeDiscoveryError("discovery probe clock is invalid")
        if not callable(backend_factory) or not callable(command):
            raise RecipeDiscoveryError("discovery probe dependency is invalid")
        self._capabilities = capabilities
        self._policy = policy
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._backend_factory = backend_factory
        self._command = command

    def run(
        self,
        *,
        candidate: CandidateRecipe,
        resolved_agent: ResolvedAgencyProfile,
        argv: tuple[str, ...] | list[str],
    ) -> DiscoveryProbeResult:
        if type(candidate) is not CandidateRecipe:
            raise RecipeDiscoveryError("discovery Candidate Recipe is invalid")
        if type(resolved_agent) is not ResolvedAgencyProfile:
            raise RecipeDiscoveryError("discovery Agency identity is invalid")
        if resolved_agent.name != candidate.agent.name:
            raise RecipeDiscoveryError(
                "discovery Agency identity does not satisfy Recipe"
            )
        probe_argv = _safe_argv(argv)
        now = self._now_ms()
        if isinstance(now, bool) or type(now) is not int or now < 0:
            raise RecipeDiscoveryError("discovery probe clock is invalid")
        deadline_ms = now + self._policy.deadline_ms
        if deadline_ms <= now:
            raise RecipeDiscoveryError("discovery probe deadline overflowed")

        resolved = ResolvedRecipe(
            recipe_hash=candidate.content_hash,
            agent=resolved_agent,
            extensions={},
        )
        identity = candidate.content_hash[7:31]
        plan = ExecutionPlan(
            execution_id=f"discovery-{identity}",
            idempotency_key=f"probe-{candidate.content_hash}",
            resolved_recipe=resolved,
            required_capabilities_hash=self._capabilities.content_hash,
        )
        realization = OciRealizationSpec(
            image=self._policy.image,
            argv=("sleep", "infinity"),
            network="none",
            cpu_millis=self._policy.cpu_millis,
            memory_bytes=self._policy.memory_bytes,
            pids_limit=self._policy.pids_limit,
        )
        backend = self._backend_factory(
            capabilities=self._capabilities,
            realization=realization,
            deadline_ms=deadline_ms,
            now_ms=self._now_ms,
        )
        if not isinstance(backend, DockerWorkshopBackend):
            raise RecipeDiscoveryError(
                "discovery backend factory returned invalid backend"
            )

        handle = None
        primary_error: BaseException | None = None
        try:
            handle = backend.ensure(plan)
            if handle.state != BackendExecutionState.RUNNING:
                raise RecipeDiscoveryError("discovery workshop is not running")
            observed = backend.inspect(handle)
            if (
                observed.state != BackendExecutionState.RUNNING
                or observed.realization_id != handle.realization_id
                or observed.plan_fingerprint != plan.fingerprint
            ):
                raise RecipeDiscoveryError("discovery workshop identity changed")
            timeout_seconds = max(0.001, (deadline_ms - self._now_ms()) / 1000.0)
            stdout = self._command(
                [
                    "docker",
                    "exec",
                    "--user",
                    "65532:65532",
                    handle.realization_id,
                    *probe_argv,
                ],
                timeout_seconds=timeout_seconds,
            )
            stdout_hash = "sha256:" + hashlib.sha256(stdout.encode("utf-8")).hexdigest()
            return DiscoveryProbeResult(
                candidate_hash=candidate.content_hash,
                policy_hash=self._policy.content_hash,
                plan_fingerprint=plan.fingerprint,
                container_id=handle.realization_id,
                argv=probe_argv,
                stdout_hash=stdout_hash,
                stdout=stdout,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                backend.cleanup_plan(plan, handle=handle)
                if backend.find(plan) is not None:
                    raise RecipeDiscoveryError("discovery workshop cleanup is unproven")
            except Exception as cleanup_error:
                if primary_error is None:
                    raise RecipeDiscoveryError(
                        "discovery workshop cleanup failed"
                    ) from cleanup_error
                raise RecipeDiscoveryError(
                    "discovery probe failed and workshop cleanup is unproven"
                ) from cleanup_error
