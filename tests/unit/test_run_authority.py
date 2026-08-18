from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from hermes_fleet.host_action_broker import HostActionGrant
from hermes_fleet.network_isolation import NETWORK_NONE
from hermes_fleet.principal_identity import PrincipalReference
from hermes_fleet.run_authority import (
    FilesystemAuthorityIntent,
    IsolationAuthority,
    ModelProviderAuthority,
    NetworkAuthorityIntent,
    RecipeAuthorityBinding,
    ResourceAuthority,
    RunAuthority,
    RunAuthorityConflict,
    RunAuthorityError,
    RunAuthorityInactive,
    RunAuthoritySigner,
    RunAuthorityStale,
    RunAuthorityStore,
    authority_from_dict,
)
from hermes_fleet.workspace_isolation import ArtifactExportGrant

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64
HASH_5 = "sha256:" + "5" * 64
HASH_6 = "sha256:" + "6" * 64
HASH_7 = "sha256:" + "7" * 64
HASH_8 = "sha256:" + "8" * 64
IMAGE = "example.invalid/workshop@sha256:" + "a" * 64
TARGET = {"source": "local", "node_id": "node-a", "generation": 3}
TARGET_DIGEST = (
    "sha256:"
    + __import__("hashlib")
    .sha256(json.dumps(TARGET, sort_keys=True, separators=(",", ":")).encode())
    .hexdigest()
)
PRINCIPAL = PrincipalReference(
    principal_id=HASH_1,
    kind="owner",
    generation=2,
    binding_hash=HASH_2,
)


def authority(**changes) -> RunAuthority:
    values = {
        "execution_id": "run-auth-1",
        "idempotency_digest": HASH_3,
        "principal": PRINCIPAL,
        "agent_instance_id": HASH_4,
        "recipe": RecipeAuthorityBinding(
            recipe_hash=HASH_5,
            resolved_recipe_hash=HASH_6,
            compiler_version="fleet.workflow-recipe-compiler.v1",
            provenance_digest=HASH_7,
            image=IMAGE,
            workflow_id="workflow-a",
            workflow_revision=4,
            workflow_hash=HASH_8,
            workflow_step_id="build",
        ),
        "policy_digest": "sha256:" + "9" * 64,
        "capabilities_hash": "sha256:" + "b" * 64,
        "target": TARGET,
        "target_digest": TARGET_DIGEST,
        "plan_fingerprint": "sha256:" + "c" * 64,
        "issued_at_ms": 900,
        "deadline_ms": 2_000,
        "resources": ResourceAuthority(
            cpu_millis=1000,
            memory_bytes=268_435_456,
            pids_limit=64,
            max_iterations=8,
        ),
        "isolation": IsolationAuthority(),
        "network": NetworkAuthorityIntent(mode=NETWORK_NONE),
        "filesystem": (
            FilesystemAuthorityIntent(
                project_id="project-a",
                relative_path="src",
                target="/workspace/inputs/project-a",
                max_bytes=1_048_576,
            ),
        ),
        "artifacts": (
            ArtifactExportGrant(
                name="build",
                path="/workspace/out/build.tar",
                max_bytes=2_097_152,
                scan_required=True,
            ),
        ),
        "toolsets": ("fleet-terminal",),
        "approval_budget": 2,
        "secret_refs": ("vault-ref-a",),
        "host_grants": (
            HostActionGrant(
                verb="query-approved-health",
                target="service-a",
                parameters_digest=HASH_2,
                max_calls=3,
                rate_limit_per_minute=2,
            ),
        ),
        "model_provider": ModelProviderAuthority(
            providers=("provider-a", "provider-b"),
            models=("model-a", "model-b"),
        ),
        "project_scope": ("project-a",),
    }
    values.update(changes)
    return RunAuthority(**values)


def store(tmp_path: Path, *, now: int = 1000) -> RunAuthorityStore:
    return RunAuthorityStore(tmp_path / "authority.sqlite", now_ms=lambda: now)


