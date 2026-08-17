from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from hermes_fleet.workspace_isolation import (
    ArtifactExportGrant,
    DockerWorkspaceIO,
    FilesystemAuthorityScope,
    FilesystemGrant,
    ProjectWorkspaceResolver,
    WorkspaceIsolationError,
    build_projection_archive,
    validate_export_archive,
)

AUTHORITY = "sha256:" + "8" * 64
WRITE_AUTHORITY = "sha256:" + "9" * 64
CONTAINER_ID = "a" * 64


def scope() -> FilesystemAuthorityScope:
    return FilesystemAuthorityScope(
        run_authority_hash=AUTHORITY,
        write_authority_hashes=(WRITE_AUTHORITY,),
    )


def grant(
    *,
    relative_path: str = "src",
    target: str | None = None,
    mode: str = "read",
    max_bytes: int = 1_000_000,
    write_authority_ref: str | None = None,
) -> FilesystemGrant:
    resolved_target = target or (
        "/workspace/work/src" if mode == "write" else "/workspace/inputs/src"
    )
    return FilesystemGrant(
        project_id="project-1",
        relative_path=relative_path,
        target=resolved_target,
        mode=mode,
        max_bytes=max_bytes,
        authority_ref=AUTHORITY,
        write_authority_ref=write_authority_ref,
    )


def test_filesystem_grants_default_read_and_require_separate_write_authority() -> None:
    value = grant()
    assert value.mode == "read"
    assert value.write_authority_ref is None

    with pytest.raises(WorkspaceIsolationError, match="write authority"):
        grant(mode="write")

    writable = grant(mode="write", write_authority_ref=WRITE_AUTHORITY)
    assert writable.mode == "write"
    assert writable.write_authority_ref == WRITE_AUTHORITY

    with pytest.raises(WorkspaceIsolationError, match="may not carry write"):
        grant(write_authority_ref=WRITE_AUTHORITY)
    with pytest.raises(WorkspaceIsolationError, match="separate authority"):
        grant(mode="write", write_authority_ref=AUTHORITY)


