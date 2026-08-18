"""Phase 8A deterministic Workflow -> Candidate Recipe compilation and discovery."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from .recipe_requirements import (
    KNOWLEDGE_DECLARED,
    KNOWLEDGE_DERIVED,
    KNOWLEDGE_DISCOVERED,
    KNOWLEDGE_PROPOSED,
    KNOWLEDGE_UNKNOWN,
    REQUIREMENT_CPU,
    REQUIREMENT_EXECUTION,
    REQUIREMENT_FILESYSTEM,
    REQUIREMENT_GPU,
    REQUIREMENT_HOST_OPERATIONS,
    REQUIREMENT_IO,
    REQUIREMENT_KEYS,
    REQUIREMENT_MEMORY,
    REQUIREMENT_NETWORK,
    REQUIREMENT_PIDS,
    REQUIREMENT_PLACEMENT,
    REQUIREMENT_PLATFORM,
    REQUIREMENT_RUNTIME,
    REQUIREMENT_SECRETS,
    REQUIREMENT_STORAGE,
    REQUIREMENT_SWAP,
    REQUIREMENT_TOOLSETS,
    CandidateRecipe,
    RecipeRequirement,
    RequirementEvidence,
    WorkflowBinding,
)
from .recipes import (
    AgentRequirement,
    _canonical,
    _digest,
    _json_value,
    _name,
    _plain,
    _text,
)

WORKFLOW_SCHEMA_V1: Final[str] = "fleet.workflow-editor.v1"
WORKFLOW_SCHEMA_V2: Final[str] = "fleet.workflow-editor.v2"
COMPILER_VERSION: Final[str] = "fleet.workflow-recipe-compiler.v1"
RECIPE_STEP_TYPE: Final[str] = "recipe-step"
RECIPE_STEP_RUNTIME: Final[str] = "recipe"

_WORKFLOW_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_WORKFLOW_ITEMS = 256
_MAX_PROJECT_FILE_BYTES = 512 * 1024
_MAX_PROJECT_TOTAL_BYTES = 2 * 1024 * 1024
_MAX_PROJECT_FILES = 32

_PROJECT_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "poetry.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "project.godot",
    "Dockerfile",
)

_ALLOWED_RECIPE_CONFIG: Final[frozenset[str]] = frozenset(
    {
        "agent_name",
        "agent_version",
        "cpu_min_millis",
        "cpu_requested_millis",
        "cpu_limit_millis",
        "memory_min_bytes",
        "memory_requested_bytes",
        "memory_limit_bytes",
        "swap_policy",
        "pids_limit",
        "gpu_mode",
        "gpu_count",
        "gpu_vendor",
        "gpu_class",
        "gpu_min_vram_bytes",
        "gpu_features",
        "operating_systems",
        "architectures",
        "runtime_image",
        "toolchains",
        "workspace_bytes",
        "tmp_bytes",
        "home_bytes",
        "inputs",
        "outputs",
        "artifacts",
        "filesystem_mode",
        "filesystem_paths",
        "network_mode",
        "dns",
        "network_allowlist",
        "toolsets",
        "secret_requirements",
        "host_operations",
        "deadline_ms",
        "max_iterations",
        "placement_capabilities",
        "placement_labels",
        "discovery_enabled",
    }
)

_PROJECT_BASELINES: Final[dict[str, tuple[int, int]]] = {
    "python": (1000, 1_073_741_824),
    "node": (1000, 1_073_741_824),
    "rust": (1500, 2_147_483_648),
    "go": (1000, 1_073_741_824),
    "godot": (2000, 2_147_483_648),
    "dockerfile": (1000, 1_073_741_824),
}


class WorkflowRecipeCompilerError(ValueError):
    """Workflow compilation or discovery cannot be proven safe/deterministic."""


def _canonical_workflow(document: object) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise WorkflowRecipeCompilerError(
            "Workflow document is not canonical JSON"
        ) from error


def _workflow_hash(document: object) -> str:
    return hashlib.sha256(_canonical_workflow(document)).hexdigest()


def _workflow_identifier(value: object, label: str) -> str:
    if type(value) is not str or _WORKFLOW_ID_RE.fullmatch(value) is None:
        raise WorkflowRecipeCompilerError(f"{label} is invalid")
    return value


def _workflow_block_type(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or any(
            _WORKFLOW_ID_RE.fullmatch(segment) is None for segment in value.split("/")
        )
    ):
        raise WorkflowRecipeCompilerError("Workflow node type is invalid")
    return value


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical(value))


def _project_manifest_signatures(root: Path) -> dict[str, tuple[int, int, int, int]]:
    signatures: dict[str, tuple[int, int, int, int]] = {}
    for name in _PROJECT_FILES:
        path = root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise WorkflowRecipeCompilerError(
                "project evidence changed while reading"
            ) from error
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > _MAX_PROJECT_FILE_BYTES
        ):
            raise WorkflowRecipeCompilerError("project evidence file is unsafe")
        signatures[name] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return signatures


def _read_project_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkflowRecipeCompilerError("project evidence file is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_PROJECT_FILE_BYTES
        ):
            raise WorkflowRecipeCompilerError("project evidence file is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor, min(65_536, _MAX_PROJECT_FILE_BYTES + 1 - total)
            )
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_PROJECT_FILE_BYTES:
                raise WorkflowRecipeCompilerError("project evidence file exceeds bound")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_after = path.lstat()
    except OSError as error:
        raise WorkflowRecipeCompilerError(
            "project evidence changed while reading"
        ) from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or path_after.st_dev != before.st_dev
        or path_after.st_ino != before.st_ino
        or stat.S_ISLNK(path_after.st_mode)
    ):
        raise WorkflowRecipeCompilerError("project evidence changed while reading")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise WorkflowRecipeCompilerError("project evidence changed while reading")
    return payload


def _positive_integer(
    value: object, label: str, *, maximum: int = (1 << 63) - 1
) -> int:
    if isinstance(value, bool) or type(value) is not int or not 0 < value <= maximum:
        raise WorkflowRecipeCompilerError(f"{label} is invalid")
    return value


def _optional_positive_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_integer(value, label)


def _csv(value: object, label: str, *, maximum: int = 64) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if type(value) is not str or len(value) > 4096:
        raise WorkflowRecipeCompilerError(f"{label} is invalid")
    parts = tuple(part.strip() for part in value.split(",") if part.strip())
    if len(parts) > maximum or len(parts) != len(set(parts)):
        raise WorkflowRecipeCompilerError(f"{label} is invalid")
    for part in parts:
        _text(part, label, maximum=512)
    return tuple(sorted(parts))


def _configuration_digest(
    binding: WorkflowBinding, configuration: Mapping[str, Any]
) -> str:
    return _sha256_json(
        {
            "workflow": binding.to_dict(),
            "configuration": _plain(dict(configuration)),
        }
    )


@dataclass(frozen=True, slots=True)
class WorkflowRevisionSnapshot:
    workflow_id: str
    revision: int
    content_hash: str
    document: Mapping[str, Any]
    created_at_ms: int

    def __post_init__(self) -> None:
        _workflow_identifier(self.workflow_id, "Workflow ID")
        _positive_integer(self.revision, "Workflow revision")
        if (
            type(self.content_hash) is not str
            or _WORKFLOW_HASH_RE.fullmatch(self.content_hash) is None
        ):
            raise WorkflowRecipeCompilerError("Workflow content hash is invalid")
        if not isinstance(self.document, Mapping):
            raise WorkflowRecipeCompilerError("Workflow revision document is invalid")
        document = _plain(dict(self.document))
        if _workflow_hash(document) != self.content_hash:
            raise WorkflowRecipeCompilerError("Workflow revision content hash changed")
        if document.get("id") != self.workflow_id:
            raise WorkflowRecipeCompilerError("Workflow revision identity changed")
        _positive_integer(self.created_at_ms, "Workflow creation timestamp")
        object.__setattr__(self, "document", MappingProxyType(document))

    @property
    def canonical_content_hash(self) -> str:
        return f"sha256:{self.content_hash}"

    @classmethod
    def from_backend(cls, value: object) -> WorkflowRevisionSnapshot:
        if type(value) is not dict or set(value) != {
            "workflowId",
            "version",
            "contentHash",
            "document",
            "createdAtMs",
        }:
            raise WorkflowRecipeCompilerError(
                "Workflow backend revision shape is invalid"
            )
        return cls(
            workflow_id=value["workflowId"],
            revision=value["version"],
            content_hash=value["contentHash"],
            document=value["document"],
            created_at_ms=value["createdAtMs"],
        )


@dataclass(frozen=True, slots=True)
class ProjectEvidence:
    root_label: str
    files: Mapping[str, str]
    total_bytes: int
    toolchains: tuple[str, ...]
    runtime_image: str | None = None

    def __post_init__(self) -> None:
        _text(self.root_label, "project evidence label", maximum=512)
        if not isinstance(self.files, Mapping) or len(self.files) > _MAX_PROJECT_FILES:
            raise WorkflowRecipeCompilerError("project evidence files are invalid")
        normalized: dict[str, str] = {}
        for name, digest in self.files.items():
            if (
                name not in _PROJECT_FILES
                or type(digest) is not str
                or not digest.startswith("sha256:")
            ):
                raise WorkflowRecipeCompilerError("project evidence file is invalid")
            normalized[name] = digest
        object.__setattr__(
            self, "files", MappingProxyType(dict(sorted(normalized.items())))
        )
        if (
            isinstance(self.total_bytes, bool)
            or type(self.total_bytes) is not int
            or not 0 <= self.total_bytes <= _MAX_PROJECT_TOTAL_BYTES
        ):
            raise WorkflowRecipeCompilerError("project evidence size is invalid")
        object.__setattr__(
            self,
            "toolchains",
            tuple(sorted(_name(item, "project toolchain") for item in self.toolchains)),
        )
        if len(self.toolchains) != len(set(self.toolchains)):
            raise WorkflowRecipeCompilerError("project toolchains contain duplicates")
        if self.runtime_image is not None:
            _text(self.runtime_image, "project runtime image", maximum=512)
            if "@sha256:" not in self.runtime_image:
                raise WorkflowRecipeCompilerError(
                    "project runtime image is not digest-pinned"
                )

    @classmethod
    def empty(cls, label: str = "no-project-evidence") -> ProjectEvidence:
        return cls(root_label=label, files={}, total_bytes=0, toolchains=())

    @classmethod
    def from_directory(
        cls, root: str | Path, *, label: str = "project"
    ) -> ProjectEvidence:
        root_path = Path(root)
        try:
            root_info = root_path.lstat()
        except OSError as error:
            raise WorkflowRecipeCompilerError(
                "project evidence root is unavailable"
            ) from error
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise WorkflowRecipeCompilerError("project evidence root is unsafe")
        try:
            canonical_root = root_path.resolve(strict=True)
        except OSError as error:
            raise WorkflowRecipeCompilerError(
                "project evidence root is unavailable"
            ) from error
        if not canonical_root.is_dir():
            raise WorkflowRecipeCompilerError("project evidence root is unsafe")
        initial_signatures = _project_manifest_signatures(canonical_root)
        files: dict[str, str] = {}
        total = 0
        contents: dict[str, bytes] = {}
        for name in sorted(initial_signatures):
            payload = _read_project_file(canonical_root / name)
            total += len(payload)
            if total > _MAX_PROJECT_TOTAL_BYTES:
                raise WorkflowRecipeCompilerError(
                    "project evidence exceeds aggregate bound"
                )
            contents[name] = payload
            files[name] = _sha256_bytes(payload)
        if _project_manifest_signatures(canonical_root) != initial_signatures:
            raise WorkflowRecipeCompilerError("project evidence changed while reading")
        toolchains = _derive_toolchains(files)
        runtime_image = _derive_digest_pinned_image(contents.get("Dockerfile"))
        return cls(
            root_label=label,
            files=files,
            total_bytes=total,
            toolchains=toolchains,
            runtime_image=runtime_image,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_label": self.root_label,
            "files": dict(self.files),
            "total_bytes": self.total_bytes,
            "toolchains": list(self.toolchains),
            "runtime_image": self.runtime_image,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict())


def _derive_toolchains(files: Mapping[str, str]) -> tuple[str, ...]:
    toolchains: set[str] = set()
    if {"pyproject.toml", "requirements.txt", "uv.lock", "poetry.lock"} & files.keys():
        toolchains.add("python")
    if {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
    } & files.keys():
        toolchains.add("node")
    if {"Cargo.toml", "Cargo.lock"} & files.keys():
        toolchains.add("rust")
    if {"go.mod", "go.sum"} & files.keys():
        toolchains.add("go")
    if "project.godot" in files:
        toolchains.add("godot")
    if "Dockerfile" in files:
        toolchains.add("dockerfile")
    return tuple(sorted(toolchains))


def _derive_digest_pinned_image(payload: bytes | None) -> str | None:
    if not payload:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.upper().startswith("FROM "):
            continue
        image = line.split(None, 2)[1]
        if "@sha256:" in image and re.fullmatch(
            r"[a-z0-9][a-z0-9./_-]{0,254}@sha256:[0-9a-f]{64}", image
        ):
            return image
        return None
    return None


@dataclass(frozen=True, slots=True)
class CompilerContext:
    project: ProjectEvidence
    agency_fingerprint: str
    runtime_fingerprint: str
    policy_fingerprint: str
    capabilities_fingerprint: str
    operator_contract_digest: str | None = None

    def __post_init__(self) -> None:
        if type(self.project) is not ProjectEvidence:
            raise WorkflowRecipeCompilerError("project evidence is invalid")
        for value, label in (
            (self.agency_fingerprint, "Agency fingerprint"),
            (self.runtime_fingerprint, "runtime fingerprint"),
            (self.policy_fingerprint, "policy fingerprint"),
            (self.capabilities_fingerprint, "capabilities fingerprint"),
        ):
            if type(value) is not str or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", value
            ):
                raise WorkflowRecipeCompilerError(f"{label} is invalid")
        if self.operator_contract_digest is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.operator_contract_digest
        ):
            raise WorkflowRecipeCompilerError("operator contract digest is invalid")

    def derivation_inputs(self, workflow_hash: str) -> dict[str, Any]:
        return {
            "workflow_hash": workflow_hash,
            "project_fingerprint": self.project.content_hash,
            "agency_fingerprint": self.agency_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "capabilities_fingerprint": self.capabilities_fingerprint,
            "operator_contract_digest": self.operator_contract_digest,
            "compiler_version": COMPILER_VERSION,
        }

    def derivation_inputs_digest(self, workflow_hash: str) -> str:
        return _digest(self.derivation_inputs(workflow_hash))


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    workflow_id: str
    revision: int
    workflow_hash: str
    compiler_version: str
    derivation_inputs_digest: str
    recipes: tuple[CandidateRecipe, ...]

    def __post_init__(self) -> None:
        _workflow_identifier(self.workflow_id, "Workflow ID")
        _positive_integer(self.revision, "Workflow revision")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.workflow_hash):
            raise WorkflowRecipeCompilerError("Workflow hash is invalid")
        _name(self.compiler_version, "compiler version")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.derivation_inputs_digest):
            raise WorkflowRecipeCompilerError("derivation inputs digest is invalid")
        if type(self.recipes) is not tuple or len(self.recipes) > _MAX_WORKFLOW_ITEMS:
            raise WorkflowRecipeCompilerError("compiled Recipe collection is invalid")
        if any(type(item) is not CandidateRecipe for item in self.recipes):
            raise WorkflowRecipeCompilerError("compiled Recipe collection is invalid")

    def audit_evidence(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "workflow_revision": self.revision,
            "workflow_hash": self.workflow_hash,
            "compiler_version": self.compiler_version,
            "derivation_inputs_digest": self.derivation_inputs_digest,
            "recipes": [
                {
                    "step_id": recipe.workflow.step_id,
                    "candidate_recipe_hash": recipe.content_hash,
                    "requirement_provenance_digest": _digest(
                        {
                            key: [
                                evidence.to_dict()
                                for evidence in recipe.requirements[key].evidence
                            ]
                            for key in REQUIREMENT_KEYS
                        }
                    ),
                }
                for recipe in self.recipes
            ],
        }


class WorkflowRecipeCompiler:
    def compile(
        self,
        revision: WorkflowRevisionSnapshot,
        context: CompilerContext,
    ) -> CompiledWorkflow:
        if (
            type(revision) is not WorkflowRevisionSnapshot
            or type(context) is not CompilerContext
        ):
            raise WorkflowRecipeCompilerError("Workflow compiler input is invalid")
        document = dict(revision.document)
        nodes, connections = _validate_workflow_document(document)
        order = _topological_order(nodes, connections)
        recipe_nodes = {
            node["id"]: node for node in nodes if node["type"] == RECIPE_STEP_TYPE
        }
        dependencies = _recipe_dependencies(nodes, connections, recipe_nodes)
        derivation_digest = context.derivation_inputs_digest(
            revision.canonical_content_hash
        )
        candidates: list[CandidateRecipe] = []
        for node_id in order:
            node = recipe_nodes.get(node_id)
            if node is None:
                continue
            candidates.append(
                self._compile_step(
                    revision=revision,
                    node=node,
                    dependencies=dependencies[node_id],
                    context=context,
                    derivation_digest=derivation_digest,
                )
            )
        return CompiledWorkflow(
            workflow_id=revision.workflow_id,
            revision=revision.revision,
            workflow_hash=revision.canonical_content_hash,
            compiler_version=COMPILER_VERSION,
            derivation_inputs_digest=derivation_digest,
            recipes=tuple(candidates),
        )

    def compile_from_client(
        self,
        client: Any,
        *,
        workflow_id: str,
        revision: int,
        context: CompilerContext,
    ) -> CompiledWorkflow:
        reader = getattr(client, "read_workflow_version", None)
        if not callable(reader):
            raise WorkflowRecipeCompilerError("Workflow backend client is unavailable")
        backend_revision = reader(workflow_id, version=revision)
        if backend_revision is None:
            raise WorkflowRecipeCompilerError("Workflow revision is unavailable")
        return self.compile(
            WorkflowRevisionSnapshot.from_backend(backend_revision), context
        )

    def _compile_step(
        self,
        *,
        revision: WorkflowRevisionSnapshot,
        node: Mapping[str, Any],
        dependencies: tuple[str, ...],
        context: CompilerContext,
        derivation_digest: str,
    ) -> CandidateRecipe:
        configuration = node["configuration"]
        if (
            type(configuration) is not dict
            or set(configuration) - _ALLOWED_RECIPE_CONFIG
        ):
            raise WorkflowRecipeCompilerError("Recipe step configuration is invalid")
        agent_name = configuration.get("agent_name")
        agent_version = configuration.get("agent_version")
        if type(agent_name) is not str or type(agent_version) is not str:
            raise WorkflowRecipeCompilerError(
                "Recipe step requires an Agency profile/version"
            )
        agent = AgentRequirement(
            kind="agency_profile", name=agent_name, version=agent_version
        )
        binding = WorkflowBinding(
            workflow_id=revision.workflow_id,
            revision=revision.revision,
            content_hash=revision.canonical_content_hash,
            step_id=node["id"],
        )
        workflow_evidence = RequirementEvidence(
            kind="workflow",
            source=f"workflow:{revision.workflow_id}@{revision.revision}#{node['id']}",
            digest=_configuration_digest(binding, configuration),
        )
        project_evidence = RequirementEvidence(
            kind="project",
            source=context.project.root_label,
            digest=context.project.content_hash,
        )
        policy_evidence = RequirementEvidence(
            kind="policy",
            source="compiler-safe-defaults",
            digest=context.policy_fingerprint,
        )
        requirements = _compile_requirements(
            configuration,
            context=context,
            workflow_evidence=workflow_evidence,
            project_evidence=project_evidence,
            policy_evidence=policy_evidence,
        )
        discovery_enabled = configuration.get("discovery_enabled", True)
        if type(discovery_enabled) is not bool:
            raise WorkflowRecipeCompilerError("Recipe discovery flag is invalid")
        return CandidateRecipe(
            workflow=binding,
            compiler_version=COMPILER_VERSION,
            derivation_inputs_digest=derivation_digest,
            agent=agent,
            requirements=requirements,
            dependencies=dependencies,
            extensions={"dev.hermes.fleet.discovery": {"enabled": discovery_enabled}},
        )


def _validate_workflow_document(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if set(document) != {"schema", "id", "name", "nodes", "connections", "metadata"}:
        raise WorkflowRecipeCompilerError("Workflow document shape is invalid")
    if document["schema"] != WORKFLOW_SCHEMA_V2:
        raise WorkflowRecipeCompilerError("Workflow must use compile-capable v2 schema")
    if document["metadata"] != {"executionAvailable": False}:
        raise WorkflowRecipeCompilerError("Workflow metadata cannot grant execution")
    nodes = document["nodes"]
    connections = document["connections"]
    if (
        type(nodes) is not list
        or type(connections) is not list
        or len(nodes) > _MAX_WORKFLOW_ITEMS
        or len(connections) > _MAX_WORKFLOW_ITEMS
    ):
        raise WorkflowRecipeCompilerError("Workflow graph exceeds compiler bounds")
    node_ids: set[str] = set()
    normalized_nodes: list[dict[str, Any]] = []
    for raw in nodes:
        if type(raw) is not dict:
            raise WorkflowRecipeCompilerError("Workflow node is invalid")
        required = {
            "id",
            "type",
            "title",
            "position",
            "configuration",
            "target",
            "runtime",
        }
        optional = {"pluginVersion", "configVersion"}
        if not required.issubset(raw) or set(raw) - required - optional:
            raise WorkflowRecipeCompilerError("Workflow node shape is invalid")
        node_id = _workflow_identifier(raw["id"], "Workflow node ID")
        if node_id in node_ids:
            raise WorkflowRecipeCompilerError("Workflow node IDs are not unique")
        node_ids.add(node_id)
        position = raw["position"]
        if (
            type(position) is not dict
            or set(position) != {"x", "y"}
            or isinstance(position["x"], bool)
            or isinstance(position["y"], bool)
            or not isinstance(position["x"], int | float)
            or not isinstance(position["y"], int | float)
            or not math.isfinite(position["x"])
            or not math.isfinite(position["y"])
        ):
            raise WorkflowRecipeCompilerError("Workflow node position is invalid")
        block_type = _workflow_block_type(raw["type"])
        runtime = raw["runtime"]
        if block_type == RECIPE_STEP_TYPE:
            if runtime != RECIPE_STEP_RUNTIME:
                raise WorkflowRecipeCompilerError(
                    "Recipe step runtime marker is invalid"
                )
        elif runtime != "unavailable":
            raise WorkflowRecipeCompilerError(
                "non-Recipe Workflow node cannot acquire runtime"
            )
        normalized_nodes.append(raw)
    normalized_connections: list[dict[str, Any]] = []
    connection_ids: set[str] = set()
    occupied_inputs: set[tuple[str, str]] = set()
    for raw in connections:
        if type(raw) is not dict or set(raw) != {
            "id",
            "source",
            "sourcePort",
            "target",
            "targetPort",
            "kind",
        }:
            raise WorkflowRecipeCompilerError("Workflow connection shape is invalid")
        connection_id = _workflow_identifier(raw["id"], "Workflow connection ID")
        if connection_id in connection_ids:
            raise WorkflowRecipeCompilerError("Workflow connection IDs are not unique")
        connection_ids.add(connection_id)
        source = _workflow_identifier(raw["source"], "Workflow connection source")
        target = _workflow_identifier(raw["target"], "Workflow connection target")
        _workflow_identifier(raw["sourcePort"], "Workflow source port")
        target_port = _workflow_identifier(raw["targetPort"], "Workflow target port")
        _workflow_identifier(raw["kind"], "Workflow connection kind")
        if source not in node_ids or target not in node_ids or source == target:
            raise WorkflowRecipeCompilerError("Workflow connection endpoint is invalid")
        input_key = (target, target_port)
        if input_key in occupied_inputs:
            raise WorkflowRecipeCompilerError("Workflow input has multiple producers")
        occupied_inputs.add(input_key)
        normalized_connections.append(raw)
    return normalized_nodes, normalized_connections


def _topological_order(
    nodes: list[dict[str, Any]], connections: list[dict[str, Any]]
) -> tuple[str, ...]:
    ids = [node["id"] for node in nodes]
    indegree = {node_id: 0 for node_id in ids}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in connections:
        if edge["kind"] != "control":
            continue
        outgoing[edge["source"]].append(edge["target"])
        indegree[edge["target"]] += 1
    queue = deque(sorted(node_id for node_id, count in indegree.items() if count == 0))
    ordered: list[str] = []
    while queue:
        node_id = queue.popleft()
        ordered.append(node_id)
        for target in sorted(outgoing[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(ordered) != len(ids):
        raise WorkflowRecipeCompilerError(
            "Workflow execution dependencies contain a cycle"
        )
    return tuple(ordered)


def _recipe_dependencies(
    nodes: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    recipe_nodes: Mapping[str, dict[str, Any]],
) -> dict[str, tuple[str, ...]]:
    incoming: dict[str, list[str]] = defaultdict(list)
    for edge in connections:
        if edge["kind"] != "control":
            continue
        incoming[edge["target"]].append(edge["source"])
    result: dict[str, tuple[str, ...]] = {}
    for recipe_id in recipe_nodes:
        found: set[str] = set()
        stack = list(incoming[recipe_id])
        visited: set[str] = set()
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            if current in recipe_nodes:
                found.add(current)
                continue
            stack.extend(incoming[current])
        result[recipe_id] = tuple(sorted(found))
    return result


def _known_requirement(
    key: str,
    value: Any,
    *,
    state: str,
    evidence: RequirementEvidence,
    mandatory: bool = True,
) -> RecipeRequirement:
    return RecipeRequirement(
        key=key,
        state=state,
        mandatory=mandatory,
        value=value,
        evidence=(evidence,),
    )


def _declared_range(
    configuration: Mapping[str, Any],
    prefix: str,
    requirement_key: str,
    evidence: RequirementEvidence,
) -> RecipeRequirement | None:
    keys = (f"{prefix}_min", f"{prefix}_requested", f"{prefix}_limit")
    if prefix in {"cpu_millis", "memory_bytes"}:
        if prefix == "cpu_millis":
            keys = ("cpu_min_millis", "cpu_requested_millis", "cpu_limit_millis")
        else:
            keys = ("memory_min_bytes", "memory_requested_bytes", "memory_limit_bytes")
    present = [key in configuration for key in keys]
    if any(present) and not all(present):
        raise WorkflowRecipeCompilerError(
            f"{requirement_key} range must be declared completely"
        )
    if not all(present):
        return None
    return _known_requirement(
        requirement_key,
        {
            "minimum": _positive_integer(
                configuration[keys[0]], f"{requirement_key} minimum"
            ),
            "requested": _positive_integer(
                configuration[keys[1]], f"{requirement_key} requested"
            ),
            "limit": _positive_integer(
                configuration[keys[2]], f"{requirement_key} limit"
            ),
        },
        state=KNOWLEDGE_DECLARED,
        evidence=evidence,
    )


def _project_baseline(project: ProjectEvidence) -> tuple[int, int] | None:
    baselines = [
        _PROJECT_BASELINES[item]
        for item in project.toolchains
        if item in _PROJECT_BASELINES
    ]
    if not baselines:
        return None
    return max(item[0] for item in baselines), max(item[1] for item in baselines)


def _compile_requirements(
    configuration: Mapping[str, Any],
    *,
    context: CompilerContext,
    workflow_evidence: RequirementEvidence,
    project_evidence: RequirementEvidence,
    policy_evidence: RequirementEvidence,
) -> dict[str, RecipeRequirement]:
    result: dict[str, RecipeRequirement] = {}

    cpu = _declared_range(
        configuration, "cpu_millis", REQUIREMENT_CPU, workflow_evidence
    )
    memory = _declared_range(
        configuration, "memory_bytes", REQUIREMENT_MEMORY, workflow_evidence
    )
    baseline = _project_baseline(context.project)
    if cpu is None:
        if baseline is None:
            cpu = RecipeRequirement.unknown(REQUIREMENT_CPU, mandatory=True)
        else:
            requested = baseline[0]
            cpu = _known_requirement(
                REQUIREMENT_CPU,
                {
                    "minimum": max(100, requested // 2),
                    "requested": requested,
                    "limit": requested * 2,
                },
                state=KNOWLEDGE_DERIVED,
                evidence=project_evidence,
            )
    if memory is None:
        if baseline is None:
            memory = RecipeRequirement.unknown(REQUIREMENT_MEMORY, mandatory=True)
        else:
            requested = baseline[1]
            memory = _known_requirement(
                REQUIREMENT_MEMORY,
                {
                    "minimum": max(67_108_864, requested // 2),
                    "requested": requested,
                    "limit": requested * 2,
                },
                state=KNOWLEDGE_DERIVED,
                evidence=project_evidence,
            )
    result[REQUIREMENT_CPU] = cpu
    result[REQUIREMENT_MEMORY] = memory

    swap_policy = configuration.get("swap_policy", "disabled")
    result[REQUIREMENT_SWAP] = _known_requirement(
        REQUIREMENT_SWAP,
        swap_policy,
        state=(
            KNOWLEDGE_DECLARED if "swap_policy" in configuration else KNOWLEDGE_DERIVED
        ),
        evidence=(
            workflow_evidence if "swap_policy" in configuration else policy_evidence
        ),
    )
    result[REQUIREMENT_PIDS] = _known_requirement(
        REQUIREMENT_PIDS,
        _positive_integer(configuration.get("pids_limit", 128), "PID limit"),
        state=(
            KNOWLEDGE_DECLARED if "pids_limit" in configuration else KNOWLEDGE_DERIVED
        ),
        evidence=(
            workflow_evidence if "pids_limit" in configuration else policy_evidence
        ),
    )

    if "gpu_mode" in configuration:
        gpu_mode = configuration["gpu_mode"]
        gpu_value = {
            "mode": gpu_mode,
            "count": (
                _positive_integer(configuration.get("gpu_count", 1), "GPU count")
                if gpu_mode != "none"
                else 0
            ),
            "vendor": configuration.get("gpu_vendor"),
            "class": configuration.get("gpu_class"),
            "minimum_vram_bytes": _optional_positive_integer(
                configuration.get("gpu_min_vram_bytes"), "GPU VRAM"
            )
            or 0,
            "features": list(_csv(configuration.get("gpu_features"), "GPU features")),
        }
        result[REQUIREMENT_GPU] = _known_requirement(
            REQUIREMENT_GPU,
            gpu_value,
            state=KNOWLEDGE_DECLARED,
            evidence=workflow_evidence,
            mandatory=gpu_mode == "required",
        )
    else:
        result[REQUIREMENT_GPU] = _known_requirement(
            REQUIREMENT_GPU,
            {
                "mode": "none",
                "count": 0,
                "vendor": None,
                "class": None,
                "minimum_vram_bytes": 0,
                "features": [],
            },
            state=KNOWLEDGE_DERIVED,
            evidence=policy_evidence,
            mandatory=False,
        )

    operating_systems = _csv(
        configuration.get("operating_systems"), "operating systems"
    )
    architectures = _csv(configuration.get("architectures"), "architectures")
    result[REQUIREMENT_PLATFORM] = _known_requirement(
        REQUIREMENT_PLATFORM,
        {
            "os": list(operating_systems or ("linux",)),
            "architectures": list(architectures or ("aarch64", "x86_64")),
        },
        state=(
            KNOWLEDGE_DECLARED
            if operating_systems or architectures
            else KNOWLEDGE_DERIVED
        ),
        evidence=(
            workflow_evidence if operating_systems or architectures else policy_evidence
        ),
    )

    declared_toolchains = _csv(configuration.get("toolchains"), "toolchains")
    toolchains = declared_toolchains or context.project.toolchains
    runtime_image = configuration.get("runtime_image", context.project.runtime_image)
    if not toolchains and runtime_image is None:
        result[REQUIREMENT_RUNTIME] = RecipeRequirement.unknown(
            REQUIREMENT_RUNTIME, mandatory=True
        )
    else:
        result[REQUIREMENT_RUNTIME] = _known_requirement(
            REQUIREMENT_RUNTIME,
            {"image": runtime_image, "toolchains": list(toolchains)},
            state=(
                KNOWLEDGE_DECLARED
                if declared_toolchains or "runtime_image" in configuration
                else KNOWLEDGE_DERIVED
            ),
            evidence=(
                workflow_evidence
                if declared_toolchains or "runtime_image" in configuration
                else project_evidence
            ),
        )

    storage_keys = ("workspace_bytes", "tmp_bytes", "home_bytes")
    storage_present = tuple(key in configuration for key in storage_keys)
    if any(storage_present) and not all(storage_present):
        raise WorkflowRecipeCompilerError(
            "storage requirement must declare workspace/tmp/home together"
        )
    if all(storage_present):
        result[REQUIREMENT_STORAGE] = _known_requirement(
            REQUIREMENT_STORAGE,
            {
                "workspace_bytes": _positive_integer(
                    configuration["workspace_bytes"], "workspace capacity"
                ),
                "tmp_bytes": _positive_integer(
                    configuration["tmp_bytes"], "tmp capacity"
                ),
                "home_bytes": _positive_integer(
                    configuration["home_bytes"], "home capacity"
                ),
            },
            state=KNOWLEDGE_DECLARED,
            evidence=workflow_evidence,
        )
    else:
        result[REQUIREMENT_STORAGE] = RecipeRequirement.unknown(
            REQUIREMENT_STORAGE,
            mandatory=True,
        )

    io_declared = any(
        key in configuration for key in ("inputs", "outputs", "artifacts")
    )
    result[REQUIREMENT_IO] = _known_requirement(
        REQUIREMENT_IO,
        {
            "inputs": list(_csv(configuration.get("inputs"), "inputs") or ("project",)),
            "outputs": list(_csv(configuration.get("outputs"), "outputs")),
            "artifacts": list(_csv(configuration.get("artifacts"), "artifacts")),
        },
        state=KNOWLEDGE_DECLARED if io_declared else KNOWLEDGE_DERIVED,
        evidence=workflow_evidence if io_declared else policy_evidence,
    )

    filesystem_declared = any(
        key in configuration for key in ("filesystem_mode", "filesystem_paths")
    )
    result[REQUIREMENT_FILESYSTEM] = _known_requirement(
        REQUIREMENT_FILESYSTEM,
        {
            "mode": configuration.get("filesystem_mode", "read-only"),
            "paths": list(
                _csv(configuration.get("filesystem_paths"), "filesystem paths")
            ),
        },
        state=KNOWLEDGE_DECLARED if filesystem_declared else KNOWLEDGE_DERIVED,
        evidence=workflow_evidence if filesystem_declared else policy_evidence,
    )

    network_declared = any(
        key in configuration for key in ("network_mode", "dns", "network_allowlist")
    )
    result[REQUIREMENT_NETWORK] = _known_requirement(
        REQUIREMENT_NETWORK,
        {
            "mode": configuration.get("network_mode", "none"),
            "dns": list(_csv(configuration.get("dns"), "DNS requirements")),
            "allowlist": list(
                _csv(configuration.get("network_allowlist"), "network allowlist")
            ),
        },
        state=KNOWLEDGE_DECLARED if network_declared else KNOWLEDGE_DERIVED,
        evidence=workflow_evidence if network_declared else policy_evidence,
    )

    toolsets = _csv(configuration.get("toolsets"), "toolsets") or ("fleet-terminal",)
    result[REQUIREMENT_TOOLSETS] = _known_requirement(
        REQUIREMENT_TOOLSETS,
        list(toolsets),
        state=KNOWLEDGE_DECLARED if "toolsets" in configuration else KNOWLEDGE_DERIVED,
        evidence=workflow_evidence if "toolsets" in configuration else policy_evidence,
    )
    result[REQUIREMENT_SECRETS] = _known_requirement(
        REQUIREMENT_SECRETS,
        list(_csv(configuration.get("secret_requirements"), "secret requirements")),
        state=(
            KNOWLEDGE_DECLARED
            if "secret_requirements" in configuration
            else KNOWLEDGE_DERIVED
        ),
        evidence=(
            workflow_evidence
            if "secret_requirements" in configuration
            else policy_evidence
        ),
        mandatory=False,
    )
    result[REQUIREMENT_HOST_OPERATIONS] = _known_requirement(
        REQUIREMENT_HOST_OPERATIONS,
        list(_csv(configuration.get("host_operations"), "host operations")),
        state=(
            KNOWLEDGE_DECLARED
            if "host_operations" in configuration
            else KNOWLEDGE_DERIVED
        ),
        evidence=(
            workflow_evidence if "host_operations" in configuration else policy_evidence
        ),
        mandatory=False,
    )
    execution_declared = any(
        key in configuration for key in ("deadline_ms", "max_iterations")
    )
    result[REQUIREMENT_EXECUTION] = _known_requirement(
        REQUIREMENT_EXECUTION,
        {
            "deadline_ms": _positive_integer(
                configuration.get("deadline_ms", 900_000), "execution deadline"
            ),
            "max_iterations": _positive_integer(
                configuration.get("max_iterations", 8), "iteration limit", maximum=32
            ),
        },
        state=KNOWLEDGE_DECLARED if execution_declared else KNOWLEDGE_DERIVED,
        evidence=workflow_evidence if execution_declared else policy_evidence,
    )
    placement_declared = any(
        key in configuration for key in ("placement_capabilities", "placement_labels")
    )
    result[REQUIREMENT_PLACEMENT] = _known_requirement(
        REQUIREMENT_PLACEMENT,
        {
            "capabilities": list(
                _csv(
                    configuration.get("placement_capabilities"),
                    "placement capabilities",
                )
            ),
            "labels": list(
                _csv(configuration.get("placement_labels"), "placement labels")
            ),
        },
        state=KNOWLEDGE_DECLARED if placement_declared else KNOWLEDGE_DERIVED,
        evidence=workflow_evidence if placement_declared else policy_evidence,
    )
    if set(result) != set(REQUIREMENT_KEYS):
        raise WorkflowRecipeCompilerError(
            "Recipe compiler emitted incomplete requirements"
        )
    return result


@dataclass(frozen=True, slots=True)
class DiscoveryObservation:
    requirement_key: str
    value: Any
    evidence_digest: str
    source: str = "probe"

    def __post_init__(self) -> None:
        if self.requirement_key not in REQUIREMENT_KEYS:
            raise WorkflowRecipeCompilerError("discovery requirement key is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.evidence_digest):
            raise WorkflowRecipeCompilerError("discovery evidence digest is invalid")
        _text(self.source, "discovery source", maximum=512)
        object.__setattr__(self, "value", _json_value(self.value))


def apply_discovery(
    candidate: CandidateRecipe,
    observations: tuple[DiscoveryObservation, ...],
) -> CandidateRecipe:
    if type(candidate) is not CandidateRecipe or type(observations) is not tuple:
        raise WorkflowRecipeCompilerError("discovery input is invalid")
    if not candidate.discovery_enabled:
        raise WorkflowRecipeCompilerError("Recipe discovery is disabled for this step")
    current = candidate
    seen: set[str] = set()
    for observation in observations:
        if type(observation) is not DiscoveryObservation:
            raise WorkflowRecipeCompilerError("discovery observation is invalid")
        if observation.requirement_key in seen:
            raise WorkflowRecipeCompilerError("duplicate discovery requirement")
        seen.add(observation.requirement_key)
        existing = current.requirements[observation.requirement_key]
        if existing.state != KNOWLEDGE_UNKNOWN:
            raise WorkflowRecipeCompilerError(
                "discovery cannot overwrite known requirement"
            )
        current = current.replace_requirement(
            RecipeRequirement(
                key=observation.requirement_key,
                state=KNOWLEDGE_DISCOVERED,
                mandatory=existing.mandatory,
                value=_plain(observation.value),
                evidence=(
                    RequirementEvidence(
                        kind="probe",
                        source=observation.source,
                        digest=observation.evidence_digest,
                    ),
                ),
            )
        )
    return current


def apply_deterministic_proposal_validation(
    candidate: CandidateRecipe,
    *,
    requirement_key: str,
    validation_digest: str,
) -> CandidateRecipe:
    """Convert one proposal only after an external deterministic validation proof."""
    if type(candidate) is not CandidateRecipe:
        raise WorkflowRecipeCompilerError("proposal validation candidate is invalid")
    current = candidate.requirements.get(requirement_key)
    if current is None or current.state != KNOWLEDGE_PROPOSED:
        raise WorkflowRecipeCompilerError("proposal validation target is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", validation_digest):
        raise WorkflowRecipeCompilerError("proposal validation digest is invalid")
    return candidate.replace_requirement(
        RecipeRequirement(
            key=current.key,
            state=KNOWLEDGE_DISCOVERED,
            mandatory=current.mandatory,
            value=_plain(current.value),
            evidence=(
                *current.evidence,
                RequirementEvidence(
                    kind="policy",
                    source="deterministic-proposal-validation",
                    digest=validation_digest,
                ),
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class DiscoveryProbePolicy:
    image: str
    cpu_millis: int = 250
    memory_bytes: int = 268_435_456
    pids_limit: int = 32
    deadline_ms: int = 30_000
    network: str = "none"
    read_only_project_inputs: bool = True
    secret_refs: tuple[str, ...] = ()
    host_broker_grants: tuple[str, ...] = ()
    management_network: bool = False
    docker_socket: bool = False
    persistent_agent_authority: bool = False
    non_root: bool = True
    cap_drop_all: bool = True
    no_new_privileges: bool = True

    def __post_init__(self) -> None:
        _text(self.image, "discovery probe image", maximum=512)
        if "@sha256:" not in self.image:
            raise WorkflowRecipeCompilerError(
                "discovery probe image must be digest-pinned"
            )
        _positive_integer(self.cpu_millis, "probe CPU limit")
        _positive_integer(self.memory_bytes, "probe memory limit")
        _positive_integer(self.pids_limit, "probe PID limit")
        _positive_integer(self.deadline_ms, "probe deadline")
        if self.network != "none":
            raise WorkflowRecipeCompilerError("default discovery probe must be offline")
        if (
            self.secret_refs
            or self.host_broker_grants
            or self.management_network
            or self.docker_socket
            or self.persistent_agent_authority
            or self.read_only_project_inputs is not True
            or self.non_root is not True
            or self.cap_drop_all is not True
            or self.no_new_privileges is not True
        ):
            raise WorkflowRecipeCompilerError(
                "discovery probe posture is over-authorized"
            )

    def evidence(self) -> dict[str, Any]:
        return {
            "image": self.image,
            "cpu_millis": self.cpu_millis,
            "memory_bytes": self.memory_bytes,
            "pids_limit": self.pids_limit,
            "deadline_ms": self.deadline_ms,
            "network": self.network,
            "read_only_project_inputs": self.read_only_project_inputs,
            "secret_refs": list(self.secret_refs),
            "host_broker_grants": list(self.host_broker_grants),
            "management_network": self.management_network,
            "docker_socket": self.docker_socket,
            "persistent_agent_authority": self.persistent_agent_authority,
            "non_root": self.non_root,
            "cap_drop_all": self.cap_drop_all,
            "no_new_privileges": self.no_new_privileges,
        }

    @property
    def content_hash(self) -> str:
        return _digest(self.evidence())
