# phoneshell

Drive your real iPhone from this Mac, with an agent, over the cable or the same wifi.

Not a mirror and not a screen-scraper: it speaks the same automation protocol Apple's
own UI tests use, so it reads the actual accessibility tree of whatever app is open,
taps real controls, types real text, and can launch any installed app by bundle id or
deep link. Ordering from Grab or replying on WhatsApp is the same code path as opening
Settings, because it operates the phone the way a finger does, with your accounts
already logged in.

## The held-out split

The suite in `environments/` is **60 of 76 tasks**. Sixteen are kept private and are not in this
repository or its history.

This is not coyness. A benchmark whose entire answer key is public becomes training data, and once
that happens the score measures memorisation rather than capability. Holding a split back is the
only way a number stays meaningful after the benchmark gets any attention, so every serious
benchmark does it.

The public 60 cover **all 22 capabilities**, so a score over them is directly comparable between
models and you can run the whole thing yourself today. The held-out 16 are one task from each
capability that had more than one, plus five end-to-end jobs, so the private half is representative
rather than a pile of leftovers.

If you want a scored run against the full 76 on real hardware, that is what we do.


## Status

Running against a physical iPhone 17 Pro Max on iOS 26.6: real apps, real accounts, real
gestures. It drives Settings, Safari, Notes, Clock, Calculator and third-party apps like Grab,
and it ships a 32-task benchmark with automatic verification that scores a model on real
hardware. The simulator path still exists and is the fastest way to develop without a phone.

## The one-time setup, with the phone

1. On the phone: **Settings > Privacy & Security > Developer Mode > on**, let it reboot.
   Then **Settings > Developer > Enable UI Automation > on**. Without that second toggle
   WebDriverAgent installs and launches perfectly and every single gesture fails, which
   is a miserable thing to debug.
2. On the phone: **Settings > Display & Brightness > Auto-Lock > Never** while it is
   docked. Automation cannot type a passcode, by design, so the phone has to be awake.
3. On the phone: **Settings > General > Software Update > Automatic Updates > off**.
   iOS 27 ships around 14 September 2026 and breaks the detached launch path this uses.
4. Plug the phone in with a cable, unlock it, tap **Trust**.
5. On the Mac:

```bash
cd ~/Desktop/projects/pro-phoneshell
./.venv/bin/python -m phoneshell.cli doctor    # says exactly what is missing
./.venv/bin/python -m phoneshell.cli setup     # builds, signs, installs WebDriverAgent
./.venv/bin/python -m phoneshell.cli up        # brings the bridge up and supervises it
```

`setup` signs with a codesigning identity from your own keychain: it detects one automatically,
or you can pin a specific team with `wda.development_team` in `runtime/config.yaml`. A paid
Apple Developer account gives a profile good for a year; a free account expires every 7 days.

Then, in a second terminal, either:

```bash
./.venv/bin/python -m phoneshell.cli serve     # the app: live screen, click to control, chat
./.venv/bin/python -m phoneshell.cli shell     # drive it by hand, see what the agent sees
```

or point any MCP client at it:

```bash
claude mcp add phoneshell -- ~/Desktop/projects/pro-phoneshell/.venv/bin/python -m phoneshell.mcp_server
```

To develop without touching the phone, `python -m phoneshell.cli sim` runs the identical
stack against an iOS 26.5 simulator.

## How it works

```
  you ──▶ app (chat + live screen)         MCP client (Claude Code, Claude Desktop)
             │                                        │
             └──────────────┬─────────────────────────┘
                            ▼
                   phoneshell MCP tools
            observe · tap · type · swipe · open_app · open_url · alert
                            │
                     perception + safety
        accessibility tree ─▶ filter ─▶ dedupe ─▶ TSV + numbered screenshot
                            │
                      WebDriverAgent HTTP  :8100     MJPEG live screen :9100
                            │
                usbmux port forward over the cable  (or the phone's LAN address)
                            │
                    WebDriverAgent (XCUITest runner) on the iPhone
```

