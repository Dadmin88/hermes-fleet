from pathlib import Path

import pytest

from hermes_fleet import profile_inventory
from hermes_fleet.profile_inventory import (
    ProfileInventoryError,
    scan_profile_distributions,
)


def _distribution(path: Path, *, name: str, version: str) -> None:
    path.mkdir(parents=True)
    (path / "distribution.yaml").write_text(
        f'name: {name}\nversion: "{version}"\ndescription: "test"\n',
        encoding="utf-8",
    )


def _digestible_distribution(
    path: Path,
    *,
    name: str = "agency-example",
    version: str = "1.0.0",
) -> None:
    _distribution(path, name=name, version=version)
    references = path / "skills" / "review" / "references"
    references.mkdir(parents=True)
    (path / "SOUL.md").write_text("identity\n", encoding="utf-8")
    (path / "skills" / "review" / "SKILL.md").write_text(
        "procedure\n", encoding="utf-8"
    )
    (references / "checklist.md").write_text("reference\n", encoding="utf-8")


def _only_digest(root: Path) -> str:
    inventory = scan_profile_distributions(root)
    assert len(inventory) == 1
    return inventory[0]["content_digest"]


def test_profile_inventory_scans_distribution_profiles_in_order(tmp_path) -> None:
    root = tmp_path / "profiles"
    _distribution(
        root / "z-local-alias",
        name="agency-backend-engineer",
        version="0.1.0",
    )
    _distribution(
        root / "a-local-alias",
        name="agency-ai-engineer",
        version="0.1.0",
    )
    (root / "plain-local-profile").mkdir()
    (root / "README.txt").write_text("not a profile", encoding="utf-8")

    assert scan_profile_distributions(root) == [
        {"name": "agency-ai-engineer", "version": "0.1.0"},
        {"name": "agency-backend-engineer", "version": "0.1.0"},
    ]


def test_profile_inventory_deduplicates_identical_aliases(tmp_path) -> None:
    root = tmp_path / "profiles"
    _distribution(
        root / "first",
        name="agency-backend-engineer",
        version="0.1.0",
    )
    _distribution(
        root / "second",
        name="agency-backend-engineer",
        version="0.1.0",
    )

    assert scan_profile_distributions(root) == [
        {"name": "agency-backend-engineer", "version": "0.1.0"}
    ]


def test_profile_inventory_fails_closed_on_conflicting_versions(tmp_path) -> None:
    root = tmp_path / "profiles"
    _distribution(
        root / "first",
        name="agency-backend-engineer",
        version="0.1.0",
    )
    _distribution(
        root / "second",
        name="agency-backend-engineer",
        version="0.2.0",
    )

    with pytest.raises(ProfileInventoryError, match="conflicting installed versions"):
        scan_profile_distributions(root)


def test_profile_inventory_omits_malformed_and_unsafe_entries(tmp_path) -> None:
    root = tmp_path / "profiles"
    _distribution(root / "good", name="agency-code-reviewer", version="0.1.0")

    missing_version = root / "missing-version"
    missing_version.mkdir(parents=True)
    (missing_version / "distribution.yaml").write_text(
        "name: agency-missing-version\n",
        encoding="utf-8",
    )

    invalid_name = root / "invalid-name"
    _distribution(invalid_name, name="agency invalid", version="0.1.0")

    duplicate_key = root / "duplicate-key"
    duplicate_key.mkdir(parents=True)
    (duplicate_key / "distribution.yaml").write_text(
        "name: agency-one\nname: agency-two\nversion: 0.1.0\n",
        encoding="utf-8",
    )

    oversized = root / "oversized"
    oversized.mkdir(parents=True)
    (oversized / "distribution.yaml").write_text(
        "name: agency-oversized\nversion: 0.1.0\n" + ("# padding\n" * 9_000),
        encoding="utf-8",
    )

    assert scan_profile_distributions(root) == [
        {"name": "agency-code-reviewer", "version": "0.1.0"}
    ]


def test_profile_inventory_skips_symlinked_profiles_and_manifests(tmp_path) -> None:
    root = tmp_path / "profiles"
    root.mkdir()
    external = tmp_path / "external"
    _distribution(external / "profile", name="agency-external", version="0.1.0")

    (root / "linked-profile").symlink_to(
        external / "profile",
        target_is_directory=True,
    )

    linked_manifest_profile = root / "linked-manifest"
    linked_manifest_profile.mkdir()
    (linked_manifest_profile / "distribution.yaml").symlink_to(
        external / "profile" / "distribution.yaml"
    )

    assert scan_profile_distributions(root) == []


