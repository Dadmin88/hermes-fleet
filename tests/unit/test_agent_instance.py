from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
import yaml

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
from hermes_fleet.agent_instance import (
    AgentInstanceConfigurationChanged,
    AgentInstanceConflict,
    AgentInstanceError,
    AgentInstanceManager,
    AgentInstanceUpgradeRequired,
)
from hermes_fleet.profile_inventory import (
    ProfileInventoryError,
    _profile_content_digest,
    scan_profile_distributions,
)


def model_config(tmp_path: Path, *, model: str = "model-a") -> Path:
    path = tmp_path / "hermes-config.yaml"
    path.write_text(
        f"model:\n  default: {model}\n  provider: provider-test\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def package(
    tmp_path: Path,
    *,
    directory: str = "source-v1",
    repository: str = "https://example.invalid/agency.git",
    revision: str = "a" * 40,
    name: str = "agency-example",
    version: str = "1.0.0",
    soul: str = "base-v1\n",
) -> AgencyProfilePackage:
    profile = tmp_path / directory
    profile.mkdir()
    (profile / "distribution.yaml").write_text(
        f"name: {name}\nversion: {version}\n",
        encoding="utf-8",
    )
    (profile / "SOUL.md").write_text(soul, encoding="utf-8")
    (profile / "config.yaml").write_text(
        "agent:\n  baseline_marker: agency\n",
        encoding="utf-8",
    )
    (profile / "skills").mkdir()
    digest = _profile_content_digest(profile, name, version)
    assert digest is not None
    return AgencyProfilePackage(
        source=AgencySource(repository, revision),
        name=name,
        version=version,
        content_digest=digest,
        category="engineering",
        priority="standard",
        capabilities=("review",),
        distribution_path=f"profiles/{name}",
        local_path=profile,
    )


def refreshed_package(value: AgencyProfilePackage) -> AgencyProfilePackage:
    digest = _profile_content_digest(value.local_path, value.name, value.version)
    assert digest is not None
    return AgencyProfilePackage(
        source=value.source,
        name=value.name,
        version=value.version,
        content_digest=digest,
        category=value.category,
        priority=value.priority,
        capabilities=value.capabilities,
        distribution_path=value.distribution_path,
        local_path=value.local_path,
    )


def manager(tmp_path: Path, config: Path | None = None) -> AgentInstanceManager:
    return AgentInstanceManager(
        profiles_root=tmp_path / "profiles",
        model_config_path=config or model_config(tmp_path),
    )


def test_stable_agent_identity_excludes_pinned_base_revision_version_and_content(
    tmp_path: Path,
) -> None:
    first = bundle_agency_profile(package(tmp_path))
    second = bundle_agency_profile(
        package(
            tmp_path,
            directory="source-v2",
            revision="b" * 40,
            version="2.0.0",
            soul="base-v2\n",
        )
    )

    first_identity = AgentInstanceManager.identity_for(first.resolved)
    second_identity = AgentInstanceManager.identity_for(second.resolved)

    assert first_identity == second_identity
    assert first_identity[0].startswith("sha256:")
    assert first_identity[1].startswith("fleet-agent-")


def test_ensure_creates_native_persistent_profile_without_run_state_or_credentials(
    tmp_path: Path,
) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    config_path = model_config(tmp_path)
    service = manager(tmp_path, config_path)

    binding = service.ensure(bundle)
    profile = service.profile_path(binding)

    assert profile.is_dir()
    assert (profile / "SOUL.md").read_text(encoding="utf-8") == "base-v1\n"
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert config["agent"]["baseline_marker"] == "agency"
    assert config["model"] == {"default": "model-a", "provider": "provider-test"}
    assert not (profile / ".env").exists()
    assert not (profile / ".fleet-execution-owner").exists()
    assert not (profile / ".fleet-execution-slot").exists()
    assert (profile / ".fleet-agent-instance.json").is_file()
    assert (profile / ".fleet-agent-state.json").is_file()
    assert (profile / ".fleet-agent-state.lock").is_file()
    assert not hasattr(service, "cleanup")


