"""Phase 9 formal principal identity, binding, revocation and transport resolution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from .managed_projection import ManagedProjectionStore

PRINCIPAL_OWNER: Final[str] = "owner"
PRINCIPAL_PROJECT: Final[str] = "project"
PRINCIPAL_NETWORK: Final[str] = "network"
PRINCIPAL_DEVICE: Final[str] = "device"
PRINCIPAL_SERVICE: Final[str] = "service"
PRINCIPAL_KINDS: Final[frozenset[str]] = frozenset(
    {
        PRINCIPAL_OWNER,
        PRINCIPAL_PROJECT,
        PRINCIPAL_NETWORK,
        PRINCIPAL_DEVICE,
        PRINCIPAL_SERVICE,
    }
)

SOURCE_LOCAL_PEER: Final[str] = "local-peer"
SOURCE_KERYX_NODESCALE: Final[str] = "keryx-nodescale"
SOURCE_SCOPED_PARENT: Final[str] = "scoped-parent"
_BINDING_SOURCES: Final[frozenset[str]] = frozenset(
    {SOURCE_LOCAL_PEER, SOURCE_KERYX_NODESCALE, SOURCE_SCOPED_PARENT}
)

_SCHEMA_ID: Final[str] = "fleet.principal-store.v1"
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,511}$")
_SCOPE_KEYS = frozenset({"owner", "project", "network", "device", "service"})
_MAX_JSON_BYTES = 128 * 1024


class PrincipalError(RuntimeError):
    """Principal identity is malformed, stale, revoked, or unprovable."""


class PrincipalConflict(PrincipalError):
    """A durable principal generation or exact identity binding changed."""


class PrincipalRevoked(PrincipalError):
    """The principal is explicitly revoked."""


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise PrincipalError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or type(value) is not int or value <= 0:
        raise PrincipalError(f"{label} is invalid")
    return value


def _generation_text(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value == "0"
        or value.startswith("0")
        or not value.isascii()
        or not value.isdigit()
        or int(value) > (1 << 64) - 1
    ):
        raise PrincipalError(f"{label} is invalid")
    return value


def _canonical(value: object, label: str) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise PrincipalError(f"{label} is not canonical JSON") from error
    if len(payload) > _MAX_JSON_BYTES:
        raise PrincipalError(f"{label} exceeds its byte bound")
    return payload


def _digest(value: object, label: str) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value, label)).hexdigest()


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise PrincipalError(f"{label} is invalid")
    return value


def _plain_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PrincipalError(f"{label} is invalid")
    document = json.loads(_canonical(dict(value), label).decode())
    return MappingProxyType(document)


@dataclass(frozen=True, slots=True)
class PrincipalDefinition:
    kind: str
    subject: str
    scope: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.kind not in PRINCIPAL_KINDS:
            raise PrincipalError("principal kind is invalid")
        _identifier(self.subject, "principal subject")
        if not isinstance(self.scope, Mapping) or not self.scope:
            raise PrincipalError("principal scope is invalid")
        if set(self.scope) - _SCOPE_KEYS:
            raise PrincipalError("principal scope contains unsupported fields")
        normalized: dict[str, str] = {}
        for key, value in self.scope.items():
            normalized[key] = _identifier(value, f"principal {key} scope")
        required = {
            PRINCIPAL_OWNER: {"owner"},
            PRINCIPAL_PROJECT: {"project"},
            PRINCIPAL_NETWORK: {"network"},
            PRINCIPAL_DEVICE: {"network", "device"},
            PRINCIPAL_SERVICE: {"service"},
        }[self.kind]
        if not required.issubset(normalized):
            raise PrincipalError("principal scope is incomplete for its kind")
        object.__setattr__(
            self, "scope", MappingProxyType(dict(sorted(normalized.items())))
        )
        _canonical(self.to_dict(), "principal definition")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "scope": dict(self.scope),
        }

    @property
    def principal_id(self) -> str:
        return _digest(self.to_dict(), "principal definition")


def _validate_binding_evidence(source: str, evidence: Mapping[str, Any]) -> None:
    if source == SOURCE_LOCAL_PEER:
        if set(evidence) != {"machine_id", "uid"}:
            raise PrincipalError("local principal binding evidence is invalid")
        _identifier(evidence["machine_id"], "local machine id")
        uid = evidence["uid"]
        if isinstance(uid, bool) or type(uid) is not int or uid < 0:
            raise PrincipalError("local principal uid is invalid")
        return
    if source == SOURCE_KERYX_NODESCALE:
        expected = {
            "keryx_peer_id",
            "nodescale_device_id",
            "nodescale_network_id",
            "nodescale_provider_kind",
            "nodescale_provider_instance_id",
            "nodescale_provider_node_id",
            "observed_id",
            "durable_trust_revision",
            "provider_binding_revision",
            "keryx_binding_id",
            "keryx_binding_revision",
            "credential_generation",
            "keryx_binding_generation",
        }
        if set(evidence) != expected:
            raise PrincipalError("remote principal binding evidence is invalid")
        for key in (
            "keryx_peer_id",
            "nodescale_device_id",
            "nodescale_network_id",
            "nodescale_provider_kind",
            "nodescale_provider_instance_id",
            "nodescale_provider_node_id",
            "keryx_binding_id",
        ):
            _identifier(evidence[key], f"remote principal {key}")
        _hash(evidence["observed_id"], "remote principal observation id")
        for key in (
            "durable_trust_revision",
            "provider_binding_revision",
            "keryx_binding_revision",
            "credential_generation",
            "keryx_binding_generation",
        ):
            _positive_int(evidence[key], f"remote principal {key}")
        return
    if source == SOURCE_SCOPED_PARENT:
        expected = {
            "parent_principal_id",
            "parent_generation",
            "parent_binding_hash",
            "parent_kind",
        }
        if set(evidence) != expected:
            raise PrincipalError("scoped principal binding evidence is invalid")
        _hash(evidence["parent_principal_id"], "parent principal id")
        _positive_int(evidence["parent_generation"], "parent principal generation")
        _hash(evidence["parent_binding_hash"], "parent principal binding hash")
        if evidence["parent_kind"] not in PRINCIPAL_KINDS:
            raise PrincipalError("parent principal kind is invalid")
        return
    raise PrincipalError("principal binding source is invalid")


@dataclass(frozen=True, slots=True)
class PrincipalBinding:
    source: str
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.source not in _BINDING_SOURCES:
            raise PrincipalError("principal binding source is invalid")
        normalized = _plain_mapping(self.evidence, "principal binding evidence")
        _validate_binding_evidence(self.source, normalized)
        object.__setattr__(self, "evidence", normalized)
        _canonical(self.to_dict(), "principal binding")

    def to_dict(self) -> dict[str, object]:
        return {"source": self.source, "evidence": dict(self.evidence)}

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict(), "principal binding")


@dataclass(frozen=True, slots=True)
class PrincipalReference:
    principal_id: str
    kind: str
    generation: int
    binding_hash: str

    def __post_init__(self) -> None:
        _hash(self.principal_id, "principal id")
        if self.kind not in PRINCIPAL_KINDS:
            raise PrincipalError("principal reference kind is invalid")
        _positive_int(self.generation, "principal generation")
        _hash(self.binding_hash, "principal binding hash")

    @classmethod
    def from_dict(cls, value: object) -> PrincipalReference:
        if type(value) is not dict or set(value) != {
            "principal_id",
            "kind",
            "generation",
            "binding_hash",
        }:
            raise PrincipalError("principal reference shape is invalid")
        return cls(
            principal_id=value["principal_id"],
            kind=value["kind"],
            generation=value["generation"],
            binding_hash=value["binding_hash"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "principal_id": self.principal_id,
            "kind": self.kind,
            "generation": self.generation,
            "binding_hash": self.binding_hash,
        }


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    definition: PrincipalDefinition
    binding: PrincipalBinding
    generation: int
    state: str
    created_at_ms: int
    updated_at_ms: int

    def __post_init__(self) -> None:
        if type(self.definition) is not PrincipalDefinition:
            raise PrincipalError("principal definition is invalid")
        if type(self.binding) is not PrincipalBinding:
            raise PrincipalError("principal binding is invalid")
        _positive_int(self.generation, "principal generation")
        if self.state not in {"active", "revoked"}:
            raise PrincipalError("principal state is invalid")
        _positive_int(self.created_at_ms, "principal created timestamp")
        _positive_int(self.updated_at_ms, "principal updated timestamp")
        if self.updated_at_ms < self.created_at_ms:
            raise PrincipalError("principal timestamps are invalid")

    @property
    def reference(self) -> PrincipalReference:
        return PrincipalReference(
            principal_id=self.definition.principal_id,
            kind=self.definition.kind,
            generation=self.generation,
            binding_hash=self.binding.content_hash,
        )


_TABLE_SQL = """
CREATE TABLE principals (
    principal_id TEXT PRIMARY KEY,
    definition_json TEXT NOT NULL CHECK(json_valid(definition_json)),
    binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    state TEXT NOT NULL CHECK(state IN ('active','revoked')),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
)
"""
_SCHEMA_SQL = "CREATE TABLE principal_schema (schema_id TEXT PRIMARY KEY)"


class PrincipalRegistry:
    """Durable Fleet-owned principal records with generation-fenced revocation."""

    def __init__(
        self,
        path: str | Path,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.is_absolute() or not self.path.name:
            raise PrincipalError("principal store path must be absolute")
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        if not callable(self._now_ms):
            raise PrincipalError("principal store clock is invalid")
        self._prepare_file()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._initialize(connection)

    def ensure(
        self,
        definition: PrincipalDefinition,
        binding: PrincipalBinding,
    ) -> tuple[PrincipalRecord, bool]:
        if (
            type(definition) is not PrincipalDefinition
            or type(binding) is not PrincipalBinding
        ):
            raise PrincipalError("principal admission input is invalid")
        now = self._now()
        principal_id = definition.principal_id
        if (
            binding.source == SOURCE_SCOPED_PARENT
            and binding.evidence["parent_principal_id"] == principal_id
        ):
            raise PrincipalError("principal cannot derive identity from itself")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, principal_id)
            if row is not None:
                record = self._row_to_record(row)
                if record.definition != definition:
                    raise PrincipalConflict("principal definition changed")
                if record.state == "revoked":
                    raise PrincipalRevoked("principal is revoked")
                if record.binding.content_hash != binding.content_hash:
                    raise PrincipalConflict(
                        "principal binding changed; explicit rebind required"
                    )
                return record, False
            connection.execute(
                """
                INSERT INTO principals(
                    principal_id, definition_json, binding_json, generation, state,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, 1, 'active', ?, ?)
                """,
                (
                    principal_id,
                    _canonical(definition.to_dict(), "principal definition").decode(),
                    _canonical(binding.to_dict(), "principal binding").decode(),
                    now,
                    now,
                ),
            )
            row = self._select(connection, principal_id)
            if row is None:
                raise PrincipalError("principal admission could not be observed")
            return self._row_to_record(row), True

    def get(self, principal_id: str) -> PrincipalRecord | None:
        _hash(principal_id, "principal id")
        with self._connect() as connection:
            row = self._select(connection, principal_id)
        return None if row is None else self._row_to_record(row)

    def require_current(self, reference: PrincipalReference) -> PrincipalRecord:
        return self._require_current(reference, visited=frozenset())

    def _require_current(
        self,
        reference: PrincipalReference,
        *,
        visited: frozenset[str],
    ) -> PrincipalRecord:
        if type(reference) is not PrincipalReference:
            raise PrincipalError("principal reference is invalid")
        if reference.principal_id in visited:
            raise PrincipalConflict("principal binding dependency contains a cycle")
        record = self.get(reference.principal_id)
        if record is None:
            raise PrincipalError("principal is unavailable")
        if record.state == "revoked":
            raise PrincipalRevoked("principal is revoked")
        if record.reference != reference:
            raise PrincipalConflict("principal reference is stale")
        if record.binding.source == SOURCE_SCOPED_PARENT:
            evidence = record.binding.evidence
            parent = PrincipalReference(
                principal_id=evidence["parent_principal_id"],
                kind=evidence["parent_kind"],
                generation=evidence["parent_generation"],
                binding_hash=evidence["parent_binding_hash"],
            )
            self._require_current(
                parent,
                visited=visited | {reference.principal_id},
            )
        return record

    def rebind(
        self,
        reference: PrincipalReference,
        binding: PrincipalBinding,
    ) -> PrincipalRecord:
        if type(binding) is not PrincipalBinding:
            raise PrincipalError("principal binding is invalid")
        record = self.require_current(reference)
        if record.binding.content_hash == binding.content_hash:
            return record
        return self._transition(
            record,
            expected_generation=reference.generation,
            binding=binding,
            state="active",
        )

    def revoke(self, reference: PrincipalReference) -> PrincipalRecord:
        record = self.require_current(reference)
        return self._transition(
            record,
            expected_generation=reference.generation,
            binding=record.binding,
            state="revoked",
        )

    def _transition(
        self,
        record: PrincipalRecord,
        *,
        expected_generation: int,
        binding: PrincipalBinding,
        state: str,
    ) -> PrincipalRecord:
        _positive_int(expected_generation, "principal expected generation")
        now = self._now()
        principal_id = record.definition.principal_id
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select(connection, principal_id)
            if row is None:
                raise PrincipalError("principal is unavailable")
            current = self._row_to_record(row)
            if current.generation != expected_generation:
                raise PrincipalConflict("principal generation changed")
            if current.reference != record.reference:
                raise PrincipalConflict("principal state changed")
            generation = current.generation + 1
            result = connection.execute(
                """
                UPDATE principals
                SET binding_json = ?, generation = ?, state = ?, updated_at_ms = ?
                WHERE principal_id = ? AND generation = ?
                """,
                (
                    _canonical(binding.to_dict(), "principal binding").decode(),
                    generation,
                    state,
                    now,
                    principal_id,
                    expected_generation,
                ),
            )
            if result.rowcount != 1:
                raise PrincipalConflict("principal generation changed")
            row = self._select(connection, principal_id)
            if row is None:
                raise PrincipalError("principal transition could not be observed")
            return self._row_to_record(row)

    def _prepare_file(self) -> None:
        parent = self.path.parent
        if not parent.is_absolute():
            raise PrincipalError("principal store parent is invalid")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_info = parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise PrincipalError("principal store parent is unsafe")
        if parent_info.st_uid != os.geteuid():
            raise PrincipalError("principal store parent ownership is unsafe")
        if parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PrincipalError("principal store parent permissions are unsafe")
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise PrincipalError("principal store file is unsafe")
            if info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise PrincipalError("principal store ownership is unsafe")
            self.path.chmod(0o600)
        else:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        self.path.chmod(0o600)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self, connection: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        expected = {"principal_schema", "principals"}
        if tables and tables != expected:
            raise PrincipalError("principal store schema is not ready")
        if not tables:
            connection.execute(_SCHEMA_SQL)
            connection.execute(_TABLE_SQL)
            connection.execute(
                "INSERT INTO principal_schema(schema_id) VALUES (?)", (_SCHEMA_ID,)
            )
        schema = connection.execute("SELECT schema_id FROM principal_schema").fetchall()
        if schema != [(_SCHEMA_ID,)]:
            raise PrincipalError("principal store schema is not ready")

    @staticmethod
    def _select(
        connection: sqlite3.Connection, principal_id: str
    ) -> tuple[Any, ...] | None:
        return connection.execute(
            """
            SELECT principal_id, definition_json, binding_json, generation, state,
                   created_at_ms, updated_at_ms
            FROM principals WHERE principal_id = ?
            """,
            (principal_id,),
        ).fetchone()

    @staticmethod
    def _row_to_record(row: tuple[Any, ...]) -> PrincipalRecord:
        try:
            definition_doc = json.loads(row[1])
            binding_doc = json.loads(row[2])
            if type(definition_doc) is not dict or set(definition_doc) != {
                "kind",
                "subject",
                "scope",
            }:
                raise PrincipalError("persisted principal definition is invalid")
            if type(binding_doc) is not dict or set(binding_doc) != {
                "source",
                "evidence",
            }:
                raise PrincipalError("persisted principal binding is invalid")
            definition = PrincipalDefinition(
                kind=definition_doc["kind"],
                subject=definition_doc["subject"],
                scope=definition_doc["scope"],
            )
            if definition.principal_id != row[0]:
                raise PrincipalConflict("persisted principal id changed")
            binding = PrincipalBinding(
                source=binding_doc["source"], evidence=binding_doc["evidence"]
            )
            return PrincipalRecord(
                definition=definition,
                binding=binding,
                generation=row[3],
                state=row[4],
                created_at_ms=row[5],
                updated_at_ms=row[6],
            )
        except PrincipalError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise PrincipalError("persisted principal record is invalid") from error

    def _now(self) -> int:
        value = self._now_ms()
        return _positive_int(value, "principal store timestamp")


@dataclass(frozen=True, slots=True)
class LocalPeerContext:
    uid: int

    def __post_init__(self) -> None:
        if isinstance(self.uid, bool) or type(self.uid) is not int or self.uid < 0:
            raise PrincipalError("local peer uid is invalid")


_current_local_peer: ContextVar[LocalPeerContext | None] = ContextVar(
    "fleet_local_peer_identity", default=None
)


def get_local_peer_context() -> LocalPeerContext | None:
    return _current_local_peer.get()


@contextmanager
def local_peer_scope(uid: int) -> Iterator[None]:
    token: Token[LocalPeerContext | None] = _current_local_peer.set(
        LocalPeerContext(uid)
    )
    try:
        yield
    finally:
        _current_local_peer.reset(token)


class LocalPrincipalResolver:
    """Resolve same-machine principal identity from kernel-authenticated peer UID."""

    def __init__(
        self, registry: PrincipalRegistry, *, machine_id: str, allowed_uid: int
    ) -> None:
        if type(registry) is not PrincipalRegistry:
            raise PrincipalError("principal registry is invalid")
        self._registry = registry
        self._machine_id = _identifier(machine_id, "local machine id")
        if (
            isinstance(allowed_uid, bool)
            or type(allowed_uid) is not int
            or allowed_uid < 0
        ):
            raise PrincipalError("allowed local uid is invalid")
        self._allowed_uid = allowed_uid

    def resolve_owner(self, peer_uid: int | None = None) -> PrincipalReference:
        if peer_uid is None:
            context = get_local_peer_context()
            if context is None:
                raise PrincipalError(
                    "local principal has no authenticated peer context"
                )
            peer_uid = context.uid
        if peer_uid != self._allowed_uid:
            raise PrincipalError("local peer uid is not authorized for this principal")
        subject = f"{self._machine_id}:uid:{peer_uid}"
        definition = PrincipalDefinition(
            kind=PRINCIPAL_OWNER,
            subject=subject,
            scope={"owner": subject},
        )
        binding = PrincipalBinding(
            source=SOURCE_LOCAL_PEER,
            evidence={"machine_id": self._machine_id, "uid": peer_uid},
        )
        record, _ = self._registry.ensure(definition, binding)
        return record.reference


@dataclass(frozen=True, slots=True)
class RemoteDeviceBinding:
    """Minimal selector for a remote principal; all trust facts are derived."""

    nodescale_device_id: str

    def __post_init__(self) -> None:
        _identifier(self.nodescale_device_id, "Nodescale device id")


class RemotePrincipalResolver:
    """Resolve a remote device principal from existing Keryx/Nodescale trust roots."""

    def __init__(
        self,
        registry: PrincipalRegistry,
        projections: ManagedProjectionStore,
        observation_overview: Callable[[], Mapping[str, Any]],
        operator_device_inspector: Callable[[str], Mapping[str, Any]],
    ) -> None:
        if type(registry) is not PrincipalRegistry:
            raise PrincipalError("principal registry is invalid")
        if type(projections) is not ManagedProjectionStore:
            raise PrincipalError("managed projection store is invalid")
        if not callable(observation_overview):
            raise PrincipalError("Nodescale observation source is invalid")
        if not callable(operator_device_inspector):
            raise PrincipalError("Nodescale operator device source is invalid")
        self._registry = registry
        self._projections = projections
        self._observation_overview = observation_overview
        self._operator_device_inspector = operator_device_inspector

    def resolve_device(
        self,
        *,
        authenticated_sender: str,
        binding: RemoteDeviceBinding,
    ) -> PrincipalReference:
        _identifier(authenticated_sender, "authenticated Keryx sender")
        if type(binding) is not RemoteDeviceBinding:
            raise PrincipalError("remote device binding is invalid")

        operator = self._operator_device_inspector(binding.nodescale_device_id)
        if not isinstance(operator, Mapping):
            raise PrincipalError("Nodescale operator device evidence is unavailable")
        required_operator = {
            "device_id",
            "network_id",
            "membership_state",
            "credential_generation",
            "keryx_binding_generation",
            "fleet_projection_generation",
            "fleet_projection_status",
            "provider_instance_id",
            "provider_node_id",
            "durable_trust_state",
            "durable_trust_revision",
            "provider_binding_state",
            "provider_binding_revision",
            "keryx_binding_id",
            "keryx_binding_state",
            "verified_keryx_peer_id",
            "keryx_binding_revision",
            "revoked_at",
        }
        if not required_operator.issubset(operator):
            raise PrincipalError("Nodescale operator device identity is incomplete")
        if (
            operator.get("device_id") != binding.nodescale_device_id
            or operator.get("membership_state") != "active"
            or operator.get("durable_trust_state") != "trusted"
            or operator.get("provider_binding_state") != "active"
            or operator.get("keryx_binding_state") != "active"
            or operator.get("fleet_projection_status") != "applied"
            or operator.get("revoked_at") is not None
        ):
            raise PrincipalError("Nodescale operator device is not currently trusted")
        if operator.get("verified_keryx_peer_id") != authenticated_sender:
            raise PrincipalError(
                "authenticated Keryx sender is not bound to the Nodescale device"
            )
        for key in (
            "credential_generation",
            "keryx_binding_generation",
            "fleet_projection_generation",
            "durable_trust_revision",
            "provider_binding_revision",
            "keryx_binding_revision",
        ):
            _positive_int(operator.get(key), f"Nodescale operator {key}")
        for key in (
            "network_id",
            "provider_instance_id",
            "provider_node_id",
            "keryx_binding_id",
        ):
            _identifier(operator.get(key), f"Nodescale operator {key}")

        network_id = operator["network_id"]
        provider_instance_id = operator["provider_instance_id"]
        provider_node_id = operator["provider_node_id"]

        overview = self._observation_overview()
        if not isinstance(overview, Mapping):
            raise PrincipalError("Nodescale observation evidence is unavailable")
        if (
            overview.get("schema") != "nodescale.observations.v1"
            or overview.get("network_id") != network_id
        ):
            raise PrincipalConflict("Nodescale network identity changed")
        reconciliation = overview.get("reconciliation")
        if (
            not isinstance(reconciliation, Mapping)
            or reconciliation.get("state") != "healthy"
        ):
            raise PrincipalError("Nodescale reconciliation is not healthy")
        observations = overview.get("observations")
        if type(observations) is not list:
            raise PrincipalError("Nodescale observations are invalid")
        matches = [
            item
            for item in observations
            if isinstance(item, Mapping)
            and item.get("provider_node_id") == provider_node_id
        ]
        if len(matches) != 1:
            raise PrincipalError("exact Nodescale device observation is unavailable")
        observed = matches[0]
        if (
            observed.get("network_id") != network_id
            or observed.get("provider_instance_id") != provider_instance_id
            or observed.get("classification") != "active"
            or observed.get("expired") is not False
            or observed.get("online") is not True
        ):
            raise PrincipalConflict("Nodescale device observation changed")
        observed_id = observed.get("observed_id")
        _hash(observed_id, "Nodescale observation id")
        provider_kind = _identifier(
            observed.get("provider_kind"), "Nodescale provider kind"
        )

        projection = self._projections.inspect(
            source="nodescale",
            network_id=network_id,
            device_id=binding.nodescale_device_id,
        )
        if type(projection) is not dict:
            raise PrincipalError("managed projection identity is unavailable")
        generated = projection.get("generated")
        effective = projection.get("effective")
        if type(generated) is not dict or type(effective) is not dict:
            raise PrincipalError("managed projection identity is incomplete")
        provenance = generated.get("provenance")
        if (
            generated.get("state") != "active"
            or effective.get("state") != "active"
            or type(provenance) is not dict
            or provenance.get("source") != "nodescale"
            or provenance.get("network_id") != network_id
            or provenance.get("device_id") != binding.nodescale_device_id
        ):
            raise PrincipalConflict("managed device identity changed")

        try:
            projection_generation = int(generated["projection_generation"])
            membership_generation = int(generated["membership_generation"])
            binding_generation = int(generated["binding_generation"])
        except (KeyError, TypeError, ValueError) as error:
            raise PrincipalError(
                "managed projection generations are invalid"
            ) from error
        if (
            projection_generation != operator["fleet_projection_generation"]
            or membership_generation != operator["credential_generation"]
            or binding_generation != operator["keryx_binding_generation"]
        ):
            raise PrincipalConflict("managed device authority epoch changed")

        subject = f"{network_id}/{binding.nodescale_device_id}"
        definition = PrincipalDefinition(
            kind=PRINCIPAL_DEVICE,
            subject=subject,
            scope={
                "network": network_id,
                "device": binding.nodescale_device_id,
            },
        )
        principal_binding = PrincipalBinding(
            source=SOURCE_KERYX_NODESCALE,
            evidence={
                "keryx_peer_id": authenticated_sender,
                "nodescale_device_id": binding.nodescale_device_id,
                "nodescale_network_id": network_id,
                "nodescale_provider_kind": provider_kind,
                "nodescale_provider_instance_id": provider_instance_id,
                "nodescale_provider_node_id": provider_node_id,
                "observed_id": observed_id,
                "durable_trust_revision": operator["durable_trust_revision"],
                "provider_binding_revision": operator["provider_binding_revision"],
                "keryx_binding_id": operator["keryx_binding_id"],
                "keryx_binding_revision": operator["keryx_binding_revision"],
                "credential_generation": operator["credential_generation"],
                "keryx_binding_generation": operator["keryx_binding_generation"],
            },
        )
        record, _ = self._registry.ensure(definition, principal_binding)
        return record.reference


def derive_scoped_principal(
    registry: PrincipalRegistry,
    *,
    parent: PrincipalReference,
    kind: str,
    subject: str,
    scope: Mapping[str, str],
) -> PrincipalReference:
    """Create identity scope only; this function grants no authority."""
    if type(registry) is not PrincipalRegistry:
        raise PrincipalError("principal registry is invalid")
    if kind not in {PRINCIPAL_PROJECT, PRINCIPAL_NETWORK, PRINCIPAL_SERVICE}:
        raise PrincipalError("scoped principal kind is unsupported")
    parent_record = registry.require_current(parent)
    definition = PrincipalDefinition(kind=kind, subject=subject, scope=scope)
    binding = PrincipalBinding(
        source=SOURCE_SCOPED_PARENT,
        evidence={
            "parent_principal_id": parent.principal_id,
            "parent_generation": parent.generation,
            "parent_binding_hash": parent.binding_hash,
            "parent_kind": parent_record.definition.kind,
        },
    )
    record, _ = registry.ensure(definition, binding)
    return record.reference
