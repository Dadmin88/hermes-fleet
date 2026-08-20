from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import hermes_fleet.templar_sandbox as templar_sandbox_module
from hermes_fleet.templar import (
    ALLOW,
    DENY,
    ORIGIN_FAIL_CLOSED,
    TEMPLAR_BACKEND_RESPONSE_SCHEMA,
    TemplarCore,
    TemplarEvaluationRequest,
    TemplarEvaluatorIdentity,
    TemplarPolicyRef,
)
from hermes_fleet.templar_sandbox import (
    PROVIDER_CHANNEL,
    PROVIDER_NONE,
    TEMPLAR_SANDBOX_POLICY_SCHEMA,
    TemplarEvaluatorArtifact,
    TemplarSandboxBackend,
    TemplarSandboxError,
    TemplarSandboxPolicy,
    TemplarSandboxProtocolError,
    TemplarSandboxTimeout,
    TemplarSandboxUnavailable,
)
from tests.unit.test_security_event import event

INTEGRATION = (
    os.environ.get("FLEET_TEMPLAR_SANDBOX_INTEGRATION") == "1"
    and sys.platform.startswith("linux")
    and shutil.which("bwrap") is not None
)


def h(character: str) -> str:
    return "sha256:" + character * 64


POLICY = TemplarPolicyRef(
    policy_id="templar-core",
    policy_version="phase21-v1",
    policy_digest=h("a"),
)
EVALUATOR = TemplarEvaluatorIdentity(
    evaluator_id="templar-sandbox-model",
    implementation_version="fleet-templar-sandbox-v1",
    model_provider="test-provider",
    model_name="test-model",
    model_version="test-model-v1",
)


class Clock:
    def __init__(self) -> None:
        self.wall = 50_000
        self.monotonic = [1_000, 1_001]

    def wall_ms(self) -> int:
        value = self.wall
        self.wall += 1
        return value

    def monotonic_ms(self) -> int:
        return self.monotonic.pop(0)


def source_hash(source: str) -> str:
    return "sha256:" + hashlib.sha256(source.encode()).hexdigest()


def document_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def artifact(tmp_path: Path, source: str) -> TemplarEvaluatorArtifact:
    path = tmp_path / "evaluator.py"
    path.write_text(source, encoding="utf-8")
    return TemplarEvaluatorArtifact(
        source_path=str(path),
        content_hash=source_hash(source),
    )


def valid_response_source(extra: str = "") -> str:
    return f"""
import json
import sys
request = json.load(sys.stdin)
{extra}
response = {{
    "schema": "{TEMPLAR_BACKEND_RESPONSE_SCHEMA}",
    "evaluation_id": request["evaluation_id"],
    "request_hash": request["request_hash"],
    "event_hash": request["event_hash"],
    "decision": "ALLOW",
    "reason_codes": [],
}}
print(json.dumps(response, sort_keys=True, separators=(",", ":")))
"""


def evaluation_request() -> dict[str, Any]:
    request = TemplarEvaluationRequest.from_event(
        event(),
        templar_policy=POLICY,
        evaluator=EVALUATOR,
        issued_at_ms=50_000,
        deadline_ms=55_000,
    )
    return request.to_dict()


def core(backend: TemplarSandboxBackend) -> TemplarCore:
    clock = Clock()
    return TemplarCore(
        backend=backend,
        policy=POLICY,
        evaluator=EVALUATOR,
        timeout_ms=2_000,
        verdict_ttl_ms=5_000,
        wall_clock_ms=clock.wall_ms,
        monotonic_ms=clock.monotonic_ms,
    )


def test_policy_is_immutable_versioned_and_content_addressed() -> None:
    first = TemplarSandboxPolicy()
    second = TemplarSandboxPolicy()
    assert first.to_dict()["schema"] == TEMPLAR_SANDBOX_POLICY_SCHEMA
    assert first.to_dict()["network"] == "unshared"
    assert first.to_dict()["host_data_mounts"] == "none"
    assert first.content_hash == second.content_hash
    assert first.provider_access == PROVIDER_NONE


