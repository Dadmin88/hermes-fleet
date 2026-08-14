from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from hermes_fleet import node_bootstrap as bootstrap


@pytest.fixture(autouse=True)
def _no_live_hermes_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.Doctor, "_http_json", lambda *_: {})


class FakeRunner:
    def __init__(self, *, auth: str = "UNAUTHENTICATED,INVALID_ARGUMENT") -> None:
        self.auth = auth
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], *, env=None) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        joined = " ".join(argv)
        if "inspect,keryx.config" in joined:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "grpc,os" in joined:
            return subprocess.CompletedProcess(argv, 0, self.auth + "\n", "")
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        if argv == ["systemctl", "is-active", "tailscaled.service"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        if argv == ["systemctl", "is-active", "fleet-node.service"]:
            return subprocess.CompletedProcess(argv, 3, "inactive\n", "")
        if argv[:3] == ["tailscale", "status", "--json"]:
            return subprocess.CompletedProcess(
                argv, 0, '{"BackendState":"Running"}', ""
            )
        if argv[:3] == ["tailscale", "dns", "query"]:
            return subprocess.CompletedProcess(argv, 1, "RCodeNameError", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _write(path: Path, content: bytes | str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    path.chmod(mode)
    return path


def _manifest(bundle: Path, *, corrupt: str | None = None) -> dict:
    artifacts = {}
    for name, filename in {
        "keryxd": "keryxd",
        "keryx-node": "keryx-node",
        "fleet-managed-control": "fleet-managed-control",
        "keryx-wheel": "keryx.whl",
        "fleet-wheel": "fleet.whl",
        "hermes-source": "hermes-source.tar.gz",
    }.items():
        path = bundle / "artifacts" / filename
        if name == "hermes-source":
            source = bundle / "hermes-source-fixture/pyproject.toml"
            _write(source, b"[build-system]\nrequires=[]\n", 0o644)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(path, mode="w:gz") as archive:
                archive.add(source, arcname="pyproject.toml")
        else:
            path = _write(
                path,
                name.encode(),
                0o755 if "wheel" not in name else 0o644,
            )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifacts[name] = {
            "path": f"artifacts/{filename}",
            "sha256": "0" * 64 if corrupt == name else digest,
        }
    document = {
        "schema": bootstrap.SCHEMA,
        "bundle_id": "",
        "role": "worker",
        "platform": ["linux-x86_64-debian"],
        "revisions": {
            "fleet": "6" * 40,
            "keryx": bootstrap.KERYX_REVISION,
            "nodescale": bootstrap.NODESCALE_REVISION,
            "hermes": bootstrap.HERMES_REVISION,
        },
        "artifacts": artifacts,
        "units": {},
        "service_scope": list(bootstrap.UNITS),
    }
    source_units = Path(bootstrap.__file__).resolve().parent.parent / "ops/systemd"
    for name in bootstrap.UNITS:
        path = _write(
            bundle / "units" / name,
            (source_units / name).read_bytes(),
            0o644,
        )
        document["units"][name] = {
            "path": f"units/{name}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    document["bundle_id"] = bootstrap._bundle_id(
        document["revisions"], document["artifacts"], document["units"]
    )
    _write(bundle / "bundle.json", json.dumps(document), 0o644)
    return document


def _worker_home(
    tmp_path: Path, bundle: Path, *, token: str = "kept-token", mode: int = 0o600
) -> Path:
    home = tmp_path / "home"
    manifest = bootstrap.load_bundle(bundle)
    for name in ("keryxd", "keryx-node", "fleet-managed-control"):
        _write(
            home / ".local/share/hermes-fleet/bin" / name,
            (bundle / manifest["artifacts"][name]["path"]).read_bytes(),
            0o755,
        )
    _write(home / ".local/share/hermes-fleet/venv/bin/python", b"python", 0o755)
    common = (
        f"{bootstrap.TOKEN_KEY}={token}\n"
        "HERMES_KERYX_REGISTRY_ADDR=registry.example.test:50053\n"
    )
    for name in bootstrap.ENV_FILES:
        if name == "hermes-api.env":
            content = (
                "API_SERVER_KEY=stable-api-key\n"
                "API_SERVER_ENABLED=true\nAPI_SERVER_HOST=127.0.0.1\n"
                "API_SERVER_PORT=8642\n"
            )
        elif name == "fleet-managed-projection.env":
            content = (
                "FLEET_MANAGED_PROJECTION_SOCKET="
                f"/run/user/{os.getuid()}/hermes-fleet/managed-control.sock\n"
                "FLEET_MANAGED_PROJECTION_DATABASE="
                f"{home}/.local/state/hermes-fleet/managed-control.sqlite3\n"
                f"FLEET_MANAGED_PROJECTION_ALLOWED_UID={os.getuid()}\n"
            )
        else:
            extra = (
                "FLEET_NODE_NAME=worker\nFLEET_CONTROLLER_PEER_IDS=peer\n"
                "API_SERVER_KEY=stable-api-key\n"
                f"FLEET_OBSERVATION_SOCKET=/run/user/{os.getuid()}"
                "/hermes-fleet/managed-control.sock\n"
                if name == "fleet-node.env"
                else ""
            )
            content = common + extra
        _write(home / ".config/hermes-fleet" / name, content, mode)
    _write(home / ".hermes/profiles/admin/fleet/nodes.yaml", "nodes: []\n", 0o600)
    source_units = Path(bootstrap.__file__).resolve().parent.parent / "ops/systemd"
    for name in bootstrap.UNITS:
        _write(
            home / ".config/systemd/user" / name,
            (source_units / name).read_bytes(),
            0o644,
        )
    return home


def test_load_bundle_rejects_binary_hash_skew(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle, corrupt="keryxd")
    with pytest.raises(ValueError, match="SHA-256"):
        bootstrap.load_bundle(bundle)


def test_load_bundle_accepts_exact_canonical_fleet_revision(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    manifest_path = bundle / "bundle.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["revisions"]["fleet"] = "7" * 40
    manifest["bundle_id"] = bootstrap._bundle_id(
        manifest["revisions"], manifest["artifacts"], manifest["units"]
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert bootstrap.load_bundle(bundle)["revisions"]["fleet"] == "7" * 40


def test_load_bundle_rejects_identity_not_derived_from_verified_contents(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    manifest_path = bundle / "bundle.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_id"] = "worker-v1-forged"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="identity"):
        bootstrap.load_bundle(bundle)


def test_build_bundle_materializes_the_exact_verified_service_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet_source = tmp_path / "fleet"
    keryx_source = tmp_path / "keryx"
    hermes_source = tmp_path / "hermes"
    source_units = Path(bootstrap.__file__).resolve().parent.parent / "ops/systemd"
    for name in bootstrap.UNITS:
        _write(fleet_source / "ops/systemd" / name, (source_units / name).read_bytes())
    fleet_dist = tmp_path / "output/fleet-dist"
    keryx_dist = tmp_path / "output/keryx-dist"

    cargo_target = tmp_path / "output/cargo-target/release"

    def fake_run(
        argv, *, cwd=None, env=None, capture_output=False, text=False, timeout=None
    ):
        if argv[:2] == ["git", "-C"]:
            archive = tmp_path / (Path(argv[2]).name + "-source.tar")
            source_root = tmp_path / (Path(argv[2]).name + "-archive")
            _write(
                source_root / "pyproject.toml", b"[build-system]\nrequires=[]\n", 0o644
            )
            if Path(argv[2]) == fleet_source:
                for name in bootstrap.UNITS:
                    _write(
                        source_root / "ops/systemd" / name,
                        (source_units / name).read_bytes(),
                    )
            if Path(argv[2]) == keryx_source:
                _write(
                    source_root / "sdk/python/pyproject.toml",
                    b"[build-system]\nrequires=[]\n",
                )
            with tarfile.open(archive, mode="w") as handle:
                for source in source_root.rglob("*"):
                    if source.is_file():
                        handle.add(source, arcname=source.relative_to(source_root))
            return subprocess.CompletedProcess(argv, 0, archive.read_bytes(), b"")
        if "cargo" in argv[0] and cwd == tmp_path / "output/frozen-sources/keryx":
            _write(cargo_target / "keryxd", b"daemon", 0o755)
            _write(cargo_target / "keryx-node", b"node", 0o755)
        elif "cargo" in argv[0] and cwd == tmp_path / "output/frozen-sources/fleet":
            _write(
                tmp_path / "output/fleet-cargo-target/release/fleet-managed-control",
                b"control",
                0o755,
            )
        elif cwd == tmp_path / "output/frozen-sources/keryx":
            _write(keryx_dist / "keryx.whl", b"keryx")
        else:
            _write(fleet_dist / "fleet.whl", b"fleet")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(
        bootstrap,
        "_git_head",
        lambda path, runner: (
            "7" * 40
            if path == fleet_source
            else bootstrap.HERMES_REVISION
            if path == hermes_source
            else bootstrap.KERYX_REVISION
        ),
    )
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    bundle = bootstrap.build_bundle(
        fleet_source=fleet_source,
        keryx_source=keryx_source,
        hermes_source=hermes_source,
        output=tmp_path / "output",
        runner=FakeRunner(),
    )
    manifest = bootstrap.load_bundle(bundle)

    assert set(manifest["units"]) == set(bootstrap.UNITS)
    for name, item in manifest["units"].items():
        assert (bundle / item["path"]).read_bytes() == (
            source_units / name
        ).read_bytes()
    assert not (bundle / "cargo-target").exists()
    assert not (bundle / "fleet-cargo-target").exists()
    assert not (bundle / "fleet-dist").exists()
    assert not (bundle / "keryx-dist").exists()


def test_freeze_git_source_materializes_only_the_requested_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    frozen = tmp_path / "frozen"

    def run(argv, **_kwargs):
        assert argv[-2:] == ["archive", "a" * 40]
        payload = tmp_path / "source.tar"
        file = tmp_path / "tracked.txt"
        file.write_text("frozen bytes")
        with tarfile.open(payload, mode="w") as archive:
            archive.add(file, arcname="tracked.txt")
        return subprocess.CompletedProcess(argv, 0, payload.read_bytes(), b"")

    monkeypatch.setattr(bootstrap.subprocess, "run", run)

    bootstrap._freeze_git_source(source, "a" * 40, frozen, FakeRunner())

    assert (frozen / "tracked.txt").read_text() == "frozen bytes"
    assert list(frozen.iterdir()) == [frozen / "tracked.txt"]


def test_build_bundle_rejects_dirty_source_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet_source = tmp_path / "fleet"
    keryx_source = tmp_path / "keryx"
    hermes_source = tmp_path / "hermes"
    runner = FakeRunner()

    def run(argv, *, env=None):
        if argv[-2:] == ["--porcelain", "--untracked-files=all"]:
            return subprocess.CompletedProcess(argv, 0, " M changed.py\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    runner.run = run  # type: ignore[method-assign]
    build_called = False

    def fake_build(*args, **kwargs):
        nonlocal build_called
        build_called = True
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_build)

    with pytest.raises(RuntimeError, match="exact clean Git checkout"):
        bootstrap.build_bundle(
            fleet_source=fleet_source,
            keryx_source=keryx_source,
            hermes_source=hermes_source,
            output=tmp_path / "output",
            runner=runner,
        )
    assert build_called is False


def test_doctor_detects_binary_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    _write(home / ".local/share/hermes-fleet/bin/keryxd", b"stale", 0o755)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(bootstrap.socket, "getaddrinfo", lambda *_: [(None,)])
    report = bootstrap.Doctor(home=home, bundle=bundle, runner=FakeRunner()).run()
    check = next(
        item for item in report.checks if item.name == "runtime.keryxd_identity"
    )
    assert not check.ok and check.status == "skew"
    assert report.primary_blocker == "runtime.keryxd_identity"


def test_doctor_detects_missing_daemon_auth_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    runner = FakeRunner()
    original = runner.run

    def run(argv, *, env=None):
        if "inspect,keryx.config" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 1, "", "unsupported")
        return original(argv, env=env)

    runner.run = run  # type: ignore[method-assign]
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(bootstrap.socket, "getaddrinfo", lambda *_: [(None,)])
    report = bootstrap.Doctor(home=home, bundle=bundle, runner=runner).run()
    assert not next(
        item for item in report.checks if item.name == "keryx.sdk_daemon_token"
    ).ok


def test_doctor_detects_missing_credential_and_bad_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle, mode=0o644)
    path = home / ".config/hermes-fleet/keryxd.env"
    path.write_text(
        "HERMES_KERYX_REGISTRY_ADDR=registry.example.test:50053\n", encoding="utf-8"
    )
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(bootstrap.socket, "getaddrinfo", lambda *_: [(None,)])
    report = bootstrap.Doctor(home=home, bundle=bundle, runner=FakeRunner()).run()
    checks = {item.name: item for item in report.checks}
    assert not checks["keryx.daemon_credential_present"].ok
    assert not checks["keryx.daemon_credential_permissions"].ok


def test_doctor_separates_magicdns_nxdomain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(
        bootstrap.socket, "getaddrinfo", lambda *_: (_ for _ in ()).throw(OSError())
    )
    report = bootstrap.Doctor(home=home, bundle=bundle, runner=FakeRunner()).run()
    dns = next(item for item in report.checks if item.name == "tailscale.registry_dns")
    assert not dns.ok and dns.status == "nxdomain"


def test_registry_hostname_uses_canonical_endpoint_name(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(
        home / ".config/hermes-fleet/keryxd.env",
        "HERMES_KERYX_REGISTRY_ENDPOINT=https://registry.example.test:50053\n",
    )
    assert bootstrap.Doctor(home=home)._registry_hostname() == "registry.example.test"


def test_secret_redaction() -> None:
    rendered = bootstrap._safe_detail(
        "token=super-secret bearer=another-secret ordinary"
    )
    assert "super-secret" not in rendered
    assert "another-secret" not in rendered
    assert rendered.count("<redacted>") == 2


def test_credential_reuse_and_consistent_wiring(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle, token="original-token")
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())
    installer._preflight()
    token = installer._existing_token()
    for name in ("keryxd.env", "keryx-node.env", "fleet-node.env"):
        bootstrap._write_env(
            home / ".config/hermes-fleet" / name, {bootstrap.TOKEN_KEY: token or ""}
        )
    assert token == "original-token"
    assert {
        _read_token(home, name)
        for name in ("keryxd.env", "keryx-node.env", "fleet-node.env")
    } == {"original-token"}
    assert _read_token(home, "hermes-api.env") is None
    assert _read_token(home, "fleet-managed-projection.env") is None
    assert (
        bootstrap._read_env(
            home / ".config/hermes-fleet/fleet-managed-projection.env"
        ).get(bootstrap.API_KEY)
        is None
    )
    assert (
        bootstrap._read_env(home / ".config/hermes-fleet/keryxd.env").get(
            bootstrap.API_KEY
        )
        is None
    )


def _read_token(home: Path, name: str) -> str | None:
    return bootstrap._read_env(home / ".config/hermes-fleet" / name).get(
        bootstrap.TOKEN_KEY
    )


def test_failed_preflight_causes_zero_mutation(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    _write(
        home / ".config/hermes-fleet/keryx-node.env",
        f"{bootstrap.TOKEN_KEY}=different\n",
    )
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())
    with pytest.raises(RuntimeError, match="inconsistent"):
        installer.converge()
    after = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    assert before == after


def test_failed_runtime_preflight_causes_zero_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    runner = FakeRunner()
    original = runner.run

    def run(argv, *, env=None):
        if argv == ["systemctl", "is-active", "fleet-node.service"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        return original(argv, env=env)

    runner.run = run  # type: ignore[method-assign]
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=runner)
    with pytest.raises(RuntimeError, match="fleet.system_service_absent"):
        installer.converge()
    after = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    assert before == after


def test_runtime_preflight_does_not_require_post_install_fleet_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = tmp_path / "home"
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())

    installer._runtime_preflight()


def test_post_mutation_failure_restores_owned_files_and_removes_new_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    old_binary = home / ".local/share/hermes-fleet/bin/keryxd"
    old_binary.write_bytes(b"old-daemon")
    old_venv_file = home / ".local/share/hermes-fleet/venv/old-runtime.txt"
    _write(old_venv_file, b"old-runtime")
    old_receipt = home / ".local/state/hermes-fleet/install-receipt.json"
    _write(old_receipt, b'{"old":true}\n', 0o600)
    new_unit = home / ".config/systemd/user/fleet-node.service"
    new_unit.unlink()
    before = old_binary.read_bytes()
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(bootstrap.Installer, "_runtime_preflight", lambda self: None)
    monkeypatch.setattr(
        bootstrap.Doctor,
        "_auth_probe",
        lambda self, python, token: "transport_failure",
    )
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())

    with pytest.raises(RuntimeError, match="authentication proof"):
        installer.converge()

    assert old_binary.read_bytes() == before
    assert old_venv_file.read_bytes() == b"old-runtime"
    assert old_receipt.read_bytes() == b'{"old":true}\n'
    assert not new_unit.exists()


