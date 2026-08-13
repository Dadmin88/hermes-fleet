use serde::{Deserialize, Serialize};

use crate::{ExecutionInstance, ManagedNodeIdentity, NodeReadiness};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DestinationAdmissionRequest {
    pub instance_id: String,
    pub idempotency_key: String,
    pub recipe_hash: String,
    pub capabilities_hash: String,
    pub target: ManagedNodeIdentity,
    pub operation: String,
    pub deadline_ms: u64,
}

impl DestinationAdmissionRequest {
    pub fn matches_instance(&self, instance: &ExecutionInstance) -> bool {
        self.instance_id == instance.instance_id
            && self.idempotency_key == instance.idempotency_key
            && self.recipe_hash == instance.recipe_hash
            && self.capabilities_hash == instance.capabilities_hash
            && self.target == instance.target
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DestinationAdmissionContext {
    pub current_target: ManagedNodeIdentity,
    pub managed_active: bool,
    pub authenticated_keryx_binding: bool,
    pub operation_authorized: bool,
    pub readiness: NodeReadiness,
    pub available_worker_slots: u32,
    pub capabilities_hash: String,
    pub evaluated_at_ms: u64,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DestinationAdmissionStatus {
    Admitted,
    InvalidRequest,
    InvalidContext,
    Expired,
    StaleTarget,
    NotManaged,
    BindingUnavailable,
    PolicyDenied,
    ReadinessStale,
    NotReady,
    NoCapacity,
    CapabilitiesChanged,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct AdmissionDecision {
    pub status: DestinationAdmissionStatus,
    pub instance_id: String,
    pub target: ManagedNodeIdentity,
    pub recipe_hash: String,
    pub capabilities_hash: String,
    pub operation: String,
    pub evaluated_at_ms: u64,
}

pub fn admit_destination(
    request: &DestinationAdmissionRequest,
    context: &DestinationAdmissionContext,
) -> Result<AdmissionDecision, DestinationAdmissionStatus> {
    if !valid_request(request) {
        return Err(DestinationAdmissionStatus::InvalidRequest);
    }
    if context.evaluated_at_ms == 0
        || !valid_identity(&context.current_target)
        || !valid_hash(&context.capabilities_hash)
        || context.readiness.fresh != context.readiness.observation_age_ms.is_some()
    {
        return Err(DestinationAdmissionStatus::InvalidContext);
    }
    if context.evaluated_at_ms > request.deadline_ms {
        return Err(DestinationAdmissionStatus::Expired);
    }
    if request.target != context.current_target {
        return Err(DestinationAdmissionStatus::StaleTarget);
    }
    if !context.managed_active {
        return Err(DestinationAdmissionStatus::NotManaged);
    }
    if !context.authenticated_keryx_binding {
        return Err(DestinationAdmissionStatus::BindingUnavailable);
    }
    if !context.operation_authorized {
        return Err(DestinationAdmissionStatus::PolicyDenied);
    }
    if !context.readiness.fresh {
        return Err(DestinationAdmissionStatus::ReadinessStale);
    }
    if !context.readiness.scheduler_ready {
        return Err(DestinationAdmissionStatus::NotReady);
    }
    if context.available_worker_slots == 0 {
        return Err(DestinationAdmissionStatus::NoCapacity);
    }
    if request.capabilities_hash != context.capabilities_hash {
        return Err(DestinationAdmissionStatus::CapabilitiesChanged);
    }
    Ok(AdmissionDecision {
        status: DestinationAdmissionStatus::Admitted,
        instance_id: request.instance_id.clone(),
        target: request.target.clone(),
        recipe_hash: request.recipe_hash.clone(),
        capabilities_hash: request.capabilities_hash.clone(),
        operation: request.operation.clone(),
        evaluated_at_ms: context.evaluated_at_ms,
    })
}

fn valid_request(request: &DestinationAdmissionRequest) -> bool {
    request.operation == "fleet.hermes.run"
        && request.deadline_ms > 0
        && valid_identifier(&request.instance_id)
        && valid_identifier(&request.idempotency_key)
        && valid_hash(&request.recipe_hash)
        && valid_hash(&request.capabilities_hash)
        && valid_identity(&request.target)
}

fn valid_identity(identity: &ManagedNodeIdentity) -> bool {
    valid_identifier(&identity.source)
        && valid_identifier(&identity.network_id)
        && valid_identifier(&identity.device_id)
        && identity.binding_generation > 0
        && identity.admission_generation > 0
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.chars().count() <= 256
        && value.trim() == value
        && !value
            .chars()
            .any(|character| character.is_control() || character.is_whitespace())
}

fn valid_hash(value: &str) -> bool {
    value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
