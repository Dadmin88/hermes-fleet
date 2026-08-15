#!/usr/bin/env python3
"""Fail closed unless the configured local Ollama model can serve Fleet safely."""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_MAX_RESPONSE_BYTES = 1_048_576
_REQUEST_TIMEOUT_SECONDS = 60.0
_MIN_CONTEXT_LENGTH = 8192
_MARKER = "HERMES_FLEET_MODEL_PREFLIGHT"


class PreflightError(RuntimeError):
    """The local inference runtime does not satisfy Fleet's execution contract."""


def _configured_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PreflightError(f"{name} is required")
    return value


def _local_endpoints(base_url: str) -> tuple[str, str]:
    if type(base_url) is not str or not base_url or base_url != base_url.strip():
        raise PreflightError("Fleet execution base URL is invalid")
    parsed = urlsplit(base_url)
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError as error:
        raise PreflightError("Fleet execution base URL is invalid") from error
    loopback = host == "localhost"
    if host and not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/v1"
    ):
        raise PreflightError("Fleet execution base URL must be loopback Ollama /v1")
    api_base = base_url.rstrip("/")
    ollama_root = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
    return api_base, ollama_root


def _request_json(
    method: str,
    url: str,
    document: dict[str, Any] | None = None,
    *,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    payload = None
    headers = {"Accept": "application/json"}
    if document is not None:
        payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise PreflightError(
                    "Ollama preflight endpoint returned non-200 status"
                )
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise PreflightError("Ollama preflight endpoint is unavailable") from error
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise PreflightError("Ollama preflight response is too large")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise PreflightError("Ollama preflight returned invalid JSON") from error
    if type(decoded) is not dict:
        raise PreflightError("Ollama preflight returned an invalid document")
    return decoded


def _require_model(tags: dict[str, Any], model: str) -> None:
    models = tags.get("models")
    if type(models) is not list:
        raise PreflightError("Ollama model inventory is invalid")
    for item in models:
        if type(item) is dict and model in {item.get("name"), item.get("model")}:
            return
    raise PreflightError("Configured Fleet execution model is not installed")


def _require_structured_terminal_call(completion: dict[str, Any]) -> None:
    choices = completion.get("choices")
    if type(choices) is not list or not choices or type(choices[0]) is not dict:
        raise PreflightError("Local model returned no OpenAI completion choice")
    message = choices[0].get("message")
    if type(message) is not dict:
        raise PreflightError("Local model returned no assistant message")
    tool_calls = message.get("tool_calls")
    if type(tool_calls) is not list or not tool_calls:
        raise PreflightError("Local model did not produce a structured tool call")
    call = tool_calls[0]
    function = call.get("function") if type(call) is dict else None
    if type(function) is not dict or function.get("name") != "terminal":
        raise PreflightError("Local model did not select the required terminal tool")
    arguments = function.get("arguments")
    if type(arguments) is not str or len(arguments) > 16_384:
        raise PreflightError("Local model returned invalid terminal arguments")
    try:
        parsed = json.loads(arguments)
    except (ValueError, TypeError, RecursionError) as error:
        raise PreflightError(
            "Local model returned malformed terminal arguments"
        ) from error
    command = parsed.get("command") if type(parsed) is dict else None
    if type(command) is not str or _MARKER not in command:
        raise PreflightError(
            "Local model terminal call did not preserve the preflight marker"
        )


def _require_cpu_context(processes: dict[str, Any], model: str) -> None:
    models = processes.get("models")
    if type(models) is not list:
        raise PreflightError("Ollama process inventory is invalid")
    for item in models:
        if type(item) is not dict or model not in {item.get("name"), item.get("model")}:
            continue
        if item.get("size_vram") != 0:
            raise PreflightError("Fleet local model is not CPU-only")
        context_length = item.get("context_length")
        if type(context_length) is not int or context_length < _MIN_CONTEXT_LENGTH:
            raise PreflightError("Fleet local model context window is below 8192")
        return
    raise PreflightError("Configured Fleet model was not loaded by preflight")


def run_preflight(
    *,
    model: str,
    base_url: str,
    request_json: Callable[..., dict[str, Any]] = _request_json,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if (
        type(model) is not str
        or not model
        or model != model.strip()
        or len(model) > 256
    ):
        raise PreflightError("Fleet execution model is invalid")
    api_base, ollama_root = _local_endpoints(base_url)
    started = monotonic()
    tags = request_json("GET", f"{ollama_root}/api/tags")
    _require_model(tags, model)
    completion = request_json(
        "POST",
        f"{api_base}/chat/completions",
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a deterministic Fleet capability probe. "
                        "Call the terminal tool exactly once and do not answer "
                        "in prose."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Use terminal to run: echo {_MARKER}",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "description": "Run a shell command.",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "temperature": 0,
            "stream": False,
        },
    )
    _require_structured_terminal_call(completion)
    processes = request_json("GET", f"{ollama_root}/api/ps")
    _require_cpu_context(processes, model)
    elapsed_ms = max(0, int((monotonic() - started) * 1000))
    return {
        "schema": "fleet.ollama-preflight.v1",
        "status": "ok",
        "model": model,
        "cpu_only": True,
        "min_context_length": _MIN_CONTEXT_LENGTH,
        "structured_tool_call": True,
        "elapsed_ms": elapsed_ms,
    }


def main() -> int:
    try:
        report = run_preflight(
            model=_configured_value("FLEET_EXECUTION_MODEL"),
            base_url=_configured_value("FLEET_EXECUTION_BASE_URL"),
        )
    except PreflightError as error:
        print(f"Fleet Ollama preflight failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
