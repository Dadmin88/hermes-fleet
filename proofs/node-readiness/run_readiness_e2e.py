#!/usr/bin/env python3
"""Real Nodescale FleetClient plus Python client -> Rust readiness proof."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.fspath(_REPOSITORY))

from hermes_fleet.observation import (  # noqa: E402
    ObservationClient,
    build_observation,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--nodescale-root", type=Path, required=True)
    result.add_argument("--fleet-binary", type=Path, required=True)
    result.add_argument("--cargo", default="cargo")
    return result


def wait_for_socket(process: subprocess.Popen[str], socket: Path) -> None:
    deadline = time.monotonic() + 5
    while not socket.exists():
        if process.poll() is not None:
            _stdout, stderr = process.communicate()
            raise RuntimeError(f"Fleet exited before readiness: {stderr}")
        if time.monotonic() >= deadline:
            raise RuntimeError("Fleet socket did not become ready")
        time.sleep(0.02)


def start_fleet(
    binary: Path,
    socket: Path,
    database: Path,
    *,
    freshness_seconds: int,
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            os.fspath(binary),
            "--socket",
            os.fspath(socket),
            "--database",
            os.fspath(database),
            "--allowed-uid",
            str(os.geteuid()),
            "--freshness-seconds",
            str(freshness_seconds),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        wait_for_socket(process, socket)
    except BaseException:
        terminate_fleet(process)
        raise
    return process


def process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def terminate_fleet(process: subprocess.Popen[str]) -> tuple[str, str]:
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        process.poll()
        if not process_group_exists(process_group):
            break
        time.sleep(0.02)
    if process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        streams = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        streams = process.communicate(timeout=2)
    kill_deadline = time.monotonic() + 2
    while process_group_exists(process_group) and time.monotonic() < kill_deadline:
        time.sleep(0.02)
    if process_group_exists(process_group):
        raise RuntimeError("Fleet process group survived bounded termination")
    return streams


def stop_fleet(process: subprocess.Popen[str], socket: Path) -> None:
    _stdout, stderr = terminate_fleet(process)
    if process.returncode != 0:
        raise RuntimeError(f"Fleet shutdown failed: {stderr}")
    if socket.exists():
        raise RuntimeError("Fleet left its Unix socket after SIGTERM")


def write_nodescale_probe(root: Path, nodescale_root: Path) -> Path:
    source = _REPOSITORY / "proofs" / "managed-control" / "nodescale_probe.rs"
    package = root / "probe"
    (package / "src").mkdir(parents=True)
    shutil.copyfile(source, package / "src" / "main.rs")
    dependency = os.fspath(nodescale_root / "crates" / "nodescale-fleet-client")
    (package / "Cargo.toml").write_text(
        "\n".join(
            [
                "[package]",
                'name = "fleet-nodescale-proof"',
                'version = "0.0.0"',
                'edition = "2024"',
                "publish = false",
                "",
                "[dependencies]",
                f"nodescale-fleet-client = {{ path = {dependency!r} }}",
                'tokio = { version = "1", features = ["macros", "rt-multi-thread"] }',
                "",
                "[workspace]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    shutil.copyfile(
        _REPOSITORY / "proofs" / "managed-control" / "nodescale_probe.Cargo.lock",
        package / "Cargo.lock",
    )
    return package


def run_nodescale_probe(cargo: str, package: Path, socket: Path) -> None:
    completed = subprocess.run(
        [cargo, "run", "--locked", "--quiet", "--", os.fspath(socket), "initial"],
        cwd=package,
        check=False,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Nodescale probe failed: {completed.stderr.strip()}")
    if "applied managed node" not in completed.stdout:
        raise RuntimeError("Nodescale probe did not apply the managed node")


def cleanup_runtime(root: Path, fleet: subprocess.Popen[str] | None) -> None:
    try:
        if fleet is not None:
            terminate_fleet(fleet)
    finally:
        shutil.rmtree(root, ignore_errors=False)


def sample(*, active_workers: int, keryx: str = "available") -> dict[str, object]:
    observation = build_observation(
        hermes_health={
            "api": "healthy",
            "run_submission": True,
            "run_status": True,
            "run_stop": True,
        },
        active_workers=active_workers,
        max_workers=1,
        network_reachable=True,
        keryx_available=keryx == "available",
        worker_available=True,
    )
    return observation


def assert_reasons(view: dict[str, object], *reasons: str) -> None:
    expected_ready = not reasons
    if view.get("scheduler_ready") is not expected_ready:
        raise RuntimeError(f"unexpected readiness boolean: {view}")
    if view.get("reasons") != list(reasons):
        raise RuntimeError(f"unexpected readiness reasons: {view}")


def main() -> None:
    args = parser().parse_args()
    nodescale = args.nodescale_root.resolve(strict=True)
    binary = args.fleet_binary.resolve(strict=True)
    if not (nodescale / "crates" / "nodescale-fleet-client" / "Cargo.toml").is_file():
        raise SystemExit("Nodescale FleetClient crate is missing")

    root = Path(tempfile.mkdtemp(prefix="fleet-node-readiness-proof-"))
    fleet: subprocess.Popen[str] | None = None
    try:
        root.chmod(0o700)
        socket = root / "fleet.sock"
        database = root / "fleet.db"
        package = write_nodescale_probe(root, nodescale)

        fleet = start_fleet(binary, socket, database, freshness_seconds=30)
        run_nodescale_probe(args.cargo, package, socket)
        client = ObservationClient(
            socket_path=socket,
            network_id="net-proof",
            device_id="node-proof",
        )

        if client.publish(sample(active_workers=0)) != "recorded":
            raise RuntimeError("first observation was not recorded")
        assert_reasons(client.inspect())
        print("NODE_READINESS_READY=PASS")

        client.publish(sample(active_workers=1))
        assert_reasons(client.inspect(), "no_worker_capacity")
        print("NODE_READINESS_CAPACITY_EXHAUSTED=PASS")

        client.publish(sample(active_workers=0, keryx="unavailable"))
        assert_reasons(client.inspect(), "keryx_unavailable")
        print("NODE_READINESS_LAYER_DROP=PASS")

        client.publish(sample(active_workers=0))
        assert_reasons(client.inspect())
        print("NODE_READINESS_RECOVERY=PASS")

        stop_fleet(fleet, socket)
        fleet = None
        fleet = start_fleet(binary, socket, database, freshness_seconds=30)
        client = ObservationClient(
            socket_path=socket,
            network_id="net-proof",
            device_id="node-proof",
        )
        restored = client.inspect()
        assert_reasons(restored)
        if restored.get("last_observation") is None:
            raise RuntimeError("restart lost the last observation")
        print("NODE_READINESS_RESTART_RECOVERY=PASS")

        stop_fleet(fleet, socket)
        fleet = None
        fleet = start_fleet(binary, socket, database, freshness_seconds=1)
        client = ObservationClient(
            socket_path=socket,
            network_id="net-proof",
            device_id="node-proof",
        )
        time.sleep(1.05)
        stale = client.inspect()
        assert_reasons(stale, "observation_stale")
        if stale.get("last_observation") is None:
            raise RuntimeError("stale evaluation deleted last-known state")
        client.publish(sample(active_workers=0))
        assert_reasons(client.inspect())
        print("NODE_READINESS_FRESHNESS_AND_LAST_KNOWN=PASS")

        stop_fleet(fleet, socket)
        fleet = None
        print("REAL_NODESCALE_TO_RUST_READINESS_PROOF=PASS")
    finally:
        cleanup_runtime(root, fleet)
    if root.exists():
        raise RuntimeError("temporary proof root residue remains")


if __name__ == "__main__":
    main()
