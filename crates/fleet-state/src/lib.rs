//! SQLite-backed durable state for Hermes Fleet.
//!
//! This crate persists accepted managed projection, one current scheduling
//! observation per managed node, and duplicate-safe Hermes run-binding truth.
//! It deliberately contains no transport, scheduler, telemetry history, profile
//! installation, Sentinel, or UI responsibilities.

use std::{
    collections::BTreeSet,
    fmt,
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use fleet_domain::{
    ApplyOutcome, ExecutionInstance, ExecutionInstancePhase, FleetOperation, Generation,
    ManagedNodeState, ManagedOperation, ManagedProjectionRecord, NodeObservation, NodeReadiness,
    ObservationRecord, ProfilePresence, ProjectionApply, ProjectionDocument, ReadinessPolicy,
    RecoveryAction, ResourceObservation, RunBindingState, apply_projection, evaluate_readiness,
    workflow::WorkflowDocument,
};
use rusqlite::{Connection, ErrorCode, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};

const SCHEMA_VERSION: i64 = 7;
const WORKFLOW_DEFINITION_LIMIT: i64 = 256;
const MIGRATION_1: &str = include_str!("../migrations/0001_fleet_state.sql");
const MIGRATION_2: &str = include_str!("../migrations/0002_node_observations.sql");
const MIGRATION_3: &str = include_str!("../migrations/0003_admission_generation.sql");
const MIGRATION_4: &str = include_str!("../migrations/0004_managed_node_aliases.sql");
const MIGRATION_5: &str = include_str!("../migrations/0005_workflow_definitions.sql");
const MIGRATION_6: &str = include_str!("../migrations/0006_execution_instances.sql");
const MIGRATION_7: &str = include_str!("../migrations/0007_execution_instance_ownership.sql");
const FLEET_STATE_SCHEMA_V1_SQL: &str = "
CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 1)
) STRICT";
const FLEET_STATE_SCHEMA_V2_SQL: &str = "
CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 2)
) STRICT";
const FLEET_STATE_SCHEMA_V3_SQL: &str = "
CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 3)
) STRICT";
const FLEET_STATE_SCHEMA_V4_SQL: &str = "
CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 4)
) STRICT";
const FLEET_STATE_SCHEMA_V5_SQL: &str = "
CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 5)
) STRICT";
const FLEET_STATE_SCHEMA_V6_SQL: &str = "
CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 6)
) STRICT";
const FLEET_STATE_SCHEMA_SQL: &str = "
CREATE TABLE fleet_state_schema (
    version INTEGER PRIMARY KEY CHECK (version = 7)
) STRICT";
const MANAGED_PROJECTIONS_SQL: &str = "
CREATE TABLE managed_projections (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    projection_generation TEXT NOT NULL,
    membership_generation TEXT NOT NULL,
    binding_generation TEXT NOT NULL,
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    PRIMARY KEY (source, network_id, device_id)
) STRICT";
const OPERATOR_DENIES_SQL: &str = "
CREATE TABLE operator_projection_denies (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (
        operation IN ('fleet.health', 'fleet.inventory', 'fleet.message')
    ),
    PRIMARY KEY (source, network_id, device_id, operation)
) STRICT";
const RUN_BINDINGS_SQL: &str = "
CREATE TABLE run_bindings (
    task_id TEXT PRIMARY KEY,
    state_json TEXT NOT NULL CHECK (json_valid(state_json))
) STRICT";
const NODE_OBSERVATIONS_SQL: &str = "
CREATE TABLE node_observations (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms > 0),
    received_at_ms INTEGER NOT NULL CHECK (received_at_ms > 0),
    observation_json TEXT NOT NULL CHECK (json_valid(observation_json)),
    PRIMARY KEY (source, network_id, device_id),
    FOREIGN KEY (source, network_id, device_id)
        REFERENCES managed_projections(source, network_id, device_id)
        ON UPDATE CASCADE ON DELETE CASCADE
) STRICT";
const MANAGED_NODE_ALIASES_SQL: &str = "
CREATE TABLE managed_node_aliases (
    source TEXT NOT NULL,
    network_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    binding_generation TEXT NOT NULL,
    alias TEXT NOT NULL
        CHECK (length(alias) BETWEEN 1 AND 128)
        CHECK (alias = trim(alias)),
    PRIMARY KEY (source, network_id, device_id),
    FOREIGN KEY (source, network_id, device_id)
        REFERENCES managed_projections(source, network_id, device_id)
        ON UPDATE CASCADE ON DELETE CASCADE
) STRICT";
const WORKFLOW_DEFINITIONS_SQL: &str = "
CREATE TABLE workflow_definitions (
    workflow_id TEXT PRIMARY KEY,
    latest_version INTEGER NOT NULL CHECK (latest_version >= 1),
    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms > 0)
) STRICT";
const WORKFLOW_VERSIONS_SQL: &str = "
CREATE TABLE workflow_versions (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    content_hash TEXT NOT NULL
        CHECK (length(content_hash) = 64)
        CHECK (content_hash = lower(content_hash)),
    document_json TEXT NOT NULL CHECK (json_valid(document_json)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    PRIMARY KEY (workflow_id, version),
    FOREIGN KEY (workflow_id) REFERENCES workflow_definitions(workflow_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
) STRICT";
const EXECUTION_INSTANCES_SQL: &str = "
CREATE TABLE execution_instances (
    instance_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms > 0),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= created_at_ms)
) STRICT";
const EXECUTION_BACKEND_OWNER_INDEX_SQL: &str = "
CREATE UNIQUE INDEX execution_instances_backend_realization_owner
ON execution_instances(
    json_extract(state_json, '$.phase.backend_kind'),
    json_extract(state_json, '$.phase.realization_id')
)
WHERE json_extract(state_json, '$.phase.backend_kind') IS NOT NULL
  AND json_extract(state_json, '$.phase.realization_id') IS NOT NULL";
const EXECUTION_KERYX_OWNER_INDEX_SQL: &str = "
CREATE UNIQUE INDEX execution_instances_keryx_task_owner
ON execution_instances(json_extract(state_json, '$.phase.keryx_task_id'))
WHERE json_extract(state_json, '$.phase.keryx_task_id') IS NOT NULL";
const MAX_IDENTIFIER_CHARS: usize = 256;
const MAX_OBSERVATION_FUTURE_SKEW_MS: u64 = 5_000;
const MAX_RESULT_CHARS: usize = 65_536;
const MAX_PROFILE_NAME_BYTES: usize = 128;
const MAX_PROFILE_VERSION_BYTES: usize = 128;
const PROFILE_CONTENT_DIGEST_BYTES: usize = 64;

#[derive(Debug)]
pub enum StateError {
    Database(rusqlite::Error),
    Serialization(serde_json::Error),
    InvalidInput(&'static str),
    InvalidTransition(&'static str),
    UnsupportedSchema(i64),
    CorruptState(&'static str),
}

impl fmt::Display for StateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Database(error) => write!(formatter, "fleet state database error: {error}"),
            Self::Serialization(error) => {
                write!(formatter, "fleet state serialization error: {error}")
            }
            Self::InvalidInput(message) => {
                write!(formatter, "invalid fleet state input: {message}")
            }
            Self::InvalidTransition(message) => {
                write!(formatter, "invalid fleet state transition: {message}")
            }
            Self::UnsupportedSchema(version) => {
                write!(
                    formatter,
                    "unsupported fleet state schema version {version}"
                )
            }
            Self::CorruptState(message) => write!(formatter, "corrupt fleet state: {message}"),
        }
    }
}

impl std::error::Error for StateError {}

impl From<rusqlite::Error> for StateError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Database(error)
    }
}

impl From<serde_json::Error> for StateError {
    fn from(error: serde_json::Error) -> Self {
        Self::Serialization(error)
    }
}

pub type Result<T> = std::result::Result<T, StateError>;

