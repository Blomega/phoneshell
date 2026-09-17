"""Additions this project makes to WebDriverAgent's own source.

WebDriverAgent can do almost everything a phone agent needs, and the gaps are
narrow enough to be worth closing rather than working around. Each patch here
adds one route to the runner that Appium's build does not ship.

Held as idempotent source edits rather than as `.patch` files on purpose. A
unified diff is pinned to byte offsets in one upstream revision and fails
unreadably on the next; these are anchored to the text they insert next to, know
whether they are already applied, and can be re-run after a re-clone or a tag
bump without a human reading a reject file. `phoneshell doctor` reports their
state and `phoneshell setup` applies them before building.

## Why patching is necessary at all

**importMedia.** There is no other way to put a picture in the camera roll.
Measured on this device, iOS 26.6.2: AFC over usbmux gives full write access to
`/var/mobile/Media/DCIM` -- a file written there lands on the disk and `stat`
confirms it -- and the photo library never sees it. The library count held at
8,030 across a write and a relaunch of Photos. The photo library is a database,
not a directory scan, and the only supported way in is `PHPhotoLibrary`, which
has to run in a process on the phone. WebDriverAgent is the process we have.

**pressButton without a session.** Upstream already exposes `/wda/homescreen`,
`/wda/lock` and `/wda/activeAppInfo` sessionless, but not `/wda/pressButton`,
so pressing volume or the home button costs a session that is then thrown away.

## What this widens

Both routes are `.withoutSession`, which means anything that can reach the
WebDriverAgent port can use them, and WebDriverAgent authenticates nothing. On a
cabled phone bound to loopback that changes little. On a phone reachable over
wifi it means anything on that network can write to the camera roll, which is
why `bringup` warns about wifi exposure and binds to the phone's loopback by
default.
"""
from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

COMMANDS = "WebDriverAgentLib/Commands/FBCustomCommands.m"
RUNNER_PLIST = "WebDriverAgentRunner/Info.plist"

# Anchors: text that exists in upstream v16.12.4 and that the additions go
# beside. If an anchor ever stops matching, the patch refuses rather than
# writing something plausible into the wrong place.
_ROUTE_ANCHOR = (
    "    [[FBRoute POST:@\"/wda/pressButton\"] respondWithTarget:self "
    "action:@selector(handlePressButtonCommand:)],\n"
)
_IMPORT_ANCHOR = "#import \"FBCustomCommands.h\"\n"
_HANDLER_ANCHOR = "+ (id<FBResponsePayload>)handleSetPasteboard:(FBRouteRequest *)request\n"

_PHOTOS_IMPORT = """#import "FBCustomCommands.h"

#if !TARGET_OS_TV && !TARGET_OS_WATCH
@import Photos;   // phoneshell: importMedia
#endif
"""

_SESSIONLESS_BUTTON = (
    "    [[FBRoute POST:@\"/wda/pressButton\"].withoutSession respondWithTarget:self "
    "action:@selector(handlePressButtonCommand:)],   // phoneshell\n"
)

_MEDIA_ROUTES = """#if !TARGET_OS_TV && !TARGET_OS_WATCH
    // phoneshell: put a picture or a video in the camera roll.
    [[FBRoute POST:@"/wda/importMedia"] respondWithTarget:self action:@selector(handleImportMedia:)],
    [[FBRoute POST:@"/wda/importMedia"].withoutSession respondWithTarget:self action:@selector(handleImportMedia:)],
#endif
"""

