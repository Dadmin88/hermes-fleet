"""Strict Python client for destination-local execution control."""

from __future__ import annotations

import json
import re
import socket
import struct
from pathlib import Path
from typing import Any

from ._paths import is_concrete_path

_SCHEMA = "fleet.execution-control.v1"
_MAX_FRAME_BYTES = 2_097_152
_U64_MAX = (1 << 64) - 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PHASE_KINDS = frozenset(
    {
        "reserved",
        "prepared",
        "running",
        "completed",
        "failed",
        "cancelled",
        "indeterminate",
        "cleanup_pending",
        "cleaned",
    }
)
_ADMISSION_STATUSES = frozenset(
    {
        "admitted",
        "invalid_request",
        "invalid_context",
        "expired",
        "stale_target",
        "not_managed",
        "binding_unavailable",
        "policy_denied",
        "readiness_stale",
        "not_ready",
        "no_capacity",
        "capabilities_changed",
    }
)


class ExecutionControlClient:
    def __init__(self, *, socket_path: Path, timeout_seconds: float = 2.0) -> None:
        if not is_concrete_path(socket_path) or not socket_path.is_absolute():
            raise ValueError("execution-control socket must be an absolute Path")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError(
                "execution-control timeout must be between 0 and 30 seconds"
            )
        self._socket_path = socket_path
        self._timeout_seconds = float(timeout_seconds)

    def reserve_admit(
        self,
        instance: dict[str, Any],
        *,
        authorization: dict[str, Any],
        current_policy_digest: str,
        current_capabilities_hash: str,
        deadline_ms: int,
    ) -> dict[str, Any]:
        normalized = _instance(instance)
        proof = _authorization_proof(authorization)
        _content_hash(current_policy_digest)
        _content_hash(current_capabilities_hash)
        _u64(deadline_ms, "execution deadline")
        if proof["recipe_hash"] != normalized["recipe_hash"]:
            raise ValueError("authorization recipe hash does not match execution")
        if proof["deadline_ms"] != deadline_ms:
            raise ValueError("authorization deadline does not match execution")
        result = self._request(
            "reserve_admit",
            {
                "instance": normalized,
                "operation": "fleet.hermes.run",
                "authorization": proof,
                "current_policy_digest": current_policy_digest,
                "current_capabilities_hash": current_capabilities_hash,
                "deadline_ms": deadline_ms,
            },
        )
        if type(result) is not dict or set(result) not in (
            {"decision"},
            {"created", "instance", "decision"},
        ):
            raise RuntimeError("Fleet returned an invalid admission result")
        decision = _admission_decision(
            result["decision"], expected_instance=normalized, deadline_ms=deadline_ms
        )
        if decision["status"] == "admitted":
            if set(result) != {"created", "instance", "decision"}:
                raise RuntimeError("Fleet omitted an admitted execution instance")
            if type(result["created"]) is not bool:
                raise RuntimeError("Fleet returned an invalid reservation outcome")
            returned_instance = _instance(result["instance"])
            if not _same_execution_request(returned_instance, normalized):
                raise RuntimeError(
                    "Fleet returned an invalid admitted execution instance"
                )
            result = {
                "created": result["created"],
                "instance": returned_instance,
                "decision": decision,
            }
        elif set(result) != {"decision"}:
            raise RuntimeError("Fleet returned state for a denied admission")
        return result

    def get(self, instance_id: str) -> dict[str, Any] | None:
        _identifier(instance_id, "execution instance ID")
        result = self._request("get", {"instance_id": instance_id})
        if type(result) is not dict or set(result) != {"instance"}:
            raise RuntimeError("Fleet returned an invalid execution read")
        return None if result["instance"] is None else _instance(result["instance"])

    def transition(
        self,
        instance_id: str,
        *,
        expected_generation: int,
        phase: dict[str, Any],
    ) -> dict[str, Any]:
        _identifier(instance_id, "execution instance ID")
        _u64(expected_generation, "execution generation")
        normalized_phase = _phase(phase)
        result = self._request(
            "transition",
            {
                "instance_id": instance_id,
                "expected_generation": expected_generation,
                "phase": normalized_phase,
            },
        )
        if type(result) is not dict or set(result) != {"instance"}:
            raise RuntimeError("Fleet returned an invalid execution transition")
        return _instance(result["instance"])

    def _request(self, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(
            {"schema": _SCHEMA, "kind": kind, **fields},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not 1 <= len(encoded) <= _MAX_FRAME_BYTES:
            raise ValueError("execution-control request exceeds its frame bound")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._timeout_seconds)
            connection.connect(str(self._socket_path))
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
            connection.shutdown(socket.SHUT_WR)
            length = struct.unpack("!I", _recv_exact(connection, 4))[0]
            if not 1 <= length <= _MAX_FRAME_BYTES:
                raise RuntimeError("Fleet returned an invalid execution-control frame")
            payload = _recv_exact(connection, length)
            if connection.recv(1):
                raise RuntimeError("Fleet returned trailing execution-control bytes")
        try:
            document = json.loads(payload, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, UnicodeError, ValueError) as error:
            raise RuntimeError(
                "Fleet returned malformed execution-control JSON"
            ) from error
        if (
            type(document) is not dict
            or set(document) != {"schema", "kind", "ok", "result"}
            or document["schema"] != _SCHEMA
            or document["kind"] != kind
            or document["ok"] is not True
            or type(document["result"]) is not dict
        ):
            raise RuntimeError(
                "Fleet rejected or malformed the execution-control request"
            )
        return document["result"]


def _authorization_proof(value: object) -> dict[str, Any]:
    fields = {
        "authenticated_sender",
        "requester",
        "operation",
        "recipe_hash",
        "policy_digest",
        "deadline_ms",
        "secret_refs_digest",
    }
    if type(value) is not dict or set(value) != fields:
        raise ValueError("execution authorization proof is invalid")
    _identifier(value["authenticated_sender"], "authenticated sender")
    _identifier(value["requester"], "execution requester")
    if value["operation"] != "fleet.hermes.run":
        raise ValueError("execution authorization operation is invalid")
    _content_hash(value["recipe_hash"])
    _content_hash(value["policy_digest"])
    _content_hash(value["secret_refs_digest"])
    _u64(value["deadline_ms"], "execution authorization deadline")
    return value


def _instance(value: object) -> dict[str, Any]:
    fields = {
        "instance_id",
        "idempotency_key",
        "recipe_hash",
        "capabilities_hash",
        "target",
        "generation",
        "phase",
        "created_at_ms",
        "updated_at_ms",
    }
    if type(value) is not dict or set(value) != fields:
        raise RuntimeError("Fleet execution instance is invalid")
    _identifier(value["instance_id"], "execution instance ID")
    _identifier(value["idempotency_key"], "execution idempotency key")
    _content_hash(value["recipe_hash"])
    _content_hash(value["capabilities_hash"])
    if type(value["target"]) is not dict or set(value["target"]) != {
        "source",
        "network_id",
        "device_id",
        "binding_generation",
        "admission_generation",
    }:
        raise RuntimeError("Fleet execution target is invalid")
    for field in ("source", "network_id", "device_id"):
        _identifier(value["target"][field], f"execution target {field}")
    for field in ("binding_generation", "admission_generation"):
        _u64(value["target"][field], f"execution target {field}")
    generation = _u64(value["generation"], "execution generation")
    created = _u64(value["created_at_ms"], "execution creation time")
    updated = _u64(value["updated_at_ms"], "execution update time")
    if updated < created or generation < 1:
        raise RuntimeError("Fleet execution chronology is invalid")
    _phase(value["phase"])
    return value


def _phase(value: object) -> dict[str, Any]:
    if type(value) is not dict or value.get("kind") not in _PHASE_KINDS:
        raise ValueError("execution phase is invalid")
    kind = value["kind"]
    fields = {
        "reserved": {"kind"},
        "prepared": {"kind", "backend_kind", "realization_id"},
        "running": {"kind", "backend_kind", "realization_id", "keryx_task_id"},
        "completed": {"kind", "backend_kind", "realization_id", "keryx_task_id"},
        "failed": {"kind", "backend_kind", "realization_id", "keryx_task_id"},
        "cancelled": {"kind", "backend_kind", "realization_id", "keryx_task_id"},
        "indeterminate": {
            "kind",
            "backend_kind",
            "realization_id",
            "keryx_task_id",
            "reason",
        },
        "cleanup_pending": {
            "kind",
            "backend_kind",
            "realization_id",
            "reason",
        },
        "cleaned": {"kind"},
    }[kind]
    if (
        kind
        in {
            "running",
            "completed",
            "failed",
            "cancelled",
            "indeterminate",
            "cleanup_pending",
        }
        and "hermes_run_id" in value
    ):
        fields = fields | {"hermes_run_id"}
    if kind == "cleanup_pending" and "keryx_task_id" in value:
        fields = fields | {"keryx_task_id"}
    if set(value) != fields:
        raise ValueError("execution phase is invalid")
    for field in (
        "backend_kind",
        "realization_id",
        "keryx_task_id",
        "hermes_run_id",
    ):
        if field in value and value[field] is not None:
            _identifier(value[field], f"execution phase {field}")
    if kind in {
        "prepared",
        "running",
        "completed",
        "failed",
        "cancelled",
        "cleanup_pending",
    }:
        if "/" not in value["backend_kind"]:
            raise ValueError("execution phase backend is invalid")
    if kind == "indeterminate":
        if (value["backend_kind"] is None) != (value["realization_id"] is None):
            raise ValueError("execution phase provenance is invalid")
        if value["keryx_task_id"] is not None and value["backend_kind"] is None:
            raise ValueError("execution phase provenance is invalid")
        if value["backend_kind"] is not None and "/" not in value["backend_kind"]:
            raise ValueError("execution phase backend is invalid")
    if "reason" in value and (
        type(value["reason"]) is not str
        or not value["reason"]
        or len(value["reason"]) > 512
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value["reason"]
        )
    ):
        raise ValueError("execution phase reason is invalid")
    return value


