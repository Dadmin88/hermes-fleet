#!/usr/bin/env python3
"""Disposable real Nodescale Rust client -> Rust Fleet managed-control proof."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


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
            _out, error = process.communicate()
            raise RuntimeError(f"Fleet exited before readiness: {error}")
        if time.monotonic() >= deadline:
            raise RuntimeError("Fleet socket did not become ready")
        time.sleep(0.02)


def start_fleet(binary: Path, socket: Path, database: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            os.fspath(binary),
            "--socket",
            os.fspath(socket),
            "--database",
            os.fspath(database),
            "--allowed-uid",
            str(os.geteuid()),
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


def stop_fleet(process: subprocess.Popen[str], socket: Path) -> str:
    stdout, stderr = terminate_fleet(process)
    if process.returncode != 0:
        raise RuntimeError(f"Fleet shutdown failed: {stderr}")
    if socket.exists():
        raise RuntimeError("Fleet left its Unix socket after SIGTERM")
    return stdout


def write_probe(root: Path, nodescale_root: Path) -> Path:
    source = Path(__file__).with_name("nodescale_probe.rs")
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
    return package


def run_probe(cargo: str, package: Path, socket: Path, phase: str) -> str:
    completed = subprocess.run(
        [cargo, "run", "--locked", "--quiet", "--", os.fspath(socket), phase],
        cwd=package,
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
    )
    return completed.stdout.strip()


def cleanup_runtime(root: Path, fleet: subprocess.Popen[str] | None) -> None:
    try:
        if fleet is not None:
            terminate_fleet(fleet)
    finally:
        shutil.rmtree(root, ignore_errors=False)


def main() -> None:
    args = parser().parse_args()
    nodescale = args.nodescale_root.resolve(strict=True)
    binary = args.fleet_binary.resolve(strict=True)
    if not (nodescale / "crates" / "nodescale-fleet-client" / "Cargo.toml").is_file():
        raise SystemExit("Nodescale FleetClient crate is missing")

    raw_root = tempfile.mkdtemp(prefix="fleet-managed-control-proof-")
    root = Path(raw_root)
    fleet: subprocess.Popen[str] | None = None
    try:
        root.chmod(0o700)
        socket = root / "fleet.sock"
        database = root / "fleet.db"
        package = write_probe(root, nodescale)
        subprocess.run(
            [args.cargo, "generate-lockfile"],
            cwd=package,
            check=True,
            timeout=120,
            stdout=subprocess.DEVNULL,
        )

        outputs: list[str] = []
        for phase in ("initial", "restart", "restored"):
            fleet = start_fleet(binary, socket, database)
            try:
                outputs.append(run_probe(args.cargo, package, socket, phase))
                outputs.append(stop_fleet(fleet, socket))
                fleet = None
            finally:
                if fleet is not None:
                    terminate_fleet(fleet)
                    fleet = None

        if socket.exists():
            raise RuntimeError("temporary socket residue remains")
        print("\n".join(output.strip() for output in outputs if output.strip()))
        print("REAL_NODESCALE_RUST_TO_RUST_FLEET_PROOF=PASS")
    finally:
        cleanup_runtime(root, fleet)
    if root.exists():
        raise RuntimeError("temporary proof root residue remains")


if __name__ == "__main__":
    main()
