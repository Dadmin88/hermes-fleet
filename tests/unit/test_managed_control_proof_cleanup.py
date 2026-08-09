"""Lifecycle tests for the disposable Rust managed-control proof harness."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _proof_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "proofs"
        / "managed-control"
        / "run_nodescale_rust_e2e.py"
    )
    spec = importlib.util.spec_from_file_location("managed_control_proof", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_attempts_root_removal_when_process_teardown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = _proof_module()
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "residue").write_text("proof residue", encoding="utf-8")

    def fail_teardown(_process: object) -> None:
        raise RuntimeError("simulated process-group teardown failure")

    monkeypatch.setattr(proof, "terminate_fleet", fail_teardown)
    with pytest.raises(RuntimeError, match="simulated process-group"):
        proof.cleanup_runtime(root, object())

    assert not root.exists()


def test_cleanup_terminates_owned_group_after_leader_already_exited() -> None:
    proof = _proof_module()
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess; "
            "subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL)",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    while leader.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert leader.returncode == 0
    assert proof.process_group_exists(leader.pid)

    proof.terminate_fleet(leader)

    assert not proof.process_group_exists(leader.pid)
