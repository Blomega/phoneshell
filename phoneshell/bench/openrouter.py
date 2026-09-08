"""Run any tool-calling model against the phone, through OpenRouter.

The rest of the benchmark drives models through `claude -p`, which can only run
Anthropic's. That makes the leaderboard a single-vendor exercise, and a
single-vendor leaderboard is not really a leaderboard. This module is the same
harness with a different brain: it hands a model the phone tools directly, runs
the tool-call loop itself, and returns the payload shape `Runner.run` already
consumes, so nothing downstream has to know which path a result came from.

Two things it deliberately supports that the CLI path cannot:

* **Text-only models.** Qwen and DeepSeek cannot see the screenshot, so they work
  from the accessibility tree alone. That is not a limitation to work around, it
  is the control group: running the same tasks with and without the image
  measures what vision is actually worth on iOS, which nobody has published.
* **Per-model tool budgets.** `max_steps` is enforced here rather than hoped for,
  and running out is reported as `error_max_turns`, exactly as the CLI reports it,
  so a model that ran out of room is never scored from whatever the phone happens
  to look like afterwards.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Callable

import httpx

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = """\
You are operating a real iPhone through the tools below. It is a physical device: \
actions have real consequences and there is no undo.

How to work:
- Call phone_observe first to see the screen. It returns a table of elements with \
an id column, and (if you can see images) a screenshot with matching numbered boxes.
- Act by id. Every action tool returns the resulting screen, so you almost never \
need to call phone_observe again straight after acting.
- If the screen does not change after an action, that action did not work. Try a \
different route rather than repeating it.
- To set a spinning wheel (a time, a date, a duration) use phone_set_picker. Do not \
swipe at a wheel: it steps by whole rows, so a swipe overshoots and never settles.
- When the task is done, call phone_done with a one-sentence summary. Do not stop \
without calling it.

