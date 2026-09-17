"""Finding controls by the shape they are drawn as, when nothing names them.

An app that exposes no accessibility tree still draws a heart for like, a
paper plane for send and an x for close, because those glyphs are the contract
with the user. So when the tree gives us nothing, match the glyph.

Two decisions worth stating, because both are the difference between a useful
candidate and a confident wrong tap:

* **Templates are drawn, not photographed.** A rendered outline generalises
  across every app's house style far better than a screenshot of one app's
  button does, and it costs nothing to render at whatever size the screen
  needs.
* **Polarity is ignored.** A white heart on a dark video and a black heart on a
  white sheet are the same control. Normalised cross-correlation reports the
  second as a strong *negative* score, so the magnitude is what counts.

This is a candidate generator, never an oracle. Every hit carries its score and
is labelled as a guess, because a photo of a real heart in a feed correlates
with a heart icon and there is no way to tell them apart from pixels alone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw

try:
    import numpy as np
    AVAILABLE = True
except ImportError:  # pragma: no cover - numpy is in requirements, but degrade cleanly
    AVAILABLE = False

# What a hit has to score before it is worth showing an agent at all. Measured
# against rendered controls in tests/test_icons.py: a correct match on a clean
# button scores 0.75-0.95, a wrong glyph of similar mass scores 0.3-0.5.
DEFAULT_THRESHOLD = 0.62

# Icons sit in a fairly narrow band on a phone: a tab bar glyph is ~26pt, a
# floating action button ~34pt, a row accessory ~18pt. Searching five sizes
# covers that without paying for a continuous scale space.
DEFAULT_SIZES = (18, 22, 26, 30, 36)


@dataclass
class IconHit:
    name: str
    x: float
    y: float
    w: float
    h: float
    score: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


# --------------------------------------------------------------------------
# The glyph library. Coordinates are fractions of the icon box, so one
# definition renders at any size.
# --------------------------------------------------------------------------

def _heart(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    pts = []
    for i in range(65):
        t = math.pi * 2 * i / 64
        x = 16 * math.sin(t) ** 3
        y = -(13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))
        pts.append((s * (0.5 + x / 38), s * (0.52 + y / 38)))
    d.polygon(pts, fill=255)


def _bookmark(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.polygon([(s * .24, s * .10), (s * .76, s * .10), (s * .76, s * .90),
               (s * .50, s * .70), (s * .24, s * .90)], fill=255)


def _star(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    pts = []
    for i in range(10):
        r = 0.46 if i % 2 == 0 else 0.20
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((s * (0.5 + r * math.cos(a)), s * (0.5 + r * math.sin(a))))
    d.polygon(pts, fill=255)


def _plus(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.rectangle([s * .5 - w / 2, s * .16, s * .5 + w / 2, s * .84], fill=255)
    d.rectangle([s * .16, s * .5 - w / 2, s * .84, s * .5 + w / 2], fill=255)


def _close(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.line([(s * .20, s * .20), (s * .80, s * .80)], fill=255, width=w)
    d.line([(s * .80, s * .20), (s * .20, s * .80)], fill=255, width=w)


def _chevron(dx: float, dy: float):
    def draw(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
        # A chevron is two strokes meeting at a point; dx/dy say which way it opens.
        tip = (s * (0.5 + 0.22 * dx), s * (0.5 + 0.22 * dy))
        a = (s * (0.5 - 0.22 * dx + 0.26 * dy), s * (0.5 - 0.22 * dy + 0.26 * dx))
        b = (s * (0.5 - 0.22 * dx - 0.26 * dy), s * (0.5 - 0.22 * dy - 0.26 * dx))
        d.line([a, tip, b], fill=255, width=w, joint="curve")
    return draw


def _search(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.ellipse([s * .14, s * .14, s * .68, s * .68], outline=255, width=w)
    d.line([(s * .62, s * .62), (s * .88, s * .88)], fill=255, width=w)


def _hamburger(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    for y in (.28, .5, .72):
        d.rectangle([s * .14, s * y - w / 2, s * .86, s * y + w / 2], fill=255)


def _dots(vertical: bool):
    def draw(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
        r = max(s * 0.075, 1.5)
        for f in (.22, .5, .78):
            cx, cy = (s * .5, s * f) if vertical else (s * f, s * .5)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    return draw


def _play(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.polygon([(s * .28, s * .16), (s * .84, s * .5), (s * .28, s * .84)], fill=255)


def _pause(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.rectangle([s * .26, s * .18, s * .43, s * .82], fill=255)
    d.rectangle([s * .57, s * .18, s * .74, s * .82], fill=255)


def _check(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.line([(s * .16, s * .52), (s * .40, s * .76), (s * .84, s * .24)],
           fill=255, width=w, joint="curve")


def _trash(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.rectangle([s * .30, s * .10, s * .70, s * .18], fill=255)
    d.polygon([(s * .24, s * .24), (s * .76, s * .24), (s * .68, s * .90), (s * .32, s * .90)],
              fill=255)


def _share(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    # iOS share: an arrow rising out of an open-topped box.
    d.line([(s * .5, s * .10), (s * .5, s * .58)], fill=255, width=w)
    d.line([(s * .30, s * .30), (s * .5, s * .10), (s * .70, s * .30)],
           fill=255, width=w, joint="curve")
    d.line([(s * .22, s * .44), (s * .22, s * .90), (s * .78, s * .90), (s * .78, s * .44)],
           fill=255, width=w, joint="curve")


def _send(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.polygon([(s * .10, s * .50), (s * .90, s * .14), (s * .58, s * .88), (s * .46, s * .58)],
              fill=255)


def _camera(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.rounded_rectangle([s * .10, s * .26, s * .90, s * .82], radius=s * .12, fill=255)
    d.rectangle([s * .36, s * .16, s * .64, s * .30], fill=255)
    d.ellipse([s * .36, s * .38, s * .64, s * .70], fill=0)


def _person(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.ellipse([s * .33, s * .12, s * .67, s * .46], fill=255)
    d.pieslice([s * .16, s * .52, s * .84, s * 1.20], start=180, end=360, fill=255)


def _home(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.polygon([(s * .5, s * .10), (s * .94, s * .48), (s * .78, s * .48),
               (s * .78, s * .88), (s * .22, s * .88), (s * .22, s * .48), (s * .06, s * .48)],
              fill=255)


def _bell(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.pieslice([s * .18, s * .16, s * .82, s * .94], start=180, end=360, fill=255)
    d.rectangle([s * .18, s * .55, s * .82, s * .72], fill=255)
    d.ellipse([s * .42, s * .76, s * .58, s * .92], fill=255)


def _gear(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    for i in range(8):
        a = i * math.pi / 4
        cx, cy = s * (.5 + .36 * math.cos(a)), s * (.5 + .36 * math.sin(a))
        r = s * .13
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    d.ellipse([s * .22, s * .22, s * .78, s * .78], fill=255)
    d.ellipse([s * .38, s * .38, s * .62, s * .62], fill=0)


def _refresh(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
    d.arc([s * .16, s * .16, s * .84, s * .84], start=40, end=330, fill=255, width=w)
    d.polygon([(s * .80, s * .06), (s * .96, s * .34), (s * .64, s * .32)], fill=255)


def _arrow(down: bool):
    def draw(d: ImageDraw.ImageDraw, s: int, w: int) -> None:
        top, bottom = (s * .12, s * .70) if down else (s * .30, s * .88)
        d.line([(s * .5, top), (s * .5, bottom)], fill=255, width=w)
        tip = bottom if down else top
        oy = -1 if down else 1
        d.line([(s * .26, tip + oy * s * .22), (s * .5, tip), (s * .74, tip + oy * s * .22)],
               fill=255, width=w, joint="curve")
    return draw


GLYPHS = {
    "heart": _heart,
    "bookmark": _bookmark,
    "star": _star,
    "plus": _plus,
    "close": _close,
    "back": _chevron(-1, 0),
    "forward": _chevron(1, 0),
    "expand": _chevron(0, 1),
    "collapse": _chevron(0, -1),
    "search": _search,
    "menu": _hamburger,
    "more-vertical": _dots(True),
    "more-horizontal": _dots(False),
    "play": _play,
    "pause": _pause,
    "check": _check,
    "trash": _trash,
    "share": _share,
    "send": _send,
    "camera": _camera,
    "person": _person,
    "home": _home,
    "bell": _bell,
    "settings": _gear,
    "refresh": _refresh,
    "download": _arrow(True),
    "upload": _arrow(False),
}


@lru_cache(maxsize=512)
def render(name: str, size: int) -> Image.Image:
    """One glyph, white on black, at the requested pixel size."""
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    stroke = max(2, round(size / 9))
    GLYPHS[name](draw, size, stroke)
    return img


@lru_cache(maxsize=64)
def _bank(size: int, names: tuple[str, ...]):
    """Every glyph at one size, as a matrix of zero-mean unit-norm rows.

    Shaped for a single matmul against a stack of candidate windows, because
    27 separate correlations is 27 trips through numpy and one matrix product
    is one trip through BLAS.
    """
    rows = []
    kept = []
    for name in names:
        arr = np.asarray(render(name, size), dtype=np.float32).ravel()
        centred = arr - arr.mean()
        norm = float(np.sqrt(float(centred @ centred)))
        if norm <= 0:
            continue
        rows.append(centred / norm)
        kept.append(name)
    if not rows:
        return np.zeros((0, size * size), dtype=np.float32), ()
    return np.ascontiguousarray(np.stack(rows)), tuple(kept)


# --------------------------------------------------------------------------
# Matching
#
# Correlating every glyph against every offset of a full screenshot is the
# obvious implementation and it is far too slow: measured at 4.4 seconds for one
# 1170x2532 frame, which would dominate a step that otherwise costs about a
# second end to end.
#
# So do not search the screen. Propose the handful of places an icon could be --
# small, isolated, high-contrast blobs -- and match only there, at the size the
# blob itself implies. That is 20-60 crops of about 40x40 instead of 300,000
# windows, and it is also more accurate, because the blob supplies the scale
# that the exhaustive sweep had to guess at.
# --------------------------------------------------------------------------

_NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def _edges(grey) -> "np.ndarray":
    """Gradient magnitude, cheaply: a forward difference in each axis."""
    gx = np.zeros_like(grey)
    gy = np.zeros_like(grey)
    gx[:, :-1] = np.abs(np.diff(grey, axis=1))
    gy[:-1, :] = np.abs(np.diff(grey, axis=0))
    return gx + gy


def _components(mask, limit: int) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of connected true-cells, in cell coordinates."""
    height, width = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    boxes: list[tuple[int, int, int, int]] = []
    ys, xs = np.nonzero(mask)
    stack: list[tuple[int, int]] = []
    for start_y, start_x in zip(ys.tolist(), xs.tolist()):
        if seen[start_y, start_x]:
            continue
        seen[start_y, start_x] = True
        stack.append((start_y, start_x))
        top = bottom = start_y
        left = right = start_x
        while stack:
            y, x = stack.pop()
            if y < top:
                top = y
            elif y > bottom:
                bottom = y
            if x < left:
                left = x
            elif x > right:
                right = x
            for dy, dx in _NEIGHBOURS:
                ny, nx = y + dy, x + dx
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        boxes.append((left, top, right, bottom))
        if len(boxes) >= limit:
            break
    return boxes