def _admission_decision(
    value: object, *, expected_instance: dict[str, Any], deadline_ms: int
) -> dict[str, Any]:
    if (
        type(value) is not dict
        or type(value.get("status")) is not str
        or value["status"] not in _ADMISSION_STATUSES
    ):
        raise RuntimeError("Fleet returned an invalid admission decision")
    if value["status"] != "admitted":
        if set(value) != {"status"}:
            raise RuntimeError("Fleet returned an invalid admission decision")
        return value
    if set(value) != {
        "status",
        "instance_id",
        "target",
        "recipe_hash",
        "capabilities_hash",
        "operation",
        "evaluated_at_ms",
    }:
        raise RuntimeError("Fleet returned an invalid admission decision")
    try:
        _identifier(value["instance_id"], "admission instance ID")
        _content_hash(value["recipe_hash"])
        _content_hash(value["capabilities_hash"])
        _u64(value["evaluated_at_ms"], "admission evaluation time")
        if value["operation"] != "fleet.hermes.run":
            raise ValueError("admission operation is invalid")
        target = value["target"]
        if type(target) is not dict or set(target) != {
            "source",
            "network_id",
            "device_id",
            "binding_generation",
            "admission_generation",
        }:
            raise ValueError("admission target is invalid")
        for field in ("source", "network_id", "device_id"):
            _identifier(target[field], f"admission target {field}")
        for field in ("binding_generation", "admission_generation"):
            _u64(target[field], f"admission target {field}")
        if (
            value["instance_id"] != expected_instance["instance_id"]
            or target != expected_instance["target"]
            or value["recipe_hash"] != expected_instance["recipe_hash"]
            or value["capabilities_hash"] != expected_instance["capabilities_hash"]
            or value["evaluated_at_ms"] > deadline_ms
        ):
            raise ValueError("admission decision does not match the request")
    except ValueError as error:
        raise RuntimeError("Fleet returned an invalid admission decision") from error
    return value


def _same_execution_request(returned: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(
        returned[field] == expected[field]
        for field in (
            "instance_id",
            "idempotency_key",
            "recipe_hash",
            "capabilities_hash",
            "target",
        )
    )


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _content_hash(value: object) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise ValueError("execution content hash is invalid")
    return value


def _u64(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _U64_MAX:
        raise ValueError(f"{label} is invalid")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate execution-control JSON member")
        result[key] = value
    return result


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = connection.recv(length - len(chunks))
        if not chunk:
            raise RuntimeError("Fleet closed the execution-control connection")
        chunks.extend(chunk)
    return bytes(chunks)