def test_same_base_reuses_agent_and_preserves_learned_skill_and_base_inventory(
    tmp_path: Path,
) -> None:
    source = package(tmp_path)
    bundle = bundle_agency_profile(source)
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)

    learned = profile / "skills" / "learned-private" / "SKILL.md"
    learned.parent.mkdir()
    learned.write_text("durable learned skill\n", encoding="utf-8")
    mutable_digest = _profile_content_digest(profile, source.name, source.version)
    assert mutable_digest is not None
    assert "sha256:" + mutable_digest != binding.base_content_digest

    reopened = service.ensure(bundle)
    assert reopened == binding
    assert learned.read_text(encoding="utf-8") == "durable learned skill\n"
    assert scan_profile_distributions(service.profiles_root) == [
        {
            "name": source.name,
            "version": source.version,
            "content_digest": binding.base_content_digest.removeprefix("sha256:"),
        }
    ]


def test_changed_agency_base_keeps_stable_identity_but_fails_upgrade_required(
    tmp_path: Path,
) -> None:
    first = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(first)
    changed = bundle_agency_profile(
        package(
            tmp_path,
            directory="source-v2",
            revision="b" * 40,
            version="2.0.0",
            soul="new immutable base\n",
        )
    )
    assert AgentInstanceManager.identity_for(changed.resolved) == (
        binding.instance_id,
        binding.profile,
    )

    with pytest.raises(AgentInstanceUpgradeRequired, match="upgrade"):
        service.ensure(changed)
    assert service.profile_path(binding).exists()


def test_model_baseline_change_fails_closed_without_rewriting_profile(
    tmp_path: Path,
) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    config_path = model_config(tmp_path)
    first = manager(tmp_path, config_path)
    binding = first.ensure(bundle)
    profile_config = first.profile_path(binding) / "config.yaml"
    before = profile_config.read_bytes()

    model_config(tmp_path, model="model-b")
    second = manager(tmp_path, config_path)
    with pytest.raises(
        AgentInstanceConfigurationChanged,
        match="model baseline changed",
    ):
        second.ensure(bundle)
    assert profile_config.read_bytes() == before


def test_profile_config_drift_and_run_scoped_state_fail_closed(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)
    config_path = profile / "config.yaml"

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["fleet_runtime"] = {"container_id": "not-durable"}
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    config_path.chmod(0o600)
    with pytest.raises(AgentInstanceError, match="run-scoped state"):
        service.open(bundle.resolved)


@pytest.mark.parametrize(
    "key",
    [
        "run_id",
        "execution-id",
        "planFingerprint",
        "idempotency_key",
        "deadline_ms",
        "resource_limits",
        "temporary_credentials",
    ],
)
def test_additional_run_scoped_config_keys_fail_closed(
    tmp_path: Path,
    key: str,
) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)
    config_path = profile / "config.yaml"

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config.setdefault("agent", {})[key] = "not-durable"
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    config_path.chmod(0o600)

    with pytest.raises(AgentInstanceError, match="run-scoped state"):
        service.open(bundle.resolved)


def test_reserved_legacy_owner_or_env_state_is_never_accepted(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)

    owner = profile / ".fleet-execution-owner"
    owner.write_text("legacy\n", encoding="utf-8")
    with pytest.raises(AgentInstanceError, match="reserved run/credential state"):
        service.open(bundle.resolved)
    owner.unlink()

    env = profile / ".env"
    env.write_text("TEMPORARY_VALUE=forbidden\n", encoding="utf-8")
    with pytest.raises(AgentInstanceError, match="reserved run/credential state"):
        service.open(bundle.resolved)


def test_memory_and_skill_guards_version_without_touching_profile_config(
    tmp_path: Path,
) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)
    config_before = (profile / "config.yaml").read_bytes()

    with service.mutation_guard(
        binding,
        component="memory",
        expected_generation=0,
    ) as state:
        assert state.memory_generation == 0
        (profile / "memory-native-marker").write_text(
            "memory mutation\n",
            encoding="utf-8",
        )
    state = service.read_state(binding)
    assert state.memory_generation == 1
    assert state.skills_generation == 0

    with service.mutation_guard(binding, component="skills", expected_generation=0):
        learned = profile / "skills" / "learned" / "SKILL.md"
        learned.parent.mkdir()
        learned.write_text("skill mutation\n", encoding="utf-8")
    state = service.read_state(binding)
    assert state.memory_generation == 1
    assert state.skills_generation == 1
    assert (profile / "config.yaml").read_bytes() == config_before


