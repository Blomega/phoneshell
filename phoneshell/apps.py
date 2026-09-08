"""What is installed on the phone, and the deep links that skip the UI entirely.

Knowing bundle ids matters more than it sounds: `activate_app("com.grabtaxi.
passenger")` is one deterministic call, while "find the Grab icon on some home
screen page and tap it" is a multi-step vision problem that fails when the user
rearranges their apps.
"""
from __future__ import annotations

import json
import logging
import plistlib
import subprocess
from functools import lru_cache

from .config import Config

log = logging.getLogger("phoneshell.apps")

# Deep links worth having by hand. Each entry is (description, url template).
# Anything here is a shortcut past several UI steps, and every one of them was
# checked against the app's registered URL types rather than guessed.
DEEP_LINKS: dict[str, dict[str, str]] = {
    "net.whatsapp.WhatsApp": {
        "open": "whatsapp://",
        "chat_with_number": "whatsapp://send?phone={phone}",
        "chat_with_text": "whatsapp://send?phone={phone}&text={text}",
    },
    # Extracted from Grab's own site rather than guessed. screenType is the only
    # navigation lever the scheme exposes: it reaches a tab and nothing deeper,
    # so cart, address, payment and confirmation stay UI automation.
    "com.grabtaxi.iphone": {
        "open": "grab://open",
        "food": "grab://open?screenType=GRABFOOD",
        "mart": "grab://open?screenType=GRABMART",
        "express": "grab://open?screenType=EXPRESS",
        "transport": "grab://open?screenType=BOOKING",
        "dinein": "grab://open?screenType=DINEIN",
    },
    "com.apple.mobilesafari": {"open": "https://{host}", "search": "https://www.google.com/search?q={q}"},
    "com.apple.MobileSMS": {"chat": "sms:{phone}", "chat_with_text": "sms:{phone}&body={text}"},
    "com.apple.mobilephone": {"call": "tel:{phone}"},
    "com.apple.Maps": {"directions": "maps://?daddr={dest}&dirflg=d"},
    "com.apple.shortcuts": {"run": "shortcuts://run-shortcut?name={name}&input=text&text={text}"},
}

# Names people use out loud, mapped to bundle ids, for phones where the catalog
# lookup comes back with a localised display name.
# Verified against the real device catalogue on 2026-09-07, not guessed. The
# Android package names differ and are a common wrong answer: Grab on iOS is
# com.grabtaxi.iphone, and WhatsApp is net.whatsapp.WhatsApp.
COMMON_ALIASES = {
    "whatsapp": "net.whatsapp.WhatsApp",
    "grab": "com.grabtaxi.iphone",
    "safari": "com.apple.mobilesafari",
    "messages": "com.apple.MobileSMS",
    "phone": "com.apple.mobilephone",
    "maps": "com.apple.Maps",
    "settings": "com.apple.Preferences",
    # iOS ships several bundles whose display name is the same word, and some of
    # them are system services rather than the app a person means. Asking for
    # "Contacts" resolved to com.apple.PeopleViewService (a service) instead of
    # the Contacts app, and the task failed before the model did anything wrong.
    # These are the canonical answers for the apps a benchmark touches.
    "contacts": "com.apple.MobileAddressBook",
    "clock": "com.apple.mobiletimer",
    "reminders": "com.apple.reminders",
    "weather": "com.apple.weather",
    "calculator": "com.apple.calculator",
    "files": "com.apple.DocumentsApp",
    "health": "com.apple.Health",
    "wallet": "com.apple.Passbook",
    "find my": "com.apple.findmy",
    "home": "com.apple.Home",
    "music": "com.apple.Music",
    "podcasts": "com.apple.podcasts",
    "tv": "com.apple.tv",
    "books": "com.apple.iBooks",
    "translate": "com.apple.Translate",
    "voice memos": "com.apple.VoiceMemos",
    "stocks": "com.apple.stocks",
    "tips": "com.apple.tips",
    "measure": "com.apple.measure",
    "compass": "com.apple.compass",
    "shortcuts": "com.apple.shortcuts",
    "mail": "com.apple.mobilemail",
    "notes": "com.apple.mobilenotes",
    "calendar": "com.apple.mobilecal",
    "photos": "com.apple.mobileslideshow",
    "camera": "com.apple.camera",
    "app store": "com.apple.AppStore",
    "shortcuts": "com.apple.shortcuts",
    "telegram": "ph.telegra.Telegraph",
    "instagram": "com.burbn.instagram",
    "gmail": "com.google.Gmail",
    "chrome": "com.google.chrome.ios",
    "uber": "com.ubercab.UberClient",
    "spotify": "com.spotify.client",
    "youtube": "com.google.ios.youtube",
}


