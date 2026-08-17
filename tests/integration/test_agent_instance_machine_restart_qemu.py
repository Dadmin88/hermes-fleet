from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _qemu_restart_proof_ready() -> bool:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        return False
    if not Path("/dev/kvm").exists():
        return False
    for executable in ("qemu-system-x86_64", "busybox", "cpio", "ldd", "modinfo"):
        if shutil.which(executable) is None:
            return False
    kernel = Path("/boot") / f"vmlinuz-{platform.uname().release}"
    if not kernel.is_file():
        return False
    for module in ("netfs", "9pnet", "9p", "9pnet_virtio"):
        completed = subprocess.run(
            ["modinfo", "-n", module],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        if completed.returncode != 0 or not Path(completed.stdout.strip()).is_file():
            return False
    return True


@pytest.mark.skipif(
    not _qemu_restart_proof_ready(),
    reason="QEMU/KVM machine-restart proof prerequisites are unavailable",
)
def test_persistent_agent_survives_real_isolated_guest_reboot(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    proof = tmp_path / "phase6-machine-restart-proof"
    script = repository / "scripts" / "run_phase6_machine_restart_proof.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--proof-dir", str(proof)],
        cwd=repository,
        capture_output=True,
        check=False,
        text=True,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stderr
    assert "PHASE6_MACHINE_RESTART_PROOF_OK" in completed.stdout

    observed = json.loads((proof / "phase6-success.json").read_text(encoding="utf-8"))
    assert observed["first_boot_id"] != observed["second_boot_id"]
    assert observed["instance_id"].startswith("sha256:")
    assert observed["profile"].startswith("fleet-agent-")
    assert observed["skills_generation"] == 1
    assert observed["learned"] == "learned before guest reboot\n"

    log = (proof / "qemu-serial.log").read_text(encoding="utf-8", errors="replace")
    for marker in (
        "PHASE6_GUEST_FIRST_BOOT_OK",
        "PHASE6_GUEST_REBOOTING",
        "PHASE6_GUEST_SECOND_BOOT_OK",
        "PHASE6_GUEST_PROOF_COMPLETE",
    ):
        assert marker in log
