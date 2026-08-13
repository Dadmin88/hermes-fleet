from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_fleet.config import FleetConfig, ManagedTargetPolicy
from hermes_fleet.models import FleetDefaults, NodePolicy
from hermes_fleet.operator_doctor import OperatorDoctor, OperatorFindingCode


class FakeRunner:
    def __init__(
        self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]]
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        self.calls.append(key)
        return self.responses.get(
            key, subprocess.CompletedProcess(argv, 0, "active\n", "")
        )


def _config() -> FleetConfig:
    return FleetConfig(
        schema_version=2,
        defaults=FleetDefaults(),
        nodes=(),
        managed_targets=(
            ManagedTargetPolicy(
                source="nodescale",
                network_id="network-test",
                device_id="device-a",
                target_name="worker",
                policy=NodePolicy(allowed_operations=("fleet.hermes.run",)),
            ),
        ),
    )


def test_doctor_detects_duplicate_hermes_gateway_profile_without_mutation(
    tmp_path: Path,
) -> None:
    units = (
        "hermes-fleet-api.service loaded active running Hermes Runs API\n"
        "hermes-gateway.service loaded active running Hermes gateway\n"
    )
    show_fleet = (
        "ActiveState=active\nNRestarts=0\n"
        "ExecStart={ path=/usr/bin/hermes ; "
        "argv[]=/usr/bin/hermes -p worker gateway run ; }\n"
    )
    show_gateway = (
        "ActiveState=active\nNRestarts=0\n"
        "ExecStart={ path=/usr/bin/hermes ; "
        "argv[]=/usr/bin/hermes --profile worker gateway run ; }\n"
    )
    responses = {
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--plain",
        ): subprocess.CompletedProcess([], 0, units, ""),
        (
            "systemctl",
            "--user",
            "show",
            "hermes-fleet-api.service",
            "--property=ActiveState,NRestarts,ExecStart",
            "--no-pager",
        ): subprocess.CompletedProcess([], 0, show_fleet, ""),
        (
            "systemctl",
            "--user",
            "show",
            "hermes-gateway.service",
            "--property=ActiveState,NRestarts,ExecStart",
            "--no-pager",
        ): subprocess.CompletedProcess([], 0, show_gateway, ""),
    }
    runner = FakeRunner(responses)
    config_path = tmp_path / "nodes.yaml"
    config_path.write_text(
        "schema_version: 2\ndefaults: {}\nnodes: []\nmanaged_targets: []\n",
        encoding="utf-8",
    )

    report = OperatorDoctor(runner=runner).run(
        config_path=config_path, config=_config(), nodes=()
    )

    duplicate = next(
        item
        for item in report.findings
        if item.code is OperatorFindingCode.DUPLICATE_HERMES_GATEWAY
    )
    assert duplicate.status == "error"
    assert duplicate.affected == ("worker",)
    assert all(
        call[1] != "stop" and call[1] != "restart" and call[1] != "disable"
        for call in runner.calls
    )


def test_doctor_reports_missing_config_policy_and_stale_readiness(
    tmp_path: Path,
) -> None:
    runner = FakeRunner({})
    stale_node = type(
        "Node", (), {"readiness": type("Readiness", (), {"fresh": False})()}
    )()

    report = OperatorDoctor(runner=runner).run(
        config_path=tmp_path / "missing.yaml",
        config=FleetConfig(
            schema_version=2, defaults=FleetDefaults(), nodes=(), managed_targets=()
        ),
        nodes=(stale_node,),
    )
    codes = {item.code for item in report.findings}
    assert OperatorFindingCode.OPERATOR_CONFIG_MISSING in codes
    assert OperatorFindingCode.EXECUTION_POLICY_MISSING in codes
    assert OperatorFindingCode.MANAGED_STATE_STALE in codes
