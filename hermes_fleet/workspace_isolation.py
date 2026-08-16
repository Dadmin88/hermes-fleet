"""Bounded project projection and declared artifact export for disposable workshops.

Phase 3 deliberately does not mint RunAuthority. It defines the filesystem
contracts that a future RunAuthority may carry and realizes only already-
authorized project projections into the per-run tmpfs workspace.
"""

from __future__ import annotations

import io
import os
import re
import stat
import subprocess
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_RUN_UID = 65532
_RUN_GID = 65532
_INPUT_UID = 65533
_INPUT_GID = 65533
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_GRANTS = 8
_MAX_INPUT_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_INPUT_BYTES = 192 * 1024 * 1024
_MAX_EXPORTS = 16
_MAX_EXPORT_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_EXPORT_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 4096
_MAX_DOCKER_DIAGNOSTIC = 64 * 1024

# These broad locations may never themselves be configured as project roots.
# An exact project directory nested below /home is allowed; mounting /home or a
# user's entire home is not.
_FORBIDDEN_PROJECT_ROOTS = frozenset(
    {
        Path("/"),
        Path("/home"),
        Path("/root"),
        Path.home().resolve(strict=False),
        Path("/etc"),
        Path("/proc"),
        Path("/sys"),
        Path("/dev"),
        Path("/run"),
        Path("/var/lib/docker"),
    }
)

# Known host authority/state surfaces. Project roots may not contain these and
# resolved sources may not equal or descend into them.
_HOME = Path.home().resolve(strict=False)
_FORBIDDEN_HOST_PATHS = frozenset(
    {
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path("/var/lib/hermes-fleet"),
        Path("/var/lib/keryx"),
        Path("/var/lib/nodescale"),
        Path("/var/lib/hermes-vault"),
        Path("/var/lib/vault"),
        _HOME / ".hermes",
        _HOME / ".keryx",
        _HOME / ".nodescale",
        _HOME / ".vault",
        _HOME / ".local" / "share" / "hermes-fleet",
        _HOME / ".local" / "share" / "keryx",
        _HOME / ".local" / "share" / "nodescale",
        _HOME / ".local" / "share" / "hermes-vault",
        _HOME / ".local" / "share" / "vault",
    }
)

_FORBIDDEN_COMPONENTS = frozenset(
    {
        ".ssh",
        ".docker",
        ".gnupg",
        ".aws",
        ".kube",
        ".hermes",
        ".keryx",
        ".nodescale",
        ".vault",
    }
)


class WorkspaceIsolationError(RuntimeError):
    """Filesystem authority or projection/export cannot be proven safe."""


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise WorkspaceIsolationError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str, *, maximum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 < value <= maximum:
        raise WorkspaceIsolationError(f"{label} is invalid")
    return value


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise WorkspaceIsolationError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class FilesystemAuthorityScope:
    """Verified filesystem slice projected from a future immutable RunAuthority."""

    run_authority_hash: str
    write_authority_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _hash(self.run_authority_hash, "run authority hash")
        if (
            type(self.write_authority_hashes) is not tuple
            or len(self.write_authority_hashes) > _MAX_GRANTS
            or len(set(self.write_authority_hashes)) != len(self.write_authority_hashes)
        ):
            raise WorkspaceIsolationError("filesystem write authority set is invalid")
        for value in self.write_authority_hashes:
            _hash(value, "filesystem write authority hash")
            if value == self.run_authority_hash:
                raise WorkspaceIsolationError(
                    "filesystem write authority must be separate from RunAuthority"
                )

    def permits(self, grant: FilesystemGrant) -> bool:
        if type(grant) is not FilesystemGrant:
            return False
        if grant.authority_ref != self.run_authority_hash:
            return False
        if grant.mode == "read":
            return grant.write_authority_ref is None
        return grant.write_authority_ref in self.write_authority_hashes


