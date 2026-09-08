"""Getting a physical iPhone from 'plugged in' to 'driveable'.

The bootstrap is six steps, and every one of them fails in its own way, so each
is a separate check with its own remedy rather than one opaque setup script.

  1. Xcode selected      - DEVELOPER_DIR, because Command Line Tools is selected
                           on this machine and devicectl only ships with Xcode
  2. Device visible      - paired, trusted, and actually connected
  3. Developer Mode      - on, and the device rebooted after turning it on
  4. WDA built + signed  - with the Blomega team, into a path the simulator and
                           the installer can both read
  5. WDA installed       - and stripped of its embedded XCTest frameworks, which
                           iOS 17+ refuses to launch
  6. WDA running         - launched detached with devicectl so nothing on the Mac
                           has to stay attached, then reachable on port 8100

Notes that cost real time to discover:

* Build products must NOT live under ~/Desktop. The simulator and the install
  path run as other processes without TCC access to it, and a build there hangs
  the runner in an open() syscall with no error message at all. Measured here.
* Only `xcrun devicectl device process launch` is fire-and-forget on iOS 26.
  xcodebuild test-without-building, `ios runwda` and pymobiledevice3's
  XCUITestService all hold the testmanagerd session open, so WDA dies with them.
* That devicectl launch path is reported dead on iOS 27, which ships in
  mid-September 2026. Do not let the phone auto-update before that is verified.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, WDA_SRC

DEVELOPER_DIR = "/Applications/Xcode.app/Contents/Developer"
# Build products live here, never under ~/Desktop or ~/Documents: those are
# TCC-protected and a build there hangs the runner with no diagnostic.
BUILD_ROOT = Path.home() / "Library" / "Developer" / "phoneshell"
DEVICE_DD = BUILD_ROOT / "wda-device"
SIM_DD = BUILD_ROOT / "wda-sim"

def detect_team() -> str | None:
    """The Apple team id to sign WebDriverAgent with, taken from THIS Mac.

    Never hardcode a team. A team id in the source means anyone who clones this
    builds a runner signed as that company and installs it on their own device,
    which breaches the Apple Developer Program agreement and endangers the
    account. Configure `wda.development_team` to pin a specific one.
    """
    res = run(["security", "find-identity", "-v", "-p", "codesigning"], timeout=45)
    for line in res.stdout.splitlines():
        if '"' not in line:
            continue
        name = line.split('"')[1]
        cert = subprocess.run(["security", "find-certificate", "-c", name, "-p"],
                              capture_output=True, text=True, timeout=30).stdout
        if not cert:
            continue
        subject = subprocess.run(["openssl", "x509", "-noout", "-subject"], input=cert,
                                 capture_output=True, text=True, timeout=30).stdout
        for part in subject.split("/"):
            if part.startswith("OU="):
                return part[3:].strip()
    return None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["DEVELOPER_DIR"] = DEVELOPER_DIR
    env["PATH"] = f"{DEVELOPER_DIR}/usr/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def run(cmd: list[str], timeout: float = 120, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=_env(), check=check
    )


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    data: dict = field(default_factory=dict)

    def line(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        out = f"[{mark}] {self.name}: {self.detail}"
        if not self.ok and self.fix:
            out += f"\n       fix: {self.fix}"
        return out


@dataclass
class Device:
    udid: str            # 40-char or 25-char hardware UDID
    identifier: str      # CoreDevice UUID used by devicectl
    name: str
    model: str
    ios_version: str
    state: str
    transport: str
    connected: bool


# --------------------------------------------------------------------- discovery


def xcode_ok() -> Check:
    if not Path(DEVELOPER_DIR).exists():
        return Check("xcode", False, "Xcode.app is not installed",
                     fix="install Xcode from the App Store")
    res = run(["xcodebuild", "-version"], timeout=60)
    version = res.stdout.splitlines()[0] if res.stdout else "?"
    selected = run(["xcode-select", "-p"], timeout=30).stdout.strip()
    detail = f"{version} (xcode-select points at {selected})"
    if "CommandLineTools" in selected:
        detail += " - phoneshell exports DEVELOPER_DIR itself, so this is fine"
    return Check("xcode", True, detail, data={"version": version})


def list_devices() -> list[Device]:
    """Every paired iPhone devicectl knows about, connected or not."""
    out = run(["xcrun", "devicectl", "list", "devices", "--json-output", "-"], timeout=90)
    devices: list[Device] = []
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return devices
    for item in payload.get("result", {}).get("devices", []):
        props = item.get("hardwareProperties", {})
        conn = item.get("connectionProperties", {})
        state = conn.get("tunnelState") or item.get("deviceProperties", {}).get("state") or "unknown"
        devices.append(Device(
            udid=str(props.get("udid") or ""),
            identifier=str(item.get("identifier") or ""),
            name=str(item.get("deviceProperties", {}).get("name") or ""),
            model=str(props.get("marketingName") or props.get("productType") or ""),
            ios_version=str(item.get("deviceProperties", {}).get("osVersionNumber") or ""),
            state=str(state),
            transport=str(conn.get("transportType") or ""),
            # tunnelState is NOT the connectivity signal: a phone sitting on the
            # cable still reports "disconnected" because the RemoteXPC tunnel is
            # built on demand. What actually distinguishes present from absent is
            # transportType (wired / localNetwork), which is null for a device
            # devicectl has not seen. Verified against this Mac with the phone
            # both plugged in and unplugged.
            connected=bool(conn.get("transportType")) and conn.get("pairingState") == "paired",
        ))
    return devices


def usbmux_devices() -> list[dict]:
    """Cross-check with the Xcode-free stack; this sees a device the moment the
    cable is in, even before CoreDevice has a tunnel to it."""
    pmd = shutil.which("pymobiledevice3") or str(Path.home() / ".local/bin/pymobiledevice3")
    if not Path(pmd).exists():
        return []
    res = run([pmd, "usbmux", "list"], timeout=45)
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


def device_check() -> Check:
    usb = usbmux_devices()
    devices = list_devices()
    live = [d for d in devices if d.connected]
    if usb:
        d = usb[0]
        return Check(
            "device", True,
            f"{d.get('DeviceName', '?')} ({d.get('ProductType', '?')}) "
            f"iOS {d.get('ProductVersion', '?')} over {d.get('ConnectionType', 'USB')}",
            data={"udid": d.get("Identifier") or d.get("UniqueDeviceID"), "raw": d},
        )
    if live:
        d = live[0]
        return Check("device", True,
                     f"{d.name.strip()} ({d.model}) iOS {d.ios_version} over {d.transport or 'usb'}",
                     data={"udid": d.udid, "identifier": d.identifier})
    known = ", ".join(f"{d.name.strip()} [{d.state}]" for d in devices) or "none paired"
    return Check(
        "device", False, f"no iPhone connected (paired but idle: {known})",
        fix="plug the iPhone into this Mac with a cable and unlock it, then tap Trust",
    )


def developer_mode_check(udid: str | None) -> Check:
    """Developer Mode has to be on or nothing developer-signed will launch."""
    pmd = shutil.which("pymobiledevice3") or str(Path.home() / ".local/bin/pymobiledevice3")
    if not Path(pmd).exists():
        return Check("developer-mode", False, "pymobiledevice3 is not installed",
                     fix="pipx install pymobiledevice3")
    cmd = [pmd, "amfi", "developer-mode-status"]
    if udid:
        cmd += ["--udid", udid]
    res = run(cmd, timeout=60)
    text = (res.stdout + res.stderr).strip().lower()
    if "true" in text:
        return Check("developer-mode", True, "enabled")
    if "false" in text:
        return Check(
            "developer-mode", False, "disabled",
            fix=("on the phone: Settings > Privacy & Security > Developer Mode > on, then let it "
                 "reboot. Doing this from the Mac instead needs the passcode removed first, so the "
                 "Settings toggle is the better route."),
        )
    return Check("developer-mode", False, f"could not read status ({text[:120]})",
                 fix="make sure the phone is unlocked and trusted")


def ddi_check(udid: str | None) -> Check:
    """Is a Developer Disk Image mounted?

    Without it the developer services XCTest needs are simply absent, and the
    failure downstream reads as a launch error rather than a missing image.
    """
    pmd = shutil.which("pymobiledevice3") or str(Path.home() / ".local/bin/pymobiledevice3")
    if not Path(pmd).exists():
        return Check("developer-image", False, "pymobiledevice3 is not installed",
                     fix="pipx install pymobiledevice3")
    cmd = [pmd, "mounter", "list"]
    if udid:
        cmd += ["--udid", udid]
    res = run(cmd, timeout=90)
    text = (res.stdout + res.stderr).strip()
    if "Developer" in text or "personalized" in text.lower():
        return Check("developer-image", True, "mounted")
    return Check("developer-image", False, "no developer image mounted",
                 fix=f"{pmd} mounter auto-mount   (downloads and mounts it, no Xcode needed)")


def lan_exposure_check(cfg, udid: str | None) -> Check:
    """Is WebDriverAgent's port open to everyone on the wifi?

    WDA authenticates nothing, so this matters. It binds every interface unless
    USE_IP was honoured at launch, and whether a physical device honours USE_IP
    is not something anyone has verified, so this probes the phone's own LAN
    address instead of assuming.
    """
    import socket
    ip = phone_lan_ip(udid)
    if not ip:
        return Check("lan-exposure", True, "could not read the phone's wifi address, nothing to probe")
    try:
        with socket.create_connection((ip, cfg.wda.port), timeout=1.5):
            return Check(
                "lan-exposure", False,
                f"SECURITY: WebDriverAgent answers on {ip}:{cfg.wda.port} to ANYONE on this wifi, "
                "with no credentials of any kind. That is full remote control of the phone: "
                "read the screen, read the pasteboard, tap anything.",
                fix=("relaunch the bridge with `phoneshell up`, which passes USE_IP=127.0.0.1 to "
                     "bind it to the phone's loopback. Do not run in this state on a shared "
                     "network, and treat this check as a failure rather than a warning."),
            )
    except OSError:
        return Check("lan-exposure", True, f"{ip}:{cfg.wda.port} is closed to the LAN, good")


def phone_lan_ip(udid: str | None) -> str | None:
    """The phone's wifi address, straight from lockdown."""
    pmd = shutil.which("pymobiledevice3") or str(Path.home() / ".local/bin/pymobiledevice3")
    if not Path(pmd).exists():
        return None
    cmd = [pmd, "lockdown", "info"]
    if udid:
        cmd += ["--udid", udid]
    res = run(cmd, timeout=60)
    try:
        info = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    for key in ("WiFiAddress", "wifi_address"):
        if info.get(key):
            pass
    # lockdown reports the MAC under WiFiAddress; the routable address lives in
    # the network domain, so ask for it explicitly.
    cmd = [pmd, "lockdown", "get", "com.apple.mobile.wireless_lockdown"]
    if udid:
        cmd += ["--udid", udid]
    res = run(cmd, timeout=60)
    match = None
    try:
        data = json.loads(res.stdout)
        for k, v in (data or {}).items():
            if isinstance(v, str) and v.count(".") == 3:
                match = v
    except json.JSONDecodeError:
        pass
    return match


