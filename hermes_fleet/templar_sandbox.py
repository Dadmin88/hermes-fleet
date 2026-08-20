"""Phase 21 disposable Bubblewrap runtime for low-authority Templar evaluation.

The sandbox is deliberately narrower than a normal Hermes runtime. It receives
one sanitized Phase 20 evaluation request on stdin, may optionally receive one
already-connected AF_UNIX provider channel, and must emit one closed Templar
backend response on stdout. It has no host data mounts, host network, durable
home, management sockets, credentials, or normal Agent state.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from .templar import (
    TEMPLAR_BACKEND_RESPONSE_SCHEMA,
    TEMPLAR_REQUEST_SCHEMA,
    TemplarEvaluationRequest,
    TemplarEvaluatorIdentity,
    TemplarPolicyRef,
)

TEMPLAR_SANDBOX_POLICY_SCHEMA: Final[str] = "fleet.templar-sandbox-policy.v1"
TEMPLAR_SANDBOX_RUNTIME: Final[str] = "bubblewrap-python-v1"
PROVIDER_NONE: Final[str] = "none"
PROVIDER_CHANNEL: Final[str] = "provider-channel"

_HASH_PREFIX = "sha256:"
_MAX_ARTIFACT_BYTES = 256 * 1024
_MAX_INPUT_BYTES = 768 * 1024
_MAX_STDOUT_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 16 * 1024
_MAX_WALL_CLOCK_MS = 30_000
_MAX_CPU_SECONDS = 10
_MAX_ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_OPEN_FILES = 256
_MAX_PROCESSES = 64
_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "credential",
        "credentials",
        "password",
        "passphrase",
        "private_key",
        "secret_body",
        "secret_bytes",
        "secret_value",
        "value",
    }
)


class TemplarSandboxError(RuntimeError):
    """The disposable evaluator boundary could not be established safely."""


class TemplarSandboxUnavailable(TemplarSandboxError):
    """Required Linux/Bubblewrap sandbox support is unavailable."""


class TemplarSandboxProtocolError(TemplarSandboxError):
    """The evaluator input/output shape is malformed or unsafe."""


class TemplarSandboxTimeout(TimeoutError):
    """The evaluator exceeded its hard wall-clock bound and was terminated."""


class ProviderChannelFactory(Protocol):
    """Trusted host seam for one fresh credential-free provider proxy channel.

    The returned socket must be dedicated to model/provider routing for this
    evaluation. Supplying a Fleet, Keryx, Docker, Hermes-management, or other
    authority-bearing connection would violate the Phase 21 wiring contract.
    """

    def __call__(
        self,
        *,
        evaluator: TemplarEvaluatorIdentity,
        request_hash: str,
    ) -> socket.socket: ...


def _canonical(value: object, label: str, *, maximum: int) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise TemplarSandboxProtocolError(f"{label} is not canonical JSON") from error
    if len(payload) > maximum:
        raise TemplarSandboxProtocolError(f"{label} exceeds the supported bound")
    return payload


def _digest_bytes(payload: bytes) -> str:
    return _HASH_PREFIX + hashlib.sha256(payload).hexdigest()


def _validate_hash(value: object, label: str) -> str:
    if type(value) is not str or not value.startswith(_HASH_PREFIX):
        raise TemplarSandboxProtocolError(f"{label} is invalid")
    suffix = value[len(_HASH_PREFIX) :]
    if len(suffix) != 64 or any(char not in "0123456789abcdef" for char in suffix):
        raise TemplarSandboxProtocolError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or not 1 <= value <= maximum:
        raise TemplarSandboxProtocolError(f"{label} is invalid")
    return value


def _contains_forbidden_secret_key(value: object) -> bool:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                return True
            if key.lower().replace("-", "_") in _FORBIDDEN_SECRET_KEYS:
                return True
            if _contains_forbidden_secret_key(item):
                return True
        return False
    if type(value) is list:
        return any(_contains_forbidden_secret_key(item) for item in value)
    return False


@dataclass(frozen=True, slots=True)
class TemplarSandboxPolicy:
    wall_clock_ms: int = 5_000
    cpu_seconds: int = 2
    address_space_bytes: int = 256 * 1024 * 1024
    file_bytes: int = 64 * 1024
    open_files: int = 64
    processes: int = 16
    stdout_bytes: int = _MAX_STDOUT_BYTES
    stderr_bytes: int = _MAX_STDERR_BYTES
    provider_access: str = PROVIDER_NONE

    def __post_init__(self) -> None:
        _positive_int(
            self.wall_clock_ms,
            "Templar sandbox wall-clock limit",
            maximum=_MAX_WALL_CLOCK_MS,
        )
        _positive_int(
            self.cpu_seconds,
            "Templar sandbox CPU limit",
            maximum=_MAX_CPU_SECONDS,
        )
        _positive_int(
            self.address_space_bytes,
            "Templar sandbox address-space limit",
            maximum=_MAX_ADDRESS_SPACE_BYTES,
        )
        if self.address_space_bytes < 64 * 1024 * 1024:
            raise TemplarSandboxProtocolError(
                "Templar sandbox address-space limit is too small for the runtime"
            )
        _positive_int(
            self.file_bytes,
            "Templar sandbox file-size limit",
            maximum=_MAX_FILE_BYTES,
        )
        _positive_int(
            self.open_files,
            "Templar sandbox open-file limit",
            maximum=_MAX_OPEN_FILES,
        )
        _positive_int(
            self.processes,
            "Templar sandbox process limit",
            maximum=_MAX_PROCESSES,
        )
        _positive_int(
            self.stdout_bytes,
            "Templar sandbox stdout limit",
            maximum=_MAX_STDOUT_BYTES,
        )
        _positive_int(
            self.stderr_bytes,
            "Templar sandbox stderr limit",
            maximum=_MAX_STDERR_BYTES,
        )
        if self.provider_access not in {PROVIDER_NONE, PROVIDER_CHANNEL}:
            raise TemplarSandboxProtocolError(
                "Templar sandbox provider-access mode is invalid"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": TEMPLAR_SANDBOX_POLICY_SCHEMA,
            "runtime": TEMPLAR_SANDBOX_RUNTIME,
            "wall_clock_ms": self.wall_clock_ms,
            "cpu_seconds": self.cpu_seconds,
            "address_space_bytes": self.address_space_bytes,
            "file_bytes": self.file_bytes,
            "open_files": self.open_files,
            "processes": self.processes,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "provider_access": self.provider_access,
            "network": "unshared",
            "host_data_mounts": "none",
            "environment": "clearenv-allowlist",
            "identity": "uid-65534-gid-65534-cap-drop-all",
        }

    @property
    def content_hash(self) -> str:
        return _digest_bytes(
            _canonical(
                self.to_dict(),
                "Templar sandbox policy",
                maximum=64 * 1024,
            )
        )


@dataclass(frozen=True, slots=True)
class TemplarEvaluatorArtifact:
    """Exact trusted evaluator source staged through an anonymous file descriptor."""

    source_path: str
    content_hash: str

    def __post_init__(self) -> None:
        if type(self.source_path) is not str or not self.source_path.startswith("/"):
            raise TemplarSandboxProtocolError(
                "Templar evaluator artifact path must be absolute"
            )
        _validate_hash(self.content_hash, "Templar evaluator artifact hash")

    def read_verified(self) -> bytes:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.source_path, flags)
        except OSError as error:
            raise TemplarSandboxProtocolError(
                "Templar evaluator artifact cannot be opened safely"
            ) from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise TemplarSandboxProtocolError(
                    "Templar evaluator artifact is not a regular file"
                )
            if info.st_size <= 0 or info.st_size > _MAX_ARTIFACT_BYTES:
                raise TemplarSandboxProtocolError(
                    "Templar evaluator artifact size is invalid"
                )
            chunks: list[bytes] = []
            remaining = _MAX_ARTIFACT_BYTES + 1
            while remaining > 0:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
        finally:
            os.close(fd)
        if not payload or len(payload) > _MAX_ARTIFACT_BYTES:
            raise TemplarSandboxProtocolError(
                "Templar evaluator artifact size is invalid"
            )
        if _digest_bytes(payload) != self.content_hash:
            raise TemplarSandboxProtocolError(
                "Templar evaluator artifact hash does not match"
            )
        return payload


def _runtime_harness(policy: TemplarSandboxPolicy) -> str:
    limits = (
        ("resource.RLIMIT_CPU", policy.cpu_seconds),
        ("resource.RLIMIT_AS", policy.address_space_bytes),
        ("resource.RLIMIT_FSIZE", policy.file_bytes),
        ("resource.RLIMIT_NOFILE", policy.open_files),
        ("resource.RLIMIT_NPROC", policy.processes),
        ("resource.RLIMIT_CORE", 0),
    )
    rendered = ",".join(f"({kind},{value})" for kind, value in limits)
    return (
        "import resource,runpy\n"
        f"limits=({rendered},)\n"
        "for kind,requested in limits:\n"
        "    _soft,hard=resource.getrlimit(kind)\n"
        "    ceiling=requested if hard==resource.RLIM_INFINITY "
        "else min(requested,hard)\n"
        "    resource.setrlimit(kind,(ceiling,ceiling))\n"
        "runpy.run_path('/app/evaluator.py',run_name='__main__')\n"
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _read_bounded(file_obj: Any, maximum: int, label: str) -> bytes:
    file_obj.flush()
    file_obj.seek(0)
    payload = file_obj.read(maximum + 1)
    if len(payload) > maximum:
        raise TemplarSandboxProtocolError(f"{label} exceeds the supported bound")
    return payload


def _validate_request_document(
    request: Mapping[str, Any],
    evaluator: TemplarEvaluatorIdentity,
) -> tuple[dict[str, Any], bytes]:
    if not isinstance(request, Mapping):
        raise TemplarSandboxProtocolError("Templar sandbox request is invalid")
    document = json.loads(
        _canonical(
            dict(request),
            "Templar sandbox request",
            maximum=_MAX_INPUT_BYTES,
        ).decode("utf-8")
    )
    expected_keys = {
        "schema",
        "evaluation_id",
        "request_hash",
        "event_hash",
        "fleet_policy_digest",
        "templar_policy",
        "evaluator",
        "issued_at_ms",
        "deadline_ms",
        "event",
    }
    if type(document) is not dict or set(document) != expected_keys:
        raise TemplarSandboxProtocolError(
            "Templar sandbox request has an invalid closed schema"
        )
    if document["schema"] != TEMPLAR_REQUEST_SCHEMA:
        raise TemplarSandboxProtocolError(
            "Templar sandbox request schema is unsupported"
        )
    try:
        parsed = TemplarEvaluationRequest(
            request_hash=document["request_hash"],
            event_hash=document["event_hash"],
            fleet_policy_digest=document["fleet_policy_digest"],
            templar_policy=TemplarPolicyRef.from_dict(document["templar_policy"]),
            evaluator=TemplarEvaluatorIdentity.from_dict(document["evaluator"]),
            issued_at_ms=document["issued_at_ms"],
            deadline_ms=document["deadline_ms"],
            event=document["event"],
        )
    except Exception as error:
        raise TemplarSandboxProtocolError(
            "Templar sandbox request binding is invalid"
        ) from error
    if parsed.evaluator != evaluator:
        raise TemplarSandboxProtocolError(
            "Templar sandbox evaluator identity does not match request"
        )
    if parsed.evaluation_id != document["evaluation_id"]:
        raise TemplarSandboxProtocolError(
            "Templar sandbox evaluation id does not match exact request"
        )
    document = parsed.to_dict()
    if _contains_forbidden_secret_key(document):
        raise TemplarSandboxProtocolError(
            "Templar sandbox request contains a forbidden secret-bearing field"
        )
    payload = _canonical(
        document,
        "Templar sandbox request",
        maximum=_MAX_INPUT_BYTES,
    )
    return document, payload + b"\n"


def _validate_provider_socket(channel: socket.socket) -> int:
    if type(channel) is not socket.socket:
        raise TemplarSandboxProtocolError("Templar provider channel is not a socket")
    if channel.family != socket.AF_UNIX:
        raise TemplarSandboxProtocolError("Templar provider channel must be AF_UNIX")
    if (channel.type & socket.SOCK_STREAM) != socket.SOCK_STREAM:
        raise TemplarSandboxProtocolError(
            "Templar provider channel must be a stream socket"
        )
    try:
        peer = channel.getpeername()
    except OSError as error:
        raise TemplarSandboxProtocolError(
            "Templar provider channel is not connected"
        ) from error
    if peer not in {"", b""}:
        raise TemplarSandboxProtocolError(
            "Templar provider channel must be an anonymous socketpair endpoint"
        )
    return channel.fileno()


class TemplarSandboxBackend:
    """Phase 20 backend adapter that executes one evaluator in one fresh sandbox."""

    def __init__(
        self,
        *,
        artifact: TemplarEvaluatorArtifact,
        evaluator: TemplarEvaluatorIdentity,
        policy: TemplarSandboxPolicy | None = None,
        provider_channel_factory: ProviderChannelFactory | None = None,
        bwrap_path: str | None = None,
        python_path: str = "/usr/bin/python3",
    ) -> None:
        if type(artifact) is not TemplarEvaluatorArtifact:
            raise TemplarSandboxProtocolError("Templar evaluator artifact is invalid")
        if type(evaluator) is not TemplarEvaluatorIdentity:
            raise TemplarSandboxProtocolError("Templar evaluator identity is invalid")
        if policy is None:
            policy = TemplarSandboxPolicy()
        if type(policy) is not TemplarSandboxPolicy:
            raise TemplarSandboxProtocolError("Templar sandbox policy is invalid")
        if type(python_path) is not str or not python_path.startswith("/"):
            raise TemplarSandboxProtocolError("Templar sandbox Python path is invalid")
        if (
            policy.provider_access == PROVIDER_NONE
            and provider_channel_factory is not None
        ):
            raise TemplarSandboxProtocolError(
                "provider channel cannot be configured when provider access is disabled"
            )
        if (
            policy.provider_access == PROVIDER_CHANNEL
            and provider_channel_factory is None
        ):
            raise TemplarSandboxProtocolError(
                "provider-channel policy requires a provider channel factory"
            )
        self._artifact = artifact
        self._evaluator = evaluator
        self._policy = policy
        self._provider_channel_factory = provider_channel_factory
        self._bwrap_path = bwrap_path
        self._python_path = python_path

    @property
    def policy(self) -> TemplarSandboxPolicy:
        return self._policy

    @property
    def artifact_hash(self) -> str:
        return self._artifact.content_hash

    def _resolve_runtime(self) -> tuple[str, str]:
        if os.name != "posix" or not Path("/proc").is_dir():
            raise TemplarSandboxUnavailable(
                "Templar disposable sandbox is available only on Linux"
            )
        bwrap = self._bwrap_path or shutil.which("bwrap")
        if not bwrap or not Path(bwrap).is_file():
            raise TemplarSandboxUnavailable(
                "Bubblewrap is required for disposable Templar evaluation"
            )
        if not Path(self._python_path).is_file():
            raise TemplarSandboxUnavailable(
                "system Python is required for disposable Templar evaluation"
            )
        return bwrap, self._python_path

    def _argv(
        self,
        *,
        bwrap: str,
        python: str,
        artifact_fd: int,
        provider_fd: int | None,
    ) -> list[str]:
        argv = [
            bwrap,
            "--unshare-all",
            "--die-with-parent",
            "--uid",
            "65534",
            "--gid",
            "65534",
            "--cap-drop",
            "ALL",
            "--hostname",
            "templar-evaluator",
            "--dir",
            "/usr",
            "--dir",
            "/usr/bin",
            "--ro-bind",
            python,
            python,
            "--ro-bind",
            "/usr/lib",
            "/usr/lib",
            "--ro-bind-try",
            "/usr/lib64",
            "/usr/lib64",
            "--ro-bind-try",
            "/lib",
            "/lib",
            "--ro-bind-try",
            "/lib64",
            "/lib64",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/work",
            "--dir",
            "/app",
            "--perms",
            "0444",
            "--ro-bind-data",
            str(artifact_fd),
            "/app/evaluator.py",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin",
            "--setenv",
            "HOME",
            "/tmp/home",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "TEMPLAR_SANDBOX",
            "1",
        ]
        if provider_fd is not None:
            argv.extend(
                [
                    "--setenv",
                    "TEMPLAR_PROVIDER_FD",
                    str(provider_fd),
                ]
            )
        argv.extend(
            [
                "--chdir",
                "/work",
                "--",
                python,
                "-I",
                "-S",
                "-c",
                _runtime_harness(self._policy),
            ]
        )
        return argv

    def evaluate(
        self,
        request: Mapping[str, Any],
        *,
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        _positive_int(
            timeout_ms,
            "Templar sandbox caller timeout",
            maximum=_MAX_WALL_CLOCK_MS,
        )
        document, input_payload = _validate_request_document(request, self._evaluator)
        artifact_bytes = self._artifact.read_verified()
        bwrap, python = self._resolve_runtime()

        provider_channel: socket.socket | None = None
        provider_fd: int | None = None
        if self._policy.provider_access == PROVIDER_CHANNEL:
            assert self._provider_channel_factory is not None
            try:
                provider_channel = self._provider_channel_factory(
                    evaluator=self._evaluator,
                    request_hash=document["request_hash"],
                )
            except Exception as error:
                raise TemplarSandboxProtocolError(
                    "Templar provider channel could not be opened"
                ) from error
            provider_fd = _validate_provider_socket(provider_channel)

        try:
            with (
                tempfile.TemporaryFile() as artifact_file,
                tempfile.TemporaryFile() as stdout_file,
                tempfile.TemporaryFile() as stderr_file,
            ):
                artifact_file.write(artifact_bytes)
                artifact_file.flush()
                artifact_file.seek(0)
                artifact_fd = artifact_file.fileno()
                pass_fds = [artifact_fd]
                if provider_fd is not None:
                    pass_fds.append(provider_fd)

                with subprocess.Popen(
                    self._argv(
                        bwrap=bwrap,
                        python=python,
                        artifact_fd=artifact_fd,
                        provider_fd=provider_fd,
                    ),
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env={},
                    cwd="/",
                    pass_fds=tuple(pass_fds),
                    start_new_session=True,
                ) as process:
                    effective_timeout_ms = min(timeout_ms, self._policy.wall_clock_ms)
                    try:
                        process.communicate(
                            input=input_payload,
                            timeout=effective_timeout_ms / 1000,
                        )
                    except subprocess.TimeoutExpired as error:
                        _kill_process_group(process)
                        raise TemplarSandboxTimeout(
                            "disposable Templar evaluation exceeded its hard timeout"
                        ) from error
                    if process.returncode != 0:
                        raise TemplarSandboxError(
                            "disposable Templar evaluator failed closed"
                        )
                stdout = _read_bounded(
                    stdout_file,
                    self._policy.stdout_bytes,
                    "Templar sandbox stdout",
                )
                _read_bounded(
                    stderr_file,
                    self._policy.stderr_bytes,
                    "Templar sandbox stderr",
                )
        except TemplarSandboxTimeout:
            raise
        except OSError as error:
            raise TemplarSandboxError(
                "disposable Templar evaluator could not start"
            ) from error
        finally:
            if provider_channel is not None:
                provider_channel.close()

        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise TemplarSandboxProtocolError(
                "Templar sandbox returned malformed JSON"
            ) from error
        if type(result) is not dict:
            raise TemplarSandboxProtocolError(
                "Templar sandbox response must be an object"
            )
        if result.get("schema") != TEMPLAR_BACKEND_RESPONSE_SCHEMA:
            raise TemplarSandboxProtocolError(
                "Templar sandbox response schema is unsupported"
            )
        _canonical(
            result,
            "Templar sandbox response",
            maximum=self._policy.stdout_bytes,
        )
        return result
