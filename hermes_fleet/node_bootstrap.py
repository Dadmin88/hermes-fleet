"""Worker-only Fleet/Keryx bootstrap, doctor, and bundle tooling."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

SCHEMA = "hermes-fleet-worker-bundle.v1"
RECEIPT_SCHEMA = "hermes-fleet-worker-install-receipt.v1"
KERYX_REVISION = "b29e66d8966d444e583b0085a81309d52b157d1d"
NODESCALE_REVISION = "ee4dc0fed28502a27d35344e4b3c7f9e31ae2ef8"
HERMES_REVISION = "a991dfc25daf68994c21d6adcdfbafb1b3dc23cf"
ENV_FILES = (
    "keryxd.env",
    "keryx-node.env",
    "fleet-managed-projection.env",
    "fleet-node.env",
    "hermes-api.env",
)
UNITS = (
    "keryxd.service",
    "keryx-node.service",
    "hermes-fleet-api.service",
    "fleet-managed-projection.service",
    "fleet-node.service",
)
TOKEN_KEY = "HERMES_KERYX_DAEMON_TOKEN"
API_KEY = "API_SERVER_KEY"
SECRET_KEY_RE = re.compile(r"(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)", re.I)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    status: str
    detail: str = ""
    blocker: bool = True


@dataclass(frozen=True)
class DoctorReport:
    schema: str
    ready: bool
    primary_blocker: str | None
    checks: tuple[Check, ...]

    def document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "ready": self.ready,
            "primary_blocker": self.primary_blocker,
            "checks": [
                {**asdict(item), "detail": _safe_detail(item.detail)}
                for item in self.checks
            ],
        }


class Runner(Protocol):
    def run(
        self, argv: list[str], *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(
        self, argv: list[str], *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env=None if env is None else dict(env),
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_detail(value: object) -> str:
    text = str(value).replace("\n", " ").strip()
    text = re.sub(
        r"(?i)(bearer|token|key|secret|password|credential)\s*[=:]\s*\S+",
        r"\1=<redacted>",
        text,
    )
    return text[:300]


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env(path: Path, changes: Mapping[str, str]) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = old.splitlines()
    remaining = dict(changes)
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    new = "\n".join(output).rstrip() + "\n"
    if new == old and path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600:
        return False
    _atomic_write(path, new.encode(), 0o600)
    return True


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def load_bundle(bundle: Path) -> dict[str, Any]:
    manifest_path = bundle / "bundle.json"
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("bundle manifest is unreadable") from error
    required = {
        "schema",
        "bundle_id",
        "role",
        "platform",
        "revisions",
        "artifacts",
        "units",
        "service_scope",
    }
    if type(document) is not dict or set(document) != required:
        raise ValueError("bundle manifest fields are invalid")
    if document["schema"] != SCHEMA or document["role"] != "worker":
        raise ValueError("bundle schema or role is unsupported")
    revisions = document["revisions"]
    expected = {
        "keryx": KERYX_REVISION,
        "nodescale": NODESCALE_REVISION,
        "hermes": HERMES_REVISION,
    }
    if (
        type(revisions) is not dict
        or set(revisions) != {"fleet", *expected}
        or not re.fullmatch(r"[0-9a-f]{40}", revisions.get("fleet", ""))
        or any(revisions.get(name) != value for name, value in expected.items())
    ):
        raise ValueError("bundle revisions do not match the accepted stack")
    artifacts = document["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != {
        "keryxd",
        "keryx-node",
        "fleet-managed-control",
        "keryx-wheel",
        "fleet-wheel",
        "hermes-source",
    }:
        raise ValueError("bundle artifacts are invalid")
    for name, item in artifacts.items():
        if type(item) is not dict or set(item) != {"path", "sha256"}:
            raise ValueError(f"bundle artifact {name} is invalid")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"bundle artifact {name} path is unsafe")
        artifact = bundle / relative
        if not artifact.is_file() or _sha256(artifact) != item["sha256"]:
            raise ValueError(f"bundle artifact {name} failed SHA-256 verification")
    units = document["units"]
    if type(units) is not dict or set(units) != set(UNITS):
        raise ValueError("bundle worker units are invalid")
    if document["service_scope"] != list(UNITS):
        raise ValueError("bundle service scope is invalid")
    for name in UNITS:
        item = units[name]
        if type(item) is not dict or set(item) != {"path", "sha256"}:
            raise ValueError(f"bundle unit {name} is invalid")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"bundle unit {name} path is unsafe")
        unit = bundle / relative
        if not unit.is_file() or _sha256(unit) != item["sha256"]:
            raise ValueError(f"bundle unit {name} failed SHA-256 verification")
    if document["bundle_id"] != _bundle_id(revisions, artifacts, units):
        raise ValueError("bundle identity does not match its verified contents")
    return document


def _bundle_id(
    revisions: dict[str, str],
    artifacts: dict[str, dict[str, str]],
    units: dict[str, dict[str, str]],
) -> str:
    identity_input = json.dumps(
        {"revisions": revisions, "artifacts": artifacts, "units": units},
        sort_keys=True,
    ).encode()
    return f"worker-v1-{hashlib.sha256(identity_input).hexdigest()[:16]}"


class Doctor:
    def __init__(
        self, *, home: Path, bundle: Path | None = None, runner: Runner | None = None
    ) -> None:
        self.home = home
        self.bundle = bundle
        self.runner = runner or SubprocessRunner()
        self.config = home / ".config/hermes-fleet"
        self.install = home / ".local/share/hermes-fleet"
        self.units = home / ".config/systemd/user"

    def run(self) -> DoctorReport:
        checks: list[Check] = []
        checks.extend(self._platform())
        checks.extend(self._tailscale())
        checks.extend(self._hermes())
        checks.extend(self._keryx())
        checks.extend(self._fleet())
        blockers = [item for item in checks if item.blocker and not item.ok]
        return DoctorReport(
            schema="hermes-fleet-worker-doctor.v1",
            ready=not blockers,
            primary_blocker=blockers[0].name if blockers else None,
            checks=tuple(checks),
        )

    def _command(
        self, argv: list[str], *, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner.run(argv, env=env)
        except (OSError, subprocess.SubprocessError) as error:
            return subprocess.CompletedProcess(argv, 127, "", _safe_detail(error))

    def _platform(self) -> list[Check]:
        systemd = shutil.which("systemctl") is not None
        machine = platform.machine()
        os_id = "unknown"
        try:
            os_id = _read_env(Path("/etc/os-release")).get("ID", "unknown")
        except OSError:
            pass
        supported = (
            os_id in {"debian", "ubuntu", "arch", "garuda"} and machine == "x86_64"
        )
        return [
            Check(
                "platform.os_arch",
                supported,
                "supported" if supported else "unsupported",
                f"{os_id}/{machine}",
            ),
            Check("platform.systemd", systemd, "present" if systemd else "missing"),
        ]

    def _tailscale(self) -> list[Check]:
        installed = shutil.which("tailscale") is not None
        active = (
            self._command(["systemctl", "is-active", "tailscaled.service"]).returncode
            == 0
            if installed
            else False
        )
        status = (
            self._command(["tailscale", "status", "--json"])
            if installed
            else subprocess.CompletedProcess([], 1, "", "")
        )
        connected = False
        if status.returncode == 0:
            try:
                connected = json.loads(status.stdout).get("BackendState") == "Running"
            except json.JSONDecodeError:
                pass
        registry = self._registry_hostname()
        resolved = False
        magicdns = "not_configured"
        if registry:
            try:
                socket.getaddrinfo(registry, None)
                resolved = True
                magicdns = "resolved"
            except OSError:
                query = self._command(["tailscale", "dns", "query", registry, "A"])
                magicdns = (
                    "nxdomain"
                    if "RCodeNameError" in (query.stdout + query.stderr)
                    else "resolver_failure"
                )
        return [
            Check(
                "tailscale.installed",
                installed,
                "installed" if installed else "missing",
            ),
            Check("tailscale.running", active, "running" if active else "stopped"),
            Check(
                "tailscale.connected",
                connected,
                "connected" if connected else "disconnected",
            ),
            Check(
                "tailscale.registry_dns",
                bool(registry and resolved),
                magicdns,
                registry or "registry hostname missing",
            ),
        ]

    def _registry_hostname(self) -> str | None:
        for filename in ENV_FILES:
            values = _read_env(self.config / filename)
            for key in (
                "HERMES_KERYX_REGISTRY_ENDPOINT",
                "HERMES_KERYX_RELAY_REGISTRY_ENDPOINT",
                "HERMES_KERYX_REGISTRY_ADDR",
                "KERYX_REGISTRY_ADDR",
            ):
                raw = values.get(key)
                if raw:
                    parsed = urlparse(raw if "://" in raw else f"//{raw}")
                    return parsed.hostname
        return None

    def _hermes(self) -> list[Check]:
        values = _read_env(self.config / "fleet-node.env")
        endpoint = values.get(
            "FLEET_HERMES_API_URL",
            values.get("HERMES_RUNS_BASE_URL", "http://127.0.0.1:8642"),
        ).rstrip("/")
        headers = {}
        if values.get("API_SERVER_KEY"):
            headers["Authorization"] = f"Bearer {values['API_SERVER_KEY']}"
        health = self._http_json(f"{endpoint}/health", headers)
        capabilities = self._http_json(f"{endpoint}/v1/capabilities", headers)
        return [
            Check(
                "hermes.installed",
                health is not None,
                "reachable" if health is not None else "unreachable",
            ),
            Check(
                "hermes.runs_capability",
                capabilities is not None,
                "available" if capabilities is not None else "unavailable",
            ),
        ]

    def _http_json(self, url: str, headers: Mapping[str, str]) -> object | None:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=dict(headers)), timeout=3
            ) as response:
                return json.loads(response.read(65536))
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
            return None

    def _keryx(self) -> list[Check]:
        checks: list[Check] = []
        manifest = load_bundle(self.bundle) if self.bundle else None
        for name in ("keryxd", "keryx-node", "fleet-managed-control"):
            path = self.install / "bin" / name
            present = path.is_file()
            expected = (
                None if manifest is None else manifest["artifacts"][name]["sha256"]
            )
            matches = present and (expected is None or _sha256(path) == expected)
            detail = "missing" if not present else _sha256(path)
            checks.append(
                Check(
                    f"runtime.{name}_identity",
                    matches and expected is not None,
                    "match"
                    if matches and expected
                    else "unknown"
                    if matches
                    else "skew",
                    detail,
                )
            )
        python = self.install / "venv/bin/python"
        sdk = self._command(
            [
                str(python),
                "-c",
                "import inspect,keryx.config,keryx.client;"
                "assert 'daemon_token' in keryx.config.KeryxConfig.__annotations__;"
                "assert 'daemon_token' in "
                "inspect.signature(keryx.client.DaemonClient).parameters",
            ]
        )
        checks.append(
            Check(
                "keryx.sdk_daemon_token",
                sdk.returncode == 0,
                "supported" if sdk.returncode == 0 else "missing",
            )
        )
        envs = [
            _read_env(self.config / name)
            for name in ("keryxd.env", "keryx-node.env", "fleet-node.env")
        ]
        present = all(values.get(TOKEN_KEY) for values in envs)
        equal = present and len({values[TOKEN_KEY] for values in envs}) == 1
        permissions = all(
            (self.config / name).is_file()
            and stat.S_IMODE((self.config / name).stat().st_mode) == 0o600
            for name in ENV_FILES
        )
        checks.extend(
            [
                Check(
                    "keryx.daemon_credential_present",
                    present,
                    "present" if present else "missing",
                ),
                Check(
                    "keryx.daemon_credential_permissions",
                    permissions,
                    "owner_only" if permissions else "insecure",
                ),
                Check(
                    "keryx.daemon_credential_consistent",
                    equal,
                    "consistent" if equal else "mismatch",
                ),
            ]
        )
        active = (
            self._command(
                ["systemctl", "--user", "is-active", "keryxd.service"]
            ).returncode
            == 0
        )
        checks.append(
            Check("keryx.keryxd_health", active, "running" if active else "unhealthy")
        )
        auth = self._auth_probe(python, envs[0].get(TOKEN_KEY) if envs else None)
        checks.extend(
            [
                Check(
                    "keryx.authenticated_rpc",
                    auth == "enforced",
                    "succeeded" if auth == "enforced" else auth,
                ),
                Check(
                    "keryx.unauthenticated_rpc",
                    auth == "enforced",
                    "UNAUTHENTICATED" if auth == "enforced" else auth,
                ),
            ]
        )
        edge = (
            self._command(
                ["systemctl", "--user", "is-active", "keryx-node.service"]
            ).returncode
            == 0
        )
        checks.append(
            Check(
                "keryx.edge_registry",
                edge and bool(self._registry_hostname()),
                "running" if edge else "unhealthy",
            )
        )
        managed_control = (
            self._command(
                [
                    "systemctl",
                    "--user",
                    "is-active",
                    "fleet-managed-projection.service",
                ]
            ).returncode
            == 0
        )
        checks.append(
            Check(
                "fleet.managed_control",
                managed_control,
                "running" if managed_control else "unhealthy",
            )
        )
        return checks

    def _auth_probe(self, python: Path, token: str | None) -> str:
        if not token:
            return "credential_missing"
        script = """import asyncio,grpc,os
