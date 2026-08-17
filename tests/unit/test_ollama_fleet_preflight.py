from __future__ import annotations

import json

import pytest

from scripts.ollama_fleet_preflight import PreflightError, run_preflight

MODEL = "qwen-test:2b"
BASE = "http://127.0.0.1:11434/v1"


def _completion(command: str = "echo HERMES_FLEET_MODEL_PREFLIGHT") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": json.dumps({"command": command}),
                            },
                        }
                    ]
                }
            }
        ]
    }


def _requester(
    *,
    tags: dict | None = None,
    completion: dict | None = None,
    processes: dict | None = None,
):
    tags = tags or {"models": [{"name": MODEL}]}
    completion = completion or _completion()
    processes = processes or {
        "models": [
            {
                "name": MODEL,
                "size_vram": 0,
                "context_length": 8192,
            }
        ]
    }
    calls: list[tuple[str, str, object]] = []

    def request(method, url, document=None):
        calls.append((method, url, document))
        if url.endswith("/api/tags"):
            return tags
        if url.endswith("/chat/completions"):
            return completion
        if url.endswith("/api/ps"):
            return processes
        raise AssertionError(url)

    return request, calls


def test_preflight_accepts_structured_cpu_only_8k_model() -> None:
    request, calls = _requester()
    ticks = iter((10.0, 10.125))

    report = run_preflight(
        model=MODEL,
        base_url=BASE,
        request_json=request,
        monotonic=lambda: next(ticks),
    )

    assert report == {
        "schema": "fleet.ollama-preflight.v1",
        "status": "ok",
        "model": MODEL,
        "cpu_only": True,
        "min_context_length": 8192,
        "structured_tool_call": True,
        "elapsed_ms": 125,
    }
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1][1] == "http://127.0.0.1:11434/v1/chat/completions"
    assert calls[1][2]["model"] == MODEL


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:11434/v1",
        "http://provider.example/v1",
        "http://127.0.0.1:11434/api",
        "http://user:pass@127.0.0.1:11434/v1",
    ],
)
def test_preflight_rejects_nonlocal_or_nonollama_base(base_url: str) -> None:
    with pytest.raises(PreflightError, match="loopback Ollama"):
        run_preflight(model=MODEL, base_url=base_url)


def test_preflight_requires_installed_model() -> None:
    request, _ = _requester(tags={"models": []})
    with pytest.raises(PreflightError, match="not installed"):
        run_preflight(model=MODEL, base_url=BASE, request_json=request)


@pytest.mark.parametrize(
    "completion",
    [
        {"choices": [{"message": {"content": "I refuse to call a tool"}}]},
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "other_tool",
                                    "arguments": "{}",
                                }
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "terminal",
                                    "arguments": "not-json",
                                }
                            }
                        ]
                    }
                }
            ]
        },
        _completion("echo WRONG_MARKER"),
    ],
)
def test_preflight_requires_structured_terminal_marker(completion: dict) -> None:
    request, _ = _requester(completion=completion)
    with pytest.raises(PreflightError):
        run_preflight(model=MODEL, base_url=BASE, request_json=request)


def test_preflight_rejects_gpu_loaded_model() -> None:
    request, _ = _requester(
        processes={
            "models": [{"name": MODEL, "size_vram": 1024, "context_length": 8192}]
        }
    )
    with pytest.raises(PreflightError, match="CPU-only"):
        run_preflight(model=MODEL, base_url=BASE, request_json=request)


def test_preflight_rejects_small_context_window() -> None:
    request, _ = _requester(
        processes={"models": [{"name": MODEL, "size_vram": 0, "context_length": 4096}]}
    )
    with pytest.raises(PreflightError, match="below 8192"):
        run_preflight(model=MODEL, base_url=BASE, request_json=request)
