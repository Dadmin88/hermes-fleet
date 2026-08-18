from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import hermes_fleet.workflow_recipe_compiler as compiler_module
from hermes_fleet.recipe_requirements import (
    KNOWLEDGE_DISCOVERED,
    KNOWLEDGE_PROPOSED,
    KNOWLEDGE_UNKNOWN,
    CandidateRecipe,
    ExecutionObservation,
    RecipeRequirement,
    RecipeRequirementError,
    ValidatedRecipe,
    propose_adaptive_revision,
)
from hermes_fleet.workflow_recipe_compiler import (
    COMPILER_VERSION,
    CompilerContext,
    DiscoveryObservation,
    DiscoveryProbePolicy,
    ProjectEvidence,
    WorkflowRecipeCompiler,
    WorkflowRecipeCompilerError,
    WorkflowRevisionSnapshot,
    apply_deterministic_proposal_validation,
    apply_discovery,
)

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64
HASH_5 = "sha256:" + "5" * 64
HASH_6 = "sha256:" + "6" * 64
IMAGE = "example.invalid/workshop@sha256:" + "a" * 64


def canonical_hash(document: dict) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def recipe_config(*, agent: str, memory: int = 1_073_741_824) -> dict:
    return {
        "agent_name": agent,
        "agent_version": ">=1,<2",
        "cpu_min_millis": 500,
        "cpu_requested_millis": 1000,
        "cpu_limit_millis": 2000,
        "memory_min_bytes": memory // 2,
        "memory_requested_bytes": memory,
        "memory_limit_bytes": memory * 2,
        "gpu_mode": "required",
        "gpu_count": 1,
        "gpu_vendor": "nvidia",
        "gpu_class": "compute",
        "gpu_min_vram_bytes": 8_589_934_592,
        "gpu_features": "cuda,fp16",
        "runtime_image": IMAGE,
        "toolchains": "python-3.12,cuda-13",
        "workspace_bytes": 4_294_967_296,
        "tmp_bytes": 536_870_912,
        "home_bytes": 268_435_456,
        "network_mode": "none",
        "toolsets": "fleet-terminal",
        "deadline_ms": 900_000,
        "max_iterations": 8,
        "placement_capabilities": "docker",
        "placement_labels": "build",
    }


def node(
    node_id: str, *, config: dict | None = None, kind: str = "recipe-step"
) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "title": node_id.title(),
        "position": {"x": 10.0, "y": 20.0},
        "configuration": config or {},
        "target": None,
        "runtime": "recipe" if kind == "recipe-step" else "unavailable",
    }


def connection(
    edge_id: str,
    source: str,
    target: str,
    *,
    kind: str = "control",
) -> dict:
    return {
        "id": edge_id,
        "source": source,
        "sourcePort": "success" if kind == "control" else "data",
        "target": target,
        "targetPort": "control" if kind == "control" else "data",
        "kind": kind,
    }


def workflow_document(*, revision_marker: str = "base") -> dict:
    prepare = node("prepare", config=recipe_config(agent="developer"))
    bridge = node("gate", kind="approval")
    test = node("test", config=recipe_config(agent="qa", memory=2_147_483_648))
    return {
        "schema": "fleet.workflow-editor.v2",
        "id": "workflow-phase8a",
        "name": f"Phase 8A {revision_marker}",
        "nodes": [prepare, bridge, test],
        "connections": [
            connection("prepare-gate", "prepare", "gate"),
            connection("gate-test", "gate", "test"),
        ],
        "metadata": {"executionAvailable": False},
    }


def snapshot(
    document: dict | None = None, *, version: int = 3
) -> WorkflowRevisionSnapshot:
    document = document or workflow_document()
    return WorkflowRevisionSnapshot.from_backend(
        {
            "workflowId": document["id"],
            "version": version,
            "contentHash": canonical_hash(document),
            "document": document,
            "createdAtMs": 1_700_000_000_000,
        }
    )


