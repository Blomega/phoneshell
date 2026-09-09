"""Run one task against one agent, and decide honestly whether it worked.

The loop is deliberately blunt:

    setup (no model)  ->  hand the instruction to the agent  ->  check the phone

The agent never sees the checks, never sees the setup script, and its own
account of what it did counts for nothing. Only the state of the phone
afterwards decides pass or fail. That is the whole point: it is what makes a run
repeatable ten thousand times without a human watching.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import Config, RUNTIME
from ..wda.client import WDAError
from .schema import Check, Step, Task
from .verify import CheckResult, run_all

RESULTS = RUNTIME / "bench"
RESULTS.mkdir(parents=True, exist_ok=True)

# The scaffold prompt. A benchmark measures a model INSIDE a harness, and the
# harness always shapes the result, so this is stated rather than hidden: it
# describes what the tools are for and nothing about any specific task. Runs
# are labelled with whether it was used, and both numbers are published.
SCAFFOLD_PROMPT = """\
You are operating a real iPhone through a set of tools.

Work efficiently. Every tool call is a round trip, so when you already know the
route, use phone_do to run several steps in one call rather than observing
between each tap. A sequence like
  [{"open_app": "Calculator"}, {"tap": "4"}, {"tap": "7"}, {"tap": "multiply"}]
is one call, not four. phone_do stops at the first step that fails and hands you
the screen there, so a wrong guess is cheap.

Observe when you are genuinely exploring, or after something unexpected. Do not
observe to confirm a step you have already been told succeeded.

To set a spinning wheel (a time, a date, a duration, a unit) use phone_set_picker
with the value you want. Do not swipe at a wheel: it steps by whole rows, so a
swipe overshoots and never settles.

