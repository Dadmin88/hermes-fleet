//! Transport- and storage-independent compatibility domain for Hermes Fleet F0.
//!
//! The Python implementation remains the behavioral oracle. This crate freezes
//! only already-proven product semantics; it does not own Keryx transport,
//! Nodescale state, persistence, scheduling, profiles, Sentinel, or UI.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Deserializer, Serialize, Serializer, de};

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
