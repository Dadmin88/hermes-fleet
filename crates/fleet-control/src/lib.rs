//! Authenticated Linux Unix-domain control for durable managed projections and
//! bounded current node observations.
//!
//! The service preserves the accepted Nodescale/Fleet projection V1 contract and
//! adds the narrow Fleet node-observation V1 contract on the same local socket.
//! Peer Unix credentials are the only authentication input; request JSON contains
//! no principal or credential.

#![cfg(target_os = "linux")]

use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::CString,
    fs,
    io::{Read, Write},
    os::{
        fd::AsRawFd,
        unix::{
            ffi::OsStrExt,
            fs::{FileTypeExt, MetadataExt, PermissionsExt},
            net::{UnixListener, UnixStream},
        },
    },
    path::{Component, Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use fleet_domain::{
    ApplyOutcome, FleetOperation, Generation, ManagedOperation, NodeObservation,
    ProjectionDocument, ReadinessPolicy,
    workflow::{WorkflowDocument, reject_duplicate_json_members},
};
use fleet_state::{
    AliasClearOutcome, AliasSetOutcome, FleetStateStore, ManagedNodeView, NodeOperationalView,
    ObservationOutcome, ProjectionView, StateError, WorkflowDeleteOutcome, WorkflowRevision,
    WorkflowWriteOutcome,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use thiserror::Error;

pub const SCHEMA: &str = "fleet.managed-projection.v1";
pub const OBSERVATION_SCHEMA: &str = "fleet.node-observation.v1";
pub const DESKTOP_SCHEMA: &str = "fleet.desktop.v1";
pub const DESKTOP_ALIAS_SCHEMA: &str = "fleet.desktop-alias.v1";
pub const WORKFLOW_SCHEMA: &str = "fleet.workflow.v1";
pub const MAX_FRAME_BYTES: usize = 2_097_152;
pub const MAX_RESPONSE_BYTES: usize = 2_097_152;
pub const MAX_DESKTOP_NODES: usize = 256;
pub const MAX_CONNECTIONS: usize = 32;
pub const IO_TIMEOUT: Duration = Duration::from_secs(2);
pub const DEFAULT_FRESHNESS_WINDOW: Duration = Duration::from_secs(90);
const MAX_FRESHNESS_WINDOW: Duration = Duration::from_secs(86_400);
const ACCEPT_POLL: Duration = Duration::from_millis(20);

#[derive(Debug, Error)]
pub enum ControlError {
    #[error("unauthorized caller")]
    UnauthorizedCaller,
    #[error("malformed request")]
    MalformedRequest,
    #[error("unsupported protocol version or schema")]
    UnsupportedSchema,
    #[error("stale projection generation")]
    StaleGeneration,
    #[error("conflicting projection generation")]
    Conflict,
    #[error("projection generation gap")]
    Gap,
    #[error("projection generation regression")]
    Regression,
    #[error("durable state is corrupt")]
    StateCorruption,
    #[error("durable state is unavailable or busy")]
    StateUnavailable,
    #[error("unsafe service path: {0}")]
    UnsafePath(&'static str),
    #[error("service I/O failed")]
    Io(#[from] std::io::Error),
    #[error("service worker failed")]
    WorkerFailed,
}

pub type Result<T> = std::result::Result<T, ControlError>;

#[derive(Clone, Debug)]
pub struct ControlConfig {
    pub socket_path: PathBuf,
    pub database_path: PathBuf,
    pub allowed_uid: u32,
    pub socket_gid: Option<u32>,
    pub freshness_window: Duration,
}

impl ControlConfig {
    pub fn new(
        socket_path: impl AsRef<Path>,
        database_path: impl AsRef<Path>,
        allowed_uid: u32,
        socket_gid: Option<u32>,
    ) -> Result<Self> {
        let socket_path = checked_absolute_file_path(socket_path.as_ref(), "socket path")?;
        let database_path = checked_absolute_file_path(database_path.as_ref(), "database path")?;
        Ok(Self {
            socket_path,
            database_path,
            allowed_uid,
            socket_gid,
            freshness_window: DEFAULT_FRESHNESS_WINDOW,
        })
    }

    pub fn with_freshness_window(mut self, freshness_window: Duration) -> Result<Self> {
        if freshness_window.as_millis() == 0 || freshness_window > MAX_FRESHNESS_WINDOW {
            return Err(ControlError::MalformedRequest);
        }
        self.freshness_window = freshness_window;
        Ok(self)
    }
}

/// Run the production local-control service until `shutdown` becomes true.
pub fn run(config: ControlConfig, shutdown: Arc<AtomicBool>) -> Result<()> {
    validate_socket_parent(&config.socket_path, config.socket_gid)?;
    validate_database_parent(&config.database_path)?;
    prepare_database_file(&config.database_path)?;
    let state = FleetStateStore::open(&config.database_path).map_err(map_state_error)?;
    let owned = OwnedListener::bind(&config.socket_path, config.socket_gid)?;
    owned.listener.set_nonblocking(true)?;

    println!("Rust Fleet started");
    println!(
        "Managed projection socket: {}",
        config.socket_path.display()
    );

    let mut workers: Vec<JoinHandle<()>> = Vec::new();
    while !shutdown.load(Ordering::Acquire) {
        reap_workers(&mut workers)?;
        match owned.listener.accept() {
            Ok((stream, _)) => {
                if workers.len() >= MAX_CONNECTIONS {
                    drop(stream);
                    continue;
                }
                let worker_state = state.clone();
                let allowed_uid = config.allowed_uid;
                let freshness_window = config.freshness_window;
                workers.push(thread::spawn(move || {
                    serve_connection(stream, allowed_uid, freshness_window, worker_state);
                }));
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(ACCEPT_POLL);
            }
            Err(error) => return Err(ControlError::Io(error)),
        }
    }

    let OwnedListener { listener, socket } = owned;
    drop(listener);
    for worker in workers {
        worker.join().map_err(|_| ControlError::WorkerFailed)?;
    }
    drop(socket);
    println!("Rust Fleet stopped cleanly");
    Ok(())
}

fn reap_workers(workers: &mut Vec<JoinHandle<()>>) -> Result<()> {
    let mut index = 0;
    while index < workers.len() {
        if workers[index].is_finished() {
            let worker = workers.swap_remove(index);
            worker.join().map_err(|_| ControlError::WorkerFailed)?;
        } else {
            index += 1;
        }
    }
    Ok(())
}

fn serve_connection(
    mut stream: UnixStream,
    allowed_uid: u32,
    freshness_window: Duration,
    state: FleetStateStore,
) {
    let _ = stream.set_read_timeout(Some(IO_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IO_TIMEOUT));
    if peer_uid(&stream).ok() != Some(allowed_uid) {
        return;
    }
    let (request, schema) = receive_request(&mut stream);
    let response = match request {
        Ok(request) => match dispatch_response(request, &state, freshness_window) {
            Ok(response) => response,
            Err(error) => {
                eprintln!("fleet control request failed: {error}");
                error_response(schema)
            }
        },
        Err(error) => {
            eprintln!("fleet control request failed: {error}");
            error_response(schema)
        }
    };
    let _ = send_response(&mut stream, &response);
}

fn peer_uid(stream: &UnixStream) -> Result<u32> {
    let mut credentials = libc::ucred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let status = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            std::ptr::addr_of_mut!(credentials).cast(),
            std::ptr::addr_of_mut!(length),
        )
    };
    if status != 0 || length as usize != std::mem::size_of::<libc::ucred>() {
        return Err(ControlError::UnauthorizedCaller);
    }
    Ok(credentials.uid)
}

fn receive_request(stream: &mut UnixStream) -> (Result<Request>, &'static str) {
    let payload = match receive_payload(stream) {
        Ok(payload) => payload,
        Err(error) => return (Err(error), SCHEMA),
    };
    let schema = declared_schema(&payload);
    (parse_request(&payload), schema)
}

fn receive_payload(stream: &mut UnixStream) -> Result<Vec<u8>> {
    let mut header = [0_u8; 4];
    stream
        .read_exact(&mut header)
        .map_err(|_| ControlError::MalformedRequest)?;
    let length = u32::from_be_bytes(header) as usize;
    if !(1..=MAX_FRAME_BYTES).contains(&length) {
        return Err(ControlError::MalformedRequest);
    }
    let mut payload = vec![0_u8; length];
    stream
        .read_exact(&mut payload)
        .map_err(|_| ControlError::MalformedRequest)?;
    let mut trailing = [0_u8; 1];
    match stream.read(&mut trailing) {
        Ok(0) => {}
        Ok(_) | Err(_) => return Err(ControlError::MalformedRequest),
    }
    Ok(payload)
}

fn declared_schema(payload: &[u8]) -> &'static str {
    let schema = serde_json::from_slice::<serde_json::Value>(payload)
        .ok()
        .and_then(|value| {
            value
                .get("schema")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
        });
    match schema.as_deref() {
        Some(OBSERVATION_SCHEMA) => OBSERVATION_SCHEMA,
        Some(DESKTOP_SCHEMA) => DESKTOP_SCHEMA,
        Some(DESKTOP_ALIAS_SCHEMA) => DESKTOP_ALIAS_SCHEMA,
        Some(WORKFLOW_SCHEMA) => WORKFLOW_SCHEMA,
        _ => SCHEMA,
    }
}

fn parse_request(payload: &[u8]) -> Result<Request> {
    if declared_schema(payload) == WORKFLOW_SCHEMA {
        let input = std::str::from_utf8(payload).map_err(|_| ControlError::MalformedRequest)?;
        reject_duplicate_json_members(input).map_err(|_| ControlError::MalformedRequest)?;
        let request: WorkflowRequest =
            serde_json::from_slice(payload).map_err(|_| ControlError::MalformedRequest)?;
        return Ok(request.into());
    }
    let request: Request =
        serde_json::from_slice(payload).map_err(|_| ControlError::MalformedRequest)?;
    if request.schema() != request.expected_schema() {
        return Err(ControlError::UnsupportedSchema);
    }
    Ok(request)
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum WorkflowRequest {
    Capabilities(WorkflowCapabilitiesRequest),
    Create(WorkflowCreateRequest),
    Read(WorkflowReadRequest),
    ReadVersion(WorkflowReadVersionRequest),
    Update(WorkflowUpdateRequest),
    List(WorkflowListRequest),
    Delete(WorkflowDeleteRequest),
}

impl From<WorkflowRequest> for Request {
    fn from(request: WorkflowRequest) -> Self {
        match request {
            WorkflowRequest::Capabilities(request) => Self::WorkflowCapabilities(request),
            WorkflowRequest::Create(request) => Self::WorkflowCreate(request),
            WorkflowRequest::Read(request) => Self::WorkflowRead(request),
            WorkflowRequest::ReadVersion(request) => Self::WorkflowReadVersion(request),
            WorkflowRequest::Update(request) => Self::WorkflowUpdate(request),
            WorkflowRequest::List(request) => Self::WorkflowList(request),
            WorkflowRequest::Delete(request) => Self::WorkflowDelete(request),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum Request {
    Capabilities(CapabilitiesRequest),
    Apply(ApplyRequest),
    Inspect(InspectRequest),
    Observe(ObserveRequest),
    InspectObservation(InspectObservationRequest),
    DesktopOverview(DesktopOverviewRequest),
    SetAlias(SetAliasRequest),
    ClearAlias(ClearAliasRequest),
    WorkflowCapabilities(WorkflowCapabilitiesRequest),
    WorkflowCreate(WorkflowCreateRequest),
    WorkflowRead(WorkflowReadRequest),
    WorkflowReadVersion(WorkflowReadVersionRequest),
    WorkflowUpdate(WorkflowUpdateRequest),
    WorkflowList(WorkflowListRequest),
    WorkflowDelete(WorkflowDeleteRequest),
}

impl Request {
    fn schema(&self) -> &str {
        match self {
            Self::Capabilities(request) => &request.schema,
            Self::Apply(request) => &request.schema,
            Self::Inspect(request) => &request.schema,
            Self::Observe(request) => &request.schema,
            Self::InspectObservation(request) => &request.schema,
            Self::DesktopOverview(request) => &request.schema,
            Self::SetAlias(request) => &request.schema,
            Self::ClearAlias(request) => &request.schema,
            Self::WorkflowCapabilities(request) => &request.schema,
            Self::WorkflowCreate(request) => &request.schema,
            Self::WorkflowRead(request) => &request.schema,
            Self::WorkflowReadVersion(request) => &request.schema,
            Self::WorkflowUpdate(request) => &request.schema,
            Self::WorkflowList(request) => &request.schema,
            Self::WorkflowDelete(request) => &request.schema,
        }
    }

    const fn expected_schema(&self) -> &'static str {
        match self {
            Self::Capabilities(_) | Self::Apply(_) | Self::Inspect(_) => SCHEMA,
            Self::Observe(_) | Self::InspectObservation(_) => OBSERVATION_SCHEMA,
            Self::DesktopOverview(_) => DESKTOP_SCHEMA,
            Self::SetAlias(_) | Self::ClearAlias(_) => DESKTOP_ALIAS_SCHEMA,
            Self::WorkflowCapabilities(_)
            | Self::WorkflowCreate(_)
            | Self::WorkflowRead(_)
            | Self::WorkflowReadVersion(_)
            | Self::WorkflowUpdate(_)
            | Self::WorkflowList(_)
            | Self::WorkflowDelete(_) => WORKFLOW_SCHEMA,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CapabilitiesRequest {
    schema: String,
    kind: CapabilitiesKind,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ApplyRequest {
    schema: String,
    kind: ApplyKind,
    document: WireProjectionDocument,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InspectRequest {
    schema: String,
    kind: InspectKind,
    selector: InspectSelector,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ObserveRequest {
    schema: String,
    kind: ObserveKind,
    selector: InspectSelector,
    observation: NodeObservation,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InspectObservationRequest {
    schema: String,
    kind: InspectObservationKind,
    selector: InspectSelector,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DesktopOverviewRequest {
    schema: String,
    kind: DesktopOverviewKind,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SetAliasRequest {
    schema: String,
    kind: SetAliasKind,
    selector: InspectSelector,
    binding_generation: Generation,
    alias: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ClearAliasRequest {
    schema: String,
    kind: ClearAliasKind,
    selector: InspectSelector,
    binding_generation: Generation,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkflowCapabilitiesRequest {
    schema: String,
    kind: WorkflowCapabilitiesKind,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkflowCreateRequest {
    schema: String,
    kind: WorkflowCreateKind,
    document: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct WorkflowReadRequest {
    schema: String,
    kind: WorkflowReadKind,
    workflow_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct WorkflowReadVersionRequest {
    schema: String,
    kind: WorkflowReadVersionKind,
    workflow_id: String,
    version: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct WorkflowUpdateRequest {
    schema: String,
    kind: WorkflowUpdateKind,
    expected_version: u64,
    document: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WorkflowListRequest {
    schema: String,
    kind: WorkflowListKind,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct WorkflowDeleteRequest {
    schema: String,
    kind: WorkflowDeleteKind,
    workflow_id: String,
    expected_version: u64,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum CapabilitiesKind {
    #[serde(rename = "capabilities")]
    Capabilities,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum ApplyKind {
    #[serde(rename = "apply")]
    Apply,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum InspectKind {
    #[serde(rename = "inspect")]
    Inspect,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum ObserveKind {
    #[serde(rename = "observe")]
    Observe,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum InspectObservationKind {
    #[serde(rename = "inspect_observation")]
    InspectObservation,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum DesktopOverviewKind {
    #[serde(rename = "overview")]
    Overview,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum SetAliasKind {
    #[serde(rename = "set_alias")]
    SetAlias,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum ClearAliasKind {
    #[serde(rename = "clear_alias")]
    ClearAlias,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum WorkflowCapabilitiesKind {
    #[serde(rename = "capabilities")]
    Capabilities,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum WorkflowCreateKind {
    #[serde(rename = "create")]
    Create,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum WorkflowReadKind {
    #[serde(rename = "read")]
    Read,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum WorkflowReadVersionKind {
    #[serde(rename = "read_version")]
    ReadVersion,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum WorkflowUpdateKind {
    #[serde(rename = "update")]
    Update,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum WorkflowListKind {
    #[serde(rename = "list")]
    List,
}

#[derive(Clone, Copy, Debug, Deserialize)]
enum WorkflowDeleteKind {
    #[serde(rename = "delete")]
    Delete,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
enum WireManagedOperation {
    Upsert,
    Disable,
    Remove,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
enum WireGeneratedOperation {
    #[serde(rename = "fleet.health")]
    Health,
    #[serde(rename = "fleet.inventory")]
    Inventory,
    #[serde(rename = "fleet.message")]
    Message,
}

impl WireGeneratedOperation {
    const fn domain(self) -> FleetOperation {
        match self {
            Self::Health => FleetOperation::Health,
            Self::Inventory => FleetOperation::Inventory,
            Self::Message => FleetOperation::Message,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct WireProvenance {
    source: String,
    network_id: String,
    device_id: String,
    snapshot: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct WireProjectionDocument {
    source: String,
    network_id: String,
    device_id: String,
    projection_generation: Generation,
    membership_generation: Generation,
    binding_generation: Generation,
    content_hash: String,
    operation: WireManagedOperation,
    generated_operations: Vec<WireGeneratedOperation>,
    provenance: WireProvenance,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InspectSelector {
    source: String,
    network_id: String,
    device_id: String,
}

#[derive(Serialize)]
struct CanonicalProjectionMaterial<'a> {
    source: &'a str,
    network_id: &'a str,
    device_id: &'a str,
    projection_generation: Generation,
    membership_generation: Generation,
    binding_generation: Generation,
    operation: WireManagedOperation,
    generated_operations: Vec<WireGeneratedOperation>,
    provenance: &'a WireProvenance,
}

pub fn canonical_projection_hash(document: &Value) -> Result<String> {
    let document: WireProjectionDocument =
        serde_json::from_value(document.clone()).map_err(|_| ControlError::MalformedRequest)?;
    canonical_hash(&document)
}

fn canonical_hash(document: &WireProjectionDocument) -> Result<String> {
    validate_wire_document(document, false)?;
    let mut operations = document.generated_operations.clone();
    operations.sort_unstable();
    let material = CanonicalProjectionMaterial {
        source: &document.source,
        network_id: &document.network_id,
        device_id: &document.device_id,
        projection_generation: document.projection_generation,
        membership_generation: document.membership_generation,
        binding_generation: document.binding_generation,
        operation: document.operation,
        generated_operations: operations,
        provenance: &document.provenance,
    };
    let value = serde_json::to_value(material).map_err(|_| ControlError::MalformedRequest)?;
    let mut canonical = String::new();
    write_canonical_json(&value, &mut canonical);
    Ok(format!("{:x}", Sha256::digest(canonical.as_bytes())))
}

fn validate_wire_document(document: &WireProjectionDocument, verify_hash: bool) -> Result<()> {
    validate_identifier(&document.source)?;
    validate_identifier(&document.network_id)?;
    validate_identifier(&document.device_id)?;
    if document.source != "nodescale"
        || document.provenance.source != document.source
        || document.provenance.network_id != document.network_id
        || document.provenance.device_id != document.device_id
    {
        return Err(ControlError::MalformedRequest);
    }
    validate_identifier(&document.provenance.snapshot)?;
    let snapshot = document
        .provenance
        .snapshot
        .parse::<u64>()
        .map_err(|_| ControlError::MalformedRequest)?;
    if snapshot == 0 || snapshot.to_string() != document.provenance.snapshot {
        return Err(ControlError::MalformedRequest);
    }
    let unique = document
        .generated_operations
        .iter()
        .copied()
        .collect::<BTreeSet<_>>();
    if unique.len() != document.generated_operations.len() {
        return Err(ControlError::MalformedRequest);
    }
    if !matches!(document.operation, WireManagedOperation::Upsert)
        && !document.generated_operations.is_empty()
    {
        return Err(ControlError::MalformedRequest);
    }
    if verify_hash
        && (document.content_hash.len() != 64
            || !document
                .content_hash
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            || canonical_hash(document)? != document.content_hash)
    {
        return Err(ControlError::MalformedRequest);
    }
    Ok(())
}

fn validate_identifier(value: &str) -> Result<()> {
    if value.is_empty()
        || value.chars().count() > 256
        || value.trim() != value
        || value
            .chars()
            .any(|character| character.is_control() || character.is_whitespace())
    {
        return Err(ControlError::MalformedRequest);
    }
    Ok(())
}

fn dispatch_result(
    request: Request,
    state: &FleetStateStore,
    freshness_window: Duration,
) -> Result<Value> {
    match request {
        Request::Capabilities(request) => {
            let _ = request.kind;
            Ok(json!({"kinds":["capabilities","apply","inspect"]}))
        }
        Request::Apply(request) => {
            let _ = request.kind;
            validate_wire_document(&request.document, true)?;
            let key = (
                request.document.network_id.clone(),
                request.document.device_id.clone(),
            );
            let applied = state
                .apply_projection(request.document.into_domain())
                .map_err(map_state_error)?;
            let outcome = match applied.outcome {
                ApplyOutcome::Applied => "applied",
                ApplyOutcome::AlreadyApplied => "already_applied",
                ApplyOutcome::Conflict => "conflict",
                ApplyOutcome::Stale => "stale",
                ApplyOutcome::Regression => "regression",
                ApplyOutcome::Gap => "gap",
            };
            if matches!(
                applied.outcome,
                ApplyOutcome::Applied | ApplyOutcome::AlreadyApplied
            ) {
                println!("Managed node received from Nodescale");
                println!("Node ID: {}", key.1);
                println!(
                    "State: {}",
                    managed_state(applied.record.document.operation)
                );
                println!(
                    "Generated grants: {}",
                    applied
                        .record
                        .document
                        .generated_operations
                        .iter()
                        .map(|operation| operation.as_str())
                        .collect::<Vec<_>>()
                        .join(" ")
                );
            }
            Ok(json!({"outcome":outcome}))
        }
        Request::Inspect(request) => {
            let _ = request.kind;
            validate_identifier(&request.selector.source)?;
            validate_identifier(&request.selector.network_id)?;
            validate_identifier(&request.selector.device_id)?;
            let view = state
                .inspect_projection(
                    &request.selector.source,
                    &request.selector.network_id,
                    &request.selector.device_id,
                )
                .map_err(map_state_error)?;
            if view.generated.is_some() {
                println!("Managed node restored successfully");
            }
            Ok(inspect_value(view))
        }
        Request::Observe(request) => {
            let _ = request.kind;
            validate_identifier(&request.selector.source)?;
            validate_identifier(&request.selector.network_id)?;
            validate_identifier(&request.selector.device_id)?;
            let applied = state
                .record_observation(
                    &request.selector.source,
                    &request.selector.network_id,
                    &request.selector.device_id,
                    request.observation,
                    current_time_ms()?,
                )
                .map_err(map_state_error)?;
            let outcome = match applied.outcome {
                ObservationOutcome::Recorded => "recorded",
                ObservationOutcome::AlreadyRecorded => "already_recorded",
                ObservationOutcome::Stale => "stale",
                ObservationOutcome::Conflict => "conflict",
            };
            Ok(json!({"outcome":outcome}))
        }
        Request::InspectObservation(request) => {
            let _ = request.kind;
            validate_identifier(&request.selector.source)?;
            validate_identifier(&request.selector.network_id)?;
            validate_identifier(&request.selector.device_id)?;
            let policy = ReadinessPolicy::new(freshness_window.as_millis() as u64)
                .map_err(|_| ControlError::MalformedRequest)?;
            let view = state
                .inspect_node(
                    &request.selector.source,
                    &request.selector.network_id,
                    &request.selector.device_id,
                    current_time_ms()?,
                    policy,
                )
                .map_err(map_state_error)?;
            Ok(observation_value(view))
        }
        Request::DesktopOverview(request) => {
            let _ = request.kind;
            let policy = ReadinessPolicy::new(freshness_window.as_millis() as u64)
                .map_err(|_| ControlError::MalformedRequest)?;
            let nodes = state
                .list_managed_nodes(current_time_ms()?, policy)
                .map_err(map_state_error)?;
            if nodes.len() > MAX_DESKTOP_NODES {
                return Err(ControlError::MalformedRequest);
            }
            Ok(json!({
                "nodes": nodes.into_iter().map(desktop_node_value).collect::<Vec<_>>()
            }))
        }
        Request::SetAlias(request) => {
            let _ = request.kind;
            validate_identifier(&request.selector.source)?;
            validate_identifier(&request.selector.network_id)?;
            validate_identifier(&request.selector.device_id)?;
            let outcome = state
                .set_node_alias(
                    &request.selector.source,
                    &request.selector.network_id,
                    &request.selector.device_id,
                    request.binding_generation.get(),
                    &request.alias,
                )
                .map_err(map_state_error)?;
            Ok(json!({
                "outcome": match outcome {
                    AliasSetOutcome::Created => "created",
                    AliasSetOutcome::Replaced => "replaced",
                    AliasSetOutcome::Unchanged => "unchanged",
                }
            }))
        }
        Request::WorkflowCapabilities(request) => {
            let _ = request.kind;
            Ok(json!({
                "kinds":["capabilities","create","read","read_version","update","list","delete"],
                "executionAvailable":false
            }))
        }
        Request::WorkflowCreate(request) => {
            let _ = request.kind;
            let document = parse_workflow_document(request.document)?;
            let write = state
                .create_workflow(document, current_time_ms()?)
                .map_err(map_state_error)?;
            Ok(json!({
                "outcome": workflow_write_outcome(write.outcome),
                "revision": workflow_revision_value(write.revision)?
            }))
        }
        Request::WorkflowRead(request) => {
            let _ = request.kind;
            let revision = state
                .read_latest_workflow(&request.workflow_id)
                .map_err(map_state_error)?
                .map(workflow_revision_value)
                .transpose()?;
            Ok(json!({"revision":revision}))
        }
        Request::WorkflowReadVersion(request) => {
            let _ = request.kind;
            let revision = state
                .read_workflow_version(&request.workflow_id, request.version)
                .map_err(map_state_error)?
                .map(workflow_revision_value)
                .transpose()?;
            Ok(json!({"revision":revision}))
        }
        Request::WorkflowUpdate(request) => {
            let _ = request.kind;
            let document = parse_workflow_document(request.document)?;
            let write = state
                .update_workflow(document, request.expected_version, current_time_ms()?)
                .map_err(map_state_error)?;
            Ok(json!({
                "outcome": workflow_write_outcome(write.outcome),
                "revision": workflow_revision_value(write.revision)?
            }))
        }
        Request::WorkflowList(request) => {
            let _ = request.kind;
            let workflows = state
                .list_workflows()
                .map_err(map_state_error)?
                .into_iter()
                .map(|summary| {
                    json!({
                        "workflowId":summary.workflow_id,
                        "latestVersion":summary.latest_version,
                        "createdAtMs":summary.created_at_ms,
                        "updatedAtMs":summary.updated_at_ms
                    })
                })
                .collect::<Vec<_>>();
            Ok(json!({"workflows":workflows}))
        }
        Request::WorkflowDelete(request) => {
            let _ = request.kind;
            let outcome = state
                .delete_workflow(
                    &request.workflow_id,
                    request.expected_version,
                    current_time_ms()?,
                )
                .map_err(map_state_error)?;
            Ok(json!({
                "outcome": match outcome {
                    WorkflowDeleteOutcome::Deleted => "deleted",
                    WorkflowDeleteOutcome::AlreadyDeleted => "already_deleted",
                }
            }))
        }
        Request::ClearAlias(request) => {
            let _ = request.kind;
            validate_identifier(&request.selector.source)?;
            validate_identifier(&request.selector.network_id)?;
            validate_identifier(&request.selector.device_id)?;
            let outcome = state
                .clear_node_alias(
                    &request.selector.source,
                    &request.selector.network_id,
                    &request.selector.device_id,
                    request.binding_generation.get(),
                )
                .map_err(map_state_error)?;
            Ok(json!({
                "outcome": match outcome {
                    AliasClearOutcome::Cleared => "cleared",
                    AliasClearOutcome::AlreadyClear => "already_clear",
                }
            }))
        }
    }
}

fn parse_workflow_document(document: Value) -> Result<WorkflowDocument> {
    let encoded = serde_json::to_string(&document).map_err(|_| ControlError::MalformedRequest)?;
    WorkflowDocument::parse_json(&encoded).map_err(|_| ControlError::MalformedRequest)
}

const fn workflow_write_outcome(outcome: WorkflowWriteOutcome) -> &'static str {
    match outcome {
        WorkflowWriteOutcome::Created => "created",
        WorkflowWriteOutcome::VersionCreated => "version_created",
        WorkflowWriteOutcome::Unchanged => "unchanged",
    }
}

fn workflow_revision_value(revision: WorkflowRevision) -> Result<Value> {
    let document =
        serde_json::to_value(revision.document).map_err(|_| ControlError::StateCorruption)?;
    Ok(json!({
        "workflowId":revision.workflow_id,
        "version":revision.version,
        "contentHash":revision.content_hash,
        "document":document,
        "createdAtMs":revision.created_at_ms
    }))
}

impl WireProjectionDocument {
    fn into_domain(self) -> ProjectionDocument {
        let operation = match self.operation {
            WireManagedOperation::Upsert => ManagedOperation::Upsert,
            WireManagedOperation::Disable => ManagedOperation::Disable,
            WireManagedOperation::Remove => ManagedOperation::Remove,
        };
        let generated_operations = self
            .generated_operations
            .into_iter()
            .map(WireGeneratedOperation::domain)
            .collect();
        let provenance = BTreeMap::from([
            ("source".to_owned(), self.provenance.source),
            ("network_id".to_owned(), self.provenance.network_id),
            ("device_id".to_owned(), self.provenance.device_id),
            ("snapshot".to_owned(), self.provenance.snapshot),
        ]);
        ProjectionDocument {
            source: self.source,
            network_id: self.network_id,
            device_id: self.device_id,
            projection_generation: self.projection_generation,
            membership_generation: self.membership_generation,
            binding_generation: self.binding_generation,
            content_hash: self.content_hash,
            operation,
            generated_operations,
            provenance,
        }
    }
}

fn inspect_value(view: ProjectionView) -> Value {
    let Some(record) = view.generated else {
        return json!({"generated":null,"effective":null});
    };
    let document = record.document;
    let state = managed_state(document.operation);
    let generated = json!({
        "state": state,
        "projection_generation": document.projection_generation,
        "membership_generation": document.membership_generation,
        "binding_generation": document.binding_generation,
        "content_hash": document.content_hash,
        "allowed_operations": document.generated_operations,
        "provenance": document.provenance,
    });
    let effective = json!({
        "state": state,
        "allowed_operations": view.effective_operations,
        "operator_denied_operations": view.operator_denied_operations,
    });
    json!({"generated":generated,"effective":effective})
}

fn observation_value(view: NodeOperationalView) -> Value {
    let (last_observation, capacity, profiles, resources) = match view.observation {
        None => (Value::Null, Value::Null, Value::Null, Value::Null),
        Some(record) => {
            let observation = record.observation;
            (
                json!({
                    "admission_generation": observation.admission_generation,
                    "observed_at_ms": observation.observed_at_ms,
                    "received_at_ms": record.received_at_ms,
                    "network": observation.network,
                    "keryx": observation.keryx,
                    "hermes": observation.hermes,
                    "worker": observation.worker,
                }),
                json!({
                    "active_workers": observation.capacity.active_workers,
                    "max_workers": observation.capacity.max_workers,
                    "available_worker_slots": observation.capacity.available_worker_slots(),
                }),
                json!(observation.profiles),
                json!(observation.resources),
            )
        }
    };
    json!({
        "managed_state": view.managed_state,
        "admission_generation": view.admission_generation,
        "alive": view.readiness.alive,
        "fresh": view.readiness.fresh,
        "scheduler_ready": view.readiness.scheduler_ready,
        "observation_age_ms": view.readiness.observation_age_ms,
        "reasons": view.readiness.reasons,
        "last_observation": last_observation,
        "capacity": capacity,
        "profiles": profiles,
        "resources": resources,
    })
}

fn desktop_node_value(view: ManagedNodeView) -> Value {
    let stable_id = stable_node_id(&view.source, &view.network_id, &view.device_id);
    let active = view.managed_state == fleet_domain::ManagedNodeState::Active;
    let display_name = view.alias.clone().unwrap_or_else(|| view.device_id.clone());
    let has_alias = view.alias.is_some();
    let readiness = observation_value(view.operational);
    json!({
        "stable_id": stable_id,
        "identity": {
            "source": view.source,
            "network_id": view.network_id,
            "device_id": view.device_id,
        },
        "naming": {
            "display_name": display_name,
            "provider_name": Value::Null,
            "alias": view.alias,
            "has_alias": has_alias,
        },
        "managed": {
            "state": view.managed_state,
            "active": active,
            "projection_generation": view.projection_generation,
            "membership_generation": view.membership_generation,
            "binding_generation": view.binding_generation,
        },
        "readiness": readiness,
        "operations": view.effective_operations,
    })
}

fn stable_node_id(source: &str, network_id: &str, device_id: &str) -> String {
    let material = serde_json::to_vec(&[source, network_id, device_id])
        .expect("managed node identity is serializable");
    format!("fleet-node-{:x}", Sha256::digest(material))
}

fn current_time_ms() -> Result<u64> {
    let milliseconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ControlError::MalformedRequest)?
        .as_millis();
    u64::try_from(milliseconds).map_err(|_| ControlError::MalformedRequest)
}

const fn managed_state(operation: ManagedOperation) -> &'static str {
    match operation {
        ManagedOperation::Upsert => "active",
        ManagedOperation::Disable => "disabled",
        ManagedOperation::Remove => "removed",
    }
}

fn success_response(schema: &'static str, kind: &'static str, result: Value) -> Value {
    json!({"schema":schema,"kind":kind,"ok":true,"result":result})
}

fn error_response(schema: &'static str) -> Value {
    json!({"schema":schema,"kind":"error","ok":false,"error":"invalid_request"})
}

fn dispatch_response(
    request: Request,
    state: &FleetStateStore,
    freshness_window: Duration,
) -> Result<Value> {
    let schema = request.expected_schema();
    let kind = match &request {
        Request::Capabilities(_) => "capabilities",
        Request::Apply(_) => "apply",
        Request::Inspect(_) => "inspect",
        Request::Observe(_) => "observe",
        Request::InspectObservation(_) => "inspect_observation",
        Request::DesktopOverview(_) => "overview",
        Request::SetAlias(_) => "set_alias",
        Request::ClearAlias(_) => "clear_alias",
        Request::WorkflowCapabilities(_) => "capabilities",
        Request::WorkflowCreate(_) => "create",
        Request::WorkflowRead(_) => "read",
        Request::WorkflowReadVersion(_) => "read_version",
        Request::WorkflowUpdate(_) => "update",
        Request::WorkflowList(_) => "list",
        Request::WorkflowDelete(_) => "delete",
    };
    dispatch_result(request, state, freshness_window)
        .map(|result| success_response(schema, kind, result))
}

fn send_response(stream: &mut UnixStream, response: &Value) -> Result<()> {
    let payload = serde_json::to_vec(response).map_err(|_| ControlError::MalformedRequest)?;
    if payload.is_empty() || payload.len() > MAX_RESPONSE_BYTES {
        return Err(ControlError::MalformedRequest);
    }
    stream.write_all(&(payload.len() as u32).to_be_bytes())?;
    stream.write_all(&payload)?;
    Ok(())
}

fn map_state_error(error: StateError) -> ControlError {
    match error {
        StateError::InvalidInput(_) | StateError::InvalidTransition(_) => {
            ControlError::MalformedRequest
        }
        StateError::CorruptState(_)
        | StateError::UnsupportedSchema { .. }
        | StateError::Serialization(_) => ControlError::StateCorruption,
        StateError::Database(_) => ControlError::StateUnavailable,
    }
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

struct OwnedSocket {
    path: PathBuf,
    device: u64,
    inode: u64,
}

impl Drop for OwnedSocket {
    fn drop(&mut self) {
        cleanup_owned_socket(&self.path, self.device, self.inode);
    }
}

struct OwnedListener {
    listener: UnixListener,
    socket: OwnedSocket,
}

impl OwnedListener {
    fn bind(path: &Path, socket_gid: Option<u32>) -> Result<Self> {
        match fs::symlink_metadata(path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Ok(_) => return Err(ControlError::UnsafePath("socket already exists")),
            Err(error) => return Err(ControlError::Io(error)),
        }
        let listener = UnixListener::bind(path)?;
        let metadata = fs::symlink_metadata(path)?;
        if !metadata.file_type().is_socket() || metadata.uid() != unsafe { libc::geteuid() } {
            let _ = fs::remove_file(path);
            return Err(ControlError::UnsafePath("socket ownership"));
        }
        if let Some(gid) = socket_gid {
            let path_bytes = CString::new(path.as_os_str().as_bytes())
                .map_err(|_| ControlError::UnsafePath("socket path"))?;
            if unsafe { libc::chown(path_bytes.as_ptr(), u32::MAX, gid) } != 0 {
                let error = std::io::Error::last_os_error();
                let _ = fs::remove_file(path);
                return Err(ControlError::Io(error));
            }
        }
        fs::set_permissions(
            path,
            fs::Permissions::from_mode(if socket_gid.is_some() { 0o660 } else { 0o600 }),
        )?;
        let verified = fs::symlink_metadata(path)?;
        let expected_mode = if socket_gid.is_some() { 0o660 } else { 0o600 };
        if !verified.file_type().is_socket()
            || verified.uid() != unsafe { libc::geteuid() }
            || verified.permissions().mode() & 0o777 != expected_mode
            || socket_gid.is_some_and(|gid| verified.gid() != gid)
        {
            let _ = fs::remove_file(path);
            return Err(ControlError::UnsafePath("socket metadata"));
        }
        Ok(Self {
            listener,
            socket: OwnedSocket {
                path: path.to_path_buf(),
                device: verified.dev(),
                inode: verified.ino(),
            },
        })
    }
}

fn cleanup_owned_socket(path: &Path, device: u64, inode: u64) {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return;
    };
    if metadata.file_type().is_socket() && metadata.dev() == device && metadata.ino() == inode {
        let _ = fs::remove_file(path);
    }
}

fn checked_absolute_file_path(path: &Path, label: &'static str) -> Result<PathBuf> {
    if !path.is_absolute()
        || path.file_name().is_none()
        || path
            .components()
            .any(|component| component == Component::ParentDir)
    {
        return Err(ControlError::UnsafePath(label));
    }
    Ok(path.to_path_buf())
}

fn validate_socket_parent(path: &Path, socket_gid: Option<u32>) -> Result<()> {
    let metadata = validate_directory_components(
        path.parent()
            .ok_or(ControlError::UnsafePath("socket parent"))?,
    )?;
    let mode = metadata.permissions().mode() & 0o777;
    if metadata.uid() != unsafe { libc::geteuid() } || mode & 0o707 != 0o700 {
        return Err(ControlError::UnsafePath("socket parent"));
    }
    let group_mode = mode & 0o070;
    match socket_gid {
        None if group_mode == 0 => Ok(()),
        Some(gid) if metadata.gid() == gid && matches!(group_mode, 0o010 | 0o050) => Ok(()),
        _ => Err(ControlError::UnsafePath("socket parent")),
    }
}

fn validate_database_parent(path: &Path) -> Result<()> {
    let metadata = validate_directory_components(
        path.parent()
            .ok_or(ControlError::UnsafePath("database parent"))?,
    )?;
    let mode = metadata.permissions().mode() & 0o777;
    if metadata.uid() != unsafe { libc::geteuid() } || mode != 0o700 {
        return Err(ControlError::UnsafePath("database parent"));
    }
    Ok(())
}

fn validate_directory_components(path: &Path) -> Result<fs::Metadata> {
    if !path.is_absolute() || path.components().any(|part| part == Component::ParentDir) {
        return Err(ControlError::UnsafePath("directory components"));
    }
    let mut current = PathBuf::from("/");
    let mut final_metadata = fs::symlink_metadata(&current)?;
    for component in path.components() {
        match component {
            Component::RootDir => continue,
            Component::Normal(part) => current.push(part),
            _ => return Err(ControlError::UnsafePath("directory components")),
        }
        final_metadata = fs::symlink_metadata(&current)?;
        if final_metadata.file_type().is_symlink() || !final_metadata.is_dir() {
            return Err(ControlError::UnsafePath("directory components"));
        }
    }
    Ok(final_metadata)
}

fn prepare_database_file(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink()
                || !metadata.file_type().is_file()
                || metadata.uid() != unsafe { libc::geteuid() }
            {
                return Err(ControlError::UnsafePath("database file"));
            }
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let path_bytes = CString::new(path.as_os_str().as_bytes())
                .map_err(|_| ControlError::UnsafePath("database path"))?;
            let fd = unsafe {
                libc::open(
                    path_bytes.as_ptr(),
                    libc::O_WRONLY
                        | libc::O_CREAT
                        | libc::O_EXCL
                        | libc::O_CLOEXEC
                        | libc::O_NOFOLLOW,
                    0o600,
                )
            };
            if fd < 0 {
                return Err(ControlError::Io(std::io::Error::last_os_error()));
            }
            unsafe { libc::close(fd) };
        }
        Err(error) => return Err(ControlError::Io(error)),
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    let verified = fs::symlink_metadata(path)?;
    if verified.file_type().is_symlink()
        || !verified.file_type().is_file()
        || verified.uid() != unsafe { libc::geteuid() }
        || verified.permissions().mode() & 0o777 != 0o600
    {
        return Err(ControlError::UnsafePath("database file"));
    }
    Ok(())
}
