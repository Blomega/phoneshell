"""Rails for an agent operating a phone that can spend money.

The threat model is not a malicious model, it is an ordinary one that
misreads a screen and taps 'Place order' on the wrong restaurant, or taps
'Delete' in a chat. Everything irreversible needs a human in the loop, and
everything that happened needs to be reconstructable afterwards.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config, LOGS
from .perception.tree import Element


@dataclass
class Verdict:
    allowed: bool
    needs_confirmation: bool = False
    reason: str = ""


class Guard:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._patterns = [re.compile(p) for p in cfg.safety.confirm_patterns]
        self.audit_path = LOGS / cfg.safety.audit_log

    def classify_tap(self, element: Element, screen_text: str = "") -> Verdict:
        label = " ".join([element.label, element.name, element.value, " ".join(element.children_text)])
        for pattern in self._patterns:
            if pattern.search(label):
                return Verdict(
                    allowed=True,
                    needs_confirmation=True,
                    reason=f"{element.text!r} looks irreversible (matched {pattern.pattern})",
                )
        return Verdict(allowed=True)

    def classify_app(self, bundle_id: str) -> Verdict:
        if bundle_id in self.cfg.safety.denied_bundle_ids:
            return Verdict(allowed=False, reason=f"{bundle_id} is on the deny list")
        return Verdict(allowed=True)

    def on_payment_screen(self, screen_text: str) -> bool:
        markers = ("place order", "confirm and pay", "pay now", "total to pay", "checkout",
                   "card ending", "apple pay", "confirm payment")
        low = screen_text.lower()
        return any(m in low for m in markers)

    def audit(self, event: str, **fields) -> None:
        record = {"t": round(time.time(), 3), "event": event, **fields}
        with self.audit_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
