use fleet_domain::{
    Availability, NodeObservation, ProfilePresence, Reachability, ReadinessPolicy,
    ResourceObservation, WorkerCapacity, canonical_projection_hash,
};
use fleet_state::{FleetStateStore, StateError};
use serde_json::{Value, json};
use tempfile::tempdir;

fn projection(
    device_id: &str,
    operation: &str,
    generation: u64,
) -> fleet_domain::ProjectionDocument {
    let mut document: fleet_domain::ProjectionDocument = serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": device_id,
        "projection_generation": generation.to_string(),
        "membership_generation": generation.to_string(),
        "binding_generation": generation.to_string(),
        "content_hash": "",
        "operation": operation,
        "generated_operations": if operation == "upsert" {
            json!(["fleet.health", "fleet.inventory", "fleet.message"])
        } else {
            json!([])
        },
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": device_id,
            "snapshot": generation.to_string(),
            "controller": "nodescale"
        }
    }))
    .unwrap();
    document.content_hash = canonical_projection_hash(&document);
    document
}

fn profile(name: &str, version: &str) -> ProfilePresence {
    ProfilePresence {
        name: name.to_owned(),
        version: version.to_owned(),
        content_digest: None,
    }
}

// Test-only constructor keeps each readiness layer explicit so the exclusion
// matrix below shows which single layer made a candidate ineligible.
#[allow(clippy::too_many_arguments)]
fn observation(
    admission_generation: u64,
    observed_at_ms: u64,
    active_workers: u32,
    max_workers: u32,
    network: Reachability,
    keryx: Availability,
    hermes: Availability,
    worker: Availability,
    profiles: Vec<ProfilePresence>,
) -> NodeObservation {
    NodeObservation {
        admission_generation,
        observed_at_ms,
        network,
        keryx,
        hermes,
        worker,
        capacity: WorkerCapacity {
            active_workers,
            max_workers,
        },
        profiles,
        resources: ResourceObservation::default(),
    }
}

fn ready_observation(
    observed_at_ms: u64,
    active_workers: u32,
    profile_name: &str,
    version: &str,
) -> NodeObservation {
    observation(
        1,
        observed_at_ms,
        active_workers,
        2,
        Reachability::Reachable,
        Availability::Available,
        Availability::Available,
        Availability::Available,
        vec![profile(profile_name, version)],
    )
}

fn add_active_node(store: &FleetStateStore, device_id: &str) {
    store
        .apply_projection(projection(device_id, "upsert", 1))
        .unwrap();
}

fn record(store: &FleetStateStore, device_id: &str, sample: NodeObservation, received_at_ms: u64) {
    store
        .record_observation("nodescale", "network-1", device_id, sample, received_at_ms)
        .unwrap();
}

fn policy() -> ReadinessPolicy {
    ReadinessPolicy::new(1_000).unwrap()
}

#[test]
fn returns_all_ready_matching_nodes_in_deterministic_identity_order() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();

    // Insert in reverse lexical order to prove query order is state-defined.
    for device_id in ["device-b", "device-a"] {
        add_active_node(&store, device_id);
        record(
            &store,
            device_id,
            ready_observation(1_000, 0, "agency-backend-engineer", "0.1.0"),
            1_100,
        );
    }

    let candidates = store
        .find_profile_candidates("agency-backend-engineer", None, 1_200, policy())
        .unwrap();

    assert_eq!(candidates.len(), 2);
    assert_eq!(candidates[0].source, "nodescale");
    assert_eq!(candidates[0].network_id, "network-1");
    assert_eq!(candidates[0].device_id, "device-a");
    assert_eq!(candidates[1].device_id, "device-b");
    assert!(candidates.iter().all(|candidate| {
        candidate.profile_version == "0.1.0"
            && candidate.available_worker_slots == 2
            && candidate.readiness.scheduler_ready
    }));
}

#[test]
fn filters_by_exact_version_without_substituting_another_profile() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();

    add_active_node(&store, "backend-old");
    record(
        &store,
        "backend-old",
        ready_observation(1_000, 0, "agency-backend-engineer", "0.1.0"),
        1_100,
    );
    add_active_node(&store, "backend-new");
    record(
        &store,
        "backend-new",
        ready_observation(1_000, 0, "agency-backend-engineer", "0.2.0"),
        1_100,
    );
    add_active_node(&store, "frontend");
    record(
        &store,
        "frontend",
        ready_observation(1_000, 0, "agency-frontend-engineer", "0.2.0"),
        1_100,
    );

    let any_version = store
        .find_profile_candidates("agency-backend-engineer", None, 1_200, policy())
        .unwrap();
    assert_eq!(
        any_version
            .iter()
            .map(|candidate| candidate.device_id.as_str())
            .collect::<Vec<_>>(),
        ["backend-new", "backend-old"]
    );

    let exact = store
        .find_profile_candidates("agency-backend-engineer", Some("0.2.0"), 1_200, policy())
        .unwrap();
    assert_eq!(exact.len(), 1);
    assert_eq!(exact[0].device_id, "backend-new");
    assert_eq!(exact[0].profile_version, "0.2.0");

    assert!(
        store
            .find_profile_candidates("agency-backend-engineer", Some("9.9.9"), 1_200, policy(),)
            .unwrap()
            .is_empty()
    );
}

