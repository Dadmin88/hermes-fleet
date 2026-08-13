from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hermes_fleet import operator_cli
from hermes_fleet.operator import (
    OperatorCompletionResult,
    OperatorError,
    OperatorErrorCode,
    OperatorIdentity,
    OperatorNodeResult,
    OperatorReadiness,
)
from hermes_fleet.operator_doctor import (
    OperatorDoctorReport,
    OperatorFinding,
    OperatorFindingCode,
)

IDENTITY = OperatorIdentity(
    source="nodescale",
    network_id="network-test",
    device_id="device-a",
    stable_id=f"fleet-node-{'a' * 64}",
    display_name="worker-a",
    alias="worker-a",
)
READINESS = OperatorReadiness(
    alive=True,
    fresh=True,
    scheduler_ready=True,
    observation_age_ms=10,
    reasons=(),
    capacity={"available_worker_slots": 1},
)
NODE = OperatorNodeResult(
    identity=IDENTITY,
    managed_state="active",
    binding_generation="4",
    current_peer_id="peer-diagnostic",
    provenance={"source": "nodescale"},
    readiness=READINESS,
    managed_operations=("fleet.health",),
    explicit_operations=("fleet.hermes.run",),
)


class FakeOperator:
    def list_nodes(self):
        return (NODE,)

    def inspect_node(self, target: str):
        assert target == "worker-a"
        return NODE

    def inspect_readiness(self, target: str):
        assert target == "worker-a"
        return READINESS

    async def run_exact(self, target: str, prompt: str, *, deadline_seconds: int):
        assert (target, prompt, deadline_seconds) == ("worker-a", "do work", 45)
        return OperatorCompletionResult(
            task_id="task-test",
            terminal_state="completed",
            requested_target=target,
            operation="fleet.hermes.run",
            deadline_ms=45_000,
            result="done",
        )

    async def inspect_task(self, task_id: str):
        return OperatorCompletionResult(task_id=task_id, terminal_state="running")


@dataclass
class FakeContext:
    operator: Any = FakeOperator()
    doctor_report: OperatorDoctorReport = OperatorDoctorReport(True, ())
    closed: bool = False

    def doctor(self):
        return self.doctor_report

    async def close(self):
        self.closed = True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    operator_cli.setup_parser(parser)
    return parser


def _invoke(argv: list[str], *, context: FakeContext | None = None):
    ctx = context or FakeContext()
    output: list[str] = []
    error: list[str] = []
    code = operator_cli.run(
        _parser().parse_args(argv),
        context_factory=lambda: ctx,
        stdout=output.append,
        stderr=error.append,
    )
    return code, output, error, ctx


def test_parser_exposes_only_phase2_operator_commands() -> None:
    parser = _parser()
    assert vars(parser.parse_args(["nodes", "--json"]))["command"] == "nodes"
    assert vars(parser.parse_args(["node", "show", "worker-a"]))["target"] == "worker-a"
    assert vars(parser.parse_args(["readiness", "worker-a"]))["target"] == "worker-a"
    run = vars(parser.parse_args(["run", "worker-a", "do work", "--detach"]))
    assert run["wait"] is False
    assert (
        vars(parser.parse_args(["task", "show", "task-test"]))["task_id"] == "task-test"
    )
    assert vars(parser.parse_args(["doctor"]))["command"] == "doctor"


def test_json_output_is_encoded_from_structured_models() -> None:
    code, output, error, ctx = _invoke(["nodes", "--json"])
    assert code == 0
    assert error == []
    payload = json.loads(output[0])
    assert payload["nodes"][0]["identity"]["device_id"] == "device-a"
    assert payload["nodes"][0]["readiness"]["scheduler_ready"] is True
    assert ctx.closed is True


def test_run_wait_reports_structured_terminal_success() -> None:
    code, output, error, _ = _invoke(
        ["run", "worker-a", "do work", "--wait", "--deadline", "45", "--json"]
    )
    assert code == 0
    assert error == []
    assert json.loads(output[0]) == {
        "deadline_ms": 45000,
        "delivery_route": None,
        "error_category": None,
        "operation": "fleet.hermes.run",
        "requested_target": "worker-a",
        "resolved_target": None,
        "result": "done",
        "routed_to": None,
        "run_id": None,
        "task_id": "task-test",
        "terminal_state": "completed",
    }


