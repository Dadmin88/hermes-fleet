from __future__ import annotations

import platform
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
from hermes_fleet.agent_instance import AgentInstanceManager
from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import BackendExecutionState, ExecutionPlan
from hermes_fleet.oci_backend import DockerWorkshopBackend, OciRealizationSpec
from hermes_fleet.profile_inventory import _profile_content_digest
from hermes_fleet.recipes import ResolvedRecipe

BASE_IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)


def _bundle(tmp_path: Path):
    profile = tmp_path / "agency-source"
    profile.mkdir()
    (profile / "distribution.yaml").write_text(
        "name: persistent-body-example\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (profile / "SOUL.md").write_text("persistent brain\n", encoding="utf-8")
    (profile / "skills").mkdir()
    digest = _profile_content_digest(profile, "persistent-body-example", "1.0.0")
    assert digest is not None
    package = AgencyProfilePackage(
        source=AgencySource("https://example.invalid/agency.git", "b" * 40),
        name="persistent-body-example",
        version="1.0.0",
        content_digest=digest,
        category="engineering",
        priority="standard",
        capabilities=("review",),
        distribution_path="profiles/persistent-body-example",
        local_path=profile,
    )
    return bundle_agency_profile(package)


def _capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "fleet.dev/docker-oci",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["container"],
            "network": ["none"],
            "resources": {"cpu_millis": 1000, "memory_bytes": 268_435_456},
            "filesystem": {"ephemeral_root": True, "read_only_inputs": True},
            "materialization": {"agency_profile": True, "artifacts": True},
            "extensions": {},
        }
    )


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


def _backend(capabilities: BackendCapabilities) -> DockerWorkshopBackend:
    return DockerWorkshopBackend(
        capabilities=capabilities,
        realization=OciRealizationSpec(
            image=BASE_IMAGE,
            argv=("sleep", "infinity"),
            network="none",
            cpu_millis=100,
            memory_bytes=67_108_864,
            pids_limit=16,
        ),
        deadline_ms=int(time.time() * 1_000) + 60_000,
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
@pytest.mark.skipif(
    platform.machine() != "x86_64",
    reason="pinned base image is amd64",
)
def test_agent_instance_survives_two_disposable_workshop_lifecycles(
    tmp_path: Path,
) -> None:
    probe = subprocess.run(
        ["docker", "image", "inspect", BASE_IMAGE],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip("pinned base image is unavailable locally")

    model_config = tmp_path / "hermes-config.yaml"
    model_config.write_text(
        "model:\n  default: persistent-model\n  provider: provider-test\n",
        encoding="utf-8",
    )
    model_config.chmod(0o600)
    manager = AgentInstanceManager(
        profiles_root=tmp_path / "profiles",
        model_config_path=model_config,
    )
    bundle = _bundle(tmp_path)
    binding = manager.ensure(bundle)
    profile = manager.profile_path(binding)
    learned = profile / "skills" / "learned" / "SKILL.md"
    with manager.mutation_guard(
        binding,
        component="skills",
        expected_generation=0,
    ):
        learned.parent.mkdir(parents=True)
        learned.write_text("learned before disposable bodies\n", encoding="utf-8")

    config_before = (profile / "config.yaml").read_bytes()
    metadata_before = (profile / ".fleet-agent-instance.json").read_bytes()
    recipe = _recipe(bundle)
    capabilities = _capabilities()

    seen_containers: list[str] = []
    for _ in range(2):
        execution_id = f"phase6-{uuid.uuid4().hex[:12]}"
        plan = ExecutionPlan(
            execution_id=execution_id,
            idempotency_key=f"request-{execution_id}",
            resolved_recipe=recipe,
            required_capabilities_hash=capabilities.content_hash,
        )
        backend = _backend(capabilities)
        handle = None
        try:
            handle = backend.ensure(plan)
            assert handle.state == BackendExecutionState.RUNNING
            assert handle.realization_id not in seen_containers
            seen_containers.append(handle.realization_id)
            backend.cleanup_plan(plan, handle=handle)
            absent = subprocess.run(
                ["docker", "inspect", handle.realization_id],
                capture_output=True,
                check=False,
                timeout=10,
            )
            assert absent.returncode != 0
            handle = None
        finally:
            if handle is not None:
                try:
                    backend.cleanup_plan(plan, handle=handle)
                except Exception:
                    pass

        reopened = manager.open(bundle.resolved)
        assert reopened == binding
        assert (profile / "config.yaml").read_bytes() == config_before
        assert (profile / ".fleet-agent-instance.json").read_bytes() == metadata_before
        assert manager.read_state(binding).skills_generation == 1
        assert learned.read_text(encoding="utf-8") == (
            "learned before disposable bodies\n"
        )

    assert len(seen_containers) == 2
    assert seen_containers[0] != seen_containers[1]
