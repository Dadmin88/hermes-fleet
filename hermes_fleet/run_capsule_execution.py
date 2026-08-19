"""Phase 8 local Run Capsule orchestration over persistent Hermes Agent Instances."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .agency_materialization import ImmutableAgencyBundle
from .agent_instance import AgentInstanceBinding, AgentInstanceManager
from .context_firewall import ContextFirewallError, authorize_context_firewall
from .hermes_runs import (
    HermesFleetRuntimeBinding,
    HermesRunDeadlineExceeded,
    HermesRunError,
    HermesRunIndeterminate,
    HermesRunSubmissionUnknown,
)
from .principal_identity import PrincipalError, PrincipalRegistry
from .run_authority import (
    RunAuthorityError,
    RunAuthorityInactive,
    RunAuthorityRecord,
    RunAuthorityStale,
    RunAuthorityStore,
)
from .run_capsule import (
    DockerRunCapsuleBody,
    RunCapsuleIndeterminate,
    RunCapsuleRecord,
    RunCapsuleSpec,
    RunCapsuleStore,
)
from .scoped_memory import ScopedMemoryError, authorize_scoped_memory

_FINALIZE_SECONDS = 5.0


class RunCapsuleExecutionError(RuntimeError):
    """The local Capsule lifecycle cannot safely proceed."""


@dataclass(frozen=True, slots=True)
class RunCapsuleOutcome:
    status: str
    record: RunCapsuleRecord
    text: str | None = None
    artifacts: Mapping[str, bytes] | None = None


EvidenceVerifier = Callable[[dict[str, Any], str | None], Mapping[str, Any]]
LearningPersister = Callable[
    [AgentInstanceBinding, RunCapsuleRecord, Mapping[str, Any]],
    Mapping[str, Any] | None,
]
GrantRevoker = Callable[[RunCapsuleSpec], None]
ArtifactPersister = Callable[[RunCapsuleRecord, Mapping[str, bytes]], Mapping[str, Any]]
BodyFactory = Callable[[RunCapsuleSpec], DockerRunCapsuleBody]
RunsFactory = Callable[[str], Any]
ClientReleaser = Callable[[str], None]
AuthorityContextInspector = Callable[[RunCapsuleSpec], Mapping[str, str | None]]


def _default_evidence_verifier(
    finalization: dict[str, Any], result_text: str | None
) -> Mapping[str, Any]:
    if type(finalization) is not dict:
        raise RunCapsuleExecutionError("Hermes finalization evidence is invalid")
    run_id = finalization.get("run_id")
    status = finalization.get("status")
    if (
        type(run_id) is not str
        or not run_id
        or status not in {"completed", "failed", "cancelled"}
        or finalization.get("quiescent") is not True
    ):
        raise RunCapsuleExecutionError("Hermes quiescence evidence is invalid")
    if finalization.get("command_evidence_invalid") is True:
        raise RunCapsuleExecutionError("Hermes command evidence is invalid")
    pending = finalization.get("pending_processes", 0)
    if isinstance(pending, bool) or type(pending) is not int or pending != 0:
        raise RunCapsuleExecutionError("Hermes pending-process evidence is unsafe")
    result_hash = (
        None
        if result_text is None
        else "sha256:" + hashlib.sha256(result_text.encode()).hexdigest()
    )
    allowed = {
        "run_id",
        "status",
        "quiescent",
        "tool_calls",
        "tool_errors",
        "last_tool_error",
        "command_calls",
        "command_errors",
        "last_command_error",
        "pending_processes",
        "command_evidence_invalid",
    }
    return {
        "hermes": {key: finalization[key] for key in allowed if key in finalization},
        "result_hash": result_hash,
    }


class LocalRunCapsuleExecutor:
    """Compose persistent Agent, exact disposable body and native Hermes run lifecycle.

    Initial creation and restart recovery are deliberately separate. Recovery
    never calls ``DockerRunCapsuleBody.create_initial``.
    """

    def __init__(
        self,
        *,
        store: RunCapsuleStore,
        principals: PrincipalRegistry,
        authorities: RunAuthorityStore,
        authority_context_inspector: AuthorityContextInspector,
        instances: AgentInstanceManager,
        runs_factory: RunsFactory,
        body_factory: BodyFactory,
        now_ms: Callable[[], int],
        evidence_verifier: EvidenceVerifier = _default_evidence_verifier,
        learning_persister: LearningPersister | None = None,
        artifact_persister: ArtifactPersister | None = None,
        grant_revoker: GrantRevoker | None = None,
        client_releaser: ClientReleaser | None = None,
        finalize_timeout_seconds: float = _FINALIZE_SECONDS,
    ) -> None:
        if type(store) is not RunCapsuleStore:
            raise RunCapsuleExecutionError("Run Capsule store is invalid")
        if type(principals) is not PrincipalRegistry:
            raise RunCapsuleExecutionError("principal registry is invalid")
        if type(authorities) is not RunAuthorityStore:
            raise RunCapsuleExecutionError("RunAuthority store is invalid")
        if not callable(authority_context_inspector):
            raise RunCapsuleExecutionError("RunAuthority context inspector is invalid")
        if type(instances) is not AgentInstanceManager:
            raise RunCapsuleExecutionError("Agent Instance manager is invalid")
        for dependency, label in (
            (runs_factory, "Hermes Runs factory"),
            (body_factory, "Run Capsule body factory"),
            (now_ms, "Run Capsule executor clock"),
            (evidence_verifier, "Run Capsule evidence verifier"),
        ):
            if not callable(dependency):
                raise RunCapsuleExecutionError(f"{label} is invalid")
        if learning_persister is not None and not callable(learning_persister):
            raise RunCapsuleExecutionError("learning persister is invalid")
        if artifact_persister is not None and not callable(artifact_persister):
            raise RunCapsuleExecutionError("artifact persister is invalid")
        if grant_revoker is not None and not callable(grant_revoker):
            raise RunCapsuleExecutionError("grant revoker is invalid")
        if client_releaser is not None and not callable(client_releaser):
            raise RunCapsuleExecutionError("client releaser is invalid")
        if (
            isinstance(finalize_timeout_seconds, bool)
            or not isinstance(finalize_timeout_seconds, int | float)
            or finalize_timeout_seconds <= 0
            or finalize_timeout_seconds > 30
        ):
            raise RunCapsuleExecutionError("finalization timeout is invalid")
        self._store = store
        self._principals = principals
        self._authorities = authorities
        self._authority_context = authority_context_inspector
        self._instances = instances
        self._runs_factory = runs_factory
        self._body_factory = body_factory
        self._now_ms = now_ms
        self._verify_evidence = evidence_verifier
        self._persist_learning = learning_persister
        self._persist_artifacts = artifact_persister
        self._revoke_grants = grant_revoker
        self._release_client = client_releaser
        self._finalize_seconds = float(finalize_timeout_seconds)

    def execute_initial(
        self,
        *,
        spec: RunCapsuleSpec,
        agency_bundle: ImmutableAgencyBundle,
        prompt: str,
    ) -> RunCapsuleOutcome:
        if type(spec) is not RunCapsuleSpec:
            raise RunCapsuleExecutionError("Run Capsule spec is invalid")
        if type(agency_bundle) is not ImmutableAgencyBundle:
            raise RunCapsuleExecutionError("Agency bundle is invalid")
        if type(prompt) is not str or not prompt.strip():
            raise RunCapsuleExecutionError("Run Capsule prompt is invalid")
        try:
            principal_record = self._principals.require_current(spec.principal)
            authorize_scoped_memory(spec, principal_record)
        except PrincipalError as error:
            raise RunCapsuleExecutionError(
                "Run Capsule principal is not current"
            ) from error
        except ScopedMemoryError as error:
            raise RunCapsuleExecutionError(
                "Run Capsule memory scope is not authorizable"
            ) from error
        try:
            self._require_authority(spec, claim=True)
        except RunAuthorityError as error:
            raise RunCapsuleExecutionError(
                "Run Capsule RunAuthority is not current"
            ) from error
        record, created = self._store.admit(spec)
        if not created:
            return self.recover(spec=spec, agency_bundle=agency_bundle)

        agent = self._instances.ensure(agency_bundle)
        self._require_agent(spec, agent)
        record = self._transition(record, "agent_ready")

        body = self._body(spec)
        try:
            self._require_authority(spec)
        except RunAuthorityError as error:
            record = self._transition(
                record,
                "failed",
                evidence={"execution_outcome": "authority_inactive"},
            )
            self._persist_no_run_cleanup(record, agent, body)
            raise RunCapsuleExecutionError(
                "RunAuthority changed before body creation"
            ) from error
        try:
            handle = body.create_initial()
        except Exception as error:
            record = self._transition(
                record,
                "indeterminate",
                evidence={"indeterminate_stage": "body_creation"},
            )
            raise RunCapsuleIndeterminate(
                "Run Capsule body creation outcome is indeterminate"
            ) from error
        if handle.plan_fingerprint != spec.plan_fingerprint:
            raise RunCapsuleExecutionError("Run Capsule body plan identity changed")
        record = self._transition(
            record,
            "body_ready",
            container_id=handle.realization_id,
        )
        try:
            self._require_authority(spec)
            principal_record = self._principals.require_current(spec.principal)
            memory = authorize_scoped_memory(spec, principal_record).binding
            context = authorize_context_firewall(spec, principal_record, agent).binding
        except (
            RunAuthorityError,
            PrincipalError,
            ScopedMemoryError,
            ContextFirewallError,
        ) as error:
            record = self._transition(
                record,
                "failed",
                evidence={"execution_outcome": "authority_inactive"},
            )
            self._persist_no_run_cleanup(record, agent, body)
            raise RunCapsuleExecutionError(
                "Run authority, memory scope, or context binding changed before "
                "Hermes submission"
            ) from error

        record = self._transition(record, "run_submitting")
        client = self._client(agent.profile)
        runtime = HermesFleetRuntimeBinding(
            container_id=handle.realization_id,
            plan_fingerprint=spec.plan_fingerprint,
            image=spec.image,
            max_iterations=spec.max_iterations,
            toolsets=spec.toolsets,
        )
        try:
            run_id = client.start(
                prompt=prompt,
                session_id=f"fleet:{spec.execution_id}",
                approval_budget=(spec.approval_budget or None),
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=context,
                timeout_seconds=self._remaining_seconds(spec),
            )
        except HermesRunSubmissionUnknown as error:
            self._transition(record, "indeterminate")
            raise RunCapsuleIndeterminate(
                "Hermes run submission outcome is unknown; body retained"
            ) from error
        except Exception:
            # A definite pre-acceptance failure still requires cleanup, but only
            # after no Hermes run identity was issued.
            record = self._transition(record, "failed")
            record = self._persist_no_run_cleanup(record, agent, body)
            return RunCapsuleOutcome(status="failed", record=record)

        record = self._transition(record, "running", hermes_run_id=run_id)
        return self._continue_known_run(record, agent, body, client)

    def recover(
        self,
        *,
        spec: RunCapsuleSpec,
        agency_bundle: ImmutableAgencyBundle,
    ) -> RunCapsuleOutcome:
        """Resume exact persisted state without create or resubmit fallback."""
        if (
            type(spec) is not RunCapsuleSpec
            or type(agency_bundle) is not ImmutableAgencyBundle
        ):
            raise RunCapsuleExecutionError("Run Capsule recovery input is invalid")
        record = self._store.require_exact(spec)
        if record.state == "finalized":
            return RunCapsuleOutcome(status="finalized", record=record)
        authority_error: RunAuthorityError | None = None
        try:
            self._require_authority(spec)
        except RunAuthorityError as error:
            authority_error = error

        agent = self._instances.ensure(agency_bundle)
        self._require_agent(spec, agent)
        body = self._body(spec)

        if (
            record.state == "indeterminate"
            and record.container_id is None
            and record.hermes_run_id is None
            and dict(record.evidence or {}).get("indeterminate_stage")
            == "body_creation"
        ):
            discovered = body.find_existing_by_plan()
            if discovered is None:
                raise RunCapsuleIndeterminate(
                    "Run Capsule body creation remains indeterminate; "
                    "no exact existing body is observable"
                )
            body.recover_exact(discovered.realization_id)
            record = self._persist_no_run_cleanup(
                record,
                agent,
                body,
                recovered_container_id=discovered.realization_id,
            )
            return RunCapsuleOutcome(status="failed", record=record)

        if authority_error is not None and record.state in {"admitted", "agent_ready"}:
            if record.state == "admitted":
                record = self._transition(record, "agent_ready")
            outcome = (
                "cancelled"
                if isinstance(authority_error, RunAuthorityInactive)
                else "failed"
            )
            record = self._transition(
                record,
                "failed",
                evidence={"execution_outcome": outcome, "authority_inactive": True},
            )
            record = self._persist_no_run_cleanup(record, agent, body)
            return RunCapsuleOutcome(status=outcome, record=record)

        if record.state == "admitted":
            record = self._transition(record, "agent_ready")
        if record.state == "agent_ready":
            # Recover an already-created exact body by plan identity only.
            # A missing body is never replaced during recovery.
            discovered = body.find_existing_by_plan()
            if discovered is None:
                record = self._transition(record, "indeterminate")
                raise RunCapsuleIndeterminate(
                    "Run Capsule recovery found no exact existing body"
                )
            record = self._transition(
                record,
                "body_ready",
                container_id=discovered.realization_id,
            )

        if record.container_id is not None and record.state not in {
            "cleaned",
            "finalized",
        }:
            if record.state == "cleanup_pending":
                body.cleanup_if_present(record.container_id)
            else:
                body.recover_exact(record.container_id)

        if authority_error is not None and record.state == "body_ready":
            outcome = (
                "cancelled"
                if isinstance(authority_error, RunAuthorityInactive)
                else "failed"
            )
            record = self._transition(
                record,
                "failed",
                evidence={"execution_outcome": outcome, "authority_inactive": True},
            )
            record = self._persist_no_run_cleanup(record, agent, body)
            return RunCapsuleOutcome(status=outcome, record=record)

        if record.state == "body_ready":
            record = self._transition(record, "indeterminate")
            raise RunCapsuleIndeterminate(
                "Run Capsule recovery has no durable Hermes run identity"
            )
        if record.state == "run_submitting":
            record = self._transition(record, "indeterminate")
            raise RunCapsuleIndeterminate(
                "Hermes run submission may have occurred; resubmission is forbidden"
            )

        if record.state == "running":
            if record.hermes_run_id is None:
                raise RunCapsuleExecutionError(
                    "running Capsule lost Hermes run identity"
                )
            client = self._client(agent.profile)
            if authority_error is not None:
                try:
                    client.stop(
                        record.hermes_run_id,
                        timeout_seconds=self._finalize_seconds,
                    )
                except Exception as error:
                    self._transition(record, "indeterminate")
                    raise RunCapsuleIndeterminate(
                        "RunAuthority cancellation could not prove Hermes stop"
                    ) from error
                outcome = (
                    "cancelled"
                    if isinstance(authority_error, RunAuthorityInactive)
                    else "failed"
                )
                evidence = dict(record.evidence or {})
                evidence.update(
                    {
                        "execution_outcome": outcome,
                        "authority_inactive": True,
                    }
                )
                record = self._transition(record, "failed", evidence=evidence)
                return self._continue_post_run(record, agent, body)
            return self._continue_known_run(record, agent, body, client)

        return self._continue_post_run(record, agent, body)

    def _continue_known_run(
        self,
        record: RunCapsuleRecord,
        agent: AgentInstanceBinding,
        body: DockerRunCapsuleBody,
        client: Any,
    ) -> RunCapsuleOutcome:
        run_id = record.hermes_run_id
        if run_id is None:
            raise RunCapsuleExecutionError("Run Capsule Hermes run id is unavailable")
        text: str | None = None
        terminal_state = "terminal"
        try:
            result = client.wait(
                run_id=run_id,
                timeout_seconds=self._remaining_seconds(record.spec),
                approval_mode=("once" if record.spec.approval_budget else None),
                approval_budget=(record.spec.approval_budget or None),
            )
            text = result.text
        except HermesRunDeadlineExceeded:
            terminal_state = "timed_out"
        except HermesRunIndeterminate as error:
            self._transition(record, "indeterminate")
            raise RunCapsuleIndeterminate(
                "Hermes run status is indeterminate; body retained"
            ) from error
        except HermesRunError:
            inspection = client.inspect(run_id)
            if inspection.status not in {"completed", "failed", "cancelled"}:
                self._transition(record, "indeterminate")
                raise RunCapsuleIndeterminate(
                    "Hermes run terminal state cannot be proven"
                )
            terminal_state = (
                "failed" if inspection.status != "completed" else "terminal"
            )
            text = inspection.text
        execution_outcome = (
            "completed" if terminal_state == "terminal" else terminal_state
        )
        terminal_evidence = {
            "execution_outcome": execution_outcome,
            "result_hash": (
                None
                if text is None
                else "sha256:" + hashlib.sha256(text.encode()).hexdigest()
            ),
        }
        record = self._transition(
            record,
            terminal_state,
            evidence=terminal_evidence,
        )

        try:
            finalization = client.finalize(
                run_id,
                timeout_seconds=self._finalize_seconds,
            )
        except Exception as error:
            self._transition(record, "indeterminate")
            raise RunCapsuleIndeterminate(
                "Hermes quiescence is unproven; body retained"
            ) from error
        if (
            finalization.get("run_id") != run_id
            or finalization.get("quiescent") is not True
        ):
            self._transition(record, "indeterminate")
            raise RunCapsuleIndeterminate(
                "Hermes finalization did not prove exact-run quiescence"
            )
        quiescent_evidence = dict(record.evidence or {})
        quiescent_evidence["finalization"] = finalization
        record = self._transition(
            record,
            "quiescent",
            evidence=quiescent_evidence,
        )
        return self._continue_after_quiescence(record, agent, body, text=text)

    def _continue_post_run(
        self,
        record: RunCapsuleRecord,
        agent: AgentInstanceBinding,
        body: DockerRunCapsuleBody,
    ) -> RunCapsuleOutcome:
        if record.state in {"terminal", "timed_out", "failed", "indeterminate"}:
            if record.hermes_run_id is None:
                raise RunCapsuleIndeterminate(
                    "Run Capsule cannot prove quiescence without Hermes run identity"
                )
            client = self._client(agent.profile)
            try:
                finalization = client.finalize(
                    record.hermes_run_id,
                    timeout_seconds=self._finalize_seconds,
                )
            except Exception as error:
                raise RunCapsuleIndeterminate(
                    "Hermes quiescence remains unproven"
                ) from error
            if (
                finalization.get("run_id") != record.hermes_run_id
                or finalization.get("quiescent") is not True
            ):
                raise RunCapsuleIndeterminate(
                    "Hermes recovery finalization did not prove exact-run quiescence"
                )
            quiescent_evidence = dict(record.evidence or {})
            quiescent_evidence["finalization"] = finalization
            record = self._transition(
                record,
                "quiescent",
                evidence=quiescent_evidence,
            )
        return self._continue_after_quiescence(record, agent, body, text=None)

    def _continue_after_quiescence(
        self,
        record: RunCapsuleRecord,
        agent: AgentInstanceBinding,
        body: DockerRunCapsuleBody,
        *,
        text: str | None,
    ) -> RunCapsuleOutcome:
        artifacts: Mapping[str, bytes] | None = None
        if record.state == "quiescent":
            finalization = dict((record.evidence or {}).get("finalization", {}))
            verified = dict(self._verify_evidence(finalization, text))
            evidence = dict(record.evidence or {})
            evidence["verified"] = verified
            if record.spec.artifact_grants:
                if record.container_id is None:
                    raise RunCapsuleExecutionError(
                        "artifact-bearing Capsule lost container identity"
                    )
                artifacts = body.export_artifacts(record.container_id)
                if self._persist_artifacts is None:
                    raise RunCapsuleExecutionError(
                        "declared artifacts require a durable artifact persister"
                    )
                artifact_evidence = dict(self._persist_artifacts(record, artifacts))
                evidence["artifacts"] = artifact_evidence
            record = self._transition(
                record,
                "evidence_verified",
                evidence=evidence,
            )
        if record.state == "evidence_verified":
            verified = dict((record.evidence or {}).get("verified", {}))
            learning = (
                {"status": "skipped", "reason": "no_learning_policy"}
                if self._persist_learning is None
                else self._persist_learning(agent, record, verified)
            )
            learning_document = (
                {"status": "persisted"} if learning is None else dict(learning)
            )
            evidence = dict(record.evidence or {})
            evidence["learning"] = learning_document
            record = self._transition(
                record,
                "learning_persisted",
                evidence=evidence,
                learning_persisted=True,
            )
        if record.state == "learning_persisted":
            if self._revoke_grants is None:
                if (
                    record.spec.secret_refs
                    or record.spec.host_broker_grants
                    or record.spec.approval_budget
                ):
                    raise RunCapsuleExecutionError(
                        "temporary grants require an explicit revocation callback"
                    )
            else:
                self._revoke_grants(record.spec)
            record = self._transition(
                record,
                "grants_revoked",
                grants_revoked=True,
            )
        if record.state == "grants_revoked":
            record = self._transition(record, "cleanup_pending")
        if record.state == "cleanup_pending":
            if record.container_id is None:
                raise RunCapsuleExecutionError(
                    "cleanup pending Capsule lost container id"
                )
            body.cleanup_if_present(record.container_id)
            self._release(agent.profile)
            record = self._transition(record, "cleaned")
        if record.state == "cleaned":
            record = self._transition(record, "finalized")
        if record.state != "finalized":
            raise RunCapsuleExecutionError(
                f"Run Capsule stopped in unexpected state {record.state}"
            )
        evidence = dict(record.evidence or {})
        execution_status = str(evidence.get("execution_outcome") or "completed")
        return RunCapsuleOutcome(
            status=execution_status,
            record=record,
            text=text,
            artifacts=artifacts,
        )

    def _persist_no_run_cleanup(
        self,
        record: RunCapsuleRecord,
        agent: AgentInstanceBinding,
        body: DockerRunCapsuleBody,
        *,
        recovered_container_id: str | None = None,
    ) -> RunCapsuleRecord:
        # A definite failure before a Hermes run has no learning/evidence to
        # preserve. We still follow revoke -> cleanup ordering.
        evidence = dict(record.evidence or {})
        evidence["finalization"] = {
            "status": "not_started",
            "quiescent": True,
        }
        record = self._transition(
            record,
            "quiescent",
            container_id=recovered_container_id,
            evidence=evidence,
        )
        evidence = dict(record.evidence or {})
        evidence["verified"] = {"status": "not_started"}
        record = self._transition(
            record,
            "evidence_verified",
            evidence=evidence,
        )
        evidence = dict(record.evidence or {})
        evidence["learning"] = {"status": "skipped"}
        record = self._transition(
            record,
            "learning_persisted",
            evidence=evidence,
            learning_persisted=True,
        )
        if self._revoke_grants is not None:
            self._revoke_grants(record.spec)
        elif (
            record.spec.secret_refs
            or record.spec.host_broker_grants
            or record.spec.approval_budget
        ):
            raise RunCapsuleExecutionError(
                "temporary grants require an explicit revocation callback"
            )
        record = self._transition(
            record,
            "grants_revoked",
            grants_revoked=True,
        )
        record = self._transition(record, "cleanup_pending")
        if record.container_id is not None:
            body.cleanup_if_present(record.container_id)
        self._release(agent.profile)
        record = self._transition(record, "cleaned")
        return self._transition(record, "finalized")

    def _transition(
        self,
        record: RunCapsuleRecord,
        state: str,
        **changes: Any,
    ) -> RunCapsuleRecord:
        return self._store.transition(
            record.spec,
            expected_generation=record.generation,
            state=state,
            **changes,
        )

    def _require_authority(
        self,
        spec: RunCapsuleSpec,
        *,
        claim: bool = False,
    ) -> RunAuthorityRecord:
        try:
            self._principals.require_current(spec.principal)
        except PrincipalError as error:
            raise RunAuthorityStale("RunAuthority principal is stale") from error
        context = self._authority_context(spec)
        required_context = {
            "policy_digest",
            "capabilities_hash",
            "target_digest",
        }
        if (
            type(context) is not dict
            or not required_context.issubset(context)
            or set(context) - required_context - {"provider", "model"}
        ):
            raise RunAuthorityError("RunAuthority context evidence is invalid")
        record = self._authorities.require_active(
            spec.run_authority_hash,
            policy_digest=context["policy_digest"],
            capabilities_hash=context["capabilities_hash"],
            target_digest=context["target_digest"],
        )
        record.authority.validate_context(
            principal=spec.principal,
            agent_instance_id=spec.agent_instance_id,
            recipe_hash=spec.recipe_hash,
            resolved_recipe_hash=spec.resolved_recipe_hash,
            policy_digest=context["policy_digest"],
            capabilities_hash=context["capabilities_hash"],
            target_digest=context["target_digest"],
            now_ms=self._now_ms(),
            provider=context.get("provider"),
            model=context.get("model"),
        )
        record.authority.validate_capsule(spec)
        if claim:
            return self._authorities.claim_capsule(spec.run_authority_hash, spec)
        return record

    def _body(self, spec: RunCapsuleSpec) -> DockerRunCapsuleBody:
        body = self._body_factory(spec)
        if type(body) is not DockerRunCapsuleBody:
            raise RunCapsuleExecutionError(
                "Run Capsule body factory returned invalid body"
            )
        return body

    def _client(self, profile: str) -> Any:
        client = self._runs_factory(profile)
        for method in ("start", "wait", "inspect", "finalize", "stop"):
            if not callable(getattr(client, method, None)):
                raise RunCapsuleExecutionError("Hermes Runs client is incomplete")
        return client

    def _release(self, profile: str) -> None:
        if self._release_client is not None:
            self._release_client(profile)

    @staticmethod
    def _require_agent(
        spec: RunCapsuleSpec,
        binding: AgentInstanceBinding,
    ) -> None:
        if type(binding) is not AgentInstanceBinding:
            raise RunCapsuleExecutionError("Agent Instance binding is invalid")
        if binding.instance_id != spec.agent_instance_id:
            raise RunCapsuleExecutionError(
                "Run Capsule Agent Instance identity changed"
            )

    def _remaining_seconds(self, spec: RunCapsuleSpec) -> float:
        now = self._now_ms()
        if isinstance(now, bool) or type(now) is not int or now < 0:
            raise RunCapsuleExecutionError("Run Capsule executor clock is invalid")
        remaining = (spec.deadline_ms - now) / 1000
        if remaining <= 0:
            raise HermesRunDeadlineExceeded("Run Capsule deadline has expired")
        return remaining
