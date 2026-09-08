# Findings

Everything here was measured on a physical iPhone 17 Pro Max running iOS 26.6, driven from a
macOS 26.5 Mac over USB. No number in this file is estimated. Where something is unverified it
says so.

The reason to write these down: the code is reimplementable in a weekend, but the failure
taxonomy is not. Every one of these cost between twenty minutes and two hours to find, and each
one is a way the stack reports success while doing nothing.

---

## 1. Silent failures: the stack lies about success

### A `200 OK` means the runner accepted the request, not that the phone moved
WebDriverAgent returns HTTP 200 with a null value for **every** gesture sent to a locked or
sleeping phone, and nothing happens. Two screenshots taken either side of such a swipe are
byte-identical. Every gesture is now gated on a liveness check that raises rather than returning
a success that did not happen.

### `open_app` reports success whether or not the app comes forward
Both `/wda/apps/activate` and `/wda/apps/launch` return OK while the app stays in the background.
An agent told "opened Grab" when nothing opened will invent new routes forever: deep links,
Spotlight, tapping the icon. That was the single cause of an agent looping for 54 seconds on one
step. Now the foreground app is polled until it actually matches, and failure is reported as a
device-level block with an instruction not to retry variations.

### `/wda/homescreen` silently does nothing, often
`XCUIDevice.pressButton(.home)` fails frequently enough to strand an agent in the app switcher.
`home()` now verifies and falls back to a HID home event, then to the bottom-edge gesture.

### "Am I on the home screen" cannot be answered by bundle id
Control Centre, Notification Centre, Spotlight and the app switcher are **all**
`com.apple.springboard`. The check has to look at the screen: four or more `Icon` elements and
none of the overlay markers.

---

## 2. Performance: where the seconds actually go

Measured per operation, best of three, on a real device.

| operation | cost |
|---|---|
| `/wda/activeAppInfo` | 43 ms |
| `/screenshot` | 72 ms |
| `/source` at depth 20 | 159-210 ms |
| `/source` at depth 25 | **1,270 ms** |
| `alert_text()` on a heavy screen | **1,360 ms** |
| W3C `/actions` tap | 500 ms |
| W3C `/actions` **drag** | **19,000 ms** |
| `/wda/dragfromtoforduration` (same gesture) | **1,400 ms** |
| `/wda/swipe` | 1,000 ms |
| `/wda/scroll` | 7,350 ms |

### A W3C pointer chain that moves costs a flat ~19 seconds
Independent of step count or pauses: 1 step 19.2s, 12 steps 19.8s, no pauses 18.9s. A **tap**
through the same endpoint is 0.5s, so it is dragging specifically that XCTest makes expensive.
The native drag endpoint does the identical gesture in 1.4s. Every single-finger drag now routes
to the native endpoint; W3C is reserved for genuinely multi-finger paths, which still cost ~20s.
Note that `dragfromtoforduration`'s `duration` is the **press before** the drag, not the travel.

### Tree depth is the entire cost of a read on a heavy app
On Grab's food list: depth 12 → 0.09s / 4 elements; depth 16 → 0.15s / 29; depth 20 → 0.21s / 59;
depth 25 → 1.27s / 78; depth 30+ → no gain over 25. Default is 20 with escalation when a shallow
read looks under-described. Changing the setting itself is free (0.00s).

### `visible` and `accessible` are the expensive attributes
XCTest hit-tests every node to compute them. Excluding both took a read from **5.82s to 0.89s**
for a difference of one element out of 24.

### Asking for alert text costs 1.36s and almost always returns nothing
WDA hunts the whole hierarchy for an alert. The tree already says whether one exists, so the
expensive call is only made when an `Alert` element is present.

**Net: one full observation on a heavy third-party app went from 7-13s to 0.99s.**

---

## 3. Perception

### A stability gate must tolerate motion, not demand stillness
Waiting for two byte-identical frames never converges on any screen with a countdown timer, a
spinner or a carousel. Grab's "40% off flash deals 14:19" sheet made every observation pay the
full timeout, turning a 2s step into a 50s one. The gate now settles when under 1.2% of pixels
differ: a clock moves well under 1%, a real transition moves tens of percent.

