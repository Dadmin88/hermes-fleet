"""Worker-only Fleet/Keryx bootstrap, doctor, and bundle tooling."""

from __future__ import annotations

import argparse
import hashlib
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
KERYX_REVISION = "1a569219517ea3f6ea216967f4dcc23dcaf5c822"
FLEET_REVISION = "6d1e0748f9a90a575b617fd2044ee65d52e344f2"
NODESCALE_REVISION = "c0a9a7c873d7086375ac53245e6fd689a3686c7d"
HERMES_REVISION = "a991dfc25daf68994c21d6adcdfbafb1b3dc23cf"
ENV_FILES = ("keryxd.env", "keryx-node.env", "fleet-node.env")
UNITS = ("keryxd.service", "keryx-node.service", "fleet-node.service")
TOKEN_KEY = "HERMES_KERYX_DAEMON_TOKEN"
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
        "service_scope",
    }
    if type(document) is not dict or set(document) != required:
        raise ValueError("bundle manifest fields are invalid")
    if document["schema"] != SCHEMA or document["role"] != "worker":
        raise ValueError("bundle schema or role is unsupported")
    revisions = document["revisions"]
    expected = {
        "fleet": FLEET_REVISION,
        "keryx": KERYX_REVISION,
        "nodescale": NODESCALE_REVISION,
        "hermes": HERMES_REVISION,
    }
    if revisions != expected:
        raise ValueError("bundle revisions do not match the accepted stack")
    artifacts = document["artifacts"]
    if type(artifacts) is not dict or set(artifacts) != {
        "keryxd",
        "keryx-node",
        "keryx-wheel",
        "fleet-wheel",
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
    return document


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
        for name in ("keryxd", "keryx-node"):
            path = self.install / "bin" / name
            present = path.is_file()
            expected = (
                None if manifest is None else manifest["artifacts"][name]["sha256"]
            )
            matches = present and (expected is None or _sha256(path) == expected)
            detail = "missing" if not present else _sha256(path)
            checks.append(
                Check(
                    f"keryx.{name}_identity",
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
        envs = [_read_env(self.config / name) for name in ENV_FILES]
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
        snapshot = self._snapshot_root()
        self._snapshot_touched(snapshot)
        token = self._existing_token() or secrets.token_urlsafe(48)
        try:
            self._install_artifacts(manifest)
            for filename in ENV_FILES:
                if _write_env(self.config / filename, {TOKEN_KEY: token}):
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
                self._restart("fleet-node.service")
            self._write_receipt(manifest)
            return Doctor(home=self.home, bundle=self.bundle, runner=self.runner).run()
        except BaseException:
            raise

    def _preflight(self) -> None:
        if platform.machine() != "x86_64" or shutil.which("systemctl") is None:
            raise RuntimeError("unsupported worker platform")
        for directory in (self.config, self.install / "bin", self.state, self.units):
            directory.mkdir(parents=True, exist_ok=True)
        for name in ENV_FILES:
            path = self.config / name
            if path.exists() and (not path.is_file() or path.is_symlink()):
                raise RuntimeError(f"unsafe environment path: {name}")
        for name in UNITS:
            path = self.units / name
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(
                    f"canonical systemd unit is missing or unsafe: {name}"
                )
        existing = [
            values.get(TOKEN_KEY)
            for values in (_read_env(self.config / name) for name in ENV_FILES)
            if values.get(TOKEN_KEY)
        ]
        if existing and len(set(existing)) != 1:
            raise RuntimeError("existing daemon credentials are inconsistent")

    def _runtime_preflight(self) -> None:
        doctor = Doctor(home=self.home, bundle=self.bundle, runner=self.runner)
        checks = doctor._tailscale() + doctor._hermes() + doctor._fleet()
        failed = [item for item in checks if item.blocker and not item.ok]
        if failed:
            raise RuntimeError(f"runtime preflight failed: {failed[0].name}")

    def _snapshot_root(self) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        root = self.state / "bootstrap-rollbacks" / stamp
        root.mkdir(parents=True, mode=0o700)
        return root

    def _snapshot_touched(self, root: Path) -> None:
        paths = [self.install / "bin/keryxd", self.install / "bin/keryx-node"]
        paths += [self.config / name for name in ENV_FILES]
        paths += [self.units / name for name in UNITS]
        for path in paths:
            if path.is_file():
                relative = str(path).lstrip("/").replace("/", "__")
                shutil.copy2(path, root / relative)

    def _existing_token(self) -> str | None:
        for name in ENV_FILES:
            token = _read_env(self.config / name).get(TOKEN_KEY)
            if token:
                return token
        return None

    def _install_artifacts(self, manifest: dict[str, Any]) -> None:
        for name in ("keryxd", "keryx-node"):
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
        wheels = [
            self.bundle / manifest["artifacts"][name]["path"]
            for name in ("keryx-wheel", "fleet-wheel")
        ]
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
            self._run(
                [
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "--force-reinstall",
                    "--no-deps",
                    *(str(path) for path in wheels),
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
    output: Path,
    runner: Runner | None = None,
) -> Path:
    runner = runner or SubprocessRunner()
    if (
        _git_head(fleet_source, runner) != FLEET_REVISION
        or _git_head(keryx_source, runner) != KERYX_REVISION
    ):
        raise RuntimeError("bundle sources are not at accepted revisions")
    output.mkdir(parents=True, exist_ok=False)
    cargo_target = output / "cargo-target"
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
            keryx_source,
            {"CARGO_TARGET_DIR": str(cargo_target)},
        ),
        (
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(keryx_dist),
                str(keryx_source / "sdk/python"),
            ],
            keryx_source,
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
                str(fleet_source),
            ],
            fleet_source,
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
    revisions = {
        "fleet": FLEET_REVISION,
        "keryx": KERYX_REVISION,
        "nodescale": NODESCALE_REVISION,
        "hermes": HERMES_REVISION,
    }
    identity_input = json.dumps(
        {"revisions": revisions, "artifacts": artifacts}, sort_keys=True
    ).encode()
    manifest = {
        "schema": SCHEMA,
        "bundle_id": f"worker-v1-{hashlib.sha256(identity_input).hexdigest()[:16]}",
        "role": "worker",
        "platform": [
            "linux-x86_64-debian",
            "linux-x86_64-ubuntu",
            "linux-x86_64-arch",
            "linux-x86_64-garuda",
        ],
        "revisions": revisions,
        "artifacts": artifacts,
        "service_scope": list(UNITS),
    }
    _atomic_write(
        output / "bundle.json",
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(),
        0o644,
    )
    load_bundle(output)
    return output


def _git_head(path: Path, runner: Runner) -> str:
    result = runner.run(["git", "-C", str(path), "rev-parse", "HEAD"])
    if result.returncode != 0:
        raise RuntimeError("unable to resolve source revision")
    return result.stdout.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-fleet-node")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--bundle", type=Path)
    doctor.add_argument("--json", action="store_true")
    install = subparsers.add_parser("install")
    install.add_argument("--bundle", type=Path, required=True)
    build = subparsers.add_parser("build-bundle")
    build.add_argument("--fleet-source", type=Path, required=True)
    build.add_argument("--keryx-source", type=Path, required=True)
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
    build_bundle(
        fleet_source=args.fleet_source,
        keryx_source=args.keryx_source,
        output=args.output,
    )
    print(args.output)


if __name__ == "__main__":
    main()
