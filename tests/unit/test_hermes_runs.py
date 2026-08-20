from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


class _RunsAPI:
    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self.statuses = list(statuses)
        self.requests: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.post_delay_seconds = 0.0
        self.post_status = 202
        self.run_fleet_runtime = True
        self.run_fleet_memory_scope = True
        self.fleet_scoped_memory_write = True
        self.run_fleet_context_firewall = True
        self.run_sensitive_interception = True
        self.run_fleet_vault_scope = True
        self.run_fleet_skill_learning = True
        self.run_fleet_skill_quarantine = True
        self.run_fleet_skill_verification = True
        self.fleet_learning_promotion = True
        self.stop_delay_seconds = 0.0
        self.stop_response_sent = False

    @contextmanager
    def serve(self) -> Iterator[str]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length else b""
                body = json.loads(raw_body) if raw_body else None
                api.requests.append(
                    ("POST", self.path, self.headers.get("Authorization", ""), body)
                )
                route = self.path
                if route.startswith("/p/"):
                    route = "/" + route.split("/", 3)[3]
                if route == "/v1/fleet/memory":
                    if not api.fleet_scoped_memory_write:
                        self._json(404, {"error": {"message": "unsupported"}})
                        return
                    self._json(
                        200,
                        {
                            "object": "hermes.api_server.fleet_memory_write",
                            "result": {"success": True, "message": "Memory updated."},
                        },
                    )
                    return
                if route == "/v1/fleet/promotions/prepare":
                    if not api.fleet_learning_promotion:
                        self._json(404, {"error": {"message": "unsupported"}})
                        return
                    assert type(body) is dict
                    source_hash = body.get("source_content_hash")
                    if body.get("subject_kind") == "memory":
                        prepared = {
                            "subject_kind": "memory",
                            "subject_key": "memory:" + str(source_hash),
                            "source_content_hash": source_hash,
                            "approved_content_hash": source_hash,
                            "sanitized": False,
                            "verification_digest": None,
                            "authority": "none",
                        }
                    else:
                        candidate_id = body.get("candidate_id")
                        prepared = {
                            "subject_kind": "skill",
                            "subject_key": candidate_id,
                            "source_content_hash": "sha256:" + "8" * 64,
                            "approved_content_hash": "sha256:" + "9" * 64,
                            "sanitized": True,
                            "verification_digest": "sha256:" + "a" * 64,
                            "authority": "none",
                        }
                    self._json(
                        200,
                        {
                            "object": "hermes.api_server.fleet_promotion_prepare",
                            "prepared": prepared,
                        },
                    )
                    return
                if route == "/v1/fleet/promotions/commit":
                    assert type(body) is dict
                    assert type(body.get("authorization")) is dict
                    authorization = body["authorization"]
                    self._json(
                        200,
                        {
                            "object": "hermes.api_server.fleet_promotion_commit",
                            "result": {
                                "promotion_id": authorization["promotion_id"],
                                "subject_kind": authorization["subject_kind"],
                                "subject_key": authorization["subject_key"],
                                "target_scope": authorization["target_scope"],
                                "approved_content_hash": authorization[
                                    "approved_content_hash"
                                ],
                                "previous_promotion_id": authorization.get(
                                    "expected_current_promotion_id"
                                ),
                                "current_promotion_id": authorization["promotion_id"],
                                "operation": "promote",
                                "idempotent": False,
                                "authority": "none",
                            },
                        },
                    )
                    return
                if route == "/v1/fleet/promotions/rollback":
                    assert type(body) is dict
                    assert type(body.get("authorization")) is dict
                    authorization = body["authorization"]
                    self._json(
                        200,
                        {
                            "object": "hermes.api_server.fleet_promotion_rollback",
                            "result": {
                                "promotion_id": authorization["promotion_id"],
                                "subject_kind": authorization["subject_kind"],
                                "subject_key": authorization["subject_key"],
                                "target_scope": authorization["target_scope"],
                                "approved_content_hash": authorization[
                                    "approved_content_hash"
                                ],
                                "previous_promotion_id": authorization[
                                    "expected_current_promotion_id"
                                ],
                                "current_promotion_id": authorization["promotion_id"],
                                "operation": "rollback",
                                "idempotent": False,
                                "authority": "none",
                            },
                        },
                    )
                    return
                if route == "/v1/fleet/promotions/history":
                    assert type(body) is dict
                    self._json(
                        200,
                        {
                            "object": "hermes.api_server.fleet_promotion_history",
                            "result": {
                                "current_promotion_id": None,
                                "history": [],
                                "records": [],
                                "authority": "none",
                            },
                        },
                    )
                    return
                if route == "/v1/runs":
                    if api.post_delay_seconds:
                        time.sleep(api.post_delay_seconds)
                    self._json(
                        api.post_status,
                        (
                            {"run_id": "run-test", "status": "started"}
                            if api.post_status == 202
                            else {"error": {"message": "rejected"}}
                        ),
                    )
                    return
                if route == "/v1/runs/run-test/approval":
                    self._json(
                        200,
                        {
                            "object": "hermes.run.approval_response",
                            "run_id": "run-test",
                            "choice": "once",
                            "resolved": 1,
                        },
                    )
                    return
                if route == "/v1/runs/run-test/stop":
                    if api.stop_delay_seconds:
                        time.sleep(api.stop_delay_seconds)
                    self._json(200, {"run_id": "run-test", "status": "stopping"})
                    api.stop_response_sent = True
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_GET(self) -> None:
                api.requests.append(
                    ("GET", self.path, self.headers.get("Authorization", ""), None)
                )
                route = self.path
                if route.startswith("/p/"):
                    route = "/" + route.split("/", 3)[3]
                if route == "/health":
                    self._json(200, {"status": "ok"})
                    return
                if route == "/v1/capabilities":
                    self._json(
                        200,
                        {
                            "object": "hermes.api_server.capabilities",
                            "features": {
                                "run_submission": True,
                                "run_status": True,
                                "run_stop": True,
                                "run_finalize": True,
                                "run_fleet_runtime": api.run_fleet_runtime,
                                "run_fleet_memory_scope": api.run_fleet_memory_scope,
                                "fleet_scoped_memory_write": (
                                    api.fleet_scoped_memory_write
                                ),
                                "run_fleet_context_firewall": (
                                    api.run_fleet_context_firewall
                                ),
                                "run_sensitive_interception": (
                                    api.run_sensitive_interception
                                ),
                                "run_fleet_vault_scope": api.run_fleet_vault_scope,
                                "run_fleet_skill_learning": (
                                    api.run_fleet_skill_learning
                                ),
                                "run_fleet_skill_quarantine": (
                                    api.run_fleet_skill_quarantine
                                ),
                                "run_fleet_skill_verification": (
                                    api.run_fleet_skill_verification
                                ),
                                "fleet_learning_promotion": (
                                    api.fleet_learning_promotion
                                ),
                                "run_approval_budget": True,
                                "run_tool_evidence": True,
                                "run_command_evidence": True,
                            },
                        },
                    )
                    return
                if route == "/v1/runs/missing-run":
                    self._json(404, {"error": {"message": "not found"}})
                    return
                status = (
                    api.statuses.pop(0) if len(api.statuses) > 1 else api.statuses[0]
                )
                self._json(
                    200, {"object": "hermes.run", "run_id": "run-test", **status}
                )

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _json(self, status: int, document: dict[str, Any]) -> None:
                payload = json.dumps(document).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            yield f"http://{host}:{port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def _memory_binding():
    from hermes_fleet.hermes_runs import HermesFleetMemoryBinding, HermesMemoryScopeRef

    principal_id = "sha256:" + "1" * 64
    private = HermesMemoryScopeRef("principal", principal_id)
    return HermesFleetMemoryBinding(
        principal_id=principal_id,
        principal_kind="owner",
        principal_generation=1,
        principal_binding_hash="sha256:" + "2" * 64,
        agent_instance_id="sha256:" + "3" * 64,
        source_run="execution-1",
        read_scopes=(
            private,
            HermesMemoryScopeRef("project", "project-a"),
            HermesMemoryScopeRef("agent_instance", "sha256:" + "3" * 64),
        ),
        write_scope=private,
    )