#[derive(Clone, Debug)]
pub struct FleetStateStore {
    path: PathBuf,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectionView {
    pub generated: Option<ManagedProjectionRecord>,
    pub effective_operations: BTreeSet<FleetOperation>,
    pub operator_denied_operations: BTreeSet<FleetOperation>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RunReservation {
    pub state: RunBindingState,
    pub created: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ExecutionInstanceReservation {
    pub instance: ExecutionInstance,
    pub created: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WorkflowWriteOutcome {
    Created,
    VersionCreated,
    Unchanged,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WorkflowDeleteOutcome {
    Deleted,
    AlreadyDeleted,
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkflowRevision {
    pub workflow_id: String,
    pub version: u64,
    pub content_hash: String,
    pub document: WorkflowDocument,
    pub created_at_ms: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WorkflowSummary {
    pub workflow_id: String,
    pub latest_version: u64,
    pub created_at_ms: u64,
    pub updated_at_ms: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct WorkflowWrite {
    pub outcome: WorkflowWriteOutcome,
    pub revision: WorkflowRevision,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationOutcome {
    Recorded,
    AlreadyRecorded,
    Stale,
    Conflict,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AliasSetOutcome {
    Created,
    Replaced,
    Unchanged,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AliasClearOutcome {
    Cleared,
    AlreadyClear,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteObservationSelector {
    pub source: String,
    pub network_id: String,
    pub device_id: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RemoteObservationAuthorityEpoch {
    pub binding_id: String,
    pub authenticated_peer_id: String,
    pub binding_generation: u64,
    pub projection_generation: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ObservationApply {
    pub outcome: ObservationOutcome,
    pub record: ObservationRecord,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct NodeOperationalView {
    pub managed_state: ManagedNodeState,
    pub admission_generation: Option<u64>,
    pub observation: Option<ObservationRecord>,
    pub available_worker_slots: Option<u32>,
    pub readiness: NodeReadiness,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ManagedNodeView {
    pub source: String,
    pub network_id: String,
    pub device_id: String,
    pub alias: Option<String>,
    pub projection_generation: Generation,
    pub membership_generation: Generation,
    pub binding_generation: Generation,
    pub managed_state: ManagedNodeState,
    pub generated_operations: BTreeSet<FleetOperation>,
    pub effective_operations: BTreeSet<FleetOperation>,
    pub operator_denied_operations: BTreeSet<FleetOperation>,
    pub operational: NodeOperationalView,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProfileNodeCandidate {
    pub source: String,
    pub network_id: String,
    pub device_id: String,
    pub profile_version: String,
    pub profile_content_digest: Option<String>,
    pub available_worker_slots: u32,
    pub readiness: NodeReadiness,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProfilePlacementCandidate {
    pub source: String,
    pub network_id: String,
    pub device_id: String,
    pub admission_generation: u64,
    pub available_worker_slots: u32,
    pub existing_profile: Option<ProfilePresence>,
    pub resources: ResourceObservation,
    pub readiness: NodeReadiness,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RunRecoveryDecision {
    StartNew,
    ResumeKnownRun,
    ReplayCompleted,
    FailCancelled,
    FailClosedIndeterminate,
}

impl FleetStateStore {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref();
        if path.as_os_str().is_empty() || path.file_name().is_none() {
            return Err(StateError::InvalidInput("database path must name a file"));
        }
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|_| StateError::InvalidInput("database parent could not be created"))?;
        }
        let store = Self {
            path: path.to_path_buf(),
        };
        store.migrate()?;
        Ok(store)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn schema_version(&self) -> Result<i64> {
        let connection = self.connect()?;
        Ok(connection.query_row("PRAGMA user_version", [], |row| row.get(0))?)
    }

    pub fn create_workflow(
        &self,
        document: WorkflowDocument,
        now_ms: u64,
    ) -> Result<WorkflowWrite> {
        document
            .validate()
            .map_err(|_| StateError::InvalidInput("workflow document is invalid"))?;
        validate_workflow_timestamp(now_ms)?;
        let workflow_id = document.id().to_owned();
        let document_json = document
            .canonical_json()
            .map_err(|_| StateError::InvalidInput("workflow document cannot be serialized"))?;
        let content_hash = document.content_hash();
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let exists = transaction
            .query_row(
                "SELECT 1 FROM workflow_definitions WHERE workflow_id = ?1",
                [&workflow_id],
                |_| Ok(()),
            )
            .optional()?
            .is_some();
        if exists {
            return Err(StateError::InvalidTransition("workflow already exists"));
        }
        let active_count: i64 = transaction.query_row(
            "SELECT COUNT(*) FROM workflow_definitions WHERE deleted = 0",
            [],
            |row| row.get(0),
        )?;
        if active_count >= WORKFLOW_DEFINITION_LIMIT {
            return Err(StateError::InvalidTransition(
                "active workflow definition limit reached",
            ));
        }
        transaction.execute(
            "INSERT INTO workflow_definitions
             (workflow_id, latest_version, deleted, created_at_ms, updated_at_ms)
             VALUES (?1, 1, 0, ?2, ?2)",
            params![workflow_id, now_ms as i64],
        )?;
        transaction.execute(
            "INSERT INTO workflow_versions
             (workflow_id, version, content_hash, document_json, created_at_ms)
             VALUES (?1, 1, ?2, ?3, ?4)",
            params![workflow_id, content_hash, document_json, now_ms as i64],
        )?;
        transaction.commit()?;
        Ok(WorkflowWrite {
            outcome: WorkflowWriteOutcome::Created,
            revision: WorkflowRevision {
                workflow_id,
                version: 1,
                content_hash,
                document,
                created_at_ms: now_ms,
            },
        })
    }

    pub fn update_workflow(
        &self,
        document: WorkflowDocument,
        expected_version: u64,
        now_ms: u64,
    ) -> Result<WorkflowWrite> {
        document
            .validate()
            .map_err(|_| StateError::InvalidInput("workflow document is invalid"))?;
        validate_workflow_timestamp(now_ms)?;
        if expected_version == 0 || expected_version > i64::MAX as u64 {
            return Err(StateError::InvalidInput("workflow version is invalid"));
        }
        let workflow_id = document.id().to_owned();
        let document_json = document
            .canonical_json()
            .map_err(|_| StateError::InvalidInput("workflow document cannot be serialized"))?;
        let content_hash = document.content_hash();
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = transaction
            .query_row(
                "SELECT latest_version, deleted FROM workflow_definitions
                 WHERE workflow_id = ?1",
                [&workflow_id],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
            )
            .optional()?
            .ok_or(StateError::InvalidTransition("workflow does not exist"))?;
        if current.1 != 0 {
            return Err(StateError::InvalidTransition("workflow is deleted"));
        }
        if current.0 <= 0 || current.0 as u64 != expected_version {
            return Err(StateError::InvalidTransition("workflow version conflict"));
        }
        let current_revision =
            load_workflow_revision(&transaction, &workflow_id, expected_version)?.ok_or(
                StateError::CorruptState("workflow latest revision is missing"),
            )?;
        if current_revision.content_hash == content_hash {
            transaction.commit()?;
            return Ok(WorkflowWrite {
                outcome: WorkflowWriteOutcome::Unchanged,
                revision: current_revision,
            });
        }
        let version = expected_version
            .checked_add(1)
            .filter(|version| *version <= i64::MAX as u64)
            .ok_or(StateError::InvalidTransition(
                "workflow version is exhausted",
            ))?;
        transaction.execute(
            "INSERT INTO workflow_versions
             (workflow_id, version, content_hash, document_json, created_at_ms)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                workflow_id,
                version as i64,
                content_hash,
                document_json,
                now_ms as i64
            ],
        )?;
        transaction.execute(
            "UPDATE workflow_definitions
             SET latest_version = ?2, updated_at_ms = ?3
             WHERE workflow_id = ?1 AND latest_version = ?4 AND deleted = 0",
            params![
                workflow_id,
                version as i64,
                now_ms as i64,
                expected_version as i64
            ],
        )?;
        transaction.commit()?;
        Ok(WorkflowWrite {
            outcome: WorkflowWriteOutcome::VersionCreated,
            revision: WorkflowRevision {
                workflow_id,
                version,
                content_hash,
                document,
                created_at_ms: now_ms,
            },
        })
    }

    pub fn read_workflow_version(
        &self,
        workflow_id: &str,
        version: u64,
    ) -> Result<Option<WorkflowRevision>> {
        if workflow_id.is_empty()
            || workflow_id.len() > 128
            || version == 0
            || version > i64::MAX as u64
        {
            return Err(StateError::InvalidInput("workflow selector is invalid"));
        }
        let connection = self.connect()?;
        load_workflow_revision(&connection, workflow_id, version)
    }

    pub fn read_latest_workflow(&self, workflow_id: &str) -> Result<Option<WorkflowRevision>> {
        if workflow_id.is_empty() || workflow_id.len() > 128 {
            return Err(StateError::InvalidInput("workflow selector is invalid"));
        }
        let connection = self.connect()?;
        let version = connection
            .query_row(
                "SELECT latest_version FROM workflow_definitions
                 WHERE workflow_id = ?1 AND deleted = 0",
                [workflow_id],
                |row| row.get::<_, i64>(0),
            )
            .optional()?;
        version
            .map(|version| {
                if version <= 0 {
                    return Err(StateError::CorruptState("workflow version is invalid"));
                }
                load_workflow_revision(&connection, workflow_id, version as u64)?.ok_or(
                    StateError::CorruptState("workflow latest revision is missing"),
                )
            })
            .transpose()
    }

    pub fn list_workflows(&self) -> Result<Vec<WorkflowSummary>> {
        let connection = self.connect()?;
        let mut statement = connection.prepare(
            "SELECT workflow_id, latest_version, created_at_ms, updated_at_ms
             FROM workflow_definitions WHERE deleted = 0 ORDER BY workflow_id LIMIT 257",
        )?;
        let workflows = statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                ))
            })?
            .map(|row| {
                let (workflow_id, latest_version, created_at_ms, updated_at_ms) = row?;
                if latest_version <= 0 || created_at_ms <= 0 || updated_at_ms <= 0 {
                    return Err(StateError::CorruptState("workflow summary is invalid"));
                }
                Ok(WorkflowSummary {
                    workflow_id,
                    latest_version: latest_version as u64,
                    created_at_ms: created_at_ms as u64,
                    updated_at_ms: updated_at_ms as u64,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        if workflows.len() > WORKFLOW_DEFINITION_LIMIT as usize {
            return Err(StateError::CorruptState(
                "active workflow definition limit exceeded",
            ));
        }
        Ok(workflows)
    }

    pub fn delete_workflow(
        &self,
        workflow_id: &str,
        expected_version: u64,
        now_ms: u64,
    ) -> Result<WorkflowDeleteOutcome> {
        validate_workflow_timestamp(now_ms)?;
        if workflow_id.is_empty()
            || workflow_id.len() > 128
            || expected_version == 0
            || expected_version > i64::MAX as u64
        {
            return Err(StateError::InvalidInput("workflow selector is invalid"));
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = transaction
            .query_row(
                "SELECT latest_version, deleted FROM workflow_definitions
                 WHERE workflow_id = ?1",
                [workflow_id],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
            )
            .optional()?
            .ok_or(StateError::InvalidTransition("workflow does not exist"))?;
        if current.1 != 0 {
            transaction.commit()?;
            return Ok(WorkflowDeleteOutcome::AlreadyDeleted);
        }
        if current.0 <= 0 || current.0 as u64 != expected_version {
            return Err(StateError::InvalidTransition("workflow version conflict"));
        }
        transaction.execute(
            "UPDATE workflow_definitions SET deleted = 1, updated_at_ms = ?2
             WHERE workflow_id = ?1 AND latest_version = ?3 AND deleted = 0",
            params![workflow_id, now_ms as i64, expected_version as i64],
        )?;
        transaction.commit()?;
        Ok(WorkflowDeleteOutcome::Deleted)
    }

    pub fn apply_projection(&self, desired: ProjectionDocument) -> Result<ProjectionApply> {
        validate_projection(&desired)?;
        if fleet_domain::canonical_projection_hash(&desired) != desired.content_hash {
            return Err(StateError::InvalidInput(
                "managed projection content hash does not match its complete document",
            ));
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = load_projection(
            &transaction,
            &desired.source,
            &desired.network_id,
            &desired.device_id,
        )?;
        let applied = apply_projection(current.as_ref(), desired.clone())
            .map_err(|_| StateError::InvalidInput("generated authority is invalid"))?;
        if applied.outcome == ApplyOutcome::Applied {
            let binding_changed = current.as_ref().is_some_and(|record| {
                record.document.binding_generation != desired.binding_generation
            });
            let document_json = serde_json::to_string(&desired)?;
            transaction.execute(
                "INSERT INTO managed_projections (
                    source, network_id, device_id, projection_generation,
                    membership_generation, binding_generation, document_json
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                 ON CONFLICT(source, network_id, device_id) DO UPDATE SET
                    projection_generation = excluded.projection_generation,
                    membership_generation = excluded.membership_generation,
                    binding_generation = excluded.binding_generation,
                    document_json = excluded.document_json",
                params![
                    desired.source,
                    desired.network_id,
                    desired.device_id,
                    desired.projection_generation.get().to_string(),
                    desired.membership_generation.get().to_string(),
                    desired.binding_generation.get().to_string(),
                    document_json,
                ],
            )?;
            transaction.execute(
                "DELETE FROM node_observations
                 WHERE source = ?1 AND network_id = ?2 AND device_id = ?3",
                params![desired.source, desired.network_id, desired.device_id],
            )?;
            if binding_changed || desired.operation == ManagedOperation::Remove {
                transaction.execute(
                    "DELETE FROM managed_node_aliases
                     WHERE source = ?1 AND network_id = ?2 AND device_id = ?3",
                    params![desired.source, desired.network_id, desired.device_id],
                )?;
            }
        }
        transaction.commit()?;
        Ok(applied)
    }

    pub fn inspect_projection(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
    ) -> Result<ProjectionView> {
        validate_key(source, network_id, device_id)?;
        let connection = self.connect()?;
        inspect_projection_from(&connection, source, network_id, device_id)
    }

    pub fn inspect_node_alias(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
    ) -> Result<Option<String>> {
        validate_key(source, network_id, device_id)?;
        let connection = self.connect()?;
        let projection = load_projection(&connection, source, network_id, device_id)?;
        let expected_binding_generation = if matches!(
            managed_state(projection.as_ref()),
            ManagedNodeState::Unknown | ManagedNodeState::Removed
        ) {
            None
        } else {
            Some(
                projection
                    .as_ref()
                    .map(|record| record.document.binding_generation.get())
                    .ok_or(StateError::CorruptState(
                        "managed alias identity has no projection",
                    ))?,
            )
        };
        load_validated_alias(
            &connection,
            source,
            network_id,
            device_id,
            expected_binding_generation,
        )
    }

    pub fn set_node_alias(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
        expected_binding_generation: u64,
        alias: &str,
    ) -> Result<AliasSetOutcome> {
        validate_key(source, network_id, device_id)?;
        validate_alias(alias)?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let projection = load_projection(&transaction, source, network_id, device_id)?;
        if matches!(
            managed_state(projection.as_ref()),
            ManagedNodeState::Unknown | ManagedNodeState::Removed
        ) {
            return Err(StateError::InvalidTransition(
                "alias identity is not a managed node",
            ));
        }
        let binding_generation = projection
            .as_ref()
            .map(|record| record.document.binding_generation.get())
            .ok_or(StateError::CorruptState(
                "managed alias identity has no projection",
            ))?;
        if expected_binding_generation == 0 || expected_binding_generation != binding_generation {
            return Err(StateError::InvalidTransition(
                "alias binding generation is stale",
            ));
        }
        let binding_generation = binding_generation.to_string();
        let current = transaction
            .query_row(
                "SELECT binding_generation, alias FROM managed_node_aliases
                 WHERE source = ?1 AND network_id = ?2 AND device_id = ?3",
                params![source, network_id, device_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()?;
        let outcome = match current.as_ref() {
            Some((generation, current_alias))
                if generation == &binding_generation && current_alias == alias =>
            {
                AliasSetOutcome::Unchanged
            }
            Some(_) => AliasSetOutcome::Replaced,
            None => AliasSetOutcome::Created,
        };
        if outcome != AliasSetOutcome::Unchanged {
            transaction.execute(
                "INSERT INTO managed_node_aliases
                 (source, network_id, device_id, binding_generation, alias)
                 VALUES (?1, ?2, ?3, ?4, ?5)
                 ON CONFLICT(source, network_id, device_id) DO UPDATE SET
                    binding_generation = excluded.binding_generation,
                    alias = excluded.alias",
                params![source, network_id, device_id, binding_generation, alias],
            )?;
        }
        transaction.commit()?;
        Ok(outcome)
    }

    pub fn clear_node_alias(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
        expected_binding_generation: u64,
    ) -> Result<AliasClearOutcome> {
        validate_key(source, network_id, device_id)?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let projection = load_projection(&transaction, source, network_id, device_id)?;
        if matches!(
            managed_state(projection.as_ref()),
            ManagedNodeState::Unknown | ManagedNodeState::Removed
        ) {
            return Err(StateError::InvalidTransition(
                "alias identity is not a managed node",
            ));
        }
        let binding_generation = projection
            .as_ref()
            .map(|record| record.document.binding_generation.get())
            .ok_or(StateError::CorruptState(
                "managed alias identity has no projection",
            ))?;
        if expected_binding_generation == 0 || expected_binding_generation != binding_generation {
            return Err(StateError::InvalidTransition(
                "alias binding generation is stale",
            ));
        }
        let changed = transaction.execute(
            "DELETE FROM managed_node_aliases
             WHERE source = ?1 AND network_id = ?2 AND device_id = ?3
               AND binding_generation = ?4",
            params![
                source,
                network_id,
                device_id,
                binding_generation.to_string()
            ],
        )?;
        transaction.commit()?;
        Ok(if changed == 0 {
            AliasClearOutcome::AlreadyClear
        } else {
            AliasClearOutcome::Cleared
        })
    }

    pub fn set_operator_deny(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
        operation: FleetOperation,
        denied: bool,
    ) -> Result<()> {
        validate_key(source, network_id, device_id)?;
        if operation.is_executable() {
            return Err(StateError::InvalidInput(
                "local deny must name a generated baseline operation",
            ));
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if denied {
            transaction.execute(
                "INSERT OR IGNORE INTO operator_projection_denies
                 (source, network_id, device_id, operation) VALUES (?1, ?2, ?3, ?4)",
                params![source, network_id, device_id, operation.as_str()],
            )?;
        } else {
            transaction.execute(
                "DELETE FROM operator_projection_denies
                 WHERE source = ?1 AND network_id = ?2 AND device_id = ?3 AND operation = ?4",
                params![source, network_id, device_id, operation.as_str()],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    pub fn acquire_remote_observation_authority(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
    ) -> Result<RemoteObservationAuthorityEpoch> {
        validate_key(source, network_id, device_id)?;
        let connection = self.connect()?;
        let projection = load_projection(&connection, source, network_id, device_id)?;
        remote_observation_authority(projection.as_ref())
    }

    pub fn record_remote_observation(
        &self,
        selector: &RemoteObservationSelector,
        authenticated_sender: &str,
        authority_epoch: &RemoteObservationAuthorityEpoch,
        observation: NodeObservation,
        received_at_ms: u64,
    ) -> Result<ObservationApply> {
        validate_key(&selector.source, &selector.network_id, &selector.device_id)?;
        validate_remote_authority_epoch(authority_epoch)?;
        validate_observation(&observation, received_at_ms)?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let projection = load_projection(
            &transaction,
            &selector.source,
            &selector.network_id,
            &selector.device_id,
        )?;
        let active_epoch = remote_observation_authority(projection.as_ref())?;
        if authenticated_sender != active_epoch.authenticated_peer_id
            || authority_epoch != &active_epoch
        {
            return Err(StateError::InvalidTransition(
                "remote observation authority epoch is stale or mismatched",
            ));
        }
        let applied = record_observation_in_transaction(
            &transaction,
            &selector.source,
            &selector.network_id,
            &selector.device_id,
            projection.as_ref(),
            observation,
            received_at_ms,
        )?;
        transaction.commit()?;
        Ok(applied)
    }

    pub fn record_observation(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
        observation: NodeObservation,
        received_at_ms: u64,
    ) -> Result<ObservationApply> {
        validate_key(source, network_id, device_id)?;
        validate_observation(&observation, received_at_ms)?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let projection = load_projection(&transaction, source, network_id, device_id)?;
        let applied = record_observation_in_transaction(
            &transaction,
            source,
            network_id,
            device_id,
            projection.as_ref(),
            observation,
            received_at_ms,
        )?;
        transaction.commit()?;
        Ok(applied)
    }

    pub fn inspect_node(
        &self,
        source: &str,
        network_id: &str,
        device_id: &str,
        now_ms: u64,
        policy: ReadinessPolicy,
    ) -> Result<NodeOperationalView> {
        validate_key(source, network_id, device_id)?;
        if now_ms == 0 || now_ms > i64::MAX as u64 {
            return Err(StateError::InvalidInput(
                "inspection timestamp is outside the supported range",
            ));
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Deferred)?;
        let view = inspect_node_from(&transaction, source, network_id, device_id, now_ms, policy)?;
        transaction.commit()?;
        Ok(view)
    }

    pub fn list_managed_nodes(
        &self,
        now_ms: u64,
        policy: ReadinessPolicy,
    ) -> Result<Vec<ManagedNodeView>> {
        if now_ms == 0 || now_ms > i64::MAX as u64 {
            return Err(StateError::InvalidInput(
                "inspection timestamp is outside the supported range",
            ));
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Deferred)?;
        let keys = {
            let mut statement = transaction.prepare(
                "SELECT source, network_id, device_id FROM managed_projections
                 ORDER BY source, network_id, device_id",
            )?;
            statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };
        let mut nodes = Vec::with_capacity(keys.len());
        for (source, network_id, device_id) in keys {
            let projection =
                inspect_projection_from(&transaction, &source, &network_id, &device_id)?;
            let record = projection.generated.ok_or(StateError::CorruptState(
                "listed managed projection disappeared",
            ))?;
            let operational = inspect_node_from(
                &transaction,
                &source,
                &network_id,
                &device_id,
                now_ms,
                policy,
            )?;
            let document = record.document;
            let alias = load_validated_alias(
                &transaction,
                &source,
                &network_id,
                &device_id,
                if matches!(
                    operational.managed_state,
                    ManagedNodeState::Unknown | ManagedNodeState::Removed
                ) {
                    None
                } else {
                    Some(document.binding_generation.get())
                },
            )?;
            nodes.push(ManagedNodeView {
                source,
                network_id,
                device_id,
                alias,
                projection_generation: document.projection_generation,
                membership_generation: document.membership_generation,
                binding_generation: document.binding_generation,
                managed_state: operational.managed_state,
                generated_operations: document.generated_operations,
                effective_operations: projection.effective_operations,
                operator_denied_operations: projection.operator_denied_operations,
                operational,
            });
        }
        transaction.commit()?;
        Ok(nodes)
    }

    pub fn find_profile_candidates(
        &self,
        profile_name: &str,
        required_version: Option<&str>,
        now_ms: u64,
        policy: ReadinessPolicy,
    ) -> Result<Vec<ProfileNodeCandidate>> {
        self.find_profile_candidates_matching(profile_name, required_version, None, now_ms, policy)
    }

    pub fn find_exact_profile_candidates(
        &self,
        profile_name: &str,
        required_version: Option<&str>,
        required_content_digest: &str,
        now_ms: u64,
        policy: ReadinessPolicy,
    ) -> Result<Vec<ProfileNodeCandidate>> {
        validate_profile_content_digest(required_content_digest)?;
        self.find_profile_candidates_matching(
            profile_name,
            required_version,
            Some(required_content_digest),
            now_ms,
            policy,
        )
    }

    fn find_profile_candidates_matching(
        &self,
        profile_name: &str,
        required_version: Option<&str>,
        required_content_digest: Option<&str>,
        now_ms: u64,
        policy: ReadinessPolicy,
    ) -> Result<Vec<ProfileNodeCandidate>> {
        validate_profile_query(profile_name, required_version)?;
        if now_ms == 0 || now_ms > i64::MAX as u64 {
            return Err(StateError::InvalidInput(
                "inspection timestamp is outside the supported range",
            ));
        }

        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Deferred)?;
        let keys = {
            let mut statement = transaction.prepare(
                "SELECT source, network_id, device_id
                 FROM managed_projections
                 ORDER BY source, network_id, device_id",
            )?;
            statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };

        let mut candidates = Vec::new();
        for (source, network_id, device_id) in keys {
            let projection = load_projection(&transaction, &source, &network_id, &device_id)?;
            let admission_generation = projection
                .as_ref()
                .map(|record| record.document.projection_generation.get());
            let managed_state = managed_state(projection.as_ref());
            if managed_state != ManagedNodeState::Active {
                continue;
            }

            let observation = load_observation(&transaction, &source, &network_id, &device_id)?;
            if observation.as_ref().is_some_and(|record| {
                Some(record.observation.admission_generation) != admission_generation
            }) {
                return Err(StateError::CorruptState(
                    "stored observation admission generation does not match projection",
                ));
            }
            let Some(record) = observation else {
                continue;
            };
            let readiness = evaluate_readiness(managed_state, Some(&record), now_ms, policy);
            if !readiness.scheduler_ready {
                continue;
            }
            let Some(profile) = record
                .observation
                .profiles
                .iter()
                .find(|profile| profile.name == profile_name)
            else {
                continue;
            };
            if required_version.is_some_and(|version| profile.version != version) {
                continue;
            }
            if required_content_digest
                .is_some_and(|digest| profile.content_digest.as_deref() != Some(digest))
            {
                continue;
            }

            candidates.push(ProfileNodeCandidate {
                source,
                network_id,
                device_id,
                profile_version: profile.version.clone(),
                profile_content_digest: profile.content_digest.clone(),
                available_worker_slots: record.observation.capacity.available_worker_slots(),
                readiness,
            });
        }
        transaction.commit()?;
        Ok(candidates)
    }

    pub fn find_profile_placement_candidates(
        &self,
        profile_name: &str,
        now_ms: u64,
        policy: ReadinessPolicy,
    ) -> Result<Vec<ProfilePlacementCandidate>> {
        validate_profile_query(profile_name, None)?;
        if now_ms == 0 || now_ms > i64::MAX as u64 {
            return Err(StateError::InvalidInput(
                "inspection timestamp is outside the supported range",
            ));
        }

        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Deferred)?;
        let keys = {
            let mut statement = transaction.prepare(
                "SELECT source, network_id, device_id
                 FROM managed_projections
                 ORDER BY source, network_id, device_id",
            )?;
            statement
                .query_map([], |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                })?
                .collect::<std::result::Result<Vec<_>, _>>()?
        };

        let mut candidates = Vec::new();
        for (source, network_id, device_id) in keys {
            let projection = load_projection(&transaction, &source, &network_id, &device_id)?;
            let admission_generation = projection
                .as_ref()
                .map(|record| record.document.projection_generation.get());
            let managed_state = managed_state(projection.as_ref());
            if managed_state != ManagedNodeState::Active {
                continue;
            }
            let Some(admission_generation) = admission_generation else {
                return Err(StateError::CorruptState(
                    "active placement identity has no admission generation",
                ));
            };

            let observation = load_observation(&transaction, &source, &network_id, &device_id)?;
            if observation.as_ref().is_some_and(|record| {
                record.observation.admission_generation != admission_generation
            }) {
                return Err(StateError::CorruptState(
                    "stored observation admission generation does not match projection",
                ));
            }
            let Some(record) = observation else {
                continue;
            };
            let readiness = evaluate_readiness(managed_state, Some(&record), now_ms, policy);
            if !readiness.scheduler_ready {
                continue;
            }
            let existing_profile = record
                .observation
                .profiles
                .iter()
                .find(|profile| profile.name == profile_name)
                .cloned();

            candidates.push(ProfilePlacementCandidate {
                source,
                network_id,
                device_id,
                admission_generation,
                available_worker_slots: record.observation.capacity.available_worker_slots(),
                existing_profile,
                resources: record.observation.resources.clone(),
                readiness,
            });
        }
        transaction.commit()?;
        Ok(candidates)
    }

    pub fn reserve_execution_instance(
        &self,
        desired: &ExecutionInstance,
    ) -> Result<ExecutionInstanceReservation> {
        desired
            .validate()
            .map_err(|_| StateError::InvalidInput("execution instance is invalid"))?;
        if !matches!(desired.phase, ExecutionInstancePhase::Reserved)
            || desired.generation != 1
            || desired.created_at_ms != desired.updated_at_ms
        {
            return Err(StateError::InvalidInput(
                "new execution instance must be reserved",
            ));
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let serialized = serde_json::to_string(desired)?;
        let created = transaction.execute(
            "INSERT OR IGNORE INTO execution_instances
             (instance_id, idempotency_key, generation, state_json, created_at_ms, updated_at_ms)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                desired.instance_id,
                desired.idempotency_key,
                desired.generation as i64,
                serialized,
                desired.created_at_ms as i64,
                desired.updated_at_ms as i64,
            ],
        )? == 1;
        let instance = match load_execution_instance(&transaction, &desired.instance_id)? {
            Some(instance) => instance,
            None if !created => {
                let conflicting_id = transaction
                    .query_row(
                        "SELECT instance_id FROM execution_instances WHERE idempotency_key = ?1",
                        [&desired.idempotency_key],
                        |row| row.get::<_, String>(0),
                    )
                    .optional()?;
                if conflicting_id.is_some() {
                    return Err(StateError::InvalidTransition(
                        "execution instance idempotency key is already bound",
                    ));
                }
                return Err(StateError::CorruptState(
                    "reserved execution instance disappeared",
                ));
            }
            None => {
                return Err(StateError::CorruptState(
                    "reserved execution instance disappeared",
                ));
            }
        };
        if !same_execution_instance_identity(&instance, desired) {
            return Err(StateError::InvalidTransition(
                "execution instance identity conflicts with existing state",
            ));
        }
        transaction.commit()?;
        Ok(ExecutionInstanceReservation { instance, created })
    }

    pub fn get_execution_instance(&self, instance_id: &str) -> Result<Option<ExecutionInstance>> {
        validate_identifier(instance_id, "execution instance ID")?;
        let connection = self.connect()?;
        load_execution_instance(&connection, instance_id)
    }

    pub fn transition_execution_instance(
        &self,
        instance_id: &str,
        expected_generation: u64,
        phase: ExecutionInstancePhase,
        now_ms: u64,
    ) -> Result<ExecutionInstance> {
        validate_identifier(instance_id, "execution instance ID")?;
        if expected_generation == 0 || expected_generation > i64::MAX as u64 {
            return Err(StateError::InvalidInput(
                "execution instance generation is invalid",
            ));
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = load_execution_instance(&transaction, instance_id)?.ok_or(
            StateError::InvalidTransition("execution instance is not reserved"),
        )?;
        if current.generation != expected_generation {
            return Err(StateError::InvalidTransition(
                "execution instance generation is stale",
            ));
        }
        let next = current.transition(phase, now_ms).map_err(|_| {
            StateError::InvalidTransition("execution instance transition is invalid")
        })?;
        reject_execution_instance_provenance_conflict(&transaction, &next)?;
        let changed = transaction.execute(
            "UPDATE execution_instances
             SET generation = ?1, state_json = ?2, updated_at_ms = ?3
             WHERE instance_id = ?4 AND generation = ?5",
            params![
                next.generation as i64,
                serde_json::to_string(&next)?,
                next.updated_at_ms as i64,
                instance_id,
                expected_generation as i64,
            ],
        )?;
        if changed != 1 {
            return Err(StateError::InvalidTransition(
                "execution instance generation changed concurrently",
            ));
        }
        transaction.commit()?;
        Ok(next)
    }

    pub fn reserve_run(&self, task_id: &str) -> Result<RunReservation> {
        validate_identifier(task_id, "task ID")?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let creating = serde_json::to_string(&RunBindingState::Creating)?;
        let created = transaction.execute(
            "INSERT OR IGNORE INTO run_bindings(task_id, state_json) VALUES (?1, ?2)",
            params![task_id, creating],
        )? == 1;
        let state = load_run(&transaction, task_id)?
            .ok_or(StateError::CorruptState("reserved run binding disappeared"))?;
        transaction.commit()?;
        Ok(RunReservation { state, created })
    }

    pub fn get_run(&self, task_id: &str) -> Result<Option<RunBindingState>> {
        validate_identifier(task_id, "task ID")?;
        let connection = self.connect()?;
        load_run(&connection, task_id)
    }

    pub fn bind_run(&self, task_id: &str, run_id: &str) -> Result<RunBindingState> {
        validate_identifier(task_id, "task ID")?;
        validate_identifier(run_id, "run ID")?;
        self.transition_run(task_id, |current| match current {
            RunBindingState::Creating => Ok(RunBindingState::Running {
                run_id: run_id.to_owned(),
            }),
            RunBindingState::Running { run_id: existing } if existing == run_id => {
                Ok(current.clone())
            }
            RunBindingState::Running { .. }
            | RunBindingState::Completed { .. }
            | RunBindingState::Cancelled { .. }
            | RunBindingState::Indeterminate => Err(StateError::InvalidTransition(
                "task binding is not in the creating state",
            )),
        })
    }

    pub fn complete_run(
        &self,
        task_id: &str,
        run_id: &str,
        result: &str,
    ) -> Result<RunBindingState> {
        validate_identifier(task_id, "task ID")?;
        validate_identifier(run_id, "run ID")?;
        if result.chars().count() > MAX_RESULT_CHARS {
            return Err(StateError::InvalidInput("result text is too large"));
        }
        self.transition_run(task_id, |current| match current {
            RunBindingState::Running { run_id: existing } if existing == run_id => {
                Ok(RunBindingState::Completed {
                    run_id: run_id.to_owned(),
                    result: result.to_owned(),
                })
            }
            RunBindingState::Completed {
                run_id: existing,
                result: existing_result,
            } if existing == run_id && existing_result == result => Ok(current.clone()),
            RunBindingState::Running { .. } | RunBindingState::Completed { .. } => Err(
                StateError::InvalidTransition("Hermes run or completed result does not match"),
            ),
            _ => Err(StateError::InvalidTransition(
                "task binding is not in the running state",
            )),
        })
    }

    pub fn cancel_run(&self, task_id: &str, run_id: &str) -> Result<RunBindingState> {
        validate_identifier(task_id, "task ID")?;
        validate_identifier(run_id, "run ID")?;
        self.transition_run(task_id, |current| match current {
            RunBindingState::Running { run_id: existing } if existing == run_id => {
                Ok(RunBindingState::Cancelled {
                    run_id: run_id.to_owned(),
                })
            }
            RunBindingState::Cancelled { run_id: existing } if existing == run_id => {
                Ok(current.clone())
            }
            RunBindingState::Running { .. } | RunBindingState::Cancelled { .. } => Err(
                StateError::InvalidTransition("Hermes run ID does not match the task binding"),
            ),
            _ => Err(StateError::InvalidTransition(
                "task binding is not in the running state",
            )),
        })
    }

    pub fn mark_run_indeterminate(&self, task_id: &str) -> Result<RunBindingState> {
        validate_identifier(task_id, "task ID")?;
        self.transition_run(task_id, |current| match current {
            RunBindingState::Creating | RunBindingState::Running { .. } => {
                Ok(RunBindingState::Indeterminate)
            }
            RunBindingState::Indeterminate => Ok(current.clone()),
            _ => Err(StateError::InvalidTransition(
                "terminal task binding cannot become indeterminate",
            )),
        })
    }

    fn transition_run(
        &self,
        task_id: &str,
        transition: impl FnOnce(&RunBindingState) -> Result<RunBindingState>,
    ) -> Result<RunBindingState> {
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = load_run(&transaction, task_id)?
            .ok_or(StateError::InvalidTransition("task must be reserved first"))?;
        let next = transition(&current)?;
        if next != current {
            transaction.execute(
                "UPDATE run_bindings SET state_json = ?1 WHERE task_id = ?2",
                params![serde_json::to_string(&next)?, task_id],
            )?;
        }
        transaction.commit()?;
        Ok(next)
    }

    fn migrate(&self) -> Result<()> {
        let mut connection = self.connect()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let version: i64 = transaction.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if version > SCHEMA_VERSION {
            return Err(StateError::UnsupportedSchema(version));
        }
        match version {
            0 => {
                transaction.execute_batch(MIGRATION_1)?;
                transaction.pragma_update(None, "user_version", 1)?;
                require_v1_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_2)?;
                transaction.pragma_update(None, "user_version", 2)?;
                require_v2_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_3)?;
                transaction.pragma_update(None, "user_version", 3)?;
                require_v3_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_4)?;
                transaction.pragma_update(None, "user_version", 4)?;
                require_v4_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_5)?;
                transaction.pragma_update(None, "user_version", 5)?;
                require_v5_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_6)?;
                transaction.pragma_update(None, "user_version", 6)?;
                require_v6_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_7)?;
                transaction.pragma_update(None, "user_version", 7)?;
            }
            1 => {
                require_v1_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_2)?;
                transaction.pragma_update(None, "user_version", 2)?;
                require_v2_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_3)?;
                transaction.pragma_update(None, "user_version", 3)?;
                require_v3_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_4)?;
                transaction.pragma_update(None, "user_version", 4)?;
                require_v4_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_5)?;
                transaction.pragma_update(None, "user_version", 5)?;
                require_v5_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_6)?;
                transaction.pragma_update(None, "user_version", 6)?;
                require_v6_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_7)?;
                transaction.pragma_update(None, "user_version", 7)?;
            }
            2 => {
                require_v2_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_3)?;
                transaction.pragma_update(None, "user_version", 3)?;
                require_v3_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_4)?;
                transaction.pragma_update(None, "user_version", 4)?;
                require_v4_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_5)?;
                transaction.pragma_update(None, "user_version", 5)?;
                require_v5_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_6)?;
                transaction.pragma_update(None, "user_version", 6)?;
                require_v6_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_7)?;
                transaction.pragma_update(None, "user_version", 7)?;
            }
            3 => {
                require_v3_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_4)?;
                transaction.pragma_update(None, "user_version", 4)?;
                require_v4_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_5)?;
                transaction.pragma_update(None, "user_version", 5)?;
                require_v5_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_6)?;
                transaction.pragma_update(None, "user_version", 6)?;
                require_v6_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_7)?;
                transaction.pragma_update(None, "user_version", 7)?;
            }
            4 => {
                require_v4_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_5)?;
                transaction.pragma_update(None, "user_version", 5)?;
                require_v5_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_6)?;
                transaction.pragma_update(None, "user_version", 6)?;
                require_v6_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_7)?;
                transaction.pragma_update(None, "user_version", 7)?;
            }
            5 => {
                require_v5_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_6)?;
                transaction.pragma_update(None, "user_version", 6)?;
                require_v6_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_7)?;
                transaction.pragma_update(None, "user_version", 7)?;
            }
            6 => {
                require_v6_schema(&transaction)?;
                transaction.execute_batch(MIGRATION_7)?;
                transaction.pragma_update(None, "user_version", 7)?;
            }
            SCHEMA_VERSION => {}
            _ => return Err(StateError::UnsupportedSchema(version)),
        }
        transaction.commit()?;
        enable_wal(&connection)?;
        require_ready_schema(&connection)?;
        Ok(())
    }

    fn connect(&self) -> Result<Connection> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(Duration::from_secs(5))?;
        connection.execute_batch(
            "PRAGMA foreign_keys = ON;
             PRAGMA synchronous = FULL;",
        )?;
        Ok(connection)
    }
}

