from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_fleet import setup


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
    assert not any(call and call[0] == "ssh" for call in runner.calls)


def test_worker_adopt_runs_remote_doctor_then_installer_without_nodescale_authority(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
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
    assert "hermes-fleet-node doctor" in joined
    assert "hermes-fleet-node install" in joined
    assert "nodescale-owner" not in joined
    assert "nodescale-adopt" not in joined
    assert report.provider_node_id == "provider-stable-id"


def test_setup_error_rendering_is_secret_free() -> None:
    error = setup.SetupError("failed token=private-value")
    assert "private-value" not in error.public_message


def test_worker_adopt_rejects_ssh_option_injection(tmp_path: Path) -> None:
    service = setup.SetupService(
        home=tmp_path, runner=FakeRunner({}), which=lambda _: "/bin/tool"
    )
    with pytest.raises(setup.SetupError, match="invalid"):
        service.adopt_worker("-oProxyCommand=bad", bundle=tmp_path)