def test_hermes_runs_client_returns_authenticated_terminal_text() -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    api = _RunsAPI([{"status": "queued"}, {"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        result = HermesRunsClient(
            endpoint=endpoint,
            api_key="secret-token-for-test",
            poll_interval_seconds=0.001,
        ).run(prompt="Inspect the repo.", timeout_seconds=1)

    assert result.run_id == "run-test"
    assert result.text == "done"
    assert api.requests[0] == (
        "POST",
        "/v1/runs",
        "Bearer secret-token-for-test",
        {"input": "Inspect the repo."},
    )
    assert all(request[2] == "Bearer secret-token-for-test" for request in api.requests)


def test_hermes_runs_client_scopes_requests_to_validated_profile_prefix() -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        client = HermesRunsClient(
            endpoint=endpoint,
            api_key="secret-token-for-test",
            profile="fleet-exec-abc123",
            poll_interval_seconds=0.001,
        )
        client.run(prompt="Inspect the exact package.", timeout_seconds=1)

    assert [path for _method, path, _auth, _body in api.requests] == [
        "/p/fleet-exec-abc123/v1/runs",
        "/p/fleet-exec-abc123/v1/runs/run-test",
    ]


def test_hermes_runs_client_inspects_completed_result_without_mutation() -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    api = _RunsAPI([{"status": "completed", "output": "FX8_OK"}])
    with api.serve() as endpoint:
        result = HermesRunsClient(
            endpoint=endpoint,
            api_key="secret-token-for-test",
        ).inspect("run-test")

    assert result.status == "completed"
    assert result.text == "FX8_OK"
    assert [request[0] for request in api.requests] == ["GET"]


@pytest.mark.parametrize("profile", ["../default", "bad/name", " bad", ""])
def test_hermes_runs_client_rejects_unsafe_profile(profile: str) -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    with pytest.raises(ValueError, match="profile"):
        HermesRunsClient(
            endpoint="http://127.0.0.1:8642",
            api_key="secret-token-for-test",
            profile=profile,
        )


def test_hermes_runs_client_reports_public_capabilities_without_run() -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    with api.serve() as endpoint:
        health = HermesRunsClient(
            endpoint=endpoint,
            api_key="secret-token-for-test",
        ).health()

    assert health == {
        "api": "healthy",
        "run_submission": True,
        "run_status": True,
        "run_stop": True,
        "run_finalize": True,
        "run_fleet_runtime": True,
        "run_fleet_memory_scope": True,
        "fleet_scoped_memory_write": True,
        "run_fleet_context_firewall": True,
        "run_sensitive_interception": True,
        "run_fleet_vault_scope": True,
        "run_fleet_skill_learning": True,
        "run_fleet_skill_quarantine": True,
        "run_fleet_skill_verification": True,
        "fleet_learning_promotion": True,
        "run_approval_budget": True,
        "run_tool_evidence": True,
        "run_command_evidence": True,
    }
    assert [request[1] for request in api.requests] == ["/health", "/v1/capabilities"]


def test_fleet_runtime_requires_capability_and_exact_payload() -> None:
    from hermes_fleet.hermes_runs import (
        HermesFleetRuntimeBinding,
        HermesRunError,
        HermesRunsClient,
    )

    runtime = HermesFleetRuntimeBinding(
        container_id="a" * 64,
        plan_fingerprint="sha256:" + "b" * 64,
        image="debian@sha256:" + "c" * 64,
        max_iterations=8,
    )
    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_fleet_runtime = False
    with api.serve() as endpoint:
        client = HermesRunsClient(
            endpoint=endpoint,
            api_key="secret-token-for-test",
        )
        with pytest.raises(HermesRunError, match="run_fleet_runtime"):
            client.start(prompt="blocked", fleet_runtime=runtime)
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        run_id = HermesRunsClient(
            endpoint=endpoint,
            api_key="secret-token-for-test",
        ).start(prompt="allowed", fleet_runtime=runtime)
    assert run_id == "run-test"
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
        ("POST", "/v1/runs"),
    ]
    assert api.requests[-1][3] == {
        "input": "allowed",
        "fleet_runtime": {
            "version": "fleet-run-v1",
            "container_id": "a" * 64,
            "plan_fingerprint": "sha256:" + "b" * 64,
            "image": "debian@sha256:" + "c" * 64,
            "toolsets": ["fleet-terminal"],
            "max_iterations": 8,
        },
    }


def test_fleet_memory_requires_runtime_capability_and_exact_payload() -> None:
    from hermes_fleet.hermes_runs import (
        HermesFleetRuntimeBinding,
        HermesRunError,
        HermesRunsClient,
    )

    runtime = HermesFleetRuntimeBinding(
        container_id="a" * 64,
        plan_fingerprint="sha256:" + "b" * 64,
        image="debian@sha256:" + "c" * 64,
        max_iterations=8,
    )
    memory = _memory_binding()

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(ValueError, match="requires a Fleet runtime"):
            client.start(prompt="blocked", fleet_memory=memory)
    assert api.requests == []

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_fleet_memory_scope = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(HermesRunError, match="run_fleet_memory_scope"):
            client.start(prompt="blocked", fleet_runtime=runtime, fleet_memory=memory)
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        run_id = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]").start(
            prompt="allowed",
            fleet_runtime=runtime,
            fleet_memory=memory,
        )
    assert run_id == "run-test"
    assert api.requests[-1][3] == {
        "input": "allowed",
        "fleet_runtime": runtime.to_request(),
        "fleet_memory": memory.to_request(),
    }


