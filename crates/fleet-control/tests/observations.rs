#![cfg(target_os = "linux")]

use std::{
    fs,
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::{Path, PathBuf},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use fleet_control::{
    ControlConfig, acquire_remote_observation_authority, canonical_projection_hash,
    publish_remote_observation, run,
};
use fleet_domain::ProjectionDocument;
use fleet_state::{
    FleetStateStore, ObservationOutcome, RemoteObservationAuthorityEpoch, RemoteObservationSelector,
};
use serde_json::{Value, json};
use tempfile::TempDir;

struct RunningService {
    socket: PathBuf,
    database: PathBuf,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl RunningService {
    fn start(root: &Path) -> Self {
        let socket = root.join("fleet.sock");
        let database = root.join("fleet.db");
        let stop = Arc::new(AtomicBool::new(false));
        let config = ControlConfig::new(&socket, &database, unsafe { libc::geteuid() }, None)
            .unwrap()
            .with_freshness_window(Duration::from_secs(30))
            .unwrap();
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || run(config, thread_stop).unwrap());
        wait_for(&socket, true);
        Self {
            socket,
            database,
            stop,
            thread: Some(thread),
        }
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.thread.take().unwrap().join().unwrap();
        wait_for(&self.socket, false);
    }
}

impl Drop for RunningService {
    fn drop(&mut self) {
        if self.thread.is_some() {
            self.stop();
        }
    }
}

fn private_tempdir() -> TempDir {
    let root = tempfile::tempdir().unwrap();
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
    root
}

fn wait_for(path: &Path, exists: bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while path.exists() != exists {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
}

fn transact(socket: &Path, request: &Value) -> Value {
    let payload = serde_json::to_vec(request).unwrap();
    transact_bytes(socket, &payload)
}

fn transact_bytes(socket: &Path, payload: &[u8]) -> Value {
    let mut stream = UnixStream::connect(socket).unwrap();
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .unwrap();
    stream.write_all(payload).unwrap();
    stream.shutdown(std::net::Shutdown::Write).unwrap();
    let mut header = [0_u8; 4];
    stream.read_exact(&mut header).unwrap();
    let mut body = vec![0_u8; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut body).unwrap();
    serde_json::from_slice(&body).unwrap()
}

fn managed_document() -> Value {
    managed_document_for("node-demo")
}

fn managed_document_for(device_id: &str) -> Value {
    let mut document = json!({
        "source": "nodescale",
        "network_id": "net-demo",
        "device_id": device_id,
        "projection_generation": "1",
        "membership_generation": "1",
        "binding_generation": "1",
        "content_hash": "",
        "operation": "upsert",
        "generated_operations": ["fleet.health", "fleet.inventory", "fleet.message"],
        "provenance": {
            "source": "nodescale",
            "network_id": "net-demo",
            "device_id": device_id,
            "snapshot": "1"
        }
    });
    document["content_hash"] = Value::String(canonical_projection_hash(&document).unwrap());
    document
}

fn apply_managed(socket: &Path) {
    let response = transact(
        socket,
        &json!({
            "schema": "fleet.managed-projection.v1",
            "kind": "apply",
            "document": managed_document(),
        }),
    );
    assert_eq!(response["result"]["outcome"], "applied");
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
}

fn observation(observed_at_ms: u64, active_workers: u32, keryx: &str) -> Value {
    json!({
        "admission_generation": 1,
        "observed_at_ms": observed_at_ms,
        "network": "reachable",
        "keryx": keryx,
        "hermes": "available",
        "worker": "available",
        "capacity": {
            "active_workers": active_workers,
            "max_workers": 1
        },
        "resources": {
            "cpu": {"logical_cores": 8, "load_basis_points": 2500},
            "ram": {"total_bytes": 16000, "available_bytes": 8000},
            "swap": {"total_bytes": 0, "available_bytes": 0},
            "disk": {"total_bytes": 100000, "available_bytes": 50000},
            "gpu": null
        }
    })
}

fn observe(socket: &Path, sample: Value) -> Value {
    transact(
        socket,
        &json!({
            "schema": "fleet.node-observation.v1",
            "kind": "observe",
            "selector": {
                "source": "nodescale",
                "network_id": "net-demo",
                "device_id": "node-demo"
            },
            "observation": sample
        }),
    )
}

fn inspect(socket: &Path) -> Value {
    transact(
        socket,
        &json!({
            "schema": "fleet.node-observation.v1",
            "kind": "inspect_observation",
            "selector": {
                "source": "nodescale",
                "network_id": "net-demo",
                "device_id": "node-demo"
            }
        }),
    )
}

#[test]
fn authenticated_projection_provenance_survives_control_apply_and_inspect() {
    let root = private_tempdir();
    let mut service = RunningService::start(root.path());
    let mut document = managed_document();
    document["provenance"]["binding_id"] = json!("binding-1");
    document["provenance"]["authenticated_peer_id"] = json!("peer-1");
    document["content_hash"] = json!(canonical_projection_hash(&document).unwrap());

    let applied = transact(
        &service.socket,
        &json!({
            "schema": "fleet.managed-projection.v1",
            "kind": "apply",
            "document": document,
        }),
    );
    assert_eq!(applied["result"]["outcome"], "applied");
    let inspected = transact(
        &service.socket,
        &json!({
            "schema": "fleet.managed-projection.v1",
            "kind": "inspect",
            "selector": {
                "source": "nodescale",
                "network_id": "net-demo",
                "device_id": "node-demo"
            }
        }),
    );
    assert_eq!(
        inspected["result"]["generated"]["provenance"]["binding_id"],
        "binding-1"
    );
    assert_eq!(
        inspected["result"]["generated"]["provenance"]["authenticated_peer_id"],
        "peer-1"
    );

    let state = FleetStateStore::open(&service.database).unwrap();
    let authority = acquire_remote_observation_authority(
        &state,
        &RemoteObservationSelector {
            source: "nodescale".to_string(),
            network_id: "net-demo".to_string(),
            device_id: "node-demo".to_string(),
        },
    )
    .unwrap();
    assert_eq!(authority.binding_id, "binding-1");
    assert_eq!(authority.authenticated_peer_id, "peer-1");

    let selector = json!({
        "source": "nodescale",
        "network_id": "net-demo",
        "device_id": "node-demo"
    });
    let acquired = transact(
        &service.socket,
        &json!({
            "authenticated_context": {"sender_peer_id": "peer-1"},
            "request": {
                "schema": "fleet.remote-observation-internal.v1",
                "kind": "acquire",
                "selector": selector
            }
        }),
    );
    assert_eq!(acquired["result"]["binding_id"], "binding-1");
    assert_eq!(acquired["result"]["authenticated_peer_id"], "peer-1");
    let sample = observation(2_000, 0, "available");
    let rejected = transact(
        &service.socket,
        &json!({
            "authenticated_context": {"sender_peer_id": "wrong-peer"},
            "request": {
                "schema": "fleet.remote-observation-internal.v1",
                "kind": "publish",
                "selector": selector,
                "authority_epoch": acquired["result"],
                "observation": sample
            }
        }),
    );
    assert_eq!(rejected["ok"], false);
    let published = transact(
        &service.socket,
        &json!({
            "authenticated_context": {"sender_peer_id": "peer-1"},
            "request": {
                "schema": "fleet.remote-observation-internal.v1",
                "kind": "publish",
                "selector": selector,
                "authority_epoch": acquired["result"],
                "observation": sample
            }
        }),
    );
    assert_eq!(published["result"]["outcome"], "published");
    let replay = transact(
        &service.socket,
        &json!({
            "authenticated_context": {"sender_peer_id": "peer-1"},
            "request": {
                "schema": "fleet.remote-observation-internal.v1",
                "kind": "publish",
                "selector": selector,
                "authority_epoch": acquired["result"],
                "observation": sample
            }
        }),
    );
    assert_eq!(replay["result"]["outcome"], "already_recorded");
    service.stop();
}

#[test]
fn remote_wire_rejects_ambiguous_or_self_asserted_identity_and_accepts_trusted_context() {
    let root = private_tempdir();
    let mut service = RunningService::start(root.path());
    let mut document = managed_document();
    document["provenance"]["binding_id"] = json!("binding-1");
    document["provenance"]["authenticated_peer_id"] = json!("peer-1");
    document["content_hash"] = json!(canonical_projection_hash(&document).unwrap());
    assert_eq!(
        transact(
            &service.socket,
            &json!({
                "schema": "fleet.managed-projection.v1",
                "kind": "apply",
                "document": document
            }),
        )["ok"],
        true
    );

    let duplicate = transact_bytes(
        &service.socket,
        br#"{"schema":"fleet.remote-observation-internal.v1","kind":"acquire","kind":"acquire","selector":{"source":"nodescale","network_id":"net-demo","device_id":"node-demo"}}"#,
    );
    assert_eq!(duplicate["ok"], false);

    let nested_unknown = transact(
        &service.socket,
        &json!({
            "schema": "fleet.remote-observation-internal.v1",
            "kind": "acquire",
            "selector": {
                "source": "nodescale",
                "network_id": "net-demo",
                "device_id": "node-demo",
                "unexpected": true
            }
        }),
    );
    assert_eq!(nested_unknown["ok"], false);

    let selector = json!({
        "source": "nodescale",
        "network_id": "net-demo",
        "device_id": "node-demo"
    });
    let unwrapped_acquire = transact(
        &service.socket,
        &json!({
            "schema": "fleet.remote-observation-internal.v1",
            "kind": "acquire",
            "selector": selector
        }),
    );
    assert_eq!(unwrapped_acquire["ok"], false);
    let wrong_acquire = transact(
        &service.socket,
        &json!({
            "authenticated_context": {"sender_peer_id": "wrong-peer"},
            "request": {
                "schema": "fleet.remote-observation-internal.v1",
                "kind": "acquire",
                "selector": selector
            }
        }),
    );
    assert_eq!(wrong_acquire["ok"], false);
    let acquired = transact(
        &service.socket,
        &json!({
            "authenticated_context": {"sender_peer_id": "peer-1"},
            "request": {
                "schema": "fleet.remote-observation-internal.v1",
                "kind": "acquire",
                "selector": selector
            }
        }),
    );
    let inner_publish = json!({
        "schema": "fleet.remote-observation-internal.v1",
        "kind": "publish",
        "selector": selector,
        "authority_epoch": acquired["result"],
        "observation": observation(2_000, 0, "available")
    });
    let mut self_asserted = inner_publish.clone();
    self_asserted["authenticated_sender"] = json!("peer-1");
    assert_eq!(transact(&service.socket, &self_asserted)["ok"], false);

    let trusted = transact(
        &service.socket,
        &json!({
            "authenticated_context": {"sender_peer_id": "peer-1"},
            "request": inner_publish
        }),
    );
    assert_eq!(trusted["result"]["outcome"], "published");
    service.stop();
}

#[test]
fn remote_adapter_uses_trusted_sender_and_atomic_state_admission() {
    let root = private_tempdir();
    let store = FleetStateStore::open(root.path().join("fleet.db")).unwrap();
    let selector = RemoteObservationSelector {
        source: "nodescale".to_string(),
        network_id: "net-demo".to_string(),
        device_id: "node-demo".to_string(),
    };
    let legacy: ProjectionDocument = serde_json::from_value(managed_document()).unwrap();
    store.apply_projection(legacy).unwrap();
    assert!(acquire_remote_observation_authority(&store, &selector).is_err());

    let mut remote = managed_document();
    remote["projection_generation"] = json!("2");
    remote["membership_generation"] = json!("2");
    remote["binding_generation"] = json!("2");
    remote["provenance"]["snapshot"] = json!("2");
    remote["provenance"]["binding_id"] = json!("binding-1");
    remote["provenance"]["authenticated_peer_id"] = json!("peer-1");
    remote["content_hash"] = json!(canonical_projection_hash(&remote).unwrap());
    store
        .apply_projection(serde_json::from_value(remote).unwrap())
        .unwrap();
    let epoch = acquire_remote_observation_authority(&store, &selector).unwrap();
    assert_eq!(
        epoch,
        RemoteObservationAuthorityEpoch {
            binding_id: "binding-1".to_string(),
            authenticated_peer_id: "peer-1".to_string(),
            binding_generation: 2,
            projection_generation: 2,
        }
    );

    let sample = json!({
        "admission_generation": 2,
        "observed_at_ms": 2_000,
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 1},
        "profiles": [],
        "resources": {}
    });
    let bytes = serde_json::to_vec(&sample).unwrap();
    assert!(
        publish_remote_observation(&store, &selector, "wrong-peer", &epoch, &bytes, 2_100).is_err()
    );
    assert!(
        publish_remote_observation(
            &store,
            &selector,
            "peer-1",
            &epoch,
            br#"{"admission_generation":2,"admission_generation":2}"#,
            2_100,
        )
        .is_err()
    );
    let first =
        publish_remote_observation(&store, &selector, "peer-1", &epoch, &bytes, 2_100).unwrap();
    assert_eq!(first.outcome, ObservationOutcome::Recorded);
    let replay =
        publish_remote_observation(&store, &selector, "peer-1", &epoch, &bytes, 2_900).unwrap();
    assert_eq!(replay.outcome, ObservationOutcome::AlreadyRecorded);
    assert_eq!(replay.record.received_at_ms, 2_100);
}

#[test]
fn freshness_config_rejects_submillisecond_and_overlong_windows() {
    let root = private_tempdir();
    let config = ControlConfig::new(
        root.path().join("fleet.sock"),
        root.path().join("fleet.db"),
        unsafe { libc::geteuid() },
        None,
    )
    .unwrap();
    assert!(
        config
            .clone()
            .with_freshness_window(Duration::from_nanos(999_999))
            .is_err()
    );
    assert!(
        config
            .with_freshness_window(Duration::from_secs(24 * 60 * 60 + 1))
            .is_err()
    );
}

#[test]
fn real_service_observes_explains_recovers_and_restores_scheduler_readiness() {
    let root = private_tempdir();
    let mut service = RunningService::start(root.path());

    let unknown_view = inspect(&service.socket);
    assert_eq!(unknown_view["result"]["managed_state"], "unknown");
    assert_eq!(unknown_view["result"]["scheduler_ready"], false);
    assert_eq!(unknown_view["result"]["profiles"], Value::Null);
    assert_eq!(
        unknown_view["result"]["reasons"],
        json!(["node_unknown", "observation_missing"])
    );
    let unknown = observe(&service.socket, observation(now_ms(), 0, "available"));
    assert_eq!(unknown["error"], "invalid_request");
    apply_managed(&service.socket);

    let first_time = now_ms();
    assert_eq!(
        observe(&service.socket, observation(first_time, 0, "available"))["result"]["outcome"],
        "recorded"
    );
    let ready = inspect(&service.socket);
    assert_eq!(ready["schema"], "fleet.node-observation.v1");
    assert_eq!(ready["result"]["managed_state"], "active");
    assert_eq!(ready["result"]["alive"], true);
    assert_eq!(ready["result"]["fresh"], true);
    assert_eq!(ready["result"]["scheduler_ready"], true);
    assert_eq!(ready["result"]["reasons"], json!([]));
    assert_eq!(ready["result"]["capacity"]["active_workers"], 0);
    assert_eq!(ready["result"]["capacity"]["max_workers"], 1);
    assert_eq!(ready["result"]["capacity"]["available_worker_slots"], 1);
    assert_eq!(ready["result"]["profiles"], json!([]));
    assert_eq!(ready["result"]["resources"]["gpu"], Value::Null);

    let saturated_time = first_time + 1;
    assert_eq!(
        observe(&service.socket, observation(saturated_time, 1, "available"))["result"]["outcome"],
        "recorded"
    );
    let saturated = inspect(&service.socket);
    assert_eq!(saturated["result"]["scheduler_ready"], false);
    assert_eq!(
        saturated["result"]["reasons"],
        json!(["no_worker_capacity"])
    );

    let keryx_down_time = saturated_time + 1;
    observe(
        &service.socket,
        observation(keryx_down_time, 0, "unavailable"),
    );
    let keryx_down = inspect(&service.socket);
    assert_eq!(keryx_down["result"]["scheduler_ready"], false);
    assert_eq!(
        keryx_down["result"]["reasons"],
        json!(["keryx_unavailable"])
    );

    let recovered_time = keryx_down_time + 1;
    observe(&service.socket, observation(recovered_time, 0, "available"));
    assert_eq!(inspect(&service.socket)["result"]["scheduler_ready"], true);

    service.stop();
    assert!(!service.socket.exists());
    let database = service.database.clone();
    let mut restarted = RunningService::start(root.path());
    assert_eq!(restarted.database, database);
    let restored = inspect(&restarted.socket);
    assert_eq!(restored["result"]["scheduler_ready"], true);
    assert_eq!(restored["result"]["profiles"], json!([]));
    assert_eq!(
        restored["result"]["last_observation"]["observed_at_ms"],
        recovered_time
    );
    restarted.stop();
}

#[test]
fn profile_presence_round_trips_through_control_and_restart() {
    let root = private_tempdir();
    let mut service = RunningService::start(root.path());
    apply_managed(&service.socket);

    let mut sample = observation(now_ms(), 0, "available");
    sample["profiles"] = json!([
        {"name": "agency-ai-engineer", "version": "0.1.0"},
        {"name": "agency-backend-engineer", "version": "0.1.0"}
    ]);
    assert_eq!(
        observe(&service.socket, sample)["result"]["outcome"],
        "recorded"
    );
    let expected = json!([
        {"name": "agency-ai-engineer", "version": "0.1.0"},
        {"name": "agency-backend-engineer", "version": "0.1.0"}
    ]);
    assert_eq!(inspect(&service.socket)["result"]["profiles"], expected);

    service.stop();
    let mut restarted = RunningService::start(root.path());
    assert_eq!(inspect(&restarted.socket)["result"]["profiles"], expected);
    restarted.stop();
}

#[test]
fn observation_payload_cannot_supply_identity_or_invalid_capacity() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    apply_managed(&service.socket);

    let mut identity = observation(now_ms(), 0, "available");
    identity["device_id"] = Value::String("other-node".into());
    let identity_response = observe(&service.socket, identity);
    assert_eq!(identity_response["schema"], "fleet.node-observation.v1");
    assert_eq!(identity_response["error"], "invalid_request");

    let mut invalid = observation(now_ms(), 0, "available");
    invalid["capacity"]["active_workers"] = json!(2);
    assert_eq!(
        observe(&service.socket, invalid)["error"],
        "invalid_request"
    );
    let mut invalid_nested = observation(now_ms(), 0, "available");
    invalid_nested["capacity"]["available_worker_slots"] = json!(1);
    assert_eq!(
        observe(&service.socket, invalid_nested)["error"],
        "invalid_request"
    );

    let mut invalid_resource = observation(now_ms(), 0, "available");
    invalid_resource["resources"] = json!({
        "cpu": {"logical_cores": 4, "extra": true}
    });
    assert_eq!(
        observe(&service.socket, invalid_resource)["error"],
        "invalid_request"
    );
    assert_eq!(
        inspect(&service.socket)["result"]["last_observation"],
        Value::Null
    );
}

