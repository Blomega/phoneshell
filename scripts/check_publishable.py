#!/usr/bin/env python3
"""Refuse to publish if anything personal is about to ship.

Run before any commit or deploy. It looks for the identifiers that this rig
inevitably accumulates: the device UDID, the signing team, account names, and
the bundle ids of the owner's own third-party apps. A benchmark repo is meant to
be cloned by strangers, so this failing is a hard stop, not a warning.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Patterns that must never appear in a publishable file.
FORBIDDEN = {
    "device UDID": re.compile(r"\b000[0-9A-F]{5}-00[0-9A-F]{16}\b"),
    "Apple team id": re.compile(r"\b[A-Z0-9]{10}\b(?=.*(?:TEAM|team|DEVELOPMENT_TEAM))"),
    "home directory": re.compile(r"/Users/[a-z]+/(?!USER)"),
    "keychain sha1": re.compile(r"\b[A-F0-9]{40}\b"),
}
def literals() -> list[str]:
    """Extra strings to forbid, read from a local file that is never committed.

    The list of your personal identifiers is itself personal, so it does not live
    in this file. Put one string per line in runtime/private_strings.txt; the
    whole runtime directory is gitignored.
    """
    path = ROOT / "runtime" / "private_strings.txt"
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]

SKIP_DIRS = {".git", ".venv", "runtime", "vendor", "build", "__pycache__", "site"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".pyc", ".log", ".jsonl"}


def files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        out.append(p)
    return out


def main() -> int:
    problems: list[str] = []
    extra = literals()
    for path in files():
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            continue
        rel = path.relative_to(ROOT)
        for literal in extra:
            if literal in text:
                problems.append(f"{rel}: contains {literal!r}")
        for label, pattern in FORBIDDEN.items():
            match = pattern.search(text)
            if match:
                problems.append(f"{rel}: looks like a {label} ({match.group(0)[:24]})")

    # Anything git would actually commit from runtime/ is a bug in .gitignore.
    tracked = subprocess.run(["git", "ls-files", "runtime"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
    if tracked:
        problems.append("runtime/ files are tracked by git: " + tracked.splitlines()[0])

    if problems:
        print("NOT PUBLISHABLE:")
        for p in sorted(set(problems)):
            print("  -", p)
        return 1
    print(f"publishable: {len(files())} files checked against "
          f"{len(FORBIDDEN)} patterns and {len(extra)} private strings, nothing found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
