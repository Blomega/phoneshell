"""Experiment 2: run the pre-registered paired vision ablation.

Design in docs/EXPERIMENT-2.md. Two things matter about how this runs:

**Interleaved, not blocked.** Each task is run with the image and then without it,
back to back, before moving on. Experiment 1 ran the two arms hours apart, which
confounds device drift with condition on a phone whose state is being mutated by
the very tasks being measured.

**Order alternates.** Half the tasks run image-first and half tree-first, so any
order effect (a warm app, a cached page) cancels instead of loading onto one arm.

Everything else is the harness: heal() unlocks and restarts the runner between
tasks, so a lock or a dead runner pauses the experiment rather than ending it.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoneshell.actions import Phone                                  # noqa: E402
from phoneshell.bench.runner import Runner, RigUnavailable, UsageLimitReached  # noqa: E402
from phoneshell.bench.schema import load_all                          # noqa: E402

MODELS = ["openai/gpt-5.1", "google/gemini-3.1-pro-preview", "moonshotai/kimi-k3"]
RUN = "exp2"


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else "probe."
    tasks = [t for t in load_all(ROOT / "environments") if t.id.startswith(only)]
    tasks.sort(key=lambda t: t.id)
    if not tasks:
        print(f"no tasks matching {only!r}")
        return 1

    phone = Phone()
    runner = Runner(phone)
    from phoneshell.prep import prepare_for_run
    for r in prepare_for_run(phone):
        print(f"  {'ok' if r.ok else '!!'} {r.detail}", flush=True)
        if r.fatal:
            print("refusing to run: the phone cannot be driven", flush=True)
            return 1

    total = len(tasks) * len(MODELS) * 2
    print(f"\n{len(tasks)} tasks x {len(MODELS)} models x 2 conditions = {total} runs\n",
          flush=True)
    done = 0
    started = time.time()
    for model in MODELS:
        for i, task in enumerate(tasks):
            # Alternate which arm goes first so order effects cancel.
            order = [True, False] if i % 2 == 0 else [False, True]
            for sees in order:
                label = "image" if sees else "tree "
                try:
                    res = runner.run(task, model=model, vision=sees)
                except (RigUnavailable, UsageLimitReached) as exc:
                    print(f"  STOPPED: {exc}", flush=True)
                    return 1
                Runner.record(res, f"{RUN}-{'v' if sees else 'n'}")
                done += 1
                mark = "pass" if res.passed else ("skip" if res.skipped else "FAIL")
                rate = (time.time() - started) / done
                print(f"  [{done:>3}/{total}] {model.split('/')[-1][:16]:<16} "
                      f"{label} {task.id:<22} {mark}  {res.turns:>2}t "
                      f"{res.seconds:>3.0f}s  eta {(total-done)*rate/60:>4.0f}m",
                      flush=True)
    print(f"\nEXPERIMENT COMPLETE in {(time.time()-started)/60:.0f} min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