#[test]
fn desktop_overview_lists_real_managed_nodes_with_current_readiness() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    apply_managed(&service.socket);
    observe(&service.socket, observation(now_ms(), 0, "available"));

    let response = transact(
        &service.socket,
        &json!({"schema": "fleet.desktop.v1", "kind": "overview"}),
    );

    assert_eq!(response["schema"], "fleet.desktop.v1");
    assert_eq!(response["kind"], "overview");
    assert_eq!(response["ok"], true);
    assert_eq!(response["result"]["nodes"].as_array().unwrap().len(), 1);
    let node = &response["result"]["nodes"][0];
    let stable_id = node["stable_id"].as_str().unwrap();
    assert_eq!(
        stable_id,
        "fleet-node-570e2a16c67a2f4812f0ee2d60039409e7b7a4b03446da1a0aa666a6d694bdf6"
    );
    assert_eq!(node["identity"]["source"], "nodescale");
    assert_eq!(node["identity"]["network_id"], "net-demo");
    assert_eq!(node["identity"]["device_id"], "node-demo");
    assert_eq!(node["naming"]["provider_name"], Value::Null);
    assert_eq!(node["naming"]["alias"], Value::Null);
    assert_eq!(node["naming"]["display_name"], "node-demo");
    assert_eq!(node["managed"]["state"], "active");
    assert_eq!(node["readiness"]["scheduler_ready"], true);
    assert_eq!(
        node["operations"],
        json!(["fleet.health", "fleet.inventory", "fleet.message"])
    );
}

