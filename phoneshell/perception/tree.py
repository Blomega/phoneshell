"""Turn WebDriverAgent's accessibility tree into something a model can act on.

The whole difficulty of a phone agent is here. A raw /source dump for a screen
like GrabFood is 3000+ nodes and 400KB of JSON: unusable as a prompt and full of
layout scaffolding with no meaning. What the model needs is the 40-100 things a
finger could actually touch, in reading order, with stable numbers it can name.

Rules encoded below, learned from how XCUITest actually reports iOS UI:
  * every node carries a rect in POINTS, already in screen space
  * `isVisible` is computed by XCTest hit-testing, so it is trustworthy, but
    offscreen scroll content is often still marked visible=false rather than
    absent, which is why we clip against the window rect ourselves
  * containers (`Other`) repeat their child's label constantly, so an unfiltered
    tree gives the model five identical "Add to cart" candidates at the same spot
  * a Cell is the unit a human perceives as "a row", so we fold a cell's static
    text into one line but keep any real buttons inside it separate
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

# Types a finger can meaningfully act on.
INTERACTIVE = {
    "Button", "Link", "Cell", "TextField", "SecureTextField", "SearchField",
    "TextView", "Switch", "Slider", "PickerWheel", "Picker", "SegmentedControl",
    "Tab", "MenuItem", "CheckBox", "RadioButton", "Stepper", "Toolbar",
    "DatePicker", "Key", "PageIndicator", "Map", "ScrollBar",
}
# Types that carry information but are not tap targets by default.
INFORMATIONAL = {"StaticText", "Image", "Icon", "ProgressIndicator", "ActivityIndicator"}
# Pure layout. Only kept when they are the only thing holding a label.
CONTAINERS = {"Other", "Window", "Application", "Group", "ScrollView", "Table",
              "CollectionView", "NavigationBar", "TabBar", "StatusBar", "Sheet", "Alert"}
TEXT_INPUT = {"TextField", "SecureTextField", "SearchField", "TextView"}

_WS = re.compile(r"\s+")


def _num(value: Any) -> float:
    """Rect components, made safe.

    WebDriverAgent reports infinite rects for some offscreen scroll containers,
    and JSON parses `Infinity` straight into a float. One of those reaching a
    gesture produces "Invalid parameter not satisfying: point.x != INFINITY"
    from the phone, so it stops here.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _clean(s: Any) -> str:
    if s is None or s is False:
        return ""
    return _WS.sub(" ", str(s)).strip()


@dataclass
class Element:
    idx: int
    type: str
    label: str = ""
    name: str = ""
    value: str = ""
    placeholder: str = ""
    identifier: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    enabled: bool = True
    visible: bool = True
    accessible: bool = False
    focused: bool = False
    traits: str = ""
    depth: int = 0
    path: tuple[int, ...] = ()
    children_text: list[str] = field(default_factory=list)

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(self.w, 0) * max(self.h, 0)

    @property
    def is_input(self) -> bool:
        return self.type in TEXT_INPUT

    @property
    def text(self) -> str:
        """The single most descriptive string for this element."""
        for candidate in (self.label, self.name, self.value, self.placeholder, self.identifier):
            if candidate:
                return candidate
        if self.children_text:
            return " / ".join(self.children_text[:4])
        return ""

    @property
    def key(self) -> str:
        """Identity that survives small layout shifts, used to match across frames."""
        basis = f"{self.type}|{self.text}|{self.identifier}|{round(self.cx / 8)}|{round(self.cy / 8)}"
        return hashlib.sha1(basis.encode()).hexdigest()[:10]

    def describe(self) -> str:
        bits = [f"[{self.idx}]", self.type]
        t = self.text
        if t:
            bits.append(f'"{t[:80]}"')
        if self.is_input:
            if self.value:
                bits.append(f"value={self.value[:40]!r}")
            elif self.placeholder:
                bits.append(f"placeholder={self.placeholder[:40]!r}")
        if self.type == "Switch":
            bits.append(f"state={'on' if self.value in ('1', 'true', 'True') else 'off'}")
        if self.focused:
            bits.append("FOCUSED")
        if not self.enabled:
            bits.append("disabled")
        bits.append(f"@({int(self.cx)},{int(self.cy)}) {int(self.w)}x{int(self.h)}")
        return " ".join(bits)


