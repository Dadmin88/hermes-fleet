from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("agent.fleet_memory_scope")
pytest.importorskip("tools.fleet_promotion")

import tools.fleet_skill_verification as skill_verification_module  # noqa: E402
import tools.skill_manager_tool as skill_manager  # noqa: E402
from agent.fleet_memory_scope import (  # noqa: E402
    FleetMemoryBinding,
    FleetMemoryScopeRef,
    fleet_memory_scope,
)
from agent.fleet_promotion import FleetPromotionAuthorization  # noqa: E402
from agent.fleet_skill_learning_scope import (  # noqa: E402
    FleetSkillFilesystemNeed,
    FleetSkillLearningBinding,
    fleet_skill_learning_scope,
)
from hermes_constants import (  # noqa: E402
    reset_hermes_home_override,
    set_hermes_home_override,
)
from tools.fleet_promotion import (  # noqa: E402
    commit_memory_promotion,
    prepare_memory_promotion,
    prepare_skill_promotion,
)
from tools.fleet_skill_quarantine import quarantine_skill_candidate  # noqa: E402
from tools.fleet_skill_verification import (  # noqa: E402
    VerificationCheck,
    verify_skill_candidate,
)
from tools.memory_tool import MemoryStore  # noqa: E402
from tools.skill_provenance import (  # noqa: E402
    BACKGROUND_REVIEW,
    reset_current_write_origin,
    set_current_write_origin,
)

from hermes_fleet.learning_promotion_gate import (  # noqa: E402
    READY,
    LearningPromotionGate,
    LearningPromotionRequest,
)
from hermes_fleet.principal_identity import (  # noqa: E402
    SOURCE_LOCAL_PEER,
    PrincipalBinding,
    PrincipalDefinition,
    PrincipalRegistry,
    derive_scoped_principal,
)
from hermes_fleet.promotion import PromotionScopeRef  # noqa: E402
from hermes_fleet.templar import (  # noqa: E402
    ALLOW,
    TEMPLAR_BACKEND_RESPONSE_SCHEMA,
    TemplarCore,
    TemplarEvaluatorIdentity,
    TemplarPolicyRef,
)


def _hash(char: str) -> str:
    return "sha256:" + char * 64


_SAFE_SKILL = """---
name: phase23-safe-helper
description: Safe repeatable helper for Phase 23 integration
allowed-tools: Bash
---

# Phase 23 safe helper

Remember phase23-skill@example.com only as sanitized promotion material.

```bash
python build.py
```
"""


def _mock_runtime_checks(*_args, **_kwargs) -> list[VerificationCheck]:
    return [
        VerificationCheck(
            "broker-denial", "broker-sockets", True, "broker paths absent"
        ),
        VerificationCheck(
            "broker-denial", "docker-socket", True, "Docker socket absent"
        ),
        VerificationCheck(
            "filesystem-denial", "host-path:/etc/passwd", True, "host path absent"
        ),
        VerificationCheck("network-denial", "internet", True, "unreachable"),
        VerificationCheck(
            "network-denial", "management-network", True, "unreachable"
        ),
        VerificationCheck(
            "positive-test", "bundle-readable", True, "files=1"
        ),
        VerificationCheck(
            "positive-test", "scratch-write", True, "isolated tmpfs"
        ),
        VerificationCheck(
            "privilege-denial", "effective-capabilities", True, "CapEff=0"
        ),
        VerificationCheck(
            "privilege-denial", "non-root-uid", True, "euid=65534"
        ),
        VerificationCheck(
            "resource-bound", "address-space", True, "bounded"
        ),
        VerificationCheck("resource-bound", "cpu", True, "bounded"),
        VerificationCheck("resource-bound", "file-size", True, "bounded"),
        VerificationCheck("resource-bound", "open-files", True, "bounded"),
        VerificationCheck("resource-bound", "processes", True, "bounded"),
        VerificationCheck(
            "secret-denial", "environment", True, "sensitive_names=0"
        ),
    ]


