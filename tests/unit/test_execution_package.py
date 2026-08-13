from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from hermes_fleet.agency_materialization import ImmutableAgencyBundle
from hermes_fleet.execution_package import (
    MEDIA_TYPE,
    ExactExecutionPackage,
    ExecutionPackageError,
    parse_execution_package,
    serialize_execution_package,
)
from hermes_fleet.recipes import ResolvedRecipe

HASH_1 = "sha256:" + "1" * 64
HASH_2 = "sha256:" + "2" * 64
HASH_3 = "sha256:" + "3" * 64


def recipe() -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": HASH_1,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "a" * 40,
                "name": "acceptance",
                "version": "1.0.0",
                "content_digest": HASH_2,
            },
            "extensions": {},
        }
    )


def package(
    *, agency_payload: bytes = b"exact agency archive"
) -> ExactExecutionPackage:
    resolved = recipe()
    return ExactExecutionPackage(
        execution_id="execution-1",
        idempotency_key="operator-request-1",
        resolved_recipe=resolved,
        capabilities_hash=HASH_3,
        target={
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "binding_generation": 7,
            "admission_generation": 9,
        },
        authorization={
            "requester": "operator-1",
            "operation": "fleet.hermes.run",
            "resolved_recipe_hash": resolved.content_hash,
            "policy_digest": HASH_3,
            "deadline_ms": 20_000,
            "secret_refs": ["secret://worker/provider-key"],
        },
        prompt="Return exactly HERMES_FLEET_FX8_NITRO_OK",
        agency_bundle=ImmutableAgencyBundle(
            resolved=resolved.agent,
            archive_sha256="sha256:" + hashlib.sha256(agency_payload).hexdigest(),
            payload=agency_payload,
        ),
    )


def test_execution_package_round_trip_is_deterministic_and_exact() -> None:
    value = package()

    first = serialize_execution_package(value)
    second = serialize_execution_package(value)
    restored = parse_execution_package(first)

    assert MEDIA_TYPE == "application/vnd.hermes.fleet.execution-package.v1+tar"
    assert first == second
    assert restored == value
    assert restored.authorization["requester"] == "operator-1"
    assert restored.authorization["secret_refs"] == ["secret://worker/provider-key"]
    assert restored.content_hash == "sha256:" + hashlib.sha256(first).hexdigest()


def test_execution_package_rejects_conflicting_agency_identity() -> None:
    value = package()
    other = recipe().agent.to_dict()
    other["name"] = "other"

    with pytest.raises(ExecutionPackageError, match="conflicts"):
        ExactExecutionPackage(
            execution_id=value.execution_id,
            idempotency_key=value.idempotency_key,
            resolved_recipe=value.resolved_recipe,
            capabilities_hash=value.capabilities_hash,
            target=value.target,
            authorization=value.authorization,
            prompt=value.prompt,
            agency_bundle=ImmutableAgencyBundle(
                resolved=type(value.resolved_recipe.agent).from_dict(other),
                archive_sha256=value.agency_bundle.archive_sha256,
                payload=value.agency_bundle.payload,
            ),
        )


def test_execution_package_rejects_tampered_agency_bytes() -> None:
    payload = bytearray(serialize_execution_package(package()))
    index = bytes(payload).find(b"exact agency archive")
    assert index >= 0
    payload[index] ^= 1

    with pytest.raises(ExecutionPackageError, match="digest"):
        parse_execution_package(bytes(payload))


def test_execution_package_rejects_duplicate_header_member() -> None:
    original = serialize_execution_package(package())
    with tarfile.open(fileobj=io.BytesIO(original), mode="r:") as archive:
        members = archive.getmembers()
        header_file = archive.extractfile(members[0])
        agency_file = archive.extractfile(members[1])
        assert header_file is not None and agency_file is not None
        header = header_file.read()
        agency = agency_file.read()
    header = header.replace(
        b'"execution_id":"execution-1"',
        b'"execution_id":"execution-1","execution_id":"execution-2"',
    )
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in (
            ("fleet-execution.json", header),
            ("agency-package.tar", agency),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    with pytest.raises(ExecutionPackageError, match="header"):
        parse_execution_package(stream.getvalue())


@pytest.mark.parametrize(
    "change, message",
    [
        ({"execution_id": "bad id"}, "execution ID"),
        ({"capabilities_hash": HASH_1[:-1]}, "capabilities hash"),
        ({"prompt": " "}, "prompt"),
        ({"target": {"source": "nodescale"}}, "target"),
    ],
)
def test_execution_package_rejects_invalid_authority(change, message) -> None:
    value = package()
    fields = {
        "execution_id": value.execution_id,
        "idempotency_key": value.idempotency_key,
        "resolved_recipe": value.resolved_recipe,
        "capabilities_hash": value.capabilities_hash,
        "target": value.target,
        "authorization": value.authorization,
        "prompt": value.prompt,
        "agency_bundle": value.agency_bundle,
        **change,
    }
    with pytest.raises(ExecutionPackageError, match=message):
        ExactExecutionPackage(**fields)


def test_execution_package_rejects_transport_oversize() -> None:
    value = package(agency_payload=b"x" * (3 * 1024 * 1024))
    with pytest.raises(ExecutionPackageError, match="transport bound"):
        serialize_execution_package(value)
