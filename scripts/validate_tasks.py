#!/usr/bin/env python3
"""Catch broken tasks before they waste a run.

A malformed check or an unknown setup verb does not fail loudly at parse time,
it fails one task into a two-hour unattended run, and every task after it
inherits a phone left in the wrong state. Cheaper to refuse at the door.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phoneshell.bench.schema import load_all  # noqa: E402

SETUP_VERBS = {"home", "open_app", "terminate", "open_url", "tap", "type", "swipe", "wait",
               "clean_alarms"}
CHECK_KINDS = {"foreground_app", "element_exists", "element_exists_anywhere",
               "text_on_screen", "element_value",
               "switch_on", "switch_off", "screen_matches", "ocr_contains",
               "regex_on_screen", "app_state"}


def main() -> int:
    tasks = load_all(Path(__file__).resolve().parent.parent / "environments")
    problems: list[str] = []
    for t in tasks:
        if not t.checks:
            problems.append(f"{t.id}: no checks, so it can never pass or fail meaningfully")
        for step in t.setup + t.teardown:
            if step.action not in SETUP_VERBS:
                problems.append(f"{t.id}: unknown setup verb {step.action!r}")
        for c in t.checks:
            if c.kind not in CHECK_KINDS:
                problems.append(f"{t.id}: unknown check kind {c.kind!r}")
            if c.kind in {"element_exists", "element_exists_anywhere", "text_on_screen", "regex_on_screen",
                          "ocr_contains", "foreground_app"} and not c.text:
                problems.append(f"{t.id}: check {c.kind} needs text")
            if c.kind == "element_value" and not c.value:
                problems.append(f"{t.id}: element_value needs a value to compare")
        if c := next((c for c in t.checks if c.kind == "regex_on_screen"), None):
            import re
            try:
                re.compile(c.text)
            except re.error as exc:
                problems.append(f"{t.id}: bad regex {c.text!r} ({exc})")
        if t.max_steps < 3:
            problems.append(f"{t.id}: max_steps {t.max_steps} is too low to attempt anything")

    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        return 1
    caps = sorted({tag for t in tasks for tag in t.tags if tag.startswith("capability:")})
    print(f"{len(tasks)} tasks valid, {sum(len(t.checks) for t in tasks)} checks, "
          f"{len(caps)} capabilities covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