def propose(
    grey,
    cell: int = 3,
    edge_threshold: float = 26.0,
    min_side: float = 9.0,
    max_side: float = 56.0,
    limit: int = 400,
) -> list[tuple[float, float, float, float]]:
    """Places on the screen that are shaped like an icon.

    Works on a coarse grid rather than on pixels: an icon is a handful of
    strokes with gaps between them, and at pixel resolution those gaps split it
    into six components. A 3pt cell closes them.
    """
    mask = _edges(grey) > edge_threshold
    height, width = mask.shape
    ch, cw = height // cell, width // cell
    if ch < 2 or cw < 2:
        return []
    cells = mask[:ch * cell, :cw * cell].reshape(ch, cell, cw, cell).any(axis=(1, 3))
    regions = []
    for left, top, right, bottom in _components(cells, limit):
        x0, y0 = left * cell, top * cell
        w = (right - left + 1) * cell
        h = (bottom - top + 1) * cell
        if not (min_side <= w <= max_side and min_side <= h <= max_side):
            continue
        # Icons are roughly square. This is what rejects words, rules and
        # progress bars without having to recognise them.
        if not (0.45 <= w / h <= 2.2):
            continue
        regions.append((float(x0), float(y0), float(w), float(h)))
    return regions


def _windows(crop, size: int):
    """Every size x size window of the crop, flattened, as one array."""
    from numpy.lib.stride_tricks import sliding_window_view
    view = sliding_window_view(crop, (size, size))
    return view.reshape(view.shape[0] * view.shape[1], size * size), view.shape[:2]


