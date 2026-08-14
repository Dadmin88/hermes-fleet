from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _runtime(tmp_path):
    from hermes_fleet.models import FleetDefaults, NodeConfig, NodePolicy
    from hermes_fleet.node_service import NodeRuntimeConfig

    return NodeRuntimeConfig(
        target=NodeConfig(
            name="vps",
            peer_id="peer-vps",
            policy=NodePolicy(
                allowed_operations=(
                    "fleet.health",
                    "fleet.inventory",
                    "fleet.message",
                    "fleet.hermes.run",
                )
            ),
        ),
        defaults=FleetDefaults(),
        controller_peer_ids=("peer-controller",),
        hermes_endpoint="http://127.0.0.1:8642",
        hermes_api_key="test-api-key",
        binding_path=tmp_path / "run-bindings.sqlite3",
        profiles_root=tmp_path / "profiles",
        keryx_node_token="test-node-token",
    )


class _Node:
    def __init__(self, *, card, node_token: str, worker_concurrency: int) -> None:
        self.card = card
        self.node_token = node_token
        self.worker_concurrency = worker_concurrency
        self.peer_id = "peer-vps"
        self.handler = None
        self.started = False
        self.registration = None
        self.served = False
        self.stopped = False
        self.connected = True
        self.include_controller = True
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def list_peers(self) -> list[dict[str, object]]:
        peers: list[dict[str, object]] = [
            {
                "peer_id": self.peer_id,
                "connected": True,
                "local": True,
            }
        ]
        if self.include_controller:
            peers.append(
                {
                    "peer_id": "peer-controller",
                    "connected": self.connected,
                    "local": False,
                }
            )
        return peers

    def task_handle(self, _task_id: str):
        raise KeyError("task not found")

    def on_task(self, handler) -> None:
        self.handler = handler

    async def start_registration(self, *, ttl_seconds: int) -> dict[str, object]:
        self.registration = ttl_seconds
        return {"accepted": True, "peer_id": self.peer_id}

    async def serve_forever(self) -> None:
        self.served = True
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()


class _Hermes:
    def __init__(self, *, healthy: bool = True, **_kwargs) -> None:
        self.healthy = healthy

    def health(self) -> dict[str, object]:
        return {
            "api": "healthy" if self.healthy else "unavailable",
            "run_submission": self.healthy,
            "run_status": self.healthy,
            "run_stop": self.healthy,
        }

    def start(self, **_kwargs):  # pragma: no cover - service lifecycle only
        raise AssertionError("not called")

    def wait(self, **_kwargs):  # pragma: no cover - service lifecycle only
        raise AssertionError("not called")

    def stop(self, *_args, **_kwargs):  # pragma: no cover - lifecycle only
        raise AssertionError("not called")


async def _fast_hermes_readiness(hermes) -> dict[str, Any]:
    from hermes_fleet.node_service import _wait_for_hermes_runs

    return await _wait_for_hermes_runs(
        hermes,
        attempts=2,
        delay_seconds=0.0,
        probe_timeout_seconds=0.1,
    )


def _card_factory(cards):
    def factory(include_hermes_run: bool):
        from hermes_fleet.node_service import operation_specs

        card = SimpleNamespace(
            name="fleet-node:vps",
            protocol_features=[],
            skills=[
                SimpleNamespace(id=operation)
                for operation, _ in operation_specs(
                    include_hermes_run=include_hermes_run
                )
            ],
        )
        cards.append(card)
        return card

    return factory


def test_keryx_signals_use_concrete_availability_and_reachability() -> None:
    from hermes_fleet.node_service import _keryx_signals

    node = _Node(card=object(), node_token="token", worker_concurrency=1)
    assert asyncio.run(_keryx_signals(node, ("peer-controller",))) == (True, True)

    node.connected = False
    assert asyncio.run(_keryx_signals(node, ("peer-controller",))) == (False, True)

    node.include_controller = False
    assert asyncio.run(_keryx_signals(node, ("peer-controller",))) == (False, True)

    async def malformed() -> list[dict[str, object]]:
        return [{}]

    node.list_peers = malformed  # type: ignore[method-assign]
    assert asyncio.run(_keryx_signals(node, ("peer-controller",))) == (False, False)

    async def unavailable() -> list[dict[str, object]]:
        raise RuntimeError("daemon unavailable")

    node.list_peers = unavailable  # type: ignore[method-assign]
    assert asyncio.run(_keryx_signals(node, ("peer-controller",))) == (False, False)


