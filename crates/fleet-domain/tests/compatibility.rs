use std::collections::BTreeSet;

use fleet_domain::{
    ApplyOutcome, EffectiveAuthority, FleetOperation, ManagedProjectionRecord, Node,
    ProjectionDocument, RecoveryAction, RunBindingState, SelectionError, apply_projection,
    select_nodes,
};
use serde::Deserialize;

#[derive(Deserialize)]
struct Fixture {
    schema: String,
    operations: Vec<OperationCase>,
    selection: SelectionFixture,
    managed_authority: Vec<AuthorityCase>,
    run_recovery: Vec<RecoveryCase>,
}

#[derive(Deserialize)]
struct OperationCase {
    value: String,
    valid: bool,
    executable: bool,
}

#[derive(Deserialize)]
struct SelectionFixture {
    nodes: Vec<Node>,
    cases: Vec<SelectionCase>,
}

#[derive(Deserialize)]
struct SelectionCase {
    names: Vec<String>,
    tags: Vec<String>,
    expected: Option<Vec<String>>,
    expected_error: Option<String>,
}

#[derive(Deserialize)]
struct AuthorityCase {
    generated: Vec<String>,
    denied: Vec<String>,
    expected: Vec<String>,
}

#[derive(Deserialize)]
struct RecoveryCase {
    state: RunBindingState,
    expected: RecoveryAction,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!("../../../fixtures/f0/domain-v1.json"))
        .expect("valid F0 compatibility fixture")
}

#[test]
fn operation_vocabulary_matches_the_python_oracle() {
    let fixture = fixture();
    assert_eq!(fixture.schema, "hermes-fleet.f0-domain-compat.v1");
    for case in fixture.operations {
        match FleetOperation::parse(&case.value) {
            Ok(operation) => {
                assert!(case.valid, "unexpected valid operation: {}", case.value);
                assert_eq!(operation.is_executable(), case.executable);
                assert_eq!(operation.as_str(), case.value);
            }
            Err(_) => assert!(!case.valid, "unexpected invalid operation: {}", case.value),
        }
    }
    assert_eq!(
        FleetOperation::managed_baseline()
            .into_iter()
            .map(FleetOperation::as_str)
            .collect::<Vec<_>>(),
        vec!["fleet.health", "fleet.inventory", "fleet.message"]
    );
}

#[test]
fn exact_node_selection_and_stable_order_match_the_python_oracle() {
    let fixture = fixture();
    for case in fixture.selection.cases {
        let selected = select_nodes(&fixture.selection.nodes, &case.names, &case.tags);
        if let Some(expected) = case.expected {
            let names = selected
                .expect("selection should succeed")
                .into_iter()
                .map(|node| node.name.clone())
                .collect::<Vec<_>>();
            assert_eq!(names, expected);
        } else {
            let expected = match case.expected_error.as_deref() {
                Some("unknown_node") => SelectionError::UnknownNode,
                Some("unknown_tag") => SelectionError::UnknownTag,
                Some("mixed_selectors") => SelectionError::MixedSelectors,
                other => panic!("unknown fixture error: {other:?}"),
            };
            assert_eq!(selected.unwrap_err(), expected);
        }
    }
}

#[test]
fn managed_baseline_excludes_execution_and_local_deny_wins() {
    for case in fixture().managed_authority {
        let generated = parse_set(&case.generated);
        let denied = parse_set(&case.denied);
        let authority = EffectiveAuthority::new(generated, denied).expect("safe generated grants");
        let expected = parse_set(&case.expected);
        assert_eq!(authority.allowed(), expected);
        assert!(!authority.allowed().contains(&FleetOperation::HermesRun));
    }

    assert!(
        EffectiveAuthority::new(BTreeSet::from([FleetOperation::HermesRun]), BTreeSet::new(),)
            .is_err()
    );
}

#[test]
fn run_recovery_replays_known_truth_and_fails_closed_on_uncertainty() {
    for case in fixture().run_recovery {
        assert_eq!(case.state.recovery_action(), case.expected);
    }
}

#[derive(Deserialize)]
struct ProjectionFixture {
    schema: String,
    initial: Option<ManagedProjectionRecord>,
    steps: Vec<ProjectionStep>,
}

#[derive(Deserialize)]
struct ProjectionStep {
    document: ProjectionDocument,
    expected_outcome: ApplyOutcome,
    expected_generation: String,
}

#[test]
fn managed_projection_outcomes_match_the_python_oracle() {
    let fixture: ProjectionFixture = serde_json::from_str(include_str!(
        "../../../fixtures/f0/managed-projection-v1.json"
    ))
    .expect("valid managed projection fixture");
    assert_eq!(fixture.schema, "hermes-fleet.managed-projection-compat.v1");

    let mut current = fixture.initial;
    for step in fixture.steps {
        let applied = apply_projection(current.as_ref(), step.document)
            .expect("fixture carries only safe generated authority");
        assert_eq!(applied.outcome, step.expected_outcome);
        assert_eq!(
            applied
                .record
                .document
                .projection_generation
                .get()
                .to_string(),
            step.expected_generation
        );
        current = Some(applied.record);
    }
}

#[test]
fn projection_fixture_rejects_noncanonical_generations_and_unsafe_removal_grants() {
    let mut fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../../fixtures/f0/managed-projection-v1.json"
    ))
    .unwrap();
    fixture["steps"][0]["document"]["projection_generation"] =
        serde_json::Value::String("01".into());
    assert!(serde_json::from_value::<ProjectionFixture>(fixture).is_err());

    let fixture: ProjectionFixture = serde_json::from_str(include_str!(
        "../../../fixtures/f0/managed-projection-v1.json"
    ))
    .unwrap();
    let mut removal = fixture.steps.last().unwrap().document.clone();
    removal.generated_operations.insert(FleetOperation::Health);
    assert!(apply_projection(None, removal).is_err());
}

fn parse_set(values: &[String]) -> BTreeSet<FleetOperation> {
    values
        .iter()
        .map(|value| FleetOperation::parse(value).expect("fixture operation"))
        .collect()
}
