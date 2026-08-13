"""Bounded Desktop projection over Fleet's authoritative local control API."""

from __future__ import annotations

import json
import re
import socket
import struct
from pathlib import Path
from typing import Any

from ._paths import is_concrete_path
from .observation import normalize_readiness

_SCHEMA = "fleet.desktop.v1"
_MANAGED_SCHEMA = "fleet.managed-projection.v1"
_ALIAS_SCHEMA = "fleet.desktop-alias.v1"
_WORKFLOW_SCHEMA = "fleet.workflow.v1"
_MAX_FRAME_BYTES = 2_097_152
_MAX_RESPONSE_BYTES = 2_097_152
_MAX_NODES = 256
_U64_MAX = (1 << 64) - 1
_STABLE_ID = re.compile(r"^fleet-node-[0-9a-f]{64}$")
_OPERATIONS = frozenset({"fleet.health", "fleet.inventory", "fleet.message"})
_NODE_FIELDS = frozenset(
    {"stable_id", "identity", "naming", "managed", "readiness", "operations"}
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Desktop JSON member")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite Desktop JSON number")


class DesktopApiClient:
    """Read Fleet Desktop V1 state without reading Fleet's database directly."""

    def __init__(self, *, socket_path: Path, timeout_seconds: float = 2.0) -> None:
        if not is_concrete_path(socket_path) or not socket_path.is_absolute():
            raise ValueError("desktop socket must be an absolute Path")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError("desktop timeout must be between 0 and 30 seconds")
        self._socket_path = socket_path
        self._timeout_seconds = float(timeout_seconds)

    def overview(self) -> dict[str, Any]:
        """Return validated authoritative rows plus bounded presentation counts."""
        result = self._request(
            {"schema": _SCHEMA, "kind": "overview"},
            expected_schema=_SCHEMA,
            expected_kind="overview",
        )
        if type(result) is not dict or set(result) != {"nodes"}:
            raise RuntimeError("Fleet returned an invalid Desktop overview")
        raw_nodes = result["nodes"]
        if type(raw_nodes) is not list or len(raw_nodes) > _MAX_NODES:
            raise RuntimeError("Fleet returned an invalid Desktop node list")
        try:
            nodes = [_normalize_node(node) for node in raw_nodes]
        except ValueError as error:
            raise RuntimeError("Fleet returned an invalid Desktop node") from error
        stable_ids = [node["stable_id"] for node in nodes]
        if len(stable_ids) != len(set(stable_ids)):
            raise RuntimeError("Fleet returned duplicate Desktop node identities")
        active = [node for node in nodes if node["managed"]["active"]]
        return {
            "schema": _SCHEMA,
            "summary": {
                "managed": len(nodes),
                "active": len(active),
                "alive": sum(node["readiness"]["alive"] for node in nodes),
                "ready": sum(node["readiness"]["scheduler_ready"] for node in nodes),
                "not_ready": sum(
                    not node["readiness"]["scheduler_ready"] for node in active
                ),
            },
            "nodes": nodes,
        }

    def inspect_projection(
        self, *, source: str, network_id: str, device_id: str
    ) -> dict[str, Any]:
        """Inspect authoritative managed provenance through the owning control API."""
        result = self._request(
            {
                "schema": _MANAGED_SCHEMA,
                "kind": "inspect",
                "selector": _selector(source, network_id, device_id),
            },
            expected_schema=_MANAGED_SCHEMA,
            expected_kind="inspect",
        )
        if set(result) != {"generated", "effective"}:
            raise RuntimeError("Fleet returned an invalid managed projection")
        return result

    def set_alias(
        self,
        *,
        source: str,
        network_id: str,
        device_id: str,
        binding_generation: str,
        alias: str,
    ) -> str:
        """Set presentation-only alias state fenced by authoritative binding."""
        normalized_alias = _alias_text(alias)
        result = self._request(
            {
                "schema": _ALIAS_SCHEMA,
                "kind": "set_alias",
                "selector": _selector(source, network_id, device_id),
                "binding_generation": _generation(binding_generation),
                "alias": normalized_alias,
            },
            expected_schema=_ALIAS_SCHEMA,
            expected_kind="set_alias",
        )
        return _alias_outcome(result, {"created", "replaced", "unchanged"})

    def clear_alias(
        self,
        *,
        source: str,
        network_id: str,
        device_id: str,
        binding_generation: str,
    ) -> str:
        """Clear presentation-only alias state fenced by authoritative binding."""
        result = self._request(
            {
                "schema": _ALIAS_SCHEMA,
                "kind": "clear_alias",
                "selector": _selector(source, network_id, device_id),
                "binding_generation": _generation(binding_generation),
            },
            expected_schema=_ALIAS_SCHEMA,
            expected_kind="clear_alias",
        )
        return _alias_outcome(result, {"cleared", "already_clear"})

    def workflow_capabilities(self) -> dict[str, Any]:
        """Return the backend-owned Workflow surface without implying execution."""
        result = self._request(
            {"schema": _WORKFLOW_SCHEMA, "kind": "capabilities"},
            expected_schema=_WORKFLOW_SCHEMA,
            expected_kind="capabilities",
        )
        expected_kinds = [
            "capabilities",
            "create",
            "read",
            "read_version",
            "update",
            "list",
            "delete",
        ]
        if result != {"kinds": expected_kinds, "executionAvailable": False}:
            raise RuntimeError("Fleet returned invalid Workflow capabilities")
        return result

    def create_workflow(self, document: dict[str, Any]) -> dict[str, Any]:
        """Create immutable Workflow version 1 from an editor document."""
        result = self._request(
            {
                "schema": _WORKFLOW_SCHEMA,
                "kind": "create",
                "document": _workflow_document(document),
            },
            expected_schema=_WORKFLOW_SCHEMA,
            expected_kind="create",
        )
        return _workflow_write(result, {"created"})

    def read_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        result = self._request(
            {
                "schema": _WORKFLOW_SCHEMA,
                "kind": "read",
                "workflowId": _workflow_id(workflow_id),
            },
            expected_schema=_WORKFLOW_SCHEMA,
            expected_kind="read",
        )
        return _optional_workflow_revision(result)

    def read_workflow_version(
        self, workflow_id: str, *, version: int
    ) -> dict[str, Any] | None:
        result = self._request(
            {
                "schema": _WORKFLOW_SCHEMA,
                "kind": "read_version",
                "workflowId": _workflow_id(workflow_id),
                "version": _workflow_version(version),
            },
            expected_schema=_WORKFLOW_SCHEMA,
            expected_kind="read_version",
        )
        return _optional_workflow_revision(result)

    def update_workflow(
        self, document: dict[str, Any], *, expected_version: int
    ) -> dict[str, Any]:
        result = self._request(
            {
                "schema": _WORKFLOW_SCHEMA,
                "kind": "update",
                "expectedVersion": _workflow_version(expected_version),
                "document": _workflow_document(document),
            },
            expected_schema=_WORKFLOW_SCHEMA,
            expected_kind="update",
        )
        return _workflow_write(result, {"version_created", "unchanged"})

    def list_workflows(self) -> list[dict[str, Any]]:
        result = self._request(
            {"schema": _WORKFLOW_SCHEMA, "kind": "list"},
            expected_schema=_WORKFLOW_SCHEMA,
            expected_kind="list",
        )
        if type(result) is not dict or set(result) != {"workflows"}:
            raise RuntimeError("Fleet returned an invalid Workflow list")
        workflows = result["workflows"]
        if type(workflows) is not list or len(workflows) > _MAX_NODES:
            raise RuntimeError("Fleet returned an invalid Workflow list")
        normalized = [_workflow_summary(value) for value in workflows]
        ids = [value["workflowId"] for value in normalized]
        if ids != sorted(ids) or len(ids) != len(set(ids)):
            raise RuntimeError("Fleet returned an invalid Workflow list")
        return normalized

    def delete_workflow(self, workflow_id: str, *, expected_version: int) -> str:
        result = self._request(
            {
                "schema": _WORKFLOW_SCHEMA,
                "kind": "delete",
                "workflowId": _workflow_id(workflow_id),
                "expectedVersion": _workflow_version(expected_version),
            },
            expected_schema=_WORKFLOW_SCHEMA,
            expected_kind="delete",
        )
        if (
            type(result) is not dict
            or set(result) != {"outcome"}
            or result["outcome"] not in {"deleted", "already_deleted"}
        ):
            raise RuntimeError("Fleet returned an invalid Workflow delete outcome")
        return result["outcome"]

    def _request(
        self,
        payload: dict[str, Any],
        *,
        expected_schema: str,
        expected_kind: str,
    ) -> dict[str, Any]:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not 1 <= len(encoded) <= _MAX_FRAME_BYTES:
            raise ValueError("Desktop request exceeds the bounded Fleet frame")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._timeout_seconds)
            connection.connect(str(self._socket_path))
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
            connection.shutdown(socket.SHUT_WR)
            length = struct.unpack("!I", _recv_exact(connection, 4))[0]
            if not 1 <= length <= _MAX_RESPONSE_BYTES:
                raise RuntimeError("Fleet returned an invalid Desktop frame")
            try:
                document = json.loads(
                    _recv_exact(connection, length),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                RecursionError,
                ValueError,
            ) as error:
                raise RuntimeError("Fleet returned malformed Desktop JSON") from error
            if connection.recv(1):
                raise RuntimeError("Fleet returned trailing Desktop frame bytes")
        if (
            type(document) is not dict
            or set(document) != {"schema", "kind", "ok", "result"}
            or document["schema"] != expected_schema
            or document["kind"] != expected_kind
            or document["ok"] is not True
            or type(document["result"]) is not dict
        ):
            raise RuntimeError("Fleet rejected or malformed the Desktop request")
        return document["result"]


