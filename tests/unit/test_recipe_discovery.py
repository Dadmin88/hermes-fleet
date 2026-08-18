from __future__ import annotations

import json

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import BackendExecutionHandle, BackendExecutionState
from hermes_fleet.oci_backend import DockerWorkshopBackend
from hermes_fleet.recipe_discovery import (
    DockerRecipeDiscoveryProbe,
    RecipeDiscoveryError,
)
from hermes_fleet.recipes import ResolvedAgencyProfile
from hermes_fleet.workflow_recipe_compiler import (
    CompilerContext,
    DiscoveryProbePolicy,
    ProjectEvidence,
    WorkflowRecipeCompiler,
    WorkflowRevisionSnapshot,
)

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64
IMAGE = "example.invalid/probe@sha256:" + "a" * 64


def workflow_candidate():
    document = {
        "schema": "fleet.workflow-editor.v2",
        "id": "probe-workflow",
        "name": "Probe",
        "nodes": [
            {
                "id": "probe-step",
                "type": "recipe-step",
                "title": "Probe Step",
                "position": {"x": 0, "y": 0},
                "configuration": {
                    "agent_name": "developer",
                    "agent_version": ">=1,<2",
                    "cpu_min_millis": 250,
                    "cpu_requested_millis": 500,
                    "cpu_limit_millis": 1000,
                    "memory_min_bytes": 134_217_728,
                    "memory_requested_bytes": 268_435_456,
                    "memory_limit_bytes": 536_870_912,
                    "runtime_image": IMAGE,
                    "toolchains": "python",
                },
                "target": None,
                "runtime": "recipe",
            }
        ],
        "connections": [],
        "metadata": {"executionAvailable": False},
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    import hashlib

    revision = WorkflowRevisionSnapshot.from_backend(
        {
            "workflowId": "probe-workflow",
            "version": 1,
            "contentHash": hashlib.sha256(encoded).hexdigest(),
            "document": document,
            "createdAtMs": 1,
        }
    )
    context = CompilerContext(
        project=ProjectEvidence.empty(),
        agency_fingerprint=HASH_1,
        runtime_fingerprint=HASH_2,
        policy_fingerprint=HASH_3,
        capabilities_fingerprint=HASH_4,
    )
    return WorkflowRecipeCompiler().compile(revision, context).recipes[0]


def resolved_agent(name: str = "developer") -> ResolvedAgencyProfile:
    return ResolvedAgencyProfile(
        repository="https://example.invalid/agency.git",
        revision="b" * 40,
        name=name,
        version="1.0.0",
        content_digest=HASH_1,
    )


def capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "fleet.dev/docker-oci",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["container"],
            "network": ["none"],
            "resources": {"cpu_millis": 4000, "memory_bytes": 8_589_934_592},
            "filesystem": {"ephemeral_root": True, "read_only_inputs": True},
            "materialization": {"agency_profile": True, "artifacts": True},
            "extensions": {},
        }
    )


class FakeWorkshop(DockerWorkshopBackend):
    def __init__(self, *, cleanup_fails: bool = False) -> None:
        self.container_id = "c" * 64
        self.cleanup_fails = cleanup_fails
        self.ensure_calls = 0
        self.inspect_calls = 0
        self.cleanup_calls = 0
        self.find_calls = 0
        self.present = False

    def ensure(self, plan):
        self.ensure_calls += 1
        self.present = True
        return BackendExecutionHandle(
            execution_id=plan.execution_id,
            backend_kind="fleet.dev/docker-oci",
            realization_id=self.container_id,
            plan_fingerprint=plan.fingerprint,
            state=BackendExecutionState.RUNNING,
        )

    def inspect(self, handle):
        self.inspect_calls += 1
        return handle

    def cleanup_plan(self, plan, *, handle=None):
        del plan, handle
        self.cleanup_calls += 1
        if self.cleanup_fails:
            raise RuntimeError("cleanup failed")
        self.present = False

    def find(self, plan):
        del plan
        self.find_calls += 1
        return (
            None
            if not self.present
            else BackendExecutionHandle(
                execution_id="discovery-placeholder",
                backend_kind="fleet.dev/docker-oci",
                realization_id=self.container_id,
                plan_fingerprint=HASH_1,
                state=BackendExecutionState.RUNNING,
            )
        )


