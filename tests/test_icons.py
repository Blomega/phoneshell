"""Glyph matching finds the right glyph in the right place.

Synthetic screens, because the property under test is geometric: a control
drawn at (300, 700) has to come back at (300, 700) in points, whatever the
screenshot's pixel density, and a glyph that is not on the screen has to come
back not at all.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from phoneshell.perception import icons

pytestmark = pytest.mark.skipif(not icons.AVAILABLE, reason="needs numpy")


def screen(placements, size=(390, 844), bg=(255, 255, 255), fg=(20, 20, 20), scale=1.0):
    """A blank screen with glyphs pasted at point coordinates."""
    img = Image.new("RGB", (int(size[0] * scale), int(size[1] * scale)), bg)
    for name, cx, cy, s in placements:
        px = int(s * scale)
        glyph = icons.render(name, px)
        tinted = Image.new("RGB", (px, px), fg)
        img.paste(tinted, (int(cx * scale - px / 2), int(cy * scale - px / 2)), glyph)
    return img


def found(hits, name):
    return [h for h in hits if h.name == name]


def test_finds_a_single_glyph_where_it_was_drawn():
    img = screen([("heart", 300, 700, 26)])
    hits = icons.find(img)
    hearts = found(hits, "heart")
    assert hearts, [(h.name, round(h.score, 2)) for h in hits]
    best = max(hearts, key=lambda h: h.score)
    assert abs(best.cx - 300) <= 6, best
    assert abs(best.cy - 700) <= 6, best


def test_reports_point_space_on_a_retina_screenshot():
    img = screen([("send", 340, 120, 30)], scale=3.0)
    hits = icons.find(img, scale=3.0)
    sends = found(hits, "send")
    assert sends, [(h.name, round(h.score, 2)) for h in hits]
    best = max(sends, key=lambda h: h.score)
    assert abs(best.cx - 340) <= 8, best
    assert abs(best.cy - 120) <= 8, best


def test_light_glyph_on_dark_background_still_matches():
    img = screen([("play", 195, 400, 36)], bg=(10, 10, 12), fg=(250, 250, 250))
    assert found(icons.find(img), "play")


def test_blank_screen_finds_nothing():
    assert icons.find(Image.new("RGB", (390, 844), (255, 255, 255))) == []


def test_does_not_hallucinate_an_absent_glyph():
    img = screen([("heart", 100, 100, 26), ("close", 300, 100, 26)])
    names = {h.name for h in icons.find(img)}
    assert "heart" in names and "close" in names
    assert "camera" not in names and "person" not in names


def test_several_glyphs_on_one_screen():
    placed = [("heart", 340, 500, 28), ("bookmark", 340, 580, 28), ("send", 340, 660, 28)]
    hits = icons.find(screen(placed))
    for name, cx, cy, _ in placed:
        matches = found(hits, name)
        assert matches, f"{name} missing from {[(h.name, round(h.score,2)) for h in hits]}"
        best = max(matches, key=lambda h: h.score)
        assert abs(best.cx - cx) <= 8 and abs(best.cy - cy) <= 8, (name, best)


def test_suppression_returns_one_hit_per_glyph_not_a_blob():
    hits = icons.find(screen([("star", 195, 300, 30)]))
    near = [h for h in hits if abs(h.cx - 195) < 20 and abs(h.cy - 300) < 20]
    assert len(near) == 1, near
