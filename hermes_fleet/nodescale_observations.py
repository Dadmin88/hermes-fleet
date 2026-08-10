"""Strict read-only client for Nodescale provider observations V1."""

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

_SCHEMA = "nodescale.observations.v1"
_MAX_REQUEST_BYTES = 8 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_PAGE_SIZE = 100
_MAX_OBSERVATIONS = 256
_MAX_PAGES = 32
_OVERVIEW_TIMEOUT_SECONDS = 5.0
_MAX_TEXT_BYTES = 256
_MAX_ITEMS = 32
_U64_MAX = (1 << 64) - 1
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROVIDER_NODE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_PROVIDER_KINDS = frozenset({"fake", "headscale", "tailscale"})
_CLASSIFICATIONS = frozenset(
    {
        "expected_joining",
        "discovered_unmanaged",
        "active",
        "provider_missing",
        "provider_expired",
        "provider_removed",
        "identity_conflict",
        "quarantined",
        "revoked",
    }
)
_RECONCILIATION_STATES = frozenset(
    {
        "never_reconciled",
        "healthy",
        "unreachable",
        "authentication_failed",
        "incompatible",
        "malformed",
        "identity_conflict",
        "state_failure",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "observed_id",
        "network_id",
        "provider_kind",
        "provider_instance_id",
        "provider_node_id",
        "hostname",
        "given_name",
        "addresses",
        "tags",
        "registered_at",
        "last_seen_at",
        "expires_at",
        "online",
        "expired",
        "classification",
        "first_observed_at",
        "last_observed_at",
        "snapshot_at",
    }
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate Nodescale JSON member")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite Nodescale JSON number")