_MEDIA_HANDLER = """#if !TARGET_OS_TV && !TARGET_OS_WATCH
// phoneshell: import media into the photo library.
//
// Every wait here goes through FBRunLoopSpinner rather than a semaphore, and
// that is not a style preference. An FBRoute handler runs on the main thread,
// the Photos permission alert needs that same thread to present itself, and a
// semaphore wait deadlocks the two. Measured on iOS 26.6.2: the runner was
// killed after 30 seconds by the FRONTBOARD scene-update watchdog with
// 0x8BADF00D, "exhausted real (wall clock) time allowance", faulting thread 0.
// Spinning the run loop keeps the main thread answering while we wait.
+ (id<FBResponsePayload>)handleImportMedia:(FBRouteRequest *)request
{
  NSString *encoded = request.arguments[@"data"];
  if (![encoded isKindOfClass:NSString.class] || 0 == encoded.length) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"'data' must be a non-empty base64 string" traceback:nil]);
  }
  NSData *payload = [[NSData alloc] initWithBase64EncodedString:encoded
                                                        options:NSDataBase64DecodingIgnoreUnknownCharacters];
  if (nil == payload || 0 == payload.length) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"Cannot decode 'data' from base64" traceback:nil]);
  }
  NSString *name = request.arguments[@"name"];
  if (![name isKindOfClass:NSString.class] || 0 == name.length) {
    name = @"phoneshell.jpg";
  }
  name = name.lastPathComponent;
  BOOL isVideo = [request.arguments[@"kind"] isEqualToString:@"video"]
    || [@[@"mov", @"mp4", @"m4v"] containsObject:name.pathExtension.lowercaseString];

  __block PHAuthorizationStatus access = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelAddOnly];
  if (PHAuthorizationStatusNotDetermined == access) {
    __block BOOL answered = NO;
    [PHPhotoLibrary requestAuthorizationForAccessLevel:PHAccessLevelAddOnly
                                               handler:^(PHAuthorizationStatus granted) {
      access = granted;
      answered = YES;
    }];
    if (![[[FBRunLoopSpinner new] timeout:25.0] spinUntilTrue:^BOOL { return answered; }]) {
      return FBResponseWithStatus([FBCommandStatus unknownErrorWithMessage:@"Nobody answered the Photos permission prompt on the device within 25s" traceback:nil]);
    }
  }
  if (PHAuthorizationStatusAuthorized != access && PHAuthorizationStatusLimited != access) {
    return FBResponseWithStatus([FBCommandStatus unknownErrorWithMessage:@"Photos access is denied for WebDriverAgentRunner. Allow 'Add Photos Only' for it in iOS Settings > Privacy & Security > Photos." traceback:nil]);
  }

  NSString *path = [NSTemporaryDirectory() stringByAppendingPathComponent:
                    [NSString stringWithFormat:@"phoneshell-%@-%@", NSUUID.UUID.UUIDString, name]];
  NSError *writeError = nil;
  if (![payload writeToFile:path options:NSDataWritingAtomic error:&writeError]) {
    return FBResponseWithUnknownError(writeError);
  }

  __block NSError *saveError = nil;
  __block BOOL saved = NO;
  __block BOOL finished = NO;
  [[PHPhotoLibrary sharedPhotoLibrary] performChanges:^{
    PHAssetCreationRequest *creation = [PHAssetCreationRequest creationRequestForAsset];
    PHAssetResourceCreationOptions *options = [PHAssetResourceCreationOptions new];
    options.originalFilename = name;
    // The temp file is ours and has served its purpose; moving it saves copying
    // the whole payload again and cleans up in one step.
    options.shouldMoveFile = YES;
    [creation addResourceWithType:(isVideo ? PHAssetResourceTypeVideo : PHAssetResourceTypePhoto)
                          fileURL:[NSURL fileURLWithPath:path]
                          options:options];
  } completionHandler:^(BOOL success, NSError *error) {
    saved = success;
    saveError = error;
    finished = YES;
  }];
  BOOL inTime = [[[FBRunLoopSpinner new] timeout:60.0] spinUntilTrue:^BOOL { return finished; }];
  [NSFileManager.defaultManager removeItemAtPath:path error:nil];
  if (!inTime) {
    return FBResponseWithStatus([FBCommandStatus unknownErrorWithMessage:@"Timed out saving to the photo library" traceback:nil]);
  }
  if (!saved) {
    return FBResponseWithUnknownError(saveError);
  }
  return FBResponseWithObject(@{
    @"name": name,
    @"bytes": @(payload.length),
    @"kind": isVideo ? @"video" : @"photo",
  });
}
#endif

"""

# iOS kills any process that touches the photo library without this string.
PHOTO_USAGE_KEY = "NSPhotoLibraryAddUsageDescription"
PHOTO_USAGE_TEXT = "phoneshell puts files an agent was given onto this phone."


@dataclass
class PatchState:
    name: str
    applied: bool
    detail: str = ""