fn enable_wal(connection: &Connection) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match connection.query_row("PRAGMA journal_mode = WAL", [], |row| {
            row.get::<_, String>(0)
        }) {
            Ok(mode) if mode.eq_ignore_ascii_case("wal") => return Ok(()),
            Ok(_) => {
                return Err(StateError::CorruptState(
                    "fleet state database could not enable WAL mode",
                ));
            }
            Err(rusqlite::Error::SqliteFailure(error, _))
                if matches!(
                    error.code,
                    ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked
                ) && Instant::now() < deadline =>
            {
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(error) => return Err(StateError::Database(error)),
        }
    }
}

pub fn recovery_decision(reservation: &RunReservation) -> RunRecoveryDecision {
    if reservation.created && reservation.state == RunBindingState::Creating {
        return RunRecoveryDecision::StartNew;
    }
    match reservation.state.recovery_action() {
        RecoveryAction::ResumeKnownRun => RunRecoveryDecision::ResumeKnownRun,
        RecoveryAction::ReplayCompleted => RunRecoveryDecision::ReplayCompleted,
        RecoveryAction::FailCancelled => RunRecoveryDecision::FailCancelled,
        RecoveryAction::FailClosedIndeterminate => RunRecoveryDecision::FailClosedIndeterminate,
    }
}