**Control plane.** WebDriverAgent, the XCTest runner Appium uses, built and signed here
and installed on the phone. It is the only supported, non-jailbreak way to get a real
accessibility tree plus real touch injection on a physical iPhone. It is launched with
`xcrun devicectl device process launch`, which is the one launcher that detaches: every
other option (xcodebuild, go-ios runwda, pymobiledevice3's test service) holds the
testmanagerd session open and takes WDA down with it when it exits.

**Perception.** Tree first, pixels second. A raw `/source` for one screen is ~200 nodes
and ~78 KB of JSON; the filter takes that to 12-16 real controls, one line each, in
reading order. Numbered boxes are drawn on the screenshot only when the tree is too thin
to trust, which is the WebView, map and game case. That split is not taste: measured,
hybrid beats tree-only 53.7% to 35.2% and pixels-only 15.6%, but on rich native trees the
tree wins and on thin ones the marks win.

**Grounding.** The model never emits coordinates. It names an id from the observation it
was given, and this side resolves that id against the same snapshot, verifies it, and
only then sends an absolute W3C touch event.

The verification is two checks. Structural: is the rect degenerate, off screen, or a flat
block of colour, all of which mean covered or not yet painted. Textual: macOS Vision
reads the pixels under the element and they have to agree with the label the model was
shown. That second check exists because when a stale tree label and the pixels disagree,
models follow the text 30-79% of the time and act wrongly on it almost always, and it is
the difference between catching "Place order" pointing at a Cancel button and not.

It is deliberately narrow. Only controls whose label is the text drawn on them get the
text check: Buttons, links, labels, tabs. Icons, images, text fields, webviews and
containers all carry labels describing something other than their own pixels, and
checking those raised a false alarm on 39% of elements when measured across three real
screens. Scoped to the controls where the label really is the rendering, it is 0% false
alarms over 46 elements and still blocks a relabelled button.

**Settling.** Every observation waits for the framebuffer to stop changing first. A home
screen read 1.0s after pressing home returned 6 elements here; the same read at 2.0s
returned 12. Reading mid-animation is the single easiest way to make an agent look stupid.

## Two modes

`takeover` (default) is the agent driving the phone. `shared` makes the agent yield: it brackets
its own actions, reads any screen change it did not cause as you picking the phone up, pauses, and
resumes when you put it down. Switch it live in the app header.

Shared mode is as close to "runs in the background" as the hardware allows, and that is not a
limitation of this code. Checked against the iOS 26.4 SDK headers on this Mac: `UIWindowScene.h`
declares exactly one external-display role, `UIWindowSceneSessionRoleExternalDisplayNonInteractive`,
and the interactive one was deprecated in iOS 16. The phone enumerates seven display slots (one
primary, one TVOut, five wireless) but everything past the built-in screen mirrors and cannot be
touched, and the CoreDevice HID path posts to the same `mainTouchscreen` digitizer your finger uses.
There is no offscreen scene either: iPhone reports `supportsMultipleScenes == NO`, and
`launchUnattached` is `LSApplicationWorkspace openApplicationWithBundleID:`, which foregrounds.
For genuine parallelism, use a second phone.

## Gestures

Forty-nine of them, thirty-one exposed to the agent through one tool: taps up to triple, long press,
force touch, two/three/four/five-finger chains, pinch, rotate, flick with momentum, page flips, edge
and corner swipes (Control Centre, Notification Centre, app switcher, Spotlight), row swipes,
drag-to-reorder, keyboard-as-trackpad, three-finger copy and paste, pull-to-refresh, and the
hardware buttons over HID.

Three things only the device taught us: top-edge gestures are issued from `y=0`; page flips need
momentum, because a drag that ends stationary snaps back; and `/wda/homescreen` silently fails often
enough that `home()` verifies itself and falls back to a HID press and then the bottom-edge swipe.
Note also that Control Centre, Notification Centre, Spotlight and the app switcher are all
`com.apple.springboard`, so "am I on the home screen" has to look at the screen, not the bundle id.

**Every gesture is gated on liveness.** WebDriverAgent answers `200` with a null value for gestures
sent to a locked or sleeping phone and does nothing at all: two screenshots either side of such a
swipe are byte-identical. A 200 means the runner accepted it, never that the phone moved, so the
gesture layer raises `ScreenLocked` instead of reporting a success that did not happen.

## Popups

Promo sheets are the most common way an agent stalls, so they are a harness reflex rather than
something the model reasons about. `phone_dismiss_popup` (and an automatic pass after opening an
app) tries the real close control, then the known wording, then swiping the sheet down, then the
backdrop, then a back swipe, verifying after each. It never answers a system permission dialog and
never taps anything matching pay, order, send, delete or allow.

## The passcode

A phone that auto-locks stops an unattended rig dead, because iOS deliberately blocks automation
from the passcode screen. `phoneshell set-passcode` stores the code in the macOS login Keychain,
encrypted at rest, typed at a hidden prompt so it never reaches your shell history or a transcript.
It is used for one thing: tapping digits on that phone's own keypad. `--forget` removes it. The
alternative, and the better one for a docked phone, is Auto-Lock set to Never.

## What it cannot do

Honest list, all verified rather than assumed:

- **Send a WhatsApp message without a tap.** `whatsapp://send?phone=…&text=…` fills the
  composer and stops. No URL, entitlement, intent or shortcut sends it. The agent can tap
  Send, but nothing can skip that step.
- **Order on Grab by deep link.** `grab://open?screenType=GRABFOOD` reaches a tab and
  nothing more. Cart, address, payment and confirmation are UI automation, every time.
- **Face ID, the passcode screen, or Apple Pay confirmation.** Not automatable on a real
  device, at all. Keep the phone unlocked and awake while it is being driven.
- **App Store purchases**, which are out-of-process and password gated.
- **Run while the phone is locked.** Unlike iPhone Mirroring, this needs the phone awake,
  and in exchange you can use the phone yourself at the same time.

Expect roughly 50-65% single-shot success on genuinely novel multi-step tasks in
third-party apps, which is where the published numbers on real closed-source apps sit.
Repeated tasks do much better, which is what the macro layer is for.

## Safety

The phone has money on it, so:

- Anything matching pay / order / checkout / send / delete / transfer is **refused once**
  and only fires when the same call is repeated with `confirmed=true`, which means the
  decision is visible in the transcript rather than buried in a tool call.
- Passwords and Keychain are on a deny list of apps the agent may not open.
- Every action, manual or agent, is appended to `runtime/logs/audit.jsonl`.
- **WebDriverAgent authenticates nothing, and by default it listens on every interface
  the phone has.** The usbmux forward puts a socket on the Mac's loopback, but that does
  not close the phone's own port: unless `USE_IP` is honoured, anyone on the same wifi
  can reach `http://<phone>:8100` and read your screen, your pasteboard and tap anything.
  `phoneshell up` passes `USE_IP=127.0.0.1` to bind it to the phone's loopback, but
  whether a physical device honours that is not something anyone has verified, so
  `phoneshell doctor` probes the phone's LAN address directly and tells you the truth
  rather than assuming. Treat a failed `lan-exposure` check as real.

## Layout

```
phoneshell/
  wda/client.py        the WebDriverAgent client, written against the runner's own source
  perception/tree.py   accessibility tree to a short, deduped, actionable element list
  perception/screen.py stability detection, downscaling, numbered overlays
  agent/observation.py one observation per step: TSV + picture + alert + hints
  agent/verify.py      consistency gate and stuck detection
  actions.py           the verb layer: tap, type, swipe, open, back, wait
  apps.py              installed-app catalogue and the deep links worth having
  device.py            build, sign, strip, install, launch, forward, health
  safety.py            confirmation rails and the audit log
  mcp_server.py        the 13 tools any MCP client can drive
  server.py + ui/      the local app: live screen, click to control, chat
  cli.py               doctor / setup / up / sim / shell / serve / mcp
```

`vendor/WebDriverAgent` is pinned to v16.12.4. Build products go to
`~/Library/Developer/phoneshell`, never under `~/Desktop`: that folder is TCC-protected
and a build there hangs the runner in an `open()` syscall with no error at all.