class NodescaleObservationClient:
    """Read bounded provider evidence without accessing Nodescale persistence."""

    def __init__(
        self,
        *,
        socket_path: Path,
        network_id: str,
        timeout_seconds: float = 2.0,
        max_observations: int = _MAX_OBSERVATIONS,
    ) -> None:
        if not is_concrete_path(socket_path) or not socket_path.is_absolute():
            raise ValueError("Nodescale observation socket must be an absolute Path")
        if _uuid(network_id, "Nodescale network ID") != network_id:
            raise ValueError("Nodescale network ID is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 30
        ):
            raise ValueError("Nodescale timeout must be between 0 and 30 seconds")
        if (
            isinstance(max_observations, bool)
            or not isinstance(max_observations, int)
            or not 1 <= max_observations <= _MAX_OBSERVATIONS
        ):
            raise ValueError("Nodescale observation cap is invalid")
        self._socket_path = socket_path
        self._network_id = network_id
        self._timeout_seconds = float(timeout_seconds)
        self._max_observations = max_observations

    def overview(self) -> dict[str, Any]:
        """Return a strict bounded current-state observation view."""
        deadline = time.monotonic() + _OVERVIEW_TIMEOUT_SECONDS
        capabilities = self._capabilities(deadline)
        page_size = capabilities["max_page_size"]
        reconciliation = self._summary(deadline)
        observations: list[dict[str, Any]] = []
        observed_ids: set[str] = set()
        cursor: str | None = None
        truncated = False

        pages = 0
        while len(observations) < self._max_observations and pages < _MAX_PAGES:
            pages += 1
            limit = min(page_size, self._max_observations - len(observations))
            request: dict[str, Any] = {
                "version": _SCHEMA,
                "kind": "list",
                "network_id": self._network_id,
                "limit": limit,
            }
            if cursor is not None:
                request["cursor"] = cursor
            document = self._request(request, deadline=deadline)
            expected_fields = {
                "version",
                "kind",
                "network_id",
                "reconciliation",
                "observations",
            }
            if "next_cursor" in document:
                expected_fields.add("next_cursor")
            if (
                set(document) != expected_fields
                or document.get("kind") != "list"
                or document.get("network_id") != self._network_id
                or type(document.get("observations")) is not list
                or len(document["observations"]) > limit
            ):
                raise RuntimeError("Nodescale returned an invalid observation page")
            reconciliation = _normalize_reconciliation(document["reconciliation"])
            page = [
                _normalize_observation(row, network_id=self._network_id)
                for row in document["observations"]
            ]
            for row in page:
                provider_node_id = row["provider_node_id"]
                if cursor is not None and provider_node_id <= cursor:
                    raise RuntimeError("Nodescale returned non-monotonic observations")
                cursor = provider_node_id
                observed_id = row["observed_id"]
                if observed_id in observed_ids:
                    raise RuntimeError(
                        "Nodescale returned duplicate Nodescale observation"
                    )
                observed_ids.add(observed_id)
                observations.append(row)

            next_cursor = document.get("next_cursor")
            if next_cursor is None:
                truncated = False
                break
            next_cursor = _provider_node_id(next_cursor)
            if not page or cursor != next_cursor:
                raise RuntimeError("Nodescale returned an invalid observation cursor")
            if len(observations) >= self._max_observations:
                truncated = True
                break
        else:
            truncated = True

        return {
            "schema": _SCHEMA,
            "network_id": self._network_id,
            "reconciliation": reconciliation,
            "observations": observations,
            "truncated": truncated,
        }

    def _capabilities(self, deadline: float) -> dict[str, int]:
        document = self._request(
            {"version": _SCHEMA, "kind": "capabilities"}, deadline=deadline
        )
        if (
            set(document) != {"version", "kind", "capabilities"}
            or document.get("kind") != "capabilities"
        ):
            raise RuntimeError("Nodescale returned invalid capabilities")
        capabilities = document["capabilities"]
        if type(capabilities) is not dict or set(capabilities) != {
            "max_page_size",
            "max_response_bytes",
        }:
            raise RuntimeError("Nodescale returned invalid capabilities")
        page_size = capabilities["max_page_size"]
        response_bytes = capabilities["max_response_bytes"]
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= _MAX_PAGE_SIZE
            or response_bytes != _MAX_RESPONSE_BYTES
        ):
            raise RuntimeError("Nodescale returned invalid capabilities")
        return {"max_page_size": page_size, "max_response_bytes": response_bytes}

    def _summary(self, deadline: float) -> dict[str, Any]:
        document = self._request(
            {
                "version": _SCHEMA,
                "kind": "summary",
                "network_id": self._network_id,
            },
            deadline=deadline,
        )
        if (
            set(document)
            != {
                "version",
                "kind",
                "network_id",
                "reconciliation",
            }
            or document.get("kind") != "summary"
            or document.get("network_id") != self._network_id
        ):
            raise RuntimeError("Nodescale returned an invalid observation summary")
        return _normalize_reconciliation(document["reconciliation"])

    def _request(self, payload: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if not 1 <= len(encoded) <= _MAX_REQUEST_BYTES:
            raise RuntimeError("Nodescale observation request is oversized")
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
                raise RuntimeError("Nodescale returned an invalid observation frame")
            try:
                document = json.loads(
                    _recv_exact(
                        connection,
                        length,
                        deadline=deadline,
                        timeout_seconds=self._timeout_seconds,
                    ),
                    object_pairs_hook=_unique_object,
                    parse_constant=_reject_constant,
                )
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                RecursionError,
                ValueError,
            ) as error:
                raise RuntimeError(
                    "Nodescale returned malformed Nodescale JSON"
                ) from error
            connection.settimeout(self._remaining_timeout(deadline))
            if connection.recv(1):
                raise RuntimeError("Nodescale returned trailing Nodescale frame bytes")
        if type(document) is not dict or document.get("version") != _SCHEMA:
            raise RuntimeError("Nodescale returned an invalid observation response")
        if document.get("kind") == "error":
            if set(document) != {"version", "kind", "error"} or document.get(
                "error"
            ) not in {"invalid_request", "unavailable"}:
                raise RuntimeError("Nodescale returned an invalid observation error")
            raise RuntimeError("Nodescale observations are unavailable")
        return document

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Nodescale observation overview timed out")
        return min(self._timeout_seconds, remaining)