class _AllowBackend:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def evaluate(
        self,
        request: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        del timeout_ms
        self.requests.append(request)
        return {
            "schema": TEMPLAR_BACKEND_RESPONSE_SCHEMA,
            "evaluation_id": request["evaluation_id"],
            "request_hash": request["request_hash"],
            "event_hash": request["event_hash"],
            "decision": ALLOW,
            "reason_codes": [],
        }


def test_real_agent_prepare_templar_gate_and_commit_are_exact(tmp_path: Path) -> None:
    home = tmp_path / "hermes-home"
    token = set_hermes_home_override(home)
    try:
        registry = PrincipalRegistry(
            tmp_path / "principals.sqlite",
            now_ms=lambda: 1_000,
        )
        owner, _created = registry.ensure(
            PrincipalDefinition(
                kind="owner",
                subject="phase23-owner",
                scope={"owner": "phase23-owner"},
            ),
            PrincipalBinding(
                source=SOURCE_LOCAL_PEER,
                evidence={"machine_id": "phase23-local", "uid": 1000},
            ),
        )
        project_ref = derive_scoped_principal(
            registry,
            parent=owner.reference,
            kind="project",
            subject="phase23-project-admin",
            scope={"project": "phase23-project"},
        )
        project_admin = registry.require_current(project_ref)

        principal_id = owner.reference.principal_id
        agent_instance_id = _hash("3")
        private = FleetMemoryScopeRef("principal", principal_id)
        project = FleetMemoryScopeRef("project", "phase23-project")
        binding = FleetMemoryBinding(
            version="fleet-memory-v1",
            principal_id=principal_id,
            principal_kind="owner",
            principal_generation=owner.reference.generation,
            principal_binding_hash=owner.reference.binding_hash,
            agent_instance_id=agent_instance_id,
            source_run="phase23-integration-source",
            read_scopes=(private,),
            write_scope=private,
            retention_until_ms=None,
        )

        source = "Contact phase23@example.com after release"
        with fleet_memory_scope(binding):
            store = MemoryStore(
                memory_char_limit=10_000,
                user_char_limit=10_000,
            )
            store.load_from_disk()
            assert store.add("memory", source)["success"] is True
        source_hash = MemoryStore._entry_hash(source)

        prepared = prepare_memory_promotion(
            target="memory",
            source_scope=private,
            source_content_hash=source_hash,
            source_owner_principal_id=principal_id,
            agent_instance_id=agent_instance_id,
        )
        prepared_document = prepared.to_document()
        assert prepared_document["sanitized"] is True
        material = prepared_document["evaluation_material"]
        assert material["content_hash"] == prepared.approved_content_hash
        assert "phase23@example.com" not in material["text"]
        assert "[email]" in material["text"]

        policy_digest = _hash("f")
        learning_request = LearningPromotionRequest.from_prepared(
            prepared_document,
            source_owner_principal_id=principal_id,
            agent_instance_id=agent_instance_id,
            source_scope=PromotionScopeRef("principal", principal_id),
            target_scope=PromotionScopeRef("project", "phase23-project"),
            administrator=project_admin.reference,
            policy_digest=policy_digest,
        )

        backend = _AllowBackend()
        templar = TemplarCore(
            backend=backend,
            policy=TemplarPolicyRef(
                policy_id="phase23-integration",
                policy_version="phase23-v1",
                policy_digest=_hash("e"),
            ),
            evaluator=TemplarEvaluatorIdentity(
                evaluator_id="phase23-integration-evaluator",
                implementation_version="phase23-test-v1",
                model_provider="test-provider",
                model_name="test-model",
                model_version="test-model-v1",
            ),
            timeout_ms=1_000,
            verdict_ttl_ms=60_000,
        )
        gate = LearningPromotionGate(
            policy_digest=policy_digest,
            templar=templar,
            authorization_ttl_ms=60_000,
        )
        outcome = gate.authorize(
            learning_request,
            administrator=project_admin,
        )

        assert outcome.status == READY
        assert outcome.authorization is not None
        assert (
            outcome.authorization.approved_content_hash
            == prepared.approved_content_hash
        )
        assert outcome.authorization.to_request()["authority"] == "none"
        assert len(backend.requests) == 1
        event = backend.requests[0]["event"]
        assert event["request"]["candidate_hash"] == prepared.approved_content_hash
        assert event["request"]["evaluation_material"] == material

        committed = commit_memory_promotion(
            target="memory",
            authorization=FleetPromotionAuthorization.from_request(
                outcome.authorization.to_request()
            ),
        )
        assert committed.to_document()["authority"] == "none"

        reader = FleetMemoryBinding(
            version="fleet-memory-v1",
            principal_id=principal_id,
            principal_kind="owner",
            principal_generation=owner.reference.generation,
            principal_binding_hash=owner.reference.binding_hash,
            agent_instance_id=agent_instance_id,
            source_run="phase23-integration-reader",
            read_scopes=(private, project),
            write_scope=private,
            retention_until_ms=None,
        )
        with fleet_memory_scope(reader):
            store = MemoryStore(
                memory_char_limit=10_000,
                user_char_limit=10_000,
            )
            store.load_from_disk()
            project_text = (
                store._scope_dir(project)
                / store._target_filename("memory")
            ).read_text(encoding="utf-8")

        assert "phase23@example.com" not in project_text
        assert "[email]" in project_text
        assert json.loads(
            json.dumps(outcome.authorization.to_request(), sort_keys=True)
        )["approved_content_hash"] == prepared.approved_content_hash
    finally:
        reset_hermes_home_override(token)


def test_real_agent_verified_skill_reconstructs_exact_phase23_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "hermes-home"
    token = set_hermes_home_override(home)
    try:
        registry = PrincipalRegistry(
            tmp_path / "principals.sqlite",
            now_ms=lambda: 1_000,
        )
        owner, _created = registry.ensure(
            PrincipalDefinition(
                kind="owner",
                subject="phase23-skill-owner",
                scope={"owner": "phase23-skill-owner"},
            ),
            PrincipalBinding(
                source=SOURCE_LOCAL_PEER,
                evidence={"machine_id": "phase23-skill-local", "uid": 1000},
            ),
        )
        project_ref = derive_scoped_principal(
            registry,
            parent=owner.reference,
            kind="project",
            subject="phase23-skill-project-admin",
            scope={"project": "phase23-project"},
        )
        project_admin = registry.require_current(project_ref)

        skills_root = tmp_path / "skills"
        skills_root.mkdir()
        monkeypatch.setattr(skill_manager, "SKILLS_DIR", skills_root)
        monkeypatch.setattr(
            "agent.skill_utils.get_all_skills_dirs",
            lambda: [skills_root],
        )
        skill_manager._reset_background_review_read_marks()
        monkeypatch.setattr(
            skill_verification_module,
            "_runtime_checks",
            _mock_runtime_checks,
        )

        principal_id = owner.reference.principal_id
        agent_instance_id = _hash("3")
        learning_binding = FleetSkillLearningBinding(
            principal_id=principal_id,
            principal_kind="owner",
            principal_generation=owner.reference.generation,
            principal_binding_hash=owner.reference.binding_hash,
            agent_instance_id=agent_instance_id,
            source_run="phase23-skill-source",
            scope_kind="principal",
            scope_id=principal_id,
            run_authority_hash=_hash("4"),
            recipe_hash=_hash("5"),
            resolved_recipe_hash=_hash("6"),
            plan_fingerprint=_hash("7"),
            capabilities_hash=_hash("8"),
            target_digest=_hash("9"),
            toolsets=("fleet-terminal",),
            filesystem_needs=(
                FleetSkillFilesystemNeed(
                    project_id="phase23-project",
                    relative_path="src",
                    target="/workspace/src",
                    mode="read-write",
                    max_bytes=4096,
                ),
            ),
            network_mode="none",
            network_policy_hash=_hash("a"),
            secret_need_fingerprints=(),
        )

        origin_token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            with fleet_skill_learning_scope(learning_binding):
                create_result = json.loads(
                    skill_manager.skill_manage(
                        action="create",
                        name="phase23-safe-helper",
                        content=_SAFE_SKILL,
                    )
                )
        finally:
            reset_current_write_origin(origin_token)
        assert create_result["success"] is True

        candidate_root = skills_root / ".fleet" / "candidates"
        candidates = [path for path in candidate_root.iterdir() if path.is_dir()]
        assert len(candidates) == 1
        candidate = candidates[0]
        metadata = json.loads(
            (candidate / "candidate.json").read_text(encoding="utf-8")
        )
        assert metadata["name"] == "phase23-safe-helper"
        assert metadata["principal"]["principal_id"] == principal_id

        quarantine = quarantine_skill_candidate(
            candidate,
            expected_binding=learning_binding,
        )
        assert quarantine.state == "verification-ready"
        verification = verify_skill_candidate(
            candidate,
            expected_binding=learning_binding,
        )
        assert verification.verified is True

        prepared = prepare_skill_promotion(
            candidate_id=metadata["candidate_id"],
            source_owner_principal_id=principal_id,
            agent_instance_id=agent_instance_id,
        )
        prepared_document = prepared.to_document()
        assert prepared.verification_digest == verification.verification_digest
        assert prepared_document["sanitized"] is True
        material = prepared_document["evaluation_material"]
        assert material["kind"] == "skill"
        assert material["content_hash"] == prepared.approved_content_hash
        skill_file = next(
            item for item in material["files"] if item["path"] == "SKILL.md"
        )
        assert "phase23-skill@example.com" not in skill_file["text"]
        assert "[email]" in skill_file["text"]

        policy_digest = _hash("f")
        learning_request = LearningPromotionRequest.from_prepared(
            prepared_document,
            source_owner_principal_id=principal_id,
            agent_instance_id=agent_instance_id,
            source_scope=PromotionScopeRef("principal", principal_id),
            target_scope=PromotionScopeRef("project", "phase23-project"),
            administrator=project_admin.reference,
            policy_digest=policy_digest,
        )
        backend = _AllowBackend()
        templar = TemplarCore(
            backend=backend,
            policy=TemplarPolicyRef(
                policy_id="phase23-skill-integration",
                policy_version="phase23-v1",
                policy_digest=_hash("e"),
            ),
            evaluator=TemplarEvaluatorIdentity(
                evaluator_id="phase23-skill-evaluator",
                implementation_version="phase23-test-v1",
                model_provider="test-provider",
                model_name="test-model",
                model_version="test-model-v1",
            ),
            timeout_ms=1_000,
            verdict_ttl_ms=60_000,
        )
        gate = LearningPromotionGate(
            policy_digest=policy_digest,
            templar=templar,
            authorization_ttl_ms=60_000,
        )

        outcome = gate.authorize(
            learning_request,
            administrator=project_admin,
        )

        assert outcome.status == READY
        assert outcome.authorization is not None
        assert (
            outcome.authorization.approved_content_hash
            == prepared.approved_content_hash
        )
        assert (
            outcome.authorization.verification_digest
            == verification.verification_digest
        )
        assert len(backend.requests) == 1
        event_request = backend.requests[0]["event"]["request"]
        assert event_request["candidate_hash"] == prepared.approved_content_hash
        assert event_request["verification_digest"] == verification.verification_digest
        assert event_request["evaluation_material"] == material
    finally:
        reset_hermes_home_override(token)
