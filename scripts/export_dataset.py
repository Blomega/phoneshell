"""Build the public dataset: the task set and every scored run, sanitised.

The repository is the harness. This is the *data*, which is the part someone
else can use without owning an iPhone and a Mac: 60 task definitions with their
machine-checkable success conditions, and every run that has been scored
against them, so the analyses in FINDINGS.md can be recomputed from source.

Sanitising is not optional and not a formality. These runs happened on a
personal phone with real contacts, real notes and real mail on it.

  * Only the 60 public tasks are exported. The 16 held-out tasks appear nowhere,
    not their ids, not their instructions, not their results. A benchmark whose
    whole answer key is public becomes training data.
  * `agent_said` is the model's own free text, and on a task like a contact
    lookup it can quote the owner's data straight back. It is kept only where a
    check of kind `answer_contains` grades it, which is exactly the case where
    the string is a controlled value the task itself planted, and dropped
    everywhere else.
  * The device UDID is stripped. The model and OS version stay, because two
    phones are two instruments and a reader needs to know which one ran what.

    ./.venv/bin/python scripts/export_dataset.py runtime/export
"""
from __future__ import annotations

import dataclasses
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoneshell.bench.schema import load_all      # noqa: E402

RESULTS = ROOT / "runtime" / "bench"
# Runs that were scored and are worth publishing, with what each one is.
RUNS = {
    "xv": "cross-vendor sweep, 6 models x 24 tasks",
    "xv-text": "the two text-only models on the same 24 tasks",
    "xv-novision": "GPT-5.1 again with the screenshot withheld",
    "exp2-v": "experiment 2, image arm",
    "exp2-n": "experiment 2, tree-only arm",
    "v4": "single-model reference run over the whole suite",
    "xv2": "full sweep, 6 models x 60 tasks",
}
UDID = re.compile(r"\b000[0-9A-F]{5}-00[0-9A-F]{16}\b")


def private_strings() -> list[str]:
    path = ROOT / "runtime" / "private_strings.txt"
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "runtime" / "export")
    out.mkdir(parents=True, exist_ok=True)
    secrets = private_strings()

    public = load_all(ROOT / "environments", include_private=False)
    ids = {t.id for t in public}
    graded_answer = {t.id for t in public
                     if any(c.kind == "answer_contains" for c in t.checks)}

    with (out / "tasks.jsonl").open("w") as fh:
        for t in sorted(public, key=lambda t: t.id):
            fh.write(json.dumps({
                "task_id": t.id,
                "instruction": t.instruction,
                "app": t.app,
                "difficulty": t.difficulty,
                "tags": list(t.tags),
                "checks": [dataclasses.asdict(c) for c in t.checks],
                "max_steps": t.max_steps,
                "timeout_seconds": t.timeout_seconds,
                "notes": t.notes,
            }) + "\n")
    print(f"  tasks.jsonl        {len(public)} public tasks")

    (out / "results").mkdir(exist_ok=True)
    total = 0
    for run, what in RUNS.items():
        src = RESULTS / f"{run}.jsonl"
        if not src.exists():
            continue
        kept = []
        for line in src.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r["task_id"] not in ids:
                continue          # held-out task, or a task since retired
            r.pop("device_udid", None)
            if r["task_id"] not in graded_answer:
                r.pop("agent_said", None)
            kept.append(r)
        blob = json.dumps(kept)
        if UDID.search(blob):
            print(f"  !! {run}: a UDID survived sanitising, refusing to export")
            return 1
        for s in secrets:
            if s and s in blob:
                print(f"  !! {run}: a private string survived sanitising, refusing")
                return 1
        (out / "results" / f"{run}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in kept))
        total += len(kept)
        print(f"  results/{run}.jsonl{'':<{max(0, 8 - len(run))}} {len(kept):>4} rows   {what}")

    print(f"\n  {total} result rows exported to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
