"""Are the models on the leaderboard actually different from each other?

Sam asked the right question of the published board: several models sit on the
same number, so what is the ranking worth? This answers it properly instead of
presenting six percentages as if they were a ranking.

Three things are computed, none of which the board showed:

1. A Wilson interval on every score. With 24 items, 83.3% carries a CI of about
   64-93%. Two models on the same point estimate are not "tied", they are
   *indistinguishable at this sample size*, and that is a different claim.
2. A pairwise McNemar test on the paired per-task outcomes. Two models run on
   the SAME tasks are a paired design, so the unpaired comparison of two
   percentages throws away exactly the information that decides the question.
3. Item statistics. A task passed by every model and a task failed by every
   model both carry zero information about ranking. What is left after removing
   them is the instrument's real resolution.
"""
from __future__ import annotations

import collections
import itertools
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "runtime" / "bench"
SHORT = {
    "openai/gpt-5.1": "GPT-5.1",
    "google/gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "anthropic/claude-opus-4.8": "Claude Opus 4.8",
    "moonshotai/kimi-k3": "Kimi K3",
    "qwen/qwen3-max": "Qwen3-Max",
    "deepseek/deepseek-v3.2": "DeepSeek v3.2",
    "claude-sonnet-5": "Claude Sonnet 5",
}


def load(run: str) -> dict[tuple[str, str], bool]:
    p = RESULTS / f"{run}.jsonl"
    out: dict[tuple[str, str], bool] = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("skipped"):
                out[(r["model"], r["task_id"])] = bool(r["passed"])
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    s = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (100 * max(0.0, c - s), 100 * min(1.0, c + s))


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def required_n(b: int, c: int, n: int, cap: int = 4000) -> int | None:
    """Smallest task count at which this pair's observed split reaches p<0.05.

    The discordance rate and its direction are held at what was measured and
    scaled up, which assumes the extra tasks look like the ones already run.
    That is the optimistic assumption, so the answer is a lower bound on how
    much more measurement the ranking needs. None means the split is symmetric
    enough that no amount of scaling separates them.
    """
    if b == c:
        return None
    for N in range(n, cap + 1):
        if mcnemar_exact(round(b * N / n), round(c * N / n)) < 0.05:
            return N
    return None


