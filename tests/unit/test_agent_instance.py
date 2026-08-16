from __future__ import annotations

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
        threading.Thread(target=ensure, args=(item,))
        for item in (first, second)
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
