from __future__ import annotations

from dataclasses import replace

import pytest

from hermes_fleet.recipe_requirements import (
    KNOWLEDGE_DECLARED,
    KNOWLEDGE_DERIVED,
    KNOWLEDGE_PROPOSED,
    KNOWLEDGE_UNKNOWN,
    CandidateRecipe,
    ExecutionObservation,
    GpuCapability,
    PlacementCapabilities,
    RecipeRequirement,
    RecipeRequirementError,
    RecipeResolutionCache,
    RequirementEvidence,
    ResolutionValidityInputs,
    ResolvedWorkflowRecipe,
    ValidatedRecipe,
    WorkflowBinding,
    evaluate_placement,
    propose_adaptive_revision,
)
from hermes_fleet.recipes import AgentRequirement, ResolvedAgencyProfile, _digest

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64
HASH_4 = "sha256:" + "4" * 64
HASH_5 = "sha256:" + "5" * 64
HASH_6 = "sha256:" + "6" * 64
WORKFLOW_HASH = "sha256:" + "a" * 64


def evidence(kind: str = "workflow", digest: str = HASH_1) -> RequirementEvidence:
    return RequirementEvidence(kind=kind, source=f"{kind}:source", digest=digest)


def requirement(
    key: str, value, *, state: str = KNOWLEDGE_DECLARED, mandatory: bool = True
):
    return RecipeRequirement(
        key=key,
        state=state,
        mandatory=mandatory,
        value=value,
        evidence=(evidence("workflow" if state == KNOWLEDGE_DECLARED else "project"),),
    )


def requirements() -> dict[str, RecipeRequirement]:
    return {
        "cpu": requirement("cpu", {"minimum": 500, "requested": 1000, "limit": 2000}),
        "memory": requirement(
            "memory",
            {
                "minimum": 536_870_912,
                "requested": 1_073_741_824,
                "limit": 2_147_483_648,
            },
        ),
        "swap": requirement("swap", "disabled", state=KNOWLEDGE_DERIVED),
        "pids": requirement("pids", 128, state=KNOWLEDGE_DERIVED),
        "gpu": requirement(
            "gpu",
            {
                "mode": "required",
                "count": 1,
                "vendor": "nvidia",
                "class": "compute",
                "minimum_vram_bytes": 8_589_934_592,
                "features": ["cuda", "fp16"],
            },
        ),
        "platform": requirement(
            "platform", {"os": ["linux"], "architectures": ["x86_64"]}
        ),
        "runtime": requirement(
            "runtime",
            {
                "image": "example.invalid/toolbox@sha256:" + "b" * 64,
                "toolchains": ["python-3.12", "cuda-13"],
            },
        ),
        "storage": requirement(
            "storage",
            {
                "workspace_bytes": 10_737_418_240,
                "tmp_bytes": 2_147_483_648,
                "home_bytes": 1_073_741_824,
            },
        ),
        "io": requirement(
            "io",
            {"inputs": ["project"], "outputs": ["build"], "artifacts": ["bundle"]},
        ),
        "filesystem": requirement(
            "filesystem", {"mode": "read-only", "paths": ["project"]}
        ),
        "network": requirement(
            "network",
            {"mode": "none", "dns": [], "allowlist": []},
            state=KNOWLEDGE_DERIVED,
        ),
        "toolsets": requirement(
            "toolsets", ["fleet-terminal"], state=KNOWLEDGE_DERIVED
        ),
        "secrets": requirement("secrets", [], state=KNOWLEDGE_DERIVED, mandatory=False),
        "host_operations": requirement(
            "host_operations", [], state=KNOWLEDGE_DERIVED, mandatory=False
        ),
        "execution": requirement(
            "execution", {"deadline_ms": 900_000, "max_iterations": 8}
        ),
        "placement": requirement(
            "placement", {"capabilities": ["docker"], "labels": ["build"]}
        ),
    }


def candidate() -> CandidateRecipe:
    return CandidateRecipe(
        workflow=WorkflowBinding(
            workflow_id="workflow-test",
            revision=7,
            content_hash=WORKFLOW_HASH,
            step_id="build",
        ),
        compiler_version="fleet.workflow-recipe-compiler.v1",
        derivation_inputs_digest=HASH_2,
        agent=AgentRequirement(
            kind="agency_profile", name="developer", version=">=1,<2"
        ),
        requirements=requirements(),
        dependencies=("prepare",),
    )


