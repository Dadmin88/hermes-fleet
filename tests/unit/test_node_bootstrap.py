from __future__ import annotations

import hashlib
import json
import subprocess
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
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
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
        "keryx-wheel": "keryx.whl",
        "fleet-wheel": "fleet.whl",
    }.items():
        path = _write(
            bundle / "artifacts" / filename,
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
        "bundle_id": "worker-v1-test",
        "role": "worker",
        "platform": ["linux-x86_64-debian"],
        "revisions": {
            "fleet": bootstrap.FLEET_REVISION,
            "keryx": bootstrap.KERYX_REVISION,
            "nodescale": bootstrap.NODESCALE_REVISION,
            "hermes": bootstrap.HERMES_REVISION,
        },
        "artifacts": artifacts,
        "service_scope": list(bootstrap.UNITS),
    }
    _write(bundle / "bundle.json", json.dumps(document), 0o644)
    return document


def _worker_home(
    tmp_path: Path, bundle: Path, *, token: str = "kept-token", mode: int = 0o600
) -> Path:
    home = tmp_path / "home"
    manifest = bootstrap.load_bundle(bundle)
    for name in ("keryxd", "keryx-node"):
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
        extra = (
            "FLEET_NODE_NAME=worker\nFLEET_CONTROLLER_PEER_IDS=peer\n"
            if name == "fleet-node.env"
            else ""
        )
        _write(home / ".config/hermes-fleet" / name, common + extra, mode)
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
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert bootstrap.load_bundle(bundle)["revisions"]["fleet"] == "7" * 40


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
    check = next(item for item in report.checks if item.name == "keryx.keryxd_identity")
    assert not check.ok and check.status == "skew"
    assert report.primary_blocker == "keryx.keryxd_identity"


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
    for name in bootstrap.ENV_FILES:
        bootstrap._write_env(
            home / ".config/hermes-fleet" / name, {bootstrap.TOKEN_KEY: token or ""}
        )
    assert token == "original-token"
    assert {_read_token(home, name) for name in bootstrap.ENV_FILES} == {
        "original-token"
    }


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
    monkeypatch.setattr(
        bootstrap.Doctor,
        "_tailscale",
        lambda self: [bootstrap.Check("tailscale.registry_dns", True, "resolved")],
    )
    monkeypatch.setattr(
        bootstrap.Doctor,
        "_hermes",
        lambda self: [bootstrap.Check("hermes.installed", False, "unreachable")],
    )
    installer = bootstrap.Installer(home=home, bundle=bundle, runner=FakeRunner())
    with pytest.raises(RuntimeError, match="hermes.installed"):
        installer.converge()
    after = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    assert before == after


def test_restart_ordering_and_no_authority_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    _manifest(bundle)
    home = _worker_home(tmp_path, bundle)
    for name in ("keryxd", "keryx-node"):
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
