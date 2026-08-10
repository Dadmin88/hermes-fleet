#!/usr/bin/env python3
"""Fail on high-signal private/operator-specific content in tracked public files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import NamedTuple

_MAX_TEXT_BYTES = 2_000_000
_IPV4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
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
        for line_number, line in enumerate(text.splitlines(), 1):
            if _USER_PATH.search(line):
                candidates.add(Candidate(relative, line_number, "absolute-user-path"))
            if any(
                _is_private_address(match.group()) for match in _IPV4.finditer(line)
            ):
                candidates.add(Candidate(relative, line_number, "private-address"))
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