def resolved_agent() -> ResolvedAgencyProfile:
    return ResolvedAgencyProfile(
        repository="https://example.invalid/agency.git",
        revision="c" * 40,
        name="developer",
        version="1.4.0",
        content_digest=HASH_3,
    )


def validity(**changes) -> ResolutionValidityInputs:
    values = {
        "workflow_hash": WORKFLOW_HASH,
        "project_fingerprint": HASH_1,
        "agency_fingerprint": _digest(resolved_agent().to_dict()),
        "runtime_fingerprint": HASH_3,
        "policy_fingerprint": HASH_4,
        "capabilities_fingerprint": HASH_5,
        "compiler_version": "fleet.workflow-recipe-compiler.v1",
    }
    values.update(changes)
    return ResolutionValidityInputs(**values)


def placement(*, gpu_vram: int = 12_884_901_888) -> PlacementCapabilities:
    return PlacementCapabilities(
        os="linux",
        architecture="x86_64",
        cpu_millis=4000,
        memory_bytes=8_589_934_592,
        pids_limit=512,
        workspace_bytes=21_474_836_480,
        tmp_bytes=4_294_967_296,
        home_bytes=2_147_483_648,
        gpus=(
            GpuCapability(
                vendor="nvidia",
                gpu_class="compute",
                vram_bytes=gpu_vram,
                features=("cuda", "fp16", "tensor"),
            ),
        ),
        toolchains=("python-3.12", "cuda-13"),
        capabilities=("docker",),
        labels=("build",),
    )


def test_candidate_validated_resolved_round_trip_preserves_requirements() -> None:
    initial = candidate()
    restored = CandidateRecipe.from_json(initial.to_json())
    assert restored == initial
    assert restored.content_hash == initial.content_hash
    assert restored.requirements["cpu"].value == {
        "minimum": 500,
        "requested": 1000,
        "limit": 2000,
    }
    assert restored.requirements["storage"].value["workspace_bytes"] == 10_737_418_240
    assert restored.requirements["gpu"].value["minimum_vram_bytes"] == 8_589_934_592
    assert restored.requirements["gpu"].evidence[0].kind == "workflow"

    validated = ValidatedRecipe.from_candidate(restored)
    resolved = ResolvedWorkflowRecipe.from_validated(
        validated, agent=resolved_agent(), validity_inputs=validity()
    )
    round_tripped = ResolvedWorkflowRecipe.from_dict(resolved.to_dict())
    assert round_tripped == resolved
    assert round_tripped.recipe_hash == validated.content_hash
    assert round_tripped.content_hash.startswith("sha256:")
    capsule = round_tripped.run_capsule_identity()
    assert capsule == {
        "recipe_hash": validated.content_hash,
        "resolved_recipe_hash": round_tripped.content_hash,
        "recipe_compiler_version": "fleet.workflow-recipe-compiler.v1",
        "requirement_provenance_digest": round_tripped.requirement_provenance_digest,
        "workflow_id": "workflow-test",
        "workflow_revision": 7,
        "workflow_hash": WORKFLOW_HASH,
        "workflow_step_id": "build",
    }


def test_validated_and_resolved_hash_claims_are_recomputed_and_fail_closed() -> None:
    validated = ValidatedRecipe.from_candidate(candidate())
    bad_validated = validated.to_dict()
    bad_validated["candidate_hash"] = HASH_6
    with pytest.raises(RecipeRequirementError, match="Candidate hash"):
        ValidatedRecipe.from_dict(bad_validated)

    resolved = ResolvedWorkflowRecipe.from_validated(
        validated, agent=resolved_agent(), validity_inputs=validity()
    )
    bad_resolved = resolved.to_dict()
    bad_resolved["validated_hash"] = HASH_6
    with pytest.raises(RecipeRequirementError, match="Validated hash"):
        ResolvedWorkflowRecipe.from_dict(bad_resolved)

    wrong_agent = replace(resolved_agent(), version="2.0.0")
    with pytest.raises(RecipeRequirementError, match="does not satisfy"):
        ResolvedWorkflowRecipe.from_validated(
            validated,
            agent=wrong_agent,
            validity_inputs=replace(
                validity(),
                agency_fingerprint=_digest(wrong_agent.to_dict()),
            ),
        )

    with pytest.raises(RecipeRequirementError, match="Workflow hash"):
        ResolvedWorkflowRecipe.from_validated(
            validated,
            agent=resolved_agent(),
            validity_inputs=replace(validity(), workflow_hash=HASH_6),
        )

    no_exact_image = candidate().replace_requirement(
        requirement(
            "runtime",
            {"image": None, "toolchains": ["python-3.12"]},
            state=KNOWLEDGE_DERIVED,
        )
    )
    validated_without_image = ValidatedRecipe.from_candidate(no_exact_image)
    with pytest.raises(RecipeRequirementError, match="runtime image"):
        ResolvedWorkflowRecipe.from_validated(
            validated_without_image,
            agent=resolved_agent(),
            validity_inputs=validity(),
        )