def test_observation_collection_and_publish_run_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_fleet import node_service

    event_loop_thread = threading.get_ident()
    observed_threads: list[int] = []

    def build(**_kwargs) -> dict[str, object]:
        observed_threads.append(threading.get_ident())
        return {"sample": True}

    class Observer:
        def publish(self, observation: dict[str, Any]) -> str:
            assert observation == {"sample": True}
            observed_threads.append(threading.get_ident())
            return "recorded"

        def inspect(self) -> dict[str, Any]:
            return {}

        def admission_generation(self) -> int:
            return 7

    monkeypatch.setattr(node_service, "build_observation", build)
    asyncio.run(
        node_service._publish_observation(
            Observer(),
            {},
            0,
            admission_generation=1,
            network_reachable=True,
            keryx_available=True,
            worker_available=True,
        )
    )

    assert len(observed_threads) == 2
    assert all(thread_id != event_loop_thread for thread_id in observed_threads)


def test_fleet_node_service_registers_four_operations_and_stops_cleanly(
    tmp_path,
) -> None:
    from hermes_fleet.node_service import run_node_service

    cards = []
    created: list[_Node] = []

    def factory(**kwargs):
        node = _Node(**kwargs)
        created.append(node)
        return node

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            _runtime(tmp_path),
            card_factory=_card_factory(cards),
            node_factory=factory,
            shutdown=shutdown,
            hermes_factory=_Hermes,
        )

    asyncio.run(exercise())

    assert [skill.id for skill in cards[0].skills] == [
        "fleet.health",
        "fleet.inventory",
        "fleet.message",
        "fleet.hermes.run",
    ]
    node = created[0]
    assert node.started is True
    assert node.handler is not None
    assert node.registration == 300
    assert node.served is True
    assert node.stopped is True
    assert node.worker_concurrency == 1
    assert node.node_token == "test-node-token"


def test_fleet_node_service_waits_for_restarted_hermes_api(tmp_path) -> None:
    from hermes_fleet import node_service

    cards = []
    health_calls = 0

    class RestartingHermes(_Hermes):
        def health(self) -> dict[str, object]:
            nonlocal health_calls
            health_calls += 1
            self.healthy = health_calls > 1
            return super().health()

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await node_service.run_node_service(
            _runtime(tmp_path),
            card_factory=_card_factory(cards),
            node_factory=_Node,
            shutdown=shutdown,
            hermes_factory=RestartingHermes,
            hermes_readiness_waiter=_fast_hermes_readiness,
        )

    asyncio.run(exercise())

    assert health_calls == 2
    assert [skill.id for skill in cards[0].skills] == [
        "fleet.health",
        "fleet.inventory",
        "fleet.message",
        "fleet.hermes.run",
    ]


def test_hermes_readiness_probe_is_individually_bounded() -> None:
    from hermes_fleet.node_service import _wait_for_hermes_runs

    calls = 0

    class StalledHermes:
        def health(self) -> dict[str, object]:
            nonlocal calls
            calls += 1
            threading.Event().wait(0.05)
            return _Hermes().health()

    health = asyncio.run(
        _wait_for_hermes_runs(
            StalledHermes(),
            attempts=1,
            delay_seconds=0.0,
            probe_timeout_seconds=0.001,
        )
    )

    assert calls == 1
    assert health == {}


