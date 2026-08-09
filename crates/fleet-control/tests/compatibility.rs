#![cfg(target_os = "linux")]

use std::{
    fs,
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

use fleet_control::{ControlConfig, canonical_projection_hash, run};
use serde_json::{Value, json};

#[test]
fn canonical_hash_matches_nodescale_unicode_oracle() {
    let document = json!({
        "source":"nodescale",
        "network_id":"nét😀",
        "device_id":"device-a",
        "projection_generation":"7",
        "membership_generation":"7",
        "binding_generation":"7",
        "content_hash":"",
        "operation":"upsert",
        "generated_operations":["fleet.health","fleet.inventory"],
        "provenance":{
            "source":"nodescale",
            "network_id":"nét😀",
            "device_id":"device-a",
            "snapshot":"7"
        }
    });
    assert_eq!(
        canonical_projection_hash(&document).expect("hash"),
        "eac86d353bfeccc60d37e0a2d5da7b56cf91a10d4b928b67442128e0d9d7ee26"
    );
}

#[test]
fn shared_managed_control_fixture_runs_through_real_service() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../fixtures/f0/managed-control-v1.json"))
            .expect("fixture");
    assert_eq!(fixture["schema"], "hermes-fleet.managed-control-compat.v1");
    assert_eq!(
        canonical_projection_hash(&fixture["document"]).expect("canonical hash"),
        fixture["document"]["content_hash"]
    );

    let root = tempfile::tempdir().expect("tempdir");
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).expect("mode");
    let socket = root.path().join("fleet.sock");
    let database = root.path().join("fleet.db");
    let stop = Arc::new(AtomicBool::new(false));
    let config =
        ControlConfig::new(&socket, &database, unsafe { libc::geteuid() }, None).expect("config");
    let thread_stop = Arc::clone(&stop);
    let service = thread::spawn(move || run(config, thread_stop).expect("service"));
    wait_for(&socket, true);

    let capabilities = transact(&socket, &fixture["capabilities_request"]);
    assert_eq!(capabilities["result"], fixture["capabilities_result"]);
    let apply_request = json!({
        "schema":"fleet.managed-projection.v1",
        "kind":"apply",
        "document":fixture["document"].clone()
    });
    for expected in fixture["apply_outcomes"].as_array().expect("outcomes") {
        assert_eq!(
            transact(&socket, &apply_request)["result"]["outcome"],
            *expected
        );
    }
    let inspect_request = json!({
        "schema":"fleet.managed-projection.v1",
        "kind":"inspect",
        "selector":fixture["inspect_selector"].clone()
    });
    let inspected = transact(&socket, &inspect_request);
    assert_eq!(
        inspected["result"]["generated"]["state"],
        fixture["expected_state"]
    );
    assert_eq!(
        inspected["result"]["effective"]["allowed_operations"],
        fixture["expected_effective_operations"]
    );

    stop.store(true, Ordering::Release);
    service.join().expect("join");
    wait_for(&socket, false);
}

fn transact(socket: &Path, request: &Value) -> Value {
    let payload = serde_json::to_vec(request).expect("payload");
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
    let mut response = vec![0_u8; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut response).expect("response");
    serde_json::from_slice(&response).expect("response JSON")
}

fn wait_for(path: &Path, exists: bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while path.exists() != exists {
        assert!(Instant::now() < deadline, "timed out waiting for {path:?}");
        thread::sleep(Duration::from_millis(10));
    }
}