def test_authority_round_trip_is_canonical_and_domain_separates_audit_hash() -> None:
    original = authority()
    restored = authority_from_dict(original.to_dict())
    assert restored == original
    assert restored.content_hash == original.content_hash
    assert restored.audit_hash == original.audit_hash
    assert restored.audit_hash != restored.content_hash
    assert json.loads(json.dumps(restored.to_dict(), sort_keys=True))["schema"] == (
        "fleet.run-authority.v1"
    )


def test_authority_derives_all_bound_grants_and_exact_capsule() -> None:
    item = authority()
    network = item.network_grant()
    filesystem = item.filesystem_grants()
    capsule = item.to_capsule_spec()

    assert network.authority_ref == item.content_hash
    assert item.network_scope().permits(network) is True
    assert len(filesystem) == 1
    assert filesystem[0].authority_ref == item.content_hash
    assert item.filesystem_scope().permits(filesystem[0]) is True
    assert item.host_scope().run_authority_hash == item.content_hash
    assert item.host_scope().grants == item.host_grants
    assert capsule.run_authority_hash == item.content_hash
    assert capsule.principal == item.principal
    assert capsule.network_grant == network
    assert capsule.filesystem_grants == filesystem
    assert capsule.host_broker_grants == item.host_grants
    assert capsule.approval_budget == item.approval_budget
    assert capsule.secret_refs == item.secret_refs
    assert capsule.cpu_millis == item.resources.cpu_millis
    assert capsule.plan_fingerprint == item.plan_fingerprint
    item.validate_capsule(capsule)

    with pytest.raises(RunAuthorityConflict, match="exact projection"):
        item.validate_capsule(replace(capsule, approval_budget=1))


def test_symmetric_attestation_binds_exact_authority_hash() -> None:
    signer = RunAuthoritySigner(key_id="fleet-key-1", key=b"k" * 32)
    item = authority()
    attestation = signer.sign(item)
    assert signer.verify(item, attestation) is True
    assert signer.verify(replace(item, deadline_ms=1_900), attestation) is False
    assert (
        RunAuthoritySigner(key_id="fleet-key-1", key=b"x" * 32).verify(
            item, attestation
        )
        is False
    )


def test_store_replay_conflict_claim_cancel_revoke_and_restart(tmp_path: Path) -> None:
    registry = store(tmp_path)
    item = authority()
    first, created = registry.admit(item)
    assert created is True
    assert first.state == "active"
    again, created = registry.admit(item)
    assert created is False
    assert again == first

    with pytest.raises(RunAuthorityConflict, match="idempotency"):
        registry.admit(
            replace(
                item,
                execution_id="run-auth-other",
                plan_fingerprint="sha256:" + "d" * 64,
            )
        )

    capsule = item.to_capsule_spec()
    claimed = registry.claim_capsule(item.content_hash, capsule)
    assert claimed.claimed_capsule_hash is not None
    assert registry.claim_capsule(item.content_hash, capsule).claimed_capsule_hash == (
        claimed.claimed_capsule_hash
    )
    with pytest.raises(RunAuthorityConflict, match="exact projection"):
        registry.claim_capsule(
            item.content_hash,
            replace(capsule, approval_budget=1),
        )

    assert registry.effect_active(item.content_hash) is True
    cancelled = registry.cancel(item.content_hash)
    assert cancelled.state == "cancelled"
    assert registry.effect_active(item.content_hash) is False
    assert cancelled.state_generation == 2
    with pytest.raises(RunAuthorityInactive, match="cancelled"):
        registry.require_active(
            item.content_hash,
            policy_digest=item.policy_digest,
            capabilities_hash=item.capabilities_hash,
            target_digest=item.target_digest,
        )
    revoked = registry.revoke(item.content_hash)
    assert revoked.state == "revoked"
    assert revoked.state_generation == 3

    reopened = RunAuthorityStore(registry.path, now_ms=lambda: 1000)
    assert reopened.get(item.content_hash) == revoked


