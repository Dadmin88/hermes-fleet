from __future__ import annotations

import io
import platform
import shutil
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import BackendExecutionState, ExecutionPlan
from hermes_fleet.oci_backend import DockerWorkshopBackend, OciRealizationSpec
from hermes_fleet.recipes import ResolvedRecipe
from hermes_fleet.workspace_isolation import (
    ArtifactExportGrant,
    DockerWorkspaceIO,
    FilesystemAuthorityScope,
    FilesystemGrant,
    ProjectWorkspaceResolver,
)

BASE_IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)
AUTHORITY = "sha256:" + "8" * 64
WRITE_AUTHORITY = "sha256:" + "9" * 64


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


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


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": "sha256:" + "6" * 64,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "c" * 40,
                "name": "workspace-proof",
                "version": "1.0.0",
                "content_digest": "sha256:" + "7" * 64,
            },
            "extensions": {},
        }
    )


def _plan(execution_id: str) -> ExecutionPlan:
    capabilities = _capabilities()
    return ExecutionPlan(
        execution_id=execution_id,
        idempotency_key=f"request-{execution_id}",
        resolved_recipe=_recipe(),
        required_capabilities_hash=capabilities.content_hash,
    )


def _backend() -> DockerWorkshopBackend:
    return DockerWorkshopBackend(
        capabilities=_capabilities(),
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
def test_real_workspace_projection_export_and_cross_run_isolation(
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

    project = tmp_path / "project"
    project.mkdir()
    read_source = project / "readonly"
    read_source.mkdir()
    (read_source / "input.txt").write_text("input-one", encoding="utf-8")
    write_source = project / "editable"
    write_source.mkdir()
    (write_source / "draft.txt").write_text("draft-one", encoding="utf-8")

    resolver = ProjectWorkspaceResolver({"project-1": project})
    grants = resolver.resolve(
        (
            FilesystemGrant(
                project_id="project-1",
                relative_path="readonly",
                target="/workspace/inputs/input",
                mode="read",
                max_bytes=1024,
                authority_ref=AUTHORITY,
            ),
            FilesystemGrant(
                project_id="project-1",
                relative_path="editable",
                target="/workspace/work/edit",
                mode="write",
                max_bytes=1024,
                authority_ref=AUTHORITY,
                write_authority_ref=WRITE_AUTHORITY,
            ),
        ),
        authority=FilesystemAuthorityScope(
            run_authority_hash=AUTHORITY,
            write_authority_hashes=(WRITE_AUTHORITY,),
        ),
    )

    first_plan = _plan(f"phase3a{uuid.uuid4().hex[:12]}")
    first_backend = _backend()
    first = None
    second = None
    second_backend = None
    workspace = DockerWorkspaceIO()
    try:
        first = first_backend.ensure(first_plan)
        assert first.state == BackendExecutionState.RUNNING
        for resolved in grants:
            workspace.stage(first.realization_id, resolved)

        read = _run(
            [
                "docker",
                "exec",
                first.realization_id,
                "cat",
                "/workspace/inputs/input/input.txt",
            ]
        )
        assert read.returncode == 0
        assert read.stdout == "input-one"

        denied = _run(
            [
                "docker",
                "exec",
                first.realization_id,
                "sh",
                "-c",
                "printf forbidden >> /workspace/inputs/input/input.txt",
            ]
        )
        assert denied.returncode != 0
        ownership = _run(
            [
                "docker",
                "exec",
                first.realization_id,
                "stat",
                "-c",
                "%u:%g:%a",
                "/workspace/inputs/input/input.txt",
            ]
        )
        assert ownership.returncode == 0
        assert ownership.stdout.strip() == "65533:65533:444"
        rechmod = _run(
            [
                "docker",
                "exec",
                first.realization_id,
                "chmod",
                "u+w",
                "/workspace/inputs/input/input.txt",
            ]
        )
        assert rechmod.returncode != 0

        writable = _run(
            [
                "docker",
                "exec",
                first.realization_id,
                "sh",
                "-c",
                "printf changed > /workspace/work/edit/draft.txt && "
                "mkdir /workspace/out && "
                "printf result > /workspace/out/result.txt && "
                "printf hidden > /workspace/undeclared.txt",
            ]
        )
        assert writable.returncode == 0

        scanned: list[str] = []

        def scanner(payload: bytes, grant: ArtifactExportGrant) -> bool:
            scanned.append(grant.name)
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
                names = [member.name for member in archive.getmembers()]
                assert names == ["out", "out/result.txt"]
                result = archive.extractfile("out/result.txt")
                assert result is not None
                assert result.read() == b"result"
            return True

        exported = workspace.export_declared(
            first.realization_id,
            (
                ArtifactExportGrant(
                    name="result.tar",
                    path="/workspace/out",
                    max_bytes=1024,
                    scan_required=True,
                ),
            ),
            scanner=scanner,
        )
        assert set(exported) == {"result.tar"}
        assert scanned == ["result.tar"]
        with tarfile.open(
            fileobj=io.BytesIO(exported["result.tar"]),
            mode="r:",
        ) as archive:
            exported_names = {member.name for member in archive.getmembers()}
            assert "undeclared.txt" not in exported_names

        first_backend.cleanup_plan(first_plan, handle=first)
        first = None

        second_plan = _plan(f"phase3b{uuid.uuid4().hex[:12]}")
        second_backend = _backend()
        second = second_backend.ensure(second_plan)
        residue = _run(
            [
                "docker",
                "exec",
                second.realization_id,
                "sh",
                "-c",
                "test ! -e /workspace/inputs/input && "
                "test ! -e /workspace/work/edit && "
                "test ! -e /workspace/out && "
                "test ! -e /workspace/undeclared.txt",
            ]
        )
        assert residue.returncode == 0
    finally:
        if first is not None:
            try:
                first_backend.cleanup_plan(first_plan, handle=first)
            except Exception:
                pass
        if second is not None and second_backend is not None:
            try:
                second_backend.cleanup_plan(second_plan, handle=second)
            except Exception:
                pass
