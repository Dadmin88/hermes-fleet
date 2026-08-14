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


class HermesRunError(RuntimeError):
    """Stable Fleet-owned error for a local Hermes run failure."""


class HermesRunSubmissionUnknown(HermesRunError):
    """Run creation may have succeeded, so reposting would be unsafe."""


class HermesRunIndeterminate(HermesRunError):
    """A known run can no longer be observed safely."""


class HermesRunDeadlineExceeded(HermesRunError):
    """The exact bound run accepted cancellation at its Fleet deadline."""


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
        }

    def start(
        self,
        *,
        prompt: str,
        session_id: str | None = None,
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
        request = {"input": prompt}
        if session_id is not None:
            request["session_id"] = session_id
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

    def wait(
        self,
        *,
        run_id: str,
        timeout_seconds: float,
        approval_mode: str | None = None,
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
                    approval_status, _ = self._request_json(
                        "POST",
                        self._path(f"/v1/runs/{run_id}/approval"),
                        {"choice": "once"},
                        timeout_seconds=remaining,
                    )
                    if approval_status == 200:
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
