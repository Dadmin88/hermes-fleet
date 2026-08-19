"""Phase 15 private skill-learning authorization derived from exact RunAuthority."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .hermes_runs import (
    HermesFleetSkillLearningBinding,
    HermesSkillFilesystemNeed,
)
from .principal_identity import PrincipalRecord
from .run_capsule import RunCapsuleSpec


class SkillLearningError(RuntimeError):
    """Fleet cannot safely authorize scoped learned-skill candidate creation."""


@dataclass(frozen=True, slots=True)
class SkillLearningAuthorization:
    binding: HermesFleetSkillLearningBinding


def _secret_need_fingerprint(reference: str) -> str:
    return "sha256:" + hashlib.sha256(reference.encode("utf-8")).hexdigest()


def authorize_skill_learning(
    spec: RunCapsuleSpec,
    principal: PrincipalRecord,
) -> SkillLearningAuthorization:
    """Derive the Phase 15 learning envelope from exact current Fleet authority.

    The envelope is descriptive provenance only. It does not grant tools,
    filesystem access, network access, or secret access to a future run.
    Phase 15 always writes principal-private quarantined candidates.
    """
    if type(spec) is not RunCapsuleSpec:
        raise SkillLearningError("Run Capsule spec is invalid")
    if type(principal) is not PrincipalRecord:
        raise SkillLearningError("principal record is invalid")
    if principal.reference != spec.principal:
        raise SkillLearningError("principal record does not match Run Capsule")

    filesystem: list[HermesSkillFilesystemNeed] = []
    for grant in spec.filesystem_grants:
        mode = "read-only" if grant.mode == "read" else "read-write"
        try:
            filesystem.append(
                HermesSkillFilesystemNeed(
                    project_id=grant.project_id,
                    relative_path=grant.relative_path,
                    target=grant.target,
                    mode=mode,
                    max_bytes=grant.max_bytes,
                )
            )
        except ValueError as error:
            raise SkillLearningError(
                "Run Capsule filesystem grant cannot be represented as skill metadata"
            ) from error

    try:
        binding = HermesFleetSkillLearningBinding(
            principal_id=spec.principal.principal_id,
            principal_kind=spec.principal.kind,
            principal_generation=spec.principal.generation,
            principal_binding_hash=spec.principal.binding_hash,
            agent_instance_id=spec.agent_instance_id,
            source_run=spec.execution_id,
            run_authority_hash=spec.run_authority_hash,
            recipe_hash=spec.recipe_hash,
            resolved_recipe_hash=spec.resolved_recipe_hash,
            plan_fingerprint=spec.plan_fingerprint,
            capabilities_hash=spec.capabilities_hash,
            target_digest=spec.target_digest,
            toolsets=spec.toolsets,
            filesystem_needs=tuple(filesystem),
            network_mode=spec.network_mode,
            network_policy_hash=spec.network_policy_hash,
            secret_need_fingerprints=tuple(
                _secret_need_fingerprint(reference) for reference in spec.secret_refs
            ),
        )
    except ValueError as error:
        raise SkillLearningError(
            "Run Capsule cannot be represented as a scoped skill-learning envelope"
        ) from error
    return SkillLearningAuthorization(binding=binding)


__all__ = [
    "SkillLearningAuthorization",
    "SkillLearningError",
    "authorize_skill_learning",
]
