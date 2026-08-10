use fleet_domain::{ApplyOutcome, ProjectionDocument, ReadinessPolicy, canonical_projection_hash};
use fleet_state::{AliasClearOutcome, AliasSetOutcome, FleetStateStore, StateError};
use rusqlite::{Connection, params};
use serde_json::json;
use tempfile::tempdir;

fn projection(
    device_id: &str,
    projection_generation: u64,
    membership_generation: u64,
    binding_generation: u64,
    operation: &str,
) -> ProjectionDocument {
    let mut document: ProjectionDocument = serde_json::from_value(json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": device_id,
        "projection_generation": projection_generation.to_string(),
        "membership_generation": membership_generation.to_string(),
        "binding_generation": binding_generation.to_string(),
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
            "snapshot": projection_generation.to_string(),
            "controller": "nodescale"
        }
    }))
    .unwrap();
    document.content_hash = canonical_projection_hash(&document);
    document
}

#[test]
fn aliases_are_durable_replaceable_and_not_global_identity() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    for device_id in ["node-a", "node-b"] {
        assert_eq!(
            store
                .apply_projection(projection(device_id, 1, 1, 1, "upsert"))
                .unwrap()
                .outcome,
            ApplyOutcome::Applied
        );
    }

    assert_eq!(
        store
            .set_node_alias("nodescale", "network-1", "node-a", 1, "Workstation")
            .unwrap(),
        AliasSetOutcome::Created
    );
    assert_eq!(
        store
            .set_node_alias("nodescale", "network-1", "node-a", 1, "Workstation")
            .unwrap(),
        AliasSetOutcome::Unchanged
    );
    assert_eq!(
        store
            .set_node_alias(
                "nodescale",
                "network-1",
                "node-a",
                1,
                "Upstairs Workstation",
            )
            .unwrap(),
        AliasSetOutcome::Replaced
    );
    assert_eq!(
        store
            .set_node_alias(
                "nodescale",
                "network-1",
                "node-b",
                1,
                "Upstairs Workstation",
            )
            .unwrap(),
        AliasSetOutcome::Created
    );
    drop(store);

    let restarted = FleetStateStore::open(&path).unwrap();
    assert_eq!(
        restarted
            .inspect_node_alias("nodescale", "network-1", "node-a")
            .unwrap()
            .as_deref(),
        Some("Upstairs Workstation")
    );
    assert_eq!(
        restarted
            .clear_node_alias("nodescale", "network-1", "node-a", 1)
            .unwrap(),
        AliasClearOutcome::Cleared
    );
    assert_eq!(
        restarted
            .clear_node_alias("nodescale", "network-1", "node-a", 1)
            .unwrap(),
        AliasClearOutcome::AlreadyClear
    );
}

#[test]
fn aliases_validate_display_text_and_require_managed_identity() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    assert!(matches!(
        store.set_node_alias("nodescale", "network-1", "unknown", 1, "Workstation"),
        Err(StateError::InvalidTransition(_))
    ));
    store
        .apply_projection(projection("node-a", 1, 1, 1, "upsert"))
        .unwrap();

    for invalid in ["", " padded", "padded ", "line\nbreak", "zero\u{200b}width"] {
        assert!(matches!(
            store.set_node_alias("nodescale", "network-1", "node-a", 1, invalid),
            Err(StateError::InvalidInput(_))
        ));
    }
    let overlong = "x".repeat(129);
    assert!(matches!(
        store.set_node_alias("nodescale", "network-1", "node-a", 1, &overlong),
        Err(StateError::InvalidInput(_))
    ));
    assert_eq!(
        store
            .set_node_alias("nodescale", "network-1", "node-a", 1, "Étage supérieur",)
            .unwrap(),
        AliasSetOutcome::Created
    );
}

