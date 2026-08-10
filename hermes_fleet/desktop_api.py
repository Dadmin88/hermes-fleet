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
_MAX_RESPONSE_BYTES = 262_144
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
        result = self._request()
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

    def _request(self) -> dict[str, Any]:
        payload = json.dumps(
            {"schema": _SCHEMA, "kind": "overview"},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._timeout_seconds)
            connection.connect(str(self._socket_path))
            connection.sendall(struct.pack("!I", len(payload)) + payload)
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
        if (
            type(document) is not dict
            or set(document) != {"schema", "kind", "ok", "result"}
            or document["schema"] != _SCHEMA
            or document["kind"] != "overview"
            or document["ok"] is not True
            or type(document["result"]) is not dict
        ):
            raise RuntimeError("Fleet rejected or malformed the Desktop request")
        return document["result"]


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
