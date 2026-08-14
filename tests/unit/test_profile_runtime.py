from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_fleet.agency_materialization import ImmutableAgencyBundle
from hermes_fleet.execution_package import ExactExecutionPackage
from hermes_fleet.hermes_runs import HermesRunResult
from hermes_fleet.recipes import ResolvedRecipe

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64


def package(payload: bytes) -> ExactExecutionPackage:
    recipe = ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": HASH_1,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "acceptance",
                "version": "1.0.0",
                "content_digest": HASH_2,
            },
            "extensions": {},
        }
    )
    return ExactExecutionPackage(
        execution_id="execution-1",
        idempotency_key="execution-1",
        resolved_recipe=recipe,
        capabilities_hash=HASH_3,
        target={
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "binding_generation": 7,
            "admission_generation": 9,
        },
        authorization={
            "requester": "peer-controller-1",
            "operation": "fleet.hermes.run",
            "resolved_recipe_hash": recipe.content_hash,
            "policy_digest": HASH_4,
            "deadline_ms": 20_000,
            "secret_refs": ["secret://worker/env/OPENROUTER_API_KEY"],
        },
        prompt="Return the exact FX8 marker.",
        agency_bundle=ImmutableAgencyBundle(
            resolved=recipe.agent,
            archive_sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
            payload=payload,
        ),
    )


class Runs:
    def __init__(self, *, profile: str, calls: list[tuple]) -> None:
        self.profile = profile
        self.calls = calls

    def start(self, *, prompt, session_id, timeout_seconds):
        self.calls.append(("start", self.profile, prompt, session_id))
        return "run-1"

    def wait(self, *, run_id, timeout_seconds):
        self.calls.append(("wait", self.profile, run_id))
        return HermesRunResult(run_id=run_id, text="done")

    def stop(self, run_id, *, timeout_seconds=None):
        self.calls.append(("stop", self.profile, run_id))

    def status(self, run_id):
        self.calls.append(("status", self.profile, run_id))
        return "running"


def execution_slot(root: Path) -> Path:
    slot = root / "profiles" / "fleet-execution"
    slot.mkdir(parents=True, mode=0o700)
    slot.chmod(0o700)
    marker = slot / ".fleet-execution-slot"
    marker.write_text("hermes-fleet.execution-slot.v1\n")
    marker.chmod(0o600)
    return slot


def hermes_model_config(root: Path) -> Path:
    path = root / "hermes-config.yaml"
    if not path.exists():
        path.write_text(
            "model:\n  default: gpt-test\n  provider: openai-codex\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
    return path


def test_profile_runtime_materializes_exact_bundle_scopes_secret_and_cleans(
    tmp_path,
) -> None:
    from hermes_fleet.agency_materialization import bundle_agency_profile
    from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    source = tmp_path / "source"
    (source / "skills").mkdir(parents=True)
    (source / "SOUL.md").write_text("exact soul")
    (source / "config.yaml").write_text("agent:\n  max_turns: 5\n")
    (source / "skills" / "SKILL.md").write_text("exact skill")
    (source / "distribution.yaml").write_text("name: acceptance\nversion: 1.0.0\n")
    agency = AgencyProfilePackage(
        source=AgencySource(
            repository="https://example.invalid/agency.git", revision="a" * 40
        ),
        name="acceptance",
        version="1.0.0",
        content_digest="",
        category="test",
        priority="normal",
        capabilities=(),
        distribution_path=".",
        local_path=source,
    )
    # Use the production digest/bundler to create exact accepted bytes.
    from hermes_fleet.profile_inventory import _profile_content_digest

    object.__setattr__(
        agency,
        "content_digest",
        _profile_content_digest(source, "acceptance", "1.0.0"),
    )
    bundle = bundle_agency_profile(agency)
    value = package(bundle.payload)
    object.__setattr__(value, "resolved_recipe", value.resolved_recipe)
    object.__setattr__(value, "agency_bundle", bundle)
    calls: list[tuple] = []
    slot = execution_slot(tmp_path)
    (slot / "sessions").mkdir()
    (slot / "SOUL.md").write_text("gateway scaffold")
    (slot / ".env").write_text("")
    model_config = tmp_path / "hermes-config.yaml"
    model_config.write_text(
        "model:\n  default: gpt-test\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    model_config.chmod(0o600)
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=calls),
        api_server_key="profile-api-key",
        model_config_path=model_config,
    )

    profile = runtime.materialize(
        value,
        secrets={"secret://worker/env/OPENROUTER_API_KEY": "test-secret-value"},
    )
    profile_path = tmp_path / "profiles" / profile

    assert profile == "fleet-execution"
    assert (profile_path / ".fleet-execution-owner").read_text() == "execution-1\n"
    assert (profile_path / "SOUL.md").read_text() == "exact soul"
    assert not (profile_path / "sessions").exists()
    assert (profile_path / ".env").read_text() == (
        "API_SERVER_KEY=profile-api-key\nOPENROUTER_API_KEY=test-secret-value\n"
    )
    assert (profile_path / ".env").stat().st_mode & 0o777 == 0o600
    assert (profile_path / "config.yaml").read_text() == (
        "agent:\n  max_turns: 5\n"
        "model:\n  default: gpt-test\n  provider: openai-codex\n"
    )
    assert (
        runtime.start(
            profile,
            prompt=value.prompt,
            session_id="fleet:execution-1",
            timeout_seconds=1,
        )
        == "run-1"
    )
    assert runtime.wait(profile, run_id="run-1", timeout_seconds=1).text == "done"

    runtime.cleanup(profile, expected_owner="execution-1")

    assert profile_path.is_dir()
    assert sorted(path.name for path in profile_path.iterdir()) == [
        ".fleet-execution-slot"
    ]
    assert calls == [
        ("start", profile, value.prompt, "fleet:execution-1"),
        ("wait", profile, "run-1"),
    ]


