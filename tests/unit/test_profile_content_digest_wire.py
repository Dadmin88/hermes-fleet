from __future__ import annotations

import copy

import pytest

from hermes_fleet.observation import normalize_readiness

DIGEST = "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f"


def _readiness(profile: dict[str, object]) -> dict[str, object]:
    return {
        "managed_state": "active",
        "admission_generation": 7,
        "alive": True,
        "fresh": True,
        "scheduler_ready": True,
        "observation_age_ms": 10,
        "reasons": [],
        "last_observation": {
            "admission_generation": 7,
            "observed_at_ms": 1_000,
            "received_at_ms": 1_001,
            "network": "reachable",
            "keryx": "available",
            "hermes": "available",
            "worker": "available",
        },
        "capacity": {
            "active_workers": 0,
            "max_workers": 1,
            "available_worker_slots": 1,
        },
        "profiles": [profile],
        "resources": {
            "cpu": None,
            "ram": None,
            "swap": None,
            "disk": None,
            "gpu": None,
        },
    }


def test_readiness_normalizer_preserves_exact_profile_digest() -> None:
    value = _readiness(
        {
            "name": "agency-backend-engineer",
            "version": "0.1.0",
            "content_digest": DIGEST,
        }
    )

    assert normalize_readiness(value)["profiles"] == value["profiles"]


def test_readiness_normalizer_preserves_legacy_digestless_profile() -> None:
    value = _readiness(
        {
            "name": "agency-backend-engineer",
            "version": "0.1.0",
        }
    )

    normalized = normalize_readiness(value)
    assert normalized["profiles"] == [
        {"name": "agency-backend-engineer", "version": "0.1.0"}
    ]


@pytest.mark.parametrize(
    "digest",
    [
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        123,
    ],
)
def test_readiness_normalizer_rejects_malformed_profile_digest(digest) -> None:
    value = _readiness(
        {
            "name": "agency-backend-engineer",
            "version": "0.1.0",
            "content_digest": digest,
        }
    )

    with pytest.raises(ValueError, match="profile presence identity"):
        normalize_readiness(value)


def test_readiness_normalizer_rejects_unknown_profile_field() -> None:
    value = _readiness(
        {
            "name": "agency-backend-engineer",
            "version": "0.1.0",
            "content_digest": DIGEST,
        }
    )
    malformed = copy.deepcopy(value)
    malformed["profiles"][0]["source"] = "unexpected"

    with pytest.raises(ValueError, match="profile presence fields"):
        normalize_readiness(malformed)