Be efficient. Every call is a round trip.
"""

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "phone_observe",
        "description": "Look at the screen. Returns the elements with their ids, and a screenshot.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "phone_tap",
        "description": "Tap an element by its id from the last observation.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer", "description": "element id"}}, "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "phone_type",
        "description": "Type text. Give the id of the field to focus first.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "id": {"type": "integer", "description": "field to type into"},
            "submit": {"type": "boolean", "description": "press return afterwards"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "phone_swipe",
        "description": "Swipe the screen. 'up' shows content further down the page.",
        "parameters": {"type": "object", "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]}},
            "required": ["direction"]}}},
    {"type": "function", "function": {
        "name": "phone_scroll_to",
        "description": "Scroll until some text is on screen, looking both ways.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "phone_open_app",
        "description": "Open an app by name, e.g. Settings, Safari, Clock.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "phone_open_url",
        "description": ("Open a web address in Safari. Use this whenever a task names a URL: "
                        "opening Safari alone lands on whatever page was last loaded."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "phone_press",
        "description": "Press home, or go back.",
        "parameters": {"type": "object", "properties": {
            "button": {"type": "string", "enum": ["home", "back"]}}, "required": ["button"]}}},
    {"type": "function", "function": {
        "name": "phone_set_picker",
        "description": ("Set a picker wheel (time, date, duration) to a value. wheel=0 is the "
                        "leftmost column. Use this instead of swiping at a wheel."),
        "parameters": {"type": "object", "properties": {
            "value": {"type": "string"},
            "wheel": {"type": "integer", "description": "0 = leftmost column"}},
            "required": ["value"]}}},
    {"type": "function", "function": {
        "name": "phone_gesture",
        "description": ("Perform a named gesture: long_press, double_tap, triple_tap, "
                        "force_touch, control_centre, notification_centre, app_switcher, "
                        "swipe_row_left, swipe_row_right, pull_to_refresh, page_left, "
                        "page_right, pinch_in, pinch_out, flick."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "id": {"type": "integer", "description": "element to act on, if the gesture needs one"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "phone_done",
        "description": "Call when the task is complete, with a one-sentence summary.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}}, "required": ["summary"]}}},
]


def _key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    # Fall back to the key the llm-council MCP server already uses, so this needs
    # no new secret and nothing has to be written to a file in the repo.
    import pathlib
    cfg = pathlib.Path.home() / ".claude.json"
    if cfg.exists():
        try:
            servers = json.loads(cfg.read_text()).get("mcpServers", {})
            found = servers.get("llm-council", {}).get("env", {}).get("OPENROUTER_API_KEY")
            if found:
                return found
        except Exception:
            pass
    raise RuntimeError("no OPENROUTER_API_KEY in the environment")


class PhoneTools:
    """The tool implementations, over a live Phone."""

    def __init__(self, phone, vision: bool = True, max_edge: int = 1536):
        self.phone = phone
        self.vision = vision
        self.max_edge = max_edge
        self.delivered: tuple[int, int] | None = None   # what the model really got
        self.last_image: str | None = None
        self.last_media: str = "image/jpeg"
        self.done: str | None = None

    # -- helpers ----------------------------------------------------------
    def _screen(self, header: str = "") -> str:
        previous = self.phone.cfg.brain.screenshot_max_edge
        self.phone.cfg.brain.screenshot_max_edge = self.max_edge
        try:
            obs = self.phone.observe(step=0, force_som=self.vision, include_image=self.vision)
        finally:
            self.phone.cfg.brain.screenshot_max_edge = previous
        if self.vision and getattr(obs, "image_size", None):
            self.delivered = obs.image_size
        self.last_image = obs.image_b64 if self.vision else None
        # Carry the REAL media type. observe() returns JPEG, and declaring it as
        # png made Anthropic reject the whole request with a bare 400: OpenAI,
        # Gemini and Kimi sniff the bytes and forgive the mismatch, Anthropic
        # validates the declared type against them.
        self.last_media = getattr(obs, "image_media_type", "image/jpeg")
        text = obs.as_text()
        return f"{header}\n{text}" if header else text

    def _element(self, idx: int):
        snap = self.phone.snapshot(with_screenshot=False, stable=False)
        for e in snap.elements:
            if e.idx == idx:
                return e
        return None

    # -- tools ------------------------------------------------------------
    def phone_observe(self) -> str:
        return self._screen()

    def phone_tap(self, id: int) -> str:
        e = self._element(int(id))
        if e is None:
            return f"there is no element {id} on this screen. Observe again."
        self.phone.tap_element(e)
        return self._screen(f"tapped [{id}] {e.text!r}")

    def phone_type(self, text: str, id: int | None = None, submit: bool = False) -> str:
        e = self._element(int(id)) if id is not None else None
        r = self.phone.type_text(str(text), into=e, submit=bool(submit))
        return self._screen(r.detail if not r.ok else f"typed {text!r}")

    def phone_swipe(self, direction: str) -> str:
        self.phone.swipe(str(direction))
        return self._screen(f"swiped {direction}")

    def phone_scroll_to(self, text: str) -> str:
        r = self.phone.scroll_to_text(str(text))
        return self._screen(r.detail if r.ok else f"could not find {text!r} on this screen")

    def phone_open_app(self, name: str) -> str:
        r = self.phone.open_app(str(name))
        if not r.ok:
            return f"could not open {name!r}: {r.error or r.detail}"
        return self._screen(f"opened {name}")

    def phone_open_url(self, url: str) -> str:
        r = self.phone.open_url(str(url))
        if not r.ok:
            return f"could not open {url!r}: {r.error or r.detail}"
        time.sleep(2.5)          # let the page paint before reading it
        return self._screen(f"opened {url}")

    def phone_press(self, button: str) -> str:
        (self.phone.home if button == "home" else self.phone.back)()
        return self._screen(f"pressed {button}")

    def phone_set_picker(self, value: str, wheel: int = 0) -> str:
        r = self.phone.set_picker(str(value), wheel=int(wheel))
        return self._screen(r.detail)

    # A compact dispatch rather than a second copy of the MCP server's 49-way
    # chain: these are the gestures the benchmark's tasks actually need, and a
    # name the model invents is answered with the list instead of a stack trace.
    GESTURES: dict[str, Callable] = {}

    def phone_gesture(self, name: str, id: int | None = None) -> str:
        g = self.phone.gestures
        geo = self.phone.wda.geometry()
        e = self._element(int(id)) if id is not None else None
        x, y = (e.cx, e.cy) if e else (geo.point_w / 2, geo.point_h / 2)
        table: dict[str, Callable[[], Any]] = {
            "long_press": lambda: g.long_press(x, y, 1.0),
            "double_tap": lambda: g.double_tap(x, y),
            "triple_tap": lambda: g.triple_tap(x, y),
            "force_touch": lambda: g.force_touch(x, y, 1.0),
            "control_centre": lambda: g.control_center(),
            "notification_centre": lambda: g.notification_center(),
            "app_switcher": lambda: g.app_switcher(),
            "spotlight": lambda: g.pull_to_refresh(),
            "swipe_row_left": lambda: g.swipe_row(x, y, "left"),
            "swipe_row_right": lambda: g.swipe_row(x, y, "right"),
            "pull_to_refresh": lambda: g.pull_to_refresh(),
            "page_left": lambda: g.page("left"),
            "page_right": lambda: g.page("right"),
            "pinch_in": lambda: g.zoom_out(0.5),
            "pinch_out": lambda: g.zoom_in(2.0),
            "flick": lambda: g.flick("down", 0.7),
        }
        fn = table.get(name)
        if fn is None:
            return f"unknown gesture {name!r}. Known: {', '.join(sorted(table))}"
        try:
            fn()
        except Exception as exc:
            return f"gesture {name!r} failed: {exc}"
        return self._screen(f"gesture {name}")

    def phone_done(self, summary: str) -> str:
        self.done = str(summary)
        return "noted"


def _trim_images(messages: list[dict], keep: int) -> None:
    """Keep only the last `keep` screenshots in the history.

    Every turn re-sends the whole conversation, so an image left in history is
    paid for again on every subsequent turn: a 25-step task with an image per
    step costs quadratically and can silently exceed the context window. Older
    images become a one-line placeholder, which keeps the transcript coherent
    without keeping the pixels.
    """
    seen = 0
    for m in reversed(messages):
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        if not any(part.get("type") == "image_url" for part in m["content"]):
            continue
        seen += 1
        if seen > keep:
            m["content"] = "[earlier screenshot omitted]"


def run_task(phone, instruction: str, model: str, max_steps: int = 25,
             vision: bool = True, timeout: float = 240.0,
             image_max_edge: int = 1536, keep_images: int = 3,
             on_step: Callable[[str], None] | None = None) -> dict:
    """Drive one task with `model` and return a claude-CLI-shaped payload."""
    tools = PhoneTools(phone, vision=vision, max_edge=image_max_edge)
    key = _key()
    started = time.time()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": instruction},
    ]
    turns = 0
    cost = 0.0
    client = httpx.Client(timeout=120.0)
    gen_ids: list[str] = []
    try:
        while turns < max_steps:
            if time.time() - started > timeout:
                return {"result": tools.done or "", "num_turns": turns,
                        "total_cost_usd": cost, "is_error": True,
                        "subtype": "error_timeout",
                        "error": f"exceeded the {timeout:.0f}s budget"}
            body: dict[str, Any] = {"model": model, "messages": messages,
                                    "tools": TOOLS, "tool_choice": "auto"}
            r = client.post(ENDPOINT, headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://blolabel.ai",
                "X-Title": "phoneshell iOS agent benchmark",
            }, json=body)
            if r.status_code != 200:
                return {"result": tools.done or "", "num_turns": turns,
                        "total_cost_usd": cost, "is_error": True,
                        "subtype": "error_api",
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            data = r.json()
            if data.get("id"):
                gen_ids.append(data["id"])
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            assistant = {k: v for k, v in msg.items()
                         if k in ("role", "content", "tool_calls")}
            if assistant.get("content") is None:
                assistant["content"] = ""      # Anthropic rejects a null here
            messages.append(assistant)
            calls = msg.get("tool_calls") or []
            pending_image: str | None = None
            pending_media = "image/jpeg"
            if not calls:
                # No tool call and no phone_done: treat the text as the answer.
                tools.done = tools.done or (msg.get("content") or "")
                turns += 1
                break
            for call in calls:
                turns += 1
                fn = (call.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                impl = getattr(tools, name, None)
                if impl is None:
                    out = f"no such tool {name!r}"
                else:
                    try:
                        out = impl(**args)
                    except Exception as exc:
                        out = f"{name} failed: {exc}"
                if on_step:
                    on_step(f"{name}({json.dumps(args)[:70]})")
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": out})
                pending_image = tools.last_image if name != "phone_done" else None
                pending_media = tools.last_media
                tools.last_image = None
            if tools.vision and pending_image:
                # The screenshot goes in its OWN user message, not inside the tool
                # result. OpenAI, Gemini and Kimi accept an image in a tool result;
                # Anthropic rejects the whole request with a 400, so the portable
                # shape is a separate user turn carrying just the picture.
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "the screen now:"},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{pending_media};base64,{pending_image}"}}]})
                _trim_images(messages, keep_images)
            if tools.done is not None:
                break
    finally:
        client.close()

    payload: dict[str, Any] = {
        "result": tools.done or "",
        "num_turns": turns,
        "total_cost_usd": _cost(gen_ids, key),
        "image_size": list(tools.delivered) if tools.delivered else None,
    }
    if turns >= max_steps and tools.done is None:
        payload |= {"is_error": True, "subtype": "error_max_turns"}
    return payload


def _cost(gen_ids: list[str], key: str) -> float:
    """Ask OpenRouter what the run actually cost, rather than guessing."""
    total = 0.0
    try:
        with httpx.Client(timeout=20.0) as c:
            for gid in gen_ids[-40:]:
                r = c.get("https://openrouter.ai/api/v1/generation",
                          params={"id": gid},
                          headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 200:
                    total += float((r.json().get("data") or {}).get("total_cost") or 0)
    except Exception:
        pass
    return round(total, 6)
