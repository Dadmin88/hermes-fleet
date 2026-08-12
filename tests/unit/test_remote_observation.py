from __future__ import annotations

from types import SimpleNamespace

from keryx.proto.hermes.keryx.v1 import control_pb2

from hermes_fleet.remote_observation import (
    RemoteObservationConfig,
    RemoteObservationPublisher,
)


class _Channel:
    def close(self) -> None:
        pass


class _Call:
    def __init__(self) -> None:
        self.requests = []
        self.metadata = []
        self.results = [
            SimpleNamespace(
                accepted=True,
                disposition=control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_ACQUIRED,
                authority_epoch=control_pb2.FleetObservationAuthorityEpochV1(
                    binding_id="binding-1",
                    authenticated_peer_id="peer-vps",
                    binding_generation=7,
                    projection_generation=9,
                ),
                HasField=lambda name: name == "authority_epoch",
            ),
            SimpleNamespace(
                accepted=False,
                disposition=control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_REJECTED,
                HasField=lambda _name: False,
            ),
            SimpleNamespace(
                accepted=True,
                disposition=control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_ACQUIRED,
                authority_epoch=control_pb2.FleetObservationAuthorityEpochV1(
                    binding_id="binding-2",
                    authenticated_peer_id="peer-vps",
                    binding_generation=8,
                    projection_generation=10,
                ),
                HasField=lambda name: name == "authority_epoch",
            ),
            SimpleNamespace(
                accepted=True,
                disposition=control_pb2.FLEET_OBSERVATION_PUBLISH_DISPOSITION_PUBLISHED,
                HasField=lambda _name: False,
            ),
        ]

    def __call__(self, request, *, metadata, timeout):
        self.requests.append(request)
        self.metadata.append(metadata)
        result = self.results.pop(0)
        return SimpleNamespace(result=result, HasField=lambda name: name == "result")


def test_remote_publisher_reacquires_exact_epoch_and_uses_metadata_only_sender(
    monkeypatch,
) -> None:
    call = _Call()
    monkeypatch.setattr(
        "hermes_fleet.remote_observation.relay_pb2_grpc.KeryxRelayStub",
        lambda _channel: SimpleNamespace(PublishFleetObservation=call),
    )
    publisher = RemoteObservationPublisher(
        RemoteObservationConfig(
            relay_endpoint="127.0.0.1:50052",
            source_peer_id="peer-vps",
            node_token="node-token",
            target_peer_id="peer-katana",
            network_id="network-1",
            device_id="device-1",
        ),
        channel=_Channel(),
    )

    assert publisher.admission_generation() == 9
    assert (
        publisher.publish({"admission_generation": 10, "observed_at_ms": 1})
        == "published"
    )
    assert len(call.requests) == 4
    assert all(
        metadata
        == (("x-keryx-node-id", "peer-vps"), ("x-keryx-node-token", "node-token"))
        for metadata in call.metadata
    )
    first_publish = call.requests[1].operation.publish
    second_publish = call.requests[3].operation.publish
    assert first_publish.authority_epoch.binding_id == "binding-1"
    assert second_publish.authority_epoch.binding_id == "binding-2"
    assert "peer-vps" not in first_publish.observation_json.decode()
