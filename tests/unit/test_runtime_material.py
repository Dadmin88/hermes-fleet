from __future__ import annotations

from dataclasses import replace

import pytest
from hermes_secure_store import InjectionTarget, ScopeRef, VaultStore

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.principal_identity import (
    PRINCIPAL_OWNER,
    SOURCE_LOCAL_PEER,
    PrincipalBinding,
    PrincipalDefinition,
    PrincipalRecord,
)
from hermes_fleet.run_capsule import RunCapsuleStore
from hermes_fleet.runtime_material import (
    RuntimeMaterialError,
    authorize_runtime_material,
    principal_context_for_run,
    revoke_runtime_material,
)
from tests.unit.test_run_capsule_execution import agency_package, make_authority


def principal_record(
    *,
    kind: str = PRINCIPAL_OWNER,
    subject: str = "node-test:uid:1000",
    scope: dict[str, str] | None = None,
    uid: int = 1000,
) -> PrincipalRecord:
    definition = PrincipalDefinition(
        kind=kind,
        subject=subject,
        scope=scope
        or {
            "owner": "owner-alpha",
            "network": "network-alpha",
        },
    )
    binding = PrincipalBinding(
        source=SOURCE_LOCAL_PEER,
        evidence={"machine_id": "node-test", "uid": uid},
    )
    return PrincipalRecord(
        definition=definition,
        binding=binding,
        generation=1,
        state="active",
        created_at_ms=1000,
        updated_at_ms=1000,
    )


def make_spec(tmp_path, record: PrincipalRecord, **changes):
    bundle = bundle_agency_profile(agency_package(tmp_path))
    authority = replace(make_authority(bundle), principal=record.reference, **changes)
    return authority.to_capsule_spec()


def test_empty_reference_set_returns_real_empty_binding_without_store(tmp_path) -> None:
    record = principal_record()
    spec = make_spec(tmp_path, record, secret_refs=())

    binding = authorize_runtime_material(spec, record).binding

    assert binding.run_id == spec.execution_id
    assert binding.run_authority_hash == spec.run_authority_hash
    assert binding.handles == ()


def test_principal_project_network_owner_scopes_mint_only_authorized_refs(
    tmp_path,
) -> None:
    clock = [1000]
    custody = VaultStore((tmp_path / "custody").absolute(), now_ms=lambda: clock[0])
    owner = principal_record()
    base = make_spec(tmp_path, owner, project_scope=("project-alpha",), secret_refs=())
    owner_context = principal_context_for_run(base, owner)

    principal_ref = custody.put(
        "principal-value",
        owner=owner_context,
        scope=ScopeRef("principal", owner.reference.principal_id),
        injection=InjectionTarget("env", "PROVIDER_KEY"),
    )
    project_ref = custody.put(
        b"project-file",
        owner=owner_context,
        scope=ScopeRef("project", "project-alpha"),
        injection=InjectionTarget("file", "provider.pem"),
    )
    network_ref = custody.put(
        "network-broker",
        owner=owner_context,
        scope=ScopeRef("network", "network-alpha"),
        injection=InjectionTarget("broker", "provider.auth"),
    )
    owner_ref = custody.put(
        "owner-value",
        owner=owner_context,
        scope=ScopeRef("owner", "owner-alpha"),
        injection=InjectionTarget("env", "OWNER_PROVIDER_KEY"),
    )
    spec = replace(
        base,
        secret_refs=(principal_ref, project_ref, network_ref, owner_ref),
    )

    binding = authorize_runtime_material(spec, owner, store=custody).binding

    assert len(binding.handles) == 4
    assert {
        (item.injection_kind, item.injection_target) for item in binding.handles
    } == {
        ("env", "PROVIDER_KEY"),
        ("file", "provider.pem"),
        ("broker", "provider.auth"),
        ("env", "OWNER_PROVIDER_KEY"),
    }
    assert all(item.expires_at_ms == spec.deadline_ms for item in binding.handles)


def test_project_scope_must_be_in_exact_run_not_only_principal_definition(
    tmp_path,
) -> None:
    clock = [1000]
    custody = VaultStore((tmp_path / "custody").absolute(), now_ms=lambda: clock[0])
    owner = principal_record(scope={"owner": "owner-alpha"})
    owner_spec = make_spec(
        tmp_path, owner, project_scope=("project-alpha",), secret_refs=()
    )
    reference = custody.put(
        "project-value",
        owner=principal_context_for_run(owner_spec, owner),
        scope=ScopeRef("project", "project-alpha"),
        injection=InjectionTarget("env", "PROJECT_KEY"),
    )

    member = principal_record(
        kind="project",
        subject="project-member",
        scope={"project": "project-alpha"},
        uid=1001,
    )
    missing_scope = replace(
        owner_spec,
        principal=member.reference,
        project_scope=(),
        secret_refs=(reference,),
    )
    with pytest.raises(RuntimeMaterialError, match="not authorized"):
        authorize_runtime_material(missing_scope, member, store=custody)

    exact_scope = replace(missing_scope, project_scope=("project-alpha",))
    binding = authorize_runtime_material(exact_scope, member, store=custody).binding
    assert len(binding.handles) == 1