# ------------------------------------------------------------------------- WDA


def signing_identity(team: str | None = None) -> tuple[str, str] | None:
    """(sha1, common name) of a codesigning identity belonging to `team`.

    Two Apple Development certs live in this keychain, so the identity is passed
    to xcodebuild as a SHA-1 hash: a display name would be ambiguous.
    """
    team = team or detect_team()
    if not team:
        return None
    res = run(["security", "find-identity", "-v", "-p", "codesigning"], timeout=45)
    for line in res.stdout.splitlines():
        if '"' not in line:
            continue
        sha1 = line.split()[1]
        name = line.split('"')[1]
        cert = subprocess.run(
            ["security", "find-certificate", "-c", name, "-p"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        if not cert:
            continue
        subject = subprocess.run(
            ["openssl", "x509", "-noout", "-subject"],
            input=cert, capture_output=True, text=True, timeout=30,
        ).stdout
        if f"OU={team}" in subject:
            return sha1, name
    return None


def wda_paths(for_device: bool = True) -> dict[str, Path]:
    dd = DEVICE_DD if for_device else SIM_DD
    products = dd / "Build" / "Products"
    flavour = "Debug-iphoneos" if for_device else "Debug-iphonesimulator"
    return {
        "derived": dd,
        "products": products,
        "app": products / flavour / "WebDriverAgentRunner-Runner.app",
    }


def build_wda(udid: str, bundle_id: str, team: str | None = None,
              log_path: Path | None = None) -> Check:
    team = team or detect_team()
    if not team:
        return Check("wda-build", False, "no Apple codesigning identity found on this Mac",
                     fix="open Xcode > Settings > Accounts, add your Apple ID and download the certificates")
    identity = signing_identity(team)
    if identity is None:
        return Check("wda-build", False, f"no codesigning identity for team {team}",
                     fix="open Xcode > Settings > Accounts and download your certificates")
    sha1, name = identity
    DEVICE_DD.mkdir(parents=True, exist_ok=True)
    cmd = [
        "xcodebuild", "build-for-testing",
        "-project", str(WDA_SRC / "WebDriverAgent.xcodeproj"),
        "-scheme", "WebDriverAgentRunner",
        "-destination", f"id={udid}",
        "-derivedDataPath", str(DEVICE_DD),
        "-allowProvisioningUpdates", "-allowProvisioningDeviceRegistration",
        f"DEVELOPMENT_TEAM={team}",
        # Automatic signing refuses an explicit identity hash ("conflicting
        # provisioning settings"), so the build gets the generic name and
        # DEVELOPMENT_TEAM does the disambiguating between the two Apple
        # Development certs in this keychain. The SHA-1 is still used later, for
        # the re-sign after the XCTest frameworks are stripped, where a direct
        # codesign call does accept it.
        "CODE_SIGN_IDENTITY=Apple Development",
        "CODE_SIGN_STYLE=Automatic",
        f"PRODUCT_BUNDLE_IDENTIFIER={bundle_id}",
        "GCC_TREAT_WARNINGS_AS_ERRORS=0",
        "COMPILER_INDEX_STORE_ENABLE=NO",
    ]
    res = run(cmd, timeout=1800)
    if log_path:
        log_path.write_text(res.stdout + "\n" + res.stderr)
    if "BUILD SUCCEEDED" not in res.stdout:
        tail = (res.stdout + res.stderr).strip().splitlines()[-25:]
        return Check("wda-build", False, "xcodebuild failed:\n" + "\n".join(tail)[:1200],
                     fix="see the full log; the usual cause is a provisioning profile that needs Xcode opened once")
    return Check("wda-build", True, f"built and signed with {name}",
                 data={"app": str(wda_paths()['app']), "identity": sha1})


def strip_xctest_frameworks(app: Path, identity_sha1: str) -> Check:
    """iOS 17+ refuses to launch a runner that carries its own XCTest.

    testmanagerd was renamed in iOS 17 and the embedded frameworks bind to the
    old one, so the bundle has to link the device's copies instead. The failure
    without this is a generic launch error that says nothing about frameworks.
    """
    frameworks = app / "Frameworks"
    removed = []
    if frameworks.exists():
        for item in frameworks.iterdir():
            if item.name.startswith("XC") or item.name in {"Testing.framework", "libXCTestSwiftSupport.dylib"}:
                shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)
                removed.append(item.name)
    if removed:
        res = run([
            "codesign", "--force", "--sign", identity_sha1,
            "--preserve-metadata=identifier,entitlements,flags", str(app),
        ], timeout=120)
        if res.returncode != 0:
            return Check("wda-strip", False, f"re-signing failed: {res.stderr.strip()[:300]}")
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file()) / 1e6
    return Check("wda-strip", True, f"removed {len(removed)} embedded frameworks, bundle is {size:.1f} MB",
                 data={"removed": removed})


