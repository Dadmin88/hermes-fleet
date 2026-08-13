"""Provider-neutral execution-backend capability contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .recipes import (
    FleetRecipe,
    RecipeError,
    _canonical,
    _digest,
    _exact_object,
    _extensions,
    _load,
    _name,
    _plain,
    _positive_int,
    _string_list,
    _text,
)

_BACKEND_KIND_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
)


class CapabilityError(ValueError):
    """A backend capability document violates the public contract."""


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise CapabilityError(f"{label} is invalid")
    return value


def _backend_kind(value: object) -> str:
    kind = _text(value, "backend kind", maximum=256)
    if _BACKEND_KIND_RE.fullmatch(kind) is None:
        raise CapabilityError("backend kind is invalid")
    return kind


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    backend_kind: str
    os: str
    architecture: str
    isolation: tuple[str, ...]
    network: tuple[str, ...]
    cpu_millis: int
    memory_bytes: int
    ephemeral_root: bool
    read_only_inputs: bool
    agency_profile: bool
    artifacts: bool
    extensions: Mapping[str, Any]

    def __post_init__(self) -> None:
        try:
            _backend_kind(self.backend_kind)
            _name(self.os, "backend operating system")
            _name(self.architecture, "backend architecture")
            if not self.isolation or not self.network:
                raise CapabilityError("backend guarantee sets cannot be empty")
            _positive_int(self.cpu_millis, "backend CPU capacity")
            _positive_int(self.memory_bytes, "backend memory capacity")
            _bool(self.ephemeral_root, "ephemeral-root guarantee")
            _bool(self.read_only_inputs, "read-only-input guarantee")
            _bool(self.agency_profile, "Agency materialization capability")
            _bool(self.artifacts, "artifact capability")
            object.__setattr__(self, "extensions", _extensions(_plain(self.extensions)))
            _canonical(self.to_dict())
        except RecipeError as error:
            raise CapabilityError(str(error)) from error

    @classmethod
    def from_dict(cls, value: object) -> BackendCapabilities:
        try:
            item = _exact_object(
                value,
                {
                    "schema",
                    "backend_kind",
                    "platform",
                    "isolation",
                    "network",
                    "resources",
                    "filesystem",
                    "materialization",
                    "extensions",
                },
                "BackendCapabilities",
            )
            if item["schema"] != "fleet.backend-capabilities.v1":
                raise CapabilityError("BackendCapabilities schema is unsupported")
            platform = _exact_object(
                item["platform"], {"os", "architecture"}, "platform"
            )
            resources = _exact_object(
                item["resources"], {"cpu_millis", "memory_bytes"}, "resources"
            )
            filesystem = _exact_object(
                item["filesystem"],
                {"ephemeral_root", "read_only_inputs"},
                "filesystem guarantees",
            )
            materialization = _exact_object(
                item["materialization"],
                {"agency_profile", "artifacts"},
                "materialization capabilities",
            )
            return cls(
                backend_kind=_backend_kind(item["backend_kind"]),
                os=_name(platform["os"], "backend operating system"),
                architecture=_name(platform["architecture"], "backend architecture"),
                isolation=_string_list(item["isolation"], "isolation guarantees"),
                network=_string_list(item["network"], "network guarantees"),
                cpu_millis=_positive_int(
                    resources["cpu_millis"], "backend CPU capacity"
                ),
                memory_bytes=_positive_int(
                    resources["memory_bytes"], "backend memory capacity"
                ),
                ephemeral_root=_bool(
                    filesystem["ephemeral_root"], "ephemeral-root guarantee"
                ),
                read_only_inputs=_bool(
                    filesystem["read_only_inputs"], "read-only-input guarantee"
                ),
                agency_profile=_bool(
                    materialization["agency_profile"],
                    "Agency materialization capability",
                ),
                artifacts=_bool(materialization["artifacts"], "artifact capability"),
                extensions=_extensions(item["extensions"]),
            )
        except RecipeError as error:
            raise CapabilityError(str(error)) from error

    @classmethod
    def from_json(cls, payload: str) -> BackendCapabilities:
        try:
            return cls.from_dict(_load(payload))
        except RecipeError as error:
            raise CapabilityError(str(error)) from error

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": self.backend_kind,
            "platform": {"os": self.os, "architecture": self.architecture},
            "isolation": list(self.isolation),
            "network": list(self.network),
            "resources": {
                "cpu_millis": self.cpu_millis,
                "memory_bytes": self.memory_bytes,
            },
            "filesystem": {
                "ephemeral_root": self.ephemeral_root,
                "read_only_inputs": self.read_only_inputs,
            },
            "materialization": {
                "agency_profile": self.agency_profile,
                "artifacts": self.artifacts,
            },
            "extensions": _plain(self.extensions),
        }

    def to_json(self) -> str:
        try:
            return _canonical(self.to_dict()).decode("utf-8")
        except RecipeError as error:
            raise CapabilityError(str(error)) from error

    @property
    def content_hash(self) -> str:
        try:
            return _digest(self.to_dict())
        except RecipeError as error:
            raise CapabilityError(str(error)) from error


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    eligible: bool
    reasons: tuple[str, ...]


def evaluate_capabilities(
    recipe: FleetRecipe, capabilities: BackendCapabilities
) -> CapabilityMatch:
    """Evaluate hard eligibility only; never rank or select a destination."""
    if type(recipe) is not FleetRecipe:
        raise TypeError("recipe must be a FleetRecipe")
    if type(capabilities) is not BackendCapabilities:
        raise TypeError("capabilities must be BackendCapabilities")
    reasons: set[str] = set()
    if capabilities.os not in recipe.environment.os:
        reasons.add("os_unsupported")
    if capabilities.architecture not in recipe.environment.architecture:
        reasons.add("architecture_unsupported")
    if recipe.security.isolation not in capabilities.isolation:
        reasons.add("isolation_unsupported")
    if recipe.security.network not in capabilities.network:
        reasons.add("network_unsupported")
    if capabilities.cpu_millis < recipe.resources.cpu_millis:
        reasons.add("cpu_insufficient")
    if capabilities.memory_bytes < recipe.resources.memory_bytes:
        reasons.add("memory_insufficient")
    if recipe.agent.kind == "agency_profile" and not capabilities.agency_profile:
        reasons.add("profile_materialization_unsupported")
    ordered = tuple(sorted(reasons))
    return CapabilityMatch(eligible=not ordered, reasons=ordered)
