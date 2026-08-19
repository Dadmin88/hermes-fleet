"""Authenticated loopback client for Hermes's public Runs API."""

from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

_MAX_RESPONSE_BYTES = 1_048_576
_ACTIVE_STATES = frozenset({"queued", "running", "stopping"})
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MEMORY_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$")
_MEMORY_SCOPE_KINDS = frozenset(
    {"principal", "project", "network", "owner", "agent_instance"}
)
_PRINCIPAL_KINDS = frozenset({"owner", "project", "network", "device", "service"})
_MAX_MEMORY_READ_SCOPES = 16
_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./_-]{0,254}@sha256:[0-9a-f]{64})$"
)


class HermesRunError(RuntimeError):
    """Stable Fleet-owned error for a local Hermes run failure."""


class HermesRunSubmissionUnknown(HermesRunError):
    """Run creation may have succeeded, so reposting would be unsafe."""


class HermesRunIndeterminate(HermesRunError):
    """A known run can no longer be observed safely."""


class HermesRunDeadlineExceeded(HermesRunError):
    """The exact bound run accepted cancellation at its Fleet deadline."""


@dataclass(frozen=True, slots=True)
class HermesFleetRuntimeBinding:
    """Exact Phase 7 run-scoped Hermes binding; never persistent profile config."""

    container_id: str
    plan_fingerprint: str
    image: str
    max_iterations: int
    version: str = "fleet-run-v1"
    toolsets: tuple[str, ...] = ("fleet-terminal",)

    def __post_init__(self) -> None:
        if self.version != "fleet-run-v1":
            raise ValueError("Fleet runtime version is unsupported")
        if (
            type(self.container_id) is not str
            or _CONTAINER_ID_RE.fullmatch(self.container_id) is None
        ):
            raise ValueError("Fleet runtime container ID is invalid")
        if (
            type(self.plan_fingerprint) is not str
            or _HASH_RE.fullmatch(self.plan_fingerprint) is None
        ):
            raise ValueError("Fleet runtime plan fingerprint is invalid")
        if type(self.image) is not str or _IMAGE_RE.fullmatch(self.image) is None:
            raise ValueError("Fleet runtime image must be digest-pinned")
        if type(self.toolsets) not in {tuple, list}:
            raise ValueError("Fleet runtime toolsets are invalid")
        toolsets = tuple(self.toolsets)
        if toolsets != ("fleet-terminal",):
            raise ValueError("Fleet runtime toolsets must be exactly fleet-terminal")
        object.__setattr__(self, "toolsets", toolsets)
        if (
            isinstance(self.max_iterations, bool)
            or type(self.max_iterations) is not int
            or not 1 <= self.max_iterations <= 32
        ):
            raise ValueError("Fleet runtime max_iterations must be between 1 and 32")

    def to_request(self) -> dict[str, object]:
        return {
            "version": self.version,
            "container_id": self.container_id,
            "plan_fingerprint": self.plan_fingerprint,
            "image": self.image,
            "toolsets": list(self.toolsets),
            "max_iterations": self.max_iterations,
        }


@dataclass(frozen=True, slots=True)
class HermesMemoryScopeRef:
    """One exact scope understood by Hermes native Fleet memory."""

    kind: str
    scope_id: str

    def __post_init__(self) -> None:
        if self.kind not in _MEMORY_SCOPE_KINDS:
            raise ValueError("Hermes memory scope kind is invalid")
        if self.kind in {"principal", "agent_instance"}:
            if (
                type(self.scope_id) is not str
                or _HASH_RE.fullmatch(self.scope_id) is None
            ):
                raise ValueError("Hermes memory scope ID is invalid")
        elif (
            type(self.scope_id) is not str
            or _MEMORY_IDENTIFIER_RE.fullmatch(self.scope_id) is None
        ):
            raise ValueError("Hermes memory scope ID is invalid")

    def to_request(self) -> dict[str, str]:
        return {"kind": self.kind, "scope_id": self.scope_id}


