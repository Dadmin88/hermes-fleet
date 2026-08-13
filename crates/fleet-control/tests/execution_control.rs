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
use serde_json::{Value, json};
use tempfile::TempDir;

const SCHEMA: &str = "fleet.execution-control.v1";

struct RunningService {
    socket: PathBuf,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}

impl RunningService {
    fn start(root: &Path) -> Self {
        let socket = root.join("fleet.sock");
        let database = root.join("fleet.db");
        let stop = Arc::new(AtomicBool::new(false));
        let config = ControlConfig::new(&socket, &database, unsafe { libc::geteuid() }, None)
            .expect("config");
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || run(config, thread_stop).expect("service"));
        wait_for(&socket, true);
        Self {
            socket,
            stop,
            thread: Some(thread),
        }
    }
}

impl Drop for RunningService {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.thread.take().expect("thread").join().expect("join");
        wait_for(&self.socket, false);
    }
}

fn private_tempdir() -> TempDir {
    let root = tempfile::tempdir().expect("tempdir");
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).expect("private root");
    root
}

fn wait_for(path: &Path, exists: bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while path.exists() != exists {
        assert!(Instant::now() < deadline, "timed out waiting for {path:?}");
        thread::sleep(Duration::from_millis(10));
    }
}

fn transact(socket: &Path, request: Value) -> Value {
    let payload = serde_json::to_vec(&request).expect("request");
    let mut stream = UnixStream::connect(socket).expect("connect");
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .expect("header");
    stream.write_all(&payload).expect("payload");
    stream
        .shutdown(std::net::Shutdown::Write)
        .expect("half close");
    let mut header = [0_u8; 4];
    stream.read_exact(&mut header).expect("response header");
    let mut body = vec![0_u8; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut body).expect("response body");
    serde_json::from_slice(&body).expect("response JSON")
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time")
        .as_millis() as u64
}

fn activate(socket: &Path) {
    let mut document = json!({
        "source": "nodescale",
        "network_id": "network-1",
        "device_id": "device-1",
        "projection_generation": "1",
        "membership_generation": "1",
        "binding_generation": "7",
        "content_hash": "",
        "operation": "upsert",
        "generated_operations": ["fleet.health", "fleet.inventory", "fleet.message"],
        "provenance": {
            "source": "nodescale",
            "network_id": "network-1",
            "device_id": "device-1",
            "snapshot": "1",
            "binding_id": "binding-1",
            "authenticated_peer_id": "peer-1"
        }
    });
    document["content_hash"] = Value::String(canonical_projection_hash(&document).unwrap());
    assert_eq!(
        transact(
            socket,
            json!({"schema":"fleet.managed-projection.v1","kind":"apply","document":document})
        )["result"]["outcome"],
        "applied"
    );
    assert_eq!(
        transact(
            socket,
            json!({
                "schema":"fleet.node-observation.v1",
                "kind":"observe",
                "selector":{"source":"nodescale","network_id":"network-1","device_id":"device-1"},
                "observation":{
                    "admission_generation":1,
                    "observed_at_ms":now_ms(),
                    "network":"reachable",
                    "keryx":"available",
                    "hermes":"available",
                    "worker":"available",
                    "capacity":{"active_workers":0,"max_workers":1},
                    "resources":{
                        "cpu":{"logical_cores":8,"load_basis_points":100},
                        "ram":{"total_bytes":16000,"available_bytes":8000},
                        "swap":{"total_bytes":0,"available_bytes":0},
                        "disk":{"total_bytes":100000,"available_bytes":50000},
                        "gpu":null
                    }
                }
            })
        )["result"]["outcome"],
        "recorded"
    );
}

fn instance() -> Value {
    json!({
        "instance_id":"instance-1",
        "idempotency_key":"request-1",
        "recipe_hash":format!("sha256:{}", "1".repeat(64)),
        "capabilities_hash":format!("sha256:{}", "2".repeat(64)),
        "target":{
            "source":"nodescale",
            "network_id":"network-1",
            "device_id":"device-1",
            "binding_generation":7,
            "admission_generation":1
        },
        "generation":1,
        "phase":{"kind":"reserved"},
        "created_at_ms":now_ms(),
        "updated_at_ms":now_ms()
    })
}

