"""The verb layer: everything an agent is allowed to do to the phone.

Deliberately small. Each verb is idempotent-ish, reports what it did, and never
silently succeeds when the screen did not change. The agent above this layer
reasons; this layer refuses to guess.
"""
from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from PIL import Image

from .config import Config
from .agent import playbook as _pb
from .agent.observation import Observation, build as build_observation
from .perception.screen import visual_difference, wait_until_stable
from .perception.tree import (
    Element, condense, find_by_text, flatten, screen_signature, to_prompt,
)
from .wda.client import Direction, Geometry, WDAClient, WDAError, WDANoSuchElement

log = logging.getLogger("phoneshell.actions")


def _digits(text: str) -> str:
    """Just the digits, so "05" and "5 min" compare equal to "5"."""
    return "".join(c for c in text if c.isdigit())


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class Snapshot:
    bundle_id: str
    app_name: str
    elements: list[Element]
    geometry: Geometry
    signature: str
    png: bytes | None = None
    alert: dict | None = None
    taken_at: float = 0.0
    settle_ms: int = 0

    def text(self) -> str:
        return to_prompt(self.elements)

    def by_index(self, idx: int) -> Element:
        for e in self.elements:
            if e.idx == idx:
                return e
        raise KeyError(f"no element [{idx}] on this screen")


@dataclass
class ActionResult:
    ok: bool
    action: str
    detail: str = ""
    changed: bool | None = None
    error: str = ""
    data: dict = field(default_factory=dict)