def test_fleet_runtime_material_requires_capability_and_exact_payload() -> None:
    from hermes_fleet.hermes_runs import (
        HermesFleetContextBinding,
        HermesFleetRuntimeBinding,
        HermesFleetVaultBinding,
        HermesRunError,
        HermesRunsClient,
        HermesRuntimeMaterialHandle,
    )

    runtime = HermesFleetRuntimeBinding(
        container_id="a" * 64,
        plan_fingerprint="sha256:" + "b" * 64,
        image="debian@sha256:" + "c" * 64,
        max_iterations=8,
    )
    memory = _memory_binding()
    context = HermesFleetContextBinding(
        principal_id=memory.principal_id,
        principal_kind=memory.principal_kind,
        principal_generation=memory.principal_generation,
        principal_binding_hash=memory.principal_binding_hash,
        agent_instance_id=memory.agent_instance_id,
        base_manifest_digest="sha256:" + "4" * 64,
        run_authority_hash="sha256:" + "5" * 64,
    )
    material = HermesFleetVaultBinding(
        run_id=memory.source_run,
        run_authority_hash=context.run_authority_hash,
        handles=(
            HermesRuntimeMaterialHandle(
                handle="hvh1_" + "A" * 32,
                injection_kind="env",
                injection_target="PROVIDER_KEY",
                version=1,
                expires_at_ms=2_000_000_000_000,
            ),
        ),
    )

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_fleet_vault_scope = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(HermesRunError, match="run_fleet_vault_scope"):
            client.start(
                prompt="blocked",
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=context,
                fleet_vault=material,
            )
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        run_id = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]").start(
            prompt="allowed",
            fleet_runtime=runtime,
            fleet_memory=memory,
            fleet_context=context,
            fleet_vault=material,
        )
    assert run_id == "run-test"
    assert api.requests[-1][3] == {
        "input": "allowed",
        "fleet_runtime": runtime.to_request(),
        "fleet_memory": memory.to_request(),
        "fleet_context": context.to_request(),
        "fleet_vault": material.to_request(),
    }


