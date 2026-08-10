//! Transport- and storage-independent domain semantics for Hermes Fleet.
//!
//! Compatibility types preserve proven Python behavior while the Rust domain
//! also owns typed node observations and scheduler-readiness derivation. This
//! crate does not own Keryx transport, Nodescale state, persistence, scheduling,
//! profiles, Sentinel, or UI.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// The complete operation vocabulary already exposed by Python Hermes Fleet.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub enum FleetOperation {
    #[serde(rename = "fleet.health")]
    Health,
    #[serde(rename = "fleet.inventory")]
    Inventory,
    #[serde(rename = "fleet.message")]
    Message,
    #[serde(rename = "fleet.hermes.run")]
    HermesRun,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ParseOperationError;

impl FleetOperation {
    pub fn parse(value: &str) -> Result<Self, ParseOperationError> {
        match value {
            "fleet.health" => Ok(Self::Health),
            "fleet.inventory" => Ok(Self::Inventory),
            "fleet.message" => Ok(Self::Message),
            "fleet.hermes.run" => Ok(Self::HermesRun),
            _ => Err(ParseOperationError),
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Health => "fleet.health",
            Self::Inventory => "fleet.inventory",
            Self::Message => "fleet.message",
            Self::HermesRun => "fleet.hermes.run",
        }
    }

    pub const fn is_executable(self) -> bool {
        matches!(self, Self::HermesRun)
    }

    /// Nodescale-managed projections may generate only this baseline.
    pub const fn managed_baseline() -> [Self; 3] {
        [Self::Health, Self::Inventory, Self::Message]
    }
}

/// Locally configured or managed node material needed for pure selection.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Node {
    pub name: String,
    pub peer_id: String,
    pub enabled: bool,
    pub priority: u32,
    pub tags: BTreeSet<String>,
    pub allowed: BTreeSet<FleetOperation>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SelectionError {
    InvalidSelector,
    MixedSelectors,
    UnknownNode,
    UnknownTag,
}

/// Match Python Fleet's normalized exact-name or AND-tag selection.
pub fn select_nodes<'a>(
    nodes: &'a [Node],
    names: &[String],
    tags: &[String],
) -> Result<Vec<&'a Node>, SelectionError> {
    let names = normalize_selectors(names)?;
    let tags = normalize_selectors(tags)?;
    if !names.is_empty() && !tags.is_empty() {
        return Err(SelectionError::MixedSelectors);
    }

    let selected = if !names.is_empty() {
        let by_name = nodes
            .iter()
            .map(|node| (node.name.as_str(), node))
            .collect::<BTreeMap<_, _>>();
        if names
            .iter()
            .any(|name| !by_name.contains_key(name.as_str()))
        {
            return Err(SelectionError::UnknownNode);
        }
        names
            .iter()
            .filter_map(|name| by_name.get(name.as_str()).copied())
            .collect::<Vec<_>>()
    } else if !tags.is_empty() {
        let configured_tags = nodes
            .iter()
            .flat_map(|node| node.tags.iter())
            .collect::<BTreeSet<_>>();
        if tags.iter().any(|tag| !configured_tags.contains(tag)) {
            return Err(SelectionError::UnknownTag);
        }
        nodes
            .iter()
            .filter(|node| tags.iter().all(|tag| node.tags.contains(tag)))
            .collect::<Vec<_>>()
    } else {
        nodes.iter().collect::<Vec<_>>()
    };

    let mut enabled = selected
        .into_iter()
        .filter(|node| node.enabled)
        .collect::<Vec<_>>();
    enabled.sort_by(|left, right| {
        right
            .priority
            .cmp(&left.priority)
            .then_with(|| left.name.cmp(&right.name))
    });
    Ok(enabled)
}

