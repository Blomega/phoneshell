"""Bringing the bridge up from the local app instead of from a terminal.

`phoneshell up` already does this, but it is a foreground command that supervises
until you stop it, so the web app could only ever tell you to go and run it. This
module is the same ladder as a long-lived object: scan, check, connect, supervise,
drop. The browser gets to do what the terminal does.

The ladder is deliberately the same one `device.py` documents, in the same order,
because every rung fails differently and has its own remedy:

    usbmux sees the cable  ->  iproxy forwards 8100  ->  the runner is installed
    ->  the runner launches  ->  WDA answers /status  ->  gestures actually move

Two things here are not in the CLI path and matter for an unattended run:

* The supervisor relaunch TERMINATES the runner first. `devicectl process launch`
  on a live process is a no-op that reports success, so a wedged runner whose HTTP
  has died gets "relaunched" forever while the corpse keeps the port (FINDINGS s29).
* Connecting takes the same exclusive lock a benchmark run takes, so the page
  cannot bring up a second bridge behind a sweep's back.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import device as dev
from .config import Config, LOGS
from .wda.client import WDAClient, WDAError

log = logging.getLogger("phoneshell.bringup")

Emit = Callable[[str, str, dict], None]


@dataclass
class Candidate:
    """A phone this Mac can see, and whether it is the configured one."""
    udid: str
    name: str
    model: str
    ios_version: str
    transport: str
    source: str            # "usbmux" (cable is in) | "devicectl" (paired, maybe over wifi)
    configured: bool
    connected: bool

    def as_dict(self) -> dict:
        return {
            "udid": self.udid, "name": self.name, "model": self.model,
            "ios": self.ios_version, "transport": self.transport, "source": self.source,
            "configured": self.configured, "connected": self.connected,
        }


def scan() -> list[Candidate]:
    """Every phone this Mac can see right now, cable first.

    usbmux is asked first on purpose: it sees a device the moment the cable is in,
    before CoreDevice has built a tunnel, and USB enumeration happens below trust
    and below the lock screen. A phone that is absent from BOTH lists is a cable
    problem, not a software one.
    """
    cfg = Config.load()
    want = cfg.device.udid
    found: dict[str, Candidate] = {}
    for d in dev.usbmux_devices():
        udid = str(d.get("Identifier") or d.get("UniqueDeviceID") or "")
        if not udid:
            continue
        found[udid] = Candidate(
            udid=udid,
            name=str(d.get("DeviceName") or "iPhone"),
            model=str(d.get("ProductType") or ""),
            ios_version=str(d.get("ProductVersion") or ""),
            transport=str(d.get("ConnectionType") or "USB"),
            source="usbmux",
            configured=udid == want,
            connected=True,
        )
    for d in dev.list_devices():
        if not d.udid or d.udid in found:
            continue
        found[d.udid] = Candidate(
            udid=d.udid, name=d.name.strip() or "iPhone", model=d.model,
            ios_version=d.ios_version, transport=d.transport or "unknown",
            source="devicectl", configured=d.udid == want, connected=d.connected,
        )
    # The configured phone sorts first, then whatever is on a cable.
    return sorted(found.values(), key=lambda c: (not c.configured, c.source != "usbmux", c.name))


def checks_for(udid: str | None) -> list[dict]:
    """The doctor ladder for one phone, as data the page can render.

    Deliberately does NOT include `input_check`: that one swipes the screen, and a
    page that swipes the user's phone every time it refreshes a status panel is
    not a diagnostic, it is a poltergeist. The crawler runs it once before a run.
    """
    cfg = Config.load()
    out: list[dev.Check] = [dev.xcode_ok(), dev.device_check(udid or cfg.device.udid or None)]
    resolved = out[-1].data.get("udid") or udid or cfg.device.udid
    if out[-1].ok:
        out.append(dev.developer_mode_check(resolved))
        out.append(dev.ddi_check(resolved))
    app_path = dev.wda_paths()["app"]
    out.append(dev.Check("wda-build", app_path.exists(),
                         str(app_path) if app_path.exists() else "not built yet",
                         fix="run `bin/phoneshell setup` in a terminal (it needs Xcode and takes a few minutes)"))
    if resolved:
        out.append(dev.runner_installed_check(resolved, cfg.wda.runner_bundle_id))
    return [{"name": c.name, "ok": c.ok, "detail": c.detail, "fix": c.fix} for c in out]


class Bridge:
    """The live connection to one phone, owned by this process.

    One instance per server. `connect` is idempotent: if WDA already answers, it
    adopts the existing bridge (started by a terminal `phoneshell up`, say) rather
    than fighting it, and says so.
    """

    def __init__(self) -> None:
        self.udid: str | None = None
        self.forward: dev.PortForward | None = None
        self.held: subprocess.Popen | None = None      # tethered launcher, if we needed one
        self.adopted = False                            # someone else's bridge
        self._supervisor: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.last_error: str = ""
        self.started_at: float = 0.0

    # ------------------------------------------------------------------ state

    def alive(self) -> bool:
        cfg = Config.load()
        try:
            return WDAClient(base_url=cfg.wda_base_url, timeout=4).is_alive()
        except Exception:
            return False

    def state(self) -> dict:
        return {
            "udid": self.udid,
            "up": self.alive(),
            "owned": self.forward is not None or self.held is not None,
            "adopted": self.adopted,
            "tethered": self.held is not None and self.held.poll() is None,
            "supervising": bool(self._supervisor and self._supervisor.is_alive()),
            "since": self.started_at or None,
            "error": self.last_error,
        }

    # ---------------------------------------------------------------- connect

    def connect(self, udid: str | None = None, relaunch: bool = False) -> Iterator[dict]:
        """Walk the ladder, yielding one event per rung so the page can show it.

        Generator rather than a function with a callback because the whole point
        is that the browser watches it happen; a connect that takes 40 seconds
        and says nothing is the thing this replaces.
        """
        with self._lock:
            cfg = Config.load()
            self.last_error = ""

            if not relaunch and self.alive():
                self.adopted = self.forward is None and self.held is None
                self.udid = udid or cfg.device.udid or self.udid
                self.started_at = self.started_at or time.time()
                yield _ev("wda", True, "WebDriverAgent is already answering"
                          + (" (bridge started outside this app)" if self.adopted else ""))
                yield from self._finish(cfg)
                return

            check = dev.device_check(udid or cfg.device.udid or None)
            yield _ev("device", check.ok, check.detail, check.fix)
            if not check.ok:
                self.last_error = check.detail
                return
            resolved = check.data.get("udid") or udid or cfg.device.udid
            if not resolved:
                yield _ev("device", False, "no UDID for the selected phone")
                return
            self.udid = resolved

            # Pin the choice, so nothing later in this process can substitute a
            # different phone. device_check refuses to substitute only because the
            # config tells it which one to want.
            if cfg.device.udid != resolved:
                cfg.device.udid = resolved
                cfg.device.name = check.data.get("raw", {}).get("DeviceName") or cfg.device.name
                cfg.save()
                yield _ev("pinned", True, f"config now pins {resolved}")

            if cfg.wda.transport != "wifi":
                if self.forward:
                    self.forward.stop()
                self.forward = dev.PortForward(resolved)
                res = self.forward.start()
                yield _ev("forward", res.ok, res.detail, res.fix)
                if not res.ok:
                    self.last_error = res.detail
                    return

            installed = dev.runner_installed_check(resolved, cfg.wda.runner_bundle_id)
            yield _ev("runner", installed.ok, installed.detail, installed.fix)
            if not installed.ok:
                self.last_error = installed.detail
                return

            client = WDAClient(base_url=cfg.wda_base_url, timeout=10)
            if relaunch or not client.is_alive():
                # Terminate before launching. A launch onto a live process is a
                # no-op that reports success (FINDINGS s29), which is how a wedged
                # runner survives twenty minutes of "relaunching".
                killed = dev.terminate_runner(resolved)
                if killed:
                    yield _ev("terminate", True, f"stopped {killed} running runner process(es)")
                res = dev.launch_wda(
                    resolved, cfg.wda.runner_bundle_id, cfg.wda.port, cfg.wda.mjpeg_port,
                    bind_ip="127.0.0.1" if cfg.wda.bind_loopback and cfg.wda.transport != "wifi" else None,
                )
                yield _ev("launch", res.ok, res.detail, res.fix)
                if not res.ok:
                    yield _ev("launch", True, "falling back to a tethered DVT session")
                    self.held = dev.launch_wda_dvt(resolved, cfg.wda.runner_bundle_id,
                                                   LOGS / "wda-dvt.log", cfg.wda.port, cfg.wda.mjpeg_port)

            for attempt in range(40):
                if client.is_alive():
                    break
                if attempt and attempt % 8 == 0:
                    yield _ev("wait", True, f"waiting for WebDriverAgent… {attempt}s")
                time.sleep(1)

            if not client.is_alive() and self.held is None:
                # The detached launch exits 0 whether or not the runner survived
                # its own bootstrap, so this is the expected shape of that lie.
                yield _ev("launch", False, "the detached runner never answered",
                          "falling back to a tethered session")
                self.held = dev.launch_wda_dvt(resolved, cfg.wda.runner_bundle_id,
                                               LOGS / "wda-dvt.log", cfg.wda.port, cfg.wda.mjpeg_port)
                for _ in range(45):
                    if client.is_alive():
                        break
                    time.sleep(1)

            if not client.is_alive():
                self.last_error = "WebDriverAgent did not come up"
                yield _ev("wda", False, self.last_error,
                          "run `bin/phoneshell doctor` in a terminal: it names the broken rung")
                return

            self.adopted = False
            self.started_at = time.time()
            yield from self._finish(cfg)

    def _finish(self, cfg: Config) -> Iterator[dict]:
        client = WDAClient(base_url=cfg.wda_base_url, timeout=10)
        try:
            status = client.status()
            ios = status.get("os", {}).get("version")
            yield _ev("wda", True, f"{status.get('device')} on iOS {ios}, "
                                   f"WebDriverAgent {status.get('build', {}).get('version')}",
                      data={"device": status.get("device"), "ios": ios})
        except WDAError as exc:
            yield _ev("wda", False, str(exc))
            return
        try:
            if client.is_locked():
                yield _ev("locked", False, "the phone is locked",
                          "unlock it, or store the passcode so phoneshell can type it")
        except WDAError:
            pass
        self._start_supervisor()
        yield _ev("ready", True, "the bridge is up")

    # ------------------------------------------------------------- supervise

    def _start_supervisor(self) -> None:
        if self._supervisor and self._supervisor.is_alive():
            return
        self._stop.clear()
        self._supervisor = threading.Thread(target=self._supervise, name="bridge-supervisor", daemon=True)
        self._supervisor.start()

    def _supervise(self) -> None:
        """Relaunch the runner if it stops answering, twice, then give up loudly.

        Two misses before acting, because a single miss is what one slow tree read
        on a busy screen looks like. Giving up loudly beats an infinite relaunch
        loop: if two launches do not fix it, the fault is the HID layer or the
        cable and only a human can clear it.
        """
        cfg = Config.load()
        client = WDAClient(base_url=cfg.wda_base_url, timeout=6)
        misses = 0
        relaunches = 0
        while not self._stop.wait(5):
            if client.is_alive():
                misses = 0
                continue
            misses += 1
            if misses < 2:
                continue
            if relaunches >= 2 or not self.udid:
                self.last_error = ("the runner stopped answering and two relaunches did not fix it; "
                                   "reboot the phone")
                log.error(self.last_error)
                return
            relaunches += 1
            misses = 0
            log.warning("runner stopped answering, relaunch %d", relaunches)
            dev.terminate_runner(self.udid)
            if self.held is not None and self.held.poll() is not None:
                self.held = dev.launch_wda_dvt(self.udid, cfg.wda.runner_bundle_id,
                                               LOGS / "wda-dvt.log", cfg.wda.port, cfg.wda.mjpeg_port)
            else:
                dev.launch_wda(self.udid, cfg.wda.runner_bundle_id, cfg.wda.port, cfg.wda.mjpeg_port,
                               bind_ip="127.0.0.1" if cfg.wda.bind_loopback else None)

    # ------------------------------------------------------------- disconnect

    def disconnect(self, stop_runner: bool = False) -> dict:
        """Drop this process's hold on the phone.

        Does not stop a bridge it merely adopted: something else started it and
        something else gets to end it.
        """
        self._stop.set()
        if self.held is not None and self.held.poll() is None:
            self.held.terminate()
            self.held = None
        if self.forward:
            self.forward.stop()
            self.forward = None
        if stop_runner and self.udid and not self.adopted:
            dev.terminate_runner(self.udid)
        self.started_at = 0.0
        return self.state()


def _ev(step: str, ok: bool, detail: str = "", fix: str = "", data: dict | None = None) -> dict:
    return {"type": "step", "step": step, "ok": ok, "detail": detail,
            "fix": fix, "data": data or {}, "at": time.time()}
