from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from typing import Any

import pytest

from hermes_fleet.security_event import DeterministicHardDeny
from hermes_fleet.templar import (
    ALLOW,
    DENY,
    ORIGIN_EVALUATOR,
    ORIGIN_FAIL_CLOSED,
    REVIEW,
    TEMPLAR_BACKEND_RESPONSE_SCHEMA,
    TEMPLAR_REQUEST_SCHEMA,
    TEMPLAR_VERDICT_SCHEMA,
    TemplarBindingError,
    TemplarCore,
    TemplarError,
    TemplarEvaluationRequest,
    TemplarEvaluatorIdentity,
    TemplarMalformedResponse,
    TemplarPolicyRef,
    TemplarStaleVerdict,
    TemplarVerdict,
    resolve_templar_disposition,
)
from tests.unit.test_security_event import event


def h(character: str) -> str:
    return "sha256:" + character * 64


POLICY = TemplarPolicyRef(
    policy_id="templar-core",
    policy_version="phase20-v1",
    policy_digest=h("a"),
)
EVALUATOR = TemplarEvaluatorIdentity(
    evaluator_id="templar-model",
    implementation_version="fleet-templar-core-v1",
    model_provider="test-provider",
    model_name="test-model",
    model_version="test-model-v1",
)


class Clock:
    def __init__(
        self,
        *,
        wall: int = 10_000,
        monotonic_values: tuple[int, ...] = (100, 101),
    ) -> None:
        self.wall = wall
        self._monotonic = list(monotonic_values)

    def wall_ms(self) -> int:
        value = self.wall
        self.wall += 1
        return value

    def monotonic_ms(self) -> int:
        if not self._monotonic:
            raise AssertionError("monotonic clock exhausted")
        return self._monotonic.pop(0)


class Backend:
    def __init__(
        self,
        *,
        decision: str = ALLOW,
        reason_codes: tuple[str, ...] = (),
        mutate: Any = None,
        error: BaseException | None = None,
    ) -> None:
        self.decision = decision
        self.reason_codes = reason_codes
        self.mutate = mutate
        self.error = error
        self.calls: list[tuple[dict[str, Any], int]] = []

    def evaluate(
        self,
        request: dict[str, Any],
        *,
        timeout_ms: int,
    ) -> dict[str, Any]:
        self.calls.append((json.loads(json.dumps(request)), timeout_ms))
        if self.error is not None:
            raise self.error
        response: dict[str, Any] = {
            "schema": TEMPLAR_BACKEND_RESPONSE_SCHEMA,
            "evaluation_id": request["evaluation_id"],
            "request_hash": request["request_hash"],
            "event_hash": request["event_hash"],
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
        }
        if self.mutate is not None:
            self.mutate(response)
        return response


def core(
    backend: Backend,
    *,
    clock: Clock | None = None,
    timeout_ms: int = 1_000,
    ttl_ms: int = 5_000,
    policy: TemplarPolicyRef = POLICY,
    evaluator: TemplarEvaluatorIdentity = EVALUATOR,
) -> TemplarCore:
    clock = clock or Clock()
    return TemplarCore(
        backend=backend,
        policy=policy,
        evaluator=evaluator,
        timeout_ms=timeout_ms,
        verdict_ttl_ms=ttl_ms,
        wall_clock_ms=clock.wall_ms,
        monotonic_ms=clock.monotonic_ms,
    )


def test_allow_verdict_is_exact_bound_audited_and_authority_free() -> None:
    security_event = event()
    backend = Backend(decision=ALLOW)
    result = core(backend).evaluate(security_event)

    assert result.decision == ALLOW
    assert result.origin == ORIGIN_EVALUATOR
    assert result.authority == "none"
    assert result.request_hash == security_event.request_hash
    assert result.event_hash == security_event.content_hash
    assert result.fleet_policy_digest == security_event.policy_digest
    assert result.templar_policy == POLICY
    assert result.evaluator == EVALUATOR
    assert result.reason_codes == ()
    assert result.to_dict()["schema"] == TEMPLAR_VERDICT_SCHEMA
    assert result.content_hash.startswith("sha256:")

    assert len(backend.calls) == 1
    request, timeout_ms = backend.calls[0]
    assert timeout_ms == 1_000
    assert request["schema"] == TEMPLAR_REQUEST_SCHEMA
    assert request["request_hash"] == security_event.request_hash
    assert request["event_hash"] == security_event.content_hash
    assert request["event"] == security_event.to_dict()
    assert request["templar_policy"] == POLICY.to_dict()
    assert request["evaluator"] == EVALUATOR.to_dict()
    assert request["evaluation_id"].startswith("sha256:")


