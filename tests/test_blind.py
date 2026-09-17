"""Pixel recovery only fires when the tree has failed, and says that it guessed.

The two ways this feature can do harm are both tested here: charging every
healthy screen 135ms it does not need, and presenting a guessed target as if
the app had named it.
"""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from phoneshell.agent.observation import build, to_tsv
from phoneshell.config import BlindConfig
from phoneshell.perception import blind, icons, ocr
from phoneshell.perception.tree import Element

FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def png_of(draw_ops, size=(390, 844), scale=1.0, bg=(255, 255, 255)) -> bytes:
    img = Image.new("RGB", (int(size[0] * scale), int(size[1] * scale)), bg)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, int(24 * scale))
    for kind, *rest in draw_ops:
        if kind == "text":
            text, x, y = rest
            d.text((x * scale, y * scale), text, fill=(10, 10, 10), font=font)
        elif kind == "icon":
            name, cx, cy, s = rest
            px = int(s * scale)
            glyph = icons.render(name, px)
            img.paste(Image.new("RGB", (px, px), (10, 10, 10)),
                      (int(cx * scale - px / 2), int(cy * scale - px / 2)), glyph)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def opaque_tree():
    """What a Flutter or WebView screen actually hands XCUITest."""
    return [Element(idx=1, type="Other", label="", x=0, y=0, w=390, h=844)]


def healthy_tree():
    return [
        Element(idx=i, type="Button", label=f"Row {i}", x=16, y=100 + i * 44, w=358, h=40)
        for i in range(1, 12)
    ]


@pytest.mark.skipif(not ocr.AVAILABLE, reason="needs Vision")
def test_recovers_text_the_tree_never_mentioned():
    png = png_of([("text", "Checkout", 40, 300), ("text", "Cancel", 40, 400)])
    rec = blind.recover(png, opaque_tree(), scale=1.0)
    labels = " ".join(e.label.lower() for e in rec.elements)
    assert "checkout" in labels and "cancel" in labels
    assert all(e.source == "pixel" for e in rec.elements)


@pytest.mark.skipif(not ocr.AVAILABLE, reason="needs Vision")
def test_recovered_targets_are_in_point_space_on_a_retina_shot():
    png = png_of([("text", "Checkout", 40, 300)], scale=3.0)
    rec = blind.recover(png, opaque_tree(), scale=3.0)
    hit = next(e for e in rec.elements if "checkout" in e.label.lower())
    assert 280 <= hit.cy <= 340, hit
    assert 40 <= hit.cx <= 240, hit


def test_ids_continue_from_the_tree_and_never_collide():
    png = png_of([("icon", "send", 340, 700, 30)])
    tree = healthy_tree()
    rec = blind.recover(png, tree, scale=1.0)
    if not rec.elements:
        pytest.skip("nothing recovered from this synthetic screen")
    assert min(e.idx for e in rec.elements) > max(e.idx for e in tree)
    combined = tree + rec.elements
    assert len({e.idx for e in combined}) == len(combined)


def test_does_not_offer_a_control_the_tree_already_named():
    png = png_of([("text", "Row 3", 16, 230)])
    named = [Element(idx=1, type="Button", label="Row 3", x=16, y=220, w=200, h=44)]
    rec = blind.recover(png, named, scale=1.0, want_shapes=False, want_icons=False)
    assert not [e for e in rec.elements if "row 3" in e.label.lower()]


def test_a_healthy_tree_pays_nothing():
    png = png_of([("text", "Checkout", 40, 300)])
    obs = build(step=1, bundle_id="x", app_name="X", elements=healthy_tree(), png=png,
                scale=1.0, screen_w=390, screen_h=844, blind=BlindConfig())
    assert all(e.source == "tree" for e in obs.elements)
    assert not obs.som


@pytest.mark.skipif(not ocr.AVAILABLE, reason="needs Vision")
def test_an_opaque_tree_gets_pixels_and_is_told_so():
    png = png_of([("text", "Checkout", 40, 300), ("text", "Cancel", 40, 400)])
    obs = build(step=1, bundle_id="x", app_name="X", elements=opaque_tree(), png=png,
                scale=1.0, screen_w=390, screen_h=844, blind=BlindConfig())
    assert obs.som
    pixel = [e for e in obs.elements if e.source == "pixel"]
    assert pixel, obs.as_text()
    assert "read from the pixels" in " ".join(obs.notes)
    assert "pixel:" in to_tsv(pixel)


def test_disabled_config_recovers_nothing():
    png = png_of([("text", "Checkout", 40, 300)])
    obs = build(step=1, bundle_id="x", app_name="X", elements=opaque_tree(), png=png,
                scale=1.0, screen_w=390, screen_h=844, blind=BlindConfig(enabled=False))
    assert all(e.source == "tree" for e in obs.elements)


def test_perception_failure_never_breaks_the_step(monkeypatch):
    monkeypatch.setattr(blind, "recover", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    png = png_of([("text", "Checkout", 40, 300)])
    obs = build(step=1, bundle_id="x", app_name="X", elements=opaque_tree(), png=png,
                scale=1.0, screen_w=390, screen_h=844, blind=BlindConfig())
    assert obs.elements == opaque_tree() or all(e.source == "tree" for e in obs.elements)


def test_a_repeating_background_pattern_is_not_offered_as_controls():
    """Measured on a WebGL page whose backdrop is a field of small crosses."""
    from phoneshell.perception.blind import _drop_wallpaper
    texture = [(x * 75.0, y * 30.0 + 700, 12.0, 12.0) for y in range(4) for x in range(5)]
    assert _drop_wallpaper(texture) == []


def test_a_tab_bar_survives_the_pattern_filter():
    """Five similar shapes on one row is a tab bar, not wallpaper."""
    from phoneshell.perception.blind import _drop_wallpaper
    tab_bar = [(x * 88.0 + 20, 900.0, 26.0, 26.0) for x in range(5)]
    assert len(_drop_wallpaper(tab_bar)) == 5
