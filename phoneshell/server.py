"""The local app: a live view of the phone, click-to-control, and a chat that
drives the agent.

The brain is the `claude` CLI in print mode with the phoneshell MCP server
attached, so this runs on the subscription already on this Mac and needs no API
key. Swap BRAIN_CMD for the Anthropic SDK if you would rather pay per token.

One wrinkle worth knowing: WebDriverAgent allows exactly one session, and
creating a new one tears down the old one. The agent's MCP process and this
server therefore take turns; the client recreates its session transparently when
it finds it has been evicted, which costs one extra round trip and nothing else.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path

import httpx
import signal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .actions import Phone
from .config import Config, ROOT, RUNTIME
from .safety import Guard
from .wda.client import WDAError, WDAUnreachable

log = logging.getLogger("phoneshell.server")

UI_DIR = Path(__file__).parent / "ui"
MCP_CONFIG = RUNTIME / "mcp.json"

ALLOWED_TOOLS = [
    "mcp__phoneshell__phone_status",
    "mcp__phoneshell__phone_observe",
    "mcp__phoneshell__phone_tap",
    "mcp__phoneshell__phone_long_press",
    "mcp__phoneshell__phone_type",
    "mcp__phoneshell__phone_swipe",
    "mcp__phoneshell__phone_scroll_to",
    "mcp__phoneshell__phone_open_app",
    "mcp__phoneshell__phone_open_url",
    "mcp__phoneshell__phone_press",
    "mcp__phoneshell__phone_alert",
    "mcp__phoneshell__phone_list_apps",
    "mcp__phoneshell__phone_wait_for",
    "mcp__phoneshell__phone_macro_list",
    "mcp__phoneshell__phone_macro_run",
    "mcp__phoneshell__phone_macro_save",
    "mcp__phoneshell__phone_gesture",
    "mcp__phoneshell__phone_dismiss_popup",
    "mcp__phoneshell__phone_memory",
    "mcp__phoneshell__phone_popup_help",
    "mcp__phoneshell__phone_do",
]

SYSTEM_APPEND = """
You are driving the user's real iPhone through the phoneshell tools.
If a popup, promo sheet or onboarding card is in the way, call phone_dismiss_popup
once rather than hunting for the X yourself; if it cannot clear it, phone_popup_help
lists every popup shape and the moves that close it. For anything beyond tap/type/swipe
(pinch, rotate, long press, row swipe, Control Centre, app switcher, undo) use
phone_gesture. Check phone_macro_list first: a saved macro is a route that already worked, and
replaying one is far more reliable than working the same path out again. When you
finish a task that was worth doing, offer to save it with phone_macro_save.
Prefer phone_do when you already know the route: it runs several steps in one
call and stops where it goes wrong, which is far faster than observe-then-tap
for every single step. Fall back to observe, act, observe again when exploring. Say what you are doing in one
short line before each action, in plain language, as if narrating over someone's
shoulder. Never invent screen contents you have not observed. If a tool refuses
an action because it looks irreversible, stop and ask the user in one sentence.
"""

app = FastAPI(title="phoneshell")
SPEND = {"tasks": 0, "usd": 0.0, "turns": 0}
_cfg = Config.load()
_phone: Phone | None = None
_guard = Guard(_cfg)


def _passcode_stored() -> bool:
    from .secrets import has_secret
    try:
        return has_secret(Config.load().device.udid or "default")
    except Exception:
        return False


def _locked_now() -> bool:
    """Surfaced in the UI because a run against a locked phone costs full price
    and achieves nothing: it is the cheapest credit leak there is."""
    try:
        return bool(phone().wda.is_locked())
    except Exception:
        return False


def phone() -> Phone:
    global _phone
    if _phone is None:
        _phone = Phone(_cfg)
    return _phone


def write_mcp_config() -> Path:
    MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    MCP_CONFIG.write_text(json.dumps({
        "mcpServers": {
            "phoneshell": {
                "command": str(ROOT / ".venv" / "bin" / "python"),
                "args": ["-m", "phoneshell.mcp_server"],
                "cwd": str(ROOT),
            }
        }
    }, indent=1))
    return MCP_CONFIG


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


@app.get("/api/status")
def status() -> dict:
    cfg = Config.load()
    try:
        st = phone().wda.status()
        # Do not refresh geometry on every poll: it costs a session round trip,
        # and WebDriverAgent allows exactly one session, so a 4s poll would keep
        # evicting the agent's session while it works.
        geo = phone().wda.geometry()
        info = {}
        try:
            info = phone().wda.active_app_info()
        except WDAError:
            pass
        return {
            "connected": True,
            "device": st.get("device"),
            "ios": st.get("os", {}).get("version"),
            "wda": st.get("build", {}).get("version"),
            "transport": cfg.wda.transport,
            "point_size": [geo.point_w, geo.point_h],
            "scale": geo.scale,
            "bundle_id": info.get("bundleId"),
            "app": phone().name_for_bundle(str(info.get("bundleId") or "")),
            "mjpeg": "/stream.mjpeg",
            "mode": cfg.session.mode,
            "human_active": phone().coexist.human_has_the_phone(),
            "human_reason": phone().coexist.presence.last_reason,
            "yields": phone().coexist.presence.yields,
            "memory": cfg.memory.enabled,
            "memory_stats": phone().memory.stats(),
            "spend_usd": round(SPEND["usd"], 4),
            "spend_tasks": SPEND["tasks"],
            "locked": _locked_now(),
            "passcode_stored": _passcode_stored(),
        }
    except WDAUnreachable as exc:
        return {"connected": False, "error": str(exc), "hint": "run `phoneshell up` in a terminal"}
    except Exception as exc:
        # A locked phone makes SpringBoard stop answering accessibility queries,
        # which surfaced here as a 500 and took the whole UI down. Status must
        # always answer: it is the thing that TELLS you the phone is locked.
        return {
            "connected": True, "degraded": True, "locked": _locked_now(),
            "error": str(exc)[:200],
            "hint": "the phone is probably locked; unlock it or run `phoneshell set-passcode`",
            "spend_usd": round(SPEND["usd"], 4), "spend_tasks": SPEND["tasks"],
            "mode": Config.load().session.mode, "memory": Config.load().memory.enabled,
            "mjpeg": "/stream.mjpeg",
        }


@app.post("/api/passcode")
async def set_passcode(payload: dict) -> dict:
    """Store the device passcode in the macOS Keychain, from the local app.

    This exists because the terminal prompt cannot be used from every context.
    The value arrives over loopback only, is never logged, never echoed back and
    never written to a file: it goes straight into the login Keychain, and the
    only thing that ever reads it is the unlock path that taps the digits on the
    phone's own keypad.
    """
    from .secrets import delete_secret, set_secret
    cfg = Config.load()
    account = cfg.device.udid or "default"
    if payload.get("forget"):
        return {"ok": delete_secret(account), "stored": False}
    code = str(payload.get("passcode") or "").strip()
    if not code:
        return {"ok": False, "error": "no passcode given"}
    if not code.isdigit():
        return {"ok": False, "error": "only a numeric passcode can be typed on the keypad"}
    if not set_secret(account, code):
        return {"ok": False, "error": "the Keychain refused to store it"}
    result = {"ok": True, "stored": True, "account": account}
    try:
        p = phone()
        if p.wda.is_locked():
            outcome = p.ensure_unlocked()
            result["tested"] = outcome.ok
            result["detail"] = outcome.detail or outcome.error
        else:
            result["detail"] = "stored; it will be used the next time the phone locks"
    except Exception as exc:
        result["detail"] = f"stored, but could not test it now: {exc}"
    return result


@app.post("/api/mode")
async def set_mode(payload: dict) -> dict:
    """takeover: the agent drives. shared: it yields the moment you touch the phone."""
    mode = str(payload.get("mode", "")).lower()
    if mode not in {"takeover", "shared"}:
        return {"ok": False, "error": "mode must be takeover or shared"}
    cfg = Config.load()
    cfg.session.mode = mode
    cfg.save()
    p = phone()
    p.cfg.session.mode = mode
    if mode == "shared":
        p.coexist.start()
    else:
        p.coexist.stop()
    return {"ok": True, "mode": mode}


@app.post("/api/memory")
async def memory_toggle(payload: dict) -> dict:
    """Turn app memory on or off, or clear it."""
    cfg = Config.load()
    if "enabled" in payload:
        cfg.memory.enabled = bool(payload["enabled"])
        cfg.save()
        phone().cfg.memory.enabled = cfg.memory.enabled
        phone().memory.enabled = cfg.memory.enabled
    if payload.get("forget"):
        phone().memory.forget(payload.get("app") or None)
    return {"ok": True, "enabled": cfg.memory.enabled, "stats": phone().memory.stats()}


@app.get("/stream.mjpeg")
async def stream() -> StreamingResponse:
    """Proxy WebDriverAgent's MJPEG stream so the browser can show it."""
    cfg = Config.load()
    url = cfg.mjpeg_url

    async def pump():
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", url) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            except (httpx.HTTPError, asyncio.CancelledError):
                return

    return StreamingResponse(pump(), media_type="multipart/x-mixed-replace; boundary=BoundaryString")


