#![cfg(target_os = "linux")]

use std::{
    fs,
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use fleet_control::{ControlConfig, canonical_projection_hash, run};
use fleet_domain::ProjectionDocument;
use fleet_state::FleetStateStore;
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
    let mut stream = UnixStream::connect(socket).unwrap();
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .unwrap();
    stream.write_all(&payload).unwrap();
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
    assert_eq!(
        restored["result"]["last_observation"]["observed_at_ms"],
        recovered_time
    );
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
    assert!(length > fleet_control::MAX_FRAME_BYTES);
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