def _simulator_apps(udid: str) -> dict[str, str]:
    out = subprocess.run(
        ["xcrun", "simctl", "listapps", udid],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/sbin", "DEVELOPER_DIR": "/Applications/Xcode.app/Contents/Developer"},
    )
    if out.returncode != 0:
        return {}
    # simctl emits an old-style plist dictionary
    try:
        data = plistlib.loads(out.stdout.encode(), fmt=plistlib.FMT_XML)
    except Exception:
        data = _parse_simctl_plist(out.stdout)
    catalog = {}
    for bundle, info in data.items():
        name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle
        catalog[str(name)] = str(bundle)
    return catalog


def _parse_simctl_plist(text: str) -> dict:
    """simctl prints NeXTSTEP-style plists; convert with plutil rather than a regex."""
    proc = subprocess.run(
        ["plutil", "-convert", "json", "-o", "-", "-"],
        input=text.encode(), capture_output=True, timeout=30,
    )
    if proc.returncode == 0:
        return json.loads(proc.stdout)
    return {}


async def _device_apps_async(udid: str | None) -> dict[str, str]:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    lockdown = await create_using_usbmux(serial=udid)
    proxy = InstallationProxyService(lockdown=lockdown)
    apps = await proxy.get_apps(application_type="Any")
    # 11.x returns {bundle_id: info}; older builds returned a list of info dicts.
    entries = apps.items() if isinstance(apps, dict) else ((None, a) for a in apps)
    catalog: dict[str, str] = {}
    for key, info in entries:
        if not isinstance(info, dict):
            continue
        bundle = info.get("CFBundleIdentifier") or key
        name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle
        if bundle:
            catalog[str(name)] = str(bundle)
    return catalog


def _device_apps(udid: str | None) -> dict[str, str]:
    """Installed apps on a real device, via pymobiledevice3's installation_proxy.

    pymobiledevice3 11.x is async throughout, and this is called from ordinary
    sync code, so the loop is owned here.
    """
    try:
        import asyncio
    except ImportError:  # pragma: no cover
        return {}
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(_device_apps_async(udid))
        # Already inside a loop (MCP server): run in a worker thread.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _device_apps_async(udid)).result(timeout=60)
    except Exception as exc:
        log.warning("could not list installed apps over usbmux: %s", exc)
        return {}


@lru_cache(maxsize=4)
def installed_apps(cfg_key: str = "") -> dict[str, str]:  # pragma: no cover - thin wrapper
    raise RuntimeError("call installed_apps(cfg) instead")


def installed_apps(cfg: Config) -> dict[str, str]:  # noqa: F811
    """Display name -> bundle id for everything on the phone."""
    catalog: dict[str, str] = {}
    udid = cfg.device.udid
    if udid and "-" in udid and len(udid) == 36:
        catalog = _simulator_apps(udid)
    if not catalog:
        catalog = _device_apps(udid)
    for alias, bundle in COMMON_ALIASES.items():
        catalog.setdefault(alias, bundle)
    return catalog


def deep_link(bundle_id: str, action: str, **params: str) -> str | None:
    entry = DEEP_LINKS.get(bundle_id, {}).get(action)
    if not entry:
        return None
    try:
        return entry.format(**params)
    except KeyError as exc:
        raise ValueError(f"deep link {bundle_id}:{action} needs {exc}") from exc
