"""Fail-closed network authority and Docker egress gateway for vNext workshops.

Phase 4 keeps model-provider traffic outside the workshop whenever possible.
Direct workshop egress is available only through a Fleet-owned gateway on a
Docker ``--internal`` network. The workshop has no default external route and
cannot bypass the gateway by ignoring proxy environment
variables.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .execution_backend import (
    ExecutionBackendError,
    ExecutionBackendErrorCode,
    ExecutionPlan,
)
from .oci_backend import DockerWorkshopBackend

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_RE = re.compile(
    r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9./_-]{0,254}@sha256:[0-9a-f]{64})$"
)
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_EXECUTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_NETWORK_NAME_RE = re.compile(r"^hermes-fleet-egress-[0-9a-f]{24}$")
_MODE_RE = re.compile(
    r"^(?:none|provider-only|project-allowlist|explicitly-approved-internet)$"
)

NETWORK_NONE = "none"
NETWORK_PROVIDER_ONLY = "provider-only"
NETWORK_PROJECT_ALLOWLIST = "project-allowlist"
NETWORK_EXPLICIT_INTERNET = "explicitly-approved-internet"
_DIRECT_MODES = frozenset({NETWORK_PROJECT_ALLOWLIST, NETWORK_EXPLICIT_INTERNET})

_GATEWAY_UID = 65534
_GATEWAY_GID = 65534
_GATEWAY_PORT = 8080
_GATEWAY_MEMORY_BYTES = 64 * 1024 * 1024
_GATEWAY_PIDS = 32
_GATEWAY_CPU_MILLIS = 100
_GATEWAY_RUNTIME_BYTES = 1024 * 1024
_MAX_DESTINATIONS = 32
_MAX_IPS_PER_DESTINATION = 16
_MAX_PORTS_PER_DESTINATION = 16
_MAX_AUDIT_BYTES = 1024 * 1024
_MAX_CLI_OUTPUT = 2 * 1024 * 1024

# Direct DNS and ordinary remote Docker daemon ports are categorically blocked.
# HTTPS on 443 still requires an exact destination allowlist and, for the broad
# internet mode, a separate explicit approval hash.
_FORBIDDEN_EGRESS_PORTS = frozenset({53, 853, 2375, 2376, 2377})
_GATEWAY_SECRET_ENV_NAME_RE = re.compile(
    r"(?i)(?:token|secret|password|credential|api[_-]?key|private[_-]?key|"
    r"access[_-]?key|cookie|jwt)"
)
_GATEWAY_FORBIDDEN_ENV_PREFIXES = (
    "DOCKER_",
    "HERMES_",
    "KERYX_",
    "NODESCALE_",
    "SSH_",
)
_WORKSHOP_PROXY_ENV_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
    }
)


class NetworkIsolationError(RuntimeError):
    """Network authority or enforcement cannot be proven safe."""


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise NetworkIsolationError(f"{label} is invalid")
    return value


def _mode(value: object) -> str:
    if type(value) is not str or _MODE_RE.fullmatch(value) is None:
        raise NetworkIsolationError("network mode is unsupported")
    return value


def _host(value: object) -> str:
    if type(value) is not str:
        raise NetworkIsolationError("network hostname is invalid")
    normalized = value.lower().rstrip(".")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None:
        if literal.version != 4 or not public_egress_ipv4(str(literal)):
            raise NetworkIsolationError("network hostname is not a public IPv4 address")
        return str(literal)
    if (
        _HOST_RE.fullmatch(normalized) is None
        or normalized in {"localhost", "localhost.localdomain"}
        or normalized.endswith((".localhost", ".local", ".internal", ".lan"))
    ):
        raise NetworkIsolationError("network hostname is invalid")
    return normalized


def _ports(values: object) -> tuple[int, ...]:
    if (
        type(values) not in {tuple, list}
        or not 0 < len(values) <= _MAX_PORTS_PER_DESTINATION
    ):
        raise NetworkIsolationError("network port allowlist is invalid")
    normalized: list[int] = []
    for value in values:
        if (
            isinstance(value, bool)
            or type(value) is not int
            or not 1 <= value <= 65535
            or value in _FORBIDDEN_EGRESS_PORTS
        ):
            raise NetworkIsolationError(
                "network port allowlist contains a forbidden port"
            )
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise NetworkIsolationError("network port allowlist contains duplicates")
    return tuple(sorted(normalized))


def public_egress_ipv4(value: str) -> bool:
    """Return whether ``value`` is an ordinary public IPv4 destination.

    Python's ``is_global`` rejects loopback, private, link-local, CGNAT/Tailscale
    shared space, documentation, multicast, unspecified, metadata, and other
    special ranges. IPv6 is deliberately unsupported in this first direct-egress
    slice, so the workshop cannot gain an unverified second address family.
    """

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.version == 4
        and bool(address.is_global)
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


@dataclass(frozen=True, slots=True)
class NetworkDestination:
    """One hostname/IP bound to exact public IPv4 answers and permitted ports."""

    host: str
    resolved_ips: tuple[str, ...]
    ports: tuple[int, ...]

    def __post_init__(self) -> None:
        host = _host(self.host)
        object.__setattr__(self, "host", host)
        if (
            type(self.resolved_ips) not in {tuple, list}
            or not 0 < len(self.resolved_ips) <= _MAX_IPS_PER_DESTINATION
        ):
            raise NetworkIsolationError("network IP allowlist is invalid")
        ips: list[str] = []
        for value in self.resolved_ips:
            if type(value) is not str or not public_egress_ipv4(value):
                raise NetworkIsolationError(
                    "network IP allowlist contains a non-public address"
                )
            ips.append(str(ipaddress.ip_address(value)))
        if len(ips) != len(set(ips)):
            raise NetworkIsolationError("network IP allowlist contains duplicates")
        ips = sorted(ips, key=lambda item: int(ipaddress.ip_address(item)))
        if _literal_ipv4(host) is not None and tuple(ips) != (host,):
            raise NetworkIsolationError(
                "literal network destination must pin exactly its own IPv4 address"
            )
        object.__setattr__(self, "resolved_ips", tuple(ips))
        object.__setattr__(self, "ports", _ports(self.ports))

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "resolved_ips": list(self.resolved_ips),
            "ports": list(self.ports),
        }


def pin_network_destination(
    host: str,
    ports: tuple[int, ...] | list[int],
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> NetworkDestination:
    """Resolve one domain before authorization and pin all public IPv4 answers."""

    normalized = _host(host)
    literal = _literal_ipv4(normalized)
    if literal is not None:
        return NetworkDestination(normalized, (literal,), tuple(ports))
    try:
        answers = resolver(
            normalized,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise NetworkIsolationError("network hostname resolution failed") from error
    ips = sorted(
        {
            str(ipaddress.ip_address(answer[4][0]))
            for answer in answers
            if (
                isinstance(answer, tuple)
                and len(answer) >= 5
                and isinstance(answer[4], tuple)
                and answer[4]
                and type(answer[4][0]) is str
            )
        },
        key=lambda item: int(ipaddress.ip_address(item)),
    )
    if not ips or any(not public_egress_ipv4(ip) for ip in ips):
        raise NetworkIsolationError(
            "network hostname did not resolve exclusively to public IPv4 addresses"
        )
    return NetworkDestination(normalized, tuple(ips), tuple(ports))


def _literal_ipv4(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.version != 4:
        return None
    return str(address)


@dataclass(frozen=True, slots=True)
class NetworkGrant:
    """Already-authorized network posture for one run."""

    mode: str
    authority_ref: str
    destinations: tuple[NetworkDestination, ...] = ()
    approval_ref: str | None = None

    def __post_init__(self) -> None:
        mode = _mode(self.mode)
        object.__setattr__(self, "mode", mode)
        _hash(self.authority_ref, "network RunAuthority hash")
        if (
            type(self.destinations) not in {tuple, list}
            or len(self.destinations) > _MAX_DESTINATIONS
            or any(type(item) is not NetworkDestination for item in self.destinations)
        ):
            raise NetworkIsolationError("network destination allowlist is invalid")
        destinations = tuple(self.destinations)
        keys = [(item.host, item.ports) for item in destinations]
        if len(keys) != len(set(keys)):
            raise NetworkIsolationError(
                "network destination allowlist contains duplicates"
            )
        object.__setattr__(
            self,
            "destinations",
            tuple(sorted(destinations, key=lambda item: (item.host, item.ports))),
        )

        if mode in {NETWORK_NONE, NETWORK_PROVIDER_ONLY}:
            if destinations or self.approval_ref is not None:
                raise NetworkIsolationError(
                    "offline/provider-only network posture may not carry direct egress"
                )
        elif mode == NETWORK_PROJECT_ALLOWLIST:
            if not destinations or self.approval_ref is not None:
                raise NetworkIsolationError(
                    "project allowlist requires exact destinations and "
                    "no ad-hoc approval"
                )
        elif mode == NETWORK_EXPLICIT_INTERNET:
            if not destinations:
                raise NetworkIsolationError(
                    "explicitly approved internet requires exact destinations"
                )
            _hash(self.approval_ref, "explicit internet approval hash")
            if self.approval_ref == self.authority_ref:
                raise NetworkIsolationError(
                    "explicit internet approval must be separate from RunAuthority"
                )

    @property
    def direct_egress(self) -> bool:
        return self.mode in _DIRECT_MODES

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "authority_ref": self.authority_ref,
            "destinations": [item.to_dict() for item in self.destinations],
            "approval_ref": self.approval_ref,
        }

    @property
    def policy_hash(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class NetworkAuthorityScope:
    """Verified Phase 4 slice projected from a future immutable RunAuthority."""

    run_authority_hash: str
    approved_internet_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _hash(self.run_authority_hash, "network RunAuthority hash")
        if (
            type(self.approved_internet_hashes) is not tuple
            or len(self.approved_internet_hashes) > 32
            or len(set(self.approved_internet_hashes))
            != len(self.approved_internet_hashes)
        ):
            raise NetworkIsolationError("network approval set is invalid")
        for value in self.approved_internet_hashes:
            _hash(value, "explicit internet approval hash")
            if value == self.run_authority_hash:
                raise NetworkIsolationError(
                    "network approval must be separate from RunAuthority"
                )

    def permits(self, grant: NetworkGrant) -> bool:
        if (
            type(grant) is not NetworkGrant
            or grant.authority_ref != self.run_authority_hash
        ):
            return False
        if grant.mode == NETWORK_EXPLICIT_INTERNET:
            return grant.approval_ref in self.approved_internet_hashes
        return grant.approval_ref is None


@dataclass(frozen=True, slots=True)
class RuntimeNetworkDecision:
    allowed: bool
    reason: str
    host: str
    port: int
    resolved_ips: tuple[str, ...]


def evaluate_runtime_destination(
    grant: NetworkGrant,
    *,
    host: str,
    port: int,
    resolved_ips: Iterable[str],
) -> RuntimeNetworkDecision:
    """Pure oracle mirrored by the gateway; used for admission/tests/audit review."""

    if type(grant) is not NetworkGrant or not grant.direct_egress:
        return RuntimeNetworkDecision(
            False, "direct_egress_disabled", str(host), port, ()
        )
    try:
        normalized_host = _host(host)
    except NetworkIsolationError:
        return RuntimeNetworkDecision(False, "invalid_host", str(host), port, ())
    if isinstance(port, bool) or type(port) is not int or not 1 <= port <= 65535:
        return RuntimeNetworkDecision(False, "invalid_port", normalized_host, 0, ())
    if port in _FORBIDDEN_EGRESS_PORTS:
        return RuntimeNetworkDecision(
            False, "forbidden_port", normalized_host, port, ()
        )
    runtime: list[str] = []
    try:
        for value in resolved_ips:
            if type(value) is not str or not public_egress_ipv4(value):
                return RuntimeNetworkDecision(
                    False,
                    "non_public_resolution",
                    normalized_host,
                    port,
                    (),
                )
            runtime.append(str(ipaddress.ip_address(value)))
    except TypeError:
        return RuntimeNetworkDecision(
            False, "invalid_resolution", normalized_host, port, ()
        )
    runtime_tuple = tuple(
        sorted(set(runtime), key=lambda item: int(ipaddress.ip_address(item)))
    )
    if not runtime_tuple:
        return RuntimeNetworkDecision(False, "dns_failed", normalized_host, port, ())

    literal = _literal_ipv4(normalized_host)
    candidates = [
        item
        for item in grant.destinations
        if item.host == normalized_host
        or (literal is not None and literal in item.resolved_ips)
    ]
    if len(candidates) != 1:
        return RuntimeNetworkDecision(
            False,
            "destination_not_allowlisted",
            normalized_host,
            port,
            runtime_tuple,
        )
    target = candidates[0]
    if port not in target.ports:
        return RuntimeNetworkDecision(
            False,
            "port_not_allowlisted",
            normalized_host,
            port,
            runtime_tuple,
        )
    if set(runtime_tuple) != set(target.resolved_ips):
        return RuntimeNetworkDecision(
            False,
            "dns_rebinding_or_drift",
            normalized_host,
            port,
            runtime_tuple,
        )
    return RuntimeNetworkDecision(True, "allowed", normalized_host, port, runtime_tuple)


@dataclass(frozen=True, slots=True)
class EgressBinding:
    execution_id: str
    mode: str
    authority_hash: str
    policy_hash: str
    docker_network: str
    gateway_container_id: str | None
    gateway_ip: str | None
    gateway_port: int = _GATEWAY_PORT

    def __post_init__(self) -> None:
        if type(self.execution_id) is not str or not self.execution_id:
            raise NetworkIsolationError("egress execution identity is invalid")
        _mode(self.mode)
        _hash(self.authority_hash, "egress authority hash")
        _hash(self.policy_hash, "egress policy hash")
        if self.mode in _DIRECT_MODES:
            if (
                _NETWORK_NAME_RE.fullmatch(self.docker_network) is None
                or type(self.gateway_container_id) is not str
                or _CONTAINER_ID_RE.fullmatch(self.gateway_container_id) is None
                or type(self.gateway_ip) is not str
                or not _private_bridge_ipv4(self.gateway_ip)
                or self.gateway_port != _GATEWAY_PORT
            ):
                raise NetworkIsolationError("direct egress binding is invalid")
        elif (
            self.docker_network != "none"
            or self.gateway_container_id is not None
            or self.gateway_ip is not None
        ):
            raise NetworkIsolationError("offline egress binding is invalid")

    @property
    def direct_egress(self) -> bool:
        return self.mode in _DIRECT_MODES


@dataclass(frozen=True, slots=True)
class NetworkAuditRecord:
    timestamp: int
    decision: str
    reason: str
    host: str
    port: int
    resolved_ips: tuple[str, ...]


class DockerEgressController:
    """Create/verify the Fleet gateway and its internal-only workshop network."""

    def __init__(
        self,
        *,
        gateway_image: str,
        command: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    ) -> None:
        if type(gateway_image) is not str or _IMAGE_RE.fullmatch(gateway_image) is None:
            raise NetworkIsolationError("egress gateway image must be digest pinned")
        if not callable(resolver):
            raise NetworkIsolationError("network resolver must be callable")
        self._gateway_image = gateway_image
        self._command = command or _run_cli
        self._resolver = resolver

    def prepare(
        self,
        *,
        execution_id: str,
        grant: NetworkGrant,
        authority: NetworkAuthorityScope,
    ) -> EgressBinding:
        if type(authority) is not NetworkAuthorityScope or not authority.permits(grant):
            raise NetworkIsolationError(
                "network grant is outside verified RunAuthority"
            )
        if (
            type(execution_id) is not str
            or _EXECUTION_ID_RE.fullmatch(execution_id) is None
        ):
            raise NetworkIsolationError("network execution identity is invalid")
        if not grant.direct_egress:
            return EgressBinding(
                execution_id=execution_id,
                mode=grant.mode,
                authority_hash=grant.authority_ref,
                policy_hash=grant.policy_hash,
                docker_network="none",
                gateway_container_id=None,
                gateway_ip=None,
            )

        self._verify_pinned_destinations(grant)
        self._verify_gateway_image()
        network_name, gateway_name = _egress_names(execution_id, grant.policy_hash)
        recovered_gateway = self._container_inspect_optional(gateway_name)
        recovered_gateway_id: str | None = None
        if recovered_gateway is not None:
            candidate_id = recovered_gateway.get("Id")
            if (
                type(candidate_id) is not str
                or _CONTAINER_ID_RE.fullmatch(candidate_id) is None
            ):
                raise NetworkIsolationError(
                    "recovered egress gateway identity is invalid"
                )
            recovered_gateway_id = candidate_id
        self._ensure_network(
            network_name,
            execution_id=execution_id,
            grant=grant,
            gateway_container_id=recovered_gateway_id,
        )
        gateway = self._ensure_gateway(
            gateway_name,
            network_name=network_name,
            execution_id=execution_id,
            grant=grant,
        )
        container_id = gateway.get("Id")
        gateway_ip = _container_network_ip(gateway, network_name)
        if (
            type(container_id) is not str
            or _CONTAINER_ID_RE.fullmatch(container_id) is None
        ):
            raise NetworkIsolationError("egress gateway identity is invalid")
        binding = EgressBinding(
            execution_id=execution_id,
            mode=grant.mode,
            authority_hash=grant.authority_ref,
            policy_hash=grant.policy_hash,
            docker_network=network_name,
            gateway_container_id=container_id,
            gateway_ip=gateway_ip,
        )
        self.verify(binding, grant=grant, expected_workshop_id=None)
        self._wait_gateway_ready(binding)
        return binding

    def verify(
        self,
        binding: EgressBinding,
        *,
        grant: NetworkGrant,
        expected_workshop_id: str | None,
    ) -> None:
        if (
            type(binding) is not EgressBinding
            or type(grant) is not NetworkGrant
            or binding.mode != grant.mode
            or binding.policy_hash != grant.policy_hash
            or binding.authority_hash != grant.authority_ref
        ):
            raise NetworkIsolationError("egress binding does not match network grant")
        if not binding.direct_egress:
            return
        network = self._network_inspect_optional(binding.docker_network)
        if network is None:
            raise NetworkIsolationError("Fleet egress network is unavailable")
        self._verify_network_document(
            network,
            network_name=binding.docker_network,
            execution_id=binding.execution_id,
            grant=grant,
            gateway_container_id=binding.gateway_container_id,
            expected_workshop_id=expected_workshop_id,
        )
        gateway = self._container_inspect_optional(binding.gateway_container_id or "")
        if gateway is None:
            raise NetworkIsolationError("Fleet egress gateway is unavailable")
        self._verify_gateway_document(
            gateway,
            network_name=binding.docker_network,
            gateway_ip=binding.gateway_ip or "",
            execution_id=binding.execution_id,
            grant=grant,
        )

    def audit(self, binding: EgressBinding) -> tuple[NetworkAuditRecord, ...]:
        if type(binding) is not EgressBinding or not binding.direct_egress:
            return ()
        result = self._command(["docker", "logs", binding.gateway_container_id or ""])
        if result.returncode != 0:
            raise NetworkIsolationError("Fleet egress audit log is unavailable")
        payload = result.stdout.encode("utf-8", errors="replace")
        if len(payload) > _MAX_AUDIT_BYTES:
            raise NetworkIsolationError("Fleet egress audit log exceeded its bound")
        records: list[NetworkAuditRecord] = []
        for raw in result.stdout.splitlines():
            if not raw.startswith("FLEET_EGRESS_V1\t"):
                continue
            fields = raw.split("\t")
            if len(fields) != 8:
                raise NetworkIsolationError("Fleet egress audit record is malformed")
            _, timestamp, decision, reason, host, port, ips, policy_hash = fields
            if policy_hash != binding.policy_hash:
                raise NetworkIsolationError(
                    "Fleet egress audit policy identity changed"
                )
            try:
                timestamp_value = int(timestamp)
                port_value = int(port)
            except ValueError as error:
                raise NetworkIsolationError(
                    "Fleet egress audit record is malformed"
                ) from error
            if decision not in {"allow", "deny"}:
                raise NetworkIsolationError("Fleet egress audit decision is invalid")
            resolved = tuple(item for item in ips.split(",") if item)
            if any(not public_egress_ipv4(item) for item in resolved):
                raise NetworkIsolationError(
                    "Fleet egress audit contains unsafe IP data"
                )
            records.append(
                NetworkAuditRecord(
                    timestamp=timestamp_value,
                    decision=decision,
                    reason=reason,
                    host=host,
                    port=port_value,
                    resolved_ips=resolved,
                )
            )
        return tuple(records)

    def cleanup(self, binding: EgressBinding) -> None:
        if type(binding) is not EgressBinding or not binding.direct_egress:
            return
        container_id = binding.gateway_container_id or ""
        existing = self._container_inspect_optional(container_id)
        if existing is not None:
            result = self._command(["docker", "rm", "--force", container_id])
            if (
                result.returncode != 0
                and self._container_inspect_optional(container_id) is not None
            ):
                raise NetworkIsolationError("Fleet egress gateway cleanup failed")
        network = self._network_inspect_optional(binding.docker_network)
        if network is not None:
            result = self._command(["docker", "network", "rm", binding.docker_network])
            if (
                result.returncode != 0
                and self._network_inspect_optional(binding.docker_network) is not None
            ):
                raise NetworkIsolationError("Fleet egress network cleanup failed")

    def _verify_pinned_destinations(self, grant: NetworkGrant) -> None:
        for destination in grant.destinations:
            literal = _literal_ipv4(destination.host)
            if literal is not None:
                continue
            try:
                answers = self._resolver(
                    destination.host,
                    None,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                )
            except OSError as error:
                raise NetworkIsolationError(
                    "network destination resolution failed before enforcement"
                ) from error
            runtime: set[str] = set()
            for answer in answers:
                if (
                    isinstance(answer, tuple)
                    and len(answer) >= 5
                    and isinstance(answer[4], tuple)
                    and answer[4]
                    and type(answer[4][0]) is str
                ):
                    value = str(ipaddress.ip_address(answer[4][0]))
                    if not public_egress_ipv4(value):
                        raise NetworkIsolationError(
                            "network destination resolved to non-public state"
                        )
                    runtime.add(value)
            if runtime != set(destination.resolved_ips):
                raise NetworkIsolationError(
                    "network destination DNS binding changed before enforcement"
                )

    def _verify_gateway_image(self) -> None:
        result = self._command(["docker", "image", "inspect", self._gateway_image])
        if result.returncode != 0:
            raise NetworkIsolationError(
                "digest-pinned egress gateway image is unavailable"
            )
        document = _one_json_document(result.stdout, "egress gateway image")
        digests = document.get("RepoDigests")
        matches = (
            document.get("Id") == self._gateway_image
            if self._gateway_image.startswith("sha256:")
            else isinstance(digests, list) and self._gateway_image in digests
        )
        if not matches:
            raise NetworkIsolationError("local egress gateway image digest changed")

    def _ensure_network(
        self,
        network_name: str,
        *,
        execution_id: str,
        grant: NetworkGrant,
        gateway_container_id: str | None,
    ) -> dict[str, object]:
        existing = self._network_inspect_optional(network_name)
        if existing is None:
            argv = [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
            ]
            for key, value in sorted(
                _network_labels(execution_id=execution_id, grant=grant).items()
            ):
                argv.extend(["--label", f"{key}={value}"])
            argv.append(network_name)
            created = self._command(argv)
            if created.returncode != 0:
                existing = self._network_inspect_optional(network_name)
                if existing is None:
                    raise NetworkIsolationError("Fleet egress network creation failed")
            else:
                existing = self._network_inspect_optional(network_name)
        if existing is None:
            raise NetworkIsolationError("Fleet egress network is not observable")
        self._verify_network_document(
            existing,
            network_name=network_name,
            execution_id=execution_id,
            grant=grant,
            gateway_container_id=gateway_container_id,
            expected_workshop_id=None,
        )
        return existing

    def _ensure_gateway(
        self,
        gateway_name: str,
        *,
        network_name: str,
        execution_id: str,
        grant: NetworkGrant,
    ) -> dict[str, object]:
        script = _PERL_GATEWAY_SCRIPT.encode("utf-8")
        policy = _gateway_policy(grant).encode("utf-8")
        script_b64 = base64.b64encode(script).decode("ascii")
        policy_b64 = base64.b64encode(policy).decode("ascii")
        script_hash = "sha256:" + hashlib.sha256(script).hexdigest()
        labels = _gateway_labels(
            execution_id=execution_id,
            grant=grant,
            script_hash=script_hash,
        )
        existing = self._container_inspect_optional(gateway_name)
        if existing is None:
            argv = [
                "docker",
                "create",
                "--name",
                gateway_name,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--network",
                network_name,
                "--pids-limit",
                str(_GATEWAY_PIDS),
                "--memory",
                str(_GATEWAY_MEMORY_BYTES),
                "--memory-swap",
                str(_GATEWAY_MEMORY_BYTES),
                "--cpus",
                f"{_GATEWAY_CPU_MILLIS / 1000:.3f}",
                "--user",
                f"{_GATEWAY_UID}:{_GATEWAY_GID}",
                "--workdir",
                "/run/fleet-gateway",
                "--tmpfs",
                (
                    "/run/fleet-gateway:rw,nosuid,nodev,noexec,"
                    f"size={_GATEWAY_RUNTIME_BYTES},uid={_GATEWAY_UID},"
                    f"gid={_GATEWAY_GID},mode=0700"
                ),
                "--env",
                f"FLEET_GATEWAY_POLICY_HASH={grant.policy_hash}",
                "--env",
                f"FLEET_GATEWAY_SCRIPT_B64={script_b64}",
                "--env",
                f"FLEET_GATEWAY_POLICY_B64={policy_b64}",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=1m",
                "--log-opt",
                "max-file=1",
                "--log-opt",
                "compress=false",
            ]
            for key, value in sorted(labels.items()):
                argv.extend(["--label", f"{key}={value}"])
            argv.extend(
                [
                    self._gateway_image,
                    "sh",
                    "-c",
                    _gateway_start_command(),
                ]
            )
            created = self._command(argv)
            if created.returncode != 0:
                existing = self._container_inspect_optional(gateway_name)
                if existing is None:
                    raise NetworkIsolationError("Fleet egress gateway creation failed")
            else:
                existing = self._container_inspect_optional(gateway_name)
        if existing is None:
            raise NetworkIsolationError("Fleet egress gateway is not observable")

        networks = _container_networks(existing)
        state = existing.get("State")
        status = state.get("Status") if isinstance(state, dict) else None

        if "bridge" in networks:
            if status != "running":
                raise NetworkIsolationError(
                    "Fleet egress gateway external network was attached before startup"
                )
            gateway_ip = _container_network_ip(existing, network_name)
            self._verify_gateway_document(
                existing,
                network_name=network_name,
                gateway_ip=gateway_ip,
                execution_id=execution_id,
                grant=grant,
                external_required=True,
            )
            self._verify_gateway_listener(
                gateway_name,
                gateway_ip=gateway_ip,
            )
            return existing

        if status == "created":
            self._verify_gateway_document(
                existing,
                network_name=network_name,
                gateway_ip=None,
                execution_id=execution_id,
                grant=grant,
                external_required=False,
            )
            started = self._command(["docker", "start", gateway_name])
            if started.returncode != 0:
                recovered = self._container_inspect_optional(gateway_name)
                recovered_state = (
                    recovered.get("State") if isinstance(recovered, dict) else None
                )
                if (
                    not isinstance(recovered_state, dict)
                    or recovered_state.get("Status") != "running"
                ):
                    raise NetworkIsolationError("Fleet egress gateway failed to start")
            existing = self._container_inspect_optional(gateway_name)
        elif status != "running":
            raise NetworkIsolationError(
                "Fleet egress gateway is not in a reusable state"
            )
        if existing is None:
            raise NetworkIsolationError("Fleet egress gateway disappeared")

        gateway_ip = _container_network_ip(existing, network_name)
        self._verify_gateway_document(
            existing,
            network_name=network_name,
            gateway_ip=gateway_ip,
            execution_id=execution_id,
            grant=grant,
            external_required=False,
        )
        self._wait_gateway_ready_endpoint(
            gateway_name,
            gateway_ip=gateway_ip,
        )
        self._verify_gateway_listener(
            gateway_name,
            gateway_ip=gateway_ip,
        )

        connected = self._command(
            ["docker", "network", "connect", "bridge", gateway_name]
        )
        if connected.returncode != 0:
            recovered = self._container_inspect_optional(gateway_name)
            if recovered is None or "bridge" not in _container_networks(recovered):
                raise NetworkIsolationError(
                    "Fleet egress gateway external attachment failed"
                )
        existing = self._container_inspect_optional(gateway_name)
        if existing is None:
            raise NetworkIsolationError("Fleet egress gateway disappeared")
        if _container_network_ip(existing, network_name) != gateway_ip:
            raise NetworkIsolationError(
                "Fleet egress gateway internal IP changed after external attachment"
            )
        self._verify_gateway_document(
            existing,
            network_name=network_name,
            gateway_ip=gateway_ip,
            execution_id=execution_id,
            grant=grant,
            external_required=True,
        )
        self._verify_gateway_listener(
            gateway_name,
            gateway_ip=gateway_ip,
        )
        return existing

    def _wait_gateway_ready(self, binding: EgressBinding) -> None:
        if not binding.direct_egress:
            return
        self._wait_gateway_ready_endpoint(
            binding.gateway_container_id or "",
            gateway_ip=binding.gateway_ip or "",
        )

    def _wait_gateway_ready_endpoint(
        self,
        container_id: str,
        *,
        gateway_ip: str,
    ) -> None:
        if not _private_bridge_ipv4(gateway_ip):
            raise NetworkIsolationError("Fleet egress gateway internal IP is invalid")
        probe = (
            "use IO::Socket::INET; "
            f"$s=IO::Socket::INET->new(PeerAddr=>'{gateway_ip}',"
            f"PeerPort=>{_GATEWAY_PORT},Proto=>'tcp',Timeout=>1); "
            "exit($s ? 0 : 1);"
        )
        for _attempt in range(20):
            result = self._command(
                ["docker", "exec", container_id, "perl", "-e", probe]
            )
            if result.returncode == 0:
                return
            time.sleep(0.05)
        raise NetworkIsolationError("Fleet egress gateway did not become ready")

    def _verify_gateway_listener(
        self,
        container_id: str,
        *,
        gateway_ip: str,
    ) -> None:
        expected_address = ipaddress.ip_address(gateway_ip)
        if expected_address.version != 4:
            raise NetworkIsolationError(
                "Fleet egress gateway listener address is invalid"
            )
        expected_hex = expected_address.packed[::-1].hex().upper()
        tcp = self._command(["docker", "exec", container_id, "cat", "/proc/net/tcp"])
        if tcp.returncode != 0:
            raise NetworkIsolationError(
                "Fleet egress gateway listener state is unavailable"
            )
        listeners: list[str] = []
        for raw in tcp.stdout.splitlines()[1:]:
            fields = raw.split()
            if len(fields) < 4 or fields[3].upper() != "0A":
                continue
            local = fields[1].upper()
            if local.endswith(f":{_GATEWAY_PORT:04X}"):
                listeners.append(local)
        if listeners != [f"{expected_hex}:{_GATEWAY_PORT:04X}"]:
            raise NetworkIsolationError(
                "Fleet egress gateway listener is not bound only to the internal IP"
            )
        tcp6 = self._command(["docker", "exec", container_id, "cat", "/proc/net/tcp6"])
        if tcp6.returncode != 0:
            raise NetworkIsolationError(
                "Fleet egress gateway IPv6 listener state is unavailable"
            )
        for raw in tcp6.stdout.splitlines()[1:]:
            fields = raw.split()
            if (
                len(fields) >= 4
                and fields[3].upper() == "0A"
                and fields[1].upper().endswith(f":{_GATEWAY_PORT:04X}")
            ):
                raise NetworkIsolationError(
                    "Fleet egress gateway unexpectedly listens on IPv6"
                )

    def _network_inspect_optional(self, name: str) -> dict[str, object] | None:
        result = self._command(["docker", "network", "inspect", name])
        if result.returncode != 0:
            if (
                "not found" in result.stderr.lower()
                or "no such network" in result.stderr.lower()
            ):
                return None
            raise NetworkIsolationError("Docker network inspection failed")
        return _one_json_document(result.stdout, "Docker network")

    def _container_inspect_optional(self, identity: str) -> dict[str, object] | None:
        result = self._command(["docker", "inspect", identity])
        if result.returncode != 0:
            text = result.stderr.lower()
            if "no such object" in text or "no such container" in text:
                return None
            raise NetworkIsolationError("Docker container inspection failed")
        return _one_json_document(result.stdout, "Docker container")

    def _verify_network_document(
        self,
        document: dict[str, object],
        *,
        network_name: str,
        execution_id: str,
        grant: NetworkGrant,
        gateway_container_id: str | None,
        expected_workshop_id: str | None,
    ) -> None:
        if (
            document.get("Name") != network_name
            or document.get("Driver") != "bridge"
            or document.get("Scope") != "local"
            or document.get("Internal") is not True
            or document.get("Attachable") is not False
            or document.get("Ingress") is not False
            or document.get("EnableIPv6") is not False
        ):
            raise NetworkIsolationError("Fleet egress network isolation changed")
        labels = document.get("Labels")
        expected = _network_labels(execution_id=execution_id, grant=grant)
        if not isinstance(labels, dict) or any(
            labels.get(key) != value for key, value in expected.items()
        ):
            raise NetworkIsolationError("Fleet egress network ownership changed")
        containers = document.get("Containers")
        if not isinstance(containers, dict):
            raise NetworkIsolationError(
                "Fleet egress network membership is unavailable"
            )
        allowed = {
            value
            for value in (gateway_container_id, expected_workshop_id)
            if value is not None
        }
        if not set(containers).issubset(allowed):
            raise NetworkIsolationError(
                "unexpected lateral peer joined Fleet egress network"
            )
        if gateway_container_id is not None and gateway_container_id not in containers:
            raise NetworkIsolationError(
                "Fleet egress gateway left the internal network"
            )
        if expected_workshop_id is not None and expected_workshop_id not in containers:
            raise NetworkIsolationError("Fleet workshop left the internal network")

    def _verify_gateway_document(
        self,
        document: dict[str, object],
        *,
        network_name: str,
        gateway_ip: str | None,
        execution_id: str,
        grant: NetworkGrant,
        external_required: bool = True,
    ) -> None:
        config = document.get("Config")
        host = document.get("HostConfig")
        state = document.get("State")
        if (
            not isinstance(config, dict)
            or not isinstance(host, dict)
            or not isinstance(state, dict)
        ):
            raise NetworkIsolationError("Fleet egress gateway inspection is incomplete")
        status = state.get("Status")
        expected_status = (
            "running" if external_required or gateway_ip is not None else "created"
        )
        if status != expected_status:
            raise NetworkIsolationError("Fleet egress gateway lifecycle state changed")
        script_hash = (
            "sha256:" + hashlib.sha256(_PERL_GATEWAY_SCRIPT.encode("utf-8")).hexdigest()
        )
        expected_labels = _gateway_labels(
            execution_id=execution_id,
            grant=grant,
            script_hash=script_hash,
        )
        labels = config.get("Labels")
        if not isinstance(labels, dict) or any(
            labels.get(key) != value for key, value in expected_labels.items()
        ):
            raise NetworkIsolationError("Fleet egress gateway ownership changed")
        if config.get("Image") != self._gateway_image:
            raise NetworkIsolationError("Fleet egress gateway image changed")
        if config.get("Cmd") != ["sh", "-c", _gateway_start_command()]:
            raise NetworkIsolationError("Fleet egress gateway startup command changed")
        if config.get("User") != f"{_GATEWAY_UID}:{_GATEWAY_GID}":
            raise NetworkIsolationError("Fleet egress gateway user changed")
        if config.get("WorkingDir") != "/run/fleet-gateway":
            raise NetworkIsolationError(
                "Fleet egress gateway working directory changed"
            )
        if host.get("NetworkMode") != network_name:
            raise NetworkIsolationError(
                "Fleet egress gateway internal attachment changed"
            )
        if (
            host.get("ReadonlyRootfs") is not True
            or host.get("Privileged") is not False
        ):
            raise NetworkIsolationError(
                "Fleet egress gateway privilege posture changed"
            )
        cap_drop = host.get("CapDrop")
        if not isinstance(cap_drop, list) or "ALL" not in {
            str(item).upper() for item in cap_drop
        }:
            raise NetworkIsolationError("Fleet egress gateway capabilities changed")
        if host.get("CapAdd") not in (None, []):
            raise NetworkIsolationError("Fleet egress gateway adds capabilities")
        security = host.get("SecurityOpt")
        if not isinstance(security, list) or not any(
            str(item).lower().startswith("no-new-privileges") for item in security
        ):
            raise NetworkIsolationError("Fleet egress gateway security option changed")
        if any("unconfined" in str(item).lower() for item in security):
            raise NetworkIsolationError("Fleet egress gateway confinement is disabled")
        if host.get("PidsLimit") != _GATEWAY_PIDS:
            raise NetworkIsolationError("Fleet egress gateway PID limit changed")
        if host.get("Memory") != _GATEWAY_MEMORY_BYTES:
            raise NetworkIsolationError("Fleet egress gateway memory limit changed")
        if host.get("MemorySwap") != _GATEWAY_MEMORY_BYTES:
            raise NetworkIsolationError("Fleet egress gateway swap limit changed")
        if host.get("NanoCpus") != _GATEWAY_CPU_MILLIS * 1_000_000:
            raise NetworkIsolationError("Fleet egress gateway CPU limit changed")
        restart_policy = host.get("RestartPolicy")
        if not isinstance(restart_policy, dict) or restart_policy.get("Name") not in {
            "",
            "no",
        }:
            raise NetworkIsolationError("Fleet egress gateway restart policy changed")
        if host.get("Binds") not in (None, []):
            raise NetworkIsolationError("Fleet egress gateway has host bind mounts")
        for key in ("Devices", "DeviceRequests"):
            if host.get(key) not in (None, []):
                raise NetworkIsolationError("Fleet egress gateway has host devices")
        if host.get("PortBindings") not in (None, {}):
            raise NetworkIsolationError("Fleet egress gateway publishes host ports")
        if host.get("PublishAllPorts") is not False:
            raise NetworkIsolationError("Fleet egress gateway publishes host ports")
        mounts = document.get("Mounts")
        if not isinstance(mounts, list):
            raise NetworkIsolationError(
                "Fleet egress gateway mount inspection is unavailable"
            )
        if mounts:
            raise NetworkIsolationError("Fleet egress gateway has persistent mounts")
        tmpfs = host.get("Tmpfs")
        runtime_options = (
            tmpfs.get("/run/fleet-gateway") if isinstance(tmpfs, dict) else None
        )
        if not isinstance(runtime_options, str):
            raise NetworkIsolationError("Fleet egress gateway tmpfs is missing")
        runtime_flags = {item.strip().lower() for item in runtime_options.split(",")}
        required_runtime = {
            "rw",
            "nosuid",
            "nodev",
            "noexec",
            f"size={_GATEWAY_RUNTIME_BYTES}",
            f"uid={_GATEWAY_UID}",
            f"gid={_GATEWAY_GID}",
            "mode=0700",
        }
        if (
            not required_runtime.issubset(runtime_flags)
            or "ro" in runtime_flags
            or "exec" in runtime_flags
        ):
            raise NetworkIsolationError("Fleet egress gateway tmpfs posture changed")
        log_config = host.get("LogConfig")
        if (
            not isinstance(log_config, dict)
            or log_config.get("Type") != "local"
            or log_config.get("Config")
            != {
                "compress": "false",
                "max-file": "1",
                "max-size": "1m",
            }
        ):
            raise NetworkIsolationError("Fleet egress gateway audit logging changed")
        networks = _container_networks(document)
        expected_networks = (
            {network_name, "bridge"} if external_required else {network_name}
        )
        if set(networks) != expected_networks:
            raise NetworkIsolationError("Fleet egress gateway network set changed")
        internal = networks.get(network_name)
        if not isinstance(internal, dict):
            raise NetworkIsolationError(
                "Fleet egress gateway internal attachment is unavailable"
            )
        if gateway_ip is not None and internal.get("IPAddress") != gateway_ip:
            raise NetworkIsolationError("Fleet egress gateway internal IP changed")
        environment = config.get("Env")
        if not isinstance(environment, list):
            raise NetworkIsolationError("Fleet egress gateway environment is invalid")
        observed_environment: dict[str, str] = {}
        for item in environment:
            if not isinstance(item, str) or "=" not in item:
                raise NetworkIsolationError(
                    "Fleet egress gateway environment is invalid"
                )
            name, value = item.split("=", 1)
            if not name or name in observed_environment:
                raise NetworkIsolationError(
                    "Fleet egress gateway environment is invalid"
                )
            observed_environment[name] = value
        expected_policy_b64 = base64.b64encode(_gateway_policy(grant).encode()).decode()
        expected_script_b64 = base64.b64encode(_PERL_GATEWAY_SCRIPT.encode()).decode()
        required_env = {
            "FLEET_GATEWAY_POLICY_HASH": grant.policy_hash,
            "FLEET_GATEWAY_SCRIPT_B64": expected_script_b64,
            "FLEET_GATEWAY_POLICY_B64": expected_policy_b64,
        }
        if any(
            observed_environment.get(key) != value
            for key, value in required_env.items()
        ):
            raise NetworkIsolationError("Fleet egress gateway policy material changed")
        for name in observed_environment:
            if name in required_env:
                continue
            if (
                name.startswith(_GATEWAY_FORBIDDEN_ENV_PREFIXES)
                or _GATEWAY_SECRET_ENV_NAME_RE.search(name) is not None
                or name in _WORKSHOP_PROXY_ENV_NAMES
            ):
                raise NetworkIsolationError(
                    "Fleet egress gateway environment contains forbidden authority"
                )


class NetworkIsolatedWorkshopBackend(DockerWorkshopBackend):
    """Workshop specialization that can only use a verified Phase 4 binding."""

    def __init__(
        self,
        *,
        network_grant: NetworkGrant,
        network_authority: NetworkAuthorityScope,
        egress_binding: EgressBinding,
        egress_controller: DockerEgressController | None = None,
        **kwargs: object,
    ) -> None:
        if (
            type(network_authority) is not NetworkAuthorityScope
            or type(network_grant) is not NetworkGrant
            or not network_authority.permits(network_grant)
            or type(egress_binding) is not EgressBinding
            or egress_binding.mode != network_grant.mode
            or egress_binding.policy_hash != network_grant.policy_hash
            or egress_binding.authority_hash != network_grant.authority_ref
        ):
            raise NetworkIsolationError("workshop network binding is unauthorized")
        if network_grant.direct_egress and egress_controller is None:
            raise NetworkIsolationError(
                "direct workshop egress requires Fleet enforcement"
            )
        self._network_grant = network_grant
        self._network_binding = egress_binding
        self._egress_controller = egress_controller
        super().__init__(**kwargs)
        if self._realization.network != "none":
            raise NetworkIsolationError(
                "Phase 4 overlays mediated egress onto an otherwise offline workshop"
            )

    def _prepare(self, plan: ExecutionPlan):
        if plan.execution_id != self._network_binding.execution_id:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.PLAN_CONFLICT,
                "workshop execution does not match its network binding",
            )
        return super()._prepare(plan)

    def _docker_network_name(self) -> str:
        return self._network_binding.docker_network

    def _egress_label(self) -> str:
        return "proxy" if self._network_binding.direct_egress else "off"

    def _network_labels(self) -> dict[str, str]:
        labels = {
            "dev.hermes.fleet.network_mode": self._network_grant.mode,
            "dev.hermes.fleet.network_policy": self._network_grant.policy_hash,
            "dev.hermes.fleet.network_authority": self._network_grant.authority_ref,
        }
        if self._network_binding.direct_egress:
            labels["dev.hermes.fleet.network_gateway"] = (
                self._network_binding.gateway_container_id or ""
            )
        return labels

    def _required_environment(self) -> set[str]:
        required = super()._required_environment()
        if not self._network_binding.direct_egress:
            return required
        proxy = (
            f"http://{self._network_binding.gateway_ip}:"
            f"{self._network_binding.gateway_port}"
        )
        return required | {
            f"HTTP_PROXY={proxy}",
            f"HTTPS_PROXY={proxy}",
            f"http_proxy={proxy}",
            f"https_proxy={proxy}",
            "NO_PROXY=",
            "no_proxy=",
        }

    def _additional_create_args(self, plan: ExecutionPlan) -> list[str]:
        args = super()._additional_create_args(plan)
        if not self._network_binding.direct_egress:
            return args
        proxy = (
            f"http://{self._network_binding.gateway_ip}:"
            f"{self._network_binding.gateway_port}"
        )
        args.extend(["--dns", "127.0.0.1"])
        for value in (
            f"HTTP_PROXY={proxy}",
            f"HTTPS_PROXY={proxy}",
            f"http_proxy={proxy}",
            f"https_proxy={proxy}",
            "NO_PROXY=",
            "no_proxy=",
        ):
            args.extend(["--env", value])
        return args

    def _validate_realization_security(self, document: dict[str, object]) -> None:
        super()._validate_realization_security(document)
        try:
            config = document.get("Config")
            host = document.get("HostConfig")
            if not isinstance(config, dict) or not isinstance(host, dict):
                raise NetworkIsolationError("workshop network inspection is incomplete")
            environment = config.get("Env")
            if not isinstance(environment, list):
                raise NetworkIsolationError(
                    "workshop network environment is unavailable"
                )
            observed_proxy: dict[str, str] = {}
            for item in environment:
                if not isinstance(item, str) or "=" not in item:
                    raise NetworkIsolationError(
                        "workshop network environment is invalid"
                    )
                name, value = item.split("=", 1)
                if name in _WORKSHOP_PROXY_ENV_NAMES:
                    if name in observed_proxy:
                        raise NetworkIsolationError(
                            "workshop proxy binding is ambiguous"
                        )
                    observed_proxy[name] = value
            if self._network_binding.direct_egress:
                dns = host.get("Dns")
                if dns != ["127.0.0.1"]:
                    raise NetworkIsolationError("workshop direct DNS is not disabled")
                proxy = (
                    f"http://{self._network_binding.gateway_ip}:"
                    f"{self._network_binding.gateway_port}"
                )
                expected_proxy = {
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                    "http_proxy": proxy,
                    "https_proxy": proxy,
                    "NO_PROXY": "",
                    "no_proxy": "",
                }
                if observed_proxy != expected_proxy:
                    raise NetworkIsolationError("workshop proxy binding changed")
                networks = _container_networks(document)
                if set(networks) != {self._network_binding.docker_network}:
                    raise NetworkIsolationError("workshop gained an unexpected network")
                if self._egress_controller is None:
                    raise NetworkIsolationError(
                        "workshop egress verifier is unavailable"
                    )
                workshop_id = document.get("Id")
                if (
                    type(workshop_id) is not str
                    or _CONTAINER_ID_RE.fullmatch(workshop_id) is None
                ):
                    raise NetworkIsolationError("workshop identity is invalid")
                state = document.get("State")
                status = state.get("Status") if isinstance(state, dict) else None
                expected_endpoint = workshop_id if status == "running" else None
                self._egress_controller.verify(
                    self._network_binding,
                    grant=self._network_grant,
                    expected_workshop_id=expected_endpoint,
                )
            else:
                if observed_proxy:
                    raise NetworkIsolationError(
                        "offline workshop gained proxy configuration"
                    )
                if host.get("NetworkMode") != "none":
                    raise NetworkIsolationError(
                        "offline workshop gained direct networking"
                    )
        except NetworkIsolationError as error:
            raise ExecutionBackendError(
                ExecutionBackendErrorCode.CAPABILITY_MISMATCH,
                f"Docker workshop network authority changed: {error}",
            ) from error


def _egress_names(execution_id: str, policy_hash: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{execution_id}\0{policy_hash}".encode()).hexdigest()[:24]
    return (
        f"hermes-fleet-egress-{digest}",
        f"hermes-fleet-gateway-{digest}",
    )


def _network_labels(*, execution_id: str, grant: NetworkGrant) -> dict[str, str]:
    return {
        "dev.hermes.fleet.role": "egress-network",
        "dev.hermes.fleet.execution": execution_id,
        "dev.hermes.fleet.network_mode": grant.mode,
        "dev.hermes.fleet.network_policy": grant.policy_hash,
        "dev.hermes.fleet.network_authority": grant.authority_ref,
    }


def _gateway_labels(
    *, execution_id: str, grant: NetworkGrant, script_hash: str
) -> dict[str, str]:
    labels = _network_labels(execution_id=execution_id, grant=grant)
    labels["dev.hermes.fleet.role"] = "egress-gateway"
    labels["dev.hermes.fleet.gateway_script"] = script_hash
    return labels


def _gateway_start_command() -> str:
    return (
        "umask 077; "
        'printf %s "$FLEET_GATEWAY_SCRIPT_B64" | base64 -d '
        "> /run/fleet-gateway/proxy.pl; "
        'printf %s "$FLEET_GATEWAY_POLICY_B64" | base64 -d '
        "> /run/fleet-gateway/policy.tsv; "
        "unset FLEET_GATEWAY_SCRIPT_B64 FLEET_GATEWAY_POLICY_B64; "
        "export FLEET_GATEWAY_BIND_IP="
        "$(hostname -i | awk '{print $1}'); "
        "exec perl /run/fleet-gateway/proxy.pl "
        "/run/fleet-gateway/policy.tsv"
    )


def _private_bridge_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and address.is_private and not address.is_loopback


def _container_networks(document: dict[str, object]) -> dict[str, object]:
    settings = document.get("NetworkSettings")
    networks = settings.get("Networks") if isinstance(settings, dict) else None
    if not isinstance(networks, dict):
        raise NetworkIsolationError("Docker network membership is unavailable")
    return networks


def _container_network_ip(document: dict[str, object], network_name: str) -> str:
    networks = _container_networks(document)
    attachment = networks.get(network_name)
    value = attachment.get("IPAddress") if isinstance(attachment, dict) else None
    if type(value) is not str or not _private_bridge_ipv4(value):
        raise NetworkIsolationError("Fleet egress gateway internal IP is unavailable")
    return value


def _gateway_policy(grant: NetworkGrant) -> str:
    if type(grant) is not NetworkGrant or not grant.direct_egress:
        raise NetworkIsolationError("gateway policy requires direct egress")
    lines = [
        f"mode\t{grant.mode}",
        f"policy\t{grant.policy_hash}",
    ]
    for item in grant.destinations:
        lines.append(
            "destination\t"
            + item.host
            + "\t"
            + ",".join(item.resolved_ips)
            + "\t"
            + ",".join(str(port) for port in item.ports)
        )
    return "\n".join(lines) + "\n"


def _one_json_document(payload: str, label: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise NetworkIsolationError(f"{label} returned invalid JSON") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise NetworkIsolationError(f"{label} returned an unsupported document")
    return value[0]


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NetworkIsolationError("Docker network command was unavailable") from error
    if (
        len(completed.stdout.encode("utf-8", errors="replace")) > _MAX_CLI_OUTPUT
        or len(completed.stderr.encode("utf-8", errors="replace")) > _MAX_CLI_OUTPUT
    ):
        raise NetworkIsolationError("Docker network command output exceeded its bound")
    return completed


_PERL_GATEWAY_SCRIPT = r"""use strict;
use warnings;
use IO::Socket::INET;
use IO::Select;
use Socket qw(AF_INET SOCK_STREAM NI_NUMERICHOST getaddrinfo getnameinfo);
$| = 1;