@app.get("/api/observe")
def observe(marks: bool = False) -> dict:
    obs = phone().observe(step=int(time.time()) % 100000, force_som=marks)
    return {
        "app": obs.app,
        "bundle_id": obs.bundle_id,
        "som": obs.som,
        "alert": obs.alert,
        "text": obs.as_text(),
        "elements": [
            {"id": e.idx, "type": e.type, "text": e.text,
             "x": e.x, "y": e.y, "w": e.w, "h": e.h,
             "input": e.is_input, "enabled": e.enabled}
            for e in obs.elements
        ],
        "image": obs.image_b64,
    }


@app.post("/api/tap")
async def tap(payload: dict) -> dict:
    """Tap a point given in the phone's POINT space (the UI converts from pixels)."""
    x, y = float(payload["x"]), float(payload["y"])
    _guard.audit("manual_tap", x=x, y=y)
    phone().tap_point(x, y)
    return {"ok": True}


@app.post("/api/gesture")
async def gesture(payload: dict) -> dict:
    kind = payload.get("kind")
    p = phone()
    if kind == "home":
        p.home()
    elif kind == "back":
        p.back()
    elif kind == "swipe":
        p.swipe(payload.get("direction", "down"), distance=float(payload.get("distance", 0.6)))
    elif kind == "drag":
        p.wda.swipe_w3c(payload["x1"], payload["y1"], payload["x2"], payload["y2"])
    elif kind == "type":
        p.type_text(payload.get("text", ""), submit=bool(payload.get("submit")))
    elif kind == "open_app":
        p.open_app(payload.get("name", ""))
    elif kind == "open_url":
        p.open_url(payload.get("url", ""))
    else:
        return {"ok": False, "error": f"unknown gesture {kind!r}"}
    _guard.audit("manual_gesture", **payload)
    return {"ok": True}