fn normalize_selectors(values: &[String]) -> Result<Vec<String>, SelectionError> {
    let mut normalized = Vec::new();
    for value in values {
        let value = value.trim().to_lowercase();
        if value.is_empty() {
            return Err(SelectionError::InvalidSelector);
        }
        if !normalized.contains(&value) {
            normalized.push(value);
        }
    }
    Ok(normalized)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidGeneratedAuthority;

/// Generated authority and operator-local deny remain distinct.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EffectiveAuthority {
    generated: BTreeSet<FleetOperation>,
    denied: BTreeSet<FleetOperation>,
}

impl EffectiveAuthority {
    pub fn new(
        generated: BTreeSet<FleetOperation>,
        denied: BTreeSet<FleetOperation>,
    ) -> Result<Self, InvalidGeneratedAuthority> {
        let baseline = FleetOperation::managed_baseline().into_iter().collect();
        if !generated.is_subset(&baseline) {
            return Err(InvalidGeneratedAuthority);
        }
        Ok(Self { generated, denied })
    }

    pub fn generated(&self) -> &BTreeSet<FleetOperation> {
        &self.generated
    }

    pub fn denied(&self) -> &BTreeSet<FleetOperation> {
        &self.denied
    }

    pub fn allowed(&self) -> BTreeSet<FleetOperation> {
        self.generated.difference(&self.denied).copied().collect()
    }
}

/// Positive u64 rendered exactly as the Python wire contract's canonical string.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Generation(u64);

impl Generation {
    pub const fn get(self) -> u64 {
        self.0
    }
}

impl Serialize for Generation {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.0.to_string())
    }
}

impl<'de> Deserialize<'de> for Generation {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        let parsed = value
            .parse::<u64>()
            .map_err(|_| de::Error::custom("generation must be a canonical positive u64 string"))?;
        if parsed == 0 || parsed.to_string() != value {
            return Err(de::Error::custom(
                "generation must be a canonical positive u64 string",
            ));
        }
        Ok(Self(parsed))
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ManagedOperation {
    Upsert,
    Disable,
    Remove,
}

/// Canonical managed projection material for one Fleet-owned identity key.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ProjectionDocument {
    pub source: String,
    pub network_id: String,
    pub device_id: String,
    pub projection_generation: Generation,
    pub membership_generation: Generation,
    pub binding_generation: Generation,
    pub content_hash: String,
    pub operation: ManagedOperation,
    pub generated_operations: BTreeSet<FleetOperation>,
    pub provenance: BTreeMap<String, String>,
}

#[derive(Serialize)]
struct CanonicalProjectionMaterial<'a> {
    source: &'a str,
    network_id: &'a str,
    device_id: &'a str,
    projection_generation: Generation,
    membership_generation: Generation,
    binding_generation: Generation,
    operation: ManagedOperation,
    generated_operations: &'a BTreeSet<FleetOperation>,
    provenance: &'a BTreeMap<String, String>,
}

/// Return the Python/Nodescale canonical SHA-256 digest for a projection body.
pub fn canonical_projection_hash(document: &ProjectionDocument) -> String {
    let material = CanonicalProjectionMaterial {
        source: &document.source,
        network_id: &document.network_id,
        device_id: &document.device_id,
        projection_generation: document.projection_generation,
        membership_generation: document.membership_generation,
        binding_generation: document.binding_generation,
        operation: document.operation,
        generated_operations: &document.generated_operations,
        provenance: &document.provenance,
    };
    let value = serde_json::to_value(material).expect("projection material is serializable");
    let mut canonical = String::new();
    write_canonical_json(&value, &mut canonical);
    format!("{:x}", Sha256::digest(canonical.as_bytes()))
}

fn write_canonical_json(value: &Value, output: &mut String) {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
        Value::Number(value) => output.push_str(&value.to_string()),
        Value::String(value) => write_ascii_json_string(value, output),
        Value::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index != 0 {
                    output.push(',');
                }
                write_canonical_json(value, output);
            }
            output.push(']');
        }
        Value::Object(values) => {
            output.push('{');
            let mut entries: Vec<_> = values.iter().collect();
            entries.sort_unstable_by(|left, right| left.0.cmp(right.0));
            for (index, (key, value)) in entries.into_iter().enumerate() {
                if index != 0 {
                    output.push(',');
                }
                write_ascii_json_string(key, output);
                output.push(':');
                write_canonical_json(value, output);
            }
            output.push('}');
        }
    }
}

