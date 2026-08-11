use std::collections::BTreeSet;

use serde::{
    Deserialize, Serialize,
    de::{Error as DeError, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

const WORKFLOW_SCHEMA: &str = "fleet.workflow-editor.v1";
const WORKFLOW_LIMIT: usize = 256;
const POSITION_LIMIT: f64 = 100_000.0;
const MAX_DOCUMENT_BYTES: usize = 1_900_000;
const MAX_PAYLOAD_BYTES: usize = 16_384;
const MAX_JSON_DEPTH: usize = 16;
const MAX_JSON_CONTAINER_ITEMS: usize = 256;
const MAX_JSON_STRING_BYTES: usize = 4_096;
const MAX_JSON_KEY_BYTES: usize = 128;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum WorkflowValidationError {
    MalformedDocument,
    UnsupportedSchema,
    InvalidIdentity,
    InvalidName,
    InvalidNode,
    InvalidConnection,
    ExecutionUnavailableRequired,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct WorkflowDocument {
    schema: String,
    id: String,
    name: String,
    nodes: Vec<WorkflowNode>,
    connections: Vec<WorkflowConnection>,
    metadata: WorkflowMetadata,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct WorkflowMetadata {
    #[serde(rename = "executionAvailable")]
    execution_available: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct WorkflowNode {
    id: String,
    #[serde(rename = "type")]
    block_type: String,
    title: String,
    #[serde(rename = "pluginVersion", skip_serializing_if = "Option::is_none")]
    plugin_version: Option<String>,
    #[serde(rename = "configVersion", skip_serializing_if = "Option::is_none")]
    config_version: Option<String>,
    position: WorkflowPosition,
    configuration: Value,
    target: Option<Value>,
    runtime: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct WorkflowPosition {
    x: f64,
    y: f64,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct WorkflowConnection {
    id: String,
    source: String,
    #[serde(rename = "sourcePort")]
    source_port: String,
    target: String,
    #[serde(rename = "targetPort")]
    target_port: String,
    kind: String,
}

impl WorkflowDocument {
    pub fn parse_json(input: &str) -> Result<Self, WorkflowValidationError> {
        let value = parse_unique_json(input)?;
        let document: Self = serde_json::from_value(value)
            .map_err(|_| WorkflowValidationError::MalformedDocument)?;
        document.validate()?;
        Ok(document)
    }

    pub fn id(&self) -> &str {
        &self.id
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    pub fn connection_count(&self) -> usize {
        self.connections.len()
    }

    pub fn execution_available(&self) -> bool {
        self.metadata.execution_available
    }

    pub fn canonical_json(&self) -> Result<String, WorkflowValidationError> {
        let value =
            serde_json::to_value(self).map_err(|_| WorkflowValidationError::MalformedDocument)?;
        serde_json::to_string(&canonicalize(value))
            .map_err(|_| WorkflowValidationError::MalformedDocument)
    }

    pub fn content_hash(&self) -> String {
        let canonical = self
            .canonical_json()
            .expect("validated WorkflowDocument must serialize");
        format!("{:x}", Sha256::digest(canonical.as_bytes()))
    }

    pub fn validate(&self) -> Result<(), WorkflowValidationError> {
        if self.schema != WORKFLOW_SCHEMA {
            return Err(WorkflowValidationError::UnsupportedSchema);
        }
        if !valid_id(&self.id) {
            return Err(WorkflowValidationError::InvalidIdentity);
        }
        if !valid_display_text(&self.name, 128) {
            return Err(WorkflowValidationError::InvalidName);
        }
        if self.metadata.execution_available {
            return Err(WorkflowValidationError::ExecutionUnavailableRequired);
        }
        if self.nodes.len() > WORKFLOW_LIMIT || self.connections.len() > WORKFLOW_LIMIT {
            return Err(WorkflowValidationError::MalformedDocument);
        }

        let mut node_ids = BTreeSet::new();
        for node in &self.nodes {
            if !valid_id(&node.id)
                || !node_ids.insert(node.id.as_str())
                || !valid_block_type(&node.block_type)
                || !valid_display_text(&node.title, 128)
                || node
                    .plugin_version
                    .as_deref()
                    .is_some_and(|value| !valid_version_reference(value))
                || node
                    .config_version
                    .as_deref()
                    .is_some_and(|value| !valid_version_reference(value))
                || !node.position.x.is_finite()
                || !node.position.y.is_finite()
                || node.position.x.abs() > POSITION_LIMIT
                || node.position.y.abs() > POSITION_LIMIT
                || node.runtime != "unavailable"
                || !bounded_json(&node.configuration)
                || !valid_target(node.target.as_ref())
            {
                return Err(WorkflowValidationError::InvalidNode);
            }
        }

        let mut connection_ids = BTreeSet::new();
        let mut endpoint_keys = BTreeSet::new();
        let mut occupied_inputs = BTreeSet::new();
        for connection in &self.connections {
            let endpoint = (
                connection.source.as_str(),
                connection.source_port.as_str(),
                connection.target.as_str(),
                connection.target_port.as_str(),
            );
            let input = (connection.target.as_str(), connection.target_port.as_str());
            if !valid_id(&connection.id)
                || !connection_ids.insert(connection.id.as_str())
                || !node_ids.contains(connection.source.as_str())
                || !node_ids.contains(connection.target.as_str())
                || connection.source == connection.target
                || !valid_id(&connection.source_port)
                || !valid_id(&connection.target_port)
                || !valid_id(&connection.kind)
                || !endpoint_keys.insert(endpoint)
                || !occupied_inputs.insert(input)
            {
                return Err(WorkflowValidationError::InvalidConnection);
            }
        }

        if self.canonical_json()?.len() > MAX_DOCUMENT_BYTES {
            return Err(WorkflowValidationError::MalformedDocument);
        }
        Ok(())
    }
}

fn canonicalize(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(canonicalize).collect()),
        Value::Object(values) => {
            let mut entries = values.into_iter().collect::<Vec<_>>();
            entries.sort_unstable_by(|left, right| left.0.cmp(&right.0));
            let mut canonical = Map::new();
            for (key, value) in entries {
                canonical.insert(key, canonicalize(value));
            }
            Value::Object(canonical)
        }
        value => value,
    }
}

pub fn reject_duplicate_json_members(input: &str) -> Result<(), WorkflowValidationError> {
    parse_unique_json(input).map(|_| ())
}

struct UniqueJson(Value);

impl<'de> Deserialize<'de> for UniqueJson {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        deserializer.deserialize_any(UniqueJsonVisitor)
    }
}

struct UniqueJsonVisitor;

impl<'de> Visitor<'de> for UniqueJsonVisitor {
    type Value = UniqueJson;

    fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("JSON without duplicate object members")
    }

    fn visit_bool<E>(self, value: bool) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Bool(value)))
    }

    fn visit_i64<E>(self, value: i64) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Number(value.into())))
    }

    fn visit_u64<E>(self, value: u64) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Number(value.into())))
    }

    fn visit_f64<E>(self, value: f64) -> Result<Self::Value, E>
    where
        E: DeError,
    {
        serde_json::Number::from_f64(value)
            .map(Value::Number)
            .map(UniqueJson)
            .ok_or_else(|| E::custom("non-finite JSON number"))
    }

    fn visit_str<E>(self, value: &str) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::String(value.to_owned())))
    }

    fn visit_string<E>(self, value: String) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::String(value)))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Null))
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(UniqueJson(Value::Null))
    }

    fn visit_seq<A>(self, mut sequence: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut values = Vec::new();
        while let Some(value) = sequence.next_element::<UniqueJson>()? {
            values.push(value.0);
        }
        Ok(UniqueJson(Value::Array(values)))
    }

    fn visit_map<A>(self, mut object: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let mut values = Map::new();
        while let Some(key) = object.next_key::<String>()? {
            if values.contains_key(&key) {
                return Err(A::Error::custom("duplicate JSON object member"));
            }
            let value = object.next_value::<UniqueJson>()?;
            values.insert(key, value.0);
        }
        Ok(UniqueJson(Value::Object(values)))
    }
}

