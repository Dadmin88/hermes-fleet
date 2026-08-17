from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
from hermes_fleet.agent_instance import AgentInstanceManager
from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import (
    BackendExecutionHandle,
    BackendExecutionState,
    ExecutionPlan,
)
from hermes_fleet.hermes_runs import (
    HermesRunDeadlineExceeded,
    HermesRunError,
    HermesRunIndeterminate,
    HermesRunInspection,
    HermesRunResult,
    HermesRunSubmissionUnknown,
)
from hermes_fleet.host_action_broker import HostActionGrant
from hermes_fleet.network_isolation import NETWORK_NONE, NetworkGrant
from hermes_fleet.oci_backend import DockerWorkshopBackend
from hermes_fleet.profile_inventory import _profile_content_digest
from hermes_fleet.recipes import ResolvedRecipe
from hermes_fleet.run_capsule import (
    DockerRunCapsuleBody,
    RunCapsuleIndeterminate,
    RunCapsuleSpec,
    RunCapsuleStore,
)
from hermes_fleet.run_capsule_execution import (
    LocalRunCapsuleExecutor,
    RunCapsuleExecutionError,
)
from hermes_fleet.workspace_isolation import ArtifactExportGrant, DockerWorkspaceIO

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_5 = "sha256:" + "5" * 64
IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)
TARGET = {"source": "local", "node_id": "node-test", "generation": 1}
TARGET_DIGEST = "sha256:" + hashlib.sha256(
    json.dumps(TARGET, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def agency_package(tmp_path: Path) -> AgencyProfilePackage:
    profile = tmp_path / "agency-source"
    profile.mkdir()
    (profile / "distribution.yaml").write_text(
        "name: capsule-agent\nversion: 1.0.0\n", encoding="utf-8"
    )
    (profile / "SOUL.md").write_text("capsule agent\n", encoding="utf-8")
    (profile / "config.yaml").write_text(
        "agent:\n  baseline_marker: capsule\n", encoding="utf-8"
    )
    (profile / "skills").mkdir()
    digest = _profile_content_digest(profile, "capsule-agent", "1.0.0")
    assert digest is not None
    return AgencyProfilePackage(
        source=AgencySource("https://example.invalid/agency.git", "a" * 40),
        name="capsule-agent",
        version="1.0.0",
        content_digest=digest,
        category="engineering",
        priority="standard",
        capabilities=("review",),
        distribution_path="profiles/capsule-agent",
        local_path=profile,
    )


def model_config(tmp_path: Path) -> Path:
    path = tmp_path / "model.yaml"
    path.write_text(
        "model:\n  default: test-model\n  provider: test-provider\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def instances(tmp_path: Path) -> AgentInstanceManager:
    return AgentInstanceManager(
        profiles_root=tmp_path / "profiles",
        model_config_path=model_config(tmp_path),
    )


def recipe(bundle) -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": HASH_1,
            "agent": {
                "kind": "agency_profile",
                "repository": bundle.resolved.repository,
                "revision": bundle.resolved.revision,
                "name": bundle.resolved.name,
                "version": bundle.resolved.version,
                "content_digest": bundle.resolved.content_digest,
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
            "materialization": {"agency_profile": True, "artifacts": True},
            "extensions": {},
        }
    )


def make_spec(
    bundle,
    *,
    execution_id: str = "capsule-execution-1",
    artifact_grants: tuple[ArtifactExportGrant, ...] = (),
    approval_budget: int = 0,
    secret_refs: tuple[str, ...] = (),
    host_broker_grants: tuple[HostActionGrant, ...] = (),
) -> RunCapsuleSpec:
    resolved = recipe(bundle)
    caps = capabilities()
    plan = ExecutionPlan(
        execution_id=execution_id,
        idempotency_key=HASH_3,
        resolved_recipe=resolved,
        required_capabilities_hash=caps.content_hash,
    )
    agent_id = AgentInstanceManager.identity_for(bundle.resolved)[0]
    network = NetworkGrant(mode=NETWORK_NONE, authority_ref=HASH_5)
    return RunCapsuleSpec(
        execution_id=execution_id,
        idempotency_digest=HASH_3,
        agent_instance_id=agent_id,
        principal_id="principal-local-test",
        recipe_hash=resolved.content_hash,
        run_authority_hash=HASH_5,
        capabilities_hash=caps.content_hash,
        target=TARGET,
        target_digest=TARGET_DIGEST,
        project_scope=(),
        network_grant=network,
        network_mode=NETWORK_NONE,
        network_policy_hash=network.policy_hash,
        toolsets=("fleet-terminal",),
        approval_budget=approval_budget,
        secret_refs=secret_refs,
        filesystem_grants=(),
        artifact_grants=artifact_grants,
        host_broker_grants=host_broker_grants,
        cpu_millis=100,
        memory_bytes=67_108_864,
        pids_limit=16,
        max_iterations=8,
        deadline_ms=2_000_000_000_000,
        image=IMAGE,
        plan_fingerprint=plan.fingerprint,
    )


class FakeWorkshop(DockerWorkshopBackend):
    def __init__(self, *, container_id: str = "c" * 64, events=None) -> None:
        self.container_id = container_id
        self.events = events if events is not None else []
        self.ensure_calls = 0
        self.find_calls = 0
        self.cleanup_calls = 0
        self.present = True

    def ensure(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        self.events.append("body.ensure")
        self.ensure_calls += 1
        self.present = True
        return self._handle(plan)

    def find(self, plan: ExecutionPlan) -> BackendExecutionHandle | None:
        self.events.append("body.find")
        self.find_calls += 1
        return self._handle(plan) if self.present else None

    def cleanup_plan(
        self,
        plan: ExecutionPlan,
        *,
        handle: BackendExecutionHandle | None = None,
    ) -> None:
        del plan, handle
        self.events.append("body.cleanup")
        self.cleanup_calls += 1
        self.present = False

    def _handle(self, plan: ExecutionPlan) -> BackendExecutionHandle:
        return BackendExecutionHandle(
            execution_id=plan.execution_id,
            backend_kind="fleet.dev/docker-oci",
            realization_id=self.container_id,
            plan_fingerprint=plan.fingerprint,
            state=BackendExecutionState.RUNNING,
        )


class FakeWorkspace(DockerWorkspaceIO):
    def __init__(self) -> None:
        self.exports = 0

    def stage(self, container_id, resolved) -> None:
        del container_id, resolved

    def export_declared(self, container_id, grants, *, scanner=None):
        del container_id, grants, scanner
        self.exports += 1
        return {"artifact": b"payload"}


class FakeRuns:
    def __init__(self, *, mode: str = "success", events=None) -> None:
        self.mode = mode
        self.events = events if events is not None else []
        self.start_calls = 0
        self.wait_calls = 0
        self.finalize_calls = 0

    def start(self, **kwargs):
        self.events.append("hermes.start")
        self.start_calls += 1
        self.start_kwargs = kwargs
        if self.mode == "submission_unknown":
            raise HermesRunSubmissionUnknown("unknown")
        if self.mode == "start_fail":
            raise HermesRunError("rejected")
        return "hermes-run-1"

    def wait(self, **kwargs):
        self.events.append("hermes.wait")
        self.wait_calls += 1
        self.wait_kwargs = kwargs
        if self.mode == "timeout":
            raise HermesRunDeadlineExceeded("deadline")
        if self.mode == "wait_fail":
            raise HermesRunError("failed")
        if self.mode == "wait_indeterminate":
            raise HermesRunIndeterminate("unknown")
        return HermesRunResult(run_id="hermes-run-1", text="RESULT")

    def inspect(self, run_id):
        self.events.append("hermes.inspect")
        if self.mode == "wait_fail":
            return HermesRunInspection(run_id=run_id, status="failed", text=None)
        return HermesRunInspection(run_id=run_id, status="completed", text="RESULT")

    def finalize(self, run_id, *, timeout_seconds):
        del timeout_seconds
        self.events.append("hermes.finalize")
        self.finalize_calls += 1
        if self.mode == "finalize_fail":
            raise HermesRunIndeterminate("not quiescent")
        status = "cancelled" if self.mode == "timeout" else "completed"
        if self.mode == "wait_fail":
            status = "failed"
        return {
            "run_id": run_id,
            "status": status,
            "quiescent": True,
            "command_calls": 1,
            "command_errors": 0,
            "pending_processes": 0,
            "command_evidence_invalid": False,
        }


def harness(
    tmp_path: Path,
    *,
    mode: str = "success",
    artifact_grants: tuple[ArtifactExportGrant, ...] = (),
    approval_budget: int = 0,
    secret_refs: tuple[str, ...] = (),
    host_broker_grants: tuple[HostActionGrant, ...] = (),
    include_artifact_persister: bool = False,
    include_revoker: bool = True,
):
    events: list[str] = []
    bundle = bundle_agency_profile(agency_package(tmp_path))
    spec = make_spec(
        bundle,
        artifact_grants=artifact_grants,
        approval_budget=approval_budget,
        secret_refs=secret_refs,
        host_broker_grants=host_broker_grants,
    )
    service = instances(tmp_path)
    fake = FakeWorkshop(events=events)
    workspace = FakeWorkspace()
    body = DockerRunCapsuleBody(
        capabilities=capabilities(),
        resolved_recipe=recipe(bundle),
        spec=spec,
        workspace_io=workspace,
        backend_factory=lambda **_kwargs: fake,
        now_ms=lambda: 1000,
    )
    runs = FakeRuns(mode=mode, events=events)
    store = RunCapsuleStore(tmp_path / "capsules.sqlite", now_ms=lambda: 1000)
    releases: list[str] = []

    def learning(agent, record, evidence):
        del agent, record, evidence
        events.append("learning.persist")
        return {"status": "persisted"}

    def persist_artifacts(_record, artifacts):
        events.append("artifact.persist")
        return {
            name: {
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
            for name, payload in artifacts.items()
        }

    def revoke(_spec):
        events.append("grants.revoke")

    executor = LocalRunCapsuleExecutor(
        store=store,
        instances=service,
        runs_factory=lambda _profile: runs,
        body_factory=lambda _spec: body,
        now_ms=lambda: 1000,
        learning_persister=learning,
        artifact_persister=(
            persist_artifacts if include_artifact_persister else None
        ),
        grant_revoker=(revoke if include_revoker else None),
        client_releaser=lambda profile: releases.append(profile),
    )
    return (
        bundle,
        spec,
        service,
        fake,
        workspace,
        runs,
        store,
        executor,
        events,
        releases,
    )


def test_success_orders_quiescence_learning_revocation_cleanup_and_keeps_agent(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        service,
        fake,
        _workspace,
        runs,
        store,
        executor,
        events,
        releases,
    ) = harness(tmp_path)
    outcome = executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")

    assert outcome.status == "completed"
    assert outcome.text == "RESULT"
    assert outcome.record.state == "finalized"
    assert fake.present is False
    assert fake.cleanup_calls == 1
    assert runs.start_kwargs["fleet_runtime"].container_id == fake.container_id
    assert events.index("hermes.finalize") < events.index("learning.persist")
    assert events.index("learning.persist") < events.index("grants.revoke")
    assert events.index("grants.revoke") < events.index("body.cleanup")
    assert service.profile_path(service.open(bundle.resolved)).is_dir()
    assert releases
    assert store.list_unfinalized() == ()


def test_timeout_is_finalized_quiescent_and_cleaned(tmp_path: Path) -> None:
    (
        bundle,
        spec,
        service,
        fake,
        _workspace,
        _runs,
        _store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path, mode="timeout")
    outcome = executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")
    assert outcome.status == "timed_out"
    assert outcome.record.state == "finalized"
    assert fake.present is False
    assert service.profile_path(service.open(bundle.resolved)).is_dir()


def test_submission_unknown_retains_exact_body_and_agent_for_recovery(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        service,
        fake,
        _workspace,
        runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path, mode="submission_unknown")
    with pytest.raises(RunCapsuleIndeterminate, match="submission outcome"):
        executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")
    record = store.require_exact(spec)
    assert record.state == "indeterminate"
    assert record.container_id == fake.container_id
    assert record.hermes_run_id is None
    assert fake.present is True
    assert fake.cleanup_calls == 0
    assert runs.start_calls == 1
    assert service.profile_path(service.open(bundle.resolved)).is_dir()


def test_finalize_failure_retains_body_until_quiescence_can_be_proven(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        _runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path, mode="finalize_fail")
    with pytest.raises(RunCapsuleIndeterminate, match="quiescence"):
        executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")
    record = store.require_exact(spec)
    assert record.state == "indeterminate"
    assert record.hermes_run_id == "hermes-run-1"
    assert fake.present is True
    assert fake.cleanup_calls == 0


def test_recovery_of_known_running_run_never_calls_body_ensure(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path)
    record, _ = store.admit(spec)
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="agent_ready",
    )
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="body_ready",
        container_id=fake.container_id,
    )
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="run_submitting",
    )
    store.transition(
        spec,
        expected_generation=record.generation,
        state="running",
        hermes_run_id="hermes-run-1",
    )

    outcome = executor.recover(spec=spec, agency_bundle=bundle)
    assert outcome.status == "completed"
    assert fake.ensure_calls == 0
    assert fake.find_calls >= 1
    assert runs.start_calls == 0
    assert runs.wait_calls == 1
    assert fake.present is False


def test_recovery_without_durable_container_binding_fails_without_creation(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path)
    record, _ = store.admit(spec)
    store.transition(
        spec,
        expected_generation=record.generation,
        state="agent_ready",
    )
    fake.present = False
    with pytest.raises(RunCapsuleIndeterminate, match="no exact existing body"):
        executor.recover(spec=spec, agency_bundle=bundle)
    assert fake.ensure_calls == 0
    assert runs.start_calls == 0


def test_recovery_discovers_existing_body_by_plan_without_creation(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path)
    record, _ = store.admit(spec)
    store.transition(
        spec,
        expected_generation=record.generation,
        state="agent_ready",
    )

    with pytest.raises(RunCapsuleIndeterminate, match="no durable Hermes run"):
        executor.recover(spec=spec, agency_bundle=bundle)

    recovered = store.require_exact(spec)
    assert recovered.state == "indeterminate"
    assert recovered.container_id == fake.container_id
    assert fake.ensure_calls == 0
    assert fake.find_calls >= 1
    assert runs.start_calls == 0


def test_recovery_run_submitting_never_resubmits(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path)
    record, _ = store.admit(spec)
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="agent_ready",
    )
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="body_ready",
        container_id=fake.container_id,
    )
    store.transition(
        spec,
        expected_generation=record.generation,
        state="run_submitting",
    )

    with pytest.raises(RunCapsuleIndeterminate, match="resubmission is forbidden"):
        executor.recover(spec=spec, agency_bundle=bundle)

    assert fake.ensure_calls == 0
    assert runs.start_calls == 0
    assert fake.present is True


