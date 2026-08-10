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

const DIGEST: &str = "7a9480c8d1d3e34ee64f66cfc8c06d7bfdcc6f9c7fdeee6d433cbdb637259b0f";

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
            .unwrap()
            .with_freshness_window(Duration::from_secs(30))
            .unwrap();
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || run(config, thread_stop).unwrap());
        wait_for(&socket, true);
        Self {
            socket,
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
    let mut document = json!({
        "source": "nodescale",
        "network_id": "net-demo",
        "device_id": "node-demo",
        "projection_generation": "1",
        "membership_generation": "1",
        "binding_generation": "1",
        "content_hash": "",
        "operation": "upsert",
        "generated_operations": ["fleet.health", "fleet.inventory", "fleet.message"],
        "provenance": {
            "source": "nodescale",
            "network_id": "net-demo",
            "device_id": "node-demo",
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

fn observation(content_digest: Value) -> Value {
    let mut profile = json!({
        "name": "agency-backend-engineer",
        "version": "0.1.0"
    });
    if !content_digest.is_null() {
        profile["content_digest"] = content_digest;
    }
    json!({
        "admission_generation": 1,
        "observed_at_ms": now_ms(),
        "network": "reachable",
        "keryx": "available",
        "hermes": "available",
        "worker": "available",
        "capacity": {"active_workers": 0, "max_workers": 1},
        "profiles": [profile],
        "resources": {}
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
fn exact_profile_digest_round_trips_through_real_control_service() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    apply_managed(&service.socket);

    let response = observe(&service.socket, observation(json!(DIGEST)));
    assert_eq!(response["result"]["outcome"], "recorded");

    let readiness = inspect(&service.socket);
    assert_eq!(readiness["result"]["profiles"], json!([{
        "name": "agency-backend-engineer",
        "version": "0.1.0",
        "content_digest": DIGEST
    }]));
}

#[test]
fn malformed_profile_digest_is_rejected_by_real_control_service() {
    let root = private_tempdir();
    let service = RunningService::start(root.path());
    apply_managed(&service.socket);

    let response = observe(&service.socket, observation(json!("A".repeat(64))));
    assert_eq!(response["error"], "invalid_request");
    assert_eq!(inspect(&service.socket)["result"]["profiles"], Value::Null);
}
