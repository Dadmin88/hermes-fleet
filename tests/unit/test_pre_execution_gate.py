from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hermes_fleet.pre_execution_gate import (
    ADMITTED,
    READY,
    DestinationAdmissionDecision,
    DeterministicPolicyDecision,
    FleetFinalDecision,
    PreExecutionContext,
    PreExecutionGate,
    PreExecutionGateStale,
    PreExecutionPermit,
    PreExecutionPermitSealer,
    PreExecutionRequest,
)
from hermes_fleet.run_authority import RunAuthorityStore
from hermes_fleet.security_event import DeterministicHardDeny
from hermes_fleet.templar import (
    ALLOW,
    DENY,
    REVIEW,
    TEMPLAR_BACKEND_RESPONSE_SCHEMA,
    TemplarCore,
    TemplarEvaluatorIdentity,
    TemplarPolicyRef,
)
from tests.unit.test_run_authority import PRINCIPAL, authority
from tests.unit.test_run_capsule_execution import (
    PERMIT_SEALER,
)
from tests.unit.test_run_capsule_execution import (
    harness as capsule_harness,
)


def h(character: str) -> str:
    return "sha256:" + character * 64


TEMPLAR_POLICY = TemplarPolicyRef(
    policy_id="templar-pre-execution",
    policy_version="phase22-v1",
    policy_digest=h("a"),
)
EVALUATOR = TemplarEvaluatorIdentity(
    evaluator_id="templar-pre-execution-model",
    implementation_version="fleet-phase22-test-v1",
    model_provider="test-provider",
    model_name="test-model",
    model_version="test-model-v1",
)


class TemplarClock:
    def __init__(self) -> None:
        self.wall = [1_000, 1_001]
        self.monotonic = [10, 11]

    def wall_ms(self) -> int:
        return self.wall.pop(0)

    def monotonic_ms(self) -> int:
        return self.monotonic.pop(0)


