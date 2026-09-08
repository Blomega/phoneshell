"""A complete, hand-written WebDriverAgent client.

Every endpoint below was read off the WDA source in vendor/WebDriverAgent
(WebDriverAgentLib/Commands/*.m), not from documentation, so the paths and
argument names are exactly what the runner on the phone accepts.

Design notes
------------
* One long-lived session. WDA kills the previous session whenever a new one is
  created, so we hold on to ours and only recreate it when the phone tells us it
  is gone. Session recreation is transparent to callers.
* Coordinates are POINTS, not pixels. A screenshot of an iPhone 16 Pro is
  1206x2622 pixels but the tap space is 402x874 points. `Geometry` converts.
* Nothing here knows about LLMs. This is the muscle, not the brain.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Literal

import httpx

log = logging.getLogger("phoneshell.wda")

Button = Literal["home", "volumeUp", "volumeDown", "power", "snapshot", "action"]
Direction = Literal["up", "down", "left", "right"]
Locator = Literal[
    "accessibility id", "class chain", "class name", "id", "link text",
    "name", "partial link text", "predicate string", "xpath",
]


class WDAError(RuntimeError):
    def __init__(self, message: str, code: str = "unknown error", payload: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.payload = payload


class WDASessionGone(WDAError):
    """The runner is alive but our session id is stale."""


class WDAUnreachable(WDAError):
    """The runner is not answering at all. Someone has to restart it."""


class ScreenLocked(WDAError):
    """The phone is locked, so this would be a silent no-op.

    WebDriverAgent answers HTTP 200 with a null value for every gesture sent to a
    locked or sleeping device, and nothing happens. Measured: two screenshots
    taken either side of a swipe on a locked phone were byte-identical. A 200 is
    proof the runner accepted the request, not that the phone did anything, so
    the gesture layer refuses instead of reporting success.
    """


class WDANoSuchElement(WDAError):
    pass


# WDA reports these when the session id in the URL is not the live one.
_STALE_SESSION_CODES = {"invalid session id", "no such session"}
# A session created while the WDA runner itself was foreground goes stale the
# moment the runner backgrounds, and reports it as a stale ELEMENT rather than a
# stale session. On a real phone that is the normal state a few seconds after
# launch, so it has to trigger the same recovery.
_STALE_SESSION_TEXT = (
    "session does not exist",
    "session is not open",
    "invalid session",
    "is not present in the current view anymore",
    "did not confirm its main run loop",
    "application is not running",
)
_STALE_ELEMENT_CODES = {"stale element reference", "no such window"}


@dataclass(frozen=True)
class Geometry:
    """Maps between screenshot pixels and WDA tap points."""
    point_w: int
    point_h: int
    scale: float
    status_bar_h: float = 0.0

    @property
    def pixel_w(self) -> int:
        return int(round(self.point_w * self.scale))

    @property
    def pixel_h(self) -> int:
        return int(round(self.point_h * self.scale))

    def px_to_pt(self, x: float, y: float) -> tuple[float, float]:
        return x / self.scale, y / self.scale

    def pt_to_px(self, x: float, y: float) -> tuple[float, float]:
        return x * self.scale, y * self.scale


class WDAClient:
    """Blocking HTTP client for one WebDriverAgent runner."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8100",
        timeout: float = 60.0,
        settings: dict[str, Any] | None = None,
        default_bundle_id: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.settings = settings or {}
        self.default_bundle_id = default_bundle_id
        self._session_id: str | None = None
        self._geometry: Geometry | None = None
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)

    # ------------------------------------------------------------------ plumbing

    def close(self) -> None:
        self._http.close()

    def _url(self, path: str, *, in_session: bool) -> str:
        if in_session:
            return f"{self.base_url}/session/{self.session_id}{path}"
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        in_session: bool = True,
        retries: int = 2,
        _recreated: bool = False,
    ) -> Any:
        url = self._url(path, in_session=in_session)
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._http.request(method, url, json=json_body)
            # TransportError is the base of ConnectError, ReadError, ReadTimeout,
            # RemoteProtocolError and friends. A usbmux forward accepts the TCP
            # connection and then resets it when nothing is listening on the
            # phone yet, which surfaces as ReadError rather than ConnectError, so
            # catching the specific subclasses missed the most common real case.
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise WDAUnreachable(
                    f"no answer from the WebDriverAgent runner at {self.base_url} ({exc})",
                    code="unreachable",
                ) from exc

            try:
                payload = resp.json()
            except json.JSONDecodeError:
                if resp.status_code >= 400:
                    raise WDAError(resp.text[:400], code=f"http {resp.status_code}")
                return resp.content

            value = payload.get("value")
            if isinstance(value, dict) and value.get("error"):
                code = str(value.get("error"))
                message = str(value.get("message", ""))
                stale = (
                    code in _STALE_SESSION_CODES
                    or (code in _STALE_ELEMENT_CODES
                        and any(t in message.lower() for t in _STALE_SESSION_TEXT))
                    or any(t in message.lower() for t in _STALE_SESSION_TEXT)
                )
                if stale:
                    if in_session and not _recreated:
                        log.info("wda session went stale, recreating")
                        self._session_id = None
                        self._geometry = None
                        # Re-attach to the app that is genuinely in front. A
                        # session created with no bundleId makes WDA switch the
                        # app under test, which sends the phone to the home
                        # screen and closes whatever was just opened.
                        try:
                            front = str((self._request(
                                "GET", "/wda/activeAppInfo", in_session=False, retries=0
                            ) or {}).get("bundleId") or "")
                            if front and front != "com.apple.springboard":
                                self.default_bundle_id = front
                        except Exception:
                            pass
                        # Recreate silently. An earlier version pressed home here
                        # to get a live app in front before re-attaching, and that
                        # was a serious mistake: sessions go stale routinely (WDA
                        # answers 404 on activeAppInfo right after an app switch),
                        # so recovery was closing whatever the user or the agent
                        # had just opened. Every app appeared to "bounce back to
                        # the home screen" and the agent looped forever trying to
                        # reopen it. Recovery must never touch the phone.
                        self.create_session()
                        return self._request(
                            method, path, json_body=json_body, in_session=in_session,
                            retries=retries, _recreated=True,
                        )
                    raise WDASessionGone(message, code=code, payload=value)
                if code == "no such element":
                    raise WDANoSuchElement(message, code=code, payload=value)
                raise WDAError(message, code=code, payload=value)
            return value
        raise WDAUnreachable(str(last_exc), code="unreachable")

    def get(self, path: str, *, in_session: bool = True) -> Any:
        return self._request("GET", path, in_session=in_session)

    def post(self, path: str, body: dict | None = None, *, in_session: bool = True) -> Any:
        return self._request("POST", path, json_body=body or {}, in_session=in_session)

    def delete(self, path: str, *, in_session: bool = True) -> Any:
        return self._request("DELETE", path, in_session=in_session)

    # ------------------------------------------------------------------- session

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            self.create_session()
        assert self._session_id
        return self._session_id

    def status(self) -> dict:
        """/status works without a session and is the liveness probe."""
        return self._request("GET", "/status", in_session=False, retries=0)

    def is_alive(self) -> bool:
        try:
            return bool(self.status().get("ready", True))
        except (WDAError, httpx.HTTPError, OSError):
            return False

    def create_session(self, bundle_id: str | None = None, **caps: Any) -> str:
        """Create a session, adopting the phone's current foreground app by default.

        Passing no bundleId means WDA attaches to whatever is on screen instead of
        launching something, which is what we want for a phone the human also uses.
        """
        always: dict[str, Any] = dict(caps)
        bundle_id = bundle_id or self.default_bundle_id
        if bundle_id:
            always["bundleId"] = bundle_id
        always.setdefault("shouldWaitForQuiescence", False)
        always.setdefault("shouldTerminateApp", False)
        always.setdefault("forceAppLaunch", False)
        always.setdefault("useNativeCachingStrategy", True)
        always.setdefault("eventloopIdleDelaySec", 0)
        body = {"capabilities": {"firstMatch": [{}], "alwaysMatch": always}}
        value = self._request("POST", "/session", json_body=body, in_session=False)
        self._session_id = value["sessionId"]
        self._geometry = None
        if self.settings:
            try:
                self.set_settings(self.settings)
            except WDAError as exc:  # a bad setting must not kill the session
                log.warning("could not apply wda settings: %s", exc)
        log.info("wda session %s created", self._session_id)
        return self._session_id

    def delete_session(self) -> None:
        if self._session_id:
            try:
                self.delete("", in_session=True)
            except WDAError:
                pass
            self._session_id = None

    def get_settings(self) -> dict:
        return self.get("/appium/settings")

    def set_settings(self, settings: dict[str, Any]) -> dict:
        return self.post("/appium/settings", {"settings": settings})

    def healthcheck(self) -> Any:
        return self.get("/wda/healthcheck", in_session=False)

    # -------------------------------------------------------------------- screen

    def screen_info(self) -> dict:
        """{'statusBarSize': {...}, 'scale': 3}"""
        return self.get("/wda/screen", in_session=False)

    def window_size(self) -> dict:
        """Logical size in points, orientation aware."""
        return self.get("/window/size", in_session=False)

    def geometry(self, refresh: bool = False) -> Geometry:
        """Screen geometry, with the last known value as a fallback.

        A locked phone stops answering accessibility queries entirely, so asking
        for the window size throws. Geometry does not change while the phone is
        locked, so the cached answer is still correct and a great deal more
        useful than an exception.
        """
        if self._geometry is not None and not refresh:
            return self._geometry
        try:
            size = self.window_size()
            screen = self.screen_info()
        except WDAError:
            if self._geometry is not None:
                return self._geometry
            raise
        if True:
            self._geometry = Geometry(
                point_w=int(size["width"]),
                point_h=int(size["height"]),
                scale=float(screen.get("scale") or 1.0),
                status_bar_h=float((screen.get("statusBarSize") or {}).get("height") or 0.0),
            )
        return self._geometry

    def screenshot(self) -> bytes:
        """Full screen capture. Returns PNG or JPEG bytes depending on screenshotQuality."""
        b64 = self._request("GET", "/screenshot", in_session=False)
        return base64.b64decode(b64)

    def element_screenshot(self, element_id: str) -> bytes:
        return base64.b64decode(self.get(f"/element/{element_id}/screenshot"))

    # Attributes dropped server-side. The first five we never read; `visible` and
    # `accessible` are the expensive ones, because XCTest hit-tests every node to
    # compute them (measured: 5.82s vs 0.89s for the same screen on a real phone).
    UNUSED_ATTRS = "nativeFrame,traits,minValue,maxValue,nativeAccessibilityElement"
    COSTLY_ATTRS = "visible,accessible"
    DEFAULT_EXCLUDED_ATTRS = UNUSED_ATTRS + "," + COSTLY_ATTRS

    def source(
        self,
        fmt: Literal["json", "xml", "description"] = "json",
        excluded_attributes: str | None = DEFAULT_EXCLUDED_ATTRS,
    ) -> Any:
        """The accessibility tree. `json` gives us nested dicts with rects.

        format/scope/excluded_attributes are query parameters, not a body.
        """
        query = f"/source?format={fmt}"
        if excluded_attributes:
            query += f"&excluded_attributes={excluded_attributes}"
        return self._request("GET", query, in_session=False)

    def accessible_source(self) -> Any:
        """Only elements exposed to assistive tech. Smaller, sometimes too small."""
        return self.get("/wda/accessibleSource")

    def orientation(self) -> str:
        return self.get("/orientation")

    def set_orientation(self, value: str) -> None:
        self.post("/orientation", {"orientation": value})

    # ---------------------------------------------------------------------- apps

    def active_app_info(self) -> dict:
        """{'processArguments':..., 'name':..., 'pid':..., 'bundleId':...}"""
        return self.get("/wda/activeAppInfo", in_session=False)

    def active_apps(self) -> list[dict]:
        return self.get("/wda/apps/list")

    def launch_app(self, bundle_id: str, *, wait_for_quiescence: bool = False,
                   arguments: list[str] | None = None, environment: dict | None = None) -> None:
        self.post("/wda/apps/launch", {
            "bundleId": bundle_id,
            "shouldWaitForQuiescence": wait_for_quiescence,
            "arguments": arguments or [],
            "environment": environment or {},
        })

    def launch_unattached(self, bundle_id: str) -> None:
        """Launch an app without making it the session's app under test.

        Session-free, so it cannot disturb session state and does not make WDA
        switch what it considers the application under test.
        """
        self.post("/wda/apps/launchUnattached", {"bundleId": bundle_id}, in_session=False)

    def activate_app(self, bundle_id: str) -> None:
        self.post("/wda/apps/activate", {"bundleId": bundle_id})

    def terminate_app(self, bundle_id: str) -> bool:
        return bool(self.post("/wda/apps/terminate", {"bundleId": bundle_id}))

    def app_state(self, bundle_id: str) -> int:
        """0 unknown, 1 not running, 2 background suspended, 3 background, 4 foreground."""
        return int(self.post("/wda/apps/state", {"bundleId": bundle_id}))

    def open_url(self, url: str, bundle_id: str | None = None, idle_timeout_ms: int | None = None) -> None:
        """Deep link. This is the cheapest, most reliable way into a known screen."""
        body: dict[str, Any] = {"url": url}
        if bundle_id:
            body["bundleId"] = bundle_id
        if idle_timeout_ms:
            body["idleTimeoutMs"] = idle_timeout_ms
        self.post("/url", body)

    def home(self) -> None:
        self.post("/wda/homescreen", in_session=False)

    def deactivate_app(self, seconds: float = 3.0) -> None:
        """Background the current app for N seconds, then come back."""
        self.post("/wda/deactivateApp", {"duration": seconds})

    # ------------------------------------------------------------------- gestures

    def tap(self, x: float, y: float) -> None:
        self.post("/wda/tap", {"x": x, "y": y})

    def double_tap(self, x: float, y: float) -> None:
        self.post("/wda/doubleTap", {"x": x, "y": y})

    def touch_and_hold(self, x: float, y: float, duration: float = 1.0) -> None:
        self.post("/wda/touchAndHold", {"x": x, "y": y, "duration": duration})

    def drag(self, from_x: float, from_y: float, to_x: float, to_y: float, duration: float = 0.2) -> None:
        self.post("/wda/dragfromtoforduration", {
            "fromX": from_x, "fromY": from_y, "toX": to_x, "toY": to_y, "duration": duration,
        })

    def press_and_drag_with_velocity(self, from_x: float, from_y: float, to_x: float, to_y: float,
                                     press_duration: float = 0.5, hold_duration: float = 0.1,
                                     velocity: float = 1000.0) -> None:
        self.post("/wda/pressAndDragWithVelocity", {
            "fromX": from_x, "fromY": from_y, "toX": to_x, "toY": to_y,
            "pressDuration": press_duration, "holdDuration": hold_duration, "velocity": velocity,
        })

    def swipe(self, direction: Direction) -> None:
        """Whole-screen swipe. Coarse: prefer drag() with explicit points."""
        self.post("/wda/swipe", {"direction": direction})

    def scroll(self, direction: Direction, distance: float = 1.0) -> None:
        self.post("/wda/scroll", {"direction": direction, "distance": distance})

    def actions(self, action_chain: list[dict]) -> None:
        """W3C actions, for anything the convenience verbs cannot express.

        pointerType MUST be "touch"; mouse and pen are rejected outright.
        """
        self.post("/actions", {"actions": action_chain})

    def tap_w3c(self, x: float, y: float, hold: float = 0.04) -> None:
        """Tap in VIEWPORT coordinates.

        /wda/tap without an element resolves its origin against the active
        application's frame, and the active application flips to Springboard the
        moment a permission dialog appears, which silently shifts every tap. A
        W3C pointer chain is absolute, so it is the safer default.
        """
        self.actions([{
            "type": "pointer", "id": "finger1",
            "parameters": {"pointerType": "touch"},
            "actions": [
                {"type": "pointerMove", "duration": 0, "x": round(x), "y": round(y)},
                {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": int(hold * 1000)},
                {"type": "pointerUp", "button": 0},
            ],
        }])

    def swipe_w3c(
        self,
        from_x: float, from_y: float, to_x: float, to_y: float,
        settle_ms: int = 250, move_ms: int = 300, lift_ms: int = 300, steps: int = 6,
    ) -> None:
        """A drag expressed as a W3C pointer chain.

        SLOW. Measured on an iPhone 17 Pro Max: any /actions chain that MOVES
        while the pointer is down costs a flat ~19 seconds, whatever the number
        of steps or pauses (1 step 19.2s, 12 steps 19.8s, no pauses 18.9s). A
        tap through the same endpoint is 0.5s, so it is dragging specifically
        that XCTest makes expensive here. /wda/dragfromtoforduration does the
        same thing in 1.4s.

        Kept for paths that genuinely need several waypoints or several fingers,
        where nothing native exists. Everything else should use drag().
        """
        chain: list[dict] = [
            {"type": "pointerMove", "duration": 0, "x": round(from_x), "y": round(from_y)},
            {"type": "pointerDown", "button": 0},
            {"type": "pause", "duration": settle_ms},
        ]
        for i in range(1, steps + 1):
            chain.append({
                "type": "pointerMove",
                "duration": max(int(move_ms / steps), 1),
                "x": round(from_x + (to_x - from_x) * i / steps),
                "y": round(from_y + (to_y - from_y) * i / steps),
            })
        chain.append({"type": "pause", "duration": lift_ms})
        chain.append({"type": "pointerUp", "button": 0})
        self.actions([{
            "type": "pointer", "id": "finger1",
            "parameters": {"pointerType": "touch"}, "actions": chain,
        }])

    def press_button(self, name: Button) -> None:
        self.post("/wda/pressButton", {"name": name})

    def perform_io_hid_event(self, page: int, usage: int, duration: float = 0.005) -> None:
        """Raw HID. page=12 usage=64 is the menu/home button, page=12 usage=0x30 is power."""
        self.post("/wda/performIoHidEvent", {"page": page, "usage": usage, "durationSeconds": duration})

    # ---------------------------------------------------------------------- text

    def type_into_element(self, element_id: str, text: str) -> None:
        """The correct way to fill a field.

        POST /element/:id/value runs fb_prepareForTextInputWithSnapshot, which
        taps the element when nothing in it reports keyboard focus. /wda/keys
        does no focus preparation at all and will happily type into whatever
        happened to be focused a moment ago.
        """
        self.post(f"/element/{element_id}/value", {"value": [text]})

    def type_text(self, text: str, frequency: int | None = None) -> None:
        """Append to whatever already holds keyboard focus. `value` must be an
        array: the handler joins it unconditionally and throws on a string."""
        body: dict[str, Any] = {"value": list(text)}
        if frequency:
            body["frequency"] = frequency
        self.post("/wda/keys", body)

    def dismiss_keyboard(self) -> None:
        self.post("/wda/keyboard/dismiss")

    def set_pasteboard(self, content: str, content_type: str = "plaintext") -> None:
        """Only works while the WDA runner itself is foreground. Kept for completeness."""
        self.post("/wda/setPasteboard", {
            "content": base64.b64encode(content.encode()).decode(), "contentType": content_type,
        })

    def get_pasteboard(self, content_type: str = "plaintext") -> str:
        raw = self.post("/wda/getPasteboard", {"contentType": content_type})
        return base64.b64decode(raw).decode(errors="replace")

    def activate_siri(self, text: str) -> None:
        """Speak a command to Siri without a microphone. A real escape hatch for
        actions that have no UI affordance, e.g. 'turn on wifi'."""
        self.post("/wda/siri/activate", {"text": text})

    # ------------------------------------------------------------------ elements

    def find_element(self, using: Locator, value: str) -> str:
        return self.post("/element", {"using": using, "value": value})["ELEMENT"]

    def find_elements(self, using: Locator, value: str) -> list[str]:
        found = self.post("/elements", {"using": using, "value": value})
        return [f["ELEMENT"] for f in found]

    def find_element_or_none(self, using: Locator, value: str) -> str | None:
        try:
            return self.find_element(using, value)
        except WDANoSuchElement:
            return None

    def click_element(self, element_id: str) -> None:
        self.post(f"/element/{element_id}/click")

    def tap_element(self, element_id: str, x: float | None = None, y: float | None = None) -> None:
        body = {} if x is None else {"x": x, "y": y}
        self.post(f"/wda/element/{element_id}/tap", body)

    def set_element_value(self, element_id: str, text: str) -> None:
        self.post(f"/element/{element_id}/value", {"value": list(text)})

    def clear_element(self, element_id: str) -> None:
        self.post(f"/element/{element_id}/clear")

    def element_rect(self, element_id: str) -> dict:
        return self.get(f"/element/{element_id}/rect")

    def element_text(self, element_id: str) -> str:
        return self.get(f"/element/{element_id}/text")

    def element_attribute(self, element_id: str, name: str) -> Any:
        return self.get(f"/element/{element_id}/attribute/{name}")

    def element_displayed(self, element_id: str) -> bool:
        return bool(self.get(f"/element/{element_id}/displayed"))

    def active_element(self) -> str | None:
        try:
            return self.get("/element/active")["ELEMENT"]
        except WDAError:
            return None

    def scroll_to_element(self, element_id: str) -> None:
        self.post(f"/wda/element/{element_id}/scrollTo")

    def element_swipe(self, element_id: str, direction: Direction) -> None:
        self.post(f"/wda/element/{element_id}/swipe", {"direction": direction})

    def pick_wheel(self, element_id: str, order: Literal["next", "previous"], offset: float = 0.2) -> None:
        self.post(f"/wda/pickerwheel/{element_id}/select", {"order": order, "offset": offset})

    # -------------------------------------------------------------------- alerts

    def alert_text(self) -> str | None:
        try:
            return self.get("/alert/text", in_session=False)
        except WDAError:
            return None

    def alert_buttons(self) -> list[str]:
        try:
            return self.get("/wda/alert/buttons")
        except WDAError:
            return []

    def accept_alert(self, button: str | None = None) -> None:
        self.post("/alert/accept", {"name": button} if button else {}, in_session=False)

    def dismiss_alert(self, button: str | None = None) -> None:
        self.post("/alert/dismiss", {"name": button} if button else {}, in_session=False)

    # -------------------------------------------------------------------- device

    def device_info(self) -> dict:
        return self.get("/wda/device/info")

    def battery(self) -> dict:
        return self.get("/wda/batteryInfo")

    def is_locked(self) -> bool:
        return bool(self.get("/wda/locked", in_session=False))

    # Liveness is checked before every gesture, so it is cached briefly: the call
    # is session-free and ~100ms, which is too much to pay per pointer event.
    _LIVENESS_TTL = 1.5

    def ensure_interactive(self) -> None:
        """Raise rather than let a gesture evaporate against a locked screen."""
        now = time.time()
        cached = getattr(self, "_liveness", None)
        if cached and now - cached[0] < self._LIVENESS_TTL:
            if cached[1]:
                raise ScreenLocked("the phone is locked, so this gesture would do nothing",
                                   code="screen locked")
            return
        try:
            locked = self.is_locked()
        except WDAError:
            return
        self._liveness = (now, locked)
        if locked:
            raise ScreenLocked("the phone is locked, so this gesture would do nothing",
                               code="screen locked")

    def invalidate_liveness(self) -> None:
        self._liveness = None

    def lock(self) -> None:
        self.post("/wda/lock")

    def unlock(self) -> None:
        """Wakes and swipes up. Cannot enter a passcode: iOS will not let any
        automation touch the passcode field. Keep auto-lock off on the phone."""
        self.post("/wda/unlock")

    def start_video(self, fps: int = 10, quality: int = 1) -> None:
        self.post("/wda/video/start", {"fps": fps, "codec": 0, "quality": quality})

    def stop_video(self) -> bytes:
        return base64.b64decode(self.post("/wda/video/stop"))

    # ----------------------------------------------------------------- utilities

    def wait_until(self, predicate, timeout: float = 10.0, interval: float = 0.35) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if predicate():
                    return True
            except WDAError:
                pass
            time.sleep(interval)
        return False