### iOS bakes instance data into accessibility identifiers
Real examples from Settings:
`com.apple.settings.followUpGroupAsItem...account.61932825-AFFA-4F26-...` (a UUID) and
`com.apple.settings.connectedHeadphone.38:C4:3A:26:96:EE-0x85a8cf0f0` (a MAC and a **raw
pointer**). These are stable within one app launch and change on the next, so any screen
fingerprint built on raw identifiers treats every revisit as a brand-new screen. UUIDs, MACs,
pointers and long digit runs are normalised out before hashing.

### Rects can be infinite
WDA reports infinite rects for some offscreen scroll containers, and JSON parses `Infinity` into a
float. One reaching a gesture produces `Invalid parameter not satisfying: point.x != INFINITY`.
Sanitised at the tree parser.

### Element labels are not what is drawn on screen
Verifying a tapped element's label against the pixels raised a **39% false-alarm rate** when
applied to everything, because icons, images, text fields, webviews and containers all carry
labels describing something other than their own rendering. Scoped to types whose label IS the
rendering (Button, StaticText, Link, MenuItem, Tab, SegmentedControl): **0% false alarms over 46
elements**, and it still catches a relabelled button.

### Any-token matching is too weak to be a safety check
"Place order" matched a Cancel button because both contain "order". Matching now requires 60%
token coverage, which is exactly the distinction that matters when the wrong tap spends money.

---

## 4. Gestures

- **Top-edge gestures must start at y=0.** From y=0 the top-left swipe opens Notification Centre
  and the top-right opens Control Centre, repeatably. Whether y=1 also works is untested.
- **Page flips need momentum.** A drag that ends stationary snaps back to the page it started on.
- **Swipe inside the thing that scrolls.** A drag through the middle of the screen pans the map
  instead of scrolling the results sheet on top of it, which reads to an agent as "scrolling does
  nothing". The scrollable container has to be found in the **raw** tree, because the element
  filter drops unlabelled containers as scaffolding.
- **The sheet grabber is usually not in the accessibility tree.** It is decorative. Aim at the
  sheet's top edge plus a few points, which is where it physically sits.

---

## 5. Sheets and popups

A sheet is recognised by **starting part-way down the screen**, not by being large. Picking the
largest container selects the app's own full-screen window every time, which is how the iOS share
sheet (a `Popover` at y=478) was invisible to the detector. Container hunting must run on the raw
tree for the same reason as above.

Fourteen overlay shapes are catalogued in `phoneshell/agent/playbook.py` with the ordered moves
that close each. The reflex belongs in the harness, not in the model: an agent that has to reason
about a promo sheet burns its whole budget narrating.

---

## 6. Session management, which is where the worst bugs live

### Creating a WDA session switches the foreground app
A session created with no bundle id makes WebDriverAgent switch its app-under-test, which sends
the phone to the home screen. Anything that creates a session mid-task therefore **closes whatever
the agent just opened**.

Three consequences, each of which was a real bug:
1. Stale-session recovery that pressed home before re-attaching was closing apps every few seconds.
2. The benchmark's own verification step called `set_settings`, which needs a session, so the
   check navigated the phone away **before measuring it**. Every task failed with "foreground is
   springboard". A benchmark that destroys the state it measures produces confident nonsense.
3. Two clients (the app and the agent) evict each other's sessions continuously.

**The fix that matters:** WDA registers the whole read path with `.withoutSession` — `/source`,
`/screenshot`, `/wda/activeAppInfo`, `/wda/screen`, `/window/size`, `/alert/*`, `/wda/homescreen`,
`/wda/locked` and `/wda/apps/launchUnattached`. The hot loop now never creates a session at all,
so observing the phone cannot disturb it.

---

## 7. Measurement integrity

Two failure modes produced **fabricated benchmark scores**, and both would have been published as
real numbers.

1. **Rate limit.** The account hit its session limit after 5 tasks. The other 27 were recorded as
   model failures. The published headline would have been 12.5% when 27 tasks were never
   attempted.
2. **A locked phone.** The screen locked mid-run and every task after it failed identically with
   "foreground is springboard".

Both are now detected, marked **not run** rather than failed, excluded from scoring, and abort the
run with an explanation. The general rule: *an environment problem must never be scored as a
capability result*, and any benchmark that cannot tell the two apart is producing fiction.

---

## 8. Token cost

