use fleet_domain::{ApplyOutcome, ManagedProjectionRecord, ProjectionDocument, RunBindingState};
use fleet_state::{FleetStateStore, RunRecoveryDecision, RunReservation, recovery_decision};
use serde::Deserialize;
use tempfile::tempdir;

#[derive(Deserialize)]
struct Fixture {
    schema: String,
    run_binding_scenarios: Vec<Scenario>,
}

#[derive(Deserialize)]
struct Scenario {
    task_id: String,
    steps: Vec<Step>,
}

#[derive(Deserialize)]
struct Step {
    action: String,
    run_id: Option<String>,
    result: Option<String>,
    expected_state: RunBindingState,
    expected_recovery: RunRecoveryDecision,
}

#[test]
fn durable_run_transitions_match_the_shared_python_oracle_fixture() {
    let fixture: Fixture =
        serde_json::from_str(include_str!("../../../fixtures/f0/fleet-state-v1.json"))
            .expect("valid fleet-state compatibility fixture");
    assert_eq!(fixture.schema, "hermes-fleet.fleet-state-compat.v1");

    let temporary = tempdir().unwrap();
    for (index, scenario) in fixture.run_binding_scenarios.into_iter().enumerate() {
        let path = temporary.path().join(format!("scenario-{index}.sqlite3"));
        let mut store = FleetStateStore::open(&path).unwrap();
        for step in scenario.steps {
            let (state, created) = match step.action.as_str() {
                "reserve" => {
                    let reservation = store.reserve_run(&scenario.task_id).unwrap();
                    (reservation.state, reservation.created)
                }
                "reopen" => {
                    drop(store);
                    store = FleetStateStore::open(&path).unwrap();
                    let reservation = store.reserve_run(&scenario.task_id).unwrap();
                    (reservation.state, reservation.created)
                }
                "bind" => (
                    store
                        .bind_run(&scenario.task_id, step.run_id.as_deref().unwrap())
                        .unwrap(),
                    false,
                ),
                "complete" => (
                    store
                        .complete_run(
                            &scenario.task_id,
                            step.run_id.as_deref().unwrap(),
                            step.result.as_deref().unwrap(),
                        )
                        .unwrap(),
                    false,
                ),
                "cancel" => (
                    store
                        .cancel_run(&scenario.task_id, step.run_id.as_deref().unwrap())
                        .unwrap(),
                    false,
                ),
                "indeterminate" => (
                    store.mark_run_indeterminate(&scenario.task_id).unwrap(),
                    false,
                ),
                other => panic!("unknown fixture action {other}"),
            };
            assert_eq!(state, step.expected_state);
            assert_eq!(
                recovery_decision(&RunReservation { state, created }),
                step.expected_recovery
            );
        }
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
    expected_record_from_step: usize,
}

#[test]
fn durable_projection_transitions_match_the_shared_python_oracle_fixture() {
    let fixture: ProjectionFixture = serde_json::from_str(include_str!(
        "../../../fixtures/f0/managed-projection-v1.json"
    ))
    .expect("valid managed projection fixture");
    assert_eq!(fixture.schema, "hermes-fleet.managed-projection-compat.v1");
    assert!(fixture.initial.is_none());

    let expected_documents = fixture
        .steps
        .iter()
        .map(|step| step.document.clone())
        .collect::<Vec<_>>();
    let temporary = tempdir().unwrap();
    let path = temporary.path().join("projection.sqlite3");
    let mut store = FleetStateStore::open(&path).unwrap();
    for step in fixture.steps {
        let source = step.document.source.clone();
        let network_id = step.document.network_id.clone();
        let device_id = step.document.device_id.clone();
        let outcome = store.apply_projection(step.document).unwrap().outcome;
        assert_eq!(outcome, step.expected_outcome);
        drop(store);
        store = FleetStateStore::open(&path).unwrap();
        let stored = store
            .inspect_projection(&source, &network_id, &device_id)
            .unwrap()
            .generated
            .unwrap()
            .document;
        assert_eq!(
            stored.projection_generation.get().to_string(),
            step.expected_generation
        );
        assert_eq!(stored, expected_documents[step.expected_record_from_step]);
    }
}
