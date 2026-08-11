#!/usr/bin/env python3
"""Fail on high-signal private/operator-specific content in tracked public files."""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

_MAX_TEXT_BYTES = 2_000_000
_IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_IPV6 = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:]{0,4}"
    r"(?![0-9A-Fa-f:])"
)
_STRING_LITERAL = re.compile(r"[\"']([^\"']+)[\"']")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_FIXTURE_IDENTITY = re.compile(
    r"""(?ix)(?:["']?(?:hostname|given_name|device_name|machine_name|node_name|display_name|provider_name|alias)["']?)"""
    r"""\s*[:=]\s*["']([^"']+)["']"""
)
_GENERIC_FIXTURE_IDENTITY = re.compile(
    r"(?i)^(?:node|worker|controller|machine|device|compute|host|agent|server|"
    r"client|peer|provider|test|example|headscale|tailscale|keryx|hermes|fleet)"
    r"(?:[-_.][a-z0-9]+)+$"
)
_RESERVED_HOST_SUFFIXES = (
    ".example",
    ".example.com",
    ".example.org",
    ".example.net",
    ".invalid",
    ".test",
)
_RESERVED_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "example.invalid",
        "users.noreply.github.com",
    }
)
_FIXTURE_SCOPE = frozenset({"tests", "docs", "examples"})
_DOCUMENTATION_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)
_USER_PATH = re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users)/[A-Za-z0-9._-]+/")
_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|\bgh[opusr]_[A-Za-z0-9]{16,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY)"
)
_LOCAL_PACK = re.compile(r"(?:^|[\\/])\.hermes[\\/]content[\\/]")
_SELF = Path("scripts/check_public_hygiene.py")
_TEST_FIXTURE = Path("tests/unit/test_public_hygiene.py")
_EXCLUDED = frozenset({_SELF, _TEST_FIXTURE})


class Candidate(NamedTuple):
    path: Path
    line: int
    category: str


def _is_private_address(value: str) -> bool:
    try:
        octets = tuple(int(part) for part in value.split("."))
    except ValueError:
        return False
    if len(octets) != 4 or any(part > 255 for part in octets):
        return False
    first, second, _, _ = octets
    return (
        first == 10
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
        or (first == 100 and 64 <= second <= 127)
    )


def _address_category(value: str) -> str | None:
    if ":" in value and not any(character.isdigit() for character in value):
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.is_unspecified or address.is_loopback:
        return None
    if any(address in network for network in _DOCUMENTATION_NETWORKS):
        return None
    if address.version == 4 and _is_private_address(value):
        return "private-address"
    return "operator-address"


def _generic_fixture_identity(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "localhost":
        return True
    if any(normalized.endswith(suffix) for suffix in _RESERVED_HOST_SUFFIXES):
        return True
    return _GENERIC_FIXTURE_IDENTITY.fullmatch(normalized) is not None


def find_candidates(root: Path, paths: list[Path]) -> list[Candidate]:
    candidates: set[Candidate] = set()
    for supplied in paths:
        path = supplied if supplied.is_absolute() else root / supplied
        try:
            relative = path.relative_to(root)
            raw = path.read_bytes()
        except (OSError, ValueError):
            continue
        if relative in _EXCLUDED or len(raw) > _MAX_TEXT_BYTES or b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        fixture_scope = bool(relative.parts and relative.parts[0] in _FIXTURE_SCOPE)
        for line_number, line in enumerate(text.splitlines(), 1):
            if _USER_PATH.search(line):
                candidates.add(Candidate(relative, line_number, "absolute-user-path"))
            for match in _IPV4.finditer(line):
                category = _address_category(match.group())
                if category:
                    candidates.add(Candidate(relative, line_number, category))
            for literal in _STRING_LITERAL.finditer(line):
                for match in _IPV6.finditer(literal.group(1)):
                    category = _address_category(match.group())
                    if category:
                        candidates.add(Candidate(relative, line_number, category))
            for match in _EMAIL.finditer(line):
                if match.group(1).lower() not in _RESERVED_EMAIL_DOMAINS:
                    candidates.add(Candidate(relative, line_number, "email-address"))
            if fixture_scope:
                for match in _FIXTURE_IDENTITY.finditer(line):
                    if not _generic_fixture_identity(match.group(1)):
                        candidates.add(
                            Candidate(
                                relative, line_number, "operator-fixture-identity"
                            )
                        )
            if _SECRET.search(line):
                candidates.add(Candidate(relative, line_number, "secret-shape"))
            if _LOCAL_PACK.search(line):
                candidates.add(Candidate(relative, line_number, "local-content-pack"))
    return sorted(candidates)


def _repository_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        Path(value.decode("utf-8")) for value in result.stdout.split(b"\0") if value
    ]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    candidates = find_candidates(root, _repository_paths(root))
    for candidate in candidates:
        print(f"{candidate.path}:{candidate.line}: {candidate.category}")
    if candidates:
        print(
            "Public-hygiene candidates found. Replace private data with generic "
            "fixtures or document a narrow false-positive exception."
        )
        return 1
    print("Public-hygiene scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
