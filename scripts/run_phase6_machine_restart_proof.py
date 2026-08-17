#!/usr/bin/env python3
"""Prove a persistent Agent Instance survives a real isolated guest reboot.

The host launches the current kernel under QEMU/KVM with a tiny initramfs. The
guest mounts the host repository read-only plus one dedicated persistent proof
share, creates a Fleet Agent Instance, learns a skill, records its kernel
``boot_id``, reboots itself, then reopens the same Agent Instance on the second
kernel boot. The proof fails unless the second ``boot_id`` differs while the
Agent identity, profile, skill generation, and learned skill remain identical.

Katana itself is never rebooted. The guest receives no writable host filesystem
other than the explicitly supplied proof directory.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class Phase6RestartProofError(RuntimeError):
    """The isolated machine-restart proof could not be completed safely."""


def _run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Phase6RestartProofError(f"command failed: {argv[0]}") from error


def _required_executable(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise Phase6RestartProofError(f"required executable is unavailable: {name}")
    return Path(value).resolve()


def _module_path(name: str) -> Path:
    completed = _run(["modinfo", "-n", name])
    if completed.returncode != 0:
        raise Phase6RestartProofError(f"required kernel module is unavailable: {name}")
    path = Path(completed.stdout.decode().strip())
    if not path.is_file():
        raise Phase6RestartProofError(f"kernel module path is invalid: {name}")
    return path


def _busybox_libraries(busybox: Path) -> tuple[Path, ...]:
    completed = _run(["ldd", str(busybox)])
    if completed.returncode != 0:
        raise Phase6RestartProofError("busybox dependency inspection failed")
    libraries: set[Path] = set()
    for raw in completed.stdout.decode(errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        candidate = ""
        if "=>" in line:
            candidate = line.split("=>", 1)[1].strip().split(" ", 1)[0]
        elif line.startswith("/"):
            candidate = line.split(" ", 1)[0]
        if candidate.startswith("/"):
            path = Path(candidate)
            if path.is_file():
                libraries.add(path)
    if not libraries:
        raise Phase6RestartProofError("busybox shared libraries are unavailable")
    return tuple(sorted(libraries))


def _copy_absolute(source: Path, root: Path) -> Path:
    target = root / source.relative_to("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def _build_initramfs(
    *,
    build_root: Path,
    output: Path,
    script_path: Path,
    repository_root: Path,
    proof_mount: Path,
) -> None:
    root = build_root / "root"
    root.mkdir(parents=True)
    for directory in ("bin", "lib/modules/phase6", "proc", "sys", "dev", "host"):
        (root / directory).mkdir(parents=True, exist_ok=True)

    busybox = _required_executable("busybox")
    target_busybox = root / "bin" / "busybox"
    shutil.copy2(busybox, target_busybox)
    for library in _busybox_libraries(busybox):
        _copy_absolute(library, root)

    module_targets: dict[str, Path] = {}
    for name in ("netfs", "9pnet", "9p", "9pnet_virtio"):
        source = _module_path(name)
        suffix = "".join(source.suffixes)
        target = root / "lib" / "modules" / "phase6" / f"{name}{suffix}"
        shutil.copy2(source, target)
        module_targets[name] = target

    for name in (
        "cat",
        "chroot",
        "echo",
        "env",
        "insmod",
        "mkdir",
        "mount",
        "poweroff",
        "reboot",
        "sh",
        "sleep",
        "sync",
    ):
        os.symlink("busybox", root / "bin" / name)

    module_commands = "\n".join(
        f"/bin/insmod /{shlex.quote(str(path.relative_to(root)))}"
        for path in module_targets.values()
    )
    init = f"""#!/bin/busybox sh
set -eu
/bin/mount -t proc proc /proc
/bin/mount -t sysfs sysfs /sys
/bin/mount -t devtmpfs devtmpfs /dev
{module_commands}
/bin/mkdir -p /host
/bin/mount -t 9p -o trans=virtio,version=9p2000.L,msize=1048576 hostroot /host
/bin/mount --bind /dev /host/dev
/bin/mount --bind /proc /host/proc
/bin/mount --bind /sys /host/sys
/bin/mount -t 9p -o trans=virtio,version=9p2000.L,msize=1048576 proof /host{proof_mount}
if [ -f /host{proof_mount}/phase6-stage1.json ]; then
    stage=second
else
    stage=first
fi
if /bin/env -i \\
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
    PYTHONPATH={shlex.quote(str(repository_root))} \\
    /bin/chroot /host /usr/bin/python3 \\
    {shlex.quote(str(script_path))} \\
    --guest-stage "$stage" --proof-dir {shlex.quote(str(proof_mount))}; then
    rc=0
else
    rc=$?
fi
/bin/sync
if [ "$rc" -ne 0 ]; then
    echo "PHASE6_GUEST_STAGE_FAILED:$stage:$rc"
    /bin/poweroff -f
fi
if [ "$stage" = first ]; then
    echo PHASE6_GUEST_REBOOTING
    /bin/reboot -f