class Phone:
    """A phone you can tell what to do."""

    def __init__(self, cfg: Config | None = None, client: WDAClient | None = None):
        self.cfg = cfg or Config.load()
        self.wda = client or WDAClient(
            base_url=self.cfg.wda_base_url,
            timeout=self.cfg.wda.request_timeout,
            settings=self.cfg.wda.settings,
        )
        if self.cfg.wda.precise_visibility:
            self.wda.DEFAULT_EXCLUDED_ATTRS = self.wda.UNUSED_ATTRS
        from .coexist import Coexistence
        from .gestures import Gestures
        from .memory import AppMemory
        self.gestures = Gestures(self.wda)
        self.memory = AppMemory(enabled=self.cfg.memory.enabled)
        self.coexist = Coexistence(
            self.wda,
            poll=self.cfg.session.yield_poll_seconds,
            grace=self.cfg.session.yield_grace_seconds,
        )
        if self.cfg.session.mode == "shared":
            self.coexist.start()
        self._last_signature: str | None = None
        self._app_catalog: dict[str, str] = {}

    # ------------------------------------------------------------------ sensing

    INTERACTIVE_TYPES = {"Button", "Cell", "Link", "TextField", "SecureTextField",
                         "SearchField", "Icon", "Switch", "Tab", "MenuItem"}

    def ensure_tree_depth(self) -> None:
        """Set the snapshot depth once, deliberately, at a moment when creating a
        session is harmless. Everything after that reads session-free."""
        depth = self.cfg.wda.fast_depth
        try:
            self.wda.set_settings({"snapshotMaxDepth": depth})
            self._tree_depth = depth
        except WDAError as exc:
            log.debug("could not set tree depth: %s", exc)

    def _read_tree(self, geo, deep: bool = False) -> list[Element]:
        """Shallow first, deeper only if the screen looks under-described.

        Depth is the whole cost of a tree read on a heavy app, and most screens
        do not need it. Grab's food list: 0.21s at depth 20 against 1.27s at 25.
        """
        # NEVER change settings on the read path. set_settings needs a SESSION,
        # and creating a WebDriverAgent session switches the app under test,
        # which sends the phone to the home screen. That turned the benchmark's
        # own verification step into the thing that made every task fail: the
        # check navigated the phone away before it could look at it.
        depth = self.cfg.wda.deep_depth if deep else self.cfg.wda.fast_depth
        if deep and getattr(self, "_tree_depth", None) != depth:
            try:
                self.wda.set_settings({"snapshotMaxDepth": depth})
                self._tree_depth = depth
            except WDAError:
                pass
        elements = condense(
            flatten(self.wda.source()), geo.point_w, geo.point_h,
            max_elements=self.cfg.brain.tree_max_elements, status_bar_h=geo.status_bar_h,
        )
        if deep:
            return elements
        interactive = sum(1 for e in elements if e.type in self.INTERACTIVE_TYPES)
        if interactive < self.cfg.wda.min_interactive_before_deepening:
            log.debug("only %d interactive elements at depth %d, reading deeper",
                      interactive, depth)
            return self._read_tree(geo, deep=True)
        return elements

    def snapshot(
        self,
        with_screenshot: bool = True,
        stable: bool = True,
        max_wait: float = 3.5,
        fast: bool = False,
        deep: bool = False,
    ) -> Snapshot:
        """Read the screen, after it has stopped moving.

        iOS animates every transition, and a tree read mid-animation is a
        half-built screen: measured here, a home-screen read 1.0s after pressing
        home returned 6 elements and the same read at 2.0s returned 12. Polling a
        64x64 hash of the framebuffer costs ~0.2s a probe against ~0.9s for a
        full accessibility snapshot, so we settle on pixels and read the tree once.
        """
        geo = self.wda.geometry()
        png = None
        settle_ms = 0
        if stable:
            # A screen this app has shown before settles the way it settled last
            # time, so the poll can be tighter. On a new screen, take the time.
            interval = 0.12 if fast else 0.3
            png, report = wait_until_stable(
                self.wda.screenshot, max_wait=1.4 if fast else max_wait, interval=interval,
            )
            settle_ms = int(report.waited * 1000)
            if not report.stable:
                log.debug("screen still moving after %.1fs", report.waited)
        try:
            info = self.wda.active_app_info()
        except WDAError:
            info = {}
        elements = self._read_tree(geo, deep=deep)
        # Asking WebDriverAgent for the alert text costs 1.36s on a heavy screen
        # and almost always returns nothing, because it hunts for an alert across
        # the whole hierarchy. The tree we already have says whether one is on
        # screen, so only pay for the details when it is.
        alert = None
        if any(e.type in {"Alert", "Dialog"} for e in elements):
            alert_text = self.wda.alert_text()
            if alert_text:
                alert = {"text": alert_text, "buttons": self.wda.alert_buttons()}
        if png is None and with_screenshot:
            png = self.wda.screenshot()
        bundle_id = str(info.get("bundleId") or "")
        snap = Snapshot(
            bundle_id=bundle_id,
            app_name=str(info.get("name") or "") or self.name_for_bundle(bundle_id),
            elements=elements,
            geometry=geo,
            signature=screen_signature(elements),
            png=png,
            alert=alert,
            taken_at=time.time(),
        )
        return snap

    def screen_changed(self, before: Snapshot, after: Snapshot) -> bool:
        return before.signature != after.signature or before.bundle_id != after.bundle_id

    # ------------------------------------------------------------------- acting

    def _after(self, action: str, detail: str, before: Snapshot | None, settle: float = 0.6) -> ActionResult:
        """Report whether the action moved the screen.

        Compared against a fresh accessibility snapshot this costs a screenshot
        instead of a tree read, which on a real phone is 0.5s against 2s, and the
        answer to "did anything happen" is a pixel question anyway.
        """
        time.sleep(settle)
        changed = None
        if before is not None and before.png is not None:
            try:
                changed = visual_difference(before.png, self.wda.screenshot()) > 0.02
            except WDAError:
                changed = None
        elif before is not None:
            changed = self.screen_changed(before, self.snapshot(with_screenshot=False))
        return ActionResult(ok=True, action=action, detail=detail, changed=changed)

    def tap_element(self, element: Element, before: Snapshot | None = None) -> ActionResult:
        """Tap the centre of an element. We tap coordinates rather than resolving
        an element handle: handles go stale the instant the tree re-renders, and
        a coordinate derived from the same snapshot the model reasoned about is
        exactly what the model intended."""
        x, y = element.cx, element.cy
        # Clamp inside the screen with a small inset so we never hit the bezel.
        geo = self.wda.geometry()
        x = min(max(x, 2), geo.point_w - 2)
        y = min(max(y, 2), geo.point_h - 2)
        self.wda.tap_w3c(x, y)
        return self._after("tap", f"[{element.idx}] {element.type} {element.text!r} at ({int(x)},{int(y)})", before)

    def tap_point(self, x: float, y: float, before: Snapshot | None = None) -> ActionResult:
        self.wda.tap_w3c(x, y)
        return self._after("tap_point", f"({int(x)},{int(y)})", before)

    def long_press(self, element: Element, duration: float = 1.0, before: Snapshot | None = None) -> ActionResult:
        self.wda.touch_and_hold(element.cx, element.cy, duration)
        return self._after("long_press", f"[{element.idx}] {element.text!r}", before)

    def type_text(
        self,
        text: str,
        into: Element | None = None,
        submit: bool = False,
        clear_first: bool = False,
        before: Snapshot | None = None,
    ) -> ActionResult:
        """Focus a field if given, then type. Typing goes to whatever holds focus,
        so the tap and the keystrokes have to be one operation to be reliable."""
        if into is not None:
            handle = self.find_handle(into)
            if handle:
                # /element/:id/value taps to acquire focus itself and injects the
                # whole string through the HID text-input path, emoji included.
                try:
                    if clear_first:
                        self.wda.clear_element(handle)
                    self.wda.type_into_element(handle, text)
                    if submit:
                        self.wda.type_text("\n")
                    typed = self._after("type", f"{text!r} into [{into.idx}]", before, settle=0.8)
                    return self._confirm_typed(text, typed, submit)
                except WDAError as exc:
                    log.debug("element value path failed (%s), falling back to tap+keys", exc)
            self.wda.tap_w3c(into.cx, into.cy)
            time.sleep(0.45)
        if clear_first:
            try:
                active = self.wda.active_element()
                if active:
                    self.wda.clear_element(active)
            except WDAError as exc:
                log.debug("clear failed, continuing: %s", exc)
        payload = text + ("\n" if submit else "")
        self.wda.type_text(payload)
        result = self._after("type", f"{text!r}{' + return' if submit else ''}", before, settle=0.8)
        return self._confirm_typed(text, result, submit)

    def _confirm_typed(self, text: str, result: ActionResult, submit: bool) -> ActionResult:
        """Say plainly whether the typed text actually reached a field.

        Keystrokes go to whatever holds keyboard focus, and when nothing does
        they are dropped in silence. The keyboard still animates in, so the pixel
        diff reports "something changed" and the agent believes it typed.
        Measured on Reminders that produced an empty reminder and a confident
        report that the text had been saved: the run passed the foreground check
        and failed on the text that was never there. The tree is the only witness
        that can tell those two apart, and one read is cheaper than a lost task.
        """
        if submit:
            return result          # submitting usually navigates away from the text
        probe = text.strip()[:24].lower()
        if not probe:
            return result
        try:
            raw = flatten(self.wda.source())
        except WDAError:
            return result          # cannot tell, so do not cry wolf
        if any(e.type == "SecureTextField" for e in raw):
            return result          # a password field masks what it holds
        for e in raw:
            for shown in (e.value, e.label, e.name):
                if shown and probe in shown.lower():
                    return result
        result.detail += " -- NOT on screen afterwards: the field almost certainly"\
                         " never had focus. Tap the field and type again before"\
                         " assuming this worked."
        result.ok = False
        return result

    # ------------------------------------------------------------------ pickers

    # XCTest's own number: the tap lands 20% of the wheel's height from centre.
    PICKER_TAP_OFFSET = 0.2

    def picker_wheels(self) -> list[Element]:
        """Every picker wheel on screen, ordered left to right.

        Read from the RAW tree. condense() keeps elements that carry text worth
        acting on, and a wheel's text is whatever row it is showing, so a wheel
        parked on "0" looks like noise and gets dropped. Date, time, timer,
        duration and unit pickers are all this one control, so finding it here
        covers Clock, Calendar, Alarms, Health and every form asking for a date
        of birth.
        """
        # /source comes back empty occasionally, mid-animation. Seen once in
        # about thirty reads on a wheel that was plainly on screen, and it aborted
        # the whole adjustment, so give it a second chance before believing it.
        for attempt in (0, 1):
            try:
                raw = flatten(self.wda.source())
            except WDAError:
                return []
            wheels = [e for e in raw if e.type == "PickerWheel" and e.w > 0 and e.h > 0]
            if wheels or attempt:
                return sorted(wheels, key=lambda e: e.x)
            time.sleep(0.25)
        return []

    def set_picker(
        self,
        value: str,
        wheel: int = 0,
        before: Snapshot | None = None,
        max_attempts: int = 40,
    ) -> ActionResult:
        """Turn a picker wheel until it reads `value`.

        XCTest moves a wheel by TAPPING a point offset from its centre, not by
        dragging it: a wheel steps by whole rows per tap beside the selected row,
        where a drag carries momentum and lands somewhere approximate. That is
        why swiping at a timer never converges.

        WebDriverAgent taps at a fixed 20% of the wheel's height and never checks
        what that lands on. Measured on the iOS Clock timer wheel, 292pt tall,
        that offset moves **two** rows per tap, and a two-row step cannot reach
        an even value from an odd one: asking for 20 minutes walked 40 taps from
        5 to 35 and could never have arrived. So calibrate first. One probe tap
        measures the row height, every tap after it moves exactly one row, and
        the distance becomes arithmetic: fire that many taps and read once at the
        end rather than paying a tree read per row.
        """
        want = value.strip()
        wheels = self.picker_wheels()
        if not wheels:
            return ActionResult(ok=False, action="set_picker",
                                detail="no picker wheel on this screen", changed=False)
        if not 0 <= wheel < len(wheels):
            shown = ", ".join(f"{i}={w.value!r}" for i, w in enumerate(wheels))
            return ActionResult(
                ok=False, action="set_picker", changed=False,
                detail=f"no wheel {wheel}; this screen has {len(wheels)}: {shown}")

        target = wheels[wheel]
        cx, cy, height = target.cx, target.cy, target.h

        def read() -> str:
            now = self.picker_wheels()
            return now[wheel].value.strip() if wheel < len(now) else ""

        def matches(got: str) -> bool:
            if got == want:
                return True
            gd, wd = _digits(got), _digits(want)
            if gd and wd:
                # Numbers decide on their own. Falling through to a substring
                # test here matched "5" against "15 min" and stopped the wheel
                # ten rows early while reporting success.
                return gd.lstrip("0") == wd.lstrip("0")
            return bool(want) and want.lower() in got.lower()

        def tap(fraction: float, down: bool) -> None:
            # The tap costs 550ms round trip, measured, and the row is readable
            # the moment it returns, so there is nothing to sleep for.
            self.wda.tap_w3c(cx, cy + (fraction if down else -fraction) * height)

        def rows_between(a: str, b: str) -> int | None:
            da, db = _digits(a), _digits(b)
            if not da or not db:
                return None
            return int(db) - int(da)

        current = read()
        if matches(current):
            return self._after("set_picker", f"wheel {wheel} already reads {current!r}",
                               before, settle=0.1)

        # ---- calibrate: find the offset that moves this wheel exactly one row.
        step = None
        taps = 0
        for probe in (0.10, 0.16, 0.24, 0.34):
            tap(probe, True)
            taps += 1
            moved = read()
            if moved == current:
                continue                      # too small to leave the current row
            jump = rows_between(current, moved)
            current = moved
            if jump is None or not 1 <= abs(jump) <= 8:
                step = probe                  # unlabelled or wrapped: take it as one row
            else:
                step = probe / abs(jump)
            break
        if step is None:
            return ActionResult(ok=False, action="set_picker", changed=False,
                                detail=f"wheel {wheel} would not move; taps are missing it")
        if matches(current):
            return self._after("set_picker",
                               f"wheel {wheel} reads {current!r} after {taps} tap(s)",
                               before, settle=0.1)

        # ---- which way is down? one calibrated tap answers it and moves us on.
        was = current
        tap(step, True)
        taps += 1
        current = read()
        per_tap = rows_between(was, current)
        down = True
        if current == was:
            tap(step, False)
            taps += 1
            current = read()
            per_tap = rows_between(was, current)
            down = False
            if current == was:
                return ActionResult(ok=False, action="set_picker", changed=False,
                                    detail=f"wheel {wheel} is stuck on {current!r}")
        if matches(current):
            return self._after("set_picker",
                               f"wheel {wheel} reads {current!r} after {taps} tap(s)",
                               before, settle=0.1)

        # ---- cover the distance. A tap costs 550ms and a read costs 154ms, both
        # measured, so the expensive thing is taps, not looking. For anything far
        # away, drag: one drag carries about nine rows in 1.4s where nine taps
        # cost five seconds. Dragging the finger down moves the wheel the way
        # tapping ABOVE centre does, hence the inverted direction below. A drag
        # spanning more than about half the wheel's height stops grabbing it at
        # all (measured: 20 rows moved nothing), so each one is clamped and
        # repeated rather than scaled up.
        row_px = step * height
        for _ in range(8):
            distance = rows_between(current, want)
            if distance is None or per_tap in (None, 0):
                break
            rows = round(distance / per_tap)
            if abs(rows) <= 5:
                break
            span = max(-8, min(8, rows))
            dy = -span * row_px if down else span * row_px
            self.wda.drag(cx, cy - dy / 2, cx, cy + dy / 2, duration=0.3)
            time.sleep(0.5)
            moved = read()
            if moved == current:
                break                      # the drag did not take; fall back to taps
            current = moved
            if matches(current):
                return self._after("set_picker", f"wheel {wheel} reads {current!r} "
                                   f"after {taps} tap(s) and a drag", before, settle=0.1)

        # ---- stride: the remaining rows are arithmetic, so read once at the end.
        distance = rows_between(current, want)
        if per_tap and distance is not None:
            rows = round(distance / per_tap)
            direction = down if rows > 0 else not down
            for _ in range(min(abs(rows), 60)):
                tap(step, direction)
                taps += 1
            current = read()
            if matches(current):
                return self._after(
                    "set_picker", f"wheel {wheel} reads {current!r} after {taps} tap(s)",
                    before, settle=0.1)
            down = direction

        # ---- closed loop for the rest: unlabelled rows, or a stride that missed.
        seen: set[str] = set()
        flipped = False
        while taps < max_attempts:
            if matches(current):
                return self._after(
                    "set_picker", f"wheel {wheel} reads {current!r} after {taps} tap(s)",
                    before, settle=0.1)
            # Once we are close, walk towards the value rather than guessing.
            gap = rows_between(current, want)
            if gap is not None and per_tap:
                down = (gap > 0) == (per_tap > 0)
            seen.add(current)
            tap(step, down)
            taps += 1
            nxt = read()
            if nxt == current:
                if flipped:
                    return ActionResult(
                        ok=False, action="set_picker", changed=False,
                        detail=f"wheel {wheel} stops at {current!r}; {want!r} is not on it")
                down, flipped, seen = not down, True, set()
            elif nxt in seen and gap is None:
                return ActionResult(
                    ok=False, action="set_picker", changed=False,
                    detail=f"wheel {wheel} cycled {len(seen)} rows without reaching {want!r}")
            current = nxt
        return ActionResult(ok=False, action="set_picker", changed=False,
                            detail=f"wheel {wheel} still reads {current!r} after {taps} taps")

    def press_key(self, key: Literal["return", "delete", "tab"], before: Snapshot | None = None) -> ActionResult:
        mapping = {"return": "\n", "delete": "\b", "tab": "\t"}
        self.wda.type_text(mapping[key])
        return self._after("press_key", key, before)

    SCROLLABLE_TYPES = {"ScrollView", "Table", "CollectionView", "Sheet", "TextView"}

    def _scrollable_area(self, hint: Element | None = None) -> Element | None:
        """The largest scrollable container that is not the whole screen.

        A sheet over a map is the case that matters: it is smaller than the
        screen and it is the only thing a swipe should touch.
        """
        # Read the RAW tree: condense() drops scroll containers as layout
        # scaffolding, so they never appear in the element list the agent sees.
        try:
            raw = flatten(self.wda.source())
        except WDAError:
            return None
        geo = self.wda.geometry()
        screen_area = geo.point_w * geo.point_h
        def visible_area(e) -> float:
            """Area of the part that is actually on screen. A container can sit
            mostly below the fold, and dragging inside it would land off-screen."""
            w = max(0.0, min(e.x + e.w, geo.point_w) - max(e.x, 0.0))
            h = max(0.0, min(e.y + e.h, geo.point_h) - max(e.y, 0.0))
            return w * h

        candidates = []
        for e in raw:
            if e.type not in self.SCROLLABLE_TYPES:
                continue
            vis = visible_area(e)
            if vis < screen_area * 0.12 or vis > screen_area * 0.92:
                continue
            if min(e.y + e.h, geo.point_h) - max(e.y, 0.0) < 120:
                continue
            candidates.append((vis, e))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        best = candidates[0][1]
        # Clamp the container to its visible part on both axes so every point of
        # the drag lands on screen.
        x0, y0 = max(best.x, 0.0), max(best.y, 0.0)
        x1 = min(best.x + best.w, geo.point_w)
        y1 = min(best.y + best.h, geo.point_h)
        best.x, best.y = x0, y0
        best.w, best.h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
        return best

    def swipe(
        self,
        direction: Direction,
        distance: float = 0.6,
        speed: Literal["slow", "normal", "fast"] = "normal",
        before: Snapshot | None = None,
        within: Element | None = None,
    ) -> ActionResult:
        """Content-scrolling swipe through the middle of the screen.

        Note the inversion: to see content further DOWN the page, the finger
        moves UP. `direction` here means the direction the content travels, i.e.
        `down` shows you what is below.
        """
        geo = self.wda.geometry()
        # Swipe inside the thing that actually scrolls. A drag through the middle
        # of the screen pans the map on a Maps-style screen instead of scrolling
        # the results sheet sitting on top of it, which looks to an agent like
        # "scrolling does nothing" and sends it hunting for workarounds.
        area = self._scrollable_area(within) if within is None else within
        if area is None:
            cx, cy = geo.point_w / 2, geo.point_h / 2
            span_y = geo.point_h * distance / 2
            span_x = geo.point_w * distance / 2
        else:
            cx, cy = area.cx, area.cy
            span_y = area.h * distance / 2
            span_x = area.w * distance / 2
        moves = {
            "down": (cx, cy + span_y, cx, cy - span_y),
            "up": (cx, cy - span_y, cx, cy + span_y),
            "left": (cx + span_x, cy, cx - span_x, cy),
            "right": (cx - span_x, cy, cx + span_x, cy),
        }
        x1, y1, x2, y2 = moves[direction]
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)):
            return ActionResult(ok=False, action="swipe",
                                error="refusing to swipe: the screen reported a non-finite rect")
        # Keep the whole gesture inside the container, with an inset so the drag
        # does not start on the sheet's grab handle or its rounded edge.
        if area is not None:
            top, bottom = area.y + 12, area.y + area.h - 12
            y1 = min(max(y1, top), bottom)
            y2 = min(max(y2, top), bottom)
        seconds = {"slow": 0.9, "normal": 0.35, "fast": 0.12}[speed]
        # Native drag, not a W3C chain. Measured on device: a moving /actions
        # chain is a flat ~19s while /wda/dragfromtoforduration is 1.4s for the
        # identical gesture, and that difference is most of what made every step
        # feel slow. /wda/scroll's
        # directional branch works without a scrollable ancestor, but it scrolls
        # by a normalized fraction of the element with no control over where the
        # finger lands, and it measured 7.4s against the drag's 1.4s.
        self.wda.drag(x1, y1, x2, y2, seconds)
        return self._after("swipe", f"{direction} {int(distance*100)}%", before, settle=0.7)

    def scroll_to_text(self, needle: str, max_swipes: int = 8,
                       direction: Direction = "down") -> ActionResult:
        """Swipe until the text shows up, looking BOTH ways.

        Searching one direction only is a trap: a list left part-scrolled from a
        previous visit hides the target above the fold, and a downward-only
        search walks to the end of the list and reports "not found" for something
        that was three swipes up. This tries the asked-for direction first, then
        reverses past the starting point.

        The snapshots here skip the stability gate and the screenshot: this loop
        only needs text, and that takes it from ~1.0s a look to ~0.4s.
        """
        def look() -> tuple[bool, int | None, str]:
            snap = self.snapshot(with_screenshot=False, stable=False)
            hits = find_by_text(snap.elements, needle, clickable_only=False)
            return bool(hits), (hits[0].idx if hits else None), snap.signature

        found, idx, _ = look()
        if found:
            return ActionResult(ok=True, action="scroll_to_text",
                                detail=f"{needle!r} was already on screen", data={"index": idx})

        opposite: Direction = {"down": "up", "up": "down",
                               "left": "right", "right": "left"}[direction]
        swipes = 0
        for leg, (way, budget) in enumerate((
            (direction, max_swipes // 2 or 1),
            (opposite, max_swipes),
        )):
            last_sig = None
            for _ in range(budget):
                self.swipe(way)
                swipes += 1
                found, idx, sig = look()
                if found:
                    return ActionResult(
                        ok=True, action="scroll_to_text",
                        detail=f"found {needle!r} after {swipes} swipes ({way})",
                        data={"index": idx},
                    )
                if sig == last_sig:
                    break   # end of the list this way, turn around
                last_sig = sig
        return ActionResult(
            ok=False, action="scroll_to_text",
            error=f"{needle!r} is not on this screen: swiped {swipes} times both ways",
        )

    def back(self, before: Snapshot | None = None) -> ActionResult:
        """iOS has no back button. Try the nav bar, then the edge swipe."""
        snap = self.snapshot(with_screenshot=False)
        for e in snap.elements:
            if e.type in {"Button", "Link"} and e.text.strip().lower() in {"back", "cancel", "done", "close", "‹", "<"}:
                self.wda.tap(e.cx, e.cy)
                return self._after("back", f"tapped {e.text!r}", before)
        geo = self.wda.geometry()
        self.wda.drag(2, geo.point_h * 0.5, geo.point_w * 0.6, geo.point_h * 0.5, 0.25)
        return self._after("back", "edge swipe", before)

    def home(self, before: Snapshot | None = None) -> ActionResult:
        """Verified: the plain press does nothing often enough to strand an agent."""
        ok = self.gestures.home(verify=True)
        res = self._after("home", "home screen" if ok else "could not reach the home screen",
                          before, settle=0.6)
        res.ok = ok
        if not ok:
            res.error = ("the phone did not go to the home screen: the home press, a HID home "
                         "event and the bottom-edge gesture were all tried")
        return res

    def open_app(self, target: str, before: Snapshot | None = None) -> ActionResult:
        """Accepts a bundle id or a human app name.

        Verified, not assumed. WebDriverAgent returns success for activate and
        launch whether or not the app actually comes forward, and on a device
        whose automation session has wedged it never does. An agent told
        "opened Grab" when nothing opened will keep trying variations forever,
        which is exactly the loop this prevents.
        """
        bundle = target if "." in target and " " not in target else self.resolve_app(target)
        if not bundle:
            return ActionResult(ok=False, action="open_app", error=f"no installed app matches {target!r}")
        if bundle in self.cfg.safety.denied_bundle_ids:
            return ActionResult(ok=False, action="open_app", error=f"{bundle} is on the deny list")

        # launchUnattached first: it is session-free, so it cannot make WDA
        # switch its app-under-test, which is what used to bounce the phone back
        # to the home screen a couple of seconds after every launch.
        for attempt, call in enumerate(
            (self.wda.launch_unattached, self.wda.activate_app, self.wda.launch_app)
        ):
            try:
                call(bundle)
            except WDAError as exc:
                log.debug("%s failed: %s", call.__name__, exc)
            if self.wda.wait_until(
                lambda: str(self.wda.active_app_info().get("bundleId")) == bundle,
                timeout=4.0, interval=0.4,
            ):
                res = self._after("open_app", bundle, before, settle=0.8)
                res.data["bundleId"] = bundle
                return res

        try:
            if self.wda.is_locked():
                return ActionResult(
                    ok=False, action="open_app",
                    error=("the phone is locked, so iOS refused to launch the app "
                           "(FBSOpenApplicationErrorDomain code 7). Unlock it, or store the "
                           "passcode with `phoneshell set-passcode` so the bridge can."),
                    data={"bundleId": bundle, "locked": True, "blocked": True},
                )
        except WDAError:
            pass
        front = str(self.wda.active_app_info().get("bundleId", "?"))
        try:
            state = {0: "unknown", 1: "not running", 2: "background (suspended)",
                     3: "background (running)", 4: "foreground"}.get(self.wda.app_state(bundle), "?")
        except WDAError:
            state = "?"
        return ActionResult(
            ok=False, action="open_app",
            error=(f"{bundle} did not come to the foreground. It is {state} and "
                   f"{front} is still in front. Both activate and launch were tried. "
                   "This is a device-level block, not a wrong bundle id: retrying, deep "
                   "links and tapping the icon will all fail the same way until the "
                   "phone's automation session is reset."),
            data={"bundleId": bundle, "front": front, "app_state": state, "blocked": True},
        )

    def open_url(self, url: str, before: Snapshot | None = None) -> ActionResult:
        """Deep links are the fastest, least fragile way into a known screen.

        Also verified: a URL open reports success even when no app handles the
        scheme, and even when the device refuses to foreground anything.
        """
        front_before = str(self.wda.active_app_info().get("bundleId", ""))
        shot_before = None
        try:
            shot_before = self.wda.screenshot()
        except WDAError:
            pass
        self.wda.open_url(url)
        time.sleep(1.6)
        front_after = str(self.wda.active_app_info().get("bundleId", ""))
        moved = front_after != front_before
        if not moved and shot_before is not None:
            try:
                moved = visual_difference(shot_before, self.wda.screenshot()) > 0.02
            except WDAError:
                moved = False
        if moved:
            return ActionResult(ok=True, action="open_url", detail=url, changed=True)
        return ActionResult(
            ok=False, action="open_url",
            error=(f"opening {url} changed nothing: still on {front_after or 'the same screen'}. "
                   "Either no installed app handles that scheme, or the phone is refusing to "
                   "bring apps forward. Do not retry variations of the same link."),
            changed=False, data={"front": front_after, "url": url},
        )

    def accept_alert(self, button: str | None = None, before: Snapshot | None = None) -> ActionResult:
        self.wda.accept_alert(button)
        return self._after("accept_alert", button or "default", before)

    def dismiss_alert(self, button: str | None = None, before: Snapshot | None = None) -> ActionResult:
        self.wda.dismiss_alert(button)
        return self._after("dismiss_alert", button or "default", before)

    def wait_for_text(self, needle: str, timeout: float = 15.0) -> ActionResult:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.snapshot(with_screenshot=False)
            hits = find_by_text(snap.elements, needle, clickable_only=False)
            if hits:
                return ActionResult(ok=True, action="wait_for_text", detail=f"{needle!r} appeared",
                                    data={"index": hits[0].idx})
            time.sleep(0.6)
        return ActionResult(ok=False, action="wait_for_text", error=f"{needle!r} did not appear in {timeout}s")

    def find_handle(self, element: Element) -> str | None:
        """Resolve a snapshot element to a live WDA element handle.

        Handles go stale the moment the tree re-renders, so we never cache them;
        this is called at the point of use and thrown away immediately after.
        """
        attempts: list[tuple[str, str]] = []
        if element.identifier:
            attempts.append(("accessibility id", element.identifier))
        if element.name:
            attempts.append(("accessibility id", element.name))
        if element.label:
            attempts.append((
                "predicate string",
                f'type == "XCUIElementType{element.type}" AND label == "{_escape(element.label)}"',
            ))
        attempts.append(("class chain", f"**/XCUIElementType{element.type}"))
        for using, value in attempts:
            try:
                handle = self.wda.find_element_or_none(using, value)
            except WDAError:
                continue
            if handle:
                return handle
        return None

    # -------------------------------------------------------------------- lock

    def passcode(self) -> str | None:
        """Keychain first, config second. Nothing else reads this."""
        from .secrets import get_secret
        account = self.cfg.device.udid or "default"
        return (get_secret(account) or (self.cfg.device.passcode or "")).strip() or None

    def ensure_unlocked(self, timeout: float = 12.0) -> ActionResult:
        """Wake the phone and get past the lock screen if we are allowed to.

        A locked phone is the single most confusing state for an agent: reads
        still work, the tree is full of digits and notification rows, and every
        tap does nothing useful. Better to say so plainly.
        """
        if not self.wda.is_locked():
            return ActionResult(ok=True, action="unlock", detail="already unlocked")
        try:
            self.wda.unlock()
        except WDAError:
            pass
        time.sleep(1.2)
        if not self.wda.is_locked():
            return ActionResult(ok=True, action="unlock", detail="woken, no passcode needed")

        code = self.passcode()
        if not code:
            return ActionResult(
                ok=False, action="unlock",
                error=("the phone is locked and no passcode is stored. Run "
                       "`phoneshell set-passcode` on the Mac to save it in the Keychain, or "
                       "unlock the phone by hand. For a phone that stays docked, "
                       "Settings > Display & Brightness > Auto-Lock > Never also helps."),
                data={"locked": True},
            )
        snap = self.snapshot(with_screenshot=False)
        keys = {e.text.strip(): e for e in snap.elements if e.text.strip().isdigit()}
        if not keys:
            return ActionResult(ok=False, action="unlock",
                                error="the passcode keypad is not on screen", data={"locked": True})
        for digit in code:
            key = keys.get(digit)
            if key is None:
                return ActionResult(ok=False, action="unlock",
                                    error=f"no key for digit {digit} on the keypad")
            self.wda.tap_w3c(key.cx, key.cy)
            time.sleep(0.18)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.wda.is_locked():
                return ActionResult(ok=True, action="unlock", detail="unlocked with the stored passcode")
            time.sleep(0.5)
        return ActionResult(ok=False, action="unlock", error="the passcode did not unlock the phone",
                            data={"locked": True})

    # -------------------------------------------------------------- observation

    def observe(self, step: int = 0, force_som: bool = False, include_image: bool = True) -> Observation:
        # Cheap (43ms) look at which app is in front, so a screen this app has
        # shown before can use the tighter settle poll from the start.
        fast = False
        if self.cfg.memory.enabled and self.cfg.memory.fast_settle_on_known:
            try:
                front = str(self.wda.active_app_info().get("bundleId") or "")
                fast = bool(front) and bool(self.memory._load(front))
            except WDAError:
                fast = False
        snap = self.snapshot(with_screenshot=True, fast=fast)
        locked = False
        try:
            locked = self.wda.is_locked()
        except WDAError:
            pass

        # What do we already know about this screen?
        remembered = None
        hint = ""
        if self.cfg.memory.enabled and not locked:
            try:
                remembered = self.memory.see(snap.bundle_id, snap.elements, settle_ms=snap.settle_ms)
                hint = self.memory.hint(remembered, snap.elements)
                self.memory.save(snap.bundle_id)
            except Exception as exc:  # memory must never break perception
                log.debug("memory skipped: %s", exc)
        known = bool(remembered and remembered.seen >= self.cfg.memory.min_visits_to_trust)
        if known and self.cfg.memory.skip_image_on_known and not force_som:
            include_image = False
        self._last_png = snap.png
        obs = build_observation(
            step=step,
            bundle_id=snap.bundle_id,
            app_name=snap.app_name,
            elements=snap.elements,
            png=snap.png,
            scale=snap.geometry.scale,
            screen_w=snap.geometry.point_w,
            screen_h=snap.geometry.point_h,
            alert=snap.alert,
            signature=snap.signature,
            max_edge=self.cfg.brain.screenshot_max_edge,
            force_som=force_som,
            include_image=include_image,
        )
        if hint:
            obs.notes.append(hint)
        # If something is covering the screen, say so and say what closes it,
        # rather than letting the model narrate its way around the problem.
        try:
            from .agent import overlays as _ov
            found = _ov.detect(snap.elements, snap.geometry.point_w, snap.geometry.point_h,
                               snap.alert)
            if found is not None and found.kind != "system_alert":
                shape = _ov.classify(found, snap.geometry.point_h)
                entry = next((e for e in _pb.PLAYBOOK if e.kind == shape), None)
                first = entry.moves[0] if entry else "call phone_dismiss_popup"
                obs.notes.append(
                    f"SOMETHING IS COVERING THE SCREEN: a {shape}. "
                    f"Call phone_dismiss_popup, which will {first}. "
                    "Do not go hunting for the close button yourself."
                )
        except Exception:
            pass
        if known and not obs.image_b64:
            obs.notes.append(
                "the picture is omitted because this screen is already understood; "
                "ask for phone_observe(force_marks=true) if you need to see it"
            )
        if locked:
            obs.notes.insert(0, (
                "THE PHONE IS LOCKED. Nothing you tap will do anything useful until it is "
                "unlocked. Stop and tell the user to unlock it."
            ))
        return obs

    # ------------------------------------------------------------------ catalog

    def name_for_bundle(self, bundle_id: str) -> str:
        """Human name for a bundle id, so observations never say app=?."""
        if not bundle_id:
            return ""
        if bundle_id == "com.apple.springboard":
            return "Home Screen"
        if not self._app_catalog:
            try:
                self.load_catalog()
            except Exception:  # catalog is a convenience, never a blocker
                return bundle_id.rsplit(".", 1)[-1]
        for label, bundle in self._app_catalog.items():
            if bundle == bundle_id and not label.islower():
                return label
        return bundle_id.rsplit(".", 1)[-1]

    def resolve_app(self, name: str) -> str | None:
        """Human name to bundle id.

        Curated aliases win over display names, because iOS ships several apps
        with the same visible name: asking for "Settings" and matching display
        names first lands on com.apple.CarPlaySettings, which launches nothing
        useful. Verified on a real device.
        """
        from .apps import COMMON_ALIASES
        n = name.strip().lower()
        if n in COMMON_ALIASES:
            return COMMON_ALIASES[n]
        if not self._app_catalog:
            self.load_catalog()
        exact = [b for label, b in self._app_catalog.items() if label.strip().lower() == n]
        if exact:
            # Prefer a real user app over an Apple internal of the same name.
            non_apple = [b for b in exact if not b.startswith("com.apple.")]
            return (non_apple or exact)[0]
        partial = sorted(
            ((label, b) for label, b in self._app_catalog.items() if n in label.strip().lower()),
            key=lambda kv: (kv[1].startswith("com.apple."), len(kv[0])),
        )
        return partial[0][1] if partial else None

    def load_catalog(self, catalog: dict[str, str] | None = None) -> dict[str, str]:
        if catalog:
            self._app_catalog = catalog
            return catalog
        from .apps import installed_apps  # local import to avoid a cycle
        self._app_catalog = installed_apps(self.cfg)
        return self._app_catalog

    # ------------------------------------------------------------------- images

    @staticmethod
    def to_image(png: bytes) -> Image.Image:
        return Image.open(io.BytesIO(png)).convert("RGB")