def _patch_commands(text: str) -> str:
    if _IMPORT_ANCHOR not in text:
        raise ValueError(f"{COMMANDS}: cannot find the import anchor; WebDriverAgent has moved on")
    if _ROUTE_ANCHOR not in text:
        raise ValueError(f"{COMMANDS}: cannot find the pressButton route; WebDriverAgent has moved on")
    if _HANDLER_ANCHOR not in text:
        raise ValueError(f"{COMMANDS}: cannot find the setPasteboard handler; WebDriverAgent has moved on")
    if "@import Photos;" not in text:
        text = text.replace(_IMPORT_ANCHOR, _PHOTOS_IMPORT, 1)
    if "/wda/importMedia" not in text:
        text = text.replace(_ROUTE_ANCHOR, _ROUTE_ANCHOR + _MEDIA_ROUTES, 1)
    if "].withoutSession respondWithTarget:self action:@selector(handlePressButtonCommand:)" not in text:
        text = text.replace(_ROUTE_ANCHOR, _ROUTE_ANCHOR + _SESSIONLESS_BUTTON, 1)
    if "handleImportMedia:(FBRouteRequest" not in text:
        text = text.replace(_HANDLER_ANCHOR, _MEDIA_HANDLER + _HANDLER_ANCHOR, 1)
    return text


def _commands_applied(text: str) -> bool:
    return all(marker in text for marker in (
        "@import Photos;",
        "/wda/importMedia",
        "handleImportMedia:(FBRouteRequest",
        "].withoutSession respondWithTarget:self action:@selector(handlePressButtonCommand:)",
    ))


def status(src: Path) -> list[PatchState]:
    """Which patches this WebDriverAgent checkout already carries."""
    out: list[PatchState] = []
    commands = src / COMMANDS
    if not commands.exists():
        out.append(PatchState("wda-routes", False, f"{COMMANDS} is missing"))
    else:
        text = commands.read_text()
        applied = _commands_applied(text)
        out.append(PatchState(
            "wda-routes", applied,
            "importMedia and a sessionless pressButton are in the source" if applied
            else "importMedia is not in this WebDriverAgent build",
        ))
    plist = src / RUNNER_PLIST
    if not plist.exists():
        out.append(PatchState("wda-photo-permission", False, f"{RUNNER_PLIST} is missing"))
    else:
        keys = plistlib.loads(plist.read_bytes())
        out.append(PatchState(
            "wda-photo-permission", PHOTO_USAGE_KEY in keys,
            "the runner declares why it wants to add photos" if PHOTO_USAGE_KEY in keys
            else f"{PHOTO_USAGE_KEY} is missing, so iOS would kill the runner on first use",
        ))
    return out


def apply(src: Path) -> list[PatchState]:
    """Add every missing patch. Safe to run repeatedly."""
    commands = src / COMMANDS
    if commands.exists():
        before = commands.read_text()
        after = _patch_commands(before)
        if after != before:
            commands.write_text(after)
        if not _commands_applied(commands.read_text()):
            raise ValueError(f"{COMMANDS}: the patch wrote but did not take")

    plist = src / RUNNER_PLIST
    if plist.exists():
        keys = plistlib.loads(plist.read_bytes())
        if keys.get(PHOTO_USAGE_KEY) != PHOTO_USAGE_TEXT:
            keys[PHOTO_USAGE_KEY] = PHOTO_USAGE_TEXT
            plist.write_bytes(plistlib.dumps(keys))
    return status(src)


def revert(src: Path) -> None:
    """Put the checkout back to upstream, for bisecting a WebDriverAgent bug."""
    commands = src / COMMANDS
    if commands.exists():
        text = commands.read_text()
        text = text.replace(_PHOTOS_IMPORT, _IMPORT_ANCHOR, 1)
        text = text.replace(_MEDIA_ROUTES, "", 1)
        text = text.replace(_SESSIONLESS_BUTTON, "", 1)
        text = text.replace(_MEDIA_HANDLER, "", 1)
        commands.write_text(text)
    plist = src / RUNNER_PLIST
    if plist.exists():
        keys = plistlib.loads(plist.read_bytes())
        keys.pop(PHOTO_USAGE_KEY, None)
        plist.write_bytes(plistlib.dumps(keys))