def test_post_mutation_failure_removes_new_venv_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    venv = home / ".local/share/hermes-fleet/venv"
    shutil.rmtree(venv, ignore_errors=True)
    profile = home / ".hermes/profiles/fleet-worker"
    receipt = home / ".local/state/hermes-fleet/install-receipt.json"
    receipt.unlink(missing_ok=True)
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(bootstrap.Installer, "_runtime_preflight", lambda self: None)

    def mutate_then_fail(self, manifest):
        _write(venv / "bin/python", b"new-runtime", 0o755)
        _write(profile / "config.yaml", b"model: isolated\n", 0o600)
        self._write_receipt(manifest)
        raise RuntimeError("post-install doctor failure")

    monkeypatch.setattr(bootstrap.Installer, "_install_artifacts", mutate_then_fail)
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())

    with pytest.raises(RuntimeError, match="post-install doctor failure"):
        installer.converge()

    assert not venv.exists()
    assert not profile.exists()
    assert not receipt.exists()


def test_restore_rejects_inventory_path_outside_owned_allowlist(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installer = bootstrap.Installer(home=home, bundle=tmp_path / "bundle")
    root = installer._snapshot_root()
    installer._snapshot_touched(root)
    inventory_path = root / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("preserve", encoding="utf-8")
    inventory[str(unrelated)] = "absent"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid paths"):
        installer._restore_snapshot(root)

    assert unrelated.read_text(encoding="utf-8") == "preserve"


def test_restore_reinstates_exact_owned_service_enable_and_active_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runner = FakeRunner()

    def run(argv, *, env=None):
        runner.calls.append(argv)
        name = argv[-1]
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return subprocess.CompletedProcess(
                argv, 0 if name == bootstrap.UNITS[0] else 1, "", ""
            )
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(
                argv, 0 if name == bootstrap.UNITS[1] else 1, "", ""
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    runner.run = run  # type: ignore[method-assign]
    installer = bootstrap.Installer(
        home=home, bundle=tmp_path / "bundle", runner=runner
    )
    for name in bootstrap.UNITS:
        _write(installer.units / name, "[Service]\n", 0o644)
    root = installer._snapshot_root()
    installer._snapshot_touched(root)
    runner.calls.clear()
    installer._restore_snapshot(root)

    actions = [
        call[-2:] for call in runner.calls if call[:2] == ["systemctl", "--user"]
    ]
    assert ["enable", bootstrap.UNITS[0]] in actions
    assert ["start", bootstrap.UNITS[1]] in actions
    for name in bootstrap.UNITS[1:]:
        assert ["disable", name] in actions
    for name in (bootstrap.UNITS[0], *bootstrap.UNITS[2:]):
        assert ["stop", name] in actions


def test_restore_absent_units_does_not_require_disable_to_succeed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runner = FakeRunner()
    original = runner.run

    def run(argv, *, env=None):
        if tuple(argv[:3]) in {
            ("systemctl", "--user", "is-enabled"),
            ("systemctl", "--user", "is-active"),
        }:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return original(argv, env=env)

    runner.run = run  # type: ignore[method-assign]
    installer = bootstrap.Installer(
        home=home, bundle=tmp_path / "bundle", runner=runner
    )
    root = installer._snapshot_root()
    installer._snapshot_touched(root)
    for name in bootstrap.UNITS:
        _write(installer.units / name, "[Service]\n", 0o644)
    runner.calls.clear()

    installer._restore_snapshot(root)

    assert all(not (installer.units / name).exists() for name in bootstrap.UNITS)
    assert not [call for call in runner.calls if "disable" in call]
    assert not [call for call in runner.calls if "stop" in call]


def test_installer_default_runner_allows_bounded_package_install(
    tmp_path: Path,
) -> None:
    installer = bootstrap.Installer(home=tmp_path, bundle=tmp_path / "bundle")

    assert isinstance(installer.runner, bootstrap.SubprocessRunner)
    assert installer.runner.timeout_seconds == 300


def test_installer_retries_transport_until_daemon_auth_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = bootstrap.Installer(
        home=tmp_path, bundle=tmp_path / "bundle", runner=FakeRunner()
    )
    results = iter(["transport_failure", "transport_failure", "enforced"])
    monkeypatch.setattr(
        bootstrap.Doctor,
        "_auth_probe",
        lambda self, python, token: next(results),
    )
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)

    assert installer._wait_for_auth("test-token") == "enforced"


def test_installer_materializes_verified_units_on_bare_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    for name in bootstrap.UNITS:
        (home / ".config/systemd/user" / name).unlink()
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())
    installer._preflight()
    manifest = bootstrap.load_bundle(bundle)
    installer._install_artifacts(manifest)

    for name in bootstrap.UNITS:
        installed = home / ".config/systemd/user" / name
        expected = bundle / manifest["units"][name]["path"]
        assert installed.read_bytes() == expected.read_bytes()