- The MCP tool definitions are ~2,542 tokens sent on **every** turn. Reference material in a tool
  *description* is therefore paid for on every request; it belongs in a tool's output.
- Returning a full screen after every action is the dominant cost, because each dump stays in the
  conversation and is re-read on every later turn: a 20-step task pays for the same tree twenty
  times. Unchanged screens now return one line, and a picture is sent once per distinct screen per
  session.
- On an identical 3-turn shape: **$0.1011 → $0.0345**, cache-write 18,584 → 1,278 tokens.
- A failed run costs exactly as much as a successful one, so preventing avoidable failures (a
  locked phone, a wedged bridge) is the cheapest optimisation available.


---

## 9. Turns are the cost, not milliseconds

Measured across the tasks that passed: **4.1 to 6.8 seconds per turn**, of which the phone
accounts for about one second. The rest is the model round trip. So the lever that matters is not
shaving device latency, it is asking the model fewer times.

The tool design was forcing a floor of two turns per action: `observe` to learn the element ids,
then `tap` using one. A three-screen navigation therefore cost six round trips and roughly thirty
seconds, for work the engine could have done deterministically in three.

`phone_do` takes a list of steps and runs them without stopping to look in between, matching taps
by visible text and scrolling to find them:

```json
[{"open_app": "Settings"}, {"tap": "General"}, {"tap": "About"}]
```

It stops at the first step that fails and returns the screen at that point, so a wrong guess costs
one call instead of derailing the run, and the safety rails still apply: a step that would tap
something matching pay, order, send or delete refuses and hands back to the model.

The general shape worth keeping: **let the model express a plan, let the engine execute it with
verification, and only return control when reality diverges.** That is the same idea as the macro
layer, one level lower and available without having recorded anything first.

---

## 10. Two engine bugs that a benchmark run exposed and normal use would not

Running 32 tasks unattended surfaced things that never appear when a human is watching:

- **`open_app("Contacts")` resolved to `com.apple.PeopleViewService`**, a system service rather
  than the Contacts app, because several bundles share a display name. The task would have failed
  with the model doing nothing wrong. Curated aliases now pin the canonical bundle for every app
  the suite touches.
- **The phone auto-locked mid-run despite Auto-Lock being set to Never.** Whatever the cause, the
  lesson is that the rig cannot depend on a phone-side setting it does not control. There is now a
  deterministic `prepare_for_run` that walks Settings and sets Auto-Lock to Never itself, written
  as a general "navigate to a settings page and choose an option" routine, because every system
  setting a rig depends on has that same shape.

This is the argument for running the benchmark continuously rather than once: it is a fault
detector for the engine, not only a scorecard for the model.


---

## 11. An iOS point update breaks the rig, silently

Overnight the phone updated itself from iOS 26.6 to 26.6.1. The next morning:

- `xcrun devicectl device process launch` reported **success**
- `AutomationModeUI` started on the phone
- the WebDriverAgent process was **not in the process list** seconds later
- port 8100 refused connections
- **no crash report was generated**

Nothing anywhere said "the runner is stale". The only symptom was a bridge that had worked for
hours and now did not. Developer Mode was still on and the app was still installed, so every
obvious check passed.

The fix is a rebuild, re-strip, re-sign and reinstall against the updated OS.

Two things this proves:

1. **The runner is tied to the OS build**, not just the major version. A point release is enough.
   Any always-on rig needs the rebuild to be automatic, or it dies overnight without warning.
2. **A signing team mismatch blocks the repair.** The rebuild first ran with whichever identity
   was first in the keychain rather than the configured one, and iOS refused the install with
   `IXUserPresentableErrorDomain error 1`, because an installed app cannot be replaced by a build
   from a different team. The error says nothing about teams. Always pass the team explicitly.

This is the strongest argument yet for treating continuity as the product rather than the tool:
the software did not change overnight, and it stopped working anyway.


---

## 12. Audit every failure, because most of them are the benchmark's fault

The first honest run scored 85.4% of attempted tasks. Then every failure was read against what the
agent actually said and did. Of six failures, **only one or two were the model's**:

