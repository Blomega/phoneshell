"""Measure, per screen, what is visible but NOT in the accessibility tree.

Experiment 2 needs tasks whose answer exists only as pixels. Inventing them by
intuition is how experiment 1 built a "vision" arm that was not one, so this
decides it mechanically instead.

The method: read the same screen twice, once through the accessibility tree and
once through Vision OCR on the screenshot, and diff the words. Anything OCR reads
that the tree does not contain is **render-only content**, and a task whose answer
lives there is genuinely render-dependent. Anything the tree carries is
tree-sufficient by definition, however visual it looks.

This also answers a question worth answering on its own: how completely does iOS
describe its own screens? If the gap is near zero across Apple's apps, then the
honest finding is that first-party iOS UI is fully described by its tree, and
vision cannot help there no matter how the tasks are written.

    ./.venv/bin/python scripts/render_gap.py Settings Clock Calculator Safari
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phoneshell.actions import Phone                      # noqa: E402
from phoneshell.perception import ocr                     # noqa: E402
from phoneshell.perception.screen import load_image       # noqa: E402

# Words that carry no task content: the clock, the carrier, single glyphs.
NOISE = re.compile(r"^(am|pm|[0-9]{1,2}:[0-9]{2}|wifi|5g|lte|[^a-z0-9]{1,2})$", re.I)
STATUS_BAR_FRACTION = 0.045          # ignore OCR hits in the status bar


def words(text: str) -> set[str]:
    return {w for w in re.split(r"[^A-Za-z0-9%+.:-]+", text.lower())
            if len(w) > 1 and not NOISE.match(w)}


def gap_for_screen(phone: Phone, label: str) -> dict:
    """Everything OCR can read on this screen that the tree cannot."""
    snap = phone.snapshot(with_screenshot=True, stable=True)
    tree_words: set[str] = set()
    for e in snap.elements:
        for field in (e.label, e.name, e.value, e.placeholder, e.identifier):
            if field:
                tree_words |= words(field)
        for c in e.children_text:
            tree_words |= words(c)

    seen = ocr.read_text(load_image(snap.png))
    ocr_words: set[str] = set()
    for line in seen:
        ocr_words |= words(line)

    # OCR noise looks exactly like render-only content unless it is filtered, and
    # counting it produces a "vision stratum" made of artifacts, which is the
    # mistake that ruined experiment 1 wearing a different hat. Three filters,
    # each for a failure seen in the first survey:
    #   "ecuri" / "rivacy" / "oca"  -> fragments of words the tree DOES have
    #   "29h" / "39m"               -> a Live Activity in the Dynamic Island
    #   "ac"                        -> a glyph the tree labels differently
    def is_fragment(w: str) -> bool:
        return any(w in t for t in tree_words if len(t) > len(w))

    LIVE_ACTIVITY = re.compile(r"^(\d+[hms])+$")   # 29h, 39m, and 29h41m
    missing = sorted(w for w in (ocr_words - tree_words)
                     if len(w) >= 4
                     and not LIVE_ACTIVITY.match(w)
                     and not is_fragment(w))
    covered = len(ocr_words & tree_words)
    total = len(ocr_words) or 1
    return {
        "screen": label,
        "raw_gap": len(ocr_words - tree_words),
        "app": snap.app_name,
        "elements": len(snap.elements),
        "ocr_words": len(ocr_words),
        "in_tree": covered,
        "render_only": missing,
        "coverage": covered / total,
    }


def main() -> int:
    # --private measures third-party apps, which hold the owner's messages and
    # mail. It reports the COUNTS only and never a word, so the statistic can be
    # published while the content stays on the phone. OCR runs locally either
    # way; nothing is uploaded and no screenshot is written to disk.
    private = "--private" in sys.argv
    apps = [a for a in sys.argv[1:] if not a.startswith("--")] or [
        "Settings", "Clock", "Calculator", "Weather", "Safari"]
    phone = Phone()
    phone.ensure_unlocked()
    rows = []
    for app in apps:
        r = phone.open_app(app)
        if not r.ok:
            print(f"  skip {app}: {r.error or r.detail}")
            continue
        time.sleep(2.0)
        row = gap_for_screen(phone, app)
        rows.append(row)
        print(f"\n{app}  ({row['app']}, {row['elements']} elements)")
        print(f"  OCR reads {row['ocr_words']} words, the tree has {row['in_tree']} "
              f"of them  ->  {row['coverage']:.0%} covered")
        print(f"  raw gap {row['raw_gap']}, of which {len(row['render_only'])} survive "
              f"the noise filters")
        if private:
            print(f"  render-only words: {len(row['render_only'])} "
                  f"(content withheld: this is a third-party app)")
        elif row["render_only"]:
            print(f"  render-only ({len(row['render_only'])}): "
                  f"{', '.join(row['render_only'][:14])}")
        else:
            print("  render-only: none. This screen is fully described by its tree.")
        phone.home()
        time.sleep(1.0)

    if rows:
        mean = sum(r["coverage"] for r in rows) / len(rows)
        total_missing = sum(len(r["render_only"]) for r in rows)
        print(f"\n{'=' * 66}")
        print(f"across {len(rows)} screens the tree covers {mean:.0%} of readable text; "
              f"{total_missing} render-only words in total")
        if mean > 0.9 and total_missing < 5:
            print("VERDICT: first-party iOS is essentially fully described by its "
                  "accessibility tree.\n         Render-dependent tasks cannot be "
                  "constructed here, and that is itself\n         the finding: vision has "
                  "nothing to add on these screens by construction.")
        else:
            print("VERDICT: there is real render-only content. Tasks targeting these words "
                  "are\n         genuinely render-dependent and belong in the vision "
                  "stratum.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
