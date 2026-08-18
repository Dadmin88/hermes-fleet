from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
from hermes_fleet.agent_instance import AgentInstanceManager
from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import ExecutionPlan
from hermes_fleet.hermes_runs import HermesRunResult
from hermes_fleet.network_isolation import NETWORK_NONE, NetworkGrant
from hermes_fleet.profile_inventory import _profile_content_digest
from hermes_fleet.recipes import ResolvedRecipe
from hermes_fleet.run_capsule import (
    DockerRunCapsuleBody,
    RunCapsuleSpec,
    RunCapsuleStore,
)
from hermes_fleet.run_capsule_execution import LocalRunCapsuleExecutor

BASE_IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)
AUTHORITY = "sha256:" + "8" * 64
IDEMPOTENCY = "sha256:" + "9" * 64
PROVENANCE = "sha256:" + "7" * 64
TARGET = {"source": "local", "node_id": "phase8-docker", "generation": 1}
TARGET_DIGEST = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(TARGET, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
)


def _run(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def _docker_ready() -> bool:
    if shutil.which("docker") is None or platform.machine() != "x86_64":
        return False
    return _run(["docker", "image", "inspect", BASE_IMAGE], timeout=10).returncode == 0


def _agency_bundle(tmp_path: Path):
    profile = tmp_path / "agency-source"
    profile.mkdir()
    (profile / "distribution.yaml").write_text(
        "name: phase8-agent\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (profile / "SOUL.md").write_text("phase8 durable brain\n", encoding="utf-8")
    (profile / "config.yaml").write_text(
        "agent:\n  baseline_marker: phase8\n",
        encoding="utf-8",
    )
    (profile / "skills").mkdir()
    digest = _profile_content_digest(profile, "phase8-agent", "1.0.0")
    assert digest is not None
    package = AgencyProfilePackage(
        source=AgencySource("https://example.invalid/agency.git", "a" * 40),
        name="phase8-agent",
        version="1.0.0",
        content_digest=digest,
        category="engineering",
        priority="standard",
        capabilities=("review",),
        distribution_path="profiles/phase8-agent",
        local_path=profile,
    )
    return bundle_agency_profile(package)


def _recipe(bundle) -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": "sha256:" + "6" * 64,
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


def _capabilities() -> BackendCapabilities:
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


def _model_config(tmp_path: Path) -> Path:
    path = tmp_path / "model.yaml"
    path.write_text(
        "model:\n  default: phase8-model\n  provider: phase8-provider\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


class _Runs:
    def __init__(self) -> None:
        self.container_id: str | None = None
        self.plan_fingerprint: str | None = None
        self.start_calls = 0

    def start(self, **kwargs):
        runtime = kwargs["fleet_runtime"]
        self.start_calls += 1
        self.container_id = runtime.container_id
        self.plan_fingerprint = runtime.plan_fingerprint
        assert runtime.image == BASE_IMAGE
        assert runtime.toolsets == ("fleet-terminal",)
        assert runtime.max_iterations == 8
        inspected = _run(["docker", "inspect", runtime.container_id])
        assert inspected.returncode == 0
        document = json.loads(inspected.stdout)[0]
        assert document["State"]["Status"] == "running"
        assert document["HostConfig"]["NetworkMode"] == "none"
        assert document["HostConfig"]["ReadonlyRootfs"] is True
        return "phase8-hermes-run"

    def wait(self, **kwargs):
        assert kwargs["run_id"] == "phase8-hermes-run"
        return HermesRunResult(run_id="phase8-hermes-run", text="PHASE8_OK")

    def inspect(self, run_id):
        raise AssertionError(f"unexpected inspect fallback for {run_id}")

    def finalize(self, run_id, *, timeout_seconds):
        assert run_id == "phase8-hermes-run"
        assert timeout_seconds > 0
        return {
            "run_id": run_id,
            "status": "completed",
            "quiescent": True,
            "command_calls": 1,
            "command_errors": 0,
            "pending_processes": 0,
            "command_evidence_invalid": False,
        }


@pytest.mark.skipif(not _docker_ready(), reason="Docker/pinned amd64 image unavailable")
def test_real_docker_capsule_is_destroyed_while_agent_instance_persists(
    tmp_path: Path,
) -> None:
    bundle = _agency_bundle(tmp_path)
    resolved = _recipe(bundle)
    caps = _capabilities()
    manager = AgentInstanceManager(
        profiles_root=tmp_path / "profiles",
        model_config_path=_model_config(tmp_path),
    )
    agent_id = AgentInstanceManager.identity_for(bundle.resolved)[0]
    execution_id = "phase8-real-docker"
    plan = ExecutionPlan(
        execution_id=execution_id,
        idempotency_key=IDEMPOTENCY,
        resolved_recipe=resolved,
        required_capabilities_hash=caps.content_hash,
    )
    network = NetworkGrant(mode=NETWORK_NONE, authority_ref=AUTHORITY)
    deadline_ms = int(time.time() * 1000) + 60_000
    spec = RunCapsuleSpec(
        execution_id=execution_id,
        idempotency_digest=IDEMPOTENCY,
        agent_instance_id=agent_id,
        principal_id="principal-phase8-local",
        recipe_hash=resolved.recipe_hash,
        resolved_recipe_hash=resolved.content_hash,
        recipe_compiler_version="fleet.recipe-direct.v1",
        requirement_provenance_digest=PROVENANCE,
        run_authority_hash=AUTHORITY,
        capabilities_hash=caps.content_hash,
        target=TARGET,
        target_digest=TARGET_DIGEST,
        project_scope=(),
        network_grant=network,
        network_mode=NETWORK_NONE,
        network_policy_hash=network.policy_hash,
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
        deadline_ms=deadline_ms,
        image=BASE_IMAGE,
        plan_fingerprint=plan.fingerprint,
    )
    store_path = tmp_path / "capsules.sqlite"
    store = RunCapsuleStore(store_path)
    runs = _Runs()
    body = DockerRunCapsuleBody(
        capabilities=caps,
        resolved_recipe=resolved,
        spec=spec,
    )
    released: list[str] = []
    executor = LocalRunCapsuleExecutor(
        store=store,
        instances=manager,
        runs_factory=lambda _profile: runs,
        body_factory=lambda _spec: body,
        now_ms=lambda: int(time.time() * 1000),
        client_releaser=lambda profile: released.append(profile),
    )

    try:
        outcome = executor.execute_initial(
            spec=spec,
            agency_bundle=bundle,
            prompt="prove the disposable body lifecycle",
        )
        assert outcome.status == "completed"
        assert outcome.text == "PHASE8_OK"
        assert outcome.record.state == "finalized"
        assert runs.start_calls == 1
        assert runs.container_id is not None
        assert _run(["docker", "inspect", runs.container_id]).returncode != 0

        binding = manager.open(bundle.resolved)
        assert binding.instance_id == agent_id
        assert manager.profile_path(binding).is_dir()
        assert released == [binding.profile]

        reopened = RunCapsuleStore(store_path)
        recovered = reopened.require_exact(spec)
        assert recovered.state == "finalized"
        assert reopened.list_unfinalized() == ()
    finally:
        if runs.container_id is not None:
            _run(["docker", "rm", "--force", runs.container_id])
