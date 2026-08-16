from __future__ import annotations

import socket
import subprocess

import pytest

from hermes_fleet.network_isolation import (
    NETWORK_EXPLICIT_INTERNET,
    NETWORK_NONE,
    NETWORK_PROJECT_ALLOWLIST,
    NETWORK_PROVIDER_ONLY,
    DockerEgressController,
    NetworkAuthorityScope,
    NetworkDestination,
    NetworkGrant,
    NetworkIsolationError,
    evaluate_runtime_destination,
    pin_network_destination,
    public_egress_ipv4,
)


def _ipv4(*octets: int) -> str:
    return ".".join(str(octet) for octet in octets)


AUTHORITY = "sha256:" + "1" * 64
APPROVAL = "sha256:" + "2" * 64
PUBLIC_IP = _ipv4(1, 1, 1, 1)
OTHER_PUBLIC_IP = _ipv4(8, 8, 8, 8)
IMAGE = (
    "debian@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258"
)


def destination(
    host: str = "one.one.one.one",
    ips: tuple[str, ...] = (PUBLIC_IP,),
    ports: tuple[int, ...] = (443,),
) -> NetworkDestination:
    return NetworkDestination(host=host, resolved_ips=ips, ports=ports)


def grant(
    mode: str = NETWORK_PROJECT_ALLOWLIST,
    *,
    destinations: tuple[NetworkDestination, ...] | None = None,
    authority_ref: str = AUTHORITY,
    approval_ref: str | None = None,
) -> NetworkGrant:
    if destinations is None:
        destinations = (
            ()
            if mode in {NETWORK_NONE, NETWORK_PROVIDER_ONLY}
            else (destination(),)
        )
    if mode == NETWORK_EXPLICIT_INTERNET and approval_ref is None:
        approval_ref = APPROVAL
    return NetworkGrant(
        mode=mode,
        authority_ref=authority_ref,
        destinations=destinations,
        approval_ref=approval_ref,
    )


def test_public_ip_classifier_blocks_lan_tailscale_metadata_and_special_ranges(
) -> None:
    assert public_egress_ipv4(PUBLIC_IP) is True
    for value in (
        _ipv4(127, 0, 0, 1),
        _ipv4(10, 0, 0, 1),
        _ipv4(172, 16, 0, 1),
        _ipv4(192, 168, 1, 1),
        _ipv4(100, 64, 0, 1),
        _ipv4(100, 100, 100, 100),
        _ipv4(169, 254, 169, 254),
        _ipv4(0, 0, 0, 0),
        _ipv4(224, 0, 0, 1),
        _ipv4(203, 0, 113, 1),
        ":".join(("", "", "1")),
        "fc00" + "::" + "1",
    ):
        assert public_egress_ipv4(value) is False


def test_network_destination_is_exact_public_ipv4_and_port_allowlist() -> None:
    value = destination(
        host="Example.COM.",
        ips=(OTHER_PUBLIC_IP, PUBLIC_IP),
        ports=(443, 80),
    )
    assert value.host == "example.com"
    assert value.resolved_ips == (PUBLIC_IP, OTHER_PUBLIC_IP)
    assert value.ports == (80, 443)

    for ip in (
        _ipv4(127, 0, 0, 1),
        _ipv4(10, 0, 0, 1),
        _ipv4(100, 64, 0, 1),
        _ipv4(169, 254, 169, 254),
    ):
        with pytest.raises(NetworkIsolationError, match="non-public"):
            destination(ips=(ip,))

    for port in (53, 853, 2375, 2376, 2377):
        with pytest.raises(NetworkIsolationError, match="forbidden port"):
            destination(ports=(port,))


def test_literal_destination_must_pin_only_itself() -> None:
    literal = destination(host=PUBLIC_IP, ips=(PUBLIC_IP,))
    assert literal.host == PUBLIC_IP
    with pytest.raises(NetworkIsolationError, match="exactly its own"):
        destination(host=PUBLIC_IP, ips=(OTHER_PUBLIC_IP,))


def test_pin_destination_resolves_before_authorization_and_rejects_mixed_private(
) -> None:
    def resolver(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (OTHER_PUBLIC_IP, 0)),
        ]

    pinned = pin_network_destination("Example.COM", (443,), resolver=resolver)
    assert pinned.host == "example.com"
    assert pinned.resolved_ips == (PUBLIC_IP, OTHER_PUBLIC_IP)

    def unsafe(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0)),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (_ipv4(10, 0, 0, 1), 0),
            ),
        ]

    with pytest.raises(NetworkIsolationError, match="exclusively to public"):
        pin_network_destination("example.com", (443,), resolver=unsafe)


def test_four_network_modes_are_explicit_and_default_modes_carry_no_direct_egress(
) -> None:
    none = grant(NETWORK_NONE)
    provider = grant(NETWORK_PROVIDER_ONLY)
    project = grant(NETWORK_PROJECT_ALLOWLIST)
    internet = grant(NETWORK_EXPLICIT_INTERNET)

    assert none.direct_egress is False
    assert provider.direct_egress is False
    assert project.direct_egress is True
    assert internet.direct_egress is True

    with pytest.raises(NetworkIsolationError, match="may not carry direct egress"):
        grant(NETWORK_NONE, destinations=(destination(),))
    with pytest.raises(NetworkIsolationError, match="exact destinations"):
        grant(NETWORK_PROJECT_ALLOWLIST, destinations=())
    with pytest.raises(NetworkIsolationError, match="approval"):
        NetworkGrant(
            mode=NETWORK_EXPLICIT_INTERNET,
            authority_ref=AUTHORITY,
            destinations=(destination(),),
        )


