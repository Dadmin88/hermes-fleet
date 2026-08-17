from __future__ import annotations

import threading

import pytest

from hermes_fleet.host_action_broker import (
    DEPLOY_APPROVED_ARTIFACT,
    PUBLISH_APPROVED_BUILD,
    QUERY_APPROVED_HEALTH,
    REPLACE_APPROVED_TREE,
    RESTART_APPROVED_SERVICE,
    HostActionAdapterSpec,
    HostActionAuthorityScope,
    HostActionBroker,
    HostActionBrokerError,
    HostActionGrant,
    HostActionIndeterminateError,
    HostActionRequest,
    canonical_digest,
)

AUTHORITY = "sha256:" + "1" * 64
RECIPE = "sha256:" + "2" * 64
POLICY = "sha256:" + "3" * 64
ARTIFACT = "sha256:" + "4" * 64
PRINCIPAL = "principal-alice"
EXECUTION = "execution-1"
DESTINATION = {
    "source": "nodescale",
    "network_id": "network-test",
    "device_id": "device-test",
    "binding_generation": 3,
}
TARGET_DIGEST = canonical_digest(DESTINATION)


class Clock:
    def __init__(self, value: int = 1_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def deploy_parameters() -> dict[str, object]:
    return {
        "artifact_id": "artifact-1",
        "artifact_digest": ARTIFACT,
        "release_id": "release-1",
    }


def grant_for(
    parameters: dict[str, object],
    *,
    verb: str = DEPLOY_APPROVED_ARTIFACT,
    target: str = "app-release",
    max_calls: int = 4,
    rate_limit_per_minute: int = 4,
) -> HostActionGrant:
    return HostActionGrant(
        verb=verb,
        target=target,
        parameters_digest=canonical_digest(parameters),
        max_calls=max_calls,
        rate_limit_per_minute=rate_limit_per_minute,
    )


def authority(
    *grants: HostActionGrant,
    deadline_ms: int = 10_000,
    principal_id: str = PRINCIPAL,
    execution_id: str = EXECUTION,
    run_authority_hash: str = AUTHORITY,
    resolved_recipe_hash: str = RECIPE,
    policy_digest: str = POLICY,
    target_digest: str = TARGET_DIGEST,
) -> HostActionAuthorityScope:
    return HostActionAuthorityScope(
        principal_id=principal_id,
        execution_id=execution_id,
        run_authority_hash=run_authority_hash,
        resolved_recipe_hash=resolved_recipe_hash,
        policy_digest=policy_digest,
        target_digest=target_digest,
        deadline_ms=deadline_ms,
        grants=tuple(grants),
    )


def request(
    parameters: dict[str, object] | None = None,
    *,
    verb: str = DEPLOY_APPROVED_ARTIFACT,
    target: str = "app-release",
    idempotency_key: str = "idempotency-1",
    deadline_ms: int = 9_000,
    principal_id: str = PRINCIPAL,
    execution_id: str = EXECUTION,
    run_authority_hash: str = AUTHORITY,
    resolved_recipe_hash: str = RECIPE,
) -> HostActionRequest:
    return HostActionRequest(
        principal_id=principal_id,
        execution_id=execution_id,
        run_authority_hash=run_authority_hash,
        resolved_recipe_hash=resolved_recipe_hash,
        verb=verb,
        target=target,
        parameters=parameters or deploy_parameters(),
        idempotency_key=idempotency_key,
        deadline_ms=deadline_ms,
    )


def adapter(
    handler,
    *,
    verb: str = DEPLOY_APPROVED_ARTIFACT,
    target: str = "app-release",
    required: tuple[str, ...] = ("artifact_id", "artifact_digest"),
    optional: tuple[str, ...] = ("release_id",),
) -> HostActionAdapterSpec:
    return HostActionAdapterSpec(
        verb=verb,
        target=target,
        handler=handler,
        required_parameters=required,
        optional_parameters=optional,
    )


def broker(
    adapters: tuple[HostActionAdapterSpec, ...],
    *,
    clock: Clock | None = None,
    node_policy=lambda _authority, _request: True,
    audit_sink=None,
) -> HostActionBroker:
    return HostActionBroker(
        adapters=adapters,
        node_policy=node_policy,
        now_ms=clock or Clock(),
        audit_sink=audit_sink,
    )


def invoke(
    value: HostActionBroker,
    scope: HostActionAuthorityScope,
    action: HostActionRequest,
    *,
    policy: str = POLICY,
    recipe: str = RECIPE,
    target: dict[str, object] | None = None,
    advisory: str | None = None,
):
    return value.invoke(
        authority=scope,
        request=action,
        current_policy_digest=policy,
        current_resolved_recipe_hash=recipe,
        current_target=target or DESTINATION,
        advisory=advisory,
    )


def test_all_structured_verbs_execute_only_registered_logical_adapters() -> None:
    cases = (
        (
            DEPLOY_APPROVED_ARTIFACT,
            "app-release",
            {"artifact_id": "artifact-1", "artifact_digest": ARTIFACT},
            ("artifact_id", "artifact_digest"),
        ),
        (
            RESTART_APPROVED_SERVICE,
            "api-service",
            {"reason": "approved-release", "expected_generation": 7},
            ("reason", "expected_generation"),
        ),
        (
            PUBLISH_APPROVED_BUILD,
            "release-channel",
            {
                "artifact_id": "artifact-1",
                "artifact_digest": ARTIFACT,
                "channel": "stable",
            },
            ("artifact_id", "artifact_digest", "channel"),
        ),
        (
            REPLACE_APPROVED_TREE,
            "web-tree",
            {
                "artifact_id": "artifact-1",
                "artifact_digest": ARTIFACT,
                "generation": 8,
            },
            ("artifact_id", "artifact_digest", "generation"),
        ),
        (
            QUERY_APPROVED_HEALTH,
            "api-health",
            {"probe_id": "ready"},
            ("probe_id",),
        ),
    )
    calls: list[tuple[str, dict[str, object]]] = []
    adapters: list[HostActionAdapterSpec] = []
    grants: list[HostActionGrant] = []
    for verb, target, parameters, required in cases:

        def handler(values, *, _verb=verb):
            calls.append((_verb, dict(values)))
            return {"accepted": True, "effect_id": f"effect-{_verb}"}

        adapters.append(
            adapter(
                handler,
                verb=verb,
                target=target,
                required=required,
                optional=(),
            )
        )
        grants.append(grant_for(parameters, verb=verb, target=target, max_calls=1))

    service = broker(tuple(adapters))
    scope = authority(*grants)
    for index, (verb, target, parameters, _required) in enumerate(cases):
        evidence = invoke(
            service,
            scope,
            request(
                parameters,
                verb=verb,
                target=target,
                idempotency_key=f"idem-{index}",
            ),
        )
        assert evidence.status == "succeeded"
        assert evidence.verb == verb
        assert evidence.target == target
        assert evidence.result["accepted"] is True
        assert evidence.result_hash == canonical_digest(dict(evidence.result))
    assert len(calls) == len(cases)


def test_request_surface_rejects_generic_shell_paths_transports_and_secrets() -> None:
    with pytest.raises(HostActionBrokerError, match="generic host power"):
        adapter(lambda _values: {}, required=("command",), optional=())

    for parameters in (
        {"artifact_id": "artifact-1", "path": "logical"},
        {"artifact_id": "artifact-1", "filepath": "logical"},
        {"artifact_id": "artifact-1", "host": "node-1"},
        {"artifact_id": "artifact-1", "endpoint": "node-1:22"},
        {"artifact_id": "artifact-1", "port": 22},
        {"artifact_id": "artifact-1", "url": "logical-endpoint"},
        {"artifact_id": "artifact-1", "destination": "/" + "etc/service"},
        {"artifact_id": "artifact-1", "destination": "../../etc/service"},
        {"artifact_id": "artifact-1", "destination": r"\\server\share"},
        {"artifact_id": "artifact-1", "destination": "user@host:/srv/release"},
        {"artifact_id": "artifact-1", "endpoint_id": "ssh" + "://host"},
        {"artifact_id": "artifact-1", "api_key": "redacted"},
        {"artifact_id": "artifact-1", "aws_access_key_id": "redacted"},
        {"artifact_id": "artifact-1", "cookie": "redacted"},
        {"artifact_id": "artifact-1", "authorization": "redacted"},
        {"artifact_id": "artifact-1", "nested": {"argv": ["anything"]}},
    ):
        with pytest.raises(HostActionBrokerError):
            request(parameters)


def test_exact_authority_recipe_policy_target_principal_and_deadline_are_required() -> (
    None
):
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters))
    service = broker((adapter(lambda _values: {"ok": True}),))

    with pytest.raises(HostActionBrokerError, match="principal changed"):
        invoke(service, scope, request(principal_id="principal-bob"))
    with pytest.raises(HostActionBrokerError, match="execution identity changed"):
        invoke(service, scope, request(execution_id="execution-2"))
    with pytest.raises(HostActionBrokerError, match="authority binding changed"):
        invoke(
            service,
            scope,
            request(run_authority_hash="sha256:" + "9" * 64),
        )
    with pytest.raises(HostActionBrokerError, match="Recipe binding changed"):
        invoke(
            service,
            scope,
            request(resolved_recipe_hash="sha256:" + "8" * 64),
        )
    with pytest.raises(HostActionBrokerError, match="policy authorization is stale"):
        invoke(service, scope, request(), policy="sha256:" + "7" * 64)
    with pytest.raises(HostActionBrokerError, match="Recipe authorization is stale"):
        invoke(service, scope, request(), recipe="sha256:" + "6" * 64)
    changed_target = {**DESTINATION, "binding_generation": 4}
    with pytest.raises(
        HostActionBrokerError,
        match="destination authorization is stale",
    ):
        invoke(service, scope, request(), target=changed_target)
    with pytest.raises(HostActionBrokerError, match="widens RunAuthority"):
        invoke(service, scope, request(deadline_ms=10_001))