| task | what really happened |
|---|---|
| `contacts.search` | The agent completed it and listed the results. The check looked for a "Cancel" button that does not exist in that UI. **Our bug.** |
| `multi.settings_to_safari` | The agent found the iOS version correctly, then **our own shared mode paused it** mid-task, believing the owner had picked up the phone. **Our bug.** |
| `reminders.create` | The agent created the reminder and verified it via the list count going 5 to 6. The check required the text to be visible without scrolling. **Our bug.** |
| `safari.newtab` | The agent diagnosed that Safari had **502 tabs** and that iOS disables the new-tab control at roughly 500. The task depended on the owner's device state. **Our unfair task.** |
| `safari.navigate.example` | The agent reported "Safari is open and showing example.com" while the phone was on the home screen. **A genuine catch, and exactly why the checks exist.** |
| `clock.timer.set` | 26 turns, 228 seconds, no result. **A genuine failure**: picker wheels are hard. |

Four of six failures were ours. A benchmark that is not audited this way publishes its own defects
as somebody else's incapability, and the number looks respectable either way, which is what makes
it dangerous.

Three fixes came out of it, and each generalises:

1. **"Not visible right now" and "not there" are different facts.** A check that refuses to scroll
   blames the model for the harness's impatience. Added `element_exists_anywhere`.
2. **Check for the surface, not for a specific button.** Button sets change between iOS versions;
   asserting on one makes the benchmark rot.
3. **A measured run must always take the phone.** Yielding to a human who is not there turns a
   courtesy feature into a scoring bug. Benchmark runs now force takeover mode.

And one task was retired outright rather than fixed: if a task can fail because of how many tabs
the owner happens to have open, it is measuring the device, not the agent.

The agent's own account is not evidence of success, but it is excellent evidence about the
harness. Reading it is the cheapest quality control available.

---

## 13. iOS offloads the automation runner itself

Part-way through a 58-task run the phone quietly deleted **the WebDriverAgent runner**, along
with the Weather app the last few tasks needed. `Settings > App Store > Offload Unused Apps`
does not spare a development build. Two tasks failed on a "Restore 'Weather'?" dialog that no
agent could have satisfied, and every task after that failed as unreachable.

What made this expensive is the next finding.

### `devicectl device process launch` reports success for an app that is not installed

```
$ xcrun devicectl device process launch --device <udid> com.blomega.WebDriverAgentRunner.xctrunner
Launched application with com.blomega.WebDriverAgentRunner.xctrunner bundle identifier.
$ echo $?
0
```

The bundle was not on the phone. `devicectl device info apps` listed three Blomega apps and no
runner. The bridge keys its fallback on that exit code, so the tethered path never fired: it
launched nothing, waited 40 seconds, and exited. Diagnosis took twenty minutes.

Two fixes, both in `phoneshell/device.py`:

* `launch_wda` now asks the phone what is installed **before** launching, and reports the
  offload as the failure it is.
* `up` no longer treats a clean exit code as proof. If nothing binds the port within the
  timeout it falls back to the tethered DVT session regardless of what the launcher claimed.

## 14. Reads can keep working while every write is silently dropped

The worst state the phone reached today looks, from the Mac, like a perfectly healthy device.
`/status`, `/source`, `/screenshot` and `/alert/text` all returned correct, current data. Every
write returned success and did nothing:

| call | reported | actually |
| --- | --- | --- |
| `POST /alert/dismiss` (x4) | `OK` | alert still on screen |
| `tap` on that alert's Cancel button | `OK` | **0.0%** of pixels changed |
| `POST /wda/homescreen` | `OK` | screen unchanged |
| full-width `drag` | `OK` | **0.0%** of pixels changed |

The device log says why:

```
process:AutomationModeUI ... dropped:110348 dropStatus:-536870168
```

The HID event system had wedged. Reinstalling the runner did not clear it. Relaunching the
runner did not clear it, and neither did the tethered DVT path, which brought WDA up cleanly and
still could not move a pixel. Only a reboot clears it.

The cost of not detecting this: one task burned **23 turns and $0.41** before the run gave up,
and the run's last five results measure the wedge rather than the model.

`doctor` now ends with an `input` check that drags across the screen and looks, and
`prepare_for_run` runs the same probe and **refuses to start a benchmark** when it fails. The
probe costs about two seconds. Before this, doctor reported the phone healthy in exactly this
state.

## 15. XCTest turns a picker wheel by tapping it, not by dragging it

From WebDriverAgent's own `XCUIElement+FBPickerWheel.m`:

```objc
XCUICoordinate *startCoord = [self coordinateWithNormalizedOffset:CGVectorMake(0.5, 0.5)];
XCUICoordinate *endCoord = [startCoord coordinateWithOffset:
    CGVectorMake(0.0, relativeHeightOffset * snapshot.frame.size.height)];
[endCoord tap];
```

A single **tap**, 20% of the wheel's height above or below its centre. A wheel advances exactly
one row per tap beside the selected row. This is why an agent swiping at a timer never
converges: a drag carries momentum and lands somewhere approximate, so it overshoots, corrects,
overshoots again. `clock.timer.set` spent the full 240-second budget doing that and produced
zero completed turns.

`Phone.set_picker()` implements the tap, reads the row back through the accessibility tree, and
closes the loop. When both the current row and the wanted row are numbers, the first tap also
measures how far one tap carries, and the remaining distance becomes arithmetic: fire that many
taps and read once at the end, rather than paying a tree read per row.

This generalises well beyond a timer. Date, time, duration, country and unit pickers are all
`XCUIElementTypePickerWheel`, so one primitive covers Clock, Calendar, Alarms, Health and every
sign-up form that asks for a date of birth.

## 16. Typing had no witness

`type_text` sent keystrokes and reported on a pixel diff. Keystrokes go to whatever holds
keyboard focus, and when nothing does they are discarded in silence, but the keyboard still
animates in, so the pixel diff says "the screen changed" and the agent believes it typed.

Measured on `reminders.create`: the agent created a reminder, typed "collect parcel", and
reported *"Done, collect parcel was added to Reminders (count went from 6 to 7 in the All
list)."* The count did go from 6 to 7. The reminder was empty. The task's foreground check
passed and its text check failed, which is the only reason anyone noticed.

`type_text` now reads the tree back and looks for what it typed. If the text is not there the
result is a failure whose detail says so in words the agent can act on, `phone_type` surfaces
that instead of its generic header, and a `type` step inside `phone_do` stops the sequence
rather than letting every later step act on an empty field. Verification is skipped when
`submit=true` (the screen usually navigates away) and when a `SecureTextField` is present (a
password field masks what it holds).

## 17. A check that depends on device state measures the device

`system.notifications.open` asserted `element_exists: Clear`. That button only exists when the
phone happens to have notifications waiting. On an empty phone the task is unpassable no matter
what the agent does. Replaced with a pattern that matches Notification Centre in either state.

This is the fourth check of this shape found by auditing failures, after the three in section 12.
The rule that keeps holding: **when a task fails, read what the agent said before believing the
check.**

---

## 18. The benchmark decayed the thing it was measuring

The alarm tasks say "do not save it". Agents saved it anyway, every run, for weeks. By today the
phone held **640 alarms**, 621 of them identical unlabelled artifacts.

That is not cosmetic. Everything the harness does on that screen got slower in proportion:

| operation | Clock timer screen | Clock alarms, 640 rows | with the Add Alarm sheet open |
| --- | --- | --- | --- |
| `/source` (full tree) | 154 ms | 2 612 ms | 24 195 ms (6.1 MB) |
| `tap` | 550 ms | 2 290 ms | |
| `drag` | 1 400 ms | 4 200 ms | |

`clock.alarm.set_time` has a 240-second budget. At 24 seconds per observation it could not have
finished, and it did not: zero completed turns. **The task failed because of the residue of
earlier runs of the same task.** A suite that mutates the device has to undo it, or its own
numbers drift out from under it. There is now a `clean_alarms` teardown action, and the three
Clock tasks run it.

The general form is worth stating plainly: **WebDriverAgent's per-action latency scales with the
size of the accessibility tree**, because the runner walks it to resolve every request. So a
screen that an agent made big is a screen the agent will be slow on afterwards. Cleanup is not
tidiness, it is throughput.

## 19. At scale, `/source` returns an incomplete tree and says nothing

One read of the 640-alarm screen returned **631 cells and 413 switches**. There is one switch per
alarm, so 218 of them were simply absent, with no error, no truncation marker and HTTP 200.

This breaks any logic that reasons about what is *not* in the tree. A cleanup guard that stopped
when a protected alarm was missing from the tree fired on all twenty protected alarms at once,
because the read was short, not because anything had been deleted. Anything checking for absence
at this scale has to require several reads to agree before believing it.