def test_unknown_mandatory_and_proposed_requirements_cannot_validate_or_resolve() -> (
    None
):
    initial = candidate().replace_requirement(
        RecipeRequirement.unknown("memory", mandatory=True)
    )
    assert initial.unresolved_mandatory == ("memory",)
    with pytest.raises(RecipeRequirementError, match="mandatory"):
        ValidatedRecipe.from_candidate(initial)

    proposal = RecipeRequirement(
        key="network",
        state=KNOWLEDGE_PROPOSED,
        mandatory=True,
        value={
            "mode": "project-allowlist",
            "dns": [],
            "allowlist": ["registry.example"],
        },
        evidence=(evidence("model", HASH_6),),
    )
    proposed = candidate().replace_requirement(proposal)
    assert proposed.has_untrusted_proposals is True
    with pytest.raises(RecipeRequirementError, match="proposals"):
        ValidatedRecipe.from_candidate(proposed)


def test_gpu_placement_is_deterministic_and_fails_when_vram_is_insufficient() -> None:
    validated = ValidatedRecipe.from_candidate(candidate())
    assert evaluate_placement(validated, placement()).eligible is True
    insufficient = evaluate_placement(validated, placement(gpu_vram=4_294_967_296))
    assert insufficient.eligible is False
    assert insufficient.reasons == ("gpu_insufficient",)


def test_cache_reuse_requires_exact_validity_inputs() -> None:
    validated = ValidatedRecipe.from_candidate(candidate())
    inputs = validity()
    resolved = ResolvedWorkflowRecipe.from_validated(
        validated, agent=resolved_agent(), validity_inputs=inputs
    )
    cache = RecipeResolutionCache()
    cache.put(inputs, resolved)
    assert cache.get(inputs) == resolved
    assert cache.get(validity(project_fingerprint=HASH_6)) is None
    assert cache.get(validity(policy_fingerprint=HASH_6)) is None
    assert cache.get(validity(capabilities_fingerprint=HASH_6)) is None


def test_failed_execution_returns_proposal_without_mutating_current_recipe() -> None:
    initial = candidate()
    observed = ExecutionObservation(kind="oom", evidence_digest=HASH_6, details={})
    proposed = propose_adaptive_revision(initial, observed)
    assert initial.requirements["memory"].state == KNOWLEDGE_DECLARED
    assert initial.requirements["memory"].value["requested"] == 1_073_741_824
    assert proposed.requirements["memory"].state == KNOWLEDGE_PROPOSED
    assert proposed.requirements["memory"].value["requested"] == 2_147_483_648
    assert proposed.content_hash != initial.content_hash
    with pytest.raises(RecipeRequirementError, match="proposals"):
        ValidatedRecipe.from_candidate(proposed)


def test_requirement_shapes_fail_closed() -> None:
    with pytest.raises(RecipeRequirementError, match="range"):
        requirement("cpu", {"minimum": 2000, "requested": 1000, "limit": 4000})
    with pytest.raises(RecipeRequirementError, match="GPU none-mode"):
        requirement(
            "gpu",
            {
                "mode": "none",
                "count": 1,
                "vendor": None,
                "class": None,
                "minimum_vram_bytes": 0,
                "features": [],
            },
        )
    with pytest.raises(RecipeRequirementError, match="digest-pinned"):
        requirement("runtime", {"image": "ubuntu:latest", "toolchains": []})


def test_optional_unknown_is_candidate_valid_but_cannot_be_resolved() -> None:
    initial = candidate().replace_requirement(
        RecipeRequirement.unknown("gpu", mandatory=False)
    )
    validated = ValidatedRecipe.from_candidate(initial)
    assert validated.requirements["gpu"].state == KNOWLEDGE_UNKNOWN
    with pytest.raises(RecipeRequirementError, match="unknown"):
        ResolvedWorkflowRecipe.from_validated(
            validated, agent=resolved_agent(), validity_inputs=validity()
        )
