from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_fleet.agency_snapshot import (
    AgencySnapshotError,
    AgencySource,
    acquire_agency_snapshot,
)

DIGEST = "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f"
OTHER_DIGEST = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _catalog(*, digest: str = DIGEST) -> dict[str, object]:
    return {
        "schema_version": 2,
        "content_digest_schema": "hermes-agency-profile-content.v1",
        "agency": {
            "name": "hermes-agency",
            "version": "1.0.0",
            "profile_count": 1,
            "orchestrator": "agency-example",
        },
        "distribution": {
            "format": "hermes-profile-distribution",
            "profile_identity_field": "name",
            "profile_path_template": "profiles/{name}",
        },
        "routing": {
            "selection_order": ["professional-profile", "eligible-node"],
            "live_presence_owner": "hermes-fleet",
            "missing_presence_behavior": "fleet-locate-or-place",
        },
        "profiles": [
            {
                "name": "agency-example",
                "version": "1.0.0",
                "category": "engineering",
                "priority": "standard",
                "description": "Example Agency profile.",
                "distribution_path": "profiles/agency-example",
                "content_digest": digest,
                "capabilities": ["review"],
            }
        ],
    }


def _write_catalog_script(repo: Path, catalog: dict[str, object]) -> None:
    (repo / "catalog.py").write_text(
        "import json\n"
        f"CATALOG = {catalog!r}\n"
        "print(json.dumps(CATALOG, separators=(',', ':'), sort_keys=True))\n",
        encoding="utf-8",
    )


def _write_profile(repo: Path) -> None:
    profile = repo / "profiles" / "agency-example"
    references = profile / "skills" / "review" / "references"
    references.mkdir(parents=True)
    (profile / "distribution.yaml").write_text(
        "name: agency-example\n"
        "version: 1.0.0\n"
        'description: "Example Agency profile."\n',
        encoding="utf-8",
    )
    (profile / "SOUL.md").write_text("identity\n", encoding="utf-8")
    (profile / "skills" / "review" / "SKILL.md").write_text(
        "procedure\n", encoding="utf-8"
    )
    (references / "checklist.md").write_text("reference\n", encoding="utf-8")


def _repo(
    tmp_path: Path, *, catalog: dict[str, object] | None = None
) -> tuple[Path, str]:
    if shutil.which("git") is None:
        pytest.skip("git is required for Agency snapshot tests")
    repo = tmp_path / "agency"
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(repo)],
        stdin=subprocess.DEVNULL,
        check=True,
    )
    _git(repo, "config", "user.name", "Fleet Test")
    _git(repo, "config", "user.email", "fleet-test@example.invalid")
    _write_profile(repo)
    _write_catalog_script(repo, catalog or _catalog())
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_snapshot_checks_out_exact_revision_and_resolves_verified_package(
    tmp_path,
) -> None:
    repo, revision = _repo(tmp_path)
    (repo / "profiles" / "agency-example" / "SOUL.md").write_text(
        "changed on main\n", encoding="utf-8"
    )
    newer_revision = _commit(repo, "newer main")
    assert newer_revision != revision

    source = AgencySource(repository=str(repo), revision=revision)
    with acquire_agency_snapshot(source) as snapshot:
        assert snapshot.source == source
        assert snapshot.agency_version == "1.0.0"
        assert snapshot.orchestrator == "agency-example"
        package = snapshot.resolve_profile("agency-example")
        assert package.source == source
        assert package.name == "agency-example"
        assert package.version == "1.0.0"
        assert package.content_digest == DIGEST
        assert package.distribution_path == "profiles/agency-example"
        assert package.local_path.is_dir()
        assert (
            package.local_path.joinpath("SOUL.md").read_text(encoding="utf-8")
            == "identity\n"
        )


def test_source_requires_full_exact_lowercase_git_object_id(tmp_path) -> None:
    repo, revision = _repo(tmp_path)
    assert len(revision) == 40

    for invalid in [
        "main",
        "deadbeef",
        revision[:39],
        revision.upper(),
        "g" * 40,
    ]:
        with pytest.raises(AgencySnapshotError, match="exact full git object ID"):
            AgencySource(repository=str(repo), revision=invalid)


