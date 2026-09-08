"""Remove the alarms the benchmark left behind, and nothing else.

Runs saved an alarm every time an agent opened Add Alarm and confirmed it, and
they accumulated to 640. That is not just clutter: with the list that long a
single accessibility read of the Clock app costs 2.6s, and 24s with the Add
Alarm sheet open, which is what made clock.alarm.set_time time out. The
measurements had decayed the thing being measured.

An alarm is an artifact only if its label is exactly "H:MM(AM|PM), Alarm" (no
name, no repeat schedule) AND its switch is off. Everything else is the owner's.
Before every single delete this re-reads the phone, re-identifies the row by
label, and refuses to touch it unless it still matches. After every delete it
checks that all the keepers are still present, and stops the moment one is not.
"""
from __future__ import annotations
import re, sys, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from phoneshell.actions import Phone
from phoneshell.perception.tree import flatten

ARTIFACT = re.compile(r"^\d{1,2}:\d{2}(AM|PM), Alarm$")


def survey(phone):
    """Every alarm row: label, whether its switch is on, and where it is."""
    raw = flatten(phone.wda.source())
    switch_on = {}
    for e in raw:
        if e.type == "Switch" and e.label:
            switch_on[e.label.strip()] = e.value in ("1", "true", "True")
    rows = []
    for e in raw:
        if e.type == "Cell" and e.h > 60 and e.text.strip():
            label = e.text.strip()
            rows.append((label, switch_on.get(label, True), e))  # unknown switch -> assume on -> keep
    return rows


def is_artifact(label: str, on: bool) -> bool:
    return bool(ARTIFACT.match(label)) and not on


def main() -> int:
    phone = Phone()
    geo = phone.wda.geometry()
    rows = survey(phone)
    keepers = {l for l, on, _ in rows if not is_artifact(l, on)}
    total = len(rows)
    print(f"{total} alarms: {total - len(keepers)} artifacts, {len(keepers)} to keep", flush=True)
    for k in sorted(keepers):
        print(f"  keep {k[:60]}", flush=True)

    deleted = 0
    stalls = 0
    started = time.time()
    while True:
        rows = survey(phone)
        # At this list length /source comes back INCOMPLETE: one read returned
        # 631 cells but only 413 switches. So a keeper "missing" from a single
        # read means the read was short, not that the alarm is gone -- the first
        # version of this guard reported all twenty keepers vanishing at once.
        # Only believe it when three reads in a row agree.
        present = {l for l, _, _ in rows}
        missing = keepers - present
        if missing:
            confirmed = missing
            for _ in range(2):
                time.sleep(1.5)
                confirmed &= (keepers - {l for l, _, _ in survey(phone)})
                if not confirmed:
                    break
            if confirmed:
                print(f"STOP: a kept alarm really is gone: {sorted(confirmed)}", flush=True)
                return 1
        # y below 110 sits under the nav bar and a swipe there does nothing at
        # all, silently: four "deletes" at y=80 changed not one row.
        targets = [e for l, on, e in rows
                   if is_artifact(l, on) and 110 <= e.cy <= geo.point_h - 140]
        remaining = [l for l, on, _ in rows if is_artifact(l, on)]
        if not remaining:
            print(f"done: {deleted} deleted, {len(rows)} alarms left, "
                  f"{(time.time()-started)/60:.1f} min", flush=True)
            return 0
        if not targets:
            phone.wda.drag(geo.point_w / 2, geo.point_h * 0.75,
                           geo.point_w / 2, geo.point_h * 0.30, duration=0.25)
            time.sleep(0.8)
            stalls += 1
            if stalls > 25:
                print(f"STOP: {len(remaining)} artifacts left but none reachable", flush=True)
                return 1
            continue
        stalls = 0
        # Exactly ONE delete per read. Batching looked safe -- removing a row
        # only shifts the rows below it, so targets above should keep the
        # position the read measured -- but iOS re-anchors the list's scroll
        # offset when the content shrinks, so every later delete in a batch
        # lands on a row nobody verified. That cost one of the owner's alarms.
        # A read per delete is slow and it is the only version that is correct.
        row = min(targets, key=lambda e: e.cy)
        phone.wda.drag(geo.point_w * 0.92, row.cy,
                       geo.point_w * 0.08, row.cy, duration=0.22)
        time.sleep(0.35)
        deleted += 1
        if deleted % 20 == 0:
            print(f"  {deleted} deleted, {len(remaining) - 1} artifacts left, "
                  f"{(time.time()-started)/60:.1f} min", flush=True)

if __name__ == "__main__":
    raise SystemExit(main())