class Backend:
    def __init__(
        self,
        decision: str,
        *,
        reason_codes: tuple[str, ...] = (),
        events: list[str] | None = None,
    ) -> None:
        self.decision = decision
        self.reason_codes = reason_codes
        self.events = events
        self.calls = 0

    def evaluate(
        self,
        request: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        del timeout_ms
        self.calls += 1
        if self.events is not None:
            self.events.append("templar")
        return {
            "schema": TEMPLAR_BACKEND_RESPONSE_SCHEMA,
            "evaluation_id": request["evaluation_id"],
            "request_hash": request["request_hash"],
            "event_hash": request["event_hash"],
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
        }


def templar(
    decision: str = ALLOW,
    *,
    reason_codes: tuple[str, ...] = (),
    events: list[str] | None = None,
) -> tuple[TemplarCore, Backend]:
    backend = Backend(decision, reason_codes=reason_codes, events=events)
    clock = TemplarClock()
    return (
        TemplarCore(
            backend=backend,
            policy=TEMPLAR_POLICY,
            evaluator=EVALUATOR,
            timeout_ms=100,
            verdict_ttl_ms=500,
            wall_clock_ms=clock.wall_ms,
            monotonic_ms=clock.monotonic_ms,
        ),
        backend,
    )


def exact_context(item) -> PreExecutionContext:
    return PreExecutionContext.from_authority(
        item,
        provider="provider-a",
        model="model-a",
    )


def request() -> PreExecutionRequest:
    item = authority()
    return PreExecutionRequest.from_authority(
        item,
        authenticated_principal=PRINCIPAL,
        requested_tools=("fleet-terminal",),
    )


def gate(
    tmp_path: Path,
    *,
    events: list[str],
    templar_core: TemplarCore | None,
    templar_required: bool = True,
    hard_deny: bool = False,
    admission_status: str = ADMITTED,
    admission_reasons: tuple[str, ...] = (),
    final_decision: str = ALLOW,
    final_reasons: tuple[str, ...] = (),
    contexts: list[PreExecutionContext] | None = None,
    review_router=None,
    permit_sealer: PreExecutionPermitSealer = PERMIT_SEALER,
) -> tuple[PreExecutionGate, RunAuthorityStore]:
    store = RunAuthorityStore(tmp_path / "authorities.sqlite", now_ms=lambda: 1_000)
    original_authority_admit = store.admit

    def authority_admit(item):
        events.append("authority")
        return original_authority_admit(item)

    store.admit = authority_admit  # type: ignore[method-assign]

    def policy(bound: PreExecutionRequest) -> DeterministicPolicyDecision:
        events.append("policy")
        denies = ()
        if hard_deny:
            denies = (
                DeterministicHardDeny.from_event(
                    bound.event,
                    code="fleet-hard-deny",
                    subject="request",
                    evidence_hash=h("d"),
                ),
            )
        return DeterministicPolicyDecision.from_request(
            bound,
            templar_required=templar_required,
            hard_denies=denies,
        )

    def admit(bound: PreExecutionRequest) -> DestinationAdmissionDecision:
        events.append("destination")
        return DestinationAdmissionDecision.from_request(
            bound,
            status=admission_status,
            reason_codes=admission_reasons,
        )

    def final(
        bound: PreExecutionRequest,
        admission: DestinationAdmissionDecision,
        verdict,
    ) -> FleetFinalDecision:
        events.append("final")
        return FleetFinalDecision.from_request(
            bound,
            admission,
            decision=final_decision,
            reason_codes=final_reasons,
            verdict=verdict,
        )

    context_values = list(contexts or [])

    def inspect(bound: PreExecutionRequest) -> PreExecutionContext:
        events.append("context")
        if context_values:
            return context_values.pop(0)
        return exact_context(bound.authority)

    def routed(bound, verdict):
        events.append("review")
        if review_router is None:
            return "review-1"
        return review_router(bound, verdict)

    gate_clock = iter(range(1_000, 1_100))
    return (
        PreExecutionGate(
            authority_store=store,
            deterministic_policy=policy,
            destination_admission=admit,
            final_decider=final,
            context_inspector=inspect,
            permit_sealer=permit_sealer,
            templar=templar_core,
            review_router=routed if review_router is not False else None,
            now_ms=lambda: next(gate_clock),
            permit_ttl_ms=500,
        ),
        store,
    )


def test_allow_path_enforces_exact_order_then_activates_authority(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    core, backend = templar(ALLOW, events=events)
    bound = request()
    security_gate, store = gate(tmp_path, events=events, templar_core=core)

    outcome = security_gate.authorize(bound)

    assert outcome.status == READY
    assert outcome.permit is not None
    assert outcome.capsule_spec is not None
    assert outcome.final_decision is not None
    assert outcome.final_decision.decision == ALLOW
    assert outcome.permit.authority == "none"
    PERMIT_SEALER.verify(
        outcome.permit,
        spec=outcome.capsule_spec,
        now_ms=outcome.permit.issued_at_ms,
    )
    assert store.get(bound.authority.content_hash) is not None
    assert backend.calls == 1
    assert events == [
        "context",
        "policy",
        "destination",
        "templar",
        "final",
        "context",
        "authority",
        "context",
    ]


def test_gate_permit_drives_exact_run_capsule_executor_lifecycle(
    tmp_path: Path,
) -> None:
    (
        bundle,
        spec,
        _service,
        fake,
        _workspace,
        runs,
        _capsule_store,
        executor,
        _events,
        _releases,
    ) = capsule_harness(tmp_path)
    existing = executor._authorities.get(spec.run_authority_hash)
    assert existing is not None
    proposed = existing.authority
    authority_store = RunAuthorityStore(
        tmp_path / "phase22-authorities.sqlite",
        now_ms=lambda: 1_000,
    )
    executor._authorities = authority_store
    bound = PreExecutionRequest.from_authority(
        proposed,
        authenticated_principal=spec.principal,
    )

    security_gate = PreExecutionGate(
        authority_store=authority_store,
        deterministic_policy=lambda item: DeterministicPolicyDecision.from_request(
            item,
            templar_required=False,
        ),
        destination_admission=lambda item: DestinationAdmissionDecision.from_request(
            item,
            status=ADMITTED,
        ),
        final_decider=lambda item, admission, verdict: FleetFinalDecision.from_request(
            item,
            admission,
            decision=ALLOW,
            verdict=verdict,
        ),
        context_inspector=lambda item: PreExecutionContext.from_authority(
            item.authority
        ),
        permit_sealer=PERMIT_SEALER,
        now_ms=lambda: 1_000,
    )

    outcome = security_gate.authorize(bound)
    assert outcome.status == READY
    assert outcome.permit is not None
    assert outcome.capsule_spec == spec

    result = executor.execute_initial(
        spec=outcome.capsule_spec,
        permit=outcome.permit,
        agency_bundle=bundle,
        prompt="work",
    )

    assert result.status == "completed"
    assert fake.ensure_calls == 1
    assert runs.start_calls == 1


def test_hard_deny_short_circuits_destination_templar_and_authority(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    core, backend = templar(ALLOW)
    bound = request()
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=core,
        hard_deny=True,
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("fleet-hard-deny",)
    assert events == ["context", "policy"]
    assert backend.calls == 0
    assert store.get(bound.authority.content_hash) is None


def test_destination_deny_stops_before_templar(tmp_path: Path) -> None:
    events: list[str] = []
    core, backend = templar(ALLOW)
    bound = request()
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=core,
        admission_status=DENY,
        admission_reasons=("destination-not-ready",),
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("destination-not-ready",)
    assert events == ["context", "policy", "destination"]
    assert backend.calls == 0
    assert store.get(bound.authority.content_hash) is None


def test_templar_deny_stops_before_fleet_final_decision(tmp_path: Path) -> None:
    events: list[str] = []
    core, backend = templar(DENY, reason_codes=("suspicious-combination",))
    bound = request()
    security_gate, store = gate(tmp_path, events=events, templar_core=core)

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("suspicious-combination",)
    assert "final" not in events
    assert backend.calls == 1
    assert store.get(bound.authority.content_hash) is None


def test_templar_review_routes_operator_flow_without_authority(tmp_path: Path) -> None:
    events: list[str] = []
    core, _backend = templar(REVIEW, reason_codes=("human-review",))
    bound = request()
    security_gate, store = gate(tmp_path, events=events, templar_core=core)

    outcome = security_gate.authorize(bound)

    assert outcome.status == REVIEW
    assert outcome.review_reference == "review-1"
    assert outcome.permit is None
    assert "review" in events
    assert "final" not in events
    assert store.get(bound.authority.content_hash) is None


def test_review_without_operator_router_fails_closed(tmp_path: Path) -> None:
    events: list[str] = []
    core, _backend = templar(REVIEW, reason_codes=("human-review",))
    bound = request()
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=core,
        review_router=False,
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("review-routing-unavailable",)
    assert store.get(bound.authority.content_hash) is None


def test_templar_allow_does_not_override_fleet_final_deny(tmp_path: Path) -> None:
    events: list[str] = []
    core, _backend = templar(ALLOW)
    bound = request()
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=core,
        final_decision=DENY,
        final_reasons=("fleet-final-deny",),
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("fleet-final-deny",)
    assert outcome.verdict is not None and outcome.verdict.decision == ALLOW
    assert store.get(bound.authority.content_hash) is None


def test_policy_can_skip_templar_but_fleet_final_decision_still_runs(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    bound = request()
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=None,
        templar_required=False,
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == READY
    assert outcome.verdict is None
    assert "final" in events
    assert store.get(bound.authority.content_hash) is not None


def test_required_templar_unavailable_fails_closed(tmp_path: Path) -> None:
    events: list[str] = []
    bound = request()
    security_gate, store = gate(tmp_path, events=events, templar_core=None)

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("templar-unavailable",)
    assert store.get(bound.authority.content_hash) is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda context: replace(context, policy_digest=h("e")),
        lambda context: replace(context, target_digest=h("f")),
        lambda context: replace(
            context,
            principal=replace(
                context.principal, generation=context.principal.generation + 1
            ),
        ),
    ],
)
def test_context_mutation_after_templar_invalidates_verdict_before_authority(
    tmp_path: Path,
    mutation,
) -> None:
    events: list[str] = []
    core, _backend = templar(ALLOW)
    bound = request()
    initial = exact_context(bound.authority)
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=core,
        contexts=[initial, mutation(initial)],
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("stale-context",)
    assert outcome.verdict is not None and outcome.verdict.decision == ALLOW
    assert store.get(bound.authority.content_hash) is None


def test_post_admission_context_race_cancels_authority_and_issues_no_permit(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    core, _backend = templar(ALLOW)
    bound = request()
    initial = exact_context(bound.authority)
    changed = replace(initial, policy_digest=h("e"))
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=core,
        contexts=[initial, initial, changed],
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("stale-context",)
    assert outcome.permit is None
    record = store.get(bound.authority.content_hash)
    assert record is not None and record.state == "cancelled"


def test_permit_issuance_failure_cancels_activated_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    core, _backend = templar(ALLOW)
    bound = request()
    sealer = PreExecutionPermitSealer(b"q" * 32)

    def fail_issue(**_kwargs):
        raise RuntimeError("synthetic permit issuance failure")

    monkeypatch.setattr(sealer, "issue", fail_issue)
    security_gate, store = gate(
        tmp_path,
        events=events,
        templar_core=core,
        permit_sealer=sealer,
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("permit-issuance-failed",)
    assert outcome.permit is None
    record = store.get(bound.authority.content_hash)
    assert record is not None and record.state == "cancelled"


def test_stale_destination_admission_binding_fails_closed(tmp_path: Path) -> None:
    core, backend = templar(ALLOW)
    bound = request()
    store = RunAuthorityStore(tmp_path / "authorities.sqlite", now_ms=lambda: 1_000)

    security_gate = PreExecutionGate(
        authority_store=store,
        deterministic_policy=lambda item: DeterministicPolicyDecision.from_request(
            item, templar_required=True
        ),
        destination_admission=lambda item: replace(
            DestinationAdmissionDecision.from_request(item, status=ADMITTED),
            target_digest=h("f"),
        ),
        final_decider=lambda item, admission, verdict: FleetFinalDecision.from_request(
            item, admission, decision=ALLOW, verdict=verdict
        ),
        context_inspector=lambda item: exact_context(item.authority),
        permit_sealer=PERMIT_SEALER,
        templar=core,
        now_ms=lambda: 1_000,
    )

    outcome = security_gate.authorize(bound)

    assert outcome.status == DENY
    assert outcome.reason_codes == ("destination-admission-stale",)
    assert backend.calls == 0
    assert store.get(bound.authority.content_hash) is None


def test_authenticated_principal_mismatch_is_rejected_at_request_boundary() -> None:
    item = authority()
    stale = replace(PRINCIPAL, generation=PRINCIPAL.generation + 1)
    with pytest.raises(Exception, match="authenticated principal"):
        PreExecutionRequest.from_authority(item, authenticated_principal=stale)


def test_permit_is_authority_free_and_rejects_capsule_substitution(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    core, _backend = templar(ALLOW)
    bound = request()
    security_gate, _store = gate(tmp_path, events=events, templar_core=core)
    outcome = security_gate.authorize(bound)
    assert outcome.permit is not None and outcome.capsule_spec is not None

    changed = replace(outcome.capsule_spec, max_iterations=7)
    with pytest.raises(PreExecutionGateStale, match="Run Capsule changed"):
        outcome.permit.validate_for(
            changed,
            now_ms=outcome.permit.issued_at_ms,
        )
    with pytest.raises(PreExecutionGateStale, match="stale"):
        outcome.permit.validate_for(
            outcome.capsule_spec,
            now_ms=outcome.permit.valid_until_ms,
        )


def test_permit_cannot_carry_execution_authority() -> None:
    with pytest.raises(Exception, match="authority:none"):
        PreExecutionPermit(
            gate_request_hash=h("1"),
            security_request_hash=h("2"),
            event_hash=h("3"),
            policy_digest=h("4"),
            run_authority_hash=h("5"),
            capsule_hash=h("6"),
            final_decision_hash=h("7"),
            issued_at_ms=1,
            valid_until_ms=2,
            seal="hmac-sha256:" + "0" * 64,
            authority="allow",
        )
