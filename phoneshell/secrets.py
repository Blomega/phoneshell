"""Secrets, kept in the macOS Keychain rather than in a file.

The device passcode is what stops an unattended rig dead: the phone auto-locks,
and iOS deliberately blocks automation from the passcode screen, so everything
stops until a human walks over. Storing the code lets the bridge get back in by
itself.

That is a real trade, so it is made explicitly:
  * the code lives in the login Keychain, encrypted at rest, unlocked only by the
    user's own macOS login, never in the repo and never in a config file
  * it is written by a command that reads it from a hidden prompt, so it never
    appears in shell history, in a transcript, or in a screenshot
  * it is only ever sent to the phone as taps on that phone's own keypad
  * `phoneshell forget-passcode` removes it, and nothing else in the codebase
    reads it except the unlock path
"""
from __future__ import annotations

import subprocess

SERVICE = "phoneshell"


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, input=stdin, timeout=30)


def set_secret(account: str, value: str, label: str = "phoneshell device passcode") -> bool:
    """Store or replace a secret. -U updates in place if it already exists."""
    res = _run([
        "security", "add-generic-password",
        "-a", account, "-s", SERVICE, "-l", label,
        "-w", value, "-U",
    ])
    return res.returncode == 0


def get_secret(account: str) -> str | None:
    res = _run(["security", "find-generic-password", "-a", account, "-s", SERVICE, "-w"])
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def delete_secret(account: str) -> bool:
    res = _run(["security", "delete-generic-password", "-a", account, "-s", SERVICE])
    return res.returncode == 0


def has_secret(account: str) -> bool:
    return get_secret(account) is not None
