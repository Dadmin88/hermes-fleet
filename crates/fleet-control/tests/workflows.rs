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
    time::{Duration, Instant},
};

use fleet_control::{ControlConfig, run};
use serde_json::{Value, json};
use tempfile::TempDir;

const SCHEMA: &str = "fleet.workflow.v1";

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

fn transact(socket: &Path, request: Value) -> Value {
    transact_payload(socket, &serde_json::to_vec(&request).expect("request"))
}

fn transact_payload(socket: &Path, payload: &[u8]) -> Value {
    let mut stream = UnixStream::connect(socket).expect("connect");
    stream
        .write_all(&(payload.len() as u32).to_be_bytes())
        .expect("header");
    stream.write_all(payload).expect("payload");
    stream
        .shutdown(std::net::Shutdown::Write)
        .expect("half close");
    let mut header = [0_u8; 4];
    stream.read_exact(&mut header).expect("response header");
    let mut body = vec![0_u8; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut body).expect("response body");
    serde_json::from_slice(&body).expect("response JSON")
}

fn recipe_document() -> Value {
    json!({
        "schema": "fleet.workflow-editor.v2",
        "id": "workflow-recipe-v2",
        "name": "Compile recipe",
        "nodes": [{
            "id": "build",
            "type": "recipe-step",
            "title": "Build",
            "position": {"x": 24.0, "y": 36.0},
            "configuration": {
                "agent_name": "developer",
                "agent_version": ">=1,<2",
                "cpu_requested_millis": 1000,
                "memory_requested_bytes": 1073741824,
                "gpu_mode": "required",
                "gpu_count": 1
            },
            "target": null,
            "runtime": "recipe"
        }],
        "connections": [],
        "metadata": {"executionAvailable": false}
    })
}

fn document(name: &str) -> Value {
    json!({
        "schema": "fleet.workflow-editor.v1",
        "id": "workflow-1",
        "name": name,
        "nodes": [{
            "id": "trigger-1",
            "type": "third-party.example/custom-action",
            "title": "Custom action",
            "position": {"x": 24.0, "y": 36.0},
            "configuration": {"plugin": {"opaque": [1, true, null]}},
            "target": {"pluginTarget": "preserved"},
            "runtime": "unavailable"
        }],
        "connections": [],
        "metadata": {"executionAvailable": false}
    })
}

#[test]
fn authenticated_workflow_api_versions_persists_lists_and_soft_deletes() {
    let root = private_tempdir();
    let mut service = RunningService::start(root.path());

    assert_eq!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"capabilities"})
        ),
        json!({
            "schema":SCHEMA,
            "kind":"capabilities",
            "ok":true,
            "result":{
                "kinds":["capabilities","create","read","read_version","update","list","delete"],
                "executionAvailable":false
            }
        })
    );

    assert_eq!(
        transact_payload(
            &service.socket,
            br#"{"schema":"fleet.workflow.v1","kind":"create","document":{"schema":"fleet.workflow-editor.v1","id":"duplicate-workflow","name":"Duplicate","nodes":[{"id":"node-1","type":"vendor/block","title":"Node","position":{"x":0,"y":0},"configuration":{"value":1,"value":2},"target":null,"runtime":"unavailable"}],"connections":[],"metadata":{"executionAvailable":false}}}"#,
        ),
        json!({"schema":SCHEMA,"kind":"error","ok":false,"error":"invalid_request"})
    );

    let created = transact(
        &service.socket,
        json!({"schema":SCHEMA,"kind":"create","document":document("Version one")}),
    );
    assert_eq!(created["kind"], "create");
    assert_eq!(created["ok"], true);
    assert_eq!(created["result"]["outcome"], "created");
    assert_eq!(created["result"]["revision"]["version"], 1);
    assert_eq!(
        created["result"]["revision"]["document"]["metadata"]["executionAvailable"],
        false
    );
    assert_eq!(
        created["result"]["revision"]["document"]["nodes"][0]["type"],
        "third-party.example/custom-action"
    );
    assert_eq!(
        created["result"]["revision"]["document"]["nodes"][0]["target"]["pluginTarget"],
        "preserved"
    );

    let updated = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"update",
            "expectedVersion":1,
            "document":document("Version two")
        }),
    );
    assert_eq!(updated["result"]["outcome"], "version_created");
    assert_eq!(updated["result"]["revision"]["version"], 2);

    let stale = transact(
        &service.socket,
        json!({
            "schema":SCHEMA,
            "kind":"update",
            "expectedVersion":1,
            "document":document("Stale edit")
        }),
    );
    assert_eq!(
        stale,
        json!({"schema":SCHEMA,"kind":"error","ok":false,"error":"invalid_request"})
    );

    assert_eq!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"read_version","workflowId":"workflow-1","version":1})
        )["result"]["revision"]["document"]["name"],
        "Version one"
    );
    assert_eq!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"read","workflowId":"workflow-1"})
        )["result"]["revision"]["document"]["name"],
        "Version two"
    );
    assert_eq!(
        transact(&service.socket, json!({"schema":SCHEMA,"kind":"list"}))["result"]["workflows"][0]
            ["latestVersion"],
        2
    );

    service.stop();
    let mut service = RunningService::start(root.path());
    assert_eq!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"read","workflowId":"workflow-1"})
        )["result"]["revision"]["version"],
        2
    );
    assert_eq!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"delete","workflowId":"workflow-1","expectedVersion":2})
        )["result"]["outcome"],
        "deleted"
    );
    assert_eq!(
        transact(&service.socket, json!({"schema":SCHEMA,"kind":"list"}))["result"]["workflows"],
        json!([])
    );
    assert_eq!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"read","workflowId":"workflow-1"})
        )["result"]["revision"],
        Value::Null
    );
    assert_eq!(
        transact(
            &service.socket,
            json!({"schema":SCHEMA,"kind":"read_version","workflowId":"workflow-1","version":1})
        )["result"]["revision"]["document"]["name"],
        "Version one"
    );

    service.stop();
}

#[test]
fn authenticated_workflow_api_preserves_v2_recipe_revision_without_execution_authority() {
    let root = private_tempdir();
    let mut service = RunningService::start(root.path());
    let document = recipe_document();

    let created = transact(
        &service.socket,
        json!({"schema":SCHEMA,"kind":"create","document":document}),
    );
    assert_eq!(created["ok"], true);
    assert_eq!(created["result"]["revision"]["version"], 1);
    assert_eq!(
        created["result"]["revision"]["document"]["schema"],
        "fleet.workflow-editor.v2"
    );
    assert_eq!(
        created["result"]["revision"]["document"]["nodes"][0]["type"],
        "recipe-step"
    );
    assert_eq!(
        created["result"]["revision"]["document"]["nodes"][0]["runtime"],
        "recipe"
    );
    assert_eq!(
        created["result"]["revision"]["document"]["metadata"]["executionAvailable"],
        false
    );
    let created_hash = created["result"]["revision"]["contentHash"]
        .as_str()
        .unwrap()
        .to_owned();

    service.stop();
    let mut service = RunningService::start(root.path());
    let read = transact(
        &service.socket,
        json!({"schema":SCHEMA,"kind":"read_version","workflowId":"workflow-recipe-v2","version":1}),
    );
    assert_eq!(read["result"]["revision"]["contentHash"], created_hash);
    assert_eq!(
        read["result"]["revision"]["document"]["metadata"]["executionAvailable"],
        false
    );
    service.stop();
}
