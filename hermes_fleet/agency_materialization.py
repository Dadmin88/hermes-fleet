"""Immutable, transportable Agency profile packages for exact Recipe execution."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .agency_snapshot import AgencyProfilePackage
from .profile_inventory import _profile_content_digest
from .recipes import ResolvedAgencyProfile

_MAX_PACKAGE_BYTES = 16 * 1024 * 1024
_MAX_MEMBER_BYTES = 4 * 1024 * 1024
_MAX_MEMBERS = 2048


class AgencyMaterializationError(ValueError):
    """Exact Agency package bytes cannot be trusted or materialized."""


@dataclass(frozen=True, slots=True)
class ImmutableAgencyBundle:
    resolved: ResolvedAgencyProfile
    archive_sha256: str
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.resolved) is not ResolvedAgencyProfile:
            raise AgencyMaterializationError("resolved Agency identity is invalid")
        if type(self.payload) is not bytes or not self.payload:
            raise AgencyMaterializationError("Agency package payload is invalid")
        if len(self.payload) > _MAX_PACKAGE_BYTES:
            raise AgencyMaterializationError(
                "Agency package exceeds the supported bound"
            )
        digest = "sha256:" + hashlib.sha256(self.payload).hexdigest()
        if digest != self.archive_sha256:
            raise AgencyMaterializationError(
                "Agency package archive digest does not match"
            )


def bundle_agency_profile(package: AgencyProfilePackage) -> ImmutableAgencyBundle:
    """Package one verified snapshot profile into deterministic exact bytes."""
    if type(package) is not AgencyProfilePackage:
        raise AgencyMaterializationError("Agency profile package is invalid")
    root = package.local_path.resolve(strict=True)
    if not root.is_dir():
        raise AgencyMaterializationError("Agency profile package directory is invalid")
    members = _regular_members(root)
    manifest = {
        "schema": "fleet.agency-package.v1",
        "repository": package.source.repository,
        "revision": package.source.revision,
        "name": package.name,
        "version": package.version,
        "content_digest": "sha256:" + package.content_digest,
    }
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        _add_bytes(
            archive,
            "fleet-package.json",
            (
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
        )
        for relative, source in members:
            _add_bytes(
                archive,
                f"profile/{relative.as_posix()}",
                source.read_bytes(),
                mode=source.stat().st_mode & 0o777,
            )
    payload = stream.getvalue()
    resolved = ResolvedAgencyProfile(
        repository=package.source.repository,
        revision=package.source.revision,
        name=package.name,
        version=package.version,
        content_digest="sha256:" + package.content_digest,
    )
    return ImmutableAgencyBundle(
        resolved=resolved,
        archive_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        payload=payload,
    )


def materialize_agency_bundle(
    bundle: ImmutableAgencyBundle, *, destination: Path
) -> Path:
    """Extract and reverify one exact Agency package into a new directory."""
    if type(bundle) is not ImmutableAgencyBundle:
        raise AgencyMaterializationError("Agency bundle is invalid")
    if destination.exists():
        raise AgencyMaterializationError("Agency materialization destination exists")
    destination.mkdir(parents=True, mode=0o700)
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle.payload), mode="r:") as archive:
            members = archive.getmembers()
            if not 1 < len(members) <= _MAX_MEMBERS + 1:
                raise AgencyMaterializationError(
                    "Agency package member count is invalid"
                )
            manifest = _read_manifest(archive, members[0])
            if manifest != {
                "schema": "fleet.agency-package.v1",
                "repository": bundle.resolved.repository,
                "revision": bundle.resolved.revision,
                "name": bundle.resolved.name,
                "version": bundle.resolved.version,
                "content_digest": bundle.resolved.content_digest,
            }:
                raise AgencyMaterializationError(
                    "Agency package identity does not match"
                )
            for member in members[1:]:
                relative = _member_path(member)
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise AgencyMaterializationError(
                        "Agency package member is unreadable"
                    )
                data = extracted.read(_MAX_MEMBER_BYTES + 1)
                if len(data) > _MAX_MEMBER_BYTES:
                    raise AgencyMaterializationError(
                        "Agency package member exceeds bound"
                    )
                target.write_bytes(data)
                target.chmod(member.mode & 0o777)
        (destination / "skills").mkdir(exist_ok=True)
        digest = _profile_content_digest(
            destination, bundle.resolved.name, bundle.resolved.version
        )
        if "sha256:" + str(digest) != bundle.resolved.content_digest:
            raise AgencyMaterializationError(
                "materialized Agency content does not match"
            )
        return destination
    except BaseException:
        import shutil

        shutil.rmtree(destination, ignore_errors=True)
        raise


def _regular_members(root: Path) -> tuple[tuple[PurePosixPath, Path], ...]:
    result: list[tuple[PurePosixPath, Path]] = []
    for source in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = PurePosixPath(source.relative_to(root).as_posix())
        if source.is_symlink():
            raise AgencyMaterializationError("Agency package contains a symlink")
        if source.is_dir():
            continue
        if not source.is_file() or source.stat().st_size > _MAX_MEMBER_BYTES:
            raise AgencyMaterializationError("Agency package member is unsupported")
        result.append((relative, source))
    if not result or len(result) > _MAX_MEMBERS:
        raise AgencyMaterializationError("Agency package member count is invalid")
    return tuple(result)


def _add_bytes(
    archive: tarfile.TarFile, name: str, data: bytes, *, mode: int = 0o644
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def _read_manifest(archive: tarfile.TarFile, member: tarfile.TarInfo) -> object:
    if member.name != "fleet-package.json" or not member.isfile():
        raise AgencyMaterializationError("Agency package manifest is invalid")
    extracted = archive.extractfile(member)
    if extracted is None:
        raise AgencyMaterializationError("Agency package manifest is unreadable")
    try:
        return json.loads(extracted.read(8193))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AgencyMaterializationError(
            "Agency package manifest is invalid"
        ) from error


def _member_path(member: tarfile.TarInfo) -> PurePosixPath:
    if not member.isfile() or member.size > _MAX_MEMBER_BYTES:
        raise AgencyMaterializationError("Agency package member is unsupported")
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or path.parts[0] != "profile":
        raise AgencyMaterializationError("Agency package member path is unsafe")
    relative = PurePosixPath(*path.parts[1:])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise AgencyMaterializationError("Agency package member path is unsafe")
    return relative
