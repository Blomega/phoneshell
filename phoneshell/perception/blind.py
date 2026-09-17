"""Reading a screen that refuses to describe itself.

The README is honest that the biggest thing this project cannot do is an app
that exposes nothing: a Flutter or Unity screen hands XCUITest one opaque view
for the whole window, and a chat app measured here reports
`WAMessageBubbleTableViewCell` as a button's name, which is a class name, not
meaning. Until now the answer was numbered boxes over whatever the tree did
say, which on those screens is one box around everything.

This module is the other half. When the tree is empty the pixels are not, so
recover targets from them:

1. **Text, through Vision.** A line of text nobody exposed is still a thing a
   finger can hit, and text is by far the most reliable of the three: on a real
   WebView splash screen measured here the tree named almost nothing and Vision
   read thirteen lines including the whole tab bar.
2. **Glyphs, through `icons`.** A heart is like, a paper plane is send. Shape
   matching finds them without anyone naming them.
3. **Control-shaped blobs.** Something compact, isolated and high-contrast that
   matched no glyph is still probably a control. Offering "there is *something*
   here" beats offering nothing, as long as it says which it is.

Everything this produces is marked `source="pixel"` and carries a confidence,
and the observation says so in words, because the failure this invites is a
model treating a guessed target as a named one. The tree, when it speaks, still
wins: a pixel candidate that lands on top of something the tree already named is
dropped rather than offered twice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image

from . import icons, ocr
from .tree import Element

# A pixel candidate this close to the centre of a tree element is the same
# control seen twice, not a second one.
_SAME_CONTROL = 0.55

_NORMALISE = re.compile(r"[^a-z0-9]+")

# Text this short is usually a fragment of a glyph rather than a label, and
# offering it as a tap target sends the agent at noise.
_MIN_TEXT = 1

# Split by how much silhouette a glyph actually has, because that is what
# decides whether a match means anything.
#
# A glyph drawn with two or three straight strokes -- a hamburger, a chevron, a
# pair of bars -- correlates with any edge in any photograph, and on a 3D render
# measured here it produced `menu`, `upload` and `more-vertical` at 0.74-0.86
# with nothing of the sort on screen. A heart or a paper plane has a shape that
# ordinary content does not accidentally contain.
#
# So the plain shapes are held to a bar that content noise rarely clears, and
# the distinctive ones keep the ordinary one.
_PLAIN = {
    "menu", "more-horizontal", "more-vertical", "pause", "play", "check",
    "back", "forward", "expand", "collapse", "close", "plus", "person",
    "home", "star", "download", "upload",
}
_PLAIN_THRESHOLD = 0.88

# Points from the top that belong to iOS rather than to the app.
_STATUS_BAR = 54.0


@dataclass
class Recovery:
    """What one pass over the pixels found, and what it cost."""
    elements: list[Element]
    from_text: int = 0
    from_icons: int = 0
    from_shapes: int = 0
    took: float = 0.0

    @property
    def total(self) -> int:
        return len(self.elements)


def _decode(png: bytes) -> Image.Image:
    import io
    return Image.open(io.BytesIO(png)).convert("RGB")


def _key(text: str) -> str:
    return _NORMALISE.sub("", text.lower())


def _covered(cx: float, cy: float, w: float, h: float, existing: list[Element]) -> bool:
    """Is this candidate the same control as something the tree already named?"""
    for e in existing:
        if e.w <= 0 or e.h <= 0:
            continue
        if not (e.x - 2 <= cx <= e.x + e.w + 2 and e.y - 2 <= cy <= e.y + e.h + 2):
            continue
        # Inside a big scroll container means nothing; inside a control the size
        # of a control means it is that control.
        if e.w * e.h <= max(w * h * 9.0, 1.0):
            return True
        if _SAME_CONTROL <= (w * h) / max(e.w * e.h, 1.0):
            return True
    return False


def _text_elements(source, scale: float, existing: list[Element],
                   next_idx: int) -> tuple[list[Element], list[tuple[float, float, float, float]]]:
    out: list[Element] = []
    rects: list[tuple[float, float, float, float]] = []
    for box in ocr.read_boxes(source):
        x, y = box.x / scale, box.y / scale
        w, h = box.w / scale, box.h / scale
        rects.append((x, y, w, h))
        if len(box.text.strip()) <= _MIN_TEXT:
            continue
        if _covered(x + w / 2, y + h / 2, w, h, existing):
            continue
        out.append(Element(
            idx=next_idx + len(out),
            type="Text",
            label=box.text,
            x=x, y=y, w=w, h=h,
            source="pixel",
            confidence=round(box.confidence, 2),
        ))
    return out, rects


def _icon_elements(points: Image.Image, existing: list[Element],
                   text_rects: list[tuple[float, float, float, float]],
                   next_idx: int) -> list[Element]:
    out: list[Element] = []
    for hit in icons.find(points, avoid=text_rects):
        if hit.name in _PLAIN and hit.score < _PLAIN_THRESHOLD:
            continue
        if _covered(hit.cx, hit.cy, hit.w, hit.h, existing):
            continue
        out.append(Element(
            idx=next_idx + len(out),
            type="Icon",
            label=hit.name,
            x=hit.x, y=hit.y, w=hit.w, h=hit.h,
            source="pixel",
            confidence=round(hit.score, 2),
        ))
    return out


def _shape_elements(points: Image.Image, existing: list[Element],
                    claimed: list[Element], next_idx: int, limit: int) -> list[Element]:
    """Compact isolated blobs that matched no glyph, offered as unknown controls."""
    if not icons.AVAILABLE:
        return []
    import numpy as np

    grey = np.asarray(points.convert("L"), dtype=np.float32)
    taken = claimed + existing
    # Wider than the glyph sweep, because an app icon or an avatar is a control
    # at 60pt and a row accessory is one at 12pt.
    candidates = [
        (x, y, w, h) for x, y, w, h in icons.propose(grey, min_side=11.0, max_side=76.0)
        # The status bar draws signal, wifi and battery, and not one of them is
        # a thing an agent should ever be offered as a tap target.
        if y + h > _STATUS_BAR and not _covered(x + w / 2, y + h / 2, w, h, taken)
    ]
    candidates = _drop_wallpaper(candidates)
    out: list[Element] = []
    for x, y, w, h in candidates[:limit]:
        out.append(Element(
            idx=next_idx + len(out),
            type="Graphic",
            label="",
            x=float(x), y=float(y), w=float(w), h=float(h),
            source="pixel",
            confidence=0.3,
        ))
    return out


def _drop_wallpaper(candidates, min_repeats: int = 4, min_rows: int = 2, band: float = 24.0):
    """Throw away blobs that are a background pattern rather than controls.

    Measured on a WebGL page whose backdrop is a field of small purple crosses:
    the blob detector loved every one of them and filled the target list with
    nine identical boxes that are not controls and never will be.

    The discriminator is repetition across rows. A tab bar is five similar
    shapes on ONE row and is kept; a texture is the same shape at the same size
    on more than one row and is dropped. Nothing here needs to know what the
    shape is, which is the point: it generalises to any wallpaper.

    The band has to be tighter than the pattern's own pitch or the rows merge
    and the texture reads as a tab bar. 24pt is below the 30pt pitch measured on
    the page this was built against, and still well above the couple of points a
    real tab bar's items differ by.
    """
    buckets: dict[tuple[int, int], list] = {}
    for candidate in candidates:
        _, _, w, h = candidate
        buckets.setdefault((round(w / 4), round(h / 4)), []).append(candidate)
    out = []
    for group in buckets.values():
        rows = {round((y + h / 2) / band) for _, y, _, h in group}
        if len(group) >= min_repeats and len(rows) >= min_rows:
            continue
        out.extend(group)
    out.sort(key=lambda c: (c[1], c[0]))
    return out


def recover(
    source: Image.Image | bytes,
    elements: list[Element],
    scale: float = 1.0,
    want_text: bool = True,
    want_icons: bool = True,
    want_shapes: bool = True,
    shape_limit: int = 20,
) -> Recovery:
    """Tap targets read out of the pixels, on top of whatever the tree gave.

    `source` is the screenshot, ideally as the PNG bytes the device sent: Vision
    reads those directly, and handing it a PIL image instead adds a 105ms
    re-encode for nothing. `scale` is pixels per point, and every rectangle
    comes back in points.
    """
    import time
    started = time.time()
    next_idx = max((e.idx for e in elements), default=0) + 1

    text: list[Element] = []
    text_rects: list[tuple[float, float, float, float]] = []
    if want_text:
        text, text_rects = _text_elements(source, scale, elements, next_idx)

    points = None
    if want_icons or want_shapes:
        image = source if isinstance(source, Image.Image) else _decode(source)
        # One downscale to point space, shared by both pixel passes. Matching in
        # points rather than device pixels is a 9x saving on a 3x screen and
        # loses nothing: a 26pt glyph is still 26 pixels of signal.
        points = image if scale == 1.0 else image.resize(
            (max(1, round(image.width / scale)), max(1, round(image.height / scale))),
            Image.LANCZOS,
        )

    glyphs: list[Element] = []
    if want_icons and points is not None:
        glyphs = _icon_elements(points, elements, text_rects, next_idx + len(text))

    shapes: list[Element] = []
    if want_shapes and points is not None:
        shapes = _shape_elements(points, elements, text + glyphs,
                                 next_idx + len(text) + len(glyphs), shape_limit)

    found = text + glyphs + shapes
    # Reading order among themselves, so the list a model reads top to bottom
    # matches the screen it is looking at. Rows are banded before sorting by x
    # because a tab bar's five items differ by a pixel or two in y.
    found.sort(key=lambda e: (round(e.cy / 12), e.cx))
    for position, element in enumerate(found):
        element.idx = next_idx + position
    return Recovery(
        elements=found,
        from_text=len(text),
        from_icons=len(glyphs),
        from_shapes=len(shapes),
        took=time.time() - started,
    )


def describe(recovery: Recovery, named: int) -> str:
    """The sentence the model is shown, which has to make the guess obvious."""
    if not recovery.elements:
        return ""
    parts = []
    if recovery.from_text:
        parts.append(f"{recovery.from_text} from text")
    if recovery.from_icons:
        parts.append(f"{recovery.from_icons} from icon shapes")
    if recovery.from_shapes:
        parts.append(f"{recovery.from_shapes} unidentified")
    return (
        f"this app named only {named} control{'' if named == 1 else 's'}, so "
        f"{recovery.total} more were read from the pixels ({', '.join(parts)}). "
        "Rows flagged `pixel` are a guess at where a control is; the app did not "
        "say they exist, and a Graphic row says only that something is drawn there."
    )
