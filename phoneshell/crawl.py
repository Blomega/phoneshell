"""Walk an iPhone's screens and collect a screenshot of every one.

No model in the loop. This is a deterministic crawler: code decides where to go
next, so it costs nothing per step but time and it can run for hours. The only
thing it ever does to the phone is tap controls that look safe and read what came
back. It never types, never toggles a switch, never taps anything that spends
money, sends a message or deletes a thing.

WHY A CRAWLER AND NOT AN AGENT. "Screenshot every flow" is a graph traversal, and
a traversal is the one job an LLM is worst at: it has no memory of which of the
eleven rows it already opened, and it pays a second of latency and a fraction of a
cent to rediscover that each time. Done in code it is 1.2 seconds a step, forever.

THE THREE THINGS THAT MAKE IT HARD, and what this does about each:

  1. "Have I seen this screen?" An app has cycles, and a list of 50 contacts has
     50 detail screens that are the same screen. So identity is TWO keys, not one:
     `memory.fingerprint` hashes the LAYOUT and ignores the contents, while
     `tree.screen_signature` hashes the contents. A new signature is worth a
     screenshot (collecting is cheap). Only the first few signatures per layout
     are worth DESCENDING into (exploring is not). That one rule is the difference
     between a crawl that finishes and a crawl that reads out someone's contacts.

  2. "Am I where I think I am?" iOS has no back button, and `back()` falls through
     to an edge swipe that sometimes does nothing. So every return is VERIFIED
     against the screen's own keys, and when verification fails the crawler stops
     guessing: home, relaunch, replay the path from the root. Cheap when it works,
     correct when it does not.

  3. "Did that tap do anything at all?" Every layer in this stack returns success
     for work it never did, and a wedged HID layer reports success while the screen
     never moves (FINDINGS s16, s30: 110,000 dropped events, a full-width swipe
     moving 0% of the screen). A crawler is the worst possible victim of that: it
     would happily collect two hundred screenshots of one screen. So a run of taps
     that change nothing triggers a swipe test, and a failed swipe test ends the
     run with the reason.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable

from .actions import Phone, Snapshot
from .apps import installed_apps
from .config import Config, RUNTIME
from .memory import control_key, fingerprint
from .perception.screen import downscale, load_image, visual_difference
from .perception.tree import INTERACTIVE, Element, screen_signature
from .safety import Guard
from .wda.client import WDAError, WDAUnreachable

log = logging.getLogger("phoneshell.crawl")

CRAWLS = RUNTIME / "crawls"

# Never tapped, whatever the settings say. Every one of these either spends money,
# destroys data, ends a session, grants a permission or places a call, and a
# crawler has no way to undo any of it. The list is matched against the control's
# whole label as a word, not as a substring: "Delete" is refused, "Deleted Items"
# is a screen worth having.
NEVER_TAP = (
    "delete", "delete all", "remove", "erase", "reset", "clear", "clear all", "clear history",
    "sign out", "log out", "logout", "unpair", "forget", "forget this device", "forget network",
    "buy", "purchase", "subscribe", "upgrade", "renew", "pay", "checkout", "order",
    "place order", "confirm", "confirm and pay", "send", "post", "publish", "tweet", "share",
    "call", "facetime", "dial", "answer", "hang up", "end call", "video call",
    "block", "report", "unfriend", "unfollow", "leave", "leave group", "unsubscribe",
    "restore", "restore purchases", "transfer", "withdraw", "deposit", "send money",
    "allow", "allow once", "don't allow", "dont allow", "agree", "i agree", "accept", "trust",
    "turn off", "turn on", "enable", "disable", "install", "update", "download", "uninstall",
    "factory reset", "erase all content and settings", "wipe", "shut down", "restart", "power off",
    # A microphone or camera button starts capturing and can background the app.
    # Measured on the simulator: the Settings search bar's "Dictate" sent the crawl
    # to the home screen on its very first tap.
    "dictate", "microphone", "voice search", "record", "start recording", "take photo",
)

# Refused by default, but only because they CREATE something rather than navigate
# to it. Nothing here is destructive, and the sheet each one opens is often a flow
# worth collecting, so `Plan.cautious = False` lets a crawl tap them.
CAREFUL_TAP = (
    "add", "new", "create", "compose", "schedule", "start", "begin", "continue",
    "next", "get started", "set up", "sign in", "log in", "connect", "pair",
)

# Labels that mean "leave this screen". They are how the crawler gets OUT, never
# a place to go, so they are removed from the forward control set.
BACK_CONTROLS = {"back", "cancel", "close", "done", "dismiss", "go back", "return",
                 "\u2039", "<", "\u00d7", "x", "\u2715", "\u2716"}

# Element types whose tap CHANGES THE PHONE rather than navigating it. A Switch in
# Settings is the clearest case: tapping it is not exploration, it is a
# configuration change on the owner's personal phone.
MUTATING_TYPES = {"Switch", "Slider", "Stepper", "PickerWheel", "Picker", "DatePicker", "CheckBox"}

# Typing is out of scope entirely, so a keyboard is only ever something to escape.
INPUT_TYPES = {"TextField", "SecureTextField", "SearchField", "TextView", "Key"}

# Apps a crawl does not enter, even when asked for "everything". The first three
# hold credentials; the rest would have the crawler read out, and screenshot, the
# owner's private correspondence and payment instruments.
DENY_BUNDLES = {
    "com.apple.Preferences.Passwords", "com.apple.Passwords", "com.apple.Keychain",
    "com.apple.Passbook", "com.apple.wallet", "com.apple.mobilephone",
}

# Springboard and friends are where a crawl ends up when a tap leaves the app.
SYSTEM_BUNDLES = {"com.apple.springboard", "com.apple.SpringBoard", "com.apple.Spotlight"}

# The Apple apps a person would actually recognise on their home screen. Asking a
# real iPhone what is installed returns 428 bundles, and ~300 of them are invisible
# UI services (AXUIViewService, AccountAuthenticationDialog, BusinessActionSheet)
# that exist to draw one sheet inside another app. They cannot be launched to a
# screen and they are not apps, so a chooser that lists them is unusable.
APPLE_USER_APPS = {
    "com.apple.Preferences", "com.apple.mobilesafari", "com.apple.MobileSMS",
    "com.apple.mobilemail", "com.apple.mobilephone", "com.apple.facetime",
    "com.apple.mobileslideshow", "com.apple.camera", "com.apple.Maps",
    "com.apple.Music", "com.apple.podcasts", "com.apple.tv", "com.apple.iBooks",
    "com.apple.mobilenotes", "com.apple.reminders", "com.apple.mobilecal",
    "com.apple.mobiletimer", "com.apple.calculator", "com.apple.weather",
    "com.apple.Health", "com.apple.Fitness", "com.apple.stocks", "com.apple.compass",
    "com.apple.measure", "com.apple.Bridge", "com.apple.shortcuts", "com.apple.Home",
    "com.apple.DocumentsApp", "com.apple.MobileAddressBook", "com.apple.AppStore",
    "com.apple.news", "com.apple.translate", "com.apple.VoiceMemos",
    "com.apple.findmy", "com.apple.freeform", "com.apple.Journal",
    "com.apple.tips", "com.apple.Magnifier", "com.apple.Passbook", "com.apple.Passwords",
    "com.apple.store.Jolly", "com.apple.Batteries", "com.apple.AdaptiveMusicApp",
    "com.apple.accessibility.AccessibilityReader",
}

# Bundle-name shapes that are never a user-facing app.
_NOT_AN_APP = ("viewservice", "uiservice", "uihost", "actionsheet", "dialog",
               "launchangel", "indicator", "sourceselection", "authorizationappsheet",
               "setupuiservice", "-extension", ".extension", "settingsbundle")


def is_user_app(name: str, bundle: str) -> bool:
    """Would this show up on the home screen? Apple ships hundreds that do not."""
    low = bundle.lower()
    if any(mark in low for mark in _NOT_AN_APP):
        return False
    if bundle.startswith("com.phoneshell") or "WebDriverAgent" in bundle:
        return False
    if bundle.startswith("com.apple."):
        return bundle in APPLE_USER_APPS
    return bool(name.strip())


class Budget(Exception):
    """Raised to unwind the traversal when a limit is reached."""


class Lost(Exception):
    """Raised when the crawler cannot get back to where it was."""


class Wedged(Exception):
    """Raised when the phone stops responding to gestures while reporting success."""


@dataclass
class Plan:
    """What to crawl and how far. Every field is a stop condition or a scope."""
    scope: str = "app"                  # "app" | "phone"
    app: str = ""                       # name or bundle id, for scope="app"
    apps: list[str] = field(default_factory=list)   # for scope="phone"
    max_screens: int = 150
    max_depth: int = 4
    max_minutes: float = 30.0
    max_taps: int = 900
    # Screens per LAYOUT that are worth exploring. 3 is deliberate: it is enough
    # to see that a list's detail pages are all the same shape, and small enough
    # that a 200-row list does not become 200 subtrees.
    variant_cap: int = 3
    # Viewport passes per screen. Each one is a scroll plus a re-read.
    scroll_cap: int = 4
    # Per-app ceilings, used when scope="phone" so one deep app cannot eat the run.
    per_app_screens: int = 14
    per_app_minutes: float = 4.0
    skip_mutating: bool = True
    # Refuse controls that CREATE something (Add, New, Compose, Sign in). Off by
    # default would collect more flows; on by default leaves nothing behind.
    cautious: bool = True
    full_res: bool = True
    label: str = ""
    # Carry on from every earlier scan of this app: keep what they collected, never
    # tap a control they already tapped, and only walk back to unfinished screens.
    resume: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Screen:
    sid: str
    structural: str                     # layout identity, contents ignored
    textual: str                        # contents identity
    bundle_id: str
    app: str
    title: str
    depth: int
    path: list[dict] = field(default_factory=list)   # controls tapped from the app root
    shots: list[str] = field(default_factory=list)   # relative paths, viewport order
    elements: int = 0
    controls: list[dict] = field(default_factory=list)
    first_seen: float = 0.0
    visits: int = 1

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Edge:
    frm: str
    to: str
    label: str
    key: str
    kind: str = "tap"                   # tap | crossing | no-op

    def as_dict(self) -> dict:
        return asdict(self)


class Crawler:
    def __init__(self, phone: Phone, plan: Plan, out: Path,
                 emit: Callable[[dict], None] | None = None) -> None:
        self.phone = phone
        self.cfg = phone.cfg
        self.plan = plan
        self.guard = Guard(self.cfg)
        self.out = out
        self.shots_dir = out / "shots"
        self.thumbs_dir = out / "thumbs"
        for d in (self.shots_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.events_path = out / "events.jsonl"
        self._emit = emit or (lambda e: None)

        self.screens: dict[str, Screen] = {}          # textual key -> Screen
        self.by_sid: dict[str, Screen] = {}
        self.variants: dict[str, int] = {}            # structural key -> how many explored
        self.tried: set[tuple[str, str]] = set()      # (structural, control_key)
        self.edges: list[Edge] = []
        self.skipped: list[dict] = []
        self.apps_done: list[dict] = []

        self.taps = 0
        self.relaunches = 0          # terminate + launch + replay the path
        self.tapbacks = 0            # one verified tap back, the cheap route
        self.crossings = 0           # taps that left the app
        self.shot_count = 0
        self.started = 0.0
        self.stop_requested = False
        self.stop_reason = ""
        self._dead_taps = 0                           # consecutive taps that changed nothing
        self._answered = threading.Event()            # the operator replying in the chat
        self._answer = ""
        self._asked_about: dict[str, bool] = {}        # one login question per app
        self._app_started = 0.0
        self._app_screens = 0
        self.seeded = 0                               # screens carried over from earlier scans
        self._backless: set[str] = set()              # layouts where back never works

    # ------------------------------------------------------------------ events

    def emit(self, kind: str, **fields) -> None:
        event = {"type": kind, "at": round(time.time(), 3),
                 "elapsed": round(time.time() - self.started, 2) if self.started else 0, **fields}
        try:
            with self.events_path.open("a") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError:
            pass
        self._emit(event)

    def say(self, text: str) -> None:
        """One plain sentence about what is happening, for the chat.

        The events are the record; this is the narration. A scan is minutes of
        silence otherwise, and silence is indistinguishable from stuck.
        """
        self.emit("say", text=text)

    def stop(self, reason: str = "stopped by the operator") -> None:
        self.stop_requested = True
        self.stop_reason = reason
        self._answered.set()          # unblock anything waiting on an answer

    # ------------------------------------------------------------ asking for help

    def ask(self, question: str, kind: str = "help", wait: float = 600.0,
            options: list[str] | None = None) -> str:
        """Stop and ask the operator, then carry on with what they say.

        A crawler meets walls it has no business climbing: a login, a one-time
        code, a paywall, a captcha. The honest move is to hand the phone back for
        a moment rather than to start typing into someone's account, so this asks,
        waits, and resumes. `wait` is generous because the answer involves a human
        picking up a phone; when it runs out the crawl carries on without them
        rather than dying, and says so.
        """
        self._answered.clear()
        self._answer = ""
        self.emit("ask", question=question, kind=kind, options=options or [])
        if not self._answered.wait(wait):
            self.emit("ask_timeout", question=question)
            self.say("Nobody answered, so I carried on with what I could reach.")
            return ""
        answer = (self._answer or "").strip()
        self.emit("answered", text=answer[:200])
        return answer

    def answer(self, text: str) -> None:
        self._answer = text
        self._answered.set()

    def _ask_for_help_if_walled(self, snap: Snapshot, app_name: str) -> None:
        """A login wall is the one place a screenshot crawl stops being useful.

        It never types a credential itself, on purpose: those are the owner's, and
        an agent that puts them into a form is a category of thing this is not. It
        asks the owner to sign in on the phone in front of them, and picks up where
        it left off.
        """
        if self._asked_about.get(snap.bundle_id):
            return
        text = " ".join(e.text.lower() for e in snap.elements)[:600]
        secure = any(e.type == "SecureTextField" for e in snap.elements)
        wall_words = ("sign in", "log in", "login", "create account", "verification code",
                      "enter your password", "one-time", "otp", "get started", "continue with")
        if not secure and not any(w in text for w in wall_words):
            return
        self._asked_about[snap.bundle_id] = True
        answer = self.ask(
            f"{app_name} is showing a sign-in wall, so there is nothing behind it for me to "
            f"collect yet. Sign in on the phone (it is right there on the cable) and type "
            f"**done** when you are in, or **skip** to scan what is reachable without an account.",
            kind="login",
        )
        if answer.lower().startswith("skip") or not answer:
            self.say("Scanning what is reachable without signing in.")
            return
        self.say("Thanks. Picking up from whatever is on screen now.")
        fresh = self._settle_twice()
        self._record(fresh, parent=None, via=None, depth=0)

    # ------------------------------------------------------------------- budget

    def _check_budget(self) -> None:
        if self.stop_requested:
            raise Budget(self.stop_reason)
        if len(self.screens) >= self.plan.max_screens:
            raise Budget(f"reached the {self.plan.max_screens}-screen limit")
        if self.taps >= self.plan.max_taps:
            raise Budget(f"reached the {self.plan.max_taps}-tap limit")
        if time.time() - self.started > self.plan.max_minutes * 60:
            raise Budget(f"reached the {self.plan.max_minutes:g}-minute limit")

    def _app_budget_spent(self) -> str:
        if self._app_screens >= self.plan.per_app_screens:
            return f"{self.plan.per_app_screens} screens from this app"
        if time.time() - self._app_started > self.plan.per_app_minutes * 60:
            return f"{self.plan.per_app_minutes:g} minutes on this app"
        return ""

    # -------------------------------------------------------------------- run

    def run(self) -> dict:
        self.started = time.time()
        self.emit("start", plan=self.plan.as_dict(), out=str(self.out))
        what = self.plan.label or self.plan.app or "every app on the phone"
        self.say(f"Starting a scan of {what}. I will tap my way through it and keep a screenshot "
                 f"of every screen, up to {self.plan.max_screens} of them or {self.plan.max_minutes:g} minutes.")
        self._take_the_phone()
        self._preflight()
        awake = self._keep_awake_if_allowed()
        try:
            if self.plan.scope == "phone":
                self._crawl_phone()
            else:
                self._crawl_app(self.plan.app)
        except Budget as exc:
            self.stop_reason = str(exc)
            self.emit("budget", reason=str(exc))
        except Wedged as exc:
            self.stop_reason = str(exc)
            self.emit("wedged", reason=str(exc))
        except (WDAUnreachable, WDAError) as exc:
            self.stop_reason = f"the bridge went away: {exc}"
            self.emit("error", text=self.stop_reason)
        finally:
            if awake is not None:
                awake.__exit__(None, None, None)
            summary = self.write_manifest()
            for line in self.report(summary):
                self.say(line)
            self.emit("done", **summary)
        return summary

    def report(self, summary: dict) -> list[str]:
        """What happened, in the words someone would use who had watched it.

        A number on its own is not a report. "40 taps" is fine; "15 of those 40
        were spent walking back to where I already was, because this app has no
        navigation bar" is the thing that tells you whether to trust the result
        and what it would take to improve it. This is the only place that
        knowledge reaches the person using AppScan, so it goes here and not into
        a log file.
        """
        carried = summary.get("carried_over", 0)
        out = [
            f"**Done.** {summary['screens']} screens"
            + (f" in all: {summary['screens'] - carried} new this time, {carried} kept from earlier "
               f"scans" if carried else "")
            + f". {summary['taps']} taps, {summary['seconds']:.0f}s. "
            f"Stopped because {summary['reason']}."
        ]
        recovery = summary.get("relaunches", 0) + summary.get("tapbacks", 0)
        if summary.get("relaunches"):
            share = summary["relaunches"] / max(summary["taps"], 1)
            out.append(
                f"Getting back cost {summary['relaunches']} restart"
                f"{'s' if summary['relaunches'] != 1 else ''} of the app"
                + (f" and {summary['tapbacks']} one-tap returns" if summary.get("tapbacks") else "")
                + ". "
                + ("That is a lot, and it is what this app's shape costs: it has no navigation bar, "
                   "so there is no back button to press and I have to reopen it and walk the path "
                   "again. The screenshots are still correct, it is just slower."
                   if share > 0.2 else
                   "That is normal: a tap occasionally goes somewhere unexpected and the safest "
                   "way back is to start the app again.")
            )
        elif summary.get("tapbacks"):
            out.append(f"Every return was a single tap ({summary['tapbacks']} of them), so nothing "
                       f"was spent reopening the app.")
        if summary.get("crossings"):
            out.append(f"{summary['crossings']} tap{'s' if summary['crossings'] != 1 else ''} led "
                       f"out of the app entirely. I kept the screenshot of where it landed and came "
                       f"back, because you asked for this app and not the whole phone.")
        if summary.get("skipped"):
            reasons: dict[str, int] = {}
            for item in self.skipped:
                raw = str(item.get("why", ""))
                if "'" in raw:
                    why = raw.split("'")[1]
                elif raw.startswith("a ") and "changes a setting" in raw:
                    why = raw.split()[1].lower() + "es"     # "a Switch ..." -> "switches"
                else:
                    why = raw[:28] or "unsafe"
                reasons[why] = reasons.get(why, 0) + 1
            top = ", ".join(f"{k} ({v})" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:4])
            out.append(f"I refused {summary['skipped']} controls as unsafe to tap: {top}. "
                       f"They are listed under Refused controls, each with its reason.")
        if summary["screens"] <= 1:
            out.append("That is barely anything, which usually means one of three things: the app "
                       "opened onto a login wall, its first screen has nothing tappable that I am "
                       "willing to touch, or it draws itself in a way iOS does not describe to me. "
                       "Open the one screenshot I did get and you will see which.")
        return out

    def _take_the_phone(self) -> None:
        """Stop shared mode for the length of the crawl.

        In shared mode a watchdog thread polls the screen every 0.8s and yields
        the phone when it sees a change it did not cause. Nothing in the action
        layer tells it that WE caused this change, so during a crawl every single
        tap looks like the owner picking the phone up. It also polls the same
        single-session WebDriverAgent we are driving, on the same client, which is
        latency on every step for an answer we never use.
        """
        if self.cfg.session.mode == "takeover":
            return
        self.cfg.session.mode = "takeover"
        try:
            self.phone.coexist.stop()
        except Exception as exc:                     # never fail a run over this
            log.debug("coexist.stop: %s", exc)
        self.emit("mode", detail="shared mode paused: the crawl drives the phone on its own")

    def _keep_awake_if_allowed(self):
        """Offer to stop the phone locking itself, and only do it if told to.

        A lock ends a scan: iOS will not launch an app from the lock screen, so
        the run stops producing screenshots and reports a failure that is really
        a phone doing its job. This project has lost four runs that way. The
        remedy is one setting on someone's personal phone, so it is asked for and
        it is put back, and a scan short enough not to care does not ask at all.
        """
        if self.plan.max_minutes < 3:
            return None
        from .autolock import KeepAwake, read as read_autolock
        current = read_autolock(self.phone)
        if current is None or current.lower() == "never":
            return None
        answer = self.ask(
            f"Your phone locks itself after **{current}**, and a locked phone stops a scan dead: "
            f"iOS will not open an app from the lock screen. Shall I set Auto-Lock to Never for "
            f"this scan and put it back to {current} when I finish?",
            kind="autolock",
            options=["Keep it awake", "Leave it alone"],
            wait=180.0,
        )
        if not answer or answer.strip().lower().startswith(("leave", "no", "skip")):
            self.say(f"Leaving Auto-Lock at {current}. If the phone locks mid-scan I will stop "
                     f"and tell you.")
            return None
        keeper = KeepAwake(self.phone, emit=self.say)
        keeper.__enter__()
        return keeper

    def _preflight(self) -> None:
        """Get the phone awake and on the home screen. Nothing else.

        There used to be a swipe test here, and it was WRONG. A full-width swipe
        only moves a home screen that has a second page to turn to; Sam's has one
        page, so the test measured 0%, declared a perfectly healthy iPhone wedged,
        and killed the Talika scan before it opened the app. A test that fails on
        a correct phone is worse than no test.

        The real evidence arrives a second later and for free: opening the app has
        to change the screen, and `_crawl_app` checks exactly that. A phone whose
        HID layer is dropping events cannot pass it.
        """
        if self.phone.wda.is_locked():
            res = self.phone.ensure_unlocked()
            self.emit("preflight", step="unlock", ok=res.ok, detail=res.detail or res.error)
            if not res.ok:
                raise Wedged("the phone is locked and it could not be unlocked from here")
        ok = self.phone.home().ok
        self.emit("preflight", step="home", ok=ok, detail="home screen" if ok else "could not reach home")
        self.say("The phone is awake and on the home screen.")

    # ------------------------------------------------------------ phone scope

    def _crawl_phone(self) -> None:
        targets = self.plan.apps or self._default_apps()
        self.emit("apps", apps=targets, count=len(targets))
        for i, app in enumerate(targets):
            self._check_budget()
            self.emit("app", app=app, index=i + 1, of=len(targets))
            try:
                self._crawl_app(app)
            except Lost as exc:
                self.emit("skip_app", app=app, reason=str(exc))
            self.apps_done.append({"app": app, "screens": self._app_screens})

    def _default_apps(self) -> list[str]:
        """Every installed app except the ones a crawl must not enter."""
        catalog = installed_apps(self.cfg)
        out = []
        for name, bundle in sorted(catalog.items()):
            if bundle in DENY_BUNDLES or bundle in set(self.cfg.safety.denied_bundle_ids):
                continue
            if bundle.startswith("com.phoneshell") or "WebDriverAgent" in bundle:
                continue
            out.append(bundle)
        return out

    # -------------------------------------------------------------- app scope

    def _crawl_app(self, target: str) -> None:
        self._app_started = time.time()
        self._app_screens = 0
        # Cold start, so screen one is the app's FIRST screen. iOS resumes an app
        # wherever you left it, and a crawl that starts three levels in records
        # that as the root and can never replay a path back to it.
        bundle = target if "." in target and " " not in target else (self.phone.resolve_app(target) or target)
        try:
            self.phone.wda.terminate_app(bundle)
            time.sleep(0.6)
        except WDAError as exc:
            log.debug("terminate %s before crawling: %s", bundle, exc)
        name = self.phone.name_for_bundle(bundle) or target
        todo = self._seed(bundle, name) if self.plan.resume and self.plan.scope == "app" else []
        self.say(f"Opening {name}.")
        before = self.phone.wda.screenshot()
        res = self.phone.open_app(target)
        self.emit("open", app=target, ok=res.ok, detail=res.detail or res.error)
        if not res.ok:
            self.skipped.append({"kind": "app", "what": target, "why": res.error or "would not open"})
            self.say(f"{name} would not come to the front. {res.error or ''}".strip())
            return
        self._app_bundle = self._current_bundle()
        self._app_target = target
        snap = self._settle_twice()

        # This is the liveness test, and it costs nothing because the launch had
        # to happen anyway. If the screen is identical after opening an app, the
        # phone is not drawing what it is told to draw, and every screenshot from
        # here would be the same picture of the home screen.
        moved = visual_difference(before, snap.png) if snap.png else 1.0
        if moved <= 0.01:
            raise Wedged(
                f"the screen did not change when {name} was opened ({moved:.1%} of pixels). "
                "Reads and every write API keep reporting success while nothing moves. "
                "Reboot the phone: relaunching the runner does not clear this.")

        root = self._record(snap, parent=None, via=None, depth=0)
        if root is None:
            return
        self._app_root = root
        self._ask_for_help_if_walled(snap, name)
        try:
            self._explore(root, todo)
        except Lost as exc:
            self.emit("lost", screen=root.sid, reason=str(exc))

    # ---------------------------------------------------------------- carry on

    def _seed(self, bundle: str, name: str) -> list[Screen]:
        """Carry on from every earlier scan of this app instead of starting over.

        Their screens, screenshots and edges are copied into this run, renumbered
        so two scans' s001 do not collide and de-duplicated by content, so this
        folder is the whole collection and no screen is collected twice. Every
        control an earlier scan tapped (or refused) is marked tried and is not
        tapped again. Returned: the screens that still have untried controls, the
        only ones worth walking back to.
        """
        wanted = {bundle, self.plan.app}
        earlier = sorted(r["run"] for r in list_runs()
                         if (r.get("plan") or {}).get("app") in wanted and r["run"] != self.out.name)
        for run in earlier:
            src = CRAWLS / run
            try:
                old = json.loads((src / "manifest.json").read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("carry on: cannot read %s: %s", run, exc)
                continue
            sids: dict[str, str] = {}
            for s in old.get("screens", []):
                # Earlier scans hold one live screen many times over (11 copies of
                # "Calculating your library" at different percentages): fold them.
                known = self.screens.get(s.get("textual", "")) or next(
                    (x for x in self.screens.values()
                     if _alike(s.get("bundle_id", ""), s.get("title", ""),
                               {c.get("key") for c in s.get("controls") or []}, x)), None)
                if known is not None:
                    sids[s["sid"]] = known.sid
                    continue
                sid = f"s{len(self.screens) + 1:03d}"
                sids[s["sid"]] = sid
                shots = [self._adopt_shot(src, rel, sid if i == 0 else f"{sid}-v{i}")
                         for i, rel in enumerate(s.get("shots") or [])]
                fields = {k: v for k, v in s.items() if k in Screen.__dataclass_fields__}
                screen = Screen(**{**fields, "sid": sid, "shots": [p for p in shots if p]})
                self.screens[screen.textual] = screen
                self.by_sid[sid] = screen
            for e in old.get("edges", []):
                frm = sids.get(e.get("frm", ""))
                if frm is None:
                    continue
                to = sids.get(e.get("to", ""), "")
                if e.get("kind") == "crossing":
                    to = self._adopt_shot(src, e.get("to", ""), f"{frm}-out")
                self.edges.append(Edge(frm, to, e.get("label", ""), e.get("key", ""), e.get("kind", "tap")))
                self.tried.add((self.by_sid[frm].structural, e.get("key", "")))
            seen = {(k.get("screen"), k.get("what")) for k in self.skipped}
            for k in old.get("skipped", []):
                k = {**k, "screen": sids.get(k.get("screen", ""), k.get("screen", ""))}
                if (k.get("screen"), k.get("what")) not in seen:
                    self.skipped.append(k)
                    seen.add((k.get("screen"), k.get("what")))
        if not self.screens:
            return []
        # A refused control would be refused again for the same reason: skip the look.
        for screen in self.screens.values():
            for c in screen.controls:
                if c.get("skip"):
                    self.tried.add((screen.structural, c.get("key", "")))
        todo: list[Screen] = []
        layouts: dict[str, int] = {}
        for screen in sorted(self.screens.values(), key=lambda s: (s.depth, s.sid)):
            n = layouts.get(screen.structural, 0)
            layouts[screen.structural] = n + 1
            if screen.depth >= self.plan.max_depth or n >= self.plan.variant_cap:
                continue
            if any((screen.structural, c.get("key", "")) not in self.tried for c in screen.controls):
                todo.append(screen)
        self.variants = {k: min(v, self.plan.variant_cap) for k, v in layouts.items()}
        self.seeded = len(self.screens)
        for screen in sorted(self.screens.values(), key=lambda s: s.sid):
            self.emit("screen", **screen.as_dict(), shot=screen.shots[0] if screen.shots else "",
                      carried=True, total=len(self.screens))
        scans = f"{len(earlier)} earlier scan" + ("s" if len(earlier) != 1 else "")
        self.say(f"Carrying on from {scans} of {name}: **{self.seeded} screens** are already "
                 f"collected, so I will not take those again. " + (
                     f"{len(todo)} of them still have controls nobody has tried, and those are "
                     f"the only ones I will walk back to." if todo else
                     "Every control on them has been tried, so I will only look for anything new "
                     "on the first screen."))
        return todo

    def _adopt_shot(self, src: Path, rel: str, tag: str) -> str:
        """Copy one screenshot, and its thumbnail, in from an earlier run, renamed."""
        png = src / rel if rel else None
        if png is None or not png.is_file():
            return ""
        self.shot_count += 1
        name = f"{self.shot_count:04d}-{tag}.png"
        shutil.copy2(png, self.shots_dir / name)
        thumb = src / rel.replace("shots/", "thumbs/", 1).replace(".png", ".jpg")
        if thumb.is_file():
            shutil.copy2(thumb, self.thumbs_dir / name.replace(".png", ".jpg"))
        return f"shots/{name}"

    # ------------------------------------------------------------- traversal

    def _explore(self, root: Screen, carried: Iterable[Screen] = ()) -> None:
        """Breadth-first from the app's first screen.

        Breadth, not depth, and the reason is what the collection is FOR. A
        depth-first crawl of Settings measured here spent its entire budget inside
        the Apple Account sign-in sheet: ten screens, nine of them the same modal,
        and it never reached General, Display or Privacy. Someone asking for "every
        screen in this app" wants the app's shape first and the third level of one
        branch last, which is the order a queue gives you and a stack does not.

        The price is navigation: a stack is always one back-tap from the next
        screen, while a queue may have to replay a path from the root. That is the
        right trade at these depths, and `_goto` takes the cheap route when the
        cheap route is available.
        """
        # Carried-over screens with untried controls queue behind the root: a
        # screen seen before is never re-queued by a revisit (visits > 1).
        frontier: list[Screen] = [root] + [s for s in carried if s is not root]
        while frontier:
            self._check_budget()
            spent = self._app_budget_spent()
            if spent:
                self.emit("app_budget", screen=frontier[0].sid, reason=spent)
                return
            screen = frontier.pop(0)
            try:
                self._goto(screen)
            except Lost as exc:
                self.emit("unreachable", screen=screen.sid, reason=str(exc))
                continue
            try:
                frontier.extend(self._sweep(screen))
            except Lost as exc:
                self.emit("lost", screen=screen.sid, reason=str(exc))

    def _sweep(self, screen: Screen) -> list[Screen]:
        """Tap every untried control on this screen, one viewport at a time.

        Controls are taken from the viewport in front of us rather than from a
        remembered list, because a coordinate read before a scroll is a coordinate
        that no longer points at anything.
        """
        found: list[Screen] = []
        for pass_i in range(self.plan.scroll_cap + 1):
            self._check_budget()
            if self._app_budget_spent():
                return found
            snap = self.phone.snapshot(with_screenshot=False, stable=False)
            if pass_i > 0:
                # A scrolled viewport is more of the same screen, not a new one.
                self._capture_viewport(screen, snap, pass_i)
            fresh = [c for c in self._controls(snap) if (screen.structural, control_key(c)) not in self.tried]
            for control in fresh:
                self._check_budget()
                if self._app_budget_spent():
                    return found
                try:
                    child = self._visit(screen, control, pass_i)
                except Lost as exc:
                    # One control going somewhere unexpected is normal and is not a
                    # reason to abandon the screen. Get back and take the next one.
                    self.emit("lost", screen=screen.sid, control=control.text[:40], reason=str(exc))
                    self._replay(screen)
                    if pass_i:
                        self._rescroll(pass_i)
                    continue
                if child is not None:
                    found.append(child)
            if not self._scroll_once(screen):
                break
        return found

    def _goto(self, screen: Screen) -> None:
        """Stand on a screen, cheapest route first, and prove it before returning."""
        if self._at(screen):
            return
        if screen.depth and self._at_parent_of(screen):
            self._back_conservatively()
            if self._at(screen):
                return
        self._replay(screen)

    def _at_parent_of(self, screen: Screen) -> bool:
        """Cheap test for 'one back-tap away', so the common case stays one tap."""
        if not screen.path:
            return False
        parent_textual = None
        for other in self.screens.values():
            if other.depth == screen.depth - 1 and other.path == screen.path[:-1]:
                parent_textual = other.textual
                break
        if parent_textual is None:
            return False
        try:
            return self.phone.snapshot(with_screenshot=False, stable=False).signature == parent_textual
        except WDAError:
            return False

    def _visit(self, screen: Screen, control: Element, scrolls: int) -> Screen | None:
        key = control_key(control)
        self.tried.add((screen.structural, key))
        label = control.text[:60] or control.type
        verdict = self._safe_to_tap(control)
        if verdict:
            self.skipped.append({"kind": "control", "screen": screen.sid, "what": label, "why": verdict})
            self.emit("skip", screen=screen.sid, control=label, why=verdict)
            return None

        # Coordinates read before the last excursion may point at nothing now, so
        # the control is resolved again, by identity, immediately before the tap.
        live = self._here(key)
        if live is None:
            self.emit("gone", screen=screen.sid, control=label)
            return None

        before_png = self.phone.wda.screenshot()
        self.emit("tap", screen=screen.sid, control=label, depth=screen.depth)
        try:
            self.phone.wda.tap(live.cx, live.cy)
        except WDAError as exc:
            self.emit("tap_failed", screen=screen.sid, control=label, error=str(exc)[:160])
            return None
        self.taps += 1
        snap = self._settle()

        moved = visual_difference(before_png, snap.png) if snap.png else 0.0
        bundle = snap.bundle_id or ""
        left_the_app = bool(bundle) and bool(self._app_bundle) and bundle != self._app_bundle
        if left_the_app:
            self._crossing(screen, label, key, snap, bundle)
            self._return_to(screen, scrolls, replay=True)
            return None

        if snap.signature == screen.textual and moved < 0.01:
            self._dead_taps += 1
            self.edges.append(Edge(screen.sid, screen.sid, label, key, "no-op"))
            self.emit("noop", screen=screen.sid, control=label, moved=round(moved, 4),
                      streak=self._dead_taps)
            # 14, not 8. A screen of informational rows (Settings > About is twelve
            # of them) answers no tap at all and is perfectly healthy, so the
            # threshold has to sit above the longest honest dead stretch.
            if self._dead_taps >= 14:
                self._assert_alive(screen)
            return None
        self._dead_taps = 0

        # Still the same screen: its contents moved on their own (Talika's
        # "Calculating your library" ring ticks up every second). Recording that as
        # a new screen, then relaunching the app to get "back" to the screen the
        # crawl never left, was 12 relaunches in two minutes of one scan.
        if self._same_screen(snap, screen):
            self.edges.append(Edge(screen.sid, screen.sid, label, key, "no-op"))
            self.emit("noop", screen=screen.sid, control=label, moved=round(moved, 4), live=True)
            return None

        child = self._record(snap, parent=screen, via={"label": label, "key": key}, depth=screen.depth + 1)
        if child is None:
            self._return_to(screen, scrolls)
            return None
        if child.sid != screen.sid:
            self.edges.append(Edge(screen.sid, child.sid, label, key, "tap"))

        worth_exploring = self._should_descend(child)
        if worth_exploring:
            self.variants[child.structural] = self.variants.get(child.structural, 0) + 1
        self._return_to(screen, scrolls)
        return child if worth_exploring else None

    def _should_descend(self, screen: Screen) -> bool:
        if screen.depth >= self.plan.max_depth:
            return False
        if screen.visits > 1:
            return False
        return self.variants.get(screen.structural, 0) < self.plan.variant_cap

    def _crossing(self, screen: Screen, label: str, key: str, snap: Snapshot, bundle: str) -> None:
        """A tap left the app. Collect the screenshot, then get back and carry on.

        Worth a shot and an edge: "Settings > Privacy > Analytics opens Safari" is
        exactly the kind of flow the collection is for. It is not worth crawling,
        because the scope was one app. The return is a replay rather than a back:
        we are in a different app now, and its back button is not ours.
        """
        name = self.phone.name_for_bundle(bundle) or bundle
        sid = self._shoot_foreign(snap, f"{screen.sid}-out")
        self.edges.append(Edge(screen.sid, sid, label, key, "crossing"))
        self.crossings += 1
        self.emit("crossing", screen=screen.sid, control=label, to=name, bundle=bundle, shot=sid)
        self.say(f"“{label}” left the app and opened {name}. Screenshot kept, going back.")

    # -------------------------------------------------------------- recording

    def _record(self, snap: Snapshot, parent: Screen | None, via: dict | None, depth: int) -> Screen | None:
        """Register what is on screen, and collect it if we have not got it.

        Collecting is cheap (one screenshot, ~100ms) so a new content signature is
        always worth keeping. Descending is expensive, and that decision is made
        separately in `_should_descend`.
        """
        textual = snap.signature
        structural = fingerprint(snap.bundle_id, snap.elements)
        known = self.screens.get(textual)
        if known is None:
            # A live screen never reads the same twice, so match it by what it is.
            title = self._title(snap.elements)
            keys = {control_key(c) for c in self._controls(snap)}
            known = next((s for s in self.screens.values()
                          if _alike(snap.bundle_id or "", title, keys, s)), None)
        if known is not None:
            known.visits += 1
            self.emit("revisit", screen=known.sid, visits=known.visits)
            return known

        sid = f"s{len(self.screens) + 1:03d}"
        screen = Screen(
            sid=sid, structural=structural, textual=textual,
            bundle_id=snap.bundle_id, app=snap.app_name or self.phone.name_for_bundle(snap.bundle_id),
            title=self._title(snap.elements, via), depth=depth,
            path=(list(parent.path) + [via]) if parent and via else [],
            elements=len(snap.elements), first_seen=time.time(),
        )
        self.screens[textual] = screen
        self.by_sid[sid] = screen
        self._app_screens += 1
        shot = self._shoot(snap, screen, viewport=0)
        screen.controls = [
            {"label": c.text[:60] or c.type, "type": c.type, "key": control_key(c),
             "x": round(c.cx), "y": round(c.cy), "skip": self._safe_to_tap(c)}
            for c in self._controls(snap)
        ]
        self.emit("screen", **screen.as_dict(), shot=shot,
                  variant_of=self.variants.get(structural, 0), total=len(self.screens))
        where = " › ".join(p["label"] for p in screen.path) or "the first screen"
        self.say(f"Collected **{screen.title or screen.sid}** ({where}). "
                 f"{len(self.screens)} screens so far.")
        return screen

    def _capture_viewport(self, screen: Screen, snap: Snapshot, viewport: int) -> None:
        """A scrolled-down view of a screen we already have. Only kept if it differs."""
        png = self.phone.wda.screenshot()
        if screen.shots:
            try:
                previous = (self.shots_dir / Path(screen.shots[-1]).name).read_bytes()
                if visual_difference(previous, png) < 0.02:
                    return
            except OSError:
                pass
        path = self._write_shot(png, f"{screen.sid}-v{viewport}")
        screen.shots.append(path)
        self.emit("viewport", screen=screen.sid, viewport=viewport, shot=path)

    def _shoot(self, snap: Snapshot, screen: Screen, viewport: int) -> str:
        png = snap.png or self.phone.wda.screenshot()
        path = self._write_shot(png, f"{screen.sid}" if viewport == 0 else f"{screen.sid}-v{viewport}")
        screen.shots.append(path)
        return path

    def _shoot_foreign(self, snap: Snapshot, tag: str) -> str:
        png = snap.png or self.phone.wda.screenshot()
        return self._write_shot(png, tag)

    def _write_shot(self, png: bytes, tag: str) -> str:
        self.shot_count += 1
        name = f"{self.shot_count:04d}-{tag}.png"
        if self.plan.full_res:
            (self.shots_dir / name).write_bytes(png)
        # A thumbnail as well as the full frame: the gallery loads 200 of these and
        # a page of 200 full-resolution PNGs is 300 MB of decode. 960 tall, because
        # a gallery card is ~220px wide on a 2x screen and 420 was visibly soft there.
        try:
            thumb = downscale(load_image(png), max_edge=960)
            thumb.convert("RGB").save(self.thumbs_dir / name.replace(".png", ".jpg"),
                                      "JPEG", quality=82, optimize=True)
        except Exception as exc:            # a thumbnail is never worth failing a run
            log.debug("thumbnail failed: %s", exc)
        return f"shots/{name}"

    @staticmethod
    def _title(elements: Iterable[Element], via: dict | None = None) -> str:
        """What to call this screen, best source first.

        The navigation bar is the right answer when it has one, but on iOS 26 a
        large-title screen often reports an unnamed NavigationBar with the title
        as a separate StaticText below the status bar, and on a real phone that
        left half the collected screens with no name at all. The last resort is
        the row that was tapped to get here, which is what a person would call it
        anyway: "Wi-Fi, JustCo_Net" is the Wi-Fi screen.
        """
        items = list(elements)
        for e in items:
            if e.type == "NavigationBar" and e.text.strip():
                return e.text.strip()[:60]
        candidates = [
            e for e in items
            if e.type == "StaticText" and e.text.strip() and 40 < e.y < 220 and e.h >= 18
        ]
        candidates.sort(key=lambda e: (e.y, -e.h))
        for e in candidates:
            text = e.text.strip()
            # The status bar's clock and carrier read like titles and are not.
            if (len(text) <= 2 or ":" in text[:6]
                    or text.lower() in {"search", "cancel", "edit", "toolbar", "tab bar",
                                        "navigation bar", "page control", "done"}):
                continue
            return text[:60]
        if via and via.get("label"):
            label = str(via["label"]).split(",")[0].strip()
            # "photo.stack.fill" is an SF Symbol name that an app left in an
            # accessibility label. It is an icon's identity, not a screen's name.
            if not (" " not in label and label.count(".") >= 2):
                return label[:60]
        return ""

    # ----------------------------------------------------------- control rules

    def _controls(self, snap: Snapshot) -> list[Element]:
        """The controls worth tapping FORWARD from this screen.

        Three whole classes of element are removed here, and each one was costing
        real taps in a measured run:

        * the way back. iOS labels a back button with the PARENT screen's name, so
          on General the back button reads "Settings" and looks exactly like a row
          worth opening. Tapping it recorded the parent as its own child and left
          a path that could never be replayed. It is found by position instead: a
          button in the left third of the navigation bar.
        * the accessory chevron. Every Settings row carries a separate `Button
          "chevron"` on top of the `Cell`, so a screen of 11 rows offers 22
          controls that go to 11 places.
        * anything that types. A keyboard key or a text field is not navigation.
        """
        back = self._nav_back(snap)
        cells = [e for e in snap.elements if e.type in {"Cell", "Button", "Link"} and e.area > 0]
        out = []
        for e in snap.elements:
            if e.type in INPUT_TYPES:
                continue
            if not e.enabled or e.w < 8 or e.h < 8:
                continue
            # `accessible` is NOT a usable test here. WebDriverAgent computes it by
            # hit-testing every node, which measured 5.82s against 0.89s for one
            # screen, so client.py strips it from the tree by default and every
            # element arrives with accessible=False. An earlier filter that
            # required it dropped all 15 app icons on the home screen: the entire
            # screen, gone, on a field that was never populated.
            if e.type not in INTERACTIVE and not e.text:
                continue
            if back is not None and e.idx == back.idx:
                continue
            if _words(e.text) in BACK_CONTROLS:
                continue
            if self._is_accessory(e, cells):
                continue
            if self._is_duplicate(e, out):
                continue
            out.append(e)
        # Reading order: a crawl that walks a screen top to bottom is a crawl whose
        # output a human can follow next to the screenshot.
        out.sort(key=lambda e: (round(e.cy / 12), e.cx))
        return out

    @staticmethod
    def _is_duplicate(e: Element, kept: list[Element]) -> bool:
        """The same target described twice.

        iOS reports the home screen's search affordance as a StaticText "Search"
        and an Image "Search" eight points apart, and a tab bar as both a Tab and
        the Image inside it. Tapping both costs a tap and a settle for nothing.
        """
        for other in kept:
            if abs(other.cx - e.cx) < 12 and abs(other.cy - e.cy) < 12:
                if not e.text or _words(other.text) == _words(e.text):
                    return True
        return False

    @staticmethod
    def _is_accessory(e: Element, cells: list[Element]) -> bool:
        """A small unlabelled control drawn inside a row, which goes where the row goes."""
        label = _words(e.text)
        if label and label not in {"chevron", "more", "info", "detail", "disclosure"}:
            return False
        for row in cells:
            if row.idx == e.idx or row.type != "Cell" or row.area <= 0:
                continue
            if (row.x <= e.cx <= row.x + row.w and row.y <= e.cy <= row.y + row.h
                    and e.area < row.area * 0.6):
                return True
        return False

    @staticmethod
    def _nav_back(snap: Snapshot) -> Element | None:
        """The navigation bar's back button, found by where it sits rather than what
        it says, because what it says is the name of the screen behind it."""
        bar = next((e for e in snap.elements if e.type == "NavigationBar"), None)
        width = snap.geometry.point_w or 400
        top, bottom = (bar.y, bar.y + bar.h) if bar else (0.0, 120.0)
        best: Element | None = None
        for e in snap.elements:
            if e.type not in {"Button", "Link"} or not (top - 2 <= e.cy <= bottom + 2):
                continue
            if e.cx > width * 0.28:
                continue
            if best is None or e.cx < best.cx:
                best = e
        return best

    def _safe_to_tap(self, e: Element) -> str:
        """Empty string means safe. Anything else is the reason it was skipped."""
        if self.plan.skip_mutating and e.type in MUTATING_TYPES:
            return f"a {e.type} changes a setting rather than navigating"
        haystack = " ".join([e.label, e.name, e.value, e.identifier, " ".join(e.children_text)]).lower()
        text = _words(e.text)
        # A row whose label happens to contain a dangerous word is not a dangerous
        # control: "Delete" is refused, "Recently Deleted" is a screen to collect.
        # So the match is on the label as a whole, or on its leading words.
        for word in NEVER_TAP:
            if text == word or text.startswith(word + " ") and len(text) <= len(word) + 14:
                return f"{word!r} is irreversible, sends something or grants a permission"
        if self.plan.cautious:
            for word in CAREFUL_TAP:
                if text == word or text.startswith(word + " ") and len(text) <= len(word) + 14:
                    return f"{word!r} creates something (turn off 'cautious' to include these)"
        verdict = self.guard.classify_tap(e)
        if verdict.needs_confirmation:
            return verdict.reason
        if "payment" in haystack or "card number" in haystack:
            return "looks like a payment screen"
        return ""

    # -------------------------------------------------------------- navigation

    def _settle_twice(self) -> Snapshot:
        """A settled read, confirmed by a second one.

        Used where the screen's identity is about to be written down and will be
        compared against for the rest of the run. The pixel-stability gate can
        return during a launch, before the tree has finished being built: the app's
        root was recorded from a half-built tree, and every later attempt to get
        back to it failed against a hash of a screen that never existed again.
        A second tree read costs ~0.2s and makes the identity real.
        """
        snap = self._settle()
        for _ in range(3):
            again = self.phone.snapshot(with_screenshot=False, stable=False)
            if again.signature == snap.signature:
                return snap
            time.sleep(0.6)
            snap = self._settle()
        return snap

    def _settle(self) -> Snapshot:
        """Read the screen once it has stopped moving, and clear any alert first.

        An alert is modal: leave it up and every subsequent tap lands on the same
        two buttons and the crawl stalls. It is also a screen worth having, so it
        is collected before it is closed.
        """
        snap = self.phone.snapshot(with_screenshot=True, stable=True)
        if not snap.alert:
            return snap
        text = str(snap.alert.get("text") or "")[:120]
        self.say(f"An alert came up: “{text}”. Keeping it and closing it the safest way.")
        shot = self._shoot_foreign(snap, "alert")
        self.emit("alert", text=text, buttons=snap.alert.get("buttons") or [], shot=shot)
        if self._close_alert(snap):
            return self.phone.snapshot(with_screenshot=True, stable=True)
        return snap

    # The least committal button on an alert, best first. "OK" is last because on
    # a confirmation it is the yes; on the "no internet connection" alerts a crawl
    # actually provokes it is the only button there is.
    _ALERT_ORDER = ("cancel", "don't allow", "dont allow", "not now", "later", "no thanks",
                    "skip", "dismiss", "close", "maybe later", "ok")

    def _close_alert(self, snap: Snapshot) -> bool:
        buttons = [str(b) for b in (snap.alert or {}).get("buttons") or []]
        choice = ""
        for want in self._ALERT_ORDER:
            for b in buttons:
                if b.strip().lower() == want:
                    choice = b
                    break
            if choice:
                break
        if not choice and buttons:
            choice = buttons[0]
        try:
            self.phone.wda.accept_alert(choice or None)
        except WDAError as exc:
            self.emit("alert_stuck", error=str(exc)[:160])
            return False
        time.sleep(0.6)
        # Every write in this stack reports success it did not earn, so check.
        if self.phone.wda.alert_text():
            for e in self.phone.snapshot(with_screenshot=False, stable=False).elements:
                if e.type == "Button" and e.text.strip() == choice:
                    self.phone.wda.tap(e.cx, e.cy)
                    time.sleep(0.6)
                    break
            if self.phone.wda.alert_text():
                self.emit("alert_stuck", detail=f"{choice!r} would not close the alert")
                return False
        self.emit("alert_closed", button=choice)
        return True

    def _current_bundle(self) -> str:
        try:
            return str(self.phone.wda.active_app_info().get("bundleId") or "")
        except WDAError:
            return ""

    def _scroll_once(self, screen: Screen) -> bool:
        """One viewport down. False when the screen did not move, i.e. the end."""
        before = self.phone.wda.screenshot()
        try:
            self.phone.swipe("down", distance=0.62)
        except WDAError:
            return False
        time.sleep(0.5)
        moved = visual_difference(before, self.phone.wda.screenshot())
        return moved > 0.02

    def _at(self, screen: Screen) -> bool:
        """Are we on this screen? Asks the tree, not the last action's return value.

        Three tests, loosening as they go, because a real screen is not identical
        to itself between visits. Settings on a real phone grows and loses a
        banner ("You've reached your storage limit") between launches, which moves
        every row below it and changes both hashes, and an exact-match test called
        that a different screen and threw the crawl away.
        """
        try:
            snap = self.phone.snapshot(with_screenshot=False, stable=False)
        except WDAError:
            return False
        if snap.signature == screen.textual:
            return True
        if fingerprint(snap.bundle_id, snap.elements) == screen.structural:
            return True
        # Same app, and most of the same controls: the same screen with different
        # contents. 0.7 is deliberate: a banner or two moving is well inside it,
        # and a different screen in the same app shares far less than that.
        if snap.bundle_id != screen.bundle_id or not screen.controls:
            return False
        want = {c["key"] for c in screen.controls}
        here = {control_key(e) for e in self._controls(snap)}
        if not want or not here:
            return False
        overlap = len(want & here) / len(want)
        if overlap >= 0.7:
            self.emit("at_loose", screen=screen.sid, overlap=round(overlap, 2))
            return True
        return False

    def _return_to(self, screen: Screen, scrolls: int = 0, replay: bool = False) -> None:
        """Get back to a screen and PROVE it, or replay the path from the app root.

        The ladder is deliberate. A nav-bar back is one tap. An edge swipe is one
        gesture. Replaying the path costs depth taps and a relaunch, so it is last,
        and it is still far cheaper than a crawl that carries on from the wrong
        screen and files the screenshots under the wrong parent.

        `replay=True` skips straight to the bottom of the ladder, for when we
        already know the back button on screen is not ours to press.

        One rung in the middle earns its place on every tab-bar app. Talika has no
        navigation bar at all, so back failed on every return and the crawl paid a
        terminate-plus-relaunch-plus-replay for it: measured, 15 relaunches in 40
        taps, about half the run spent walking back to where it already was. But a
        tab bar is persistent, which means the way back is usually still on screen
        and one tap away. Trying that before the relaunch turned those 15 into
        almost none.
        """
        if replay:
            self._replay(screen)
            if scrolls:
                self._rescroll(scrolls)
            return
        # A layout that ignored two back attempts ignores them every time (Talika's
        # library flow: 26 misses in one run, ~4s each), so it is remembered and
        # the crawl goes straight to the routes that do work there.
        here = self._layout_here()
        if here not in self._backless:
            for attempt in range(2):
                self._back_conservatively()
                if self._at(screen):
                    if scrolls:
                        self._rescroll(scrolls)
                    return
                self.emit("back_missed", screen=screen.sid, attempt=attempt + 1)
            if here:
                self._backless.add(here)
        if self._tap_way_back(screen):
            if scrolls:
                self._rescroll(scrolls)
            return
        self._replay(screen)
        if scrolls:
            self._rescroll(scrolls)

    def _same_screen(self, snap: Snapshot, screen: Screen) -> bool:
        return _alike(snap.bundle_id or "", self._title(snap.elements),
                      {control_key(c) for c in self._controls(snap)}, screen)

    def _layout_here(self) -> str:
        try:
            snap = self.phone.snapshot(with_screenshot=False, stable=False)
        except WDAError:
            return ""
        return fingerprint(snap.bundle_id, snap.elements)

    def _back_conservatively(self) -> None:
        """Prefer the nav bar's own back, then Cancel/Close, then the edge swipe.

        Phone.back() also accepts "Done", which on an edit sheet COMMITS what is in
        it. Nothing here types, so there is rarely anything to commit, but "rarely"
        is not a thing to build a crawler on.
        """
        try:
            snap = self.phone.snapshot(with_screenshot=False, stable=False)
        except WDAError:
            return
        # The navigation bar's own back button first, found by position: on iOS it
        # is labelled with the parent screen's name, so there is no word to match.
        target = self._nav_back(snap)
        if target is None:
            ranked = {"back": 0, "‹": 0, "<": 1, "close": 2, "cancel": 3, "dismiss": 3, "×": 3, "x": 4}
            best: tuple[int, Element] | None = None
            for e in snap.elements:
                if e.type not in {"Button", "Link"}:
                    continue
                rank = ranked.get(_words(e.text))
                if rank is None:
                    continue
                if best is None or rank < best[0]:
                    best = (rank, e)
            target = best[1] if best else None
        if target is not None:
            try:
                self.phone.wda.tap(target.cx, target.cy)
                time.sleep(0.6)
                return
            except WDAError:
                pass
        try:
            geo = self.phone.wda.geometry()
            self.phone.wda.drag(2, geo.point_h * 0.5, geo.point_w * 0.6, geo.point_h * 0.5, 0.25)
            time.sleep(0.6)
        except WDAError:
            pass

    def _rescroll(self, times: int) -> None:
        for _ in range(times):
            if not self._scroll_once_quiet():
                return

    def _scroll_once_quiet(self) -> bool:
        try:
            self.phone.swipe("down", distance=0.62)
            time.sleep(0.4)
            return True
        except WDAError:
            return False

    def _tap_way_back(self, screen: Screen) -> bool:
        """One tap back, using a control that is still on screen.

        Two shapes, both common. A screen reached by tapping X is usually reached
        again by tapping X, and in a tab-bar app that X never left the screen. The
        app's first screen is usually whatever the leftmost tab shows, so that is
        the fallback. Either way it is verified before it is believed.
        """
        want = screen.path[-1]["key"] if screen.path else None
        if want:
            live = self._here(want)
            if live is not None:
                try:
                    self.phone.wda.tap(live.cx, live.cy)
                except WDAError:
                    return False
                self.taps += 1
                self._settle()
                if self._at(screen):
                    self.tapbacks += 1
                    self.emit("tapped_back", screen=screen.sid,
                              control=screen.path[-1]["label"][:40])
                    return True
            return False

        # The root. Try the tab bar it was wearing when we first saw it.
        try:
            snap = self.phone.snapshot(with_screenshot=False, stable=False)
        except WDAError:
            return False
        floor = (snap.geometry.point_h or 800) * 0.86
        known = {c["key"] for c in screen.controls}
        tabs = [e for e in snap.elements
                if e.cy >= floor and control_key(e) in known and e.type in {"Tab", "Button", "Icon"}]
        tabs.sort(key=lambda e: e.cx)
        for tab in tabs[:2]:
            try:
                self.phone.wda.tap(tab.cx, tab.cy)
            except WDAError:
                return False
            self.taps += 1
            self._settle()
            if self._at(screen):
                self.tapbacks += 1
                self.emit("tapped_back", screen=screen.sid, control=(tab.text or tab.type)[:40])
                return True
        return False

    def _replay(self, screen: Screen) -> None:
        """Home, relaunch, and tap the recorded path again, verifying as we go."""
        self.relaunches += 1
        self.emit("replay", screen=screen.sid, depth=len(screen.path))
        # From the phone this looks exactly like the app crashing. Say what it is
        # once, then only count: twelve identical sentences explained nothing.
        if self.relaunches == 1:
            self.say(f"The screen I was on has no Back button that works, so to get back to "
                     f"{screen.title or screen.sid} I close the app and open it again. When you see "
                     f"it vanish and reopen, that is me, not a crash.")
        elif self.relaunches % 5 == 0:
            self.say(f"Closed and reopened the app {self.relaunches} times so far, to get back "
                     f"from screens that have no Back button.")
        self._recover_to_root()
        for step in screen.path:
            target = self._find_control(step["key"])
            if target is None:
                raise Lost(f"replaying to {screen.sid}: {step['label']!r} is not on the screen any more")
            self.phone.wda.tap(target.cx, target.cy)
            self.taps += 1
            self._settle()
        if not self._at(screen):
            raise Lost(f"replayed the path to {screen.sid} and landed somewhere else")
        self.emit("replayed", screen=screen.sid)

    def _here(self, key: str) -> Element | None:
        """The control with this identity on the current screen, or None."""
        try:
            snap = self.phone.snapshot(with_screenshot=False, stable=False)
        except WDAError:
            return None
        for e in snap.elements:
            if control_key(e) == key:
                return e
        return None

    def _find_control(self, key: str) -> Element | None:
        try:
            snap = self.phone.snapshot(with_screenshot=False, stable=False)
        except WDAError:
            return None
        for e in snap.elements:
            if control_key(e) == key:
                return e
        # It may be below the fold. Scroll a little and look again.
        for _ in range(self.plan.scroll_cap):
            if not self._scroll_once_quiet():
                break
            try:
                snap = self.phone.snapshot(with_screenshot=False, stable=False)
            except WDAError:
                return None
            for e in snap.elements:
                if control_key(e) == key:
                    return e
        return None

    def _recover_to_root(self) -> None:
        """Back to the app's FIRST screen, by killing it and starting it again.

        Reopening is not enough: iOS restores an app to wherever it was when you
        left it, so `open_app` on a Settings that was showing General comes back
        showing General. Measured here: a replay of the root path landed on the
        child screen and the crawler declared itself lost. Terminating first makes
        the launch cold, and cold means the root.
        """
        target = getattr(self, "_app_target", "") or self.plan.app
        if not target:
            return
        bundle = getattr(self, "_app_bundle", "") or self.phone.resolve_app(target) or target
        try:
            self.phone.wda.terminate_app(bundle)
            time.sleep(0.6)
        except WDAError as exc:
            log.debug("terminate %s: %s", bundle, exc)
        res = self.phone.open_app(target)
        if not res.ok:
            raise Lost(f"could not reopen {target}: {res.error or res.detail}")
        self._settle_twice()

    def _assert_alive(self, back_to: Screen | None = None) -> None:
        """A long run of taps that changed nothing: is it the screen, or the phone?

        The test has to run somewhere guaranteed to move. Measured here: the first
        version swiped wherever it happened to be, hit Settings > About (a list of
        informational rows that neither respond to taps nor scroll sideways), got
        0% movement and declared a perfectly healthy simulator wedged. The home
        screen always turns a page, so the test goes there, and the crawler replays
        its way back afterwards.
        """
        self.emit("liveness", detail=f"{self._dead_taps} taps changed nothing; testing the phone itself")
        self.say("A run of taps changed nothing on screen. Checking whether the phone is still responding.")
        self._dead_taps = 0
        try:
            self.phone.home()
            time.sleep(0.8)
            geo = self.phone.wda.geometry()
            y = geo.point_h * 0.5
            moved = 0.0
            for from_x, to_x in ((geo.point_w * 0.85, geo.point_w * 0.15),
                                 (geo.point_w * 0.15, geo.point_w * 0.85)):
                before = self.phone.wda.screenshot()
                self.phone.wda.drag(from_x, y, to_x, y, duration=0.15)
                time.sleep(0.9)
                moved = max(moved, visual_difference(before, self.phone.wda.screenshot()))
                self.phone.wda.drag(to_x, y, from_x, y, duration=0.15)
                time.sleep(0.5)
                if moved > 0.01:
                    break
        except WDAError as exc:
            raise Wedged(f"the phone stopped answering during the liveness test: {exc}") from exc
        if moved <= 0.01:
            raise Wedged("the phone stopped responding to gestures while still reporting success "
                         "(a full-width swipe on the home screen moved 0%). Reboot the phone.")
        self.emit("liveness", detail=f"gestures still land ({moved:.1%} moved); going back")
        if back_to is not None:
            self._replay(back_to)

    # ---------------------------------------------------------------- output

    def write_manifest(self) -> dict:
        screens = sorted(self.screens.values(), key=lambda s: s.sid)
        device = {}
        try:
            st = self.phone.wda.status()
            device = {"name": st.get("device"), "ios": st.get("os", {}).get("version"),
                      "wda": st.get("build", {}).get("version")}
        except Exception:
            pass
        summary = {
            "screens": len(screens),
            "shots": self.shot_count,
            "taps": self.taps,
            "edges": len(self.edges),
            "skipped": len(self.skipped),
            "seconds": round(time.time() - self.started, 1),
            "relaunches": self.relaunches,
            "tapbacks": self.tapbacks,
            "crossings": self.crossings,
            "carried_over": self.seeded,
            "reason": self.stop_reason or "the crawl ran out of new screens",
        }
        manifest = {
            "run": self.out.name,
            "plan": self.plan.as_dict(),
            "device": device,
            "started": self.started,
            "summary": summary,
            "screens": [s.as_dict() for s in screens],
            "edges": [e.as_dict() for e in self.edges],
            "skipped": self.skipped,
            "apps": self.apps_done,
        }
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
        write_gallery(self.out, manifest)
        return summary


# ------------------------------------------------------------------- gallery


def write_gallery(out: Path, manifest: dict) -> Path:
    """A standalone page in the run folder, so the collection survives the server.

    The point of collecting screenshots is to look at them later, and "later"
    should not require a python process to be running.
    """
    screens = manifest.get("screens", [])
    edges = manifest.get("edges", [])
    children: dict[str, list[dict]] = {}
    for e in edges:
        children.setdefault(e["frm"], []).append(e)
    summary = manifest.get("summary", {})
    plan = manifest.get("plan", {})
    device = manifest.get("device", {})
    cards = []
    for s in screens:
        shots = s.get("shots") or []
        thumb = (shots[0].replace("shots/", "thumbs/").replace(".png", ".jpg")) if shots else ""
        kids = children.get(s["sid"], [])
        trail = " › ".join(p["label"] for p in s.get("path", [])) or "app root"
        extra = "".join(
            f'<a class="vp" href="{h}">+{i + 1}</a>' for i, h in enumerate(shots[1:])
        )
        cards.append(f"""
