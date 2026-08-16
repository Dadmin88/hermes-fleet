"""Strict read-only client for Nodescale operator control V1."""

from __future__ import annotations

import json
import re
import socket
import struct
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ._paths import is_concrete_path

_SCHEMA = "nodescale.operator.v1"
_MAX_REQUEST_BYTES = 8 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_PAGE_SIZE = 32
_MAX_DEVICES = 256
_MAX_PAGES = 32
_OPERATION_TIMEOUT_SECONDS = 5.0
_MAX_TEXT_BYTES = 256
_U64_MAX = (1 << 64) - 1
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_ROLES = ("node", "worker", "controller", "profile_host", "observer", "admin")
_MEMBERSHIP_STATES = frozenset(
    {"pending", "joining", "active", "suspended", "revoking", "revoked"}
)
_PROJECTION_STATES = frozenset(
    {
        "not_requested",
        "pending",
        "applied",
        "failed_retryable",
        "conflict",
        "revoked",
    }
)
_TRUST_STATES = frozenset({"untrusted", "trusted", "revoked"})
_PROVIDER_BINDING_STATES = frozenset({"active", "stale", "cleanup_pending", "removed"})
_KERYX_BINDING_STATES = frozenset({"pending", "active", "stale", "rotated", "revoked"})
_DEVICE_FIELDS = frozenset(
    {
        "device_id",
        "network_id",
        "display_name",
        "membership_state",
        "roles",
        "credential_generation",
        "keryx_binding_generation",
        "fleet_projection_generation",
        "fleet_projection_status",
        "provider_instance_id",
        "provider_node_id",
        "durable_trust_state",
        "durable_trust_revision",
        "live_trust_evidence",
        "provider_binding_state",
        "provider_binding_revision",
        "keryx_binding_id",
        "keryx_binding_state",
        "verified_keryx_peer_id",
        "keryx_binding_revision",
        "live_keryx_binding_health",
        "created_at",
        "updated_at",
        "revoked_at",
    }
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Nodescale operator JSON member")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite Nodescale operator JSON number")