def test_filesystem_authority_scope_binds_read_and_separate_write_authority(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    src = project / "src"
    src.mkdir()
    (src / "file.txt").write_text("ok", encoding="utf-8")
    resolver = ProjectWorkspaceResolver({"project-1": project})

    assert resolver.resolve((grant(),), authority=scope())
    writable = grant(mode="write", write_authority_ref=WRITE_AUTHORITY)
    assert resolver.resolve((writable,), authority=scope())

    wrong_scope = FilesystemAuthorityScope(
        run_authority_hash="sha256:" + "a" * 64,
        write_authority_hashes=(WRITE_AUTHORITY,),
    )
    with pytest.raises(WorkspaceIsolationError, match="outside verified RunAuthority"):
        resolver.resolve((grant(),), authority=wrong_scope)

    no_write = FilesystemAuthorityScope(run_authority_hash=AUTHORITY)
    with pytest.raises(WorkspaceIsolationError, match="outside verified RunAuthority"):
        resolver.resolve((writable,), authority=no_write)

    with pytest.raises(WorkspaceIsolationError, match="must be separate"):
        FilesystemAuthorityScope(
            run_authority_hash=AUTHORITY,
            write_authority_hashes=(AUTHORITY,),
        )


def test_grants_reject_traversal_absolute_sources_and_workspace_root_target() -> None:
    for relative in ("../secret", "/etc/passwd", "."):
        with pytest.raises(WorkspaceIsolationError):
            grant(relative_path=relative)
    for target in ("/workspace", "/tmp/project", "/workspace/../etc"):
        with pytest.raises(WorkspaceIsolationError):
            grant(target=target)


def test_project_resolver_canonicalizes_source_and_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    src = project / "src"
    src.mkdir()
    (src / "ok.txt").write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private", encoding="utf-8")
    (project / "escape").symlink_to(outside, target_is_directory=True)

    resolver = ProjectWorkspaceResolver({"project-1": project})
    resolved = resolver.resolve((grant(),), authority=scope())
    assert resolved[0].source == src.resolve()
    assert resolved[0].source_is_dir is True
    assert resolved[0].observed_bytes == 2

    with pytest.raises(WorkspaceIsolationError, match="escapes project root"):
        resolver.resolve(
            (grant(relative_path="escape", target="/workspace/inputs/escape"),),
            authority=scope(),
        )

    unauthorized_escape = FilesystemGrant(
        project_id="project-1",
        relative_path="escape",
        target="/workspace/inputs/escape-two",
        authority_ref="sha256:" + "a" * 64,
    )
    with pytest.raises(WorkspaceIsolationError, match="escapes project root"):
        resolver.resolve((unauthorized_escape,), authority=scope())


def test_project_resolver_allows_exact_project_below_home_but_not_broad_home() -> None:
    # The master plan forbids exposing /home or a whole user home. An exact
    # configured project nested below it is still a valid narrow project root.
    with pytest.raises(WorkspaceIsolationError, match="unsafe"):
        ProjectWorkspaceResolver({"project-1": Path("/home")})


def test_project_resolver_rejects_sensitive_state_and_symlinks_inside_tree(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sensitive = project / ".ssh"
    sensitive.mkdir()
    (sensitive / "key").write_text("nope", encoding="utf-8")
    resolver = ProjectWorkspaceResolver({"project-1": project})
    with pytest.raises(WorkspaceIsolationError, match="sensitive state"):
        resolver.resolve(
            (grant(relative_path=".ssh", target="/workspace/inputs/ssh"),),
            authority=scope(),
        )

    src = project / "src"
    src.mkdir()
    (src / "regular.txt").write_text("safe", encoding="utf-8")
    (src / "link.txt").symlink_to(project / ".ssh" / "key")
    with pytest.raises(WorkspaceIsolationError, match="symlink or special"):
        resolver.resolve((grant(),), authority=scope())


def test_project_resolver_rejects_forbidden_state_intersection(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = project / "fleet-state"
    state.mkdir()
    with pytest.raises(WorkspaceIsolationError, match="intersects forbidden"):
        ProjectWorkspaceResolver(
            {"project-1": project},
            forbidden_paths=(state,),
        )


def test_project_resolver_bounds_grant_count_targets_and_bytes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for index in range(9):
        item = project / f"p{index}"
        item.mkdir()
        (item / "x").write_text("x", encoding="utf-8")
    resolver = ProjectWorkspaceResolver({"project-1": project})

    with pytest.raises(WorkspaceIsolationError, match="collection"):
        resolver.resolve(
            tuple(
                grant(
                    relative_path=f"p{index}",
                    target=f"/workspace/inputs/p{index}",
                )
                for index in range(9)
            ),
            authority=scope(),
        )

    with pytest.raises(WorkspaceIsolationError, match="targets must be unique"):
        resolver.resolve(
            (
                grant(relative_path="p0", target="/workspace/inputs/same"),
                grant(relative_path="p1", target="/workspace/inputs/same"),
            ),
            authority=scope(),
        )

    big = project / "big.bin"
    big.write_bytes(b"x" * 10)
    with pytest.raises(WorkspaceIsolationError, match="byte limit"):
        resolver.resolve(
            (
                grant(
                    relative_path="big.bin",
                    target="/workspace/inputs/big.bin",
                    max_bytes=5,
                ),
            ),
            authority=scope(),
        )


def test_projection_archive_is_deterministic_owned_and_renames_single_file(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "source.txt").write_text("payload", encoding="utf-8")
    resolved = ProjectWorkspaceResolver({"project-1": project}).resolve(
        (
            grant(
                relative_path="source.txt",
                target="/workspace/inputs/renamed.txt",
            ),
        ),
        authority=scope(),
    )[0]
    payload = build_projection_archive(resolved)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == ["renamed.txt"]
        member = members[0]
        assert member.uid == 65533
        assert member.gid == 65533
        assert member.mtime == 0
        extracted = archive.extractfile(member)
        assert extracted is not None
        assert extracted.read() == b"payload"


def _tar_with_member(member: tarfile.TarInfo, payload: bytes = b"") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        archive.addfile(member, io.BytesIO(payload) if member.isfile() else None)
    return stream.getvalue()


def test_artifact_validation_rejects_links_traversal_special_and_overflow() -> None:
    export = ArtifactExportGrant(
        name="out.tar",
        path="/workspace/out",
        max_bytes=4,
    )

    symlink = tarfile.TarInfo("out/link")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "/etc/passwd"
    with pytest.raises(WorkspaceIsolationError, match="link or special"):
        validate_export_archive(_tar_with_member(symlink), export)

    traversal = tarfile.TarInfo("../escape")
    traversal.size = 1
    with pytest.raises(WorkspaceIsolationError, match="unsafe path"):
        validate_export_archive(_tar_with_member(traversal, b"x"), export)

    too_large = tarfile.TarInfo("out/big")
    too_large.size = 5
    with pytest.raises(WorkspaceIsolationError, match="byte limit"):
        validate_export_archive(_tar_with_member(too_large, b"12345"), export)


def test_artifact_export_only_declared_outputs_and_scanner_policy() -> None:
    member = tarfile.TarInfo("out/result.txt")
    member.size = 2
    payload = _tar_with_member(member, b"ok")
    calls: list[list[str]] = []

    def command(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, payload, b"")

    io_layer = DockerWorkspaceIO(command=command)
    required = ArtifactExportGrant(
        name="result.tar",
        path="/workspace/out",
        max_bytes=10,
        scan_required=True,
    )
    with pytest.raises(WorkspaceIsolationError, match="requires an output scanner"):
        io_layer.export_declared(CONTAINER_ID, (required,))

    scanned: list[str] = []

    def scanner(data: bytes, grant_value: ArtifactExportGrant) -> bool:
        assert data == payload
        scanned.append(grant_value.name)
        return True

    exported = io_layer.export_declared(
        CONTAINER_ID,
        (required,),
        scanner=scanner,
    )
    assert exported == {"result.tar": payload}
    assert scanned == ["result.tar"]
    assert all("/workspace/out" not in " ".join(call[:-1]) for call in calls)


def test_stage_uses_copy_projection_and_read_grant_becomes_immutable(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    src = project / "src"
    src.mkdir()
    (src / "file.txt").write_text("hello", encoding="utf-8")
    resolved = ProjectWorkspaceResolver({"project-1": project}).resolve(
        (grant(),),
        authority=scope(),
    )[0]
    calls: list[tuple[list[str], bytes | None]] = []

    def command(argv, *, input, **_kwargs):
        calls.append((argv, input))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    DockerWorkspaceIO(command=command).stage(CONTAINER_ID, resolved)
    argv_calls = [argv for argv, _ in calls]
    assert any(
        argv[-3:] == ["mkdir", "-p", "/workspace/inputs/src"] for argv in argv_calls
    )
    assert all(argv[2:4] == ["--user", "65533:65533"] for argv in argv_calls)
    assert any("tar" in argv and "-xf" in argv for argv in argv_calls)
    assert any(
        argv[-4:] == ["chmod", "-R", "a-w", "/workspace/inputs/src"]
        for argv in argv_calls
    )
    assert not any("mount" in argv for argv in argv_calls)


def test_stage_write_projection_requires_authority_and_stays_writable(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    src = project / "src"
    src.mkdir()
    (src / "file.txt").write_text("hello", encoding="utf-8")
    writable = grant(
        mode="write",
        write_authority_ref=WRITE_AUTHORITY,
    )
    resolved = ProjectWorkspaceResolver({"project-1": project}).resolve(
        (writable,),
        authority=scope(),
    )[0]
    calls: list[list[str]] = []

    def command(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    DockerWorkspaceIO(command=command).stage(CONTAINER_ID, resolved)
    assert all(argv[2:4] == ["--user", "65532:65532"] for argv in calls)
    assert any(argv[-3:] == ["mkdir", "-p", "/workspace/work/src"] for argv in calls)
    assert not any("chmod" in argv for argv in calls)
    assert not any("mount" in argv for argv in calls)