def test_probe_uses_low_authority_realization_direct_argv_and_always_cleans() -> None:
    fake = FakeWorkshop()
    observed_factory: dict = {}
    observed_command: dict = {}

    def factory(**kwargs):
        observed_factory.update(kwargs)
        return fake

    def command(argv, *, timeout_seconds):
        observed_command["argv"] = argv
        observed_command["timeout_seconds"] = timeout_seconds
        return "Python 3.12.0\n"

    policy = DiscoveryProbePolicy(image=IMAGE)
    probe = DockerRecipeDiscoveryProbe(
        capabilities=capabilities(),
        policy=policy,
        now_ms=lambda: 1_000,
        backend_factory=factory,
        command=command,
    )
    candidate = workflow_candidate()
    result = probe.run(
        candidate=candidate,
        resolved_agent=resolved_agent(),
        argv=("python", "--version"),
    )

    realization = observed_factory["realization"]
    assert realization.image == IMAGE
    assert realization.network == "none"
    assert realization.cpu_millis == policy.cpu_millis
    assert realization.memory_bytes == policy.memory_bytes
    assert realization.pids_limit == policy.pids_limit
    assert observed_command["argv"] == [
        "docker",
        "exec",
        "--user",
        "65532:65532",
        fake.container_id,
        "python",
        "--version",
    ]
    assert result.stdout == "Python 3.12.0\n"
    assert result.candidate_hash == candidate.content_hash
    assert result.policy_hash == policy.content_hash
    assert result.evidence().kind == "probe"
    observation = result.observation(
        "runtime",
        {"image": IMAGE, "toolchains": ["python-3.12"]},
    )
    assert observation.requirement_key == "runtime"
    assert observation.evidence_digest == result.stdout_hash
    assert fake.ensure_calls == 1
    assert fake.inspect_calls == 1
    assert fake.cleanup_calls == 1
    assert fake.present is False


def test_probe_cleanup_runs_when_probe_command_fails() -> None:
    fake = FakeWorkshop()

    def command(_argv, *, timeout_seconds):
        del timeout_seconds
        raise RecipeDiscoveryError("probe failed")

    probe = DockerRecipeDiscoveryProbe(
        capabilities=capabilities(),
        policy=DiscoveryProbePolicy(image=IMAGE),
        now_ms=lambda: 1_000,
        backend_factory=lambda **_kwargs: fake,
        command=command,
    )
    with pytest.raises(RecipeDiscoveryError, match="probe failed"):
        probe.run(
            candidate=workflow_candidate(),
            resolved_agent=resolved_agent(),
            argv=("python", "--version"),
        )
    assert fake.cleanup_calls == 1
    assert fake.present is False


def test_probe_cleanup_failure_is_never_hidden_by_probe_failure() -> None:
    fake = FakeWorkshop(cleanup_fails=True)

    def command(_argv, *, timeout_seconds):
        del timeout_seconds
        raise RecipeDiscoveryError("probe failed")

    probe = DockerRecipeDiscoveryProbe(
        capabilities=capabilities(),
        policy=DiscoveryProbePolicy(image=IMAGE),
        now_ms=lambda: 1_000,
        backend_factory=lambda **_kwargs: fake,
        command=command,
    )
    with pytest.raises(RecipeDiscoveryError, match="cleanup is unproven"):
        probe.run(
            candidate=workflow_candidate(),
            resolved_agent=resolved_agent(),
            argv=("python", "--version"),
        )
    assert fake.cleanup_calls == 1
    assert fake.present is True


def test_probe_rejects_secret_like_argv_and_wrong_agent_before_body_creation() -> None:
    fake = FakeWorkshop()
    probe = DockerRecipeDiscoveryProbe(
        capabilities=capabilities(),
        policy=DiscoveryProbePolicy(image=IMAGE),
        backend_factory=lambda **_kwargs: fake,
        command=lambda *_args, **_kwargs: "",
    )
    with pytest.raises(RecipeDiscoveryError, match="unsafe"):
        probe.run(
            candidate=workflow_candidate(),
            resolved_agent=resolved_agent(),
            argv=("tool", "api_key=should-never-enter-probe"),
        )
    assert fake.ensure_calls == 0

    with pytest.raises(RecipeDiscoveryError, match="does not satisfy"):
        probe.run(
            candidate=workflow_candidate(),
            resolved_agent=resolved_agent("other-agent"),
            argv=("tool", "--version"),
        )
    assert fake.ensure_calls == 0


def test_probe_policy_cannot_exceed_backend_capacity() -> None:
    with pytest.raises(RecipeDiscoveryError, match="CPU"):
        DockerRecipeDiscoveryProbe(
            capabilities=capabilities(),
            policy=DiscoveryProbePolicy(image=IMAGE, cpu_millis=10_000),
        )
