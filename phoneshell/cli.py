"""phoneshell command line.

    phoneshell doctor          what is working and what is not
    phoneshell setup           build, sign, install and start WebDriverAgent on the phone
    phoneshell up              bring the bridge up and keep it up
    phoneshell shell           drive the phone by hand, to see what the agent sees
    phoneshell mcp-config      the block to paste into an MCP client
    phoneshell sim             same thing against a simulator, for development
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import device as dev
from .config import Config, LOGS, ROOT, RUNTIME
from .wda.client import WDAClient, WDAError

app = typer.Typer(add_completion=False, help="Drive your iPhone from this Mac.")
console = Console()
STATE_PATH = RUNTIME / "bridge.json"


def _cfg() -> Config:
    return Config.load()


def _save_state(**fields) -> None:
    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            state = {}
    state.update(fields)
    STATE_PATH.write_text(json.dumps(state, indent=1))


@app.command()
def doctor() -> None:
    """Check every link in the chain and say exactly what to do about each break."""
    cfg = _cfg()
    console.print(Panel.fit("phoneshell doctor", style="bold"))
    checks = [dev.xcode_ok(), dev.device_check(_cfg().device.udid or None)]
    udid = checks[-1].data.get("udid") or cfg.device.udid
    if checks[-1].ok:
        checks.append(dev.developer_mode_check(udid))
    identity = dev.signing_identity(cfg.wda.development_team)
    team = cfg.wda.development_team or dev.detect_team()
    checks.append(dev.Check(
        "signing", identity is not None,
        f"{identity[1]} (team {team})" if identity else "no Apple codesigning identity on this Mac",
        fix="open Xcode > Settings > Accounts and download manual profiles",
    ))
    app_path = dev.wda_paths()["app"]
    checks.append(dev.Check(
        "wda-build", app_path.exists(),
        str(app_path) if app_path.exists() else "not built yet",
        fix="run `phoneshell setup`",
    ))
    if checks[1].ok:
        checks.append(dev.ddi_check(udid))
    checks.extend(dev.health(cfg, udid)[1:])
    checks.append(dev.lan_exposure_check(cfg, udid))
    console.print()
    for c in checks:
        style = "green" if c.ok else "red"
        console.print(f"[{style}]{escape(c.line())}[/{style}]")
    console.print(
        "\n[dim]one thing nothing can check from here: on the phone, "
        "Settings > Developer > Enable UI Automation must be ON. "
        "Without it WebDriverAgent installs and launches but every gesture fails.[/dim]"
    )
    if all(c.ok for c in checks):
        console.print("\n[bold green]everything is up. try `phoneshell shell`.[/bold green]")
    else:
        console.print("\n[yellow]fix the failures above in order, top to bottom.[/yellow]")


@app.command()
def setup(
    udid: str = typer.Option(None, help="target device UDID (default: the connected one)"),
    bundle_id: str = typer.Option(None, help="bundle id for the runner (default: from config)"),
    skip_build: bool = typer.Option(False, help="reuse an existing build"),
) -> None:
    """Build, sign, install and start WebDriverAgent on the phone. Run once."""
    cfg = _cfg()
    # --udid wins: setup is the command you run when adopting a NEW phone, so it
    # must not be blocked by a config that still points at the old one.
    check = dev.device_check(udid or cfg.device.udid or None)
    console.print(escape(check.line()))
    if not check.ok:
        raise typer.Exit(1)
    udid = udid or check.data.get("udid")
    bundle_id = bundle_id or cfg.wda.app_bundle_id
    console.print(f"[bold]target:[/bold] {udid}  [bold]runner:[/bold] {bundle_id}.xctrunner")

    dm = dev.developer_mode_check(udid)
    console.print(dm.line())
    if not dm.ok:
        raise typer.Exit(1)

    src = dev.ensure_wda_source()
    console.print(escape(src.line()))
    if not src.ok:
        raise typer.Exit(1)

    app_path = dev.wda_paths()["app"]
    if not skip_build:
        console.print("[bold]building WebDriverAgent[/bold] (first build takes a few minutes)")
        log_path = LOGS / "wda-build.log"
        # Pass the configured team explicitly. Letting it auto-detect picks
        # whichever identity is first in the keychain, and if that is not the
        # team the installed runner was signed with, iOS refuses the install
        # outright: an app cannot be replaced by a build from a different team.
        result = dev.build_wda(udid, bundle_id, team=cfg.wda.development_team, log_path=log_path)
        console.print(result.line())
        if not result.ok:
            console.print(f"full log: {log_path}")
            raise typer.Exit(1)

    identity = dev.signing_identity(cfg.wda.development_team)
    strip = dev.strip_xctest_frameworks(app_path, identity[0])
    console.print(strip.line())

    install = dev.install_wda(udid, app_path)
    console.print(install.line())
    if not install.ok:
        raise typer.Exit(1)

    runner_bundle = f"{bundle_id}.xctrunner"
    cfg.device.udid = udid
    cfg.wda.app_bundle_id = bundle_id
    cfg.wda.runner_bundle_id = runner_bundle
    cfg.wda.transport = "usb"
    cfg.save()
    console.print(f"[green]saved config[/green] with runner {runner_bundle}")
    console.print("\nnow run: [bold]phoneshell up[/bold]")


@app.command()
def up(
    udid: str = typer.Option(None, help="target device UDID"),
    relaunch: bool = typer.Option(False, help="force WDA to restart even if it answers"),
    launcher: str = typer.Option(
        "auto", help="how to start the runner: auto | devicectl (detached) | dvt | xcodebuild"),
    wifi: bool = typer.Option(False, help="talk to the phone over the LAN instead of the cable"),
) -> None:
    """Bring the bridge up, then supervise it until you stop it."""
    cfg = _cfg()
    check = dev.device_check(udid or cfg.device.udid or None)
    console.print(escape(check.line()))
    if not check.ok and not wifi:
        raise typer.Exit(1)
    udid = udid or check.data.get("udid") or cfg.device.udid

    forward = None
    if not wifi:
        forward = dev.PortForward(udid)
        res = forward.start()
        console.print(res.line())
        if not res.ok:
            raise typer.Exit(1)

    client = WDAClient(base_url=cfg.wda_base_url, timeout=10)
    held: subprocess.Popen | None = None
    if relaunch or not client.is_alive():
        console.print("[bold]starting WebDriverAgent on the phone[/bold]")
        # Detached first: nothing on the Mac has to stay attached, so the bridge
        # survives this terminal closing. If that path is refused, which is what
        # iOS 27 does, fall back to a tethered session rather than giving up.
        if launcher in {"auto", "devicectl"}:
            res = dev.launch_wda(udid, cfg.wda.runner_bundle_id, cfg.wda.port, cfg.wda.mjpeg_port,
                                 bind_ip="127.0.0.1" if cfg.wda.bind_loopback and not wifi else None)
            console.print(escape(res.line()))
            if not res.ok and launcher == "devicectl":
                if forward:
                    forward.stop()
                raise typer.Exit(1)
            if not res.ok:
                console.print("[yellow]falling back to a tethered DVT session[/yellow]")
                held = dev.launch_wda_dvt(udid, cfg.wda.runner_bundle_id, LOGS / "wda-dvt.log",
                                          cfg.wda.port, cfg.wda.mjpeg_port)
        elif launcher == "dvt":
            held = dev.launch_wda_dvt(udid, cfg.wda.runner_bundle_id, LOGS / "wda-dvt.log",
                                      cfg.wda.port, cfg.wda.mjpeg_port)
        elif launcher == "xcodebuild":
            held = dev.launch_wda_attached(udid, LOGS / "wda-attached.log")
        if held is not None:
            console.print(f"tethered runner, pid {held.pid} (WDA stops when this command stops)")

    for _ in range(40):
        if client.is_alive():
            break
        time.sleep(1)
    if not client.is_alive() and held is None and launcher == "auto":
        # The detached launch can report success and still never bind: devicectl
        # exits 0 whether or not the runner survived its own bootstrap. Waiting
        # longer does not help and giving up here is what left the bridge down
        # for twenty minutes, so take the tethered route rather than the exit.
        console.print("[yellow]detached runner never answered; falling back to a "
                      "tethered DVT session[/yellow]")
        held = dev.launch_wda_dvt(udid, cfg.wda.runner_bundle_id, LOGS / "wda-dvt.log",
                                  cfg.wda.port, cfg.wda.mjpeg_port)
        console.print(f"tethered runner, pid {held.pid} (WDA stops when this command stops)")
        for _ in range(45):
            if client.is_alive():
                break
            time.sleep(1)
    if not client.is_alive():
        console.print("[red]WebDriverAgent did not come up[/red]")
        installed = dev.runner_installed_check(udid, cfg.wda.runner_bundle_id)
        if not installed.ok:
            console.print(escape(installed.line()))
        if forward:
            forward.stop()
        raise typer.Exit(1)

    status = client.status()
    console.print(Panel.fit(
        f"connected to {status.get('device')} on iOS {status.get('os', {}).get('version')}\n"
        f"WebDriverAgent {status.get('build', {}).get('version')} at {cfg.wda_base_url}\n"
        f"live screen: {cfg.mjpeg_url}",
        title="bridge up", style="green",
    ))
    _save_state(udid=udid, base_url=cfg.wda_base_url, started=time.time(), transport=cfg.wda.transport)

    console.print("supervising, ctrl-c to stop")
    misses = 0
    try:
        while True:
            time.sleep(5)
            if client.is_alive():
                misses = 0
                continue
            misses += 1
            console.print(f"[yellow]WDA stopped answering ({misses})[/yellow]")
            if misses >= 2:
                console.print("[bold]relaunching the runner[/bold]")
                if held is not None and held.poll() is not None:
                    held = dev.launch_wda_dvt(udid, cfg.wda.runner_bundle_id, LOGS / "wda-dvt.log",
                                              cfg.wda.port, cfg.wda.mjpeg_port)
                else:
                    res = dev.launch_wda(udid, cfg.wda.runner_bundle_id, cfg.wda.port, cfg.wda.mjpeg_port,
                                         bind_ip="127.0.0.1" if cfg.wda.bind_loopback and not wifi else None)
                    console.print(escape(res.line()))
                misses = 0
    except KeyboardInterrupt:
        console.print("\nstopping")
    finally:
        if forward:
            forward.stop()
        if held is not None and held.poll() is None:
            held.terminate()


@app.command()
def sim(
    name: str = typer.Option("iPhone 17 Pro", help="simulator name"),
    rebuild: bool = typer.Option(False, help="rebuild WDA for the simulator"),
) -> None:
    """Run the whole stack against a simulator. Same code path, no phone needed."""
    env = dev._env()
    out = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "-j"],
                         capture_output=True, text=True, env=env, timeout=90)
    data = json.loads(out.stdout)
    target = None
    for runtime, devices in data.get("devices", {}).items():
        for d in devices:
            if d.get("name") == name:
                target = (d["udid"], runtime, d.get("state"))
    if target is None:
        console.print(f"[red]no simulator called {name!r}[/red]")
        raise typer.Exit(1)
    udid, runtime, state = target
    console.print(f"simulator {name} {udid} ({runtime}) state={state}")
    if state != "Booted":
        subprocess.run(["xcrun", "simctl", "boot", udid], env=env, timeout=180)
        subprocess.run(["open", "-a", "Simulator"], env=env, timeout=60)
        time.sleep(6)
    if rebuild or not dev.wda_paths(for_device=False)["app"].exists():
        console.print("[bold]building WDA for the simulator[/bold]")
        res = dev.run([
            "xcodebuild", "build-for-testing",
            "-project", str(ROOT / "vendor" / "WebDriverAgent" / "WebDriverAgent.xcodeproj"),
            "-scheme", "WebDriverAgentRunner",
            "-destination", f"id={udid}",
            "-derivedDataPath", str(dev.SIM_DD),
        ], timeout=1800)
        if "BUILD SUCCEEDED" not in res.stdout:
            console.print("[red]build failed[/red]")
            raise typer.Exit(1)
    log = LOGS / "wda-sim.log"
    console.print(f"launching the runner, log at {log}")
    proc = subprocess.Popen(
        ["xcodebuild", "test-without-building",
         "-project", str(ROOT / "vendor" / "WebDriverAgent" / "WebDriverAgent.xcodeproj"),
         "-scheme", "WebDriverAgentRunner", "-destination", f"id={udid}",
         "-derivedDataPath", str(dev.SIM_DD)],
        stdout=log.open("w"), stderr=subprocess.STDOUT, env=env,
    )
    cfg = _cfg()
    cfg.device.udid = udid
    cfg.save()
    client = WDAClient(base_url=cfg.wda_base_url, timeout=10)
    for _ in range(60):
        if client.is_alive():
            break
        time.sleep(1)
    if not client.is_alive():
        console.print("[red]WDA did not start; see the log[/red]")
        raise typer.Exit(1)
    _save_state(udid=udid, base_url=cfg.wda_base_url, started=time.time(), transport="simulator")
    console.print(Panel.fit(f"simulator bridge up at {cfg.wda_base_url}", style="green"))
    console.print("ctrl-c to stop")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


@app.command()
def shell() -> None:
    """Drive the phone by hand, seeing exactly what the agent would see."""
    from .actions import Phone
    cfg = _cfg()
    phone = Phone(cfg)
    console.print(Panel.fit(
        "commands: observe | tap N | type TEXT | into N TEXT | swipe up|down|left|right | "
        "open APP | url URL | home | back | alert accept|dismiss | apps | quit",
        title="phoneshell", style="bold",
    ))
    obs = None
    while True:
        try:
            raw = console.input("[bold cyan]phone>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not raw:
            continue
        cmd, _, rest = raw.partition(" ")
        try:
            if cmd in {"quit", "exit", "q"}:
                break
            if cmd in {"observe", "o", ""}:
                obs = phone.observe(step=(obs.step + 1) if obs else 1)
                console.print(obs.as_text())
            elif cmd == "tap":
                el = obs.elements[int(rest) - 1] if obs else None
                console.print(phone.tap_element(el))
                obs = phone.observe(step=obs.step + 1)
                console.print(obs.as_text())
            elif cmd == "type":
                console.print(phone.type_text(rest))
            elif cmd == "into":
                idx, _, text = rest.partition(" ")
                el = obs.elements[int(idx) - 1]
                console.print(phone.type_text(text, into=el, submit=True))
                obs = phone.observe(step=obs.step + 1)
                console.print(obs.as_text())
            elif cmd == "swipe":
                console.print(phone.swipe(rest or "down"))
                obs = phone.observe(step=(obs.step + 1) if obs else 1)
                console.print(obs.as_text())
            elif cmd == "open":
                console.print(phone.open_app(rest))
                obs = phone.observe(step=(obs.step + 1) if obs else 1)
                console.print(obs.as_text())
            elif cmd == "url":
                console.print(phone.open_url(rest))
            elif cmd == "home":
                console.print(phone.home())
            elif cmd == "back":
                console.print(phone.back())
            elif cmd == "alert":
                console.print(phone.accept_alert() if rest.startswith("a") else phone.dismiss_alert())
            elif cmd == "apps":
                catalog = phone.load_catalog()
                table = Table("app", "bundle id")
                for n, b in sorted(catalog.items())[:60]:
                    table.add_row(n, b)
                console.print(table)
            else:
                console.print(f"unknown command {cmd!r}")
        except Exception as exc:  # a REPL should never die on a typo
            console.print(f"[red]{type(exc).__name__}: {exc}[/red]")


@app.command("set-passcode")
def set_passcode(forget: bool = typer.Option(False, "--forget", help="remove the stored passcode")) -> None:
    """Store the phone's passcode in the macOS Keychain so the bridge can unlock it.

    Typed at a hidden prompt, so it never reaches your shell history or a
    transcript. It is sent only to this phone, as taps on its own keypad.
    """
    import getpass
    from .secrets import delete_secret, get_secret, set_secret
    from .actions import Phone

    cfg = _cfg()
    account = cfg.device.udid or dev.device_check().data.get("udid") or "default"
    if forget:
        console.print("removed" if delete_secret(account) else "nothing stored for this device")
        return

    console.print(Panel.fit(
        "This stores your iPhone passcode in the macOS login Keychain, encrypted at rest\n"
        "and readable only by your own macOS login. It is never written to the repo, to\n"
        "runtime/config.yaml, or to any log. phoneshell uses it for exactly one thing:\n"
        "tapping the digits on the phone's own keypad when the phone has auto-locked.\n\n"
        "Remove it any time with: phoneshell set-passcode --forget",
        title="what this does", style="yellow",
    ))
    if not typer.confirm("store the passcode for this iPhone?", default=False):
        console.print("nothing stored")
        return
    code = getpass.getpass("iPhone passcode (hidden): ").strip()
    again = getpass.getpass("again: ").strip()
    if not code or code != again:
        console.print("[red]they did not match, nothing stored[/red]")
        return
    if not code.isdigit():
        console.print("[yellow]note: only numeric passcodes can be typed on the keypad[/yellow]")
    if not set_secret(account, code):
        console.print("[red]the Keychain refused to store it[/red]")
        raise typer.Exit(1)
    console.print(f"[green]stored in the Keychain[/green] (service 'phoneshell', account {account})")

    phone = Phone(cfg)
    try:
        if phone.wda.is_locked():
            console.print("phone is locked, trying it now...")
            result = phone.ensure_unlocked()
            console.print(escape(result.detail or result.error))
        else:
            console.print("phone is already unlocked; it will be used next time it locks")
    except Exception as exc:
        console.print(f"[yellow]could not test it right now: {exc}[/yellow]")


@app.command("install-agent")
def install_agent(remove: bool = typer.Option(False, help="remove it again")) -> None:
    """Keep the bridge running across logins, via a launchd agent."""
    label = "ai.blolabel.phoneshell"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if remove:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        plist_path.unlink(missing_ok=True)
        console.print("removed")
        return
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{ROOT / 'bin' / 'phoneshell'}</string>
    <string>up</string>
  </array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{LOGS / 'launchd.out.log'}</string>
  <key>StandardErrorPath</key><string>{LOGS / 'launchd.err.log'}</string>
  <key>EnvironmentVariables</key>
  <dict><key>DEVELOPER_DIR</key><string>{dev.DEVELOPER_DIR}</string></dict>
</dict></plist>
"""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    res = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        console.print(f"[yellow]launchctl said: {res.stderr.strip()}[/yellow]")
    console.print(f"installed {plist_path}\nit will retry every 30s, which is what you want when the "
                  "cable is not plugged in yet")


