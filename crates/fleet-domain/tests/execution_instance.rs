use fleet_domain::{
    ExecutionInstance, ExecutionInstancePhase, ExecutionInstanceRecovery, ManagedNodeIdentity,
};

fn identity() -> ManagedNodeIdentity {
    ManagedNodeIdentity {
        source: "nodescale".into(),
        network_id: "network-1".into(),
        device_id: "device-1".into(),
        binding_generation: 7,
        admission_generation: 9,
    }
}

fn reserved() -> ExecutionInstance {
    ExecutionInstance::reserve(
        "instance-1".into(),
        "request-1".into(),
        "sha256:".to_owned() + &"1".repeat(64),
        "sha256:".to_owned() + &"2".repeat(64),
        identity(),
        1_000,
    )
    .unwrap()
}

#[test]
fn durable_instance_binds_exact_ingredients_and_managed_generation() {
    let instance = reserved();

    assert_eq!(instance.generation, 1);
    assert_eq!(instance.recipe_hash, "sha256:".to_owned() + &"1".repeat(64));
    assert_eq!(instance.target.binding_generation, 7);
    assert_eq!(instance.target.admission_generation, 9);
    assert_eq!(instance.recovery(), ExecutionInstanceRecovery::DoNotStart);
}

#[test]
fn lifecycle_requires_exact_provider_and_task_provenance() {
    let prepared = reserved()
        .transition(
            ExecutionInstancePhase::Prepared {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
            },
            1_100,
        )
        .unwrap();
    let running = prepared
        .transition(
            ExecutionInstancePhase::Running {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
                keryx_task_id: "task-1".into(),
            },
            1_200,
        )
        .unwrap();

    assert_eq!(running.generation, 3);
    assert_eq!(
        running.recovery(),
        ExecutionInstanceRecovery::InspectBackendAndTask
    );
    assert!(
        running
            .transition(
                ExecutionInstancePhase::Completed {
                    backend_kind: "other.dev/runtime".into(),
                    realization_id: "container-1".into(),
                    keryx_task_id: "task-1".into(),
                },
                1_300,
            )
            .is_err()
    );
}

#[test]
fn indeterminate_and_cleanup_states_preserve_fail_closed_recovery() {
    let uncertain = reserved()
        .transition(
            ExecutionInstancePhase::Indeterminate {
                backend_kind: None,
                realization_id: None,
                keryx_task_id: None,
                reason: "provider outcome unavailable".into(),
            },
            1_100,
        )
        .unwrap();
    assert_eq!(
        uncertain.recovery(),
        ExecutionInstanceRecovery::InspectRequired
    );

    let pending = uncertain
        .transition(
            ExecutionInstancePhase::CleanupPending {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
                keryx_task_id: None,
                reason: "cleanup outcome unavailable".into(),
            },
            1_200,
        )
        .unwrap();
    assert_eq!(
        pending.recovery(),
        ExecutionInstanceRecovery::RetryBackendCleanup
    );
    assert_eq!(
        pending
            .transition(ExecutionInstancePhase::Cleaned, 1_300)
            .unwrap()
            .recovery(),
        ExecutionInstanceRecovery::NoAction
    );
}

#[test]
fn uncertainty_after_realization_preserves_known_provenance() {
    let prepared = reserved()
        .transition(
            ExecutionInstancePhase::Prepared {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
            },
            1_100,
        )
        .unwrap();

    assert!(
        prepared
            .transition(
                ExecutionInstancePhase::Indeterminate {
                    backend_kind: None,
                    realization_id: None,
                    keryx_task_id: None,
                    reason: "inspection unavailable".into(),
                },
                1_200,
            )
            .is_err()
    );
    assert!(
        prepared
            .transition(
                ExecutionInstancePhase::Indeterminate {
                    backend_kind: Some("fleet.dev/docker-oci".into()),
                    realization_id: Some("container-1".into()),
                    keryx_task_id: None,
                    reason: "inspection unavailable".into(),
                },
                1_200,
            )
            .is_ok()
    );
}

#[test]
fn cleanup_requires_pending_proof_and_retains_known_task_provenance() {
    let prepared = reserved()
        .transition(
            ExecutionInstancePhase::Prepared {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
            },
            1_100,
        )
        .unwrap();
    assert!(
        prepared
            .transition(ExecutionInstancePhase::Cleaned, 1_200)
            .is_err()
    );
    let running = prepared
        .transition(
            ExecutionInstancePhase::Running {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
                keryx_task_id: "task-1".into(),
            },
            1_200,
        )
        .unwrap();
    assert!(
        running
            .transition(
                ExecutionInstancePhase::CleanupPending {
                    backend_kind: "fleet.dev/docker-oci".into(),
                    realization_id: "container-1".into(),
                    keryx_task_id: None,
                    reason: "cleanup required".into(),
                },
                1_300,
            )
            .is_err()
    );
    let pending = running
        .transition(
            ExecutionInstancePhase::CleanupPending {
                backend_kind: "fleet.dev/docker-oci".into(),
                realization_id: "container-1".into(),
                keryx_task_id: Some("task-1".into()),
                reason: "cleanup required".into(),
            },
            1_300,
        )
        .unwrap();
    assert_eq!(
        pending.recovery(),
        ExecutionInstanceRecovery::RetryBackendCleanup
    );
    assert!(
        pending
            .transition(ExecutionInstancePhase::Cleaned, 1_400)
            .is_ok()
    );
}