def test_recipe_executor_uses_local_control_with_remote_observation(
    tmp_path,
) -> None:
    from dataclasses import replace

    from hermes_fleet.models import NodePolicy
    from hermes_fleet.node_service import _build_recipe_executor

    execution_policy = NodePolicy(
        allowed_operations=("fleet.hermes.run",),
        allowed_secret_references=("secret://worker/file/HERMES_AUTH",),
    )
    runtime = replace(
        _runtime(tmp_path),
        execution_control_socket=tmp_path / "managed-control.sock",
        execution_policy=execution_policy,
        remote_observation_endpoint="https://relay.example:50052",
        remote_observation_target_peer_id="peer-controller",
        remote_observation_ca_cert_path=tmp_path / "relay-ca.pem",
        managed_network_id="network-1",
        managed_device_id="device-1",
        model_config_path=tmp_path / "config.yaml",
    )
    created: dict[str, Any] = {}

    class Control:
        def __init__(self, *, socket_path):
            created["socket"] = socket_path

    class ProfileRuntime:
        def __init__(
            self, *, profiles_root, runs_factory, api_server_key, model_config_path
        ):
            created["profiles_root"] = profiles_root
            created["runs"] = runs_factory("fleet-execution")
            created["profile_api_server_key"] = api_server_key
            created["model_config_path"] = model_config_path

    class Secrets:
        def __init__(self, *, allowed_references, file_sources):
            created["allowed"] = allowed_references
            created["file_sources"] = file_sources

    class Executor:
        def __init__(self, **kwargs):
            created.update(kwargs)

    value = _build_recipe_executor(
        runtime,
        execution_control_factory=Control,
        profile_runtime_factory=ProfileRuntime,
        secret_resolver_factory=Secrets,
        executor_factory=Executor,
        hermes_factory=lambda **kwargs: SimpleNamespace(**kwargs),
        host_capabilities_factory=lambda: SimpleNamespace(
            content_hash="sha256:" + "3" * 64
        ),
        now_ms=lambda: 10_000,
    )

    assert isinstance(value, Executor)
    assert created["socket"] == runtime.execution_control_socket
    assert created["profiles_root"] == tmp_path / "profiles"
    assert created["runs"].profile == "fleet-execution"
    assert created["profile_api_server_key"] == runtime.hermes_api_key
    assert created["model_config_path"] == tmp_path / "config.yaml"
    assert created["allowed"] == execution_policy.allowed_secret_references
    assert created["file_sources"] == {}
    assert created["current_policy_digest"]() == execution_policy.content_hash
    assert created["current_capabilities_hash"]() == "sha256:" + "3" * 64


def test_destination_recipe_executor_is_unavailable_without_local_managed_control(
    tmp_path,
) -> None:
    from hermes_fleet.node_service import _build_recipe_executor

    assert _build_recipe_executor(_runtime(tmp_path)) is None


def test_file_secret_sources_are_destination_local_and_provider_neutral() -> None:
    from dataclasses import replace

    from hermes_fleet.node_service import _file_secret_sources

    values = _file_secret_sources(
        {
            "FLEET_SECRET_FILE_HERMES_AUTH": "/srv/hermes-worker/auth.json",
            "FLEET_SECRET_FILE_HERMES_AUTH_DESTINATION": "auth.json",
        }
    )

    assert values == (
        (
            "secret://worker/file/HERMES_AUTH",
            Path("/srv/hermes-worker/auth.json"),
            "auth.json",
        ),
    )
    assert replace(_runtime(Path("/tmp/fleet-test")), file_secret_sources=values)


@pytest.mark.parametrize(
    "environment",
    [
        {"FLEET_SECRET_FILE_HERMES_AUTH": "relative/auth.json"},
        {"FLEET_SECRET_FILE_HERMES_AUTH": "/safe/auth.json"},
        {"FLEET_SECRET_FILE_HERMES_AUTH_DESTINATION": "auth.json"},
        {
            "FLEET_SECRET_FILE_bad": "/safe/auth.json",
            "FLEET_SECRET_FILE_bad_DESTINATION": "auth.json",
        },
        {
            "FLEET_SECRET_FILE_HERMES_AUTH": "/safe/auth.json",
            "FLEET_SECRET_FILE_HERMES_AUTH_DESTINATION": "../auth.json",
        },
    ],
)
def test_file_secret_sources_reject_incomplete_or_unsafe_mapping(environment) -> None:
    from hermes_fleet.node_service import _file_secret_sources

    with pytest.raises(ValueError, match="file secret source"):
        _file_secret_sources(environment)


