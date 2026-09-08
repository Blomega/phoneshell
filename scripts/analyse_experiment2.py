"""The analysis pre-registered in docs/EXPERIMENT-2.md.

Primary endpoint: paired per-task pass/fail for the same model with and without
the screenshot, pooled across models, McNemar's exact test, reported per stratum.

The two strata are reported separately because that is the whole design. The
render stratum's answers exist only as pixels; the tree stratum's are in the
accessibility tree. If vision does anything, it does it in the first and not the
second, and the second is what proves the effect is not an artifact of the
harness.
"""
from __future__ import annotations

import collections
import json
import math
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoneshell.bench.schema import load_all      # noqa: E402

RESULTS = ROOT / "runtime" / "bench"


def load(run: str) -> dict[tuple[str, str], dict]:
    p = RESULTS / f"{run}.jsonl"
    if not p.exists():
        return {}
    out: dict[tuple[str, str], dict] = {}
    for line in p.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            if not r.get("skipped"):
                out[(r["model"], r["task_id"])] = r
    return out


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact p for discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    s = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (100 * max(0.0, c - s), 100 * min(1.0, c + s))


def main() -> int:
    vis, blind = load("exp2-v"), load("exp2-n")
    if not vis or not blind:
        print("no experiment 2 results yet")
        return 1
    stratum = {}
    for t in load_all(ROOT / "environments"):
        for tag in t.tags:
            if tag.startswith("stratum:"):
                stratum[t.id] = tag.split(":", 1)[1]

    shared = sorted(set(vis) & set(blind))
    models = sorted({m for m, _ in shared})
    print(f"EXPERIMENT 2   {len(shared)} paired observations, "
          f"{len(models)} models, physical iPhone\n")

    print(f"{'':<10}{'':<12}{'with image':>14}{'tree only':>14}{'b':>5}{'c':>5}"
          f"{'McNemar p':>12}")
    print("-" * 74)

    pooled: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for strat in ("render", "tree"):
        keys = [k for k in shared if stratum.get(k[1]) == strat]
        if not keys:
            continue
        for scope in ["POOLED"] + models:
            sel = keys if scope == "POOLED" else [k for k in keys if k[0] == scope]
            if not sel:
                continue
            a = sum(1 for k in sel if vis[k]["passed"])
            d = sum(1 for k in sel if blind[k]["passed"])
            b = sum(1 for k in sel if vis[k]["passed"] and not blind[k]["passed"])
            c = sum(1 for k in sel if blind[k]["passed"] and not vis[k]["passed"])
            p = mcnemar_exact(b, c)
            name = scope if scope == "POOLED" else scope.split("/")[-1][:11]
            star = "  ***" if p < 0.001 else ("  **" if p < 0.01 else ("  *" if p < 0.05 else ""))
            print(f"{strat:<10}{name:<12}{a}/{len(sel):>4} {100*a/len(sel):>5.0f}%"
                  f"{d}/{len(sel):>4} {100*d/len(sel):>5.0f}%{b:>5}{c:>5}{p:>12.4f}{star}")
            if scope == "POOLED":
                pooled[strat] = [b, c]
        print()

    print("-" * 74)
    print("b = passed only WITH the image     c = passed only WITHOUT it\n")

    rb, rc = pooled.get("render", [0, 0])
    tb, tc = pooled.get("tree", [0, 0])
    print("THE INTERACTION, which is the actual result:")
    print(f"  render stratum   b={rb} c={rc}   p={mcnemar_exact(rb, rc):.4f}")
    print(f"  tree stratum     b={tb} c={tc}   p={mcnemar_exact(tb, tc):.4f}")
    if rb > rc and mcnemar_exact(rb, rc) < 0.05 and mcnemar_exact(tb, tc) >= 0.05:
        print("\n  Vision helps where, and ONLY where, the answer is not in the tree.")
        print("  The control stratum shows no effect, so this is not a harness artifact.")
    elif mcnemar_exact(rb, rc) >= 0.05:
        print("\n  No detectable effect even where the answer is render-only. That would be")
        print("  a surprise and would point at the harness before the models.")

    # cost of carrying the image
    for strat in ("render", "tree"):
        keys = [k for k in shared if stratum.get(k[1]) == strat]
        if not keys:
            continue
        cv = sum(vis[k].get("cost_usd") or 0 for k in keys) / len(keys)
        cn = sum(blind[k].get("cost_usd") or 0 for k in keys) / len(keys)
        tv = st.mean(vis[k]["turns"] for k in keys)
        tn = st.mean(blind[k]["turns"] for k in keys)
        print(f"\n  {strat:<7} cost/task  image ${cv:.4f}  tree ${cn:.4f}  "
              f"({100*(cv-cn)/max(cn,1e-9):+.0f}%)   steps {tv:.1f} vs {tn:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
