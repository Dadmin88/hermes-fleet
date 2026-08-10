use fleet_domain::{
    Availability, NodeObservation, Reachability, ReadinessPolicy, ResourceObservation,
    WorkerCapacity, canonical_projection_hash,
};
use fleet_state::FleetStateStore;
use serde_json::{Value, json};
use tempfile::tempdir;

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

#[test]
fn persisted_observation_without_profiles_remains_readable_as_empty_presence() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    store.apply_projection(projection()).unwrap();
    store
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            NodeObservation {
                admission_generation: 1,
                observed_at_ms: 1_000,
                network: Reachability::Reachable,
                keryx: Availability::Available,
                hermes: Availability::Available,
                worker: Availability::Available,
                capacity: WorkerCapacity {
                    active_workers: 0,
                    max_workers: 1,
                },
                profiles: Vec::new(),
                resources: ResourceObservation::default(),
            },
            1_100,
        )
        .unwrap();

    // Simulate the exact persisted JSON shape produced before profile presence
    // existed by removing only the newly defaulted `profiles` member.
    let connection = rusqlite::Connection::open(&path).unwrap();
    let stored: String = connection
        .query_row(
            "SELECT observation_json FROM node_observations
             WHERE source = 'nodescale' AND network_id = 'network-1'
             AND device_id = 'device-1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let mut value: Value = serde_json::from_str(&stored).unwrap();
    value.as_object_mut().unwrap().remove("profiles");
    connection
        .execute(
            "UPDATE node_observations SET observation_json = ?1
             WHERE source = 'nodescale' AND network_id = 'network-1'
             AND device_id = 'device-1'",
            [serde_json::to_string(&value).unwrap()],
        )
        .unwrap();
    drop(connection);

    let inspected = store
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            1_200,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    assert!(inspected.readiness.scheduler_ready);
    assert!(
        inspected
            .observation
            .unwrap()
            .observation
            .profiles
            .is_empty()
    );

    assert!(
        store
            .find_profile_candidates(
                "agency-backend-engineer",
                None,
                1_200,
                ReadinessPolicy::new(1_000).unwrap(),
            )
            .unwrap()
            .is_empty()
    );
}