<figure class="card">
  <a href="{shots[0] if shots else '#'}"><img loading="lazy" src="{thumb}" alt="{s['sid']}"></a>
  <figcaption>
    <b>{s['sid']}</b> {_esc(s.get('title') or s.get('app') or '')}
    <span class="dim">depth {s.get('depth', 0)} · {s.get('elements', 0)} elements{' · ' + str(len(shots)) + ' shots' if len(shots) > 1 else ''}</span>
    <span class="trail">{_esc(trail)}</span>
    <span class="dim">{len(kids)} way{'s' if len(kids) != 1 else ''} out</span>
    {extra}
  </figcaption>
</figure>""")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(plan.get('app') or plan.get('scope', 'crawl'))} · {len(screens)} screens</title>
<style>
 :root{{--bg:#0e1013;--panel:#15181d;--line:#262b33;--text:#e7eaf0;--dim:#8b93a1;--accent:#5b8cff}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--text);
   font:14px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif}}
 header{{padding:22px 26px;border-bottom:1px solid var(--line);background:var(--panel)}}
 h1{{margin:0 0 6px;font-size:17px}}
 .meta{{color:var(--dim);font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:18px;padding:22px 26px}}
 .card{{margin:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}}
 .card img{{display:block;width:100%;height:auto;background:#000}}
 figcaption{{padding:9px 11px;font-size:12px;display:flex;flex-direction:column;gap:2px}}
 .dim{{color:var(--dim);font-size:11px}}
 .trail{{color:var(--accent);font-size:11px;word-break:break-word}}
 .vp{{color:var(--dim);font-size:11px;text-decoration:none;border:1px solid var(--line);
   border-radius:4px;padding:0 4px;width:fit-content}}
 a{{color:inherit}}
</style></head><body>
<header>
 <h1>{_esc(plan.get('app') or 'the whole phone')} · {len(screens)} screens, {summary.get('shots', 0)} screenshots</h1>
 <div class="meta">
  {_esc(device.get('name') or 'iPhone')} · iOS {_esc(device.get('ios') or '?')} ·
  {summary.get('taps', 0)} taps · {summary.get('seconds', 0)}s ·
  depth &le;{plan.get('max_depth')} · stopped because {_esc(summary.get('reason', ''))}<br>
  {summary.get('skipped', 0)} controls skipped as unsafe to tap · read-only crawl: nothing was typed,
  toggled, sent or bought
 </div>
</header>
<div class="grid">{''.join(cards)}</div>
</body></html>"""
    path = out / "index.html"
    path.write_text(html)
    return path