my $policy_file = shift @ARGV or die "policy path required\n";
my $policy_hash = $ENV{FLEET_GATEWAY_POLICY_HASH} // die "policy hash missing\n";
my $bind_ip = $ENV{FLEET_GATEWAY_BIND_IP} // die "bind IP missing\n";
my %destinations;
open my $pf, '<', $policy_file or die "policy unavailable\n";
while (my $line = <$pf>) {
    chomp $line;
    my @parts = split /\t/, $line, -1;
    next if !@parts;
    if ($parts[0] eq 'destination') {
        die "invalid destination\n" unless @parts == 4;
        my ($host, $ips, $ports) = @parts[1..3];
        my %ipset = map { $_ => 1 } grep { length $_ } split /,/, $ips;
        my %portset = map { int($_) => 1 } grep { /^\d+$/ } split /,/, $ports;
        $destinations{$host} = { ips => \%ipset, ports => \%portset };
    }
}
close $pf;

sub audit {
    my ($decision, $reason, $host, $port, $ips) = @_;
    $host //= '-';
    $host =~ s/[\t\r\n]/_/g;
    $reason =~ s/[\t\r\n]/_/g;
    my $ip_text = join(',', @$ips);
    print join("\t", 'FLEET_EGRESS_V1', time(), $decision, $reason,
        $host, int($port || 0), $ip_text, $policy_hash), "\n";
}

