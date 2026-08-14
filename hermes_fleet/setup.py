"""Idempotent Fleet setup and existing-device worker convergence."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from .node_bootstrap import _safe_detail, load_bundle

_SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$")


class SetupError(RuntimeError):
    def __init__(self, detail: object, *, code: str = "setup_failed") -> None:
        super().__init__(str(detail))
        self.public_message = _safe_detail(detail)
        self.code = code


class PrerequisiteBlocked(SetupError):
    def __init__(self, detail: object) -> None:
        super().__init__(detail, code="prerequisite_blocked")


@dataclass(frozen=True, slots=True)
class SetupCheck:
    name: str
    ok: bool
    status: str


@dataclass(frozen=True, slots=True)
class SetupReport:
    mode: str
    ready: bool
    changed: bool
    checks: tuple[SetupCheck, ...]


@dataclass(frozen=True, slots=True)
class AdoptionReport:
    ssh_target: str
    provider_node_id: str
    installed: bool
    trusted: bool = False
    managed: bool = False
    execution_authorized: bool = False


class Runner(Protocol):
    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]: ...


class SubprocessRunner:
    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )


class SetupService:
    """Compose existing setup owners without manufacturing authority."""

    def __init__(
        self,
        *,
        home: Path,
        runner: Runner | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.home = home
        self.runner = runner or SubprocessRunner()
        self.which = which

    def check_controller(self, *, bundle: Path | None = None) -> SetupReport:
        checks = tuple(
            SetupCheck(
                f"dependency.{name}",
                self.which(name) is not None,
                "present" if self.which(name) else "missing",
            )
            for name in ("systemctl", "tailscale", "hermes")
        )
        if bundle is not None:
            checks += (
                SetupCheck(
                    "bundle.readable",
                    bundle.is_dir(),
                    "present" if bundle.is_dir() else "missing",
                ),
            )
        return SetupReport(
            mode="check",
            ready=all(item.ok for item in checks),
            changed=False,
            checks=checks,
        )

    def adopt_worker(self, ssh_target: str, *, bundle: Path) -> AdoptionReport:
        if _SSH_TARGET.fullmatch(ssh_target) is None:
            raise SetupError("SSH target is invalid.")
        provider_node_id = self._provider_identity(ssh_target)
        try:
            manifest = load_bundle(bundle)
        except (OSError, ValueError) as error:
            raise SetupError(f"Worker bundle is invalid: {error}") from error
        reachable = self._run(self._ssh(ssh_target, ["true"]))
        if reachable.returncode != 0:
            raise SetupError("SSH target is unavailable.")
        self._remote_preflight(ssh_target)
        remote_state = ".local/state/hermes-fleet"
        remote_bundle = f"{remote_state}/setup-bundle"
        staging = self._run(self._ssh(ssh_target, ["mkdir", "-p", remote_state]))
        if staging.returncode != 0:
            raise SetupError("Worker bundle staging failed.")
        transfer = self._run(
            ["scp", "-r", f"{bundle}/.", f"{ssh_target}:{remote_bundle}"]
        )
        if transfer.returncode != 0:
            raise SetupError("Worker bundle transfer failed.")
        remote_venv = f"{remote_state}/setup-venv"
        fleet_wheel = f"{remote_bundle}/{manifest['artifacts']['fleet-wheel']['path']}"
        keryx_wheel = f"{remote_bundle}/{manifest['artifacts']['keryx-wheel']['path']}"
        for command in (
            ["python3", "-m", "venv", remote_venv],
            [
                remote_venv + "/bin/python",
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                keryx_wheel,
            ],
            [
                remote_venv + "/bin/python",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--force-reinstall",
                fleet_wheel,
            ],
        ):
            result = self._run(self._ssh(ssh_target, command))
            if result.returncode != 0:
                raise SetupError("Fleet bootstrap helper staging failed.")
        snapshot = self._run(
            self._ssh(
                ssh_target,
                [
                    remote_venv + "/bin/hermes-fleet-node",
                    "snapshot",
                ],
            )
        )
        if snapshot.returncode != 0:
            raise SetupError("Worker rollback snapshot failed.")
        install = [
            remote_venv + "/bin/hermes-fleet-node",
            "install",
            "--bundle",
            remote_bundle,
        ]
        result = self._run(self._ssh(ssh_target, install))
        if result.returncode != 0:
            raise SetupError("Worker software convergence failed.")
        return AdoptionReport(
            ssh_target=ssh_target,
            provider_node_id=provider_node_id,
            installed=True,
        )

    def _remote_preflight(self, ssh_target: str) -> None:
        checks = (
            [
                "python3",
                "-c",
                "import sys,venv,ensurepip; assert sys.version_info >= (3, 11)",
            ],
            ["systemctl", "--user", "--version"],
            [
                "sh",
                "-c",
                'test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes',
            ],
        )
        for command in checks:
            result = self._run(self._ssh(ssh_target, command))
            if result.returncode != 0:
                raise PrerequisiteBlocked(
                    "Worker lacks a required unprivileged bootstrap prerequisite."
                )

    def _provider_identity(self, target: str) -> str:
        result = self._run(["tailscale", "status", "--json"])
        if result.returncode != 0:
            raise SetupError("Tailscale provider observation is unavailable.")
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SetupError("Tailscale provider observation is invalid.") from error
        matches: list[str] = []
        for peer in document.get("Peer", {}).values():
            selectors = {
                peer.get("HostName"),
                str(peer.get("DNSName", "")).rstrip("."),
                *(peer.get("TailscaleIPs") or []),
            }
            if target in selectors and peer.get("Online") is not False:
                stable = peer.get("ID")
                if isinstance(stable, str) and stable:
                    matches.append(stable)
        if len(set(matches)) != 1:
            raise SetupError("Unable to resolve one exact provider identity.")
        return matches[0]

    @staticmethod
    def _ssh(target: str, remote_argv: list[str]) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", target, shlex.join(remote_argv)]

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner.run(argv)
        except (OSError, subprocess.SubprocessError) as error:
            return subprocess.CompletedProcess(argv, 127, "", _safe_detail(error))


def setup_parser(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="setup_command", required=True)
    add_commands(commands)


def add_commands(commands: Any, *, node_commands: Any | None = None) -> None:
    setup = commands.add_parser("setup", help="Check or converge local Fleet setup")
    setup.add_argument("--bundle", type=Path)
    setup.add_argument("--json", action="store_true", dest="json_output")
    if node_commands is None:
        node = commands.add_parser("node", help="Converge an existing worker device")
        node_commands = node.add_subparsers(dest="node_command", required=True)
    assert node_commands is not None
    adopt = node_commands.add_parser(
        "adopt", help="Install an observed existing device"
    )
    adopt.add_argument("ssh_target")
    adopt.add_argument("--bundle", type=Path, required=True)
    adopt.add_argument("--json", action="store_true", dest="json_output")


def run(args: argparse.Namespace, *, service: SetupService | None = None) -> int:
    service = service or SetupService(home=Path.home())
    try:
        if args.setup_command == "setup":
            result: Any = service.check_controller(bundle=args.bundle)
        elif args.setup_command == "node" and args.node_command == "adopt":
            result = service.adopt_worker(args.ssh_target, bundle=args.bundle)
        else:
            raise SetupError("Unknown Fleet setup command.")
    except SetupError as error:
        payload = {"error": {"code": error.code, "message": error.public_message}}
        print(
            json.dumps(payload, sort_keys=True)
            if getattr(args, "json_output", False)
            else error.public_message
        )
        return 6
    document = asdict(result)
    print(
        json.dumps(document, sort_keys=True)
        if getattr(args, "json_output", False)
        else _human(document)
    )
    return 0 if document.get("ready", True) else 6


def _human(document: dict[str, Any]) -> str:
    if "provider_node_id" in document:
        return f"worker software installed: {str(document['installed']).lower()}"
    return "\n".join(
        f"{'OK' if item['ok'] else 'FAIL'} {item['name']}: {item['status']}"
        for item in document["checks"]
    )