def test_node_policy_fails_closed_before_adapter_execution() -> None:
    calls: list[str] = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters))
    service = broker(
        (adapter(lambda _values: calls.append("effect") or {"ok": True}),),
        node_policy=lambda _scope, _request: False,
    )
    with pytest.raises(HostActionBrokerError, match="denied by node policy"):
        invoke(service, scope, request())
    assert calls == []

    unavailable = broker(
        (adapter(lambda _values: {"ok": True}),),
        node_policy=lambda _scope, _request: (_ for _ in ()).throw(RuntimeError()),
    )
    with pytest.raises(HostActionBrokerError, match="policy is unavailable"):
        invoke(unavailable, scope, request())


def test_parameter_digest_and_registered_target_must_match_exact_grant() -> None:
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters))
    service = broker((adapter(lambda _values: {"ok": True}),))

    changed = {**parameters, "release_id": "release-2"}
    with pytest.raises(HostActionBrokerError, match="not granted"):
        invoke(service, scope, request(changed))

    wrong_target = request(target="other-target")
    with pytest.raises(HostActionBrokerError, match="no registered adapter"):
        invoke(service, scope, wrong_target)


def test_idempotency_returns_same_evidence_and_changed_request_is_rejected() -> None:
    calls: list[int] = []
    clock = Clock()
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters, max_calls=2))
    service = broker(
        (
            adapter(
                lambda _values: calls.append(1) or {"effect_generation": len(calls)}
            ),
        ),
        clock=clock,
    )
    action = request()
    first = invoke(service, scope, action)

    # Replaying stored evidence is not a new host effect. It remains available
    # even if live admission state changes after the original effect.
    clock.value = 0
    second = invoke(
        service,
        scope,
        action,
        policy="sha256:" + "8" * 64,
        recipe="sha256:" + "7" * 64,
        target={**DESTINATION, "binding_generation": 999},
    )
    assert second == first
    assert calls == [1]

    changed = request(deadline_ms=8_500)
    with pytest.raises(HostActionBrokerError, match="reused for a changed request"):
        invoke(service, scope, changed)
    assert calls == [1]