def test_recovery_body_ready_never_resubmits_unknown_hermes_run(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path)
    record, _ = store.admit(spec)
    record = store.transition(
        spec,
        expected_generation=record.generation,
        state="agent_ready",
    )
    store.transition(
        spec,
        expected_generation=record.generation,
        state="body_ready",
        container_id=fake.container_id,
    )
    with pytest.raises(RunCapsuleIndeterminate, match="no durable Hermes run"):
        executor.recover(spec=spec, agency_bundle=bundle)
    assert fake.ensure_calls == 0
    assert runs.start_calls == 0
    assert fake.present is True


def test_definite_start_failure_cleans_body_without_deleting_agent(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        service,
        fake,
        _workspace,
        _runs,
        _store,
        executor,
        _events,
        _releases,
    ) = harness(tmp_path, mode="start_fail")
    outcome = executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")
    assert outcome.status == "failed"
    assert outcome.record.state == "finalized"
    assert fake.present is False
    assert service.profile_path(service.open(bundle.resolved)).is_dir()


def test_declared_artifacts_are_persisted_before_learning_revocation_and_cleanup(
    tmp_path: Path,
) -> None:
    artifact = ArtifactExportGrant(
        name="artifact",
        path="/workspace/out/artifact",
        max_bytes=1024,
    )
    (
        bundle,
        spec,
        _service,
        fake,
        workspace,
        _runs,
        _store,
        executor,
        events,
        _releases,
    ) = harness(
        tmp_path,
        artifact_grants=(artifact,),
        include_artifact_persister=True,
    )

    outcome = executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")

    assert outcome.status == "completed"
    assert outcome.artifacts == {"artifact": b"payload"}
    assert workspace.exports == 1
    assert events.index("hermes.finalize") < events.index("artifact.persist")
    assert events.index("artifact.persist") < events.index("learning.persist")
    assert events.index("learning.persist") < events.index("grants.revoke")
    assert events.index("grants.revoke") < events.index("body.cleanup")
    artifact_evidence = outcome.record.evidence["artifacts"]["artifact"]
    assert artifact_evidence["bytes"] == len(b"payload")
    assert artifact_evidence["sha256"].startswith("sha256:")
    assert b"payload" not in json.dumps(outcome.record.evidence).encode()
    assert fake.present is False