@dataclass(frozen=True, slots=True)
class FilesystemGrant:
    """Already-authorized project projection, never an arbitrary host path."""

    project_id: str
    relative_path: str
    target: str
    mode: str = "read"
    max_bytes: int = _MAX_INPUT_BYTES
    authority_ref: str = ""
    write_authority_ref: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.project_id, "filesystem project id")
        _hash(self.authority_ref, "filesystem authority digest")
        relative = PurePosixPath(self.relative_path)
        if (
            type(self.relative_path) is not str
            or relative.is_absolute()
            or ".." in relative.parts
            or str(relative) in {"", "."}
        ):
            raise WorkspaceIsolationError("filesystem relative path is invalid")
        object.__setattr__(self, "relative_path", relative.as_posix())

        target = PurePosixPath(self.target)
        if (
            type(self.target) is not str
            or not target.is_absolute()
            or ".." in target.parts
            or len(target.parts) < 3
            or target.parts[1] != "workspace"
            or target.as_posix() == "/workspace"
        ):
            raise WorkspaceIsolationError(
                "filesystem target must be strictly beneath /workspace"
            )
        object.__setattr__(self, "target", target.as_posix())
        _positive_int(
            self.max_bytes,
            "filesystem grant byte limit",
            maximum=_MAX_INPUT_BYTES,
        )

        if self.mode == "read":
            if self.write_authority_ref is not None:
                raise WorkspaceIsolationError(
                    "read-only filesystem grant may not carry write authority"
                )
            if len(target.parts) < 4 or target.parts[2] != "inputs":
                raise WorkspaceIsolationError(
                    "read-only filesystem target must be beneath /workspace/inputs"
                )
        elif self.mode == "write":
            _hash(
                self.write_authority_ref,
                "filesystem write authority digest",
            )
            if self.write_authority_ref == self.authority_ref:
                raise WorkspaceIsolationError(
                    "writable filesystem projection requires separate authority"
                )
            if len(target.parts) < 4 or target.parts[2] != "work":
                raise WorkspaceIsolationError(
                    "writable filesystem target must be beneath /workspace/work"
                )
        else:
            raise WorkspaceIsolationError("filesystem grant mode is unsupported")


@dataclass(frozen=True, slots=True)
class ArtifactExportGrant:
    """One explicitly declared output below the disposable /workspace tree."""

    name: str
    path: str
    max_bytes: int
    scan_required: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or not self.name
            or len(self.name) > 255
            or "/" in self.name
            or "\\" in self.name
            or self.name in {".", ".."}
        ):
            raise WorkspaceIsolationError("artifact export name is invalid")
        path = PurePosixPath(self.path)
        if (
            type(self.path) is not str
            or not path.is_absolute()
            or ".." in path.parts
            or len(path.parts) < 3
            or path.parts[1] != "workspace"
            or path.parts[2] != "out"
        ):
            raise WorkspaceIsolationError(
                "artifact export path must be /workspace/out or beneath it"
            )
        object.__setattr__(self, "path", path.as_posix())
        _positive_int(
            self.max_bytes,
            "artifact export byte limit",
            maximum=_MAX_EXPORT_BYTES,
        )
        if type(self.scan_required) is not bool:
            raise WorkspaceIsolationError("artifact scan requirement is invalid")


@dataclass(frozen=True, slots=True)
class ResolvedFilesystemGrant:
    grant: FilesystemGrant
    source: Path
    source_is_dir: bool
    observed_bytes: int

    def __post_init__(self) -> None:
        if type(self.grant) is not FilesystemGrant:
            raise WorkspaceIsolationError("resolved filesystem grant is invalid")
        if not isinstance(self.source, Path) or not self.source.is_absolute():
            raise WorkspaceIsolationError("resolved filesystem source is invalid")
        if type(self.source_is_dir) is not bool:
            raise WorkspaceIsolationError("resolved filesystem source type is invalid")
        if (
            type(self.observed_bytes) is not int
            or not 0 <= self.observed_bytes <= self.grant.max_bytes
        ):
            raise WorkspaceIsolationError("resolved filesystem source size is invalid")