class NodescaleOperatorClient:
    """Read bounded durable device authority through Nodescale's public UDS."""

    def __init__(
        self,
        *,
        socket_path: Path,
        network_id: str,
        timeout_seconds: float = 2.0,
        max_devices: int = _MAX_DEVICES,
    ) -> None:
        if not is_concrete_path(socket_path) or not socket_path.is_absolute():
            raise ValueError("Nodescale operator socket must be an absolute Path")
        if _uuid(network_id, "Nodescale operator network ID") != network_id:
            raise ValueError("Nodescale operator network ID is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError(
                "Nodescale operator timeout must be between 0 and 30 seconds"
            )
        if (
            isinstance(max_devices, bool)
            or not isinstance(max_devices, int)
            or not 1 <= max_devices <= _MAX_DEVICES
        ):
            raise ValueError("Nodescale operator device cap is invalid")
        self._socket_path = socket_path
        self._network_id = network_id
        self._timeout_seconds = float(timeout_seconds)
        self._max_devices = max_devices

    def list_devices(self) -> dict[str, Any]:
        """Return a strict bounded current-state view for one exact network."""
        deadline = time.monotonic() + _OPERATION_TIMEOUT_SECONDS
        page_size = self._capabilities(deadline)
        devices: list[dict[str, Any]] = []
        device_ids: set[str] = set()
        cursor: str | None = None
        truncated = False

        pages = 0
        while len(devices) < self._max_devices and pages < _MAX_PAGES:
            pages += 1
            limit = min(page_size, self._max_devices - len(devices))
            request: dict[str, Any] = {
                "version": _SCHEMA,
                "kind": "devices.list",
                "network_id": self._network_id,
                "limit": limit,
            }
            if cursor is not None:
                request["cursor"] = cursor
            document = self._request(request, deadline=deadline)
            expected_fields = {"version", "kind", "network_id", "devices"}
            if "next_cursor" in document:
                expected_fields.add("next_cursor")
            if (
                set(document) != expected_fields
                or document.get("kind") != "devices.list"
                or document.get("network_id") != self._network_id
                or type(document.get("devices")) is not list
                or len(document["devices"]) > limit
            ):
                raise RuntimeError("Nodescale returned an invalid operator device page")
            page = [
                _normalize_device(device, network_id=self._network_id)
                for device in document["devices"]
            ]
            for device in page:
                device_id = device["device_id"]
                if device_id in device_ids:
                    raise RuntimeError(
                        "Nodescale returned duplicate Nodescale operator device"
                    )
                if cursor is not None and device_id <= cursor:
                    raise RuntimeError(
                        "Nodescale returned non-monotonic operator devices"
                    )
                cursor = device_id
                device_ids.add(device_id)
                devices.append(device)

            next_cursor = document.get("next_cursor")
            if next_cursor is None:
                truncated = False
                break
            try:
                next_cursor = _uuid(next_cursor, "Nodescale operator cursor")
            except ValueError as error:
                raise RuntimeError(
                    "Nodescale returned an invalid Nodescale operator cursor"
                ) from error
            if not page or cursor != next_cursor:
                raise RuntimeError(
                    "Nodescale returned an invalid Nodescale operator cursor"
                )
            if len(devices) >= self._max_devices:
                truncated = True
                break
        else:
            truncated = True

        return {
            "schema": _SCHEMA,
            "network_id": self._network_id,
            "devices": devices,
            "truncated": truncated,
        }

    def inspect_device(self, device_id: str) -> dict[str, Any]:
        """Read back one exact durable device identity from Nodescale."""
        device_id = _uuid(device_id, "Nodescale operator device ID")
        deadline = time.monotonic() + _OPERATION_TIMEOUT_SECONDS
        document = self._request(
            {
                "version": _SCHEMA,
                "kind": "devices.inspect",
                "network_id": self._network_id,
                "device_id": device_id,
            },
            deadline=deadline,
        )
        if (
            set(document) != {"version", "kind", "network_id", "device"}
            or document.get("kind") != "devices.inspect"
            or document.get("network_id") != self._network_id
        ):
            raise RuntimeError(
                "Nodescale returned an invalid Nodescale operator inspection"
            )
        device = _normalize_device(document["device"], network_id=self._network_id)
        if device["device_id"] != device_id:
            raise RuntimeError(
                "Nodescale returned an invalid Nodescale operator inspection"
            )
        return device

    def _capabilities(self, deadline: float) -> int:
        document = self._request(
            {"version": _SCHEMA, "kind": "capabilities"}, deadline=deadline
        )
        if (
            set(document) != {"version", "kind", "capabilities"}
            or document.get("kind") != "capabilities"
        ):
            raise RuntimeError("Nodescale returned invalid operator capabilities")
        capabilities = document["capabilities"]
        if type(capabilities) is not dict or set(capabilities) != {
            "read_operations",
            "mutation_operations",
            "max_page_size",
            "max_response_bytes",
        }:
            raise RuntimeError("Nodescale returned invalid operator capabilities")
        if (
            capabilities["read_operations"]
            != ["capabilities", "devices.list", "devices.inspect"]
            or capabilities["mutation_operations"] != []
            or type(capabilities["max_page_size"]) is not int
            or capabilities["max_page_size"] != _MAX_PAGE_SIZE
            or type(capabilities["max_response_bytes"]) is not int
            or capabilities["max_response_bytes"] != _MAX_RESPONSE_BYTES
        ):
            raise RuntimeError("Nodescale returned invalid operator capabilities")
        return _MAX_PAGE_SIZE

    def _request(self, payload: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not 1 <= len(encoded) <= _MAX_REQUEST_BYTES:
            raise RuntimeError("Nodescale operator request is oversized")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self._remaining_timeout(deadline))
            connection.connect(str(self._socket_path))
            connection.settimeout(self._remaining_timeout(deadline))
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
            connection.shutdown(socket.SHUT_WR)
            length = struct.unpack(
                "!I",
                _recv_exact(
                    connection,
                    4,
                    deadline=deadline,
                    timeout_seconds=self._timeout_seconds,
                ),
            )[0]
            if not 1 <= length <= _MAX_RESPONSE_BYTES:
                raise RuntimeError("Nodescale returned an invalid operator frame")
            raw = _recv_exact(
                connection,
                length,
                deadline=deadline,
                timeout_seconds=self._timeout_seconds,
            )
            try:
                document = json.loads(
                    raw,
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                RecursionError,
                ValueError,
            ):
                document = None
            if document is None:
                raise RuntimeError(
                    "Nodescale returned malformed Nodescale operator JSON"
                )
            connection.settimeout(self._remaining_timeout(deadline))
            if connection.recv(1):
                raise RuntimeError(
                    "Nodescale returned trailing Nodescale operator frame bytes"
                )
        if type(document) is not dict or document.get("version") != _SCHEMA:
            raise RuntimeError("Nodescale returned an invalid operator response")
        if document.get("kind") == "error":
            if set(document) != {"version", "kind", "error"}:
                raise RuntimeError("Nodescale returned an invalid operator error")
            error = document["error"]
            if error == "not_found":
                raise RuntimeError("Nodescale operator device was not found")
            if error == "unavailable":
                raise RuntimeError("Nodescale operator state is unavailable")
            if error == "invalid_request":
                raise RuntimeError("Nodescale rejected the operator request")
            raise RuntimeError("Nodescale returned an invalid operator error")
        return document

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Nodescale operator request timed out")
        return min(self._timeout_seconds, remaining)


def _normalize_device(value: object, *, network_id: str) -> dict[str, Any]:
    try:
        return _normalize_device_fields(value, network_id=network_id)
    except (TypeError, ValueError):
        raise RuntimeError(
            "Nodescale returned an invalid Nodescale operator device"
        ) from None