def install_wda(udid: str, app: Path) -> Check:
    res = run(["xcrun", "devicectl", "device", "install", "app", "--device", udid, str(app)], timeout=600)
    if res.returncode != 0:
        return Check("wda-install", False, (res.stderr or res.stdout).strip()[:400],
                     fix="unlock the phone and keep it unlocked during install")
    return Check("wda-install", True, f"installed {app.name}")


def installed_bundle_ids(udid: str) -> set[str] | None:
    """Every app bundle id the phone currently has, or None if we cannot ask."""
    res = run(["xcrun", "devicectl", "device", "info", "apps", "--device", udid], timeout=180)
    if res.returncode != 0:
        return None
    ids = set()
    for line in (res.stdout or "").splitlines():
        for word in line.split():
            if word.count(".") >= 2 and not word.endswith(".app"):
                ids.add(word)
    return ids or None


def runner_installed_check(udid: str | None, runner_bundle_id: str) -> Check:
    """Is the runner still on the phone?

    iOS offloads apps it decides are unused, and it does not spare a developer
    build: measured here, the phone silently removed the WebDriverAgent runner
    (and the Weather app) part-way through a benchmark run. Worse, `devicectl
    device process launch` then answers "Launched application with <id> bundle
    identifier" and exits 0 for a bundle that is not installed, so every layer
    above it believed the runner was starting. Asking the phone what it has is
    the only honest check, and it is the first thing to check when the runner
    launches but never binds its port.
    """
    if not udid:
        return Check("runner-installed", False, "no device", fix="connect the phone")
    ids = installed_bundle_ids(udid)
    if ids is None:
        return Check("runner-installed", True, "could not list apps, skipping")
    if runner_bundle_id in ids:
        return Check("runner-installed", True, f"{runner_bundle_id} is on the phone")
    return Check(
        "runner-installed", False, f"{runner_bundle_id} is NOT installed",
        fix="iOS offloaded it. Run `phoneshell setup` to reinstall, then turn OFF "
            "Settings > App Store > Offload Unused Apps so it cannot happen mid-run.")