#[test]
fn binding_change_and_remove_fence_aliases() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();
    store
        .apply_projection(projection("node-a", 1, 1, 1, "upsert"))
        .unwrap();
    store
        .set_node_alias("nodescale", "network-1", "node-a", 1, "Workstation")
        .unwrap();

    assert_eq!(
        store
            .apply_projection(projection("node-a", 2, 1, 2, "upsert"))
            .unwrap()
            .outcome,
        ApplyOutcome::Applied
    );
    assert_eq!(
        store
            .inspect_node_alias("nodescale", "network-1", "node-a")
            .unwrap(),
        None
    );
    assert!(matches!(
        store.set_node_alias("nodescale", "network-1", "node-a", 1, "Stale"),
        Err(StateError::InvalidTransition(_))
    ));

    store
        .set_node_alias("nodescale", "network-1", "node-a", 2, "Replacement")
        .unwrap();
    assert!(matches!(
        store.clear_node_alias("nodescale", "network-1", "node-a", 1),
        Err(StateError::InvalidTransition(_))
    ));
    assert_eq!(
        store
            .apply_projection(projection("node-a", 3, 2, 2, "remove"))
            .unwrap()
            .outcome,
        ApplyOutcome::Applied
    );
    assert_eq!(
        store
            .inspect_node_alias("nodescale", "network-1", "node-a")
            .unwrap(),
        None
    );
    assert!(matches!(
        store.set_node_alias("nodescale", "network-1", "node-a", 2, "Forbidden"),
        Err(StateError::InvalidTransition(_))
    ));
}

#[test]
fn corrupt_persisted_aliases_fail_closed_on_read() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    store
        .apply_projection(projection("node-a", 1, 1, 1, "upsert"))
        .unwrap();
    store
        .set_node_alias("nodescale", "network-1", "node-a", 1, "Workstation")
        .unwrap();
    drop(store);

    let connection = Connection::open(&path).unwrap();
    connection
        .execute(
            "UPDATE managed_node_aliases SET alias = ?1
             WHERE source = ?2 AND network_id = ?3 AND device_id = ?4",
            params!["bad\nname", "nodescale", "network-1", "node-a"],
        )
        .unwrap();
    drop(connection);

    let reopened = FleetStateStore::open(&path).unwrap();
    assert!(matches!(
        reopened.inspect_node_alias("nodescale", "network-1", "node-a"),
        Err(StateError::CorruptState(_))
    ));
}

#[test]
fn stale_generation_aliases_fail_closed_for_inspect_and_overview() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    store
        .apply_projection(projection("node-a", 1, 1, 1, "upsert"))
        .unwrap();
    store
        .set_node_alias("nodescale", "network-1", "node-a", 1, "Workstation")
        .unwrap();
    drop(store);

    let connection = Connection::open(&path).unwrap();
    connection
        .execute(
            "UPDATE managed_node_aliases SET binding_generation = ?1
             WHERE source = ?2 AND network_id = ?3 AND device_id = ?4",
            params!["2", "nodescale", "network-1", "node-a"],
        )
        .unwrap();
    drop(connection);

    let reopened = FleetStateStore::open(&path).unwrap();
    assert!(matches!(
        reopened.inspect_node_alias("nodescale", "network-1", "node-a"),
        Err(StateError::CorruptState(_))
    ));
    assert!(matches!(
        reopened.list_managed_nodes(1, ReadinessPolicy::new(1).unwrap()),
        Err(StateError::CorruptState(_))
    ));
}

#[test]
fn aliases_for_removed_projections_fail_closed() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();
    store
        .apply_projection(projection("node-a", 1, 1, 1, "upsert"))
        .unwrap();
    store
        .apply_projection(projection("node-a", 2, 2, 1, "remove"))
        .unwrap();
    drop(store);

    let connection = Connection::open(&path).unwrap();
    connection
        .execute(
            "INSERT INTO managed_node_aliases
             (source, network_id, device_id, binding_generation, alias)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params!["nodescale", "network-1", "node-a", "1", "Dangling"],
        )
        .unwrap();
    drop(connection);

    let reopened = FleetStateStore::open(&path).unwrap();
    assert!(matches!(
        reopened.inspect_node_alias("nodescale", "network-1", "node-a"),
        Err(StateError::CorruptState(_))
    ));
    assert!(matches!(
        reopened.list_managed_nodes(1, ReadinessPolicy::new(1).unwrap()),
        Err(StateError::CorruptState(_))
    ));
}
