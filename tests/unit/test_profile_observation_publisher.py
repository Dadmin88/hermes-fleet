from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

DIGEST = "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f"


def _health() -> dict[str, object]:
    return {
        "api": "healthy",
        "run_submission": True,
        "run_status": True,
        "run_stop": True,
        "run_finalize": True,
    }


def test_build_observation_includes_explicit_canonical_profiles(monkeypatch) -> None:
    from hermes_fleet import observation

    monkeypatch.setattr(observation, "linux_resources", lambda: {})
    profiles = [
        {"name": "agency-ai-engineer", "version": "0.1.0"},
        {
            "name": "agency-backend-engineer",
            "version": "0.1.0",
            "content_digest": DIGEST,
        },
    ]

    sample = observation.build_observation(
        admission_generation=7,
        hermes_health=_health(),
        active_workers=0,
        max_workers=1,
        now_ms=lambda: 5_000,
        network_reachable=True,
        keryx_available=True,
        worker_available=True,
        profiles=profiles,
    )

    assert sample["profiles"] == profiles
    assert sample["resources"] == {}


def test_build_observation_preserves_legacy_shape_when_profiles_are_omitted(
    monkeypatch,
) -> None:
    from hermes_fleet import observation

    monkeypatch.setattr(observation, "linux_resources", lambda: {})
    sample = observation.build_observation(
        admission_generation=7,
        hermes_health=_health(),
        active_workers=0,
        max_workers=1,
        now_ms=lambda: 5_000,
        network_reachable=True,
        keryx_available=True,
        worker_available=True,
    )

    assert "profiles" not in sample


@pytest.mark.parametrize(
    "profiles",
    [
        [{"name": "agency-backend", "version": "0.1.0"}] * 2,
        [
            {"name": "agency-backend", "version": "0.1.0"},
            {"name": "agency-ai", "version": "0.1.0"},
        ],
        [{"name": "agency backend", "version": "0.1.0"}],
        [{"name": "agency-backend", "version": "0.1 0"}],
        [{"name": "agency-backend", "version": "0.1.0", "extra": True}],
        [
            {
                "name": "agency-backend",
                "version": "0.1.0",
                "content_digest": "a" * 63,
            }
        ],
        [
            {
                "name": "agency-backend",
                "version": "0.1.0",
                "content_digest": "A" * 64,
            }
        ],
    ],
)
def test_build_observation_rejects_noncanonical_profile_inventory(
    monkeypatch,
    profiles,
) -> None:
    from hermes_fleet import observation

    monkeypatch.setattr(observation, "linux_resources", lambda: {})
    with pytest.raises(ValueError, match="profile"):
        observation.build_observation(
            admission_generation=7,
            hermes_health=_health(),
            active_workers=0,
            max_workers=1,
            now_ms=lambda: 5_000,
            network_reachable=True,
            keryx_available=True,
            worker_available=True,
            profiles=profiles,
        )


def test_publish_observation_scans_builds_and_publishes_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_fleet import node_service

    event_loop_thread = threading.get_ident()
    worker_threads: list[int] = []
    expected_profiles = [
        {
            "name": "agency-backend-engineer",
            "version": "0.1.0",
            "content_digest": DIGEST,
        }
    ]

    def scan() -> list[dict[str, str]]:
        worker_threads.append(threading.get_ident())
        return expected_profiles

    def build(**fields) -> dict[str, object]:
        worker_threads.append(threading.get_ident())
        assert fields["profiles"] == expected_profiles
        return {"profiles": fields["profiles"]}

    class Observer:
        def publish(self, sample: dict[str, Any]) -> str:
            worker_threads.append(threading.get_ident())
            assert sample == {"profiles": expected_profiles}
            return "recorded"

    monkeypatch.setattr(node_service, "scan_profile_distributions", scan)
    monkeypatch.setattr(node_service, "build_observation", build)

    asyncio.run(
        node_service._publish_observation(
            Observer(),
            _health(),
            0,
            admission_generation=7,
            network_reachable=True,
            keryx_available=True,
            worker_available=True,
        )
    )

    assert len(worker_threads) == 3
    assert all(thread_id != event_loop_thread for thread_id in worker_threads)


def test_profile_inventory_failure_fails_observation_publish_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_fleet import node_service
    from hermes_fleet.profile_inventory import ProfileInventoryError

    published: list[dict[str, Any]] = []

    def fail_scan() -> list[dict[str, str]]:
        raise ProfileInventoryError("ambiguous local profile inventory")

    class Observer:
        def publish(self, sample: dict[str, Any]) -> str:
            published.append(sample)
            return "recorded"

    monkeypatch.setattr(node_service, "scan_profile_distributions", fail_scan)

    asyncio.run(
        node_service._publish_observation(
            Observer(),
            _health(),
            0,
            admission_generation=7,
            network_reachable=True,
            keryx_available=True,
            worker_available=True,
        )
    )

    assert published == []