def test_provider_policy_requires_exact_channel_configuration(tmp_path: Path) -> None:
    item = artifact(tmp_path, valid_response_source())
    with pytest.raises(TemplarSandboxProtocolError, match="cannot be configured"):
        TemplarSandboxBackend(
            artifact=item,
            evaluator=EVALUATOR,
            provider_channel_factory=lambda **_kwargs: socket.socketpair()[0],
        )
    with pytest.raises(TemplarSandboxProtocolError, match="requires"):
        TemplarSandboxBackend(
            artifact=item,
            evaluator=EVALUATOR,
            policy=TemplarSandboxPolicy(provider_access=PROVIDER_CHANNEL),
        )


def test_provider_channel_rejects_named_unix_socket(tmp_path: Path) -> None:
    socket_path = tmp_path / "provider.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        server.listen(1)
        client.connect(str(socket_path))
        accepted, _address = server.accept()
        try:
            with pytest.raises(
                TemplarSandboxProtocolError, match="anonymous socketpair"
            ):
                templar_sandbox_module._validate_provider_socket(client)
        finally:
            accepted.close()
    finally:
        client.close()
        server.close()


def test_artifact_hash_mismatch_fails_before_execution(tmp_path: Path) -> None:
    path = tmp_path / "evaluator.py"
    path.write_text(valid_response_source(), encoding="utf-8")
    item = TemplarEvaluatorArtifact(source_path=str(path), content_hash=h("f"))
    backend = TemplarSandboxBackend(artifact=item, evaluator=EVALUATOR)
    with pytest.raises(TemplarSandboxProtocolError, match="hash does not match"):
        backend.evaluate(evaluation_request(), timeout_ms=1_000)


def test_evaluator_artifact_symlink_is_rejected(tmp_path: Path) -> None:
    source = valid_response_source()
    target = tmp_path / "real.py"
    target.write_text(source, encoding="utf-8")
    link = tmp_path / "linked.py"
    link.symlink_to(target)
    item = TemplarEvaluatorArtifact(
        source_path=str(link),
        content_hash=source_hash(source),
    )
    with pytest.raises(TemplarSandboxProtocolError, match="opened safely"):
        item.read_verified()


def test_direct_request_closed_schema_and_evaluation_id_fail_closed(
    tmp_path: Path,
) -> None:
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, valid_response_source()),
        evaluator=EVALUATOR,
    )
    extra = evaluation_request()
    extra["unexpected"] = True
    with pytest.raises(TemplarSandboxProtocolError, match="closed schema"):
        backend.evaluate(extra, timeout_ms=1_000)

    forged = evaluation_request()
    forged["evaluation_id"] = h("e")
    with pytest.raises(TemplarSandboxProtocolError, match="evaluation id"):
        backend.evaluate(forged, timeout_ms=1_000)


def test_secret_bearing_request_field_is_rejected(tmp_path: Path) -> None:
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, valid_response_source()),
        evaluator=EVALUATOR,
    )
    request = evaluation_request()
    request["event"]["secret_body"] = "must-never-enter-sandbox"
    request["event_hash"] = document_hash(request["event"])
    request_without_id = {
        key: value for key, value in request.items() if key != "evaluation_id"
    }
    request["evaluation_id"] = document_hash(request_without_id)
    with pytest.raises(TemplarSandboxProtocolError, match="secret-bearing"):
        backend.evaluate(request, timeout_ms=1_000)


def test_missing_bubblewrap_fails_closed(tmp_path: Path) -> None:
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, valid_response_source()),
        evaluator=EVALUATOR,
        bwrap_path="/definitely/not/bwrap",
    )
    with pytest.raises(TemplarSandboxUnavailable, match="Bubblewrap"):
        backend.evaluate(evaluation_request(), timeout_ms=1_000)