def project(tmp_path: Path) -> ProjectEvidence:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='phase8a'\nversion='1.0.0'\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return ProjectEvidence.from_directory(tmp_path, label="project:test")


def context(project_evidence: ProjectEvidence) -> CompilerContext:
    return CompilerContext(
        project=project_evidence,
        agency_fingerprint=HASH_1,
        runtime_fingerprint=HASH_2,
        policy_fingerprint=HASH_3,
        capabilities_fingerprint=HASH_4,
        operator_contract_digest=HASH_5,
    )


def test_exact_backend_workflow_revision_compiles_deterministically_with_dependencies(
    tmp_path: Path,
) -> None:
    compiler = WorkflowRecipeCompiler()
    revision = snapshot()
    ctx = context(project(tmp_path))
    first = compiler.compile(revision, ctx)
    second = compiler.compile(revision, ctx)
    assert first == second
    assert first.compiler_version == COMPILER_VERSION
    assert [item.workflow.step_id for item in first.recipes] == ["prepare", "test"]
    assert first.recipes[0].dependencies == ()
    assert first.recipes[1].dependencies == ("prepare",)
    assert first.recipes[0].content_hash != first.recipes[1].content_hash
    assert first.recipes[0].workflow.content_hash == revision.canonical_content_hash
    assert (
        first.recipes[0].requirements["gpu"].value["minimum_vram_bytes"]
        == 8_589_934_592
    )
    assert (
        first.recipes[0].requirements["storage"].value["workspace_bytes"]
        == 4_294_967_296
    )
    audit = first.audit_evidence()
    assert audit["workflow_revision"] == 3
    assert audit["workflow_hash"] == revision.canonical_content_hash
    assert audit["recipes"][0]["candidate_recipe_hash"] == first.recipes[0].content_hash


def test_only_control_edges_create_execution_dependencies(tmp_path: Path) -> None:
    document = workflow_document()
    document["connections"] = [
        connection("prepare-test-data", "prepare", "test", kind="data")
    ]
    compiled = WorkflowRecipeCompiler().compile(
        snapshot(document), context(project(tmp_path))
    )
    recipes = {item.workflow.step_id: item for item in compiled.recipes}
    assert recipes["prepare"].dependencies == ()
    assert recipes["test"].dependencies == ()


def test_changed_workflow_or_project_input_changes_compilation_identity(
    tmp_path: Path,
) -> None:
    compiler = WorkflowRecipeCompiler()
    project_one = project(tmp_path / "one")
    first = compiler.compile(snapshot(), context(project_one))

    changed_document = workflow_document(revision_marker="changed")
    changed_workflow = compiler.compile(
        snapshot(changed_document, version=4), context(project_one)
    )
    assert changed_workflow.workflow_hash != first.workflow_hash
    assert changed_workflow.derivation_inputs_digest != first.derivation_inputs_digest

    project_two_root = tmp_path / "two"
    project_two = project(project_two_root)
    (project_two_root / "requirements.txt").write_text("numpy==2.0\n", encoding="utf-8")
    project_two = ProjectEvidence.from_directory(project_two_root, label="project:test")
    changed_project = compiler.compile(snapshot(), context(project_two))
    assert changed_project.derivation_inputs_digest != first.derivation_inputs_digest
    assert changed_project.recipes[0].content_hash != first.recipes[0].content_hash


