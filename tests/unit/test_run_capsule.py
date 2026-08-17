from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import (
    BackendExecutionHandle,
    BackendExecutionState,
    ExecutionPlan,
)
from hermes_fleet.network_isolation import NETWORK_NONE, NetworkGrant
from hermes_fleet.oci_backend import DockerWorkshopBackend
from hermes_fleet.recipes import ResolvedRecipe
from hermes_fleet.run_capsule import (
    DockerRunCapsuleBody,
    RunCapsuleConflict,
    RunCapsuleIndeterminate,
    RunCapsuleSpec,
    RunCapsuleStore,
)

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64
HASH_5 = "sha256:" + "5" * 64
HASH_6 = "sha256:" + "6" * 64
TARGET = {
    "source": "local",
    "node_id": "node-test",
    "generation": 1,
}
TARGET_DIGEST = "sha256:" + __import__("hashlib").sha256(
    json.dumps(TARGET, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)


def recipe() -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": HASH_1,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "capsule-test",
                "version": "1.0.0",
                "content_digest": HASH_2,
            },
            "extensions": {},
        }
    )


def capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "fleet.dev/docker-oci",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["container"],
            "network": ["none"],
            "resources": {
                "cpu_millis": 1000,
                "memory_bytes": 268_435_456,
            },
            "filesystem": {
                "ephemeral_root": True,
                "read_only_inputs": True,
            },
            "materialization": {
                "agency_profile": True,
                "artifacts": True,
            },
            "extensions": {},
        }
    )


def make_spec(*, execution_id: str = "capsule-exec-1") -> RunCapsuleSpec:
    resolved = recipe()
    caps = capabilities()
    plan = ExecutionPlan(
        execution_id=execution_id,
        idempotency_key=HASH_3,
        resolved_recipe=resolved,
        required_capabilities_hash=caps.content_hash,
    )
    return RunCapsuleSpec(
        execution_id=execution_id,
        idempotency_digest=HASH_3,
        agent_instance_id=HASH_4,
        principal_id="local-principal-test",
        recipe_hash=resolved.content_hash,
        run_authority_hash=HASH_5,
        capabilities_hash=caps.content_hash,
        target=TARGET,
        target_digest=TARGET_DIGEST,
        project_scope=(),
        network_grant=NetworkGrant(
            mode=NETWORK_NONE,
            authority_ref=HASH_5,
        ),
        network_mode=NETWORK_NONE,
        network_policy_hash=NetworkGrant(
            mode=NETWORK_NONE,
            authority_ref=HASH_5,
        ).policy_hash,
        toolsets=("fleet-terminal",),
        approval_budget=0,
        secret_refs=(),
        filesystem_grants=(),
        artifact_grants=(),
        host_broker_grants=(),
        cpu_millis=100,
        memory_bytes=67_108_864,
        pids_limit=16,
        max_iterations=8,
        deadline_ms=2_000_000_000_000,
        image=IMAGE,
        plan_fingerprint=plan.fingerprint,
    )


def test_store_admission_is_idempotent_and_changed_replay_fails(tmp_path: Path) -> None:
    store = RunCapsuleStore(tmp_path / "capsules.sqlite", now_ms=lambda: 1000)
    spec = make_spec()

    first, created = store.admit(spec)
    assert created is True
    assert first.state == "admitted"
    second, created = store.admit(spec)
    assert created is False
    assert second == first

    changed = replace(spec, principal_id="other-principal")
    with pytest.raises(RunCapsuleConflict, match="replay identity"):
        store.admit(changed)


def test_store_generation_fences_state_machine_and_survives_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capsules.sqlite"
    clock = iter(
        (1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009)
    )
    store = RunCapsuleStore(path, now_ms=lambda: next(clock))
    spec = make_spec()
    record, _ = store.admit(spec)
    record = store.transition(spec, expected_generation=1, state="agent_ready")
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="body_ready",
        container_id="a" * 64,
    )
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="run_submitting",
    )
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="running",
        hermes_run_id="run-1",
    )
    with pytest.raises(RunCapsuleConflict, match="generation"):
        store.transition(spec, expected_generation=1, state="terminal")

    reopened = RunCapsuleStore(path, now_ms=lambda: 2000)
    recovered = reopened.require_exact(spec)
    assert recovered.state == "running"
    assert recovered.container_id == "a" * 64
    assert recovered.hermes_run_id == "run-1"
    assert reopened.list_unfinalized() == (recovered,)
    assert path.stat().st_mode & 0o777 == 0o600


