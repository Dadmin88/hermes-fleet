use fleet_domain::destination_admission::AuthorizationProof;
use fleet_domain::{
    AdmissionDecision, DestinationAdmissionContext, DestinationAdmissionRequest,
    DestinationAdmissionStatus, ExecutionInstance, ManagedNodeIdentity, NodeReadiness,
    ReadinessReason, admit_destination,
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

fn authorization_proof() -> AuthorizationProof {
    AuthorizationProof {
        authenticated_sender: "requester-1".into(),
        requester: "requester-1".into(),
        operation: "fleet.hermes.run".into(),
        recipe_hash: "sha256:".to_owned() + &"1".repeat(64),
        policy_digest: "sha256:".to_owned() + &"3".repeat(64),
        deadline_ms: 2_000,
        secret_refs_digest: "sha256:".to_owned() + &"4".repeat(64),
    }
}

fn request() -> DestinationAdmissionRequest {
    DestinationAdmissionRequest {
        instance_id: "instance-1".into(),
        idempotency_key: "request-1".into(),
        recipe_hash: "sha256:".to_owned() + &"1".repeat(64),
        capabilities_hash: "sha256:".to_owned() + &"2".repeat(64),
        target: identity(),
        operation: "fleet.hermes.run".into(),
        deadline_ms: 2_000,
        authorization: authorization_proof(),
    }
}

fn request_with_proof(proof: AuthorizationProof) -> DestinationAdmissionRequest {
    DestinationAdmissionRequest {
        authorization: proof,
        ..request()
    }
}

fn context() -> DestinationAdmissionContext {
    DestinationAdmissionContext {
        current_target: identity(),
        managed_active: true,
        authenticated_keryx_binding: true,
        current_policy_digest: "sha256:".to_owned() + &"3".repeat(64),
        readiness: NodeReadiness {
            alive: true,
            fresh: true,
            scheduler_ready: true,
            observation_age_ms: Some(100),
            reasons: vec![],
        },
        available_worker_slots: 1,
        capabilities_hash: "sha256:".to_owned() + &"2".repeat(64),
        evaluated_at_ms: 1_500,
    }
}

#[test]
fn exact_current_destination_is_admitted_without_placement() {
    let request = request();
    let decision = admit_destination(&request, &context()).unwrap();

    assert_eq!(
        decision,
        AdmissionDecision {
            status: DestinationAdmissionStatus::Admitted,
            instance_id: request.instance_id,
            target: request.target,
            recipe_hash: request.recipe_hash,
            capabilities_hash: request.capabilities_hash,
            operation: request.operation,
            evaluated_at_ms: 1_500,
        }
    );
}

#[test]
fn stale_generation_policy_revocation_and_binding_loss_fail_closed() {
    let request = request();
    let mut stale = context();
    stale.current_target.binding_generation = 8;
    assert_eq!(
        admit_destination(&request, &stale).unwrap_err(),
        DestinationAdmissionStatus::StaleTarget
    );

    let mut denied = context();
    denied.current_policy_digest = "sha256:".to_owned() + &"4".repeat(64);
    assert_eq!(
        admit_destination(&request, &denied).unwrap_err(),
        DestinationAdmissionStatus::PolicyDenied
    );

    let mut unbound = context();
    unbound.authenticated_keryx_binding = false;
    assert_eq!(
        admit_destination(&request, &unbound).unwrap_err(),
        DestinationAdmissionStatus::BindingUnavailable
    );
}

#[test]
fn authorization_sender_must_match_requester() {
    let mut forged = authorization_proof();
    forged.authenticated_sender = "other-sender".into();
    assert_eq!(
        admit_destination(&request_with_proof(forged), &context()).unwrap_err(),
        DestinationAdmissionStatus::PolicyDenied
    );
}

#[test]
fn stale_policy_digest_is_rejected_against_destination_context() {
    let mut stale = context();
    stale.current_policy_digest = "sha256:".to_owned() + &"4".repeat(64);
    assert_eq!(
        admit_destination(&request(), &stale).unwrap_err(),
        DestinationAdmissionStatus::PolicyDenied
    );
}

#[test]
fn authorization_recipe_hash_must_match_the_request() {
    let mut substituted = authorization_proof();
    substituted.recipe_hash = "sha256:".to_owned() + &"9".repeat(64);
    assert_eq!(
        admit_destination(&request_with_proof(substituted), &context()).unwrap_err(),
        DestinationAdmissionStatus::PolicyDenied
    );
}

#[test]
fn authorization_deadline_must_match_the_request() {
    let mut mismatched = authorization_proof();
    mismatched.deadline_ms = 1_999;
    assert_eq!(
        admit_destination(&request_with_proof(mismatched), &context()).unwrap_err(),
        DestinationAdmissionStatus::PolicyDenied
    );
}

#[test]
fn malformed_secret_references_digest_is_rejected_before_admission() {
    let mut malformed = authorization_proof();
    malformed.secret_refs_digest = "not-a-sha256-digest".into();
    assert_eq!(
        admit_destination(&request_with_proof(malformed), &context()).unwrap_err(),
        DestinationAdmissionStatus::InvalidRequest
    );
}

#[test]
fn inactive_stale_unready_expired_and_saturated_destinations_fail_closed() {
    let request = request();
    let mut inactive = context();
    inactive.managed_active = false;
    assert_eq!(
        admit_destination(&request, &inactive).unwrap_err(),
        DestinationAdmissionStatus::NotManaged
    );

    let mut stale = context();
    stale.readiness.fresh = false;
    stale.readiness.scheduler_ready = false;
    stale.readiness.observation_age_ms = None;
    stale.readiness.reasons = vec![ReadinessReason::ObservationStale];
    assert_eq!(
        admit_destination(&request, &stale).unwrap_err(),
        DestinationAdmissionStatus::ReadinessStale
    );

    let mut saturated = context();
    saturated.available_worker_slots = 0;
    assert_eq!(
        admit_destination(&request, &saturated).unwrap_err(),
        DestinationAdmissionStatus::NoCapacity
    );

    let mut expired = context();
    expired.evaluated_at_ms = request.deadline_ms + 1;
    assert_eq!(
        admit_destination(&request, &expired).unwrap_err(),
        DestinationAdmissionStatus::Expired
    );
}

#[test]
fn capability_snapshot_and_operation_are_exact() {
    let mut wrong_capabilities = context();
    wrong_capabilities.capabilities_hash = "sha256:".to_owned() + &"3".repeat(64);
    assert_eq!(
        admit_destination(&request(), &wrong_capabilities).unwrap_err(),
        DestinationAdmissionStatus::CapabilitiesChanged
    );

    let mut wrong_operation = request();
    wrong_operation.operation = "fleet.health".into();
    assert!(admit_destination(&wrong_operation, &context()).is_err());
}

#[test]
fn invalid_authority_context_never_admits() {
    let mut zero_generation = request();
    zero_generation.target.binding_generation = 0;
    let mut matching = context();
    matching.current_target.binding_generation = 0;
    assert_eq!(
        admit_destination(&zero_generation, &matching).unwrap_err(),
        DestinationAdmissionStatus::InvalidRequest
    );

    let mut invalid_time = context();
    invalid_time.evaluated_at_ms = 0;
    assert_eq!(
        admit_destination(&request(), &invalid_time).unwrap_err(),
        DestinationAdmissionStatus::InvalidContext
    );
}

#[test]
fn request_must_match_the_durable_execution_instance() {
    let instance = ExecutionInstance::reserve(
        "instance-1".into(),
        "request-1".into(),
        "sha256:".to_owned() + &"1".repeat(64),
        "sha256:".to_owned() + &"2".repeat(64),
        identity(),
        1_000,
    )
    .unwrap();
    assert!(request().matches_instance(&instance));

    let mut forged = request();
    forged.recipe_hash = "sha256:".to_owned() + &"9".repeat(64);
    assert!(!forged.matches_instance(&instance));
}
