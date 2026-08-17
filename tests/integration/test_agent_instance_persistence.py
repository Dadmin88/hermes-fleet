from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from hermes_fleet.agency_materialization import bundle_agency_profile
from hermes_fleet.agency_snapshot import AgencyProfilePackage, AgencySource
from hermes_fleet.agent_instance import AgentInstanceManager
from hermes_fleet.profile_inventory import _profile_content_digest


def _bundle(tmp_path: Path):
    profile = tmp_path / "agency-source"
    profile.mkdir()
    (profile / "distribution.yaml").write_text(
        "name: persistent-example\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (profile / "SOUL.md").write_text("persistent brain\n", encoding="utf-8")
    (profile / "skills").mkdir()
    digest = _profile_content_digest(profile, "persistent-example", "1.0.0")
    assert digest is not None
    package = AgencyProfilePackage(
        source=AgencySource("https://example.invalid/agency.git", "a" * 40),
        name="persistent-example",
        version="1.0.0",
        content_digest=digest,
        category="engineering",
        priority="standard",
        capabilities=("review",),
        distribution_path="profiles/persistent-example",
        local_path=profile,
    )
    return bundle_agency_profile(package)


def test_agent_instance_survives_fresh_python_process(tmp_path: Path) -> None:
    model_config = tmp_path / "hermes-config.yaml"
    model_config.write_text(
        "model:\n  default: persistent-model\n  provider: provider-test\n",
        encoding="utf-8",
    )
    model_config.chmod(0o600)
    profiles_root = tmp_path / "profiles"
    manager = AgentInstanceManager(
        profiles_root=profiles_root,
        model_config_path=model_config,
    )
    bundle = _bundle(tmp_path)
    binding = manager.ensure(bundle)
    profile = manager.profile_path(binding)

    learned = profile / "skills" / "learned" / "SKILL.md"
    learned.parent.mkdir()
    learned.write_text("learned before process restart\n", encoding="utf-8")
    with manager.mutation_guard(
        binding,
        component="skills",
        expected_generation=0,
    ):
        pass

    script = r"""
import json
import sys
from pathlib import Path
from hermes_fleet.agent_instance import AgentInstanceManager
from hermes_fleet.recipes import ResolvedAgencyProfile

profiles_root = Path(sys.argv[1])
model_config = Path(sys.argv[2])
agent = ResolvedAgencyProfile(
    repository=sys.argv[3],
    revision=sys.argv[4],
    name=sys.argv[5],
    version=sys.argv[6],
    content_digest=sys.argv[7],
)
manager = AgentInstanceManager(
    profiles_root=profiles_root,
    model_config_path=model_config,
)
binding = manager.open(agent)
state = manager.read_state(binding)
learned = manager.profile_path(binding) / "skills" / "learned" / "SKILL.md"
print(json.dumps({
    "instance_id": binding.instance_id,
    "profile": binding.profile,
    "skills_generation": state.skills_generation,
    "learned": learned.read_text(encoding="utf-8"),
}, sort_keys=True))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(profiles_root),
            str(model_config),
            bundle.resolved.repository,
            bundle.resolved.revision,
            bundle.resolved.name,
            bundle.resolved.version,
            bundle.resolved.content_digest,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed == {
        "instance_id": binding.instance_id,
        "profile": binding.profile,
        "skills_generation": 1,
        "learned": "learned before process restart\n",
    }


def test_agent_instance_mutation_guard_serializes_across_processes(
    tmp_path: Path,
) -> None:
    model_config = tmp_path / "hermes-config.yaml"
    model_config.write_text(
        "model:\n  default: persistent-model\n  provider: provider-test\n",
        encoding="utf-8",
    )
    model_config.chmod(0o600)
    profiles_root = tmp_path / "profiles"
    manager = AgentInstanceManager(
        profiles_root=profiles_root,
        model_config_path=model_config,
    )
    bundle = _bundle(tmp_path)
    binding = manager.ensure(bundle)
    entered = tmp_path / "first-entered"
    release = tmp_path / "release-first"

    script = r"""
import sys
import time
from pathlib import Path
from hermes_fleet.agent_instance import AgentInstanceConflict, AgentInstanceManager
from hermes_fleet.recipes import ResolvedAgencyProfile

profiles_root = Path(sys.argv[1])
model_config = Path(sys.argv[2])
agent = ResolvedAgencyProfile(
    repository=sys.argv[3],
    revision=sys.argv[4],
    name=sys.argv[5],
    version=sys.argv[6],
    content_digest=sys.argv[7],
)
role = sys.argv[8]
entered = Path(sys.argv[9])
release = Path(sys.argv[10])
manager = AgentInstanceManager(
    profiles_root=profiles_root,
    model_config_path=model_config,
)
binding = manager.open(agent)
try:
    with manager.mutation_guard(
        binding,
        component="skills",
        expected_generation=0,
    ):
        if role == "first":
            entered.write_text("entered\n", encoding="utf-8")
            deadline = time.monotonic() + 10
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("release signal timed out")
                time.sleep(0.01)
        print("SUCCESS", flush=True)
except AgentInstanceConflict:
    print("CONFLICT", flush=True)
"""
    base = [
        sys.executable,
        "-c",
        script,
        str(profiles_root),
        str(model_config),
        bundle.resolved.repository,
        bundle.resolved.revision,
        bundle.resolved.name,
        bundle.resolved.version,
        bundle.resolved.content_digest,
    ]
    first = subprocess.Popen(
        [*base, "first", str(entered), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not entered.exists():
        if first.poll() is not None:
            stdout, stderr = first.communicate()
            raise AssertionError(f"first mutation exited early: {stdout!r} {stderr!r}")
        if time.monotonic() >= deadline:
            first.kill()
            raise AssertionError("first mutation did not enter its lock window")
        time.sleep(0.01)

    second = subprocess.Popen(
        [*base, "second", str(entered), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.1)
    assert second.poll() is None, "second mutation did not block on the process lock"
    release.write_text("release\n", encoding="utf-8")
    first_stdout, first_stderr = first.communicate(timeout=10)
    second_stdout, second_stderr = second.communicate(timeout=10)

    assert first.returncode == 0, first_stderr
    assert second.returncode == 0, second_stderr
    assert first_stdout.strip() == "SUCCESS"
    assert second_stdout.strip() == "CONFLICT"
    assert manager.read_state(binding).skills_generation == 1