def test_temporary_powers_cannot_reach_cleanup_without_explicit_revocation(
    tmp_path: Path,
) -> None:
    host_grant = HostActionGrant(
        verb="query-approved-health",
        target="service-test",
        parameters_digest=HASH_2,
        max_calls=1,
    )
    (
        bundle,
        spec,
        service,
        fake,
        _workspace,
        _runs,
        store,
        executor,
        _events,
        _releases,
    ) = harness(
        tmp_path,
        approval_budget=1,
        secret_refs=("secret://test/reference",),
        host_broker_grants=(host_grant,),
        include_revoker=False,
    )

    with pytest.raises(RunCapsuleExecutionError, match="revocation callback"):
        executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")

    record = store.require_exact(spec)
    assert record.state == "learning_persisted"
    assert record.grants_revoked is False
    assert fake.present is True
    assert fake.cleanup_calls == 0
    assert service.profile_path(service.open(bundle.resolved)).is_dir()


def test_temporary_powers_are_revoked_before_body_cleanup(
    tmp_path: Path,
) -> None:
    host_grant = HostActionGrant(
        verb="query-approved-health",
        target="service-test",
        parameters_digest=HASH_2,
        max_calls=1,
    )
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        _runs,
        store,
        executor,
        events,
        _releases,
    ) = harness(
        tmp_path,
        approval_budget=1,
        secret_refs=("secret://test/reference",),
        host_broker_grants=(host_grant,),
    )

    outcome = executor.execute_initial(spec=spec, agency_bundle=bundle, prompt="work")

    assert outcome.status == "completed"
    assert outcome.record.state == "finalized"
    assert outcome.record.grants_revoked is True
    assert store.require_exact(spec).grants_revoked is True
    assert events.index("grants.revoke") < events.index("body.cleanup")
    assert fake.present is False
