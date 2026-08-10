"""Clean-install acceptance for the standalone Hermes directory plugin."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed: {shlex.join(command)}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return result


def _build_git_artifact(destination: Path) -> None:
    """Build the supported repository-directory artifact from tracked paths."""
    tracked = _run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
    ).stdout.split("\0")
    for relative in filter(None, tracked):
        source = REPO_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    _run(["git", "init", "--quiet"], cwd=destination)
    _run(["git", "config", "user.name", "Fleet Tests"], cwd=destination)
    _run(
        ["git", "config", "user.email", "fleet-tests@example.invalid"], cwd=destination
    )
    _run(["git", "add", "--force", "--all"], cwd=destination)
    _run(["git", "commit", "--quiet", "-m", "test plugin artifact"], cwd=destination)


def test_git_artifact_installs_and_registers_outside_source_checkout(
    tmp_path: Path,
) -> None:
    hermes = shutil.which("hermes")
    assert hermes is not None, "Hermes CLI must be installed for plugin acceptance"

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _build_git_artifact(artifact)

    hermes_home = (tmp_path / "hermes-home").resolve()
    run_dir = tmp_path / "outside-source"
    run_dir.mkdir()
    env = {**os.environ, "HERMES_HOME": str(hermes_home), "PYTHONPATH": ""}
    env.pop("HERMES_PROFILE", None)

    _run(
        [hermes, "plugins", "install", artifact.as_uri(), "--enable"],
        cwd=run_dir,
        env=env,
    )

    installed = hermes_home / "plugins" / "hermes-fleet"
    assert installed.is_dir()
    assert installed.resolve() != REPO_ROOT.resolve()
    assert (installed / "plugin.yaml").is_file()
    assert (installed / "__init__.py").is_file()
    assert (installed / "dashboard" / "manifest.json").is_file()
    assert (installed / "dashboard" / "plugin_api.py").is_file()
    assert (installed / "dashboard" / "dist" / "index.js").is_file()
    assert (installed / "desktop" / "plugin.js").is_file()

    inspection = _run(
        [hermes, "plugins", "list", "--json", "--no-bundled"],
        cwd=run_dir,
        env=env,
    )
    plugins = json.loads(inspection.stdout)
    fleet = next(plugin for plugin in plugins if plugin["name"] == "hermes-fleet")
    assert fleet["status"] == "enabled"

    _run([hermes, "fleet", "--help"], cwd=run_dir, env=env)
    _run([hermes, "fleet", "init"], cwd=run_dir, env=env)
    assert (hermes_home / "fleet" / "nodes.yaml").is_file()
    assert (hermes_home / "fleet" / "cache.json").is_file()
