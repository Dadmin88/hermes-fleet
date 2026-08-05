from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


class _RunsAPI:
    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self.statuses = list(statuses)
        self.requests: list[tuple[str, str, str, dict[str, Any] | None]] = []

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
                if self.path == "/v1/runs":
                    self._json(202, {"run_id": "run-test", "status": "started"})
                    return
                if self.path == "/v1/runs/run-test/stop":
                    self._json(200, {"run_id": "run-test", "status": "stopping"})
                    return
                self._json(404, {"error": {"message": "not found"}})

            def do_GET(self) -> None:
                api.requests.append(
                    ("GET", self.path, self.headers.get("Authorization", ""), None)
                )
                if self.path == "/health":
                    self._json(200, {"status": "ok"})
                    return
                if self.path == "/v1/capabilities":
                    self._json(
                        200,
                        {
                            "object": "hermes.api_server.capabilities",
                            "features": {
                                "run_submission": True,
                                "run_status": True,
                                "run_stop": True,
                            },
                        },
                    )
                    return
                if self.path != "/v1/runs/run-test":
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
    }
    assert [request[1] for request in api.requests] == ["/health", "/v1/capabilities"]


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
