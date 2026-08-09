use std::{
    collections::BTreeSet,
    sync::{Arc, Barrier},
    thread,
};

use fleet_domain::{
    ApplyOutcome, FleetOperation, ManagedOperation, ProjectionDocument, RunBindingState,
};
use fleet_state::{FleetStateStore, RunRecoveryDecision, StateError, recovery_decision};
use serde_json::json;
use tempfile::tempdir;

fn document(
    projection_generation: u64,
    membership_generation: u64,
    binding_generation: u64,
    hash_seed: u64,
    operation: &str,
) -> ProjectionDocument {
    let operations = if operation == "upsert" {
        json!(["fleet.health", "fleet.inventory", "fleet.message"])
    } else {
        json!([])
    };
    serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": "device-1",
        "projection_generation": projection_generation.to_string(),
        "membership_generation": membership_generation.to_string(),
        "binding_generation": binding_generation.to_string(),
        "content_hash": format!("{hash_seed:064x}"),
        "operation": operation,
        "generated_operations": operations,
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "snapshot": projection_generation.to_string(),
            "controller": "nodescale"
        }
    }))
    .unwrap()
}

#[test]
fn fresh_database_migrates_explicit_schema_and_rejects_future_versions() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    assert_eq!(store.schema_version().unwrap(), 1);

    let connection = rusqlite::Connection::open(&path).unwrap();
    let tables = connection
        .prepare(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
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
            "operator_projection_denies",
            "run_bindings"
        ]
    );
    connection.pragma_update(None, "user_version", 2).unwrap();
    drop(connection);
    assert!(matches!(
        FleetStateStore::open(&path),
        Err(StateError::UnsupportedSchema(2))
    ));
}

#[test]
fn managed_projection_survives_restart_with_exact_document_and_local_deny() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let desired = document(1, 2, 3, 1, "upsert");
    {
        let store = FleetStateStore::open(&path).unwrap();
        let applied = store.apply_projection(desired.clone()).unwrap();
        assert_eq!(applied.outcome, ApplyOutcome::Applied);
        store
            .set_operator_deny(
                "nodescale",
                "network-1",
                "device-1",
                FleetOperation::Message,
                true,
            )
            .unwrap();
    }

    let restarted = FleetStateStore::open(&path).unwrap();
    let view = restarted
        .inspect_projection("nodescale", "network-1", "device-1")
        .unwrap();
    assert_eq!(view.generated.unwrap().document, desired);
    assert_eq!(
        view.operator_denied_operations,
        BTreeSet::from([FleetOperation::Message])
    );
    assert_eq!(
        view.effective_operations,
        BTreeSet::from([FleetOperation::Health, FleetOperation::Inventory])
    );
}

#[test]
fn projection_rejections_leave_stored_document_unchanged() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let initial = document(1, 4, 5, 1, "upsert");
    assert_eq!(
        store.apply_projection(initial.clone()).unwrap().outcome,
        ApplyOutcome::Applied
    );

    let cases = [
        (document(1, 4, 5, 2, "upsert"), ApplyOutcome::Conflict),
        (document(2, 3, 5, 3, "upsert"), ApplyOutcome::Regression),
        (document(3, 4, 5, 4, "upsert"), ApplyOutcome::Gap),
    ];
    for (candidate, expected) in cases {
        assert_eq!(store.apply_projection(candidate).unwrap().outcome, expected);
        assert_eq!(
            store
                .inspect_projection("nodescale", "network-1", "device-1")
                .unwrap()
                .generated
                .unwrap()
                .document,
            initial
        );
    }

    let next = document(2, 4, 6, 5, "disable");
    assert_eq!(
        store.apply_projection(next.clone()).unwrap().outcome,
        ApplyOutcome::Applied
    );
    let disabled = store
        .inspect_projection("nodescale", "network-1", "device-1")
        .unwrap();
    assert_eq!(
        disabled.generated.unwrap().document.operation,
        ManagedOperation::Disable
    );
    assert!(disabled.effective_operations.is_empty());

    let removal = document(3, 5, 6, 6, "remove");
    assert_eq!(
        store.apply_projection(removal).unwrap().outcome,
        ApplyOutcome::Applied
    );
    assert!(
        store
            .inspect_projection("nodescale", "network-1", "device-1")
            .unwrap()
            .effective_operations
            .is_empty()
    );
}

#[test]
fn concurrent_conflicting_projection_has_one_durable_winner() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let handles = [11, 12].map(|seed| {
        let store = store.clone();
        let barrier = barrier.clone();
        thread::spawn(move || {
            barrier.wait();
            store.apply_projection(document(1, 1, 1, seed, "upsert"))
        })
    });
    let outcomes = handles
        .into_iter()
        .map(|handle| handle.join().unwrap().unwrap().outcome)
        .collect::<Vec<_>>();
    assert_eq!(
        outcomes
            .iter()
            .filter(|outcome| **outcome == ApplyOutcome::Applied)
            .count(),
        1
    );
    assert_eq!(
        outcomes
            .iter()
            .filter(|outcome| **outcome == ApplyOutcome::Conflict)
            .count(),
        1
    );
}