fn validate_workflow_timestamp(now_ms: u64) -> Result<()> {
    if now_ms == 0 || now_ms > i64::MAX as u64 {
        return Err(StateError::InvalidInput("workflow timestamp is invalid"));
    }
    Ok(())
}

fn load_workflow_revision(
    connection: &Connection,
    workflow_id: &str,
    version: u64,
) -> Result<Option<WorkflowRevision>> {
    let stored = connection
        .query_row(
            "SELECT content_hash, document_json, created_at_ms
             FROM workflow_versions WHERE workflow_id = ?1 AND version = ?2",
            params![workflow_id, version as i64],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            },
        )
        .optional()?;
    stored
        .map(|(content_hash, document_json, created_at_ms)| {
            if created_at_ms <= 0 {
                return Err(StateError::CorruptState("workflow timestamp is invalid"));
            }
            let document = WorkflowDocument::parse_json(&document_json)
                .map_err(|_| StateError::CorruptState("workflow document is invalid"))?;
            if document.id() != workflow_id
                || document.content_hash() != content_hash
                || document.canonical_json().map_err(|_| {
                    StateError::CorruptState("workflow document cannot be serialized")
                })? != document_json
            {
                return Err(StateError::CorruptState(
                    "workflow revision contradicts its document",
                ));
            }
            Ok(WorkflowRevision {
                workflow_id: workflow_id.to_owned(),
                version,
                content_hash,
                document,
                created_at_ms: created_at_ms as u64,
            })
        })
        .transpose()
}