def test_fleet_node_service_rejects_wrong_local_keryx_identity(tmp_path) -> None:
    from hermes_fleet.node_service import run_node_service

    class WrongNode(_Node):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.peer_id = "wrong-peer"

    cards = []
    created = []

    def node_factory(**kwargs):
        node = WrongNode(**kwargs)
        created.append(node)
        return node

    with pytest.raises(RuntimeError, match="does not match configured Fleet peer"):
        asyncio.run(
            run_node_service(
                _runtime(tmp_path),
                card_factory=_card_factory(cards),
                node_factory=node_factory,
                shutdown=asyncio.Event(),
                hermes_factory=_Hermes,
            )
        )

    node = created[0]
    assert node.stopped is True
    assert node.registration is None


def test_fleet_node_service_advertises_direct_only_when_runs_are_unavailable(
    tmp_path,
) -> None:
    from hermes_fleet.node_service import operation_specs, run_node_service

    cards = []
    created = []

    def node_factory(**kwargs):
        node = _Node(**kwargs)
        created.append(node)
        return node

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            _runtime(tmp_path),
            card_factory=_card_factory(cards),
            node_factory=node_factory,
            shutdown=shutdown,
            hermes_factory=lambda **kwargs: _Hermes(healthy=False, **kwargs),
            hermes_readiness_waiter=_fast_hermes_readiness,
        )

    asyncio.run(exercise())

    skill_ids = [skill.id for skill in cards[0].skills]
    protocol_features = cards[0].protocol_features
    assert skill_ids == [
        "fleet.health",
        "fleet.inventory",
        "fleet.message",
    ]
    assert protocol_features == [
        "absolute_deadlines_v1",
        "result_artifact_bytes_v1",
    ]
    assert len(protocol_features) == len(set(protocol_features))
    operation_ids = {operation for operation, _description in operation_specs()}
    assert operation_ids.isdisjoint(protocol_features)
    assert created[0].registration == 300


def test_fleet_node_service_advertises_observation_protocol_feature(
    tmp_path,
) -> None:
    from dataclasses import replace

    from hermes_fleet.node_service import operation_specs, run_node_service

    cards = []
    created: list[_Node] = []

    def factory(**kwargs):
        node = _Node(**kwargs)
        created.append(node)
        return node

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            replace(
                _runtime(tmp_path),
                advertise_observation_publish=True,
            ),
            card_factory=_card_factory(cards),
            node_factory=factory,
            shutdown=shutdown,
            hermes_factory=lambda **kwargs: _Hermes(
                healthy=False,
                **kwargs,
            ),
            hermes_readiness_waiter=_fast_hermes_readiness,
        )

    asyncio.run(exercise())

    skill_ids = [skill.id for skill in cards[0].skills]
    protocol_features = cards[0].protocol_features
    assert protocol_features == [
        "absolute_deadlines_v1",
        "result_artifact_bytes_v1",
        "fleet.observation.publish.v1",
    ]
    assert len(protocol_features) == len(set(protocol_features))
    assert skill_ids == [
        "fleet.health",
        "fleet.inventory",
        "fleet.message",
    ]
    operation_ids = {operation for operation, _description in operation_specs()}
    assert operation_ids.isdisjoint(protocol_features)
    assert created[0].registration == 300


def test_observation_publish_environment_flag_is_strict() -> None:
    import pytest

    from hermes_fleet.node_service import _environment_flag

    name = "FLEET_ADVERTISE_OBSERVATION_PUBLISH"

    assert _environment_flag({}, name) is False
    assert _environment_flag({name: "0"}, name) is False
    assert _environment_flag({name: "1"}, name) is True

    for invalid in ("", "2", "true", "yes", " 1", "1 "):
        with pytest.raises(ValueError, match="must be 0 or 1"):
            _environment_flag({name: invalid}, name)


