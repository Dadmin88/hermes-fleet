from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from hermes_fleet.network_isolation import (
    NETWORK_PROJECT_ALLOWLIST,
    NetworkDestination,
)
from hermes_fleet.principal_identity import PrincipalReference
from hermes_fleet.run_authority import (
    IsolationAuthority,
    NetworkAuthorityIntent,
    RecipeAuthorityBinding,
    ResourceAuthority,
    RunAuthority,
)
from hermes_fleet.security_event import (
    HARD_DENY_SCHEMA,
    SECURITY_EVENT_SCHEMA,
    SECURITY_REQUEST_SCHEMA,
    DeterministicHardDeny,
    MemorySkillRisk,
    PolicyMismatch,
    QuarantineSignal,
    SecretInterceptionFact,
    SecurityEvent,
    SecurityEventError,
    hard_deny_from_dict,
    security_event_from_dict,
    validate_hard_denies,
)


def h(character: str) -> str:
    return "sha256:" + character * 64


def _ipv4(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


PUBLIC_IP = _ipv4(1, 1, 1, 1)
TARGET = {"source": "local", "node_id": "node-phase19", "generation": 7}
TARGET_DIGEST = (
    "sha256:"
    + __import__("hashlib")
    .sha256(json.dumps(TARGET, sort_keys=True, separators=(",", ":")).encode())
    .hexdigest()
)
PRINCIPAL = PrincipalReference(
    principal_id=h("1"),
    kind="owner",
    generation=3,
    binding_hash=h("2"),
)


def authority(**changes) -> RunAuthority:
    values = {
        "execution_id": "phase19-run-1",
        "idempotency_digest": h("3"),
        "principal": PRINCIPAL,
        "agent_instance_id": h("4"),
        "recipe": RecipeAuthorityBinding(
            recipe_hash=h("5"),
            resolved_recipe_hash=h("6"),
            compiler_version="fleet.workflow-recipe-compiler.v1",
            provenance_digest=h("7"),
            image="example.invalid/workshop@sha256:" + "a" * 64,
            workflow_id="workflow-phase19",
            workflow_revision=2,
            workflow_hash=h("8"),
            workflow_step_id="build",
        ),
        "policy_digest": h("9"),
        "capabilities_hash": h("b"),
        "target": TARGET,
        "target_digest": TARGET_DIGEST,
        "plan_fingerprint": h("c"),
        "issued_at_ms": 1_000,
        "deadline_ms": 5_000,
        "resources": ResourceAuthority(
            cpu_millis=2_000,
            memory_bytes=536_870_912,
            pids_limit=96,
            max_iterations=8,
        ),
        "isolation": IsolationAuthority(),
        "network": NetworkAuthorityIntent(
            mode=NETWORK_PROJECT_ALLOWLIST,
            destinations=(
                NetworkDestination(
                    host="example.com",
                    resolved_ips=(PUBLIC_IP,),
                    ports=(443,),
                ),
            ),
        ),
        "toolsets": ("fleet-terminal",),
        "approval_budget": 1,
        "project_scope": ("project-a",),
    }
    values.update(changes)
    return RunAuthority(**values)


def facts() -> tuple[
    tuple[MemorySkillRisk, ...],
    tuple[SecretInterceptionFact, ...],
    tuple[PolicyMismatch, ...],
    tuple[QuarantineSignal, ...],
]:
    return (
        (
            MemorySkillRisk(
                subject_kind="memory",
                subject_hash=h("d"),
                scope_kind="principal",
                risk_level="high",
                signal_codes=("stored-instruction", "authority-manipulation"),
                evidence_hash=h("e"),
            ),
            MemorySkillRisk(
                subject_kind="skill",
                subject_hash=h("f"),
                scope_kind="project",
                risk_level="medium",
                signal_codes=("network-requirement",),
                evidence_hash=h("0"),
            ),
        ),
        (
            SecretInterceptionFact(
                source_kind="prompt",
                detected_kinds=("api-key", "bearer-token"),
                detected_count=2,
                action="redacted",
                evidence_hash=h("a"),
            ),
        ),
        (
            PolicyMismatch(
                code="stale-capability",
                subject="destination-capabilities",
                expected_hash=h("b"),
                observed_hash=h("c"),
                evidence_hash=h("d"),
            ),
        ),
        (
            QuarantineSignal(
                candidate_hash=h("e"),
                quarantine_digest=h("f"),
                state="needs-review",
                reason_digest=h("0"),
                reason_codes=("protected-host-path", "network-requirement"),
                verification_state="not-run",
                verification_digest=None,
            ),
        ),
    )


def event() -> SecurityEvent:
    risks, interceptions, mismatches, quarantine = facts()
    return SecurityEvent.from_run_authority(
        authority(),
        requested_tools=("terminal.exec", "artifact.write"),
        memory_skill_risks=risks,
        secret_interceptions=interceptions,
        policy_mismatches=mismatches,
        quarantine_signals=quarantine,
    )


def test_event_binds_exact_request_authority_and_all_phase19_fact_families() -> None:
    item = event()
    document = item.to_dict()

    assert document["schema"] == SECURITY_EVENT_SCHEMA
    assert document["request"]["schema"] == SECURITY_REQUEST_SCHEMA
    assert document["request_hash"] == item.request_hash
    assert document["request"]["principal"] == PRINCIPAL.to_dict()
    assert document["request"]["recipe"]["recipe_hash"] == h("5")
    assert document["request"]["recipe"]["resolved_recipe_hash"] == h("6")
    assert document["request"]["run_authority_hash"] == authority().content_hash
    assert document["request"]["target"] == {
        "target_digest": TARGET_DIGEST,
        "target": TARGET,
    }
    assert document["request"]["requested_tools"] == ["artifact.write", "terminal.exec"]
    assert document["request"]["authorized_toolsets"] == ["fleet-terminal"]
    assert document["request"]["resources"] == {
        "cpu_millis": 2_000,
        "memory_bytes": 536_870_912,
        "pids_limit": 96,
        "max_iterations": 8,
        "deadline_ms": 5_000,
    }
    assert document["request"]["network"]["mode"] == NETWORK_PROJECT_ALLOWLIST
    assert document["request"]["network"]["destinations"] == [
        {
            "host": "example.com",
            "resolved_ips": [PUBLIC_IP],
            "ports": [443],
        }
    ]
    assert len(document["memory_skill_risks"]) == 2
    assert len(document["secret_interceptions"]) == 1
    assert len(document["policy_mismatches"]) == 1
    assert len(document["quarantine_signals"]) == 1
    assert "hard_denies" not in document
    assert item.content_hash.startswith("sha256:")
    assert item.content_hash != item.request_hash


def test_round_trip_is_closed_versioned_and_hash_stable() -> None:
    original = event()
    restored = security_event_from_dict(original.to_dict())
    assert restored == original
    assert restored.request_hash == original.request_hash
    assert restored.content_hash == original.content_hash

    tampered_network = original.to_dict()
    tampered_network["request"]["network"]["policy_hash"] = h("0")
    tampered_network["request_hash"] = (
        "sha256:"
        + __import__("hashlib")
        .sha256(
            json.dumps(
                tampered_network["request"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        .hexdigest()
    )
    with pytest.raises(SecurityEventError, match="network policy hash"):
        security_event_from_dict(tampered_network)

    tampered_target = original.to_dict()
    tampered_target["request"]["target"]["target"]["generation"] = 8
    tampered_target["request_hash"] = (
        "sha256:"
        + __import__("hashlib")
        .sha256(
            json.dumps(
                tampered_target["request"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        .hexdigest()
    )
    with pytest.raises(SecurityEventError, match="target digest does not match"):
        security_event_from_dict(tampered_target)

    wrong_schema = original.to_dict()
    wrong_schema["schema"] = "fleet.security-event.v2"
    with pytest.raises(SecurityEventError, match="unsupported"):
        security_event_from_dict(wrong_schema)

    extra_field = original.to_dict()
    extra_field["secret_body"] = "not-allowed"
    with pytest.raises(SecurityEventError, match="closed schema"):
        security_event_from_dict(extra_field)


def test_request_mutation_changes_request_and_event_hash() -> None:
    original = event()
    changed = SecurityEvent.from_run_authority(
        authority(),
        requested_tools=("terminal.exec", "artifact.write", "network.fetch"),
        memory_skill_risks=original.memory_skill_risks,
        secret_interceptions=original.secret_interceptions,
        policy_mismatches=original.policy_mismatches,
        quarantine_signals=original.quarantine_signals,
    )
    assert changed.request_hash != original.request_hash
    assert changed.content_hash != original.content_hash


def test_derived_fact_mutation_keeps_request_hash_but_changes_event_hash() -> None:
    original = event()
    changed_risks = (
        replace(original.memory_skill_risks[0], risk_level="critical"),
        original.memory_skill_risks[1],
    )
    changed = SecurityEvent.from_run_authority(
        authority(),
        requested_tools=original.requested_tools,
        memory_skill_risks=changed_risks,
        secret_interceptions=original.secret_interceptions,
        policy_mismatches=original.policy_mismatches,
        quarantine_signals=original.quarantine_signals,
    )
    assert changed.request_hash == original.request_hash
    assert changed.content_hash != original.content_hash


def test_fact_order_is_canonical_but_duplicates_fail_closed() -> None:
    original = event()
    reordered = SecurityEvent.from_run_authority(
        authority(),
        requested_tools=tuple(reversed(original.requested_tools)),
        memory_skill_risks=tuple(reversed(original.memory_skill_risks)),
        secret_interceptions=original.secret_interceptions,
        policy_mismatches=original.policy_mismatches,
        quarantine_signals=original.quarantine_signals,
    )
    assert reordered == original
    assert reordered.content_hash == original.content_hash

    with pytest.raises(SecurityEventError, match="duplicates"):
        SecurityEvent.from_run_authority(
            authority(),
            requested_tools=("terminal.exec", "terminal.exec"),
        )
    with pytest.raises(SecurityEventError, match="duplicates"):
        SecurityEvent.from_run_authority(
            authority(),
            memory_skill_risks=(
                original.memory_skill_risks[0],
                original.memory_skill_risks[0],
            ),
        )


def test_security_event_and_nested_facts_are_immutable() -> None:
    item = event()
    with pytest.raises(FrozenInstanceError):
        item.policy_digest = h("0")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        item.resources.cpu_millis = 1  # type: ignore[misc]


def test_interception_schema_cannot_carry_secret_bodies_or_secret_value_hashes() -> (
    None
):
    item = event().to_dict()
    interception = item["secret_interceptions"][0]
    assert set(interception) == {
        "source_kind",
        "detected_kinds",
        "detected_count",
        "action",
        "evidence_hash",
    }
    assert "value" not in interception
    assert "body" not in interception
    assert "secret_hash" not in interception

    tampered = event().to_dict()
    tampered["secret_interceptions"][0]["value"] = "forbidden"
    with pytest.raises(SecurityEventError, match="closed schema"):
        security_event_from_dict(tampered)


def test_interception_and_signal_validation_fail_closed() -> None:
    with pytest.raises(SecurityEventError, match="zero-detection"):
        SecretInterceptionFact(
            source_kind="prompt",
            detected_kinds=("api-key",),
            detected_count=0,
            action="none",
            evidence_hash=h("1"),
        )
    with pytest.raises(SecurityEventError, match="require classification"):
        SecretInterceptionFact(
            source_kind="prompt",
            detected_kinds=(),
            detected_count=1,
            action="blocked",
            evidence_hash=h("1"),
        )
    with pytest.raises(SecurityEventError, match="require classification"):
        SecretInterceptionFact(
            source_kind="prompt",
            detected_kinds=("api-key",),
            detected_count=1,
            action="none",
            evidence_hash=h("1"),
        )
    with pytest.raises(SecurityEventError, match="risk level"):
        MemorySkillRisk(
            subject_kind="memory",
            subject_hash=h("1"),
            scope_kind="principal",
            risk_level="unknown",
            signal_codes=(),
            evidence_hash=h("2"),
        )
    with pytest.raises(SecurityEventError, match="quarantine state"):
        QuarantineSignal(
            candidate_hash=h("1"),
            quarantine_digest=h("2"),
            state="active",
            reason_digest=h("3"),
            reason_codes=(),
        )
    with pytest.raises(SecurityEventError, match="verification state"):
        QuarantineSignal(
            candidate_hash=h("1"),
            quarantine_digest=h("2"),
            state="verification-ready",
            reason_digest=h("3"),
            reason_codes=(),
            verification_state="unknown",
        )
    with pytest.raises(SecurityEventError, match="cannot carry verification digest"):
        QuarantineSignal(
            candidate_hash=h("1"),
            quarantine_digest=h("2"),
            state="verification-ready",
            reason_digest=h("3"),
            reason_codes=(),
            verification_state="not-run",
            verification_digest=h("4"),
        )
    verified = QuarantineSignal(
        candidate_hash=h("1"),
        quarantine_digest=h("2"),
        state="verification-ready",
        reason_digest=h("3"),
        reason_codes=(),
        verification_state="verified",
        verification_digest=h("4"),
    )
    assert verified.to_dict()["verification_state"] == "verified"


def test_policy_mismatch_is_fact_only_and_carries_no_deny_effect() -> None:
    mismatch = event().policy_mismatches[0]
    serialized = mismatch.to_dict()
    assert set(serialized) == {
        "code",
        "subject",
        "expected_hash",
        "observed_hash",
        "evidence_hash",
    }
    assert "effect" not in serialized
    assert "verdict" not in serialized
    assert "deny" not in serialized
    generic = PolicyMismatch(
        code="resource-policy-mismatch",
        subject="resources",
        expected_hash=None,
        observed_hash=None,
        evidence_hash=h("4"),
    )
    assert generic.to_dict()["expected_hash"] is None
    assert generic.to_dict()["observed_hash"] is None


def test_hard_denies_are_separate_exact_event_bound_and_round_trip() -> None:
    item = event()
    deny = DeterministicHardDeny.from_event(
        item,
        code="policy-stale",
        subject="run-authority",
        evidence_hash=h("1"),
    )
    assert deny.to_dict()["schema"] == HARD_DENY_SCHEMA
    assert hard_deny_from_dict(deny.to_dict()) == deny
    assert validate_hard_denies(item, [deny]) == (deny,)
    assert "hard_denies" not in item.to_dict()

    changed = SecurityEvent.from_run_authority(
        authority(),
        requested_tools=("different.tool",),
        memory_skill_risks=item.memory_skill_risks,
        secret_interceptions=item.secret_interceptions,
        policy_mismatches=item.policy_mismatches,
        quarantine_signals=item.quarantine_signals,
    )
    with pytest.raises(SecurityEventError, match="stale or request-substituted"):
        deny.validate_event(changed)
    with pytest.raises(SecurityEventError, match="duplicates"):
        validate_hard_denies(item, (deny, deny))


def test_hard_deny_mutation_changes_identity_and_closed_schema_rejects_extra_fields() -> (
    None
):
    item = event()
    first = DeterministicHardDeny.from_event(
        item,
        code="policy-stale",
        subject="run-authority",
        evidence_hash=h("1"),
    )
    second = DeterministicHardDeny.from_event(
        item,
        code="policy-stale",
        subject="run-authority",
        evidence_hash=h("2"),
    )
    assert first.content_hash != second.content_hash

    document = first.to_dict()
    document["message"] = "human prose is not part of the hard-deny contract"
    with pytest.raises(SecurityEventError, match="closed schema"):
        hard_deny_from_dict(document)


def test_builder_requires_exact_run_authority_and_deterministic_target_shape() -> None:
    with pytest.raises(SecurityEventError, match="exact RunAuthority"):
        SecurityEvent.from_run_authority(object())  # type: ignore[arg-type]

    malformed_target = {"source": "local", "node_id": "node-phase19"}
    malformed_digest = (
        "sha256:"
        + __import__("hashlib")
        .sha256(
            json.dumps(malformed_target, sort_keys=True, separators=(",", ":")).encode()
        )
        .hexdigest()
    )
    with pytest.raises(SecurityEventError, match="source/node/generation"):
        SecurityEvent.from_run_authority(
            authority(target=malformed_target, target_digest=malformed_digest)
        )