fn parse_unique_json(input: &str) -> Result<Value, WorkflowValidationError> {
    let mut deserializer = serde_json::Deserializer::from_str(input);
    let value = UniqueJson::deserialize(&mut deserializer)
        .map_err(|_| WorkflowValidationError::MalformedDocument)?;
    deserializer
        .end()
        .map_err(|_| WorkflowValidationError::MalformedDocument)?;
    Ok(value.0)
}

fn bounded_json(value: &Value) -> bool {
    if serde_json::to_vec(value).map_or(true, |encoded| encoded.len() > MAX_PAYLOAD_BYTES) {
        return false;
    }
    let mut items = 0;
    bounded_json_value(value, 0, &mut items)
}

fn bounded_json_value(value: &Value, depth: usize, items: &mut usize) -> bool {
    if depth > MAX_JSON_DEPTH {
        return false;
    }
    *items += 1;
    if *items > MAX_JSON_CONTAINER_ITEMS * 4 {
        return false;
    }
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => true,
        Value::String(value) => value.len() <= MAX_JSON_STRING_BYTES,
        Value::Array(values) => {
            values.len() <= MAX_JSON_CONTAINER_ITEMS
                && values
                    .iter()
                    .all(|value| bounded_json_value(value, depth + 1, items))
        }
        Value::Object(values) => {
            values.len() <= MAX_JSON_CONTAINER_ITEMS
                && values.keys().all(|key| {
                    !key.is_empty()
                        && key.len() <= MAX_JSON_KEY_BYTES
                        && !key.chars().any(char::is_control)
                })
                && values
                    .values()
                    .all(|value| bounded_json_value(value, depth + 1, items))
        }
    }
}

