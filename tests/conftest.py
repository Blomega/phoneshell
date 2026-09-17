"""Shared fixtures.

Everything here runs without a phone. The device-dependent behaviour is covered
by the benchmark; these tests cover the parts that are pure functions of an
image or a tree, which is where the subtle bugs live.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
