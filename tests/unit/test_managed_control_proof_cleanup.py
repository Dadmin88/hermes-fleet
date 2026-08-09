"""Lifecycle tests for the disposable Rust managed-control proof harness."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROOF_PATHS = (
    ("managed_control_proof", "managed-control", "run_nodescale_rust_e2e.py"),
    ("node_readiness_proof", "node-readiness", "run_readiness_e2e.py"),
)


def _proof_module(module_name: str, proof_directory: str, filename: str):
    path = Path(__file__).resolve().parents[2] / "proofs" / proof_directory / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("module_name", "proof_directory", "filename"), PROOF_PATHS)
def test_cleanup_attempts_root_removal_when_process_teardown_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    proof_directory: str,
    filename: str,
) -> None:
    proof = _proof_module(module_name, proof_directory, filename)
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "residue").write_text("proof residue", encoding="utf-8")

    def fail_teardown(_process: object) -> None:
        raise RuntimeError("simulated process-group teardown failure")

    monkeypatch.setattr(proof, "terminate_fleet", fail_teardown)
    with pytest.raises(RuntimeError, match="simulated process-group"):
        proof.cleanup_runtime(root, object())

    assert not root.exists()


@pytest.mark.parametrize(("module_name", "proof_directory", "filename"), PROOF_PATHS)
def test_cleanup_terminates_owned_group_after_leader_already_exited(
    module_name: str, proof_directory: str, filename: str
) -> None:
    proof = _proof_module(module_name, proof_directory, filename)
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