def test_fleet_skill_learning_requires_capability_and_exact_payload() -> None:
    from hermes_fleet.hermes_runs import (
        HermesFleetContextBinding,
        HermesFleetRuntimeBinding,
        HermesFleetSkillLearningBinding,
        HermesRunError,
        HermesRunsClient,
    )

    runtime = HermesFleetRuntimeBinding(
        container_id="a" * 64,
        plan_fingerprint="sha256:" + "b" * 64,
        image="debian@sha256:" + "c" * 64,
        max_iterations=8,
    )
    memory = _memory_binding()
    context = HermesFleetContextBinding(
        principal_id=memory.principal_id,
        principal_kind=memory.principal_kind,
        principal_generation=memory.principal_generation,
        principal_binding_hash=memory.principal_binding_hash,
        agent_instance_id=memory.agent_instance_id,
        base_manifest_digest="sha256:" + "4" * 64,
        run_authority_hash="sha256:" + "5" * 64,
    )
    learning = HermesFleetSkillLearningBinding(
        principal_id=memory.principal_id,
        principal_kind=memory.principal_kind,
        principal_generation=memory.principal_generation,
        principal_binding_hash=memory.principal_binding_hash,
        agent_instance_id=memory.agent_instance_id,
        source_run=memory.source_run,
        run_authority_hash=context.run_authority_hash,
        recipe_hash="sha256:" + "6" * 64,
        resolved_recipe_hash="sha256:" + "7" * 64,
        plan_fingerprint=runtime.plan_fingerprint,
        capabilities_hash="sha256:" + "9" * 64,
        target_digest="sha256:" + "a" * 64,
        toolsets=("fleet-terminal",),
        filesystem_needs=(),
        network_mode="none",
        network_policy_hash="sha256:" + "d" * 64,
        secret_need_fingerprints=(),
    )

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_fleet_skill_learning = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(HermesRunError, match="run_fleet_skill_learning"):
            client.start(
                prompt="blocked",
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=context,
                fleet_skill_learning=learning,
            )
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_fleet_skill_quarantine = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(HermesRunError, match="run_fleet_skill_quarantine"):
            client.start(
                prompt="blocked",
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=context,
                fleet_skill_learning=learning,
            )
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    api.run_fleet_skill_verification = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(HermesRunError, match="run_fleet_skill_verification"):
            client.start(
                prompt="blocked",
                fleet_runtime=runtime,
                fleet_memory=memory,
                fleet_context=context,
                fleet_skill_learning=learning,
            )
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        run_id = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]").start(
            prompt="allowed",
            fleet_runtime=runtime,
            fleet_memory=memory,
            fleet_context=context,
            fleet_skill_learning=learning,
        )
    assert run_id == "run-test"
    assert api.requests[-1][3] == {
        "input": "allowed",
        "fleet_runtime": runtime.to_request(),
        "fleet_memory": memory.to_request(),
        "fleet_context": context.to_request(),
        "fleet_skill_learning": learning.to_request(),
    }