from keryx.proto.hermes.keryx.v1 import daemon_pb2,daemon_pb2_grpc
async def call(token):
 c=grpc.aio.insecure_channel(os.environ.get('HERMES_KERYX_DAEMON_ADDR','127.0.0.1:50051'))
 try:
  metadata=() if token is None else (('authorization','Bearer '+token),)
  stub=daemon_pb2_grpc.KeryxDaemonStub(c)
  request=daemon_pb2.SubmitTaskRequest()
  await stub.SubmitTask(request,metadata=metadata)
  return 'OK'
 except grpc.aio.AioRpcError as e: return e.code().name
 finally: await c.close()
async def main():
 print((await call(None))+','+(await call(os.environ['HERMES_KERYX_DAEMON_TOKEN'])))
asyncio.run(main())
"""
        environment = dict(os.environ)
        environment.update(_read_env(self.config / "keryxd.env"))
        result = self._command([str(python), "-c", script], env=environment)
        value = result.stdout.strip()
        if value == "UNAUTHENTICATED,INVALID_ARGUMENT":
            return "enforced"
        if value == "INVALID_ARGUMENT,INVALID_ARGUMENT":
            return "not_enforced"
        if value == "UNAUTHENTICATED,UNAUTHENTICATED":
            return "client_token_rejected"
        return "transport_failure"

    def _fleet(self) -> list[Check]:
        values = _read_env(self.config / "fleet-node.env")
        active = (
            self._command(
                ["systemctl", "--user", "is-active", "fleet-node.service"]
            ).returncode
            == 0
        )
        identity = bool(values.get("FLEET_NODE_NAME"))
        controllers = bool(values.get("FLEET_CONTROLLER_PEER_IDS"))
        config_path = self._fleet_config_path()
        policy = "unknown"
        readable = config_path.is_file()
        if readable:
            text = config_path.read_text(encoding="utf-8")
            policy = "configured" if "fleet.hermes.run" in text else "not_granted"
        return [
            Check("fleet.service", active, "running" if active else "unhealthy"),
            Check(
                "fleet.worker_identity",
                identity,
                "preserved" if identity else "missing",
            ),
            Check(
                "fleet.controller_peers",
                controllers,
                "configured" if controllers else "missing",
            ),
            Check("fleet.policy", readable, policy),
        ]

    def _fleet_config_path(self) -> Path:
        unit = self.units / "fleet-node.service"
        if unit.is_file():
            match = re.search(r"--config\s+(\S+)", unit.read_text(encoding="utf-8"))
            if match:
                return Path(match.group(1).replace("%h", str(self.home)))
        return self.home / ".hermes/profiles/admin/fleet/nodes.yaml"


def render_report(report: DoctorReport, *, json_output: bool) -> str:
    if json_output:
        return json.dumps(report.document(), sort_keys=True, separators=(",", ":"))
    lines = [f"worker software-ready: {'YES' if report.ready else 'NO'}"]
    if report.primary_blocker:
        lines.append(f"primary blocker: {report.primary_blocker}")
    for item in report.checks:
        mark = "OK" if item.ok else "FAIL" if item.blocker else "WARN"
        detail = f" ({_safe_detail(item.detail)})" if item.detail else ""
        lines.append(f"[{mark}] {item.name}: {item.status}{detail}")
    return "\n".join(lines)


class Installer:
    def __init__(
        self, *, home: Path, bundle: Path, runner: Runner | None = None
    ) -> None:
        self.home = home
        self.bundle = bundle
        self.runner = runner or SubprocessRunner()
        self.config = home / ".config/hermes-fleet"
        self.install = home / ".local/share/hermes-fleet"
        self.state = home / ".local/state/hermes-fleet"
        self.units = home / ".config/systemd/user"
        self.changes: list[str] = []

    def converge(self) -> DoctorReport:
        manifest = load_bundle(self.bundle)  # all preflight happens before mutation
        self._preflight()
        self._runtime_preflight()
        for directory in (self.config, self.install / "bin", self.state, self.units):
            directory.mkdir(parents=True, exist_ok=True)
        snapshot = self._snapshot_root()
        self._snapshot_touched(snapshot)
        token = self._existing_token() or secrets.token_urlsafe(48)
        api_key = self._existing_api_key() or secrets.token_urlsafe(48)
        try:
            self._install_artifacts(manifest)
            self._converge_execution_profile()
            for filename in ENV_FILES:
                values: dict[str, str] = {}
                if filename not in {
                    "hermes-api.env",
                    "fleet-managed-projection.env",
                }:
                    values[TOKEN_KEY] = token
                if filename in {"fleet-node.env", "hermes-api.env"}:
                    values[API_KEY] = api_key
                if filename == "hermes-api.env":
                    values["API_SERVER_ENABLED"] = "true"
                    values["API_SERVER_HOST"] = "127.0.0.1"
                    values["API_SERVER_PORT"] = "8642"
                runtime = Path(f"/run/user/{os.getuid()}/hermes-fleet")
                if filename == "fleet-managed-projection.env":
                    runtime.mkdir(parents=True, mode=0o700, exist_ok=True)
                    values.update(
                        {
                            "FLEET_MANAGED_PROJECTION_SOCKET": str(
                                runtime / "managed-control.sock"
                            ),
                            "FLEET_MANAGED_PROJECTION_DATABASE": str(
                                self.state / "managed-control.sqlite3"
                            ),
                            "FLEET_MANAGED_PROJECTION_ALLOWED_UID": str(os.getuid()),
                        }
                    )
                if filename == "fleet-node.env":
                    values["FLEET_OBSERVATION_SOCKET"] = str(
                        runtime / "managed-control.sock"
                    )
                if _write_env(self.config / filename, values):
                    self.changes.append(f"env:{filename}")
            changed_before_services = bool(self.changes)
            if changed_before_services:
                self._run(["systemctl", "--user", "daemon-reload"])
                self._restart("keryxd.service")
            auth = Doctor(
                home=self.home, bundle=self.bundle, runner=self.runner
            )._auth_probe(self.install / "venv/bin/python", token)
            if auth != "enforced":
                raise RuntimeError(f"daemon authentication proof failed: {auth}")
            doctor = Doctor(home=self.home, bundle=self.bundle, runner=self.runner)
            dns = next(
                item
                for item in doctor._tailscale()
                if item.name == "tailscale.registry_dns"
            )
            if not dns.ok:
                raise RuntimeError(f"registry DNS precondition failed: {dns.status}")
            if changed_before_services:
                self._restart("keryx-node.service")
                self._restart("hermes-fleet-api.service")
                self._restart("fleet-managed-projection.service")
                self._restart("fleet-node.service")
            self._write_receipt(manifest)
            return Doctor(home=self.home, bundle=self.bundle, runner=self.runner).run()
        except BaseException:
            self._restore_snapshot(snapshot)
            raise

    def _preflight(self) -> None:
        if platform.machine() != "x86_64" or shutil.which("systemctl") is None:
            raise RuntimeError("unsupported worker platform")
        for name in ENV_FILES:
            path = self.config / name
            if path.exists() and (not path.is_file() or path.is_symlink()):
                raise RuntimeError(f"unsafe environment path: {name}")
        for name in UNITS:
            path = self.units / name
            if path.exists() and (not path.is_file() or path.is_symlink()):
                raise RuntimeError(f"unsafe systemd unit path: {name}")
        existing = [
            values.get(TOKEN_KEY)
            for values in (_read_env(self.config / name) for name in ENV_FILES)
            if values.get(TOKEN_KEY)
        ]
        if existing and len(set(existing)) != 1:
            raise RuntimeError("existing daemon credentials are inconsistent")

    def _runtime_preflight(self) -> None:
        doctor = Doctor(home=self.home, bundle=self.bundle, runner=self.runner)
        tailscale = {
            item.name: item
            for item in doctor._tailscale()
            if item.name
            in {"tailscale.installed", "tailscale.running", "tailscale.connected"}
        }
        system_fleet = self.runner.run(["systemctl", "is-active", "fleet-node.service"])
        checks = [
            *(tailscale[name] for name in sorted(tailscale)),
            Check(
                "fleet.system_service_absent",
                system_fleet.returncode != 0,
                "absent" if system_fleet.returncode != 0 else "active",
            ),
        ]
        failed = [item for item in checks if item.blocker and not item.ok]
        if failed:
            raise RuntimeError(f"runtime preflight failed: {failed[0].name}")

    def _snapshot_root(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        root = self.state / "bootstrap-rollbacks" / stamp
        root.mkdir(parents=True, mode=0o700)
        return root

    def _snapshot_touched(self, root: Path) -> None:
        paths = self._owned_paths()

        inventory: dict[str, str] = {}
        for path in paths:
            if path.is_file():
                inventory[str(path)] = "file"
                relative = str(path).lstrip("/").replace("/", "__")
                shutil.copy2(path, root / relative)
            elif path.is_dir() and not path.is_symlink():
                inventory[str(path)] = "directory"
                relative = str(path).lstrip("/").replace("/", "__")
                shutil.copytree(path, root / relative, symlinks=True)
            else:
                inventory[str(path)] = "absent"
        _atomic_write(
            root / "inventory.json",
            (json.dumps(inventory, sort_keys=True) + "\n").encode(),
            0o600,
        )
        service_state = {
            name: {
                "enabled": self.runner.run(
                    ["systemctl", "--user", "is-enabled", name]
                ).returncode
                == 0,
                "active": self.runner.run(
                    ["systemctl", "--user", "is-active", name]
                ).returncode
                == 0,
            }
            for name in UNITS
        }
        _atomic_write(
            root / "services.json",
            (json.dumps(service_state, sort_keys=True) + "\n").encode(),
            0o600,
        )

    def _owned_paths(self) -> list[Path]:
        paths = [
            self.install / "bin/keryxd",
            self.install / "bin/keryx-node",
            self.install / "bin/fleet-managed-control",
            self.install / "venv",
            self.install / "hermes-source",
            self.home / ".hermes/profiles/fleet-worker",
            self.home / ".hermes/profiles/fleet-execution",
            self.state / "install-receipt.json",
        ]
        paths += [self.config / name for name in ENV_FILES]
        paths += [self.units / name for name in UNITS]
        return paths

    def _converge_execution_profile(self) -> None:
        worker = self.home / ".hermes/profiles/fleet-worker"
        slot = self.home / ".hermes/profiles/fleet-execution"
        worker.mkdir(parents=True, mode=0o700, exist_ok=True)
        slot.mkdir(parents=True, mode=0o700, exist_ok=True)
        worker.chmod(0o700)
        slot.chmod(0o700)
        config = (
            b"gateway:\n"
            b"  multiplex_profile_allowlist:\n"
            b"    - fleet-execution\n"
            b"  multiplex_profiles: true\n"
        )
        marker = b"hermes-fleet.execution-slot.v1\n"
        config_path = worker / "config.yaml"
        marker_path = slot / ".fleet-execution-slot"
        if not config_path.is_file() or config_path.read_bytes() != config:
            _atomic_write(config_path, config, 0o600)
            self.changes.append("profile:fleet-worker-config")
        if not marker_path.is_file() or marker_path.read_bytes() != marker:
            if any(slot.iterdir()):
                raise RuntimeError("execution profile slot contains foreign state")
            _atomic_write(marker_path, marker, 0o600)
            self.changes.append("profile:fleet-execution-slot")
        elif stat.S_IMODE(marker_path.stat().st_mode) != 0o600:
            marker_path.chmod(0o600)
            self.changes.append("profile:fleet-execution-slot-mode")

    def _restore_snapshot(self, root: Path) -> None:
        try:
            inventory = json.loads(
                (root / "inventory.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("worker rollback inventory is unavailable") from error
        if type(inventory) is not dict:
            raise RuntimeError("worker rollback inventory is invalid")
        owned = {str(path) for path in self._owned_paths()}
        if set(inventory) != owned:
            raise RuntimeError("worker rollback inventory has invalid paths")
        for raw_path, kind in inventory.items():
            if type(raw_path) is not str or kind not in {"file", "directory", "absent"}:
                raise RuntimeError("worker rollback inventory is invalid")
            path = Path(raw_path)
            saved = root / raw_path.lstrip("/").replace("/", "__")
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            if kind == "file":
                if not saved.is_file():
                    raise RuntimeError("worker rollback snapshot is incomplete")
                _atomic_write(
                    path,
                    saved.read_bytes(),
                    stat.S_IMODE(saved.stat().st_mode),
                )
            elif kind == "directory":
                if not saved.is_dir() or saved.is_symlink():
                    raise RuntimeError("worker rollback snapshot is incomplete")
                shutil.copytree(saved, path, symlinks=True)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._restore_services(root)

    def _restore_services(self, root: Path) -> None:
        try:
            state = json.loads((root / "services.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "worker rollback service state is unavailable"
            ) from error
        if type(state) is not dict or set(state) != set(UNITS):
            raise RuntimeError("worker rollback service state is invalid")
        for name in UNITS:
            item = state[name]
            if (
                type(item) is not dict
                or set(item) != {"enabled", "active"}
                or any(type(item[key]) is not bool for key in item)
            ):
                raise RuntimeError("worker rollback service state is invalid")
            self._run(
                [
                    "systemctl",
                    "--user",
                    "enable" if item["enabled"] else "disable",
                    name,
                ]
            )
            self._run(
                [
                    "systemctl",
                    "--user",
                    "start" if item["active"] else "stop",
                    name,
                ]
            )

    def _existing_token(self) -> str | None:
        for name in ENV_FILES:
            token = _read_env(self.config / name).get(TOKEN_KEY)
            if token:
                return token
        return None

    def _existing_api_key(self) -> str | None:
        for name in ("fleet-node.env", "hermes-api.env"):
            value = _read_env(self.config / name).get(API_KEY)
            if value:
                return value
        return None

    def _install_artifacts(self, manifest: dict[str, Any]) -> None:
        for name in UNITS:
            source = self.bundle / manifest["units"][name]["path"]
            destination = self.units / name
            if (
                destination.is_file()
                and _sha256(destination) == manifest["units"][name]["sha256"]
            ):
                continue
            _atomic_write(destination, source.read_bytes(), 0o644)
            self.changes.append(f"unit:{name}")
        for name in ("keryxd", "keryx-node", "fleet-managed-control"):
            source = self.bundle / manifest["artifacts"][name]["path"]
            destination = self.install / "bin" / name
            if (
                destination.is_file()
                and _sha256(destination) == manifest["artifacts"][name]["sha256"]
            ):
                continue
            _atomic_write(destination, source.read_bytes(), 0o755)
            self.changes.append(f"artifact:{name}")
        venv_python = self.install / "venv/bin/python"
        if not venv_python.exists():
            self._run([sys.executable, "-m", "venv", str(self.install / "venv")])
        keryx_wheel = self.bundle / manifest["artifacts"]["keryx-wheel"]["path"]
        fleet_wheel = self.bundle / manifest["artifacts"]["fleet-wheel"]["path"]
        hermes_archive = self.bundle / manifest["artifacts"]["hermes-source"]["path"]
        hermes_source = self.install / "hermes-source"
        expected_receipt = self.state / "install-receipt.json"
        current = None
        if expected_receipt.is_file():
            try:
                current = json.loads(expected_receipt.read_text(encoding="utf-8")).get(
                    "artifacts"
                )
            except json.JSONDecodeError:
                pass
        wanted = {
            name: manifest["artifacts"][name]["sha256"]
            for name in manifest["artifacts"]
        }
        if current != wanted:
            if hermes_source.exists():
                shutil.rmtree(hermes_source)
            hermes_source.mkdir(parents=True)
            with tarfile.open(hermes_archive, mode="r:gz") as archive:
                archive.extractall(hermes_source, filter="data")
            self._run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    str(keryx_wheel),
                ]
            )
            self._run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    "--no-deps",
                    str(fleet_wheel),
                ]
            )
            self._run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    "--editable",
                    str(hermes_source),
                ]
            )
            profile = self.home / ".hermes/profiles/fleet-worker"
            if not profile.is_dir():
                self._run(
                    [
                        str(self.install / "venv/bin/hermes"),
                        "profile",
                        "create",
                        "fleet-worker",
                        "--no-skills",
                        "--no-alias",
                    ]
                )
            self.changes.append("python-runtime")

    def _restart(self, unit: str) -> None:
        self._run(["systemctl", "--user", "restart", unit])
        self.changes.append(f"restart:{unit}")

    def _run(self, argv: list[str]) -> None:
        result = self.runner.run(argv)
        if result.returncode != 0:
            raise RuntimeError(
                f"command failed: {argv[0]} {_safe_detail(result.stderr)}"
            )

    def _write_receipt(self, manifest: dict[str, Any]) -> None:
        path = self.state / "install-receipt.json"
        artifacts = {
            name: item["sha256"] for name, item in manifest["artifacts"].items()
        }
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = None
            if current and all(
                current.get(key) == value
                for key, value in {
                    "schema": RECEIPT_SCHEMA,
                    "bundle_id": manifest["bundle_id"],
                    "role": "worker",
                    "revisions": manifest["revisions"],
                    "artifacts": artifacts,
                    "service_scope": manifest["service_scope"],
                }.items()
            ):
                return
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "bundle_id": manifest["bundle_id"],
            "role": "worker",
            "installed_at": datetime.now(UTC).isoformat(),
            "revisions": manifest["revisions"],
            "artifacts": artifacts,
            "service_scope": manifest["service_scope"],
        }
        text = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        if SECRET_KEY_RE.search(" ".join(receipt)):
            raise AssertionError("receipt keys contain secret material")
        _atomic_write(path, text.encode(), 0o600)


def build_bundle(
    *,
    fleet_source: Path,
    keryx_source: Path,
    hermes_source: Path,
    output: Path,
    runner: Runner | None = None,
) -> Path:
    runner = runner or SubprocessRunner()
    _require_clean_source(fleet_source, runner)
    _require_clean_source(keryx_source, runner)
    _require_clean_source(hermes_source, runner)
    fleet_revision = _git_head(fleet_source, runner)
    if _git_head(keryx_source, runner) != KERYX_REVISION:
        raise RuntimeError("bundle sources are not at accepted revisions")
    if _git_head(hermes_source, runner) != HERMES_REVISION:
        raise RuntimeError("bundle sources are not at accepted revisions")
    output.mkdir(parents=True, exist_ok=False)
    frozen_root = output / "frozen-sources"
    frozen_root.mkdir()
    fleet_frozen = frozen_root / "fleet"
    keryx_frozen = frozen_root / "keryx"
    _freeze_git_source(fleet_source, fleet_revision, fleet_frozen, runner)
    _freeze_git_source(keryx_source, KERYX_REVISION, keryx_frozen, runner)
    cargo_target = output / "cargo-target"
    fleet_cargo_target = output / "fleet-cargo-target"
    fleet_dist = output / "fleet-dist"
    keryx_dist = output / "keryx-dist"

    commands = [
        (
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "-p",
                "keryx-daemon",
                "-p",
                "keryx-relay",
            ],
            keryx_frozen,
            {"CARGO_TARGET_DIR": str(cargo_target)},
        ),
        (
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "-p",
                "fleet-control",
                "--bin",
                "fleet-managed-control",
            ],
            fleet_frozen,
            {"CARGO_TARGET_DIR": str(fleet_cargo_target)},
        ),
        (
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(keryx_dist),
                str(keryx_frozen / "sdk/python"),
            ],
            keryx_frozen,
            None,
        ),
        (
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(fleet_dist),
                str(fleet_frozen),
            ],
            fleet_frozen,
            None,
        ),
    ]
    for argv, cwd, extra in commands:
        environment = dict(os.environ)
        if extra:
            environment.update(extra)
        result = subprocess.run(
            argv, cwd=cwd, env=environment, capture_output=True, text=True, timeout=1800
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"bundle build failed: {argv[0]} {_safe_detail(result.stderr)}"
            )
    artifact_dir = output / "artifacts"
    artifact_dir.mkdir()
    sources = {
        "keryxd": cargo_target / "release/keryxd",
        "keryx-node": cargo_target / "release/keryx-node",
        "fleet-managed-control": fleet_cargo_target / "release/fleet-managed-control",
        "keryx-wheel": next(keryx_dist.glob("*.whl")),
        "fleet-wheel": next(fleet_dist.glob("*.whl")),
    }
    artifacts: dict[str, dict[str, str]] = {}
    for name, source in sources.items():
        destination = artifact_dir / source.name
        shutil.copy2(source, destination)
        artifacts[name] = {
            "path": str(destination.relative_to(output)),
            "sha256": _sha256(destination),
        }
    archived = subprocess.run(
        ["git", "-C", str(hermes_source), "archive", "--format=tar", "HEAD"],
        capture_output=True,
        timeout=180,
    )
    if archived.returncode != 0:
        raise RuntimeError("unable to archive exact Hermes source revision")
    hermes_archive = artifact_dir / "hermes-source.tar.gz"
    hermes_archive.write_bytes(gzip.compress(archived.stdout, mtime=0))
    artifacts["hermes-source"] = {
        "path": str(hermes_archive.relative_to(output)),
        "sha256": _sha256(hermes_archive),
    }
    unit_dir = output / "units"
    unit_dir.mkdir()
    source_units = fleet_frozen / "ops/systemd"
    units: dict[str, dict[str, str]] = {}
    for name in UNITS:
        destination = unit_dir / name
        shutil.copy2(source_units / name, destination)
        units[name] = {
            "path": str(destination.relative_to(output)),
            "sha256": _sha256(destination),
        }
    revisions = {
        "fleet": fleet_revision,
        "keryx": KERYX_REVISION,
        "nodescale": NODESCALE_REVISION,
        "hermes": HERMES_REVISION,
    }
    manifest = {
        "schema": SCHEMA,
        "bundle_id": _bundle_id(revisions, artifacts, units),
        "role": "worker",
        "platform": [
            "linux-x86_64-debian",
            "linux-x86_64-ubuntu",
            "linux-x86_64-arch",
            "linux-x86_64-garuda",
        ],
        "revisions": revisions,
        "artifacts": artifacts,
        "units": units,
        "service_scope": list(UNITS),
    }
    _atomic_write(
        output / "bundle.json",
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        0o644,
    )
    for temporary in (cargo_target, fleet_cargo_target, fleet_dist, keryx_dist):
        shutil.rmtree(temporary)
    load_bundle(output)
    return output


def _freeze_git_source(
    source: Path, revision: str, destination: Path, runner: Runner
) -> None:
    del runner
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("source revision is invalid")
    archived = subprocess.run(
        ["git", "-C", str(source), "archive", revision],
        capture_output=True,
        timeout=180,
    )
    payload = archived.stdout
    if (
        archived.returncode != 0
        or type(payload) not in (bytes, bytearray)
        or not payload
    ):
        raise RuntimeError("unable to freeze exact source revision")
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(fileobj=io.BytesIO(bytes(payload)), mode="r:") as archive:
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError, ValueError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise RuntimeError("exact source archive is invalid") from error


def _git_head(path: Path, runner: Runner) -> str:
    result = runner.run(["git", "-C", str(path), "rev-parse", "HEAD"])
    if result.returncode != 0:
        raise RuntimeError("unable to resolve source revision")
    return result.stdout.strip()


def _require_clean_source(path: Path, runner: Runner) -> None:
    result = runner.run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"]
    )
    if result.returncode != 0 or result.stdout.strip():
        raise RuntimeError("bundle source must be an exact clean Git checkout")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-fleet-node")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--bundle", type=Path)
    doctor.add_argument("--json", action="store_true")
    install = subparsers.add_parser("install")
    install.add_argument("--bundle", type=Path, required=True)
    subparsers.add_parser("snapshot")
    build = subparsers.add_parser("build-bundle")
    build.add_argument("--fleet-source", type=Path, required=True)
    build.add_argument("--keryx-source", type=Path, required=True)
    build.add_argument("--hermes-source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "doctor":
        report = Doctor(home=Path.home(), bundle=args.bundle).run()
        print(render_report(report, json_output=args.json))
        raise SystemExit(0 if report.ready else 1)
    if args.command == "install":
        installer = Installer(home=Path.home(), bundle=args.bundle)
        report = installer.converge()
        print(render_report(report, json_output=False))
        print(f"changes: {len(installer.changes)}")
        raise SystemExit(0 if report.ready else 1)
    if args.command == "snapshot":
        installer = Installer(home=Path.home(), bundle=Path("."))
        root = installer._snapshot_root()
        installer._snapshot_touched(root)
        print(root)
        return
    build_bundle(
        fleet_source=args.fleet_source,
        keryx_source=args.keryx_source,
        hermes_source=args.hermes_source,
        output=args.output,
    )
    print(args.output)


if __name__ == "__main__":
    main()
