"""Reusable Keryx direct-control publisher for Fleet observations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import grpc
from keryx.proto.hermes.keryx.v1 import control_pb2, relay_pb2, relay_pb2_grpc

_MAX_SAMPLE_BYTES = 32_768
_RPC_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class RemoteObservationConfig:
    relay_endpoint: str
    source_peer_id: str
    node_token: str
    target_peer_id: str
    network_id: str
    device_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("relay_endpoint", self.relay_endpoint),
            ("source_peer_id", self.source_peer_id),
            ("node_token", self.node_token),
            ("target_peer_id", self.target_peer_id),
            ("network_id", self.network_id),
            ("device_id", self.device_id),
        ):
            if type(value) is not str or not value or value.strip() != value:
                raise ValueError(f"{name} must be nonempty and trimmed")


class RemoteObservationPublisher:
    """Publish through Keryx while the existing fleet-node owns scheduling."""

    def __init__(
        self,
        config: RemoteObservationConfig,
        *,
        channel: grpc.Channel | None = None,
    ) -> None:
        if type(config) is not RemoteObservationConfig:
            raise ValueError("config must be RemoteObservationConfig")
        self._config = config
        self._channel = channel or grpc.insecure_channel(config.relay_endpoint)
        self._owns_channel = channel is None
        self._stub = relay_pb2_grpc.KeryxRelayStub(self._channel)
        self._epoch: Any | None = None

    def close(self) -> None:
        if self._owns_channel:
            self._channel.close()

    def admission_generation(self) -> int:
        epoch = self._acquire()
        return int(epoch.projection_generation)

    def publish(self, observation: dict[str, Any]) -> str:
        encoded = json.dumps(
            observation, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if not encoded or len(encoded) > _MAX_SAMPLE_BYTES:
            raise ValueError("remote observation payload is outside the bounded frame")
        epoch = self._epoch or self._acquire()
        outcome = self._publish(encoded, epoch)
        if outcome == "rejected":
            epoch = self._acquire()
            outcome = self._publish(encoded, epoch)
        if outcome == "rejected":
            raise RuntimeError("remote observation authority was rejected")
        return outcome

    def _selector(self) -> Any:
        return control_pb2.FleetObservationSelectorV1(
            source="nodescale",
            network_id=self._config.network_id,
            device_id=self._config.device_id,
        )

    def _metadata(self) -> tuple[tuple[str, str], ...]:
        return (
            ("x-keryx-node-id", self._config.source_peer_id),
            ("x-keryx-node-token", self._config.node_token),
        )

    def _call(self, operation: Any) -> Any:
        request = relay_pb2.PublishFleetObservationRequest(
            operation=operation,
            target_node_id=self._config.target_peer_id,
            deadline_ms=int(time.time() * 1_000) + 8_000,
        )
        response = self._stub.PublishFleetObservation(
            request, metadata=self._metadata(), timeout=_RPC_TIMEOUT_SECONDS
        )
        if not response.HasField("result"):
            raise RuntimeError("Keryx returned no Fleet observation result")
        return response.result

    def _acquire(self) -> Any:
        result = self._call(
            control_pb2.FleetObservationPublishV1(
                acquire=control_pb2.FleetObservationAcquireV1(selector=self._selector())
            )
        )
        if (
            not result.accepted
            or result.disposition
            != control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_ACQUIRED
            or not result.HasField("authority_epoch")
        ):
            raise RuntimeError("remote observation authority acquisition was rejected")
        self._epoch = result.authority_epoch
        return self._epoch

    def _publish(self, encoded: bytes, epoch: Any) -> str:
        result = self._call(
            control_pb2.FleetObservationPublishV1(
                publish=control_pb2.FleetObservationSampleV1(
                    selector=self._selector(),
                    authority_epoch=epoch,
                    observation_json=encoded,
                )
            )
        )
        dispositions = {
            control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_PUBLISHED: "published",
            control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_ALREADY_RECORDED: (
                "already_recorded"
            ),
            control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_REJECTED: "rejected",
        }
        outcome = dispositions.get(result.disposition)
        if outcome is None or result.accepted != (outcome != "rejected"):
            raise RuntimeError(
                "Keryx returned an invalid Fleet observation disposition"
            )
        return outcome