def _walk(node: dict, depth: int = 0, path: tuple[int, ...] = ()) -> Iterator[tuple[dict, int, tuple[int, ...]]]:
    yield node, depth, path
    for i, child in enumerate(node.get("children") or []):
        yield from _walk(child, depth + 1, path + (i,))


def _node_to_element(node: dict, depth: int, path: tuple[int, ...]) -> Element:
    rect = node.get("rect") or {}
    return Element(
        idx=-1,
        type=_clean(node.get("type")) or "Other",
        label=_clean(node.get("label")),
        name=_clean(node.get("name")),
        value=_clean(node.get("value")),
        placeholder=_clean(node.get("placeholderValue")),
        identifier=_clean(node.get("rawIdentifier")),
        x=_num(rect.get("x")),
        y=_num(rect.get("y")),
        w=_num(rect.get("width")),
        h=_num(rect.get("height")),
        enabled=str(node.get("isEnabled", "true")).lower() in ("1", "true"),
        visible=str(node.get("isVisible", "true")).lower() in ("1", "true"),
        accessible=str(node.get("isAccessible", "false")).lower() in ("1", "true"),
        focused=str(node.get("isFocused", "false")).lower() in ("1", "true"),
        traits=_clean(node.get("traits")),
        depth=depth,
        path=path,
    )


def flatten(source: dict) -> list[Element]:
    return [_node_to_element(n, d, p) for n, d, p in _walk(source)]


def _on_screen(e: Element, sw: float, sh: float) -> bool:
    if e.w <= 1 or e.h <= 1:
        return False
    if e.x >= sw or e.y >= sh:
        return False
    if e.x + e.w <= 0 or e.y + e.h <= 0:
        return False
    # A node that spans far beyond the window is scaffolding, not a target.
    return not (e.w > sw * 3 or e.h > sh * 3)


def _looks_clickable(e: Element) -> bool:
    if e.type in INTERACTIVE:
        return True
    if "Button" in e.traits or "Link" in e.traits:
        return True
    # Tappable images and labels are extremely common in RN / Flutter apps.
    if e.type in {"Image", "StaticText", "Other"} and e.accessible and e.text:
        return True
    return False


