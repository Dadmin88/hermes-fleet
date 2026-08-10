use fleet_domain::{NodeObservation, ReadinessPolicy, canonical_projection_hash};
use fleet_state::FleetStateStore;
use serde_json::json;
use tempfile::tempdir;

const DIGEST: &str = "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f";

fn projection() -> fleet_domain::ProjectionDocument {
    let mut document: fleet_domain::ProjectionDocument = serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": "device-1",
        "projection_generation": "1",
        "membership_generation": "1",
        "binding_generation": "1",
        "content_hash": "",
        "operation": "upsert",
        "generated_operations": ["fleet.health", "fleet.inventory", "fleet.message"],
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "snapshot": "1",
            "controller": "nodescale"
        }
    }))
    .unwrap();
    document.content_hash = canonical_projection_hash(&document);
    document
}

fn observation(profile: serde_json::Value) -> NodeObservation {
    serde_json::from_value(json!({
        "admission_generation": 1,
        "observed_at_ms": 1_000,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 1},
        "profiles": [profile],
        "resources": {}
    }))
    .unwrap()
}

#[test]
fn exact_profile_digest_survives_persistence_and_restart() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    {
        let store = FleetStateStore::open(&path).unwrap();
        store.apply_projection(projection()).unwrap();
        let sample = observation(json!({
            "name": "agency-backend-engineer",
            "version": "0.1.0",
            "content_digest": DIGEST
        }));
        assert!(sample.validate().is_ok());
        store
            .record_observation(
                "nodescale",
                "network-1",
                "device-1",
                sample,
                1_100,
            )
            .unwrap();
    }

    let restarted = FleetStateStore::open(&path).unwrap();
    let view = restarted
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            1_200,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    let profile = &view.observation.unwrap().observation.profiles[0];
    assert_eq!(profile.name, "agency-backend-engineer");
    assert_eq!(profile.version, "0.1.0");
    assert_eq!(profile.content_digest.as_deref(), Some(DIGEST));
}

#[test]
fn digestless_profile_presence_remains_canonical_after_restart() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    {
        let store = FleetStateStore::open(&path).unwrap();
        store.apply_projection(projection()).unwrap();
        let sample = observation(json!({
            "name": "agency-backend-engineer",
            "version": "0.1.0"
        }));
        assert!(sample.validate().is_ok());
        let serialized = serde_json::to_value(&sample).unwrap();
        assert!(serialized["profiles"][0].get("content_digest").is_none());
        store
            .record_observation(
                "nodescale",
                "network-1",
                "device-1",
                sample,
                1_100,
            )
            .unwrap();
    }

    let restarted = FleetStateStore::open(&path).unwrap();
    let view = restarted
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            1_200,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    let profile = &view.observation.unwrap().observation.profiles[0];
    assert_eq!(profile.content_digest, None);
}