@pytest.mark.parametrize(
    "reference",
    [
        "secret://worker/env/../TOKEN",
        "secret://worker/env/lowercase",
        "secret://other/env/OPENROUTER_API_KEY",
        "secret://worker/env/PATH",
    ],
)
def test_profile_runtime_rejects_unapproved_secret_reference(
    tmp_path, reference
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )
    slot = execution_slot(tmp_path)
    with pytest.raises(ValueError, match="secret reference"):
        runtime.materialize(
            package(b"not-used"), secrets={reference: "test-secret-value"}
        )
    assert sorted(path.name for path in slot.iterdir()) == [".fleet-execution-slot"]


@pytest.mark.parametrize("api_server_key", ["", "bad\nkey", "bad\x00key"])
def test_profile_runtime_rejects_invalid_profile_api_server_key(
    tmp_path, api_server_key
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    with pytest.raises(ValueError, match="API server key"):
        ProfileHermesRuntime(
            profiles_root=tmp_path / "profiles",
            runs_factory=lambda profile: Runs(profile=profile, calls=[]),
            api_server_key=api_server_key,
            model_config_path=hermes_model_config(tmp_path),
        )


def test_profile_runtime_rejects_secret_bearing_model_config_before_reclamation(
    tmp_path,
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = execution_slot(tmp_path)
    scaffold = slot / "SOUL.md"
    scaffold.write_text("gateway scaffold", encoding="utf-8")
    model_config = tmp_path / "hermes-config.yaml"
    model_config.write_text(
        "model:\n  default: gpt-test\n  provider: openai-codex\n  api_key: forbidden\n",
        encoding="utf-8",
    )
    model_config.chmod(0o600)
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=model_config,
    )

    with pytest.raises(ValueError, match="model config capability is invalid"):
        runtime.materialize(package(b"not-an-agency-archive"), secrets={})

    assert scaffold.read_text(encoding="utf-8") == "gateway scaffold"
    assert not (slot / ".materializing").exists()


def test_profile_runtime_rejects_symlinked_model_config_before_reclamation(
    tmp_path,
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = execution_slot(tmp_path)
    scaffold = slot / "SOUL.md"
    scaffold.write_text("gateway scaffold", encoding="utf-8")
    target = hermes_model_config(tmp_path)
    model_config = tmp_path / "model-link.yaml"
    model_config.symlink_to(target)
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=model_config,
    )

    with pytest.raises(ValueError, match="model config capability is unavailable"):
        runtime.materialize(package(b"not-an-agency-archive"), secrets={})

    assert scaffold.read_text(encoding="utf-8") == "gateway scaffold"
    assert not (slot / ".materializing").exists()


def test_agency_model_config_cannot_override_destination_model(tmp_path) -> None:
    from hermes_fleet.profile_runtime import _stage_model_config

    staging = tmp_path / "staging"
    staging.mkdir()
    agency_config = staging / "config.yaml"
    original = "model:\n  default: agency-model\n  provider: other\n"
    agency_config.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="cannot override destination model"):
        _stage_model_config(
            staging,
            {"model": {"default": "gpt-test", "provider": "openai-codex"}},
        )

    assert agency_config.read_text(encoding="utf-8") == original