def test_direct_only_node_publishes_worker_unavailable(tmp_path) -> None:
    from dataclasses import replace

    from hermes_fleet.node_service import run_node_service

    runtime = replace(
        _runtime(tmp_path),
        observation_socket=tmp_path / "fleet.sock",
        managed_network_id="network-1",
        managed_device_id="device-1",
    )
    samples: list[dict[str, Any]] = []

    class Observer:
        def __init__(self, **_kwargs) -> None:
            pass

        def publish(self, observation: dict[str, Any]) -> str:
            samples.append(observation)
            return "recorded"

        def inspect(self) -> dict[str, Any]:
            return {}

        def admission_generation(self) -> int:
            return 1

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            runtime,
            card_factory=_card_factory([]),
            node_factory=_Node,
            shutdown=shutdown,
            hermes_factory=lambda **kwargs: _Hermes(healthy=False, **kwargs),
            hermes_readiness_waiter=_fast_hermes_readiness,
            observation_factory=Observer,
            recipe_executor_factory=lambda runtime: None,
        )

    asyncio.run(exercise())

    assert samples[0]["hermes"] == "unavailable"
    assert samples[0]["worker"] == "unavailable"
    assert samples[0]["admission_generation"] == 1


def test_remote_observation_uses_existing_node_lifecycle(tmp_path) -> None:
    from dataclasses import replace

    from hermes_fleet.node_service import run_node_service

    runtime = replace(
        _runtime(tmp_path),
        remote_observation_endpoint="https://relay.example:50052",
        remote_observation_target_peer_id="peer-katana",
        remote_observation_ca_cert_path=tmp_path / "relay-ca.pem",
        managed_network_id="network-1",
        managed_device_id="device-1",
    )
    created_publishers = []
    created_nodes = []

    class Publisher:
        def __init__(self, config) -> None:
            self.config = config
            self.samples = []
            self.closed = False
            created_publishers.append(self)

        def publish(self, observation: dict[str, Any]) -> str:
            assert not self.closed
            self.samples.append(observation)
            return "published"

        def admission_generation(self) -> int:
            return 11

        def close(self) -> None:
            self.closed = True

    def node_factory(**kwargs):
        node = _Node(**kwargs)
        node.include_controller = False
        created_nodes.append(node)
        return node

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            runtime,
            card_factory=_card_factory([]),
            node_factory=node_factory,
            shutdown=shutdown,
            hermes_factory=_Hermes,
            remote_observation_factory=Publisher,
        )

    asyncio.run(exercise())

    assert len(created_publishers) == 1
    publisher = created_publishers[0]
    assert publisher.config.relay_endpoint == "https://relay.example:50052"
    assert publisher.config.relay_ca_cert_path == tmp_path / "relay-ca.pem"
    assert publisher.config.source_peer_id == "peer-vps"
    assert publisher.config.target_peer_id == "peer-katana"
    assert publisher.config.network_id == "network-1"
    assert publisher.config.device_id == "device-1"
    assert len(publisher.samples) == 2
    assert publisher.samples[0]["admission_generation"] == 11
    assert publisher.samples[0]["network"] == "reachable"
    assert publisher.samples[0]["keryx"] == "available"
    assert publisher.samples[0]["worker"] == "available"
    assert publisher.samples[1]["worker"] == "unavailable"
    assert publisher.closed is True