def test_call_budget_is_reserved_before_effect_so_concurrent_calls_cannot_race() -> (
    None
):
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters, max_calls=1, rate_limit_per_minute=5))

    def handler(_values):
        calls.append("effect")
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    service = broker((adapter(handler),))
    first_error: list[BaseException] = []

    def worker() -> None:
        try:
            invoke(service, scope, request(idempotency_key="idem-first"))
        except BaseException as error:  # pragma: no cover - surfaced below
            first_error.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(5)
    with pytest.raises(HostActionBrokerError, match="call budget"):
        invoke(service, scope, request(idempotency_key="idem-second"))
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert first_error == []
    assert calls == ["effect"]


def test_rate_limit_is_attempt_based_and_window_expires() -> None:
    clock = Clock()
    parameters = deploy_parameters()
    scope = authority(
        grant_for(parameters, max_calls=3, rate_limit_per_minute=1),
        deadline_ms=100_000,
    )
    service = broker((adapter(lambda _values: {"ok": True}),), clock=clock)
    invoke(service, scope, request(idempotency_key="idem-one", deadline_ms=90_000))
    with pytest.raises(HostActionBrokerError, match="rate limit"):
        invoke(service, scope, request(idempotency_key="idem-two", deadline_ms=90_000))
    clock.value += 60_001
    evidence = invoke(
        service,
        scope,
        request(idempotency_key="idem-two", deadline_ms=90_000),
    )
    assert evidence.status == "succeeded"


