"""What this project has actually cost, from the provider rather than from us.

Summing `cost_usd` across the result rows is the obvious way to report spend and
it under-reports, for a reason worth writing down: **a row is not a receipt.**

Money leaves the account when a request is made. A row is written only when a
request finishes AND survives. So every one of these is spent money with no row
behind it:

  * rows discarded after the fact. A contaminated sweep window was thrown out
    and its 64 requests were re-run; the provider billed both.
  * requests in flight when a run is killed. This rig's sweeps were killed three
    times, and the slower, pricier models are the likeliest to be mid-request.
  * any retry that does not write its own row.

Measured against OpenRouter's own figures the row-sum was low by 2.2x, and most
of that was simply counting the rows that were left instead of the requests that
were made. So this asks the provider.

    ./.venv/bin/python scripts/spend.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def api_key() -> str:
    """Reuse the key already configured for this machine; never store one here."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    cfg = Path.home() / ".claude.json"
    if cfg.exists():
        found = re.search(r"sk-or-v1-[A-Za-z0-9]+", cfg.read_text())
        if found:
            return found.group(0)
    return ""


def get(path: str, key: str) -> dict:
    req = urllib.request.Request(f"https://openrouter.ai/api/v1{path}",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    key = api_key()
    if not key:
        print("no OpenRouter key found; set OPENROUTER_API_KEY")
        return 1
    d = get("/credits", key).get("data", {})
    bought = float(d.get("total_credits") or 0)
    used = float(d.get("total_usage") or 0)
    print(f"  purchased  ${bought:>8.2f}")
    print(f"  used       ${used:>8.2f}")
    print(f"  remaining  ${bought - used:>8.2f}")
    if bought - used < 5:
        print("\n  LOW: a full six-model sweep costs about $25. Top up before starting one.")

    # What the rows claim, so the gap stays visible instead of being forgotten.
    # Only vendor-prefixed slugs went through OpenRouter: a bare model name was
    # driven by the `claude` CLI and billed elsewhere entirely, so counting
    # those here compares one provider's bill against two providers' rows and
    # makes the row-sum look HIGH when it is in fact low.
    rows_total = 0.0
    for f in (ROOT / "runtime" / "bench").glob("*.jsonl*"):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if "/" in str(r.get("model", "")):
                rows_total += r.get("cost_usd") or 0
    print(f"\n  OpenRouter-routed result rows sum to ${rows_total:.2f}")
    if used > rows_total:
        print(f"  the provider is ${used - rows_total:.2f} higher "
              f"({used / max(rows_total, 0.01):.1f}x). Requests without a surviving row: "
              f"discarded windows, kills mid-flight, retries. Trust this figure, not that one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