def test_principal_scoped_reference_cannot_cross_principal_boundary(tmp_path) -> None:
    clock = [1000]
    custody = VaultStore((tmp_path / "custody").absolute(), now_ms=lambda: clock[0])
    owner = principal_record()
    owner_spec = make_spec(tmp_path, owner, secret_refs=())
    reference = custody.put(
        "principal-private-value",
        owner=principal_context_for_run(owner_spec, owner),
        scope=ScopeRef("principal", owner.reference.principal_id),
        injection=InjectionTarget("env", "PRIVATE_PROVIDER_KEY"),
    )

    other = principal_record(
        subject="node-test:uid:1001",
        scope={"owner": "owner-alpha", "network": "network-alpha"},
        uid=1001,
    )
    other_spec = replace(
        owner_spec,
        principal=other.reference,
        secret_refs=(reference,),
    )

    with pytest.raises(RuntimeMaterialError, match="not authorized"):
        authorize_runtime_material(other_spec, other, store=custody)


def test_rotation_expiry_and_revoke_are_enforced_by_real_store(tmp_path) -> None:
    clock = [1000]
    custody = VaultStore((tmp_path / "custody").absolute(), now_ms=lambda: clock[0])
    owner = principal_record()
    base = make_spec(tmp_path, owner, secret_refs=())
    context = principal_context_for_run(base, owner)
    reference = custody.put(
        "old-value",
        owner=context,
        scope=ScopeRef("principal", owner.reference.principal_id),
        injection=InjectionTarget("env", "PROVIDER_KEY"),
    )
    spec = replace(base, secret_refs=(reference,))
    old = authorize_runtime_material(spec, owner, store=custody).binding.handles[0]
    assert old.version == 1

    assert custody.rotate(reference, "new-value", owner=context) == 2
    newer_spec = replace(spec, execution_id="capsule-execution-2")
    new = authorize_runtime_material(newer_spec, owner, store=custody).binding.handles[
        0
    ]
    assert new.version == 2

    custody.revoke(reference, owner=context, version=2)
    with pytest.raises(RuntimeMaterialError, match="not authorized"):
        authorize_runtime_material(
            replace(spec, execution_id="capsule-execution-3"),
            owner,
            store=custody,
        )

    reference = custody.put(
        "expiring-value",
        owner=context,
        scope=ScopeRef("principal", owner.reference.principal_id),
        injection=InjectionTarget("env", "EXPIRING_KEY"),
        expires_at_ms=1500,
    )
    expiring = replace(
        spec,
        execution_id="capsule-execution-expiring",
        secret_refs=(reference,),
    )
    assert authorize_runtime_material(expiring, owner, store=custody).binding.handles
    clock[0] = 1500
    with pytest.raises(RuntimeMaterialError, match="not authorized"):
        authorize_runtime_material(expiring, owner, store=custody)


def test_temporary_handles_never_enter_durable_run_capsule_state(tmp_path) -> None:
    clock = [1000]
    custody = VaultStore((tmp_path / "custody").absolute(), now_ms=lambda: clock[0])
    owner = principal_record()
    base = make_spec(tmp_path, owner, secret_refs=())
    body = "body-must-not-enter-fleet-state"
    reference = custody.put(
        body,
        owner=principal_context_for_run(base, owner),
        scope=ScopeRef("principal", owner.reference.principal_id),
        injection=InjectionTarget("env", "PROVIDER_KEY"),
    )
    spec = replace(base, secret_refs=(reference,))
    binding = authorize_runtime_material(spec, owner, store=custody).binding
    handle = binding.handles[0].handle

    state_path = tmp_path / "capsules.sqlite"
    run_store = RunCapsuleStore(state_path, now_ms=lambda: 1000)
    record, created = run_store.admit(spec)
    assert created is True
    assert record.spec.secret_refs == (reference,)
    import sqlite3

    with sqlite3.connect(state_path) as connection:
        persisted = connection.execute(
            "SELECT spec_json FROM run_capsules WHERE execution_id = ?",
            (spec.execution_id,),
        ).fetchone()
    assert persisted is not None
    durable_text = persisted[0]
    assert reference in durable_text
    assert handle not in durable_text
    assert body not in durable_text

    assert revoke_runtime_material(spec, store=custody) == 1
    assert handle not in repr(custody.audit_records(limit=50))
    assert body not in repr(custody.audit_records(limit=50))