def test_scoped_memory_write_requires_capability_and_exact_binding() -> None:
    from hermes_fleet.hermes_runs import HermesRunError, HermesRunsClient

    memory = _memory_binding()
    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    api.fleet_scoped_memory_write = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(HermesRunError, match="fleet_scoped_memory_write"):
            client.write_scoped_memory(
                fleet_memory=memory,
                action="add",
                content="Remember this privately.",
            )
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        result = client.write_scoped_memory(
            fleet_memory=memory,
            action="add",
            content="Remember this privately.",
        )
    assert result["success"] is True
    assert api.requests[-1][0:2] == ("POST", "/v1/fleet/memory")
    assert api.requests[-1][3] == {
        "fleet_memory": memory.to_request(),
        "target": "memory",
        "action": "add",
        "content": "Remember this privately.",
    }


def test_learning_promotion_client_requires_capability_and_uses_exact_documents() -> (
    None
):
    from hermes_fleet.hermes_runs import HermesRunError, HermesRunsClient
    from hermes_fleet.principal_identity import PrincipalReference
    from hermes_fleet.promotion import PromotionAuthorization, PromotionScopeRef

    memory = _memory_binding()
    source_hash = "sha256:" + "6" * 64
    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    api.fleet_learning_promotion = False
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        with pytest.raises(HermesRunError, match="fleet_learning_promotion"):
            client.prepare_memory_promotion(
                target="memory",
                source_scope=memory.write_scope,
                source_content_hash=source_hash,
                source_owner_principal_id=memory.principal_id,
                agent_instance_id=memory.agent_instance_id,
            )
    assert [request[0:2] for request in api.requests] == [
        ("GET", "/health"),
        ("GET", "/v1/capabilities"),
    ]

    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    issued = int(time.time() * 1000)
    administrator = PrincipalReference(
        principal_id=memory.principal_id,
        kind="project",
        generation=1,
        binding_hash="sha256:" + "5" * 64,
    )
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="[REDACTED]")
        prepared = client.prepare_memory_promotion(
            target="memory",
            source_scope=memory.write_scope,
            source_content_hash=source_hash,
            source_owner_principal_id=memory.principal_id,
            agent_instance_id=memory.agent_instance_id,
        )
        assert prepared["approved_content_hash"] == source_hash
        assert prepared["authority"] == "none"

        authorization = PromotionAuthorization(
            subject_kind="memory",
            subject_key="memory:" + source_hash,
            source_owner_principal_id=memory.principal_id,
            agent_instance_id=memory.agent_instance_id,
            source_scope=PromotionScopeRef("principal", memory.principal_id),
            target_scope=PromotionScopeRef("project", "project-a"),
            source_content_hash=source_hash,
            approved_content_hash=source_hash,
            administrator=administrator,
            issued_at_ms=issued,
            expires_at_ms=issued + 60_000,
        )
        committed = client.commit_promotion(
            authorization=authorization,
            target="memory",
        )
        assert committed["promotion_id"] == authorization.promotion_id
        assert committed["authority"] == "none"

        history = client.promotion_history(
            subject_kind="memory",
            subject_key=authorization.subject_key,
            source_owner_principal_id=memory.principal_id,
            agent_instance_id=memory.agent_instance_id,
            source_scope=authorization.source_scope,
            target_scope=authorization.target_scope,
        )
        assert history == {
            "current_promotion_id": None,
            "history": [],
            "records": [],
            "authority": "none",
        }

        rollback = PromotionAuthorization(
            subject_kind="memory",
            subject_key=authorization.subject_key,
            source_owner_principal_id=memory.principal_id,
            agent_instance_id=memory.agent_instance_id,
            source_scope=authorization.source_scope,
            target_scope=authorization.target_scope,
            source_content_hash=source_hash,
            approved_content_hash=source_hash,
            administrator=administrator,
            issued_at_ms=issued + 1,
            expires_at_ms=issued + 60_001,
            expected_current_promotion_id="sha256:" + "7" * 64,
            rollback_to_promotion_id="sha256:" + "8" * 64,
            operation="rollback",
        )
        rolled_back = client.rollback_promotion(authorization=rollback)
        assert rolled_back["promotion_id"] == rollback.promotion_id
        assert rolled_back["operation"] == "rollback"
        assert rolled_back["authority"] == "none"

    post_paths = [request[1] for request in api.requests if request[0] == "POST"]
    assert post_paths == [
        "/v1/fleet/promotions/prepare",
        "/v1/fleet/promotions/commit",
        "/v1/fleet/promotions/history",
        "/v1/fleet/promotions/rollback",
    ]


