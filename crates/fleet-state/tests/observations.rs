use std::{
    sync::{Arc, Barrier},
    thread,
};

use fleet_domain::{
    ApplyOutcome, Availability, ManagedNodeState, NodeObservation, Reachability, ReadinessPolicy,
    ReadinessReason, ResourceObservation, WorkerCapacity, canonical_projection_hash,
};
use fleet_state::{FleetStateStore, ObservationOutcome, StateError};
use serde_json::json;
use tempfile::tempdir;

fn projection(operation: &str, generation: u64) -> fleet_domain::ProjectionDocument {
    let mut document: fleet_domain::ProjectionDocument = serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": "device-1",
        "projection_generation": generation.to_string(),
        "membership_generation": generation.to_string(),
        "binding_generation": generation.to_string(),
        "content_hash": "",
        "operation": operation,
        "generated_operations": if operation == "upsert" { json!(["fleet.health", "fleet.inventory", "fleet.message"]) } else { json!([]) },
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "snapshot": generation.to_string(),
            "controller": "nodescale"
        }
    }))
    .unwrap();
    document.content_hash = canonical_projection_hash(&document);
    document
}

fn observation(observed_at_ms: u64, active_workers: u32) -> NodeObservation {
    observation_for_generation(1, observed_at_ms, active_workers)
}

fn observation_for_generation(
    binding_generation: u64,
    observed_at_ms: u64,
    active_workers: u32,
) -> NodeObservation {
    NodeObservation {
        binding_generation,
        observed_at_ms,
        network: Reachability::Reachable,
        keryx: Availability::Available,
        hermes: Availability::Available,
        worker: Availability::Available,
        capacity: WorkerCapacity {
            active_workers,
            max_workers: 2,
        },
        resources: ResourceObservation::default(),
    }
}

fn active_store(path: &std::path::Path) -> FleetStateStore {
    let store = FleetStateStore::open(path).unwrap();
    assert_eq!(
        store
            .apply_projection(projection("upsert", 1))
            .unwrap()
            .outcome,
        ApplyOutcome::Applied
    );
    store
}

#[test]
fn first_update_restart_and_last_known_stale_observation_are_durable() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = active_store(&path);

    let first = store
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(1_000, 0),
            1_100,
        )
        .unwrap();
    assert_eq!(first.outcome, ObservationOutcome::Recorded);
    assert_eq!(first.record.received_at_ms, 1_100);

    let update = store
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(2_000, 1),
            2_100,
        )
        .unwrap();
    assert_eq!(update.outcome, ObservationOutcome::Recorded);
    drop(store);

    let restarted = FleetStateStore::open(&path).unwrap();
    let fresh = restarted
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            2_200,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    assert_eq!(fresh.managed_state, ManagedNodeState::Active);
    assert_eq!(
        fresh
            .observation
            .as_ref()
            .unwrap()
            .observation
            .observed_at_ms,
        2_000
    );
    assert_eq!(fresh.available_worker_slots, Some(1));
    assert!(fresh.readiness.scheduler_ready);

    let stale = restarted
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            3_101,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    assert!(!stale.readiness.scheduler_ready);
    assert_eq!(
        stale.readiness.reasons,
        vec![ReadinessReason::ObservationStale]
    );
    assert_eq!(stale.observation.unwrap().observation.observed_at_ms, 2_000);
}

#[test]
fn observation_identity_must_resolve_to_an_active_managed_projection() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();

    assert!(matches!(
        store.record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(1_000, 0),
            1_100,
        ),
        Err(StateError::InvalidTransition(_))
    ));
    let unknown = store
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            1_100,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    assert_eq!(unknown.managed_state, ManagedNodeState::Unknown);
    assert!(unknown.observation.is_none());

    store.apply_projection(projection("upsert", 1)).unwrap();
    store.apply_projection(projection("disable", 2)).unwrap();
    assert!(matches!(
        store.record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(2_000, 0),
            2_100,
        ),
        Err(StateError::InvalidTransition(_))
    ));
}

#[test]
fn disable_and_remove_invalidate_observation_before_readmission() {
    let temporary = tempdir().unwrap();
    for operation in ["disable", "remove"] {
        let path = temporary.path().join(format!("{operation}.sqlite3"));
        let store = active_store(&path);
        store
            .record_observation(
                "nodescale",
                "network-1",
                "device-1",
                observation(1_000, 0),
                1_100,
            )
            .unwrap();

        store.apply_projection(projection(operation, 2)).unwrap();
        let inactive = store
            .inspect_node(
                "nodescale",
                "network-1",
                "device-1",
                1_200,
                ReadinessPolicy::new(1_000).unwrap(),
            )
            .unwrap();
        assert!(inactive.observation.is_none());

        store.apply_projection(projection("upsert", 3)).unwrap();
        assert!(matches!(
            store.record_observation(
                "nodescale",
                "network-1",
                "device-1",
                observation_for_generation(1, 1_150, 0),
                1_250,
            ),
            Err(StateError::InvalidTransition(
                "observation admission generation does not match active projection"
            ))
        ));
        let readmitted = store
            .inspect_node(
                "nodescale",
                "network-1",
                "device-1",
                1_300,
                ReadinessPolicy::new(1_000).unwrap(),
            )
            .unwrap();
        assert_eq!(readmitted.managed_state, ManagedNodeState::Active);
        assert_eq!(readmitted.binding_generation, Some(3));
        assert!(readmitted.observation.is_none());
        assert_eq!(
            readmitted.readiness.reasons,
            vec![ReadinessReason::ObservationMissing]
        );
        store
            .record_observation(
                "nodescale",
                "network-1",
                "device-1",
                observation_for_generation(3, 1_260, 0),
                1_270,
            )
            .unwrap();
        assert!(
            store
                .inspect_node(
                    "nodescale",
                    "network-1",
                    "device-1",
                    1_300,
                    ReadinessPolicy::new(1_000).unwrap(),
                )
                .unwrap()
                .readiness
                .scheduler_ready
        );
    }
}

