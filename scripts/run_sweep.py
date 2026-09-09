"""The sweep that would let the leaderboard rank anything.

The published board ran six models over 24 tasks and separated none of them:
zero of fifteen pairs reach p<0.05 under McNemar, and the widest gap on the
board (Kimi K3 over DeepSeek) would need about 33 tasks before it counted as a
result. The suite has 80 public tasks. Running all of them is the fix, and it
is the only fix: more models on 24 tasks adds rows, not resolution.

Two design choices, both learned from watching earlier runs die halfway.

**Round-robin by task, not blocked by model.** Every model attempts task 1
before any model attempts task 2. A run killed at any point therefore leaves an
almost perfectly balanced design, and the paired analysis still works on what
finished. The old sweep ran one model to completion at a time, so an
interruption left one model with 60 tasks and another with none, which is not a
paired comparison at all.

**Resumable by (model, task).** Anything already recorded in the target run is
skipped, so an interrupted sweep is restarted with the same command and costs
nothing for the work already done. A four-hour run on a physical phone WILL be
interrupted.

The 20 probe.* tasks are excluded. They are experiment 2's stimuli, engineered
so that a model with eyes gets all ten and a model without gets none. Adding
them to a general leaderboard would measure one narrow property twenty times
and manufacture a gap between vision and text-only models that says nothing
about phone control.

    ./.venv/bin/python scripts/run_sweep.py            # all 60, all 6 models
    ./.venv/bin/python scripts/run_sweep.py --run xv2 --models openai/gpt-5.1
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoneshell.actions import Phone                                          # noqa: E402
from phoneshell.bench.runner import (RESULTS, Runner, RigUnavailable,          # noqa: E402
                                     UsageLimitReached)
from phoneshell.bench.schema import load_all                                   # noqa: E402
from phoneshell.lock import DeviceBusy, device_lock                            # noqa: E402

MODELS = [
    "moonshotai/kimi-k3",
    "openai/gpt-5.1",
    "anthropic/claude-opus-4.8",
    "qwen/qwen3-max",
    "google/gemini-3.1-pro-preview",
    "deepseek/deepseek-v3.2",
]


def devices_in(run: str) -> set[str]:
    """Which phones produced the results already in this run file.

    There are two phones on this desk with different iOS builds and different
    apps installed. Resuming a sweep on the other one would pool two
    instruments into a single score with nothing on the page to say so, which
    is precisely the class of quiet error this project keeps finding after the
    fact. Cheaper to refuse.
    """
    path = RESULTS / f"{run}.jsonl"
    if not path.exists():
        return set()
    seen = set()
    for line in path.read_text().splitlines():
        if line.strip():
            udid = json.loads(line).get("device_udid")
            if udid:
                seen.add(udid)
    return seen


def already_done(run: str) -> set[tuple[str, str]]:
    path = RESULTS / f"{run}.jsonl"
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            # A skip is not a result: the model was never asked, so it must be
            # retried rather than silently left as a hole in the design.
            if not r.get("skipped"):
                done.add((r["model"], r["task_id"]))
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="xv2")
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--only", default="", help="task id prefix filter")
    ap.add_argument("--include-probes", action="store_true")
    ap.add_argument("--private", action="store_true",
                    help="also run the held-out tasks (never published)")
    args = ap.parse_args()

    models = [m for m in args.models.split(",") if m]
    tasks = [t for t in load_all(ROOT / "environments", include_private=args.private)
             if (args.include_probes or not t.id.startswith("probe."))
             and t.id.startswith(args.only)]
    tasks.sort(key=lambda t: t.id)
    if not tasks:
        print("no tasks matched")
        return 1

    done = already_done(args.run)
    todo = [(t, m) for t in tasks for m in models if (m, t.id) not in done]
    print(f"{len(tasks)} tasks x {len(models)} models = {len(tasks) * len(models)} cells; "
          f"{len(done)} already recorded, {len(todo)} to run")
    if not todo:
        print("nothing to do")
        return 0

    # One phone, one job. A watchdog resumed this sweep, the operator started a
    # second believing the first was dead, and the two drove the same phone for
    # seventy minutes. The duplicated task ids in the log were the only symptom;
    # the corrupted checks looked like ordinary passes and failures. 64 rows had
    # to be discarded.
    try:
        lock = device_lock(f"run_sweep --run {args.run}")
        lock.__enter__()
    except DeviceBusy as exc:
        print(exc)
        return 1

    phone = Phone()
    here = phone.cfg.device.udid or ""
    prior = devices_in(args.run)
    if prior and here and prior != {here}:
        print(f"refusing to resume: run {args.run!r} holds results from "
              f"{', '.join(sorted(prior))} and this phone is {here}.")
        print("Two devices are two instruments. Use a different --run name.")
        return 1
    runner = Runner(phone)
    from phoneshell.prep import prepare_for_run
    for r in prepare_for_run(phone):
        print(f"  {'ok' if r.ok else '!!'} {r.detail}", flush=True)
        if r.fatal:
            print("refusing to run: the phone cannot be driven", flush=True)
            return 1

    started = time.time()
    tally: dict[str, list[int]] = {m: [0, 0] for m in models}
    for i, (task, model) in enumerate(todo, 1):
        try:
            res = runner.run(task, model=model)
        except (RigUnavailable, UsageLimitReached) as exc:
            print(f"\n  STOPPED at {i}/{len(todo)}: {exc}")
            print(f"  restart with the same command; {i - 1} results are already saved")
            break
        Runner.record(res, args.run)
        if not res.skipped:
            tally[model][1] += 1
            tally[model][0] += 1 if res.passed else 0
        mark = "pass" if res.passed else ("skip" if res.skipped else "FAIL")
        rate = (time.time() - started) / i
        print(f"  [{i:>3}/{len(todo)}] {model.split('/')[-1][:14]:<14} "
              f"{task.id:<28} {mark:<4} {res.turns:>2}t {res.seconds:>3.0f}s "
              f"eta {(len(todo) - i) * rate / 60:>4.0f}m", flush=True)

    lock.__exit__(None, None, None)
    print(f"\nswept for {(time.time() - started) / 60:.0f} min")
    for m in models:
        k, n = tally[m]
        if n:
            print(f"  {m:<32}{k}/{n}  {100 * k / n:.1f}%")
    print(f"\nnow: ./.venv/bin/python scripts/analyse_models.py {args.run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