fn valid_target(target: Option<&Value>) -> bool {
    let Some(target) = target else {
        return true;
    };
    if !bounded_json(target) {
        return false;
    }
    let Some(target) = target.as_object() else {
        return true;
    };
    if !target.contains_key("authority") {
        return true;
    }
    match target.get("authority").and_then(Value::as_str) {
        Some("managed") => valid_managed_target(target),
        Some("observed") => valid_observed_target(target),
        _ => false,
    }
}

fn valid_managed_target(target: &Map<String, Value>) -> bool {
    const KEYS: &[&str] = &[
        "stable_id",
        "authority",
        "source",
        "network_id",
        "device_id",
    ];
    if !exact_string_keys(target, KEYS)
        || ["source", "network_id", "device_id"]
            .iter()
            .any(|key| !target[*key].as_str().is_some_and(valid_external_identity))
    {
        return false;
    }
    let source = target["source"].as_str().unwrap();
    let network_id = target["network_id"].as_str().unwrap();
    let device_id = target["device_id"].as_str().unwrap();
    let material = serde_json::to_vec(&[source, network_id, device_id])
        .expect("managed target identity is serializable");
    target["stable_id"].as_str().unwrap() == format!("fleet-node-{:x}", Sha256::digest(material))
}

fn valid_observed_target(target: &Map<String, Value>) -> bool {
    const KEYS: &[&str] = &[
        "stable_id",
        "authority",
        "provider",
        "provider_instance_id",
        "provider_node_id",
        "network_id",
        "observed_id",
    ];
    if !exact_string_keys(target, KEYS)
        || [
            "provider",
            "provider_instance_id",
            "provider_node_id",
            "network_id",
        ]
        .iter()
        .any(|key| !target[*key].as_str().is_some_and(valid_external_identity))
    {
        return false;
    }
    let observed_id = target["observed_id"].as_str().unwrap();
    let stable_id = target["stable_id"].as_str().unwrap();
    observed_id.len() == 71
        && observed_id
            .strip_prefix("sha256:")
            .is_some_and(valid_hex_64)
        && stable_id == format!("observed-node-{}", &observed_id[7..])
}

fn exact_string_keys(target: &Map<String, Value>, expected: &[&str]) -> bool {
    target.len() == expected.len()
        && expected.iter().all(|key| {
            target
                .get(*key)
                .and_then(Value::as_str)
                .is_some_and(|value| !value.is_empty() && value.len() <= 256)
        })
}

fn valid_hex_64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_version_reference(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value
            .chars()
            .all(|character| !character.is_control() && !character.is_whitespace())
}

fn valid_external_identity(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value.trim() == value
        && !value
            .chars()
            .any(|character| character.is_control() || character.is_whitespace())
}

fn valid_display_text(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.chars().count() <= maximum
        && value.trim() == value
        && !value.chars().any(char::is_control)
}

fn valid_block_type(value: &str) -> bool {
    !value.is_empty() && value.len() <= 256 && value.split('/').all(valid_id_segment)
}

fn valid_id(value: &str) -> bool {
    value.len() <= 128 && valid_id_segment(value)
}

fn valid_id_segment(value: &str) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}
