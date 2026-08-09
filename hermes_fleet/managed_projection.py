"""Fleet-owned durable managed projections from the NodeScale control plane."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA_ID = "fleet.managed-projection.v1"
_GENERATED_ALLOWLIST = frozenset({"fleet.health", "fleet.inventory", "fleet.message"})
_U64_MAX = 18_446_744_073_709_551_615
_PROVENANCE_REQUIRED = frozenset({"source", "network_id", "device_id"})
_PROVENANCE_OPTIONAL = frozenset({"snapshot", "controller"})
_PROVENANCE_FIELDS = _PROVENANCE_REQUIRED | _PROVENANCE_OPTIONAL
_DOCUMENT_FIELDS = frozenset(
    {
        "source",
        "network_id",
        "device_id",
        "projection_generation",
        "membership_generation",
        "binding_generation",
        "operation",
        "generated_operations",
        "provenance",
    }
)

_CANONICAL_OPERATION_JSON = (
    "[]",
    '["fleet.health"]',
    '["fleet.inventory"]',
    '["fleet.message"]',
    '["fleet.health","fleet.inventory"]',
    '["fleet.health","fleet.message"]',
    '["fleet.inventory","fleet.message"]',
    '["fleet.health","fleet.inventory","fleet.message"]',
)
_CANONICAL_OPERATION_SQL = ", ".join(repr(value) for value in _CANONICAL_OPERATION_JSON)
_CANONICAL_OPERATION_NAME_SQL = ", ".join(
    repr(value) for value in sorted(_GENERATED_ALLOWLIST)
)

_CANONICAL_TABLE_SQL = {
    "managed_projection_schema": """
        CREATE TABLE managed_projection_schema (schema_id TEXT PRIMARY KEY)
    """,
    "managed_projections": f"""
        CREATE TABLE managed_projections (
            source TEXT NOT NULL,
            network_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            projection_generation TEXT NOT NULL,
            membership_generation TEXT NOT NULL,
            binding_generation TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active', 'disabled', 'removed')),
            allowed_operations TEXT NOT NULL CHECK(
                allowed_operations IN ({_CANONICAL_OPERATION_SQL})
            ),
            provenance TEXT NOT NULL CHECK(json_valid(provenance)),
            PRIMARY KEY(source, network_id, device_id)
        )
    """,
    "operator_projection_denies": f"""
        CREATE TABLE operator_projection_denies (
            source TEXT NOT NULL,
            network_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(
                operation IN ({_CANONICAL_OPERATION_NAME_SQL})
            ),
            PRIMARY KEY(source, network_id, device_id, operation)
        )
    """,
    "managed_projection_audit": f"""
        CREATE TABLE managed_projection_audit (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            network_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            projection_generation TEXT NOT NULL,
            membership_generation TEXT NOT NULL,
            binding_generation TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('active', 'disabled', 'removed')),
            allowed_operations TEXT NOT NULL CHECK(
                allowed_operations IN ({_CANONICAL_OPERATION_SQL})
            ),
            provenance TEXT NOT NULL CHECK(json_valid(provenance)),
            outcome TEXT NOT NULL CHECK(
                outcome IN (
                    'applied', 'already_applied', 'conflict', 'stale', 'gap',
                    'regression'
                )
            ),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """,
}

_PROVENANCE_TRIGGER_WHEN = """
    json_type(NEW.provenance) IS NOT 'object'
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.provenance) AS field
        WHERE field.key NOT IN (
            'source', 'network_id', 'device_id', 'snapshot', 'controller'
        )
           OR field.type IS NOT 'text'
    )
    OR json_extract(NEW.provenance, '$.source') IS NOT NEW.source
    OR json_extract(NEW.provenance, '$.network_id') IS NOT NEW.network_id
    OR json_extract(NEW.provenance, '$.device_id') IS NOT NEW.device_id
    OR (
        json_type(NEW.provenance, '$.controller') IS NOT NULL
        AND json_extract(NEW.provenance, '$.controller') IS NOT NEW.source
    )
    OR (
        json_type(NEW.provenance, '$.snapshot') IS NOT NULL
        AND (
            json_type(NEW.provenance, '$.snapshot') IS NOT 'text'
            OR json_extract(NEW.provenance, '$.snapshot') = ''
            OR json_extract(NEW.provenance, '$.snapshot') = '0'
            OR substr(json_extract(NEW.provenance, '$.snapshot'), 1, 1) = '0'
            OR json_extract(NEW.provenance, '$.snapshot') GLOB '*[^0-9]*'
            OR length(json_extract(NEW.provenance, '$.snapshot')) > 20
            OR (
                length(json_extract(NEW.provenance, '$.snapshot')) = 20
                AND json_extract(NEW.provenance, '$.snapshot') > '18446744073709551615'
            )
        )
    )