def test_runtime_install_creates_isolated_worker_profile_without_cloning_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    manifest = _manifest(bundle)
    home = tmp_path / "home"
    runner = FakeRunner()
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=runner)
    installer.install.mkdir(parents=True)
    monkeypatch.setattr(
        bootstrap.Installer, "_run", lambda self, argv: runner.calls.append(argv)
    )

    installer._install_artifacts(manifest)

    assert [
        str(installer.install / "venv/bin/hermes"),
        "profile",
        "create",
        "fleet-worker",
        "--no-skills",
        "--no-alias",
    ] in runner.calls
    flattened = " ".join(" ".join(call) for call in runner.calls)
    assert "--clone" not in flattened and "--clone-from" not in flattened


def test_installer_converges_owned_execution_slot_and_worker_gateway_config(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = tmp_path / "home"
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())

    installer._converge_execution_profile()
    first_changes = list(installer.changes)
    installer._converge_execution_profile()

    worker_config = home / ".hermes/profiles/fleet-worker/config.yaml"
    slot = home / ".hermes/profiles/fleet-execution"
    assert worker_config.read_text() == (
        "gateway:\n"
        "  multiplex_profile_allowlist:\n"
        "    - fleet-execution\n"
        "  multiplex_profiles: true\n"
    )
    assert (slot / ".fleet-execution-slot").read_text() == (
        "hermes-fleet.execution-slot.v1\n"
    )
    assert set(installer._owned_paths()).issuperset(
        {
            home / ".hermes/profiles/fleet-worker",
            slot,
        }
    )
    assert first_changes == installer.changes


