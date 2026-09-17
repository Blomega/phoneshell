"""Vision text boxes land where the text actually is.

The y flip is the whole risk: Vision reports a normalised box with the origin at
the bottom left, and a silent sign error there puts every blind-mode tap target
in the mirror image of its real position, which reads as "the agent taps the
wrong row" rather than as a crash.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageFont

from phoneshell.perception import ocr

pytestmark = pytest.mark.skipif(not ocr.AVAILABLE, reason="needs pyobjc Vision bindings")

FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"


def canvas(lines: list[tuple[str, int, int]], size=(390, 844)) -> Image.Image:
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 28)
    for text, x, y in lines:
        draw.text((x, y), text, fill=(0, 0, 0), font=font)
    return img


def test_box_is_near_the_drawn_text():
    img = canvas([("Checkout", 40, 100)])
    boxes = ocr.read_boxes(img)
    hit = next((b for b in boxes if "checkout" in b.text.lower()), None)
    assert hit is not None, [b.text for b in boxes]
    # Drawn at y=100 with a 28px font, so the line centre sits near y=115.
    assert 90 <= hit.cy <= 140, hit
    assert 40 <= hit.cx <= 200, hit


def test_top_text_reports_a_smaller_y_than_bottom_text():
    img = canvas([("Top", 40, 60), ("Bottom", 40, 700)])
    boxes = {b.text.lower(): b for b in ocr.read_boxes(img)}
    assert "top" in boxes and "bottom" in boxes, list(boxes)
    assert boxes["top"].cy < boxes["bottom"].cy
    assert boxes["bottom"].cy > 600


def test_blank_image_reads_nothing():
    assert ocr.read_boxes(Image.new("RGB", (390, 844), (255, 255, 255))) == []