def shot_filenames(out: Path) -> dict[str, str]:
    """A readable name for every screenshot in a run, keyed by its path in the run.

    On disk a shot is `0007-s007.png`, which records the order it was taken in and
    nothing else. Downloaded, it is `s007-explore.png`: which screen, and what it
    is. A run still in progress has no manifest yet, so its shots keep disk names.
    """
    try:
        screens = json.loads((out / "manifest.json").read_text()).get("screens", [])
    except (OSError, json.JSONDecodeError):
        screens = []
    names: dict[str, str] = {}
    for s in screens:
        base = "-".join(p for p in (s.get("sid", ""), _slug(s.get("title") or "")) if p) or "screen"
        for i, shot in enumerate(s.get("shots") or []):
            names[shot] = f"{base}-scroll{i}.png" if i else f"{base}.png"
    for path in sorted((out / "shots").glob("*.png")):
        names.setdefault(f"shots/{path.name}", path.name)
    # Crossings and alerts are not screens and keep their disk names, and a name
    # is only as good as its uniqueness inside one zip, so settle that here.
    seen: set[str] = set()
    for key, name in names.items():
        stem, n = name[:-4], 2
        while name in seen:
            name, n = f"{stem}-{n}.png", n + 1
        names[key] = name
        seen.add(name)
    return names