#[test]
fn excludes_nodes_that_are_not_currently_scheduler_ready() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let profile_name = "agency-backend-engineer";

    add_active_node(&store, "good");
    record(
        &store,
        "good",
        ready_observation(2_000, 1, profile_name, "0.1.0"),
        2_100,
    );

    add_active_node(&store, "stale");
    record(
        &store,
        "stale",
        ready_observation(1_000, 0, profile_name, "0.1.0"),
        1_100,
    );

    add_active_node(&store, "unreachable");
    record(
        &store,
        "unreachable",
        observation(
            1,
            2_000,
            0,
            2,
            Reachability::Unreachable,
            Availability::Available,
            Availability::Available,
            Availability::Available,
            vec![profile(profile_name, "0.1.0")],
        ),
        2_100,
    );

    add_active_node(&store, "keryx-down");
    record(
        &store,
        "keryx-down",
        observation(
            1,
            2_000,
            0,
            2,
            Reachability::Reachable,
            Availability::Unavailable,
            Availability::Available,
            Availability::Available,
            vec![profile(profile_name, "0.1.0")],
        ),
        2_100,
    );

    add_active_node(&store, "hermes-down");
    record(
        &store,
        "hermes-down",
        observation(
            1,
            2_000,
            0,
            2,
            Reachability::Reachable,
            Availability::Available,
            Availability::Unavailable,
            Availability::Available,
            vec![profile(profile_name, "0.1.0")],
        ),
        2_100,
    );

    add_active_node(&store, "worker-down");
    record(
        &store,
        "worker-down",
        observation(
            1,
            2_000,
            0,
            2,
            Reachability::Reachable,
            Availability::Available,
            Availability::Available,
            Availability::Unavailable,
            vec![profile(profile_name, "0.1.0")],
        ),
        2_100,
    );

    add_active_node(&store, "saturated");
    record(
        &store,
        "saturated",
        ready_observation(2_000, 2, profile_name, "0.1.0"),
        2_100,
    );

    add_active_node(&store, "missing-observation");

    add_active_node(&store, "disabled");
    record(
        &store,
        "disabled",
        ready_observation(2_000, 0, profile_name, "0.1.0"),
        2_100,
    );
    store
        .apply_projection(projection("disabled", "disable", 2))
        .unwrap();

    let candidates = store
        .find_profile_candidates(profile_name, None, 2_500, policy())
        .unwrap();
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].device_id, "good");
    assert_eq!(candidates[0].available_worker_slots, 1);
}

#[test]
fn readmission_fences_old_profile_presence() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let profile_name = "agency-backend-engineer";

    add_active_node(&store, "device-1");
    record(
        &store,
        "device-1",
        ready_observation(1_000, 0, profile_name, "0.1.0"),
        1_100,
    );
    assert_eq!(
        store
            .find_profile_candidates(profile_name, None, 1_200, policy())
            .unwrap()
            .len(),
        1
    );

    store
        .apply_projection(projection("device-1", "upsert", 2))
        .unwrap();
    assert!(
        store
            .find_profile_candidates(profile_name, None, 1_300, policy())
            .unwrap()
            .is_empty()
    );
    assert!(matches!(
        store.record_observation(
            "nodescale",
            "network-1",
            "device-1",
            ready_observation(1_250, 0, profile_name, "0.1.0"),
            1_260,
        ),
        Err(StateError::InvalidTransition(_))
    ));
}

#[test]
fn invalid_profile_queries_are_rejected() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();

    for name in ["", ".", "..", "agency backend", "agency/backend"] {
        assert!(matches!(
            store.find_profile_candidates(name, None, 1_000, policy()),
            Err(StateError::InvalidInput(_))
        ));
    }
    let too_long = "a".repeat(129);
    assert!(matches!(
        store.find_profile_candidates(&too_long, None, 1_000, policy()),
        Err(StateError::InvalidInput(_))
    ));
    for version in ["", "0.1.0 beta", "\n"] {
        assert!(matches!(
            store.find_profile_candidates(
                "agency-backend-engineer",
                Some(version),
                1_000,
                policy(),
            ),
            Err(StateError::InvalidInput(_))
        ));
    }
}

#[test]
fn candidates_survive_store_restart_without_a_new_registry() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let profile_name = "agency-backend-engineer";
    {
        let store = FleetStateStore::open(&path).unwrap();
        add_active_node(&store, "device-1");
        record(
            &store,
            "device-1",
            ready_observation(1_000, 0, profile_name, "0.1.0"),
            1_100,
        );
        assert_eq!(store.schema_version().unwrap(), 6);
    }

    let restarted = FleetStateStore::open(&path).unwrap();
    let candidates = restarted
        .find_profile_candidates(profile_name, None, 1_200, policy())
        .unwrap();
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].device_id, "device-1");
    assert_eq!(restarted.schema_version().unwrap(), 6);
}

#[test]
fn contradictory_persisted_admission_generation_fails_closed() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    let profile_name = "agency-backend-engineer";
    add_active_node(&store, "device-1");
    record(
        &store,
        "device-1",
        ready_observation(1_000, 0, profile_name, "0.1.0"),
        1_100,
    );

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
    value["admission_generation"] = json!(2);
    connection
        .execute(
            "UPDATE node_observations SET observation_json = ?1
             WHERE source = 'nodescale' AND network_id = 'network-1'
             AND device_id = 'device-1'",
            [serde_json::to_string(&value).unwrap()],
        )
        .unwrap();
    drop(connection);

    assert!(matches!(
        store.find_profile_candidates(profile_name, None, 1_200, policy()),
        Err(StateError::CorruptState(_))
    ));
}
