from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
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

BASE_IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)
HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64


def _run_bytes(
    argv: list[str], *, timeout_seconds: float = 30.0
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=False,
        timeout=timeout_seconds,
    )


def _candidate():
    document = {
        "schema": "fleet.workflow-editor.v2",
        "id": "real-probe-workflow",
        "name": "Real probe",
        "nodes": [
            {
                "id": "probe-step",
                "type": "recipe-step",
                "title": "Probe Step",
                "position": {"x": 0, "y": 0},
                "configuration": {
                    "agent_name": "developer",
                    "agent_version": ">=1,<2",
                    "cpu_min_millis": 100,
                    "cpu_requested_millis": 250,
                    "cpu_limit_millis": 500,
                    "memory_min_bytes": 67_108_864,
                    "memory_requested_bytes": 134_217_728,
                    "memory_limit_bytes": 268_435_456,
                    "runtime_image": BASE_IMAGE,
                    "toolchains": "shell",
                },
                "target": None,
                "runtime": "recipe",
            }
        ],
        "connections": [],
        "metadata": {"executionAvailable": False},
    }
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    revision = WorkflowRevisionSnapshot.from_backend(
        {
            "workflowId": document["id"],
            "version": 1,
            "contentHash": hashlib.sha256(payload).hexdigest(),
            "document": document,
            "createdAtMs": 1,
        }
    )
    return (
        WorkflowRecipeCompiler()
        .compile(
            revision,
            CompilerContext(
                project=ProjectEvidence.empty(),
                agency_fingerprint=HASH_1,
                runtime_fingerprint=HASH_2,
                policy_fingerprint=HASH_3,
                capabilities_fingerprint=HASH_4,
            ),
        )
        .recipes[0]
    )


def _capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "fleet.dev/docker-oci",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["container"],
            "network": ["none"],
            "resources": {"cpu_millis": 1000, "memory_bytes": 536_870_912},
            "filesystem": {"ephemeral_root": True, "read_only_inputs": True},
            "materialization": {"agency_profile": True, "artifacts": True},
            "extensions": {},
        }
    )


def _agent() -> ResolvedAgencyProfile:
    return ResolvedAgencyProfile(
        repository="https://example.invalid/agency.git",
        revision="b" * 40,
        name="developer",
        version="1.0.0",
        content_digest=HASH_1,
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
@pytest.mark.skipif(platform.machine() != "x86_64", reason="pinned base image is amd64")
def test_real_discovery_probe_is_low_authority_and_destroyed_after_observation() -> (
    None
):
    image = _run_bytes(["docker", "image", "inspect", BASE_IMAGE], timeout_seconds=10)
    if image.returncode != 0:
        pytest.skip("pinned base image is unavailable locally")

    inspected: dict[str, object] = {}

    def command(argv: list[str], *, timeout_seconds: float) -> str:
        container_id = argv[4]
        inspect = _run_bytes(["docker", "inspect", container_id], timeout_seconds=10)
        if inspect.returncode != 0:
            raise RecipeDiscoveryError("probe inspection failed")
        document = json.loads(inspect.stdout.decode())[0]
        config = document["Config"]
        host = document["HostConfig"]
        inspected.update(
            {
                "container_id": container_id,
                "user": config["User"],
                "readonly": host["ReadonlyRootfs"],
                "network": host["NetworkMode"],
                "binds": host["Binds"],
                "cap_drop": host["CapDrop"],
                "security_opt": host["SecurityOpt"],
                "pids": host["PidsLimit"],
                "memory": host["Memory"],
                "env": config.get("Env") or [],
            }
        )
        completed = _run_bytes(argv, timeout_seconds=timeout_seconds)
        if completed.returncode != 0:
            raise RecipeDiscoveryError("probe command failed")
        return completed.stdout.decode()

    policy = DiscoveryProbePolicy(
        image=BASE_IMAGE,
        cpu_millis=250,
        memory_bytes=134_217_728,
        pids_limit=32,
        deadline_ms=30_000,
    )
    result = DockerRecipeDiscoveryProbe(
        capabilities=_capabilities(),
        policy=policy,
        command=command,
    ).run(
        candidate=_candidate(),
        resolved_agent=_agent(),
        argv=("id", "-u"),
    )

    assert result.stdout.strip() == "65532"
    assert inspected["user"] == "65532:65532"
    assert inspected["readonly"] is True
    assert inspected["network"] == "none"
    assert inspected["binds"] in (None, [])
    assert "ALL" in (inspected["cap_drop"] or [])
    assert any(
        "no-new-privileges" in item for item in (inspected["security_opt"] or [])
    )
    assert inspected["pids"] == 32
    assert inspected["memory"] == 134_217_728
    assert not any(
        value.startswith(("FLEET_", "KERYX_", "NODESCALE_", "SSH_"))
        for value in inspected["env"]
    )

    absent = _run_bytes(["docker", "inspect", result.container_id], timeout_seconds=10)
    assert absent.returncode != 0
