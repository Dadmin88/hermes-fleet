"""Strict versioned payloads for the three Fleet Keryx operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from .models import FleetDefaults, NodeConfig, _require_exact_type

ENVELOPE_VERSION = 1
OPERATIONS = frozenset({"fleet.health", "fleet.inventory", "fleet.hermes.run"})


class _DuplicateJsonObjectKey(ValueError):
    """Private decoder signal; it intentionally retains no untrusted key text."""


class _NonStandardJsonValue(ValueError):
    """Private decoder signal for Python-only non-finite numeric tokens."""


class _InvalidJsonUnicode(ValueError):
    """Private decoder signal for strings containing lone surrogate code points."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one decoded JSON object while rejecting repeated members."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKey
        result[key] = value
    return result


def _reject_nonstandard_json_value(value: str) -> None:
    del value
    raise _NonStandardJsonValue


def _contains_surrogate_code_point(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _validate_decoded_json_strings(value: object) -> None:
    """Reject lone surrogates without adding a second recursive parser walk."""
    pending = [value]
    while pending:
        current = pending.pop()
        if type(current) is str:
            if _contains_surrogate_code_point(current):
                raise _InvalidJsonUnicode
        elif type(current) is dict:
            mapping = cast(dict[object, object], current)
            pending.extend(mapping.keys())
            pending.extend(mapping.values())
        elif type(current) is list:
            pending.extend(cast(list[object], current))


def _decode_json_payload(payload: str) -> object:
    """Decode strict interoperable JSON behind stable Fleet-owned errors."""
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonstandard_json_value,
        )
        _validate_decoded_json_strings(document)
        return document
    except _DuplicateJsonObjectKey:
        error_message = "payload must not contain duplicate JSON object keys"
    except _NonStandardJsonValue:
        error_message = "payload must contain only standard JSON values"
    except _InvalidJsonUnicode:
        error_message = "payload must be valid UTF-8"
    except (ValueError, RecursionError):
        error_message = "payload must be a JSON object"
    raise ValueError(error_message)


def _validate_json_value(value: object, ancestors: set[int]) -> None:
    """Reject non-primitive JSON values before their Python hooks can execute."""
    if value is None or type(value) in (bool, int, float):
        return
    if type(value) is str:
        if _contains_surrogate_code_point(value):
            raise ValueError("envelope input must be JSON serializable")
        return
    if type(value) not in (dict, list, tuple):
        raise ValueError("envelope input must be JSON serializable")
    identity = id(value)
    if identity in ancestors:
        raise ValueError("envelope input must be JSON serializable")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            mapping = cast(dict[object, object], value)
            if any(
                type(key) is not str or _contains_surrogate_code_point(key)
                for key in mapping
            ):
                raise ValueError("envelope input must be JSON serializable")
            nested_values = mapping.values()
        else:
            nested_values = cast(list[object] | tuple[object, ...], value)
        for nested in nested_values:
            _validate_json_value(nested, ancestors)
    finally:
        ancestors.remove(identity)


@dataclass(frozen=True, slots=True)
class FleetEnvelope:
    """A validated Fleet payload; it performs no transport or Keryx SDK work."""

    version: int
    operation: str
    target_name: str
    target_peer_id: str
    input: dict[str, Any]
    deadline_seconds: int

    def to_json(self) -> str:
        """Serialize a stable envelope representation for a future Keryx task part."""
        if type(self.version) is not int:
            raise ValueError("version must be an integer")
        if type(self.operation) is not str:
            raise ValueError("operation must be a string")
        if type(self.target_name) is not str:
            raise ValueError("target_name must be a string")
        if type(self.target_peer_id) is not str:
            raise ValueError("target_peer_id must be a string")
        if type(self.deadline_seconds) is not int:
            raise ValueError("deadline_seconds must be an integer")
        if type(self.input) is not dict:
            raise ValueError("envelope input must be a JSON object")
        try:
            document = {
                "version": self.version,
                "operation": self.operation,
                "target": {
                    "name": self.target_name,
                    "peer_id": self.target_peer_id,
                },
                "input": self.input,
                "limits": {"deadline_seconds": self.deadline_seconds},
            }
            _validate_json_value(document, set())
            return json.dumps(
                document,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError, RuntimeError, RecursionError):
            pass
        raise ValueError("envelope input must be JSON serializable")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _positive_int(value: object, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"{label} must be a bounded positive integer")
    return value


def _export_paths(value: object, maximum: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("export_paths must be a bounded array")
    paths: list[str] = []
    for path in value:
        if not isinstance(path, str) or not path or len(path) > 256:
            raise ValueError("export_paths contains an invalid path")
        if path.startswith("/") or "\\" in path or any(ord(char) < 32 for char in path):
            raise ValueError("export_paths contains an unsafe path")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("export_paths contains an unsafe path")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ValueError("export_paths must not contain duplicates")
    return tuple(paths)


def parse_envelope(
    payload: str, *, target: NodeConfig, defaults: FleetDefaults
) -> FleetEnvelope:
    """Parse and validate a bounded Fleet envelope before any later dispatch layer."""
    target = _require_exact_type(target, NodeConfig, "target must be a NodeConfig")
    defaults = _require_exact_type(
        defaults, FleetDefaults, "defaults must be FleetDefaults"
    )
    if type(payload) is not str:
        raise ValueError("payload must be a string")
    try:
        payload_bytes = payload.encode("utf-8")
    except UnicodeEncodeError:
        payload_bytes = None
    if payload_bytes is None:
        raise ValueError("payload must be valid UTF-8")
    if len(payload_bytes) > defaults.max_payload_bytes:
        raise ValueError("payload exceeds the configured size limit")
    document = _decode_json_payload(payload)
    document = _object(document, "payload")
    if set(document) != {"version", "operation", "target", "input", "limits"}:
        raise ValueError("payload has an invalid envelope shape")
    version = document["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != ENVELOPE_VERSION
    ):
        raise ValueError("unsupported envelope version")
    operation = document["operation"]
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise ValueError("unsupported operation")

    target_data = _object(document["target"], "target")
    if set(target_data) != {"name", "peer_id"} or target_data != {
        "name": target.name,
        "peer_id": target.peer_id,
    }:
        raise ValueError("envelope target does not match configured target")
    limits = _object(document["limits"], "limits")
    if set(limits) != {"deadline_seconds"}:
        raise ValueError("limits has an invalid shape")
    deadline_seconds = _positive_int(
        limits["deadline_seconds"], "deadline_seconds", defaults.max_deadline_seconds
    )
    input_data = _object(document["input"], "input")
    if operation in {"fleet.health", "fleet.inventory"}:
        if input_data:
            raise ValueError("health and inventory input must be empty")
        normalized_input: dict[str, Any] = {}
    else:
        if set(input_data).difference({"prompt", "export_paths"}):
            raise ValueError("run input has unknown keys")
        prompt = input_data.get("prompt")
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > defaults.max_prompt_chars
        ):
            raise ValueError("prompt exceeds the configured size limit")
        normalized_input = {
            "prompt": prompt,
            "export_paths": _export_paths(
                input_data.get("export_paths"), defaults.max_export_paths
            ),
        }
    return FleetEnvelope(
        version=ENVELOPE_VERSION,
        operation=operation,
        target_name=target.name,
        target_peer_id=target.peer_id,
        input=normalized_input,
        deadline_seconds=deadline_seconds,
    )