fn write_ascii_json_string(value: &str, output: &mut String) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0C}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            value if value <= '\u{1f}' || value > '\u{7e}' => {
                let mut units = [0_u16; 2];
                for unit in value.encode_utf16(&mut units) {
                    output.push_str(&format!("\\u{unit:04x}"));
                }
            }
            value => output.push(value),
        }
    }
    output.push('"');
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ManagedProjectionRecord {
    pub document: ProjectionDocument,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ApplyOutcome {
    Applied,
    AlreadyApplied,
    Conflict,
    Stale,
    Regression,
    Gap,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectionApply {
    pub outcome: ApplyOutcome,
    pub record: ManagedProjectionRecord,
}

/// Apply Python Fleet's replay/stale/regression/gap rules for one managed key.
pub fn apply_projection(
    current: Option<&ManagedProjectionRecord>,
    desired: ProjectionDocument,
) -> Result<ProjectionApply, InvalidGeneratedAuthority> {
    validate_projection_authority(&desired)?;
    let Some(current) = current else {
        return Ok(ProjectionApply {
            outcome: ApplyOutcome::Applied,
            record: ManagedProjectionRecord { document: desired },
        });
    };

    let incoming = desired.projection_generation.get();
    let existing = current.document.projection_generation.get();
    let outcome = if incoming < existing {
        ApplyOutcome::Stale
    } else if incoming == existing {
        if desired == current.document {
            ApplyOutcome::AlreadyApplied
        } else {
            ApplyOutcome::Conflict
        }
    } else if desired.membership_generation < current.document.membership_generation
        || desired.binding_generation < current.document.binding_generation
    {
        ApplyOutcome::Regression
    } else if incoming != existing.saturating_add(1) {
        ApplyOutcome::Gap
    } else {
        ApplyOutcome::Applied
    };

    let record = if outcome == ApplyOutcome::Applied {
        ManagedProjectionRecord { document: desired }
    } else {
        current.clone()
    };
    Ok(ProjectionApply { outcome, record })
}

fn validate_projection_authority(
    desired: &ProjectionDocument,
) -> Result<(), InvalidGeneratedAuthority> {
    let baseline: BTreeSet<_> = FleetOperation::managed_baseline().into_iter().collect();
    if !desired.generated_operations.is_subset(&baseline)
        || (!matches!(desired.operation, ManagedOperation::Upsert)
            && !desired.generated_operations.is_empty())
    {
        return Err(InvalidGeneratedAuthority);
    }
    Ok(())
}

/// Durable execution truth required for duplicate-safe recovery decisions.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum RunBindingState {
    Creating,
    Running { run_id: String },
    Completed { run_id: String, result: String },
    Cancelled { run_id: String },
    Indeterminate,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryAction {
    ResumeKnownRun,
    ReplayCompleted,
    FailCancelled,
    FailClosedIndeterminate,
}

impl RunBindingState {
    pub const fn recovery_action(&self) -> RecoveryAction {
        match self {
            Self::Running { .. } => RecoveryAction::ResumeKnownRun,
            Self::Completed { .. } => RecoveryAction::ReplayCompleted,
            Self::Cancelled { .. } => RecoveryAction::FailCancelled,
            Self::Creating | Self::Indeterminate => RecoveryAction::FailClosedIndeterminate,
        }
    }
}

/// Nodescale admission state used as one input to operational readiness.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ManagedNodeState {
    Unknown,
    Active,
    Disabled,
    Removed,
}

/// Reachability observed by the Fleet node runtime.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Reachability {
    Reachable,
    Unreachable,
}

