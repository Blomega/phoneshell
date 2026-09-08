"""Local text recognition, through macOS Vision.

Used by the consistency gate to answer one question: does the element the model
just named actually display that text, right now, in pixels? Vision runs on the
Mac, offline, in a few tens of milliseconds for a small crop, so this costs
nothing per step and never leaves the machine.

Degrades to "inconclusive" rather than to a wrong answer: if the bindings are
missing or Vision returns nothing, callers treat the check as not-run instead of
as a failure, because plenty of real controls are icons with no text at all.
"""
from __future__ import annotations

import io
import re
from functools import lru_cache

from PIL import Image

try:  # pyobjc is optional; everything still works without it
    import Quartz
    import Vision
    from Foundation import NSData
    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

_WORD = re.compile(r"[a-z0-9]+")
# Words too generic to prove anything if they happen to match.
_STOP = {"the", "a", "an", "to", "of", "and", "or", "in", "on", "for", "is", "it", "your", "you"}


def _cgimage(img: Image.Image):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    raw = buf.getvalue()
    data = NSData.dataWithBytes_length_(raw, len(raw))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    if source is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)


def read_text(img: Image.Image, fast: bool = True) -> list[str]:
    """Every string Vision can find in the image."""
    if not AVAILABLE:
        return []
    if img.width < 8 or img.height < 8:
        img = img.resize((max(img.width, 8) * 4, max(img.height, 8) * 4), Image.LANCZOS)
    elif img.width < 40 or img.height < 20:
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    cg = _cgimage(img)
    if cg is None:
        return []
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(1 if fast else 0)
    request.setUsesLanguageCorrection_(False)
    ok, _err = handler.performRequests_error_([request], None)
    if not ok:
        return []
    found: list[str] = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates and len(candidates):
            found.append(str(candidates[0].string()))
    return found


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def label_matches(claimed: str, seen: list[str]) -> tuple[bool | None, str]:
    """Does the claimed label appear in what Vision read?

    Returns (True, why) match, (False, why) contradiction, (None, why) inconclusive.
    """
    if not AVAILABLE:
        return None, "Vision bindings are not installed"
    if not claimed.strip():
        return None, "the element has no text to verify"
    if not seen:
        return None, "no text found in the crop, which is normal for an icon"
    # Glyph-only controls (chevrons, arrows, dots) OCR as one or two junk
    # characters. There is nothing to verify against, so do not pretend there is.
    if all(len(t.strip()) < 3 for t in seen):
        return None, f"only glyph-sized text in the crop ({seen[:3]}), nothing to verify"
    claim_tokens = set(list(tokens(claimed))[:8])
    if not claim_tokens:
        return None, "the label has no distinctive words"
    seen_tokens = tokens(" ".join(seen))
    overlap = claim_tokens & seen_tokens
    coverage = len(overlap) / len(claim_tokens)

    # Any-token overlap is far too weak. "Place order" and "Cancel order" share
    # the word that does not matter and differ on the word that does, which is
    # exactly the confusion this gate exists to catch.
    if coverage >= 0.6:
        return True, f"pixels show {sorted(overlap)[:3]}"
    if len(claim_tokens) > 4 and len(overlap) >= 2:
        # Long labels get clipped by the crop; two solid hits is enough.
        return True, f"pixels show {sorted(overlap)[:3]} of a long label"
    if len(claim_tokens) <= 2 and overlap:
        return False, (f"pixels read {seen[:3]}, which shares only {sorted(overlap)} with the "
                       f"claimed {claimed[:60]!r}")
    if not overlap:
        return False, f"pixels read {seen[:3]} but the element claims {claimed[:60]!r}"
    return None, f"partial match ({int(coverage * 100)}% of the label), inconclusive"
