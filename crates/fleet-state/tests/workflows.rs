use fleet_domain::workflow::WorkflowDocument;
use fleet_state::{FleetStateStore, StateError, WorkflowDeleteOutcome, WorkflowWriteOutcome};
use tempfile::tempdir;

fn document(name: &str) -> WorkflowDocument {
    document_with_id("workflow-1", name)
}

fn document_with_id(id: &str, name: &str) -> WorkflowDocument {
    WorkflowDocument::parse_json(
        &serde_json::json!({
            "schema": "fleet.workflow-editor.v1",
            "id": id,
            "name": name,
            "nodes": [{
                "id": "custom-1",
                "type": "third-party.example/custom-action",
                "title": "Custom action",
                "position": {"x": 1, "y": 2},
                "configuration": {"plugin": {"opaque": [1, true, null]}},
                "target": {"pluginTarget": "preserved"},
                "runtime": "unavailable"
            }],
            "connections": [],
            "metadata": {"executionAvailable": false}
        })
        .to_string(),
    )
    .unwrap()
}

fn unchecked_executable_document() -> WorkflowDocument {
    serde_json::from_value(serde_json::json!({
        "schema": "fleet.workflow-editor.v1",
        "id": "unchecked-workflow",
        "name": "Unchecked",
        "nodes": [],
        "connections": [],
        "metadata": {"executionAvailable": true}
    }))
    .unwrap()
}

#[test]
fn workflow_revisions_are_durable_immutable_and_soft_deletable() {
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("fleet.sqlite3");
    let store = FleetStateStore::open(&path).unwrap();

    let created = store
        .create_workflow(document("Version one"), 1_000)
        .unwrap();
    assert_eq!(created.outcome, WorkflowWriteOutcome::Created);
    assert_eq!(created.revision.version, 1);
    assert_eq!(created.revision.document.name(), "Version one");

    let updated = store
        .update_workflow(document("Version two"), 1, 2_000)
        .unwrap();
    assert_eq!(updated.outcome, WorkflowWriteOutcome::VersionCreated);
    assert_eq!(updated.revision.version, 2);
    assert_eq!(updated.revision.document.name(), "Version two");
    assert_ne!(updated.revision.content_hash, created.revision.content_hash);

    drop(store);
    let restarted = FleetStateStore::open(&path).unwrap();
    assert_eq!(restarted.schema_version().unwrap(), 6);
    assert_eq!(
        restarted
            .read_workflow_version("workflow-1", 1)
            .unwrap()
            .unwrap()
            .document
            .name(),
        "Version one"
    );
    assert_eq!(
        restarted
            .read_workflow_version("workflow-1", 2)
            .unwrap()
            .unwrap()
            .document
            .name(),
        "Version two"
    );
    assert_eq!(
        restarted
            .read_latest_workflow("workflow-1")
            .unwrap()
            .unwrap()
            .version,
        2
    );
    let listed = restarted.list_workflows().unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].workflow_id, "workflow-1");
    assert_eq!(listed[0].latest_version, 2);

    assert_eq!(
        restarted.delete_workflow("workflow-1", 2, 3_000).unwrap(),
        WorkflowDeleteOutcome::Deleted
    );
    assert!(restarted.list_workflows().unwrap().is_empty());
    assert!(
        restarted
            .read_latest_workflow("workflow-1")
            .unwrap()
            .is_none()
    );
    assert!(
        restarted
            .read_workflow_version("workflow-1", 1)
            .unwrap()
            .is_some()
    );
    let historical = restarted
        .read_workflow_version("workflow-1", 1)
        .unwrap()
        .unwrap()
        .document
        .canonical_json()
        .unwrap();
    assert!(historical.contains("third-party.example/custom-action"));
    assert!(historical.contains("pluginTarget"));

    assert!(matches!(
        restarted.create_workflow(document("Resurrected"), 4_000),
        Err(StateError::InvalidTransition("workflow already exists"))
    ));
    assert!(matches!(
        restarted.update_workflow(document("Resurrected"), 2, 5_000),
        Err(StateError::InvalidTransition("workflow is deleted"))
    ));
}

#[test]
fn active_workflow_definition_count_is_owner_bounded() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();

    for index in 0..256 {
        let id = format!("workflow-{index}");
        store
            .create_workflow(document_with_id(&id, &id), index + 1)
            .unwrap();
    }
    assert_eq!(store.list_workflows().unwrap().len(), 256);
    assert!(matches!(
        store.create_workflow(document_with_id("workflow-overflow", "Overflow"), 300),
        Err(StateError::InvalidTransition(_))
    ));
}

#[test]
fn state_write_boundary_rejects_deserialized_unvalidated_documents() {
    let temporary = tempdir().unwrap();
    let store = FleetStateStore::open(temporary.path().join("fleet.sqlite3")).unwrap();

    assert!(matches!(
        store.create_workflow(unchecked_executable_document(), 1_000),
        Err(StateError::InvalidInput("workflow document is invalid"))
    ));

    store
        .create_workflow(document_with_id("unchecked-workflow", "Valid"), 2_000)
        .unwrap();
    assert!(matches!(
        store.update_workflow(unchecked_executable_document(), 1, 3_000),
        Err(StateError::InvalidInput("workflow document is invalid"))
    ));
}
