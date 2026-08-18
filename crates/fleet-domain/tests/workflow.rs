use fleet_domain::workflow::{WorkflowDocument, WorkflowValidationError};
use sha2::{Digest, Sha256};

const VALID_DOCUMENT: &str = r#"{
  "schema": "fleet.workflow-editor.v1",
  "id": "workflow-1",
  "name": "Deploy safely",
  "nodes": [
    {
      "id": "trigger-1",
      "type": "manual-trigger",
      "title": "Manual Trigger",
      "position": {"x": 10, "y": 20},
      "configuration": {},
      "target": null,
      "runtime": "unavailable"
    },
    {
      "id": "delay-1",
      "type": "delay",
      "title": "Delay",
      "position": {"x": 310, "y": 20},
      "configuration": {"seconds": 2},
      "target": null,
      "runtime": "unavailable"
    }
  ],
  "connections": [
    {
      "id": "connection-1",
      "source": "trigger-1",
      "sourcePort": "control",
      "target": "delay-1",
      "targetPort": "control",
      "kind": "control"
    }
  ],
  "metadata": {"executionAvailable": false}
}"#;

#[test]
fn workflow_document_preserves_editor_v1_envelope_and_has_stable_identity() {
    let document = WorkflowDocument::parse_json(VALID_DOCUMENT).unwrap();

    assert_eq!(document.id(), "workflow-1");
    assert_eq!(document.name(), "Deploy safely");
    assert_eq!(document.node_count(), 2);
    assert_eq!(document.connection_count(), 1);
    assert!(!document.execution_available());

    let canonical = document.canonical_json().unwrap();
    let reparsed = WorkflowDocument::parse_json(&canonical).unwrap();
    assert_eq!(reparsed, document);
    assert_eq!(reparsed.content_hash(), document.content_hash());
    assert_eq!(document.content_hash().len(), 64);
}

#[test]
fn workflow_document_preserves_unknown_namespaced_blocks_and_arbitrary_payloads() {
    let document = serde_json::json!({
        "schema": "fleet.workflow-editor.v1",
        "id": "plugin-workflow",
        "name": "Plugin workflow",
        "nodes": [
            {
                "id": "custom-1",
                "type": "third-party.example/custom-action",
                "title": "Custom action",
                "pluginVersion": "plugin-v2",
                "configVersion": "config-v3",
                "position": {"x": 1, "y": 2},
                "configuration": {
                    "pluginData": {"z": [true, null, 3], "a": "preserved"}
                },
                "target": {"pluginTarget": {"opaque": 7}},
                "runtime": "unavailable"
            },
            {
                "id": "custom-2",
                "type": "another.vendor/sink",
                "title": "Custom sink",
                "position": {"x": 3, "y": 4},
                "configuration": ["arbitrary", {"nested": true}],
                "target": null,
                "runtime": "unavailable"
            }
        ],
        "connections": [{
            "id": "plugin-edge",
            "source": "custom-1",
            "sourcePort": "vendor-result",
            "target": "custom-2",
            "targetPort": "vendor-input",
            "kind": "vendor-data"
        }],
        "metadata": {"executionAvailable": false}
    });

    let parsed = WorkflowDocument::parse_json(&document.to_string()).unwrap();
    let canonical = parsed.canonical_json().unwrap();
    let reparsed = WorkflowDocument::parse_json(&canonical).unwrap();

    assert_eq!(reparsed, parsed);
    assert!(canonical.contains("third-party.example/custom-action"));
    assert!(canonical.contains("pluginTarget"));
    assert!(!parsed.execution_available());
}

#[test]
fn workflow_document_rejects_duplicate_members_at_any_depth() {
    let duplicate_configuration = VALID_DOCUMENT.replace(
        "{\"seconds\": 2}",
        "{\"plugin\": {\"value\": 1, \"value\": 2}}",
    );
    assert_eq!(
        WorkflowDocument::parse_json(&duplicate_configuration),
        Err(WorkflowValidationError::MalformedDocument)
    );
}

#[test]
fn canonical_hash_ignores_object_key_insertion_order() {
    let left = VALID_DOCUMENT.replace(
        "{\"seconds\": 2}",
        "{\"plugin\": {\"z\": 1, \"a\": 2}, \"seconds\": 2}",
    );
    let right = VALID_DOCUMENT.replace(
        "{\"seconds\": 2}",
        "{\"seconds\": 2, \"plugin\": {\"a\": 2, \"z\": 1}}",
    );

    let left = WorkflowDocument::parse_json(&left).unwrap();
    let right = WorkflowDocument::parse_json(&right).unwrap();
    assert_eq!(
        left.canonical_json().unwrap(),
        right.canonical_json().unwrap()
    );
    assert_eq!(left.content_hash(), right.content_hash());
}