def test_failed_mutation_does_not_advance_generation(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)

    with pytest.raises(RuntimeError, match="abort mutation"):
        with service.mutation_guard(binding, component="memory", expected_generation=0):
            raise RuntimeError("abort mutation")
    assert service.read_state(binding).memory_generation == 0


def test_concurrent_same_generation_mutations_serialize_and_one_conflicts(
    tmp_path: Path,
) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    entered = threading.Event()
    release = threading.Event()
    successes: list[str] = []
    conflicts: list[str] = []
    failures: list[BaseException] = []

    def first() -> None:
        try:
            with service.mutation_guard(
                binding,
                component="memory",
                expected_generation=0,
            ):
                entered.set()
                assert release.wait(5)
                successes.append("first")
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    def second() -> None:
        try:
            with service.mutation_guard(
                binding,
                component="memory",
                expected_generation=0,
            ):
                successes.append("second")
        except AgentInstanceConflict:
            conflicts.append("second")
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    assert entered.wait(5)
    second_thread.start()
    release.set()
    first_thread.join(5)
    second_thread.join(5)

    assert failures == []
    assert successes == ["first"]
    assert conflicts == ["second"]
    assert service.read_state(binding).memory_generation == 1


def test_new_manager_reopens_same_disk_agent_and_generations(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    config_path = model_config(tmp_path)
    first = manager(tmp_path, config_path)
    binding = first.ensure(bundle)
    with first.mutation_guard(binding, component="skills", expected_generation=0):
        pass

    second = manager(tmp_path, config_path)
    reopened = second.open(bundle.resolved)
    assert reopened == binding
    assert second.read_state(reopened).skills_generation == 1


def test_concurrent_ensure_creates_one_agent_without_staging_residue(
    tmp_path: Path,
) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    config_path = model_config(tmp_path)
    first = manager(tmp_path, config_path)
    second = manager(tmp_path, config_path)
    bindings = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def ensure(value: AgentInstanceManager) -> None:
        try:
            barrier.wait(5)
            bindings.append(value.ensure(bundle))
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    threads = [
        threading.Thread(target=ensure, args=(item,)) for item in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert errors == []
    assert len(bindings) == 2
    assert bindings[0] == bindings[1]
    profiles = [item for item in first.profiles_root.iterdir() if item.is_dir()]
    assert [item.name for item in profiles] == [bindings[0].profile]
    assert not any(".creating-" in item.name for item in first.profiles_root.iterdir())


def test_agency_bundle_cannot_preseed_reserved_agent_or_run_metadata(
    tmp_path: Path,
) -> None:
    source = package(tmp_path)
    (source.local_path / ".fleet-agent-instance.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    # Recompute an exact bundle after the source gained the reserved file. The
    # Agency package is still internally self-consistent; Agent Instance policy
    # is what rejects the reserved destination state.
    bundle = bundle_agency_profile(source)
    service = manager(tmp_path)
    with pytest.raises(AgentInstanceError, match="reserved Agent Instance metadata"):
        service.ensure(bundle)


def test_metadata_and_lock_symlinks_fail_closed(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)

    metadata = profile / ".fleet-agent-instance.json"
    metadata_payload = metadata.read_bytes()
    metadata.unlink()
    outside = tmp_path / "outside-metadata"
    outside.write_bytes(metadata_payload)
    metadata.symlink_to(outside)
    with pytest.raises(AgentInstanceError):
        service.open(bundle.resolved)


def test_immutable_agency_base_manifest_rejects_base_file_drift_but_allows_new_learning(
    tmp_path: Path,
) -> None:
    source = package(tmp_path)
    bundle = bundle_agency_profile(source)
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)

    manifest = profile / ".fleet-agent-base-manifest.json"
    assert manifest.is_file()
    assert manifest.stat().st_mode & 0o777 == 0o600

    learned = profile / "skills" / "learned-after-create" / "SKILL.md"
    learned.parent.mkdir()
    learned.write_text("new learned overlay\n", encoding="utf-8")
    assert service.ensure(bundle) == binding

    (profile / "SOUL.md").write_text("tampered base\n", encoding="utf-8")
    with pytest.raises(AgentInstanceError, match="immutable Agency base"):
        service.open(bundle.resolved)
    with pytest.raises(
        ProfileInventoryError,
        match="persistent Agent Instance metadata",
    ):
        scan_profile_distributions(service.profiles_root)


def test_nested_reserved_run_state_in_agency_bundle_is_rejected(tmp_path: Path) -> None:
    source = package(tmp_path)
    nested = source.local_path / "skills" / "unsafe"
    nested.mkdir()
    (nested / ".env").write_text("TOKEN=not-allowed\n", encoding="utf-8")
    bundle = bundle_agency_profile(refreshed_package(source))
    service = manager(tmp_path)
    with pytest.raises(AgentInstanceError, match="reserved run/credential state"):
        service.ensure(bundle)


@pytest.mark.parametrize(
    "key",
    [
        "container_ids",
        "approval_budgets",
        "network_grant",
        "filesystem_grant",
        "host_broker_grant",
        "run_authority_hash",
        "secret_handle",
        "secret_ref",
    ],
)
def test_persistent_config_rejects_run_state_key_variants(
    tmp_path: Path,
    key: str,
) -> None:
    source = package(tmp_path)
    config = yaml.safe_load(
        (source.local_path / "config.yaml").read_text(encoding="utf-8")
    )
    config[key] = "temporary"
    (source.local_path / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=True),
        encoding="utf-8",
    )
    bundle = bundle_agency_profile(refreshed_package(source))
    with pytest.raises(AgentInstanceError, match="run-scoped state"):
        manager(tmp_path).ensure(bundle)


def test_profile_and_profiles_root_permissions_fail_closed(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)

    profile.chmod(0o755)
    with pytest.raises(AgentInstanceError, match="profile is invalid"):
        service.open(bundle.resolved)
    profile.chmod(0o700)

    service.profiles_root.chmod(0o777)
    with pytest.raises(AgentInstanceError, match="profiles root is unsafe"):
        service.open(bundle.resolved)


def test_generation_exhaustion_fails_before_mutation_window(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)
    state_path = profile / ".fleet-agent-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": "fleet.agent-state.v1",
                "memory_generation": (1 << 63) - 1,
                "skills_generation": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    state_path.chmod(0o600)

    entered = False
    with pytest.raises(AgentInstanceConflict, match="generation is exhausted"):
        with service.mutation_guard(
            binding,
            component="memory",
            expected_generation=(1 << 63) - 1,
        ):
            entered = True
    assert entered is False


def test_base_manifest_symlink_fails_closed(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)
    manifest = profile / ".fleet-agent-base-manifest.json"
    payload = manifest.read_bytes()
    manifest.unlink()
    outside = tmp_path / "outside-base-manifest"
    outside.write_bytes(payload)
    outside.chmod(0o600)
    manifest.symlink_to(outside)
    with pytest.raises(AgentInstanceError):
        service.open(bundle.resolved)


def test_base_manifest_digest_is_bound_into_agent_metadata(tmp_path: Path) -> None:
    bundle = bundle_agency_profile(package(tmp_path))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)
    manifest = profile / ".fleet-agent-base-manifest.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["files"] = []
    manifest.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)

    with pytest.raises(AgentInstanceError, match="base manifest digest changed"):
        service.open(bundle.resolved)


def test_immutable_base_intermediate_symlink_cannot_redirect_verification(
    tmp_path: Path,
) -> None:
    source = package(tmp_path)
    bundled = source.local_path / "skills" / "bundled"
    bundled.mkdir()
    (bundled / "SKILL.md").write_text("immutable bundled skill\n", encoding="utf-8")
    bundle = bundle_agency_profile(refreshed_package(source))
    service = manager(tmp_path)
    binding = service.ensure(bundle)
    profile = service.profile_path(binding)

    skills = profile / "skills"
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    outside_bundled = outside / "bundled"
    outside_bundled.mkdir()
    (outside_bundled / "SKILL.md").write_text(
        "immutable bundled skill\n",
        encoding="utf-8",
    )
    moved = profile / "skills-original"
    skills.rename(moved)
    skills.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AgentInstanceError, match="immutable Agency base"):
        service.open(bundle.resolved)
