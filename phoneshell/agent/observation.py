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
    if big_opaque is not None and len(actionable) < 5:
        return True, (
            f"a {big_opaque.type} covers most of the screen and the tree exposes only "
            f"{len(actionable)} controls, so the screenshot carries numbered boxes"
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
) -> Observation:
    t0 = time.time()
    som, som_reason = needs_som(elements, screen_w, screen_h)
    som = som or force_som
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