If a popup or promo sheet is in the way, call phone_dismiss_popup rather than
hunting for its close button. If a screen does not change after an action, that
action did not work: try a different route instead of repeating it.
"""

# Models that cannot accept an image. They work from the accessibility tree
# alone, which makes them the control group for what vision is actually worth.
VISIONLESS = {"qwen/qwen3-max", "deepseek/deepseek-v3.2"}

AGENT_TOOLS = [
    "mcp__phoneshell__phone_observe", "mcp__phoneshell__phone_tap",
    "mcp__phoneshell__phone_type", "mcp__phoneshell__phone_swipe",
    "mcp__phoneshell__phone_scroll_to", "mcp__phoneshell__phone_open_app",
    "mcp__phoneshell__phone_press", "mcp__phoneshell__phone_alert",
    "mcp__phoneshell__phone_gesture", "mcp__phoneshell__phone_dismiss_popup",
    "mcp__phoneshell__phone_long_press", "mcp__phoneshell__phone_wait_for",
    "mcp__phoneshell__phone_do", "mcp__phoneshell__phone_set_picker",
]


# Apple's ProductType is the only stable machine-readable model id. The map
# exists because "iPhone15,2" names nothing to a reader; these are public
# hardware identifiers, not anything personal.
PRODUCT_NAMES = {
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max",
}
_MODEL_CACHE: list[str] = []


def _hardware_model() -> str:
    """The marketing name of the attached phone, resolved once per process."""
    if _MODEL_CACHE:
        return _MODEL_CACHE[0]
    name = ""
    try:
        out = subprocess.run(["pymobiledevice3", "usbmux", "list"],
                             capture_output=True, text=True, timeout=20).stdout
        for dev in json.loads(out or "[]"):
            pt = str(dev.get("ProductType") or "")
            if pt:
                name = PRODUCT_NAMES.get(pt, pt)
                break
    except Exception:
        pass
    _MODEL_CACHE.append(name)
    return name


class RigUnavailable(RuntimeError):
    """The phone could not be driven, so the task was never really attempted.

    A locked phone accepts nothing and every task after it fails identically. In
    a 32-task run that produces a page of red that looks like a model that cannot
    use a phone, when the truth is a screen that was off. Environment problems
    must never be scored as capability results.
    """


class UsageLimitReached(RuntimeError):
    """The model refused to answer because the account is rate limited.

    This must never be recorded as a failed task. "The model tried and got it
    wrong" and "we could not ask the model" are different facts, and conflating
    them turns a benchmark into a fabrication: a first run here recorded 12.5%
    when in truth 27 of 32 tasks were never attempted.
    """


# Phrases that mean the account, not the model, stopped the run.
LIMIT_PHRASES = (
    "session limit", "usage limit", "rate limit", "rate_limit",
    "hit your limit", "resets at", "try again later", "quota",
    "overloaded_error", "insufficient credit",
)


def looks_rate_limited(text: str) -> bool:
    low = (text or "").lower()
    return any(p in low for p in LIMIT_PHRASES)


@dataclass
class TaskResult:
    task_id: str
    model: str
    passed: bool
    scaffold: bool = False
    device_model: str = ""
    device_os: str = ""
    device_udid: str = ""
    skipped: bool = False
    skip_reason: str = ""
    checks: list[dict] = field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0
    seconds: float = 0.0
    agent_said: str = ""
    error: str = ""
    started_at: float = 0.0


class Runner:
    def __init__(self, phone, mcp_config: Path | None = None):
        self.phone = phone
        self.mcp_config = mcp_config or (RUNTIME / "mcp.json")
        # Once, up front, while nothing is being measured. After this the runner
        # only makes session-free calls, so verifying a task cannot disturb it.
        self.phone.ensure_tree_depth()
        # Shared mode yields the phone when it sees a change it did not cause.
        # During a benchmark that is a false positive waiting to happen: one task
        # was paused mid-run and scored as a failure when the agent had actually
        # found the answer. A measured run always takes the phone.
        if self.phone.cfg.session.mode != "takeover":
            self.phone.cfg.session.mode = "takeover"
            try:
                self.phone.coexist.stop()
            except Exception:
                pass

    def wait_for_unlock(self, patience: float = 900.0) -> bool:
        """Try to unlock, and failing that, WAIT rather than throw the run away.

        A lock has aborted four runs. With no passcode stored there is nothing
        clever to do about the lock itself, but abandoning hours of completed
        work over a screen that a person could clear in two seconds is a choice,
        not a necessity. So: try the stored passcode if there is one, then say
        plainly what is wrong and wait, checking every few seconds. Only give up
        when the phone has stayed locked for a quarter of an hour.
        """
        try:
            if self.phone.ensure_unlocked().ok:
                return True
        except Exception:
            pass
        print("\n  the phone is LOCKED and no passcode is stored.", flush=True)
        print("  unlock it and the run continues by itself; waiting up to "
              f"{patience/60:.0f} minutes.", flush=True)
        deadline = time.time() + patience
        while time.time() < deadline:
            time.sleep(5)
            try:
                if not self.phone.wda.is_locked():
                    print("  unlocked, carrying on\n", flush=True)
                    return True
            except WDAError:
                pass
        return False

    # ------------------------------------------------------------------ setup

    def apply(self, steps: list[Step]) -> None:
        """Deterministic phone manipulation, no model in the loop."""
        for step in steps:
            action = step.action
            if action == "home":
                self.phone.home()
            elif action == "open_app":
                self.phone.open_app(step.value)
            elif action == "terminate":
                self.phone.wda.terminate_app(step.value)
            elif action == "open_url":
                self.phone.open_url(step.value)
            elif action == "tap":
                result = self.phone.scroll_to_text(step.value, max_swipes=4)
                snap = self.phone.snapshot(with_screenshot=False, stable=False)
                from ..perception.tree import find_by_text
                hits = find_by_text(snap.elements, step.value)
                if hits:
                    self.phone.tap_element(hits[0])
            elif action == "type":
                self.phone.type_text(step.text or step.value, submit=True)
            elif action == "swipe":
                self.phone.swipe(step.value or "down")
            elif action == "wait":
                time.sleep(step.seconds or 1.0)
            elif action == "clean_alarms":
                self.remove_artifact_alarms()
            else:
                raise ValueError(f"unknown setup action {action!r}")

    ARTIFACT_ALARM = re.compile(r"^\d{1,2}:\d{2}(AM|PM), Alarm$")

    def remove_artifact_alarms(self, limit: int = 8) -> int:
        """Delete alarms this task saved, and only those.

        The alarm tasks say "do not save it" and agents save anyway, so every run
        left one behind. They reached 640, and at that length a single
        accessibility read of the Clock app cost 2.6s, or 24s with the Add Alarm
        sheet open: the benchmark had made the phone slow enough to fail its own
        timer task. A suite that mutates the device has to undo it, or the
        environment decays under measurement.

        An alarm counts as ours only if its label is exactly "H:MM(AM|PM), Alarm"
        with no name and no repeat schedule, and its switch is off. Anything the
        owner named, scheduled or switched on is left alone.
        """
        from ..perception.tree import flatten
        removed = 0
        geo = self.phone.wda.geometry()
        for _ in range(limit):
            try:
                raw = flatten(self.phone.wda.source())
            except WDAError:
                break
            on = {e.label.strip(): e.value in ("1", "true", "True")
                  for e in raw if e.type == "Switch" and e.label}
            rows = [e for e in raw
                    if e.type == "Cell" and e.h > 60 and e.text.strip()
                    and self.ARTIFACT_ALARM.match(e.text.strip())
                    and not on.get(e.text.strip(), True)
                    and 60 <= e.cy <= geo.point_h - 130]
            if not rows:
                break
            # A full-width swipe deletes an alarm outright, no confirm tap. Start
            # at 65% across, NOT 92%: the row's toggle is drawn at about 88%, and
            # a swipe beginning on top of it is taken as a tap on the switch.
            # Measured twice on this device: forty such "deletes" removed nothing
            # and turned nine alarms ON, several of them after midnight. This copy
            # of the gesture was missed when scripts/clean_alarms.py was fixed.
            self.phone.wda.drag(geo.point_w * 0.65, rows[0].cy,
                                geo.point_w * 0.05, rows[0].cy, duration=0.25)
            time.sleep(0.45)
            removed += 1
        return removed

    # ------------------------------------------------------------------ agent

    def run_agent(self, task: Task, model: str, claude: str = "claude",
                  scaffold: bool = False, vision: bool | None = None) -> dict:
        """Hand the instruction to the model under test and let it work.

        A slug with a vendor prefix ("openai/gpt-5.1") goes through OpenRouter, so
        the leaderboard is not limited to the models one CLI happens to support.
        Both paths return the same payload shape and are scored identically.
        """
        if "/" in model:
            from .openrouter import run_task
            can_see = model not in VISIONLESS if vision is None else bool(vision)
            return run_task(self.phone, task.instruction, model,
                            max_steps=task.max_steps, vision=can_see,
                            timeout=task.timeout_seconds)
        cmd = [
            claude, "-p", task.instruction,
            "--mcp-config", str(self.mcp_config),
            "--allowedTools", *AGENT_TOOLS,
            "--model", model,
            "--max-turns", str(task.max_steps),
            "--output-format", "json",
        ]
        if scaffold:
            cmd += ["--append-system-prompt", SCAFFOLD_PROMPT]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=task.timeout_seconds,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            stdin=subprocess.DEVNULL,
        )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"result": "", "error": (proc.stderr or proc.stdout)[-400:]}

    # -------------------------------------------------------------------- run

    # Phrases that mean the phone needs its runner restarted, not that the task failed.
    UNHEALTHY = ("not authorized", "did not confirm its main run loop",
                 "unable to capture screen", "unreachable")

    def healthy(self) -> bool:
        """Cheap probe before each task: a wedged phone fails every task after it
        and the results look like a model regression rather than a broken rig."""
        try:
            # A locked phone answers both of these perfectly: the screenshot is
            # the lock screen and the foreground app is SpringBoard. So checking
            # only that they respond lets the task start, waste its budget, and
            # fail on the check afterwards. Ask about the lock FIRST, so heal()
            # runs before the work rather than after it is thrown away.
            if self.phone.wda.is_locked():
                return False
            self.phone.wda.screenshot()
            self.phone.wda.active_app_info()
            return True
        except Exception as exc:
            return not any(p in str(exc).lower() for p in self.UNHEALTHY)

    def heal(self) -> bool:
        """Put the rig back together without a person, and without a reboot.

        A long run is interrupted by exactly three things, and none of them needs
        a human once the passcode is stored: the phone locks, the runner dies, or
        the forward drops. Healing lives here rather than in a separate watchdog
        process on purpose. Two processes reaching for one phone is what stalled
        the alarm cleaner for twenty minutes tonight, so the repair is serialised
        with the work that needs it.

        What it will NOT do is reboot. A reboot leaves iOS refusing developer
        services until somebody unlocks the phone by hand, which ends an
        unattended run rather than saving it (FINDINGS.md section 24).
        """
        from .. import device as dev
        cfg = self.phone.cfg
        udid = cfg.device.udid or (dev.device_check().data.get("udid") or "")
        if not udid:
            return False

        # 1. A locked phone is the commonest cause and the cheapest to fix.
        try:
            if self.phone.wda.is_locked():
                if self.phone.ensure_unlocked().ok:
                    print("    [heal] phone was locked, unlocked it", flush=True)
                    return True
        except WDAError:
            pass

        # 2. The forward can drop while the runner is perfectly healthy.
        try:
            import socket
            with socket.create_connection((cfg.wda.host, cfg.wda.port), timeout=2):
                pass
        except OSError:
            print("    [heal] port forward is down, restarting it", flush=True)
            try:
                dev.PortForward(udid).start()
                time.sleep(2)
                if self.phone.wda.is_alive():
                    self.phone.wda.invalidate_liveness()
                    return True
            except Exception:
                pass

        print("    [heal] restarting WebDriverAgent", flush=True)
        outcome = dev.recycle_runner(udid, cfg.wda.runner_bundle_id,
                                     cfg.wda.port, cfg.wda.mjpeg_port)
        if outcome.ok:
            self.phone.wda.invalidate_liveness()
            self.phone.ensure_tree_depth()
        # Shared mode yields the phone when it sees a change it did not cause.
        # During a benchmark that is a false positive waiting to happen: one task
        # was paused mid-run and scored as a failure when the agent had actually
        # found the answer. A measured run always takes the phone.
        if self.phone.cfg.session.mode != "takeover":
            self.phone.cfg.session.mode = "takeover"
            try:
                self.phone.coexist.stop()
            except Exception:
                pass
        return outcome.ok

    def ensure_awake(self) -> bool:
        """A locked phone invalidates everything after it, so check before each
        task and try to get back in. Without a stored passcode this can only
        wake the screen, which is enough when auto-lock is off and useless when
        it is not: hence the hard stop rather than a silent stream of failures.
        """
        try:
            if not self.phone.wda.is_locked():
                return True
        except Exception:
            return True
        outcome = self.phone.ensure_unlocked()
        return bool(outcome.ok)

    def run(self, task: Task, model: str = "claude-sonnet-5",
            scaffold: bool = False, vision: bool | None = None) -> TaskResult:
        started = time.time()
        result = TaskResult(task_id=task.id, model=model, passed=False, started_at=started,
                            scaffold=scaffold)
        if not self.ensure_awake():
            result.skipped = True
            result.skip_reason = ("the phone is locked and could not be unlocked, so the task was "
                                  "never attempted")
            raise RigUnavailable(result.skip_reason)
        if not self.healthy():
            if not self.heal():
                result.skipped = True
                result.skip_reason = "the phone was unresponsive and could not be recovered"
                raise RigUnavailable(result.skip_reason)
        try:
            self.apply(task.setup)
        except (WDAError, ValueError) as exc:
            # The model was never asked, so this is not a model failure. Scoring
            # it as one puts a harness fault (an offloaded app's restore dialog, a
            # launch timeout, one typo in a task file) into the denominator and
            # quietly lowers the reported score for reasons no model can affect.
            result.skipped = True
            result.skip_reason = f"setup failed: {exc}"
            result.error = f"setup failed: {exc}"
            return result

        timed_out = False
        try:
            payload = self.run_agent(task, model, scaffold=scaffold, vision=vision)
        except subprocess.TimeoutExpired:
            # A model that runs out of time has FAILED the task. It has not been
            # prevented from attempting it, which is the distinction that decides
            # whether the run continues. Conflating the two let one hard task
            # (a timer picker) abort the 22 tasks queued behind it.
            timed_out = True
            payload = {"result": "", "error": f"exceeded the {task.timeout_seconds:.0f}s budget"}
        # The CLI also gives up when the model exhausts --max-turns, and it exits
        # 0 with {"subtype":"error_max_turns","is_error":true} and usually no
        # result text. That is a model that ran out of budget mid-task, exactly
        # like the wall-clock timeout, and scoring it from whatever the phone
        # happens to look like afterwards credits work it never finished.
        ran_out = bool(payload.get("is_error")) or \
            str(payload.get("subtype") or "").startswith("error_max_turns")
        if ran_out and not timed_out:
            timed_out = True
            result.error = result.error or f"ran out of turns ({payload.get('subtype') or 'is_error'})"
        # Which phone produced this. Two devices with different iOS builds and
        # different app mixes are not the same instrument, and pooling them
        # silently would be the kind of quiet error this file is full of.
        try:
            st = self.phone.wda.status()
            # WDA's own "device" field is the literal string "iphone" on every
            # iPhone ever made, which does not identify an instrument. The
            # hardware model comes from usbmux instead, resolved once per
            # process because it costs a subprocess and never changes mid-run.
            result.device_model = _hardware_model() or str(st.get("device") or "")
            result.device_os = str((st.get("os") or {}).get("version") or "")
            result.device_udid = self.phone.cfg.device.udid or ""
        except Exception:
            pass
        result.turns = int(payload.get("num_turns") or 0)
        result.cost_usd = float(payload.get("total_cost_usd") or 0)
        result.agent_said = str(payload.get("result") or "")[:400]
        if payload.get("error"):
            result.error = str(payload["error"])[:300]

        # Never score a task the model was never asked.
        blob = f"{result.agent_said} {result.error}"
        starved = (result.turns <= 1 and result.cost_usd == 0
                   and not result.agent_said.strip() and not timed_out)
        if looks_rate_limited(blob) or starved:
            result.skipped = True
            result.skip_reason = (result.agent_said or result.error or "no answer from the model")[:200]
            result.seconds = time.time() - started
            raise UsageLimitReached(result.skip_reason)

        try:
            if self.phone.wda.is_locked() and not self.wait_for_unlock():
                result.skipped = True
                result.skip_reason = "the phone locked during the task, so the result is not valid"
                result.seconds = time.time() - started
                raise RigUnavailable(result.skip_reason)
        except RigUnavailable:
            raise
        except Exception:
            pass

        passed, checks = run_all(self.phone, task.checks, said=result.agent_said)
        # A model that ran out of time did not complete the task, whatever the
        # phone happens to look like afterwards. Measured: clock.alarm.set_time
        # timed out with ZERO completed turns and $0.00 spent, and still
        # satisfied both of its checks from whatever was left on screen. Scoring
        # that as a pass credits a model for work it was cut off before doing.
        result.passed = passed and not timed_out
        result.checks = [asdict(c) for c in checks]
        result.seconds = time.time() - started

        try:
            self.apply(task.teardown)
        except Exception:
            pass
        return result

    # ----------------------------------------------------------------- record

    @staticmethod
    def record(result: TaskResult, run_name: str = "latest") -> Path:
        path = RESULTS / f"{run_name}.jsonl"
        with path.open("a") as fh:
            fh.write(json.dumps(asdict(result)) + "\n")
        return path


def score(run_name: str = "latest", include_private: bool = True) -> dict:
    """Aggregate a run into the numbers that go on the leaderboard.

    Skipped tasks are excluded entirely rather than counted as failures, and the
    count of them is reported, so a partial run can never be mistaken for a
    complete one.
    """
    path = RESULTS / f"{run_name}.jsonl"
    if not path.exists():
        return {}
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    # Only score tasks that still exist. A retired task leaves its last result in
    # the file, and counting it scores the model against something no longer in
    # the suite. Also keep only the LATEST attempt per task, so a re-run after a
    # check was corrected supersedes the result the broken check produced.
    from .schema import load_all
    # include_private=False when scoring for PUBLICATION, so the published rate
    # is over the published tasks. Mixing the two put a leaderboard computed over
    # 74 tasks above a footer computed over 60, on the same page.
    live = {t.id for t in load_all(Path(__file__).resolve().parent.parent.parent / "environments",
                                   include_private=include_private)}
    newest: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row["task_id"] not in live:
            continue
        newest[(row["model"], row["task_id"])] = row
    rows = list(newest.values())

    by_model: dict[str, list[dict]] = {}
    skipped: dict[str, int] = {}
    for row in rows:
        if row.get("skipped"):
            skipped[row["model"]] = skipped.get(row["model"], 0) + 1
            continue
        by_model.setdefault(row["model"], []).append(row)
    out = {}
    for model, runs in by_model.items():
        passed = sum(1 for r in runs if r["passed"])
        out[model] = {
            "skipped": skipped.get(model, 0),
            "tasks": len(runs),
            "passed": passed,
            "pass_rate": round(passed / len(runs) * 100, 1),
            "avg_turns": round(sum(r["turns"] for r in runs) / len(runs), 1),
            "avg_seconds": round(sum(r["seconds"] for r in runs) / len(runs), 1),
            "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
        }
    return out
