from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from hermes_fleet import node_bootstrap, setup


class FakeRunner:
    def __init__(
        self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]]
    ) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return self.responses.get(
            tuple(argv), subprocess.CompletedProcess(argv, 0, "", "")
        )


def _response(argv: list[str], code: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def _bundle(path: Path) -> Path:
    artifacts = {}
    for name, filename in {
        "keryxd": "keryxd",
        "keryx-node": "keryx-node",
        "fleet-managed-control": "fleet-managed-control",
        "keryx-wheel": "keryx.whl",
        "fleet-wheel": "fleet.whl",
        "hermes-source": "hermes-source.tar.gz",
    }.items():
        artifact = path / "artifacts" / filename
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if name == "hermes-source":
            source = path / "hermes-source-fixture/pyproject.toml"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("[build-system]\nrequires=[]\n", encoding="utf-8")
            with tarfile.open(artifact, mode="w:gz") as archive:
                archive.add(source, arcname="pyproject.toml")
        else:
            artifact.write_bytes(name.encode())
        artifacts[name] = {
            "path": f"artifacts/{filename}",
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    units = {}
    source_units = Path(node_bootstrap.__file__).resolve().parent.parent / "ops/systemd"
    for name in node_bootstrap.UNITS:
        unit = path / "units" / name
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_bytes((source_units / name).read_bytes())
        units[name] = {
            "path": f"units/{name}",
            "sha256": hashlib.sha256(unit.read_bytes()).hexdigest(),
        }
    document = {
        "schema": node_bootstrap.SCHEMA,
        "bundle_id": "",
        "role": "worker",
        "platform": ["linux-x86_64-debian"],
        "revisions": {
            "fleet": "6" * 40,
            "keryx": node_bootstrap.KERYX_REVISION,
            "nodescale": node_bootstrap.NODESCALE_REVISION,
            "hermes": node_bootstrap.HERMES_REVISION,
        },
        "artifacts": artifacts,
        "units": units,
        "service_scope": list(node_bootstrap.UNITS),
    }
    document["bundle_id"] = node_bootstrap._bundle_id(
        document["revisions"], document["artifacts"], document["units"]
    )
    (path / "bundle.json").write_text(json.dumps(document), encoding="utf-8")
    return path


def test_setup_parser_exposes_controller_check_and_worker_bundle_install() -> None:
    parser = argparse.ArgumentParser()
    setup.setup_parser(parser)
    assert vars(parser.parse_args(["setup", "--json"])) == {
        "setup_command": "setup",
        "bundle": None,
        "json_output": True,
    }
    assert vars(
        parser.parse_args(["node", "adopt", "nitro", "--bundle", "/tmp/b"])
    ) == {
        "setup_command": "node",
        "node_command": "adopt",
        "ssh_target": "nitro",
        "bundle": Path("/tmp/b"),
        "json_output": False,
    }


def test_controller_setup_is_read_only_without_bundle(tmp_path: Path) -> None:
    report = setup.SetupService(
        home=tmp_path,
        runner=FakeRunner({}),
        which=lambda name: (
            f"/bin/{name}" if name in {"systemctl", "tailscale", "hermes"} else None
        ),
    ).check_controller()
    assert report.mode == "check"
    assert not report.changed
    assert not (tmp_path / ".config/hermes-fleet").exists()


def test_worker_adopt_preflight_fails_before_ssh_when_provider_identity_missing(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(
        {
            ("tailscale", "status", "--json"): _response(
                ["tailscale", "status", "--json"],
                0,
                json.dumps({"Peer": {}}),
            )
        }
    )
    service = setup.SetupService(
        home=tmp_path, runner=runner, which=lambda _: "/bin/tool"
    )
    with pytest.raises(setup.SetupError, match="exact provider identity"):
        service.adopt_worker("nitro", bundle=tmp_path / "bundle")
    assert not any(call[0] == "ssh" for call in runner.calls)


def test_worker_adopt_requires_linger_before_transfer_or_mutation(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    status = {
        "Peer": {
            "peer-key": {
                "HostName": "worker-example",
                "ID": "provider-stable-id",
                "Online": True,
            }
        }
    }
    linger_check = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "worker-example",
        'sh -c \'test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes\'',
    ]
    runner = FakeRunner(
        {
            ("tailscale", "status", "--json"): _response(
                ["tailscale", "status", "--json"], 0, json.dumps(status)
            ),
            tuple(linger_check): _response(linger_check, 1),
        }
    )

    with pytest.raises(setup.PrerequisiteBlocked):
        setup.SetupService(
            home=tmp_path, runner=runner, which=lambda _: "/bin/tool"
        ).adopt_worker("worker-example", bundle=bundle)

    assert not any(call[0] == "scp" for call in runner.calls)
    assert not any("snapshot" in call for call in runner.calls)


def test_worker_adopt_shell_encodes_each_remote_argv_once(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    status = {
        "Peer": {
            "peer-key": {
                "HostName": "worker-example",
                "ID": "provider-stable-id",
                "Online": True,
            }
        }
    }
    runner = FakeRunner(
        {
            ("tailscale", "status", "--json"): _response(
                ["tailscale", "status", "--json"], 0, json.dumps(status)
            )
        }
    )

    setup.SetupService(
        home=tmp_path, runner=runner, which=lambda _: "/bin/tool"
    ).adopt_worker("worker-example", bundle=bundle)

    remote_calls = [call for call in runner.calls if call[0] == "ssh"]
    assert remote_calls
    assert all(len(call) == 5 for call in remote_calls)
    assert [
        "ssh",
        "-o",
        "BatchMode=yes",
        "worker-example",
        "python3 -c 'import sys,venv,ensurepip; assert sys.version_info >= (3, 11)'",
    ] in remote_calls
    assert [
        "ssh",
        "-o",
        "BatchMode=yes",
        "worker-example",
        'sh -c \'test "$(loginctl show-user "$(id -un)" -p Linger --value)" = yes\'',
    ] in remote_calls


def test_worker_adopt_runs_installer_owned_preflight_without_nodescale_authority(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    status = {
        "Peer": {
            "peer-key": {
                "HostName": "worker-test",
                "DNSName": "worker-test.example.test.",
                "TailscaleIPs": ["192.0.2.10"],
                "ID": "provider-stable-id",
                "Online": True,
            }
        }
    }
    runner = FakeRunner(
        {
            ("tailscale", "status", "--json"): _response(
                ["tailscale", "status", "--json"], 0, json.dumps(status)
            ),
            ("ssh", "-o", "BatchMode=yes", "worker-test", "true"): _response([], 0),
        }
    )
    service = setup.SetupService(
        home=tmp_path, runner=runner, which=lambda _: "/bin/tool"
    )
    report = service.adopt_worker("worker-test", bundle=bundle)
    joined = "\n".join(" ".join(call) for call in runner.calls)
    assert "hermes-fleet-node snapshot" in joined
    assert "hermes-fleet-node install" in joined
    assert "hermes-fleet-node doctor" not in joined
    assert "nodescale-owner" not in joined
    assert "nodescale-adopt" not in joined
    assert report.provider_node_id == "provider-stable-id"


def test_worker_adopt_bootstraps_bare_host_from_verified_wheel_before_snapshot(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    status = {
        "Peer": {
            "peer-key": {
                "HostName": "worker-example",
                "ID": "provider-stable-id",
                "Online": True,
            }
        }
    }
    runner = FakeRunner(
        {
            ("tailscale", "status", "--json"): _response(
                ["tailscale", "status", "--json"], 0, json.dumps(status)
            ),
            ("ssh", "-o", "BatchMode=yes", "worker-example", "true"): _response([], 0),
        }
    )
    service = setup.SetupService(
        home=tmp_path, runner=runner, which=lambda _: "/bin/tool"
    )

    service.adopt_worker("worker-example", bundle=bundle)

    calls = [" ".join(call) for call in runner.calls]
    assert calls.index(next(call for call in calls if "scp" in call)) < calls.index(
        next(call for call in calls if "snapshot" in call)
    )
    assert any("keryx.whl" in call and "pip install" in call for call in calls)
    assert any(
        "fleet.whl" in call and "pip install" in call and "--no-deps" in call
        for call in calls
    )
    assert any("snapshot" in call for call in calls)
    assert any("install" in call for call in calls)
    assert all(
        "hermes-fleet-node" not in call or "setup-venv/bin/hermes-fleet-node" in call
        for call in calls
    )
    assert not any("nodescale" in call.lower() for call in calls)


def test_worker_adopt_transfers_contents_into_stable_staging_directory(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    status = {
        "Peer": {
            "peer-key": {
                "HostName": "worker-example",
                "ID": "provider-stable-id",
                "Online": True,
            }
        }
    }
    runner = FakeRunner(
        {
            ("tailscale", "status", "--json"): _response(
                ["tailscale", "status", "--json"], 0, json.dumps(status)
            ),
            ("ssh", "-o", "BatchMode=yes", "worker-example", "true"): _response([], 0),
        }
    )

    setup.SetupService(home=tmp_path, runner=runner).adopt_worker(
        "worker-example", bundle=bundle
    )

    mkdir = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "worker-example",
        "mkdir -p .local/state/hermes-fleet",
    ]
    assert mkdir in runner.calls
    assert runner.calls.index(mkdir) < next(
        index for index, call in enumerate(runner.calls) if call[0] == "scp"
    )
    assert next(call for call in runner.calls if call[0] == "scp") == [
        "scp",
        "-r",
        f"{bundle}/.",
        "worker-example:.local/state/hermes-fleet/setup-bundle",
    ]


def test_setup_error_rendering_is_secret_free() -> None:
    error = setup.SetupError("failed token=private-value")
    assert "private-value" not in error.public_message


def test_worker_adopt_rejects_ssh_option_injection(tmp_path: Path) -> None:
    service = setup.SetupService(
        home=tmp_path, runner=FakeRunner({}), which=lambda _: "/bin/tool"
    )
    with pytest.raises(setup.SetupError, match="invalid"):
        service.adopt_worker("-oProxyCommand=bad", bundle=tmp_path)