def test_network_authority_scope_never_broadens_and_internet_needs_separate_approval(
) -> None:
    scope = NetworkAuthorityScope(
        run_authority_hash=AUTHORITY,
        approved_internet_hashes=(APPROVAL,),
    )
    assert scope.permits(grant(NETWORK_NONE)) is True
    assert scope.permits(grant(NETWORK_PROVIDER_ONLY)) is True
    assert scope.permits(grant(NETWORK_PROJECT_ALLOWLIST)) is True
    assert scope.permits(grant(NETWORK_EXPLICIT_INTERNET)) is True

    wrong = grant(
        NETWORK_PROJECT_ALLOWLIST,
        authority_ref="sha256:" + "3" * 64,
    )
    assert scope.permits(wrong) is False

    unapproved = NetworkGrant(
        mode=NETWORK_EXPLICIT_INTERNET,
        authority_ref=AUTHORITY,
        destinations=(destination(),),
        approval_ref="sha256:" + "4" * 64,
    )
    assert scope.permits(unapproved) is False

    with pytest.raises(NetworkIsolationError, match="separate from RunAuthority"):
        NetworkAuthorityScope(
            run_authority_hash=AUTHORITY,
            approved_internet_hashes=(AUTHORITY,),
        )


def test_network_policy_hash_is_stable_and_authority_bound() -> None:
    one = grant()
    reordered = NetworkGrant(
        mode=NETWORK_PROJECT_ALLOWLIST,
        authority_ref=AUTHORITY,
        destinations=(
            NetworkDestination(
                host="one.one.one.one",
                resolved_ips=(PUBLIC_IP,),
                ports=(443,),
            ),
        ),
    )
    assert one.policy_hash == reordered.policy_hash
    changed = grant(authority_ref="sha256:" + "3" * 64)
    assert changed.policy_hash != one.policy_hash


def test_runtime_oracle_detects_rebinding_private_resolution_and_scope_changes(
) -> None:
    value = grant()
    allowed = evaluate_runtime_destination(
        value,
        host="one.one.one.one",
        port=443,
        resolved_ips=(PUBLIC_IP,),
    )
    assert allowed.allowed is True

    rebound = evaluate_runtime_destination(
        value,
        host="one.one.one.one",
        port=443,
        resolved_ips=(OTHER_PUBLIC_IP,),
    )
    assert rebound.allowed is False
    assert rebound.reason == "dns_rebinding_or_drift"

    private = evaluate_runtime_destination(
        value,
        host="one.one.one.one",
        port=443,
        resolved_ips=(_ipv4(100, 64, 0, 1),),
    )
    assert private.allowed is False
    assert private.reason == "non_public_resolution"

    wrong_host = evaluate_runtime_destination(
        value,
        host="dns.google",
        port=443,
        resolved_ips=(OTHER_PUBLIC_IP,),
    )
    assert wrong_host.allowed is False
    assert wrong_host.reason == "destination_not_allowlisted"

    wrong_port = evaluate_runtime_destination(
        value,
        host="one.one.one.one",
        port=80,
        resolved_ips=(PUBLIC_IP,),
    )
    assert wrong_port.allowed is False
    assert wrong_port.reason == "port_not_allowlisted"

    docker_port = evaluate_runtime_destination(
        value,
        host="one.one.one.one",
        port=2375,
        resolved_ips=(PUBLIC_IP,),
    )
    assert docker_port.allowed is False
    assert docker_port.reason == "forbidden_port"


def test_none_and_provider_only_controller_paths_never_touch_docker() -> None:
    calls: list[list[str]] = []

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        raise AssertionError("offline/provider-only network modes must not call Docker")

    controller = DockerEgressController(gateway_image=IMAGE, command=command)
    authority = NetworkAuthorityScope(run_authority_hash=AUTHORITY)
    for mode in (NETWORK_NONE, NETWORK_PROVIDER_ONLY):
        binding = controller.prepare(
            execution_id=f"execution-{mode}",
            grant=grant(mode),
            authority=authority,
        )
        assert binding.docker_network == "none"
        assert binding.gateway_container_id is None
        assert binding.gateway_ip is None
        assert controller.audit(binding) == ()
        controller.cleanup(binding)
    assert calls == []


def test_direct_controller_revalidates_pinned_dns_before_any_docker_mutation() -> None:
    calls: list[list[str]] = []

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        raise AssertionError("DNS mismatch must fail before Docker")

    def resolver(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (OTHER_PUBLIC_IP, 0)),
        ]

    controller = DockerEgressController(
        gateway_image=IMAGE,
        command=command,
        resolver=resolver,
    )
    with pytest.raises(NetworkIsolationError, match="DNS binding changed"):
        controller.prepare(
            execution_id="execution-1",
            grant=grant(),
            authority=NetworkAuthorityScope(run_authority_hash=AUTHORITY),
        )
    assert calls == []


def test_direct_controller_fails_before_docker_when_authority_is_not_proven() -> None:
    calls: list[list[str]] = []

    def command(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")

    controller = DockerEgressController(gateway_image=IMAGE, command=command)
    with pytest.raises(NetworkIsolationError, match="outside verified RunAuthority"):
        controller.prepare(
            execution_id="execution-1",
            grant=grant(),
            authority=NetworkAuthorityScope(
                run_authority_hash="sha256:" + "3" * 64
            ),
        )
    assert calls == []
