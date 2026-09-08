"""What a benchmark task IS.

Three parts, and the third is the one that makes it worth money:

  setup    deterministic steps that put the phone in a known start state,
           run with no model involved so every attempt starts identically
  instruct the sentence handed to the agent under test
  checks   assertions run afterwards that decide pass or fail automatically

Recordings of someone using a phone are worth about a dollar. A task with a
working automatic checker is worth two to three orders of magnitude more,
because it can be run against a model ten thousand times unattended. The checker
is the product.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Step:
    """One deterministic action for setup or teardown."""
    action: str                       # open_app | terminate | home | tap | type | swipe | open_url | wait
    value: str = ""
    text: str = ""
    seconds: float = 0.0

    @classmethod
    def parse(cls, raw: Any) -> "Step":
        if isinstance(raw, str):                       # "home"
            return cls(action=raw)
        if isinstance(raw, dict) and len(raw) == 1:    # {open_app: Settings}
            (action, value), = raw.items()
            if isinstance(value, dict):
                return cls(action=action, **value)
            return cls(action=action, value=str(value))
        raise ValueError(f"cannot read step: {raw!r}")


@dataclass
class Check:
    """One assertion about the phone after the agent has finished."""
    kind: str                         # see bench/verify.py for the vocabulary
    text: str = ""
    value: str = ""
    app: str = ""
    negate: bool = False
    note: str = ""

    @classmethod
    def parse(cls, raw: Any) -> "Check":
        if isinstance(raw, dict) and len(raw) == 1 and isinstance(list(raw.values())[0], (str, int, float)):
            (kind, value), = raw.items()
            return cls(kind=kind, text=str(value))
        if isinstance(raw, dict):
            data = dict(raw)
            kind = data.pop("kind", None) or data.pop("check", None)
            if kind is None:
                raise ValueError(f"check needs a kind: {raw!r}")
            return cls(kind=str(kind), **{k: v for k, v in data.items() if k in
                                          {"text", "value", "app", "negate", "note"}})
        raise ValueError(f"cannot read check: {raw!r}")


@dataclass
class Task:
    id: str
    name: str
    instruction: str
    app: str = ""                     # bundle id the task centres on
    difficulty: str = "medium"        # easy | medium | hard
    tags: list[str] = field(default_factory=list)
    setup: list[Step] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    teardown: list[Step] = field(default_factory=list)
    max_steps: int = 25
    timeout_seconds: float = 240.0
    notes: str = ""
    source: Path | None = None

    @classmethod
    def load(cls, path: Path) -> "Task":
        raw = yaml.safe_load(path.read_text())
        return cls(
            id=raw["id"], name=raw["name"], instruction=raw["instruction"],
            app=raw.get("app", ""), difficulty=raw.get("difficulty", "medium"),
            tags=list(raw.get("tags", [])),
            setup=[Step.parse(s) for s in raw.get("setup", [])],
            checks=[Check.parse(c) for c in raw.get("checks", [])],
            teardown=[Step.parse(s) for s in raw.get("teardown", [])],
            max_steps=int(raw.get("max_steps", 25)),
            timeout_seconds=float(raw.get("timeout_seconds", 240)),
            notes=raw.get("notes", ""), source=path,
        )


def load_all(directory: Path) -> list[Task]:
    tasks = [Task.load(p) for p in sorted(directory.glob("*.yaml"))]
    ids = [t.id for t in tasks]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate task ids: {sorted(duplicates)}")
    return tasks
