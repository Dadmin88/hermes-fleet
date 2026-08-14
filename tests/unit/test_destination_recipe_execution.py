from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermes_fleet.agency_materialization import ImmutableAgencyBundle
from hermes_fleet.destination_recipe_execution import DestinationExecutionError
from hermes_fleet.execution_package import ExactExecutionPackage
from hermes_fleet.hermes_runs import (
    HermesRunError,
    HermesRunResult,
    HermesRunSubmissionUnknown,
)
from hermes_fleet.recipes import ResolvedRecipe

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64


def package(*, secret_refs: list[str] | None = None) -> ExactExecutionPackage:
    recipe = ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": HASH_1,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "acceptance",
                "version": "1.0.0",
                "content_digest": HASH_2,
            },
            "extensions": {},
        }
    )
    payload = b"exact immutable Agency package"
    return ExactExecutionPackage(
        execution_id="execution-1",
        idempotency_key="execution-1",
        resolved_recipe=recipe,
        capabilities_hash=HASH_3,
        target={
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "binding_generation": 7,
            "admission_generation": 9,
        },
        authorization={
            "requester": "peer-controller-1",
            "operation": "fleet.hermes.run",
            "resolved_recipe_hash": recipe.content_hash,
            "policy_digest": HASH_4,
            "deadline_ms": 20_000,
            "secret_refs": secret_refs or [],
        },
        prompt="Return the exact FX8 marker.",
        agency_bundle=ImmutableAgencyBundle(
            resolved=recipe.agent,
            archive_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
            payload=payload,
        ),
    )


class Control:
    def __init__(self, status: str = "admitted") -> None:
        self.status = status
        self.calls: list[str] = []
        self.transitions: list[dict[str, Any]] = []
        self.generation = 1
        self.instance: dict[str, Any] | None = None

    def reserve_admit(self, instance, **kwargs):
        self.calls.append("admit")
        if self.status != "admitted":
            return {"decision": {"status": self.status}}
        returned = self.instance or instance
        self.generation = returned["generation"]
        return {
            "created": self.instance is None,
            "instance": returned,
            "decision": {
                "status": "admitted",
                "instance_id": instance["instance_id"],
                "target": instance["target"],
                "recipe_hash": instance["recipe_hash"],
                "capabilities_hash": instance["capabilities_hash"],
                "operation": "fleet.hermes.run",
                "evaluated_at_ms": 10_000,
            },
        }

    def transition(self, instance_id, *, expected_generation, phase):
        assert expected_generation == self.generation
        self.generation += 1
        self.transitions.append(phase)
        return {"generation": self.generation, "phase": phase}


class Runtime:
    def __init__(
        self, root: Path, events: list[str], *, fail_at: str | None = None
    ) -> None:
        self.root = root
        self.events = events
        self.cleaned = False
        self.fail_at = fail_at
        self.start_calls = 0
        self.inspection: Any = None

    def materialize(self, package, *, secrets):
        self.events.append("materialize")
        profile = self.root / "fleet-exec-execution-1"
        profile.mkdir()
        (profile / "marker").write_text(package.execution_id)
        assert secrets == {}
        return profile.name

    def start(self, profile, *, prompt, session_id, timeout_seconds):
        self.start_calls += 1
        self.events.append("start")
        assert profile == "fleet-exec-execution-1"
        assert prompt == "Return the exact FX8 marker."
        assert session_id == "fleet:execution-1"
        if self.fail_at == "start":
            raise HermesRunSubmissionUnknown("uncertain start response")
        if self.fail_at == "start_known":
            raise HermesRunError("deterministic start rejection")
        return "hermes-run-1"

    def wait(self, profile, *, run_id, timeout_seconds):
        self.events.append("wait")
        if self.fail_at == "wait":
            raise RuntimeError("known terminal failure")
        return HermesRunResult(run_id=run_id, text="FX8_OK")

    def cleanup(self, profile):
        self.events.append("cleanup")
        path = self.root / profile
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
        self.cleaned = True

    def inspect_owner(self, profile):
        return "execution-1"

    def inspect(self, profile, *, run_id):
        self.events.append("inspect")
        return self.inspection


class Secrets:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def resolve(self, references, **kwargs):
        self.events.append("secrets")
        return {}


class Incoming:
    def __init__(self) -> None:
        self.completed = None
        self.failed = None

    async def complete(self, artifacts):
        self.completed = artifacts

    async def fail(self, reason):
        self.failed = reason


def executor(tmp_path, *, status="admitted", fail_at=None):
    from hermes_fleet.destination_recipe_execution import DestinationRecipeExecutor

    events: list[str] = []
    control = Control(status)
    runtime = Runtime(tmp_path, events, fail_at=fail_at)
    return (
        DestinationRecipeExecutor(
            execution_control=control,
            runtime=runtime,
            secret_resolver=Secrets(events),
            current_policy_digest=lambda: HASH_4,
            current_capabilities_hash=lambda: HASH_3,
            now_ms=lambda: 10_000,
        ),
        control,
        runtime,
        events,
    )


def test_destination_denial_precedes_materialization_secrets_and_hermes(
    tmp_path,
) -> None:
    service, control, runtime, events = executor(tmp_path, status="policy_denied")
    incoming = Incoming()

    result = asyncio.run(
        service.execute(
            package=package(),
            authenticated_sender="peer-controller-1",
            incoming=incoming,
        )
    )

    assert result == "policy_denied"
    assert control.calls == ["admit"]
    assert events == []
    assert list(tmp_path.iterdir()) == []
    assert incoming.completed is None
    assert runtime.cleaned is False