def test_active_validation_rejects_expiry_and_stale_context(tmp_path: Path) -> None:
    item = authority()
    registry = store(tmp_path)
    registry.admit(item)
    registry.require_active(
        item.content_hash,
        policy_digest=item.policy_digest,
        capabilities_hash=item.capabilities_hash,
        target_digest=item.target_digest,
    )
    for field, value, message in (
        ("policy_digest", HASH_1, "policy"),
        ("capabilities_hash", HASH_2, "capabilities"),
        ("target_digest", HASH_3, "target"),
    ):
        kwargs = {
            "policy_digest": item.policy_digest,
            "capabilities_hash": item.capabilities_hash,
            "target_digest": item.target_digest,
        }
        kwargs[field] = value
        with pytest.raises(RunAuthorityStale, match=message):
            registry.require_active(item.content_hash, **kwargs)

    expired = RunAuthorityStore(registry.path, now_ms=lambda: 2_001)
    with pytest.raises(RunAuthorityInactive, match="expired"):
        expired.require_active(
            item.content_hash,
            policy_digest=item.policy_digest,
            capabilities_hash=item.capabilities_hash,
            target_digest=item.target_digest,
        )


def test_validate_context_binds_principal_agent_recipe_and_destination() -> None:
    item = authority()
    item.validate_context(
        principal=item.principal,
        agent_instance_id=item.agent_instance_id,
        recipe_hash=item.recipe.recipe_hash,
        resolved_recipe_hash=item.recipe.resolved_recipe_hash,
        policy_digest=item.policy_digest,
        capabilities_hash=item.capabilities_hash,
        target_digest=item.target_digest,
        now_ms=1000,
        provider="provider-a",
        model="model-a",
    )
    with pytest.raises(RunAuthorityStale, match="principal"):
        item.validate_context(
            principal=replace(item.principal, generation=3),
            agent_instance_id=item.agent_instance_id,
            recipe_hash=item.recipe.recipe_hash,
            resolved_recipe_hash=item.recipe.resolved_recipe_hash,
            policy_digest=item.policy_digest,
            capabilities_hash=item.capabilities_hash,
            target_digest=item.target_digest,
            now_ms=1000,
        )


def test_monotonic_narrowing_can_only_reduce_power() -> None:
    parent = authority()
    child = parent.narrow(
        plan_fingerprint="sha256:" + "d" * 64,
        deadline_ms=1_800,
        resources=ResourceAuthority(
            cpu_millis=500,
            memory_bytes=134_217_728,
            pids_limit=32,
            max_iterations=4,
        ),
        network=NetworkAuthorityIntent(mode=NETWORK_NONE),
        filesystem=(),
        artifacts=(),
        toolsets=(),
        approval_budget=1,
        secret_refs=(),
        host_grants=(),
        model_provider=ModelProviderAuthority(
            providers=("provider-a",), models=("model-a",)
        ),
        project_scope=(),
    )
    assert child.parent_authority_hash == parent.content_hash
    assert child.content_hash != parent.content_hash
    assert child.approval_budget == 1

    with pytest.raises(RunAuthorityError, match="resources"):
        parent.narrow(
            plan_fingerprint="sha256:" + "e" * 64,
            resources=replace(parent.resources, cpu_millis=2_000),
        )
    with pytest.raises(RunAuthorityError, match="toolsets"):
        parent.narrow(
            plan_fingerprint="sha256:" + "e" * 64,
            toolsets=("fleet-terminal", "other-toolset"),
        )
    with pytest.raises(RunAuthorityError, match="providers"):
        parent.narrow(
            plan_fingerprint="sha256:" + "e" * 64,
            model_provider=ModelProviderAuthority(providers=(), models=("model-a",)),
        )