Checks for *presence* are still sound. That asymmetry is the useful rule: **a truncated tree can
make something look gone; it cannot invent something that is not there.**

## 20. Deleting rows renormalises the scroll offset, and it cost a real alarm

Deleting 640 rows one read at a time is about 10 seconds each, so batching was tempting and
looked provably safe: removing a row shifts only the rows *below* it, so if you delete
bottom-to-top, every target above the one just deleted keeps the position the read measured.

That reasoning is wrong on iOS. When the content shrinks, the list re-anchors its scroll offset,
so the whole visible window moves and the remaining "verified" positions point at different rows.
A batch of seven deletes removed one alarm that was not an artifact: a labelled, weekday-repeating
7:00 AM alarm belonging to the phone's owner.

It was rebuilt from what the tree had recorded (time, label, repeat), and the incident is the
reason the cleanup now does exactly one delete per read, verified immediately before the gesture.
Two things generalise:

* **A position measured before a mutation is not valid after it**, even when the mutation is
  "below" it. Re-read, or do not act.
* A cheap safety rule beats an expensive one when reads are unreliable. Deciding from the row's
  own label and switch state needs only the rows on screen, and an unknown switch defaults to
  "on", which keeps the alarm. That rule stays correct even when the tree comes back short. The
  rule that failed was the one needing a complete tree.

### An element's frame is only meaningful when the element is on screen

Correcting something asserted earlier in this file. An alarm's switch reports
`x=0, y=116, width=63` and it is tempting to call that frame wrong, because the switch is plainly
drawn near the right-hand edge. It is not wrong, it is a placeholder: iOS clamps the frame of a row
it has not rendered. Read the same switch while its row is actually visible and it reports
`x=359, width=63` on a 440-point screen, which is exactly where it is drawn.

So `x > 0` is a usable test for "this control can be tapped right now", and a visible control's own
frame is trustworthy. What is not trustworthy is the *row's* centre as a stand-in for the control's:
the toggle is aligned with the alarm's time, about 24 points above the middle of a 107-point row, so
tapping the row centre misses a 29-point-tall switch entirely. That produced a tap that reported
success, changed nothing, and looked identical to a dropped event.

## 21. A task that ran out of time could still be scored as a pass

`result.passed` came from the checks alone. `clock.alarm.set_time` hit its 240-second budget with
**zero completed turns and $0.00 spent**, and then passed both of its checks from whatever
happened to be on screen: its `(?<![0-9])30(?![0-9])` pattern matched `10:30AM, Alarm` in the
list sitting behind the sheet.

Two separate faults, both now fixed. A timed-out task cannot pass, whatever the phone looks like
afterwards. And the checks now match the wheel's own value (`30 minutes`, `7 o.clock`) rather than
any digits anywhere on the screen.

This is the fifth distinct way this benchmark has found to report a number that was not true. The
others: fabricating scores for tasks that never ran, verification that destroyed its own
measurement, counting results from retired tasks, and scoring a task whose app was not installed.
Every one of them made the number look *better*.

---

## 22. An adversarial review of one day's work found nine critical defects

Five reviewers, one per dimension, each finding verified by an independent skeptic told to refute
it. Fifteen findings, **thirteen survived**, nine of them critical. Two were refuted and dropped.

What makes them worth writing down is that they are almost all the *same* defect wearing different
clothes: **a reading taken at the wrong moment, or a reading that proves less than it appears to,
used as grounds to act or to score.** That is the exact fault that had already deleted one of the
owner's alarms earlier in the day, and it was still present in eight other places.

### The fix was applied to one copy of the gesture and not the other

The delete swipe that started at 92% of the row width, landed on the alarm's toggle, and switched
nine alarms on was fixed in `scripts/clean_alarms.py`. **The same gesture in
`Runner.remove_artifact_alarms` was missed**, and it runs as teardown on three Clock tasks, so the
next benchmark run turned three more alarms on: 12:00 AM, 12:10 AM and 12:56 AM. Found by the
review, confirmed on the phone, fixed in both places.

The lesson is duller than the bug: when a gesture is worth a comment explaining why it is fragile,
grep for every copy of it before considering it fixed.

### Digits alone are not a match

`set_picker` compared picker rows by their digits, which was a correct fix for `"5"` matching
`"15 min"` and a wrong one for everything else. It discarded weekday, month, AM/PM and unit:

| row shows | asked for | old verdict | truth |
| --- | --- | --- | --- |
| `9:00 AM` | `9:00 PM` | match | different by twelve hours |
| `Fri Oct 9` | `Mon Sep 9` | match | different by a month |
| `1 hour before` | `1 day before` | match | different by a day |

On a combined date wheel that returns `ok=True` having fired no taps at all, and the caller saves
the wrong date. Now the numbers must agree **and** whatever letters were asked for must be on the
row.

### Proving something is gone is harder than proving it is there

`element_exists` with `negate: true` took one un-settled, non-scrolling read and inverted it, so a
read that simply missed became "correctly not found" and scored a pass. `reminders.complete` passed
exactly that way while the reminders it was meant to have completed were still on the phone.

Absence now has to survive a settled read and a scroll, and an unreadable screen returns "absence
is unproven" rather than a pass. The asymmetry from section 19 holds here too: **a bad read can
make something look gone; it cannot invent something that is not there.**

### A row the tree can see is not a row you can tap

`scroll_to_text` returned as soon as the text appeared anywhere on screen. iOS 26 floats a search
field over the bottom of a list and a navigation bar over the top, and the tree reports a row
underneath one of them as perfectly visible. Measured: `Display & Brightness` was found at y=906 of
956, the tap hit the search bar instead, Settings search opened, and every later step searched a
screen it had never left. The visible symptom was `could not find 'Auto-Lock' in Settings` on a
phone where Auto-Lock was one swipe away.

A hit is now nudged clear of both bars before it is returned, which fixes it for every caller
rather than for Settings alone.

### iOS does not tell you which row is selected

Confirming a setting by searching the page for the option's own label proves nothing: on Auto-Lock
the row `Never` is listed whether or not it is the chosen value, so the check passed every time and
the phone kept locking mid-run, which cost four benchmark runs.

The accessibility tree exposes no selection state at all here: no `traits`, no `value`, no
`isSelected`. What it does expose is a `Button` labelled `checkmark` inside the chosen row and
nowhere else. That is the evidence, and it is only visible in the raw tree, because `condense()`
drops it as decoration.

### The rest

* **A TCP connect proves the usbmux forward, not the runner.** `recycle_runner` accepted
  `socket.create_connection` as proof WebDriverAgent came back. This repo's own client documents
  that a forward accepts the connection and then resets it when nothing is listening on the phone,
  so with `phoneshell up` holding iproxy open this succeeded on the first attempt whether or not
  the runner ever restarted. It now requires an HTTP answer.
* **`doctor` swiped destructively across whatever app was in front.** The input probe dragged
  full-width through the vertical centre of the foreground app, which on a list is the destructive
  row action, and is literally the gesture the alarm cleaner uses to delete a row. It now goes to
  the home screen first, and tries both directions, because a rubber-band at the end of the home
  screen was reading as a dead phone.
* **A task cut off by `--max-turns` still scored a pass.** Only the wall-clock timeout was guarded.
  The CLI exits 0 with `{"subtype":"error_max_turns","is_error":true}`, which was read as an
  ordinary result. That is the sixth distinct way this benchmark found to report a number that was
  not true.