def test_destination_success_uses_execution_instance_and_cleans_profile(
    tmp_path,
) -> None:
    service, control, runtime, events = executor(tmp_path)
    incoming = Incoming()

    result = asyncio.run(
        service.execute(
            package=package(),
            authenticated_sender="peer-controller-1",
            incoming=incoming,
        )
    )

    assert result == "completed"
    assert events == ["secrets", "materialize", "start", "wait", "cleanup"]
    assert [phase["kind"] for phase in control.transitions] == [
        "prepared",
        "running",
        "completed",
        "cleanup_pending",
        "cleaned",
    ]
    assert runtime.cleaned is True
    assert list(tmp_path.iterdir()) == []
    assert incoming.completed[0]["parts"][0]["text"] == "FX8_OK"
    assert incoming.completed[0]["parts"][0]["metadata"] == {
        "hermes_run_id": "hermes-run-1",
        "execution_instance_id": "execution-1",
    }


def test_known_hermes_failure_is_durable_cleaned_and_failed_to_keryx(tmp_path) -> None:
    service, control, runtime, events = executor(tmp_path, fail_at="wait")
    incoming = Incoming()

    result = asyncio.run(
        service.execute(
            package=package(),
            authenticated_sender="peer-controller-1",
            incoming=incoming,
        )
    )

    assert result == "failed"
    assert events == ["secrets", "materialize", "start", "wait", "cleanup"]
    assert [phase["kind"] for phase in control.transitions] == [
        "prepared",
        "running",
        "failed",
        "cleanup_pending",
        "cleaned",
    ]
    assert incoming.failed == "Hermes execution failed"
    assert runtime.cleaned is True


def test_uncertain_start_is_indeterminate_and_preserves_profile_for_inspection(
    tmp_path,
) -> None:
    service, control, runtime, events = executor(tmp_path, fail_at="start")
    incoming = Incoming()

    result = asyncio.run(
        service.execute(
            package=package(),
            authenticated_sender="peer-controller-1",
            incoming=incoming,
        )
    )

    assert result == "indeterminate"
    assert events == ["secrets", "materialize", "start"]
    assert [phase["kind"] for phase in control.transitions] == [
        "prepared",
        "indeterminate",
    ]
    assert incoming.failed == "Hermes execution outcome is indeterminate"
    assert runtime.cleaned is False


def test_deterministic_start_failure_is_durable_cleaned_and_failed_to_keryx(
    tmp_path,
) -> None:
    service, control, runtime, events = executor(tmp_path, fail_at="start_known")
    incoming = Incoming()

    result = asyncio.run(
        service.execute(
            package=package(),
            authenticated_sender="peer-controller-1",
            incoming=incoming,
        )
    )

    assert result == "failed"
    assert events == ["secrets", "materialize", "start", "cleanup"]
    assert [phase["kind"] for phase in control.transitions] == [
        "prepared",
        "failed",
        "cleanup_pending",
        "cleaned",
    ]
    assert incoming.failed == "Hermes execution failed"
    assert runtime.cleaned is True


def test_restart_reconciles_exact_completed_run_without_start_or_secrets(
    tmp_path,
) -> None:
    service, control, runtime, events = executor(tmp_path)
    control.instance = {
        "instance_id": "execution-1",
        "idempotency_key": "execution-1",
        "recipe_hash": package().resolved_recipe.content_hash,
        "capabilities_hash": HASH_3,
        "target": package().target,
        "generation": 4,
        "phase": {
            "kind": "running",
            "backend_kind": "hermes.local/profile-runs",
            "realization_id": "fleet-exec-execution-1",
            "keryx_task_id": "execution-1",
            "hermes_run_id": "hermes-run-1",
        },
        "created_at_ms": 1_000,
        "updated_at_ms": 2_000,
    }
    profile = tmp_path / "fleet-exec-execution-1"
    profile.mkdir()
    (profile / "marker").write_text("execution-1")
    runtime.inspection = SimpleNamespace(
        run_id="hermes-run-1", status="completed", text="FX8_RECOVERED"
    )
    incoming = Incoming()

    result = asyncio.run(
        service.execute(
            package=package(),
            authenticated_sender="peer-controller-1",
            incoming=incoming,
        )
    )

    assert result == "completed"
    assert runtime.start_calls == 0
    assert events == ["inspect", "cleanup"]
    assert [phase["kind"] for phase in control.transitions] == [
        "completed",
        "cleanup_pending",
        "cleaned",
    ]
    assert incoming.completed is not None
    assert incoming.completed[0]["parts"][0]["text"] == "FX8_RECOVERED"


def test_replayed_reserved_instance_never_starts_or_resolves_secrets(tmp_path) -> None:
    service, control, runtime, events = executor(tmp_path)
    control.instance = {
        "instance_id": "execution-1",
        "idempotency_key": "execution-1",
        "recipe_hash": package().resolved_recipe.content_hash,
        "capabilities_hash": HASH_3,
        "target": package().target,
        "phase": {"kind": "reserved"},
        "generation": 0,
        "created_at_ms": 1_000,
        "updated_at_ms": 1_000,
    }
    incoming = Incoming()

    with pytest.raises(
        DestinationExecutionError,
        match="reserved execution ownership is uncertain",
    ):
        asyncio.run(
            service.execute(
                package=package(),
                authenticated_sender="peer-controller-1",
                incoming=incoming,
            )
        )

    assert runtime.start_calls == 0
    assert events == []
    assert incoming.failed == "reserved execution ownership is uncertain"
