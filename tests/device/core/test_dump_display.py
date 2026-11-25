"""Tests for the display a dump reports (size, rotation, override)."""

from __future__ import annotations

import pytest

from tests.device.core.helpers import (
    script_dump,
)
from tests.fake_adb_server import Fail
from uiprobe.device import Device


def dialog_xml(rotation: object = "0") -> bytes:
    """A dump that holds only a dialog rooted at [100,200][900,600]."""
    return (
        f'<?xml version="1.0"?><hierarchy rotation="{rotation}">'
        '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
        'package="com.android.systemui" content-desc="" bounds="[100,200][900,600]"/>'
        "</hierarchy>"
    ).encode()


PHYSICAL = b"Physical size: 1080x2400\n"


def test_dump_display_is_the_device_size_not_the_dialog_extent(phone):
    script_dump(phone, xml=dialog_xml(), wm_size=PHYSICAL)
    assert Device(adb_server=phone.address).dump().display == (1080, 2400)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        ("0", (1080, 2400)),
        ("1", (2400, 1080)),
        ("2", (1080, 2400)),
        ("3", (2400, 1080)),
        ("9", (1080, 2400)),
        ("x", (1080, 2400)),
        ("", (1080, 2400)),
    ],
)
def test_dump_display_follows_the_rotation(phone, rotation, expected):
    script_dump(phone, xml=dialog_xml(rotation), wm_size=PHYSICAL)
    assert Device(adb_server=phone.address).dump().display == expected


def test_dump_display_without_a_rotation_attribute_is_unrotated(phone):
    xml = dialog_xml().replace(b' rotation="0"', b"")
    script_dump(phone, xml=xml, wm_size=PHYSICAL)
    assert Device(adb_server=phone.address).dump().display == (1080, 2400)


def test_dump_display_prefers_the_override_size(phone):
    script_dump(
        phone,
        xml=dialog_xml("1"),
        wm_size=PHYSICAL + b"Override size: 720x1600\n",
    )
    assert Device(adb_server=phone.address).dump().display == (1600, 720)


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"garbage\n",
        b"Physical size: 1080x\n",
        b"Physical size: 0x2400\n",
        b"Physical size: 1080x240000\n",
        b"Physical size: \xd9\xa1080x2400\n",
        b"Physical size: 1080x2400\nPhysical size: 720x1600\n",
        b"Physical size: 1080x2400\nsomething else\n",
        b"Override size: 720x1600 extra\n",
        b"/system/bin/sh: wm: not found\n",
    ],
)
def test_dump_display_falls_back_when_wm_size_is_unusable(phone, output):
    script_dump(phone, xml=dialog_xml(), wm_size=output)
    assert Device(adb_server=phone.address).dump().display == (900, 600)


def test_dump_display_falls_back_when_wm_size_fails(phone):
    script_dump(phone, xml=dialog_xml(), wm_size=Fail("closed"))
    assert Device(adb_server=phone.address).dump().display == (900, 600)
