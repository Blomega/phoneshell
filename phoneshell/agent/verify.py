"""Guards that sit between the model's decision and the phone.

The consistency gate is the important one. A model that reads "Confirm booking"
in the serialized tree will tap that element even when the pixels under it now
say something else, because a stale or wrong label overrides correct visual
perception 30-79% of the time in measurement, and that becomes a wrong action
65-100% of the time.

So before any tap, the element's rect is cropped out of the current frame and
checked two ways: structurally (is it degenerate, off screen, or a flat block of
colour, all of which mean covered or not yet painted) and textually (does macOS
Vision actually read the claimed label in those pixels). The text check is the
one that earns the measured improvement; the structural check alone only catches
blanks. Vision runs locally in tens of milliseconds, so this is free per step.

An element with no text, or a crop with no readable text, comes back
inconclusive and is allowed through: plenty of real controls are bare icons, and
a gate that blocked those would be worse than no gate.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageStat

from ..perception import ocr
from ..perception.tree import Element


# Types whose accessibility label is the text actually drawn on screen. Icons,
# images, text fields, webviews and containers all carry labels that describe
# something other than their own pixels: an app name printed under an icon, a
# caption rather than the value, a whole page. Verifying those produces noise.
# Measured on three real screens: checking every element raised a false alarm on
# 39% of them, and scoping to this set takes that to zero without losing the
# check on the controls that actually matter, which are the ones with words on
# them like "Place order" and "Confirm and pay".
LABEL_IS_RENDERED = {"Button", "StaticText", "Link", "MenuItem", "Tab", "SegmentedControl"}
MIN_TEXT_W = 40.0
MIN_TEXT_H = 12.0


def worth_checking(element: Element) -> bool:
    return (
        element.type in LABEL_IS_RENDERED
        and bool(element.label or element.name)
        and element.w >= MIN_TEXT_W
        and element.h >= MIN_TEXT_H
    )


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    verified_label: bool | None = None   # True matched, False contradicted, None not checked
    crop_b64: str | None = None


def crop_element(png: bytes, element: Element, scale: float, pad: int = 6) -> Image.Image:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    x0 = max(int(element.x * scale) - pad, 0)
    y0 = max(int(element.y * scale) - pad, 0)
    x1 = min(int((element.x + element.w) * scale) + pad, img.width)
    y1 = min(int((element.y + element.h) * scale) + pad, img.height)
    if x1 <= x0 or y1 <= y0:
        return img.crop((0, 0, 1, 1))
    return img.crop((x0, y0, x1, y1))


def consistency_gate(
    png: bytes | None,
    element: Element,
    scale: float,
    check_label: bool = True,
) -> GateResult:
    """Verify the named element is really there, and really says what it claims."""
    if png is None:
        return GateResult(True, "no frame to check against")
    if element.w < 2 or element.h < 2:
        return GateResult(False, f"element [{element.idx}] has a degenerate rect")
    crop = crop_element(png, element, scale)
    if crop.width < 3 or crop.height < 3:
        return GateResult(False, f"element [{element.idx}] is off screen")

    stat = ImageStat.Stat(crop.convert("L"))
    # A control that renders as a single flat colour is either covered by an
    # overlay or has not painted yet. Text and icons always vary.
    if stat.stddev[0] < 1.2 and element.text:
        return GateResult(
            False,
            f"element [{element.idx}] {element.text!r} is a flat block of colour on screen, "
            "so it is probably covered or not painted yet",
        )

    if not check_label or not worth_checking(element):
        return GateResult(True, "label not verifiable for this element type", verified_label=None)

    seen = ocr.read_text(crop)
    matched, why = ocr.label_matches(element.text, seen)
    if matched is False:
        return GateResult(
            False,
            f"element [{element.idx}] claims {element.text[:60]!r} but the pixels there say "
            f"{seen[:3]}. The screen has moved on since it was read; observe again.",
            verified_label=False,
        )
    return GateResult(True, why, verified_label=matched)


class StuckDetector:
    """Round-layer traps (loops, deadlocks) are the failure class models cannot
    fix on their own, so the harness has to notice them."""

    def __init__(self, repeat_limit: int = 3, history: int = 8):
        self.repeat_limit = repeat_limit
        self.signatures: list[str] = []
        self.actions: list[str] = []
        self.history = history

    def record(self, signature: str, action: str) -> None:
        self.signatures.append(signature)
        self.actions.append(action)
        self.signatures = self.signatures[-self.history:]
        self.actions = self.actions[-self.history:]

    def diagnosis(self) -> str | None:
        if len(self.signatures) >= self.repeat_limit:
            tail = self.signatures[-self.repeat_limit:]
            if len(set(tail)) == 1:
                atail = self.actions[-self.repeat_limit:]
                if len(set(atail)) == 1:
                    return (
                        f"the same action ({atail[-1]}) has been repeated {self.repeat_limit} times "
                        "and the screen has not changed at all"
                    )
                return f"the screen has not changed for {self.repeat_limit} actions"
        if len(self.signatures) >= 6:
            # A -> B -> A -> B ping-pong.
            a, b = self.signatures[-2], self.signatures[-1]
            if a != b and self.signatures[-6:] == [a, b, a, b, a, b]:
                return "the screen is alternating between two states, this is a loop"
        return None

    def reset(self) -> None:
        self.signatures.clear()
        self.actions.clear()