fn load_projection(
    connection: &Connection,
    source: &str,
    network_id: &str,
    device_id: &str,
) -> Result<Option<ManagedProjectionRecord>> {
    let stored = connection
        .query_row(
            "SELECT projection_generation, membership_generation, binding_generation, document_json
             FROM managed_projections
             WHERE source = ?1 AND network_id = ?2 AND device_id = ?3",
            params![source, network_id, device_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .optional()?;
    stored
        .map(
            |(projection_generation, membership_generation, binding_generation, json)| {
                let value: serde_json::Value = serde_json::from_str(&json)?;
                let document: ProjectionDocument = serde_json::from_value(value.clone())?;
                validate_projection(&document)?;
                if fleet_domain::canonical_projection_hash(&document) != document.content_hash {
                    return Err(StateError::CorruptState(
                        "managed projection content hash does not match its complete document",
                    ));
                }
                if serde_json::to_value(&document)? != value
                    || document.source != source
                    || document.network_id != network_id
                    || document.device_id != device_id
                    || document.projection_generation.get().to_string() != projection_generation
                    || document.membership_generation.get().to_string() != membership_generation
                    || document.binding_generation.get().to_string() != binding_generation
                {
                    return Err(StateError::CorruptState(
                        "managed projection row contradicts its complete document",
                    ));
                }
                Ok(ManagedProjectionRecord { document })
            },
        )
        .transpose()
}

fn load_observation(
    connection: &Connection,
    source: &str,
    network_id: &str,
    device_id: &str,
) -> Result<Option<ObservationRecord>> {
    let stored = connection
        .query_row(
            "SELECT observed_at_ms, received_at_ms, observation_json
             FROM node_observations
             WHERE source = ?1 AND network_id = ?2 AND device_id = ?3",
            params![source, network_id, device_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        )
        .optional()?;
    stored
        .map(|(observed_at_ms, received_at_ms, json)| {
            if observed_at_ms <= 0 || received_at_ms <= 0 {
                return Err(StateError::CorruptState(
                    "observation timestamps are outside the supported range",
                ));
            }
            let value: serde_json::Value = serde_json::from_str(&json)?;
            let observation: NodeObservation = serde_json::from_value(value.clone())?;
            validate_observation(&observation, received_at_ms as u64)
                .map_err(|_| StateError::CorruptState("persisted node observation is invalid"))?;
            let mut canonical_value = value.clone();
            if let serde_json::Value::Object(fields) = &mut canonical_value {
                fields
                    .entry("profiles".to_owned())
                    .or_insert_with(|| serde_json::Value::Array(Vec::new()));
            }
            if serde_json::to_value(&observation)? != canonical_value
                || observation.observed_at_ms != observed_at_ms as u64
            {
                return Err(StateError::CorruptState(
                    "node observation row contradicts its complete document",
                ));
            }
            Ok(ObservationRecord {
                observation,
                received_at_ms: received_at_ms as u64,
            })
        })
        .transpose()
}

fn inspect_projection_from(
    connection: &Connection,
    source: &str,
    network_id: &str,
    device_id: &str,
) -> Result<ProjectionView> {
    let generated = load_projection(connection, source, network_id, device_id)?;
    let mut statement = connection.prepare(
        "SELECT operation FROM operator_projection_denies
         WHERE source = ?1 AND network_id = ?2 AND device_id = ?3
         ORDER BY operation",
    )?;
    let denied = statement
        .query_map(params![source, network_id, device_id], |row| {
            row.get::<_, String>(0)
        })?
        .map(|value| {
            let value = value?;
            FleetOperation::parse(&value).map_err(|_| rusqlite::Error::InvalidQuery)
        })
        .collect::<std::result::Result<BTreeSet<_>, _>>()?;
    let effective_operations = generated
        .as_ref()
        .filter(|record| record.document.operation == ManagedOperation::Upsert)
        .map(|record| {
            record
                .document
                .generated_operations
                .difference(&denied)
                .copied()
                .collect()
        })
        .unwrap_or_default();
    Ok(ProjectionView {
        generated,
        effective_operations,
        operator_denied_operations: denied,
    })
}

fn inspect_node_from(
    connection: &Connection,
    source: &str,
    network_id: &str,
    device_id: &str,
    now_ms: u64,
    policy: ReadinessPolicy,
) -> Result<NodeOperationalView> {
    let projection = load_projection(connection, source, network_id, device_id)?;
    let admission_generation = projection
        .as_ref()
        .map(|record| record.document.projection_generation.get());
    let managed_state = managed_state(projection.as_ref());
    let observation = load_observation(connection, source, network_id, device_id)?;
    if observation
        .as_ref()
        .is_some_and(|record| Some(record.observation.admission_generation) != admission_generation)
    {
        return Err(StateError::CorruptState(
            "stored observation admission generation does not match projection",
        ));
    }
    let available_worker_slots = observation
        .as_ref()
        .map(|record| record.observation.capacity.available_worker_slots());
    let readiness = evaluate_readiness(managed_state, observation.as_ref(), now_ms, policy);
    Ok(NodeOperationalView {
        managed_state,
        admission_generation,
        observation,
        available_worker_slots,
        readiness,
    })
}

fn managed_state(record: Option<&ManagedProjectionRecord>) -> ManagedNodeState {
    match record.map(|record| record.document.operation) {
        None => ManagedNodeState::Unknown,
        Some(ManagedOperation::Upsert) => ManagedNodeState::Active,
        Some(ManagedOperation::Disable) => ManagedNodeState::Disabled,
        Some(ManagedOperation::Remove) => ManagedNodeState::Removed,
    }
}

fn load_execution_instance(
    connection: &Connection,
    instance_id: &str,
) -> Result<Option<ExecutionInstance>> {
    let stored = connection
        .query_row(
            "SELECT idempotency_key, generation, state_json, created_at_ms, updated_at_ms
             FROM execution_instances WHERE instance_id = ?1",
            [instance_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, i64>(4)?,
                ))
            },
        )
        .optional()?;
    stored
        .map(
            |(idempotency_key, generation, state_json, created_at_ms, updated_at_ms)| {
                if generation <= 0 || created_at_ms <= 0 || updated_at_ms < created_at_ms {
                    return Err(StateError::CorruptState(
                        "execution instance columns are invalid",
                    ));
                }
                let instance: ExecutionInstance = serde_json::from_str(&state_json)?;
                instance.validate().map_err(|_| {
                    StateError::CorruptState("execution instance document is invalid")
                })?;
                if instance.instance_id != instance_id
                    || instance.idempotency_key != idempotency_key
                    || instance.generation != generation as u64
                    || instance.created_at_ms != created_at_ms as u64
                    || instance.updated_at_ms != updated_at_ms as u64
                    || serde_json::to_string(&instance)? != state_json
                {
                    return Err(StateError::CorruptState(
                        "execution instance columns contradict its document",
                    ));
                }
                Ok(instance)
            },
        )
        .transpose()
}

fn reject_execution_instance_provenance_conflict(
    connection: &Connection,
    instance: &ExecutionInstance,
) -> Result<()> {
    let state_json = serde_json::to_string(instance)?;
    let conflict = connection
        .query_row(
            "SELECT instance_id FROM execution_instances
             WHERE instance_id <> ?1 AND (
                 (
                     json_extract(state_json, '$.phase.backend_kind') = json_extract(?2, '$.phase.backend_kind')
                     AND json_extract(state_json, '$.phase.realization_id') = json_extract(?2, '$.phase.realization_id')
                     AND json_extract(?2, '$.phase.backend_kind') IS NOT NULL
                 ) OR (
                     json_extract(state_json, '$.phase.keryx_task_id') = json_extract(?2, '$.phase.keryx_task_id')
                     AND json_extract(?2, '$.phase.keryx_task_id') IS NOT NULL
                 )
             ) LIMIT 1",
            params![instance.instance_id, state_json],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    if conflict.is_some() {
        return Err(StateError::InvalidTransition(
            "execution instance provenance is already owned",
        ));
    }
    Ok(())
}

fn same_execution_instance_identity(left: &ExecutionInstance, right: &ExecutionInstance) -> bool {
    left.instance_id == right.instance_id
        && left.idempotency_key == right.idempotency_key
        && left.recipe_hash == right.recipe_hash
        && left.capabilities_hash == right.capabilities_hash
        && left.target == right.target
}

fn load_run(connection: &Connection, task_id: &str) -> Result<Option<RunBindingState>> {
    let json = connection
        .query_row(
            "SELECT state_json FROM run_bindings WHERE task_id = ?1",
            [task_id],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    json.map(|value| {
        let persisted: serde_json::Value = serde_json::from_str(&value)?;
        let state: RunBindingState = serde_json::from_value(persisted.clone())?;
        validate_run_state(&state)?;
        if serde_json::to_value(&state)? != persisted {
            return Err(StateError::CorruptState(
                "run binding contains noncanonical or unknown fields",
            ));
        }
        Ok(state)
    })
    .transpose()
}

fn require_v1_schema(connection: &Connection) -> Result<()> {
    let mut statement = connection.prepare(
        "SELECT name FROM sqlite_master
         WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )?;
    let tables = statement
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if tables
        != [
            "fleet_state_schema",
            "managed_projections",
            "operator_projection_denies",
            "run_bindings",
        ]
    {
        return Err(StateError::CorruptState(
            "fleet state schema v1 has unexpected tables",
        ));
    }
    let versions = connection
        .prepare("SELECT version FROM fleet_state_schema ORDER BY version")?
        .query_map([], |row| row.get::<_, i64>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if versions != [1] {
        return Err(StateError::CorruptState(
            "fleet state schema v1 marker is invalid",
        ));
    }
    require_table_shape_with_definition(
        connection,
        "fleet_state_schema",
        &[("version", "INTEGER", 1)],
        FLEET_STATE_SCHEMA_V1_SQL,
    )?;
    require_table_shape(
        connection,
        "managed_projections",
        &[
            ("source", "TEXT", 1),
            ("network_id", "TEXT", 2),
            ("device_id", "TEXT", 3),
            ("projection_generation", "TEXT", 0),
            ("membership_generation", "TEXT", 0),
            ("binding_generation", "TEXT", 0),
            ("document_json", "TEXT", 0),
        ],
    )?;
    require_table_shape(
        connection,
        "operator_projection_denies",
        &[
            ("source", "TEXT", 1),
            ("network_id", "TEXT", 2),
            ("device_id", "TEXT", 3),
            ("operation", "TEXT", 4),
        ],
    )?;
    require_table_shape(
        connection,
        "run_bindings",
        &[("task_id", "TEXT", 1), ("state_json", "TEXT", 0)],
    )?;
    let integrity: String = connection.query_row("PRAGMA integrity_check", [], |row| row.get(0))?;
    if integrity != "ok" {
        return Err(StateError::CorruptState(
            "fleet state database integrity check failed",
        ));
    }
    Ok(())
}

fn require_v2_schema(connection: &Connection) -> Result<()> {
    require_complete_schema(connection, 2, FLEET_STATE_SCHEMA_V2_SQL)
}

fn require_v3_schema(connection: &Connection) -> Result<()> {
    require_complete_schema(connection, 3, FLEET_STATE_SCHEMA_V3_SQL)
}

fn require_v4_schema(connection: &Connection) -> Result<()> {
    require_complete_schema(connection, 4, FLEET_STATE_SCHEMA_V4_SQL)
}

fn require_v5_schema(connection: &Connection) -> Result<()> {
    require_complete_schema(connection, 5, FLEET_STATE_SCHEMA_V5_SQL)
}

fn require_v6_schema(connection: &Connection) -> Result<()> {
    require_complete_schema(connection, 6, FLEET_STATE_SCHEMA_V6_SQL)
}

fn require_ready_schema(connection: &Connection) -> Result<()> {
    require_complete_schema(connection, SCHEMA_VERSION, FLEET_STATE_SCHEMA_SQL)?;
    require_index_definition(
        connection,
        "execution_instances_backend_realization_owner",
        EXECUTION_BACKEND_OWNER_INDEX_SQL,
    )?;
    require_index_definition(
        connection,
        "execution_instances_keryx_task_owner",
        EXECUTION_KERYX_OWNER_INDEX_SQL,
    )
}

fn require_complete_schema(
    connection: &Connection,
    expected_version: i64,
    expected_marker_sql: &str,
) -> Result<()> {
    let mut statement = connection.prepare(
        "SELECT name FROM sqlite_master
         WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
    )?;
    let tables = statement
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let expected_tables = if expected_version >= 6 {
        vec![
            "execution_instances",
            "fleet_state_schema",
            "managed_node_aliases",
            "managed_projections",
            "node_observations",
            "operator_projection_denies",
            "run_bindings",
            "workflow_definitions",
            "workflow_versions",
        ]
    } else if expected_version >= 5 {
        vec![
            "fleet_state_schema",
            "managed_node_aliases",
            "managed_projections",
            "node_observations",
            "operator_projection_denies",
            "run_bindings",
            "workflow_definitions",
            "workflow_versions",
        ]
    } else if expected_version >= 4 {
        vec![
            "fleet_state_schema",
            "managed_node_aliases",
            "managed_projections",
            "node_observations",
            "operator_projection_denies",
            "run_bindings",
        ]
    } else {
        vec![
            "fleet_state_schema",
            "managed_projections",
            "node_observations",
            "operator_projection_denies",
            "run_bindings",
        ]
    };
    if tables != expected_tables {
        return Err(StateError::CorruptState(
            "fleet state schema has unexpected tables",
        ));
    }
    let versions = connection
        .prepare("SELECT version FROM fleet_state_schema ORDER BY version")?
        .query_map([], |row| row.get::<_, i64>(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if versions != [expected_version] {
        return Err(StateError::CorruptState(
            "fleet state schema marker is invalid",
        ));
    }
    require_table_shape_with_definition(
        connection,
        "fleet_state_schema",
        &[("version", "INTEGER", 1)],
        expected_marker_sql,
    )?;
    require_table_shape(
        connection,
        "managed_projections",
        &[
            ("source", "TEXT", 1),
            ("network_id", "TEXT", 2),
            ("device_id", "TEXT", 3),
            ("projection_generation", "TEXT", 0),
            ("membership_generation", "TEXT", 0),
            ("binding_generation", "TEXT", 0),
            ("document_json", "TEXT", 0),
        ],
    )?;
    if expected_version >= 4 {
        require_table_shape(
            connection,
            "managed_node_aliases",
            &[
                ("source", "TEXT", 1),
                ("network_id", "TEXT", 2),
                ("device_id", "TEXT", 3),
                ("binding_generation", "TEXT", 0),
                ("alias", "TEXT", 0),
            ],
        )?;
    }
    if expected_version >= 6 {
        require_table_shape(
            connection,
            "execution_instances",
            &[
                ("instance_id", "TEXT", 1),
                ("idempotency_key", "TEXT", 0),
                ("generation", "INTEGER", 0),
                ("state_json", "TEXT", 0),
                ("created_at_ms", "INTEGER", 0),
                ("updated_at_ms", "INTEGER", 0),
            ],
        )?;
    }
    if expected_version >= 5 {
        require_table_shape(
            connection,
            "workflow_definitions",
            &[
                ("workflow_id", "TEXT", 1),
                ("latest_version", "INTEGER", 0),
                ("deleted", "INTEGER", 0),
                ("created_at_ms", "INTEGER", 0),
                ("updated_at_ms", "INTEGER", 0),
            ],
        )?;
        require_table_shape(
            connection,
            "workflow_versions",
            &[
                ("workflow_id", "TEXT", 1),
                ("version", "INTEGER", 2),
                ("content_hash", "TEXT", 0),
                ("document_json", "TEXT", 0),
                ("created_at_ms", "INTEGER", 0),
            ],
        )?;
    }
    require_table_shape(
        connection,
        "node_observations",
        &[
            ("source", "TEXT", 1),
            ("network_id", "TEXT", 2),
            ("device_id", "TEXT", 3),
            ("observed_at_ms", "INTEGER", 0),
            ("received_at_ms", "INTEGER", 0),
            ("observation_json", "TEXT", 0),
        ],
    )?;
    require_table_shape(
        connection,
        "operator_projection_denies",
        &[
            ("source", "TEXT", 1),
            ("network_id", "TEXT", 2),
            ("device_id", "TEXT", 3),
            ("operation", "TEXT", 4),
        ],
    )?;
    require_table_shape(
        connection,
        "run_bindings",
        &[("task_id", "TEXT", 1), ("state_json", "TEXT", 0)],
    )?;
    let integrity: String = connection.query_row("PRAGMA integrity_check", [], |row| row.get(0))?;
    if integrity != "ok" {
        return Err(StateError::CorruptState(
            "fleet state database integrity check failed",
        ));
    }
    let has_foreign_key_violation = connection
        .prepare("PRAGMA foreign_key_check")?
        .query([])?
        .next()?
        .is_some();
    if has_foreign_key_violation {
        return Err(StateError::CorruptState(
            "fleet state database has invalid foreign-key references",
        ));
    }
    Ok(())
}

fn require_table_shape(
    connection: &Connection,
    table: &'static str,
    expected: &[(&str, &str, i64)],
) -> Result<()> {
    let expected_definition = match table {
        "execution_instances" => EXECUTION_INSTANCES_SQL,
        "fleet_state_schema" => FLEET_STATE_SCHEMA_SQL,
        "managed_node_aliases" => MANAGED_NODE_ALIASES_SQL,
        "managed_projections" => MANAGED_PROJECTIONS_SQL,
        "node_observations" => NODE_OBSERVATIONS_SQL,
        "operator_projection_denies" => OPERATOR_DENIES_SQL,
        "run_bindings" => RUN_BINDINGS_SQL,
        "workflow_definitions" => WORKFLOW_DEFINITIONS_SQL,
        "workflow_versions" => WORKFLOW_VERSIONS_SQL,
        _ => return Err(StateError::CorruptState("unknown fleet state table")),
    };
    require_table_shape_with_definition(connection, table, expected, expected_definition)
}

fn require_table_shape_with_definition(
    connection: &Connection,
    table: &'static str,
    expected: &[(&str, &str, i64)],
    expected_definition: &str,
) -> Result<()> {
    let sql = format!("PRAGMA table_info({table})");
    let actual = connection
        .prepare(&sql)?
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(5)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let expected = expected
        .iter()
        .map(|(name, kind, primary_key)| ((*name).to_owned(), (*kind).to_owned(), *primary_key))
        .collect::<Vec<_>>();
    let definition: String = connection.query_row(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?1",
        [table],
        |row| row.get(0),
    )?;
    if actual != expected || normalized_sql(&definition) != normalized_sql(expected_definition) {
        return Err(StateError::CorruptState(
            "fleet state table definition is invalid",
        ));
    }
    Ok(())
}

fn require_index_definition(
    connection: &Connection,
    index: &'static str,
    expected_definition: &str,
) -> Result<()> {
    let definition = connection
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?1",
            [index],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    if definition.as_deref().map(normalized_sql).as_deref()
        != Some(normalized_sql(expected_definition).as_str())
    {
        return Err(StateError::CorruptState(
            "fleet state ownership index is invalid",
        ));
    }
    Ok(())
}

fn normalized_sql(sql: &str) -> String {
    sql.chars()
        .filter(|character| !character.is_whitespace() && *character != ';')
        .flat_map(char::to_lowercase)
        .collect()
}

fn validate_remote_authority_epoch(epoch: &RemoteObservationAuthorityEpoch) -> Result<()> {
    for value in [&epoch.binding_id, &epoch.authenticated_peer_id] {
        validate_identifier(value, "remote observation authority identifier")?;
    }
    if epoch.binding_generation == 0 || epoch.projection_generation == 0 {
        return Err(StateError::InvalidInput(
            "remote observation authority generations must be positive",
        ));
    }
    Ok(())
}

fn remote_observation_authority(
    projection: Option<&ManagedProjectionRecord>,
) -> Result<RemoteObservationAuthorityEpoch> {
    if managed_state(projection) != ManagedNodeState::Active {
        return Err(StateError::InvalidTransition(
            "remote observation identity is not an active managed node",
        ));
    }
    let document = &projection
        .ok_or(StateError::CorruptState(
            "active remote observation identity has no managed projection",
        ))?
        .document;
    let epoch = RemoteObservationAuthorityEpoch {
        binding_id: document.provenance.get("binding_id").cloned().ok_or(
            StateError::InvalidTransition(
                "managed projection cannot authorize remote observation publication",
            ),
        )?,
        authenticated_peer_id: document
            .provenance
            .get("authenticated_peer_id")
            .cloned()
            .ok_or(StateError::InvalidTransition(
                "managed projection cannot authorize remote observation publication",
            ))?,
        binding_generation: document.binding_generation.get(),
        projection_generation: document.projection_generation.get(),
    };
    validate_remote_authority_epoch(&epoch)?;
    Ok(epoch)
}

fn record_observation_in_transaction(
    connection: &Connection,
    source: &str,
    network_id: &str,
    device_id: &str,
    projection: Option<&ManagedProjectionRecord>,
    observation: NodeObservation,
    received_at_ms: u64,
) -> Result<ObservationApply> {
    if managed_state(projection) != ManagedNodeState::Active {
        return Err(StateError::InvalidTransition(
            "observation identity is not an active managed node",
        ));
    }
    let active_admission_generation = projection
        .map(|record| record.document.projection_generation.get())
        .ok_or(StateError::CorruptState(
            "active observation identity has no managed projection",
        ))?;
    if observation.admission_generation != active_admission_generation {
        return Err(StateError::InvalidTransition(
            "observation admission generation does not match active projection",
        ));
    }

    let current = load_observation(connection, source, network_id, device_id)?;
    let incoming_time = observation.observed_at_ms;
    let clock_rebased = current.as_ref().is_some_and(|record| {
        record.observation.observed_at_ms
            > received_at_ms.saturating_add(MAX_OBSERVATION_FUTURE_SKEW_MS)
    });
    let outcome = match current.as_ref() {
        None => ObservationOutcome::Recorded,
        Some(_) if clock_rebased => ObservationOutcome::Recorded,
        Some(current) if incoming_time < current.observation.observed_at_ms => {
            ObservationOutcome::Stale
        }
        Some(current) if incoming_time == current.observation.observed_at_ms => {
            if observation == current.observation {
                ObservationOutcome::AlreadyRecorded
            } else {
                ObservationOutcome::Conflict
            }
        }
        Some(_) => ObservationOutcome::Recorded,
    };

    let record = if outcome == ObservationOutcome::Recorded {
        let record = ObservationRecord {
            observation,
            received_at_ms,
        };
        connection.execute(
            "INSERT INTO node_observations (
                source, network_id, device_id, observed_at_ms,
                received_at_ms, observation_json
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(source, network_id, device_id) DO UPDATE SET
                observed_at_ms = excluded.observed_at_ms,
                received_at_ms = excluded.received_at_ms,
                observation_json = excluded.observation_json",
            params![
                source,
                network_id,
                device_id,
                i64::try_from(record.observation.observed_at_ms).map_err(|_| {
                    StateError::InvalidInput("observation timestamp exceeds SQLite range")
                })?,
                i64::try_from(record.received_at_ms).map_err(|_| {
                    StateError::InvalidInput("receipt timestamp exceeds SQLite range")
                })?,
                serde_json::to_string(&record.observation)?,
            ],
        )?;
        record
    } else {
        current.ok_or(StateError::CorruptState(
            "non-recorded observation has no current state",
        ))?
    };
    Ok(ObservationApply { outcome, record })
}

fn validate_observation(observation: &NodeObservation, received_at_ms: u64) -> Result<()> {
    observation
        .validate()
        .map_err(|_| StateError::InvalidInput("node observation is invalid"))?;
    if received_at_ms == 0
        || received_at_ms > i64::MAX as u64
        || observation.observed_at_ms > i64::MAX as u64
        || observation.observed_at_ms
            > received_at_ms.saturating_add(MAX_OBSERVATION_FUTURE_SKEW_MS)
    {
        return Err(StateError::InvalidInput(
            "observation timestamps are outside the supported range",
        ));
    }
    Ok(())
}

fn validate_alias(alias: &str) -> Result<()> {
    let character_count = alias.chars().count();
    if !(1..=128).contains(&character_count)
        || alias.trim() != alias
        || alias.chars().any(|character| {
            character.is_control()
                || matches!(
                    character,
                    '\u{200b}'
                        | '\u{200c}'
                        | '\u{200d}'
                        | '\u{2060}'
                        | '\u{202a}'
                        | '\u{202b}'
                        | '\u{202c}'
                        | '\u{202d}'
                        | '\u{202e}'
                        | '\u{2066}'
                        | '\u{2067}'
                        | '\u{2068}'
                        | '\u{2069}'
                        | '\u{feff}'
                )
        })
    {
        return Err(StateError::InvalidInput(
            "alias must be bounded visible display text without surrounding whitespace",
        ));
    }
    Ok(())
}

fn load_validated_alias(
    connection: &Connection,
    source: &str,
    network_id: &str,
    device_id: &str,
    expected_binding_generation: Option<u64>,
) -> Result<Option<String>> {
    let alias = connection
        .query_row(
            "SELECT binding_generation, alias FROM managed_node_aliases
             WHERE source = ?1 AND network_id = ?2 AND device_id = ?3",
            params![source, network_id, device_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()?;
    let Some((binding_generation, alias)) = alias else {
        return Ok(None);
    };
    let expected_binding_generation = expected_binding_generation.ok_or(
        StateError::CorruptState("managed alias has no active projection"),
    )?;
    if binding_generation != expected_binding_generation.to_string() {
        return Err(StateError::CorruptState(
            "managed alias binding generation does not match its projection",
        ));
    }
    validate_persisted_alias(&alias)?;
    Ok(Some(alias))
}

fn validate_persisted_alias(alias: &str) -> Result<()> {
    validate_alias(alias).map_err(|_| {
        StateError::CorruptState("managed alias contains invalid persisted display text")
    })
}

fn validate_run_state(state: &RunBindingState) -> Result<()> {
    match state {
        RunBindingState::Creating | RunBindingState::Indeterminate => Ok(()),
        RunBindingState::Running { run_id } | RunBindingState::Cancelled { run_id } => {
            validate_identifier(run_id, "run ID")
        }
        RunBindingState::Completed { run_id, result } => {
            validate_identifier(run_id, "run ID")?;
            if result.chars().count() > MAX_RESULT_CHARS {
                return Err(StateError::CorruptState(
                    "completed run result exceeds the persisted bound",
                ));
            }
            Ok(())
        }
    }
}

fn validate_projection(document: &ProjectionDocument) -> Result<()> {
    validate_key(&document.source, &document.network_id, &document.device_id)?;
    if document.content_hash.len() != 64
        || !document
            .content_hash
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(StateError::InvalidInput(
            "content hash must be lowercase SHA-256 hexadecimal",
        ));
    }
    let required = [
        ("source", document.source.as_str()),
        ("network_id", document.network_id.as_str()),
        ("device_id", document.device_id.as_str()),
    ];
    for (field, expected) in required {
        if document.provenance.get(field).map(String::as_str) != Some(expected) {
            return Err(StateError::InvalidInput(
                "projection provenance does not match its identity key",
            ));
        }
    }
    if document.provenance.keys().any(|key| {
        !matches!(
            key.as_str(),
            "source"
                | "network_id"
                | "device_id"
                | "snapshot"
                | "controller"
                | "binding_id"
                | "authenticated_peer_id"
        )
    }) {
        return Err(StateError::InvalidInput(
            "projection provenance has unknown fields",
        ));
    }
    if let Some(controller) = document.provenance.get("controller") {
        if controller != "nodescale" {
            return Err(StateError::InvalidInput(
                "projection controller provenance is invalid",
            ));
        }
    }
    if let Some(snapshot) = document.provenance.get("snapshot") {
        let parsed = snapshot.parse::<u64>().map_err(|_| {
            StateError::InvalidInput("projection snapshot must be a canonical generation")
        })?;
        if parsed == 0 || parsed.to_string() != *snapshot {
            return Err(StateError::InvalidInput(
                "projection snapshot must be a canonical generation",
            ));
        }
    }
    apply_projection(None, document.clone())
        .map(|_| ())
        .map_err(|_| StateError::InvalidInput("generated authority is invalid"))
}

fn validate_key(source: &str, network_id: &str, device_id: &str) -> Result<()> {
    if source != "nodescale" {
        return Err(StateError::InvalidInput("source must be nodescale"));
    }
    validate_identifier(network_id, "network ID")?;
    validate_identifier(device_id, "device ID")
}

fn validate_profile_query(profile_name: &str, required_version: Option<&str>) -> Result<()> {
    let valid_name = !profile_name.is_empty()
        && profile_name.len() <= MAX_PROFILE_NAME_BYTES
        && !matches!(profile_name, "." | "..")
        && profile_name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'));
    let valid_version = required_version.is_none_or(|version| {
        !version.is_empty()
            && version.len() <= MAX_PROFILE_VERSION_BYTES
            && version.bytes().all(|byte| byte.is_ascii_graphic())
    });
    if !valid_name || !valid_version {
        return Err(StateError::InvalidInput(
            "profile query is not bounded canonical text",
        ));
    }
    Ok(())
}

fn validate_profile_content_digest(value: &str) -> Result<()> {
    if value.len() != PROFILE_CONTENT_DIGEST_BYTES
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    {
        return Err(StateError::InvalidInput(
            "profile content digest must be lowercase SHA-256 hexadecimal",
        ));
    }
    Ok(())
}

fn validate_identifier(value: &str, _label: &'static str) -> Result<()> {
    if value.is_empty()
        || value.chars().count() > MAX_IDENTIFIER_CHARS
        || value.trim() != value
        || value
            .chars()
            .any(|character| character.is_control() || character.is_whitespace())
    {
        return Err(StateError::InvalidInput(
            "identifier is not bounded canonical text",
        ));
    }
    Ok(())
}