/// Availability of one required operational layer.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Availability {
    Available,
    Unavailable,
}

/// Current Fleet worker usage. Available slots are derived, never trusted input.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerCapacity {
    pub active_workers: u32,
    pub max_workers: u32,
}

impl WorkerCapacity {
    pub const fn available_worker_slots(self) -> u32 {
        self.max_workers.saturating_sub(self.active_workers)
    }

    const fn is_valid(self) -> bool {
        self.max_workers > 0 && self.active_workers <= self.max_workers
    }
}

/// Total and currently available bytes for one resource class.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ByteCapacity {
    pub total_bytes: u64,
    pub available_bytes: u64,
}

impl ByteCapacity {
    const fn is_valid(self) -> bool {
        self.total_bytes > 0 && self.available_bytes <= self.total_bytes
    }
}

/// Bounded CPU data useful to later scheduler policy.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CpuObservation {
    pub logical_cores: u16,
    /// Load in hundredths of one percent: 10_000 is 100.00%.
    pub load_basis_points: Option<u16>,
}

impl CpuObservation {
    const fn is_valid(self) -> bool {
        self.logical_cores > 0
            && match self.load_basis_points {
                Some(load) => load <= 10_000,
                None => true,
            }
    }
}

/// Optional GPU data. No GPU and unknown GPU telemetry are both valid.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GpuObservation {
    pub present: bool,
    pub vram: Option<ByteCapacity>,
}

impl GpuObservation {
    const fn is_valid(self) -> bool {
        match (self.present, self.vram) {
            (false, None) => true,
            (true, None) => true,
            (true, Some(vram)) => vram.is_valid(),
            (false, Some(_)) => false,
        }
    }
}

/// Scheduling-relevant resources without a generic telemetry namespace.
#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct ResourceObservation {
    pub cpu: Option<CpuObservation>,
    pub ram: Option<ByteCapacity>,
    pub swap: Option<ByteCapacity>,
    pub disk: Option<ByteCapacity>,
    pub gpu: Option<GpuObservation>,
}

const MAX_PROFILE_PRESENCE: usize = 256;
const MAX_PROFILE_NAME_BYTES: usize = 128;
const MAX_PROFILE_VERSION_BYTES: usize = 128;

/// One installed Hermes profile distribution advertised by a Fleet node.
#[derive(Clone, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProfilePresence {
    pub name: String,
    pub version: String,
}

impl ProfilePresence {
    fn is_valid(&self) -> bool {
        let name = self.name.as_str();
        let version = self.version.as_str();
        !name.is_empty()
            && name.len() <= MAX_PROFILE_NAME_BYTES
            && name != "."
            && name != ".."
            && name.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
            })
            && !version.is_empty()
            && version.len() <= MAX_PROFILE_VERSION_BYTES
            && version.bytes().all(|byte| byte.is_ascii_graphic())
    }
}

fn profile_presence_is_valid(profiles: &[ProfilePresence]) -> bool {
    profiles.len() <= MAX_PROFILE_PRESENCE
        && profiles.iter().all(ProfilePresence::is_valid)
        && profiles
            .windows(2)
            .all(|pair| pair[0].name.as_str() < pair[1].name.as_str())
}

/// One node-authored operational sample. Identity is deliberately absent; the
/// strictly advancing projection generation fences the sample to one admission epoch.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct NodeObservation {
    pub admission_generation: u64,
    pub observed_at_ms: u64,
    pub network: Reachability,
    pub keryx: Availability,
    pub hermes: Availability,
    pub worker: Availability,
    pub capacity: WorkerCapacity,
    #[serde(default)]
    pub profiles: Vec<ProfilePresence>,
    #[serde(default)]
    pub resources: ResourceObservation,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct InvalidObservation;