def test_persisted_authority_tampering_fails_closed(tmp_path: Path) -> None:
    registry = store(tmp_path)
    item = authority()
    registry.admit(item)
    with sqlite3.connect(registry.path) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT authority_json FROM run_authorities WHERE authority_hash = ?",
                (item.content_hash,),
            ).fetchone()[0]
        )
        payload["approval_budget"] = 31
        connection.execute(
            "UPDATE run_authorities SET authority_json = ? WHERE authority_hash = ?",
            (json.dumps(payload, sort_keys=True), item.content_hash),
        )
    with pytest.raises(RunAuthorityError, match="identity changed"):
        registry.get(item.content_hash)


def test_exact_deadline_boundary_is_expired(tmp_path: Path) -> None:
    item = authority()
    path = tmp_path / "boundary.sqlite"
    RunAuthorityStore(path, now_ms=lambda: 1000).admit(item)
    registry = RunAuthorityStore(path, now_ms=lambda: item.deadline_ms)
    with pytest.raises(RunAuthorityInactive, match="expired"):
        registry.require_active(
            item.content_hash,
            policy_digest=item.policy_digest,
            capabilities_hash=item.capabilities_hash,
            target_digest=item.target_digest,
        )
    with pytest.raises(RunAuthorityInactive, match="expired"):
        item.validate_context(
            principal=item.principal,
            agent_instance_id=item.agent_instance_id,
            recipe_hash=item.recipe.recipe_hash,
            resolved_recipe_hash=item.recipe.resolved_recipe_hash,
            policy_digest=item.policy_digest,
            capabilities_hash=item.capabilities_hash,
            target_digest=item.target_digest,
            now_ms=item.deadline_ms,
            provider="provider-a",
            model="model-a",
        )


def test_model_provider_constraints_are_enforced() -> None:
    item = authority()
    kwargs = {
        "principal": item.principal,
        "agent_instance_id": item.agent_instance_id,
        "recipe_hash": item.recipe.recipe_hash,
        "resolved_recipe_hash": item.recipe.resolved_recipe_hash,
        "policy_digest": item.policy_digest,
        "capabilities_hash": item.capabilities_hash,
        "target_digest": item.target_digest,
        "now_ms": 1000,
    }
    item.validate_context(**kwargs, provider="provider-a", model="model-a")
    with pytest.raises(RunAuthorityStale, match="provider"):
        item.validate_context(**kwargs, provider="provider-z", model="model-a")
    with pytest.raises(RunAuthorityStale, match="model"):
        item.validate_context(**kwargs, provider="provider-a", model="model-z")
    with pytest.raises(RunAuthorityStale, match="provider"):
        item.validate_context(**kwargs, provider=None, model="model-a")


def test_decoder_rejects_unknown_nested_fields_and_unpinned_image() -> None:
    document = authority().to_dict()
    document["network"]["unexpected"] = True
    with pytest.raises(RunAuthorityError, match="closed schema"):
        authority_from_dict(document)

    document = authority().to_dict()
    document["recipe"]["image"] = "example.invalid/workshop:latest"
    with pytest.raises(RunAuthorityError, match="digest-pinned"):
        authority_from_dict(document)


