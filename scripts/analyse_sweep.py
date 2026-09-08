"""Turn a multi-model sweep into the numbers worth publishing.

A leaderboard row is the least interesting thing a benchmark produces. What is
worth having is the shape underneath it: which capabilities separate models,
where the cost sits, and whether the screenshot is doing any work at all.

Run after a sweep:  ./.venv/bin/python scripts/analyse_sweep.py xv xv-novision
"""
from __future__ import annotations

import collections
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoneshell.bench.schema import load_all           # noqa: E402

RESULTS = ROOT / "runtime" / "bench"
VISIONLESS = {"qwen/qwen3-max", "deepseek/deepseek-v3.2"}
SHORT = {
    "openai/gpt-5.1": "GPT-5.1",
    "google/gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "anthropic/claude-opus-4.8": "Claude Opus 4.8",
    "moonshotai/kimi-k3": "Kimi K3",
    "qwen/qwen3-max": "Qwen3-Max",
    "deepseek/deepseek-v3.2": "DeepSeek v3.2",
    "claude-sonnet-5": "Claude Sonnet 5",
}


def load(run: str) -> list[dict]:
    p = RESULTS / f"{run}.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    newest: dict[tuple[str, str], dict] = {}
    for r in rows:                       # newest attempt per (model, task) wins
        newest[(r["model"], r["task_id"])] = r
    return [r for r in newest.values() if not r.get("skipped")]


def table(rows: list[dict], caps: dict[str, list[str]]) -> None:
    by = collections.defaultdict(list)
    for r in rows:
        by[r["model"]].append(r)
    order = sorted(by, key=lambda m: -sum(1 for r in by[m] if r["passed"]) / max(1, len(by[m])))

    print(f"\n{'model':<20}{'score':>10}{'steps':>8}{'secs':>7}{'cost':>9}{'$/task':>9}  sees")
    print("-" * 74)
    for m in order:
        rs = by[m]
        p = sum(1 for r in rs if r["passed"])
        cost = sum(r.get("cost_usd") or 0 for r in rs)
        sees = "tree only" if m in VISIONLESS else "tree+image"
        print(f"{SHORT.get(m, m):<20}{p}/{len(rs):>3} {100*p/len(rs):>5.1f}%"
              f"{st.mean(r['turns'] for r in rs):>8.1f}"
              f"{st.mean(r['seconds'] for r in rs):>7.0f}"
              f"{cost:>9.3f}{cost/len(rs):>9.4f}  {sees}")

    # ---- where models actually differ
    print("\ncapability, by model (blank = not attempted)")
    models = order
    head = "".join(f"{SHORT.get(m,m)[:11]:>13}" for m in models)
    print(f"{'capability':<22}{head}")
    print("-" * (22 + 13 * len(models)))
    hardest = []
    for cap in sorted(caps):
        ids = set(caps[cap])
        cells, tot, won = "", 0, 0
        for m in models:
            rs = [r for r in by[m] if r["task_id"] in ids]
            if not rs:
                cells += f"{'-':>13}"
                continue
            k = sum(1 for r in rs if r["passed"])
            tot += len(rs); won += k
            cells += f"{k}/{len(rs):<11}".rjust(13)
        if tot:
            hardest.append((won / tot, cap, won, tot))
        print(f"{cap:<22}{cells}")

    hardest.sort()
    print("\nhardest capabilities across every model:")
    for rate, cap, won, tot in hardest[:6]:
        print(f"  {cap:<22} {won}/{tot}  {100*rate:.0f}%")

    # ---- tasks nobody could do are a benchmark result too
    per_task = collections.defaultdict(list)
    for r in rows:
        per_task[r["task_id"]].append(r["passed"])
    never = [t for t, v in per_task.items() if not any(v) and len(v) >= 3]
    always = [t for t, v in per_task.items() if all(v) and len(v) >= 3]
    print(f"\n{len(always)} tasks every model passed, {len(never)} no model passed")
    for t in sorted(never):
        print(f"  unsolved: {t}")


def ablation(vision_rows: list[dict], blind_rows: list[dict]) -> None:
    """The only clean way to ask what the screenshot is worth."""
    if not blind_rows:
        print("\nno ablation run found")
        return
    model = blind_rows[0]["model"]
    seeing = [r for r in vision_rows if r["model"] == model]
    if not seeing:
        print("\nablation has no sighted counterpart")
        return
    shared = {r["task_id"] for r in seeing} & {r["task_id"] for r in blind_rows}
    a = [r for r in seeing if r["task_id"] in shared]
    b = [r for r in blind_rows if r["task_id"] in shared]
    pa = sum(1 for r in a if r["passed"]); pb = sum(1 for r in b if r["passed"])
    print(f"\n\nWHAT IS THE SCREENSHOT WORTH?   {SHORT.get(model, model)}, "
          f"same model, same {len(shared)} tasks")
    print("-" * 74)
    print(f"  with the image   {pa}/{len(a)}  {100*pa/len(a):.1f}%   "
          f"{st.mean(r['turns'] for r in a):.1f} steps   "
          f"{st.mean(r['seconds'] for r in a):.0f}s   ${sum(r.get('cost_usd') or 0 for r in a):.3f}")
    print(f"  tree only        {pb}/{len(b)}  {100*pb/len(b):.1f}%   "
          f"{st.mean(r['turns'] for r in b):.1f} steps   "
          f"{st.mean(r['seconds'] for r in b):.0f}s   ${sum(r.get('cost_usd') or 0 for r in b):.3f}")
    delta = 100 * (pa - pb) / len(a)
    print(f"  vision is worth {delta:+.1f} points and "
          f"{st.mean(r['turns'] for r in a) - st.mean(r['turns'] for r in b):+.1f} steps")

    byid_a = {r["task_id"]: r["passed"] for r in a}
    byid_b = {r["task_id"]: r["passed"] for r in b}
    only_sighted = sorted(t for t in shared if byid_a[t] and not byid_b[t])
    only_blind = sorted(t for t in shared if byid_b[t] and not byid_a[t])
    if only_sighted:
        print("\n  needed the image:")
        for t in only_sighted:
            print(f"    {t}")
    if only_blind:
        print("\n  passed WITHOUT the image but failed with it "
              "(the picture is not free: it is context that can mislead):")
        for t in only_blind:
            print(f"    {t}")


def main() -> int:
    runs = sys.argv[1:] or ["xv"]
    tasks = load_all(ROOT / "environments")
    caps: dict[str, list[str]] = collections.defaultdict(list)
    for t in tasks:
        for g in t.tags:
            if g.startswith("capability:"):
                caps[g.split(":", 1)[1]].append(t.id)

    main_rows = load(runs[0])
    if not main_rows:
        print(f"no results in {runs[0]}")
        return 1
    print(f"CROSS-VENDOR SWEEP  ({len({r['task_id'] for r in main_rows})} tasks, "
          f"{len({r['model'] for r in main_rows})} models, physical iPhone, iOS 26.6)")
    table(main_rows, caps)
    if len(runs) > 1:
        ablation(main_rows, load(runs[1]))
    total = sum(r.get("cost_usd") or 0 for r in main_rows)
    print(f"\ntotal API spend for this sweep: ${total:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
