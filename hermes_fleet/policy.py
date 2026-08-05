"""Pure local policy checks; Phase 1 performs no remote dispatch."""

from __future__ import annotations

from .models import FleetDefaults, NodePolicy


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def enforce_request_policy(
    policy: NodePolicy,
    *,
    defaults: FleetDefaults,
    operation: str,
    deadline_seconds: object,
    payload_bytes: object,
    prompt_chars: object,
    export_path_count: object,
) -> None:
    """Raise when a requested operation or bound exceeds local allowlisted policy."""
    if not isinstance(operation, str) or operation not in policy.allowed_operations:
        raise ValueError(f"operation is not allowed: {operation}")
    deadline = _positive(deadline_seconds, "deadline_seconds")
    payload = _positive(payload_bytes, "payload_bytes")
    prompt = _nonnegative(prompt_chars, "prompt_chars")
    exports = _nonnegative(export_path_count, "export_path_count")
    if deadline > min(policy.max_deadline_seconds, defaults.max_deadline_seconds):
        raise ValueError("deadline_seconds exceeds policy")
    if payload > min(policy.max_payload_bytes, defaults.max_payload_bytes):
        raise ValueError("payload_bytes exceeds policy")
    if prompt > min(policy.max_prompt_chars, defaults.max_prompt_chars):
        raise ValueError("prompt_chars exceeds policy")
    if exports > min(policy.max_export_paths, defaults.max_export_paths):
        raise ValueError("export_path_count exceeds policy")