def test_oversized_agency_config_is_rejected_unchanged(tmp_path) -> None:
    from hermes_fleet.profile_runtime import _stage_model_config

    staging = tmp_path / "staging"
    staging.mkdir()
    agency_config = staging / "config.yaml"
    original = b"#" * 65_537
    agency_config.write_bytes(original)

    with pytest.raises(ValueError, match="Agency model config is invalid"):
        _stage_model_config(
            staging,
            {"model": {"default": "gpt-test", "provider": "openai-codex"}},
        )

    assert agency_config.read_bytes() == original


def test_profile_runtime_rejects_recipe_override_of_profile_api_server_key(
    tmp_path,
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )
    slot = execution_slot(tmp_path)

    with pytest.raises(ValueError, match="reserved execution state"):
        runtime.materialize(
            package(b"not-used"),
            secrets={"secret://worker/env/API_SERVER_KEY": "recipe-override"},
        )

    assert sorted(path.name for path in slot.iterdir()) == [".fleet-execution-slot"]


def test_profile_runtime_refuses_foreign_execution_slot(tmp_path) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = tmp_path / "profiles" / "fleet-execution"
    slot.mkdir(parents=True)
    (slot / "foreign").write_text("preserve")
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(ValueError, match="not an empty owned slot"):
        runtime.materialize(package(b"not-a-tar"), secrets={})
    with pytest.raises(ValueError, match="not owned"):
        runtime.cleanup("fleet-execution", expected_owner="execution-1")

    assert (slot / "foreign").read_text() == "preserve"


@pytest.mark.parametrize("target", ["directory", "marker"])
def test_profile_runtime_rejects_permissive_owned_slot(tmp_path, target) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = tmp_path / "profiles" / "fleet-execution"
    slot.mkdir(parents=True, mode=0o700)
    marker = slot / ".fleet-execution-slot"
    marker.write_text("hermes-fleet.execution-slot.v1\n")
    marker.chmod(0o600)
    (slot if target == "directory" else marker).chmod(0o755)
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(ValueError, match="empty owned slot"):
        runtime.materialize(package(b"not-a-tar"), secrets={})


def test_profile_runtime_inspects_exact_slot_owner_and_run_without_starting(
    tmp_path: Path,
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    calls: list[tuple] = []
    slot = execution_slot(tmp_path)
    runs = Runs(profile="fleet-execution", calls=calls)
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: runs,
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )
    (slot / ".fleet-execution-owner").write_text("execution-1\n")
    profile = "fleet-execution"

    with pytest.raises(ValueError, match="empty owned slot"):
        runtime.materialize(package(b"not-used"), secrets={})
    assert runtime.owner(profile) == "execution-1"
    assert runtime.status(profile, run_id="run-1") == "running"
    assert calls == [("status", "fleet-execution", "run-1")]


def test_profile_runtime_cleanup_rejects_changed_execution_owner(tmp_path) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = execution_slot(tmp_path)
    owner = slot / ".fleet-execution-owner"
    owner.write_text("execution-2\n")
    payload = slot / "SOUL.md"
    payload.write_text("preserve exact owner state")
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(ValueError, match="ownership changed"):
        runtime.cleanup("fleet-execution", expected_owner="execution-1")

    assert owner.read_text() == "execution-2\n"
    assert payload.read_text() == "preserve exact owner state"


