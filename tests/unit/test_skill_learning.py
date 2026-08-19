from __future__ import annotations

import hashlib
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
from hermes_fleet.skill_learning import SkillLearningError, authorize_skill_learning
from hermes_fleet.workspace_isolation import FilesystemGrant
from tests.unit.test_run_capsule_execution import agency_package, make_authority


def principal_record() -> PrincipalRecord:
    definition = PrincipalDefinition(
        kind=PRINCIPAL_OWNER,
        subject="node-test:uid:1000",
        scope={"owner": "node-test:uid:1000", "network": "network-a"},
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


def test_skill_learning_derives_private_non_authoritative_need_envelope(
    tmp_path,
) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    record = principal_record()
    reference = "secret://worker/provider-key"
    authority = replace(
        make_authority(bundle, secret_refs=(reference,)),
        principal=record.reference,
        project_scope=("project-a",),
    )
    spec = authority.to_capsule_spec()
    spec = replace(
        spec,
        filesystem_grants=(
            FilesystemGrant(
                project_id="project-a",
                relative_path="src",
                target="/workspace/inputs/src",
                mode="read",
                max_bytes=4096,
                authority_ref=spec.run_authority_hash,
            ),
        ),
    )

    binding = authorize_skill_learning(spec, record).binding

    assert binding.scope_kind == "principal"
    assert binding.scope_id == record.reference.principal_id
    assert binding.principal_id == record.reference.principal_id
    assert binding.agent_instance_id == spec.agent_instance_id
    assert binding.source_run == spec.execution_id
    assert binding.run_authority_hash == spec.run_authority_hash
    assert binding.recipe_hash == spec.recipe_hash
    assert binding.resolved_recipe_hash == spec.resolved_recipe_hash
    assert binding.plan_fingerprint == spec.plan_fingerprint
    assert binding.capabilities_hash == spec.capabilities_hash
    assert binding.target_digest == spec.target_digest
    assert binding.toolsets == spec.toolsets
    assert [item.to_request() for item in binding.filesystem_needs] == [
        {
            "project_id": "project-a",
            "relative_path": "src",
            "target": "/workspace/inputs/src",
            "mode": "read-only",
            "max_bytes": 4096,
        }
    ]
    assert binding.network_mode == spec.network_mode
    assert binding.network_policy_hash == spec.network_policy_hash
    expected = "sha256:" + hashlib.sha256(reference.encode()).hexdigest()
    assert binding.secret_need_fingerprints == (expected,)
    assert reference not in repr(binding.to_request())


def test_skill_learning_fails_closed_on_principal_identity_mismatch(tmp_path) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    record = principal_record()
    authority = replace(make_authority(bundle), principal=record.reference)
    spec = authority.to_capsule_spec()

    with pytest.raises(SkillLearningError, match="does not match"):
        authorize_skill_learning(spec, replace(record, generation=2))


def test_skill_learning_wire_shape_carries_metadata_not_future_permissions(
    tmp_path,
) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    record = principal_record()
    authority = replace(make_authority(bundle), principal=record.reference)
    spec = authority.to_capsule_spec()

    payload = authorize_skill_learning(spec, record).binding.to_request()

    assert payload["version"] == "fleet-skill-learning-v1"
    assert payload["scope"] == {
        "kind": "principal",
        "scope_id": record.reference.principal_id,
    }
    assert set(payload) == {
        "version",
        "principal",
        "agent_instance_id",
        "source_run",
        "scope",
        "run_authority_hash",
        "provenance",
        "needs",
    }
    assert "approval_budget" not in repr(payload)
    assert "host_grants" not in repr(payload)
    assert "write_authority_ref" not in repr(payload)