class Run:
    """One agent run, so it can actually be stopped.

    Cancelling the asyncio task is not enough: the brain is a child process, and
    the MCP server is a grandchild. Both have to be signalled, or "stop" leaves a
    process still driving the phone.
    """

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.stopped = False

    async def kill(self) -> None:
        self.stopped = True
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            # Signal the whole process group: claude spawns the MCP server, which
            # holds the WebDriverAgent session.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=4)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()


async def run_agent(task: str, ws: WebSocket, model: str, run: "Run") -> None:
    """Spawn the brain and relay its stream to the browser."""
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(claude).exists():
        await ws.send_json({"type": "error", "text": "the `claude` CLI is not installed on this Mac"})
        return
    cfg_path = write_mcp_config()
    cmd = [
        claude, "-p", task,
        "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(cfg_path),
        "--allowedTools", *ALLOWED_TOOLS,
        "--append-system-prompt", SYSTEM_APPEND,
        "--model", model,
        "--max-turns", "60",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(ROOT),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,   # own process group, so stop can signal all of it
    )
    run.proc = proc
    t0 = time.time()
    await ws.send_json({"type": "started", "task": task, "at": t0})
    assert proc.stdout
    pending: dict[str, float] = {}
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        now = time.time()
        if kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    await ws.send_json({"type": "say", "text": block["text"],
                                        "at": now, "elapsed": now - t0})
                elif block.get("type") == "tool_use":
                    name = str(block.get("name", "")).replace("mcp__phoneshell__phone_", "")
                    pending[str(block.get("id") or name)] = now
                    await ws.send_json({
                        "type": "action", "name": name, "input": block.get("input", {}),
                        "id": str(block.get("id") or name), "at": now, "elapsed": now - t0,
                    })
        elif kind == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    started = pending.pop(str(block.get("tool_use_id") or ""), None)
                    content = block.get("content")
                    text = ""
                    if isinstance(content, list):
                        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                    elif isinstance(content, str):
                        text = content
                    await ws.send_json({
                        "type": "result", "text": text[:400],
                        "id": str(block.get("tool_use_id") or ""),
                        "took": (now - started) if started else None,
                        "at": now, "elapsed": now - t0,
                    })
        elif kind == "result":
            SPEND["tasks"] += 1
            SPEND["usd"] += float(event.get("total_cost_usd") or 0)
            SPEND["turns"] += int(event.get("num_turns") or 0)
            await ws.send_json({
                "type": "done",
                "text": event.get("result", ""),
                "at": now, "elapsed": now - t0,
                "cost": event.get("total_cost_usd"),
                "turns": event.get("num_turns"),
                "duration_ms": event.get("duration_ms"),
                "spend_usd": round(SPEND["usd"], 4),
                "spend_tasks": SPEND["tasks"],
            })
    err = (await proc.stderr.read()).decode()[-600:] if proc.stderr else ""
    await proc.wait()
    if run.stopped:
        await ws.send_json({"type": "stopped", "elapsed": time.time() - t0})
    elif proc.returncode != 0:
        await ws.send_json({"type": "error", "text": err or f"agent exited {proc.returncode}",
                            "elapsed": time.time() - t0})


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    task_handle: asyncio.Task | None = None
    run: Run | None = None
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "task":
                if task_handle and not task_handle.done():
                    await ws.send_json({"type": "error", "text": "a task is already running"})
                    continue
                model = msg.get("model") or Config.load().brain.model
                run = Run()
                task_handle = asyncio.create_task(run_agent(msg["text"], ws, model, run))
            elif msg.get("type") == "stop":
                if run:
                    await run.kill()
                if task_handle and not task_handle.done():
                    task_handle.cancel()
                await ws.send_json({"type": "stopped"})
    except WebSocketDisconnect:
        if run:
            await run.kill()
        if task_handle:
            task_handle.cancel()


if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


def main() -> None:
    import uvicorn
    cfg = Config.load()
    write_mcp_config()
    uvicorn.run(app, host=cfg.server_host, port=cfg.server_port, log_level="warning")


if __name__ == "__main__":
    main()
