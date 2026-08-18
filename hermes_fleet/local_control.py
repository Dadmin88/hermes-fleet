"""Authenticated Linux-only local control for Nodescale-managed Fleet state.

The Unix credential is the only authentication input.  JSON is deliberately a
closed, credential-free protocol and cannot select an identity or privilege.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import stat
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from .managed_projection import ManagedProjectionStore
from .principal_identity import local_peer_scope

SCHEMA = "fleet.managed-projection.v1"
MAX_FRAME_BYTES = 32_768
IO_TIMEOUT_SECONDS = 2.0
MAX_CONNECTIONS = 32


class LocalControlProtocolError(ValueError):
    """Untrusted local-control input did not satisfy the closed protocol."""


def _reject_number(_value: str) -> None:
    raise LocalControlProtocolError("JSON numbers are not permitted")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise LocalControlProtocolError("duplicate or invalid JSON object key")
        result[key] = value
    return result


def _object(value: object, *, keys: frozenset[str], label: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise LocalControlProtocolError(f"{label} has an invalid schema")
    return value


def _string(value: object, *, label: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise LocalControlProtocolError(f"{label} must be a bounded nonempty string")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise LocalControlProtocolError(f"{label} contains prohibited characters")
    return value


def _apply_document(value: object) -> dict[str, object]:
    document = _object(
        value,
        keys=frozenset(
            {
                "source",
                "network_id",
                "device_id",
                "projection_generation",
                "membership_generation",
                "binding_generation",
                "content_hash",
                "operation",
                "generated_operations",
                "provenance",
            }
        ),
        label="apply document",
    )
    for key in (
        "source",
        "network_id",
        "device_id",
        "projection_generation",
        "membership_generation",
        "binding_generation",
        "content_hash",
        "operation",
    ):
        _string(document[key], label=f"apply document {key}")
    generated_operations = document["generated_operations"]
    if type(generated_operations) is not list or not all(
        type(operation) is str for operation in generated_operations
    ):
        raise LocalControlProtocolError("generated_operations must be a string list")
    provenance = _object(
        document["provenance"],
        keys=frozenset({"source", "network_id", "device_id", "snapshot"}),
        label="provenance",
    )
    for key in ("source", "network_id", "device_id", "snapshot"):
        _string(provenance[key], label=f"provenance {key}")
    for key in ("source", "network_id", "device_id"):
        if provenance[key] != document[key]:
            raise LocalControlProtocolError(
                "provenance identity does not match document"
            )
    return document


def _inspect_selector(value: object) -> dict[str, object]:
    selector = _object(
        value,
        keys=frozenset({"source", "network_id", "device_id"}),
        label="inspect selector",
    )
    for key in ("source", "network_id", "device_id"):
        _string(selector[key], label=f"inspect selector {key}")
    return selector


def parse_request(payload: bytes) -> tuple[str, dict[str, object] | None]:
    """Parse one bounded closed request, rejecting duplicate keys and numbers."""
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise LocalControlProtocolError("invalid request frame")
    try:
        decoded = payload.decode("utf-8")
        request = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_int=_reject_number,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise LocalControlProtocolError("invalid JSON request") from error
    if type(request) is not dict:
        raise LocalControlProtocolError("request must be an object")
    schema = request.get("schema")
    kind = request.get("kind")
    if schema != SCHEMA or type(kind) is not str:
        raise LocalControlProtocolError("unsupported request schema")
    if kind == "capabilities":
        _object(
            request,
            keys=frozenset({"schema", "kind"}),
            label="capabilities request",
        )
        return kind, None
    if kind == "apply":
        parsed = _object(
            request,
            keys=frozenset({"schema", "kind", "document"}),
            label="apply request",
        )
        return kind, _apply_document(parsed["document"])
    if kind == "inspect":
        parsed = _object(
            request,
            keys=frozenset({"schema", "kind", "selector"}),
            label="inspect request",
        )
        return kind, _inspect_selector(parsed["selector"])
    raise LocalControlProtocolError("unsupported request kind")


def _safe_json_value(value: object) -> object:
    """Keep response values typed and avoid a number-bearing or exotic JSON surface."""
    if value is None or type(value) in (str, bool):
        return value
    if type(value) in (list, tuple):
        return [_safe_json_value(item) for item in value]
    if type(value) is dict:
        if not all(type(key) is str for key in value):
            raise LocalControlProtocolError("control result has invalid object keys")
        return {key: _safe_json_value(item) for key, item in value.items()}
    raise LocalControlProtocolError("control result contains unsupported value")


class LocalControlServer:
    """Fleet UDS control with group transport permissions and exact UID authentication.

    ``socket_gid`` permits a distinct Nodescale service to connect through a
    group-writable socket; it never changes the exact ``SO_PEERCRED`` UID check.
    """

    def __init__(
        self,
        *,
        socket_path: Path,
        allowed_uid: int,
        managed_projection: ManagedProjectionStore,
        socket_gid: int | None = None,
        io_timeout_seconds: float = IO_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(socket_path, Path) or not socket_path.is_absolute():
            raise ValueError("socket_path must be an absolute Path")
        if type(allowed_uid) is not int or allowed_uid < 0:
            raise ValueError("allowed_uid must be a non-negative integer")
        if socket_gid is not None and (type(socket_gid) is not int or socket_gid < 0):
            raise ValueError("socket_gid must be a non-negative integer or None")
        if managed_projection is None:
            raise ValueError("managed_projection is required")
        if type(io_timeout_seconds) is not float or not 0 < io_timeout_seconds <= 2.0:
            raise ValueError("io_timeout_seconds must be between zero and two seconds")
        self.socket_path = socket_path
        self.allowed_uid = allowed_uid
        self.socket_gid = socket_gid
        self._managed_projection = managed_projection
        self._io_timeout_seconds = io_timeout_seconds
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._owned_identity: tuple[int, int] | None = None
        self._workers: ThreadPoolExecutor | None = None
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._accept_thread: threading.Thread | None = None

    def start(self) -> None:
        """Atomically bind a private socket after rejecting every pre-existing path."""
        if self._listener is not None:
            raise RuntimeError("local control server is already started")
        _require_safe_socket_parent(self.socket_path, self.socket_gid)
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ValueError("unsafe local-control socket path") from error
        else:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISSOCK(existing.st_mode):
                raise ValueError("unsafe local-control socket path")
            raise ValueError("unsafe existing local-control socket path")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(os.fspath(self.socket_path))
            identity = self.socket_path.lstat()
            if not stat.S_ISSOCK(identity.st_mode) or identity.st_uid != os.geteuid():
                raise ValueError("unsafe local-control socket path")
            self._owned_identity = (identity.st_dev, identity.st_ino)
            if self.socket_gid is None:
                os.chmod(self.socket_path, 0o600, follow_symlinks=False)
            else:
                os.chown(
                    self.socket_path,
                    -1,
                    self.socket_gid,
                    follow_symlinks=False,
                )
                os.chmod(self.socket_path, 0o660, follow_symlinks=False)
            identity = self.socket_path.lstat()
            expected_mode = 0o600 if self.socket_gid is None else 0o660
            if (
                not stat.S_ISSOCK(identity.st_mode)
                or (identity.st_dev, identity.st_ino) != self._owned_identity
                or identity.st_uid != os.geteuid()
                or stat.S_IMODE(identity.st_mode) != expected_mode
                or (self.socket_gid is not None and identity.st_gid != self.socket_gid)
            ):
                raise ValueError("unsafe local-control socket path")
            listener.listen(MAX_CONNECTIONS)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            self._unlink_owned_socket()
            raise
        self._listener = listener
        self._stop.clear()
        self._workers = ThreadPoolExecutor(
            max_workers=MAX_CONNECTIONS, thread_name_prefix="fleet-local-control"
        )
        self._accept_thread = threading.Thread(
            target=self.serve_forever, name="fleet-local-control-accept", daemon=True
        )
        self._accept_thread.start()

    def serve_forever(self) -> None:
        """Accept bounded peer-authenticated connections until :meth:`close`."""
        listener = self._listener
        workers = self._workers
        if listener is None or workers is None:
            raise RuntimeError("local control server is not started")
        while not self._stop.is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            if not self._connection_slots.acquire(blocking=False):
                connection.close()
                continue
            workers.submit(self._serve_connection, connection)

    def close(self) -> None:
        """Stop accepting, drain workers, and remove only this server's socket."""
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        accept_thread, self._accept_thread = self._accept_thread, None
        if (
            accept_thread is not None
            and accept_thread is not threading.current_thread()
        ):
            accept_thread.join(timeout=self._io_timeout_seconds)
        workers, self._workers = self._workers, None
        if workers is not None:
            workers.shutdown(wait=True, cancel_futures=False)
        self._unlink_owned_socket()

    def _unlink_owned_socket(self) -> None:
        identity, self._owned_identity = self._owned_identity, None
        if identity is None:
            return
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if (
            stat.S_ISSOCK(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
        ):
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(self._io_timeout_seconds)
                # Denied peers receive no body parsing, dispatch, or response.
                peer_uid = self._peer_uid(connection)
                if peer_uid is None or peer_uid != self.allowed_uid:
                    return
                try:
                    header = _read_exact(connection, 4)
                    length = struct.unpack(">I", header)[0]
                    if not 0 < length <= MAX_FRAME_BYTES:
                        raise LocalControlProtocolError("invalid request frame")
                    payload = _read_exact(connection, length)
                    _require_write_half_close(connection)
                    kind, argument = parse_request(payload)
                    with local_peer_scope(peer_uid):
                        raw_result = self._dispatch(kind, argument)
                    response = {
                        "schema": SCHEMA,
                        "kind": kind,
                        "ok": True,
                        "result": _safe_json_value(raw_result),
                    }
                except (
                    EOFError,
                    OSError,
                    ValueError,
                    json.JSONDecodeError,
                    struct.error,
                ):
                    response = {
                        "schema": SCHEMA,
                        "kind": "error",
                        "ok": False,
                        "error": "invalid_request",
                    }
                self._send_response(connection, response)
        finally:
            self._connection_slots.release()

    @staticmethod
    def _peer_uid(connection: socket.socket) -> int | None:
        if not hasattr(socket, "SO_PEERCRED"):
            return None
        try:
            credentials = connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
        except (OSError, struct.error):
            return None
        return uid if uid >= 0 else None

    def _authenticated(self, connection: socket.socket) -> bool:
        return self._peer_uid(connection) == self.allowed_uid

    def _dispatch(
        self, kind: str, argument: dict[str, object] | None
    ) -> dict[str, object]:
        if kind == "capabilities":
            return {"kinds": ["capabilities", "apply", "inspect"]}
        if argument is None:
            raise LocalControlProtocolError("request argument is required")
        if kind == "apply":
            document = cast(dict[str, Any], argument)
            result = self._managed_projection.apply(
                **document,
                wire_document=copy.deepcopy(document),
            )
            outcome = getattr(result, "outcome", None)
            if type(outcome) is not str:
                raise LocalControlProtocolError("managed apply result is invalid")
            return {"outcome": outcome}
        if kind == "inspect":
            result = self._managed_projection.inspect(**argument)
            if type(result) is not dict:
                raise LocalControlProtocolError("managed inspect result is invalid")
            return result
        raise LocalControlProtocolError("unsupported request kind")

    def _send_response(
        self, connection: socket.socket, response: dict[str, object]
    ) -> None:
        try:
            payload = json.dumps(
                response, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if len(payload) > MAX_FRAME_BYTES:
                return
            connection.sendall(struct.pack(">I", len(payload)) + payload)
        except (OSError, UnicodeError, ValueError, struct.error):
            return


def _read_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError("unexpected end of frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_write_half_close(connection: socket.socket) -> None:
    """Require EOF after one frame so no unexamined bytes reach dispatch."""
    if connection.recv(1):
        raise LocalControlProtocolError("trailing request data")


def _require_safe_socket_parent(socket_path: Path, socket_gid: int | None) -> None:
    """Reject parent traversal and permissions unsafe for a Fleet-owned socket."""
    parent_identity = _require_nonsymlink_directory_components(
        socket_path.parent, "local-control socket parent"
    )
    mode = stat.S_IMODE(parent_identity.st_mode)
    if parent_identity.st_uid != os.geteuid() or mode & 0o007 or mode & 0o700 != 0o700:
        raise ValueError("unsafe local-control socket parent")
    group_mode = mode & 0o070
    if socket_gid is None:
        if group_mode:
            raise ValueError("unsafe local-control socket parent")
        return
    # Cross-UID clients need directory search permission to connect, but never
    # group write: write would let a client unlink and replace Fleet's socket.
    if parent_identity.st_gid != socket_gid or group_mode not in (0o010, 0o050):
        raise ValueError("unsafe local-control socket parent")


def _require_nonsymlink_directory_components(path: Path, label: str) -> os.stat_result:
    """Lstat every lexical component without resolving an attacker-controlled link."""
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if ".." in path.parts:
        raise ValueError(f"unsafe {label}")
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
