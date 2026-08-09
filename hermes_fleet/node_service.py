"""Foreground Fleet node service over the public Keryx Python SDK."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from ._paths import is_concrete_path
from .config import get_fleet_dir, load_fleet_config
from .fleet_node import FleetNodeWorker
from .hermes_runs import HermesRunsClient
from .models import FleetDefaults, NodeConfig, _require_exact_type
from .observation import ObservationClient, build_observation
from .run_binding import RunBindingStore
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

    def inspect(self) -> dict[str, Any]: ...


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
    keryx_node_token: str = field(repr=False)
    registration_ttl_seconds: int = 300
    observation_socket: Path | None = None
    managed_network_id: str | None = None
    managed_device_id: str | None = None
    observation_interval_seconds: int = 30

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
        if type(self.keryx_node_token) is not str or not self.keryx_node_token:
            raise ValueError("keryx_node_token must be nonempty")
        if (
            isinstance(self.registration_ttl_seconds, bool)
            or not isinstance(self.registration_ttl_seconds, int)
            or self.registration_ttl_seconds < 30
            or self.registration_ttl_seconds > 86_400
        ):
            raise ValueError("registration_ttl_seconds must be between 30 and 86400")
        observation_fields = (
            self.observation_socket,
            self.managed_network_id,
            self.managed_device_id,
        )
        if any(value is not None for value in observation_fields) and not all(
            value is not None for value in observation_fields
        ):
            raise ValueError("observation configuration must be complete")
        if self.observation_socket is not None and (
            not is_concrete_path(self.observation_socket)
            or not self.observation_socket.is_absolute()
            or not _managed_identifier(self.managed_network_id)
            or not _managed_identifier(self.managed_device_id)
        ):
            raise ValueError("observation configuration is invalid")
        if (
            isinstance(self.observation_interval_seconds, bool)
            or not isinstance(self.observation_interval_seconds, int)
            or self.observation_interval_seconds < 5
            or self.observation_interval_seconds > 3_600
        ):
            raise ValueError("observation_interval_seconds must be between 5 and 3600")


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
        peer["peer_id"] in controller_peer_ids and peer["local"] is False
        for peer in peers
    )
    return controller_routable, True


async def run_node_service(
    runtime: NodeRuntimeConfig,
    *,
    card_factory: Callable[[bool], object],
    node_factory: Callable[..., _Node],
    shutdown: asyncio.Event,
    hermes_factory: Callable[..., Any] = HermesRunsClient,
    observation_factory: Callable[..., _Observation] = ObservationClient,
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
    card = card_factory(include_hermes_run)
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

        worker = FleetNodeWorker(
            target=runtime.target,
            defaults=runtime.defaults,
            hermes=hermes,
            bindings=RunBindingStore(runtime.binding_path),
            controller_peer_ids=runtime.controller_peer_ids,
            advertised_operations=tuple(
                operation for operation, _description in expected_specs
            ),
            readiness_inspector=observer.inspect if observer is not None else None,
            capacity_observer=capacity_observer if observer is not None else None,
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
            network_reachable, keryx_available = await _keryx_signals(
                node, runtime.controller_peer_ids
            )
            await _publish_observation(
                observer,
                health,
                worker.observed_active_worker_count,
                network_reachable=network_reachable,
                keryx_available=keryx_available,
                worker_available=True,
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
                await _publish_observation(
                    observer,
                    health,
                    worker.observed_active_worker_count,
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
                await _publish_observation(
                    observer,
                    health,
                    worker.observed_active_worker_count,
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


async def _publish_observation(
    observer: _Observation,
    health: object,
    active_workers: int,
    *,
    network_reachable: bool,
    keryx_available: bool,
    worker_available: bool,
) -> None:
    observed_at_ms = int(time.time() * 1_000)

    def build_and_publish() -> str:
        sample = build_observation(
            hermes_health=health,
            active_workers=active_workers,
            max_workers=1,
            network_reachable=network_reachable,
            keryx_available=keryx_available,
            worker_available=worker_available,
            now_ms=lambda: observed_at_ms,
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
) -> None:
    while not shutdown.is_set():
        try:
            await asyncio.wait_for(capacity_updates.get(), timeout=interval_seconds)
        except TimeoutError:
            pass
        if shutdown.is_set():
            return
        try:
            health = await asyncio.to_thread(hermes.health)
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning("fleet-node Hermes observation failed: %s", error)
            health = None
        network_reachable, keryx_available = await _keryx_signals(
            node, controller_peer_ids
        )
        await _publish_observation(
            observer,
            health,
            worker.observed_active_worker_count,
            network_reachable=network_reachable,
            keryx_available=keryx_available,
            worker_available=True,
        )


def _managed_identifier(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 256
        and value == value.strip()
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-node")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--node")
    parser.add_argument("--controller-peer-id", action="append", default=[])
    parser.add_argument("--hermes-endpoint", default="http://127.0.0.1:8642")
    parser.add_argument("--binding-db", type=Path)
    parser.add_argument("--registration-ttl", type=int, default=300)
    parser.add_argument("--observation-socket", type=Path)
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
    binding_path = args.binding_db or (config_path.parent / "run-bindings.sqlite3")
    if not binding_path.is_absolute():
        raise ValueError("binding DB path must be absolute")
    observation_socket = args.observation_socket
    if observation_socket is None and environment.get("FLEET_OBSERVATION_SOCKET"):
        observation_socket = Path(environment["FLEET_OBSERVATION_SOCKET"])
    managed_network_id = args.managed_network_id or environment.get(
        "NODESCALE_NETWORK_ID"
    )
    managed_device_id = args.managed_device_id or environment.get("NODESCALE_DEVICE_ID")
    return NodeRuntimeConfig(
        target=selected[0],
        defaults=config.defaults,
        controller_peer_ids=controller_peer_ids,
        hermes_endpoint=args.hermes_endpoint,
        hermes_api_key=api_key,
        binding_path=binding_path,
        keryx_node_token=node_token,
        registration_ttl_seconds=args.registration_ttl,
        observation_socket=observation_socket,
        managed_network_id=managed_network_id,
        managed_device_id=managed_device_id,
        observation_interval_seconds=args.observation_interval,
    )


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