def launch_wda(udid: str, runner_bundle_id: str, port: int = 8100, mjpeg_port: int = 9100,
               bind_ip: str | None = None) -> Check:
    """Detached launch. devicectl exits immediately and WDA keeps running."""
    # devicectl reports success launching a bundle id that is not installed, so
    # the only way to tell "started" from "does not exist" is to ask first.
    present = installed_bundle_ids(udid)
    if present is not None and runner_bundle_id not in present:
        return Check(
            "wda-launch", False, f"{runner_bundle_id} is not installed on the phone",
            fix="iOS offloaded the runner. Run `phoneshell setup` to reinstall it.")
    env = {
        "USE_PORT": str(port),
        "MJPEG_SERVER_PORT": str(mjpeg_port),
        "WDA_PRODUCT_BUNDLE_IDENTIFIER": runner_bundle_id,
    }
    if bind_ip:
        env["USE_IP"] = bind_ip
    res = run([
        "xcrun", "devicectl", "device", "process", "launch",
        "--device", udid, "--terminate-existing",
        "--environment-variables", json.dumps(env),
        runner_bundle_id,
    ], timeout=180)
    if res.returncode != 0:
        text = (res.stderr or res.stdout).strip()
        fix = "unlock the phone (the first XCTest session needs the passcode screen dismissed)"
        if "10300" in text or "background test runner" in text:
            fix = ("this is the iOS 27 failure mode for the devicectl launch path. "
                   "Fall back to `phoneshell up --attached`, which holds an xcodebuild session open.")
        return Check("wda-launch", False, text[:400], fix=fix)
    return Check("wda-launch", True, f"launched {runner_bundle_id} detached")


