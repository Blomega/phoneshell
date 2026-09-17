"""One observation per step: what the model sees before it decides.

Shaped by three measured results from the 2026 GUI-agent literature, not by taste:

* Hybrid beats either channel alone (53.7% vs 35.2% tree-only vs 15.6% pixels-only),
  and iOS is the rich-tree case, so the tree leads and the picture supports it.
* When the tree and the pixels disagree, models believe the text 30-79% of the
  time and act wrongly on it 65-100% of the time. Hence the consistency gate in
  verify.py: before a tap fires we check the pixels under the element actually
  look like the thing the model named.
* Full-resolution frames are ~4k tokens each and mostly waste. A 512px-wide
  frame is ~760 tokens and loses nothing that matters at tap granularity.

Serialization is TSV rather than JSON on purpose: repeated keys and braces cost
30-40% more tokens for zero extra information.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from ..perception import screen as sc
from ..perception.tree import Element

# Container types that mean "the tree is probably lying about what is here".
OPAQUE_TYPES = {"WebView", "Map", "Other", "ScrollView", "Image"}


@dataclass
class Observation:
    step: int
    app: str
    bundle_id: str
    elements: list[Element]
    tsv: str
    image_b64: str | None
    image_media_type: str = "image/jpeg"
    image_size: tuple[int, int] | None = None
    som: bool = False
    alert: dict | None = None
    keyboard_visible: bool = False
    scroll_hint: str = ""
    signature: str = ""
    notes: list[str] = field(default_factory=list)
    took: float = 0.0

    def as_text(self) -> str:
        head = [f"SCREEN step={self.step} app={self.app or '?'} ({self.bundle_id or '?'})"]
        if self.alert:
            head.append(f"!! SYSTEM ALERT ON SCREEN: {self.alert['text']!r} buttons={self.alert['buttons']}")
        if self.keyboard_visible:
            head.append("keyboard: visible")
        if self.som:
            head.append("the screenshot has numbered boxes matching the id column")
        if self.scroll_hint:
            head.append(self.scroll_hint)
        for n in self.notes:
            head.append(n)
        head.append("")
        head.append("id\ttype\ttext\tcentre\tsize\tflags")
        return "\n".join(head) + "\n" + self.tsv


def _flags(e: Element) -> str:
    flags = []
    # Say where the row came from. A model that cannot tell a control the app
    # declared from one this harness guessed at will treat both as certain, and
    # the guessed one is the whole reason the consistency gate exists.
    if e.source == "pixel":
        flags.append(f"pixel:{e.confidence:.2f}")
    if e.is_input:
        flags.append("input")
    if e.focused:
        flags.append("focused")
    if not e.enabled:
        flags.append("disabled")
    if e.type == "Switch":
        flags.append("on" if e.value in ("1", "true", "True") else "off")
    return ",".join(flags)


def to_tsv(elements: list[Element]) -> str:
    rows = []
    for e in elements:
        text = e.text.replace("\t", " ").replace("\n", " ")[:90]
        if e.is_input and e.placeholder and not e.value:
            text = f"{text} (placeholder)"
        rows.append(
            f"{e.idx}\t{e.type}\t{text}\t{int(e.cx)},{int(e.cy)}\t{int(e.w)}x{int(e.h)}\t{_flags(e)}"
        )
    return "\n".join(rows)


def needs_som(elements: list[Element], screen_w: float, screen_h: float) -> tuple[bool, str]:
    """Overlay numbered boxes only when the tree is too thin to trust.

    This is the WebView/canvas/game case, where the accessibility tree collapses
    to a couple of opaque containers and the pixels carry all the information.
    """
    actionable = [e for e in elements if e.type not in {"StaticText", "Image", "WebView", "Other"}]
    if len(elements) < 8:
        return True, "the accessibility tree is very thin, so the screenshot carries numbered boxes"
    if not actionable:
        return True, "the tree exposes nothing interactive, so the screenshot carries numbered boxes"
    screen_area = screen_w * screen_h
    big_opaque = next(
        (e for e in elements if e.type in OPAQUE_TYPES and e.area > screen_area * 0.4),
        None,
    )
    if big_opaque is None:
        return False, ""
    # Count what the tree exposes INSIDE the opaque view, not on the whole
    # screen, and decide "inside" by tree descent rather than by rectangle.
    #
    # Measured on Safari at bruno-simon.com: eleven nodes, ten of them Safari's
    # own toolbar and the eleventh a single 440x956 WebView holding the entire
    # page. Counting controls screen-wide says "ten, this is fine" and hands the
    # agent a browser it can drive and a page it cannot see. Counting by
    # rectangle says the same, because the WebView's rect covers the toolbars
    # too. Only the path separates them: the toolbar is a sibling of the
    # WebView, and exactly two nodes are its descendants.
    prefix = big_opaque.path
    inside = [
        e for e in actionable
        if e is not big_opaque and prefix and e.path[:len(prefix)] == prefix
    ]
    if len(inside) < 5:
        return True, (
            f"a {big_opaque.type} covers most of the screen and the tree exposes only "
            f"{len(inside)} controls inside it, so the screenshot carries numbered boxes"
        )
    return False, ""


def build(
    step: int,
    bundle_id: str,
    app_name: str,
    elements: list[Element],
    png: bytes | None,
    scale: float,
    screen_w: float,
    screen_h: float,
    alert: dict | None = None,
    signature: str = "",
    max_edge: int = 512,
    force_som: bool = False,
    include_image: bool = True,
    blind: "BlindConfig | None" = None,
) -> Observation:
    t0 = time.time()
    som, som_reason = needs_som(elements, screen_w, screen_h)
    som = som or force_som

    # The tree being too thin to trust is exactly the condition that makes
    # numbered boxes necessary, and it is exactly the condition blind mode
    # exists for. Recover the targets first, so the boxes get drawn over them
    # too -- numbering one opaque WebView helps nobody.
    recovery = None
    if som and png is not None and blind is not None and blind.enabled:
        from ..perception import blind as _blind
        try:
            recovery = _blind.recover(
                png, elements, scale,
                want_text=blind.text, want_icons=blind.icons,
                want_shapes=blind.shapes, shape_limit=blind.max_shapes,
            )
        except Exception:  # perception must degrade, never fail the step
            recovery = None
        if recovery is not None and recovery.elements:
            named = len(elements)
            elements = elements + recovery.elements
            som_reason = _blind.describe(recovery, named)
    image_b64 = None
    image_size = None
    if png and include_image:
        if som:
            img = sc.annotate(png, elements, scale)
        else:
            img = sc.load_image(png)
        img = sc.downscale(img, max_edge)
        image_size = img.size
        image_b64 = sc.to_jpeg_b64(img, quality=70)

    keyboard = any(e.type == "Key" for e in elements) or any(
        e.type == "Other" and e.text.lower() in {"keyboard", "emoji"} for e in elements
    )
    obs = Observation(
        step=step,
        app=app_name,
        bundle_id=bundle_id,
        elements=elements,
        tsv=to_tsv(elements),
        image_b64=image_b64,
        image_size=image_size,
        som=som,
        alert=alert,
        keyboard_visible=keyboard,
        scroll_hint="there may be more content below, swipe down to see it" if len(elements) >= 40 else "",
        signature=signature,
        took=time.time() - t0,
    )
    if som and som_reason:
        obs.notes.append(som_reason)
    return obs
