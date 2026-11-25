"""Tests for a dump that loses the device or the server after the XML is read."""

from __future__ import annotations

import pytest

from tests.device.core.helpers import (
    script_dump,
)
from uiprobe.device import Device


def _after_xml_is_read(monkeypatch, action):
    """Run `action` once the XML has been read, before the metadata lookups."""
    real = Device._dump_xml

    def dump_xml(self):
        xml = real(self)
        action()
        return xml

    monkeypatch.setattr(Device, "_dump_xml", dump_xml)


def test_dump_keeps_the_screen_when_the_server_stops_after_the_xml(phone, monkeypatch):
    script_dump(phone)
    device = Device(adb_server=phone.address, start_server=False)
    _after_xml_is_read(monkeypatch, phone.stop)
    screen = device.dump()
    assert (screen.activity, screen.api_level) == (None, None)
    assert screen.package == "com.example.shop"
    assert len(screen.elements) > 0


@pytest.mark.parametrize("state", ["offline", "unauthorized"])
def test_dump_keeps_the_screen_when_the_device_changes_state_after_the_xml(
    phone, monkeypatch, state
):
    script_dump(phone)
    device = Device(adb_server=phone.address)

    def change_state():
        phone.devices[0]["state"] = state

    _after_xml_is_read(monkeypatch, change_state)
    screen = device.dump()
    assert (screen.activity, screen.api_level) == (None, None)
    assert len(screen.elements) > 0


def test_dump_keeps_the_screen_when_the_device_disappears_after_the_xml(
    phone, monkeypatch
):
    script_dump(phone)
    device = Device(adb_server=phone.address)
    _after_xml_is_read(monkeypatch, phone.devices.clear)
    screen = device.dump()
    assert (screen.activity, screen.api_level) == (None, None)
    assert len(screen.elements) > 0


def test_api_level_is_none_when_the_server_or_device_is_gone(phone):
    script_dump(phone)
    device = Device(adb_server=phone.address, start_server=False)
    later = Device(adb_server=phone.address, start_server=False)
    phone.devices[0]["state"] = "offline"
    assert device.api_level() is None
    phone.devices[0]["state"] = "device"
    assert device.api_level() == 36
    phone.stop()
    assert later.api_level() is None