def test_mirrored_observation_updates_local_and_remote_authority() -> None:
    from hermes_fleet.node_service import (
        _build_observation_publisher,
        _MirroredObservationPublisher,
    )

    class Publisher:
        def __init__(
            self, generation: int, *, publish_error: Exception | None = None
        ) -> None:
            self.generation = generation
            self.publish_error = publish_error
            self.samples = []
            self.closed = False

        def publish(self, observation: dict[str, Any]) -> str:
            if self.publish_error is not None:
                raise self.publish_error
            self.samples.append(observation)
            return "published"

        def admission_generation(self) -> int:
            return self.generation

        def close(self) -> None:
            self.closed = True

    local = Publisher(11)
    remote = Publisher(11)
    publisher = _MirroredObservationPublisher(local=local, remote=remote)
    sample = {"worker": "available", "admission_generation": 11}

    assert publisher.admission_generation() == 11
    assert publisher.publish(sample) == "published"
    publisher.close()

    assert local.samples == [sample]
    assert remote.samples == [sample]
    assert local.closed is True
    assert remote.closed is True

    mismatched = _MirroredObservationPublisher(
        local=Publisher(11), remote=Publisher(12)
    )
    with pytest.raises(RuntimeError, match="admission generations differ"):
        mismatched.admission_generation()

    local_failure = Publisher(11, publish_error=RuntimeError("local failed"))
    remote_not_called = Publisher(11)
    with pytest.raises(RuntimeError, match="local failed"):
        _MirroredObservationPublisher(
            local=local_failure, remote=remote_not_called
        ).publish(sample)
    assert remote_not_called.samples == []

    local_succeeded = Publisher(11)
    remote_failure = Publisher(11, publish_error=RuntimeError("remote failed"))
    with pytest.raises(RuntimeError, match="remote failed"):
        _MirroredObservationPublisher(
            local=local_succeeded, remote=remote_failure
        ).publish(sample)
    assert local_succeeded.samples == [sample]

    from dataclasses import replace

    root = Path("/tmp/fleet-observation-selection")
    local_runtime = replace(
        _runtime(root),
        observation_socket=root / "observation.sock",
        managed_network_id="network-1",
        managed_device_id="device-1",
    )
    remote_runtime = replace(
        _runtime(root),
        remote_observation_endpoint="https://relay.example:50052",
        remote_observation_target_peer_id="peer-katana",
        remote_observation_ca_cert_path=root / "ca.pem",
        managed_network_id="network-1",
        managed_device_id="device-1",
    )
    local_created = []
    remote_created = []

    def local_factory(**kwargs):
        value = Publisher(11)
        local_created.append((kwargs, value))
        return value

    def remote_factory(config):
        value = Publisher(11)
        remote_created.append((config, value))
        return value

    assert (
        _build_observation_publisher(
            local_runtime,
            observation_factory=local_factory,
            remote_observation_factory=remote_factory,
        )
        is local_created[-1][1]
    )
    assert remote_created == []
    assert (
        _build_observation_publisher(
            remote_runtime,
            observation_factory=local_factory,
            remote_observation_factory=remote_factory,
        )
        is remote_created[-1][1]
    )
    mirrored = _build_observation_publisher(
        replace(remote_runtime, execution_control_socket=root / "control.sock"),
        observation_factory=local_factory,
        remote_observation_factory=remote_factory,
    )
    assert isinstance(mirrored, _MirroredObservationPublisher)


def test_remote_observation_acquire_failure_publishes_no_fresh_sample(tmp_path) -> None:
    from dataclasses import replace

    from hermes_fleet.node_service import run_node_service

    runtime = replace(
        _runtime(tmp_path),
        remote_observation_endpoint="https://relay.example:50052",
        remote_observation_target_peer_id="peer-katana",
        remote_observation_ca_cert_path=tmp_path / "relay-ca.pem",
        managed_network_id="network-1",
        managed_device_id="device-1",
    )
    samples: list[dict[str, Any]] = []

    class Publisher:
        def __init__(self, _config) -> None:
            pass

        def admission_generation(self) -> int:
            raise RuntimeError("exact controller acquire failed")

        def publish(self, observation: dict[str, Any]) -> str:
            samples.append(observation)
            return "published"

        def close(self) -> None:
            pass

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            runtime,
            card_factory=_card_factory([]),
            node_factory=_Node,
            shutdown=shutdown,
            hermes_factory=_Hermes,
            remote_observation_factory=Publisher,
        )

    asyncio.run(exercise())
    assert samples == []


def test_remote_observation_requires_complete_tls_configuration(tmp_path) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="configuration must be complete"):
        replace(
            _runtime(tmp_path),
            remote_observation_endpoint="https://relay.example:50052",
            remote_observation_target_peer_id="peer-katana",
            managed_network_id="network-1",
            managed_device_id="device-1",
        )

    with pytest.raises(ValueError, match="CA certificate path"):
        replace(
            _runtime(tmp_path),
            remote_observation_endpoint="https://relay.example:50052",
            remote_observation_target_peer_id="peer-katana",
            remote_observation_ca_cert_path=Path("relative-ca.pem"),
            managed_network_id="network-1",
            managed_device_id="device-1",
        )

    runtime = replace(
        _runtime(tmp_path),
        remote_observation_ca_cert_path=tmp_path / "relay-ca.pem",
    )
    assert runtime.remote_observation_endpoint is None
    assert runtime.observation_socket is None


def test_node_runtime_config_redacts_secrets_from_repr(tmp_path) -> None:
    rendered = repr(_runtime(tmp_path))

    assert "test-api-key" not in rendered
    assert "test-node-token" not in rendered


