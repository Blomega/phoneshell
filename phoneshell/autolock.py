"""Keeping the screen awake for the length of a scan, with permission.

A lock ends a scan. iOS refuses to launch apps from the lock screen, so the run
stops producing screenshots and reports a failure that is really just a phone
doing its job. This project has lost four runs that way.

The remedy is Auto-Lock: Never, and it is a setting on someone's personal phone,
so it is asked for and it is put back. Never changed quietly, never left changed.

Reading it is easier than it looks. The Auto-Lock row on Display & Brightness
carries its own value as secondary text ("Auto-Lock  30 Seconds"), which iOS
folds into the cell's label, so the current setting can be read without opening
the screen at all.
"""
from __future__ import annotations

import logging
import re
import time

from .actions import Phone
from .perception.tree import find_by_text
from .wda.client import WDAError

log = logging.getLogger("phoneshell.autolock")

SETTINGS = "com.apple.Preferences"
PANEL = "Display & Brightness"
ROW = "Auto-Lock"
NEVER = "Never"

# The choices iOS offers. Order matters only for reading them back out.
CHOICES = ("30 Seconds", "1 Minute", "2 Minutes", "3 Minutes", "4 Minutes", "5 Minutes", "Never")

_VALUE = re.compile(r"auto-?lock\W+(.+)$", re.I)


def _open_panel(phone: Phone) -> bool:
    """Settings > Display & Brightness, from wherever we are."""
    res = phone.open_app(SETTINGS)
    if not res.ok:
        return False
    time.sleep(0.6)
    found = phone.scroll_to_text(PANEL, max_swipes=8)
    if not found.ok:
        return False
    snap = phone.snapshot(with_screenshot=False)
    hits = find_by_text(snap.elements, PANEL)
    if not hits:
        return False
    phone.tap_element(hits[0], before=snap)
    time.sleep(0.8)
    return True


def read(phone: Phone) -> str | None:
    """The current Auto-Lock value, or None if it could not be read.

    Does not open the Auto-Lock screen: the value is already written on the row.
    """
    try:
        if not _open_panel(phone):
            return None
        phone.scroll_to_text(ROW, max_swipes=6)
        snap = phone.snapshot(with_screenshot=False)
        for e in snap.elements:
            text = " ".join([e.text] + list(e.children_text)).strip()
            if not text.lower().startswith("auto"):
                continue
            match = _VALUE.search(text)
            if match:
                value = match.group(1).strip(" ,>›")
                for choice in CHOICES:
                    if choice.lower() in value.lower():
                        return choice
                return value[:24] or None
        return None
    except WDAError as exc:
        log.debug("read auto-lock: %s", exc)
        return None


def set_to(phone: Phone, value: str) -> bool:
    """Set Auto-Lock, and verify it took rather than trusting the tap."""
    try:
        if not _open_panel(phone):
            return False
        phone.scroll_to_text(ROW, max_swipes=6)
        snap = phone.snapshot(with_screenshot=False)
        hits = [e for e in snap.elements
                if " ".join([e.text] + list(e.children_text)).lower().startswith("auto")]
        if not hits:
            return False
        phone.tap_element(hits[0], before=snap)
        time.sleep(0.8)

        snap = phone.snapshot(with_screenshot=False)
        rows = find_by_text(snap.elements, value, clickable_only=False)
        rows = [e for e in rows if e.text.strip().lower() == value.lower()] or rows
        if not rows:
            return False
        phone.tap_element(rows[0], before=snap)
        time.sleep(0.7)
        # Verified, because a tap that reports success and changes nothing is the
        # normal failure in this stack, not the unusual one.
        return (read(phone) or "").lower() == value.lower()
    except WDAError as exc:
        log.debug("set auto-lock: %s", exc)
        return False


class KeepAwake:
    """Hold Auto-Lock at Never for the length of a scan, then put it back.

    A context manager because the restore has to happen on every exit, including
    the crash and the stop button. Leaving someone's phone set to never sleep
    because a scan raised would be a rude thing to do with their battery.
    """

    def __init__(self, phone: Phone, emit=None) -> None:
        self.phone = phone
        self.previous: str | None = None
        self.applied = False
        self._emit = emit or (lambda *a, **k: None)

    def __enter__(self) -> "KeepAwake":
        self.previous = read(self.phone)
        if self.previous is None:
            self._emit("I could not read the Auto-Lock setting, so I left it alone.")
            return self
        if self.previous.lower() == NEVER.lower():
            self._emit("Auto-Lock is already off, so there is nothing to change.")
            return self
        self.applied = set_to(self.phone, NEVER)
        if self.applied:
            self._emit(f"Auto-Lock was **{self.previous}**; I have set it to Never for this scan "
                       f"and will put it back when I finish.")
        else:
            self._emit(f"I could not change Auto-Lock, so the phone may lock itself after "
                       f"{self.previous}. The scan will stop if it does.")
        return self

    def __exit__(self, *exc) -> None:
        if not self.applied or not self.previous:
            return
        ok = set_to(self.phone, self.previous)
        self._emit(f"Auto-Lock is back to **{self.previous}**." if ok else
                   f"I could not put Auto-Lock back to {self.previous}. It is still set to Never, "
                   f"so change it in Settings > Display & Brightness when you get a moment.")
        self.applied = False