def test_snapshot_cache_reuses_pinned_object_without_source_repository(
    tmp_path, monkeypatch
) -> None:
    repo, revision = _repo(tmp_path)
    cache_root = tmp_path / "agency-cache"
    source = AgencySource(repository=str(repo), revision=revision)
    monkeypatch.setenv("FLEET_AGENCY_CACHE_ROOT", str(cache_root))

    with acquire_agency_snapshot(source) as snapshot:
        package = snapshot.resolve_profile("agency-example")
        assert package.name == "agency-example"
        assert snapshot.checkout_root.is_relative_to(cache_root / "checkouts")

    shutil.rmtree(repo)

    with acquire_agency_snapshot(source) as snapshot:
        package = snapshot.resolve_profile("agency-example")
        assert package.content_digest == DIGEST
        assert snapshot.checkout_root.is_relative_to(cache_root / "checkouts")

    repositories = list((cache_root / "repositories").glob("*.git"))
    assert len(repositories) == 1
    assert _git(repositories[0], "rev-parse", "--is-bare-repository") == "true"
    assert list((cache_root / "checkouts").iterdir()) == []


def test_snapshot_cache_root_must_be_absolute(tmp_path) -> None:
    repo, revision = _repo(tmp_path)
    with pytest.raises(AgencySnapshotError, match="cache root must be an absolute"):
        with acquire_agency_snapshot(
            AgencySource(str(repo), revision),
            cache_root=Path("relative-cache"),
        ):
            pass


def test_acquisition_fails_when_exact_revision_is_not_in_repository(tmp_path) -> None:
    repo, _ = _repo(tmp_path)
    source = AgencySource(repository=str(repo), revision="a" * 40)

    with pytest.raises(AgencySnapshotError, match="git operation failed"):
        with acquire_agency_snapshot(source):
            pass


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 99, "catalog schema is unsupported"),
        (
            "content_digest_schema",
            "hermes-agency-profile-content.v99",
            "content digest schema is unsupported",
        ),
    ],
)
def test_unsupported_catalog_contract_fails_closed(
    tmp_path, field: str, value: object, message: str
) -> None:
    catalog = _catalog()
    catalog[field] = value
    repo, revision = _repo(tmp_path, catalog=catalog)

    with pytest.raises(AgencySnapshotError, match=message):
        with acquire_agency_snapshot(AgencySource(str(repo), revision)):
            pass


def test_duplicate_catalog_json_members_fail_closed(tmp_path) -> None:
    repo, _ = _repo(tmp_path)
    (repo / "catalog.py").write_text(
        'print(\'{"schema_version":2,"schema_version":2}\')\n',
        encoding="utf-8",
    )
    revision = _commit(repo, "duplicate catalog key")

    with pytest.raises(AgencySnapshotError, match="invalid JSON"):
        with acquire_agency_snapshot(AgencySource(str(repo), revision)):
            pass


def test_catalog_distribution_path_traversal_is_rejected(tmp_path) -> None:
    catalog = _catalog()
    catalog["profiles"][0]["distribution_path"] = "../agency-example"
    repo, revision = _repo(tmp_path, catalog=catalog)

    with pytest.raises(AgencySnapshotError, match="distribution path is invalid"):
        with acquire_agency_snapshot(AgencySource(str(repo), revision)):
            pass


def test_selected_profile_digest_must_match_checkout_bytes(tmp_path) -> None:
    repo, revision = _repo(tmp_path, catalog=_catalog(digest=OTHER_DIGEST))

    with acquire_agency_snapshot(AgencySource(str(repo), revision)) as snapshot:
        with pytest.raises(
            AgencySnapshotError,
            match="content does not match catalog",
        ):
            snapshot.resolve_profile("agency-example")


def test_selected_profile_manifest_identity_must_match_catalog(tmp_path) -> None:
    repo, _ = _repo(tmp_path)
    manifest = repo / "profiles" / "agency-example" / "distribution.yaml"
    manifest.write_text(
        'name: agency-other\nversion: 1.0.0\ndescription: "Example Agency profile."\n',
        encoding="utf-8",
    )
    revision = _commit(repo, "manifest drift")

    with acquire_agency_snapshot(AgencySource(str(repo), revision)) as snapshot:
        with pytest.raises(
            AgencySnapshotError,
            match="identity does not match catalog",
        ):
            snapshot.resolve_profile("agency-example")


def test_symlinked_distribution_path_fails_closed(tmp_path) -> None:
    repo, _ = _repo(tmp_path)
    profile = repo / "profiles" / "agency-example"
    external = repo / "real-profile"
    profile.rename(external)
    profile.symlink_to(external, target_is_directory=True)
    revision = _commit(repo, "symlink profile")

    with acquire_agency_snapshot(AgencySource(str(repo), revision)) as snapshot:
        with pytest.raises(AgencySnapshotError, match="contains a symlink"):
            snapshot.resolve_profile("agency-example")


def test_unknown_profile_does_not_fall_back_to_another_profession(
    tmp_path,
) -> None:
    repo, revision = _repo(tmp_path)

    with acquire_agency_snapshot(AgencySource(str(repo), revision)) as snapshot:
        with pytest.raises(AgencySnapshotError, match="not in the snapshot"):
            snapshot.resolve_profile("agency-frontend-engineer")