#[test]
fn active_binding_generation_change_invalidates_prior_observation() {
    let temporary = tempdir().unwrap();
    let store = active_store(&temporary.path().join("rebind.sqlite3"));
    store
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation_for_generation(1, 1_000, 0),
            1_100,
        )
        .unwrap();

    store.apply_projection(projection("upsert", 2)).unwrap();
    let rebound = store
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            1_200,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    assert_eq!(rebound.binding_generation, Some(2));
    assert!(rebound.observation.is_none());
    assert_eq!(
        rebound.readiness.reasons,
        vec![ReadinessReason::ObservationMissing]
    );
    assert!(matches!(
        store.record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation_for_generation(1, 1_050, 0),
            1_210,
        ),
        Err(StateError::InvalidTransition(_))
    ));
    store
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation_for_generation(2, 1_220, 0),
            1_230,
        )
        .unwrap();
}

#[test]
fn stale_equal_conflicting_and_invalid_updates_do_not_replace_current_state() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = active_store(&path);
    let initial = observation(2_000, 0);
    store
        .record_observation("nodescale", "network-1", "device-1", initial.clone(), 2_100)
        .unwrap();

    assert_eq!(
        store
            .record_observation("nodescale", "network-1", "device-1", initial.clone(), 2_200,)
            .unwrap()
            .outcome,
        ObservationOutcome::AlreadyRecorded
    );
    assert_eq!(
        store
            .record_observation(
                "nodescale",
                "network-1",
                "device-1",
                observation(1_999, 0),
                2_300,
            )
            .unwrap()
            .outcome,
        ObservationOutcome::Stale
    );
    assert_eq!(
        store
            .record_observation(
                "nodescale",
                "network-1",
                "device-1",
                observation(2_000, 1),
                2_300,
            )
            .unwrap()
            .outcome,
        ObservationOutcome::Conflict
    );

    let mut invalid = observation(3_000, 0);
    invalid.capacity.active_workers = 3;
    assert!(matches!(
        store.record_observation("nodescale", "network-1", "device-1", invalid, 3_100,),
        Err(StateError::InvalidInput(_))
    ));
    assert!(matches!(
        store.record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(8_000, 0),
            2_000,
        ),
        Err(StateError::InvalidInput(_))
    ));

    let current = store
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            2_500,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap()
        .observation
        .unwrap();
    assert_eq!(current.observation, initial);
    assert_eq!(current.received_at_ms, 2_100);
}

#[test]
fn current_sample_rebases_after_a_detected_wall_clock_regression() {
    let temporary = tempdir().unwrap();
    let store = active_store(&temporary.path().join("fleet.sqlite3"));
    store
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(100_000, 0),
            100_100,
        )
        .unwrap();

    let rebased = store
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(10_000, 1),
            10_100,
        )
        .unwrap();

    assert_eq!(rebased.outcome, ObservationOutcome::Recorded);
    assert_eq!(rebased.record.observation.observed_at_ms, 10_000);
    assert_eq!(rebased.record.observation.capacity.active_workers, 1);
}

#[test]
fn malformed_v1_schema_is_rejected_before_migration_mutates_it() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("malformed-v1.sqlite3");
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection
        .execute_batch(include_str!("../migrations/0001_fleet_state.sql"))
        .unwrap();
    connection.pragma_update(None, "user_version", 1).unwrap();
    connection
        .execute_batch(
            "DROP TABLE operator_projection_denies;
             CREATE TABLE operator_projection_denies (
                 source TEXT NOT NULL,
                 network_id TEXT NOT NULL,
                 device_id TEXT NOT NULL,
                 operation TEXT NOT NULL,
                 PRIMARY KEY (source, network_id, device_id, operation)
             ) STRICT;",
        )
        .unwrap();
    drop(connection);

    assert!(matches!(
        FleetStateStore::open(&path),
        Err(StateError::CorruptState(_))
    ));
    let connection = rusqlite::Connection::open(&path).unwrap();
    assert_eq!(
        connection
            .query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        1
    );
    let observation_table_count = connection
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'node_observations'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .unwrap();
    assert_eq!(observation_table_count, 0);
}