def main() -> int:
    runs = sys.argv[1:] or ["xv"]
    res: dict[tuple[str, str], bool] = {}
    for r in runs:
        res.update(load(r))
    if not res:
        print("no results")
        return 1

    models = sorted({m for m, _ in res})
    # Only tasks every model actually attempted: an unbalanced set makes the
    # percentages incomparable before any statistics are done.
    per_model = {m: {t for mm, t in res if mm == m} for m in models}
    shared = set.intersection(*per_model.values())
    dropped = {m: len(per_model[m] - shared) for m in models}
    n = len(shared)

    print(f"MODEL COMPARISON   {n} tasks attempted by all {len(models)} models"
          f"   (runs: {', '.join(runs)})")
    if any(dropped.values()):
        print("  tasks dropped as not-common: "
              + ", ".join(f"{SHORT.get(m,m)} {d}" for m, d in dropped.items() if d))
    print()

    score = {m: sum(1 for t in shared if res[(m, t)]) for m in models}
    order = sorted(models, key=lambda m: -score[m])

    print(f"{'model':<18}{'score':>10}{'':>3}{'95% CI (Wilson)':>20}")
    print("-" * 52)
    for m in order:
        lo, hi = wilson(score[m], n)
        print(f"{SHORT.get(m,m):<18}{score[m]}/{n:<3} {100*score[m]/n:>5.1f}%   "
              f"{lo:>6.1f}% to {hi:<6.1f}%")

    # ---- item statistics: how much of the set can discriminate at all
    by_task = {t: sum(1 for m in models if res[(m, t)]) for t in shared}
    ceiling = [t for t, k in by_task.items() if k == len(models)]
    floor = [t for t, k in by_task.items() if k == 0]
    live = [t for t in shared if t not in ceiling and t not in floor]
    print(f"\nITEM STATISTICS")
    print(f"  {len(ceiling):>3} tasks every model passed   (zero information)")
    print(f"  {len(floor):>3} tasks no model passed       (zero information)")
    print(f"  {len(live):>3} tasks that actually discriminate  "
          f"-> {100*len(live)/n:.0f}% of the set is doing the work")
    if live:
        print(f"\n  score on the {len(live)} discriminating tasks only:")
        for m in sorted(models, key=lambda m: -sum(1 for t in live if res[(m, t)])):
            k = sum(1 for t in live if res[(m, t)])
            lo, hi = wilson(k, len(live))
            print(f"    {SHORT.get(m,m):<18}{k}/{len(live):<3} "
                  f"{100*k/len(live):>5.1f}%   [{lo:.0f}-{hi:.0f}%]")

    # ---- the pairwise test, which is what the ranking actually rests on
    print(f"\nPAIRWISE McNEMAR   (paired on the same {n} tasks; b/c = tasks won only "
          f"by the row / only by the column)")
    print(f"{'':<18}" + "".join(f"{SHORT.get(m,m)[:11]:>14}" for m in order))
    print("-" * (18 + 14 * len(order)))
    verdicts = []
    for a in order:
        row = f"{SHORT.get(a,a):<18}"
        for bm in order:
            if a == bm:
                row += f"{'-':>14}"
                continue
            b = sum(1 for t in shared if res[(a, t)] and not res[(bm, t)])
            c = sum(1 for t in shared if res[(bm, t)] and not res[(a, t)])
            p = mcnemar_exact(b, c)
            row += f"{f'{b}/{c} p={p:.2f}':>14}"
            if order.index(a) < order.index(bm):
                verdicts.append((p, a, bm, b, c))
        print(row)

    sig = [v for v in verdicts if v[0] < 0.05]
    print(f"\n  {len(sig)} of {len(verdicts)} model pairs differ significantly at p<0.05.")
    if sig:
        for p, a, bm, b, c in sorted(sig):
            print(f"    {SHORT.get(a,a)} > {SHORT.get(bm,bm)}  (b={b} c={c}, p={p:.3f})")
    else:
        print("    Not one. On this task set the six models are statistically")
        print("    indistinguishable, and any ordering of them is noise.")
    # ---- how big does the set have to be before it can rank anything?
    # McNemar with every discordant pair pointing one way needs b >= 6 to reach
    # p < 0.05 (2 * 0.5^6 = 0.031). Below that, no amount of consistency helps.
    print("\nHOW MANY TASKS WOULD IT TAKE?")
    print("  Holding each pair's observed discordance rate fixed and asking for the")
    print("  smallest task set that would reach p<0.05:")
    rows = []
    for p_, a, bm, b, c in sorted(verdicts):
        need = required_n(b, c, n)
        rows.append((need if need else 10 ** 9, a, bm, b, c, need))
    for _, a, bm, b, c, need in sorted(rows):
        gap = f"{SHORT.get(a,a)} vs {SHORT.get(bm,bm)}"
        if need is None:
            print(f"    {gap:<38} b={b} c={c}   never: the wins are symmetric, "
                  f"these two are the same model to this instrument")
        else:
            print(f"    {gap:<38} b={b} c={c}   needs ~{need} tasks")
    best = min((r for r in rows if r[5]), default=None)
    if best:
        print(f"\n  So the cheapest real ranking claim on this board, "
              f"{SHORT.get(best[1],best[1])} > {SHORT.get(best[2],best[2])}, "
              f"needs ~{best[5]} tasks.")
        print(f"  The set has {n}. The suite is not underpowered by a little; it is")
        print(f"  {best[5]/n:.1f}x short of separating even its widest gap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
