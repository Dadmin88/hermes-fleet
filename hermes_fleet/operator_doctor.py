"""Read-only local diagnostics for future Fleet operator surfaces."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .config import FleetConfig


class OperatorFindingCode(StrEnum):
    FLEET_NODE_UNAVAILABLE = "FLEET_NODE_UNAVAILABLE"
    MANAGED_PROJECTION_UNAVAILABLE = "MANAGED_PROJECTION_UNAVAILABLE"
    KERYX_DAEMON_UNAVAILABLE = "KERYX_DAEMON_UNAVAILABLE"
    KERYX_NODE_UNAVAILABLE = "KERYX_NODE_UNAVAILABLE"
    HERMES_GATEWAY_UNAVAILABLE = "HERMES_GATEWAY_UNAVAILABLE"
    DUPLICATE_HERMES_GATEWAY = "DUPLICATE_HERMES_GATEWAY"
    GATEWAY_RESTART_LOOP = "GATEWAY_RESTART_LOOP"
    OPERATOR_CONFIG_MISSING = "OPERATOR_CONFIG_MISSING"
    EXECUTION_POLICY_MISSING = "EXECUTION_POLICY_MISSING"
    MANAGED_STATE_STALE = "MANAGED_STATE_STALE"


@dataclass(frozen=True, slots=True)
class OperatorFinding:
    code: OperatorFindingCode
    status: str
    message: str
    affected: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OperatorDoctorReport:
    healthy: bool
    findings: tuple[OperatorFinding, ...]


class DiagnosticRunner(Protocol):
    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]: ...


class SubprocessDiagnosticRunner:
    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=10
        )


_SERVICE_CODES = {
    "fleet-node.service": OperatorFindingCode.FLEET_NODE_UNAVAILABLE,
    "fleet-managed-projection.service": (
        OperatorFindingCode.MANAGED_PROJECTION_UNAVAILABLE
    ),
    "keryxd.service": OperatorFindingCode.KERYX_DAEMON_UNAVAILABLE,
    "keryx-node.service": OperatorFindingCode.KERYX_NODE_UNAVAILABLE,
    "hermes-fleet-api.service": OperatorFindingCode.HERMES_GATEWAY_UNAVAILABLE,
}
_PROFILE = re.compile(r"(?:^|\s)(?:-p|--profile)(?:=|\s+)([A-Za-z0-9._-]+)(?:\s|$)")


class OperatorDoctor:
    """Collect bounded service/config/readiness findings without repair actions."""

    def __init__(self, *, runner: DiagnosticRunner | None = None) -> None:
        self._runner = runner or SubprocessDiagnosticRunner()

    def run(
        self,
        *,
        config_path: Path,
        config: FleetConfig,
        nodes: tuple[Any, ...],
    ) -> OperatorDoctorReport:
        findings: list[OperatorFinding] = []
        for service, code in _SERVICE_CODES.items():
            result = self._run(["systemctl", "--user", "is-active", service])
            if result.returncode != 0 or result.stdout.strip() != "active":
                findings.append(
                    OperatorFinding(code, "error", f"{service} is unavailable.")
                )
        findings.extend(self._gateway_findings())
        if not config_path.is_file():
            findings.append(
                OperatorFinding(
                    OperatorFindingCode.OPERATOR_CONFIG_MISSING,
                    "error",
                    "Canonical operator configuration is missing.",
                )
            )
        if not any(
            "fleet.hermes.run" in item.policy.allowed_operations
            for item in config.managed_targets
        ):
            findings.append(
                OperatorFinding(
                    OperatorFindingCode.EXECUTION_POLICY_MISSING,
                    "warning",
                    "No managed target has explicit Hermes execution authority.",
                )
            )
        stale = sum(
            1
            for node in nodes
            if getattr(getattr(node, "readiness", None), "fresh", None) is False
        )
        if stale:
            findings.append(
                OperatorFinding(
                    OperatorFindingCode.MANAGED_STATE_STALE,
                    "warning",
                    "One or more managed nodes have stale readiness evidence.",
                    affected=(str(stale),),
                )
            )
        return OperatorDoctorReport(
            healthy=not any(item.status == "error" for item in findings),
            findings=tuple(findings),
        )

    def _gateway_findings(self) -> list[OperatorFinding]:
        result = self._run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=service",
                "--all",
                "--no-legend",
                "--plain",
            ]
        )
        if result.returncode != 0:
            return []
        units = sorted(
            {
                line.split()[0]
                for line in result.stdout.splitlines()
                if line.split()
                and "hermes" in line.split()[0]
                and line.split()[0].endswith(".service")
            }
        )
        profiles: dict[str, list[str]] = {}
        findings: list[OperatorFinding] = []
        for unit in units:
            shown = self._run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=ActiveState,NRestarts,ExecStart",
                    "--no-pager",
                ]
            )
            if shown.returncode != 0:
                continue
            fields = dict(
                line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line
            )
            restarts = fields.get("NRestarts", "0")
            if restarts.isdigit() and int(restarts) >= 3:
                findings.append(
                    OperatorFinding(
                        OperatorFindingCode.GATEWAY_RESTART_LOOP,
                        "warning",
                        f"{unit} has restarted repeatedly.",
                        affected=(unit,),
                    )
                )
            if fields.get("ActiveState") != "active":
                continue
            match = _PROFILE.search(fields.get("ExecStart", ""))
            if match:
                profiles.setdefault(match.group(1), []).append(unit)
        duplicates = tuple(
            sorted(profile for profile, owners in profiles.items() if len(owners) > 1)
        )
        if duplicates:
            findings.append(
                OperatorFinding(
                    OperatorFindingCode.DUPLICATE_HERMES_GATEWAY,
                    "error",
                    "Multiple active Hermes gateway services target the same profile.",
                    affected=duplicates,
                )
            )
        return findings

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner.run(argv)
        except (OSError, subprocess.SubprocessError):
            return subprocess.CompletedProcess(argv, 127, "", "unavailable")
