"""Runtime-neutral FleetRecipe and immutable ResolvedRecipe contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

_MAX_DOCUMENT_BYTES: Final[int] = 256 * 1024
_MAX_TEXT: Final[int] = 512
_MAX_EXTENSIONS: Final[int] = 64
_MAX_DEPTH: Final[int] = 12
_MAX_COLLECTION: Final[int] = 256
_MAX_STRING: Final[int] = 16_384
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_EXTENSION_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)


class RecipeError(ValueError):
    """A Recipe document violates the bounded public contract."""


class _DuplicateKey(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise RecipeError(f"non-standard JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _load(payload: str) -> dict[str, Any]:
    if type(payload) is not str or not payload:
        raise RecipeError("Recipe document exceeds the supported bound")
    try:
        if len(payload.encode("utf-8")) > _MAX_DOCUMENT_BYTES:
            raise RecipeError("Recipe document exceeds the supported bound")
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, _DuplicateKey, RecursionError) as error:
        raise RecipeError("Recipe document is invalid JSON") from error
    if type(value) is not dict:
        raise RecipeError("Recipe document must be an object")
    return cast(dict[str, Any], value)


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(cast(dict, value)) != keys:
        raise RecipeError(f"{label} has an unsupported shape")
    return cast(dict[str, Any], value)


def _text(value: object, label: str, *, maximum: int = _MAX_TEXT) -> str:
    try:
        encoded_length = len(value.encode("utf-8")) if type(value) is str else -1
    except UnicodeError as error:
        raise RecipeError(f"{label} is invalid") from error
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or encoded_length > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise RecipeError(f"{label} is invalid")
    return value


def _name(value: object, label: str) -> str:
    text = _text(value, label, maximum=128)
    if _NAME_RE.fullmatch(text) is None:
        raise RecipeError(f"{label} is invalid")
    return text


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= (1 << 63) - 1:
        raise RecipeError(f"{label} is invalid")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not list or not value or len(value) > 32:
        raise RecipeError(f"{label} is invalid")
    values = tuple(_name(item, label) for item in cast(list, value))
    if len(values) != len(set(values)):
        raise RecipeError(f"{label} contains duplicates")
    return values


def _json_value(value: object, *, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise RecipeError("extension data exceeds the supported depth")
    if value is None or type(value) in {bool, int, str}:
        if type(value) is int and not -(1 << 63) <= cast(int, value) <= (1 << 63) - 1:
            raise RecipeError("extension integer exceeds the supported bound")
        if type(value) is str:
            try:
                if len(cast(str, value).encode("utf-8")) > _MAX_STRING:
                    raise RecipeError("extension text exceeds the supported bound")
            except UnicodeError as error:
                raise RecipeError("extension text is invalid") from error
        return value
    if type(value) is float:
        raise RecipeError("extension floating-point values are unsupported")
    if type(value) is list:
        items = cast(list, value)
        if len(items) > _MAX_COLLECTION:
            raise RecipeError("extension list exceeds the supported bound")
        return tuple(_json_value(item, depth=depth + 1) for item in items)
    if type(value) is dict:
        mapping = cast(dict, value)
        if len(mapping) > _MAX_COLLECTION:
            raise RecipeError("extension object exceeds the supported bound")
        normalized: dict[str, Any] = {}
        for key, item in mapping.items():
            try:
                key_length = len(key.encode("utf-8")) if type(key) is str else -1
            except UnicodeError as error:
                raise RecipeError("extension object key is invalid") from error
            if type(key) is not str or not key or key_length > _MAX_TEXT:
                raise RecipeError("extension object key is invalid")
            normalized[key] = _json_value(item, depth=depth + 1)
        return MappingProxyType(normalized)
    raise RecipeError("extension data must be JSON-compatible")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _extensions(value: object) -> Mapping[str, Any]:
    if type(value) is not dict or len(cast(dict, value)) > _MAX_EXTENSIONS:
        raise RecipeError("extensions exceed the supported bound")
    normalized: dict[str, Any] = {}
    for key, item in cast(dict, value).items():
        if type(key) is not str or _EXTENSION_RE.fullmatch(key) is None:
            raise RecipeError("extension keys must be namespaced")
        normalized[key] = _json_value(item)
    return MappingProxyType(normalized)


def _canonical(document: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            _plain(document),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise RecipeError("Recipe document cannot be canonicalized") from error
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise RecipeError("Recipe document exceeds the supported bound")
    return payload


def _digest(document: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical(document)).hexdigest()}"


@dataclass(frozen=True, slots=True)
class AgentRequirement:
    kind: str
    name: str
    version: str

    def __post_init__(self) -> None:
        if self.kind != "agency_profile":
            raise RecipeError("agent requirement kind is unsupported")
        _name(self.name, "agent name")
        _text(self.version, "agent version", maximum=128)

    @classmethod
    def from_dict(cls, value: object) -> AgentRequirement:
        item = _exact_object(value, {"kind", "name", "version"}, "agent requirement")
        if item["kind"] != "agency_profile":
            raise RecipeError("agent requirement kind is unsupported")
        return cls(
            kind="agency_profile",
            name=_name(item["name"], "agent name"),
            version=_text(item["version"], "agent version", maximum=128),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "version": self.version}


@dataclass(frozen=True, slots=True)
class EnvironmentRequirement:
    os: tuple[str, ...]
    architecture: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.os) not in (list, tuple) or type(self.architecture) not in (
            list,
            tuple,
        ):
            raise RecipeError("environment requirement is invalid")
        object.__setattr__(self, "os", _string_list(list(self.os), "operating systems"))
        object.__setattr__(
            self,
            "architecture",
            _string_list(list(self.architecture), "architectures"),
        )

    @classmethod
    def from_dict(cls, value: object) -> EnvironmentRequirement:
        item = _exact_object(value, {"os", "architecture"}, "environment requirement")
        return cls(
            os=_string_list(item["os"], "operating systems"),
            architecture=_string_list(item["architecture"], "architectures"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"os": list(self.os), "architecture": list(self.architecture)}


@dataclass(frozen=True, slots=True)
class ResourceRequirement:
    cpu_millis: int
    memory_bytes: int

    def __post_init__(self) -> None:
        _positive_int(self.cpu_millis, "CPU requirement")
        _positive_int(self.memory_bytes, "memory requirement")

    @classmethod
    def from_dict(cls, value: object) -> ResourceRequirement:
        item = _exact_object(
            value, {"cpu_millis", "memory_bytes"}, "resource requirement"
        )
        return cls(
            cpu_millis=_positive_int(item["cpu_millis"], "CPU requirement"),
            memory_bytes=_positive_int(item["memory_bytes"], "memory requirement"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"cpu_millis": self.cpu_millis, "memory_bytes": self.memory_bytes}


@dataclass(frozen=True, slots=True)
class SecurityRequirement:
    isolation: str
    network: str

    def __post_init__(self) -> None:
        _name(self.isolation, "isolation requirement")
        _name(self.network, "network requirement")

    @classmethod
    def from_dict(cls, value: object) -> SecurityRequirement:
        item = _exact_object(value, {"isolation", "network"}, "security requirement")
        isolation = _name(item["isolation"], "isolation requirement")
        network = _name(item["network"], "network requirement")
        return cls(isolation=isolation, network=network)

    def to_dict(self) -> dict[str, Any]:
        return {"isolation": self.isolation, "network": self.network}


@dataclass(frozen=True, slots=True)
class FleetRecipe:
    agent: AgentRequirement
    environment: EnvironmentRequirement
    resources: ResourceRequirement
    security: SecurityRequirement
    extensions: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.agent) is not AgentRequirement:
            raise RecipeError("agent requirement is invalid")
        if type(self.environment) is not EnvironmentRequirement:
            raise RecipeError("environment requirement is invalid")
        if type(self.resources) is not ResourceRequirement:
            raise RecipeError("resource requirement is invalid")
        if type(self.security) is not SecurityRequirement:
            raise RecipeError("security requirement is invalid")
        object.__setattr__(self, "extensions", _extensions(_plain(self.extensions)))
        _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> FleetRecipe:
        item = _exact_object(
            value,
            {"schema", "agent", "environment", "resources", "security", "extensions"},
            "FleetRecipe",
        )
        if item["schema"] != "fleet.recipe.v1":
            raise RecipeError("FleetRecipe schema is unsupported")
        recipe = cls(
            agent=AgentRequirement.from_dict(item["agent"]),
            environment=EnvironmentRequirement.from_dict(item["environment"]),
            resources=ResourceRequirement.from_dict(item["resources"]),
            security=SecurityRequirement.from_dict(item["security"]),
            extensions=_extensions(item["extensions"]),
        )
        return recipe

    @classmethod
    def from_json(cls, payload: str) -> FleetRecipe:
        return cls.from_dict(_load(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "fleet.recipe.v1",
            "agent": self.agent.to_dict(),
            "environment": self.environment.to_dict(),
            "resources": self.resources.to_dict(),
            "security": self.security.to_dict(),
            "extensions": _plain(self.extensions),
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8")

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ResolvedAgencyProfile:
    repository: str
    revision: str
    name: str
    version: str
    content_digest: str

    def __post_init__(self) -> None:
        _text(self.repository, "Agency repository", maximum=2048)
        if (
            type(self.revision) is not str
            or _REVISION_RE.fullmatch(self.revision) is None
        ):
            raise RecipeError("Agency revision must be an exact full object ID")
        _name(self.name, "resolved agent name")
        _text(self.version, "resolved agent version", maximum=128)
        if any(character in self.version for character in "<>=~^*, "):
            raise RecipeError("resolved agent version must be exact")
        if (
            type(self.content_digest) is not str
            or _HASH_RE.fullmatch(self.content_digest) is None
        ):
            raise RecipeError("resolved content digest is invalid")

    @classmethod
    def from_dict(cls, value: object) -> ResolvedAgencyProfile:
        item = _exact_object(
            value,
            {"kind", "repository", "revision", "name", "version", "content_digest"},
            "resolved agent",
        )
        if item["kind"] != "agency_profile":
            raise RecipeError("resolved agent kind is unsupported")
        return cls(
            repository=_text(item["repository"], "Agency repository", maximum=2048),
            revision=_text(item["revision"], "Agency revision", maximum=64),
            name=_name(item["name"], "resolved agent name"),
            version=_text(item["version"], "resolved agent version", maximum=128),
            content_digest=_text(item["content_digest"], "content digest", maximum=71),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "agency_profile",
            "repository": self.repository,
            "revision": self.revision,
            "name": self.name,
            "version": self.version,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class ResolvedRecipe:
    recipe_hash: str
    agent: ResolvedAgencyProfile
    extensions: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            type(self.recipe_hash) is not str
            or _HASH_RE.fullmatch(self.recipe_hash) is None
        ):
            raise RecipeError("Recipe hash is invalid")
        if type(self.agent) is not ResolvedAgencyProfile:
            raise RecipeError("resolved agent is invalid")
        object.__setattr__(self, "extensions", _extensions(_plain(self.extensions)))
        _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> ResolvedRecipe:
        item = _exact_object(
            value,
            {"schema", "recipe_hash", "agent", "extensions"},
            "ResolvedRecipe",
        )
        if item["schema"] != "fleet.resolved-recipe.v1":
            raise RecipeError("ResolvedRecipe schema is unsupported")
        return cls(
            recipe_hash=_text(item["recipe_hash"], "Recipe hash", maximum=71),
            agent=ResolvedAgencyProfile.from_dict(item["agent"]),
            extensions=_extensions(item["extensions"]),
        )

    @classmethod
    def from_json(cls, payload: str) -> ResolvedRecipe:
        return cls.from_dict(_load(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": self.recipe_hash,
            "agent": self.agent.to_dict(),
            "extensions": _plain(self.extensions),
        }

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8")

    @property
    def content_hash(self) -> str:
        return _digest(self.to_dict())