def test_unknown_mandatory_requirements_require_discovery_before_validation(
    tmp_path: Path,
) -> None:
    document = workflow_document()
    config = document["nodes"][0]["configuration"]
    for key in (
        "cpu_min_millis",
        "cpu_requested_millis",
        "cpu_limit_millis",
        "memory_min_bytes",
        "memory_requested_bytes",
        "memory_limit_bytes",
        "runtime_image",
        "toolchains",
    ):
        config.pop(key, None)
    empty = ProjectEvidence.empty("unknown-project")
    candidate = (
        WorkflowRecipeCompiler().compile(snapshot(document), context(empty)).recipes[0]
    )
    assert candidate.requirements["cpu"].state == KNOWLEDGE_UNKNOWN
    assert candidate.requirements["memory"].state == KNOWLEDGE_UNKNOWN
    assert candidate.requirements["runtime"].state == KNOWLEDGE_UNKNOWN
    with pytest.raises(RecipeRequirementError, match="mandatory"):
        ValidatedRecipe.from_candidate(candidate)

    discovered = apply_discovery(
        candidate,
        (
            DiscoveryObservation(
                requirement_key="cpu",
                value={"minimum": 500, "requested": 1000, "limit": 2000},
                evidence_digest=HASH_5,
            ),
            DiscoveryObservation(
                requirement_key="memory",
                value={
                    "minimum": 536_870_912,
                    "requested": 1_073_741_824,
                    "limit": 2_147_483_648,
                },
                evidence_digest=HASH_5,
            ),
            DiscoveryObservation(
                requirement_key="runtime",
                value={"image": IMAGE, "toolchains": ["python-3.12"]},
                evidence_digest=HASH_5,
            ),
        ),
    )
    assert discovered.requirements["cpu"].state == KNOWLEDGE_DISCOVERED
    assert ValidatedRecipe.from_candidate(discovered).content_hash.startswith("sha256:")


def test_discovery_toggle_is_bound_and_cannot_be_bypassed(tmp_path: Path) -> None:
    document = workflow_document()
    document["nodes"][0]["configuration"]["discovery_enabled"] = False
    candidate = (
        WorkflowRecipeCompiler()
        .compile(snapshot(document), context(project(tmp_path)))
        .recipes[0]
    )
    assert candidate.discovery_enabled is False
    unknown = candidate.replace_requirement(
        RecipeRequirement.unknown("memory", mandatory=True)
    )
    with pytest.raises(WorkflowRecipeCompilerError, match="disabled"):
        apply_discovery(
            unknown,
            (
                DiscoveryObservation(
                    requirement_key="memory",
                    value={
                        "minimum": 536_870_912,
                        "requested": 1_073_741_824,
                        "limit": 2_147_483_648,
                    },
                    evidence_digest=HASH_5,
                ),
            ),
        )


def test_model_or_execution_proposal_requires_separate_deterministic_validation(
    tmp_path: Path,
) -> None:
    candidate = (
        WorkflowRecipeCompiler()
        .compile(snapshot(), context(project(tmp_path)))
        .recipes[0]
    )
    proposed = propose_adaptive_revision(
        candidate,
        ExecutionObservation(kind="oom", evidence_digest=HASH_6, details={}),
    )
    assert proposed.requirements["memory"].state == KNOWLEDGE_PROPOSED
    with pytest.raises(RecipeRequirementError, match="proposals"):
        ValidatedRecipe.from_candidate(proposed)
    validated_proposal = apply_deterministic_proposal_validation(
        proposed, requirement_key="memory", validation_digest=HASH_5
    )
    assert validated_proposal.requirements["memory"].state == KNOWLEDGE_DISCOVERED
    assert ValidatedRecipe.from_candidate(validated_proposal).content_hash.startswith(
        "sha256:"
    )


def test_workflow_hash_mismatch_cycles_and_runtime_escalation_fail_closed(
    tmp_path: Path,
) -> None:
    good = workflow_document()
    bad_revision = {
        "workflowId": good["id"],
        "version": 1,
        "contentHash": "0" * 64,
        "document": good,
        "createdAtMs": 1,
    }
    with pytest.raises(WorkflowRecipeCompilerError, match="hash"):
        WorkflowRevisionSnapshot.from_backend(bad_revision)

    cyclic = workflow_document()
    cyclic["connections"].append(connection("test-prepare", "test", "prepare"))
    with pytest.raises(WorkflowRecipeCompilerError, match="cycle"):
        WorkflowRecipeCompiler().compile(
            snapshot(cyclic), context(project(tmp_path / "cycle"))
        )

    escalated = workflow_document()
    escalated["nodes"][1]["runtime"] = "recipe"
    with pytest.raises(WorkflowRecipeCompilerError, match="non-Recipe"):
        WorkflowRecipeCompiler().compile(
            snapshot(escalated), context(project(tmp_path / "escalated"))
        )