def test_skill_promotion_prepare_requires_exact_verification_evidence() -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    candidate_id = "sha256:" + "b" * 64
    with api.serve() as endpoint:
        prepared = HermesRunsClient(
            endpoint=endpoint,
            api_key="[REDACTED]",
        ).prepare_skill_promotion(
            candidate_id=candidate_id,
            source_owner_principal_id="sha256:" + "1" * 64,
            agent_instance_id="sha256:" + "3" * 64,
        )
    assert prepared["subject_key"] == candidate_id
    assert prepared["verification_digest"] == "sha256:" + "a" * 64
    assert prepared["authority"] == "none"


def test_fleet_runtime_binding_rejects_overbroad_or_unpinned_values() -> None:
    from hermes_fleet.hermes_runs import HermesFleetRuntimeBinding

    with pytest.raises(ValueError, match="container"):
        HermesFleetRuntimeBinding(
            container_id="short",
            plan_fingerprint="sha256:" + "b" * 64,
            image="debian@sha256:" + "c" * 64,
            max_iterations=8,
        )
    with pytest.raises(ValueError, match="digest-pinned"):
        HermesFleetRuntimeBinding(
            container_id="a" * 64,
            plan_fingerprint="sha256:" + "b" * 64,
            image="debian:latest",
            max_iterations=8,
        )
    with pytest.raises(ValueError, match="toolsets"):
        HermesFleetRuntimeBinding(
            container_id="a" * 64,
            plan_fingerprint="sha256:" + "b" * 64,
            image="debian@sha256:" + "c" * 64,
            max_iterations=8,
            toolsets=("fleet-terminal", "web"),
        )


def test_hermes_runs_client_finalizes_after_retryable_pending_state(
    monkeypatch,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    client = HermesRunsClient(
        endpoint="http://127.0.0.1:8642",
        api_key="[REDACTED]",
        poll_interval_seconds=0.001,
    )
    calls: list[tuple[str, str]] = []
    responses = iter(
        (
            (
                409,
                {
                    "error": {
                        "code": "run_finalization_pending",
                        "message": "pending",
                    }
                },
            ),
            (
                200,
                {
                    "object": "hermes.run.finalization",
                    "run_id": "run-test",
                    "status": "completed",
                    "quiescent": True,
                    "session_db_released": True,
                    "log_handlers_released": 2,
                },
            ),
        )
    )

    def request_json(method, path, document=None, *, timeout_seconds=None):
        del document, timeout_seconds
        calls.append((method, path))
        return next(responses)

    monkeypatch.setattr(client, "_request_json", request_json)

    result = client.finalize("run-test", timeout_seconds=1.0)

    assert result["quiescent"] is True
    assert calls == [
        ("POST", "/v1/runs/run-test/finalize"),
        ("POST", "/v1/runs/run-test/finalize"),
    ]


def test_hermes_runs_client_rejects_malformed_finalization(monkeypatch) -> None:
    from hermes_fleet.hermes_runs import HermesRunError, HermesRunsClient

    client = HermesRunsClient(
        endpoint="http://127.0.0.1:8642",
        api_key="[REDACTED]",
    )
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: (
            200,
            {
                "object": "hermes.run.finalization",
                "run_id": "run-test",
                "status": "completed",
                "quiescent": False,
            },
        ),
    )

    with pytest.raises(HermesRunError, match="finalization response is invalid"):
        client.finalize("run-test", timeout_seconds=1.0)


def test_hermes_runs_client_treats_missing_finalization_as_indeterminate(
    monkeypatch,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunIndeterminate, HermesRunsClient

    client = HermesRunsClient(
        endpoint="http://127.0.0.1:8642",
        api_key="[REDACTED]",
    )
    monkeypatch.setattr(
        client,
        "_request_json",
        lambda *args, **kwargs: (404, {"error": {"code": "run_not_found"}}),
    )

    with pytest.raises(HermesRunIndeterminate, match="finalization is unavailable"):
        client.finalize("run-test", timeout_seconds=1.0)


def test_hermes_runs_client_classifies_exact_run_without_mutation() -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    api = _RunsAPI([{"status": "running"}, {"status": "completed", "output": "done"}])
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="secret-token-for-test")
        assert client.status("run-test") == "running"
        assert client.status("run-test") == "terminal"
        assert client.status("missing-run") == "missing"

    assert all(method == "GET" for method, _path, _auth, _body in api.requests)


