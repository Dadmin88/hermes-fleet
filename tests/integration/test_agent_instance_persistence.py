from __future__ import annotations

import json
import subprocess
import sys
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
