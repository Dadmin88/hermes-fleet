#![cfg(target_os = "linux")]

use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::Path,
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[test]
fn binary_handles_sigterm_and_removes_owned_socket() {
    let root = tempfile::tempdir().expect("tempdir");
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).expect("mode");
    let socket = root.path().join("fleet.sock");
    let database = root.path().join("fleet.db");
    let child = Command::new(env!("CARGO_BIN_EXE_fleet-managed-control"))
        .args([
            "--socket",
            socket.to_str().expect("socket path"),
            "--database",
            database.to_str().expect("database path"),
            "--allowed-uid",
            &unsafe { libc::geteuid() }.to_string(),
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn service");
    wait_for(&socket, true);

    let killed = unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
    assert_eq!(killed, 0);
    let output = child.wait_with_output().expect("wait");
    assert!(
        output.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );
    wait_for(&socket, false);
    let stdout = String::from_utf8(output.stdout).expect("stdout");
    assert!(stdout.contains("Rust Fleet started"));
    assert!(stdout.contains("Rust Fleet stopped cleanly"));
}

fn wait_for(path: &Path, exists: bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while path.exists() != exists {
        assert!(Instant::now() < deadline, "timed out waiting for {path:?}");
        thread::sleep(Duration::from_millis(10));
    }
}