class ProjectWorkspaceResolver:
    """Canonicalize trusted project IDs into bounded disposable projections."""

    def __init__(
        self,
        project_roots: Mapping[str, Path],
        *,
        forbidden_paths: tuple[Path, ...] = (),
    ) -> None:
        if not isinstance(project_roots, Mapping) or not 0 < len(project_roots) <= 128:
            raise WorkspaceIsolationError("project root configuration is invalid")

        forbidden = {path.resolve(strict=False) for path in _FORBIDDEN_HOST_PATHS}
        for path in forbidden_paths:
            if not isinstance(path, Path) or not path.is_absolute():
                raise WorkspaceIsolationError("forbidden host path is invalid")
            forbidden.add(path.resolve(strict=False))
        self._forbidden = tuple(
            sorted(forbidden, key=lambda item: len(item.parts), reverse=True)
        )

        roots: dict[str, Path] = {}
        for project_id, root in project_roots.items():
            _identifier(project_id, "project root id")
            if not isinstance(root, Path) or not root.is_absolute():
                raise WorkspaceIsolationError("project root path is invalid")
            try:
                if root.is_symlink():
                    raise WorkspaceIsolationError("project root may not be a symlink")
                canonical = root.resolve(strict=True)
            except OSError as error:
                raise WorkspaceIsolationError("project root is unavailable") from error
            if not canonical.is_dir() or canonical in _FORBIDDEN_PROJECT_ROOTS:
                raise WorkspaceIsolationError("project root is unsafe")
            if any(
                _intersects(canonical, forbidden_path)
                for forbidden_path in self._forbidden
            ):
                raise WorkspaceIsolationError(
                    "project root intersects forbidden host state"
                )
            roots[project_id] = canonical
        self._roots = roots

    def resolve(
        self,
        grants: tuple[FilesystemGrant, ...] | list[FilesystemGrant],
        *,
        authority: FilesystemAuthorityScope,
    ) -> tuple[ResolvedFilesystemGrant, ...]:
        if type(authority) is not FilesystemAuthorityScope:
            raise WorkspaceIsolationError("filesystem authority scope is required")
        if type(grants) not in {tuple, list} or len(grants) > _MAX_GRANTS:
            raise WorkspaceIsolationError("filesystem grant collection is invalid")
        targets: set[str] = set()
        resolved: list[ResolvedFilesystemGrant] = []
        total = 0
        for grant in grants:
            if type(grant) is not FilesystemGrant:
                raise WorkspaceIsolationError("filesystem grant is invalid")
            if grant.target in targets:
                raise WorkspaceIsolationError("filesystem grant targets must be unique")
            targets.add(grant.target)
            root = self._roots.get(grant.project_id)
            if root is None:
                raise WorkspaceIsolationError("filesystem project is not configured")

            candidate = root.joinpath(*PurePosixPath(grant.relative_path).parts)
            try:
                canonical = candidate.resolve(strict=True)
            except OSError as error:
                raise WorkspaceIsolationError(
                    "filesystem source is unavailable"
                ) from error
            if not _within(canonical, root):
                raise WorkspaceIsolationError("filesystem source escapes project root")
            if any(
                _within(canonical, forbidden_path) or canonical == forbidden_path
                for forbidden_path in self._forbidden
            ):
                raise WorkspaceIsolationError(
                    "filesystem source enters forbidden host state"
                )
            relative_parts = canonical.relative_to(root).parts
            if any(part in _FORBIDDEN_COMPONENTS for part in relative_parts):
                raise WorkspaceIsolationError(
                    "filesystem source enters sensitive state"
                )
            if not authority.permits(grant):
                raise WorkspaceIsolationError(
                    "filesystem grant is outside verified RunAuthority scope"
                )

            observed, is_dir = _measure_safe_tree(canonical, grant.max_bytes)
            total += observed
            if total > _MAX_TOTAL_INPUT_BYTES:
                raise WorkspaceIsolationError(
                    "filesystem inputs exceed aggregate staging bound"
                )
            resolved.append(ResolvedFilesystemGrant(grant, canonical, is_dir, observed))
        return tuple(resolved)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _intersects(project_root: Path, forbidden: Path) -> bool:
    """Reject project roots above, equal to, or inside forbidden host state."""
    return (
        project_root == forbidden
        or _within(forbidden, project_root)
        or _within(project_root, forbidden)
    )