def _match_region(grey, region, names, threshold):
    """Best glyph, if any, for one proposed region."""
    x0, y0, w, h = region
    side = max(w, h)
    height, width = grey.shape
    best: IconHit | None = None
    # The proposal bounds the ink; a rendered glyph carries its own margin, so
    # the template that fits is a little larger than the blob it covers.
    for factor in (1.0, 1.25, 1.5):
        size = int(round(side * factor))
        if size < 8 or size > 64:
            continue
        pad = 4
        cx, cy = x0 + w / 2, y0 + h / 2
        left = int(round(cx - size / 2)) - pad
        top = int(round(cy - size / 2)) - pad
        right, bottom = left + size + 2 * pad, top + size + 2 * pad
        if left < 0 or top < 0 or right > width or bottom > height:
            left = max(0, min(left, width - size))
            top = max(0, min(top, height - size))
            right, bottom = min(width, left + size + 2 * pad), min(height, top + size + 2 * pad)
            if right - left < size or bottom - top < size:
                continue
        crop = grey[top:bottom, left:right]
        stack, (rows, cols) = _windows(crop, size)
        bank, kept = _bank(size, names)
        if bank.shape[0] == 0:
            continue
        n = size * size
        sums = stack.sum(axis=1)
        squares = np.einsum("ij,ij->i", stack, stack)
        variance = np.maximum(squares - (sums * sums) / n, 0.0)
        norms = np.sqrt(variance)
        # A flat window correlates with everything and means nothing.
        usable = norms > 1e-3
        if not usable.any():
            continue
        scores = np.abs(stack[usable] @ bank.T) / norms[usable, None]
        flat = int(np.argmax(scores))
        window_index, glyph_index = divmod(flat, scores.shape[1])
        score = float(scores[window_index, glyph_index])
        if score < threshold:
            continue
        origin = int(np.nonzero(usable)[0][window_index])
        oy, ox = divmod(origin, cols)
        if best is None or score > best.score:
            best = IconHit(
                name=kept[glyph_index],
                x=float(left + ox), y=float(top + oy),
                w=float(size), h=float(size),
                score=score,
            )
    return best