def test_adapter_failure_becomes_sticky_indeterminate_and_never_reexecutes() -> None:
    calls: list[str] = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters, max_calls=3))

    def handler(_values):
        calls.append("attempt")
        raise RuntimeError("unknown partial host effect")

    service = broker((adapter(handler),))
    action = request()
    with pytest.raises(HostActionIndeterminateError) as raised:
        invoke(service, scope, action)
    assert raised.value.evidence.status == "indeterminate"
    assert raised.value.evidence.result == {"reason": "adapter_failed"}
    with pytest.raises(HostActionIndeterminateError) as repeated:
        invoke(service, scope, action)
    assert repeated.value.evidence == raised.value.evidence
    assert calls == ["attempt"]


def test_invalid_adapter_evidence_becomes_indeterminate_without_secret_leak() -> None:
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters))
    service = broker(
        (adapter(lambda _values: {"api_key": "would-have-been-sensitive"}),)
    )
    with pytest.raises(HostActionIndeterminateError) as raised:
        invoke(service, scope, request())
    assert raised.value.evidence.result == {"reason": "invalid_adapter_evidence"}
    assert "would-have-been-sensitive" not in str(raised.value.evidence.to_dict())


def test_completion_after_deadline_is_sticky_indeterminate() -> None:
    clock = Clock(1_000)
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters), deadline_ms=1_500)

    def handler(_values):
        clock.value = 1_600
        return {"ok": True}

    service = broker((adapter(handler),), clock=clock)
    action = request(deadline_ms=1_500)
    with pytest.raises(HostActionIndeterminateError) as raised:
        invoke(service, scope, action)
    assert raised.value.evidence.result == {"reason": "completed_after_deadline"}
    with pytest.raises(HostActionIndeterminateError):
        invoke(service, scope, action)


def test_security_advisory_can_only_narrow_and_allow_grants_nothing() -> None:
    calls: list[str] = []
    parameters = deploy_parameters()
    valid_scope = authority(grant_for(parameters))
    service = broker((adapter(lambda _values: calls.append("effect") or {"ok": True}),))
    for advisory, text in (("deny", "denied"), ("review", "review")):
        with pytest.raises(HostActionBrokerError, match=text):
            invoke(service, valid_scope, request(), advisory=advisory)
    assert calls == []

    no_grant = authority()
    with pytest.raises(HostActionBrokerError, match="not granted"):
        invoke(service, no_grant, request(), advisory="allow")
    assert calls == []


def test_audit_receives_structured_nonsecret_evidence_after_effect() -> None:
    audits = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters))
    service = broker(
        (adapter(lambda _values: {"generation": 11, "changed": True}),),
        audit_sink=audits.append,
    )
    evidence = invoke(service, scope, request())
    assert audits == [evidence]
    assert evidence.to_dict()["result"] == {"generation": 11, "changed": True}


def test_invalid_clock_and_expired_authority_fail_before_host_effect() -> None:
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters), deadline_ms=1_500)
    invalid = broker((adapter(lambda _values: {"ok": True}),), clock=Clock(0))
    with pytest.raises(HostActionBrokerError, match="clock"):
        invoke(invalid, scope, request(deadline_ms=1_500))

    expired = broker((adapter(lambda _values: {"ok": True}),), clock=Clock(1_501))
    with pytest.raises(HostActionBrokerError, match="authority has expired"):
        invoke(expired, scope, request(deadline_ms=1_500))


def test_nested_request_parameters_are_detached_and_deeply_immutable() -> None:
    parameters = {
        **deploy_parameters(),
        "metadata": {
            "release": "one",
            "labels": ["stable"],
        },
    }
    action = request(parameters)

    parameters["metadata"]["release"] = "two"  # type: ignore[index]
    parameters["metadata"]["labels"].append("mutated")  # type: ignore[index,union-attr]

    assert action.parameters["metadata"]["release"] == "one"
    assert action.parameters["metadata"]["labels"] == ["stable"]
    with pytest.raises(TypeError, match="immutable"):
        action.parameters["metadata"]["release"] = "changed"
    with pytest.raises(TypeError, match="immutable"):
        action.parameters["metadata"]["labels"].append("changed")


