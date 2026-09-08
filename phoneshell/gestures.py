"""Every gesture a finger can make, expressed against WebDriverAgent.

Two mechanisms underneath, chosen per gesture:

* W3C pointer chains (POST /actions). Arbitrary numbers of simultaneous fingers,
  each an independent chain with its own id, synchronised by matching pause
  durations. pointerType MUST be "touch"; WDA rejects mouse and pen outright.
  This is how anything with more than one finger, or a precise path, is built.
* WDA's own gesture endpoints, where XCTest has a native implementation that is
  better than anything synthesised: pinch by scale, rotate by radians, force
  touch by pressure, taps by (numberOfTaps, numberOfTouches).

Coordinates are POINTS throughout, matching the accessibility tree.

Where iOS genuinely refuses, the method says so instead of pretending: a gesture
that silently does nothing is worse than an error, because an agent will repeat
it forever.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

from .wda.client import Geometry, ScreenLocked, WDAClient, WDAError

Direction = Literal["up", "down", "left", "right"]
Edge = Literal["left", "right", "top", "bottom", "top_left", "top_right"]
Speed = Literal["slow", "normal", "fast"]

SPEED_MS = {"slow": 900, "normal": 320, "fast": 110}


class GestureUnsupported(RuntimeError):
    """iOS will not do this from an automation session, and no workaround exists."""


@dataclass
class Path:
    """One finger's journey: where it lands, where it goes, when it lifts."""
    points: Sequence[tuple[float, float]]
    press_ms: int = 0          # pause after touching down
    move_ms: int = 300         # total travel time
    hold_ms: int = 0           # pause before lifting
    start_delay_ms: int = 0    # stagger this finger relative to the others
    steps: int = 8


