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


def diagnose(bridge: "Bridge | None" = None) -> dict:
    """One sentence about why the phone is not usable, and what to do about it.

    This exists because the answers used to live in my head and in a terminal.
    The person using AppScan never sees either: they see a page that stopped
    working. Every failure this rig has produced is one of a small number of
    shapes, each with a different remedy, and telling them apart is cheap:

      no phone in usbmux        -> the cable, always. USB enumeration is
                                   kernel-level and happens before trust and
                                   before the lock screen, so a phone that is
                                   powered on and plugged into a DATA cable is
                                   visible here even locked and untrusted.
      phone present, port shut  -> the tunnel died, usually because the helper
                                   was restarted and iproxy was its child.
      port open, WDA silent     -> the runner stopped. Relaunch it.
      WDA fine, phone locked    -> iOS refuses to launch apps from the lock
                                   screen, and every launch fails with an
                                   unhelpful error until it is unlocked.
    """
    cfg = Config.load()
    want = cfg.device.udid
    seen = scan()
    # usbmux is the authority on "is it on the cable". CoreDevice keeps reporting
    # a transport for a phone that was unplugged a moment ago, which had this
    # calling an absent phone a dead runner and sending people to Relaunch.
    cabled = [c for c in seen if c.source == "usbmux"]
    wifi = [c for c in seen if c.connected and c.transport.lower() in {"wifi", "localnetwork"}]
    over_wifi = cfg.wda.transport == "wifi"
    live = cabled or (wifi if over_wifi else [])

    if not live:
        if wifi and not over_wifi:
            names = ", ".join(c.name for c in wifi)
            if cfg.wda.wifi_host:
                return {
                    "state": "wifi_available", "ok": False,
                    "title": "The cable is out, but the phone is on this wifi",
                    "detail": f"{names} is on the network at {cfg.wda.wifi_host}, so a cable is "
                              f"not needed.",
                    "fix": "Press Connect to carry on wirelessly. While that is up, anything on "
                           "this wifi can drive the phone, so disconnect when you are done.",
                    "action": "connect",
                }
            return {
                "state": "no_cable", "ok": False,
                "title": "The cable is out",
                "detail": f"{names} is on this network, but I have never seen it on a cable, so "
                          f"I do not know the address to reach it on. iOS only hands that out "
                          f"over USB.",
                "fix": "Plug it in once and press Connect. After that I can use wifi on its own, "
                       "cable or not.",
                "action": "",
            }
        names = ", ".join(f"{c.name} ({c.model})" for c in seen) or "none"
        return {
            "state": "no_device", "ok": False,
            "title": "No iPhone on the cable",
            "detail": f"This Mac cannot see a connected iPhone. Paired but not here: {names}.",
            "fix": "Plug the phone in with a cable that carries data, and unlock it. "
                   "If it was working a moment ago, the cable has come out.",
            "action": "",
        }

    chosen = next((c for c in live if c.udid == want), None)
    if want and chosen is None:
        present = ", ".join(f"{c.name} ({c.model})" for c in live)
        return {
            "state": "wrong_device", "ok": False,
            "title": "A different iPhone is plugged in",
            "detail": f"AppScan is set to {want[:8]}…, and what is here is {present}.",
            "fix": "Pick the phone that is actually connected under Devices, then press Connect.",
            "action": "connect",
        }
    chosen = chosen or live[0]

    import socket
    reach = cfg.wda.wifi_host if over_wifi and cfg.wda.wifi_host else cfg.wda.host
    port_open = False
    try:
        with socket.create_connection((reach, cfg.wda.port), timeout=1.2):
            port_open = True
    except OSError:
        pass

    if not port_open:
        return {
            "state": "no_tunnel", "ok": False,
            "title": "The phone is here but nothing is listening",
            "detail": f"{chosen.name} is reachable, but nothing answers on {reach}:{cfg.wda.port}.",
            "fix": "Press Connect. (If AppScan's helper was restarted, this is expected: "
                   "the tunnel belongs to the helper and goes with it.)",
            "action": "connect",
        }

    client = WDAClient(base_url=cfg.wda_base_url, timeout=5)
    try:
        status = client.status()
    except WDAError as exc:
        text = str(exc)
        # An iproxy from a previous helper outlives it, keeps the port, accepts
        # the connection and resets it. The port looks open and nothing is behind
        # it, and no amount of relaunching the runner helps (FINDINGS s29).
        if "reset by peer" in text or "Connection reset" in text:
            return {
                "state": "stale_tunnel", "ok": False,
                "title": "The tunnel to the phone is a leftover",
                "detail": f"Port {cfg.wda.port} is held by a tunnel from an earlier session that "
                          f"accepts connections and answers nothing.",
                "fix": "Press Connect. It clears the old tunnel before opening a new one.",
                "action": "connect",
            }
        return {
            "state": "runner_dead", "ok": False,
            "title": "The runner on the phone stopped answering",
            "detail": text[:160],
            "fix": "Press Relaunch. If two of those do not fix it, the phone needs a restart.",
            "action": "relaunch",
        }

    try:
        if client.is_locked():
            return {
                "state": "locked", "ok": False,
                "title": "The phone is locked",
                "detail": "iOS refuses to launch apps from the lock screen, so a scan would "
                          "fail on its first step.",
                "fix": "Unlock the phone. To keep it from locking during long scans, set "
                       "Settings > Display & Brightness > Auto-Lock to Never.",
                "action": "",
            }
    except WDAError:
        pass

    return {
        "state": "ready", "ok": True,
        "title": f"{status.get('device') or chosen.name} is ready",
        "detail": f"iOS {status.get('os', {}).get('version')} · "
                  f"WebDriverAgent {status.get('build', {}).get('version')}",
        "fix": "", "action": "",
    }


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

    def heal(self) -> list[dict]:
        """Put the bridge back without being asked.

        The helper owns the usbmux tunnel as a child process, so restarting the
        helper takes the tunnel with it and the phone goes dark through no fault
        of the person using it. Telling them to press Connect is not a fix, it is
        a chore invented by an implementation detail. So on startup: if a phone is
        there and the runner is installed, connect.

        Silent on purpose when there is nothing to do, and it never fights a
        healthy bridge: `connect` adopts one that already answers.
        """
        try:
            state = diagnose(self)
        except Exception as exc:                       # never block startup
            log.debug("heal: diagnose failed: %s", exc)
            return []
        if state["state"] in {"ready", "no_device", "wrong_device", "locked"}:
            return [state]
        log.info("healing the bridge: %s", state["title"])
        return list(self.connect(Config.load().device.udid))

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

            # No cable, but the phone is on this network: drive it over wifi
            # instead of asking for a cable that is not needed.
            on_cable = any(c.source == "usbmux" and c.udid == resolved for c in scan())
            if not on_cable:
                yield from self._connect_wifi(resolved, cfg, relaunch)
                return

            # Pin the choice, so nothing later in this process can substitute a
            # different phone. device_check refuses to substitute only because the
            # config tells it which one to want.
            if cfg.device.udid != resolved:
                cfg.device.udid = resolved
                cfg.device.name = check.data.get("raw", {}).get("DeviceName") or cfg.device.name
                cfg.save()
                yield _ev("pinned", True, f"config now pins {resolved}")

            if cfg.wda.transport == "wifi":
                # The cable is back. Prefer it: it is faster, and it does not put
                # an unauthenticated automation server on the wifi.
                cfg.wda.transport = "usb"
                cfg.save()
                yield _ev("transport", True, "the cable is back, so using it instead of wifi")
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
            yield from self._learn_wifi_address(resolved, cfg)
            yield from self._finish(cfg)

    def _connect_wifi(self, udid: str, cfg: Config, relaunch: bool) -> Iterator[dict]:
        """Drive the phone over the network, with no cable in it.

        Everything except the HTTP works without a cable already: CoreDevice keeps
        its own tunnel to a paired phone on the same network, and `devicectl` can
        launch the runner across it. What is missing is an address to talk to
        WebDriverAgent on, and that is the part iOS will not hand over wirelessly:
        lockdown answers it over usbmux, and mDNS only advertises the phone when
        wifi sync is on. So the address is LEARNED during a cable connection and
        remembered. One cable connection, ever; wifi from then on.

        The runner also has to bind to something other than loopback to be
        reachable at all, which means anything on this network can drive the phone
        while the bridge is up: WebDriverAgent authenticates nothing. That is a
        real trade and the page says so rather than burying it here.
        """
        host = cfg.wda.wifi_host
        if not host:
            yield _ev(
                "wifi", False,
                "I can see this phone on the network, but I have never seen it on a cable, "
                "so I do not know its address.",
                "Plug it in once and press Connect. I will learn the address and use wifi "
                "from then on, cable or not.")
            return

        yield _ev("wifi", True, f"no cable, so going over the network to {host}")
        cfg.wda.transport = "wifi"
        cfg.wda.wifi_host = host
        cfg.save()
        self.udid = udid

        client = WDAClient(base_url=cfg.wda_base_url, timeout=8)
        if relaunch or not client.is_alive():
            killed = dev.terminate_runner(udid)
            if killed:
                yield _ev("terminate", True, f"stopped {killed} running runner process(es)")
            # bind_ip=None on purpose: loopback-only is unreachable over wifi.
            res = dev.launch_wda(udid, cfg.wda.runner_bundle_id, cfg.wda.port,
                                 cfg.wda.mjpeg_port, bind_ip=None)
            yield _ev("launch", res.ok, res.detail, res.fix)
            if not res.ok:
                self.last_error = res.detail
                return

        for attempt in range(40):
            if client.is_alive():
                break
            if attempt and attempt % 8 == 0:
                yield _ev("wait", True, f"waiting for WebDriverAgent on {host}… {attempt}s")
            time.sleep(1)

        if not client.is_alive():
            self.last_error = f"no answer from {host}:{cfg.wda.port}"
            yield _ev("wifi", False, self.last_error,
                      "The phone may have moved to another network, or its address changed. "
                      "Plug it in once to relearn the address.")
            return

        yield _ev("exposure", True,
                  "While this is up, anything on this wifi can drive the phone: WebDriverAgent "
                  "has no password. Disconnect when you are done, or use the cable.")
        self.adopted = False
        self.started_at = time.time()
        yield from self._finish(cfg)

    def _learn_wifi_address(self, udid: str, cfg: Config) -> Iterator[dict]:
        """Ask the phone its wifi address while the cable is in, and keep it.

        This is the whole cost of wireless working later, and it is one lockdown
        call on a connection that is already open.
        """
        try:
            ip = dev.phone_lan_ip(udid)
        except Exception as exc:                       # never fail a connect over this
            log.debug("lan ip: %s", exc)
            return
        if ip and ip != cfg.wda.wifi_host:
            cfg.wda.wifi_host = ip
            cfg.save()
            yield _ev("wifi", True, f"learned its wifi address ({ip}), so the cable is optional "
                                    f"from now on")

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