def launch_wda_dvt(udid: str, runner_bundle_id: str, log_path: Path,
                   port: int = 8100, mjpeg_port: int = 9100) -> subprocess.Popen:
    """Launch the runner through the DVT/testmanagerd XCTest session.

    Tethered like the xcodebuild path (WDA dies when this process does), but it
    needs no Xcode and it rides the RemoteXPC route that keeps working on the
    iOS versions where the detached devicectl launch is reported broken. This is
    the fallback to reach for on iOS 27.
    """
    pmd = shutil.which("pymobiledevice3") or str(Path.home() / ".local/bin/pymobiledevice3")
    cmd = [
        pmd, "developer", "dvt", "xcuitest", runner_bundle_id,
        "--env", f"USE_PORT={port}",
        "--env", f"MJPEG_SERVER_PORT={mjpeg_port}",
    ]
    if udid:
        cmd += ["--udid", udid]
    log = log_path.open("w")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=_env())


def launch_wda_attached(udid: str, log_path: Path) -> subprocess.Popen:
    """Fallback launcher: keeps xcodebuild attached for the life of the session.

    Slower to start and dies with this process, but it does not depend on the
    devicectl XCTest path, which is the one reported broken on iOS 27.
    """
    xctestrun = None
    for candidate in (DEVICE_DD / "Build" / "Products").glob("WebDriverAgentRunner_iphoneos*.xctestrun"):
        xctestrun = candidate
        break
    log = log_path.open("w")
    if xctestrun:
        cmd = ["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
               "-destination", f"id={udid}"]
    else:
        cmd = ["xcodebuild", "-project", str(WDA_SRC / "WebDriverAgent.xcodeproj"),
               "-scheme", "WebDriverAgentRunner", "-destination", f"id={udid}",
               "-derivedDataPath", str(DEVICE_DD), "test-without-building"]
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=_env())