def test_deny_and_review_require_bounded_reason_codes() -> None:
    security_event = event()
    denied = core(Backend(decision=DENY, reason_codes=("exfiltration-risk",))).evaluate(
        security_event
    )
    reviewed = core(
        Backend(decision=REVIEW, reason_codes=("human-review-needed",))
    ).evaluate(security_event)
    assert denied.decision == DENY
    assert denied.reason_codes == ("exfiltration-risk",)
    assert reviewed.decision == REVIEW
    assert reviewed.reason_codes == ("human-review-needed",)

    malformed = core(Backend(decision=DENY)).evaluate(security_event)
    assert malformed.decision == DENY
    assert malformed.origin == ORIGIN_FAIL_CLOSED
    assert malformed.reason_codes == ("malformed-response",)


def test_only_allow_deny_review_are_accepted() -> None:
    result = core(Backend(decision="EXECUTE")).evaluate(event())
    assert result.decision == DENY
    assert result.origin == ORIGIN_FAIL_CLOSED
    assert result.reason_codes == ("malformed-response",)


def test_backend_timeout_and_failure_fail_closed_without_error_text() -> None:
    timed_out = core(Backend(error=TimeoutError())).evaluate(event())
    assert timed_out.decision == DENY
    assert timed_out.origin == ORIGIN_FAIL_CLOSED
    assert timed_out.reason_codes == ("evaluator-timeout",)

    failed = core(Backend(error=RuntimeError("sensitive-runtime-detail"))).evaluate(
        event()
    )
    assert failed.decision == DENY
    assert failed.origin == ORIGIN_FAIL_CLOSED
    assert failed.reason_codes == ("evaluator-failure",)
    assert "sensitive-runtime-detail" not in json.dumps(failed.to_dict())


def test_elapsed_timeout_fails_closed_even_if_backend_returns() -> None:
    clock = Clock(monotonic_values=(100, 1_100))
    result = core(Backend(), clock=clock, timeout_ms=1_000).evaluate(event())
    assert result.decision == DENY
    assert result.origin == ORIGIN_FAIL_CLOSED
    assert result.reason_codes == ("evaluator-timeout",)


def test_completion_after_wall_clock_deadline_fails_closed() -> None:
    class DeadlineClock(Clock):
        def wall_ms(self) -> int:
            if self.wall == 10_000:
                self.wall = 11_001
                return 10_000
            return self.wall

    result = core(
        Backend(),
        clock=DeadlineClock(monotonic_values=(100, 101)),
        timeout_ms=1_000,
    ).evaluate(event())
    assert result.decision == DENY
    assert result.reason_codes == ("evaluator-timeout",)


def test_response_binding_mismatch_fails_closed() -> None:
    def mutate(response: dict[str, Any]) -> None:
        response["request_hash"] = h("b")

    result = core(Backend(mutate=mutate)).evaluate(event())
    assert result.decision == DENY
    assert result.origin == ORIGIN_FAIL_CLOSED
    assert result.reason_codes == ("response-binding-mismatch",)


def test_backend_payload_mutation_cannot_change_fleet_request() -> None:
    class MutatingBackend(Backend):
        def evaluate(
            self,
            request: dict[str, Any],
            *,
            timeout_ms: int,
        ) -> dict[str, Any]:
            original_evaluation_id = request["evaluation_id"]
            original_request_hash = request["request_hash"]
            original_event_hash = request["event_hash"]
            request["event"]["request"]["policy_digest"] = h("f")
            return {
                "schema": TEMPLAR_BACKEND_RESPONSE_SCHEMA,
                "evaluation_id": original_evaluation_id,
                "request_hash": original_request_hash,
                "event_hash": original_event_hash,
                "decision": ALLOW,
                "reason_codes": [],
            }

    result = core(MutatingBackend()).evaluate(event())
    assert result.decision == ALLOW
    assert result.origin == ORIGIN_EVALUATOR


def test_unknown_response_fields_are_rejected_not_ignored() -> None:
    def mutate(response: dict[str, Any]) -> None:
        response["rationale"] = "unbounded evaluator prose"

    result = core(Backend(mutate=mutate)).evaluate(event())
    assert result.decision == DENY
    assert result.origin == ORIGIN_FAIL_CLOSED
    assert result.reason_codes == ("malformed-response",)


def test_verdict_round_trip_is_closed_and_content_addressed() -> None:
    verdict = core(Backend()).evaluate(event())
    restored = TemplarVerdict.from_dict(verdict.to_dict())
    assert restored == verdict
    assert restored.content_hash == verdict.content_hash

    extra = verdict.to_dict()
    extra["execution_authorized"] = True
    with pytest.raises(TemplarMalformedResponse, match="closed schema"):
        TemplarVerdict.from_dict(extra)

    broadened = verdict.to_dict()
    broadened["authority"] = "execute"
    with pytest.raises(TemplarError, match="cannot carry authority"):
        TemplarVerdict.from_dict(broadened)

    forged_binding = verdict.to_dict()
    forged_binding["evaluation_id"] = h("f")
    forged_verdict = TemplarVerdict.from_dict(forged_binding)
    with pytest.raises(TemplarStaleVerdict, match="evaluation binding"):
        forged_verdict.validate_for(
            event(),
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            now_ms=forged_verdict.issued_at_ms,
        )