sub public_ipv4 {
    my ($ip) = @_;
    return 0 unless $ip =~ /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
    my @o = ($1, $2, $3, $4);
    return 0 if grep { $_ > 255 } @o;
    return 0 if $o[0] == 0 || $o[0] == 10 || $o[0] == 127;
    return 0 if $o[0] == 100 && $o[1] >= 64 && $o[1] <= 127;
    return 0 if $o[0] == 169 && $o[1] == 254;
    return 0 if $o[0] == 172 && $o[1] >= 16 && $o[1] <= 31;
    return 0 if $o[0] == 192 && $o[1] == 168;
    return 0 if $o[0] == 192 && $o[1] == 0;
    return 0 if $o[0] == 198 && ($o[1] == 18 || $o[1] == 19);
    return 0 if $o[0] == 198 && $o[1] == 51 && $o[2] == 100;
    return 0 if $o[0] == 203 && $o[1] == 0 && $o[2] == 113;
    return 0 if $o[0] >= 224;
    return 1;
}

sub is_ipv4_literal {
    my ($value) = @_;
    return $value =~ /^\d{1,3}(?:\.\d{1,3}){3}$/;
}

sub resolve4 {
    my ($host) = @_;
    if (is_ipv4_literal($host)) {
        return public_ipv4($host) ? ($host) : ();
    }
    my ($err, @answers) = getaddrinfo($host, undef,
        { family => AF_INET, socktype => SOCK_STREAM });
    return () if $err;
    my %seen;
    for my $answer (@answers) {
        my ($name_err, $numeric) = getnameinfo($answer->{addr}, NI_NUMERICHOST);
        next if $name_err || !$numeric;
        $seen{$numeric} = 1;
    }
    return sort keys %seen;
}