def _workflow_document(value: object) -> dict[str, Any]:
    if (
        type(value) is not dict
        or value.get("schema") != "fleet.workflow-editor.v1"
        or set(value) != {"schema", "id", "name", "nodes", "connections", "metadata"}
        or type(value.get("nodes")) is not list
        or type(value.get("connections")) is not list
        or len(value["nodes"]) > _MAX_NODES
        or len(value["connections"]) > _MAX_NODES
        or type(value.get("metadata")) is not dict
        or value["metadata"] != {"executionAvailable": False}
    ):
        raise ValueError("Workflow document envelope is invalid")
    _workflow_id(value.get("id"))
    if (
        type(value.get("name")) is not str
        or not value["name"]
        or value["name"] != value["name"].strip()
        or len(value["name"]) > 128
    ):
        raise ValueError("Workflow document name is invalid")
    return value


def _workflow_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is None
    ):
        raise ValueError("Workflow ID is invalid")
    return value


def _workflow_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _U64_MAX:
        raise ValueError("Workflow version is invalid")
    return value


def _workflow_timestamp(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _U64_MAX:
        raise RuntimeError("Fleet returned an invalid Workflow timestamp")
    return value


def _workflow_revision(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "workflowId",
        "version",
        "contentHash",
        "document",
        "createdAtMs",
    }:
        raise RuntimeError("Fleet returned an invalid Workflow revision")
    try:
        workflow_id = _workflow_id(value["workflowId"])
        version = _workflow_version(value["version"])
        document = _workflow_document(value["document"])
    except ValueError as error:
        raise RuntimeError("Fleet returned an invalid Workflow revision") from error
    content_hash = value["contentHash"]
    if (
        type(content_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
        or document["id"] != workflow_id
    ):
        raise RuntimeError("Fleet returned an invalid Workflow revision")
    return {
        "workflowId": workflow_id,
        "version": version,
        "contentHash": content_hash,
        "document": document,
        "createdAtMs": _workflow_timestamp(value["createdAtMs"]),
    }


def _optional_workflow_revision(result: object) -> dict[str, Any] | None:
    if type(result) is not dict or set(result) != {"revision"}:
        raise RuntimeError("Fleet returned an invalid Workflow read")
    if result["revision"] is None:
        return None
    return _workflow_revision(result["revision"])


def _workflow_write(result: object, allowed: set[str]) -> dict[str, Any]:
    if type(result) is not dict or set(result) != {"outcome", "revision"}:
        raise RuntimeError("Fleet returned an invalid Workflow write")
    outcome = result["outcome"]
    if type(outcome) is not str or outcome not in allowed:
        raise RuntimeError("Fleet returned an invalid Workflow write")
    return {"outcome": outcome, "revision": _workflow_revision(result["revision"])}


def _workflow_summary(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "workflowId",
        "latestVersion",
        "createdAtMs",
        "updatedAtMs",
    }:
        raise RuntimeError("Fleet returned an invalid Workflow summary")
    try:
        workflow_id = _workflow_id(value["workflowId"])
        latest_version = _workflow_version(value["latestVersion"])
    except ValueError as error:
        raise RuntimeError("Fleet returned an invalid Workflow summary") from error
    created_at_ms = _workflow_timestamp(value["createdAtMs"])
    updated_at_ms = _workflow_timestamp(value["updatedAtMs"])
    if updated_at_ms < created_at_ms:
        raise RuntimeError("Fleet returned an invalid Workflow summary")
    return {
        "workflowId": workflow_id,
        "latestVersion": latest_version,
        "createdAtMs": created_at_ms,
        "updatedAtMs": updated_at_ms,
    }


def _selector(source: object, network_id: object, device_id: object) -> dict[str, str]:
    return {
        "source": _identity_text(source, "Desktop alias source"),
        "network_id": _identity_text(network_id, "Desktop alias network ID"),
        "device_id": _identity_text(device_id, "Desktop alias device ID"),
    }


def _alias_text(value: object) -> str:
    alias = _display_text(value, "Desktop alias")
    if len(alias) > 128 or any(
        character in {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
        for character in alias
    ):
        raise ValueError("Desktop alias is invalid")
    return alias


def _alias_outcome(result: object, allowed: set[str]) -> str:
    if type(result) is not dict or set(result) != {"outcome"}:
        raise RuntimeError("Fleet returned an invalid Desktop alias outcome")
    outcome = result["outcome"]
    if type(outcome) is not str or outcome not in allowed:
        raise RuntimeError("Fleet returned an invalid Desktop alias outcome")
    return outcome


def _normalize_node(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _NODE_FIELDS:
        raise ValueError("Desktop node fields are invalid")
    stable_id = value["stable_id"]
    if type(stable_id) is not str or _STABLE_ID.fullmatch(stable_id) is None:
        raise ValueError("Desktop stable ID is invalid")

    identity = value["identity"]
    if type(identity) is not dict or set(identity) != {
        "source",
        "network_id",
        "device_id",
    }:
        raise ValueError("Desktop node identity is invalid")
    normalized_identity = {
        key: _identity_text(identity[key], f"Desktop identity {key}")
        for key in identity
    }

    naming = value["naming"]
    if type(naming) is not dict or set(naming) != {
        "display_name",
        "provider_name",
        "alias",
        "has_alias",
    }:
        raise ValueError("Desktop naming fields are invalid")
    display_name = _display_text(naming["display_name"], "Desktop display name")
    provider_name = _optional_display_text(
        naming["provider_name"], "Desktop provider name"
    )
    alias = _optional_display_text(naming["alias"], "Desktop alias")
    if type(naming["has_alias"]) is not bool or naming["has_alias"] != (
        alias is not None
    ):
        raise ValueError("Desktop alias state is invalid")

    managed = value["managed"]
    if type(managed) is not dict or set(managed) != {
        "state",
        "active",
        "projection_generation",
        "membership_generation",
        "binding_generation",
    }:
        raise ValueError("Desktop managed fields are invalid")
    state = managed["state"]
    if state not in {"active", "disabled", "removed"}:
        raise ValueError("Desktop managed state is invalid")
    if type(managed["active"]) is not bool or managed["active"] != (state == "active"):
        raise ValueError("Desktop active state is invalid")
    normalized_managed = {
        "state": state,
        "active": managed["active"],
        "projection_generation": _generation(managed["projection_generation"]),
        "membership_generation": _generation(managed["membership_generation"]),
        "binding_generation": _generation(managed["binding_generation"]),
    }

    readiness = normalize_readiness(value["readiness"])
    if readiness["managed_state"] != state:
        raise ValueError("Desktop managed and readiness states disagree")

    operations = value["operations"]
    if (
        type(operations) is not list
        or operations != sorted(operations)
        or len(operations) != len(set(operations))
        or any(
            type(operation) is not str or operation not in _OPERATIONS
            for operation in operations
        )
    ):
        raise ValueError("Desktop operations are invalid")

    return {
        "stable_id": stable_id,
        "identity": normalized_identity,
        "naming": {
            "display_name": display_name,
            "provider_name": provider_name,
            "alias": alias,
            "has_alias": naming["has_alias"],
        },
        "managed": normalized_managed,
        "readiness": readiness,
        "operations": list(operations),
    }


def _identity_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
    ):
        raise ValueError(f"{label} is invalid")
    if any(
        character.isspace() or ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _display_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(
            ord(character) < 32
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _optional_display_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _display_text(value, label)


def _generation(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value.startswith("0")
        or not value.isascii()
        or not value.isdigit()
    ):
        raise ValueError("Desktop generation is invalid")
    if int(value) > _U64_MAX:
        raise ValueError("Desktop generation is invalid")
    return value


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise RuntimeError("Fleet closed the Desktop response early")
        result.extend(chunk)
    return bytes(result)
