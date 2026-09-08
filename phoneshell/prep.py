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
        found = phone.scroll_to_text(label, max_swipes=8)
        if not found.ok:
            return PrepResult(False, f"could not find {label!r} in Settings")
        snap = phone.snapshot(with_screenshot=False, stable=False)
        hits = find_by_text(snap.elements, label)
        if not hits:
            return PrepResult(False, f"{label!r} vanished before it could be tapped")
        phone.tap_element(hits[0])
        time.sleep(1.0)

    found = phone.scroll_to_text(option, max_swipes=8)
    if not found.ok:
        return PrepResult(False, f"could not find the option {option!r}")
    snap = phone.snapshot(with_screenshot=False, stable=False)
    hits = find_by_text(snap.elements, option)
    if not hits:
        return PrepResult(False, f"{option!r} vanished before it could be tapped")
    phone.tap_element(hits[0])
    time.sleep(1.0)

    marker = verify or option
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = phone.snapshot(with_screenshot=False, stable=False)
        if find_by_text(snap.elements, marker, clickable_only=False):
            return PrepResult(True, f"{' > '.join(path)} set to {option}")
        time.sleep(0.5)
    return PrepResult(False, f"selected {option!r} but could not confirm it stuck")


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
    geo = phone.wda.geometry()
    y = geo.point_h * 0.5
    right, left = geo.point_w * 0.85, geo.point_w * 0.15
    before = phone.wda.screenshot()
    phone.wda.drag(right, y, left, y, duration=0.15)
    time.sleep(1.0)
    moved = visual_difference(before, phone.wda.screenshot())
    phone.wda.drag(left, y, right, y, duration=0.15)
    time.sleep(0.6)
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