@pytest.mark.skipif(
    not INTEGRATION, reason="explicit Bubblewrap integration proof required"
)
def test_real_sandbox_denies_host_network_tools_state_and_excess_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_TEST_CREDENTIAL", "must-not-cross-boundary")
    monkeypatch.setenv("KERYX_TOKEN", "must-not-cross-boundary")
    monkeypatch.setenv("HERMES_HOME", "/host/hermes-home")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/host/ssh-agent.sock")
    monkeypatch.setenv("DOCKER_HOST", "unix:///host/docker.sock")
    monkeypatch.setenv("VAULT_TOKEN", "must-not-cross-boundary")
    checks = r"""
import os
import resource
import shutil
import socket

assert os.geteuid() == 65534
assert os.getegid() == 65534
assert os.environ.get("TEMPLAR_SANDBOX") == "1"
for denied_env in (
    "FLEET_TEST_CREDENTIAL",
    "KERYX_TOKEN",
    "HERMES_HOME",
    "SSH_AUTH_SOCK",
    "DOCKER_HOST",
    "VAULT_TOKEN",
):
    assert denied_env not in os.environ
assert "TEMPLAR_PROVIDER_FD" not in os.environ
assert set(os.environ) == {
    "HOME",
    "LC_CTYPE",
    "PATH",
    "PWD",
    "TEMPLAR_SANDBOX",
    "TMPDIR",
}
assert os.environ["PWD"] == "/work"
assert os.getcwd() == "/work"
assert shutil.which("ssh") is None
assert shutil.which("sh") is None
for denied in (
    "/home",
    "/root",
    "/run",
    "/var",
    "/media",
    "/mnt",
    "/workspace",
    "/run/docker.sock",
    "/var/run/docker.sock",
    "/run/fleet",
    "/run/hermes",
):
    assert not os.path.exists(denied), denied
assert not os.path.exists("/tmp/home/.hermes")

with open("/tmp/ephemeral-write", "w", encoding="utf-8") as handle:
    handle.write("ok")
with open("/work/ephemeral-work", "w", encoding="utf-8") as handle:
    handle.write("ok")

cap_eff = None
no_new_privs = None
for line in open("/proc/self/status", encoding="utf-8"):
    if line.startswith("CapEff:"):
        cap_eff = int(line.split()[1], 16)
    elif line.startswith("NoNewPrivs:"):
        no_new_privs = int(line.split()[1])
assert cap_eff == 0
assert no_new_privs == 1

limits = (
    (resource.RLIMIT_CPU, 2),
    (resource.RLIMIT_AS, 268435456),
    (resource.RLIMIT_FSIZE, 65536),
    (resource.RLIMIT_NOFILE, 64),
    (resource.RLIMIT_NPROC, 16),
)
for kind, ceiling in limits:
    soft, _hard = resource.getrlimit(kind)
    assert soft != resource.RLIM_INFINITY
    assert soft <= ceiling

try:
    resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
except (ValueError, PermissionError):
    pass
else:
    raise AssertionError("evaluator raised a hard resource ceiling")

probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.settimeout(0.15)
probe_address = ".".join(("1", "1", "1", "1"))
try:
    assert probe.connect_ex((probe_address, 53)) != 0
finally:
    probe.close()

try:
    bytearray(536870912)
except MemoryError:
    pass
else:
    raise AssertionError("address-space limit did not hold")
"""
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, valid_response_source(checks)),
        evaluator=EVALUATOR,
    )
    verdict = core(backend).evaluate(event())
    assert verdict.decision == ALLOW
    assert verdict.authority == "none"


class EchoProviderFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.threads: list[threading.Thread] = []

    def __call__(
        self,
        *,
        evaluator: TemplarEvaluatorIdentity,
        request_hash: str,
    ) -> socket.socket:
        self.calls.append((evaluator.evaluator_id, request_hash))
        parent, child = socket.socketpair()

        def serve() -> None:
            with parent:
                payload = parent.recv(16)
                assert payload == b"provider-ping"
                parent.sendall(b"provider-pong")

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.threads.append(thread)
        return child


