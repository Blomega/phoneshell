"""Stamp the real hardware model onto result rows that recorded only "iphone".

WebDriverAgent answers `"device": "iphone"` on every iPhone ever built, so runs
recorded before the harness learned to ask usbmux carry an OS version and no
usable model. That is enough to tell two phones apart only by coincidence, and
a published page should name the instrument outright.

This resolves the model from the row's own device_udid against the phones
currently attached, and rewrites only rows whose model is generic. Rows with a
real model, or with no udid to resolve, are left exactly as they are: a
backfill that guesses is worse than a blank.

    ./.venv/bin/python scripts/backfill_device.py xv2
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoneshell.bench.runner import PRODUCT_NAMES     # noqa: E402

RESULTS = ROOT / "runtime" / "bench"
GENERIC = {"", "iphone", "iPhone"}


def attached() -> dict[str, str]:
    """udid -> marketing name, for every phone on the cable right now."""
    try:
        out = subprocess.run(["pymobiledevice3", "usbmux", "list"],
                             capture_output=True, text=True, timeout=20).stdout
        devices = json.loads(out or "[]")
    except Exception as exc:
        print(f"  could not list devices: {exc}")
        return {}
    found = {}
    for d in devices:
        udid = str(d.get("UniqueDeviceID") or "")
        pt = str(d.get("ProductType") or "")
        if udid and pt:
            found[udid] = PRODUCT_NAMES.get(pt, pt)
    return found


def main() -> int:
    runs = sys.argv[1:]
    if not runs:
        print("usage: backfill_device.py <run> [run ...]")
        return 1
    known = attached()
    if not known:
        print("no phone attached, nothing can be resolved")
        return 1
    for udid, name in known.items():
        print(f"  attached: ...{udid[-6:]} is a {name}")

    for run in runs:
        path = RESULTS / f"{run}.jsonl"
        if not path.exists():
            print(f"  {run}: no such run")
            continue
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        changed = skipped = 0
        for r in rows:
            if str(r.get("device_model") or "") not in GENERIC:
                continue
            name = known.get(str(r.get("device_udid") or ""))
            if name:
                r["device_model"] = name
                changed += 1
            else:
                skipped += 1
        if changed:
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        print(f"  {run}: {changed} rows stamped, {skipped} left blank "
              f"(no udid, or a phone that is not attached)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