#[test]
fn duplicate_reservations_create_one_execution_and_recovery_survives_restart() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    let barrier = Arc::new(Barrier::new(8));
    let handles = (0..8)
        .map(|_| {
            let store = store.clone();
            let barrier = barrier.clone();
            thread::spawn(move || {
                barrier.wait();
                store.reserve_run("task-1").unwrap()
            })
        })
        .collect::<Vec<_>>();
    let reservations = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(reservations.iter().filter(|item| item.created).count(), 1);
    assert_eq!(
        reservations
            .iter()
            .filter(|item| recovery_decision(item) == RunRecoveryDecision::StartNew)
            .count(),
        1
    );

    let store = FleetStateStore::open(&path).unwrap();
    let duplicate = store.reserve_run("task-1").unwrap();
    assert_eq!(
        recovery_decision(&duplicate),
        RunRecoveryDecision::FailClosedIndeterminate
    );
    store.bind_run("task-1", "run-1").unwrap();
    drop(store);

    let restarted = FleetStateStore::open(&path).unwrap();
    let running = restarted.reserve_run("task-1").unwrap();
    assert_eq!(
        recovery_decision(&running),
        RunRecoveryDecision::ResumeKnownRun
    );
    restarted
        .complete_run("task-1", "run-1", "durable result")
        .unwrap();
    drop(restarted);

    let restarted = FleetStateStore::open(&path).unwrap();
    let completed = restarted.reserve_run("task-1").unwrap();
    assert_eq!(
        recovery_decision(&completed),
        RunRecoveryDecision::ReplayCompleted
    );
    assert_eq!(
        completed.state,
        RunBindingState::Completed {
            run_id: "run-1".into(),
            result: "durable result".into()
        }
    );
}

#[test]
fn terminal_race_is_transactionally_fenced_and_uncertainty_fails_closed() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    store.reserve_run("task-terminal").unwrap();
    store.bind_run("task-terminal", "run-terminal").unwrap();
    let barrier = Arc::new(Barrier::new(2));
    let complete = {
        let store = store.clone();
        let barrier = barrier.clone();
        thread::spawn(move || {
            barrier.wait();
            store.complete_run("task-terminal", "run-terminal", "winner")
        })
    };
    let cancel = {
        let store = store.clone();
        let barrier = barrier.clone();
        thread::spawn(move || {
            barrier.wait();
            store.cancel_run("task-terminal", "run-terminal")
        })
    };
    let results = [complete.join().unwrap(), cancel.join().unwrap()];
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert!(matches!(
        store.get_run("task-terminal").unwrap().unwrap(),
        RunBindingState::Completed { .. } | RunBindingState::Cancelled { .. }
    ));

    let reservation = store.reserve_run("task-uncertain").unwrap();
    assert_eq!(
        recovery_decision(&reservation),
        RunRecoveryDecision::StartNew
    );
    store.mark_run_indeterminate("task-uncertain").unwrap();
    let restarted = FleetStateStore::open(store.path()).unwrap();
    let uncertain = restarted.reserve_run("task-uncertain").unwrap();
    assert_eq!(
        recovery_decision(&uncertain),
        RunRecoveryDecision::FailClosedIndeterminate
    );
}

#[test]
fn malformed_schema_and_contradictory_rows_fail_closed() {
    let temporary = tempdir().unwrap();
    let schema_path = temporary.path().join("malformed-schema.sqlite3");
    FleetStateStore::open(&schema_path).unwrap();
    let connection = rusqlite::Connection::open(&schema_path).unwrap();
    connection.execute("DROP TABLE run_bindings", []).unwrap();
    connection
        .execute(
            "CREATE TABLE run_bindings(
                task_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL
             ) STRICT",
            [],
        )
        .unwrap();
    drop(connection);
    assert!(matches!(
        FleetStateStore::open(&schema_path),
        Err(StateError::CorruptState(_))
    ));

    let state_path = temporary.path().join("corrupt-state.sqlite3");
    let store = FleetStateStore::open(&state_path).unwrap();
    let connection = rusqlite::Connection::open(&state_path).unwrap();
    connection
        .execute(
            "INSERT INTO run_bindings(task_id, state_json) VALUES (?1, ?2)",
            rusqlite::params!["bad-run", r#"{"kind":"running","run_id":""}"#],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO run_bindings(task_id, state_json) VALUES (?1, ?2)",
            rusqlite::params![
                "unknown-field",
                r#"{"kind":"creating","unexpected":"authority"}"#
            ],
        )
        .unwrap();
    drop(connection);
    assert!(store.get_run("bad-run").is_err());
    assert!(store.get_run("unknown-field").is_err());

    let desired = document(1, 1, 1, 88, "upsert");
    store.apply_projection(desired).unwrap();
    let connection = rusqlite::Connection::open(&state_path).unwrap();
    connection
        .execute(
            "UPDATE managed_projections SET projection_generation = '2'",
            [],
        )
        .unwrap();
    drop(connection);
    assert!(
        store
            .inspect_projection("nodescale", "network-1", "device-1")
            .is_err()
    );
}
