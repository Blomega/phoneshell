"""push_media rejects what the phone would reject, before touching the phone.

Every one of these returns a sentence a person can act on rather than a Photos
error code surfacing three layers up.
"""
from __future__ import annotations

import pytest

from phoneshell.actions import Phone


class FakeWDA:
    def __init__(self):
        self.calls = []

    def import_media(self, payload, name, kind=None):
        self.calls.append((len(payload), name, kind))
        return {"name": name, "bytes": len(payload), "kind": kind}


@pytest.fixture
def phone(monkeypatch):
    p = Phone.__new__(Phone)
    p.wda = FakeWDA()
    monkeypatch.setattr(Phone, "_after",
                        lambda self, action, detail, before=None, **kw:
                        type("R", (), {"ok": True, "action": action, "detail": detail,
                                       "error": "", "data": {}})())
    return p


def test_missing_file(phone, tmp_path):
    result = phone.push_media(tmp_path / "nope.jpg")
    assert not result.ok and "not a file" in result.error


def test_rejects_a_format_ios_will_not_take(phone, tmp_path):
    f = tmp_path / "notes.pdf"
    f.write_bytes(b"%PDF-1.4")
    result = phone.push_media(f)
    assert not result.ok
    assert ".pdf" in result.error and ".jpg" in result.error
    assert phone.wda.calls == []


def test_rejects_an_empty_file(phone, tmp_path):
    f = tmp_path / "empty.jpg"
    f.write_bytes(b"")
    result = phone.push_media(f)
    assert not result.ok and "empty" in result.error


def test_rejects_something_too_big_without_reading_it(phone, tmp_path, monkeypatch):
    f = tmp_path / "huge.mp4"
    f.write_bytes(b"\0" * 16)
    monkeypatch.setattr(Phone, "PUSH_MAX_BYTES", 8)
    result = phone.push_media(f)
    assert not result.ok and "limit" in result.error
    assert phone.wda.calls == []


@pytest.mark.parametrize("name,kind", [
    ("a.jpg", "photo"), ("b.PNG", "photo"), ("c.heic", "photo"),
    ("d.mov", "video"), ("e.mp4", "video"),
])
def test_kind_follows_the_extension(phone, tmp_path, name, kind):
    f = tmp_path / name
    f.write_bytes(b"x" * 32)
    assert phone.push_media(f).ok
    assert phone.wda.calls[-1] == (32, name, kind)