def _normalize_reconciliation(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "state",
        "last_attempted_at",
        "last_successful_at",
        "observed_count",
    }:
        raise RuntimeError("Nodescale returned invalid reconciliation evidence")
    state = value["state"]
    count = value["observed_count"]
    if state not in _RECONCILIATION_STATES or (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= _U64_MAX
    ):
        raise RuntimeError("Nodescale returned invalid reconciliation evidence")
    return {
        "state": state,
        "last_attempted_at": _optional_timestamp(value["last_attempted_at"]),
        "last_successful_at": _optional_timestamp(value["last_successful_at"]),
        "observed_count": count,
    }


def _normalize_observation(value: object, *, network_id: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _OBSERVATION_FIELDS:
        raise RuntimeError("Nodescale returned an invalid Nodescale observation")
    observed_id = value["observed_id"]
    if type(observed_id) is not str or _SHA256.fullmatch(observed_id) is None:
        raise RuntimeError("Nodescale returned an invalid Nodescale observation")
    if value["network_id"] != network_id:
        raise RuntimeError("Nodescale returned an invalid Nodescale observation")
    provider_kind = value["provider_kind"]
    if provider_kind not in _PROVIDER_KINDS:
        raise RuntimeError("Nodescale returned an invalid Nodescale observation")
    online = value["online"]
    expired = value["expired"]
    classification = value["classification"]
    if (
        online is not None
        and type(online) is not bool
        or type(expired) is not bool
        or classification not in _CLASSIFICATIONS
    ):
        raise RuntimeError("Nodescale returned an invalid Nodescale observation")
    try:
        provider_instance_id = _uuid(
            value["provider_instance_id"], "Nodescale provider instance ID"
        )
    except ValueError as error:
        raise RuntimeError(
            "Nodescale returned an invalid Nodescale observation"
        ) from error
    return {
        "observed_id": observed_id,
        "network_id": network_id,
        "provider_kind": provider_kind,
        "provider_instance_id": provider_instance_id,
        "provider_node_id": _provider_node_id(value["provider_node_id"]),
        "hostname": _text(value["hostname"], allow_empty=True),
        "given_name": _text(value["given_name"], allow_empty=True),
        "addresses": _text_list(value["addresses"]),
        "tags": _text_list(value["tags"]),
        "registered_at": _optional_timestamp(value["registered_at"]),
        "last_seen_at": _optional_timestamp(value["last_seen_at"]),
        "expires_at": _optional_timestamp(value["expires_at"]),
        "online": online,
        "expired": expired,
        "classification": classification,
        "first_observed_at": _timestamp(value["first_observed_at"]),
        "last_observed_at": _timestamp(value["last_observed_at"]),
        "snapshot_at": _timestamp(value["snapshot_at"]),
    }


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


def _provider_node_id(value: object) -> str:
    if type(value) is not str or _PROVIDER_NODE_ID.fullmatch(value) is None:
        raise RuntimeError("Nodescale returned an invalid provider node ID")
    return value


def _text(value: object, *, allow_empty: bool) -> str:
    if (
        type(value) is not str
        or (not allow_empty and not value)
        or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise RuntimeError("Nodescale returned invalid bounded text")
    return value


def _text_list(value: object) -> list[str]:
    if type(value) is not list or len(value) > _MAX_ITEMS:
        raise RuntimeError("Nodescale returned invalid bounded text list")
    return [_text(item, allow_empty=False) for item in value]


def _timestamp(value: object) -> str:
    if type(value) is not str or not value or len(value) > 64:
        raise RuntimeError("Nodescale returned an invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RuntimeError("Nodescale returned an invalid timestamp") from error
    if parsed.tzinfo is None:
        raise RuntimeError("Nodescale returned an invalid timestamp")
    return value


def _optional_timestamp(value: object) -> str | None:
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
            raise RuntimeError("Nodescale observation overview timed out")
        connection.settimeout(min(timeout_seconds, remaining))
        chunk = connection.recv(size - len(result))
        if not chunk:
            raise RuntimeError("Nodescale closed the observation response early")
        result.extend(chunk)
    return bytes(result)