fi
echo PHASE6_GUEST_PROOF_COMPLETE
/bin/poweroff -f
"""
    init_path = root / "init"
    init_path.write_text(init, encoding="utf-8")
    init_path.chmod(0o755)

    names = ["."]
    for base, directories, files in os.walk(root):
        relative_base = Path(base).relative_to(root)
        for name in sorted(directories):
            names.append((relative_base / name).as_posix())
        for name in sorted(files):
            names.append((relative_base / name).as_posix())
    cpio = _required_executable("cpio")
    completed = subprocess.run(
        [str(cpio), "-o", "-H", "newc", "--quiet"],
        cwd=root,
        input=("\n".join(sorted(set(names))) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise Phase6RestartProofError("initramfs archive creation failed")
    with gzip.open(output, "wb", compresslevel=6) as handle:
        handle.write(completed.stdout)


def _guest_stage(*, proof_dir: Path, stage: str) -> int:
    from hermes_fleet.agency_materialization import bundle_agency_profile
    from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
    from hermes_fleet.agent_instance import AgentInstanceManager
    from hermes_fleet.profile_inventory import _profile_content_digest
    from hermes_fleet.recipes import ResolvedAgencyProfile

    boot_id = (
        Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    )
    model_config = proof_dir / "hermes-config.yaml"
    profiles_root = proof_dir / "profiles"
    stage1 = proof_dir / "phase6-stage1.json"
    success = proof_dir / "phase6-success.json"

    if stage == "first":
        proof_dir.mkdir(parents=True, exist_ok=True)
        model_config.write_text(
            "model:\n  default: persistent-model\n  provider: provider-test\n",
            encoding="utf-8",
        )
        model_config.chmod(0o600)
        source = proof_dir / "agency-source"
        source.mkdir(exist_ok=True)
        (source / "distribution.yaml").write_text(
            "name: phase6-machine-proof\nversion: 1.0.0\n",
            encoding="utf-8",
        )
        (source / "SOUL.md").write_text(
            "machine persistent brain\n",
            encoding="utf-8",
        )
        (source / "config.yaml").write_text(
            "agent:\n  baseline_marker: machine-proof\n",
            encoding="utf-8",
        )
        (source / "skills").mkdir(exist_ok=True)
        digest = _profile_content_digest(source, "phase6-machine-proof", "1.0.0")
        if digest is None:
            raise Phase6RestartProofError("could not compute Agency proof digest")
        package = AgencyProfilePackage(
            source=AgencySource("https://example.invalid/agency.git", "a" * 40),
            name="phase6-machine-proof",
            version="1.0.0",
            content_digest=digest,
            category="engineering",
            priority="standard",
            capabilities=("review",),
            distribution_path="profiles/phase6-machine-proof",
            local_path=source,
        )
        bundle = bundle_agency_profile(package)
        manager = AgentInstanceManager(
            profiles_root=profiles_root,
            model_config_path=model_config,
        )
        binding = manager.ensure(bundle)
        learned = manager.profile_path(binding) / "skills" / "learned" / "SKILL.md"
        learned.parent.mkdir()
        learned.write_text("learned before guest reboot\n", encoding="utf-8")
        with manager.mutation_guard(
            binding,
            component="skills",
            expected_generation=0,
        ):
            pass
        stage1.write_text(
            json.dumps(
                {
                    "boot_id": boot_id,
                    "instance_id": binding.instance_id,
                    "profile": binding.profile,
                    "repository": bundle.resolved.repository,
                    "revision": bundle.resolved.revision,
                    "name": bundle.resolved.name,
                    "version": bundle.resolved.version,
                    "content_digest": bundle.resolved.content_digest,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print("PHASE6_GUEST_FIRST_BOOT_OK", flush=True)
        return 0

    if stage != "second":
        raise Phase6RestartProofError("unknown guest proof stage")
    first = json.loads(stage1.read_text(encoding="utf-8"))
    if first["boot_id"] == boot_id:
        raise Phase6RestartProofError("guest boot ID did not change across reboot")
    agent = ResolvedAgencyProfile(
        repository=first["repository"],
        revision=first["revision"],
        name=first["name"],
        version=first["version"],
        content_digest=first["content_digest"],
    )
    manager = AgentInstanceManager(
        profiles_root=profiles_root,
        model_config_path=model_config,
    )
    binding = manager.open(agent)
    state = manager.read_state(binding)
    learned = manager.profile_path(binding) / "skills" / "learned" / "SKILL.md"
    observed = {
        "first_boot_id": first["boot_id"],
        "second_boot_id": boot_id,
        "instance_id": binding.instance_id,
        "profile": binding.profile,
        "skills_generation": state.skills_generation,
        "learned": learned.read_text(encoding="utf-8"),
    }
    if observed["instance_id"] != first["instance_id"]:
        raise Phase6RestartProofError("persistent Agent ID changed across reboot")
    if observed["profile"] != first["profile"]:
        raise Phase6RestartProofError("persistent profile changed across reboot")
    if observed["skills_generation"] != 1:
        raise Phase6RestartProofError("skill generation was lost across reboot")
    if observed["learned"] != "learned before guest reboot\n":
        raise Phase6RestartProofError("learned skill was lost across reboot")
    success.write_text(json.dumps(observed, sort_keys=True) + "\n", encoding="utf-8")
    print("PHASE6_GUEST_SECOND_BOOT_OK", flush=True)
    return 0


def _host_proof(*, proof_dir: Path) -> int:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise Phase6RestartProofError("machine-restart proof requires Linux x86_64")
    qemu = _required_executable("qemu-system-x86_64")
    _required_executable("modinfo")
    _required_executable("ldd")
    _required_executable("cpio")
    _required_executable("busybox")
    if not Path("/dev/kvm").exists():
        raise Phase6RestartProofError("KVM is unavailable")

    script_path = Path(__file__).resolve()
    repository_root = script_path.parents[1]
    kernel_release = platform.uname().release
    kernel = Path("/boot") / f"vmlinuz-{kernel_release}"
    if not kernel.is_file():
        raise Phase6RestartProofError(f"running kernel image is unavailable: {kernel}")

    proof_dir = proof_dir.resolve()
    if proof_dir.exists() and any(proof_dir.iterdir()):
        raise Phase6RestartProofError("proof directory must be empty")
    proof_dir.mkdir(parents=True, exist_ok=True)
    proof_mount = Path("/tmp") / f"hermes-phase6-proof-mount-{os.getpid()}"
    proof_mount.mkdir(mode=0o700)
    log = proof_dir / "qemu-serial.log"

    temporary_parent = Path("/dev/shm") if Path("/dev/shm").is_dir() else None
    try:
        with tempfile.TemporaryDirectory(
            prefix="hermes-phase6-initramfs-",
            dir=str(temporary_parent) if temporary_parent is not None else None,
        ) as temporary:
            build_root = Path(temporary)
            initramfs = build_root / "phase6-initramfs.cpio.gz"
            _build_initramfs(
                build_root=build_root,
                output=initramfs,
                script_path=script_path,
                repository_root=repository_root,
                proof_mount=proof_mount,
            )
            argv = [
                str(qemu),
                "-enable-kvm",
                "-m",
                "768",
                "-kernel",
                str(kernel),
                "-initrd",
                str(initramfs),
                "-append",
                "console=ttyS0 rdinit=/init panic=-1",
                "-display",
                "none",
                "-monitor",
                "none",
                "-serial",
                f"file:{log}",
                "-virtfs",
                "local,path=/,mount_tag=hostroot,security_model=none,readonly=on",
                "-virtfs",
                f"local,path={proof_dir},mount_tag=proof,security_model=mapped-xattr",
            ]
            try:
                completed = subprocess.run(argv, check=False, timeout=180)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise Phase6RestartProofError("QEMU proof did not complete") from error
            if completed.returncode != 0:
                raise Phase6RestartProofError(
                    f"QEMU proof failed with status {completed.returncode}"
                )
    finally:
        try:
            proof_mount.rmdir()
        except OSError:
            pass

    success_path = proof_dir / "phase6-success.json"
    if not success_path.is_file():
        raise Phase6RestartProofError("guest did not produce success evidence")
    observed = json.loads(success_path.read_text(encoding="utf-8"))
    if observed.get("first_boot_id") == observed.get("second_boot_id"):
        raise Phase6RestartProofError("guest boot ID did not change")
    if observed.get("skills_generation") != 1:
        raise Phase6RestartProofError("persistent skill generation was not preserved")
    if observed.get("learned") != "learned before guest reboot\n":
        raise Phase6RestartProofError("persistent learned skill was not preserved")
    log_text = log.read_text(encoding="utf-8", errors="replace")
    for marker in (
        "PHASE6_GUEST_FIRST_BOOT_OK",
        "PHASE6_GUEST_REBOOTING",
        "PHASE6_GUEST_SECOND_BOOT_OK",
        "PHASE6_GUEST_PROOF_COMPLETE",
    ):
        if marker not in log_text:
            raise Phase6RestartProofError(f"guest proof marker is missing: {marker}")
    print("PHASE6_MACHINE_RESTART_PROOF_OK")
    print(json.dumps(observed, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof-dir", type=Path)
    parser.add_argument("--guest-stage", choices=("first", "second"))
    args = parser.parse_args()
    if args.guest_stage is not None:
        if args.proof_dir is None:
            raise Phase6RestartProofError("guest proof directory is required")
        return _guest_stage(proof_dir=args.proof_dir, stage=args.guest_stage)
    proof_dir = args.proof_dir
    if proof_dir is None:
        proof_dir = Path.cwd() / "phase6-machine-restart-proof"
    return _host_proof(proof_dir=proof_dir)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Phase6RestartProofError as error:
        print(f"PHASE6_MACHINE_RESTART_PROOF_FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