@pytest.mark.skipif(
    not INTEGRATION, reason="explicit Bubblewrap integration proof required"
)
def test_provider_access_is_one_af_unix_channel_while_ip_network_stays_denied(
    tmp_path: Path,
) -> None:
    provider = EchoProviderFactory()
    checks = r"""
import os
import socket

fd = int(os.environ["TEMPLAR_PROVIDER_FD"])
channel = socket.socket(fileno=fd)
assert channel.family == socket.AF_UNIX
channel.sendall(b"provider-ping")
assert channel.recv(32) == b"provider-pong"

probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
probe.settimeout(0.15)
probe_address = ".".join(("1", "1", "1", "1"))
try:
    assert probe.connect_ex((probe_address, 53)) != 0
finally:
    probe.close()
"""
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, valid_response_source(checks)),
        evaluator=EVALUATOR,
        policy=TemplarSandboxPolicy(provider_access=PROVIDER_CHANNEL),
        provider_channel_factory=provider,
    )
    verdict = core(backend).evaluate(event())
    assert verdict.decision == ALLOW
    assert len(provider.calls) == 1
    assert provider.calls[0][0] == EVALUATOR.evaluator_id
    for thread in provider.threads:
        thread.join(timeout=1)
        assert not thread.is_alive()


@pytest.mark.skipif(
    not INTEGRATION, reason="explicit Bubblewrap integration proof required"
)
def test_fresh_sandbox_has_no_cross_evaluation_state(tmp_path: Path) -> None:
    checks = r"""
import os
assert not os.path.exists("/tmp/cross-evaluation-state")
with open("/tmp/cross-evaluation-state", "w", encoding="utf-8") as handle:
    handle.write("must-disappear")
"""
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, valid_response_source(checks)),
        evaluator=EVALUATOR,
    )
    assert core(backend).evaluate(event()).decision == ALLOW
    assert core(backend).evaluate(event()).decision == ALLOW


def host_process_has_marker(marker: str) -> bool:
    encoded = marker.encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if encoded in command:
            return True
    return False


@pytest.mark.skipif(
    not INTEGRATION, reason="explicit Bubblewrap integration proof required"
)
def test_hard_timeout_kills_evaluator_process_tree(tmp_path: Path) -> None:
    marker = f"templar-timeout-child-{os.getpid()}-{time.time_ns()}"
    source = f"""
import json
import subprocess
import sys
import time
json.load(sys.stdin)
subprocess.Popen([
    sys.executable,
    "-I",
    "-S",
    "-c",
    "import time; time.sleep(30)",
    "{marker}",
])
time.sleep(30)
"""
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, source),
        evaluator=EVALUATOR,
        policy=TemplarSandboxPolicy(wall_clock_ms=250),
    )
    with pytest.raises(TemplarSandboxTimeout, match="hard timeout"):
        backend.evaluate(evaluation_request(), timeout_ms=2_000)

    deadline = time.monotonic() + 2
    while host_process_has_marker(marker) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not host_process_has_marker(marker)


@pytest.mark.skipif(
    not INTEGRATION, reason="explicit Bubblewrap integration proof required"
)
def test_core_maps_hard_timeout_to_fail_closed_deny(tmp_path: Path) -> None:
    source = "import json,sys,time; json.load(sys.stdin); time.sleep(30)\n"
    backend = TemplarSandboxBackend(
        artifact=artifact(tmp_path, source),
        evaluator=EVALUATOR,
        policy=TemplarSandboxPolicy(wall_clock_ms=200),
    )
    verdict = core(backend).evaluate(event())
    assert verdict.decision == DENY
    assert verdict.origin == ORIGIN_FAIL_CLOSED
    assert verdict.reason_codes == ("evaluator-timeout",)


@pytest.mark.skipif(
    not INTEGRATION, reason="explicit Bubblewrap integration proof required"
)
def test_nonzero_or_malformed_evaluator_output_fails_closed(tmp_path: Path) -> None:
    bad_exit = artifact(tmp_path, "raise SystemExit(7)\n")
    backend = TemplarSandboxBackend(artifact=bad_exit, evaluator=EVALUATOR)
    with pytest.raises(TemplarSandboxError, match="failed closed"):
        backend.evaluate(evaluation_request(), timeout_ms=1_000)

    malformed_source = 'print("not-json")\n'
    malformed = artifact(tmp_path, malformed_source)
    backend = TemplarSandboxBackend(artifact=malformed, evaluator=EVALUATOR)
    with pytest.raises(TemplarSandboxProtocolError, match="malformed JSON"):
        backend.evaluate(evaluation_request(), timeout_ms=1_000)
