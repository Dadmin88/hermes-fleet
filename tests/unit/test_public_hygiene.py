import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_public_hygiene.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("fleet_public_hygiene", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_hygiene_flags_private_paths_addresses_and_secret_shapes(
    tmp_path,
) -> None:
    checker = _load_checker()
    source = tmp_path / "candidate.txt"
    source.write_text(
        "workspace=/home/private-user/project\n"
        "endpoint=100.106.1.2\n"
        "token=" + "sk-" + ("a" * 24) + "\n",
        encoding="utf-8",
    )

    candidates = checker.find_candidates(tmp_path, [source])

    assert {(item.category, item.line) for item in candidates} == {
        ("absolute-user-path", 1),
        ("private-address", 2),
        ("secret-shape", 3),
    }


def test_public_hygiene_allows_generic_fixtures_and_documentation_addresses(
    tmp_path,
) -> None:
    checker = _load_checker()
    source = tmp_path / "generic.md"
    source.write_text(
        "node-a controller-1 worker-1 /path/to/hermes-fleet 198.51.100.2\n",
        encoding="utf-8",
    )

    assert checker.find_candidates(tmp_path, [source]) == []