"""

_CANONICAL_TRIGGER_SQL = {
    "managed_projection_reject_invalid_provenance_insert": f"""
        CREATE TRIGGER managed_projection_reject_invalid_provenance_insert
        BEFORE INSERT ON managed_projections
        WHEN {_PROVENANCE_TRIGGER_WHEN}
        BEGIN
            SELECT RAISE(ABORT, 'managed projection provenance is invalid');
        END
    """,
    "managed_projection_reject_invalid_provenance_update": f"""
        CREATE TRIGGER managed_projection_reject_invalid_provenance_update
        BEFORE UPDATE OF provenance ON managed_projections
        WHEN {_PROVENANCE_TRIGGER_WHEN}
        BEGIN
            SELECT RAISE(ABORT, 'managed projection provenance is invalid');
        END
    """,
    "managed_projection_audit_reject_invalid_provenance": f"""
        CREATE TRIGGER managed_projection_audit_reject_invalid_provenance
        BEFORE INSERT ON managed_projection_audit
        WHEN {_PROVENANCE_TRIGGER_WHEN}
        BEGIN
            SELECT RAISE(ABORT, 'managed projection audit provenance is invalid');
        END
    """,
    "managed_projection_audit_immutable_update": """
        CREATE TRIGGER managed_projection_audit_immutable_update
        BEFORE UPDATE ON managed_projection_audit
        BEGIN
            SELECT RAISE(ABORT, 'managed projection audit is immutable');
        END
    """,
    "managed_projection_audit_immutable_delete": """
        CREATE TRIGGER managed_projection_audit_immutable_delete
        BEFORE DELETE ON managed_projection_audit
        WHEN managed_projection_audit_trim_allowed() != 1
        BEGIN
            SELECT RAISE(ABORT, 'managed projection audit is immutable');
        END
    """,
}


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The deterministic result of applying one controller projection."""

    outcome: str