def test_verdict_is_immutable() -> None:
    verdict = core(Backend()).evaluate(event())
    with pytest.raises(FrozenInstanceError):
        verdict.decision = DENY  # type: ignore[misc]


def test_stale_request_event_policy_and_evaluator_are_rejected() -> None:
    security_event = event()
    verdict = core(Backend()).evaluate(security_event)

    verdict.validate_for(
        security_event,
        templar_policy=POLICY,
        evaluator=EVALUATOR,
        now_ms=verdict.issued_at_ms,
    )

    changed_event = replace(
        security_event,
        policy_mismatches=(),
    )
    assert changed_event.request_hash == security_event.request_hash
    assert changed_event.content_hash != security_event.content_hash
    with pytest.raises(TemplarStaleVerdict, match="security event"):
        verdict.validate_for(
            changed_event,
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            now_ms=verdict.issued_at_ms,
        )

    changed_policy = replace(POLICY, policy_digest=h("c"))
    with pytest.raises(TemplarStaleVerdict, match="policy"):
        verdict.validate_for(
            security_event,
            templar_policy=changed_policy,
            evaluator=EVALUATOR,
            now_ms=verdict.issued_at_ms,
        )

    changed_evaluator = replace(EVALUATOR, model_version="test-model-v2")
    with pytest.raises(TemplarStaleVerdict, match="evaluator"):
        verdict.validate_for(
            security_event,
            templar_policy=POLICY,
            evaluator=changed_evaluator,
            now_ms=verdict.issued_at_ms,
        )


def test_expired_and_future_verdicts_are_rejected() -> None:
    verdict = core(Backend()).evaluate(event())
    with pytest.raises(TemplarStaleVerdict, match="stale"):
        verdict.validate_for(
            event(),
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            now_ms=verdict.valid_until_ms,
        )
    with pytest.raises(TemplarStaleVerdict, match="stale"):
        verdict.validate_for(
            event(),
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            now_ms=verdict.issued_at_ms - 1,
        )


def test_deterministic_fleet_hard_deny_always_beats_templar_allow() -> None:
    security_event = event()
    verdict = core(Backend(decision=ALLOW)).evaluate(security_event)
    hard_deny = DeterministicHardDeny.from_event(
        security_event,
        code="fleet-policy-deny",
        subject="run-authority",
        evidence_hash=h("d"),
    )

    assert (
        resolve_templar_disposition(
            security_event,
            verdict,
            hard_denies=(hard_deny,),
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            now_ms=verdict.issued_at_ms,
        )
        == DENY
    )
    assert verdict.decision == ALLOW
    assert verdict.authority == "none"

    assert (
        resolve_templar_disposition(
            security_event,
            verdict,
            hard_denies=(hard_deny,),
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            now_ms=verdict.valid_until_ms + 1,
        )
        == DENY
    )


def test_without_hard_deny_advisory_disposition_matches_valid_verdict() -> None:
    security_event = event()
    verdict = core(
        Backend(decision=REVIEW, reason_codes=("operator-review",))
    ).evaluate(security_event)
    assert (
        resolve_templar_disposition(
            security_event,
            verdict,
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            now_ms=verdict.issued_at_ms,
        )
        == REVIEW
    )


def test_evaluation_request_context_is_bounded_and_exact() -> None:
    security_event = event()
    request = TemplarEvaluationRequest.from_event(
        security_event,
        templar_policy=POLICY,
        evaluator=EVALUATOR,
        issued_at_ms=1_000,
        deadline_ms=2_000,
    )
    payload = request.to_dict()
    assert payload["event"] == security_event.to_dict()
    assert payload["evaluation_id"] == request.evaluation_id
    assert len(json.dumps(payload).encode("utf-8")) < 512 * 1024

    with pytest.raises(TypeError):
        request.event["request_hash"] = h("f")  # type: ignore[index]
    with pytest.raises(TypeError):
        request.event["request"]["policy_digest"] = h("f")  # type: ignore[index]

    forged = dict(payload)
    forged_event = json.loads(json.dumps(forged["event"]))
    forged_event["request_hash"] = h("e")
    with pytest.raises(TemplarBindingError, match="request hash"):
        TemplarEvaluationRequest(
            request_hash=request.request_hash,
            event_hash=request.event_hash,
            fleet_policy_digest=request.fleet_policy_digest,
            templar_policy=POLICY,
            evaluator=EVALUATOR,
            issued_at_ms=1_000,
            deadline_ms=2_000,
            event=forged_event,
        )