@app.command()
def bench(
    task: str = typer.Option(None, help="run one task by id (default: all)"),
    only: str = typer.Option(None, help="run just these task ids, comma separated"),
    model: str = typer.Option("claude-sonnet-5", help="the model under test"),
    run_name: str = typer.Option("latest", help="name for this results file"),
    list_only: bool = typer.Option(False, "--list", help="just list the tasks"),
    resume: bool = typer.Option(True, help="skip tasks that already passed in this run file"),
    limit: int = typer.Option(0, help="run at most N tasks, to spend quota in controlled batches"),
    prepare: bool = typer.Option(True, help="set Auto-Lock to Never before running"),
    scaffold: bool = typer.Option(False, help="give the model harness guidance on using the tools"),
    no_vision: bool = typer.Option(False, "--no-vision",
        help="withhold the screenshot and run on the accessibility tree alone"),
) -> None:
    """Run the iOS agent benchmark against a model, on the real phone."""
    from pathlib import Path as _Path
    from .actions import Phone
    from .bench.runner import RigUnavailable, Runner, UsageLimitReached, score
    from .bench.runner import TaskResult
    from .bench.schema import load_all

    tasks = load_all(ROOT / "environments")
    if list_only:
        table = Table("id", "difficulty", "checks", "instruction")
        for t in tasks:
            table.add_row(t.id, t.difficulty, str(len(t.checks)), t.instruction[:58])
        console.print(table)
        return
    if task:
        tasks = [t for t in tasks if t.id == task]
        if not tasks:
            console.print(f"[red]no task with id {task!r}[/red]")
            raise typer.Exit(1)
    if resume:
        import json as _json
        from .bench.runner import RESULTS
        done_path = RESULTS / f"{run_name}.jsonl"
        already = set()
        if done_path.exists():
            for line in done_path.read_text().splitlines():
                if not line.strip():
                    continue
                row = _json.loads(line)
                if row.get("passed") and row.get("model") == model:
                    already.add(row["task_id"])
        if already:
            tasks = [t for t in tasks if t.id not in already]
            console.print(f"[dim]resuming: {len(already)} already passed, {len(tasks)} to run[/dim]")

    if only:
        wanted = {t.strip() for t in only.split(",") if t.strip()}
        missing = wanted - {t.id for t in tasks}
        if missing:
            console.print(f"[red]no such task(s): {', '.join(sorted(missing))}[/red]")
            raise typer.Exit(1)
        tasks = [t for t in tasks if t.id in wanted]

    if limit:
        tasks = tasks[:limit]
    phone = Phone(_cfg())
    if phone.wda.is_locked():
        console.print("[red]the phone is locked; unlock it or run `phoneshell set-passcode`[/red]")
        raise typer.Exit(1)
    runner = Runner(phone)
    if prepare:
        from .prep import prepare_for_run
        console.print("[dim]preparing the device for an unattended run[/dim]")
        blocked = False
        for r in prepare_for_run(phone):
            mark = "[green]ok[/green]" if r.ok else "[yellow]!![/yellow]"
            console.print(f"  {mark} {escape(r.detail)}")
            blocked = blocked or r.fatal
        if blocked:
            # A run against a phone that drops gestures produces a page of
            # numbers that measure the phone's wedge, not the model.
            console.print("[red]refusing to run: the phone cannot be driven right now[/red]")
            raise typer.Exit(1)
    # A task whose app is not on the phone cannot be passed by any agent, and
    # scoring it as a failure blames the model for the device. iOS offloads apps
    # on its own, so this is not hypothetical: it removed Weather part-way
    # through a run and two tasks went from passing to impossible.
    wanted = {t.app for t in tasks if t.app}
    if wanted:
        target = _cfg().device.udid or dev.device_check().data.get("udid") or ""
        present = dev.installed_bundle_ids(target) if target else None
        if present:
            missing = {b for b in wanted if b not in present}
            if missing:
                blocked = [t for t in tasks if t.app in missing]
                tasks = [t for t in tasks if t.app not in missing]
                for t in blocked:
                    Runner.record(TaskResult(
                        task_id=t.id, model=model, passed=False, skipped=True,
                        skip_reason=f"{t.app} is not installed on this phone"), run_name)
                console.print(f"[yellow]not scored: {len(blocked)} task(s) need apps this "
                              f"phone does not have ({', '.join(sorted(missing))})[/yellow]")

    console.print(f"[bold]running {len(tasks)} task(s) against {model}"
                  f"{' with scaffold guidance' if scaffold else ' with tools only'}[/bold]\n")
    stopped_early = ""
    for t in tasks:
        console.print(f"  [cyan]{t.id}[/cyan] {t.instruction[:60]}")
        try:
            # None means "decide per model": a flag defaulting to True overrode the
            # VISIONLESS list and sent an image to models that cannot take one, and
            # OpenRouter answered "no endpoints found that support image input" for
            # every task. Only an explicit --no-vision forces it.
            result = runner.run(t, model=model, scaffold=scaffold,
                                vision=False if no_vision else None)
        except (UsageLimitReached, RigUnavailable) as exc:
            stopped_early = str(exc)
            Runner.record(TaskResult(task_id=t.id, model=model, passed=False, skipped=True,
                                     skip_reason=str(exc)), run_name)
            console.print(f"    [yellow]STOPPED: {escape(stopped_early)}[/yellow]")
            console.print("    [yellow]remaining tasks were NOT run and are NOT scored[/yellow]")
            break
        Runner.record(result, run_name)
        mark = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        console.print(f"    {mark}  {result.turns} turns  {result.seconds:.0f}s  ${result.cost_usd:.3f}")
        for c in result.checks:
            tick = "ok" if c["passed"] else "no"
            console.print(f"      [{tick}] {escape(c['detail'])[:88]}")
        if result.error:
            console.print(f"      [yellow]{escape(result.error)[:110]}[/yellow]")
    console.print()
    if stopped_early:
        console.print(f"[yellow]run incomplete: {escape(stopped_early)}[/yellow]")
    for model_name, row in score(run_name).items():
        note = f", {row['skipped']} not run" if row.get("skipped") else ""
        console.print(f"[bold]{model_name}[/bold]: {row['pass_rate']}% "
                      f"({row['passed']}/{row['tasks']} attempted{note})  avg {row['avg_turns']} turns, "
                      f"{row['avg_seconds']}s, ${row['total_cost_usd']} total")