class ManagedProjectionStore:
    """SQLite-backed generated state, isolated from local operator denials."""

    def __init__(self, path: str | Path, *, audit_limit: int = 256) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.name:
            raise ValueError(
                "managed projection store path must be an absolute file Path"
            )
        if type(audit_limit) is not int or audit_limit < 1:
            raise ValueError("audit_limit must be a positive integer")
        self.audit_limit = audit_limit
        self._audit_trim_connection_ids: set[int] = set()
        _require_safe_database_parent(self.path)
        _prepare_database_file(self.path)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._initialize(connection)

    def apply(
        self,
        *,
        source: str,
        network_id: str,
        device_id: str,
        projection_generation: str,
        membership_generation: str,
        binding_generation: str,
        content_hash: str,
        operation: str,
        generated_operations: tuple[str, ...] | list[str] = (),
        provenance: dict[str, Any] | None = None,
        wire_document: dict[str, Any] | None = None,
    ) -> ApplyResult:
        """Apply one atomically durable projection transition.

        ``wire_document`` is optional compatibility-safe evidence from a strict
        wire parser.  When supplied, its complete canonical material is hashed
        and must match ``content_hash``; legacy callers that only provide the
        decomposed fields retain their explicit fixture hash contract.
        """
        key = _key(source, network_id, device_id)
        projection_generation = _generation(
            projection_generation, "projection_generation"
        )
        membership_generation = _generation(
            membership_generation, "membership_generation"
        )
        binding_generation = _generation(binding_generation, "binding_generation")
        content_hash = _content_hash(content_hash)
        if operation not in {"upsert", "disable", "remove"}:
            raise ValueError("operation must be upsert, disable, or remove")
        operations = _operations(generated_operations)
        provenance_json = _provenance(provenance, key)
        if wire_document is not None:
            _verify_wire_document(
                wire_document,
                source=key[0],
                network_id=key[1],
                device_id=key[2],
                projection_generation=projection_generation,
                membership_generation=membership_generation,
                binding_generation=binding_generation,
                content_hash=content_hash,
                operation=operation,
                generated_operations=operations,
                provenance_json=provenance_json,
            )

        state = {"upsert": "active", "disable": "disabled", "remove": "removed"}[
            operation
        ]
        # A disabled/removal transition accepts only already-validated input and
        # materializes no generated grants.  This makes revocation atomic and
        # cannot silently retain a prior authorization.
        stored_operations = operations if state == "active" else ()
        stored_operations_json = _canonical_json(list(stored_operations))
        incoming = (
            projection_generation,
            membership_generation,
            binding_generation,
            content_hash,
            state,
            stored_operations_json,
            provenance_json,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT projection_generation, membership_generation, binding_generation,
                       content_hash, state, allowed_operations, provenance
                FROM managed_projections
                WHERE source = ? AND network_id = ? AND device_id = ?
                """,
                key,
            ).fetchone()
            if existing is not None:
                current_generation = int(existing[0])
                incoming_generation = int(projection_generation)
                if incoming_generation < current_generation:
                    self._append_audit(connection, key, incoming, "stale")
                    return ApplyResult("stale")
                if incoming_generation == current_generation:
                    outcome = "already_applied" if existing == incoming else "conflict"
                    self._append_audit(connection, key, incoming, outcome)
                    return ApplyResult(outcome)
                if (
                    int(membership_generation) < int(existing[1])
                    or int(binding_generation) < int(existing[2])
                ):
                    self._append_audit(connection, key, incoming, "regression")
                    return ApplyResult("regression")
                if incoming_generation != current_generation + 1:
                    self._append_audit(connection, key, incoming, "gap")
                    return ApplyResult("gap")
            connection.execute(
                """
                INSERT INTO managed_projections(
                    source, network_id, device_id, projection_generation,
                    membership_generation, binding_generation, content_hash, state,
                    allowed_operations, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, network_id, device_id) DO UPDATE SET
                    projection_generation = excluded.projection_generation,
                    membership_generation = excluded.membership_generation,
                    binding_generation = excluded.binding_generation,
                    content_hash = excluded.content_hash,
                    state = excluded.state,
                    allowed_operations = excluded.allowed_operations,
                    provenance = excluded.provenance
                """,
                (*key, *incoming),
            )
            self._append_audit(connection, key, incoming, "applied")
        return ApplyResult("applied")

    def set_operator_deny(
        self,
        *,
        source: str,
        network_id: str,
        device_id: str,
        operation: str,
        denied: bool,
    ) -> None:
        """Set a local-only deny override without modifying generated state."""
        key = _key(source, network_id, device_id)
        if operation not in _GENERATED_ALLOWLIST:
            raise ValueError("operation is not a generated Fleet operation")
        if type(denied) is not bool:
            raise ValueError("denied must be a boolean")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if denied:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO operator_projection_denies(
                        source, network_id, device_id, operation
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (*key, operation),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM operator_projection_denies
                    WHERE source = ? AND network_id = ? AND device_id = ?
                      AND operation = ?
                    """,
                    (*key, operation),
                )

    def audit(
        self, *, source: str, network_id: str, device_id: str
    ) -> tuple[dict[str, Any], ...]:
        """Read bounded durable audit outcomes in causal insertion order."""
        key = _key(source, network_id, device_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, projection_generation, membership_generation,
                       binding_generation, content_hash, state, allowed_operations,
                       provenance, outcome, created_at
                FROM (
                    SELECT sequence, projection_generation, membership_generation,
                           binding_generation, content_hash, state, allowed_operations,
                           provenance, outcome, created_at
                    FROM managed_projection_audit
                    WHERE source = ? AND network_id = ? AND device_id = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                )
                ORDER BY sequence
                """,
                (*key, self.audit_limit),
            ).fetchall()
        return tuple(
            {
                "sequence": row[0],
                "projection_generation": row[1],
                "membership_generation": row[2],
                "binding_generation": row[3],
                "content_hash": row[4],
                "state": row[5],
                "allowed_operations": _stored_operations(row[6]),
                "provenance": _stored_provenance(row[7], key),
                "outcome": row[8],
                "created_at": row[9],
            }
            for row in rows
        )

    def inspect(
        self, *, source: str, network_id: str, device_id: str
    ) -> dict[str, Any]:
        """Return generated and effective state for one exact managed resource."""
        key = _key(source, network_id, device_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT projection_generation, membership_generation, binding_generation,
                       content_hash, state, allowed_operations, provenance
                FROM managed_projections
                WHERE source = ? AND network_id = ? AND device_id = ?
                """,
                key,
            ).fetchone()
            denied_rows = connection.execute(
                """
                SELECT operation FROM operator_projection_denies
                WHERE source = ? AND network_id = ? AND device_id = ?
                ORDER BY operation
                """,
                key,
            ).fetchall()
        if row is None:
            return {"generated": None, "effective": None}

        operations = _stored_operations(row[5])
        provenance = _stored_provenance(row[6], key)
        generated = {
            "state": row[4],
            "projection_generation": row[0],
            "membership_generation": row[1],
            "binding_generation": row[2],
            "content_hash": row[3],
            "allowed_operations": operations,
            "provenance": provenance,
        }
        denied_operations = tuple(item[0] for item in denied_rows)
        # Defense in depth: effective grants require a currently active generated
        # record, an allowlisted persisted grant, and no local deny.
        effective_operations = (
            tuple(
                operation
                for operation in operations
                if operation not in denied_operations
            )
            if generated["state"] == "active"
            else ()
        )
        effective = {
            "state": generated["state"],
            "allowed_operations": effective_operations,
            "operator_denied_operations": denied_operations,
        }
        return {"generated": generated, "effective": effective}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection_id = id(connection)
        connection.create_function(
            "managed_projection_audit_trim_allowed",
            0,
            lambda: int(connection_id in self._audit_trim_connection_ids),
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        existing_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        if existing_tables and existing_tables != set(_CANONICAL_TABLE_SQL):
            raise RuntimeError("managed projection store schema is not ready")
        if not existing_tables:
            for sql in _CANONICAL_TABLE_SQL.values():
                connection.execute(sql)
            for sql in _CANONICAL_TRIGGER_SQL.values():
                connection.execute(sql)
            connection.execute(
                "INSERT INTO managed_projection_schema(schema_id) VALUES (?)",
                (_SCHEMA_ID,),
            )
        self._require_ready_schema(connection)

    def _require_ready_schema(self, connection: sqlite3.Connection) -> None:
        schema_rows = connection.execute(
            "SELECT schema_id FROM managed_projection_schema"
        ).fetchall()
        if schema_rows != [(_SCHEMA_ID,)]:
            raise RuntimeError("managed projection store schema is not ready")
        actual_tables = dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
        actual_triggers = dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        )
        if (
            set(actual_tables) != set(_CANONICAL_TABLE_SQL)
            or set(actual_triggers) != set(_CANONICAL_TRIGGER_SQL)
            or any(
                _normalized_sql(actual_tables.get(name)) != _normalized_sql(expected)
                for name, expected in _CANONICAL_TABLE_SQL.items()
            )
            or any(
                _normalized_sql(actual_triggers.get(name)) != _normalized_sql(expected)
                for name, expected in _CANONICAL_TRIGGER_SQL.items()
            )
        ):
            raise RuntimeError("managed projection store schema is not ready")

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        key: tuple[str, str, str],
        incoming: tuple[str, str, str, str, str, str, str],
        outcome: str,
    ) -> None:
        (
            projection_generation,
            membership_generation,
            binding_generation,
            content_hash,
            state,
            allowed_operations,
            provenance,
        ) = incoming
        connection.execute(
            """
            INSERT INTO managed_projection_audit(
                source, network_id, device_id, projection_generation,
                membership_generation, binding_generation, content_hash, state,
                allowed_operations, provenance, outcome
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *key,
                projection_generation,
                membership_generation,
                binding_generation,
                content_hash,
                state,
                allowed_operations,
                provenance,
                outcome,
            ),
        )
        self._trim_audit(connection, key)

    def _trim_audit(
        self, connection: sqlite3.Connection, key: tuple[str, str, str]
    ) -> None:
        """Retain the configured durable audit window through a private UDF gate."""
        connection_id = id(connection)
        self._audit_trim_connection_ids.add(connection_id)
        try:
            connection.execute(
                """
                DELETE FROM managed_projection_audit
                WHERE source = ? AND network_id = ? AND device_id = ?
                  AND sequence NOT IN (
                      SELECT sequence FROM managed_projection_audit
                      WHERE source = ? AND network_id = ? AND device_id = ?
                      ORDER BY sequence DESC
                      LIMIT ?
                  )
                """,
                (*key, *key, self.audit_limit),
            )
        finally:
            self._audit_trim_connection_ids.discard(connection_id)


def canonical_content_hash(document: object) -> str:
    """Return SHA-256 for the canonical complete projection material.

    The digest field is deliberately excluded from its own preimage.  Callers
    with a full parsed wire document can use this helper to recompute and verify
    the declared digest without changing legacy fixture-based ``apply`` calls.
    """
    material, _declared_hash = _canonical_document(document)
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def verify_canonical_content_hash(document: object) -> bool:
    """Return whether a full document's declared canonical SHA-256 is correct."""
    material, declared_hash = _canonical_document(document)
    if declared_hash is None:
        raise ValueError("wire document must include content_hash to verify it")
    actual_hash = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return hmac.compare_digest(declared_hash, actual_hash)


def _verify_wire_document(
    wire_document: object,
    *,
    source: str,
    network_id: str,
    device_id: str,
    projection_generation: str,
    membership_generation: str,
    binding_generation: str,
    content_hash: str,
    operation: str,
    generated_operations: tuple[str, ...],
    provenance_json: str,
) -> None:
    material, declared_hash = _canonical_document(wire_document)
    if declared_hash is not None and not hmac.compare_digest(
        declared_hash, content_hash
    ):
        raise ValueError("wire document content_hash does not match apply content_hash")
    expected = {
        "source": source,
        "network_id": network_id,
        "device_id": device_id,
        "projection_generation": projection_generation,
        "membership_generation": membership_generation,
        "binding_generation": binding_generation,
        "operation": operation,
        "generated_operations": list(generated_operations),
        "provenance": json.loads(provenance_json),
    }
    if material != expected:
        raise ValueError("wire document does not match the managed projection")
    actual_hash = canonical_content_hash(wire_document)
    if not hmac.compare_digest(content_hash, actual_hash):
        raise ValueError("content_hash does not match canonical wire document")


def _canonical_document(document: object) -> tuple[dict[str, object], str | None]:
    if type(document) is not dict:
        raise ValueError("wire document must be a mapping")
    fields = set(document)
    if fields not in (_DOCUMENT_FIELDS, _DOCUMENT_FIELDS | {"content_hash"}):
        raise ValueError("wire document has an invalid schema")
    key = _key(document["source"], document["network_id"], document["device_id"])
    operation = document["operation"]
    if operation not in {"upsert", "disable", "remove"}:
        raise ValueError("operation must be upsert, disable, or remove")
    material: dict[str, object] = {
        "source": key[0],
        "network_id": key[1],
        "device_id": key[2],
        "projection_generation": _generation(
            document["projection_generation"], "projection_generation"
        ),
        "membership_generation": _generation(
            document["membership_generation"], "membership_generation"
        ),
        "binding_generation": _generation(
            document["binding_generation"], "binding_generation"
        ),
        "operation": operation,
        "generated_operations": list(_operations(document["generated_operations"])),
        "provenance": json.loads(_provenance(document["provenance"], key)),
    }
    declared_hash = (
        _content_hash(document["content_hash"]) if "content_hash" in document else None
    )
    return material, declared_hash


def _key(source: object, network_id: object, device_id: object) -> tuple[str, str, str]:
    if source != "nodescale":
        raise ValueError("source must be nodescale")
    return (
        _identifier(source, "source"),
        _identifier(network_id, "network_id"),
        _identifier(device_id, "device_id"),
    )


def _identifier(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
    ):
        raise ValueError(f"{label} must be a bounded identifier")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _generation(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value == "0"
        or value[0] == "0"
        or any(character not in "0123456789" for character in value)
        or int(value) > _U64_MAX
    ):
        raise ValueError(f"{label} must be a canonical positive u64 string")
    return value


def _content_hash(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("content_hash must be a canonical SHA-256 hex string")
    return value


def _operations(value: object) -> tuple[str, ...]:
    if type(value) is tuple:
        items = value
    elif type(value) is list:
        items = value
    else:
        raise ValueError("generated_operations must be a bounded list or tuple")
    if len(items) > len(_GENERATED_ALLOWLIST):
        raise ValueError("generated_operations must be a bounded list or tuple")
    if not all(type(item) is str for item in items):
        raise ValueError("generated_operations must be a list or tuple of strings")
    if len(set(items)) != len(items):
        raise ValueError("generated_operations must not contain duplicates")
    if any(operation not in _GENERATED_ALLOWLIST for operation in items):
        raise ValueError("generated_operations contains an unsupported operation")
    return tuple(sorted(items))


def _provenance(value: object, key: tuple[str, str, str]) -> str:
    if type(value) is not dict:
        raise ValueError("provenance has an invalid closed schema")
    fields = set(value)
    if fields - _PROVENANCE_FIELDS or not _PROVENANCE_REQUIRED <= fields:
        raise ValueError("provenance has an invalid closed schema")
    identity = {"source": key[0], "network_id": key[1], "device_id": key[2]}
    if any(value.get(field) != expected for field, expected in identity.items()):
        raise ValueError("provenance identity must match the managed resource")
    if "snapshot" in value:
        _generation(value["snapshot"], "provenance snapshot")
    if "controller" in value and value["controller"] != key[0]:
        raise ValueError("provenance controller must match source")
    if any(type(item) is not str for item in value.values()):
        raise ValueError("provenance fields must be strings")
    if any(not _safe_provenance_text(item) for item in value.values()):
        raise ValueError("provenance contains unsafe data")
    return _canonical_json(value)


def _safe_provenance_text(value: str) -> bool:
    return (
        0 < len(value) <= 256
        and value == value.strip()
        and not any(
            character.isspace() or ord(character) < 32 or ord(character) > 126
            for character in value
        )
    )


def _stored_operations(value: object) -> tuple[str, ...]:
    if type(value) is not str:
        raise RuntimeError("managed projection store contains invalid grants")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "managed projection store contains invalid grants"
        ) from error
    try:
        operations = _operations(decoded)
    except ValueError as error:
        raise RuntimeError(
            "managed projection store contains invalid grants"
        ) from error
    if list(operations) != decoded:
        raise RuntimeError("managed projection store contains noncanonical grants")
    return operations


def _stored_provenance(value: object, key: tuple[str, str, str]) -> dict[str, str]:
    if type(value) is not str:
        raise RuntimeError("managed projection store contains invalid provenance")
    try:
        decoded = json.loads(value)
        encoded = _provenance(decoded, key)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "managed projection store contains invalid provenance"
        ) from error
    if encoded != value:
        raise RuntimeError("managed projection store contains noncanonical provenance")
    return decoded


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("value must be canonical JSON data") from error


