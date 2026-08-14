"""Foreground Fleet node service over the public Keryx Python SDK."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import re
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from ._paths import is_concrete_path
from .backend_capabilities import BackendCapabilities
from .config import get_fleet_dir, get_hermes_home, load_fleet_config
from .destination_recipe_execution import DestinationRecipeExecutor
from .execution_control import ExecutionControlClient
from .fleet_node import FleetNodeWorker
from .hermes_runs import HermesRunsClient
from .host_profile_capabilities import host_profile_capabilities
from .models import FleetDefaults, NodeConfig, NodePolicy, _require_exact_type
from .observation import ObservationClient, build_observation
from .profile_inventory import scan_profile_distributions
from .profile_runtime import DestinationSecretResolver, ProfileHermesRuntime
from .remote_observation import RemoteObservationConfig, RemoteObservationPublisher
from .selection import select_nodes

logger = logging.getLogger(__name__)


class _Node(Protocol):
    peer_id: str

    async def start(self) -> None: ...

    def on_task(self, handler: object) -> None: ...

    async def list_peers(self) -> list[dict[str, Any]]: ...

    async def start_registration(self, *, ttl_seconds: int) -> dict[str, object]: ...

    async def serve_forever(self) -> None: ...

    async def stop(self) -> None: ...


class _Observation(Protocol):
    def publish(self, observation: dict[str, Any]) -> str: ...

    def admission_generation(self) -> int: ...


class _ObservedWorker(Protocol):
    @property
    def observed_active_worker_count(self) -> int: ...


@dataclass(frozen=True, slots=True)
class NodeRuntimeConfig:
    """Validated non-secret worker settings plus redacted secret values."""

    target: NodeConfig
    defaults: FleetDefaults
    controller_peer_ids: tuple[str, ...]
    hermes_endpoint: str
    hermes_api_key: str = field(repr=False)
    binding_path: Path
    profiles_root: Path
    keryx_node_token: str = field(repr=False)
    registration_ttl_seconds: int = 300
    advertise_observation_publish: bool = False
    observation_socket: Path | None = None
    execution_control_socket: Path | None = None
    execution_policy: NodePolicy | None = None
    remote_observation_endpoint: str | None = None
    remote_observation_target_peer_id: str | None = None
    remote_observation_ca_cert_path: Path | None = None
    managed_network_id: str | None = None
    managed_device_id: str | None = None
    observation_interval_seconds: int = 30
    file_secret_sources: tuple[tuple[str, Path, str], ...] = ()

    def __post_init__(self) -> None:
        _require_exact_type(self.target, NodeConfig, "target must be a NodeConfig")
        _require_exact_type(
            self.defaults, FleetDefaults, "defaults must be FleetDefaults"
        )
        if type(self.controller_peer_ids) is not tuple or not self.controller_peer_ids:
            raise ValueError("controller_peer_ids must be a nonempty tuple")
        if type(self.hermes_endpoint) is not str or not self.hermes_endpoint:
            raise ValueError("hermes_endpoint must be nonempty")
        if type(self.hermes_api_key) is not str or not self.hermes_api_key:
            raise ValueError("hermes_api_key must be nonempty")
        if (
            not is_concrete_path(self.binding_path)
            or not self.binding_path.is_absolute()
        ):
            raise ValueError("binding_path must be an absolute Path")
        if (
            not is_concrete_path(self.profiles_root)
            or not self.profiles_root.is_absolute()
        ):
            raise ValueError("profiles_root must be an absolute Path")
        if type(self.keryx_node_token) is not str or not self.keryx_node_token:
            raise ValueError("keryx_node_token must be nonempty")
        if (
            isinstance(self.registration_ttl_seconds, bool)
            or not isinstance(self.registration_ttl_seconds, int)
            or self.registration_ttl_seconds < 30
            or self.registration_ttl_seconds > 86_400
        ):
            raise ValueError("registration_ttl_seconds must be between 30 and 86400")
        if type(self.advertise_observation_publish) is not bool:
            raise ValueError("advertise_observation_publish must be a bool")
        remote_identity_fields = (
            self.remote_observation_endpoint,
            self.remote_observation_target_peer_id,
        )
        if any(value is not None for value in remote_identity_fields) and not all(
            value is not None for value in remote_identity_fields
        ):
            raise ValueError("remote observation configuration must be complete")
        if (
            self.remote_observation_endpoint is not None
            and self.remote_observation_ca_cert_path is None
        ):
            raise ValueError("remote observation configuration must be complete")
        if (
            self.observation_socket is not None
            and self.remote_observation_endpoint is not None
        ):
            raise ValueError("local and remote observation transports are exclusive")
        observation_enabled = (
            self.observation_socket is not None
            or self.remote_observation_endpoint is not None
        )
        if observation_enabled != (
            self.managed_network_id is not None and self.managed_device_id is not None
        ):
            raise ValueError("observation configuration must be complete")
        if self.observation_socket is not None and (
            not is_concrete_path(self.observation_socket)
            or not self.observation_socket.is_absolute()
        ):
            raise ValueError("observation socket is invalid")
        if self.execution_control_socket is not None and (
            not is_concrete_path(self.execution_control_socket)
            or not self.execution_control_socket.is_absolute()
        ):
            raise ValueError("execution control socket is invalid")
        if self.execution_policy is not None:
            _require_exact_type(
                self.execution_policy,
                NodePolicy,
                "execution_policy must be a NodePolicy",
            )
        if observation_enabled and (
            not _managed_identifier(self.managed_network_id)
            or not _managed_identifier(self.managed_device_id)
        ):
            raise ValueError("observation identity is invalid")
        if self.remote_observation_endpoint is not None and (
            not self.remote_observation_endpoint
            or not _managed_identifier(self.remote_observation_target_peer_id)
        ):
            raise ValueError("remote observation transport is invalid")
        remote_ca_path = self.remote_observation_ca_cert_path
        if self.remote_observation_endpoint is not None and (
            remote_ca_path is None
            or not is_concrete_path(remote_ca_path)
            or not remote_ca_path.is_absolute()
        ):
            raise ValueError("remote observation CA certificate path is invalid")
        if (
            isinstance(self.observation_interval_seconds, bool)
            or not isinstance(self.observation_interval_seconds, int)
            or self.observation_interval_seconds < 5
            or self.observation_interval_seconds > 3_600
        ):
            raise ValueError("observation_interval_seconds must be between 5 and 3600")
        if type(self.file_secret_sources) is not tuple:
            raise ValueError("file secret sources must be a tuple")
        references: set[str] = set()
        for item in self.file_secret_sources:
            if (
                type(item) is not tuple
                or len(item) != 3
                or type(item[0]) is not str
                or not isinstance(item[1], Path)
                or not item[1].is_absolute()
                or type(item[2]) is not str
            ):
                raise ValueError("file secret source is invalid")
            if item[0] in references:
                raise ValueError("file secret references must be unique")
            references.add(item[0])


_KERYX_BASELINE_PROTOCOL_FEATURES = (
    "absolute_deadlines_v1",
    "result_artifact_bytes_v1",
)
_FLEET_OBSERVATION_PUBLISH_PROTOCOL_FEATURE = "fleet.observation.publish.v1"


def operation_specs(*, include_hermes_run: bool = True) -> tuple[tuple[str, str], ...]:
    """Return the fixed v0.1 operation card; this is not a plugin registry."""
    direct = (
        ("fleet.health", "Bounded Fleet, Keryx, and Hermes capability health"),
        ("fleet.inventory", "Safe Fleet node identity and capability summary"),
        ("fleet.message", "Bounded direct text message acknowledgment"),
    )
    if not include_hermes_run:
        return direct
    return direct + (("fleet.hermes.run", "Deliberate authenticated local Hermes run"),)


async def _keryx_signals(
    node: _Node, controller_peer_ids: tuple[str, ...]
) -> tuple[bool, bool]:
    try:
        peers = await node.list_peers()
    except (OSError, RuntimeError, ValueError) as error:
        logger.warning("fleet-node Keryx observation failed: %s", error)
        return False, False
    if type(peers) is not list or any(
        type(peer) is not dict
        or type(peer.get("peer_id")) is not str
        or type(peer.get("connected")) is not bool
        or type(peer.get("local")) is not bool
        for peer in peers
    ):
        logger.warning("fleet-node Keryx observation returned invalid peer inventory")
        return False, False
    controller_routable = any(
        peer["peer_id"] in controller_peer_ids
        and peer["local"] is False
        and peer["connected"] is True
        for peer in peers
    )
    return controller_routable, True


def _build_recipe_executor(
    runtime: NodeRuntimeConfig,
    *,
    execution_control_factory: Callable[..., Any] = ExecutionControlClient,
    profile_runtime_factory: Callable[..., Any] = ProfileHermesRuntime,
    secret_resolver_factory: Callable[..., Any] = DestinationSecretResolver,
    executor_factory: Callable[..., Any] = DestinationRecipeExecutor,
    hermes_factory: Callable[..., Any] = HermesRunsClient,
    host_capabilities_factory: Callable[[], Any] | None = None,
    backend_capabilities: BackendCapabilities | None = None,
    now_ms: Callable[[], int] | None = None,
) -> Any | None:
    """Build destination FX8 authority only for a complete local managed node."""
    if (
        runtime.execution_control_socket is None
        or runtime.execution_policy is None
        or runtime.managed_network_id is None
        or runtime.managed_device_id is None
    ):
        return None
    execution_policy = runtime.execution_policy
    capabilities = (
        backend_capabilities
        if backend_capabilities is not None
        else (host_capabilities_factory or _live_host_capabilities)()
    )
    profile_runtime = profile_runtime_factory(
        profiles_root=runtime.profiles_root,
        runs_factory=lambda profile: hermes_factory(
            endpoint=runtime.hermes_endpoint,
            api_key=runtime.hermes_api_key,
            profile=profile,
        ),
    )
    return executor_factory(
        execution_control=execution_control_factory(
            socket_path=runtime.execution_control_socket
        ),
        runtime=profile_runtime,
        secret_resolver=secret_resolver_factory(
            allowed_references=execution_policy.allowed_secret_references,
            file_sources={
                reference: (source, destination_name)
                for reference, source, destination_name in runtime.file_secret_sources
            },
        ),
        current_policy_digest=lambda: execution_policy.content_hash,
        current_capabilities_hash=lambda: capabilities.content_hash,
        now_ms=now_ms or (lambda: int(time.time() * 1_000)),
    )


def _live_host_capabilities() -> BackendCapabilities:
    logical_cpus = os.cpu_count()
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        physical_pages = os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        page_size = physical_pages = 0
    if type(logical_cpus) is not int or logical_cpus < 1:
        raise RuntimeError("host logical CPU capacity is unavailable")
    if type(page_size) is not int or type(physical_pages) is not int:
        raise RuntimeError("host memory capacity is unavailable")
    memory_bytes = page_size * physical_pages
    if memory_bytes < 1:
        raise RuntimeError("host memory capacity is unavailable")
    return host_profile_capabilities(
        logical_cpus=logical_cpus,
        memory_bytes=memory_bytes,
        operating_system=platform.system().lower(),
        architecture=platform.machine().lower(),
    )


async def run_node_service(
    runtime: NodeRuntimeConfig,
    *,
    card_factory: Callable[[bool], object],
    node_factory: Callable[..., _Node],
    shutdown: asyncio.Event,
    hermes_factory: Callable[..., Any] = HermesRunsClient,
    observation_factory: Callable[..., _Observation] = ObservationClient,
    remote_observation_factory: Callable[..., _Observation] = (
        RemoteObservationPublisher
    ),
    recipe_executor_factory: Callable[[NodeRuntimeConfig], Any | None] | None = None,
) -> None:
    """Run one registered Keryx worker until shutdown or worker failure."""
    if type(runtime) is not NodeRuntimeConfig:
        raise ValueError("runtime must be a NodeRuntimeConfig")
    if type(shutdown) is not asyncio.Event:
        raise ValueError("shutdown must be an asyncio.Event")
    hermes = hermes_factory(
        endpoint=runtime.hermes_endpoint,
        api_key=runtime.hermes_api_key,
    )
    health = await asyncio.to_thread(hermes.health)
    include_hermes_run = _runs_available(health)
    observer: _Observation | None = None
    if runtime.observation_socket is not None:
        assert runtime.managed_network_id is not None
        assert runtime.managed_device_id is not None
        observer = observation_factory(
            socket_path=runtime.observation_socket,
            network_id=runtime.managed_network_id,
            device_id=runtime.managed_device_id,
        )
    elif runtime.remote_observation_endpoint is not None:
        assert runtime.managed_network_id is not None
        assert runtime.managed_device_id is not None
        assert runtime.remote_observation_target_peer_id is not None
        observer = remote_observation_factory(
            RemoteObservationConfig(
                relay_endpoint=runtime.remote_observation_endpoint,
                relay_ca_cert_path=runtime.remote_observation_ca_cert_path,
                source_peer_id=runtime.target.peer_id,
                node_token=runtime.keryx_node_token,
                target_peer_id=runtime.remote_observation_target_peer_id,
                network_id=runtime.managed_network_id,
                device_id=runtime.managed_device_id,
            )
        )
    card = card_factory(include_hermes_run)
    _preserve_keryx_baseline_protocol_features(card)
    if runtime.advertise_observation_publish:
        _advertise_observation_publish(card)
    expected_specs = operation_specs(include_hermes_run=include_hermes_run)
    skill_ids = tuple(
        getattr(skill, "id", None) for skill in getattr(card, "skills", ())
    )
    if skill_ids != tuple(operation for operation, _description in expected_specs):
        raise ValueError("card must contain the exact Fleet operation set")

    node = node_factory(
        card=card,
        node_token=runtime.keryx_node_token,
        worker_concurrency=1,
    )
    started = False
    serve_task: asyncio.Task[None] | None = None
    observation_task: asyncio.Task[None] | None = None
    stop_wait: asyncio.Task[bool] | None = None
    try:
        await node.start()
        started = True
        if node.peer_id != runtime.target.peer_id:
            raise RuntimeError("local Keryx peer does not match configured Fleet peer")

        capacity_updates: asyncio.Queue[None] = asyncio.Queue(maxsize=1)

        async def capacity_observer(_active_workers: int) -> None:
            if not capacity_updates.full():
                capacity_updates.put_nowait(None)

        backend_capabilities = _live_host_capabilities()
        recipe_executor = (
            _build_recipe_executor(runtime, backend_capabilities=backend_capabilities)
            if recipe_executor_factory is None
            else recipe_executor_factory(runtime)
        )
        worker = FleetNodeWorker(
            target=runtime.target,
            defaults=runtime.defaults,
            hermes=hermes,
            controller_peer_ids=runtime.controller_peer_ids,
            advertised_operations=tuple(
                operation for operation, _description in expected_specs
            ),
            readiness_inspector=(
                cast(ObservationClient, observer).inspect
                if runtime.observation_socket is not None and observer is not None
                else None
            ),
            admission_generation_inspector=(
                observer.admission_generation if observer is not None else None
            ),
            managed_network_id=runtime.managed_network_id,
            managed_device_id=runtime.managed_device_id,
            capacity_observer=capacity_observer if observer is not None else None,
            recipe_executor=recipe_executor,
            backend_capabilities=(
                backend_capabilities if recipe_executor is not None else None
            ),
        )
        worker.bind(node)
        registration = await node.start_registration(
            ttl_seconds=runtime.registration_ttl_seconds
        )
        if registration.get("accepted") is not True:
            raise RuntimeError("Keryx rejected Fleet operation registration")
        logger.info(
            "fleet-node ready name=%s peer_id=%s operations=%s",
            runtime.target.name,
            runtime.target.peer_id,
            len(expected_specs),
        )

        if observer is not None:
            admission_generation = await _admission_generation(observer)
            if admission_generation is not None:
                health = await asyncio.to_thread(hermes.health)
                if runtime.remote_observation_endpoint is not None:
                    network_reachable, keryx_available = True, True
                else:
                    network_reachable, keryx_available = await _keryx_signals(
                        node, runtime.controller_peer_ids
                    )
                await _publish_observation(
                    observer,
                    health,
                    worker.observed_active_worker_count,
                    admission_generation=admission_generation,
                    network_reachable=network_reachable,
                    keryx_available=keryx_available,
                    worker_available=include_hermes_run,
                )
            observation_task = asyncio.create_task(
                _observation_loop(
                    observer,
                    node,
                    runtime.controller_peer_ids,
                    hermes,
                    worker,
                    capacity_updates,
                    shutdown,
                    runtime.observation_interval_seconds,
                    include_hermes_run,
                    runtime.remote_observation_endpoint is not None,
                ),
                name="fleet-node-observation",
            )
        serve_task = asyncio.create_task(node.serve_forever(), name="fleet-node-worker")
        stop_wait = asyncio.create_task(shutdown.wait(), name="fleet-node-shutdown")
        waiters: set[asyncio.Task[Any]] = {serve_task, stop_wait}
        if observation_task is not None:
            waiters.add(observation_task)
        done, _pending = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_wait in done:
            if observation_task is not None and not observation_task.done():
                if not capacity_updates.full():
                    capacity_updates.put_nowait(None)
                await asyncio.gather(observation_task, return_exceptions=True)
            if observer is not None:
                admission_generation = await _admission_generation(observer)
                if admission_generation is not None:
                    await _publish_observation(
                        observer,
                        health,
                        worker.observed_active_worker_count,
                        admission_generation=admission_generation,
                        network_reachable=False,
                        keryx_available=False,
                        worker_available=False,
                    )
            await node.stop()
            await serve_task
        elif observation_task is not None and observation_task in done:
            await observation_task
        else:
            shutdown.set()
            if observation_task is not None and not observation_task.done():
                if not capacity_updates.full():
                    capacity_updates.put_nowait(None)
                await asyncio.gather(observation_task, return_exceptions=True)
            if observer is not None:
                admission_generation = await _admission_generation(observer)
                if admission_generation is not None:
                    await _publish_observation(
                        observer,
                        health,
                        worker.observed_active_worker_count,
                        admission_generation=admission_generation,
                        network_reachable=False,
                        keryx_available=False,
                        worker_available=False,
                    )
            await serve_task
    finally:
        if observation_task is not None and not observation_task.done():
            observation_task.cancel()
            await asyncio.gather(observation_task, return_exceptions=True)
        if stop_wait is not None:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)
        if serve_task is not None and not serve_task.done():
            serve_task.cancel()
            await asyncio.gather(serve_task, return_exceptions=True)
        if started:
            await node.stop()
        close_observer = getattr(observer, "close", None)
        if callable(close_observer):
            await asyncio.to_thread(close_observer)


async def _admission_generation(observer: _Observation) -> int | None:
    try:
        return await asyncio.to_thread(observer.admission_generation)
    except (OSError, RuntimeError, ValueError) as error:
        logger.warning("fleet-node admission generation lookup failed: %s", error)
        return None


async def _publish_observation(
    observer: _Observation,
    health: object,
    active_workers: int,
    *,
    admission_generation: int,
    network_reachable: bool,
    keryx_available: bool,
    worker_available: bool,
) -> None:
    observed_at_ms = int(time.time() * 1_000)

    def build_and_publish() -> str:
        profiles = scan_profile_distributions()
        sample = build_observation(
            admission_generation=admission_generation,
            hermes_health=health,
            active_workers=active_workers,
            max_workers=1,
            network_reachable=network_reachable,
            keryx_available=keryx_available,
            worker_available=worker_available,
            now_ms=lambda: observed_at_ms,
            profiles=profiles,
        )
        return observer.publish(sample)

    try:
        outcome = await asyncio.to_thread(build_and_publish)
    except (OSError, RuntimeError, ValueError) as error:
        logger.warning("fleet-node observation publish failed: %s", error)
        return
    if outcome in {"stale", "conflict"}:
        logger.warning("fleet-node observation was not current: outcome=%s", outcome)
    else:
        logger.debug("fleet-node observation outcome=%s", outcome)


async def _observation_loop(
    observer: _Observation,
    node: _Node,
    controller_peer_ids: tuple[str, ...],
    hermes: Any,
    worker: _ObservedWorker,
    capacity_updates: asyncio.Queue[None],
    shutdown: asyncio.Event,
    interval_seconds: float,
    worker_available: bool,
    remote_observation: bool,
) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(capacity_updates.get(), timeout=interval_seconds)
        except TimeoutError:
            pass
        if shutdown.is_set():
            return
        admission_generation = await _admission_generation(observer)
        if admission_generation is None:
            continue

        try:
            health = await asyncio.to_thread(hermes.health)
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("fleet-node Hermes observation failed: %s", error)
            health = None
        if remote_observation:
            network_reachable, keryx_available = True, True
        else:
            network_reachable, keryx_available = await _keryx_signals(
                node, controller_peer_ids
            )
        await _publish_observation(
            observer,
            health,
            worker.observed_active_worker_count,
            admission_generation=admission_generation,
            network_reachable=network_reachable,
            keryx_available=keryx_available,
            worker_available=worker_available,
        )


def _managed_identifier(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 256
        and value == value.strip()
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _environment_flag(
    environment: Mapping[str, str],
    name: str,
) -> bool:
    raw = environment.get(name)
    if raw is None or raw == "0":
        return False
    if raw == "1":
        return True
    raise ValueError(f"{name} must be 0 or 1")


def _protocol_features(card: object) -> list[str]:
    protocol_features = getattr(card, "protocol_features", None)
    if type(protocol_features) is not list:
        raise ValueError("card protocol_features must be a list")
    return protocol_features


def _preserve_keryx_baseline_protocol_features(card: object) -> None:
    _protocol_features(card)[:] = _KERYX_BASELINE_PROTOCOL_FEATURES


def _advertise_observation_publish(card: object) -> None:
    protocol_features = _protocol_features(card)
    if _FLEET_OBSERVATION_PUBLISH_PROTOCOL_FEATURE not in protocol_features:
        protocol_features.append(_FLEET_OBSERVATION_PUBLISH_PROTOCOL_FEATURE)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-node")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--node")
    parser.add_argument("--controller-peer-id", action="append", default=[])
    parser.add_argument("--hermes-endpoint", default="http://127.0.0.1:8642")
    parser.add_argument("--binding-db", type=Path)
    parser.add_argument("--registration-ttl", type=int, default=300)
    parser.add_argument("--observation-socket", type=Path)
    parser.add_argument("--remote-observation-endpoint")
    parser.add_argument("--remote-observation-target-peer-id")
    parser.add_argument("--remote-observation-ca-cert", type=Path)
    parser.add_argument("--managed-network-id")
    parser.add_argument("--managed-device-id")
    parser.add_argument("--observation-interval", type=int, default=30)
    parser.add_argument("--log-level", default="INFO")
    return parser


def _runtime_from_args(
    args: argparse.Namespace, *, environment: Mapping[str, str]
) -> NodeRuntimeConfig:
    config_path = args.config or (get_fleet_dir() / "nodes.yaml")
    if not config_path.is_absolute():
        raise ValueError("Fleet config path must be absolute")
    config = load_fleet_config(config_path)
    node_name = args.node or environment.get("FLEET_NODE_NAME", "")
    selected = select_nodes(config.nodes, names=(node_name,))
    if len(selected) != 1:
        raise ValueError("FLEET_NODE_NAME must identify one enabled Fleet node")

    controller_peer_ids = tuple(args.controller_peer_id)
    if not controller_peer_ids:
        controller_peer_ids = tuple(
            peer_id.strip()
            for peer_id in environment.get("FLEET_CONTROLLER_PEER_IDS", "").split(",")
            if peer_id.strip()
        )
    api_key = environment.get("API_SERVER_KEY", "")
    node_token = environment.get("KERYX_NODE_TOKEN", "")
    advertise_observation_publish = _environment_flag(
        environment,
        "FLEET_ADVERTISE_OBSERVATION_PUBLISH",
    )
    binding_path = args.binding_db or (config_path.parent / "run-bindings.sqlite3")
    if not binding_path.is_absolute():
        raise ValueError("binding DB path must be absolute")
    observation_socket = args.observation_socket
    if observation_socket is None and environment.get("FLEET_OBSERVATION_SOCKET"):
        observation_socket = Path(environment["FLEET_OBSERVATION_SOCKET"])
    execution_control_socket = (
        Path(environment["FLEET_EXECUTION_CONTROL_SOCKET"])
        if environment.get("FLEET_EXECUTION_CONTROL_SOCKET")
        else observation_socket
    )
    managed_network_id = args.managed_network_id or environment.get(
        "NODESCALE_NETWORK_ID"
    )
    managed_device_id = args.managed_device_id or environment.get("NODESCALE_DEVICE_ID")
    execution_policies = tuple(
        item.policy
        for item in config.managed_targets
        if item.source == "nodescale"
        and item.network_id == managed_network_id
        and item.device_id == managed_device_id
    )
    execution_policy = execution_policies[0] if len(execution_policies) == 1 else None
    remote_observation_endpoint = args.remote_observation_endpoint or environment.get(
        "FLEET_REMOTE_OBSERVATION_ENDPOINT"
    )
    remote_observation_target_peer_id = (
        args.remote_observation_target_peer_id
        or environment.get("FLEET_REMOTE_OBSERVATION_TARGET_PEER_ID")
    )
    remote_observation_ca_cert_path = args.remote_observation_ca_cert
    if remote_observation_ca_cert_path is None and environment.get(
        "HERMES_KERYX_REGISTRY_CA_CERT"
    ):
        remote_observation_ca_cert_path = Path(
            environment["HERMES_KERYX_REGISTRY_CA_CERT"]
        )
    file_secret_sources = _file_secret_sources(environment)
    return NodeRuntimeConfig(
        target=selected[0],
        defaults=config.defaults,
        controller_peer_ids=controller_peer_ids,
        hermes_endpoint=args.hermes_endpoint,
        hermes_api_key=api_key,
        binding_path=binding_path,
        profiles_root=get_hermes_home() / "profiles",
        keryx_node_token=node_token,
        registration_ttl_seconds=args.registration_ttl,
        advertise_observation_publish=advertise_observation_publish,
        observation_socket=observation_socket,
        execution_control_socket=execution_control_socket,
        execution_policy=execution_policy,
        remote_observation_endpoint=remote_observation_endpoint,
        remote_observation_target_peer_id=remote_observation_target_peer_id,
        remote_observation_ca_cert_path=remote_observation_ca_cert_path,
        managed_network_id=managed_network_id,
        managed_device_id=managed_device_id,
        observation_interval_seconds=args.observation_interval,
        file_secret_sources=file_secret_sources,
    )


def _file_secret_sources(
    environment: Mapping[str, str],
) -> tuple[tuple[str, Path, str], ...]:
    prefix = "FLEET_SECRET_FILE_"
    suffix = "_DESTINATION"
    result: list[tuple[str, Path, str]] = []
    for key in sorted(environment):
        if not key.startswith(prefix) or key.endswith(suffix):
            continue
        name = key[len(prefix) :]
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name) is None:
            raise ValueError("file secret source name is invalid")
        source_text = environment[key]
        destination_name = environment.get(key + suffix, "")
        source = Path(source_text).expanduser()
        if (
            not source_text
            or not source.is_absolute()
            or ".." in source.parts
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", destination_name)
            is None
        ):
            raise ValueError("file secret source is invalid")
        result.append((f"secret://worker/file/{name}", source, destination_name))
    configured_destinations = {
        key[: -len(suffix)]
        for key in environment
        if key.startswith(prefix) and key.endswith(suffix)
    }
    configured_sources = {
        key
        for key in environment
        if key.startswith(prefix) and not key.endswith(suffix)
    }
    if configured_destinations != configured_sources:
        raise ValueError("file secret source is invalid")
    return tuple(result)


def _build_card(include_hermes_run: bool) -> object:
    try:
        from keryx.card import AgentCard, Skill
    except ImportError as error:
        raise RuntimeError(
            "fleet-node requires the pinned Keryx Python SDK in its runtime environment"
        ) from error
    return AgentCard(
        name="hermes-fleet-node",
        description="Hermes Fleet node communication and execution adapter",
        version="0.1.0",
        skills=[
            Skill(id=operation, description=description)
            for operation, description in operation_specs(
                include_hermes_run=include_hermes_run
            )
        ],
    )


def _node_factory(**kwargs: Any) -> _Node:
    try:
        from keryx.node import KeryxNode
    except ImportError as error:
        raise RuntimeError(
            "fleet-node requires the pinned Keryx Python SDK in its runtime environment"
        ) from error
    return cast(_Node, KeryxNode(**kwargs))


async def _async_main(args: argparse.Namespace) -> None:
    runtime = _runtime_from_args(args, environment=os.environ)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, shutdown.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX fallback
            pass
    await run_node_service(
        runtime,
        card_factory=_build_card,
        node_factory=_node_factory,
        shutdown=shutdown,
    )


def _runs_available(health: object) -> bool:
    return (
        type(health) is dict
        and health.get("api") == "healthy"
        and all(
            health.get(field) is True
            for field in ("run_submission", "run_status", "run_stop")
        )
    )


def main() -> None:
    args = _parser().parse_args()
    level = getattr(logging, str(args.log_level).upper(), None)
    if type(level) is not int:
        raise SystemExit("invalid --log-level")
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        asyncio.run(_async_main(args))
    except (RuntimeError, ValueError) as error:
        logger.error("fleet-node failed: %s", error)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
