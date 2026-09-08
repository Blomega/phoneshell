"""Getting past promos, interstitials and onboarding, without the model's help.

This is the single most common way a phone agent stalls. It opens an app, an
unrelated promo sheet covers the screen, and the model burns its whole budget
narrating "a popup appeared, let me find a way to close it". The screen is not a
reasoning problem: dismissing a sheet is a reflex with maybe six known moves, and
it belongs in the harness where it costs nothing and cannot loop.

Deliberate boundaries:
  * permission dialogs are NEVER auto-answered, because "Allow location" is the
    user's decision, not ours
  * anything that reads as money, sending or deleting is never tapped
  * the overlay must be verified gone; a dismissal that did nothing reports that
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from ..perception.tree import Element, find_by_text, flatten
from ..wda.client import WDAError
from . import playbook

log = logging.getLogger("phoneshell.overlays")

# Ordered by how safe and how likely they are. Labels first, because a real close
# control is always better than a guessed gesture.
CLOSE_LABELS = [
    "not now", "no thanks", "no, thanks", "maybe later", "later", "skip",
    "skip for now", "dismiss", "close", "not interested", "no thanks!",
    "got it", "ok, got it", "continue", "done", "×", "✕", "✖", "x",
]
# Accessibility identifiers and SF Symbol names real apps use for the X control.
CLOSE_IDENTIFIERS = [
    "xmark", "xmark.circle", "xmark.circle.fill", "close", "closebutton",
    "close_button", "btn_close", "dismiss", "dismissbutton", "cancelbutton",
    "modal-close", "popup_close", "icclose", "ic_close",
]
# Never tapped by the reflex, whatever else matches.
NEVER = re.compile(
    r"(?i)\b(pay|buy|order|checkout|purchase|subscribe|confirm|send|delete|remove|"
    r"allow|don'?t allow|sign\s*out|log\s*out|agree|accept)\b"
)
# Types iOS uses for something presented over the current screen.
SHEET_TYPES = {"Sheet", "Popover", "Alert", "Dialog"}

# Words that mark a sheet as promotional rather than part of the task.
PROMO_HINTS = re.compile(
    r"(?i)(promo|offer|deal|discount|voucher|rate us|rate this|review|"
    r"notification|subscribe|premium|upgrade|try free|welcome|what'?s new|"
    r"tips|tour|introducing|new feature|invite|refer)"
)


@dataclass
class Overlay:
    kind: str                 # "system_alert" | "sheet" | "fullscreen" | "banner"
    container: Element | None
    text: str
    close_candidates: list[Element] = field(default_factory=list)
    promotional: bool = False
    grabber: Element | None = None

    def describe(self) -> str:
        return f"{self.kind}: {self.text[:110]!r}"


@dataclass
class DismissResult:
    dismissed: bool
    how: str = ""
    overlay: Overlay | None = None
    note: str = ""


def _is_close_control(e: Element) -> bool:
    text = " ".join([e.label, e.name, e.value]).strip().lower()
    ident = e.identifier.lower()
    if NEVER.search(text):
        return False
    if any(ident == c or c in ident for c in CLOSE_IDENTIFIERS):
        return True
    if text in CLOSE_LABELS:
        return True
    # A tiny square button in a corner with no text is almost always the X.
    if e.type in {"Button", "Image", "Icon"} and not text and 16 <= e.w <= 60 and 16 <= e.h <= 60:
        return True
    return False


def find_grabber(raw: list[Element], sheet: Element) -> Element | None:
    """The little horizontal bar at the top of an iOS sheet.

    It is the handle: an iOS sheet is dismissed by dragging it down, and tapping
    it does nothing. It is almost never in the condensed element list, because it
    has no label and is not interactive, so this searches the raw tree. Typical
    shape is 30-70pt wide, 3-8pt tall, centred, within ~24pt of the sheet's top.
    """
    best = None
    for e in raw:
        if not (22 <= e.w <= 90 and 2 <= e.h <= 10):
            continue
        if abs(e.cx - sheet.cx) > sheet.w * 0.18:
            continue
        if not (sheet.y - 6 <= e.y <= sheet.y + 30):
            continue
        if best is None or e.y < best.y:
            best = e
    return best


def classify(overlay: "Overlay", screen_h: float) -> str:
    """Name the shape, so the right page of the playbook is used."""
    text = overlay.text.lower()
    if overlay.kind == "system_alert":
        if any(w in text for w in ("rate", "review", "enjoying")):
            return "rating or review prompt"
        if any(w in text for w in ("update", "new version")):
            return "update nag"
        return "system alert"
    if overlay.container is None:
        return "full-screen modal"
    c = overlay.container
    if c.h >= screen_h * 0.85:
        if any(w in text for w in ("per month", "free trial", "subscribe", "upgrade", "/mo")):
            return "paywall or upsell"
        if not overlay.close_candidates and not text.strip():
            return "interstitial advert"
        return "full-screen modal"
    if overlay.grabber is not None:
        return "bottom sheet with a grabber"
    if c.y + c.h >= screen_h - 12:
        return "action sheet" if "cancel" in text else "bottom sheet with a grabber"
    return "popover with an arrow"


def detect(elements: list[Element], screen_w: float, screen_h: float,
           alert: dict | None = None, raw: list[Element] | None = None) -> Overlay | None:
    """Is something covering the screen that the task did not ask for?

    The giveaway is not size, it is that a sheet STARTS PART-WAY DOWN. Picking
    the largest container instead selects the app's own full-screen window every
    time, which is how this used to miss the iOS share sheet (a Popover at
    y=478) and Grab's promo sheet alike.

    Container hunting runs on the RAW tree, because condense() deliberately drops
    unlabelled containers as scaffolding, which throws away the very element we
    are looking for.
    """
    if alert:
        return Overlay(kind="system_alert", container=None, text=alert.get("text", ""),
                       promotional=False)

    pool = raw if raw else elements
    screen_area = screen_w * screen_h
    typed, positional = [], []
    for e in pool:
        bottom = e.y + e.h
        if e.type in SHEET_TYPES:
            typed.append(e)
        elif (
            e.type in {"Other", "ScrollView", "WebView", "Table", "CollectionView"}
            and e.area > screen_area * 0.2
            and e.y >= 40                       # a sheet never starts at the top
            and bottom >= screen_h * 0.7        # and reaches well down the screen
            and e.w >= screen_w * 0.6
        ):
            positional.append(e)

    if not typed and not positional:
        return None
    # A typed Sheet/Popover beats a guess; otherwise take the outermost panel,
    # then let the duplicate-rect ones collapse to it.
    candidates = typed or positional
    candidates.sort(key=lambda e: (e.y, -e.area))
    container = candidates[0]

    inside = [
        e for e in elements
        if e.y >= container.y - 8 and e.y + e.h <= container.y + container.h + 8
    ]
    text = " ".join(e.text for e in inside if e.text)[:400]
    closes = [e for e in inside if _is_close_control(e)]
    if container.area > screen_area * 0.85 and container.y < 40:
        kind = "fullscreen"
    elif container.type in {"Alert", "Dialog"}:
        kind = "system_alert"
    else:
        kind = "sheet"
    grabber = find_grabber(raw, container) if raw else None
    return Overlay(
        kind=kind, container=container, text=text,
        close_candidates=closes, promotional=bool(PROMO_HINTS.search(text)),
        grabber=grabber,
    )


def dismiss(phone, max_attempts: int = 4) -> DismissResult:
    """Work through the playbook for whatever shape is in the way."""
    snap = phone.snapshot(with_screenshot=True)
    try:
        raw = flatten(phone.wda.source())
    except WDAError:
        raw = None
    overlay = detect(snap.elements, snap.geometry.point_w, snap.geometry.point_h, snap.alert, raw=raw)
    if overlay is None:
        return DismissResult(False, note="nothing is covering the screen")

    if overlay.kind == "system_alert":
        return DismissResult(
            False, overlay=overlay,
            note=("this is a system dialog and the harness will not answer it: "
                  "permissions and confirmations are the user's call"),
        )

    shape = classify(overlay, snap.geometry.point_h)
    before_sig = snap.signature

    def gone() -> bool:
        time.sleep(0.9)
        after = phone.snapshot(with_screenshot=False)
        if after.signature == before_sig:
            return False
        again = detect(after.elements, after.geometry.point_w, after.geometry.point_h, after.alert)
        return again is None or (overlay.container is not None and again.container is not None
                                 and abs(again.container.area - overlay.container.area) > 1000)

    # 1. a real close control
    for candidate in overlay.close_candidates[:3]:
        try:
            phone.tap_element(candidate)
        except WDAError as exc:
            log.debug("close tap failed: %s", exc)
            continue
        if gone():
            return DismissResult(True, how=f"tapped {candidate.text or 'the close button'!r}",
                                 overlay=overlay)

    # 2. a labelled way out anywhere on screen
    for label in CLOSE_LABELS[:8]:
        hits = [e for e in find_by_text(snap.elements, label) if not NEVER.search(e.text)]
        if not hits:
            continue
        try:
            phone.tap_element(hits[0])
        except WDAError:
            continue
        if gone():
            return DismissResult(True, how=f"tapped {hits[0].text!r}", overlay=overlay)

    # 2b. a Popover is dismissed by tapping outside it; that is its contract.
    if overlay.container is not None and overlay.container.type == "Popover":
        c = overlay.container
        target_y = max(c.y / 2, 40) if c.y > 80 else min(c.y + c.h + 40, snap.geometry.point_h - 20)
        try:
            phone.wda.tap_w3c(snap.geometry.point_w / 2, target_y)
        except WDAError:
            pass
        if gone():
            return DismissResult(True, how="tapped outside the popover", overlay=overlay)

    # 3. drag the sheet down. Starting ON the grabber is what actually works:
    # it is the handle iOS gives you, and a drag from the sheet's body often
    # scrolls the sheet's content instead of moving the sheet.
    if overlay.container is not None and overlay.kind != "fullscreen":
        c = overlay.container
        bottom = snap.geometry.point_h - 6
        starts = []
        if overlay.grabber is not None:
            starts.append((overlay.grabber.cx, overlay.grabber.cy, "the grabber"))
        # The grabber is usually decorative and absent from the tree, so aim
        # where it physically is: just inside the sheet's top edge.
        starts.append((c.cx, max(c.y + 8, 30), "just inside the sheet's top edge"))
        starts.append((c.cx, max(c.y + 26, 40), "a little further into the sheet"))
        for x, y, where in starts:
            try:
                # Native drag: a W3C chain that moves costs ~19s on device.
                phone.wda.drag(x, y, x, bottom, 0.08)
            except WDAError:
                continue
            if gone():
                return DismissResult(True, how=f"dragged the sheet down from {where}",
                                     overlay=overlay)

    # 4. tap the dimmed backdrop above the sheet
    if overlay.container is not None and overlay.container.y > 60:
        try:
            phone.wda.tap_w3c(snap.geometry.point_w / 2, max(overlay.container.y / 2, 30))
        except WDAError:
            pass
        if gone():
            return DismissResult(True, how="tapped the backdrop", overlay=overlay)

    # 5. the back edge swipe
    try:
        phone.back()
    except WDAError:
        pass
    if gone():
        return DismissResult(True, how="swiped back from the edge", overlay=overlay)

    # 6. interstitials hide their X for a few seconds; look again before giving up.
    if shape == "interstitial advert":
        time.sleep(3.0)
        again = dismiss_once_by_label(phone)
        if again:
            return DismissResult(True, how="waited for the close control, then tapped it",
                                 overlay=overlay)

    entry = next((e for e in playbook.PLAYBOOK if e.kind == shape), None)
    hint = ""
    if entry:
        hint = "\n".join(f"  {i}. {m}" for i, m in enumerate(entry.moves, 1))
    return DismissResult(
        False, overlay=overlay,
        note=(f"could not close this {shape}. Tried the close control, the usual labels, "
              f"dragging it down (from {'the grabber' if overlay.grabber else 'its top edge'}), "
              "the backdrop and a back swipe.\n"
              f"what usually works for a {shape}:\n{hint}"),
    )


def dismiss_once_by_label(phone) -> bool:
    """One more pass for a close control that only just appeared."""
    snap = phone.snapshot(with_screenshot=False, stable=False)
    for e in snap.elements:
        if _is_close_control(e):
            try:
                phone.tap_element(e)
                return True
            except WDAError:
                continue
    return False