impl NodeObservation {
    pub fn validate(&self) -> Result<(), InvalidObservation> {
        if self.admission_generation == 0
            || self.observed_at_ms == 0
            || !self.capacity.is_valid()
            || !profile_presence_is_valid(&self.profiles)
            || self.resources.cpu.is_some_and(|value| !value.is_valid())
            || self.resources.ram.is_some_and(|value| !value.is_valid())
            || self
                .resources
                .swap
                .is_some_and(|value| value.available_bytes > value.total_bytes)
            || self.resources.disk.is_some_and(|value| !value.is_valid())
            || self.resources.gpu.is_some_and(|value| !value.is_valid())
        {
            return Err(InvalidObservation);
        }
        Ok(())
    }
}

/// Fleet-owned receipt time paired with the last accepted node sample.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationRecord {
    pub observation: NodeObservation,
    pub received_at_ms: u64,
}

/// Explicit freshness policy. The exact boundary remains fresh.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ReadinessPolicy {
    freshness_window_ms: u64,
}

impl ReadinessPolicy {
    pub const fn new(freshness_window_ms: u64) -> Result<Self, InvalidObservation> {
        if freshness_window_ms == 0 {
            return Err(InvalidObservation);
        }
        Ok(Self {
            freshness_window_ms,
        })
    }

    pub const fn freshness_window_ms(self) -> u64 {
        self.freshness_window_ms
    }
}

/// Stable machine-readable explanation for a not-ready result.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReadinessReason {
    NodeUnknown,
    NodeNotActive,
    ObservationMissing,
    ObservationStale,
    ObservationTimeInvalid,
    NetworkUnreachable,
    KeryxUnavailable,
    HermesUnavailable,
    WorkerUnavailable,
    NoWorkerCapacity,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct NodeReadiness {
    pub alive: bool,
    pub fresh: bool,
    pub scheduler_ready: bool,
    pub observation_age_ms: Option<u64>,
    pub reasons: Vec<ReadinessReason>,
}

/// Derive platform readiness from admitted identity, current facts, and time.
pub fn evaluate_readiness(
    managed_state: ManagedNodeState,
    record: Option<&ObservationRecord>,
    now_ms: u64,
    policy: ReadinessPolicy,
) -> NodeReadiness {
    let mut reasons = Vec::new();
    match managed_state {
        ManagedNodeState::Unknown => reasons.push(ReadinessReason::NodeUnknown),
        ManagedNodeState::Active => {}
        ManagedNodeState::Disabled | ManagedNodeState::Removed => {
            reasons.push(ReadinessReason::NodeNotActive);
        }
    }

    let (fresh, observation_age_ms) = match record {
        None => {
            reasons.push(ReadinessReason::ObservationMissing);
            (false, None)
        }
        Some(record) => {
            let age = now_ms.checked_sub(record.received_at_ms);
            match age {
                None => {
                    reasons.push(ReadinessReason::ObservationTimeInvalid);
                    (false, None)
                }
                Some(age) if age > policy.freshness_window_ms => {
                    reasons.push(ReadinessReason::ObservationStale);
                    (false, Some(age))
                }
                Some(age) => (true, Some(age)),
            }
        }
    };

    if let Some(record) = record {
        let observation = &record.observation;
        if observation.network != Reachability::Reachable {
            reasons.push(ReadinessReason::NetworkUnreachable);
        }
        if observation.keryx != Availability::Available {
            reasons.push(ReadinessReason::KeryxUnavailable);
        }
        if observation.hermes != Availability::Available {
            reasons.push(ReadinessReason::HermesUnavailable);
        }
        if observation.worker != Availability::Available {
            reasons.push(ReadinessReason::WorkerUnavailable);
        }
        if observation.capacity.available_worker_slots() == 0 {
            reasons.push(ReadinessReason::NoWorkerCapacity);
        }
    }

    NodeReadiness {
        alive: fresh,
        fresh,
        scheduler_ready: reasons.is_empty(),
        observation_age_ms,
        reasons,
    }
}