#[test]
fn workflow_document_preserves_strict_managed_machine_target_evidence() {
    let mut document: serde_json::Value = serde_json::from_str(VALID_DOCUMENT).unwrap();
    let identity = ["nodescale", "network-1", "device-1"];
    let stable_id = format!(
        "fleet-node-{:x}",
        Sha256::digest(serde_json::to_vec(&identity).unwrap())
    );
    document["nodes"][0]["target"] = serde_json::json!({
        "stable_id": stable_id,
        "authority": "managed",
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": "device-1"
    });

    let parsed = WorkflowDocument::parse_json(&document.to_string()).unwrap();
    let canonical = parsed.canonical_json().unwrap();
    assert!(canonical.contains("\"authority\":\"managed\""));
    assert!(canonical.contains("\"device_id\":\"device-1\""));
    assert!(!parsed.execution_available());

    document["nodes"][0]["target"]["stable_id"] =
        serde_json::Value::String(format!("fleet-node-{}", "a".repeat(64)));
    assert_eq!(
        WorkflowDocument::parse_json(&document.to_string()),
        Err(WorkflowValidationError::InvalidNode)
    );
}

#[test]
fn workflow_document_rejects_malformed_machine_authority_evidence() {
    let mut document: serde_json::Value = serde_json::from_str(VALID_DOCUMENT).unwrap();
    document["nodes"][0]["target"] = serde_json::json!({
        "stable_id": "observed-node-not-a-digest",
        "authority": "observed",
        "provider": "tailscale"
    });

    assert_eq!(
        WorkflowDocument::parse_json(&document.to_string()),
        Err(WorkflowValidationError::InvalidNode)
    );
}

#[test]
fn workflow_document_rejects_execution_claims() {
    let executable = VALID_DOCUMENT.replace(
        "\"executionAvailable\": false",
        "\"executionAvailable\": true",
    );

    assert_eq!(
        WorkflowDocument::parse_json(&executable),
        Err(WorkflowValidationError::ExecutionUnavailableRequired)
    );
}

#[test]
fn workflow_document_rejects_dangling_self_or_malformed_connections() {
    for invalid in [
        VALID_DOCUMENT.replace("\"target\": \"delay-1\"", "\"target\": \"missing\""),
        VALID_DOCUMENT.replace("\"target\": \"delay-1\"", "\"target\": \"trigger-1\""),
        VALID_DOCUMENT.replace(
            "\"targetPort\": \"control\"",
            "\"targetPort\": \"bad port\"",
        ),
    ] {
        assert_eq!(
            WorkflowDocument::parse_json(&invalid),
            Err(WorkflowValidationError::InvalidConnection)
        );
    }
}

#[test]
fn workflow_document_rejects_oversized_extensible_payloads() {
    let mut document: serde_json::Value = serde_json::from_str(VALID_DOCUMENT).unwrap();
    document["nodes"][0]["configuration"] = serde_json::json!({"value": "x".repeat(20_000)});

    assert_eq!(
        WorkflowDocument::parse_json(&document.to_string()),
        Err(WorkflowValidationError::InvalidNode)
    );
}

#[test]
fn workflow_v2_allows_compile_only_recipe_steps_without_execution_authority() {
    let document = serde_json::json!({
        "schema": "fleet.workflow-editor.v2",
        "id": "workflow-v2",
        "name": "Compile Recipes",
        "nodes": [
            {
                "id": "trigger",
                "type": "manual-trigger",
                "title": "Manual Trigger",
                "position": {"x": 0, "y": 0},
                "configuration": {},
                "target": null,
                "runtime": "unavailable"
            },
            {
                "id": "build",
                "type": "recipe-step",
                "title": "Build",
                "position": {"x": 250, "y": 0},
                "configuration": {"agent_name": "developer", "agent_version": ">=1,<2"},
                "target": null,
                "runtime": "recipe"
            }
        ],
        "connections": [{
            "id": "trigger-build",
            "source": "trigger",
            "sourcePort": "control",
            "target": "build",
            "targetPort": "control",
            "kind": "control"
        }],
        "metadata": {"executionAvailable": false}
    });

    let parsed = WorkflowDocument::parse_json(&document.to_string()).unwrap();
    assert_eq!(parsed.node_count(), 2);
    assert!(!parsed.execution_available());
    assert_eq!(
        WorkflowDocument::parse_json(&parsed.canonical_json().unwrap()).unwrap(),
        parsed
    );
}

#[test]
fn workflow_runtime_marker_cannot_widen_v1_or_non_recipe_v2_nodes() {
    let v1_recipe_runtime =
        VALID_DOCUMENT.replace("\"runtime\": \"unavailable\"", "\"runtime\": \"recipe\"");
    assert_eq!(
        WorkflowDocument::parse_json(&v1_recipe_runtime),
        Err(WorkflowValidationError::InvalidNode)
    );

    let mut v2: serde_json::Value = serde_json::from_str(VALID_DOCUMENT).unwrap();
    v2["schema"] = serde_json::Value::String("fleet.workflow-editor.v2".to_owned());
    v2["nodes"][0]["runtime"] = serde_json::Value::String("recipe".to_owned());
    assert_eq!(
        WorkflowDocument::parse_json(&v2.to_string()),
        Err(WorkflowValidationError::InvalidNode)
    );
}
