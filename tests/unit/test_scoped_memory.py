from __future__ import annotations

from dataclasses import replace

import pytest

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.principal_identity import (
    PRINCIPAL_OWNER,
    SOURCE_LOCAL_PEER,
    PrincipalBinding,
    PrincipalDefinition,
    PrincipalRecord,
)
from hermes_fleet.scoped_memory import ScopedMemoryError, authorize_scoped_memory
from tests.unit.test_run_capsule_execution import agency_package, make_authority


def _principal_record() -> PrincipalRecord:
    definition = PrincipalDefinition(
        kind=PRINCIPAL_OWNER,
        subject="node-test:uid:1000",
        scope={
            "owner": "node-test:uid:1000",
            "network": "network-a",
        },
    )
    binding = PrincipalBinding(
        source=SOURCE_LOCAL_PEER,
        evidence={"machine_id": "node-test", "uid": 1000},
    )
    return PrincipalRecord(
        definition=definition,
        binding=binding,
        generation=1,
        state="active",
        created_at_ms=1000,
        updated_at_ms=1000,
    )


def test_scoped_memory_is_private_write_with_authorized_promoted_read_scopes(
    tmp_path,
) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    record = _principal_record()
    authority = make_authority(bundle)
    authority = replace(
        authority,
        principal=record.reference,
        project_scope=("project-a", "project-b"),
    )
    spec = authority.to_capsule_spec()

    binding = authorize_scoped_memory(spec, record).binding

    assert binding.principal_id == record.reference.principal_id
    assert binding.principal_kind == record.reference.kind
    assert binding.principal_generation == record.reference.generation
    assert binding.principal_binding_hash == record.reference.binding_hash
    assert binding.agent_instance_id == spec.agent_instance_id
    assert binding.source_run == spec.execution_id
    assert [(scope.kind, scope.scope_id) for scope in binding.read_scopes] == [
        ("principal", record.reference.principal_id),
        ("project", "project-a"),
        ("project", "project-b"),
        ("network", "network-a"),
        ("owner", "node-test:uid:1000"),
        ("agent_instance", spec.agent_instance_id),
    ]
    assert binding.write_scope.kind == "principal"
    assert binding.write_scope.scope_id == record.reference.principal_id


def test_scoped_memory_does_not_infer_project_access_outside_run_authority(
    tmp_path,
) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    definition = PrincipalDefinition(
        kind="project",
        subject="project-agent",
        scope={"project": "project-a"},
    )
    binding = PrincipalBinding(
        source=SOURCE_LOCAL_PEER,
        evidence={"machine_id": "node-test", "uid": 1000},
    )
    record = PrincipalRecord(
        definition=definition,
        binding=binding,
        generation=1,
        state="active",
        created_at_ms=1000,
        updated_at_ms=1000,
    )
    authority = replace(
        make_authority(bundle), principal=record.reference, project_scope=()
    )

    memory = authorize_scoped_memory(authority.to_capsule_spec(), record).binding

    assert all(scope.kind != "project" for scope in memory.read_scopes)


def test_scoped_memory_fails_closed_on_identity_mismatch_or_scope_overflow(
    tmp_path,
) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    record = _principal_record()
    spec = replace(make_authority(bundle), principal=record.reference).to_capsule_spec()
    other = replace(record, generation=2)

    with pytest.raises(ScopedMemoryError, match="does not match"):
        authorize_scoped_memory(spec, other)

    projects = tuple(f"project-{index}" for index in range(20))
    overflow = replace(
        make_authority(bundle),
        principal=record.reference,
        project_scope=projects,
    )
    with pytest.raises(ScopedMemoryError, match="exceed"):
        authorize_scoped_memory(overflow.to_capsule_spec(), record)
