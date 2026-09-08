"""App memory: stop re-deriving screens the agent has already understood.

Measured on a real iPhone, one step costs roughly:

    tree read            159 ms
    screenshot            72 ms
    stability gate       504 ms
    fixed settle sleep   600-800 ms
    the model deciding   1-3 s      <- everything else is noise next to this

So the win is not a faster tree, it is not asking the model at all. The first
time a screen is seen it gets the full treatment: stabilise, read, annotate,
send the picture, let the model work it out. What is learned then is kept:

  * a structural fingerprint of the screen, insensitive to the text that changes
    (prices, restaurant names, timestamps) but sensitive to the layout that does not
  * where its controls are and what they are called
  * which action led to which screen, and how often that held
  * how long this particular screen actually takes to settle

Next time the same screen appears, the agent is told what it is and what worked
here before, the screenshot is skipped because the tree is already understood,
and the settle wait shrinks to what this screen has actually needed. A route
walked enough times becomes replayable outright, with no model in the loop.

Nothing here is trusted blindly: a remembered control is still resolved against
the live tree before it is tapped, and a transition that stops holding decays.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import RUNTIME
from .perception.tree import Element

log = logging.getLogger("phoneshell.memory")

STORE = RUNTIME / "memory"
STORE.mkdir(parents=True, exist_ok=True)

# Text that changes every visit and must not affect a fingerprint.
_VOLATILE = re.compile(
    r"(?i)(\d+[.,]\d+|\d{1,2}:\d{2}|\b\d+\s*(min|mins|km|m|%|฿|\$|€|£)\b|"
    r"\b(just now|ago|today|tomorrow|yesterday)\b|\d{3,})"
)
# Identifiers are the best signal a screen has, but real apps bake instance data
# into them: iOS Settings ships ids like
#   com.apple.settings.followUpGroupAsItem...group.account.61932825-AFFA-4F26-...
#   com.apple.settings.connectedHeadphone.38:C4:3A:26:96:EE-0x85a8cf0f0
# containing a UUID, a MAC address and a raw pointer. Those survive within one
# launch and change on the next, which silently turns every revisit into a screen
# the agent has never seen. Strip them before hashing.
_ID_NOISE = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "#uuid"),
    (re.compile(r"0x[0-9a-f]{4,}", re.I), "#ptr"),
    (re.compile(r"\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I), "#mac"),
    (re.compile(r"\b\d{4,}\b"), "#n"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "#hex"),
]


def _norm_id(identifier: str) -> str:
    out = identifier.strip()
    for pattern, replacement in _ID_NOISE:
        out = pattern.sub(replacement, out)
    return out[:80]


# Element types that make up a screen's skeleton, i.e. its chrome and controls.
_STRUCTURAL = {
    "Button", "TabBar", "NavigationBar", "SearchField", "TextField", "SecureTextField",
    "SegmentedControl", "Tab", "Switch", "Link", "Toolbar", "PageIndicator", "Icon",
}


def _norm(text: str) -> str:
    return _VOLATILE.sub("#", text.strip().lower())[:40]


def fingerprint(bundle_id: str, elements: list[Element], coarse: int = 32) -> str:
    """Identity of a screen's LAYOUT, not its contents.

    Two visits to the same GrabFood list with different restaurants produce the
    same fingerprint; the food list and the checkout sheet do not.
    """
    parts = []
    for e in elements:
        if e.type not in _STRUCTURAL and not e.identifier:
            continue
        parts.append(
            f"{e.type}|{_norm_id(e.identifier) or _norm(e.label or e.name)}|"
            f"{round(e.x / coarse)},{round(e.y / coarse)}"
        )
    parts.sort()
    basis = bundle_id + "::" + "|".join(parts) + f"::{len(parts) // 4}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def control_key(e: Element) -> str:
    return hashlib.sha1(
        f"{e.type}|{_norm_id(e.identifier) or _norm(e.label or e.name)}".encode()
    ).hexdigest()[:10]


@dataclass
class ControlMemory:
    key: str
    type: str
    label: str
    identifier: str = ""
    x: float = 0.0
    y: float = 0.0
    taps: int = 0
    led_to: dict[str, int] = field(default_factory=dict)   # fingerprint -> times

    def best_outcome(self) -> tuple[str, int] | None:
        if not self.led_to:
            return None
        fp, n = max(self.led_to.items(), key=lambda kv: kv[1])
        return fp, n


@dataclass
class ScreenMemory:
    fingerprint: str
    bundle_id: str
    name: str = ""
    seen: int = 0
    last_seen: float = 0.0
    settle_ms: int = 0            # observed, not guessed
    settle_samples: int = 0
    controls: dict[str, ControlMemory] = field(default_factory=dict)
    notes: str = ""

    @property
    def known(self) -> bool:
        return self.seen >= 2

    def learn_settle(self, ms: int) -> None:
        # Running mean, then bias slightly high so a fast sample cannot make the
        # gate too tight for a slow day.
        self.settle_samples += 1
        self.settle_ms = int((self.settle_ms * (self.settle_samples - 1) + ms) / self.settle_samples)

    def suggested_settle(self) -> float | None:
        if self.settle_samples < 2 or not self.settle_ms:
            return None
        return min(max(self.settle_ms * 1.4 / 1000, 0.15), 2.5)


class AppMemory:
    """One JSON file per app, so a bad memory can be deleted app by app."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._cache: dict[str, dict[str, ScreenMemory]] = {}

    # ------------------------------------------------------------------- store

    def _path(self, bundle_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id or "unknown")
        return STORE / f"{safe}.json"

    def _load(self, bundle_id: str) -> dict[str, ScreenMemory]:
        if bundle_id in self._cache:
            return self._cache[bundle_id]
        path = self._path(bundle_id)
        screens: dict[str, ScreenMemory] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                for fp, data in raw.get("screens", {}).items():
                    controls = {
                        k: ControlMemory(**v) for k, v in (data.pop("controls", {}) or {}).items()
                    }
                    screens[fp] = ScreenMemory(controls=controls, **data)
            except Exception as exc:
                log.warning("could not read memory for %s: %s", bundle_id, exc)
        self._cache[bundle_id] = screens
        return screens

    def save(self, bundle_id: str) -> None:
        if not self.enabled:
            return
        screens = self._cache.get(bundle_id) or {}
        payload = {"bundle_id": bundle_id, "screens": {fp: asdict(s) for fp, s in screens.items()}}
        self._path(bundle_id).write_text(json.dumps(payload, indent=1))

    # ------------------------------------------------------------------ recall

    def recall(self, bundle_id: str, elements: list[Element]) -> ScreenMemory | None:
        if not self.enabled:
            return None
        fp = fingerprint(bundle_id, elements)
        return self._load(bundle_id).get(fp)

    def see(self, bundle_id: str, elements: list[Element], settle_ms: int | None = None) -> ScreenMemory:
        """Record that we are looking at this screen, and return what we know."""
        fp = fingerprint(bundle_id, elements)
        screens = self._load(bundle_id)
        screen = screens.get(fp)
        if screen is None:
            screen = ScreenMemory(fingerprint=fp, bundle_id=bundle_id)
            screens[fp] = screen
        screen.seen += 1
        screen.last_seen = time.time()
        if settle_ms:
            screen.learn_settle(settle_ms)
        for e in elements:
            if e.type not in _STRUCTURAL and not e.identifier:
                continue
            key = control_key(e)
            control = screen.controls.get(key)
            if control is None:
                screen.controls[key] = ControlMemory(
                    key=key, type=e.type, label=(e.label or e.name or e.text)[:60],
                    identifier=e.identifier, x=e.cx, y=e.cy,
                )
            else:
                control.x, control.y = e.cx, e.cy
        return screen

    def record_action(self, bundle_id: str, before: list[Element], element: Element | None,
                      after_bundle: str, after: list[Element]) -> None:
        """Remember that this control, on this screen, led to that screen."""
        if not self.enabled or element is None:
            return
        fp = fingerprint(bundle_id, before)
        screen = self._load(bundle_id).get(fp)
        if screen is None:
            return
        key = control_key(element)
        control = screen.controls.get(key)
        if control is None:
            control = ControlMemory(key=key, type=element.type,
                                    label=(element.label or element.name or element.text)[:60],
                                    identifier=element.identifier, x=element.cx, y=element.cy)
            screen.controls[key] = control
        control.taps += 1
        target = fingerprint(after_bundle, after)
        control.led_to[target] = control.led_to.get(target, 0) + 1
        self.save(bundle_id)

    def name_screen(self, bundle_id: str, elements: list[Element], name: str) -> None:
        screen = self.recall(bundle_id, elements)
        if screen is not None:
            screen.name = name[:80]
            self.save(bundle_id)

    # ------------------------------------------------------------------- hints

    def hint(self, screen: ScreenMemory | None, elements: list[Element]) -> str:
        """One short block telling the model what it already knows about here."""
        if screen is None or not screen.known:
            return ""
        live = {control_key(e): e for e in elements}
        lines = []
        label = screen.name or "this screen"
        lines.append(
            f"MEMORY: you have been on {label} {screen.seen} times before."
        )
        routes = []
        for control in screen.controls.values():
            if control.taps < 1 or not control.led_to:
                continue
            if control.key not in live:
                continue
            best = control.best_outcome()
            if not best:
                continue
            other = self._load(screen.bundle_id).get(best[0])
            where = (other.name if other and other.name else "another screen")
            routes.append(f"  [{live[control.key].idx}] {control.label!r} -> {where} ({best[1]}x)")
        if routes:
            lines.append("what these controls did last time:")
            lines.extend(sorted(routes)[:8])
        if screen.notes:
            lines.append(f"note: {screen.notes}")
        return "\n".join(lines)

    # ------------------------------------------------------------------- admin

    def stats(self) -> list[dict]:
        out = []
        for path in sorted(STORE.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
            except Exception:
                continue
            screens = raw.get("screens", {})
            out.append({
                "app": raw.get("bundle_id", path.stem),
                "screens": len(screens),
                "visits": sum(s.get("seen", 0) for s in screens.values()),
                "controls": sum(len(s.get("controls", {})) for s in screens.values()),
                "routes": sum(
                    1 for s in screens.values()
                    for c in s.get("controls", {}).values() if c.get("led_to")
                ),
            })
        return out

    def forget(self, bundle_id: str | None = None) -> int:
        if bundle_id:
            path = self._path(bundle_id)
            self._cache.pop(bundle_id, None)
            if path.exists():
                path.unlink()
                return 1
            return 0
        count = 0
        for path in STORE.glob("*.json"):
            path.unlink()
            count += 1
        self._cache.clear()
        return count