def _measure_safe_tree(path: Path, maximum: int) -> tuple[int, bool]:
    try:
        root_info = path.lstat()
    except OSError as error:
        raise WorkspaceIsolationError(
            "filesystem source cannot be inspected"
        ) from error
    if stat.S_ISLNK(root_info.st_mode):
        raise WorkspaceIsolationError("filesystem source may not be a symlink")
    if stat.S_ISREG(root_info.st_mode):
        if root_info.st_size > maximum:
            raise WorkspaceIsolationError(
                "filesystem source exceeds authority byte limit"
            )
        return root_info.st_size, False
    if not stat.S_ISDIR(root_info.st_mode):
        raise WorkspaceIsolationError(
            "filesystem source must be a regular file or directory"
        )

    total = 0
    members = 0
    for current, directories, files in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        for name in list(directories):
            try:
                info = (current_path / name).lstat()
            except OSError as error:
                raise WorkspaceIsolationError(
                    "filesystem source changed during inspection"
                ) from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceIsolationError(
                    "filesystem tree contains a symlink or special entry"
                )
            members += 1
            if members > _MAX_ARCHIVE_MEMBERS:
                raise WorkspaceIsolationError(
                    "filesystem source contains too many entries"
                )
        for name in files:
            try:
                info = (current_path / name).lstat()
            except OSError as error:
                raise WorkspaceIsolationError(
                    "filesystem source changed during inspection"
                ) from error
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise WorkspaceIsolationError(
                    "filesystem tree contains a symlink or special entry"
                )
            total += info.st_size
            members += 1
            if total > maximum:
                raise WorkspaceIsolationError(
                    "filesystem source exceeds authority byte limit"
                )
            if members > _MAX_ARCHIVE_MEMBERS:
                raise WorkspaceIsolationError(
                    "filesystem source contains too many entries"
                )
    return total, True


def build_projection_archive(resolved: ResolvedFilesystemGrant) -> bytes:
    """Build a deterministic no-link tar archive owned by the workshop UID/GID."""
    if type(resolved) is not ResolvedFilesystemGrant:
        raise WorkspaceIsolationError("resolved filesystem grant is invalid")
    read_only = resolved.grant.mode == "read"
    uid = _INPUT_UID if read_only else _RUN_UID
    gid = _INPUT_GID if read_only else _RUN_GID
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        if resolved.source_is_dir:
            _archive_directory(
                archive,
                resolved.source,
                resolved.grant.max_bytes,
                uid=uid,
                gid=gid,
                read_only=read_only,
            )
        else:
            _archive_regular_file(
                archive,
                resolved.source,
                PurePosixPath(resolved.grant.target).name,
                resolved.grant.max_bytes,
                uid=uid,
                gid=gid,
                read_only=read_only,
            )
    payload = stream.getvalue()
    if len(payload) > resolved.grant.max_bytes + 4 * 1024 * 1024:
        raise WorkspaceIsolationError("filesystem projection archive exceeds its bound")
    return payload


def _archive_directory(
    archive: tarfile.TarFile,
    root: Path,
    maximum: int,
    *,
    uid: int,
    gid: int,
    read_only: bool,
) -> None:
    total = 0
    members = 0
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            current_info = current_path.lstat()
        except OSError as error:
            raise WorkspaceIsolationError(
                "filesystem source changed during staging"
            ) from error
        if stat.S_ISLNK(current_info.st_mode) or not stat.S_ISDIR(current_info.st_mode):
            raise WorkspaceIsolationError(
                "filesystem tree changed to a symlink or special entry"
            )
        directories.sort()
        files.sort()
        for name in directories:
            try:
                child_info = (current_path / name).lstat()
            except OSError as error:
                raise WorkspaceIsolationError(
                    "filesystem source changed during staging"
                ) from error
            if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
                raise WorkspaceIsolationError(
                    "filesystem tree changed to a symlink or special entry"
                )
        relative = current_path.relative_to(root)
        if relative.parts:
            info = tarfile.TarInfo(relative.as_posix())
            info.type = tarfile.DIRTYPE
            info.mode = 0o555 if read_only else 0o700
            _normalize_tar_info(info, uid=uid, gid=gid)
            archive.addfile(info)
            members += 1
        for name in files:
            source = current_path / name
            arcname = (relative / name).as_posix()
            total += _archive_regular_file(
                archive,
                source,
                arcname,
                maximum - total,
                uid=uid,
                gid=gid,
                read_only=read_only,
            )
            members += 1
            if members > _MAX_ARCHIVE_MEMBERS:
                raise WorkspaceIsolationError(
                    "filesystem projection contains too many entries"
                )


