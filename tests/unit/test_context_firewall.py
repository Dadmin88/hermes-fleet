from __future__ import annotations

from dataclasses import replace

import pytest

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.context_firewall import (
    ContextFirewallError,
    authorize_context_firewall,
)
from hermes_fleet.hermes_runs import (
    HermesFleetContextBinding,
    HermesFleetMemoryBinding,
    HermesFleetRuntimeBinding,
    HermesMemoryScopeRef,
    HermesRunError,
    HermesRunsClient,
)
from hermes_fleet.principal_identity import PrincipalRecord
from tests.unit.test_hermes_runs import _RunsAPI
from tests.unit.test_run_capsule_execution import (
    PRINCIPAL_BINDING,
    PRINCIPAL_DEFINITION,
    agency_package,
    instances,
    make_authority,
)


def principal_record() -> PrincipalRecord:
    return PrincipalRecord(
        definition=PRINCIPAL_DEFINITION,
        binding=PRINCIPAL_BINDING,
        generation=1,
        state="active",
        created_at_ms=1000,
        updated_at_ms=1000,
    )


def test_context_authorization_binds_principal_agent_manifest_and_authority(
    tmp_path,
) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    authority = make_authority(bundle)
    spec = authority.to_capsule_spec()
    agent = instances(tmp_path).ensure(bundle)

    binding = authorize_context_firewall(spec, principal_record(), agent).binding

    assert binding.principal_id == spec.principal.principal_id
    assert binding.agent_instance_id == spec.agent_instance_id
    assert binding.base_manifest_digest == agent.base_manifest_digest
    assert binding.run_authority_hash == spec.run_authority_hash
    assert binding.to_request()["version"] == "fleet-context-v1"


def test_context_authorization_rejects_identity_mismatch(tmp_path) -> None:
    bundle = bundle_agency_profile(agency_package(tmp_path))
    authority = make_authority(bundle)
    spec = authority.to_capsule_spec()
    agent = instances(tmp_path).ensure(bundle)
    stale = replace(principal_record(), generation=2)

    with pytest.raises(ContextFirewallError, match="principal record"):
        authorize_context_firewall(spec, stale, agent)


def test_runs_client_requires_context_capability_and_exact_identity() -> None:
    principal = "sha256:" + "1" * 64
    private = HermesMemoryScopeRef("principal", principal)
    memory = HermesFleetMemoryBinding(
        principal_id=principal,
        principal_kind="owner",
        principal_generation=1,
        principal_binding_hash="sha256:" + "2" * 64,
        agent_instance_id="sha256:" + "3" * 64,
        source_run="run-context",
        read_scopes=(private,),
        write_scope=private,
    )
    context = HermesFleetContextBinding(
        principal_id=principal,
        principal_kind="owner",
        principal_generation=1,
        principal_binding_hash="sha256:" + "2" * 64,
        agent_instance_id="sha256:" + "3" * 64,
        base_manifest_digest="sha256:" + "4" * 64,
        run_authority_hash="sha256:" + "5" * 64,
    )
    runtime = HermesFleetRuntimeBinding(
        container_id="a" * 64,
        plan_fingerprint="sha256:" + "b" * 64,
        image="debian@sha256:" + "c" * 64,
        max_iterations=8,
    )

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_fleet_context_firewall = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="test")
        with pytest.raises(HermesRunError, match="run_fleet_context_firewall"):
            client.start(
                prompt="blocked",
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=context,
            )

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_sensitive_interception = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="test")
        with pytest.raises(HermesRunError, match="run_sensitive_interception"):
            client.start(
                prompt="blocked",
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=context,
            )

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        run_id = HermesRunsClient(endpoint=endpoint, api_key="test").start(
            prompt="allowed",
            fleet_runtime=runtime,
            fleet_memory=memory,
            fleet_context=context,
        )
    assert run_id == "run-test"
    assert api.requests[-1][3]["fleet_context"] == context.to_request()

    mismatch = replace(context, principal_id="sha256:" + "9" * 64)
    api = _RunsAPI([])
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="test")
        with pytest.raises(ValueError, match="does not match memory"):
            client.start(
                prompt="blocked",
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=mismatch,
            )
