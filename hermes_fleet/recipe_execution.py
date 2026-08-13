"""Submission-only controller service for exact-node FX8 Recipe execution."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .agency_materialization import bundle_agency_profile
from .agency_snapshot import AgencySource, acquire_agency_snapshot
from .backend_capabilities import BackendCapabilities, evaluate_capabilities
from .controller import submit_execution_package
from .execution_package import ExactExecutionPackage, serialize_execution_package
from .recipes import FleetRecipe, ResolvedRecipe


class ExactRecipeSubmissionService:
    """Resolve/package on the controller, then submit once; never execute locally."""

    def __init__(
        self,
        *,
        agency_snapshot_factory: Callable[..., Any] = acquire_agency_snapshot,
        package_builder: Callable[..., Any] = bundle_agency_profile,
        submitter: Callable[..., Any] = submit_execution_package,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._agency_snapshot = agency_snapshot_factory
        self._package_builder = package_builder
        self._submitter = submitter
        self._now_ms = now_ms or (lambda: int(time.time() * 1_000))

    async def submit(
        self,
        *,
        keryx: Any,
        requester: str,
        peer_id: str,
        execution_id: str,
        recipe: FleetRecipe,
        capabilities: BackendCapabilities,
        agency_source: AgencySource,
        target: dict[str, Any],
        policy_digest: str,
        prompt: str,
        secret_refs: list[str],
        deadline_seconds: int,
    ) -> Any:
        if type(recipe) is not FleetRecipe:
            raise ValueError("recipe must be FleetRecipe")
        if type(capabilities) is not BackendCapabilities:
            raise ValueError("capabilities must be BackendCapabilities")
        eligibility = evaluate_capabilities(recipe, capabilities)
        if not eligibility.eligible:
            raise ValueError(
                "Recipe is ineligible for destination capabilities: "
                + ",".join(eligibility.reasons)
            )
        if type(agency_source) is not AgencySource:
            raise ValueError("agency source must be an exact AgencySource")
        if type(secret_refs) is not list or any(
            type(item) is not str for item in secret_refs
        ):
            raise ValueError("secret references must be a list of strings")
        if type(deadline_seconds) is not int or not 0 < deadline_seconds <= 900:
            raise ValueError("deadline must be between 1 and 900 seconds")
        now = self._now_ms()
        if type(now) is not int or now <= 0:
            raise ValueError("controller clock is invalid")
        deadline_ms = now + deadline_seconds * 1_000
        with self._agency_snapshot(agency_source) as snapshot:
            profile = snapshot.resolve_profile(recipe.agent.name)
            if profile.version != recipe.agent.version:
                raise ValueError("Agency profile version does not satisfy exact Recipe")
            agency_bundle = self._package_builder(profile)
        resolved = ResolvedRecipe(
            recipe_hash=recipe.content_hash,
            agent=agency_bundle.resolved,
            extensions=recipe.extensions,
        )
        package = ExactExecutionPackage(
            execution_id=execution_id,
            idempotency_key=execution_id,
            resolved_recipe=resolved,
            capabilities_hash=capabilities.content_hash,
            target=target,
            authorization={
                "requester": requester,
                "operation": "fleet.hermes.run",
                "resolved_recipe_hash": resolved.content_hash,
                "policy_digest": policy_digest,
                "deadline_ms": deadline_ms,
                "secret_refs": secret_refs,
            },
            prompt=prompt,
            agency_bundle=agency_bundle,
        )
        payload = serialize_execution_package(package)
        return await self._submitter(
            keryx=keryx,
            peer_id=peer_id,
            task_id=execution_id,
            idempotency_key=execution_id,
            package_payload=payload,
            package_hash=package.content_hash,
            deadline_ms=deadline_ms,
        )
