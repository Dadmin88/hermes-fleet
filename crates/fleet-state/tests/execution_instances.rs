use fleet_domain::{
    ExecutionInstance, ExecutionInstancePhase, ExecutionInstanceRecovery, ManagedNodeIdentity,
};
use fleet_state::{ExecutionInstanceReservation, FleetStateStore, StateError};
use tempfile::tempdir;

fn identity() -> ManagedNodeIdentity {
    ManagedNodeIdentity {
        source: "nodescale".into(),
        network_id: "network-1".into(),
        device_id: "device-1".into(),
        binding_generation: 7,
        admission_generation: 9,
    }
}

fn instance(idempotency_key: &str) -> ExecutionInstance {
    ExecutionInstance::reserve(
        "instance-1".into(),
        idempotency_key.into(),
        "sha256:".to_owned() + &"1".repeat(64),
        "sha256:".to_owned() + &"2".repeat(64),
        identity(),
        1_000,
    )
    .unwrap()
}

#[test]
fn reservation_is_idempotent_and_conflicting_identity_fails_closed() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let desired = instance("request-1");

    assert_eq!(
        store.reserve_execution_instance(&desired).unwrap(),
        ExecutionInstanceReservation {
            instance: desired.clone(),
            created: true,
        }
    );
    let later_replay = ExecutionInstance::reserve(
        desired.instance_id.clone(),
        desired.idempotency_key.clone(),
        desired.recipe_hash.clone(),
        desired.capabilities_hash.clone(),
        desired.target.clone(),
        2_000,
    )
    .unwrap();
    assert_eq!(
        store.reserve_execution_instance(&later_replay).unwrap(),
        ExecutionInstanceReservation {
            instance: desired,
            created: false,
        }
    );
    assert!(matches!(
        store.reserve_execution_instance(&instance("request-2")),
        Err(StateError::InvalidTransition(_))
    ));
}

#[test]
fn idempotency_key_cannot_bind_a_second_instance() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let first = instance("request-1");
    store.reserve_execution_instance(&first).unwrap();
    let second = ExecutionInstance::reserve(
        "instance-2".into(),
        "request-1".into(),
        first.recipe_hash.clone(),
        first.capabilities_hash.clone(),
        first.target.clone(),
        1_000,
    )
    .unwrap();

    assert!(matches!(
        store.reserve_execution_instance(&second),
        Err(StateError::InvalidTransition(_))
    ));
}

#[test]
fn durable_instance_survives_restart_and_never_stores_keryx_results() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    let reserved = store
        .reserve_execution_instance(&instance("request-1"))
        .unwrap()
        .instance;
    let prepared = store
        .transition_execution_instance(
            "instance-1",
            reserved.generation,
            ExecutionInstancePhase::Prepared {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
            },
            1_100,
        )
        .unwrap();
    let running = store
        .transition_execution_instance(
            "instance-1",
            prepared.generation,
            ExecutionInstancePhase::Running {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
                keryx_task_id: "task-1".into(),
            },
            1_200,
        )
        .unwrap();
    drop(store);

    let restarted = FleetStateStore::open(&path).unwrap();
    let observed = restarted
        .get_execution_instance("instance-1")
        .unwrap()
        .unwrap();
    assert_eq!(observed, running);
    assert_eq!(
        observed.recovery(),
        ExecutionInstanceRecovery::InspectBackendAndTask
    );
}

#[test]
fn generation_fences_stale_writers_and_terminal_state_cannot_regress() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let reserved = store
        .reserve_execution_instance(&instance("request-1"))
        .unwrap()
        .instance;
    let prepared = store
        .transition_execution_instance(
            "instance-1",
            reserved.generation,
            ExecutionInstancePhase::Prepared {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
            },
            1_100,
        )
        .unwrap();
    assert!(matches!(
        store.transition_execution_instance(
            "instance-1",
            reserved.generation,
            ExecutionInstancePhase::Indeterminate {
                backend_kind: None,
                realization_id: None,
                keryx_task_id: None,
                reason: "stale writer".into(),
            },
            1_150,
        ),
        Err(StateError::InvalidTransition(_))
    ));
    let cleaned = store
        .transition_execution_instance(
            "instance-1",
            prepared.generation,
            ExecutionInstancePhase::Cleaned,
            1_200,
        )
        .unwrap();
    assert_eq!(cleaned.recovery(), ExecutionInstanceRecovery::NoAction);
    assert!(matches!(
        store.transition_execution_instance(
            "instance-1",
            cleaned.generation,
            ExecutionInstancePhase::Running {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
                keryx_task_id: "task-1".into(),
            },
            1_300,
        ),
        Err(StateError::InvalidTransition(_))
    ));
}

#[test]
fn cleanup_pending_is_durable_until_cleanup_is_proven() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let reserved = store
        .reserve_execution_instance(&instance("request-1"))
        .unwrap()
        .instance;
    let pending = store
        .transition_execution_instance(
            "instance-1",
            reserved.generation,
            ExecutionInstancePhase::CleanupPending {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
                reason: "provider unavailable".into(),
            },
            1_100,
        )
        .unwrap();

    assert_eq!(
        pending.recovery(),
        ExecutionInstanceRecovery::RetryBackendCleanup
    );
    assert_eq!(
        store.get_execution_instance("instance-1").unwrap().unwrap(),
        pending
    );
}