def recycle_runner(udid: str, runner_bundle_id: str, port: int = 8100,
                   mjpeg_port: int = 9100, wait: float = 40.0) -> Check:
    """Restart WebDriverAgent on the phone and wait for it to answer.

    Two device-side states stop an unattended run and neither heals on its own:
    XCTest returns "Not authorized" (error 41) once the automation session has
    lost its authorisation, and SpringBoard stops confirming its run loop, which
    makes even session-free reads throw. Relaunching the runner clears both.
    """
    import socket
    import time as _time

    launched = launch_wda(udid, runner_bundle_id, port, mjpeg_port)
    if not launched.ok:
        return launched
    deadline = _time.time() + wait
    while _time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.5):
                return Check("recycle", True, "WebDriverAgent restarted and answering")
        except OSError:
            _time.sleep(1.5)
    return Check("recycle", False, f"WebDriverAgent did not come back within {wait:.0f}s")


# ------------------------------------------------------------------ port forward


class PortForward:
    """usbmux tunnel from a local port to the phone.

    Preferred over the phone's LAN address: WDA authenticates nothing, so an
    open 8100 on the wifi lets anyone in range drive the phone.
    """

    def __init__(self, udid: str, pairs: tuple[tuple[int, int], ...] = ((8100, 8100), (9100, 9100))):
        self.udid = udid
        self.pairs = pairs
        self.proc: subprocess.Popen | None = None

    def start(self) -> Check:
        iproxy = shutil.which("iproxy") or "/opt/homebrew/bin/iproxy"
        if not Path(iproxy).exists():
            return Check("forward", False, "iproxy is missing", fix="brew install libimobiledevice")
        args = [iproxy] + [f"{local}:{remote}" for local, remote in self.pairs] + ["-u", self.udid]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        if self.proc.poll() is not None:
            return Check("forward", False, "iproxy exited immediately",
                         fix="check the cable and that the device is trusted")
        return Check("forward", True, " ".join(f"{l}->{r}" for l, r in self.pairs))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def activation_check(cfg) -> Check:
    """Can the phone actually bring an app to the foreground?

    Everything else can look healthy while this is broken: the tree reads, the
    screenshots come back, taps land, and every launch API returns success. The
    only way to know is to launch something and look at what is in front
    afterwards. Seen on a real device after a long automation session.
    """
    from .actions import Phone
    from .wda.client import WDAError
    try:
        phone = Phone(cfg)
        before = str(phone.wda.active_app_info().get("bundleId", ""))
        probe = "com.apple.Preferences" if before != "com.apple.Preferences" else "com.apple.mobilesafari"
        result = phone.open_app(probe)
        if result.ok:
            return Check("app-activation", True, f"apps come to the foreground (opened {probe})")
        return Check(
            "app-activation", False,
            "the phone accepts taps and reads fine, but will not bring any app to the foreground",
            fix=("restart the bridge with `phoneshell up --relaunch`. If that does not clear it, "
                 "reboot the phone: the automation session can wedge FrontBoard, and every launch "
                 "API keeps reporting success while nothing moves."),
        )
    except WDAError as exc:
        return Check("app-activation", False, f"could not test: {exc}", fix="run `phoneshell up`")