def test_detach_returns_durable_task_identity_without_waiting() -> None:
    class DetachOperator(FakeOperator):
        async def submit_exact(
            self, target: str, prompt: str, *, deadline_seconds: int
        ):
            return OperatorCompletionResult(
                task_id="task-detached",
                terminal_state="submitted",
                requested_target=target,
                operation="fleet.hermes.run",
                deadline_ms=deadline_seconds * 1000,
            )

    context = FakeContext(operator=DetachOperator())
    code, output, error, _ = _invoke(
        ["run", "worker-a", "do work", "--detach", "--json"], context=context
    )
    assert code == 0
    assert error == []
    assert json.loads(output[0])["task_id"] == "task-detached"


def test_stable_operator_error_is_sanitized_and_has_documented_exit_code() -> None:
    class DeniedOperator(FakeOperator):
        def list_nodes(self):
            raise OperatorError(
                OperatorErrorCode.POLICY_DENIED,
                "token=top-secret path=/private/operator/config",
            )

    code, output, error, _ = _invoke(
        ["nodes", "--json"], context=FakeContext(operator=DeniedOperator())
    )
    assert code == operator_cli.EXIT_DENIED
    assert output == []
    payload = json.loads(error[0])
    assert payload["error"]["code"] == "POLICY_DENIED"
    assert "top-secret" not in error[0]
    assert "/private/operator" not in error[0]


def test_doctor_reports_structured_findings_and_nonzero_exit() -> None:
    report = OperatorDoctorReport(
        False,
        (
            OperatorFinding(
                OperatorFindingCode.DUPLICATE_HERMES_GATEWAY,
                "error",
                "Multiple active Hermes gateway services target the same profile.",
                affected=("example-profile",),
            ),
        ),
    )
    code, output, error, _ = _invoke(
        ["doctor", "--json"], context=FakeContext(doctor_report=report)
    )
    assert code == operator_cli.EXIT_UNAVAILABLE
    assert error == []
    assert json.loads(output[0])["healthy"] is False


def test_default_context_uses_canonical_paths_and_requires_existing_token(
    tmp_path: Path, monkeypatch
) -> None:
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir()
    monkeypatch.setattr(operator_cli, "get_fleet_dir", lambda: fleet_dir)
    monkeypatch.delenv("KERYX_NODE_TOKEN", raising=False)
    with pytest.raises(OperatorError) as caught:
        asyncio.run(operator_cli.OperatorCliContext.open())
    assert caught.value.code is OperatorErrorCode.TRANSPORT_UNAVAILABLE


def test_exit_codes_are_stable() -> None:
    assert {
        "ok": operator_cli.EXIT_OK,
        "usage": operator_cli.EXIT_USAGE,
        "not_found": operator_cli.EXIT_NOT_FOUND,
        "denied": operator_cli.EXIT_DENIED,
        "not_ready": operator_cli.EXIT_NOT_READY,
        "unavailable": operator_cli.EXIT_UNAVAILABLE,
        "task_failed": operator_cli.EXIT_TASK_FAILED,
        "indeterminate": operator_cli.EXIT_INDETERMINATE,
    } == {
        "ok": 0,
        "usage": 2,
        "not_found": 3,
        "denied": 4,
        "not_ready": 5,
        "unavailable": 6,
        "task_failed": 7,
        "indeterminate": 8,
    }


def test_async_context_lifecycle_uses_one_event_loop() -> None:
    events: list[tuple[str, int]] = []

    class AsyncContext(FakeContext):
        async def close(self):
            events.append(("close", id(asyncio.get_running_loop())))
            self.closed = True

    async def open_context():
        events.append(("open", id(asyncio.get_running_loop())))
        return AsyncContext()

    parser = _parser()
    output: list[str] = []
    error: list[str] = []
    code = operator_cli.run(
        parser.parse_args(["nodes", "--json"]),
        context_factory=open_context,
        stdout=output.append,
        stderr=error.append,
    )

    assert code == 0
    assert output and error == []
    assert events[0][0] == "open"
    assert events[-1][0] == "close"
    assert events[0][1] == events[-1][1]