class Gestures:
    def __init__(self, wda: WDAClient):
        self.wda = wda

    # ------------------------------------------------------------------ helpers

    @property
    def geo(self) -> Geometry:
        return self.wda.geometry()

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        g = self.geo
        return min(max(x, 0), g.point_w - 1), min(max(y, 0), g.point_h - 1)

    def perform(self, paths: Iterable[Path]) -> None:
        """Multi-finger and multi-waypoint paths go through W3C /actions.

        For a plain one-finger, two-point drag this falls through to the native
        endpoint instead, because a moving /actions chain costs ~19s on device
        against 1.4s for /wda/dragfromtoforduration.
        """
        paths = list(paths)
        if len(paths) == 1 and len(paths[0].points) == 2:
            # /wda/dragfromtoforduration's `duration` is the press BEFORE the
            # drag (XCUIElement pressForDuration:thenDragTo:), which is exactly
            # what press_ms means here. The drag ends stationary by nature, so
            # hold_ms has nothing to express and is dropped.
            path = paths[0]
            self.wda.ensure_interactive()
            (x1, y1), (x2, y2) = (self._clamp(*pt) for pt in path.points)
            self.wda.drag(x1, y1, x2, y2, max(path.press_ms, 50) / 1000)
            return
        return self._perform_w3c(paths)

    def _perform_w3c(self, paths: Iterable[Path]) -> None:
        """Run several fingers at once as one synchronised action.

        Gated on liveness first: a locked phone accepts every gesture with a 200
        and does nothing, so without this the whole catalogue silently no-ops and
        an agent reads success.
        """
        self.wda.ensure_interactive()
        chains = []
        for i, path in enumerate(paths):
            pts = [self._clamp(x, y) for x, y in path.points]
            actions: list[dict] = []
            if path.start_delay_ms:
                actions.append({"type": "pause", "duration": path.start_delay_ms})
            x0, y0 = pts[0]
            actions.append({"type": "pointerMove", "duration": 0, "x": round(x0), "y": round(y0)})
            actions.append({"type": "pointerDown", "button": 0})
            if path.press_ms:
                actions.append({"type": "pause", "duration": path.press_ms})
            for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
                per = max(int(path.move_ms / max(len(pts) - 1, 1) / path.steps), 1)
                for s in range(1, path.steps + 1):
                    actions.append({
                        "type": "pointerMove", "duration": per,
                        "x": round(xa + (xb - xa) * s / path.steps),
                        "y": round(ya + (yb - ya) * s / path.steps),
                    })
            if path.hold_ms:
                actions.append({"type": "pause", "duration": path.hold_ms})
            actions.append({"type": "pointerUp", "button": 0})
            chains.append({
                "type": "pointer", "id": f"finger{i + 1}",
                "parameters": {"pointerType": "touch"}, "actions": actions,
            })
        self.wda.actions(chains)

    def _vector(self, direction: Direction, distance: float,
                origin: tuple[float, float] | None = None) -> tuple[float, float, float, float]:
        """`direction` is where the CONTENT goes: 'down' reveals what is below."""
        g = self.geo
        cx, cy = origin or (g.point_w / 2, g.point_h / 2)
        dy = g.point_h * distance / 2
        dx = g.point_w * distance / 2
        return {
            "down": (cx, cy + dy, cx, cy - dy),
            "up": (cx, cy - dy, cx, cy + dy),
            "left": (cx + dx, cy, cx - dx, cy),
            "right": (cx - dx, cy, cx + dx, cy),
        }[direction]

    # -------------------------------------------------------------- basic touch

    def tap(self, x: float, y: float) -> None:
        self.wda.ensure_interactive()
        self.wda.tap_w3c(x, y)

    def double_tap(self, x: float, y: float) -> None:
        self.wda.ensure_interactive()
        self.wda.double_tap(x, y)

    def tap_n(self, x: float, y: float, taps: int = 1, fingers: int = 1) -> None:
        """Triple tap, two-finger tap, three-finger tap: XCTest does these natively."""
        self.wda.post("/wda/tapWithNumberOfTaps",
                      {"x": x, "y": y, "numberOfTaps": taps, "numberOfTouches": fingers})

    def triple_tap(self, x: float, y: float) -> None:
        self.tap_n(x, y, taps=3, fingers=1)

    def two_finger_tap(self, x: float, y: float) -> None:
        self.wda.post("/wda/twoFingerTap", {"x": x, "y": y})

    def three_finger_tap(self, x: float, y: float) -> None:
        self.tap_n(x, y, taps=1, fingers=3)

    def long_press(self, x: float, y: float, seconds: float = 1.0) -> None:
        self.wda.ensure_interactive()
        self.wda.touch_and_hold(x, y, seconds)

    def force_touch(self, x: float, y: float, pressure: float = 1.0, seconds: float = 0.5) -> None:
        """Deep press. Real on Force Touch hardware; on Haptic Touch devices iOS
        treats it as a long press, which is the same affordance."""
        self.wda.post("/wda/forceTouch", {"x": x, "y": y, "pressure": pressure, "duration": seconds})

    # ------------------------------------------------------------ drag & scroll

    def drag(self, x1: float, y1: float, x2: float, y2: float,
             seconds: float = 0.3, hold_first: float = 0.0) -> None:
        self.perform([Path([(x1, y1), (x2, y2)],
                           press_ms=int(hold_first * 1000), move_ms=int(seconds * 1000),
                           hold_ms=250)])

    def flick(self, direction: Direction, distance: float = 0.7) -> None:
        """Fast, with momentum: lift immediately so iOS keeps the content moving."""
        x1, y1, x2, y2 = self._vector(direction, distance)
        self.perform([Path([(x1, y1), (x2, y2)], move_ms=90, hold_ms=0, steps=4)])

    def scroll(self, direction: Direction, distance: float = 0.6, speed: Speed = "normal",
               origin: tuple[float, float] | None = None, momentum: bool = False) -> None:
        """Hold before lifting by default, which stops exactly where told.

        Set momentum=True to lift while still moving, which is what page-flipping
        (home screen pages, photo carousels, story reels) needs: a drag that ends
        stationary just snaps back to the page it started on.
        """
        x1, y1, x2, y2 = self._vector(direction, distance, origin)
        self.perform([Path([(x1, y1), (x2, y2)],
                           press_ms=0 if momentum else 200,
                           move_ms=140 if momentum else SPEED_MS[speed],
                           hold_ms=0 if momentum else 280,
                           steps=4 if momentum else 8)])

    def page(self, direction: Literal["left", "right"]) -> None:
        """Flip to the next or previous page: home screen pages, carousels, reels."""
        self.scroll(direction, distance=0.75, momentum=True)

    def pull_to_refresh(self, top: float | None = None) -> None:
        g = self.geo
        start = top if top is not None else g.point_h * 0.22
        self.perform([Path([(g.point_w / 2, start), (g.point_w / 2, start + g.point_h * 0.45)],
                           press_ms=120, move_ms=520, hold_ms=420)])

    # --------------------------------------------------------- edges and system

    def edge_swipe(self, edge: Edge, distance: float = 0.55) -> None:
        """System gestures live at the very edge, in the few points iOS reserves."""
        g = self.geo
        w, h = g.point_w, g.point_h
        mid_y, mid_x = h / 2, w / 2
        # The top gestures start at y=0, in the very first row of pixels, which
        # is what was measured working on a real iPhone 17 Pro Max: from y=0 the
        # top-left swipe opened Notification Center and the top-right one opened
        # Control Center, repeatably, on an unlocked phone. Whether y=1 also
        # works is untested on a clean unlocked run, so y=0 is simply what is
        # known good rather than a claim that y=1 fails.
        moves = {
            "left": ((0, mid_y), (w * distance, mid_y)),             # back
            "right": ((w - 1, mid_y), (w * (1 - distance), mid_y)),  # forward
            "bottom": ((mid_x, h - 1), (mid_x, h * (1 - distance))), # home
            "top": ((mid_x, 0), (mid_x, h * distance)),              # notifications
            "top_left": ((20, 0), (20, h * distance)),               # notifications
            "top_right": ((w - 20, 0), (w - 20, h * distance)),      # control centre
        }
        (x1, y1), (x2, y2) = moves[edge]
        self.perform([Path([(x1, y1), (x2, y2)], move_ms=320, hold_ms=200)])

    def home(self, verify: bool = True) -> bool:
        """Get to the home screen, and check that it worked.

        POST /wda/homescreen is XCUIDevice.pressButton(.home) and on a real
        iPhone 17 it silently does nothing often enough to strand an agent in the
        app switcher. So: try it, look, then fall back to a HID home press, then
        to the bottom-edge home gesture. Measured on device, the HID press lands
        when the XCTest one does not.
        """
        from .perception.screen import visual_difference

        def on_home() -> bool:
            """Control Center, Notification Center, Spotlight and the app switcher
            are ALL com.apple.springboard, so the bundle id alone says nothing.
            The home screen is the one with a grid of app icons on it."""
            try:
                if str(self.wda.active_app_info().get("bundleId")) != "com.apple.springboard":
                    return False
                from .perception.tree import condense, flatten
                g = self.geo
                els = condense(flatten(self.wda.source()), g.point_w, g.point_h,
                               status_bar_h=g.status_bar_h)
                icons = sum(1 for e in els if e.type == "Icon")
                overlay_words = {"add controls", "clear", "services in use"}
                if any(e.text.strip().lower() in overlay_words for e in els):
                    return False
                return icons >= 4
            except WDAError:
                return False

        attempts = [
            ("pressButton", lambda: self.wda.home()),
            ("hid", lambda: self.wda.perform_io_hid_event(12, 0x40, 0.05)),
            ("edge swipe", lambda: self.perform([Path(
                [(self.geo.point_w / 2, self.geo.point_h - 1),
                 (self.geo.point_w / 2, self.geo.point_h * 0.35)], move_ms=260, hold_ms=0, steps=4)])),
        ]
        for name, action in attempts:
            before = None
            try:
                before = self.wda.screenshot()
            except WDAError:
                pass
            try:
                action()
            except WDAError:
                continue
            if not verify:
                return True
            time.sleep(1.1)
            try:
                moved = before is None or visual_difference(before, self.wda.screenshot()) > 0.02
            except WDAError:
                moved = False
            if on_home() and moved is not False:
                return True
            if on_home():
                return True
        return on_home()

    def app_switcher(self) -> None:
        """Swipe up from the bottom edge and hold, which is what makes iOS show
        the switcher rather than just going home."""
        g = self.geo
        self.perform([Path([(g.point_w / 2, g.point_h - 1), (g.point_w / 2, g.point_h * 0.45)],
                           move_ms=380, hold_ms=900)])

    def control_center(self) -> None:
        self.edge_swipe("top_right")

    def notification_center(self) -> None:
        self.edge_swipe("top_left")

    def close_app_card(self, x: float, y: float) -> None:
        """In the app switcher, flick a card up to kill that app."""
        self.perform([Path([(x, y), (x, self.geo.point_h * 0.1)], move_ms=180, steps=4)])

    def back(self) -> None:
        self.edge_swipe("left", distance=0.6)

    # ------------------------------------------------------------- multi-finger

    def pinch(self, scale: float = 0.5, velocity: float = 1.0) -> None:
        self.wda.ensure_interactive()
        """scale < 1 pinches in (zoom out), > 1 spreads (zoom in). XCTest's own
        implementation, which behaves better than two synthesised fingers."""
        self.wda.post("/wda/pinch", {"scale": scale, "velocity": velocity})

    def zoom_in(self, factor: float = 2.0) -> None:
        self.pinch(scale=factor, velocity=1.5)

    def zoom_out(self, factor: float = 0.5) -> None:
        self.pinch(scale=factor, velocity=-1.0)

    def rotate(self, radians: float = math.pi / 2, velocity: float = 1.0) -> None:
        self.wda.post("/wda/rotate", {"rotation": radians, "velocity": velocity})

    def pinch_manual(self, cx: float, cy: float, start_gap: float, end_gap: float,
                     seconds: float = 0.5) -> None:
        """Two-finger pinch at a chosen point, when the native pinch cannot be
        aimed (it applies to the whole application)."""
        half_s, half_e = start_gap / 2, end_gap / 2
        self.perform([
            Path([(cx - half_s, cy), (cx - half_e, cy)], move_ms=int(seconds * 1000), press_ms=120),
            Path([(cx + half_s, cy), (cx + half_e, cy)], move_ms=int(seconds * 1000), press_ms=120),
        ])

    def two_finger_scroll(self, direction: Direction, distance: float = 0.5, gap: float = 90) -> None:
        x1, y1, x2, y2 = self._vector(direction, distance)
        self.perform([
            Path([(x1 - gap / 2, y1), (x2 - gap / 2, y2)], press_ms=150, move_ms=340, hold_ms=220),
            Path([(x1 + gap / 2, y1), (x2 + gap / 2, y2)], press_ms=150, move_ms=340, hold_ms=220),
        ])

    def n_finger_swipe(self, fingers: int, direction: Direction, distance: float = 0.5,
                       gap: float = 70) -> None:
        """Three fingers for iOS text undo/redo and accessibility, four or five
        for the iPad switcher gestures."""
        x1, y1, x2, y2 = self._vector(direction, distance)
        spread = (fingers - 1) * gap / 2
        self.perform([
            Path([(x1 - spread + i * gap, y1), (x2 - spread + i * gap, y2)],
                 press_ms=140, move_ms=340, hold_ms=220)
            for i in range(fingers)
        ])

    def three_finger_pinch(self, cx: float, cy: float, spread_out: bool = False,
                           gap: float = 110) -> None:
        """iOS copy (pinch in with three fingers) and paste (pinch out)."""
        near, far = (gap, 20) if not spread_out else (20, gap)
        self.perform([
            Path([(cx - near, cy), (cx - far, cy)], press_ms=120, move_ms=300),
            Path([(cx, cy - near / 2), (cx, cy - far / 2)], press_ms=120, move_ms=300),
            Path([(cx + near, cy), (cx + far, cy)], press_ms=120, move_ms=300),
        ])

    def copy_selection(self, cx: float, cy: float) -> None:
        self.three_finger_pinch(cx, cy, spread_out=False)

    def paste_selection(self, cx: float, cy: float) -> None:
        self.three_finger_pinch(cx, cy, spread_out=True)

    def undo(self) -> None:
        """Three-finger swipe left, the modern replacement for shake-to-undo."""
        self.n_finger_swipe(3, "left", distance=0.4)

    def redo(self) -> None:
        self.n_finger_swipe(3, "right", distance=0.4)

    # --------------------------------------------------------------------- text

    def select_word(self, x: float, y: float) -> None:
        self.double_tap(x, y)

    def select_paragraph(self, x: float, y: float) -> None:
        self.triple_tap(x, y)

    def cursor_drag(self, from_x: float, from_y: float, to_x: float, to_y: float) -> None:
        """The keyboard-as-trackpad gesture: press and hold on the keyboard, then
        move. The initial hold is what turns the keys into a trackpad."""
        self.perform([Path([(from_x, from_y), (to_x, to_y)],
                           press_ms=700, move_ms=420, hold_ms=200)])

    def magnify_cursor(self, x: float, y: float, seconds: float = 1.0) -> None:
        self.long_press(x, y, seconds)

    # --------------------------------------------------------------- list rows

    def swipe_row(self, element, direction: Literal["left", "right"] = "left",
                  fraction: float = 0.55) -> None:
        """Reveal a row's actions: delete, archive, reply, mark as read."""
        span = element.w * fraction
        if direction == "left":
            x1, x2 = element.x + element.w - 8, element.x + element.w - 8 - span
        else:
            x1, x2 = element.x + 8, element.x + 8 + span
        self.perform([Path([(x1, element.cy), (x2, element.cy)],
                           press_ms=120, move_ms=300, hold_ms=260)])

    def drag_to_reorder(self, element, to_y: float, hold: float = 0.9) -> None:
        """Long press to pick the row up, then move it. The hold is essential:
        without it iOS reads the gesture as a scroll."""
        self.perform([Path([(element.cx, element.cy), (element.cx, to_y)],
                           press_ms=int(hold * 1000), move_ms=650, hold_ms=420)])

    def long_press_icon(self, element, seconds: float = 1.1) -> None:
        self.long_press(element.cx, element.cy, seconds)

    # ----------------------------------------------------------------- hardware

    # HID usage page 12 (0x0C, consumer page) is where the phone's buttons live.
    HID = {
        "home": (12, 0x40),
        "power": (12, 0x30),
        "lock": (12, 0x30),
        "volume_up": (12, 0xE9),
        "volume_down": (12, 0xEA),
        "mute": (12, 0xE2),
        "snapshot": (12, 0x65),
    }

    def press_hardware(self, name: str, seconds: float = 0.05) -> None:
        if name not in self.HID:
            raise GestureUnsupported(f"no HID mapping for {name!r}; known: {sorted(self.HID)}")
        page, usage = self.HID[name]
        self.wda.perform_io_hid_event(page, usage, seconds)

    def press_button(self, name: str) -> None:
        """XCTest's own button API: home, volumeUp, volumeDown, and on newer
        hardware the Action Button and Camera Control if XCTest exposes them."""
        self.wda.press_button(name)  # type: ignore[arg-type]

    def lock_screen(self) -> None:
        self.wda.lock()

    def wake(self) -> None:
        """Tap-to-wake is a hardware feature; from automation, a HID power press
        is the reliable way to light the screen."""
        self.press_hardware("power")

    def screenshot_chord(self) -> None:
        self.press_hardware("snapshot")

    def siri(self, text: str) -> None:
        """An escape hatch for things with no UI affordance: 'turn on wifi',
        'set brightness to 50%'."""
        self.wda.activate_siri(text)

    # ------------------------------------------------- honestly unsupported

    def shake(self) -> None:
        raise GestureUnsupported(
            "XCUIDevice.shake is not exposed by WebDriverAgent on a physical device. "
            "Use undo() (three-finger swipe left), which is what shake-to-undo triggers."
        )

    def back_tap(self) -> None:
        raise GestureUnsupported(
            "Back Tap is an accessibility feature driven by the device's own accelerometer. "
            "Nothing outside the device can synthesise it. Map the action you wanted to a "
            "normal gesture instead."
        )

    def squeeze(self) -> None:
        raise GestureUnsupported("Squeeze is Android hardware (Pixel Active Edge); no iPhone equivalent.")

    def air_gesture(self) -> None:
        raise GestureUnsupported("Air gestures need the device's own proximity sensors; not synthesisable.")