* **A setup failure was scored as a model failure.** The model was never asked, so a harness fault
  (an offloaded app's restore dialog, one typo in a task file) went into the denominator and
  lowered the score for reasons no model can affect. It is now recorded as not scored.
* **Typing verification could fail on an unreadable tree, pass on text that was already there, and
  trip over whitespace.** The tree collapses whitespace runs, so a typed newline could never match
  what was read back. All three guarded.

---

## 23. The screenshot is not paying for itself

The benchmark could only ever run Anthropic models, because the agent loop was the `claude` CLI.
Routing a vendor-prefixed slug through OpenRouter instead made it cross-vendor, and the first thing
that made possible was an experiment rather than a leaderboard.

Two of the six models, Qwen3-Max and DeepSeek v3.2, cannot accept an image at all. They work from
the accessibility tree alone. That makes them a control group for a question with no published
answer: **on iOS, with a well-labelled accessibility tree, what is the screenshot actually worth?**

Comparing them against GPT-5.1 confounds the model with the modality, so the clean version is one
model run both ways. Same model, same 24 tasks, same physical phone:

| GPT-5.1 | score | steps | seconds | cost |
| --- | --- | --- | --- | --- |
| tree **and** screenshot | 20/24, 83.3% | 6.5 | 42 | $0.196 |
| tree only | 20/24, 83.3% | 6.1 | 38 | $0.154 |

**Zero points.** The image costs 27% more money, half a step more, and four seconds a task, and buys
nothing measurable. Task by task it is not quite a wash, which is the more interesting part:

* `notes.text_select` passed only **with** the image.
* `calculator.chain` passed only **without** it. The picture is not free even when it is ignored: it
  is context, and context can mislead.

The independent confirmation is stronger than the ablation. **Qwen3-Max, which never saw a single
screenshot, tied GPT-5.1 and Claude Opus 4.8 at 83.3%**, and did it for less money than either.

### The leaderboard that produced it

24 tasks covering all 22 capabilities, one physical iPhone 17 Pro Max on iOS 26.6, $4.29 of API
spend in total.

| model | score | steps | $/task | input |
| --- | --- | --- | --- | --- |
| Kimi K3 | **91.7%** | 6.2 | $0.0228 | tree + image |
| GPT-5.1 | 83.3% | 6.5 | $0.0082 | tree + image |
| Claude Opus 4.8 | 83.3% | 5.8 | $0.1130 | tree + image |
| Qwen3-Max | 83.3% | 6.3 | **$0.0076** | **tree only** |
| Gemini 3.1 Pro | 79.2% | 5.2 | $0.0223 | tree + image |
| DeepSeek v3.2 | 75.0% | 8.9 | $0.0050 | tree only |

Claude Opus 4.8 costs **fifteen times** what Qwen3-Max costs for exactly the same score. On this
task set the accuracy frontier is nearly flat between 79% and 92% while price moves by a factor of
twenty-three, which says the interesting axis for anyone actually deploying this is not accuracy.

**The scope of the claim matters.** Every task here runs against Apple's own applications, and Apple
labels its controls properly. Section 20 records a chat app returning `WAMessageBubbleTableViewCell`
as a button's name, and a Flutter or Unity app can expose one opaque view for a whole screen. So the
honest statement is: *where the tree is good, the screenshot is redundant.* Whether vision rescues a
badly-labelled app is the obvious next experiment, and it is the one that would matter commercially.

### One capability defeats every model

| capability | passed |
| --- | --- |
| edge-gesture | **0/6** |
| precise-taps | 4/6 |
| scroll-end | 4/6 |

`edge-gesture` is a swipe that must begin at `y=0`, off the drawn screen, to open Control Centre or
Notification Centre. Not one of the six frontier models managed it, and the two tasks no model
solved at all are that one and `clock.timer.set`. This is what a capability-isolated suite buys: not
"the models scored 83%", but *the gesture that starts off-screen is unsolved across the industry*.

**Twelve of the twenty-four tasks were passed by every single model.** They no longer separate
anything and are dead weight in a leaderboard, which is a finding about the benchmark rather than
about the models: the suite needs harder tasks more than it needs more tasks.

---

## 24. A reboot is not an unattended remedy

A wedged device-side test session (`DTX capability handshake timed out`, zero runner processes)
clears with a reboot and does not clear any other way. But the reboot has a cost that matters more
than the wedge did:

**After a cold boot iOS is in Before First Unlock, and developer services do not come up until
somebody unlocks the phone by hand.**

```
lockdown:   answering, "PasswordProtected": true
devicectl:  unavailable        <- CoreDevice / developer services withheld
WebDriverAgent: cannot start, so it cannot type the passcode it has
```

The stored passcode does not help, and cannot: unlocking is what WebDriverAgent does, and
WebDriverAgent is what will not start. It is a genuine chicken and egg, and it is a security
property rather than a bug.

So the operational rule for anything unattended: **a reboot ends the session.** Schedule it at the
start of a run with a person present, never as mid-run recovery. Everything else in this stack can
be recovered from over the wire; this cannot.

The corollary is that the things which *cause* a wedge deserve more attention than the recovery
does. The one seen here was memory pressure on the Mac killing a half-open test session, which is
worth guarding against directly: do not run a long device job alongside anything that can exhaust
memory, because losing the Mac-side process leaves the device-side session dangling.