def _normalized_sql(value: object) -> str:
    if type(value) is not str:
        return ""
    return "".join(value.lower().split())


def _require_safe_database_parent(database_path: Path) -> None:
    """Require a service-owned private parent without resolving symlink components."""
    parent_identity = _require_nonsymlink_directory_components(
        database_path.parent, "managed projection database parent"
    )
    mode = stat.S_IMODE(parent_identity.st_mode)
    if (
        parent_identity.st_uid != os.geteuid()
        or mode & 0o077
        or mode & 0o700 != 0o700
    ):
        raise ValueError("unsafe managed projection database parent")


def _prepare_database_file(database_path: Path) -> None:
    """Create or tighten only a regular service-owned Fleet database file."""
    try:
        identity = database_path.lstat()
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(database_path, flags, 0o600)
        except FileExistsError:
            identity = database_path.lstat()
        else:
            try:
                identity = os.fstat(descriptor)
            finally:
                os.close(descriptor)
    except OSError as error:
        raise ValueError("unsafe managed projection database path") from error
    if (
        stat.S_ISLNK(identity.st_mode)
        or not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.geteuid()
    ):
        raise ValueError("unsafe managed projection database path")
    os.chmod(database_path, 0o600, follow_symlinks=False)
    identity = database_path.lstat()
    if (
        stat.S_ISLNK(identity.st_mode)
        or not stat.S_ISREG(identity.st_mode)
        or identity.st_uid != os.geteuid()
        or stat.S_IMODE(identity.st_mode) != 0o600
    ):
        raise ValueError("unsafe managed projection database path")


def _require_nonsymlink_directory_components(path: Path, label: str) -> os.stat_result:
    """Lstat every lexical component without resolving an attacker-controlled link."""
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    normalized = Path(os.path.abspath(os.fspath(path)))
    current = Path(normalized.anchor)
    try:
        identity = current.lstat()
        for component in normalized.parts[1:]:
            current /= component
            identity = current.lstat()
            if stat.S_ISLNK(identity.st_mode) or not stat.S_ISDIR(identity.st_mode):
                raise ValueError(f"unsafe {label}")
    except FileNotFoundError as error:
        raise ValueError(f"{label} must exist") from error
    except OSError as error:
        raise ValueError(f"unsafe {label}") from error
    return identity
