"""Phase 8A backend-neutral Recipe requirements, provenance, placement and reuse."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Final

from .recipes import (
    AgentRequirement,
    ResolvedAgencyProfile,
    _canonical,
    _digest,
    _exact_object,
    _json_value,
    _load,
    _name,
    _plain,
    _positive_int,
    _text,
)

_CANDIDATE_SCHEMA: Final[str] = "fleet.candidate-recipe.v1"
_VALIDATED_SCHEMA: Final[str] = "fleet.validated-recipe.v1"
_RESOLVED_SCHEMA: Final[str] = "fleet.resolved-workflow-recipe.v1"

KNOWLEDGE_DECLARED = "declared"
KNOWLEDGE_DERIVED = "derived"
KNOWLEDGE_DISCOVERED = "discovered"
KNOWLEDGE_PROPOSED = "proposed"
KNOWLEDGE_UNKNOWN = "unknown"
_KNOWLEDGE_STATES = {
    KNOWLEDGE_DECLARED,
    KNOWLEDGE_DERIVED,
    KNOWLEDGE_DISCOVERED,
    KNOWLEDGE_PROPOSED,
    KNOWLEDGE_UNKNOWN,
}

REQUIREMENT_CPU = "cpu"
REQUIREMENT_MEMORY = "memory"
REQUIREMENT_SWAP = "swap"
REQUIREMENT_PIDS = "pids"
REQUIREMENT_GPU = "gpu"
REQUIREMENT_PLATFORM = "platform"
REQUIREMENT_RUNTIME = "runtime"
REQUIREMENT_STORAGE = "storage"
REQUIREMENT_IO = "io"
REQUIREMENT_FILESYSTEM = "filesystem"
REQUIREMENT_NETWORK = "network"
REQUIREMENT_TOOLSETS = "toolsets"
REQUIREMENT_SECRETS = "secrets"
REQUIREMENT_HOST_OPERATIONS = "host_operations"
REQUIREMENT_EXECUTION = "execution"
REQUIREMENT_PLACEMENT = "placement"

REQUIREMENT_KEYS: Final[tuple[str, ...]] = (
    REQUIREMENT_CPU,
    REQUIREMENT_MEMORY,
    REQUIREMENT_SWAP,
    REQUIREMENT_PIDS,
    REQUIREMENT_GPU,
    REQUIREMENT_PLATFORM,
    REQUIREMENT_RUNTIME,
    REQUIREMENT_STORAGE,
    REQUIREMENT_IO,
    REQUIREMENT_FILESYSTEM,
    REQUIREMENT_NETWORK,
    REQUIREMENT_TOOLSETS,
    REQUIREMENT_SECRETS,
    REQUIREMENT_HOST_OPERATIONS,
    REQUIREMENT_EXECUTION,
    REQUIREMENT_PLACEMENT,
)

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION_CLAUSE_RE = re.compile(r"^(>=|<=|==|>|<)?(\d+(?:\.\d+){0,3})$")
_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./_-]{0,254}@sha256:[0-9a-f]{64})$"
)
_MAX_REQUIREMENT_EVIDENCE = 16
_MAX_LIST = 64
_MAX_INT = (1 << 63) - 1


class RecipeRequirementError(ValueError):
    """A Phase 8A Recipe requirement or resolution contract is invalid."""


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise RecipeRequirementError(f"{label} is invalid")
    return value


def _workflow_hash(value: object) -> str:
    return _hash(value, "Workflow content hash")


def _workflow_identifier(value: object, label: str) -> str:
    if type(value) is not str or _WORKFLOW_ID_RE.fullmatch(value) is None:
        raise RecipeRequirementError(f"{label} is invalid")
    return value


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    match = _VERSION_CLAUSE_RE.fullmatch(value)
    if match is None or match.group(1) is not None:
        raise RecipeRequirementError("Agency version requirement is unsupported")
    parts = tuple(int(part) for part in match.group(2).split("."))
    return (*parts, *(0 for _ in range(4 - len(parts))))


def _version_satisfies(requirement: str, exact: str) -> bool:
    if type(requirement) is not str or type(exact) is not str:
        return False
    try:
        exact_tuple = _version_tuple(exact)
    except RecipeRequirementError:
        return requirement == exact
    clauses = tuple(part.strip() for part in requirement.split(",") if part.strip())
    if not clauses:
        return False
    for clause in clauses:
        match = _VERSION_CLAUSE_RE.fullmatch(clause)
        if match is None:
            return requirement == exact
        operator = match.group(1) or "=="
        target = _version_tuple(match.group(2))
        accepted = {
            "==": exact_tuple == target,
            ">=": exact_tuple >= target,
            "<=": exact_tuple <= target,
            ">": exact_tuple > target,
            "<": exact_tuple < target,
        }[operator]
        if not accepted:
            return False
    return True


def _nonnegative_int(value: object, label: str, *, maximum: int = _MAX_INT) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 <= value <= maximum:
        raise RecipeRequirementError(f"{label} is invalid")
    return value


def _bounded_list(
    value: object, label: str, *, maximum: int = _MAX_LIST
) -> tuple[str, ...]:
    if type(value) not in {list, tuple}:
        raise RecipeRequirementError(f"{label} is invalid")
    items = tuple(_name(item, label) for item in value)
    if len(items) > maximum or len(items) != len(set(items)):
        raise RecipeRequirementError(f"{label} is invalid")
    return items


def _bounded_text_list(
    value: object, label: str, *, maximum: int = _MAX_LIST, item_maximum: int = 512
) -> tuple[str, ...]:
    if type(value) not in {list, tuple}:
        raise RecipeRequirementError(f"{label} is invalid")
    items = tuple(_text(item, label, maximum=item_maximum) for item in value)
    if len(items) > maximum or len(items) != len(set(items)):
        raise RecipeRequirementError(f"{label} is invalid")
    return items


def _plain_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(_plain(dict(value)))


def _range_value(value: object, label: str) -> dict[str, int]:
    item = _exact_object(value, {"minimum", "requested", "limit"}, label)
    minimum = _positive_int(item["minimum"], f"{label} minimum")
    requested = _positive_int(item["requested"], f"{label} requested")
    limit = _positive_int(item["limit"], f"{label} limit")
    if not minimum <= requested <= limit:
        raise RecipeRequirementError(f"{label} range is invalid")
    return {"minimum": minimum, "requested": requested, "limit": limit}


def _gpu_value(value: object) -> dict[str, Any]:
    item = _exact_object(
        value,
        {"mode", "count", "vendor", "class", "minimum_vram_bytes", "features"},
        "GPU requirement",
    )
    mode = _text(item["mode"], "GPU mode", maximum=16)
    if mode not in {"none", "optional", "required"}:
        raise RecipeRequirementError("GPU mode is invalid")
    count = _nonnegative_int(item["count"], "GPU count", maximum=64)
    vendor = item["vendor"]
    gpu_class = item["class"]
    if vendor is not None:
        vendor = _name(vendor, "GPU vendor")
    if gpu_class is not None:
        gpu_class = _name(gpu_class, "GPU class")
    minimum_vram = _nonnegative_int(item["minimum_vram_bytes"], "GPU VRAM")
    features = _bounded_list(item["features"], "GPU feature")
    if mode == "none" and (
        count != 0
        or vendor is not None
        or gpu_class is not None
        or minimum_vram != 0
        or features
    ):
        raise RecipeRequirementError(
            "GPU none-mode cannot carry accelerator constraints"
        )
    if mode == "required" and count < 1:
        raise RecipeRequirementError("required GPU count must be positive")
    return {
        "mode": mode,
        "count": count,
        "vendor": vendor,
        "class": gpu_class,
        "minimum_vram_bytes": minimum_vram,
        "features": list(features),
    }


def _platform_value(value: object) -> dict[str, Any]:
    item = _exact_object(value, {"os", "architectures"}, "platform requirement")
    operating_systems = _bounded_list(item["os"], "operating system")
    architectures = _bounded_list(item["architectures"], "architecture")
    if not operating_systems or not architectures:
        raise RecipeRequirementError("platform requirement cannot be empty")
    return {"os": list(operating_systems), "architectures": list(architectures)}


def _runtime_value(value: object) -> dict[str, Any]:
    item = _exact_object(value, {"image", "toolchains"}, "runtime requirement")
    image = item["image"]
    if image is not None:
        image = _text(image, "runtime image", maximum=512)
        if _IMAGE_RE.fullmatch(image) is None:
            raise RecipeRequirementError("runtime image must be digest-pinned")
    toolchains = _bounded_text_list(
        item["toolchains"], "runtime toolchain", item_maximum=128
    )
    return {"image": image, "toolchains": list(toolchains)}


def _storage_value(value: object) -> dict[str, int]:
    item = _exact_object(
        value, {"workspace_bytes", "tmp_bytes", "home_bytes"}, "storage requirement"
    )
    return {
        "workspace_bytes": _positive_int(item["workspace_bytes"], "workspace capacity"),
        "tmp_bytes": _positive_int(item["tmp_bytes"], "tmp capacity"),
        "home_bytes": _positive_int(item["home_bytes"], "home capacity"),
    }


def _io_value(value: object) -> dict[str, Any]:
    item = _exact_object(value, {"inputs", "outputs", "artifacts"}, "I/O requirement")
    return {
        "inputs": list(_bounded_text_list(item["inputs"], "input requirement")),
        "outputs": list(_bounded_text_list(item["outputs"], "output requirement")),
        "artifacts": list(
            _bounded_text_list(item["artifacts"], "artifact requirement")
        ),
    }


def _filesystem_value(value: object) -> dict[str, Any]:
    item = _exact_object(value, {"mode", "paths"}, "filesystem requirement")
    mode = _text(item["mode"], "filesystem mode", maximum=32)
    if mode not in {"read-only", "project-write"}:
        raise RecipeRequirementError("filesystem mode is invalid")
    paths = _bounded_text_list(item["paths"], "filesystem path", item_maximum=1024)
    return {"mode": mode, "paths": list(paths)}


def _network_value(value: object) -> dict[str, Any]:
    item = _exact_object(value, {"mode", "dns", "allowlist"}, "network requirement")
    mode = _text(item["mode"], "network mode", maximum=32)
    if mode not in {"none", "provider-only", "project-allowlist", "internet-approved"}:
        raise RecipeRequirementError("network mode is invalid")
    dns = _bounded_text_list(item["dns"], "DNS requirement", item_maximum=255)
    allowlist = _bounded_text_list(
        item["allowlist"], "network allowlist", item_maximum=512
    )
    if mode in {"none", "provider-only"} and allowlist:
        raise RecipeRequirementError(
            "offline/provider-only network cannot carry direct allowlist"
        )
    return {"mode": mode, "dns": list(dns), "allowlist": list(allowlist)}


def _execution_value(value: object) -> dict[str, int]:
    item = _exact_object(
        value, {"deadline_ms", "max_iterations"}, "execution requirement"
    )
    deadline = _positive_int(item["deadline_ms"], "execution deadline")
    iterations = _positive_int(item["max_iterations"], "iteration limit")
    if iterations > 32:
        raise RecipeRequirementError("iteration limit is invalid")
    return {"deadline_ms": deadline, "max_iterations": iterations}


def _placement_value(value: object) -> dict[str, Any]:
    item = _exact_object(value, {"capabilities", "labels"}, "placement requirement")
    return {
        "capabilities": list(
            _bounded_list(item["capabilities"], "placement capability")
        ),
        "labels": list(_bounded_list(item["labels"], "placement label")),
    }


def _validate_requirement_value(key: str, value: object) -> Any:
    if key in {REQUIREMENT_CPU, REQUIREMENT_MEMORY}:
        return _range_value(value, key)
    if key == REQUIREMENT_SWAP:
        policy = _text(value, "swap policy", maximum=32)
        if policy not in {"disabled", "bounded", "backend-policy"}:
            raise RecipeRequirementError("swap policy is invalid")
        return policy
    if key == REQUIREMENT_PIDS:
        return _positive_int(value, "PID limit")
    if key == REQUIREMENT_GPU:
        return _gpu_value(value)
    if key == REQUIREMENT_PLATFORM:
        return _platform_value(value)
    if key == REQUIREMENT_RUNTIME:
        return _runtime_value(value)
    if key == REQUIREMENT_STORAGE:
        return _storage_value(value)
    if key == REQUIREMENT_IO:
        return _io_value(value)
    if key == REQUIREMENT_FILESYSTEM:
        return _filesystem_value(value)
    if key == REQUIREMENT_NETWORK:
        return _network_value(value)
    if key == REQUIREMENT_TOOLSETS:
        return list(_bounded_list(value, "toolset"))
    if key == REQUIREMENT_SECRETS:
        return list(_bounded_list(value, "symbolic secret requirement"))
    if key == REQUIREMENT_HOST_OPERATIONS:
        return list(_bounded_list(value, "host operation"))
    if key == REQUIREMENT_EXECUTION:
        return _execution_value(value)
    if key == REQUIREMENT_PLACEMENT:
        return _placement_value(value)
    raise RecipeRequirementError("unsupported Recipe requirement")


@dataclass(frozen=True, slots=True)
class RequirementEvidence:
    kind: str
    source: str
    digest: str

    def __post_init__(self) -> None:
        if self.kind not in {
            "workflow",
            "project",
            "agency",
            "probe",
            "model",
            "execution",
            "policy",
            "cache",
        }:
            raise RecipeRequirementError("requirement evidence kind is invalid")
        _text(self.source, "requirement evidence source", maximum=512)
        _hash(self.digest, "requirement evidence digest")

    @classmethod
    def from_dict(cls, value: object) -> RequirementEvidence:
        item = _exact_object(
            value, {"kind", "source", "digest"}, "requirement evidence"
        )
        return cls(kind=item["kind"], source=item["source"], digest=item["digest"])

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "source": self.source, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class RecipeRequirement:
    key: str
    state: str
    mandatory: bool
    value: Any | None
    evidence: tuple[RequirementEvidence, ...] = ()

    def __post_init__(self) -> None:
        if self.key not in REQUIREMENT_KEYS:
            raise RecipeRequirementError("Recipe requirement key is invalid")
        if self.state not in _KNOWLEDGE_STATES:
            raise RecipeRequirementError("Recipe knowledge state is invalid")
        if type(self.mandatory) is not bool:
            raise RecipeRequirementError("Recipe requirement mandatory flag is invalid")
        if (
            type(self.evidence) is not tuple
            or len(self.evidence) > _MAX_REQUIREMENT_EVIDENCE
        ):
            raise RecipeRequirementError("Recipe requirement evidence is invalid")
        if any(type(item) is not RequirementEvidence for item in self.evidence):
            raise RecipeRequirementError("Recipe requirement evidence is invalid")
        if self.state == KNOWLEDGE_UNKNOWN:
            if self.value is not None or self.evidence:
                raise RecipeRequirementError(
                    "unknown Recipe requirement cannot carry value/evidence"
                )
            return
        if self.value is None or not self.evidence:
            raise RecipeRequirementError(
                "known Recipe requirement requires value/evidence"
            )
        normalized = _validate_requirement_value(self.key, self.value)
        object.__setattr__(self, "value", _json_value(normalized))
        if self.state == KNOWLEDGE_PROPOSED and not any(
            item.kind in {"model", "execution"} for item in self.evidence
        ):
            raise RecipeRequirementError(
                "proposed Recipe requirement lacks proposal evidence"
            )

    @classmethod
    def unknown(cls, key: str, *, mandatory: bool = True) -> RecipeRequirement:
        return cls(key=key, state=KNOWLEDGE_UNKNOWN, mandatory=mandatory, value=None)

    @classmethod
    def from_dict(cls, value: object) -> RecipeRequirement:
        item = _exact_object(
            value,
            {"key", "state", "mandatory", "value", "evidence"},
            "Recipe requirement",
        )
        evidence = item["evidence"]
        if type(evidence) is not list:
            raise RecipeRequirementError("Recipe requirement evidence is invalid")
        return cls(
            key=item["key"],
            state=item["state"],
            mandatory=item["mandatory"],
            value=item["value"],
            evidence=tuple(RequirementEvidence.from_dict(entry) for entry in evidence),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "state": self.state,
            "mandatory": self.mandatory,
            "value": _plain(self.value),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class WorkflowBinding:
    workflow_id: str
    revision: int
    content_hash: str
    step_id: str

    def __post_init__(self) -> None:
        _workflow_identifier(self.workflow_id, "Workflow ID")
        _positive_int(self.revision, "Workflow revision")
        _workflow_hash(self.content_hash)
        _workflow_identifier(self.step_id, "Workflow step ID")

    @classmethod
    def from_dict(cls, value: object) -> WorkflowBinding:
        item = _exact_object(
            value,
            {"workflow_id", "revision", "content_hash", "step_id"},
            "Workflow binding",
        )
        return cls(
            workflow_id=item["workflow_id"],
            revision=item["revision"],
            content_hash=item["content_hash"],
            step_id=item["step_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "step_id": self.step_id,
        }


@dataclass(frozen=True, slots=True)
class CandidateRecipe:
    workflow: WorkflowBinding
    compiler_version: str
    derivation_inputs_digest: str
    agent: AgentRequirement
    requirements: Mapping[str, RecipeRequirement]
    dependencies: tuple[str, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.workflow) is not WorkflowBinding:
            raise RecipeRequirementError("Candidate Recipe Workflow binding is invalid")
        _name(self.compiler_version, "Recipe compiler version")
        _hash(self.derivation_inputs_digest, "Recipe derivation inputs digest")
        if type(self.agent) is not AgentRequirement:
            raise RecipeRequirementError(
                "Candidate Recipe Agent requirement is invalid"
            )
        if not isinstance(self.requirements, Mapping) or set(self.requirements) != set(
            REQUIREMENT_KEYS
        ):
            raise RecipeRequirementError("Candidate Recipe requirements are incomplete")
        normalized: dict[str, RecipeRequirement] = {}
        for key in REQUIREMENT_KEYS:
            requirement = self.requirements[key]
            if type(requirement) is not RecipeRequirement or requirement.key != key:
                raise RecipeRequirementError(
                    "Candidate Recipe requirement binding is invalid"
                )
            normalized[key] = requirement
        object.__setattr__(self, "requirements", MappingProxyType(normalized))
        dependencies = tuple(
            _workflow_identifier(item, "Workflow dependency")
            for item in self.dependencies
        )
        if len(dependencies) > 256 or len(dependencies) != len(set(dependencies)):
            raise RecipeRequirementError("Workflow dependency set is invalid")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "extensions", _plain_mapping(self.extensions))
        _canonical(self.to_dict())

    @property
    def unresolved_mandatory(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in REQUIREMENT_KEYS
            if self.requirements[key].mandatory
            and self.requirements[key].state in {KNOWLEDGE_UNKNOWN, KNOWLEDGE_PROPOSED}
        )

    @property
    def has_untrusted_proposals(self) -> bool:
        return any(
            item.state == KNOWLEDGE_PROPOSED for item in self.requirements.values()
        )

    @property
    def discovery_enabled(self) -> bool:
        configuration = self.extensions.get("dev.hermes.fleet.discovery")
        if configuration is None:
            return True
        if type(configuration) is not dict or set(configuration) != {"enabled"}:
            raise RecipeRequirementError(
                "Candidate Recipe discovery extension is invalid"
            )
        if type(configuration["enabled"]) is not bool:
            raise RecipeRequirementError("Candidate Recipe discovery flag is invalid")
        return configuration["enabled"]

    @classmethod
    def from_dict(cls, value: object) -> CandidateRecipe:
        item = _exact_object(
            value,
            {
                "schema",
                "workflow",
                "compiler_version",
                "derivation_inputs_digest",
                "agent",
                "requirements",
                "dependencies",
                "extensions",
            },
            "CandidateRecipe",
        )
        if item["schema"] != _CANDIDATE_SCHEMA:
            raise RecipeRequirementError("CandidateRecipe schema is unsupported")
        requirements = item["requirements"]
        if type(requirements) is not dict:
            raise RecipeRequirementError("Candidate Recipe requirements are invalid")
        return cls(
            workflow=WorkflowBinding.from_dict(item["workflow"]),
            compiler_version=item["compiler_version"],
            derivation_inputs_digest=item["derivation_inputs_digest"],
            agent=AgentRequirement.from_dict(item["agent"]),
            requirements={
                key: RecipeRequirement.from_dict(entry)
                for key, entry in requirements.items()
            },
            dependencies=tuple(item["dependencies"]),
            extensions=item["extensions"],
        )

    @classmethod
    def from_json(cls, payload: str) -> CandidateRecipe:
        return cls.from_dict(_load(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _CANDIDATE_SCHEMA,
            "workflow": self.workflow.to_dict(),
            "compiler_version": self.compiler_version,
            "derivation_inputs_digest": self.derivation_inputs_digest,
            "agent": self.agent.to_dict(),
            "requirements": {
                key: self.requirements[key].to_dict() for key in REQUIREMENT_KEYS
            },
            "dependencies": list(self.dependencies),
            "extensions": _plain(dict(self.extensions)),
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8")

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict())

    def replace_requirement(self, requirement: RecipeRequirement) -> CandidateRecipe:
        if type(requirement) is not RecipeRequirement:
            raise RecipeRequirementError("replacement Recipe requirement is invalid")
        next_requirements = dict(self.requirements)
        next_requirements[requirement.key] = requirement
        return replace(self, requirements=next_requirements)


@dataclass(frozen=True, slots=True)
class ValidatedRecipe:
    candidate_hash: str
    workflow: WorkflowBinding
    compiler_version: str
    derivation_inputs_digest: str
    agent: AgentRequirement
    requirements: Mapping[str, RecipeRequirement]
    dependencies: tuple[str, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _hash(self.candidate_hash, "Candidate Recipe hash")
        candidate = CandidateRecipe(
            workflow=self.workflow,
            compiler_version=self.compiler_version,
            derivation_inputs_digest=self.derivation_inputs_digest,
            agent=self.agent,
            requirements=self.requirements,
            dependencies=self.dependencies,
            extensions=self.extensions,
        )
        if candidate.content_hash != self.candidate_hash:
            raise RecipeRequirementError("Validated Recipe Candidate hash changed")
        if candidate.has_untrusted_proposals:
            raise RecipeRequirementError(
                "model/execution proposals require deterministic validation"
            )
        if candidate.unresolved_mandatory:
            raise RecipeRequirementError(
                "mandatory Recipe requirements remain unresolved"
            )
        object.__setattr__(self, "requirements", candidate.requirements)
        object.__setattr__(self, "dependencies", candidate.dependencies)
        object.__setattr__(self, "extensions", candidate.extensions)

    @classmethod
    def from_candidate(cls, candidate: CandidateRecipe) -> ValidatedRecipe:
        if type(candidate) is not CandidateRecipe:
            raise RecipeRequirementError("Candidate Recipe is invalid")
        return cls(
            candidate_hash=candidate.content_hash,
            workflow=candidate.workflow,
            compiler_version=candidate.compiler_version,
            derivation_inputs_digest=candidate.derivation_inputs_digest,
            agent=candidate.agent,
            requirements=candidate.requirements,
            dependencies=candidate.dependencies,
            extensions=candidate.extensions,
        )

    @classmethod
    def from_dict(cls, value: object) -> ValidatedRecipe:
        item = _exact_object(
            value,
            {
                "schema",
                "candidate_hash",
                "workflow",
                "compiler_version",
                "derivation_inputs_digest",
                "agent",
                "requirements",
                "dependencies",
                "extensions",
            },
            "ValidatedRecipe",
        )
        if item["schema"] != _VALIDATED_SCHEMA:
            raise RecipeRequirementError("ValidatedRecipe schema is unsupported")
        requirements = item["requirements"]
        if type(requirements) is not dict:
            raise RecipeRequirementError("Validated Recipe requirements are invalid")
        return cls(
            candidate_hash=item["candidate_hash"],
            workflow=WorkflowBinding.from_dict(item["workflow"]),
            compiler_version=item["compiler_version"],
            derivation_inputs_digest=item["derivation_inputs_digest"],
            agent=AgentRequirement.from_dict(item["agent"]),
            requirements={
                key: RecipeRequirement.from_dict(entry)
                for key, entry in requirements.items()
            },
            dependencies=tuple(item["dependencies"]),
            extensions=item["extensions"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _VALIDATED_SCHEMA,
            "candidate_hash": self.candidate_hash,
            "workflow": self.workflow.to_dict(),
            "compiler_version": self.compiler_version,
            "derivation_inputs_digest": self.derivation_inputs_digest,
            "agent": self.agent.to_dict(),
            "requirements": {
                key: self.requirements[key].to_dict() for key in REQUIREMENT_KEYS
            },
            "dependencies": list(self.dependencies),
            "extensions": _plain(dict(self.extensions)),
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8")

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ResolutionValidityInputs:
    workflow_hash: str
    project_fingerprint: str
    agency_fingerprint: str
    runtime_fingerprint: str
    policy_fingerprint: str
    capabilities_fingerprint: str
    compiler_version: str

    def __post_init__(self) -> None:
        _workflow_hash(self.workflow_hash)
        for value, label in (
            (self.project_fingerprint, "project fingerprint"),
            (self.agency_fingerprint, "Agency fingerprint"),
            (self.runtime_fingerprint, "runtime fingerprint"),
            (self.policy_fingerprint, "policy fingerprint"),
            (self.capabilities_fingerprint, "capabilities fingerprint"),
        ):
            _hash(value, label)
        _name(self.compiler_version, "Recipe compiler version")

    def to_dict(self) -> dict[str, str]:
        return {
            "workflow_hash": self.workflow_hash,
            "project_fingerprint": self.project_fingerprint,
            "agency_fingerprint": self.agency_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "capabilities_fingerprint": self.capabilities_fingerprint,
            "compiler_version": self.compiler_version,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ResolvedWorkflowRecipe:
    validated_hash: str
    candidate_hash: str
    workflow: WorkflowBinding
    compiler_version: str
    derivation_inputs_digest: str
    resolution_inputs_digest: str
    agent_requirement: AgentRequirement
    agent: ResolvedAgencyProfile
    requirements: Mapping[str, RecipeRequirement]
    dependencies: tuple[str, ...] = ()
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _hash(self.validated_hash, "Validated Recipe hash")
        _hash(self.candidate_hash, "Candidate Recipe hash")
        _hash(self.resolution_inputs_digest, "Recipe resolution inputs digest")
        if type(self.agent_requirement) is not AgentRequirement:
            raise RecipeRequirementError("resolved Agency requirement is invalid")
        if type(self.agent) is not ResolvedAgencyProfile:
            raise RecipeRequirementError("resolved Agency profile is invalid")
        validated = ValidatedRecipe(
            candidate_hash=self.candidate_hash,
            workflow=self.workflow,
            compiler_version=self.compiler_version,
            derivation_inputs_digest=self.derivation_inputs_digest,
            agent=self.agent_requirement,
            requirements=self.requirements,
            dependencies=self.dependencies,
            extensions=self.extensions,
        )
        if validated.content_hash != self.validated_hash:
            raise RecipeRequirementError("Resolved Recipe Validated hash changed")
        runtime = validated.requirements[REQUIREMENT_RUNTIME].value
        if not isinstance(runtime, Mapping) or runtime.get("image") is None:
            raise RecipeRequirementError(
                "Resolved Recipe requires an exact digest-pinned runtime image"
            )
        if self.agent.name != self.agent_requirement.name or not _version_satisfies(
            self.agent_requirement.version,
            self.agent.version,
        ):
            raise RecipeRequirementError(
                "resolved Agency profile does not satisfy Recipe"
            )
        object.__setattr__(self, "requirements", validated.requirements)
        object.__setattr__(self, "dependencies", validated.dependencies)
        object.__setattr__(self, "extensions", validated.extensions)

    @property
    def recipe_hash(self) -> str:
        return self.validated_hash

    @property
    def requirement_provenance_digest(self) -> str:
        return _digest(
            {
                key: [
                    evidence.to_dict() for evidence in self.requirements[key].evidence
                ]
                for key in REQUIREMENT_KEYS
            }
        )

    def run_capsule_identity(self) -> dict[str, Any]:
        return {
            "recipe_hash": self.recipe_hash,
            "resolved_recipe_hash": self.content_hash,
            "recipe_compiler_version": self.compiler_version,
            "requirement_provenance_digest": self.requirement_provenance_digest,
            "workflow_id": self.workflow.workflow_id,
            "workflow_revision": self.workflow.revision,
            "workflow_hash": self.workflow.content_hash,
            "workflow_step_id": self.workflow.step_id,
        }

    @classmethod
    def from_validated(
        cls,
        validated: ValidatedRecipe,
        *,
        agent: ResolvedAgencyProfile,
        validity_inputs: ResolutionValidityInputs,
    ) -> ResolvedWorkflowRecipe:
        if type(validated) is not ValidatedRecipe:
            raise RecipeRequirementError("Validated Recipe is invalid")
        if type(validity_inputs) is not ResolutionValidityInputs:
            raise RecipeRequirementError("Recipe validity inputs are invalid")
        if validity_inputs.workflow_hash != validated.workflow.content_hash:
            raise RecipeRequirementError("Recipe validity Workflow hash changed")
        if validity_inputs.compiler_version != validated.compiler_version:
            raise RecipeRequirementError("Recipe validity compiler version changed")
        if validity_inputs.agency_fingerprint != _digest(agent.to_dict()):
            raise RecipeRequirementError("Recipe validity Agency fingerprint changed")
        if agent.name != validated.agent.name or not _version_satisfies(
            validated.agent.version, agent.version
        ):
            raise RecipeRequirementError(
                "resolved Agency profile does not satisfy Recipe"
            )
        for requirement in validated.requirements.values():
            if requirement.state == KNOWLEDGE_UNKNOWN:
                raise RecipeRequirementError(
                    "Resolved Recipe cannot retain unknown requirement"
                )
        return cls(
            validated_hash=validated.content_hash,
            candidate_hash=validated.candidate_hash,
            workflow=validated.workflow,
            compiler_version=validated.compiler_version,
            derivation_inputs_digest=validated.derivation_inputs_digest,
            resolution_inputs_digest=validity_inputs.content_hash,
            agent_requirement=validated.agent,
            agent=agent,
            requirements=validated.requirements,
            dependencies=validated.dependencies,
            extensions=validated.extensions,
        )

    @classmethod
    def from_dict(cls, value: object) -> ResolvedWorkflowRecipe:
        item = _exact_object(
            value,
            {
                "schema",
                "validated_hash",
                "candidate_hash",
                "workflow",
                "compiler_version",
                "derivation_inputs_digest",
                "resolution_inputs_digest",
                "agent_requirement",
                "agent",
                "requirements",
                "dependencies",
                "extensions",
            },
            "ResolvedWorkflowRecipe",
        )
        if item["schema"] != _RESOLVED_SCHEMA:
            raise RecipeRequirementError(
                "Resolved Workflow Recipe schema is unsupported"
            )
        requirements = item["requirements"]
        if type(requirements) is not dict:
            raise RecipeRequirementError(
                "Resolved Workflow Recipe requirements are invalid"
            )
        return cls(
            validated_hash=item["validated_hash"],
            candidate_hash=item["candidate_hash"],
            workflow=WorkflowBinding.from_dict(item["workflow"]),
            compiler_version=item["compiler_version"],
            derivation_inputs_digest=item["derivation_inputs_digest"],
            resolution_inputs_digest=item["resolution_inputs_digest"],
            agent_requirement=AgentRequirement.from_dict(item["agent_requirement"]),
            agent=ResolvedAgencyProfile.from_dict(item["agent"]),
            requirements={
                key: RecipeRequirement.from_dict(entry)
                for key, entry in requirements.items()
            },
            dependencies=tuple(item["dependencies"]),
            extensions=item["extensions"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _RESOLVED_SCHEMA,
            "validated_hash": self.validated_hash,
            "candidate_hash": self.candidate_hash,
            "workflow": self.workflow.to_dict(),
            "compiler_version": self.compiler_version,
            "derivation_inputs_digest": self.derivation_inputs_digest,
            "resolution_inputs_digest": self.resolution_inputs_digest,
            "agent_requirement": self.agent_requirement.to_dict(),
            "agent": self.agent.to_dict(),
            "requirements": {
                key: self.requirements[key].to_dict() for key in REQUIREMENT_KEYS
            },
            "dependencies": list(self.dependencies),
            "extensions": _plain(dict(self.extensions)),
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8")

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class GpuCapability:
    vendor: str
    gpu_class: str
    vram_bytes: int
    features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _name(self.vendor, "GPU vendor")
        _name(self.gpu_class, "GPU class")
        _positive_int(self.vram_bytes, "GPU VRAM")
        object.__setattr__(
            self, "features", _bounded_list(self.features, "GPU feature")
        )


@dataclass(frozen=True, slots=True)
class PlacementCapabilities:
    os: str
    architecture: str
    cpu_millis: int
    memory_bytes: int
    pids_limit: int
    workspace_bytes: int
    tmp_bytes: int
    home_bytes: int
    gpus: tuple[GpuCapability, ...] = ()
    toolchains: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _name(self.os, "placement operating system")
        _name(self.architecture, "placement architecture")
        _positive_int(self.cpu_millis, "placement CPU capacity")
        _positive_int(self.memory_bytes, "placement memory capacity")
        _positive_int(self.pids_limit, "placement PID capacity")
        _positive_int(self.workspace_bytes, "placement workspace capacity")
        _positive_int(self.tmp_bytes, "placement tmp capacity")
        _positive_int(self.home_bytes, "placement home capacity")
        if type(self.gpus) is not tuple or any(
            type(item) is not GpuCapability for item in self.gpus
        ):
            raise RecipeRequirementError("placement GPU capabilities are invalid")
        object.__setattr__(
            self,
            "toolchains",
            _bounded_text_list(
                self.toolchains, "placement toolchain", item_maximum=128
            ),
        )
        object.__setattr__(
            self,
            "capabilities",
            _bounded_list(self.capabilities, "placement capability"),
        )
        object.__setattr__(
            self, "labels", _bounded_list(self.labels, "placement label")
        )


@dataclass(frozen=True, slots=True)
class PlacementMatch:
    eligible: bool
    reasons: tuple[str, ...]


def evaluate_placement(
    recipe: ValidatedRecipe | ResolvedWorkflowRecipe,
    capabilities: PlacementCapabilities,
) -> PlacementMatch:
    if type(recipe) not in {ValidatedRecipe, ResolvedWorkflowRecipe}:
        raise TypeError("recipe must be a validated/resolved workflow Recipe")
    if type(capabilities) is not PlacementCapabilities:
        raise TypeError("capabilities must be PlacementCapabilities")
    requirements = recipe.requirements
    reasons: set[str] = set()

    cpu = requirements[REQUIREMENT_CPU]
    memory = requirements[REQUIREMENT_MEMORY]
    pids = requirements[REQUIREMENT_PIDS]
    platform = requirements[REQUIREMENT_PLATFORM]
    storage = requirements[REQUIREMENT_STORAGE]
    gpu = requirements[REQUIREMENT_GPU]
    runtime = requirements[REQUIREMENT_RUNTIME]
    placement = requirements[REQUIREMENT_PLACEMENT]

    if cpu.value is not None and capabilities.cpu_millis < cpu.value["requested"]:
        reasons.add("cpu_insufficient")
    if (
        memory.value is not None
        and capabilities.memory_bytes < memory.value["requested"]
    ):
        reasons.add("memory_insufficient")
    if pids.value is not None and capabilities.pids_limit < pids.value:
        reasons.add("pids_insufficient")
    if platform.value is not None:
        if capabilities.os not in platform.value["os"]:
            reasons.add("os_unsupported")
        if capabilities.architecture not in platform.value["architectures"]:
            reasons.add("architecture_unsupported")
    if storage.value is not None:
        if capabilities.workspace_bytes < storage.value["workspace_bytes"]:
            reasons.add("workspace_insufficient")
        if capabilities.tmp_bytes < storage.value["tmp_bytes"]:
            reasons.add("tmp_insufficient")
        if capabilities.home_bytes < storage.value["home_bytes"]:
            reasons.add("home_insufficient")
    if runtime.value is not None:
        missing_toolchains = set(runtime.value["toolchains"]) - set(
            capabilities.toolchains
        )
        if missing_toolchains:
            reasons.add("runtime_toolchain_unsupported")
    if placement.value is not None:
        if set(placement.value["capabilities"]) - set(capabilities.capabilities):
            reasons.add("placement_capability_missing")
        if set(placement.value["labels"]) - set(capabilities.labels):
            reasons.add("placement_label_missing")
    if gpu.value is not None and gpu.value["mode"] == "required":
        matching = [
            item
            for item in capabilities.gpus
            if (gpu.value["vendor"] is None or item.vendor == gpu.value["vendor"])
            and (gpu.value["class"] is None or item.gpu_class == gpu.value["class"])
            and item.vram_bytes >= gpu.value["minimum_vram_bytes"]
            and set(gpu.value["features"]).issubset(item.features)
        ]
        if len(matching) < gpu.value["count"]:
            reasons.add("gpu_insufficient")

    ordered = tuple(sorted(reasons))
    return PlacementMatch(eligible=not ordered, reasons=ordered)


class RecipeResolutionCache:
    """Exact-input cache; history never bypasses current validity inputs."""

    def __init__(self) -> None:
        self._entries: dict[str, ResolvedWorkflowRecipe] = {}

    def put(
        self, inputs: ResolutionValidityInputs, recipe: ResolvedWorkflowRecipe
    ) -> None:
        if (
            type(inputs) is not ResolutionValidityInputs
            or type(recipe) is not ResolvedWorkflowRecipe
        ):
            raise RecipeRequirementError("Recipe cache input is invalid")
        if recipe.resolution_inputs_digest != inputs.content_hash:
            raise RecipeRequirementError("Recipe cache validity binding changed")
        self._entries[inputs.content_hash] = recipe

    def get(self, inputs: ResolutionValidityInputs) -> ResolvedWorkflowRecipe | None:
        if type(inputs) is not ResolutionValidityInputs:
            raise RecipeRequirementError("Recipe cache input is invalid")
        return self._entries.get(inputs.content_hash)


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    kind: str
    evidence_digest: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in {
            "oom",
            "disk_exhausted",
            "network_denied",
            "missing_accelerator",
            "missing_runtime",
            "resource_saturation",
        }:
            raise RecipeRequirementError("execution observation kind is invalid")
        _hash(self.evidence_digest, "execution observation digest")
        object.__setattr__(self, "details", _plain_mapping(self.details))


def propose_adaptive_revision(
    candidate: CandidateRecipe,
    observation: ExecutionObservation,
) -> CandidateRecipe:
    """Return a proposal only. The current Recipe/RunAuthority is never mutated."""
    if (
        type(candidate) is not CandidateRecipe
        or type(observation) is not ExecutionObservation
    ):
        raise RecipeRequirementError("adaptive Recipe input is invalid")
    evidence = RequirementEvidence(
        kind="execution",
        source=f"execution:{observation.kind}",
        digest=observation.evidence_digest,
    )
    if observation.kind in {"oom", "resource_saturation"}:
        current = candidate.requirements[REQUIREMENT_MEMORY]
        if current.value is None:
            raise RecipeRequirementError("memory proposal requires a known baseline")
        current_range = dict(current.value)
        proposed_requested = min(
            _MAX_INT,
            max(current_range["requested"] + 1, current_range["requested"] * 2),
        )
        proposed_limit = min(_MAX_INT, max(current_range["limit"], proposed_requested))
        requirement = RecipeRequirement(
            key=REQUIREMENT_MEMORY,
            state=KNOWLEDGE_PROPOSED,
            mandatory=True,
            value={
                "minimum": current_range["minimum"],
                "requested": proposed_requested,
                "limit": proposed_limit,
            },
            evidence=(evidence,),
        )
    elif observation.kind == "disk_exhausted":
        current = candidate.requirements[REQUIREMENT_STORAGE]
        if current.value is None:
            raise RecipeRequirementError("storage proposal requires a known baseline")
        value = dict(current.value)
        value["workspace_bytes"] = min(_MAX_INT, value["workspace_bytes"] * 2)
        requirement = RecipeRequirement(
            key=REQUIREMENT_STORAGE,
            state=KNOWLEDGE_PROPOSED,
            mandatory=True,
            value=value,
            evidence=(evidence,),
        )
    elif observation.kind == "network_denied":
        allowlist = observation.details.get("allowlist", [])
        if type(allowlist) is not list:
            raise RecipeRequirementError("network proposal evidence is invalid")
        requirement = RecipeRequirement(
            key=REQUIREMENT_NETWORK,
            state=KNOWLEDGE_PROPOSED,
            mandatory=True,
            value={"mode": "project-allowlist", "dns": [], "allowlist": allowlist},
            evidence=(evidence,),
        )
    elif observation.kind == "missing_accelerator":
        requirement = RecipeRequirement(
            key=REQUIREMENT_GPU,
            state=KNOWLEDGE_PROPOSED,
            mandatory=True,
            value={
                "mode": "required",
                "count": observation.details.get("count", 1),
                "vendor": observation.details.get("vendor"),
                "class": observation.details.get("class"),
                "minimum_vram_bytes": observation.details.get("minimum_vram_bytes", 0),
                "features": observation.details.get("features", []),
            },
            evidence=(evidence,),
        )
    elif observation.kind == "missing_runtime":
        current = candidate.requirements[REQUIREMENT_RUNTIME]
        image = None if current.value is None else current.value.get("image")
        toolchains = observation.details.get("toolchains", [])
        if type(toolchains) is not list:
            raise RecipeRequirementError("runtime proposal evidence is invalid")
        requirement = RecipeRequirement(
            key=REQUIREMENT_RUNTIME,
            state=KNOWLEDGE_PROPOSED,
            mandatory=True,
            value={"image": image, "toolchains": toolchains},
            evidence=(evidence,),
        )
    else:
        current = candidate.requirements[REQUIREMENT_CPU]
        if current.value is None:
            raise RecipeRequirementError("CPU proposal requires a known baseline")
        current_range = dict(current.value)
        proposed_requested = min(_MAX_INT, current_range["requested"] * 2)
        proposed_limit = min(_MAX_INT, max(current_range["limit"], proposed_requested))
        requirement = RecipeRequirement(
            key=REQUIREMENT_CPU,
            state=KNOWLEDGE_PROPOSED,
            mandatory=True,
            value={
                "minimum": current_range["minimum"],
                "requested": proposed_requested,
                "limit": proposed_limit,
            },
            evidence=(evidence,),
        )
    return candidate.replace_requirement(requirement)
