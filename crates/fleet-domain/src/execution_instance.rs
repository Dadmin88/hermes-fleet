use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ManagedNodeIdentity {
    pub source: String,
    pub network_id: String,
    pub device_id: String,
    pub binding_generation: u64,
    pub admission_generation: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ExecutionInstancePhase {
    Reserved,
    Prepared {
        backend_kind: String,
        realization_id: String,
    },
    Running {
        backend_kind: String,
        realization_id: String,
        keryx_task_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        hermes_run_id: Option<String>,
    },
    Completed {
        backend_kind: String,
        realization_id: String,
        keryx_task_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        hermes_run_id: Option<String>,
    },
    Failed {
        backend_kind: String,
        realization_id: String,
        keryx_task_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        hermes_run_id: Option<String>,
    },
    Cancelled {
        backend_kind: String,
        realization_id: String,
        keryx_task_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        hermes_run_id: Option<String>,
    },
    Indeterminate {
        backend_kind: Option<String>,
        realization_id: Option<String>,
        keryx_task_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        hermes_run_id: Option<String>,
        reason: String,
    },
    CleanupPending {
        backend_kind: String,
        realization_id: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        keryx_task_id: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        hermes_run_id: Option<String>,
        reason: String,
    },
    Cleaned,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExecutionInstanceRecovery {
    DoNotStart,
    InspectBackend,
    InspectBackendAndTask,
    InspectRequired,
    RetryBackendCleanup,
    NoAction,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidExecutionInstance;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionInstance {
    pub instance_id: String,
    pub idempotency_key: String,
    pub recipe_hash: String,
    pub capabilities_hash: String,
    pub target: ManagedNodeIdentity,
    pub generation: u64,
    pub phase: ExecutionInstancePhase,
    pub created_at_ms: u64,
    pub updated_at_ms: u64,
}

impl ExecutionInstance {
    pub fn reserve(
        instance_id: String,
        idempotency_key: String,
        recipe_hash: String,
        capabilities_hash: String,
        target: ManagedNodeIdentity,
        now_ms: u64,
    ) -> Result<Self, InvalidExecutionInstance> {
        let instance = Self {
            instance_id,
            idempotency_key,
            recipe_hash,
            capabilities_hash,
            target,
            generation: 1,
            phase: ExecutionInstancePhase::Reserved,
            created_at_ms: now_ms,
            updated_at_ms: now_ms,
        };
        instance.validate()?;
        Ok(instance)
    }

    pub fn transition(
        &self,
        phase: ExecutionInstancePhase,
        now_ms: u64,
    ) -> Result<Self, InvalidExecutionInstance> {
        if now_ms < self.updated_at_ms || !valid_transition(&self.phase, &phase) {
            return Err(InvalidExecutionInstance);
        }
        require_matching_provenance(&self.phase, &phase)?;
        let mut next = self.clone();
        next.generation = next
            .generation
            .checked_add(1)
            .ok_or(InvalidExecutionInstance)?;
        next.phase = phase;
        next.updated_at_ms = now_ms;
        next.validate()?;
        Ok(next)
    }

    pub const fn recovery(&self) -> ExecutionInstanceRecovery {
        match self.phase {
            ExecutionInstancePhase::Reserved => ExecutionInstanceRecovery::DoNotStart,
            ExecutionInstancePhase::Prepared { .. } => ExecutionInstanceRecovery::InspectBackend,
            ExecutionInstancePhase::Running { .. } => {
                ExecutionInstanceRecovery::InspectBackendAndTask
            }
            ExecutionInstancePhase::Indeterminate { .. } => {
                ExecutionInstanceRecovery::InspectRequired
            }
            ExecutionInstancePhase::CleanupPending { .. } => {
                ExecutionInstanceRecovery::RetryBackendCleanup
            }
            ExecutionInstancePhase::Completed { .. }
            | ExecutionInstancePhase::Failed { .. }
            | ExecutionInstancePhase::Cancelled { .. } => {
                ExecutionInstanceRecovery::InspectBackendAndTask
            }
            ExecutionInstancePhase::Cleaned => ExecutionInstanceRecovery::NoAction,
        }
    }

    pub fn validate(&self) -> Result<(), InvalidExecutionInstance> {
        for value in [
            &self.instance_id,
            &self.idempotency_key,
            &self.target.source,
            &self.target.network_id,
            &self.target.device_id,
        ] {
            validate_identifier(value)?;
        }
        if !valid_hash(&self.recipe_hash)
            || !valid_hash(&self.capabilities_hash)
            || self.target.binding_generation == 0
            || self.target.admission_generation == 0
            || self.generation == 0
            || self.created_at_ms == 0
            || self.updated_at_ms < self.created_at_ms
        {
            return Err(InvalidExecutionInstance);
        }
        validate_phase(&self.phase)
    }
}

fn valid_transition(from: &ExecutionInstancePhase, to: &ExecutionInstancePhase) -> bool {
    use ExecutionInstancePhase::{
        Cancelled, Cleaned, CleanupPending, Completed, Failed, Indeterminate, Prepared, Reserved,
        Running,
    };
    match from {
        Reserved => matches!(
            to,
            Prepared { .. } | Indeterminate { .. } | CleanupPending { .. }
        ),
        Prepared { .. } => matches!(
            to,
            Running { .. } | Indeterminate { .. } | CleanupPending { .. }
        ),
        Running { .. } => matches!(
            to,
            Completed { .. }
                | Failed { .. }
                | Cancelled { .. }
                | Indeterminate { .. }
                | CleanupPending { .. }
        ),
        Completed { .. } | Failed { .. } | Cancelled { .. } => {
            matches!(to, CleanupPending { .. })
        }
        Indeterminate { .. } => matches!(
            to,
            Prepared { .. }
                | Running { .. }
                | Completed { .. }
                | Failed { .. }
                | Cancelled { .. }
                | CleanupPending { .. }
        ),
        CleanupPending { .. } => matches!(to, CleanupPending { .. } | Cleaned),
        Cleaned => false,
    }
}

fn require_matching_provenance(
    from: &ExecutionInstancePhase,
    to: &ExecutionInstancePhase,
) -> Result<(), InvalidExecutionInstance> {
    if let Some((from_backend, from_realization)) = backend_provenance(from) {
        let Some((to_backend, to_realization)) = backend_provenance(to) else {
            return if matches!(to, ExecutionInstancePhase::Cleaned) {
                Ok(())
            } else {
                Err(InvalidExecutionInstance)
            };
        };
        if from_backend != to_backend || from_realization != to_realization {
            return Err(InvalidExecutionInstance);
        }
    }
    if let Some(from_task) = keryx_task(from) {
        let Some(to_task) = keryx_task(to) else {
            return if matches!(to, ExecutionInstancePhase::Cleaned) {
                Ok(())
            } else {
                Err(InvalidExecutionInstance)
            };
        };
        if from_task != to_task {
            return Err(InvalidExecutionInstance);
        }
    }
    if let Some(from_run) = hermes_run(from) {
        let Some(to_run) = hermes_run(to) else {
            return if matches!(to, ExecutionInstancePhase::Cleaned) {
                Ok(())
            } else {
                Err(InvalidExecutionInstance)
            };
        };
        if from_run != to_run {
            return Err(InvalidExecutionInstance);
        }
    }
    Ok(())
}

fn backend_provenance(phase: &ExecutionInstancePhase) -> Option<(&str, &str)> {
    match phase {
        ExecutionInstancePhase::Prepared {
            backend_kind,
            realization_id,
        }
        | ExecutionInstancePhase::Running {
            backend_kind,
            realization_id,
            ..
        }
        | ExecutionInstancePhase::Completed {
            backend_kind,
            realization_id,
            ..
        }
        | ExecutionInstancePhase::Failed {
            backend_kind,
            realization_id,
            ..
        }
        | ExecutionInstancePhase::Cancelled {
            backend_kind,
            realization_id,
            ..
        }
        | ExecutionInstancePhase::CleanupPending {
            backend_kind,
            realization_id,
            ..
        } => Some((backend_kind, realization_id)),
        ExecutionInstancePhase::Indeterminate {
            backend_kind: Some(backend_kind),
            realization_id: Some(realization_id),
            ..
        } => Some((backend_kind, realization_id)),
        _ => None,
    }
}

fn keryx_task(phase: &ExecutionInstancePhase) -> Option<&str> {
    match phase {
        ExecutionInstancePhase::Running { keryx_task_id, .. }
        | ExecutionInstancePhase::Completed { keryx_task_id, .. }
        | ExecutionInstancePhase::Failed { keryx_task_id, .. }
        | ExecutionInstancePhase::Cancelled { keryx_task_id, .. }
        | ExecutionInstancePhase::Indeterminate {
            keryx_task_id: Some(keryx_task_id),
            ..
        }
        | ExecutionInstancePhase::CleanupPending {
            keryx_task_id: Some(keryx_task_id),
            ..
        } => Some(keryx_task_id),
        _ => None,
    }
}

fn hermes_run(phase: &ExecutionInstancePhase) -> Option<&str> {
    match phase {
        ExecutionInstancePhase::Running { hermes_run_id, .. }
        | ExecutionInstancePhase::Completed { hermes_run_id, .. }
        | ExecutionInstancePhase::Failed { hermes_run_id, .. }
        | ExecutionInstancePhase::Cancelled { hermes_run_id, .. }
        | ExecutionInstancePhase::Indeterminate { hermes_run_id, .. }
        | ExecutionInstancePhase::CleanupPending { hermes_run_id, .. } => hermes_run_id.as_deref(),
        _ => None,
    }
}

fn validate_phase(phase: &ExecutionInstancePhase) -> Result<(), InvalidExecutionInstance> {
    if let Some((backend, realization)) = backend_provenance(phase) {
        validate_identifier(backend)?;
        if !backend.contains('/') {
            return Err(InvalidExecutionInstance);
        }
        validate_identifier(realization)?;
    }
    if let Some(task_id) = keryx_task(phase) {
        validate_identifier(task_id)?;
    }
    if let Some(run_id) = hermes_run(phase) {
        validate_identifier(run_id)?;
    }
    match phase {
        ExecutionInstancePhase::Indeterminate {
            backend_kind,
            realization_id,
            keryx_task_id,
            reason,
            ..
        } => {
            if backend_kind.is_some() != realization_id.is_some()
                || (keryx_task_id.is_some() && backend_kind.is_none())
                || reason.is_empty()
                || reason.chars().count() > 512
                || reason.chars().any(char::is_control)
            {
                Err(InvalidExecutionInstance)
            } else {
                Ok(())
            }
        }
        ExecutionInstancePhase::CleanupPending { reason, .. }
            if reason.is_empty()
                || reason.chars().count() > 512
                || reason.chars().any(char::is_control) =>
        {
            Err(InvalidExecutionInstance)
        }
        _ => Ok(()),
    }
}

fn validate_identifier(value: &str) -> Result<(), InvalidExecutionInstance> {
    if value.is_empty()
        || value.chars().count() > 256
        || value.trim() != value
        || value
            .chars()
            .any(|character| character.is_control() || character.is_whitespace())
    {
        return Err(InvalidExecutionInstance);
    }
    Ok(())
}

fn valid_hash(value: &str) -> bool {
    value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