def _slug(text: str, limit: int = 40) -> str:
    words = "".join(c if c.isalnum() else " " for c in str(text).lower()).split()
    return "-".join(words)[:limit].strip("-")


def _alike(bundle: str, title: str, keys: set, other: Screen) -> bool:
    """Same app, same title, most of the same controls: the same screen.

    For screens whose contents never hold still. A progress ring or a counter
    changes the content signature on every read, and the layout fingerprint too
    (element labels are part of it), so neither can say "this again". The title
    and the controls around the moving part do not move.
    """
    if not title or bundle != other.bundle_id or title != other.title:
        return False
    want = {c.get("key") for c in other.controls}
    return bool(want and keys) and len(want & keys) / len(want) >= 0.7


def _words(text: str) -> str:
    """A control's label, normalised for whole-label matching."""
    return " ".join(str(text or "").lower().replace("\u2019", "'").split()).strip(" .…›>")


def _esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ------------------------------------------------------------------- planning


def new_run_dir(plan: Plan) -> Path:
    tag = (plan.label or plan.app or plan.scope or "crawl").lower()
    tag = "".join(c if c.isalnum() else "-" for c in tag).strip("-")[:32] or "crawl"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = CRAWLS / f"{stamp}-{tag}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def list_runs() -> list[dict]:
    if not CRAWLS.exists():
        return []
    out = []
    for d in sorted(CRAWLS.iterdir(), reverse=True):
        manifest = d / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "run": d.name,
            "plan": data.get("plan", {}),
            "summary": data.get("summary", {}),
            "screens": len(data.get("screens", [])),
            "started": data.get("started"),
        })
    return out


