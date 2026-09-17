"""The WebDriverAgent patches apply cleanly, twice, and refuse to guess.

A patch that half-applies produces a runner that builds and then 404s at the one
moment it is needed, so the properties worth pinning are: idempotence, exact
round-trip on revert, and a loud refusal when upstream has moved the anchor
rather than a plausible insertion somewhere else.
"""
from __future__ import annotations

import plistlib

import pytest

from phoneshell.wda import patches

UPSTREAM_COMMANDS = '''#import "FBCustomCommands.h"

#import "FBRoute.h"

@implementation FBCustomCommands

+ (NSArray *)routes
{
  return
  @[
    [[FBRoute POST:@"/wda/homescreen"].withoutSession respondWithTarget:self action:@selector(handleHomescreenCommand:)],
    [[FBRoute POST:@"/wda/pressButton"] respondWithTarget:self action:@selector(handlePressButtonCommand:)],
  ];
}

+ (id<FBResponsePayload>)handleSetPasteboard:(FBRouteRequest *)request
{
  return FBResponseWithOK();
}

@end
'''


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "WebDriverAgentLib" / "Commands").mkdir(parents=True)
    (tmp_path / "WebDriverAgentRunner").mkdir(parents=True)
    (tmp_path / patches.COMMANDS).write_text(UPSTREAM_COMMANDS)
    (tmp_path / patches.RUNNER_PLIST).write_bytes(
        plistlib.dumps({"CFBundleName": "$(PRODUCT_NAME)"})
    )
    return tmp_path


def test_a_stock_checkout_reports_unpatched(checkout):
    assert not any(s.applied for s in patches.status(checkout))


def test_apply_adds_every_piece(checkout):
    assert all(s.applied for s in patches.apply(checkout))
    text = (checkout / patches.COMMANDS).read_text()
    assert "@import Photos;" in text
    assert "/wda/importMedia" in text
    assert "handleImportMedia:(FBRouteRequest" in text
    assert ".withoutSession respondWithTarget:self action:@selector(handlePressButtonCommand:)" in text
    keys = plistlib.loads((checkout / patches.RUNNER_PLIST).read_bytes())
    assert keys[patches.PHOTO_USAGE_KEY] == patches.PHOTO_USAGE_TEXT


def test_applying_twice_changes_nothing(checkout):
    patches.apply(checkout)
    once = (checkout / patches.COMMANDS).read_text()
    patches.apply(checkout)
    assert (checkout / patches.COMMANDS).read_text() == once
    assert once.count("handleImportMedia:(FBRouteRequest") == 1
    assert once.count("@import Photos;") == 1


def test_the_session_route_is_kept_alongside_the_sessionless_one(checkout):
    patches.apply(checkout)
    text = (checkout / patches.COMMANDS).read_text()
    assert '[[FBRoute POST:@"/wda/pressButton"] respondWithTarget' in text


def test_revert_restores_upstream_exactly(checkout):
    patches.apply(checkout)
    patches.revert(checkout)
    assert (checkout / patches.COMMANDS).read_text() == UPSTREAM_COMMANDS
    assert patches.PHOTO_USAGE_KEY not in plistlib.loads(
        (checkout / patches.RUNNER_PLIST).read_bytes()
    )


def test_a_moved_anchor_refuses_rather_than_guessing(checkout):
    (checkout / patches.COMMANDS).write_text(
        UPSTREAM_COMMANDS.replace("handlePressButtonCommand:", "handleButtonPressCommand:")
    )
    with pytest.raises(ValueError, match="WebDriverAgent has moved on"):
        patches.apply(checkout)