@dataclass(frozen=True, slots=True)
class HermesFleetMemoryBinding:
    """Exact Fleet memory authorization sent to one Hermes run or write."""

    principal_id: str
    principal_kind: str
    principal_generation: int
    principal_binding_hash: str
    agent_instance_id: str
    source_run: str
    read_scopes: tuple[HermesMemoryScopeRef, ...]
    write_scope: HermesMemoryScopeRef
    retention_until_ms: int | None = None
    version: str = "fleet-memory-v1"

    def __post_init__(self) -> None:
        if self.version != "fleet-memory-v1":
            raise ValueError("Hermes Fleet memory version is unsupported")
        for value, label in (
            (self.principal_id, "principal ID"),
            (self.principal_binding_hash, "principal binding hash"),
            (self.agent_instance_id, "Agent Instance ID"),
        ):
            if type(value) is not str or _HASH_RE.fullmatch(value) is None:
                raise ValueError(f"Hermes Fleet memory {label} is invalid")
        if self.principal_kind not in _PRINCIPAL_KINDS:
            raise ValueError("Hermes Fleet memory principal kind is invalid")
        if (
            isinstance(self.principal_generation, bool)
            or type(self.principal_generation) is not int
            or self.principal_generation < 1
        ):
            raise ValueError("Hermes Fleet memory principal generation is invalid")
        if (
            type(self.source_run) is not str
            or _MEMORY_IDENTIFIER_RE.fullmatch(self.source_run) is None
        ):
            raise ValueError("Hermes Fleet memory source run is invalid")
        if type(self.read_scopes) not in {tuple, list}:
            raise ValueError("Hermes Fleet memory read scopes are invalid")
        read_scopes = tuple(self.read_scopes)
        if (
            not 1 <= len(read_scopes) <= _MAX_MEMORY_READ_SCOPES
            or any(type(scope) is not HermesMemoryScopeRef for scope in read_scopes)
            or len(set(read_scopes)) != len(read_scopes)
        ):
            raise ValueError("Hermes Fleet memory read scopes are invalid")
        private_scope = HermesMemoryScopeRef("principal", self.principal_id)
        if private_scope not in read_scopes:
            raise ValueError("Hermes Fleet memory must include principal read scope")
        if (
            type(self.write_scope) is not HermesMemoryScopeRef
            or self.write_scope != private_scope
        ):
            raise ValueError("Hermes Fleet memory writes must remain principal-private")
        if self.retention_until_ms is not None and (
            isinstance(self.retention_until_ms, bool)
            or type(self.retention_until_ms) is not int
            or self.retention_until_ms < 1
        ):
            raise ValueError("Hermes Fleet memory retention deadline is invalid")
        object.__setattr__(self, "read_scopes", read_scopes)

    def to_request(self) -> dict[str, object]:
        return {
            "version": self.version,
            "principal": {
                "principal_id": self.principal_id,
                "kind": self.principal_kind,
                "generation": self.principal_generation,
                "binding_hash": self.principal_binding_hash,
            },
            "agent_instance_id": self.agent_instance_id,
            "source_run": self.source_run,
            "read_scopes": [scope.to_request() for scope in self.read_scopes],
            "write_scope": self.write_scope.to_request(),
            "retention_until_ms": self.retention_until_ms,
        }


@dataclass(frozen=True, slots=True)
class HermesRunResult:
    """Terminal text returned by one authenticated Hermes run."""

    run_id: str
    text: str


@dataclass(frozen=True, slots=True)
class HermesRunInspection:
    run_id: str
    status: str
    text: str | None