def earlier_scans(app: str) -> dict:
    """Finished scans of one app, and how many distinct screens they hold between them.

    Folded exactly the way `Crawler._seed` folds them (oldest run first, same
    content or `_alike`), so the number the page promises is the number kept.
    """
    runs, seen, kept = 0, set(), []
    fields = Screen.__dataclass_fields__
    for r in sorted(list_runs(), key=lambda r: r["run"]):
        if (r.get("plan") or {}).get("app") != app:
            continue
        runs += 1
        try:
            screens = json.loads((CRAWLS / r["run"] / "manifest.json").read_text()).get("screens", [])
        except (OSError, json.JSONDecodeError):
            continue
        for s in screens:
            keys = {c.get("key") for c in s.get("controls") or []}
            if s.get("textual") in seen or any(
                    _alike(s.get("bundle_id", ""), s.get("title", ""), keys, k) for k in kept):
                continue
            seen.add(s.get("textual"))
            kept.append(Screen(**{k: v for k, v in s.items() if k in fields}))
    return {"runs": runs, "screens": len(kept)}


def plan_from_prompt(prompt: str, cfg: Config | None = None,
                     catalog: dict[str, str] | None = None,
                     default_app: tuple[str, str] | None = None,
                     fresh: bool = False) -> tuple[Plan, list[str]]:
    """Read a plain-language instruction into a Plan, and say what was understood.

    Deterministic on purpose. A model could parse this more gracefully, but then
    the page could not tell you what it is about to do before it does it, and
    "screenshot everything in Settings" would cost a round trip and a reasoning
    step to become `scope=app, app=Settings`.
    """
    cfg = cfg or Config.load()
    text = " ".join(prompt.lower().split())
    notes: list[str] = []
    plan = Plan()

    catalog = catalog if catalog is not None else installed_apps(cfg)
    # Only real apps are candidates. A phone reports several hundred bundles that
    # are not apps, and two of them are called "Settings": asking for Settings
    # resolved to com.apple.CarPlaySettings, which opens nothing.
    real = {n: b for n, b in catalog.items() if is_user_app(n, b)}
    # Longest app name first, so "App Store" wins over "App".
    hit: tuple[str, str] | None = None
    for name, bundle in sorted(real.items(), key=lambda kv: -len(kv[0])):
        if len(name) < 3:
            continue
        if name.lower() in text:
            hit = (name, bundle)
            break

    whole = any(w in text for w in ("every app", "all apps", "whole phone", "entire phone",
                                    "the phone", "all the apps", "everything on the phone"))
    if whole and not hit:
        plan.scope = "phone"
        plan.max_depth = 2
        plan.per_app_screens = 8
        notes.append("scope: every installed app, shallow (depth 2, 8 screens each)")
    elif hit:
        plan.scope = "app"
        plan.app = hit[1]
        plan.label = hit[0]
        notes.append(f"scope: {hit[0]} only ({hit[1]})")
    elif default_app and default_app[1]:
        # The app picked in the list. "take the rest and skip what you have" names
        # no app, and used to become a scan of Settings.
        plan.scope = "app"
        plan.app = default_app[1]
        plan.label = default_app[0] or default_app[1]
        notes.append(f"scope: {plan.label} only (the app you picked)")
    else:
        plan.scope = "app"
        plan.app = "com.apple.Preferences"
        plan.label = "Settings"
        notes.append("no app named in the prompt, so: Settings. Name an app to crawl that instead.")

    # Carrying on is the default whenever this app has been scanned before: nobody
    # asks for the same 11 screenshots twice. Starting over has to be said.
    over = any(w in text for w in ("start over", "from scratch", "fresh", "all again", "rescan",
                                   "re-scan", "redo"))
    if plan.scope == "app" and not fresh and not over and earlier_scans(plan.app)["runs"]:
        plan.resume = True

    numbers = [int(t) for t in text.replace(",", " ").split() if t.isdigit()]
    if numbers:
        for n in numbers:
            if "minute" in text and n <= 600:
                plan.max_minutes = float(n)
                notes.append(f"time limit: {n} minutes")
                break
        for n in numbers:
            if "screen" in text and 1 <= n <= 2000:
                plan.max_screens = n
                notes.append(f"screen limit: {n}")
                break
        for n in numbers:
            if ("deep" in text or "depth" in text or "level" in text) and 1 <= n <= 8:
                plan.max_depth = n
                notes.append(f"depth limit: {n}")
                break

    if "everything" in text or "exhaustive" in text or "as deep as" in text:
        plan.max_depth = max(plan.max_depth, 5)
        plan.max_screens = max(plan.max_screens, 300)
        plan.max_minutes = max(plan.max_minutes, 60.0)
        notes.append("asked for exhaustive, so the ceilings went up (depth 5, 300 screens, 60 min)")
    if "quick" in text or "fast" in text or "just the main" in text:
        plan.max_depth = min(plan.max_depth, 2)
        plan.max_screens = min(plan.max_screens, 40)
        plan.max_minutes = min(plan.max_minutes, 8.0)
        notes.append("asked for quick, so: depth 2, 40 screens, 8 minutes")

    notes.append("read-only: it taps to navigate, and never types, toggles a switch, "
                 "sends, buys or deletes anything")
    return plan, notes
