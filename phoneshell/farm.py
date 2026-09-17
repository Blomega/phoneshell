"""More than one phone on the same Mac.

Everything else in this project assumes one device, because for a benchmark that
is the honest shape: one phone, one cable, one result. But a rig with three
phones runs a sweep in a third of the wall clock, and AppScan wants to walk
several apps at once, so the single-phone assumption is a throughput ceiling
rather than a correctness requirement.

What makes this possible without rewriting the bridge is that `Bridge` now
carries its own `Config`. A farm is then a small amount of bookkeeping on top:
give each phone a config with its own udid and its own pair of ports, keep one
bridge per phone, and let every existing code path work unchanged inside it.

## Ports

Each device gets a slot, and a slot is a pair: WebDriverAgent on `8100 + slot`
and MJPEG on `9100 + slot`. Slots are written to disk and never reused while a
device is still known, because a device that swaps ports between runs breaks
every terminal, log line and bookmark that referred to it. The configured
device always holds slot 0, so a farm of one is byte-for-byte the setup that
already works.

## What this does not do

It does not make WebDriverAgent thread-safe, because nothing can: one phone is
one screen and one digitizer. Two bridges drive two phones in parallel; two
requests to the SAME phone still serialise, exactly as they did before.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .bringup import Bridge, diagnose, scan
from .config import Config, RUNTIME

log = logging.getLogger("phoneshell.farm")

SLOTS_PATH = RUNTIME / "farm-slots.json"

# One slot is one pair of ports. Sixteen is far past what a Mac's USB tree and
# one xcodebuild per phone will carry; the limit exists so a bug cannot walk off
# into the ephemeral port range.
MAX_SLOTS = 16
WDA_BASE = 8100
MJPEG_BASE = 9100


@dataclass
class Member:
    """One phone in the farm, and what this process knows about it."""
    udid: str
    name: str = ""
    model: str = ""
    ios: str = ""
    transport: str = ""
    source: str = ""
    connected: bool = False
    configured: bool = False
    slot: int = 0
    wda_port: int = WDA_BASE
    mjpeg_port: int = MJPEG_BASE
    state: str = "unknown"
    title: str = ""
    detail: str = ""
    fix: str = ""
    action: str = ""
    bridge: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items()}
        return data


class Farm:
    """Every phone this Mac can see, and a bridge for each one we have brought up.

    Deliberately lazy: a device gets a bridge the first time someone asks for
    one. Scanning is cheap and safe, connecting is neither, so nothing here
    touches a phone until it is told to.
    """

    def __init__(self, primary: Bridge | None = None) -> None:
        self._bridges: dict[str, Bridge] = {}
        self._lock = threading.Lock()
        self._slots: dict[str, int] = self._load_slots()
        # The helper already owns a bridge for the configured phone. Reuse it
        # rather than building a second object for the same device: two bridges
        # to one phone would each think they owned the tunnel.
        self.primary = primary
        if primary is not None:
            udid = primary.udid or Config.load().device.udid
            if udid:
                self._bridges[udid] = primary

    # ------------------------------------------------------------------ slots

    def _load_slots(self) -> dict[str, int]:
        try:
            raw = json.loads(SLOTS_PATH.read_text())
            return {str(k): int(v) for k, v in raw.items()}
        except Exception:
            return {}

    def _save_slots(self) -> None:
        try:
            SLOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SLOTS_PATH.write_text(json.dumps(self._slots, indent=2))
        except Exception as exc:
            log.debug("could not persist farm slots: %s", exc)

    def slot_for(self, udid: str) -> int:
        """This device's port slot, stable across restarts.

        The configured phone is always slot 0. That is not a tidiness rule: it
        means `phoneshell up` in a terminal, every port in every log line, and a
        farm of one all refer to the same 8100, so adding a second phone cannot
        silently move the first one.
        """
        configured = Config.load().device.udid
        if udid and udid == configured:
            if self._slots.get(udid) != 0:
                # Whoever held 0 gives it up; they get a fresh slot below.
                self._slots = {k: v for k, v in self._slots.items() if v != 0}
                self._slots[udid] = 0
                self._save_slots()
            return 0
        if udid in self._slots:
            return self._slots[udid]
        taken = set(self._slots.values()) | {0}
        for candidate in range(MAX_SLOTS):
            if candidate not in taken:
                self._slots[udid] = candidate
                self._save_slots()
                return candidate
        raise RuntimeError(f"the farm is full at {MAX_SLOTS} phones")

    def config_for(self, udid: str) -> Config:
        """A config scoped to one phone: its udid, its ports, everything else shared."""
        cfg = Config.load()
        slot = self.slot_for(udid)
        cfg.device.udid = udid
        cfg.wda.port = WDA_BASE + slot
        cfg.wda.mjpeg_port = MJPEG_BASE + slot
        return cfg

    # ---------------------------------------------------------------- bridges

    def bridge_for(self, udid: str) -> Bridge:
        with self._lock:
            bridge = self._bridges.get(udid)
            if bridge is None:
                bridge = Bridge(self.config_for(udid))
                self._bridges[udid] = bridge
            return bridge

    def known_bridges(self) -> dict[str, Bridge]:
        with self._lock:
            return dict(self._bridges)

    # ------------------------------------------------------------------ views

    def members(self, deep: bool = False) -> list[Member]:
        """Every phone this Mac can see, with its slot and its state.

        `deep` asks each phone we already hold a bridge for how it actually is,
        which costs an HTTP round trip per bridge. The grid refreshes on a timer,
        so it asks shallowly; a card opened on purpose asks deeply.
        """
        configured = Config.load().device.udid
        out: list[Member] = []
        for candidate in scan():
            udid = candidate.udid
            if not udid:
                continue
            slot = self.slot_for(udid)
            member = Member(
                udid=udid,
                name=(candidate.name or "").strip() or "iPhone",
                model=candidate.model,
                ios=candidate.ios_version,
                transport=candidate.transport,
                source=candidate.source,
                connected=candidate.connected,
                configured=udid == configured,
                slot=slot,
                wda_port=WDA_BASE + slot,
                mjpeg_port=MJPEG_BASE + slot,
            )
            bridge = self._bridges.get(udid)
            if bridge is not None:
                member.bridge = bridge.state()
                if deep:
                    try:
                        health = diagnose(bridge)
                        member.state = health.get("state", "unknown")
                        member.title = health.get("title", "")
                        member.detail = health.get("detail", "")
                        member.fix = health.get("fix", "")
                        member.action = health.get("action", "")
                    except Exception as exc:
                        member.state, member.title = "error", str(exc)[:160]
                elif member.bridge.get("up"):
                    member.state, member.title = "ready", f"{member.name} is ready"
                else:
                    member.state, member.title = "idle", "brought up before, not answering now"
            elif not candidate.connected:
                member.state = "absent"
                member.title = "not on the cable and not on this network"
            else:
                member.state = "standby"
                member.title = "plugged in, no bridge started for it yet"
            out.append(member)
        out.sort(key=lambda m: (m.slot, m.name))
        return out

    def state(self) -> dict:
        members = self.members()
        return {
            "members": [m.as_dict() for m in members],
            "slots": dict(self._slots),
            "live": sum(1 for m in members if m.bridge.get("up")),
            "seen": len(members),
        }

    def screenshot(self, udid: str) -> bytes | None:
        """One still from one phone, or None if it is not answering.

        A still and not a stream, and that is the whole design of the grid. Six
        MJPEG feeds is six phones encoding video continuously and six sockets
        held open for a page nobody is looking closely at; six stills on a timer
        is nothing until someone asks. The live feed belongs to the one card a
        person has actually opened.
        """
        from .wda.client import WDAClient
        cfg = self.config_for(udid)
        try:
            return WDAClient(base_url=cfg.wda_base_url, timeout=6).screenshot()
        except Exception as exc:
            log.debug("no screenshot from %s: %s", udid, exc)
            return None

    # ----------------------------------------------------------------- verbs

    def connect(self, udid: str, relaunch: bool = False) -> Iterator[dict]:
        """Bring one phone up on its own ports."""
        yield from self.bridge_for(udid).connect(udid, relaunch=relaunch)

    def disconnect(self, udid: str, stop_runner: bool = False) -> dict:
        bridge = self._bridges.get(udid)
        if bridge is None:
            return {"ok": True, "detail": "no bridge was held for that phone"}
        return {"ok": True, "bridge": bridge.disconnect(stop_runner=stop_runner)}