def _archive_regular_file(
    archive: tarfile.TarFile,
    source: Path,
    arcname: str,
    maximum: int,
    *,
    uid: int,
    gid: int,
    read_only: bool,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise WorkspaceIsolationError(
            "filesystem source changed during staging"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise WorkspaceIsolationError("filesystem staged file exceeds authority")
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
            payload = handle.read(info.st_size + 1)
        if len(payload) != info.st_size:
            raise WorkspaceIsolationError(
                "filesystem source changed during staging"
            )
        tar_info = tarfile.TarInfo(arcname)
        tar_info.size = len(payload)
        if read_only:
            tar_info.mode = 0o555 if info.st_mode & 0o111 else 0o444
        else:
            tar_info.mode = 0o700 if info.st_mode & 0o111 else 0o600
        _normalize_tar_info(tar_info, uid=uid, gid=gid)
        archive.addfile(tar_info, io.BytesIO(payload))
        return len(payload)
    finally:
        os.close(descriptor)


def _normalize_tar_info(info: tarfile.TarInfo, *, uid: int, gid: int) -> None:
    info.uid = uid
    info.gid = gid
    info.uname = ""
    info.gname = ""
    info.mtime = 0


class DockerWorkspaceIO:
    """Bounded copy-in/copy-out against an existing Fleet-owned workshop."""

    def __init__(
        self,
        *,
        command: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    ) -> None:
        self._command = command or _run_binary

    def stage(self, container_id: str, resolved: ResolvedFilesystemGrant) -> None:
        _container_id(container_id)
        if type(resolved) is not ResolvedFilesystemGrant:
            raise WorkspaceIsolationError("resolved filesystem grant is invalid")
        payload = build_projection_archive(resolved)
        target = PurePosixPath(resolved.grant.target)
        extraction_root = target if resolved.source_is_dir else target.parent
        if resolved.grant.mode == "read":
            projection_user = f"{_INPUT_UID}:{_INPUT_GID}"
        else:
            projection_user = f"{_RUN_UID}:{_RUN_GID}"
        exec_prefix = ["docker", "exec", "--user", projection_user]
        self._checked(
            [*exec_prefix, container_id, "mkdir", "-p", extraction_root.as_posix()],
            timeout=15,
        )
        self._checked(
            [
                *exec_prefix,
                "-i",
                container_id,
                "tar",
                "-xf",
                "-",
                "-C",
                extraction_root.as_posix(),
            ],
            input_bytes=payload,
            timeout=30,
        )
        if resolved.grant.mode == "read":
            self._checked(
                [
                    *exec_prefix,
                    container_id,
                    "chmod",
                    "-R",
                    "a-w",
                    target.as_posix(),
                ],
                timeout=15,
            )

    def export_declared(
        self,
        container_id: str,
        grants: tuple[ArtifactExportGrant, ...] | list[ArtifactExportGrant],
        *,
        scanner: Callable[[bytes, ArtifactExportGrant], bool] | None = None,
    ) -> dict[str, bytes]:
        _container_id(container_id)
        if type(grants) not in {tuple, list} or len(grants) > _MAX_EXPORTS:
            raise WorkspaceIsolationError(
                "artifact export grant collection is invalid"
            )
        names: set[str] = set()
        result: dict[str, bytes] = {}
        total = 0
        for grant in grants:
            if type(grant) is not ArtifactExportGrant:
                raise WorkspaceIsolationError(
                    "artifact export grant is invalid"
                )
            if grant.name in names:
                raise WorkspaceIsolationError(
                    "artifact export names must be unique"
                )
            names.add(grant.name)
            payload = self._export_one(container_id, grant)
            total += _payload_file_bytes(payload)
            if total > _MAX_TOTAL_EXPORT_BYTES:
                raise WorkspaceIsolationError(
                    "declared artifact exports exceed aggregate byte limit"
                )
            if grant.scan_required:
                if scanner is None:
                    raise WorkspaceIsolationError(
                        "artifact export requires an output scanner"
                    )
                try:
                    accepted = scanner(payload, grant)
                except Exception as error:
                    raise WorkspaceIsolationError(
                        "artifact output scan failed"
                    ) from error
                if accepted is not True:
                    raise WorkspaceIsolationError(
                        "artifact output scan rejected export"
                    )
            elif scanner is not None:
                try:
                    accepted = scanner(payload, grant)
                except Exception as error:
                    raise WorkspaceIsolationError(
                        "artifact output scan failed"
                    ) from error
                if accepted is False:
                    raise WorkspaceIsolationError(
                        "artifact output scan rejected export"
                    )
            result[grant.name] = payload
        return result

    def _export_one(self, container_id: str, grant: ArtifactExportGrant) -> bytes:
        path = PurePosixPath(grant.path)
        completed = self._command(
            [
                "docker",
                "exec",
                container_id,
                "tar",
                "-cf",
                "-",
                "-C",
                path.parent.as_posix(),
                path.name,
            ],
            input=None,
            timeout=30,
            max_stdout=grant.max_bytes + 4 * 1024 * 1024,
        )
        if completed.returncode != 0:
            raise WorkspaceIsolationError("declared artifact export failed")
        payload = bytes(completed.stdout)
        validate_export_archive(payload, grant)
        return payload

    def _checked(
        self,
        argv: list[str],
        *,
        input_bytes: bytes | None = None,
        timeout: int,
    ) -> None:
        completed = self._command(
            argv,
            input=input_bytes,
            timeout=timeout,
            max_stdout=_MAX_DOCKER_DIAGNOSTIC,
        )
        if completed.returncode != 0:
            raise WorkspaceIsolationError("Docker workspace staging failed")


def validate_export_archive(payload: bytes, grant: ArtifactExportGrant) -> None:
    if type(payload) is not bytes or not payload:
        raise WorkspaceIsolationError("artifact export is empty or invalid")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError as error:
        raise WorkspaceIsolationError(
            "artifact export is not a valid tar archive"
        ) from error
    total = 0
    members = 0
    with archive:
        for member in archive.getmembers():
            members += 1
            if members > _MAX_ARCHIVE_MEMBERS:
                raise WorkspaceIsolationError(
                    "artifact export contains too many entries"
                )
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise WorkspaceIsolationError("artifact export contains an unsafe path")
            if not (member.isfile() or member.isdir()):
                raise WorkspaceIsolationError(
                    "artifact export contains a link or special entry"
                )
            if member.isfile():
                total += member.size
                if total > grant.max_bytes:
                    raise WorkspaceIsolationError(
                        "artifact export exceeds its authority byte limit"
                    )


def _payload_file_bytes(payload: bytes) -> int:
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")
    except tarfile.TarError as error:
        raise WorkspaceIsolationError("artifact export is invalid") from error
    with archive:
        return sum(member.size for member in archive.getmembers() if member.isfile())


def _container_id(value: object) -> str:
    if type(value) is not str or _CONTAINER_ID_RE.fullmatch(value) is None:
        raise WorkspaceIsolationError("container identity must be an exact Docker ID")
    return value


def _run_binary(
    argv: list[str],
    *,
    input: bytes | None,
    timeout: int,
    max_stdout: int,
) -> subprocess.CompletedProcess[bytes]:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        if process is not None:
            try:
                process.kill()
                process.communicate()
            except OSError:
                pass
        raise WorkspaceIsolationError(
            "Docker workspace command was unavailable"
        ) from error
    if len(stdout) > max_stdout or len(stderr) > _MAX_DOCKER_DIAGNOSTIC:
        raise WorkspaceIsolationError(
            "Docker workspace command output exceeded its bound"
        )
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
