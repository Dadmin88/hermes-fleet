from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from hermes_fleet.agency_materialization import ImmutableAgencyBundle
from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.recipes import FleetRecipe, ResolvedAgencyProfile


def recipe() -> FleetRecipe:
    return FleetRecipe.from_dict(
        {
            "schema": "fleet.recipe.v1",
            "agent": {
                "kind": "agency_profile",
                "name": "acceptance",
                "version": "1.0.0",
            },
            "environment": {"os": ["linux"], "architecture": ["x86_64"]},
            "resources": {"cpu_millis": 1000, "memory_bytes": 1000},
            "security": {"isolation": "process", "network": "provider"},
            "extensions": {},
        }
    )


def capabilities() -> BackendCapabilities:
    from hermes_fleet.host_profile_capabilities import host_profile_capabilities

    return host_profile_capabilities(
        logical_cpus=2,
        memory_bytes=10_000,
        operating_system="linux",
        architecture="x86_64",
    )


def test_submission_service_builds_exact_package_and_submits_once(
    tmp_path: Path,
) -> None:
    from hermes_fleet.recipe_execution import ExactRecipeSubmissionService

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "distribution.yaml").write_text("name: acceptance\nversion: 1.0.0\n")
    agency = AgencyProfilePackage(
        source=AgencySource("https://example.invalid/agency.git", "a" * 40),
        name="acceptance",
        version="1.0.0",
        content_digest="unused",
        category="test",
        priority="normal",
        capabilities=(),
        distribution_path=".",
        local_path=profile,
    )
    submitted: list[dict] = []

    @contextmanager
    def snapshot(_source):
        yield SimpleNamespace(resolve_profile=lambda name: agency)

    async def submit(**kwargs):
        submitted.append(kwargs)
        return SimpleNamespace(task_id="execution-1")

    service = ExactRecipeSubmissionService(
        agency_snapshot_factory=snapshot,
        package_builder=lambda package: ImmutableAgencyBundle(
            resolved=ResolvedAgencyProfile(
                repository=package.source.repository,
                revision=package.source.revision,
                name=package.name,
                version=package.version,
                content_digest="sha256:" + "2" * 64,
            ),
            archive_sha256="sha256:" + hashlib.sha256(b"agency").hexdigest(),
            payload=b"agency",
        ),
        submitter=submit,
        now_ms=lambda: 10_000,
    )
    result = asyncio.run(
        service.submit(
            keryx=object(),
            requester="peer-controller-1",
            peer_id="peer-worker-1",
            execution_id="execution-1",
            recipe=recipe(),
            capabilities=capabilities(),
            agency_source=AgencySource("https://example.invalid/agency.git", "a" * 40),
            target={
                "source": "nodescale",
                "network_id": "network-1",
                "device_id": "device-1",
                "binding_generation": 7,
                "admission_generation": 9,
            },
            policy_digest="sha256:" + "4" * 64,
            prompt="Return FX8_OK.",
            secret_refs=[],
            deadline_seconds=30,
        )
    )

    assert result.task_id == "execution-1"
    assert len(submitted) == 1
    assert submitted[0]["peer_id"] == "peer-worker-1"
    assert submitted[0]["task_id"] == "execution-1"
    assert submitted[0]["idempotency_key"] == "execution-1"
    assert submitted[0]["deadline_ms"] == 40_000
    assert submitted[0]["package_hash"].startswith("sha256:")
    assert type(submitted[0]["package_payload"]) is bytes


def test_submission_rejects_ineligible_capabilities_before_transport() -> None:
    from hermes_fleet.recipe_execution import ExactRecipeSubmissionService

    called: list[str] = []

    @contextmanager
    def snapshot(_source):
        called.append("agency")
        yield None

    async def submit(**_kwargs):
        called.append("submit")

    incompatible = BackendCapabilities(
        backend_kind="fleet.dev/profile-runs",
        os="linux",
        architecture="x86_64",
        isolation=("process",),
        network=("none",),
        cpu_millis=2_000,
        memory_bytes=10_000,
        ephemeral_root=False,
        read_only_inputs=True,
        agency_profile=True,
        artifacts=True,
        extensions={},
    )
    service = ExactRecipeSubmissionService(
        agency_snapshot_factory=snapshot,
        package_builder=lambda package: package,
        submitter=submit,
        now_ms=lambda: 10_000,
    )
    try:
        asyncio.run(
            service.submit(
                keryx=object(),
                requester="peer-controller-1",
                peer_id="peer-worker-1",
                execution_id="execution-1",
                recipe=recipe(),
                capabilities=incompatible,
                agency_source=AgencySource(
                    "https://example.invalid/agency.git", "a" * 40
                ),
                target={
                    "source": "nodescale",
                    "network_id": "network-1",
                    "device_id": "device-1",
                    "binding_generation": 7,
                    "admission_generation": 9,
                },
                policy_digest="sha256:" + "4" * 64,
                prompt="Return FX8_OK.",
                secret_refs=[],
                deadline_seconds=30,
            )
        )
    except ValueError as error:
        assert "ineligible" in str(error)
    else:
        raise AssertionError("ineligible Recipe must fail")
    assert called == []
