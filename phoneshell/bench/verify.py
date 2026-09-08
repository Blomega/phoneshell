"""Deciding whether the agent actually did the job.

Every check reads the phone directly rather than believing the agent's account
of itself. An agent that says "I have enabled Do Not Disturb" and an agent that
enabled Do Not Disturb are different things, and only the second one passes.

Where a check can be made against real device state rather than against pixels,
it is: state is harder to fake and does not move when Apple redesigns a screen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..perception.tree import Element, find_by_text
from ..wda.client import WDAError
from .schema import Check


@dataclass
class CheckResult:
    kind: str
    passed: bool
    detail: str = ""


def _match(elements: list[Element], needle: str) -> list[Element]:
    return find_by_text(elements, needle, clickable_only=False)


def run_check(phone, check: Check) -> CheckResult:
    kind = check.kind
    try:
        result = _dispatch(phone, check)
    except WDAError as exc:
        return CheckResult(kind, False, f"could not read the phone: {exc}")
    if check.negate:
        result = CheckResult(result.kind, not result.passed,
                             ("unexpectedly " if not result.passed else "correctly not ") + result.detail)
    return result


def _dispatch(phone, check: Check) -> CheckResult:
    kind = check.kind

    if kind == "foreground_app":
        front = str(phone.wda.active_app_info().get("bundleId") or "")
        want = check.text or check.app
        return CheckResult(kind, front == want, f"foreground is {front}, wanted {want}")

    if kind == "element_exists_anywhere":
        # Scrolls to look. A reminder that exists but sits below the fold is a
        # pass, and treating it as a failure blames the model for our impatience.
        snap = phone.snapshot(with_screenshot=False, stable=False)
        if _match(snap.elements, check.text):
            return CheckResult(kind, True, f"{check.text!r} was already visible")
        found = phone.scroll_to_text(check.text, max_swipes=6)
        return CheckResult(kind, found.ok,
                           f"{check.text!r} {'found after scrolling' if found.ok else 'not found anywhere'}")

    if kind in {"element_exists", "text_on_screen"}:
        snap = phone.snapshot(with_screenshot=False, stable=False)
        hits = _match(snap.elements, check.text)
        return CheckResult(kind, bool(hits),
                           f"{check.text!r} {'found' if hits else 'not found'} on screen")

    if kind == "element_value":
        snap = phone.snapshot(with_screenshot=False, stable=False)
        hits = _match(snap.elements, check.text)
        if not hits:
            return CheckResult(kind, False, f"{check.text!r} is not on screen")
        actual = hits[0].value or hits[0].text
        return CheckResult(kind, check.value.lower() in actual.lower(),
                           f"{check.text!r} shows {actual!r}, wanted {check.value!r}")

    if kind in {"switch_on", "switch_off"}:
        want_on = kind == "switch_on"
        snap = phone.snapshot(with_screenshot=False, stable=False)
        candidates = [e for e in snap.elements if e.type == "Switch"]
        labelled = [e for e in candidates if check.text.lower() in
                    " ".join([e.label, e.name, " ".join(e.children_text)]).lower()]
        target = (labelled or candidates)[:1]
        if not target:
            return CheckResult(kind, False, f"no switch found for {check.text!r}")
        on = str(target[0].value).lower() in {"1", "true", "on"}
        return CheckResult(kind, on == want_on,
                           f"{check.text!r} is {'on' if on else 'off'}, wanted {'on' if want_on else 'off'}")

    if kind == "screen_matches":
        # A structural fingerprint: the agent ended on the right SCREEN, whatever
        # the content happens to say today.
        from ..memory import fingerprint
        snap = phone.snapshot(with_screenshot=False, stable=False)
        actual = fingerprint(snap.bundle_id, snap.elements)
        return CheckResult(kind, actual == check.value,
                           f"screen fingerprint {actual}, wanted {check.value}")

    if kind == "ocr_contains":
        from ..perception import ocr
        from ..perception.screen import load_image
        snap = phone.snapshot(with_screenshot=True, stable=False)
        seen = " ".join(ocr.read_text(load_image(snap.png))).lower()
        return CheckResult(kind, check.text.lower() in seen,
                           f"pixels {'show' if check.text.lower() in seen else 'do not show'} {check.text!r}")

    if kind == "regex_on_screen":
        snap = phone.snapshot(with_screenshot=False, stable=False)
        blob = " ".join(e.text for e in snap.elements)
        found = bool(re.search(check.text, blob))
        return CheckResult(kind, found, f"pattern {check.text!r} {'matched' if found else 'did not match'}")

    if kind == "app_state":
        state = phone.wda.app_state(check.app or check.text)
        names = {0: "unknown", 1: "not running", 2: "background", 3: "background", 4: "foreground"}
        want = check.value or "foreground"
        return CheckResult(kind, names.get(state, "?") == want,
                           f"{check.app or check.text} is {names.get(state, '?')}, wanted {want}")

    return CheckResult(kind, False, f"unknown check kind {kind!r}")


def run_all(phone, checks: list[Check]) -> tuple[bool, list[CheckResult]]:
    results = [run_check(phone, c) for c in checks]
    return (all(r.passed for r in results) and bool(results)), results