#[test]
fn desktop_alias_control_is_generation_fenced_and_updates_overview_naming() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    apply_managed(&service.socket);
    let selector = json!({
        "source": "nodescale",
        "network_id": "net-demo",
        "device_id": "node-demo"
    });

    let stale = transact(
        &service.socket,
        &json!({
            "schema": "fleet.desktop-alias.v1",
            "kind": "set_alias",
            "selector": selector,
            "binding_generation": "2",
            "alias": "Stale"
        }),
    );
    assert_eq!(stale["schema"], "fleet.desktop-alias.v1");
    assert_eq!(stale["kind"], "error");
    assert_eq!(stale["ok"], false);

    let set = transact(
        &service.socket,
        &json!({
            "schema": "fleet.desktop-alias.v1",
            "kind": "set_alias",
            "selector": selector,
            "binding_generation": "1",
            "alias": "Workstation"
        }),
    );
    assert_eq!(set["schema"], "fleet.desktop-alias.v1");
    assert_eq!(set["kind"], "set_alias");
    assert_eq!(set["ok"], true);
    assert_eq!(set["result"]["outcome"], "created");

    let overview = transact(
        &service.socket,
        &json!({"schema": "fleet.desktop.v1", "kind": "overview"}),
    );
    let naming = &overview["result"]["nodes"][0]["naming"];
    assert_eq!(naming["display_name"], "Workstation");
    assert_eq!(naming["provider_name"], Value::Null);
    assert_eq!(naming["alias"], "Workstation");
    assert_eq!(naming["has_alias"], true);

    let clear = transact(
        &service.socket,
        &json!({
            "schema": "fleet.desktop-alias.v1",
            "kind": "clear_alias",
            "selector": selector,
            "binding_generation": "1"
        }),
    );
    assert_eq!(clear["kind"], "clear_alias");
    assert_eq!(clear["result"]["outcome"], "cleared");
    let overview = transact(
        &service.socket,
        &json!({"schema": "fleet.desktop.v1", "kind": "overview"}),
    );
    assert_eq!(
        overview["result"]["nodes"][0]["naming"]["display_name"],
        "node-demo"
    );
}

