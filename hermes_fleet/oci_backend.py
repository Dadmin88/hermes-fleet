"""First provider-specific ExecutionBackend using the mature Docker OCI runtime."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .backend_capabilities import BackendCapabilities
from .execution_backend import (
    BackendExecutionHandle,
    BackendExecutionState,
    ExecutionBackend,
    ExecutionBackendError,
    ExecutionBackendErrorCode,
    ExecutionPlan,
)

_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./_-]{0,254}@sha256:[0-9a-f]{64})$"
)
_SAFE_ARGUMENT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,4096}$")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)^(?:[^=]*(?:token|secret|password|api[_-]?key)[^=]*)="
)
_LABEL_PREFIX = "dev.hermes.fleet."
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_OUTPUT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class OciRealizationSpec:
    """Validated provider-specific realization details for one exact image."""

    image: str
    argv: tuple[str, ...]
    network: str
    cpu_millis: int
    memory_bytes: int
    pids_limit: int

    def __post_init__(self) -> None:
        if type(self.image) is not str or _IMAGE_RE.fullmatch(self.image) is None:
            _invalid("OCI image must be pinned by sha256 digest")
        if type(self.argv) not in {list, tuple} or not 0 < len(self.argv) <= 128:
            _invalid("OCI argument vector is invalid")
        normalized: list[str] = []
        for argument in self.argv:
            if (
                type(argument) is not str
                or _SAFE_ARGUMENT_RE.fullmatch(argument) is None
                or _SECRET_ASSIGNMENT_RE.search(argument) is not None
            ):
                _invalid("OCI argument vector contains an unsafe value")
            normalized.append(argument)
        object.__setattr__(self, "argv", tuple(normalized))
        if self.network != "none":
            _invalid("the first OCI backend requires disabled networking")
        for value, label, maximum in (
            (self.cpu_millis, "OCI CPU limit", 1_000_000),
            (self.memory_bytes, "OCI memory limit", 1 << 50),
            (self.pids_limit, "OCI PID limit", 1_000_000),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                _invalid(f"{label} is invalid")


class DockerExecutionBackend(ExecutionBackend):
    """Docker CLI adapter with exact ownership and idempotent lifecycle checks."""

    def __init__(
        self,
        *,
        capabilities: BackendCapabilities,
        realization: OciRealizationSpec,
        command: Callable[[list[str]], str] | None = None,
    ) -> None:
        if type(capabilities) is not BackendCapabilities:
            _invalid("Docker capabilities are invalid")
        if capabilities.backend_kind != "fleet.dev/docker-oci":
            _invalid("Docker backend kind is invalid")
        if "container" not in capabilities.isolation:
            _invalid("Docker capabilities do not advertise container isolation")
        if realization.network not in capabilities.network:
            _invalid("Docker realization exceeds advertised network guarantees")
        if realization.cpu_millis > capabilities.cpu_millis:
            _invalid("Docker realization exceeds advertised CPU capacity")
        if realization.memory_bytes > capabilities.memory_bytes:
            _invalid("Docker realization exceeds advertised memory capacity")
        self._capabilities = capabilities
        self._realization = realization
        self._command = command or _run_docker

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def _prepare(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        name = _container_name(plan.execution_id)
        existing = self._inspect_optional(name)
        if existing is not None:
            return self._owned_handle(plan, existing)
        self._verify_image(plan)
        argv = self._create_argv(plan, name)
        try:
            self._command(argv)
        except ExecutionBackendError as error:
            recovered = self._inspect_optional(name)
            if recovered is None:
                raise ExecutionBackendError(
                    ExecutionBackendErrorCode.PREPARE_FAILED,
                    "Docker create outcome is indeterminate",
                ) from error
            return self._owned_handle(plan, recovered)
        created = self._inspect_optional(name)
        if created is None:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.PREPARE_FAILED,
                "Docker create was not authoritatively observable",
            )
        return self._owned_handle(plan, created)

    def start(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        document = self._inspect_owned(handle)
        current = self._handle_from_document(handle.execution_id, document)
        if current.state in {
            BackendExecutionState.RUNNING,
            BackendExecutionState.COMPLETED,
        }:
            return current
        if current.state != BackendExecutionState.PREPARED:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INVALID_TRANSITION,
                "Docker realization cannot be started from its current state",
            )
        try:
            self._command(["docker", "start", current.realization_id])
        except ExecutionBackendError as error:
            recovered = self._inspect_optional(current.realization_id)
            if recovered is not None:
                state = self._handle_from_document(handle.execution_id, recovered)
                if state.state in {
                    BackendExecutionState.RUNNING,
                    BackendExecutionState.COMPLETED,
                    BackendExecutionState.FAILED,
                }:
                    return state
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.START_FAILED,
                "Docker start outcome is indeterminate",
            ) from error
        return self.inspect(current)

    def inspect(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        return self._handle_from_document(
            handle.execution_id, self._inspect_owned(handle)
        )

    def stop(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        document = self._inspect_owned(handle)
        current = self._handle_from_document(handle.execution_id, document)
        if current.state in {
            BackendExecutionState.COMPLETED,
            BackendExecutionState.FAILED,
            BackendExecutionState.STOPPED,
        }:
            return current
        if current.state == BackendExecutionState.PREPARED:
            return current
        try:
            self._command(["docker", "stop", current.realization_id])
        except ExecutionBackendError as error:
            recovered = self._inspect_optional(current.realization_id)
            if recovered is not None:
                state = self._handle_from_document(handle.execution_id, recovered)
                if state.state != BackendExecutionState.RUNNING:
                    return state
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.STOP_INDETERMINATE,
                "Docker stop outcome is indeterminate",
            ) from error
        return current.with_state(BackendExecutionState.STOPPED)

    def cleanup(self, handle: BackendExecutionHandle) -> BackendExecutionHandle:
        document = self._inspect_optional(handle.realization_id)
        if document is None:
            return _cleaned_handle(handle)
        self._require_ownership(handle.execution_id, document)
        try:
            self._command(["docker", "rm", "--force", handle.realization_id])
        except ExecutionBackendError as error:
            if self._inspect_optional(handle.realization_id) is None:
                return _cleaned_handle(handle)
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.CLEANUP_FAILED,
                "Docker cleanup failed",
            ) from error
        if self._inspect_optional(handle.realization_id) is not None:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.CLEANUP_FAILED,
                "Docker cleanup was not authoritatively observable",
            )
        return _cleaned_handle(handle)

    def _verify_image(self, plan: ExecutionPlan) -> None:
        try:
            document = _one_document(
                self._command(["docker", "image", "inspect", self._realization.image])
            )
        except ExecutionBackendError as error:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.PREPARE_FAILED,
                "digest-pinned OCI image is unavailable",
            ) from error
        digests = document.get("RepoDigests")
        image_matches = (
            document.get("Id") == self._realization.image
            if self._realization.image.startswith("sha256:")
            else type(digests) is list and self._realization.image in digests
        )
        if not image_matches:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.CAPABILITY_MISMATCH,
                "local OCI image does not match the requested digest",
            )
        config = document.get("Config")
        labels = config.get("Labels") if type(config) is dict else None
        agent = plan.resolved_recipe.agent
        expected = {
            "dev.hermes.agency.repository": agent.repository,
            "dev.hermes.agency.revision": agent.revision,
            "dev.hermes.agency.profile": agent.name,
            "dev.hermes.agency.version": agent.version,
            "dev.hermes.agency.content": agent.content_digest,
        }
        if type(labels) is not dict or any(
            labels.get(key) != value for key, value in expected.items()
        ):
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.CAPABILITY_MISMATCH,
                "OCI image does not contain the exact resolved Agency identity",
            )

    def _create_argv(self, plan: ExecutionPlan, name: str) -> list[str]:
        labels = self._expected_labels(plan)
        argv = [
            "docker",
            "create",
            "--name",
            name,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            self._realization.network,
            "--pids-limit",
            str(self._realization.pids_limit),
            "--memory",
            str(self._realization.memory_bytes),
            "--memory-swap",
            str(self._realization.memory_bytes),
            "--cpus",
            _cpu_limit(self._realization.cpu_millis),
            "--log-driver",
            "none",
        ]
        for key in sorted(labels):
            argv.extend(["--label", f"{key}={labels[key]}"])
        argv.append(self._realization.image)
        argv.extend(self._realization.argv)
        return argv

    def _expected_labels(self, plan: ExecutionPlan) -> dict[str, str]:
        return {
            f"{_LABEL_PREFIX}backend": self.capabilities.backend_kind,
            f"{_LABEL_PREFIX}capabilities": self.capabilities.content_hash,
            f"{_LABEL_PREFIX}execution": plan.execution_id,
            f"{_LABEL_PREFIX}idempotency": _idempotency_digest(plan.idempotency_key),
            f"{_LABEL_PREFIX}recipe": plan.resolved_recipe_hash,
        }

    def _inspect_optional(self, identity: str) -> dict[str, object] | None:
        payload = self._command(["docker", "inspect", identity])
        try:
            value = json.loads(payload)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                "Docker returned invalid JSON",
            ) from error
        if value == []:
            return None
        if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                "Docker returned an unsupported document",
            )
        return value[0]

    def _inspect_owned(self, handle: BackendExecutionHandle) -> dict[str, object]:
        if (
            type(handle) is not BackendExecutionHandle
            or handle.backend_kind != self.capabilities.backend_kind
        ):
            _invalid("Docker execution handle is invalid")
        document = self._inspect_optional(handle.realization_id)
        if document is None:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                "Docker realization is unavailable",
            )
        self._require_ownership(handle.execution_id, document)
        return document

    def _owned_handle(
        self, plan: ExecutionPlan, document: dict[str, object]
    ) -> BackendExecutionHandle:
        self._require_ownership(plan.execution_id, document, plan=plan)
        return self._handle_from_document(plan.execution_id, document)

    def _require_ownership(
        self,
        execution_id: str,
        document: dict[str, object],
        *,
        plan: ExecutionPlan | None = None,
    ) -> None:
        config = document.get("Config")
        if type(config) is not dict:
            _ownership_error()
        labels = config.get("Labels")
        if type(labels) is not dict:
            _ownership_error()
        expected = {
            f"{_LABEL_PREFIX}backend": self.capabilities.backend_kind,
            f"{_LABEL_PREFIX}capabilities": self.capabilities.content_hash,
            f"{_LABEL_PREFIX}execution": execution_id,
        }
        if plan is not None:
            expected[f"{_LABEL_PREFIX}idempotency"] = _idempotency_digest(
                plan.idempotency_key
            )
            expected[f"{_LABEL_PREFIX}recipe"] = plan.resolved_recipe_hash
        if any(labels.get(key) != value for key, value in expected.items()):
            _ownership_error()
        if config.get("Image") != self._realization.image:
            _ownership_error()

    def _handle_from_document(
        self, execution_id: str, document: dict[str, object]
    ) -> BackendExecutionHandle:
        container_id = document.get("Id")
        if type(container_id) is not str or not container_id:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                "Docker realization identity is invalid",
            )
        state = document.get("State")
        config = document.get("Config")
        labels = config.get("Labels") if type(config) is dict else None
        plan_fingerprint = _plan_fingerprint_from_labels(labels)
        if plan_fingerprint is None:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                "Docker realization plan identity is invalid",
            )
        if type(state) is not dict or type(state.get("Status")) is not str:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                "Docker realization state is invalid",
            )
        status = state["Status"]
        if status == "created":
            mapped = BackendExecutionState.PREPARED
        elif status in {"running", "restarting", "paused"}:
            mapped = BackendExecutionState.RUNNING
        elif status == "exited":
            mapped = (
                BackendExecutionState.COMPLETED
                if state.get("ExitCode") == 0
                else BackendExecutionState.FAILED
            )
        elif status in {"dead", "removing"}:
            mapped = BackendExecutionState.INDETERMINATE
        else:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
                "Docker realization returned an unsupported state",
            )
        return BackendExecutionHandle(
            execution_id=execution_id,
            backend_kind=self.capabilities.backend_kind,
            realization_id=container_id,
            plan_fingerprint=plan_fingerprint,
            state=mapped,
        )


def _run_docker(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        code = (
            ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE
            if len(argv) > 1 and argv[1] == "inspect"
            else ExecutionBackendErrorCode.PREPARE_FAILED
        )
        raise ExecutionBackendError(code, "Docker command was unavailable") from error
    if len(completed.stdout.encode()) > _MAX_OUTPUT_BYTES:
        raise ExecutionBackendError(
            ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
            "Docker output exceeded its bound",
        )
    if completed.returncode != 0:
        if len(argv) > 1 and argv[1] == "inspect" and completed.returncode == 1:
            stderr = completed.stderr.strip()
            if "No such object:" in stderr or "No such container:" in stderr:
                return "[]"
        raise ExecutionBackendError(
            ExecutionBackendErrorCode.PREPARE_FAILED,
            "Docker command failed",
        )
    return completed.stdout


def _one_document(payload: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ExecutionBackendError(
            ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
            "Docker returned invalid JSON",
        ) from error
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise ExecutionBackendError(
            ExecutionBackendErrorCode.INSPECTION_UNAVAILABLE,
            "Docker returned an unsupported document",
        )
    return value[0]


def _container_name(execution_id: str) -> str:
    digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:32]
    return f"hermes-fleet-{digest}"


def _cpu_limit(cpu_millis: int) -> str:
    whole, fraction = divmod(cpu_millis, 1000)
    return f"{whole}.{fraction:03d}"


def _idempotency_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plan_fingerprint_from_labels(labels: object) -> str | None:
    """Recover exact plan identity from labels written by every FX4 release."""
    if type(labels) is not dict:
        return None
    execution = labels.get(f"{_LABEL_PREFIX}execution")
    capabilities = labels.get(f"{_LABEL_PREFIX}capabilities")
    idempotency = labels.get(f"{_LABEL_PREFIX}idempotency")
    recipe = labels.get(f"{_LABEL_PREFIX}recipe")
    if (
        type(execution) is not str
        or type(capabilities) is not str
        or type(idempotency) is not str
        or type(recipe) is not str
        or _HASH_RE.fullmatch(capabilities) is None
        or _HASH_RE.fullmatch(idempotency) is None
        or _HASH_RE.fullmatch(recipe) is None
    ):
        return None
    document = json.dumps(
        {
            "capabilities": capabilities,
            "execution": execution,
            "idempotency": idempotency.removeprefix("sha256:"),
            "recipe": recipe,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(document).hexdigest()


def _cleaned_handle(handle: BackendExecutionHandle) -> BackendExecutionHandle:
    """Record CLEANED only after provider inspection proved realization absence."""
    return BackendExecutionHandle(
        execution_id=handle.execution_id,
        backend_kind=handle.backend_kind,
        realization_id=handle.realization_id,
        plan_fingerprint=handle.plan_fingerprint,
        state=BackendExecutionState.CLEANED,
    )


def _invalid(message: str) -> None:
    raise ExecutionBackendError(ExecutionBackendErrorCode.INVALID_INPUT, message)


def _ownership_error() -> None:
    raise ExecutionBackendError(
        ExecutionBackendErrorCode.CAPABILITY_MISMATCH,
        "Docker realization conflicts with Fleet ownership",
    )