def test_adapter_evidence_is_detached_and_deeply_immutable() -> None:
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters))
    raw_result = {
        "details": {
            "generation": 1,
            "labels": ["stable"],
        }
    }
    service = broker((adapter(lambda _values: raw_result),))

    evidence = invoke(service, scope, request())
    raw_result["details"]["generation"] = 999
    raw_result["details"]["authorization"] = "would-have-leaked"
    raw_result["details"]["labels"].append("mutated")

    assert evidence.result["details"]["generation"] == 1
    assert evidence.result["details"]["labels"] == ["stable"]
    assert "authorization" not in evidence.result["details"]
    with pytest.raises(TypeError, match="immutable"):
        evidence.result["details"]["generation"] = 2
    with pytest.raises(TypeError, match="immutable"):
        evidence.result["details"]["labels"].append("changed")


def test_same_idempotency_key_cannot_race_through_final_reservation() -> None:
    barrier = threading.Barrier(2)
    entered = threading.Event()
    release = threading.Event()
    loser_done = threading.Event()
    calls: list[str] = []
    outcomes: list[object] = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters, max_calls=2, rate_limit_per_minute=2))

    def node_policy(_scope, _request):
        barrier.wait(5)
        return True

    def handler(_values):
        calls.append("effect")
        entered.set()
        assert release.wait(5)
        return {"ok": True}

    service = broker((adapter(handler),), node_policy=node_policy)
    action = request()

    def worker() -> None:
        try:
            outcomes.append(invoke(service, scope, action))
        except BaseException as error:  # pragma: no cover - asserted below
            outcomes.append(error)
        finally:
            loser_done.set()

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    assert entered.wait(5)
    assert loser_done.wait(5)
    assert calls == ["effect"]
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == ["effect"]
    assert len(outcomes) == 2
    errors = [item for item in outcomes if isinstance(item, HostActionBrokerError)]
    successes = [item for item in outcomes if not isinstance(item, BaseException)]
    assert len(errors) == 1
    assert "already in flight" in str(errors[0])
    assert len(successes) == 1


def test_deadline_is_rechecked_immediately_before_host_effect() -> None:
    clock = Clock(1_000)
    calls: list[str] = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters), deadline_ms=1_500)

    def node_policy(_scope, _request):
        clock.value = 1_600
        return True

    service = broker(
        (adapter(lambda _values: calls.append("effect") or {"ok": True}),),
        clock=clock,
        node_policy=node_policy,
    )
    with pytest.raises(HostActionBrokerError, match="expired before effect"):
        invoke(service, scope, request(deadline_ms=1_500))
    assert calls == []


def test_post_effect_clock_failure_becomes_sticky_indeterminate() -> None:
    values: list[object] = [1_000, 1_000, RuntimeError("clock failed"), 1_001]
    calls: list[str] = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters, max_calls=2))

    def clock() -> int:
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        assert isinstance(value, int)
        return value

    service = HostActionBroker(
        adapters=(adapter(lambda _values: calls.append("effect") or {"ok": True}),),
        node_policy=lambda _scope, _request: True,
        now_ms=clock,
    )
    action = request()
    with pytest.raises(HostActionIndeterminateError) as raised:
        invoke(service, scope, action)
    assert raised.value.evidence.result == {"reason": "completion_time_unavailable"}
    with pytest.raises(HostActionIndeterminateError) as repeated:
        invoke(service, scope, action)
    assert repeated.value.evidence == raised.value.evidence
    assert calls == ["effect"]


def test_audit_failure_is_sticky_indeterminate_and_never_reexecutes() -> None:
    calls: list[str] = []
    audits: list[str] = []
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters, max_calls=2))

    def audit_sink(_evidence) -> None:
        audits.append("attempt")
        raise RuntimeError("audit store unavailable")

    service = broker(
        (adapter(lambda _values: calls.append("effect") or {"ok": True}),),
        audit_sink=audit_sink,
    )
    action = request()
    with pytest.raises(HostActionIndeterminateError) as raised:
        invoke(service, scope, action)
    assert raised.value.evidence.result == {"reason": "audit_persistence_failed"}
    with pytest.raises(HostActionIndeterminateError) as repeated:
        invoke(service, scope, action)
    assert repeated.value.evidence == raised.value.evidence
    assert calls == ["effect"]
    assert audits == ["attempt"]


def test_nested_secret_bearing_adapter_evidence_is_indeterminate() -> None:
    parameters = deploy_parameters()
    scope = authority(grant_for(parameters))
    service = broker(
        (
            adapter(
                lambda _values: {
                    "details": {"authorization": "sensitive"},
                }
            ),
        )
    )
    with pytest.raises(HostActionIndeterminateError) as raised:
        invoke(service, scope, request())
    assert raised.value.evidence.result == {"reason": "invalid_adapter_evidence"}
    assert "sensitive" not in str(raised.value.evidence.to_dict())
