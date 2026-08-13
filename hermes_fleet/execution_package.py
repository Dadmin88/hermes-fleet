"""Deterministic cross-node package for one exact Recipe execution."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from dataclasses import dataclass
from typing import Any, Final

from .agency_materialization import AgencyMaterializationError, ImmutableAgencyBundle
from .recipes import RecipeError, ResolvedRecipe

MEDIA_TYPE: Final = "application/vnd.hermes.fleet.execution-package.v1+tar"
EXECUTION_PACKAGE_MEDIA_TYPE: Final = MEDIA_TYPE
_MAX_PACKAGE_BYTES: Final = 3 * 1024 * 1024
_MAX_PROMPT_BYTES: Final = 16 * 1024
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TARGET_FIELDS = {
    "source",
    "network_id",
    "device_id",
    "binding_generation",
    "admission_generation",
}
_AUTHORIZATION_FIELDS = {
    "requester",
    "operation",
    "resolved_recipe_hash",
    "policy_digest",
    "deadline_ms",
    "secret_refs",
}
_HEADER_FIELDS = {
    "schema",
    "execution_id",
    "idempotency_key",
    "resolved_recipe",
    "capabilities_hash",
    "target",
    "authorization",
    "prompt",
    "agency_archive_sha256",
    "agency_archive_bytes",
}


class ExecutionPackageError(ValueError):
    """An exact execution package is malformed or has conflicting identity."""


@dataclass(frozen=True, slots=True)
class ExactExecutionPackage:
    execution_id: str
    idempotency_key: str
    resolved_recipe: ResolvedRecipe
    capabilities_hash: str
    target: dict[str, Any]
    authorization: dict[str, Any]
    prompt: str
    agency_bundle: ImmutableAgencyBundle

    def __post_init__(self) -> None:
        _identifier(self.execution_id, "execution ID")
        _identifier(self.idempotency_key, "idempotency key")
        if type(self.resolved_recipe) is not ResolvedRecipe:
            raise ExecutionPackageError("resolved Recipe is invalid")
        if not isinstance(self.agency_bundle, ImmutableAgencyBundle):
            raise ExecutionPackageError("Agency bundle is invalid")
        if self.agency_bundle.resolved != self.resolved_recipe.agent:
            raise ExecutionPackageError(
                "Agency bundle identity conflicts with resolved Recipe"
            )
        if (
            type(self.capabilities_hash) is not str
            or _HASH_RE.fullmatch(self.capabilities_hash) is None
        ):
            raise ExecutionPackageError("capabilities hash is invalid")
        object.__setattr__(self, "target", _target(self.target))
        object.__setattr__(
            self,
            "authorization",
            _authorization(
                self.authorization, recipe_hash=self.resolved_recipe.content_hash
            ),
        )
        if (
            type(self.prompt) is not str
            or not self.prompt
            or self.prompt != self.prompt.strip()
        ):
            raise ExecutionPackageError("prompt is invalid")
        try:
            prompt_bytes = self.prompt.encode("utf-8")
        except UnicodeError as error:
            raise ExecutionPackageError("prompt is invalid") from error
        if len(prompt_bytes) > _MAX_PROMPT_BYTES or any(
            ord(char) < 32 and char not in "\n\t" for char in self.prompt
        ):
            raise ExecutionPackageError("prompt exceeds the supported bound")

    @property
    def content_hash(self) -> str:
        return "sha256:" + hashlib.sha256(serialize_execution_package(self)).hexdigest()


def serialize_execution_package(package: ExactExecutionPackage) -> bytes:
    if type(package) is not ExactExecutionPackage:
        raise ExecutionPackageError("execution package is invalid")
    agency_payload = package.agency_bundle.payload
    header = {
        "schema": "fleet.execution-package.v1",
        "execution_id": package.execution_id,
        "idempotency_key": package.idempotency_key,
        "resolved_recipe": package.resolved_recipe.to_dict(),
        "capabilities_hash": package.capabilities_hash,
        "target": package.target,
        "authorization": package.authorization,
        "prompt": package.prompt,
        "agency_archive_sha256": package.agency_bundle.archive_sha256,
        "agency_archive_bytes": len(agency_payload),
    }
    header_bytes = (
        json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        _add(archive, "fleet-execution.json", header_bytes)
        _add(archive, "agency-package.tar", agency_payload)
    payload = stream.getvalue()
    if len(payload) > _MAX_PACKAGE_BYTES:
        raise ExecutionPackageError(
            "execution package exceeds the Keryx transport bound"
        )
    return payload


def parse_execution_package(payload: bytes) -> ExactExecutionPackage:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_PACKAGE_BYTES:
        raise ExecutionPackageError("execution package exceeds the supported bound")
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            members = archive.getmembers()
            if [item.name for item in members] != [
                "fleet-execution.json",
                "agency-package.tar",
            ]:
                raise ExecutionPackageError("execution package members are invalid")
            if any(not item.isfile() or item.size < 1 for item in members):
                raise ExecutionPackageError("execution package members are invalid")
            header_bytes = _read_member(archive, members[0], 256 * 1024)
            agency_payload = _read_member(archive, members[1], _MAX_PACKAGE_BYTES)
    except (tarfile.TarError, OSError) as error:
        raise ExecutionPackageError("execution package archive is invalid") from error
    try:
        header = json.loads(header_bytes, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise ExecutionPackageError("execution package header is invalid") from error
    if (
        type(header) is not dict
        or set(header) != _HEADER_FIELDS
        or header["schema"] != "fleet.execution-package.v1"
    ):
        raise ExecutionPackageError("execution package header has an unsupported shape")
    if type(header["agency_archive_bytes"]) is not int or header[
        "agency_archive_bytes"
    ] != len(agency_payload):
        raise ExecutionPackageError("Agency archive length does not match")
    digest = "sha256:" + hashlib.sha256(agency_payload).hexdigest()
    if header["agency_archive_sha256"] != digest:
        raise ExecutionPackageError("Agency archive digest does not match")
    try:
        recipe = ResolvedRecipe.from_dict(header["resolved_recipe"])
        agency = ImmutableAgencyBundle(
            resolved=recipe.agent,
            archive_sha256=digest,
            payload=agency_payload,
        )
        return ExactExecutionPackage(
            execution_id=header["execution_id"],
            idempotency_key=header["idempotency_key"],
            resolved_recipe=recipe,
            capabilities_hash=header["capabilities_hash"],
            target=header["target"],
            authorization=header["authorization"],
            prompt=header["prompt"],
            agency_bundle=agency,
        )
    except (RecipeError, AgencyMaterializationError, TypeError) as error:
        raise ExecutionPackageError("execution package identity is invalid") from error


def _target(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _TARGET_FIELDS:
        raise ExecutionPackageError("execution target has an unsupported shape")
    target = dict(value)
    for field in ("source", "network_id", "device_id"):
        _identifier(target[field], f"execution target {field}")
    for field in ("binding_generation", "admission_generation"):
        if type(target[field]) is not int or not 1 <= target[field] <= (1 << 64) - 1:
            raise ExecutionPackageError(f"execution target {field} is invalid")
    return target


def _authorization(value: object, *, recipe_hash: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _AUTHORIZATION_FIELDS:
        raise ExecutionPackageError("execution authorization has an unsupported shape")
    authorization = dict(value)
    _identifier(authorization["requester"], "execution requester")
    if authorization["operation"] != "fleet.hermes.run":
        raise ExecutionPackageError("execution authorization operation is invalid")
    if authorization["resolved_recipe_hash"] != recipe_hash:
        raise ExecutionPackageError(
            "execution authorization Recipe hash does not match"
        )
    if (
        type(authorization["policy_digest"]) is not str
        or _HASH_RE.fullmatch(authorization["policy_digest"]) is None
    ):
        raise ExecutionPackageError("execution authorization policy digest is invalid")
    if (
        type(authorization["deadline_ms"]) is not int
        or not 1 <= authorization["deadline_ms"] <= (1 << 64) - 1
    ):
        raise ExecutionPackageError("execution authorization deadline is invalid")
    references = authorization["secret_refs"]
    if type(references) is not list or len(references) > 32:
        raise ExecutionPackageError(
            "execution authorization secret references are invalid"
        )
    for reference in references:
        if (
            type(reference) is not str
            or not reference.startswith("secret://")
            or len(reference) > 512
            or any(
                character.isspace() or ord(character) < 32 for character in reference
            )
        ):
            raise ExecutionPackageError(
                "execution authorization secret references are invalid"
            )
    return authorization


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ExecutionPackageError(f"{label} is invalid")
    return value


def _add(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o600
    info.mtime = info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(content))


def _read_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, maximum: int
) -> bytes:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ExecutionPackageError("execution package member is unreadable")
    content = extracted.read(maximum + 1)
    if len(content) > maximum or len(content) != member.size:
        raise ExecutionPackageError("execution package member exceeds its bound")
    return content


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate execution package member")
        result[key] = value
    return result