def test_fleet_node_unit_can_write_only_execution_profile_and_fleet_state() -> None:
    unit = (
        Path(bootstrap.__file__).resolve().parent.parent
        / "ops/systemd/fleet-node.service"
    ).read_text()

    assert (
        "ReadWritePaths=%h/.local/state/hermes-fleet "
        "%h/.hermes/profiles/fleet-execution"
    ) in unit
    assert "ReadWritePaths=%h/.hermes/profiles\n" not in unit


def test_restart_ordering_and_no_authority_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    for name in ("keryxd", "keryx-node", "fleet-managed-control"):
        _write(home / ".local/share/hermes-fleet/bin" / name, b"stale", 0o755)
    source_units = Path(bootstrap.__file__).resolve().parent.parent / "ops/systemd"
    for name in bootstrap.UNITS:
        assert (source_units / name).is_file()
    runner = FakeRunner()
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(bootstrap.socket, "getaddrinfo", lambda *_: [(None,)])
    monkeypatch.setattr(
        bootstrap.Installer,
        "_install_artifacts",
        lambda self, manifest: self.changes.append("artifact:test"),
    )
    monkeypatch.setattr(
        bootstrap.Installer, "_write_receipt", lambda self, manifest: None
    )
    monkeypatch.setattr(
        bootstrap.Doctor,
        "run",
        lambda self: bootstrap.DoctorReport(
            "hermes-fleet-worker-doctor.v1", True, None, ()
        ),
    )
    report = bootstrap.Installer(home=home, bundle=bundle, runner=runner).converge()
    restart_calls = [
        call[-1]
        for call in runner.calls
        if call[:3] == ["systemctl", "--user", "restart"]
    ]
    assert restart_calls == [
        "keryxd.service",
        "keryx-node.service",
        "hermes-fleet-api.service",
        "fleet-managed-projection.service",
        "fleet-node.service",
    ]
    flattened = " ".join(" ".join(call) for call in runner.calls).lower()
    assert all(
        word not in flattened
        for word in ("nodescale", "challenge", "bind", "fleet_run", "hermes run")
    )
    assert report.ready


