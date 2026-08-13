from __future__ import annotations

import json
import platform
import shutil
import subprocess
import time
import uuid

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import BackendExecutionState, ExecutionPlan
from hermes_fleet.oci_backend import DockerExecutionBackend, OciRealizationSpec
from hermes_fleet.recipes import ResolvedRecipe

BASE_IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)


def _run(argv: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("Docker acceptance command failed")
    return completed.stdout.strip()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
@pytest.mark.skipif(platform.machine() != "x86_64", reason="pinned base image is amd64")
def test_real_docker_lifecycle_is_hardened_and_leaves_zero_container_residue() -> None:
    probe = subprocess.run(
        ["docker", "image", "inspect", BASE_IMAGE],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip("pinned base image is unavailable locally")
    nonce = uuid.uuid4().hex[:12]
    execution_id = f"fx4{nonce}"
    recipe = ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": "sha256:" + "1" * 64,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "researcher",
                "version": "1.0.0",
                "content_digest": "sha256:" + "2" * 64,
            },
            "extensions": {},
        }
    )
    dockerfile = f"""FROM {BASE_IMAGE}
LABEL dev.hermes.agency.repository={recipe.agent.repository}
LABEL dev.hermes.agency.revision={recipe.agent.revision}
LABEL dev.hermes.agency.profile={recipe.agent.name}
LABEL dev.hermes.agency.version={recipe.agent.version}
LABEL dev.hermes.agency.content={recipe.agent.content_digest}
"""
    image = ""
    handle = None
    backend = None
    try:
        image = _run(["docker", "build", "--quiet", "-"], input_text=dockerfile)
        capabilities = BackendCapabilities.from_dict(
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
                "materialization": {"agency_profile": True, "artifacts": False},
                "extensions": {},
            }
        )
        backend = DockerExecutionBackend(
            capabilities=capabilities,
            realization=OciRealizationSpec(
                image=image,
                argv=("/bin/sh", "-c", "exit 0"),
                network="none",
                cpu_millis=100,
                memory_bytes=33_554_432,
                pids_limit=16,
            ),
        )
        plan = ExecutionPlan(
            execution_id=execution_id,
            idempotency_key=f"request-{nonce}",
            resolved_recipe=recipe,
            required_capabilities_hash=capabilities.content_hash,
        )
        handle = backend.prepare(plan)
        document = json.loads(_run(["docker", "inspect", handle.realization_id]))[0]
        assert document["HostConfig"]["ReadonlyRootfs"] is True
        assert document["HostConfig"]["NetworkMode"] == "none"
        assert document["HostConfig"]["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in document["HostConfig"]["SecurityOpt"]
        terminal = backend.start(handle)
        deadline = time.monotonic() + 5
        while (
            terminal.state == BackendExecutionState.RUNNING
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
            terminal = backend.inspect(terminal)
        assert terminal.state == BackendExecutionState.COMPLETED
        backend.cleanup(terminal)
        absent = subprocess.run(
            ["docker", "inspect", handle.realization_id],
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert absent.returncode != 0
    finally:
        if backend is not None and handle is not None:
            try:
                backend.cleanup(handle)
            except Exception:
                pass
        subprocess.run(
            ["docker", "rm", "--force", f"hermes-fleet-{execution_id}"],
            capture_output=True,
            check=False,
        )
        if image:
            subprocess.run(
                ["docker", "image", "rm", "--force", image],
                capture_output=True,
                check=False,
            )
