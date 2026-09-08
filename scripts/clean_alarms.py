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


def survey(phone, tries: int = 5):
    """Every alarm row: label, whether its switch is on, and where it is.

    Reads taken while the list is still animating a deletion come back SHORT and
    say nothing about it: one returned 631 cells with only 413 switches, another
    returned nothing at all, and the loop concluded it was finished. Settled, the
    same screen returns 631 and 631 every time. There is exactly one switch per
    alarm row, so demanding the two counts agree is a cheap, exact test of
    whether this read can be trusted.
    """
    for attempt in range(tries):
        # An empty read is not "no alarms", it is "the Clock app is not in front"
        # -- the owner picking the phone up looks exactly like a finished job.
        raw = flatten(phone.wda.source())
        switches = [e for e in raw if e.type == "Switch" and e.label]
        cells = [e for e in raw if e.type == "Cell" and e.h > 60 and e.text.strip()]
        if cells and len(cells) == len(switches):
            on = {e.label.strip(): e.value in ("1", "true", "True") for e in switches}
            return [(c.text.strip(), on.get(c.text.strip(), True), c) for c in cells]
        time.sleep(0.8)
    return []                      # caller treats an unreadable screen as "do nothing"


def is_artifact(label: str, on: bool) -> bool:
    return bool(ARTIFACT.match(label)) and not on


def open_alarm_list(phone) -> bool:
    """Get to Clock > Alarms, and say so honestly if we could not.

    The script used to assume the list was already open and silently found zero
    alarms when it was not, then reported "0 artifacts, 0 to keep" and stopped,
    which looks exactly like a clean phone. A cleaner that cannot see the thing
    it cleans must refuse to start, not report success.
    """
    if phone.wda.active_app_info().get("bundleId") != "com.apple.mobiletimer":
        phone.open_app("Clock")
        time.sleep(2.2)
    tab = [e for e in flatten(phone.wda.source())
           if e.type == "Button" and (e.text or "").strip() == "Alarms"]
    if tab:
        phone.wda.tap_w3c(tab[0].cx, tab[0].cy)
        time.sleep(2.0)
    for _ in range(6):
        if survey(phone):
            return True
        time.sleep(1.2)
    return False


def main() -> int:
    phone = Phone()
    geo = phone.wda.geometry()
    if not open_alarm_list(phone):
        print("STOP: could not reach the alarm list; refusing to run blind", flush=True)
        return 1
    rows = survey(phone)
    keepers = {l for l, on, _ in rows if not is_artifact(l, on)}
    total = len(rows)
    print(f"{total} alarms: {total - len(keepers)} artifacts, {len(keepers)} to keep", flush=True)
    for k in sorted(keepers):
        print(f"  keep {k[:60]}", flush=True)

    budget = total - len(keepers)
    deleted = 0
    stalls = 0
    started = time.time()
    print(f"ceiling: this run will not delete more than {budget} alarms", flush=True)
    while True:
        rows = survey(phone)
        # No global "are the keepers still there" check. /source truncates
        # POSITIONALLY at this list length -- it returns the rows near the
        # current scroll offset and drops the rest -- so a keeper far from the
        # viewport is absent from every read, and three reads agreeing means
        # nothing. What protects the owner's alarms is the per-row rule below,
        # which needs only the rows actually on screen: delete a row solely if
        # its label is exactly "H:MM(AM|PM), Alarm" and its switch reads off,
        # where an unknown switch counts as on and is kept. Plus a hard ceiling
        # on how many deletions this run may ever perform.
        if deleted >= budget:
            print(f"STOP: hit the {budget}-delete ceiling", flush=True)
            return 1
        # y below 110 sits under the nav bar and a swipe there does nothing at
        # all, silently: four "deletes" at y=80 changed not one row.
        targets = [e for l, on, e in rows
                   if is_artifact(l, on) and 110 <= e.cy <= geo.point_h - 140]
        if not rows:
            print("unreadable screen, waiting", flush=True)
            time.sleep(2.0)
            continue
        remaining = [l for l, on, _ in rows if is_artifact(l, on)]
        if not remaining:
            print(f"done: {deleted} deleted, {len(rows)} alarms left, "
                  f"{(time.time()-started)/60:.1f} min", flush=True)
            return 0
        if not targets:
            # Losing the screen is not the same as reaching the end of the list.
            # If something else took the phone (a person, or another script), keep
            # scrolling an unrelated app forever achieves nothing: check we are
            # still on the alarm list and walk back to it if not.
            if phone.wda.active_app_info().get("bundleId") != "com.apple.mobiletimer":
                print("lost the alarm list, navigating back", flush=True)
                if not open_alarm_list(phone):
                    print("STOP: cannot get back to the alarm list", flush=True)
                    return 1
                continue
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
        # Start the swipe at 65% across, NOT 92%. The alarm's toggle sits at
        # about 88% and a swipe beginning on top of it is taken as a tap on the
        # switch: forty "deletes" removed nothing and turned nine alarms ON,
        # several of them in the small hours. The row's own frame says the
        # switch is at x=0 width=63, which is nowhere near where it is drawn, so
        # the tree cannot be used to find it. 65% is clear of the toggle and
        # still leaves enough travel for the swipe to register.
        phone.wda.drag(geo.point_w * 0.65, row.cy,
                       geo.point_w * 0.05, row.cy, duration=0.22)
        time.sleep(0.9)            # let the row-removal animation finish, or the next read is short
        deleted += 1
        if deleted % 20 == 0:
            print(f"  {deleted} deleted, {len(remaining) - 1} artifacts left, "
                  f"{(time.time()-started)/60:.1f} min", flush=True)

if __name__ == "__main__":
    raise SystemExit(main())