def test_profile_inventory_enforces_explicit_profile_bound(tmp_path) -> None:
    root = tmp_path / "profiles"
    _distribution(root / "one", name="agency-one", version="0.1.0")
    _distribution(root / "two", name="agency-two", version="0.1.0")

    with pytest.raises(ProfileInventoryError, match="exceeds the bound"):
        scan_profile_distributions(root, max_profiles=1)


def test_missing_profiles_root_reports_empty_inventory(tmp_path) -> None:
    assert scan_profile_distributions(tmp_path / "missing") == []


def test_profile_inventory_matches_agency_v1_digest_fixture(tmp_path) -> None:
    root = tmp_path / "profiles"
    _digestible_distribution(root / "profile")

    assert scan_profile_distributions(root) == [
        {
            "name": "agency-example",
            "version": "1.0.0",
            "content_digest": (
                "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f"
            ),
        }
    ]
    assert (
        profile_inventory.PROFILE_CONTENT_DIGEST_SCHEMA
        == "hermes-agency-profile-content.v1"
    )


def test_profile_digest_tracks_behavior_but_ignores_runtime_manifest_metadata(
    tmp_path,
) -> None:
    root = tmp_path / "profiles"
    profile = root / "profile"
    _digestible_distribution(profile)
    original = _only_digest(root)

    (profile / "README.md").write_text("human docs\n", encoding="utf-8")
    (profile / "distribution.yaml").write_text(
        "name: agency-example\n"
        "version: 1.0.0\n"
        "description: changed\n"
        "source: /runtime/install/path\n"
        "installed_at: 2026-08-10T00:00:00+00:00\n",
        encoding="utf-8",
    )
    assert _only_digest(root) == original

    (profile / "SOUL.md").write_text("changed identity\n", encoding="utf-8")
    assert _only_digest(root) != original


def test_profile_digest_tracks_nested_skill_support_files(tmp_path) -> None:
    root = tmp_path / "profiles"
    profile = root / "profile"
    _digestible_distribution(profile)
    original = _only_digest(root)

    (profile / "skills" / "review" / "references" / "checklist.md").write_text(
        "changed reference\n", encoding="utf-8"
    )
    assert _only_digest(root) != original


def test_profile_digest_tracks_config_marker_and_executable_semantics(tmp_path) -> None:
    root = tmp_path / "profiles"
    profile = root / "profile"
    _digestible_distribution(profile)
    original = _only_digest(root)

    (profile / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    with_config = _only_digest(root)
    assert with_config != original

    (profile / "config.yaml").unlink()
    (profile / ".no-bundled-skills").write_text("", encoding="utf-8")
    with_marker = _only_digest(root)
    assert with_marker != original

    (profile / ".no-bundled-skills").unlink()
    script = profile / "skills" / "review" / "helper.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    non_executable = _only_digest(root)
    script.chmod(0o755)
    assert _only_digest(root) != non_executable


def test_unsafe_or_over_bound_behavior_keeps_presence_without_exact_digest(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "profiles"
    profile = root / "profile"
    _digestible_distribution(profile)
    external = tmp_path / "external.md"
    external.write_text("outside\n", encoding="utf-8")
    (profile / "skills" / "review" / "linked.md").symlink_to(external)

    assert scan_profile_distributions(root) == [
        {"name": "agency-example", "version": "1.0.0"}
    ]

    (profile / "skills" / "review" / "linked.md").unlink()
    monkeypatch.setattr(profile_inventory, "_MAX_CONTENT_FILES", 1)
    assert scan_profile_distributions(root) == [
        {"name": "agency-example", "version": "1.0.0"}
    ]


def test_duplicate_distribution_fails_closed_on_conflicting_content_digest(
    tmp_path,
) -> None:
    root = tmp_path / "profiles"
    first = root / "first"
    second = root / "second"
    _digestible_distribution(first, name="agency-backend-engineer")
    _digestible_distribution(second, name="agency-backend-engineer")
    (second / "SOUL.md").write_text("different identity\n", encoding="utf-8")

    with pytest.raises(ProfileInventoryError, match="conflicting installed content"):
        scan_profile_distributions(root)
