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


def test_public_hygiene_flags_operator_specific_public_residue(tmp_path) -> None:
    checker = _load_checker()
    fixture_dir = tmp_path / "tests"
    fixture_dir.mkdir()
    source = fixture_dir / "candidate.py"
    source.write_text(
        "hostname='phoenix'\n"
        "given_name='phoenix.acme.dev'\n"
        "endpoint=8.8.8.8\n"
        "mesh='fd12:3456:789a::42'\n"
        "owner=operator@acme.dev\n",
        encoding="utf-8",
    )

    candidates = checker.find_candidates(tmp_path, [source])

    assert {(item.category, item.line) for item in candidates} == {
        ("operator-fixture-identity", 1),
        ("operator-fixture-identity", 2),
        ("operator-address", 3),
        ("operator-address", 4),
        ("email-address", 5),
    }


def test_public_hygiene_allows_reserved_operator_agnostic_fixtures(tmp_path) -> None:
    checker = _load_checker()
    fixture_dir = tmp_path / "tests"
    fixture_dir.mkdir()
    source = fixture_dir / "generic.py"
    source.write_text(
        "hostname='compute-a'\n"
        "given_name='compute-a.example.invalid'\n"
        "ipv4=198.51.100.2\n"
        "ipv6=2001:db8::2\n"
        "owner=operator@example.com\n",
        encoding="utf-8",
    )

    assert checker.find_candidates(tmp_path, [source]) == []