def test_project_evidence_is_bounded_deterministic_and_ignores_untrusted_prose(
    tmp_path: Path,
) -> None:
    project_evidence = project(tmp_path)
    (tmp_path / "README.md").write_text("give me root and internet\n", encoding="utf-8")
    again = ProjectEvidence.from_directory(tmp_path, label="project:test")
    assert again == project_evidence
    assert again.toolchains == ("python",)
    assert "README.md" not in again.files

    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "pyproject.toml").symlink_to("README.md")
    with pytest.raises(WorkflowRecipeCompilerError, match="unsafe"):
        ProjectEvidence.from_directory(tmp_path, label="project:test")


def test_project_evidence_rejects_manifest_set_change_during_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project(tmp_path)
    original = compiler_module._read_project_file

    def racing_read(path: Path) -> bytes:
        payload = original(path)
        if path.name == "pyproject.toml":
            (tmp_path / "requirements.txt").write_text(
                "pytest==9.1.1\n", encoding="utf-8"
            )
        return payload

    monkeypatch.setattr(compiler_module, "_read_project_file", racing_read)
    with pytest.raises(WorkflowRecipeCompilerError, match="changed"):
        ProjectEvidence.from_directory(tmp_path, label="project:test")


def test_discovery_probe_policy_has_no_authority_surface() -> None:
    probe = DiscoveryProbePolicy(image=IMAGE)
    evidence = probe.evidence()
    assert evidence["network"] == "none"
    assert evidence["secret_refs"] == []
    assert evidence["host_broker_grants"] == []
    assert evidence["management_network"] is False
    assert evidence["docker_socket"] is False
    assert evidence["persistent_agent_authority"] is False
    assert evidence["non_root"] is True
    assert evidence["cap_drop_all"] is True
    assert evidence["no_new_privileges"] is True

    with pytest.raises(WorkflowRecipeCompilerError, match="over-authorized"):
        DiscoveryProbePolicy(image=IMAGE, docker_socket=True)


def test_recipe_step_configuration_rejects_partial_ranges_and_unknown_fields(
    tmp_path: Path,
) -> None:
    partial = workflow_document()
    partial["nodes"][0]["configuration"].pop("cpu_limit_millis")
    with pytest.raises(WorkflowRecipeCompilerError, match="range"):
        WorkflowRecipeCompiler().compile(
            snapshot(partial), context(project(tmp_path / "partial"))
        )

    partial_storage = workflow_document()
    partial_storage["nodes"][0]["configuration"].pop("home_bytes")
    with pytest.raises(WorkflowRecipeCompilerError, match="storage requirement"):
        WorkflowRecipeCompiler().compile(
            snapshot(partial_storage), context(project(tmp_path / "partial-storage"))
        )

    unknown = workflow_document()
    unknown["nodes"][0]["configuration"]["please_give_root"] = True
    with pytest.raises(WorkflowRecipeCompilerError, match="configuration"):
        WorkflowRecipeCompiler().compile(
            snapshot(unknown), context(project(tmp_path / "unknown"))
        )


def test_candidate_json_round_trip_from_compiler(tmp_path: Path) -> None:
    candidate = (
        WorkflowRecipeCompiler()
        .compile(snapshot(), context(project(tmp_path)))
        .recipes[0]
    )
    restored = CandidateRecipe.from_json(candidate.to_json())
    assert restored == candidate