def test_install_receipt_contains_no_secrets_and_is_stable(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    manifest = _manifest(bundle)
    home = tmp_path / "home"
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())
    installer.state.mkdir(parents=True)
    installer._write_receipt(manifest)
    receipt = installer.state / "install-receipt.json"
    first = receipt.read_bytes()
    installer._write_receipt(manifest)
    assert receipt.read_bytes() == first
    text = first.decode().lower()
    assert "token" not in text and "credential" not in text and "secret" not in text


def test_second_correct_convergence_does_not_restart_or_regenerate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    manifest = _manifest(bundle)
    home = _worker_home(tmp_path, bundle, token="stable-token")
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())
    installer.state.mkdir(parents=True, exist_ok=True)
    installer._write_receipt(manifest)
    installer._converge_execution_profile()
    installer.changes.clear()
    for name in bootstrap.UNITS:
        source = Path(bootstrap.__file__).resolve().parent.parent / "ops/systemd" / name
        _write(home / ".config/systemd/user" / name, source.read_bytes(), 0o644)
    runner = FakeRunner()
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=runner)
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/tool")
    monkeypatch.setattr(bootstrap.socket, "getaddrinfo", lambda *_: [(None,)])
    monkeypatch.setattr(
        bootstrap.Installer, "_install_artifacts", lambda self, manifest: None
    )
    report = installer.converge()
    assert report.ready
    assert _read_token(home, "keryxd.env") == "stable-token"
    assert not [call for call in runner.calls if "restart" in call]
    assert installer.changes == []


def test_json_output_is_machine_readable_and_secret_free() -> None:
    report = bootstrap.DoctorReport(
        schema="hermes-fleet-worker-doctor.v1",
        ready=False,
        primary_blocker="keryx.test",
        checks=(bootstrap.Check("keryx.test", False, "missing", "token=hidden"),),
    )
    output = bootstrap.render_report(report, json_output=True)
    parsed = json.loads(output)
    assert parsed["primary_blocker"] == "keryx.test"
    assert "hidden" not in bootstrap.render_report(report, json_output=False)


def test_snapshot_command_is_available() -> None:
    args = bootstrap._parser().parse_args(["snapshot"])
    assert args.command == "snapshot"
