"""Sharing one phone between its owner and the agent.

There is no way around the hardware fact: iOS 26 has one interactive display and
one digitizer, so the agent and the human cannot both drive the phone at once.
Verified from the iOS 26.4 SDK headers, not assumed: UIWindowScene.h declares
only UIWindowSceneSessionRoleExternalDisplayNonInteractive, and the interactive
external-display role was deprecated in iOS 16. The device enumerates seven
display slots, but everything past the built-in one mirrors and cannot be
touched.

So the honest version of "run in the background" is: the agent gets out of the
way the instant you pick up your phone, and picks up where it left off when you
put it down. In shared mode the agent holds the phone only in short bursts, and
any screen change it did not cause is read as you, which pauses it.

Costs measured on this rig, which is why the watchdog is shaped this way:
    /wda/activeAppInfo    ~100ms   -> the cheap foreground check
    /screenshot           90-130ms -> the change detector
    /source?format=json   ~2.2s    -> far too slow to poll, never used here
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .perception.screen import visual_difference
from .wda.client import WDAClient, WDAError

log = logging.getLogger("phoneshell.coexist")


@dataclass
class Presence:
    human_active: bool = False
    last_human_at: float = 0.0
    last_reason: str = ""
    samples: int = 0
    yields: int = 0


class Coexistence:
    """Watches for the owner touching the phone, so the agent can yield.

    The agent brackets its own actions with `claim()`. Anything the watchdog sees
    outside a claim is somebody else, which in practice means the person holding
    the phone.
    """

    def __init__(
        self,
        wda: WDAClient,
        poll: float = 0.8,
        grace: float = 6.0,
        settle_after_action: float = 1.2,
    ):
        self.wda = wda
        self.poll = poll
        self.grace = grace                      # how long the human keeps the phone after a touch
        self.settle = settle_after_action       # ignore changes just after our own action
        self.presence = Presence()
        self._claim_until = 0.0
        self._last_png: bytes | None = None
        self._last_app: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="phoneshell-coexist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def claim(self, seconds: float = 2.5) -> None:
        """The agent is about to act; changes for the next few seconds are ours."""
        with self._lock:
            self._claim_until = max(self._claim_until, time.time() + seconds)

    def release(self) -> None:
        with self._lock:
            self._claim_until = time.time() + self.settle

    @property
    def ours(self) -> bool:
        return time.time() < self._claim_until

    # ------------------------------------------------------------------- policy

    def human_has_the_phone(self) -> bool:
        p = self.presence
        return p.human_active and (time.time() - p.last_human_at) < self.grace

    def wait_for_turn(self, timeout: float = 120.0) -> tuple[bool, str]:
        """Block until the human has been idle for the grace period."""
        deadline = time.time() + timeout
        waited = False
        while self.human_has_the_phone():
            waited = True
            if time.time() > deadline:
                return False, (
                    f"still waiting after {timeout:.0f}s: the phone has been in use "
                    f"({self.presence.last_reason})"
                )
            time.sleep(0.4)
        if waited:
            self.presence.yields += 1
            return True, "the phone is free again, carrying on"
        return True, ""

    # -------------------------------------------------------------------- watch

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample()
            except WDAError:
                pass
            except Exception as exc:  # a watchdog must never take the bridge down
                log.debug("coexist sample failed: %s", exc)
            self._stop.wait(self.poll)

    def _sample(self) -> None:
        if self.ours:
            # Keep a fresh baseline so our own change is not read as the human's.
            self._last_png = self._safe_shot()
            self._last_app = self._safe_app()
            return

        app = self._safe_app()
        png = self._safe_shot()
        self.presence.samples += 1

        reason = ""
        if app and self._last_app and app != self._last_app:
            reason = f"the foreground app changed to {app.rsplit('.', 1)[-1]}"
        elif png and self._last_png:
            try:
                if visual_difference(self._last_png, png) > 0.03:
                    reason = "the screen changed while the agent was not acting"
            except Exception:
                reason = ""

        if reason:
            self.presence.human_active = True
            self.presence.last_human_at = time.time()
            self.presence.last_reason = reason
        elif self.presence.human_active and (
            time.time() - self.presence.last_human_at
        ) > self.grace:
            self.presence.human_active = False

        self._last_app = app or self._last_app
        self._last_png = png or self._last_png

    def _safe_app(self) -> str | None:
        try:
            return str(self.wda.active_app_info().get("bundleId") or "") or None
        except WDAError:
            return None

    def _safe_shot(self) -> bytes | None:
        try:
            return self.wda.screenshot()
        except WDAError:
            return None
