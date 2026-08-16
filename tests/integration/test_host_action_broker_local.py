from __future__ import annotations

import os
from pathlib import Path

from hermes_fleet.host_action_broker import (
    DEPLOY_APPROVED_ARTIFACT,
    QUERY_APPROVED_HEALTH,
    HostActionAdapterSpec,
    HostActionAuthorityScope,
    HostActionBroker,
    HostActionGrant,
    HostActionRequest,
    canonical_digest,
)

AUTHORITY = "sha256:" + "1" * 64
RECIPE = "sha256:" + "2" * 64
POLICY = "sha256:" + "3" * 64
ARTIFACT_DIGEST = "sha256:" + "4" * 64
PRINCIPAL = "principal-local"
EXECUTION = "execution-local"
DESTINATION = {
    "source": "local",
    "device_id": "device-local",
    "generation": 1,
}


def test_real_local_host_effect_stays_behind_logical_broker_target(
    tmp_path: Path,
) -> None:
    deployment_root = tmp_path / "approved-deployment"
    deployment_root.mkdir()
    deployed = deployment_root / "current.bin"
    artifact_store = {
        "artifact-1": (ARTIFACT_DIGEST, b"release-one\n"),
    }
    generation = {"value": 0}

    deploy_parameters = {
        "artifact_id": "artifact-1",
        "artifact_digest": ARTIFACT_DIGEST,
        "release_id": "release-1",
    }
    health_parameters = {"probe_id": "ready"}

    def deploy(values):
        artifact_id = values["artifact_id"]
        expected_digest = values["artifact_digest"]
        actual_digest, payload = artifact_store[artifact_id]
        if expected_digest != actual_digest:
            raise RuntimeError("artifact digest changed")
        candidate = deployment_root / "candidate.bin"
        candidate.write_bytes(payload)
        os.replace(candidate, deployed)
        generation["value"] += 1
        return {
            "changed": True,
            "artifact_id": artifact_id,
            "generation": generation["value"],
        }

    def health(values):
        return {
            "healthy": deployed.exists() and deployed.read_bytes() == b"release-one\n",
            "probe_id": values["probe_id"],
            "generation": generation["value"],
        }

    deploy_grant = HostActionGrant(
        verb=DEPLOY_APPROVED_ARTIFACT,
        target="app-release",
        parameters_digest=canonical_digest(deploy_parameters),
        max_calls=1,
        rate_limit_per_minute=1,
    )
    health_grant = HostActionGrant(
        verb=QUERY_APPROVED_HEALTH,
        target="app-health",
        parameters_digest=canonical_digest(health_parameters),
        max_calls=2,
        rate_limit_per_minute=2,
    )
    scope = HostActionAuthorityScope(
        principal_id=PRINCIPAL,
        execution_id=EXECUTION,
        run_authority_hash=AUTHORITY,
        resolved_recipe_hash=RECIPE,
        policy_digest=POLICY,
        target_digest=canonical_digest(DESTINATION),
        deadline_ms=10_000,
        grants=(deploy_grant, health_grant),
    )
    broker = HostActionBroker(
        adapters=(
            HostActionAdapterSpec(
                verb=DEPLOY_APPROVED_ARTIFACT,
                target="app-release",
                handler=deploy,
                required_parameters=("artifact_id", "artifact_digest", "release_id"),
            ),
            HostActionAdapterSpec(
                verb=QUERY_APPROVED_HEALTH,
                target="app-health",
                handler=health,
                required_parameters=("probe_id",),
            ),
        ),
        node_policy=lambda _scope, _request: True,
        now_ms=lambda: 1_000,
    )

    deploy_request = HostActionRequest(
        principal_id=PRINCIPAL,
        execution_id=EXECUTION,
        run_authority_hash=AUTHORITY,
        resolved_recipe_hash=RECIPE,
        verb=DEPLOY_APPROVED_ARTIFACT,
        target="app-release",
        parameters=deploy_parameters,
        idempotency_key="deploy-once",
        deadline_ms=9_000,
    )
    deploy_evidence = broker.invoke(
        authority=scope,
        request=deploy_request,
        current_policy_digest=POLICY,
        current_resolved_recipe_hash=RECIPE,
        current_target=DESTINATION,
    )
    assert deploy_evidence.status == "succeeded"
    assert deployed.read_bytes() == b"release-one\n"
    assert not (deployment_root / "candidate.bin").exists()

    health_request = HostActionRequest(
        principal_id=PRINCIPAL,
        execution_id=EXECUTION,
        run_authority_hash=AUTHORITY,
        resolved_recipe_hash=RECIPE,
        verb=QUERY_APPROVED_HEALTH,
        target="app-health",
        parameters=health_parameters,
        idempotency_key="health-once",
        deadline_ms=9_000,
    )
    health_evidence = broker.invoke(
        authority=scope,
        request=health_request,
        current_policy_digest=POLICY,
        current_resolved_recipe_hash=RECIPE,
        current_target=DESTINATION,
    )
    assert health_evidence.result == {
        "healthy": True,
        "probe_id": "ready",
        "generation": 1,
    }

    # The request/evidence surface contains no configured host path. The only
    # code that knows deployment_root is the operator-registered adapter closure.
    serialized = str(deploy_evidence.to_dict()) + str(health_evidence.to_dict())
    assert str(deployment_root) not in serialized