def test_hermes_runs_client_health_shares_one_absolute_request_budget(
    monkeypatch,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    client = HermesRunsClient(
        endpoint="http://127.0.0.1:8642",
        api_key="secret-token-for-test",
    )
    request_timeouts: list[float | None] = []
    monotonic_values = iter((10.0, 10.0, 10.25))

    monkeypatch.setattr(
        "hermes_fleet.hermes_runs.time.monotonic",
        lambda: next(monotonic_values),
    )

    def request_json(method, path, document=None, *, timeout_seconds=None):
        del method, document
        request_timeouts.append(timeout_seconds)
        if path == "/health":
            return 200, {"status": "ok"}
        return 200, {
            "object": "hermes.api_server.capabilities",
            "features": {
                "run_submission": True,
                "run_status": True,
                "run_stop": True,
                "run_finalize": True,
                "run_approval_budget": True,
                "run_tool_evidence": True,
                "run_command_evidence": True,
            },
        }

    monkeypatch.setattr(client, "_request_json", request_json)

    health = client.health(timeout_seconds=0.5)

    assert health["api"] == "healthy"
    assert request_timeouts == [pytest.approx(0.5), pytest.approx(0.25)]


def test_hermes_runs_client_expired_submission_budget_sends_no_post() -> None:
    from hermes_fleet.hermes_runs import (
        HermesRunsClient,
        HermesRunSubmissionUnknown,
    )

    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    with api.serve() as endpoint:
        client = HermesRunsClient(endpoint=endpoint, api_key="secret-token-for-test")
        with pytest.raises(HermesRunSubmissionUnknown, match="outcome is unknown"):
            client.start(prompt="Do not send.", timeout_seconds=0)

    assert api.requests == []


def test_hermes_runs_client_bounds_blocking_post_by_remaining_deadline() -> None:
    from hermes_fleet.hermes_runs import (
        HermesRunsClient,
        HermesRunSubmissionUnknown,
    )

    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    api.post_delay_seconds = 0.2
    with api.serve() as endpoint:
        client = HermesRunsClient(
            endpoint=endpoint,
            api_key="secret-token-for-test",
            request_timeout_seconds=1,
        )
        started = time.monotonic()
        with pytest.raises(HermesRunSubmissionUnknown, match="outcome is unknown"):
            client.start(prompt="Bounded request.", timeout_seconds=0.03)
        elapsed = time.monotonic() - started

    assert elapsed < 0.15


def test_hermes_runs_client_preserves_deterministic_http_rejection() -> None:
    from hermes_fleet.hermes_runs import HermesRunError, HermesRunsClient

    api = _RunsAPI([{"status": "completed", "output": "unused"}])
    api.post_status = 401
    with api.serve() as endpoint:
        with pytest.raises(HermesRunError, match="did not accept"):
            HermesRunsClient(
                endpoint=endpoint,
                api_key="wrong-profile-token",
                profile="fleet-execution",
            ).start(prompt="Do not create a run.", timeout_seconds=1)


def test_hermes_runs_client_fails_closed_when_approval_is_required() -> None:
    from hermes_fleet.hermes_runs import HermesRunError, HermesRunsClient

    api = _RunsAPI([{"status": "waiting_for_approval"}])
    with api.serve() as endpoint:
        with pytest.raises(HermesRunError, match="requires approval"):
            HermesRunsClient(
                endpoint=endpoint,
                api_key="secret-token-for-test",
                poll_interval_seconds=0.001,
            ).run(prompt="Do work.", timeout_seconds=1)


def test_hermes_runs_client_resolves_recipe_scoped_approval_once() -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    api = _RunsAPI(
        [
            {"status": "waiting_for_approval"},
            {"status": "completed", "output": "done"},
        ]
    )
    with api.serve() as endpoint:
        client = HermesRunsClient(
            endpoint=endpoint,
            api_key="[REDACTED]",
            poll_interval_seconds=0.001,
        )
        run_id = client.start(
            prompt="Do approved work.",
            approval_budget=1,
            timeout_seconds=1,
        )
        result = client.wait(
            run_id=run_id,
            timeout_seconds=1,
            approval_mode="once",
            approval_budget=1,
        )

    assert result.text == "done"
    assert any(
        method == "POST"
        and path.endswith("/v1/runs")
        and body is not None
        and body.get("approval_budget") == 1
        for method, path, _, body in api.requests
    )
    assert any(
        method == "POST" and path.endswith("/approval") and body == {"choice": "once"}
        for method, path, _, body in api.requests
    )
    assert not any(path.endswith("/stop") for _, path, _, _ in api.requests)


def test_hermes_runs_client_refuses_approval_beyond_budget() -> None:
    from hermes_fleet.hermes_runs import HermesRunError, HermesRunsClient

    api = _RunsAPI(
        [
            {"status": "waiting_for_approval"},
            {"status": "waiting_for_approval"},
        ]
    )
    with api.serve() as endpoint:
        client = HermesRunsClient(
            endpoint=endpoint,
            api_key="[REDACTED]",
            poll_interval_seconds=0.001,
        )
        run_id = client.start(
            prompt="Try too many approvals.",
            approval_budget=1,
            timeout_seconds=1,
        )
        with pytest.raises(HermesRunError, match="exceeded approval budget"):
            client.wait(
                run_id=run_id,
                timeout_seconds=1,
                approval_mode="once",
                approval_budget=1,
            )

    approval_posts = [
        request
        for request in api.requests
        if request[0] == "POST" and request[1].endswith("/approval")
    ]
    assert len(approval_posts) == 1
    assert any(path.endswith("/stop") for _, path, _, _ in api.requests)


def test_hermes_runs_client_requests_stop_at_the_fleet_deadline() -> None:
    from hermes_fleet.hermes_runs import HermesRunError, HermesRunsClient

    api = _RunsAPI([{"status": "running"}])
    with api.serve() as endpoint:
        with pytest.raises(HermesRunError, match="exceeded Fleet deadline"):
            HermesRunsClient(
                endpoint=endpoint,
                api_key="secret-token-for-test",
                poll_interval_seconds=0.001,
            ).run(prompt="Keep working.", timeout_seconds=0.01)

    assert any(
        method == "POST" and path.endswith("/stop")
        for method, path, _, _ in api.requests
    )


def test_hermes_runs_client_confirms_exact_stop_before_reporting_deadline() -> None:
    from hermes_fleet.hermes_runs import HermesRunDeadlineExceeded, HermesRunsClient

    api = _RunsAPI([{"status": "running"}])
    api.stop_delay_seconds = 0.1
    with api.serve() as endpoint:
        with pytest.raises(HermesRunDeadlineExceeded):
            HermesRunsClient(
                endpoint=endpoint,
                api_key="secret-token-for-test",
                poll_interval_seconds=0.001,
                request_timeout_seconds=1,
            ).run(prompt="Keep working.", timeout_seconds=0.01)

        assert api.stop_response_sent is True
        stop_requests = [
            request for request in api.requests if request[1].endswith("/stop")
        ]
        assert [request[1] for request in stop_requests] == ["/v1/runs/run-test/stop"]


def test_hermes_runs_client_treats_unconfirmed_deadline_stop_as_indeterminate(
    monkeypatch,
) -> None:
    from hermes_fleet.hermes_runs import (
        HermesRunError,
        HermesRunIndeterminate,
        HermesRunsClient,
    )

    client = HermesRunsClient(
        endpoint="http://127.0.0.1:8642",
        api_key="secret-token-for-test",
        poll_interval_seconds=0.001,
    )
    requests: list[tuple[str, str, float | None]] = []

    def request_json(method, path, document=None, *, timeout_seconds=None):
        del document
        requests.append((method, path, timeout_seconds))
        if method == "GET":
            return 200, {"status": "running"}
        raise HermesRunError("stop was not confirmed")

    monkeypatch.setattr(client, "_request_json", request_json)

    with pytest.raises(HermesRunIndeterminate, match="cancellation is indeterminate"):
        client.wait(run_id="run-test", timeout_seconds=0.001)

    assert requests[-1] == ("POST", "/v1/runs/run-test/stop", 0.25)


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://192.0.2.10:8642",
        "https://example.com",
        "http://user:password@127.0.0.1:8642",
    ),
)
def test_hermes_runs_client_rejects_non_loopback_or_credentialed_endpoints(
    endpoint: str,
) -> None:
    from hermes_fleet.hermes_runs import HermesRunsClient

    with pytest.raises(ValueError, match="loopback"):
        HermesRunsClient(endpoint=endpoint, api_key="secret-token-for-test")
