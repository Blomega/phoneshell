"""Putting the phone into a state where a long unattended run can survive.

Written as a general "navigate Settings and choose an option" routine rather
than a one-off, because every system setting an agent rig depends on has the
same shape: open Settings, reach a page, pick a value, verify it stuck.

Auto-Lock is the one that matters most. A benchmark run that outlives the
auto-lock timer loses the phone, and every task after that point fails
identically for reasons that have nothing to do with the model. Relying on the
human having set it correctly is not good enough: it was set to Never on this
device and the phone still locked mid-run.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .perception.tree import find_by_text
from .wda.client import WDAError

log = logging.getLogger("phoneshell.prep")


@dataclass
class PrepResult:
    ok: bool
    detail: str = ""
    # Set when continuing would measure nothing: the run must stop.
    fatal: bool = False


def _row_is_ticked(phone, label: str) -> bool:
    """Is `label`'s row the chosen one on an iOS single-select list?

    Read from the RAW tree, because the evidence is a child element that
    condense() drops: the selected row holds a Button labelled "checkmark" and
    the others do not. Nothing else distinguishes them, so matching the option's
    text alone confirms a setting that was never applied.
    """
    from .perception.tree import flatten
    try:
        raw = flatten(phone.wda.source())
    except WDAError:
        return False
    rows = [e for e in raw if e.type == "Cell"
            and label.lower() in (e.label or e.name or "").lower()]
    if not rows:
        return False
    ticks = [e for e in raw if "checkmark" in ((e.label or "") + (e.name or "")).lower()]
    for row in rows:
        for t in ticks:
            if row.y <= t.cy <= row.y + row.h:
                return True
    return False


def _wait_for_new_screen(phone, previous: str, timeout: float = 3.0) -> bool:
    """Block until the screen is no longer the one whose signature was passed."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.35)
        try:
            if phone.snapshot(with_screenshot=False, stable=False).signature != previous:
                time.sleep(0.35)          # let the push animation land
                return True
        except WDAError:
            pass
    return False


def choose_setting(phone, path: list[str], option: str, verify: str | None = None,
                   timeout: float = 12.0) -> PrepResult:
    """Walk Settings down `path` and select `option`.

    Each hop scrolls to find its label first, because iOS Settings remembers
    where it was left and the target is as often above the fold as below it.
    """
    phone.wda.terminate_app("com.apple.Preferences")
    time.sleep(0.8)
    result = phone.open_app("Settings")
    if not result.ok:
        return PrepResult(False, f"could not open Settings: {result.error}")
    time.sleep(1.0)

    for label in path:
        # Each hop verifies it actually moved. A tap on a Settings row does not
        # always take, and when it does not the walk carries on searching the
        # screen it never left, then reports the CHILD row missing: "could not
        # find Auto-Lock in Settings" on a phone where Auto-Lock is one swipe
        # below Display & Brightness. Silently failing to set Auto-Lock is what
        # let the phone lock in the middle of a benchmark run.
        for attempt in range(3):
            found = phone.scroll_to_text(label, max_swipes=8)
            if not found.ok:
                return PrepResult(False, f"could not find {label!r} in Settings")
            # stable=True: scroll_to_text has just set this list moving, and a
            # coordinate read while it is still travelling points at whichever
            # row has slid into that spot by the time the tap lands.
            snap = phone.snapshot(with_screenshot=False, stable=True)
            hits = find_by_text(snap.elements, label)
            if not hits:
                return PrepResult(False, f"{label!r} vanished before it could be tapped")
            was = snap.signature
            phone.tap_element(hits[0])
            if _wait_for_new_screen(phone, was):
                break
        else:
            return PrepResult(False, f"tapping {label!r} never opened it")

    found = phone.scroll_to_text(option, max_swipes=8)
    if not found.ok:
        return PrepResult(False, f"could not find the option {option!r}")
    snap = phone.snapshot(with_screenshot=False, stable=True)
    hits = find_by_text(snap.elements, option)
    if not hits:
        return PrepResult(False, f"{option!r} vanished before it could be tapped")
    was = snap.signature
    phone.tap_element(hits[0])
    _wait_for_new_screen(phone, was, timeout=2.0)

    # Confirming by searching the page for the option's own label proves nothing:
    # on Auto-Lock the row "Never" is listed whether or not it is the chosen
    # value, so this reported success every time and the phone kept locking
    # mid-run. iOS exposes no selection attribute at all here (no traits, no
    # value, no isSelected), but it does hang a Button labelled "checkmark"
    # inside the chosen row and nowhere else. That is the evidence.
    marker = verify or option
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _row_is_ticked(phone, marker):
            return PrepResult(True, f"{' > '.join(path)} set to {option}")
        if verify:
            # An explicit marker is a different string from the option, so its
            # presence anywhere IS the evidence the caller asked for.
            snap = phone.snapshot(with_screenshot=False, stable=False)
            if find_by_text(snap.elements, marker, clickable_only=False):
                return PrepResult(True, f"{' > '.join(path)} set to {option}")
        time.sleep(0.5)
    return PrepResult(False,
                      f"tapped {option!r} but no checkmark sits on that row")


def disable_auto_lock(phone) -> PrepResult:
    """Settings > Display & Brightness > Auto-Lock > Never."""
    return choose_setting(phone, ["Display & Brightness", "Auto-Lock"], "Never")


def check_input_reaches_screen(phone) -> PrepResult:
    """Confirm a gesture actually moves the phone before spending a run on it.

    The phone can reach a state where reads are perfect and writes are silently
    discarded, and nothing in the reply distinguishes it from a working device.
    Measured here it cost a whole benchmark tail. A swipe out and back is a
    second, and it turns an expensive mystery into one honest line.
    """
    from .perception.screen import visual_difference
    # The home screen first: a full-width swipe through a list row is the
    # destructive row action, and this probe ran on whatever was in front.
    try:
        phone.home()
        time.sleep(1.0)
    except WDAError:
        pass
    geo = phone.wda.geometry()
    y = geo.point_h * 0.5
    right, left = geo.point_w * 0.85, geo.point_w * 0.15
    # Try BOTH directions and take the best. One direction is not guaranteed to
    # move anything: on the last home-screen page a leftward swipe rubber-bands
    # straight back, and reporting that as "gestures are being dropped" is a
    # false alarm on a perfectly healthy phone.
    moved = 0.0
    for from_x, to_x in ((right, left), (left, right)):
        before = phone.wda.screenshot()
        phone.wda.drag(from_x, y, to_x, y, duration=0.15)
        time.sleep(1.0)
        moved = max(moved, visual_difference(before, phone.wda.screenshot()))
        phone.wda.drag(to_x, y, from_x, y, duration=0.15)
        time.sleep(0.6)
        if moved > 0.01:
            break
    if moved > 0.01:
        return PrepResult(True, f"gestures reach the screen ({moved:.1%} moved)")
    return PrepResult(False, "gestures are being DROPPED: a full-width swipe moved 0% of "
                             "the screen. The phone's HID event system is wedged and only a "
                             "reboot clears it. Nothing run now would mean anything.",
                      fatal=True)


def prepare_for_run(phone) -> list[PrepResult]:
    """Everything that has to be true before an unattended run starts."""
    results = []
    try:
        results.append(disable_auto_lock(phone))
    except WDAError as exc:
        results.append(PrepResult(False, f"could not set auto-lock: {exc}"))
    try:
        phone.home()
    except WDAError:
        pass
    try:
        results.append(check_input_reaches_screen(phone))
    except WDAError as exc:
        results.append(PrepResult(False, f"could not test input: {exc}"))
    return results
