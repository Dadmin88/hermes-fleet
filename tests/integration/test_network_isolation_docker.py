from __future__ import annotations

import json
import platform
import shutil
import socket
import subprocess
import time
import uuid

import pytest

from hermes_fleet.backend_capabilities import BackendCapabilities
from hermes_fleet.execution_backend import (
    BackendExecutionState,
    ExecutionBackendError,
    ExecutionBackendErrorCode,
    ExecutionPlan,
)
from hermes_fleet.network_isolation import (
    NETWORK_EXPLICIT_INTERNET,
    NETWORK_PROJECT_ALLOWLIST,
    NETWORK_PROVIDER_ONLY,
    DockerEgressController,
    NetworkAuthorityScope,
    NetworkDestination,
    NetworkGrant,
    NetworkIsolatedWorkshopBackend,
)
from hermes_fleet.oci_backend import OciRealizationSpec
from hermes_fleet.recipes import ResolvedRecipe

BASE_IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)


def _ipv4(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


AUTHORITY = "sha256:" + "8" * 64
APPROVAL = "sha256:" + "9" * 64
PUBLIC_IP = _ipv4(1, 1, 1, 1)


def _run(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def _docker_ready() -> bool:
    if shutil.which("docker") is None or platform.machine() != "x86_64":
        return False
    return _run(["docker", "image", "inspect", BASE_IMAGE], timeout=10).returncode == 0


def _capabilities() -> BackendCapabilities:
    return BackendCapabilities.from_dict(
        {
            "schema": "fleet.backend-capabilities.v1",
            "backend_kind": "fleet.dev/docker-oci",
            "platform": {"os": "linux", "architecture": "x86_64"},
            "isolation": ["container"],
            "network": ["none"],
            "resources": {
                "cpu_millis": 1000,
                "memory_bytes": 268_435_456,
            },
            "filesystem": {
                "ephemeral_root": True,
                "read_only_inputs": True,
            },
            "materialization": {
                "agency_profile": True,
                "artifacts": True,
            },
            "extensions": {},
        }
    )


def _recipe() -> ResolvedRecipe:
    return ResolvedRecipe.from_dict(
        {
            "schema": "fleet.resolved-recipe.v1",
            "recipe_hash": "sha256:" + "6" * 64,
            "agent": {
                "kind": "agency_profile",
                "repository": "https://example.invalid/agency.git",
                "revision": "c" * 40,
                "name": "network-proof",
                "version": "1.0.0",
                "content_digest": "sha256:" + "7" * 64,
            },
            "extensions": {},
        }
    )


def _plan(execution_id: str) -> ExecutionPlan:
    capabilities = _capabilities()
    return ExecutionPlan(
        execution_id=execution_id,
        idempotency_key=f"request-{execution_id}",
        resolved_recipe=_recipe(),
        required_capabilities_hash=capabilities.content_hash,
    )


def _realization() -> OciRealizationSpec:
    return OciRealizationSpec(
        image=BASE_IMAGE,
        argv=("sleep", "infinity"),
        network="none",
        cpu_millis=100,
        memory_bytes=67_108_864,
        pids_limit=16,
    )


def _authority() -> NetworkAuthorityScope:
    return NetworkAuthorityScope(run_authority_hash=AUTHORITY)


def _approved_internet_authority() -> NetworkAuthorityScope:
    return NetworkAuthorityScope(
        run_authority_hash=AUTHORITY,
        approved_internet_hashes=(APPROVAL,),
    )


def _project_grant() -> NetworkGrant:
    return NetworkGrant(
        mode=NETWORK_PROJECT_ALLOWLIST,
        authority_ref=AUTHORITY,
        destinations=(
            NetworkDestination(
                host=PUBLIC_IP,
                resolved_ips=(PUBLIC_IP,),
                ports=(443,),
            ),
        ),
    )


def _approved_internet_grant() -> NetworkGrant:
    return NetworkGrant(
        mode=NETWORK_EXPLICIT_INTERNET,
        authority_ref=AUTHORITY,
        destinations=(
            NetworkDestination(
                host=PUBLIC_IP,
                resolved_ips=(PUBLIC_IP,),
                ports=(443,),
            ),
        ),
        approval_ref=APPROVAL,
    )


def _backend(
    *,
    grant: NetworkGrant,
    controller: DockerEgressController,
    binding,
    authority: NetworkAuthorityScope | None = None,
) -> NetworkIsolatedWorkshopBackend:
    return NetworkIsolatedWorkshopBackend(
        capabilities=_capabilities(),
        realization=_realization(),
        deadline_ms=int(time.time() * 1000) + 60_000,
        network_grant=grant,
        network_authority=authority or _authority(),
        egress_binding=binding,
        egress_controller=controller,
    )


def _connect_proxy(
    container_id: str,
    proxy_ip: str,
    host: str,
    port: int,
) -> subprocess.CompletedProcess[str]:
    script = (
        "use IO::Socket::INET; "
        "$s=IO::Socket::INET->new(PeerAddr=>$ARGV[0],PeerPort=>8080,"
        "Proto=>'tcp',Timeout=>3); exit 20 unless $s; "
        "$s->autoflush(1); "
        'print $s "CONNECT ".$ARGV[1].\':\'.$ARGV[2]." HTTP/1.1\\r\\n"; '
        'print $s "Host: ".$ARGV[1].\':\'.$ARGV[2]."\\r\\n\\r\\n"; '
        "$line=<$s>; print $line // '';"
    )
    return _run(
        [
            "docker",
            "exec",
            container_id,
            "perl",
            "-MIO::Socket::INET",
            "-e",
            script,
            proxy_ip,
            host,
            str(port),
        ]
    )


def _direct_tcp(
    container_id: str,
    host: str,
    port: int,
) -> subprocess.CompletedProcess[str]:
    script = (
        "$s=IO::Socket::INET->new(PeerAddr=>$ARGV[0],PeerPort=>$ARGV[1],"
        "Proto=>'tcp',Timeout=>2); exit($s ? 0 : 1);"
    )
    return _run(
        [
            "docker",
            "exec",
            container_id,
            "perl",
            "-MIO::Socket::INET",
            "-e",
            script,
            host,
            str(port),
        ]
    )


@pytest.mark.skipif(not _docker_ready(), reason="Docker/pinned amd64 image unavailable")
def test_provider_only_real_workshop_stays_network_none() -> None:
    grant = NetworkGrant(
        mode=NETWORK_PROVIDER_ONLY,
        authority_ref=AUTHORITY,
    )
    controller = DockerEgressController(gateway_image=BASE_IMAGE)
    binding = controller.prepare(
        execution_id="phase4-provider-only",
        grant=grant,
        authority=_authority(),
    )
    backend = NetworkIsolatedWorkshopBackend(
        capabilities=_capabilities(),
        realization=_realization(),
        deadline_ms=int(time.time() * 1000) + 60_000,
        network_grant=grant,
        network_authority=_authority(),
        egress_binding=binding,
    )
    plan = _plan(f"phase4provider{uuid.uuid4().hex[:12]}")
    handle = None
    try:
        handle = backend.ensure(plan)
        assert handle.state == BackendExecutionState.RUNNING
        inspected = json.loads(
            _run(["docker", "inspect", handle.realization_id]).stdout
        )[0]
        assert inspected["HostConfig"]["NetworkMode"] == "none"
        assert inspected["Config"]["Labels"]["dev.hermes.fleet.network_mode"] == (
            NETWORK_PROVIDER_ONLY
        )
        assert inspected["Config"]["Labels"]["hermes-egress"] == "off"
        assert not any(
            item.lower().startswith(("http_proxy=", "https_proxy="))
            for item in inspected["Config"].get("Env", [])
        )
    finally:
        if handle is not None:
            backend.cleanup_plan(plan, handle=handle)


@pytest.mark.skipif(not _docker_ready(), reason="Docker/pinned amd64 image unavailable")
def test_real_gateway_blocks_proxy_bypass_management_and_lateral_peers() -> None:
    execution_id = f"phase4net{uuid.uuid4().hex[:12]}"
    grant = _project_grant()
    controller = DockerEgressController(gateway_image=BASE_IMAGE)
    binding = controller.prepare(
        execution_id=execution_id,
        grant=grant,
        authority=_authority(),
    )
    backend = _backend(grant=grant, controller=controller, binding=binding)
    plan = _plan(execution_id)
    handle = None
    lateral_id: str | None = None
    try:
        handle = backend.ensure(plan)
        assert handle.state == BackendExecutionState.RUNNING
        inspected = json.loads(
            _run(["docker", "inspect", handle.realization_id]).stdout
        )[0]
        assert inspected["HostConfig"]["NetworkMode"] == binding.docker_network
        assert inspected["HostConfig"]["Dns"] == [_ipv4(127, 0, 0, 1)]
        assert set(inspected["NetworkSettings"]["Networks"]) == {binding.docker_network}
        proxy = f"http://{binding.gateway_ip}:8080"
        env = set(inspected["Config"].get("Env", []))
        assert {f"HTTP_PROXY={proxy}", f"HTTPS_PROXY={proxy}"}.issubset(env)

        # Topology, not proxy cooperation, blocks raw external sockets.
        assert _direct_tcp(handle.realization_id, PUBLIC_IP, 443).returncode != 0

        # Direct DNS is disabled; all domain resolution must occur at the gateway.
        dns = _run(
            [
                "docker",
                "exec",
                handle.realization_id,
                "perl",
                "-MSocket",
                "-e",
                "exit(gethostbyname('example.com') ? 0 : 1);",
            ]
        )
        assert dns.returncode != 0

        metadata = _connect_proxy(
            handle.realization_id,
            binding.gateway_ip or "",
            _ipv4(169, 254, 169, 254),
            80,
        )
        assert " 403 " in metadata.stdout
        tailscale = _connect_proxy(
            handle.realization_id,
            binding.gateway_ip or "",
            _ipv4(100, 100, 100, 100),
            443,
        )
        assert " 403 " in tailscale.stdout
        unlisted = _connect_proxy(
            handle.realization_id,
            binding.gateway_ip or "",
            _ipv4(8, 8, 8, 8),
            443,
        )
        assert " 403 " in unlisted.stdout
        docker_remote = _connect_proxy(
            handle.realization_id,
            binding.gateway_ip or "",
            PUBLIC_IP,
            2375,
        )
        assert " 403 " in docker_remote.stdout

        # A new peer on the internal network is lateral movement and invalidates
        # the workshop before Hermes may continue using it.
        created = _run(
            [
                "docker",
                "create",
                "--network",
                binding.docker_network,
                BASE_IMAGE,
                "sleep",
                "infinity",
            ]
        )
        assert created.returncode == 0
        lateral_id = created.stdout.strip()
        assert _run(["docker", "start", lateral_id]).returncode == 0
        with pytest.raises(ExecutionBackendError) as drift:
            backend.inspect(handle)
        assert drift.value.code == ExecutionBackendErrorCode.CAPABILITY_MISMATCH
        _run(["docker", "rm", "--force", lateral_id])
        lateral_id = None
        assert backend.inspect(handle).state == BackendExecutionState.RUNNING

        # Every proxy decision is recorded against the exact policy hash.
        time.sleep(0.2)
        records = controller.audit(binding)
        reasons = {record.reason for record in records}
        assert "dns_failed" in reasons or "non_public_resolution" in reasons
        assert "destination_not_allowlisted" in reasons
        assert "forbidden_port" in reasons
        assert all(record.decision == "deny" for record in records)
    finally:
        if lateral_id is not None:
            _run(["docker", "rm", "--force", lateral_id])
        if handle is not None:
            backend.cleanup_plan(plan, handle=handle)
        controller.cleanup(binding)
        gateway_absent = _run(["docker", "inspect", binding.gateway_container_id or ""])
        assert gateway_absent.returncode != 0
        network_absent = _run(["docker", "network", "inspect", binding.docker_network])
        assert network_absent.returncode != 0


@pytest.mark.skipif(
    not _docker_ready(),
    reason="Docker/pinned amd64 image unavailable",
)
def test_real_gateway_allows_only_exact_pinned_public_destination_when_reachable() -> (
    None
):
    try:
        probe = socket.create_connection((PUBLIC_IP, 443), timeout=2)
    except OSError:
        pytest.skip("public egress probe is unavailable from this host")
    else:
        probe.close()

    execution_id = f"phase4allow{uuid.uuid4().hex[:12]}"
    grant = _project_grant()
    controller = DockerEgressController(gateway_image=BASE_IMAGE)
    binding = controller.prepare(
        execution_id=execution_id,
        grant=grant,
        authority=_authority(),
    )
    backend = _backend(grant=grant, controller=controller, binding=binding)
    plan = _plan(execution_id)
    handle = None
    try:
        handle = backend.ensure(plan)
        response = _connect_proxy(
            handle.realization_id,
            binding.gateway_ip or "",
            PUBLIC_IP,
            443,
        )
        assert response.returncode == 0
        assert " 200 " in response.stdout
        time.sleep(0.2)
        records = controller.audit(binding)
        assert any(
            record.decision == "allow"
            and record.reason == "connected"
            and record.host == PUBLIC_IP
            and record.port == 443
            for record in records
        )
    finally:
        if handle is not None:
            backend.cleanup_plan(plan, handle=handle)
        controller.cleanup(binding)


@pytest.mark.skipif(
    not _docker_ready(),
    reason="Docker/pinned amd64 image unavailable",
)
def test_explicitly_approved_internet_requires_and_uses_separate_approval() -> None:
    try:
        probe = socket.create_connection((PUBLIC_IP, 443), timeout=2)
    except OSError:
        pytest.skip("public egress probe is unavailable from this host")
    else:
        probe.close()

    execution_id = f"phase4internet{uuid.uuid4().hex[:12]}"
    grant = _approved_internet_grant()
    authority = _approved_internet_authority()
    controller = DockerEgressController(gateway_image=BASE_IMAGE)
    binding = controller.prepare(
        execution_id=execution_id,
        grant=grant,
        authority=authority,
    )
    backend = _backend(
        grant=grant,
        controller=controller,
        binding=binding,
        authority=authority,
    )
    plan = _plan(execution_id)
    handle = None
    try:
        handle = backend.ensure(plan)
        response = _connect_proxy(
            handle.realization_id,
            binding.gateway_ip or "",
            PUBLIC_IP,
            443,
        )
        assert response.returncode == 0
        assert " 200 " in response.stdout
        inspected = json.loads(
            _run(["docker", "inspect", handle.realization_id]).stdout
        )[0]
        labels = inspected["Config"]["Labels"]
        assert labels["dev.hermes.fleet.network_mode"] == NETWORK_EXPLICIT_INTERNET
        assert labels["dev.hermes.fleet.network_authority"] == AUTHORITY
        assert grant.approval_ref == APPROVAL
    finally:
        if handle is not None:
            backend.cleanup_plan(plan, handle=handle)
        controller.cleanup(binding)


@pytest.mark.skipif(not _docker_ready(), reason="Docker/pinned amd64 image unavailable")
def test_real_gateway_runtime_dns_rebinding_is_rejected() -> None:
    # The controller is given an authorization-time resolver that pins
    # example.com to PUBLIC_IP. The gateway resolves independently at runtime.
    # Any different answer set must be denied rather than silently adopted.
    def pinned_resolver(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
        ]

    execution_id = f"phase4dns{uuid.uuid4().hex[:12]}"
    grant = NetworkGrant(
        mode=NETWORK_PROJECT_ALLOWLIST,
        authority_ref=AUTHORITY,
        destinations=(
            NetworkDestination(
                host="example.com",
                resolved_ips=(PUBLIC_IP,),
                ports=(443,),
            ),
        ),
    )
    controller = DockerEgressController(
        gateway_image=BASE_IMAGE,
        resolver=pinned_resolver,
    )
    binding = controller.prepare(
        execution_id=execution_id,
        grant=grant,
        authority=_authority(),
    )
    backend = _backend(grant=grant, controller=controller, binding=binding)
    plan = _plan(execution_id)
    handle = None
    try:
        handle = backend.ensure(plan)
        response = _connect_proxy(
            handle.realization_id,
            binding.gateway_ip or "",
            "example.com",
            443,
        )
        assert " 403 " in response.stdout
        time.sleep(0.2)
        records = controller.audit(binding)
        matching = [record for record in records if record.host == "example.com"]
        if not matching:
            pytest.skip(
                "gateway DNS resolution is unavailable in this Docker environment"
            )
        assert matching[-1].decision == "deny"
        assert matching[-1].reason in {
            "dns_rebinding_or_drift",
            "dns_failed",
        }
    finally:
        if handle is not None:
            backend.cleanup_plan(plan, handle=handle)
        controller.cleanup(binding)
