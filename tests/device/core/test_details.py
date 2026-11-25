"""Tests for `device_details`."""

from __future__ import annotations

from tests.device.core.helpers import (
    SERIAL,
)
from tests.fake_adb_server import Fail
from uiprobe.device import device_details

# --- device_details ---


def test_device_details_uses_one_listing_for_every_device(phone):
    phone.add_device("R58M123", model="SM_G991")
    phone.add_device("off", "offline")
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", b"1\n")
    phone.on_shell("R58M123", "getprop ro.build.version.sdk", b"34\n")
    found = device_details(adb_server=phone.address)
    assert [(d.info.serial, d.api_level, d.emulator) for d in found] == [
        (SERIAL, 36, True),
        ("R58M123", 34, False),
        ("off", None, None),
    ]
    assert phone.requests.count("host:devices-l") == 1
    assert phone.requests.count("host:version") == 0


def test_device_details_properties_fail_independently(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", Fail("closed"))
    (details,) = device_details(adb_server=phone.address)
    assert (details.api_level, details.emulator) == (36, None)
    phone._shell[SERIAL].clear()
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", Fail("closed"))
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", b"1\n")
    (details,) = device_details(adb_server=phone.address)
    assert (details.api_level, details.emulator) == (None, True)


def test_device_details_survives_a_device_that_vanishes(phone):
    phone.fail_request("host:transport:" + SERIAL, "device offline")
    (details,) = device_details(adb_server=phone.address)
    assert (details.info.serial, details.api_level, details.emulator) == (
        SERIAL,
        None,
        None,
    )