#[test]
fn concurrent_v1_migration_opens_once_and_preserves_projection() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("concurrent-v1.sqlite3");
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection
        .execute_batch(include_str!("../migrations/0001_fleet_state.sql"))
        .unwrap();
    connection.pragma_update(None, "user_version", 1).unwrap();
    let document = projection("upsert", 1);
    let document_json = serde_json::to_string(&document).unwrap();
    connection
        .execute(
            "INSERT INTO managed_projections(
                source, network_id, device_id, projection_generation,
                membership_generation, binding_generation, document_json
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            rusqlite::params![
                document.source,
                document.network_id,
                document.device_id,
                document.projection_generation.get().to_string(),
                document.membership_generation.get().to_string(),
                document.binding_generation.get().to_string(),
                document_json,
            ],
        )
        .unwrap();
    drop(connection);

    let barrier = Arc::new(Barrier::new(9));
    let handles = (0..8)
        .map(|_| {
            let barrier = barrier.clone();
            let path = path.clone();
            thread::spawn(move || {
                barrier.wait();
                let store = FleetStateStore::open(path).unwrap();
                store
                    .inspect_node(
                        "nodescale",
                        "network-1",
                        "device-1",
                        1_000,
                        ReadinessPolicy::new(1_000).unwrap(),
                    )
                    .unwrap()
                    .managed_state
            })
        })
        .collect::<Vec<_>>();
    barrier.wait();
    for handle in handles {
        assert_eq!(handle.join().unwrap(), ManagedNodeState::Active);
    }

    let connection = rusqlite::Connection::open(path).unwrap();
    assert_eq!(
        connection
            .query_row("PRAGMA user_version", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        2
    );
}

#[test]
fn existing_foreign_key_corruption_is_rejected_on_open() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("orphan-v2.sqlite3");
    drop(FleetStateStore::open(&path).unwrap());
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection
        .pragma_update(None, "foreign_keys", false)
        .unwrap();
    let sample = observation(1_000, 0);
    connection
        .execute(
            "INSERT INTO node_observations(
                source, network_id, device_id, observed_at_ms, received_at_ms,
                observation_json
             ) VALUES ('nodescale', 'missing-network', 'missing-device', ?1, ?2, ?3)",
            rusqlite::params![
                sample.observed_at_ms,
                1_100_u64,
                serde_json::to_string(&sample).unwrap(),
            ],
        )
        .unwrap();
    drop(connection);

    assert!(matches!(
        FleetStateStore::open(path),
        Err(StateError::CorruptState(_))
    ));
}

#[test]
fn concurrent_out_of_order_updates_have_the_newest_durable_winner() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = active_store(&path);
    let barrier = Arc::new(Barrier::new(2));
    let handles = [(2_000, 0), (3_000, 1)].map(|(time, active)| {
        let store = store.clone();
        let barrier = barrier.clone();
        thread::spawn(move || {
            barrier.wait();
            store.record_observation(
                "nodescale",
                "network-1",
                "device-1",
                observation(time, active),
                time + 100,
            )
        })
    });
    for handle in handles {
        handle.join().unwrap().unwrap();
    }

    let current = store
        .inspect_node(
            "nodescale",
            "network-1",
            "device-1",
            3_200,
            ReadinessPolicy::new(1_000).unwrap(),
        )
        .unwrap();
    assert_eq!(
        current.observation.unwrap().observation.observed_at_ms,
        3_000
    );
    assert_eq!(current.available_worker_slots, Some(1));
}

#[test]
fn accepted_v1_database_migrates_in_place_and_preserves_managed_projection() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let desired = projection("upsert", 1);
    let connection = rusqlite::Connection::open(&path).unwrap();
    connection
        .execute_batch(include_str!("../migrations/0001_fleet_state.sql"))
        .unwrap();
    connection.pragma_update(None, "user_version", 1).unwrap();
    connection
        .execute(
            "INSERT INTO managed_projections (
                source, network_id, device_id, projection_generation,
                membership_generation, binding_generation, document_json
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            rusqlite::params![
                desired.source,
                desired.network_id,
                desired.device_id,
                desired.projection_generation.get().to_string(),
                desired.membership_generation.get().to_string(),
                desired.binding_generation.get().to_string(),
                serde_json::to_string(&desired).unwrap(),
            ],
        )
        .unwrap();
    drop(connection);

    let migrated = FleetStateStore::open(&path).unwrap();
    assert_eq!(migrated.schema_version().unwrap(), 2);
    assert_eq!(
        migrated
            .inspect_projection("nodescale", "network-1", "device-1")
            .unwrap()
            .generated
            .unwrap()
            .document,
        desired
    );
    migrated
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(1_000, 0),
            1_100,
        )
        .unwrap();

    let connection = rusqlite::Connection::open(&path).unwrap();
    let tables = connection
        .prepare(
            "SELECT name FROM sqlite_master
             WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
        .unwrap()
        .query_map([], |row| row.get::<_, String>(0))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(
        tables,
        vec![
            "fleet_state_schema",
            "managed_projections",
            "node_observations",
            "operator_projection_denies",
            "run_bindings",
        ]
    );
}