sub authorize {
    my ($host, $port) = @_;
    $host = lc($host // '');
    $host =~ s/\.$//;
    return (0, 'invalid_host', undef, []) unless $host =~ /^[a-z0-9.-]{1,253}$/;
    return (0, 'forbidden_port', undef, []) if $port == 53 || $port == 853 ||
        $port == 2375 || $port == 2376 || $port == 2377;
    my @runtime = resolve4($host);
    return (0, 'dns_failed', undef, []) unless @runtime;
    for my $ip (@runtime) {
        return (0, 'non_public_resolution', undef, \@runtime) unless public_ipv4($ip);
    }
    my $target = $destinations{$host};
    if (!$target && is_ipv4_literal($host)) {
        my @matches = grep {
            exists $destinations{$_}->{ips}->{$host}
        } keys %destinations;
        $target = $destinations{$matches[0]} if @matches == 1;
    }
    return (0, 'destination_not_allowlisted', undef, \@runtime)
        unless $target;
    return (0, 'port_not_allowlisted', undef, \@runtime)
        unless $target->{ports}->{$port};
    my %runtime = map { $_ => 1 } @runtime;
    my @pinned = sort keys %{$target->{ips}};
    return (0, 'dns_rebinding_or_drift', undef, \@runtime)
        unless @pinned == @runtime && !grep { !$runtime{$_} } @pinned;
    return (1, 'allowed', $runtime[0], \@runtime);
}

sub write_all {
    my ($fh, $data) = @_;
    my $offset = 0;
    while ($offset < length($data)) {
        my $written = syswrite($fh, $data, length($data) - $offset, $offset);
        return 0 unless defined $written && $written > 0;
        $offset += $written;
    }
    return 1;
}

sub relay {
    my ($client, $remote) = @_;
    my $selector = IO::Select->new($client, $remote);
    while ($selector->count) {
        my @ready = $selector->can_read(60);
        last unless @ready;
        for my $source (@ready) {
            my $buffer = '';
            my $read = sysread($source, $buffer, 16384);
            if (!defined $read || $read == 0) {
                $selector->remove($source);
                next;
            }
            my $target = fileno($source) == fileno($client) ? $remote : $client;
            return unless write_all($target, $buffer);
        }
    }
}

sub handle_client {
    my ($client) = @_;
    $client->autoflush(1);
    my $request = '';
    while (length($request) < 8192 && $request !~ /\r?\n\r?\n/) {
        my $buffer = '';
        my $read = sysread($client, $buffer, 1024);
        last unless defined $read && $read > 0;
        $request .= $buffer;
    }
    my ($first) = split /\r?\n/, $request, 2;
    my $connect_pattern = qr{
        ^CONNECT\s+([A-Za-z0-9.-]+):(\d{1,5})\s+HTTP/1\.[01]$
    }x;
    unless (defined $first && $first =~ $connect_pattern) {
        audit('deny', 'connect_only', '-', 0, []);
        write_all(
            $client,
            "HTTP/1.1 405 Method Not Allowed\r\n" .
            "Connection: close\r\n\r\n"
        );
        return;
    }
    my ($host, $port) = (lc($1), int($2));
    my ($allowed, $reason, $ip, $ips) = authorize($host, $port);
    unless ($allowed) {
        audit('deny', $reason, $host, $port, $ips);
        write_all($client, "HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
        return;
    }
    my $remote = IO::Socket::INET->new(
        PeerAddr => $ip,
        PeerPort => $port,
        Proto => 'tcp',
        Timeout => 5,
    );
    unless ($remote) {
        audit('deny', 'connect_failed', $host, $port, $ips);
        write_all($client, "HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n");
        return;
    }
    audit('allow', 'connected', $host, $port, $ips);
    write_all($client, "HTTP/1.1 200 Connection Established\r\n\r\n") or return;
    relay($client, $remote);
    close $remote;
}

my $server = IO::Socket::INET->new(
    LocalAddr => $bind_ip,
    LocalPort => 8080,
    Proto => 'tcp',
    Listen => 32,
    ReuseAddr => 1,
) or die "gateway listen failed\n";
$SIG{CHLD} = 'IGNORE';
while (my $client = $server->accept()) {
    my $pid = fork();
    if (!defined $pid) {
        audit('deny', 'fork_failed', '-', 0, []);
        close $client;
        next;
    }
    if ($pid == 0) {
        close $server;
        handle_client($client);
        close $client;
        exit 0;
    }
    close $client;
}
"""