def input_check(cfg) -> Check:
    """Do gestures actually reach the screen?

    This is the failure that looks like nothing at all. Reads keep working, so
    the tree, the screenshots and even the alert text all come back correct,
    while every write returns success and the screen never moves. Measured on a
    real phone: `dismiss_alert` reported OK four times running against an alert
    that stayed put, a tap on its Cancel button changed 0.0% of the pixels, and
    the device log showed the HID event system dropping 110,000 events. No
    amount of reasoning gets an agent out of that, and it burned 23 turns and
    $0.41 on a single task before the run gave up. One swipe answers it, so pay
    for the swipe before paying for the run.
    """
    from .actions import Phone
    from .perception.screen import visual_difference
    from .wda.client import WDAError
    try:
        phone = Phone(cfg)
        geo = phone.wda.geometry()
        y = geo.point_h * 0.5
        right, left = geo.point_w * 0.85, geo.point_w * 0.15
        before = phone.wda.screenshot()
        phone.wda.drag(right, y, left, y, duration=0.15)
        time.sleep(1.0)
        moved = visual_difference(before, phone.wda.screenshot())
        # Put the screen back where it was, whatever the verdict.
        phone.wda.drag(left, y, right, y, duration=0.15)
        time.sleep(0.6)
        if moved > 0.01:
            return Check("input", True, f"gestures reach the screen ({moved:.1%} moved)")
        return Check(
            "input", False,
            "gestures are being dropped: a full-width swipe moved 0% of the screen",
            fix=("the phone's HID event system is wedged. Reads and every write API keep "
                 "reporting success while nothing moves. Reboot the phone: relaunching the "
                 "runner does not clear it."))
    except WDAError as exc:
        return Check("input", False, f"could not test: {exc}", fix="run `phoneshell up`")


# ------------------------------------------------------------------- health ladder


def health(cfg: Config, udid: str | None) -> list[Check]:
    """Three-stage diagnosis, because each stage fails for a different reason and
    needs a different remedy: is the device there, is the port forwarded, is WDA
    answering."""
    checks = [device_check()]
    udid = udid or checks[0].data.get("udid")
    port = cfg.wda.port
    sock_ok = False
    try:
        import socket
        with socket.create_connection((cfg.wda.host, port), timeout=1.5):
            sock_ok = True
    except OSError:
        pass
    checks.append(Check(
        "forward", sock_ok,
        f"{cfg.wda.host}:{port} is {'open' if sock_ok else 'closed'}",
        fix="run `phoneshell up` to start the usbmux forward",
    ))
    if sock_ok:
        from .wda.client import WDAClient, WDAError
        client = WDAClient(base_url=cfg.wda_base_url, timeout=8)
        try:
            status = client.status()
            ios = status.get("os", {})
            checks.append(Check("wda", True,
                                f"ready, WDA {status.get('build', {}).get('version')} on iOS {ios.get('version')}"))
            # Everything above can pass while gestures go nowhere, so ask the
            # screen to move before calling the phone healthy.
            checks.append(input_check(cfg))
        except WDAError as exc:
            checks.append(Check("wda", False, str(exc),
                                fix="run `phoneshell up --relaunch` to restart the runner on the phone"))
    else:
        checks.append(Check("wda", False, "not reachable", fix="run `phoneshell up`"))
        checks.append(runner_installed_check(udid, cfg.wda.runner_bundle_id))
    return checks
