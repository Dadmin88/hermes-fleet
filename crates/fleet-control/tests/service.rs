#![cfg(target_os = "linux")]

use std::{
    fs,
    io::{Read, Write},
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use fleet_control::{ControlConfig, canonical_projection_hash, run};
use fleet_domain::FleetOperation;
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
    fn start(root: &Path, allowed_uid: u32) -> Self {
        let socket = root.join("fleet.sock");
        let database = root.join("fleet.db");
        let stop = Arc::new(AtomicBool::new(false));
        let config = ControlConfig::new(&socket, &database, allowed_uid, None).expect("config");
        let thread_stop = Arc::clone(&stop);
        let thread = thread::spawn(move || run(config, thread_stop).expect("service"));
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
        self.thread.take().expect("thread").join().expect("join");
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

fn transact(socket: &Path, request: &Value) -> Value {
    transact_bytes(socket, serde_json::to_vec(request).expect("request"))
}

fn transact_bytes(socket: &Path, payload: Vec<u8>) -> Value {
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

fn document(
    projection: &str,
    membership: &str,
    binding: &str,
    operation: &str,
    grants: &[&str],
    snapshot: &str,
) -> Value {
    let mut document = json!({
        "source": "nodescale",
        "network_id": "net-demo",
        "device_id": "node-demo",
        "projection_generation": projection,
        "membership_generation": membership,
        "binding_generation": binding,
        "content_hash": "",
        "operation": operation,
        "generated_operations": grants,
        "provenance": {
            "source": "nodescale",
            "network_id": "net-demo",
            "device_id": "node-demo",
            "snapshot": snapshot
        }
    });
    let hash = canonical_projection_hash(&document).expect("canonical hash");
    document["content_hash"] = Value::String(hash);
    document
}

fn apply(socket: &Path, document: Value) -> Value {
    transact(
        socket,
        &json!({
            "schema": "fleet.managed-projection.v1",
            "kind": "apply",
            "document": document
        }),
    )
}

fn inspect(socket: &Path) -> Value {
    transact(
        socket,
        &json!({
            "schema": "fleet.managed-projection.v1",
            "kind": "inspect",
            "selector": {
                "source": "nodescale",
                "network_id": "net-demo",
                "device_id": "node-demo"
            }
        }),
    )
}

#[test]
fn real_service_persists_projection_semantics_local_deny_and_restart() {
    let root = private_tempdir();
    let uid = unsafe { libc::geteuid() };
    let mut service = RunningService::start(root.path(), uid);

    let capabilities = transact(
        &service.socket,
        &json!({"schema":"fleet.managed-projection.v1","kind":"capabilities"}),
    );
    assert_eq!(
        capabilities,
        json!({
            "schema":"fleet.managed-projection.v1",
            "kind":"capabilities",
            "ok":true,
            "result":{"kinds":["capabilities","apply","inspect"]}
        })
    );

    let active = document(
        "1",
        "1",
        "1",
        "upsert",
        &["fleet.health", "fleet.inventory", "fleet.message"],
        "1",
    );
    assert_eq!(
        apply(&service.socket, active.clone())["result"]["outcome"],
        "applied"
    );
    assert_eq!(
        apply(&service.socket, active.clone())["result"]["outcome"],
        "already_applied"
    );

    let conflict = document("1", "1", "1", "upsert", &["fleet.health"], "1");
    assert_eq!(
        apply(&service.socket, conflict)["result"]["outcome"],
        "conflict"
    );
    let gap = document("3", "3", "3", "upsert", &["fleet.health"], "3");
    assert_eq!(apply(&service.socket, gap)["result"]["outcome"], "gap");

    let second = document(
        "2",
        "2",
        "2",
        "upsert",
        &["fleet.health", "fleet.inventory", "fleet.message"],
        "2",
    );
    assert_eq!(
        apply(&service.socket, second)["result"]["outcome"],
        "applied"
    );
    assert_eq!(apply(&service.socket, active)["result"]["outcome"], "stale");
    let regression = document("3", "1", "2", "upsert", &["fleet.health"], "3");
    assert_eq!(
        apply(&service.socket, regression)["result"]["outcome"],
        "regression"
    );

    FleetStateStore::open(&service.database)
        .expect("open state")
        .set_operator_deny(
            "nodescale",
            "net-demo",
            "node-demo",
            FleetOperation::Health,
            true,
        )
        .expect("deny");
    let state = inspect(&service.socket);
    assert_eq!(
        state["result"]["generated"]["allowed_operations"],
        json!(["fleet.health", "fleet.inventory", "fleet.message"])
    );
    assert_eq!(
        state["result"]["effective"]["allowed_operations"],
        json!(["fleet.inventory", "fleet.message"])
    );
    assert_eq!(
        state["result"]["effective"]["operator_denied_operations"],
        json!(["fleet.health"])
    );

    let disabled = document("3", "2", "2", "disable", &[], "3");
    assert_eq!(
        apply(&service.socket, disabled)["result"]["outcome"],
        "applied"
    );
    assert_eq!(
        inspect(&service.socket)["result"]["effective"]["allowed_operations"],
        json!([])
    );

    let removed = document("4", "2", "2", "remove", &[], "4");
    assert_eq!(
        apply(&service.socket, removed)["result"]["outcome"],
        "applied"
    );
    service.stop();
    assert!(!service.socket.exists());

    let mut restarted = RunningService::start(root.path(), uid);
    let restored = inspect(&restarted.socket);
    assert_eq!(restored["result"]["generated"]["state"], "removed");
    assert_eq!(
        restored["result"]["generated"]["projection_generation"],
        "4"
    );
    assert_eq!(
        restored["result"]["effective"]["operator_denied_operations"],
        json!(["fleet.health"])
    );
    restarted.stop();
}

#[test]
fn service_rejects_wrong_uid_duplicate_keys_and_trailing_data() {
    let root = private_tempdir();
    let uid = unsafe { libc::geteuid() };
    let mut denied = RunningService::start(root.path(), uid.saturating_add(1));
    let mut stream = UnixStream::connect(&denied.socket).expect("connect denied");
    let payload = br#"{"schema":"fleet.managed-projection.v1","kind":"capabilities"}"#;
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .expect("header");
    stream.write_all(payload).expect("payload");
    stream
        .shutdown(std::net::Shutdown::Write)
        .expect("half close");
    let mut byte = [0_u8; 1];
    match stream.read(&mut byte) {
        Ok(0) => {}
        Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => {}
        result => panic!("denied peer received data: {result:?}"),
    }
    denied.stop();

    let mut service = RunningService::start(root.path(), uid);
    let duplicate =
        br#"{"schema":"fleet.managed-projection.v1","kind":"capabilities","kind":"capabilities"}"#
            .to_vec();
    assert_eq!(
        transact_bytes(&service.socket, duplicate)["error"],
        "invalid_request"
    );

    let payload = br#"{"schema":"fleet.managed-projection.v1","kind":"capabilities"}"#;
    let mut stream = UnixStream::connect(&service.socket).expect("connect trailing");
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .expect("header");
    stream.write_all(payload).expect("payload");
    stream.write_all(b"x").expect("trailing");
    stream
        .shutdown(std::net::Shutdown::Write)
        .expect("half close");
    let mut header = [0_u8; 4];
    stream.read_exact(&mut header).expect("error header");
    let mut response = vec![0; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut response).expect("error body");
    assert_eq!(
        serde_json::from_slice::<Value>(&response).expect("error")["error"],
        "invalid_request"
    );
    service.stop();
}

#[test]
fn malformed_and_authority_expanding_requests_fail_closed() {
    let root = private_tempdir();
    let uid = unsafe { libc::geteuid() };
    let mut service = RunningService::start(root.path(), uid);
    let invalid_payloads = [
        br#"{"schema":"fleet.managed-projection.v1","kind":"capabilities","extra":"x"}"#.as_slice(),
        br#"{"schema":"fleet.managed-projection.v1","kind":"inspect","selector":{"source":"nodescale","network_id":"net-demo","device_id":1}}"#,
        br#"{"schema":"fleet.managed-projection.v1","kind":"inspect","selector":{"source":"nodescale","network_id":"net-demo","network_id":"other","device_id":"node-demo"}}"#,
        b"\xff",
    ];
    for payload in invalid_payloads {
        assert_eq!(
            transact_bytes(&service.socket, payload.to_vec())["error"],
            "invalid_request"
        );
    }

    let mut authority = document("1", "1", "1", "upsert", &["fleet.health"], "1");
    authority["generated_operations"] = json!(["fleet.hermes.run"]);
    assert_eq!(
        apply(&service.socket, authority)["error"],
        "invalid_request"
    );

    let mut bad_hash = document("1", "1", "1", "upsert", &["fleet.health"], "1");
    bad_hash["content_hash"] = Value::String("a".repeat(64));
    assert_eq!(apply(&service.socket, bad_hash)["error"], "invalid_request");

    let mut stream = UnixStream::connect(&service.socket).expect("connect oversize");
    stream
        .write_all(&((fleet_control::MAX_FRAME_BYTES as u32) + 1).to_be_bytes())
        .expect("oversize header");
    stream
        .shutdown(std::net::Shutdown::Write)
        .expect("half close");
    let mut header = [0_u8; 4];
    stream.read_exact(&mut header).expect("oversize response");
    let mut response = vec![0; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut response).expect("oversize body");
    assert_eq!(
        serde_json::from_slice::<Value>(&response).expect("error")["error"],
        "invalid_request"
    );
    service.stop();
}

#[test]
fn configured_group_transport_changes_mode_but_not_uid_authentication() {
    let root = private_tempdir();
    let socket_parent = root.path().join("socket");
    let database_parent = root.path().join("database");
    fs::create_dir(&socket_parent).expect("socket parent");
    fs::create_dir(&database_parent).expect("database parent");
    fs::set_permissions(&socket_parent, fs::Permissions::from_mode(0o750)).expect("socket mode");
    fs::set_permissions(&database_parent, fs::Permissions::from_mode(0o700))
        .expect("database mode");
    let socket = socket_parent.join("fleet.sock");
    let database = database_parent.join("fleet.db");
    let stop = Arc::new(AtomicBool::new(false));
    let config = ControlConfig::new(
        &socket,
        &database,
        unsafe { libc::geteuid() },
        Some(unsafe { libc::getegid() }),
    )
    .expect("config");
    let thread_stop = Arc::clone(&stop);
    let thread = thread::spawn(move || run(config, thread_stop).expect("service"));
    wait_for(&socket, true);
    let metadata = fs::symlink_metadata(&socket).expect("socket metadata");
    assert_eq!(metadata.permissions().mode() & 0o777, 0o660);
    assert_eq!(
        transact(
            &socket,
            &json!({"schema":"fleet.managed-projection.v1","kind":"capabilities"})
        )["ok"],
        true
    );
    stop.store(true, Ordering::Release);
    thread.join().expect("join");
    wait_for(&socket, false);
}

#[test]
fn socket_is_private_and_owned_cleanup_does_not_remove_replacement() {
    let root = private_tempdir();
    let uid = unsafe { libc::geteuid() };
    let mut service = RunningService::start(root.path(), uid);
    let mode = fs::symlink_metadata(&service.socket)
        .expect("socket metadata")
        .permissions()
        .mode()
        & 0o777;
    assert_eq!(mode, 0o600);

    let payload = br#"{"schema":"fleet.managed-projection.v1","kind":"capabilities"}"#;
    let mut blocked = UnixStream::connect(&service.socket).expect("blocked connection");
    blocked
        .write_all(&(payload.len() as u32).to_be_bytes())
        .expect("header");
    blocked.write_all(payload).expect("payload");
    thread::sleep(Duration::from_millis(50));
    service.stop.store(true, Ordering::Release);
    thread::sleep(Duration::from_millis(50));
    fs::remove_file(&service.socket).expect("unlink original socket name");
    let replacement = UnixListener::bind(&service.socket).expect("bind replacement");
    service.thread.take().expect("thread").join().expect("join");
    assert!(
        service.socket.exists(),
        "replacement socket must survive cleanup"
    );
    drop(blocked);
    drop(replacement);
    fs::remove_file(&service.socket).expect("remove replacement");
}
