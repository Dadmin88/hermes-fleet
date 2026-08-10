use fleet_domain::{NodeObservation, ReadinessPolicy, canonical_projection_hash};
use fleet_state::{FleetStateStore, StateError};
use serde_json::json;
use tempfile::tempdir;

const DIGEST_A: &str = "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f";
const DIGEST_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn projection(
    device_id: &str,
    operation: &str,
    generation: u64,
) -> fleet_domain::ProjectionDocument {
    let generation = generation.to_string();
    let mut document: fleet_domain::ProjectionDocument = serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": device_id,
        "projection_generation": generation,
        "membership_generation": generation,
        "binding_generation": generation,
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
            "snapshot": generation,
            "controller": "nodescale"
        }
    }))
    .unwrap();
    document.content_hash = canonical_projection_hash(&document);
    document
}

fn profile(name: &str, version: &str, digest: Option<&str>) -> serde_json::Value {
    let mut value = json!({"name": name, "version": version});
    if let Some(digest) = digest {
        value["content_digest"] = json!(digest);
    }
    value
}

fn observation(
    admission_generation: u64,
    observed_at_ms: u64,
    active_workers: u32,
    profiles: Vec<serde_json::Value>,
) -> NodeObservation {
    serde_json::from_value(json!({
        "admission_generation": admission_generation,
        "observed_at_ms": observed_at_ms,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": active_workers, "max_workers": 2},
        "profiles": profiles,
        "resources": {
            "cpu": {"logical_cores": 8, "load_basis_points": 2500},
            "ram": {"total_bytes": 16000000000_u64, "available_bytes": 8000000000_u64}
        }
    }))
    .unwrap()
}

fn add_ready(
    store: &FleetStateStore,
    device_id: &str,
    active_workers: u32,
    profiles: Vec<serde_json::Value>,
) {
    store
        .apply_projection(projection(device_id, "upsert", 1))
        .unwrap();
    store
        .record_observation(
            "nodescale",
            "network-1",
            device_id,
            observation(1, 1_000, active_workers, profiles),
            1_100,
        )
        .unwrap();
}

fn policy() -> ReadinessPolicy {
    ReadinessPolicy::new(1_000).unwrap()
}

#[test]
fn returns_all_ready_targets_with_existing_same_name_profile_and_resources() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let requested = "agency-backend-engineer";

    add_ready(
        &store,
        "node-z",
        1,
        vec![profile(requested, "0.1.0", Some(DIGEST_B))],
    );
    add_ready(&store, "node-empty", 0, vec![]);
    add_ready(
        &store,
        "node-digestless",
        0,
        vec![profile(requested, "0.1.0", None)],
    );
    add_ready(
        &store,
        "node-other",
        0,
        vec![profile("agency-frontend-engineer", "0.1.0", Some(DIGEST_A))],
    );

    let candidates = store
        .find_profile_placement_candidates(requested, 1_200, policy())
        .unwrap();
    assert_eq!(
        candidates
            .iter()
            .map(|candidate| candidate.device_id.as_str())
            .collect::<Vec<_>>(),
        ["node-digestless", "node-empty", "node-other", "node-z"]
    );

    let digestless = &candidates[0];
    assert_eq!(digestless.admission_generation, 1);
    assert_eq!(digestless.available_worker_slots, 2);
    assert_eq!(
        digestless.existing_profile.as_ref().unwrap().name,
        requested
    );
    assert_eq!(
        digestless.existing_profile.as_ref().unwrap().content_digest,
        None
    );
    assert_eq!(digestless.resources.cpu.as_ref().unwrap().logical_cores, 8);
    assert_eq!(
        digestless.resources.ram.as_ref().unwrap().available_bytes,
        8_000_000_000
    );
    assert!(digestless.readiness.scheduler_ready);

    assert!(candidates[1].existing_profile.is_none());
    assert!(candidates[2].existing_profile.is_none());
    assert_eq!(
        candidates[3]
            .existing_profile
            .as_ref()
            .unwrap()
            .content_digest
            .as_deref(),
        Some(DIGEST_B)
    );
    assert_eq!(candidates[3].available_worker_slots, 1);
}

#[test]
fn excludes_stale_saturated_disabled_and_missing_observation_nodes() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    let requested = "agency-backend-engineer";

    add_ready(&store, "ready", 0, vec![]);

    store
        .apply_projection(projection("stale", "upsert", 1))
        .unwrap();
    store
        .record_observation(
            "nodescale",
            "network-1",
            "stale",
            observation(1, 100, 0, vec![]),
            100,
        )
        .unwrap();

    add_ready(&store, "saturated", 2, vec![]);

    add_ready(&store, "disabled", 0, vec![]);
    store
        .apply_projection(projection("disabled", "disable", 2))
        .unwrap();

    store
        .apply_projection(projection("missing", "upsert", 1))
        .unwrap();

    let candidates = store
        .find_profile_placement_candidates(requested, 1_200, policy())
        .unwrap();
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].device_id, "ready");
}

#[test]
fn placement_candidates_survive_restart_and_readmission_fences_old_state() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let requested = "agency-backend-engineer";

    {
        let store = FleetStateStore::open(&path).unwrap();
        add_ready(&store, "device-1", 0, vec![]);
        assert_eq!(
            store
                .find_profile_placement_candidates(requested, 1_200, policy())
                .unwrap()
                .len(),
            1
        );
    }

    let restarted = FleetStateStore::open(&path).unwrap();
    assert_eq!(
        restarted
            .find_profile_placement_candidates(requested, 1_200, policy())
            .unwrap()
            .len(),
        1
    );

    restarted
        .apply_projection(projection("device-1", "upsert", 2))
        .unwrap();
    assert!(
        restarted
            .find_profile_placement_candidates(requested, 1_300, policy())
            .unwrap()
            .is_empty()
    );
    assert!(matches!(
        restarted.record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(1, 1_250, 0, vec![]),
            1_260,
        ),
        Err(StateError::InvalidTransition(_))
    ));

    restarted
        .record_observation(
            "nodescale",
            "network-1",
            "device-1",
            observation(2, 1_400, 0, vec![]),
            1_410,
        )
        .unwrap();
    let candidates = restarted
        .find_profile_placement_candidates(requested, 1_500, policy())
        .unwrap();
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].admission_generation, 2);
}

#[test]
fn invalid_requested_profile_name_is_rejected() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();

    for invalid in ["", ".", "..", "agency backend", "agency/backend"] {
        assert!(matches!(
            store.find_profile_placement_candidates(invalid, 1_200, policy()),
            Err(StateError::InvalidInput(_))
        ));
    }
}