def test_store_requires_learning_and_revocation_before_cleanup() -> None:
    spec = make_spec()
    record = type("R", (), {})
    del record
    from hermes_fleet.run_capsule import RunCapsuleError, RunCapsuleRecord

    with pytest.raises(RunCapsuleError, match="revoked grants"):
        RunCapsuleRecord(
            spec=spec,
            generation=9,
            state="cleanup_pending",
            container_id="a" * 64,
            hermes_run_id="run-1",
            evidence={"ok": True},
            grants_revoked=False,
            learning_persisted=True,
            created_at_ms=1000,
            updated_at_ms=1001,
        )


class FakeWorkshop(DockerWorkshopBackend):
    def __init__(self, *, container_id: str = "b" * 64) -> None:
        self.container_id = container_id
        self.ensure_calls = 0
        self.find_calls = 0
        self.cleanup_calls = 0
        self.present = True
        self.last_plan: ExecutionPlan | None = None

    def ensure(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        self.ensure_calls += 1
        self.last_plan = plan
        self.present = True
        return self._handle(plan)

    def find(self, plan: ExecutionPlan) -> BackendExecutionHandle | None:
        self.find_calls += 1
        self.last_plan = plan
        return self._handle(plan) if self.present else None

    def cleanup_plan(
        self,
        plan: ExecutionPlan,
        *,
        handle: BackendExecutionHandle | None = None,
    ) -> None:
        del handle
        self.cleanup_calls += 1
        self.last_plan = plan
        self.present = False

    def _handle(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        return BackendExecutionHandle(
            execution_id=plan.execution_id,
            backend_kind="fleet.dev/docker-oci",
            realization_id=self.container_id,
            plan_fingerprint=plan.fingerprint,
            state=BackendExecutionState.RUNNING,
        )


def test_body_creation_and_recovery_are_separate_api_paths() -> None:
    fake = FakeWorkshop()
    spec = make_spec()
    body = DockerRunCapsuleBody(
        capabilities=capabilities(),
        resolved_recipe=recipe(),
        spec=spec,
        backend_factory=lambda **_kwargs: fake,
    )

    created = body.create_initial()
    assert created.realization_id == fake.container_id
    assert fake.ensure_calls == 1

    recovered = body.recover_exact(fake.container_id)
    assert recovered.realization_id == fake.container_id
    assert fake.ensure_calls == 1
    assert fake.find_calls >= 1

    fake.present = False
    with pytest.raises(RunCapsuleIndeterminate, match="missing"):
        body.recover_exact(fake.container_id)
    assert fake.ensure_calls == 1, "recovery must never manufacture a replacement body"


def test_cleanup_if_present_is_idempotent_and_never_recreates() -> None:
    fake = FakeWorkshop()
    spec = make_spec()
    body = DockerRunCapsuleBody(
        capabilities=capabilities(),
        resolved_recipe=recipe(),
        spec=spec,
        backend_factory=lambda **_kwargs: fake,
    )
    body.create_initial()
    body.cleanup_if_present(fake.container_id)
    body.cleanup_if_present(fake.container_id)
    assert fake.cleanup_calls == 1
    assert fake.ensure_calls == 1


def test_persisted_capsule_json_contains_references_not_secret_bodies(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capsules.sqlite"
    store = RunCapsuleStore(path, now_ms=lambda: 1000)
    spec = make_spec()
    store.admit(spec)
    with sqlite3.connect(path) as connection:
        raw = connection.execute("SELECT spec_json FROM run_capsules").fetchone()[0]
    document = json.loads(raw)
    assert "secret_refs" in document
    assert "API_SERVER_KEY" not in raw
    assert "PRIVATE KEY" not in raw
    assert spec.run_authority_hash in raw