def _suppress(hits: list[IconHit], min_distance: float) -> list[IconHit]:
    """Keep the best hit in any neighbourhood.

    Adjacent proposals often cover the same control, and two glyphs can both
    like the same spot, so suppression is across names rather than within one.
    """
    kept: list[IconHit] = []
    for hit in sorted(hits, key=lambda h: -h.score):
        if any(math.dist((hit.cx, hit.cy), (k.cx, k.cy)) < min_distance for k in kept):
            continue
        kept.append(hit)
    return kept


def find(
    image: Image.Image,
    names: tuple[str, ...] | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    scale: float = 1.0,
    limit: int = 24,
    avoid: list[tuple[float, float, float, float]] | None = None,
) -> list[IconHit]:
    """Every glyph in the library that the image appears to contain.

    `scale` is pixels per point: pass the screenshot's own scale and the hits
    come back in point space, which is what every gesture in this codebase
    speaks. `avoid` takes rectangles already accounted for -- text Vision has
    read, controls the tree named -- so the sweep spends its budget on the parts
    of the screen nothing else explains.
    """
    if not AVAILABLE:
        return []
    names = tuple(names or GLYPHS)
    # Everything downstream is in points, and matching in points rather than
    # device pixels is a 9x saving on a 3x screen for no loss: a 26pt glyph is
    # still 26 pixels of signal.
    if scale != 1.0:
        image = image.resize(
            (max(1, round(image.width / scale)), max(1, round(image.height / scale))),
            Image.LANCZOS,
        )
    grey = np.asarray(image.convert("L"), dtype=np.float32)
    if grey.size == 0:
        return []

    hits: list[IconHit] = []
    for region in propose(grey):
        if avoid and _overlaps(region, avoid):
            continue
        hit = _match_region(grey, region, names, threshold)
        if hit is not None:
            hits.append(hit)
    if not hits:
        return []
    return _suppress(hits, min_distance=14.0)[:limit]


def _overlaps(region, rects, fraction: float = 0.6) -> bool:
    rx, ry, rw, rh = region
    area = rw * rh
    if area <= 0:
        return False
    for ox, oy, ow, oh in rects:
        wide = min(rx + rw, ox + ow) - max(rx, ox)
        tall = min(ry + rh, oy + oh) - max(ry, oy)
        if wide > 0 and tall > 0 and (wide * tall) / area >= fraction:
            return True
    return False