#[test]
fn local_control_reserves_admits_transitions_and_restores_exact_instance() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    activate(&service.socket);
    let desired = instance();
    let deadline = now_ms() + 60_000;

    let admitted = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"reserve_admit",
            "instance":desired,
            "operation":"fleet.hermes.run",
            "operation_authorized":true,
            "current_capabilities_hash":format!("sha256:{}", "2".repeat(64)),
            "deadline_ms":deadline
        }),
    );
    assert_eq!(admitted["ok"], true);
    assert_eq!(admitted["result"]["created"], true);
    assert_eq!(admitted["result"]["decision"]["status"], "admitted");
    assert_eq!(admitted["result"]["instance"]["generation"], 1);

    let replay = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"reserve_admit",
            "instance":admitted["result"]["instance"],
            "operation":"fleet.hermes.run",
            "operation_authorized":true,
            "current_capabilities_hash":format!("sha256:{}", "2".repeat(64)),
            "deadline_ms":deadline
        }),
    );
    assert_eq!(replay["result"]["created"], false);

    let prepared = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"transition",
            "instance_id":"instance-1",
            "expected_generation":1,
            "phase":{"kind":"prepared","backend_kind":"fleet.dev/docker-oci","realization_id":"container-1"}
        }),
    );
    assert_eq!(prepared["result"]["instance"]["generation"], 2);

    let restored = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,"kind":"get","instance_id":"instance-1"
        }),
    );
    assert_eq!(
        restored["result"]["instance"],
        prepared["result"]["instance"]
    );
}

#[test]
fn local_admission_fails_closed_on_stale_generation_policy_and_capacity() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    activate(&service.socket);

    let mut stale = instance();
    stale["target"]["binding_generation"] = json!(6);
    let rejected = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"reserve_admit",
            "instance":stale,
            "operation":"fleet.hermes.run",
            "operation_authorized":true,
            "current_capabilities_hash":format!("sha256:{}", "2".repeat(64)),
            "deadline_ms":now_ms()+60_000
        }),
    );
    assert_eq!(rejected["result"]["decision"]["status"], "stale_target");
    assert!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"get","instance_id":"instance-1"})
        )["result"]["instance"]
            .is_null()
    );

    let mut denied_instance = instance();
    denied_instance["instance_id"] = json!("instance-2");
    denied_instance["idempotency_key"] = json!("request-2");
    let denied = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"reserve_admit",
            "instance":denied_instance,
            "operation":"fleet.hermes.run",
            "operation_authorized":false,
            "current_capabilities_hash":format!("sha256:{}", "2".repeat(64)),
            "deadline_ms":now_ms()+60_000
        }),
    );
    assert_eq!(denied["result"]["decision"]["status"], "policy_denied");

    let mut drifted_instance = instance();
    drifted_instance["instance_id"] = json!("instance-3");
    drifted_instance["idempotency_key"] = json!("request-3");
    let drifted = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"reserve_admit",
            "instance":drifted_instance,
            "operation":"fleet.hermes.run",
            "operation_authorized":true,
            "current_capabilities_hash":format!("sha256:{}", "3".repeat(64)),
            "deadline_ms":now_ms()+60_000
        }),
    );
    assert_eq!(
        drifted["result"]["decision"]["status"],
        "capabilities_changed"
    );
}

#[test]
fn execution_control_rejects_unknown_fields_and_forged_lifecycle() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    activate(&service.socket);

    let malformed = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,"kind":"get","instance_id":"instance-1","extra":true
        }),
    );
    assert_eq!(malformed["ok"], false);

    let missing = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"transition",
            "instance_id":"instance-1",
            "expected_generation":1,
            "phase":{"kind":"cleaned"}
        }),
    );
    assert_eq!(missing["ok"], false);
}