@app.command("mcp-config")
def mcp_config(claude_code: bool = typer.Option(True, help="print the `claude mcp add` command too")) -> None:
    """Print the MCP server config to paste into a client."""
    python = ROOT / ".venv" / "bin" / "python"
    block = {
        "mcpServers": {
            "phoneshell": {
                "command": str(python),
                "args": ["-m", "phoneshell.mcp_server"],
                "cwd": str(ROOT),
            }
        }
    }
    console.print(Panel.fit(json.dumps(block, indent=2), title="claude_desktop_config.json"))
    if claude_code:
        console.print("\nfor Claude Code, run:\n")
        console.print(f"  claude mcp add phoneshell -- {python} -m phoneshell.mcp_server\n")


@app.command()
def serve(port: int = typer.Option(8765, help='port for the local app')) -> None:
    """Open the phoneshell app: live phone screen, click to control, chat to delegate."""
    import uvicorn
    from .server import app as api, write_mcp_config
    write_mcp_config()
    console.print(Panel.fit(f'phoneshell app on http://127.0.0.1:{port}', style='green'))
    subprocess.Popen(['open', f'http://127.0.0.1:{port}'])
    uvicorn.run(api, host='127.0.0.1', port=port, log_level='warning')


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (this is what a client launches)."""
    from .mcp_server import main
    main()


def entrypoint() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    app()


if __name__ == "__main__":
    entrypoint()
