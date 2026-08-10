use fleet_domain::{
    Availability, FleetOperation, ManagedNodeState, NodeObservation, Reachability, ReadinessPolicy,
    ResourceObservation, WorkerCapacity, canonical_projection_hash,
};
use fleet_state::FleetStateStore;
use serde_json::json;
use tempfile::tempdir;

fn projection(device_id: &str, operation: &str) -> fleet_domain::ProjectionDocument {
    let generated_operations = if operation == "upsert" {
        json!(["fleet.health", "fleet.inventory", "fleet.message"])
    } else {
        json!([])
    };
    let mut document: fleet_domain::ProjectionDocument = serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": device_id,
        "projection_generation": "1",
        "membership_generation": "1",
        "binding_generation": "1",
        "content_hash": "",
        "operation": operation,
        "generated_operations": generated_operations,
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": device_id,
            "snapshot": "1",
            "controller": "nodescale"
        }
    }))
    .unwrap();
    document.content_hash = canonical_projection_hash(&document);
    document
}

fn observation() -> NodeObservation {
    NodeObservation {
        admission_generation: 1,
        observed_at_ms: 1_000,
        network: Reachability::Reachable,
        keryx: Availability::Available,
        hermes: Availability::Available,
        worker: Availability::Available,
        capacity: WorkerCapacity {
            active_workers: 1,
            max_workers: 3,
        },
        resources: ResourceObservation::default(),
    }
}

#[test]
fn managed_node_list_is_stable_authoritative_and_readiness_aware() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    store
        .apply_projection(projection("node-b", "disable"))
        .unwrap();
    store
        .apply_projection(projection("node-a", "upsert"))
        .unwrap();
    store
        .record_observation("nodescale", "network-1", "node-a", observation(), 1_050)
        .unwrap();
    store
        .set_operator_deny(
            "nodescale",
            "network-1",
            "node-a",
            FleetOperation::Message,
            true,
        )
        .unwrap();

    let nodes = store
        .list_managed_nodes(1_100, ReadinessPolicy::new(1_000).unwrap())
        .unwrap();

    assert_eq!(nodes.len(), 2);
    assert_eq!(nodes[0].source, "nodescale");
    assert_eq!(nodes[0].network_id, "network-1");
    assert_eq!(nodes[0].device_id, "node-a");
    assert_eq!(nodes[0].managed_state, ManagedNodeState::Active);
    assert!(nodes[0].operational.readiness.scheduler_ready);
    assert_eq!(nodes[0].operational.available_worker_slots, Some(2));
    assert_eq!(
        nodes[0].effective_operations,
        [FleetOperation::Health, FleetOperation::Inventory]
            .into_iter()
            .collect()
    );
    assert_eq!(
        nodes[0].operator_denied_operations,
        [FleetOperation::Message].into_iter().collect()
    );

    assert_eq!(nodes[1].device_id, "node-b");
    assert_eq!(nodes[1].managed_state, ManagedNodeState::Disabled);
    assert!(!nodes[1].operational.readiness.scheduler_ready);
}