#[test]
fn desktop_overview_supports_a_bounded_many_node_response() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    for index in 0..50 {
        let response = transact(
            &service.socket,
            &json!({
                "schema": "fleet.managed-projection.v1",
                "kind": "apply",
                "document": managed_document_for(&format!("node-{index:03}")),
            }),
        );
        assert_eq!(response["result"]["outcome"], "applied");
    }

    let response = transact(
        &service.socket,
        &json!({"schema": "fleet.desktop.v1", "kind": "overview"}),
    );

    assert_eq!(response["ok"], true);
    assert_eq!(response["result"]["nodes"].as_array().unwrap().len(), 50);
    let length = serde_json::to_vec(&response).unwrap().len();
    assert!(length > 32_768);
    assert!(length <= fleet_control::MAX_RESPONSE_BYTES);
}

#[test]
fn desktop_overview_rejects_more_than_the_bounded_node_limit() {
    let root = private_tempdir();
    let store = FleetStateStore::open(root.path().join("fleet.db")).unwrap();
    for index in 0..=fleet_control::MAX_DESKTOP_NODES {
        let document: ProjectionDocument =
            serde_json::from_value(managed_document_for(&format!("node-{index:03}"))).unwrap();
        store.apply_projection(document).unwrap();
    }
    let service = RunningService::start(root.path());

    let response = transact(
        &service.socket,
        &json!({"schema": "fleet.desktop.v1", "kind": "overview"}),
    );

    assert_eq!(response["schema"], "fleet.desktop.v1");
    assert_eq!(response["kind"], "error");
    assert_eq!(response["ok"], false);
    assert_eq!(response["error"], "invalid_request");
}

#[test]
fn production_python_desktop_client_accepts_exactly_one_rust_service_frame() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    let repository = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new("python3")
        .current_dir(&repository)
        .env("PYTHONPATH", &repository)
        .arg("-c")
        .arg(
            "import json, sys\nfrom pathlib import Path\nfrom hermes_fleet.desktop_api import DesktopApiClient\nprint(json.dumps(DesktopApiClient(socket_path=Path(sys.argv[1])).overview(), sort_keys=True))",
        )
        .arg(&service.socket)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "python client failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let overview: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(overview["schema"], "fleet.desktop.v1");
    assert_eq!(overview["nodes"], json!([]));
}