class HermesRunsClient:
    """Small synchronous adapter over Hermes's authenticated loopback Runs API."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        profile: str | None = None,
        poll_interval_seconds: float = 0.1,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self._endpoint = _loopback_endpoint(endpoint)
        if profile is not None and (
            type(profile) is not str or _PROFILE_RE.fullmatch(profile) is None
        ):
            raise ValueError("Hermes profile is invalid")
        self._profile_prefix = "" if profile is None else f"/p/{profile}"
        if (
            type(api_key) is not str
            or not api_key
            or "\r" in api_key
            or "\n" in api_key
        ):
            raise ValueError("Hermes API key must be a nonempty string")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, int | float)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll interval must be positive")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, int | float)
            or request_timeout_seconds <= 0
        ):
            raise ValueError("request timeout must be positive")
        self._api_key = api_key
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._request_timeout_seconds = float(request_timeout_seconds)

    def run(self, *, prompt: str, timeout_seconds: float) -> HermesRunResult:
        """Compatibility helper that starts and waits for one run."""
        deadline = time.monotonic() + float(timeout_seconds)
        run_id = self.start(prompt=prompt, timeout_seconds=timeout_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._cancel_at_deadline(run_id)
        return self.wait(run_id=run_id, timeout_seconds=remaining)

    def health(self, *, timeout_seconds: float | None = None) -> dict[str, object]:
        """Return bounded public Runs capability health without creating a run."""
        unavailable = {
            "api": "unavailable",
            "run_submission": False,
            "run_status": False,
            "run_stop": False,
            "run_finalize": False,
            "run_fleet_runtime": False,
            "run_fleet_memory_scope": False,
            "fleet_scoped_memory_write": False,
            "run_approval_budget": False,
            "run_tool_evidence": False,
            "run_command_evidence": False,
        }
        deadline = None
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int | float)
                or timeout_seconds <= 0
            ):
                return unavailable
            deadline = time.monotonic() + float(timeout_seconds)

        def remaining() -> float | None:
            if deadline is None:
                return None
            value = deadline - time.monotonic()
            if value <= 0:
                raise HermesRunError("Hermes health deadline has expired")
            return value

        try:
            health_status, _health = self._request_json(
                "GET", self._path("/health"), timeout_seconds=remaining()
            )
            capability_status, capabilities = self._request_json(
                "GET", self._path("/v1/capabilities"), timeout_seconds=remaining()
            )
        except HermesRunError:
            return unavailable
        if health_status != 200 or capability_status != 200:
            return unavailable
        features = capabilities.get("features")
        if (
            capabilities.get("object") != "hermes.api_server.capabilities"
            or type(features) is not dict
        ):
            return unavailable
        return {
            "api": "healthy",
            "run_submission": features.get("run_submission") is True,
            "run_status": features.get("run_status") is True,
            "run_stop": features.get("run_stop") is True,
            "run_finalize": features.get("run_finalize") is True,
            "run_fleet_runtime": features.get("run_fleet_runtime") is True,
            "run_fleet_memory_scope": features.get("run_fleet_memory_scope") is True,
            "fleet_scoped_memory_write": (
                features.get("fleet_scoped_memory_write") is True
            ),
            "run_approval_budget": features.get("run_approval_budget") is True,
            "run_tool_evidence": features.get("run_tool_evidence") is True,
            "run_command_evidence": features.get("run_command_evidence") is True,
        }

    def start(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
        approval_budget: int | None = None,
        fleet_runtime: HermesFleetRuntimeBinding | None = None,
        fleet_memory: HermesFleetMemoryBinding | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Create exactly one run and return its server-generated ID."""
        if type(prompt) is not str or not prompt.strip():
            raise ValueError("Hermes run prompt must be a nonempty string")
        if session_id is not None and (
            type(session_id) is not str
            or not session_id
            or len(session_id) > 512
            or any(ord(character) < 32 for character in session_id)
        ):
            raise ValueError("Hermes session ID must be bounded text")
        if approval_budget is not None and (
            type(approval_budget) is not int or not 1 <= approval_budget <= 32
        ):
            raise ValueError("Hermes approval budget must be between 1 and 32")
        if fleet_memory is not None and fleet_runtime is None:
            raise ValueError("Hermes Fleet memory requires a Fleet runtime binding")
        features: dict[str, object] | None = None
        if fleet_runtime is not None:
            if type(fleet_runtime) is not HermesFleetRuntimeBinding:
                raise ValueError("Hermes Fleet runtime binding is invalid")
            features = self.health(timeout_seconds=timeout_seconds)
            if features.get("run_fleet_runtime") is not True:
                raise HermesRunError("Hermes does not advertise run_fleet_runtime")
        if fleet_memory is not None:
            if type(fleet_memory) is not HermesFleetMemoryBinding:
                raise ValueError("Hermes Fleet memory binding is invalid")
            if features is None:
                features = self.health(timeout_seconds=timeout_seconds)
            if features.get("run_fleet_memory_scope") is not True:
                raise HermesRunError("Hermes does not advertise run_fleet_memory_scope")
        request = {"input": prompt}
        if session_id is not None:
            request["session_id"] = session_id
        if approval_budget is not None:
            request["approval_budget"] = approval_budget
        if fleet_runtime is not None:
            request["fleet_runtime"] = fleet_runtime.to_request()
        if fleet_memory is not None:
            request["fleet_memory"] = fleet_memory.to_request()
        try:
            status_code, document = self._request_json(
                "POST",
                self._path("/v1/runs"),
                request,
                timeout_seconds=timeout_seconds,
            )
        except HermesRunError:
            raise HermesRunSubmissionUnknown(
                "Hermes run submission outcome is unknown"
            ) from None
        run_id = document.get("run_id")
        if status_code != 202 or type(run_id) is not str or not run_id:
            raise HermesRunError("Hermes did not accept the Fleet run")
        return run_id

    def write_scoped_memory(
        self,
        *,
        fleet_memory: HermesFleetMemoryBinding,
        target: str = "memory",
        action: str | None = None,
        content: str | None = None,
        old_text: str | None = None,
        operations: list[dict[str, object]] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        """Persist one explicitly Fleet-authorized mutation via Hermes native memory."""
        if type(fleet_memory) is not HermesFleetMemoryBinding:
            raise ValueError("Hermes Fleet memory binding is invalid")
        if target not in {"memory", "user"}:
            raise ValueError("Hermes Fleet memory target is invalid")
        if operations is not None:
            if (
                type(operations) is not list
                or action is not None
                or content is not None
                or old_text is not None
            ):
                raise ValueError("Hermes Fleet memory mutation shape is invalid")
            if any(type(operation) is not dict for operation in operations):
                raise ValueError("Hermes Fleet memory operations are invalid")
        else:
            if action not in {"add", "replace", "remove"}:
                raise ValueError("Hermes Fleet memory action is invalid")
            if action == "add" and type(content) is not str:
                raise ValueError("Hermes Fleet memory add content is invalid")
            if action == "replace" and (
                type(old_text) is not str or type(content) is not str
            ):
                raise ValueError("Hermes Fleet memory replacement is invalid")
            if action == "remove" and type(old_text) is not str:
                raise ValueError("Hermes Fleet memory removal is invalid")

        features = self.health(timeout_seconds=timeout_seconds)
        if features.get("fleet_scoped_memory_write") is not True:
            raise HermesRunError("Hermes does not advertise fleet_scoped_memory_write")

        request: dict[str, object] = {
            "fleet_memory": fleet_memory.to_request(),
            "target": target,
        }
        if operations is not None:
            request["operations"] = operations
        else:
            request["action"] = action
            if content is not None:
                request["content"] = content
            if old_text is not None:
                request["old_text"] = old_text

        status_code, document = self._request_json(
            "POST",
            self._path("/v1/fleet/memory"),
            request,
            timeout_seconds=timeout_seconds,
        )
        result = document.get("result")
        if (
            status_code != 200
            or document.get("object") != "hermes.api_server.fleet_memory_write"
            or type(result) is not dict
            or result.get("success") is not True
        ):
            raise HermesRunError("Hermes rejected the Fleet scoped memory write")
        return result

    def wait(
        self,
        *,
        run_id: str,
        timeout_seconds: float,
        approval_mode: str | None = None,
        approval_budget: int | None = None,
    ) -> HermesRunResult:
        """Poll one known run to terminal text without creating another run."""
        if type(run_id) is not str or not run_id:
            raise ValueError("Hermes run ID must be a nonempty string")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or timeout_seconds <= 0
        ):
            raise ValueError("Hermes run timeout must be positive")
        if approval_mode not in {None, "once"}:
            raise ValueError("Hermes approval mode is invalid")
        if approval_budget is not None and (
            type(approval_budget) is not int or not 1 <= approval_budget <= 32
        ):
            raise ValueError("Hermes approval budget must be between 1 and 32")
        if approval_budget is not None and approval_mode != "once":
            raise ValueError("Hermes approval budget requires once approval mode")

        approvals_granted = 0
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._cancel_at_deadline(run_id)

            status_code, document = self._request_json(
                "GET",
                self._path(f"/v1/runs/{run_id}"),
                timeout_seconds=remaining,
            )
            if status_code == 404:
                raise HermesRunIndeterminate("Hermes run status is indeterminate")
            if status_code != 200:
                raise HermesRunError("Hermes run status is unavailable")
            state = document.get("status")
            if state == "completed":
                output = document.get("output")
                if type(output) is not str:
                    raise HermesRunError("Hermes completed without terminal text")
                return HermesRunResult(run_id=run_id, text=output)
            if state == "waiting_for_approval":
                if approval_mode == "once":
                    if (
                        approval_budget is not None
                        and approvals_granted >= approval_budget
                    ):
                        self.stop(run_id, timeout_seconds=min(0.25, remaining))
                        raise HermesRunError("Hermes run exceeded approval budget")
                    approval_status, _ = self._request_json(
                        "POST",
                        self._path(f"/v1/runs/{run_id}/approval"),
                        {"choice": "once"},
                        timeout_seconds=remaining,
                    )
                    if approval_status == 200:
                        approvals_granted += 1
                        continue
                    self.stop(run_id, timeout_seconds=min(0.25, remaining))
                    raise HermesRunError("Hermes run approval failed")
                self.stop(run_id, timeout_seconds=min(0.25, remaining))
                raise HermesRunError("Hermes run requires approval")
            if state == "failed":
                raise HermesRunError("Hermes run failed")
            if state == "cancelled":
                raise HermesRunError("Hermes run was cancelled")
            if state not in _ACTIVE_STATES:
                raise HermesRunError("Hermes returned an unsupported run status")
            time.sleep(min(self._poll_interval_seconds, remaining))

    def status(self, run_id: str) -> str:
        """Return a bounded exact-run lifecycle classification without mutation."""
        if type(run_id) is not str or not run_id:
            raise ValueError("Hermes run ID must be a nonempty string")
        status_code, document = self._request_json(
            "GET", self._path(f"/v1/runs/{run_id}")
        )
        if status_code == 404:
            return "missing"
        if status_code != 200:
            raise HermesRunError("Hermes run status is unavailable")
        state = document.get("status")
        if state in _ACTIVE_STATES or state == "waiting_for_approval":
            return "running"
        if state in {"completed", "failed", "cancelled"}:
            return "terminal"
        raise HermesRunError("Hermes returned an unsupported run status")

    def inspect(self, run_id: str) -> HermesRunInspection:
        if type(run_id) is not str or not run_id:
            raise ValueError("Hermes run ID must be a nonempty string")
        status_code, document = self._request_json(
            "GET", self._path(f"/v1/runs/{run_id}")
        )
        if status_code == 404:
            return HermesRunInspection(run_id=run_id, status="missing", text=None)
        if status_code != 200 or document.get("run_id") != run_id:
            raise HermesRunError("Hermes run inspection is unavailable")
        state = document.get("status")
        if state in _ACTIVE_STATES or state == "waiting_for_approval":
            return HermesRunInspection(run_id=run_id, status="running", text=None)
        if state == "completed":
            text = document.get("output")
            if type(text) is not str:
                raise HermesRunError("Hermes completed without terminal text")
            return HermesRunInspection(run_id=run_id, status="completed", text=text)
        if state in {"failed", "cancelled"}:
            return HermesRunInspection(run_id=run_id, status=state, text=None)
        raise HermesRunError("Hermes returned an unsupported run status")

    def finalize(self, run_id: str, *, timeout_seconds: float) -> dict[str, Any]:
        """Require Hermes to prove profile-owned runtime state is quiescent.

        The endpoint is idempotent.  Short-lived 409 responses are retryable
        because terminal status can become visible a few milliseconds before
        the API task finishes its own in-process cleanup.
        """
        if type(run_id) is not str or not run_id:
            raise ValueError("Hermes run ID must be a nonempty string")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or timeout_seconds <= 0
        ):
            raise ValueError("Hermes finalization timeout must be positive")

        deadline = time.monotonic() + float(timeout_seconds)
        retryable_codes = {
            "run_finalization_pending",
            "run_profile_busy",
            "run_not_terminal",
        }
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HermesRunIndeterminate("Hermes run quiescence is indeterminate")
            status_code, document = self._request_json(
                "POST",
                self._path(f"/v1/runs/{run_id}/finalize"),
                timeout_seconds=remaining,
            )
            if status_code == 200:
                if (
                    document.get("run_id") != run_id
                    or document.get("quiescent") is not True
                    or document.get("status")
                    not in {"completed", "failed", "cancelled"}
                ):
                    raise HermesRunError("Hermes finalization response is invalid")
                return document
            if status_code == 404:
                raise HermesRunIndeterminate("Hermes run finalization is unavailable")
            error = document.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if status_code == 409 and code in retryable_codes:
                time.sleep(min(self._poll_interval_seconds, remaining))
                continue
            raise HermesRunError("Hermes run finalization failed")

    def stop(self, run_id: str, *, timeout_seconds: float | None = None) -> None:
        """Request and confirm cooperative stop for one exact known run."""
        if type(run_id) is not str or not run_id:
            raise ValueError("Hermes run ID must be a nonempty string")
        status_code, document = self._request_json(
            "POST",
            self._path(f"/v1/runs/{run_id}/stop"),
            timeout_seconds=timeout_seconds,
        )
        if (
            status_code != 200
            or document.get("run_id") != run_id
            or document.get("status") not in {"stopping", "cancelled"}
        ):
            raise HermesRunError("Hermes run cancellation was not confirmed")

    def _cancel_at_deadline(self, run_id: str) -> None:
        """Confirm exact-run cancellation outside the expired execution budget."""
        try:
            self.stop(run_id, timeout_seconds=0.25)
        except HermesRunError:
            raise HermesRunIndeterminate(
                "Hermes deadline cancellation is indeterminate"
            ) from None
        raise HermesRunDeadlineExceeded("Hermes run exceeded Fleet deadline")

    def _request_json(
        self,
        method: str,
        path: str,
        document: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any]]:
        request_timeout = self._request_timeout_seconds
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int | float)
                or timeout_seconds <= 0
            ):
                raise HermesRunError("Hermes request deadline has expired")
            request_timeout = min(request_timeout, float(timeout_seconds))
        payload = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        if document is not None:
            payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self._endpoint}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        error_message: str | None = None
        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                status = response.status
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, urllib.error.URLError):
            error_message = "Hermes Runs API is unavailable"
            status = 0
            raw = b""
        if error_message is not None:
            raise HermesRunError(error_message)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise HermesRunError("Hermes Runs API response is too large")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            decoded = None
        if type(decoded) is not dict:
            raise HermesRunError("Hermes Runs API returned an invalid response")
        return status, decoded

    def _path(self, path: str) -> str:
        return self._profile_prefix + path


def _loopback_endpoint(endpoint: str) -> str:
    if type(endpoint) is not str:
        raise ValueError("Hermes endpoint must be loopback HTTP")
    parsed = urlsplit(endpoint)
    host = parsed.hostname
    loopback = host == "localhost"
    if host and not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Hermes endpoint must be loopback HTTP without credentials")
    return endpoint.rstrip("/")
