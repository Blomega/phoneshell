"""Screenshots: stability detection, downscaling, and set-of-mark overlays.

Two jobs here.

1. Knowing when the screen has stopped moving. iOS animates everything, and a
   tree read mid-transition returns a half-built screen: on this machine a home
   screen read 1.0s after pressing home gave 6 elements, and the same read at
   2.0s gave 12. Polling a cheap 64x64 grayscale hash of the framebuffer costs
   ~0.2s per probe versus ~0.9s for a full accessibility snapshot, so we settle
   visually first and read the tree once.

2. Numbering what the model is allowed to touch. Handing a model raw pixels and
   asking for coordinates is the single biggest source of wrong taps; handing it
   a numbered overlay plus the matching text list turns a grounding problem into
   a selection problem.
"""
from __future__ import annotations

import base64
import hashlib
import io
import time
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from .tree import Element

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/SFNSRounded.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

# High-contrast palette that stays readable on light and dark UIs alike.
_COLORS = [
    (255, 59, 48), (0, 122, 255), (52, 199, 89), (255, 149, 0),
    (175, 82, 222), (255, 45, 85), (90, 200, 250), (162, 132, 94),
]


def load_image(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def visual_hash(png: bytes, size: int = 64) -> str:
    """Cheap perceptual fingerprint used only to answer 'did anything move'."""
    img = load_image(png).convert("L").resize((size, size), Image.BILINEAR)
    return hashlib.sha1(img.tobytes()).hexdigest()[:16]


def visual_difference(a: bytes, b: bytes, size: int = 64) -> float:
    """0.0 identical, 1.0 completely different. Used for stuck detection."""
    ia = load_image(a).convert("L").resize((size, size), Image.BILINEAR)
    ib = load_image(b).convert("L").resize((size, size), Image.BILINEAR)
    pa, pb = ia.tobytes(), ib.tobytes()
    diff = sum(1 for x, y in zip(pa, pb) if abs(x - y) > 12)
    return diff / len(pa)


@dataclass
class StabilityReport:
    stable: bool
    waited: float
    probes: int


def wait_until_stable(
    grab,
    max_wait: float = 3.5,
    interval: float = 0.3,
    required_matches: int = 2,
    tolerance: float = 0.012,
) -> tuple[bytes, StabilityReport]:
    """Poll screenshots until the screen stops moving MEANINGFULLY.

    Not until two frames are identical. Identical is the wrong test: a countdown
    timer, a carousel, a spinner or a blinking cursor changes a handful of pixels
    every tick, so an exact-match gate never converges and every single
    observation pays the full timeout. Grab's "40% off flash deals 14:19" sheet
    does exactly this, and it turned a 2s step into a 50s one.

    `tolerance` is the fraction of pixels allowed to differ and still count as
    settled: a ticking clock moves well under 1% of the frame, while a screen
    transition moves tens of percent.
    """
    start = time.time()
    previous = None
    matches = 0
    shot = grab()
    probes = 1
    while time.time() - start < max_wait:
        if previous is not None:
            try:
                moved = visual_difference(previous, shot)
            except Exception:
                moved = 1.0
            if moved <= tolerance:
                matches += 1
                if matches >= required_matches - 1:
                    return shot, StabilityReport(True, time.time() - start, probes)
            else:
                matches = 0
        previous = shot
        time.sleep(interval)
        shot = grab()
        probes += 1
    return shot, StabilityReport(False, time.time() - start, probes)


def downscale(img: Image.Image, max_edge: int = 1024) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_edge:
        return img
    ratio = max_edge / max(w, h)
    return img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)


def to_jpeg_b64(img: Image.Image, quality: int = 72) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def annotate(
    png: bytes,
    elements: list[Element],
    scale: float,
    only_clickable: bool = True,
    max_marks: int = 60,
) -> Image.Image:
    """Draw numbered boxes matching the element list.

    Element rects are in points; the screenshot is in native pixels. Every mark
    here is drawn at rect * scale, which is the same conversion the tap path
    runs in reverse, so what the model sees and what the finger hits cannot
    drift apart.
    """
    img = load_image(png)
    draw = ImageDraw.Draw(img, "RGBA")
    marks = 0
    for e in elements:
        if only_clickable and e.type in {"StaticText", "Image"} and not e.identifier:
            continue
        if marks >= max_marks:
            break
        x0, y0 = e.x * scale, e.y * scale
        x1, y1 = (e.x + e.w) * scale, (e.y + e.h) * scale
        color = _COLORS[e.idx % len(_COLORS)]
        draw.rectangle([x0, y0, x1, y1], outline=color + (255,), width=max(2, int(scale)))
        label = str(e.idx)
        size = max(20, int(11 * scale))
        font = _font(size)
        tw = draw.textlength(label, font=font)
        pad = size * 0.25
        bx1, by1 = x0 + tw + pad * 2, y0 + size + pad
        draw.rectangle([x0, y0, bx1, by1], fill=color + (235,))
        draw.text((x0 + pad, y0 + pad * 0.4), label, fill=(255, 255, 255), font=font)
        marks += 1
    return img