@pytest.mark.parametrize("stored_owner", [" execution-1\n", "execution-1 \n"])
def test_profile_runtime_cleanup_rejects_normalized_owner_substitution(
    tmp_path, stored_owner
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = execution_slot(tmp_path)
    owner = slot / ".fleet-execution-owner"
    owner.write_text(stored_owner)
    payload = slot / "SOUL.md"
    payload.write_text("preserve exact serialized owner")
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(ValueError, match="ownership changed"):
        runtime.cleanup("fleet-execution", expected_owner="execution-1")

    assert owner.read_text() == stored_owner
    assert payload.read_text() == "preserve exact serialized owner"


def test_profile_runtime_refuses_dangling_execution_owner_symlink(tmp_path) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = execution_slot(tmp_path)
    owner = slot / ".fleet-execution-owner"
    owner.symlink_to(slot / "missing-owner-target")
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(ValueError, match="empty owned slot"):
        runtime.materialize(package(b"not-used"), secrets={})

    assert owner.is_symlink()


def test_profile_runtime_preserves_scaffold_when_agency_bundle_is_invalid(
    tmp_path,
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = execution_slot(tmp_path)
    scaffold = slot / "SOUL.md"
    scaffold.write_text("gateway scaffold")
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(Exception):
        runtime.materialize(package(b"not-a-tar"), secrets={})

    assert scaffold.read_text() == "gateway scaffold"
    assert sorted(path.name for path in slot.iterdir()) == [
        ".fleet-execution-slot",
        "SOUL.md",
    ]


def test_profile_runtime_preserves_scaffold_when_file_secret_changed(
    tmp_path, monkeypatch
) -> None:
    from hermes_fleet.profile_runtime import (
        DestinationSecretResolver,
        ProfileHermesRuntime,
    )

    source = tmp_path / "auth.json"
    source.write_text("original")
    source.chmod(0o600)
    reference = "secret://worker/file/HERMES_AUTH"
    resolved = DestinationSecretResolver(
        allowed_references=(reference,),
        file_sources={reference: (source, "auth.json")},
    ).resolve(
        [reference],
        requester="peer-controller-1",
        target={"device_id": "device-1"},
        execution_id="execution-1",
    )
    source.write_text("changed")
    slot = execution_slot(tmp_path)
    scaffold = slot / "SOUL.md"
    scaffold.write_text("gateway scaffold")

    def materialize(_bundle, *, destination):
        destination.mkdir()
        (destination / "SOUL.md").write_text("exact soul")

    monkeypatch.setattr(
        "hermes_fleet.profile_runtime.materialize_agency_bundle", materialize
    )
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(ValueError, match="changed before use"):
        runtime.materialize(package(b"not-used"), secrets=resolved)

    assert scaffold.read_text() == "gateway scaffold"
    assert sorted(path.name for path in slot.iterdir()) == [
        ".fleet-execution-slot",
        "SOUL.md",
    ]


def test_profile_runtime_rejects_staged_fleet_slot_marker(
    tmp_path, monkeypatch
) -> None:
    from hermes_fleet.profile_runtime import ProfileHermesRuntime

    slot = execution_slot(tmp_path)
    scaffold = slot / "SOUL.md"
    scaffold.write_text("gateway scaffold")

    def materialize(_bundle, *, destination):
        destination.mkdir()
        (destination / ".fleet-execution-slot").write_text("forged")

    monkeypatch.setattr(
        "hermes_fleet.profile_runtime.materialize_agency_bundle", materialize
    )
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )

    with pytest.raises(ValueError, match="reserved Fleet state"):
        runtime.materialize(package(b"not-used"), secrets={})

    assert scaffold.read_text() == "gateway scaffold"
    assert (slot / ".fleet-execution-slot").read_text() == (
        "hermes-fleet.execution-slot.v1\n"
    )


def test_environment_secret_resolver_is_explicitly_allowlisted_and_never_repr_leaks(
    monkeypatch,
) -> None:
    from hermes_fleet.profile_runtime import EnvironmentSecretResolver

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-value")
    resolver = EnvironmentSecretResolver(
        allowed_references=("secret://worker/env/OPENROUTER_API_KEY",)
    )

    values = resolver.resolve(
        ["secret://worker/env/OPENROUTER_API_KEY"],
        requester="peer-controller-1",
        target={"device_id": "device-1"},
        execution_id="execution-1",
    )

    assert values == {"secret://worker/env/OPENROUTER_API_KEY": "test-secret-value"}
    assert "test-secret-value" not in repr(resolver)
    with pytest.raises(ValueError, match="not allowed"):
        resolver.resolve(
            ["secret://worker/env/OPENAI_API_KEY"],
            requester="peer-controller-1",
            target={"device_id": "device-1"},
            execution_id="execution-1",
        )


def test_local_file_secret_is_copied_minimally_and_source_is_unchanged(
    tmp_path,
) -> None:
    from hermes_fleet.agency_materialization import bundle_agency_profile
    from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
    from hermes_fleet.profile_inventory import _profile_content_digest
    from hermes_fleet.profile_runtime import (
        DestinationSecretResolver,
        ProfileHermesRuntime,
    )

    source = tmp_path / "canonical-auth.json"
    source.write_bytes(b'{"provider":"test"}\n')
    source.chmod(0o600)
    before = (source.read_bytes(), source.stat().st_mode, source.stat().st_ino)
    reference = "secret://worker/file/HERMES_AUTH"
    resolver = DestinationSecretResolver(
        allowed_references=(reference,),
        file_sources={reference: (source, "auth.json")},
    )
    resolved = resolver.resolve(
        [reference],
        requester="peer-controller-1",
        target={"device_id": "device-1"},
        execution_id="execution-1",
    )
    execution_slot(tmp_path)
    runtime = ProfileHermesRuntime(
        profiles_root=tmp_path / "profiles",
        runs_factory=lambda profile: Runs(profile=profile, calls=[]),
        api_server_key="profile-api-key",
        model_config_path=hermes_model_config(tmp_path),
    )
    agency_source = tmp_path / "agency"
    (agency_source / "skills").mkdir(parents=True)
    (agency_source / "SOUL.md").write_text("exact soul")
    (agency_source / "distribution.yaml").write_text(
        "name: acceptance\nversion: 1.0.0\n"
    )
    agency = AgencyProfilePackage(
        source=AgencySource(
            repository="https://example.invalid/agency.git", revision="a" * 40
        ),
        name="acceptance",
        version="1.0.0",
        content_digest="",
        category="test",
        priority="normal",
        capabilities=(),
        distribution_path=".",
        local_path=agency_source,
    )
    object.__setattr__(
        agency,
        "content_digest",
        _profile_content_digest(agency_source, "acceptance", "1.0.0"),
    )
    bundle = bundle_agency_profile(agency)
    value = package(bundle.payload)
    object.__setattr__(value, "agency_bundle", bundle)

    profile = runtime.materialize(value, secrets=resolved)
    copied = tmp_path / "profiles" / profile / "auth.json"

    assert copied.read_bytes() == b'{"provider":"test"}\n'
    assert copied.stat().st_mode & 0o777 == 0o600
    assert (source.read_bytes(), source.stat().st_mode, source.stat().st_ino) == before
    assert str(source) not in repr(resolver)
    assert source.read_text() not in repr(resolved[reference])

    runtime.cleanup(profile, expected_owner="execution-1")
    assert not copied.exists()
    assert source.read_bytes() == before[0]


@pytest.mark.parametrize("kind", ["symlink", "permissive", "multiple-links"])
def test_local_file_secret_rejects_unsafe_source(tmp_path, kind) -> None:
    from hermes_fleet.profile_runtime import DestinationSecretResolver

    real = tmp_path / "real-auth.json"
    real.write_text("secret")
    real.chmod(0o600)
    source = real
    if kind == "symlink":
        source = tmp_path / "auth-link.json"
        source.symlink_to(real)
    elif kind == "permissive":
        real.chmod(0o640)
    else:
        os.link(real, tmp_path / "second-link.json")
    reference = "secret://worker/file/HERMES_AUTH"
    resolver = DestinationSecretResolver(
        allowed_references=(reference,),
        file_sources={reference: (source, "auth.json")},
    )

    with pytest.raises(ValueError, match="file secret source is unsafe"):
        resolver.resolve(
            [reference],
            requester="peer-controller-1",
            target={"device_id": "device-1"},
            execution_id="execution-1",
        )


def test_file_secret_reference_requires_destination_local_mapping() -> None:
    from hermes_fleet.profile_runtime import DestinationSecretResolver

    reference = "secret://worker/file/HERMES_AUTH"
    resolver = DestinationSecretResolver(
        allowed_references=(reference,), file_sources={}
    )

    with pytest.raises(ValueError, match="not configured"):
        resolver.resolve(
            [reference],
            requester="peer-controller-1",
            target={"device_id": "device-1"},
            execution_id="execution-1",
        )


def test_local_file_secret_rejects_forged_destination_escape(tmp_path) -> None:
    from hermes_fleet.profile_runtime import DestinationSecretResolver, LocalFileSecret

    source = tmp_path / "auth.json"
    source.write_text("secret")
    source.chmod(0o600)
    reference = "secret://worker/file/HERMES_AUTH"
    resolved = DestinationSecretResolver(
        allowed_references=(reference,),
        file_sources={reference: (source, "auth.json")},
    ).resolve(
        [reference],
        requester="peer-controller-1",
        target={"device_id": "device-1"},
        execution_id="execution-1",
    )

    secret = resolved[reference]
    assert type(secret) is LocalFileSecret
    with pytest.raises(ValueError, match="file secret capability is invalid"):
        replace(secret, destination_name="../escaped-auth")