def test_concurrent_exact_admission_and_capsule_claim_converge(tmp_path: Path) -> None:
    registry = RunAuthorityStore(tmp_path / "concurrent.sqlite", now_ms=lambda: 1000)
    item = authority()
    created: list[bool] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def admit_worker() -> None:
        try:
            barrier.wait(timeout=5)
            _record, was_created = registry.admit(item)
            created.append(was_created)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=admit_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert created.count(True) == 1
    assert created.count(False) == 7

    capsule = item.to_capsule_spec()
    claims: list[str | None] = []
    barrier = threading.Barrier(8)

    def claim_worker() -> None:
        try:
            barrier.wait(timeout=5)
            claims.append(
                registry.claim_capsule(item.content_hash, capsule).claimed_capsule_hash
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=claim_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    assert len(set(claims)) == 1
    assert claims[0] is not None


def test_store_rejects_not_yet_valid_authority(tmp_path: Path) -> None:
    item = authority(issued_at_ms=1_500, deadline_ms=2_000)
    registry = RunAuthorityStore(tmp_path / "future.sqlite", now_ms=lambda: 1_000)
    with pytest.raises(RunAuthorityInactive, match="not yet valid"):
        registry.admit(item)


def test_store_rejects_symlinked_parent_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(RunAuthorityError, match="unsafe"):
        RunAuthorityStore(alias / "authority.sqlite", now_ms=lambda: 1000)


def test_store_schema_tampering_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "schema.sqlite"
    RunAuthorityStore(path, now_ms=lambda: 1000)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE run_authorities ADD COLUMN injected TEXT")
    with pytest.raises(RunAuthorityError, match="schema is not ready"):
        RunAuthorityStore(path, now_ms=lambda: 1000)


def test_store_activates_narrowed_child_and_preserves_superseded_parent(
    tmp_path: Path,
) -> None:
    registry = RunAuthorityStore(tmp_path / "narrow.sqlite", now_ms=lambda: 1000)
    parent = authority()
    registry.admit(parent)
    child = parent.narrow(
        plan_fingerprint="sha256:" + "d" * 64,
        deadline_ms=1_800,
        resources=ResourceAuthority(
            cpu_millis=500,
            memory_bytes=134_217_728,
            pids_limit=32,
            max_iterations=4,
        ),
        filesystem=(),
        artifacts=(),
        toolsets=("fleet-terminal",),
        approval_budget=1,
        secret_refs=(),
        host_grants=(),
        model_provider=ModelProviderAuthority(
            providers=("provider-a",), models=("model-a",)
        ),
        project_scope=(),
    )

    with pytest.raises(RunAuthorityConflict, match="idempotency"):
        registry.admit(child)

    active_child, created = registry.narrow(parent.content_hash, child)
    assert created is True
    assert active_child.authority == child
    assert active_child.state == "active"
    superseded = registry.get(parent.content_hash)
    assert superseded is not None
    assert superseded.state == "superseded"
    assert superseded.authority == parent
    assert registry.effect_active(parent.content_hash) is False
    assert registry.effect_active(child.content_hash) is True

    again, created = registry.narrow(parent.content_hash, child)
    assert created is False
    assert again == active_child

    grandchild = child.narrow(
        plan_fingerprint="sha256:" + "e" * 64,
        approval_budget=0,
    )
    active_grandchild, created = registry.narrow(child.content_hash, grandchild)
    assert created is True
    assert active_grandchild.state == "active"
    assert registry.get(child.content_hash).state == "superseded"
    assert registry.effect_active(child.content_hash) is False
    assert registry.effect_active(grandchild.content_hash) is True

    capsule = grandchild.to_capsule_spec()
    registry.claim_capsule(grandchild.content_hash, capsule)
    great_grandchild = grandchild.narrow(
        plan_fingerprint="sha256:" + "f" * 64,
    )
    with pytest.raises(RunAuthorityConflict, match="claimed"):
        registry.narrow(grandchild.content_hash, great_grandchild)


def test_principal_revocation_propagates_through_authority_store_effect_check(
    tmp_path: Path,
) -> None:
    current = {"value": True}
    registry = RunAuthorityStore(
        tmp_path / "principal-aware.sqlite",
        now_ms=lambda: 1000,
        principal_state_check=lambda _principal: current["value"],
    )
    item = authority()
    registry.admit(item)
    assert registry.effect_active(item.content_hash) is True

    current["value"] = False
    assert registry.effect_active(item.content_hash) is False
    with pytest.raises(RunAuthorityStale, match="principal"):
        registry.require_active(
            item.content_hash,
            policy_digest=item.policy_digest,
            capabilities_hash=item.capabilities_hash,
            target_digest=item.target_digest,
        )
    with pytest.raises(RunAuthorityStale, match="principal"):
        registry.claim_capsule(item.content_hash, item.to_capsule_spec())
