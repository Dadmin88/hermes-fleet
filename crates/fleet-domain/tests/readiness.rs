use fleet_domain::{
    Availability, ByteCapacity, CpuObservation, GpuObservation, ManagedNodeState, NodeObservation,
    ObservationRecord, Reachability, ReadinessPolicy, ReadinessReason, ResourceObservation,
    WorkerCapacity, evaluate_readiness,
};

fn ready_observation() -> NodeObservation {
    NodeObservation {
        admission_generation: 1,
        observed_at_ms: 1_000,
        network: Reachability::Reachable,
        keryx: Availability::Available,
        hermes: Availability::Available,
        worker: Availability::Available,
        capacity: WorkerCapacity {
            active_workers: 1,
            max_workers: 2,
        },
        profiles: Vec::new(),
        resources: ResourceObservation {
            cpu: Some(CpuObservation {
                logical_cores: 8,
                load_basis_points: Some(2_500),
            }),
            ram: Some(ByteCapacity {
                total_bytes: 16_000,
                available_bytes: 8_000,
            }),
            swap: Some(ByteCapacity {
                total_bytes: 4_000,
                available_bytes: 3_000,
            }),
            disk: Some(ByteCapacity {
                total_bytes: 100_000,
                available_bytes: 60_000,
            }),
            gpu: None,
        },
    }
}

fn record(received_at_ms: u64) -> ObservationRecord {
    ObservationRecord {
        observation: ready_observation(),
        received_at_ms,
    }
}

#[test]
fn fresh_active_node_with_capacity_is_alive_and_scheduler_ready() {
    let readiness = evaluate_readiness(
        ManagedNodeState::Active,
        Some(&record(10_000)),
        10_500,
        ReadinessPolicy::new(1_000).unwrap(),
    );

    assert!(readiness.alive);
    assert!(readiness.fresh);
    assert!(readiness.scheduler_ready);
    assert_eq!(readiness.observation_age_ms, Some(500));
    assert!(readiness.reasons.is_empty());
    assert_eq!(ready_observation().capacity.available_worker_slots(), 1);
}

#[test]
fn freshness_boundary_is_inclusive_and_stale_state_keeps_last_known_age() {
    let policy = ReadinessPolicy::new(1_000).unwrap();
    let observation = record(10_000);

    let boundary = evaluate_readiness(ManagedNodeState::Active, Some(&observation), 11_000, policy);
    assert!(boundary.fresh);
    assert!(boundary.scheduler_ready);

    let stale = evaluate_readiness(ManagedNodeState::Active, Some(&observation), 11_001, policy);
    assert!(!stale.alive);
    assert!(!stale.fresh);
    assert!(!stale.scheduler_ready);
    assert_eq!(stale.observation_age_ms, Some(1_001));
    assert_eq!(stale.reasons, vec![ReadinessReason::ObservationStale]);
}

#[test]
fn readiness_explains_unknown_inactive_missing_and_unavailable_layers() {
    let policy = ReadinessPolicy::new(1_000).unwrap();
    let unknown = evaluate_readiness(ManagedNodeState::Unknown, None, 10_000, policy);
    assert_eq!(
        unknown.reasons,
        vec![
            ReadinessReason::NodeUnknown,
            ReadinessReason::ObservationMissing
        ]
    );

    let mut observation = ready_observation();
    observation.network = Reachability::Unreachable;
    observation.keryx = Availability::Unavailable;
    observation.hermes = Availability::Unavailable;
    observation.worker = Availability::Unavailable;
    observation.capacity.active_workers = 2;
    let record = ObservationRecord {
        observation,
        received_at_ms: 10_000,
    };
    let unavailable = evaluate_readiness(ManagedNodeState::Disabled, Some(&record), 10_100, policy);
    assert!(unavailable.alive, "freshness is distinct from admission");
    assert_eq!(
        unavailable.reasons,
        vec![
            ReadinessReason::NodeNotActive,
            ReadinessReason::NetworkUnreachable,
            ReadinessReason::KeryxUnavailable,
            ReadinessReason::HermesUnavailable,
            ReadinessReason::WorkerUnavailable,
            ReadinessReason::NoWorkerCapacity,
        ]
    );
}

#[test]
fn zero_worker_slots_blocks_readiness_but_missing_gpu_does_not() {
    let deserialized: NodeObservation = serde_json::from_value(serde_json::json!({
        "admission_generation": 1,
        "observed_at_ms": 1_000,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 1},
        "resources": {}
    }))
    .unwrap();
    assert!(deserialized.profiles.is_empty());
    assert!(deserialized.resources.gpu.is_none());
    assert!(deserialized.validate().is_ok());

    let mut with_gpu = deserialized.clone();
    with_gpu.resources.gpu = Some(GpuObservation {
        present: true,
        vram: Some(ByteCapacity {
            total_bytes: 8_000,
            available_bytes: 4_000,
        }),
    });
    assert!(with_gpu.validate().is_ok());

    let mut saturated = ready_observation();
    saturated.capacity.active_workers = saturated.capacity.max_workers;
    saturated.resources.gpu = None;
    let record = ObservationRecord {
        observation: saturated,
        received_at_ms: 10_000,
    };

    let readiness = evaluate_readiness(
        ManagedNodeState::Active,
        Some(&record),
        10_100,
        ReadinessPolicy::new(1_000).unwrap(),
    );
    assert_eq!(readiness.reasons, vec![ReadinessReason::NoWorkerCapacity]);
}

#[test]
fn observation_validation_rejects_inconsistent_or_unbounded_telemetry() {
    let mut observation = ready_observation();
    observation.admission_generation = 0;
    assert!(observation.validate().is_err());

    let mut observation = ready_observation();
    observation.capacity.active_workers = 3;
    assert!(observation.validate().is_err());

    let mut observation = ready_observation();
    observation
        .resources
        .cpu
        .as_mut()
        .unwrap()
        .load_basis_points = Some(10_001);
    assert!(observation.validate().is_err());

    let mut observation = ready_observation();
    observation.resources.ram.as_mut().unwrap().available_bytes = 16_001;
    assert!(observation.validate().is_err());

    let mut observation = ready_observation();
    observation.resources.gpu = Some(GpuObservation {
        present: false,
        vram: Some(ByteCapacity {
            total_bytes: 8_000,
            available_bytes: 8_000,
        }),
    });
    assert!(observation.validate().is_err());

    let mut observation = ready_observation();
    observation.resources = ResourceObservation::default();
    assert!(observation.validate().is_ok());
}

#[test]
fn invalid_or_future_receipt_time_is_not_fresh() {
    assert!(ReadinessPolicy::new(0).is_err());
    let future = evaluate_readiness(
        ManagedNodeState::Active,
        Some(&record(11_000)),
        10_000,
        ReadinessPolicy::new(1_000).unwrap(),
    );
    assert_eq!(
        future.reasons,
        vec![ReadinessReason::ObservationTimeInvalid]
    );
    assert!(!future.scheduler_ready);
}
