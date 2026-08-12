from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
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


def _config(ca_cert_path: Path) -> RemoteObservationConfig:
    return RemoteObservationConfig(
        relay_endpoint="https://relay.example:50052",
        relay_ca_cert_path=ca_cert_path,
        source_peer_id="peer-vps",
        node_token="node-token",
        target_peer_id="peer-katana",
        network_id="network-1",
        device_id="device-1",
    )


def test_remote_publisher_uses_secure_channel_with_configured_trust(
    monkeypatch, tmp_path
) -> None:
    ca_cert = tmp_path / "relay-ca.pem"
    ca_cert.write_bytes(b"test-ca-pem")
    credentials = object()
    channel = _Channel()
    calls = []

    monkeypatch.setattr(
        "hermes_fleet.remote_observation.grpc.ssl_channel_credentials",
        lambda *, root_certificates: (
            calls.append(("credentials", root_certificates)) or credentials
        ),
    )
    monkeypatch.setattr(
        "hermes_fleet.remote_observation.grpc.secure_channel",
        lambda target, supplied_credentials: (
            calls.append(("secure", target, supplied_credentials)) or channel
        ),
    )
    monkeypatch.setattr(
        "hermes_fleet.remote_observation.grpc.insecure_channel",
        lambda *_args, **_kwargs: pytest.fail("plaintext fallback is forbidden"),
    )
    monkeypatch.setattr(
        "hermes_fleet.remote_observation.relay_pb2_grpc.KeryxRelayStub",
        lambda supplied_channel: SimpleNamespace(channel=supplied_channel),
    )

    publisher = RemoteObservationPublisher(_config(ca_cert))

    assert calls == [
        ("credentials", b"test-ca-pem"),
        ("secure", "relay.example:50052", credentials),
    ]
    publisher.close()


def test_remote_publisher_requires_https_and_trust_material(tmp_path) -> None:
    ca_cert = tmp_path / "relay-ca.pem"
    ca_cert.write_text("test-ca")

    with pytest.raises(ValueError, match="https"):
        replace(_config(ca_cert), relay_endpoint="relay.example:50052")

    with pytest.raises(ValueError, match="CA certificate"):
        RemoteObservationConfig(
            relay_endpoint="https://relay.example:50052",
            relay_ca_cert_path=None,
            source_peer_id="peer-vps",
            node_token="node-token",
            target_peer_id="peer-katana",
            network_id="network-1",
            device_id="device-1",
        )


def test_remote_publisher_missing_trust_file_fails_before_channel_creation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "hermes_fleet.remote_observation.grpc.secure_channel",
        lambda *_args, **_kwargs: pytest.fail("channel must not be created"),
    )

    with pytest.raises(ValueError, match="read relay CA certificate"):
        RemoteObservationPublisher(_config(tmp_path / "missing-ca.pem"))


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
            relay_endpoint="https://relay.example:50052",
            relay_ca_cert_path=Path("/unused/injected-channel-ca.pem"),
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
