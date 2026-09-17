"""Slots are stable, and the configured phone always holds slot 0.

The property that matters is that a device's ports do not move. A phone that
gets 8100 today and 8103 tomorrow invalidates every terminal, log line and
bookmark that referred to it, and the person debugging it has no way to know
that is what happened.
"""
from __future__ import annotations

import json

import pytest

from phoneshell import farm as farm_module
from phoneshell.farm import Farm


def default_phone(monkeypatch, udid):
    """Pretend runtime/config.yaml names this phone as the default.

    A real Config each time, not a stub: config_for() mutates wda.port and
    wda.mjpeg_port on it, and a stub that happens to lack those fields tests the
    stub rather than the code.
    """
    from phoneshell.config import Config

    def load():
        cfg = Config()
        cfg.device.udid = udid
        return cfg

    monkeypatch.setattr(farm_module.Config, "load", staticmethod(load))


@pytest.fixture
def clean(tmp_path, monkeypatch):
    monkeypatch.setattr(farm_module, "SLOTS_PATH", tmp_path / "farm-slots.json")
    default_phone(monkeypatch, "AAA")
    return tmp_path


def test_configured_phone_holds_slot_zero(clean):
    f = Farm()
    assert f.slot_for("AAA") == 0


def test_other_phones_get_their_own_slots(clean):
    f = Farm()
    f.slot_for("AAA")
    assert f.slot_for("BBB") == 1
    assert f.slot_for("CCC") == 2
    assert f.slot_for("BBB") == 1, "a slot must not move once handed out"


def test_slots_survive_a_restart(clean):
    first = Farm()
    first.slot_for("AAA")
    first.slot_for("BBB")
    first.slot_for("CCC")
    assert Farm().slot_for("CCC") == 2


def test_ports_follow_the_slot(clean):
    f = Farm()
    f.slot_for("AAA")
    cfg = f.config_for("BBB")
    assert (cfg.wda.port, cfg.wda.mjpeg_port) == (8101, 9101)
    assert cfg.device.udid == "BBB"


def test_changing_the_default_phone_takes_slot_zero_back(clean, monkeypatch):
    f = Farm()
    f.slot_for("AAA")
    f.slot_for("BBB")
    # The config now names a different phone as the default.
    default_phone(monkeypatch, "BBB")
    assert f.slot_for("BBB") == 0
    assert f.slot_for("AAA") != 0, "the old default must not keep 8100"


def test_a_full_farm_refuses_rather_than_walking_into_ephemeral_ports(clean):
    """Slot 0 belongs to the default phone, so there are MAX_SLOTS - 1 to hand out."""
    f = Farm()
    for i in range(farm_module.MAX_SLOTS - 1):
        f.slot_for(f"udid-{i}")
    assert max(f._slots.values()) == farm_module.MAX_SLOTS - 1
    with pytest.raises(RuntimeError, match="full"):
        f.slot_for("one-too-many")
