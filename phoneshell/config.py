"""Central configuration for phoneshell.

Everything that a user might reasonably want to change lives here or in
runtime/config.yaml. Nothing else in the codebase reads os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
LOGS = RUNTIME / "logs"
SCREENS = RUNTIME / "screens"
MACROS = RUNTIME / "macros"
CONFIG_PATH = RUNTIME / "config.yaml"
VENDOR = ROOT / "vendor"
WDA_SRC = VENDOR / "WebDriverAgent"

for _d in (RUNTIME, LOGS, SCREENS, MACROS):
    _d.mkdir(parents=True, exist_ok=True)


@dataclass
class WdaConfig:
    # Where the phone's WDA server is reachable from this Mac.
    # "usb"  -> we run a local port forward over usbmux and talk to 127.0.0.1
    # "wifi" -> we talk straight to the phone's LAN address
    transport: str = "usb"
    host: str = "127.0.0.1"
    port: int = 8100
    mjpeg_port: int = 9100
    # LAN address of the phone, filled in by `phoneshell doctor` when transport=wifi
    wifi_host: str | None = None
    # XCTest hit-tests every node to compute `visible` and `accessible`, and on a
    # real iPhone 17 Pro Max that is the whole cost of a tree read: measured on
    # the App Library, 5.82s with them and 0.89s without, for a difference of one
    # element out of 24. Geometric clipping against the window rect already
    # removes offscreen content, and the consistency gate catches an element that
    # is covered rather than absent. Turn this on to get strict visibility back
    # at roughly 6x the latency per step.
    precise_visibility: bool = False
    # Tree depth dominates the cost on heavy apps. Measured on Grab's food list:
    # depth 20 -> 0.21s and 59 elements, depth 25 -> 1.27s and 78, depth 30+ adds
    # nothing over 25. So read shallow and only pay for depth when the shallow
    # read comes back looking under-described.
    fast_depth: int = 20
    deep_depth: int = 32
    min_interactive_before_deepening: int = 8
    # The runner's bundle id, which is always <app_bundle_id>.xctrunner. These two
    # must agree with what `phoneshell setup` actually builds, or every launch and
    # every supervisor relaunch fails with an opaque devicectl error.
    # Signing is per-user and MUST NOT be baked into the source. Shipping one
    # company's team id means strangers build a runner signed by that company and
    # install it on their own phones, which is an Apple Developer Program
    # agreement problem and puts the account that ships real apps at risk.
    # None means: detect a codesigning identity from this Mac's keychain.
    development_team: str | None = None
    app_bundle_id: str = "com.phoneshell.WebDriverAgentRunner"
    runner_bundle_id: str = "com.phoneshell.WebDriverAgentRunner.xctrunner"
    # Bind WDA to the phone's loopback so port 8100 is not open to the whole wifi.
    # Honour USE_IP is unverified on physical hardware, so `doctor` probes the LAN
    # address and says so plainly rather than assuming this worked.
    bind_loopback: bool = True
    request_timeout: float = 60.0
    # Session tuning, applied on every new session. See vendor/WebDriverAgent
    # WebDriverAgentLib/Utilities/FBSettings.m for the authoritative list.
    settings: dict[str, Any] = field(
        default_factory=lambda: {
            # Speed: waitForIdleTimeout defaults to 10s, and on any screen with a
            # spinner or looping banner every single command pays it in full.
            "waitForIdleTimeout": 0,
            "animationCoolOffTimeout": 0.2,
            "accessibilityDeadline": 10.0,
            # One round trip per find instead of find-then-N-attribute-GETs.
            "shouldUseCompactResponses": False,
            "elementResponseAttributes": "type,label,rect,enabled,displayed,name,value",
            "useFirstMatch": True,
            # Perception: we need hittability and native frames in the tree.
            "includeHittableInPageSource": True,
            "includeNativeFrameInPageSource": True,
            "includeMinMaxValueInPageSource": True,
            "snapshotMaxDepth": 20,
            # Typing: autocorrect and predictive text corrupt agent input.
            "keyboardAutocorrection": False,
            "keyboardPrediction": False,
            "maxTypingFrequency": 30,
            "useClearTextShortcut": True,
            # Screenshots. The default is 3, which is HEIC, and a PNG decoder
            # fed HEIC gets garbage. 0 is lossless PNG.
            "screenshotQuality": 0,
            "mjpegServerFramerate": 12,
            "mjpegScalingFactor": 50,
            "mjpegServerScreenshotQuality": 40,
            # Alerts are surfaced to the agent, never auto-accepted: an alert can
            # be "Confirm payment of 450 THB".
            "defaultAlertAction": "",
            "respectSystemAlerts": True,
            # Follow the real foreground app rather than pinning the session to
            # whatever happened to be in front when it was created.
            "defaultActiveApplication": "auto",
        }
    )


@dataclass
class DeviceConfig:
    udid: str | None = None
    # Optional and off by default. iOS deliberately blocks automation from the
    # passcode screen, but the keypad is a normal accessibility element, so the
    # digits can be tapped if you choose to store the code here. It lives in
    # runtime/config.yaml, which is gitignored and never leaves this Mac.
    # The alternative, and the better one for a docked rig, is Auto-Lock: Never.
    passcode: str | None = None
    name: str | None = None
    ios_version: str | None = None
    # Screen geometry, discovered at runtime
    point_size: tuple[int, int] | None = None
    scale: float | None = None


@dataclass
class SafetyConfig:
    """Rails. The agent operates a phone with a payment method on it."""
    # Actions matching these need explicit human approval before they fire.
    confirm_patterns: list[str] = field(
        default_factory=lambda: [
            r"(?i)\bplace\s*order\b", r"(?i)\bpay\b", r"(?i)\bconfirm\s*(and\s*)?pay\b",
            r"(?i)\bcheckout\b", r"(?i)\bbuy\b", r"(?i)\bpurchase\b", r"(?i)\bsend\s*money\b",
            r"(?i)\btransfer\b", r"(?i)\bbook\s*(now|ride)\b", r"(?i)\bsubscribe\b",
            r"(?i)\bdelete\b", r"(?i)\bremove\b", r"(?i)\blog\s*out\b", r"(?i)\bsign\s*out\b",
        ]
    )
    # Apps the agent may never open.
    denied_bundle_ids: list[str] = field(
        default_factory=lambda: [
            "com.apple.Preferences.Passwords",
            "com.apple.Passwords",
            "com.apple.Keychain",
        ]
    )
    # Hard cap on agent steps per task, and on money.
    max_steps: int = 60
    max_spend_local: float = 0.0  # 0 = every payment needs a human tap
    require_confirm_on_payment_screen: bool = True
    audit_log: str = "audit.jsonl"


@dataclass
class MemoryConfig:
    """Remembering screens the agent has already worked out.

    The point is not a faster tree read (that is 159ms); it is not having to ask
    the model what it is looking at, which is 1-3 seconds every step.
    """
    enabled: bool = True
    # On a screen seen before, send the tree without the picture. Saves the
    # image round trip and roughly 700 tokens a step.
    skip_image_on_known: bool = True
    # Known apps settle predictably, so poll for stability more tightly.
    fast_settle_on_known: bool = True
    min_visits_to_trust: int = 2


@dataclass
class BrainConfig:
    """Which LLM drives the loop."""
    provider: str = "claude-cli"  # "claude-cli" (uses the local subscription) | "anthropic-api"
    model: str = "claude-opus-5"
    fast_model: str = "claude-sonnet-5"
    max_tokens: int = 8000
    # Perception budget per step
    send_screenshot_every_step: bool = True
    screenshot_max_edge: int = 512
    tree_max_elements: int = 60


@dataclass
class SessionConfig:
    """How the agent shares the phone with its owner.

    takeover: the agent drives, and whatever you do at the same time fights it.
    shared:   the agent yields the moment you touch the phone and resumes when
              you put it down. iOS has one display and one digitizer, so this is
              as close to "runs in the background" as the hardware allows.
    """
    mode: str = "takeover"
    yield_grace_seconds: float = 6.0
    yield_poll_seconds: float = 0.8
    yield_wait_timeout: float = 120.0


@dataclass
class Config:
    session: SessionConfig = field(default_factory=SessionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    wda: WdaConfig = field(default_factory=WdaConfig)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    server_host: str = "127.0.0.1"
    server_port: int = 8765

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        cfg = cls()
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            cfg = _merge(cfg, raw)
        # Env overrides, useful for one-off runs.
        if os.environ.get("PHONESHELL_WDA_URL"):
            url = os.environ["PHONESHELL_WDA_URL"].rstrip("/")
            host, _, port = url.split("//", 1)[1].partition(":")
            cfg.wda.host, cfg.wda.port = host, int(port or 8100)
        return cfg

    # Tuned WebDriverAgent settings live in code, never in the saved file: an
    # older runtime/config.yaml was overriding improved defaults (it still held
    # snapshotMaxDepth 62 long after the code moved to 30) with no way to notice.
    _NEVER_PERSIST = {"wda": {"settings"}}

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_PATH
        data = asdict(self)
        for section, keys in self._NEVER_PERSIST.items():
            for key in keys:
                data.get(section, {}).pop(key, None)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    @property
    def wda_base_url(self) -> str:
        host = self.wda.wifi_host if self.wda.transport == "wifi" and self.wda.wifi_host else self.wda.host
        return f"http://{host}:{self.wda.port}"

    @property
    def mjpeg_url(self) -> str:
        host = self.wda.wifi_host if self.wda.transport == "wifi" and self.wda.wifi_host else self.wda.host
        return f"http://{host}:{self.wda.mjpeg_port}"


def _merge(cfg: Config, raw: dict) -> Config:
    """Shallow-merge a yaml dict onto the dataclass tree."""
    for section, value in raw.items():
        if not hasattr(cfg, section):
            continue
        current = getattr(cfg, section)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            for k, v in value.items():
                if hasattr(current, k):
                    if k == "point_size" and isinstance(v, list):
                        v = tuple(v)
                    setattr(current, k, v)
        else:
            setattr(cfg, section, value)
    return cfg
