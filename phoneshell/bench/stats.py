"""The statistics a benchmark needs to avoid publishing noise as a ranking.

These live in one module because the first version of this project computed
them in three places and published a leaderboard that did none of it. Six
models sat on six percentages, three of them identical, and the page presented
that as an ordering. It was not one: on 24 paired tasks not a single pair of
those models separates at p<0.05.

Two ideas do all the work here.

**A percentage is an estimate, not a measurement.** 20/24 is 83.3%, but the
95% Wilson interval on it runs from 64% to 93%. Publishing the point estimate
alone invites the reader to rank models by differences the data cannot see.

**Models run on the same tasks are a paired design.** Comparing two pass rates
as independent proportions throws away the pairing, which is the only thing
that makes small-n comparison possible at all. McNemar's exact test uses just
the disagreements: the tasks one model passed and the other failed.
"""
from __future__ import annotations

import math

Z95 = 1.959963985


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """95% confidence interval on a pass rate, as percentages.

    Wilson rather than the normal approximation because a benchmark's scores
    cluster near 1.0, where the normal interval runs past 100% and lies about
    the bottom end.
    """
    if n <= 0:
        return (0.0, 0.0)
    ph = k / n
    d = 1 + z * z / n
    centre = (ph + z * z / (2 * n)) / d
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (100 * max(0.0, centre - half), 100 * min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact p for a paired binary comparison.

    b and c are the discordant counts: tasks passed only by the first arm and
    only by the second. Concordant tasks carry no information about which arm
    is better and are correctly ignored, which is why n never appears here.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def min_discordant_for_significance(max_b: int = 64) -> int:
    """How many one-directional disagreements it takes to reach p<0.05.

    The answer is 6, and it does not depend on the size of the task set. A
    benchmark on which no two models disagree six times in the same direction
    cannot rank them however many tasks it contains.
    """
    for b in range(1, max_b + 1):
        if mcnemar_exact(b, 0) < 0.05:
            return b
    return max_b


def required_n(b: int, c: int, n: int, cap: int = 4000) -> int | None:
    """Task count at which this observed split would reach p<0.05.

    The measured discordance rate and direction are held fixed and scaled up,
    which assumes the tasks not yet written resemble the ones already run.
    That is the optimistic assumption, so the result is a lower bound on the
    measurement still owed. None means the disagreements are symmetric and no
    amount of scaling separates the pair.
    """
    if b == c:
        return None
    for size in range(n, cap + 1):
        if mcnemar_exact(round(b * size / n), round(c * size / n)) < 0.05:
            return size
    return None


def paired(results: dict[tuple[str, str], bool], a: str, b: str,
           tasks: set[str]) -> tuple[int, int, float]:
    """Discordant counts and exact p for two models over the same tasks."""
    win_a = sum(1 for t in tasks if results.get((a, t)) and not results.get((b, t)))
    win_b = sum(1 for t in tasks if results.get((b, t)) and not results.get((a, t)))
    return win_a, win_b, mcnemar_exact(win_a, win_b)
