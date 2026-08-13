from __future__ import annotations

from pathlib import Path

import pytest

from hermes_fleet.agency_materialization import (
    AgencyMaterializationError,
    ImmutableAgencyBundle,
    bundle_agency_profile,
    materialize_agency_bundle,
)
from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
from hermes_fleet.profile_inventory import _profile_content_digest


def _package(tmp_path: Path) -> AgencyProfilePackage:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "distribution.yaml").write_text(
        "name: agency-example\nversion: 1.0.0\n", encoding="utf-8"
    )
    (profile / "SOUL.md").write_text("exact identity\n", encoding="utf-8")
    (profile / "skills").mkdir()
    digest = _profile_content_digest(profile, "agency-example", "1.0.0")
    assert digest is not None
    return AgencyProfilePackage(
        source=AgencySource("https://example.invalid/agency.git", "a" * 40),
        name="agency-example",
        version="1.0.0",
        content_digest=digest,
        category="engineering",
        priority="standard",
        capabilities=("review",),
        distribution_path="profiles/agency-example",
        local_path=profile,
    )


def test_exact_package_materializes_after_mutable_source_changes(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    bundle = bundle_agency_profile(package)
    (package.local_path / "SOUL.md").write_text("mutated later\n", encoding="utf-8")

    materialized = materialize_agency_bundle(bundle, destination=tmp_path / "worker")

    assert (materialized / "SOUL.md").read_text(encoding="utf-8") == "exact identity\n"
    assert bundle.resolved.content_digest == "sha256:" + package.content_digest


def test_package_bytes_are_deterministic(tmp_path: Path) -> None:
    package = _package(tmp_path)
    first = bundle_agency_profile(package)
    second = bundle_agency_profile(package)
    assert first.payload == second.payload
    assert first.archive_sha256 == second.archive_sha256


def test_tampered_archive_fails_before_materialization(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(_package(tmp_path))
    with pytest.raises(AgencyMaterializationError, match="archive digest"):
        ImmutableAgencyBundle(
            resolved=bundle.resolved,
            archive_sha256=bundle.archive_sha256,
            payload=bundle.payload + b"tampered",
        )


def test_symlinked_profile_content_is_rejected(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (package.local_path / "escape").symlink_to(package.local_path / "SOUL.md")
    with pytest.raises(AgencyMaterializationError, match="symlink"):
        bundle_agency_profile(package)


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(_package(tmp_path))
    destination = tmp_path / "worker"
    destination.mkdir()
    marker = destination / "keep"
    marker.write_text("unrelated", encoding="utf-8")
    with pytest.raises(AgencyMaterializationError, match="destination exists"):
        materialize_agency_bundle(bundle, destination=destination)
    assert marker.read_text(encoding="utf-8") == "unrelated"