def condense(
    elements: list[Element],
    screen_w: float,
    screen_h: float,
    max_elements: int = 120,
    status_bar_h: float = 0.0,
    keep_status_bar: bool = False,
) -> list[Element]:
    """Filter, dedupe and rank a flattened tree into an actionable element list.

    Measured on a real iOS 26 home screen this takes 193 raw nodes down to ~14
    meaningful ones. Every rule below exists because a real screen produced junk
    without it.
    """
    live = [e for e in elements if _on_screen(e, screen_w, screen_h)]
    live = [e for e in live if e.visible or e.type in TEXT_INPUT]

    if not keep_status_bar and status_bar_h:
        live = [
            e for e in live
            if not (e.y + e.h <= status_bar_h + 2 and e.type not in INTERACTIVE)
        ]

    # Fold a cell's descendant static text into the cell so rows read as one line,
    # and remember which nodes were folded so we can drop the duplicates.
    folded: set[tuple[int, ...]] = set()
    for e in live:
        if e.type in {"Cell", "Button", "Link"}:
            for other in live:
                if (
                    other is not e
                    and len(other.path) > len(e.path)
                    and other.path[: len(e.path)] == e.path
                    and other.type in INFORMATIONAL
                    and other.text
                ):
                    e.children_text.append(other.text)
                    folded.add(other.path)

    keep: list[Element] = []
    for e in live:
        if e.path in folded:
            continue
        # Layout scaffolding that wraps something else adds nothing but ambiguity.
        if e.type in CONTAINERS and not _looks_clickable(e):
            has_child = any(
                o is not e and len(o.path) > len(e.path) and o.path[: len(e.path)] == e.path
                for o in live
            )
            if has_child:
                continue
        # Anonymous decoration: no text, no id, not a real control.
        if not e.text and not e.identifier and e.type not in INTERACTIVE:
            continue
        # A full-screen anonymous layer is never a target.
        if not e.text and e.w >= screen_w * 0.98 and e.h >= screen_h * 0.9:
            continue
        keep.append(e)

    # Collapse duplicates: same text, overlapping centre. Prefer the smallest
    # clickable node, since that is the real tap target rather than its wrapper.
    buckets: dict[tuple, list[Element]] = {}
    for e in keep:
        bucket = (e.text.lower(), round(e.cx / 12), round(e.cy / 12))
        buckets.setdefault(bucket, []).append(e)
    deduped: list[Element] = []
    for bucket, group in buckets.items():
        if len(group) == 1:
            deduped.append(group[0])
            continue
        clickable = [g for g in group if _looks_clickable(g)]
        pool = clickable or group
        pool.sort(key=lambda g: (g.type in CONTAINERS, g.area))
        deduped.append(pool[0])

    # Same-rect duplicates: iOS reports a Cell and the Button inside it with
    # identical frames and near-identical labels, so an unfiltered list offers
    # the model two indistinguishable ways to tap one row. Keep the control.
    TYPE_PRIORITY = {"Button": 0, "Link": 0, "TextField": 0, "SecureTextField": 0,
                     "SearchField": 0, "Switch": 0, "Cell": 1, "Icon": 1, "Other": 3}
    rect_buckets: dict[tuple, list[Element]] = {}
    for e in deduped:
        rect_buckets.setdefault(
            (round(e.x / 6), round(e.y / 6), round(e.w / 6), round(e.h / 6)), []
        ).append(e)
    survivors: list[Element] = []
    for group in rect_buckets.values():
        if len(group) == 1:
            survivors.append(group[0])
            continue
        group.sort(key=lambda g: (TYPE_PRIORITY.get(g.type, 2), len(g.text) or 999))
        winner = group[0]
        # Do not lose text that only the discarded twin carried.
        for other in group[1:]:
            if other.text and other.text not in winner.text and other.text not in winner.children_text:
                winner.children_text.append(other.text)
        survivors.append(winner)
    deduped = survivors

    def rank(e: Element) -> tuple:
        return (0 if _looks_clickable(e) else 1, round(e.y / 4), e.x)

    deduped.sort(key=rank)
    if len(deduped) > max_elements:
        clickable = [e for e in deduped if _looks_clickable(e)]
        rest = [e for e in deduped if not _looks_clickable(e)]
        deduped = (clickable[:max_elements] + rest)[:max_elements]
        deduped.sort(key=rank)

    for i, e in enumerate(deduped, start=1):
        e.idx = i
    return deduped


def to_prompt(elements: list[Element], max_chars: int = 12000) -> str:
    lines = [e.describe() for e in elements]
    out, total = [], 0
    for line in lines:
        if total + len(line) > max_chars:
            out.append(f"... {len(lines) - len(out)} more elements omitted")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def screen_signature(elements: list[Element]) -> str:
    """A hash of what is on screen, used to detect 'nothing changed' loops."""
    basis = "|".join(sorted(f"{e.type}:{e.text[:30]}:{int(e.cy / 20)}" for e in elements))
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def find_by_text(elements: list[Element], needle: str, clickable_only: bool = True) -> list[Element]:
    n = needle.lower().strip()
    hits = []
    for e in elements:
        if clickable_only and not _looks_clickable(e):
            continue
        hay = " ".join([e.label, e.name, e.value, e.identifier, " ".join(e.children_text)]).lower()
        if n and n in hay:
            hits.append(e)
    hits.sort(key=lambda e: (len(e.text), e.y))
    return hits
