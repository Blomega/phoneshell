"""The popup cheatsheet: what kinds of thing block a phone screen, and the move
that actually closes each one.

There are not many. Almost everything an app throws in front of you is one of
about a dozen shapes, and each shape has a known way out. Written down here as
data so the harness can work through it without thinking, and so the model can
be handed the same list when the reflex fails.

The ordering inside each entry matters: it goes from the move that is most
certain and least destructive to the one that is most likely to have a side
effect. Tapping a labelled "Not now" is always better than guessing at a
backdrop, which is always better than a back swipe that might leave the screen
you wanted.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Entry:
    kind: str
    looks_like: str
    tell: str                  # how to recognise it in the accessibility tree
    moves: list[str]           # ordered, most reliable first
    never: str = ""            # what must not be tapped
    note: str = ""


PLAYBOOK: list[Entry] = [
    Entry(
        kind="bottom sheet with a grabber",
        looks_like=("a panel anchored to the bottom of the screen with a short horizontal bar "
                    "centred on its top edge, the rest of the app dimmed behind it"),
        tell=("a container starting part-way down the screen and reaching the bottom, with a "
              "small wide element (roughly 30-70pt by 3-8pt) centred at its top"),
        moves=[
            "drag DOWN starting exactly on the grabber bar, about 60% of the screen height",
            "drag down from just below the sheet's top edge",
            "tap the dimmed area ABOVE the sheet",
            "tap a close control if the sheet has one",
        ],
        note=("this is the Grab flash-deals sheet shape. The grabber is the handle: iOS sheets are "
              "dismissed by dragging it DOWN, and tapping it does nothing at all. The grabber is "
              "usually decorative and absent from the accessibility tree, so aim at the sheet's "
              "top edge plus a few points, which is where it physically sits"),
    ),
    Entry(
        kind="full-screen modal",
        looks_like="a page that covers everything, usually with an X in a top corner",
        tell="a container covering more than 85% of the screen that was not there a moment ago",
        moves=[
            "tap the X (look for identifiers xmark, xmark.circle.fill, close, btn_close, ic_close)",
            "tap 'Not now', 'Skip', 'Maybe later', 'No thanks'",
            "drag down from the very top of the modal",
            "swipe in from the left edge to go back",
        ],
    ),
    Entry(
        kind="interstitial advert",
        looks_like="a full-screen ad, often with a countdown before the X appears",
        tell="a full-screen WebView or Image with no useful accessibility labels, often an ad SDK",
        moves=[
            "wait 2-4 seconds for the close control to appear, then look again",
            "tap the X, which is usually small and in a top corner",
            "tap 'Skip Ad' once the countdown finishes",
            "swipe in from the left edge to go back",
        ],
        note="the X is deliberately tiny and late; observing again after a pause is usually the fix",
    ),
    Entry(
        kind="action sheet",
        looks_like="a stack of buttons at the bottom with Cancel underneath",
        tell="type Sheet, or a bottom-anchored group whose last button is Cancel",
        moves=["tap 'Cancel'", "tap the dimmed area above it", "drag it down"],
    ),
    Entry(
        kind="system alert",
        looks_like="a small centred box with one or two buttons",
        tell="an element of type Alert, or GET /alert/text returns something",
        moves=["read it and decide", "phone_alert('dismiss') to decline", "phone_alert('accept') to agree"],
        never="never auto-accept a permission, a payment, or anything that agrees to terms",
        note="permissions, tracking prompts and payment confirmations are the user's decision, not the agent's",
    ),
    Entry(
        kind="rating or review prompt",
        looks_like="'Enjoying the app?' with stars, or 'Rate us'",
        tell="an Alert containing the words rate, review, enjoying or feedback",
        moves=["tap 'Not Now'", "tap 'Later'", "phone_alert('dismiss')"],
    ),
    Entry(
        kind="onboarding tour or coach mark",
        looks_like="a tooltip pointing at a control, with Next or Got it",
        tell="a small overlay with a highlighted cut-out and a dimmed rest of screen",
        moves=["tap 'Skip'", "tap 'Got it'", "tap 'Next' until it ends", "tap the dimmed area"],
    ),
    Entry(
        kind="paywall or upsell",
        looks_like="prices, a subscribe button, and a small close control",
        tell="a full-screen modal containing prices, 'per month', 'free trial', 'upgrade'",
        moves=[
            "tap the X, which may take a second or two to appear",
            "tap 'Not now', 'Maybe later', 'Continue with limited'",
            "drag down from the top of the sheet",
        ],
        never="never tap Subscribe, Start free trial, or anything that begins a purchase",
    ),
    Entry(
        kind="cookie or consent banner",
        looks_like="a bar or sheet inside a WebView about cookies or privacy",
        tell="a WebView containing the words cookie, consent, privacy or GDPR",
        moves=[
            "tap 'Reject all' or 'Only necessary' when present",
            "tap 'Accept' only if the user has said it is fine",
            "scroll inside the banner: the buttons are often below the fold",
        ],
        never="do not agree to terms on the user's behalf without asking",
    ),
    Entry(
        kind="update nag",
        looks_like="'A new version is available'",
        tell="an Alert or sheet containing update, version or what's new",
        moves=["tap 'Later'", "tap 'Not now'", "tap 'Skip this version'"],
        never="do not tap Update: it leaves the app for the App Store",
    ),
    Entry(
        kind="notification banner",
        looks_like="a message sliding in from the top over whatever you were doing",
        tell="a transient element near the top, above the app's own content",
        moves=["swipe it UP to dismiss", "wait 4 seconds and it leaves on its own"],
        note="it steals taps aimed at the top of the screen while it is there",
    ),
    Entry(
        kind="toast or snackbar",
        looks_like="a small message near the bottom that fades by itself",
        tell="a small element near the bottom with no buttons",
        moves=["wait 2-3 seconds, it goes away without help"],
        note="never worth acting on; acting on it usually means tapping through it by mistake",
    ),
    Entry(
        kind="keyboard covering the content",
        looks_like="the keyboard hiding the button you need",
        tell="Key elements present, or the bottom third is a keyboard",
        moves=["phone_gesture('scroll') to bring the target above the keyboard",
               "tap a neutral area to dismiss the keyboard", "press Return if the field is complete"],
    ),
    Entry(
        kind="popover with an arrow",
        looks_like="a small panel with a pointer to the control that opened it",
        tell="type Popover, or a small panel with a dimmed rest of screen",
        moves=["tap anywhere outside the popover", "press the back or close control"],
    ),
]


def as_text(limit: int | None = None) -> str:
    """The cheatsheet, compact enough to hand to a model mid-task."""
    lines = ["POPUP CHEATSHEET - what is in the way, and how it closes:"]
    for entry in PLAYBOOK[:limit]:
        lines.append(f"\n* {entry.kind}: {entry.looks_like}")
        lines.append(f"  recognise: {entry.tell}")
        for i, move in enumerate(entry.moves, 1):
            lines.append(f"  {i}. {move}")
        if entry.never:
            lines.append(f"  NEVER: {entry.never}")
        if entry.note:
            lines.append(f"  note: {entry.note}")
    return "\n".join(lines)


def summary() -> str:
    return "; ".join(f"{e.kind} -> {e.moves[0]}" for e in PLAYBOOK)
