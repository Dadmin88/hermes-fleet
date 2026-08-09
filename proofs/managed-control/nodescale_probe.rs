use std::{env, path::PathBuf};

use nodescale_fleet_client::{
    ApplyOperation, ApplyOutcome, FleetClient, GeneratedOperation, GeneratedStateKind,
    InspectSelector, ProjectionDocument, ProjectionGenerations, Provenance,
};

#[tokio::main]
async fn main() {
    let mut args = env::args_os().skip(1);
    let socket = PathBuf::from(args.next().expect("socket argument"));
    let phase = args.next().expect("phase argument");
    assert!(args.next().is_none());
    let client = FleetClient::new(socket);
    match phase.to_str().expect("UTF-8 phase") {
        "initial" => initial(&client).await,
        "restart" => restart(&client).await,
        "restored" => restored(&client).await,
        value => panic!("unknown phase {value}"),
    }
}

async fn initial(client: &FleetClient) {
    let capabilities = client.capabilities().await.expect("capabilities");
    assert_eq!(capabilities.kinds.len(), 3);
    let applied = client
        .apply(document("1", "1", "1", ApplyOperation::Upsert, baseline()))
        .await
        .expect("apply initial");
    assert_eq!(applied.outcome, ApplyOutcome::Applied);
    let inspected = client
        .inspect(InspectSelector::new("net-proof", "node-proof"))
        .await
        .expect("inspect initial");
    assert_eq!(
        inspected.generated.expect("generated").state,
        GeneratedStateKind::Active
    );
    println!("Nodescale Rust FleetClient applied managed node node-proof");
}

async fn restart(client: &FleetClient) {
    let restored = client
        .inspect(InspectSelector::new("net-proof", "node-proof"))
        .await
        .expect("inspect after restart");
    assert_eq!(
        restored
            .generated
            .as_ref()
            .expect("restored generated")
            .projection_generation,
        "1"
    );
    assert_eq!(
        client
            .apply(document("1", "1", "1", ApplyOperation::Upsert, baseline(),))
            .await
            .expect("replay")
            .outcome,
        ApplyOutcome::AlreadyApplied
    );
    assert_eq!(
        client
            .apply(document(
                "1",
                "1",
                "1",
                ApplyOperation::Upsert,
                vec![GeneratedOperation::Health],
            ))
            .await
            .expect("conflict")
            .outcome,
        ApplyOutcome::Conflict
    );
    assert_eq!(
        client
            .apply(document("3", "3", "3", ApplyOperation::Upsert, baseline(),))
            .await
            .expect("gap")
            .outcome,
        ApplyOutcome::Gap
    );
    assert_eq!(
        client
            .apply(document("2", "2", "2", ApplyOperation::Upsert, baseline(),))
            .await
            .expect("successor")
            .outcome,
        ApplyOutcome::Applied
    );
    assert_eq!(
        client
            .apply(document("1", "1", "1", ApplyOperation::Upsert, baseline(),))
            .await
            .expect("stale")
            .outcome,
        ApplyOutcome::Stale
    );
    assert_eq!(
        client
            .apply(document("3", "1", "2", ApplyOperation::Upsert, baseline()))
            .await
            .expect("regression")
            .outcome,
        ApplyOutcome::Regression
    );
    assert_eq!(
        client
            .apply(document("3", "2", "2", ApplyOperation::Disable, Vec::new(),))
            .await
            .expect("disable")
            .outcome,
        ApplyOutcome::Applied
    );
    let disabled = client
        .inspect(InspectSelector::new("net-proof", "node-proof"))
        .await
        .expect("inspect disabled");
    assert_eq!(
        disabled.generated.expect("disabled generated").state,
        GeneratedStateKind::Disabled
    );
    assert!(
        disabled
            .effective
            .expect("disabled effective")
            .allowed_operations
            .is_empty()
    );
    assert_eq!(
        client
            .apply(document("4", "2", "2", ApplyOperation::Remove, Vec::new(),))
            .await
            .expect("remove")
            .outcome,
        ApplyOutcome::Applied
    );
    println!(
        "Rust Fleet restart preserved authoritative node; replay/stale/conflict/gap/regression passed"
    );
}

async fn restored(client: &FleetClient) {
    let state = client
        .inspect(InspectSelector::new("net-proof", "node-proof"))
        .await
        .expect("inspect tombstone after second restart");
    let generated = state.generated.expect("durable tombstone");
    assert_eq!(generated.state, GeneratedStateKind::Removed);
    assert_eq!(generated.projection_generation, "4");
    assert!(
        state
            .effective
            .expect("removed effective")
            .allowed_operations
            .is_empty()
    );
    println!("Rust Fleet restored durable managed-node tombstone after restart");
}

fn document(
    projection: &str,
    membership: &str,
    binding: &str,
    operation: ApplyOperation,
    grants: Vec<GeneratedOperation>,
) -> ProjectionDocument {
    ProjectionDocument::new(
        "net-proof",
        "node-proof",
        ProjectionGenerations::new(projection, membership, binding),
        operation,
        grants,
        Provenance::new("net-proof", "node-proof", projection),
    )
}

fn baseline() -> Vec<GeneratedOperation> {
    vec![
        GeneratedOperation::Health,
        GeneratedOperation::Inventory,
        GeneratedOperation::Message,
    ]
}