def _normalize_device_fields(value: object, *, network_id: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _DEVICE_FIELDS:
        raise ValueError("invalid device fields")
    device_id = _uuid(value["device_id"], "Nodescale operator device ID")
    if value["network_id"] != network_id:
        raise ValueError("invalid network identity")
    membership_state = _enum(value["membership_state"], _MEMBERSHIP_STATES)
    roles = value["roles"]
    if (
        type(roles) is not list
        or not roles
        or len(roles) > len(_ROLES)
        or any(type(role) is not str or role not in _ROLES for role in roles)
        or roles != sorted(set(roles), key=_ROLES.index)
    ):
        raise ValueError("invalid roles")

    provider_instance_id = _optional_uuid(
        value["provider_instance_id"], "Nodescale provider instance ID"
    )
    provider_node_id = _optional_identifier(value["provider_node_id"])
    if (provider_instance_id is None) != (provider_node_id is None):
        raise ValueError("incoherent provider identity")

    trust_state = _optional_enum(value["durable_trust_state"], _TRUST_STATES)
    trust_revision = _optional_generation(value["durable_trust_revision"])
    if (trust_state is None) != (trust_revision is None):
        raise ValueError("incoherent trust evidence")

    provider_binding_state = _optional_enum(
        value["provider_binding_state"], _PROVIDER_BINDING_STATES
    )
    provider_binding_revision = _optional_generation(value["provider_binding_revision"])
    if (provider_binding_state is None) != (provider_binding_revision is None):
        raise ValueError("incoherent provider binding evidence")
    if provider_binding_state is not None and trust_state is None:
        raise ValueError("provider binding evidence requires durable trust evidence")

    keryx_binding_id = _optional_uuid(
        value["keryx_binding_id"], "Nodescale Keryx binding ID"
    )
    keryx_binding_state = _optional_enum(
        value["keryx_binding_state"], _KERYX_BINDING_STATES
    )
    verified_peer_id = _optional_identifier(value["verified_keryx_peer_id"])
    keryx_binding_revision = _optional_generation(value["keryx_binding_revision"])
    binding_presence = (
        keryx_binding_id is not None,
        keryx_binding_state is not None,
        keryx_binding_revision is not None,
    )
    if len(set(binding_presence)) != 1 or (
        verified_peer_id is not None and keryx_binding_id is None
    ):
        raise ValueError("incoherent Keryx binding evidence")

    created_at = _timestamp(value["created_at"])
    updated_at = _timestamp(value["updated_at"])
    revoked_at = _optional_timestamp(value["revoked_at"])
    if updated_at < created_at or (
        revoked_at is not None and not created_at <= revoked_at <= updated_at
    ):
        raise ValueError("incoherent device timestamps")
    if (
        value["live_trust_evidence"] != "not_reconciled_by_operator_read"
        or value["live_keryx_binding_health"] != "not_exposed"
    ):
        raise ValueError("unsupported live authority claim")

    return {
        "device_id": device_id,
        "network_id": network_id,
        "display_name": _text(value["display_name"]),
        "membership_state": membership_state,
        "roles": roles,
        "credential_generation": _generation(value["credential_generation"]),
        "keryx_binding_generation": _generation(value["keryx_binding_generation"]),
        "fleet_projection_generation": _generation(
            value["fleet_projection_generation"]
        ),
        "fleet_projection_status": _enum(
            value["fleet_projection_status"], _PROJECTION_STATES
        ),
        "provider_instance_id": provider_instance_id,
        "provider_node_id": provider_node_id,
        "durable_trust_state": trust_state,
        "durable_trust_revision": trust_revision,
        "live_trust_evidence": "not_reconciled_by_operator_read",
        "provider_binding_state": provider_binding_state,
        "provider_binding_revision": provider_binding_revision,
        "keryx_binding_id": keryx_binding_id,
        "keryx_binding_state": keryx_binding_state,
        "verified_keryx_peer_id": verified_peer_id,
        "keryx_binding_revision": keryx_binding_revision,
        "live_keryx_binding_health": "not_exposed",
        "created_at": value["created_at"],
        "updated_at": value["updated_at"],
        "revoked_at": value["revoked_at"],
    }


def _enum(value: object, allowed: frozenset[str]) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError("invalid enum")
    return value


def _optional_enum(value: object, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    return _enum(value, allowed)


def _generation(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _U64_MAX:
        raise ValueError("invalid generation")
    return value


def _optional_generation(value: object) -> int | None:
    if value is None:
        return None
    return _generation(value)


def _uuid(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} is invalid")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if value != normalized:
        raise ValueError(f"{label} is invalid")
    return normalized


def _optional_uuid(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _uuid(value, label)


def _optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid safe identifier")
    return value


def _text(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("invalid bounded text")
    return value


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not value or len(value) > 64:
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("invalid timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("invalid timestamp")
    return parsed


def _optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return _timestamp(value)


def _recv_exact(
    connection: socket.socket,
    size: int,
    *,
    deadline: float,
    timeout_seconds: float,
) -> bytes:
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Nodescale operator request timed out")
        connection.settimeout(min(timeout_seconds, remaining))
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise RuntimeError("Nodescale closed the operator response early")
        result.extend(chunk)
    return bytes(result)
