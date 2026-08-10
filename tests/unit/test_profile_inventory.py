from pathlib import Path

import pytest


def _distribution(path: Path, *, name: str, version: str) -> None:
    path.mkdir(parents=True)
    (path / "distribution.yaml").write_text(
        f'name: {name}\nversion: "{version}"\ndescription: "test"\n',
        encoding="utf-8",
    )


def test_profile_inventory_scans_only_distribution_profiles_in_canonical_order(tmp_path) -> None:
    from hermes_fleet.profile_inventory import scan_profile_distributions

    root = tmp_path / "profiles"
    _distribution(root / "z-local-alias", name="agency-backend-engineer", version="0.1.0")
    _distribution(root / "a-local-alias", name="agency-ai-engineer", version="0.1.0")
    (root / "plain-local-profile").mkdir()
    (root / "README.txt").write_text("not a profile", encoding="utf-8")

    assert scan_profile_distributions(root) == [
        {"name": "agency-ai-engineer", "version": "0.1.0"},
        {"name": "agency-backend-engineer", "version": "0.1.0"},
    ]


def test_profile_inventory_deduplicates_identical_distribution_aliases(tmp_path) -> None:
    from hermes_fleet.profile_inventory import scan_profile_distributions

    root = tmp_path / "profiles"
    _distribution(root / "first", name="agency-backend-engineer", version="0.1.0")
    _distribution(root / "second", name="agency-backend-engineer", version="0.1.0")

    assert scan_profile_distributions(root) == [
        {"name": "agency-backend-engineer", "version": "0.1.0"}
    ]


def test_profile_inventory_fails_closed_on_conflicting_versions(tmp_path) -> None:
    from hermes_fleet.profile_inventory import ProfileInventoryError, scan_profile_distributions

    root = tmp_path / "profiles"
    _distribution(root / "first", name="agency-backend-engineer", version="0.1.0")
    _distribution(root / "second", name="agency-backend-engineer", version="0.2.0")

    with pytest.raises(ProfileInventoryError, match="conflicting installed versions"):
        scan_profile_distributions(root)


def test_profile_inventory_omits_malformed_and_unsafe_entries(tmp_path) -> None:
    from hermes_fleet.profile_inventory import scan_profile_distributions

    root = tmp_path / "profiles"
    _distribution(root / "good", name="agency-code-reviewer", version="0.1.0")

    missing_version = root / "missing-version"
    missing_version.mkdir(parents=True)
    (missing_version / "distribution.yaml").write_text(
        "name: agency-missing-version\n", encoding="utf-8"
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
    from hermes_fleet.profile_inventory import scan_profile_distributions

    root = tmp_path / "profiles"
    root.mkdir()
    external = tmp_path / "external"
    _distribution(external / "profile", name="agency-external", version="0.1.0")

    (root / "linked-profile").symlink_to(external / "profile", target_is_directory=True)

    linked_manifest_profile = root / "linked-manifest"
    linked_manifest_profile.mkdir()
    (linked_manifest_profile / "distribution.yaml").symlink_to(
        external / "profile" / "distribution.yaml"
    )

    assert scan_profile_distributions(root) == []


def test_profile_inventory_enforces_explicit_profile_bound(tmp_path) -> None:
    from hermes_fleet.profile_inventory import ProfileInventoryError, scan_profile_distributions

    root = tmp_path / "profiles"
    _distribution(root / "one", name="agency-one", version="0.1.0")
    _distribution(root / "two", name="agency-two", version="0.1.0")

    with pytest.raises(ProfileInventoryError, match="exceeds the bound"):
        scan_profile_distributions(root, max_profiles=1)


def test_missing_profiles_root_reports_empty_inventory(tmp_path) -> None:
    from hermes_fleet.profile_inventory import scan_profile_distributions

    assert scan_profile_distributions(tmp_path / "missing") == []