def test_fleet_node_service_publishes_initial_scheduler_observation(tmp_path) -> None:
    from dataclasses import replace

    from hermes_fleet.node_service import run_node_service

    runtime = replace(
        _runtime(tmp_path),
        observation_socket=tmp_path / "fleet.sock",
        managed_network_id="network-1",
        managed_device_id="device-1",
    )
    created_nodes = []
    created_observers = []

    class Observer:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.samples = []
            created_observers.append(self)

        def publish(self, sample) -> str:
            self.samples.append(sample)
            return "recorded"

        def inspect(self) -> dict[str, Any]:
            return {
                "managed_state": "active",
                "alive": True,
                "fresh": True,
                "scheduler_ready": True,
                "reasons": [],
            }

        def admission_generation(self) -> int:
            return 7

    def node_factory(**kwargs):
        node = _Node(**kwargs)
        created_nodes.append(node)
        return node

    async def exercise() -> None:
        shutdown = asyncio.Event()
        shutdown.set()
        await run_node_service(
            runtime,
            card_factory=_card_factory([]),
            node_factory=node_factory,
            shutdown=shutdown,
            hermes_factory=_Hermes,
            observation_factory=Observer,
            recipe_executor_factory=lambda runtime: None,
        )

    asyncio.run(exercise())

    observer = created_observers[0]
    assert observer.kwargs == {
        "socket_path": tmp_path / "fleet.sock",
        "network_id": "network-1",
        "device_id": "device-1",
    }
    assert len(observer.samples) == 2
    assert observer.samples[0]["admission_generation"] == 7
    assert observer.samples[1]["admission_generation"] == 7
    assert observer.samples[0]["network"] == "reachable"
    assert observer.samples[0]["keryx"] == "available"
    assert observer.samples[0]["hermes"] == "available"
    assert observer.samples[0]["worker"] == "available"
    assert observer.samples[0]["capacity"] == {
        "active_workers": 0,
        "max_workers": 1,
    }
    assert observer.samples[1]["network"] == "unreachable"
    assert observer.samples[1]["keryx"] == "unavailable"
    assert observer.samples[1]["worker"] == "unavailable"
    assert created_nodes[0].stopped is True


def test_observation_loop_refreshes_periodically_and_on_capacity_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from hermes_fleet import node_service

    samples: list[dict[str, Any]] = []

    class Observer:
        def publish(self, observation: dict[str, Any]) -> str:
            samples.append(observation)
            return "recorded"

        def inspect(self) -> dict[str, Any]:
            return {}

        def admission_generation(self) -> int:
            return 7

    class Worker:
        observed_active_worker_count = 0

    def build_observation(**fields):
        return {
            "network": fields["network_reachable"],
            "keryx": fields["keryx_available"],
            "capacity": fields["active_workers"],
        }

    monkeypatch.setattr(node_service, "build_observation", build_observation)

    async def exercise() -> None:
        node = _Node(card=object(), node_token="token", worker_concurrency=1)
        worker = Worker()
        capacity_updates: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        shutdown = asyncio.Event()
        task = asyncio.create_task(
            node_service._observation_loop(
                Observer(),
                node,
                ("peer-controller",),
                _Hermes(),
                worker,
                capacity_updates,
                shutdown,
                0.01,
                True,
                False,
            )
        )

        async def wait_for_samples(count: int) -> None:
            while len(samples) < count:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_samples(1), timeout=1)
        worker.observed_active_worker_count = 1
        capacity_updates.put_nowait(None)
        await asyncio.wait_for(wait_for_samples(2), timeout=1)
        shutdown.set()
        if not capacity_updates.full():
            capacity_updates.put_nowait(None)
        await task

    asyncio.run(exercise())

    assert samples == [
        {"network": True, "keryx": True, "capacity": 0},
        {"network": True, "keryx": True, "capacity": 1},
    ]


def test_node_runtime_config_requires_complete_observation_identity(tmp_path) -> None:
    from dataclasses import replace

    with pytest.raises(ValueError, match="observation configuration"):
        replace(_runtime(tmp_path), observation_socket=tmp_path / "fleet.sock")
