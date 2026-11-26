"""Tests for the crawl's private wheel input: `Device._wheel_scroll` and
`Device._wheel_supported`. The public `scroll` and `swipe` are untouched."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.fake_device import El, FakeAndroid, Screen
from uiprobe.device import (
    MAX_WHEEL_AMOUNT,
    PIN_NOT_PROVEN,
    SEND_STOPPED,
    Device,
    _monotonic,
)
from uiprobe.errors import DeviceNotFoundError

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SUFFIX = "; echo uiprobe-exit:$?"
SCREENS = (Screen("main", "com.example.shop/.Main", (El((0, 0, 320, 640), text="x"),)),)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", wheel=True)


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


def sent(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands if c.startswith("input ")]


def test_the_wheel_command_is_exact_and_sent_once(device, android, fake_adb):
    assert device._wheel_scroll((160, 320), 3) == (160, 320)
    assert sent(fake_adb) == ["input mouse scroll 160 320 --axis VSCROLL,-3" + SUFFIX]
    assert [(i.kind, i.args) for i in android.inputs] == [
        ("wheel", ("mouse", "scroll", "160", "320", "--axis", "VSCROLL,-3"))
    ]


@pytest.mark.parametrize("amount", [1, 2, MAX_WHEEL_AMOUNT])
def test_the_sign_is_always_negative(device, fake_adb, amount):
    device._wheel_scroll((1, 2), amount)
    assert sent(fake_adb)[-1].endswith(f"VSCROLL,-{amount}{SUFFIX}")


@pytest.mark.parametrize(
    ("amount", "error"),
    [
        (0, ValueError),
        (11, ValueError),
        (-3, ValueError),
        (1.5, TypeError),
        (True, TypeError),
        ("3", TypeError),
    ],
)
def test_a_bad_amount_sends_nothing(device, android, amount, error):
    with pytest.raises(error):
        device._wheel_scroll((1, 2), amount)
    assert android.inputs == []


def test_a_bad_point_sends_nothing(device, android):
    with pytest.raises(ValueError, match="coordinate"):
        device._wheel_scroll((-1, 2), 3)
    with pytest.raises(TypeError):
        device._wheel_scroll((1.5, 2), 3)
    assert android.inputs == []


def test_it_goes_with_the_approved_epoch_and_time(device, android):
    device._wheel_scroll(
        (1, 2), 3, _send_by=_monotonic() + 60.0, _epoch=device.pin_epoch
    )
    assert len(android.inputs) == 1


def test_it_is_not_sent_when_the_device_was_proven_again_since(device, android):
    with pytest.raises(DeviceNotFoundError) as info:
        device._wheel_scroll((1, 2), 3, _epoch=device.pin_epoch - 1)
    assert info.value.details["reason"] == PIN_NOT_PROVEN
    assert android.inputs == []


def test_it_is_not_sent_after_the_time_it_had_to_go_out_by(device, android):
    with pytest.raises(DeviceNotFoundError) as info:
        device._wheel_scroll((1, 2), 3, _send_by=_monotonic() - 1.0)
    assert info.value.details["reason"] == SEND_STOPPED
    assert android.inputs == []


@pytest.mark.parametrize(
    ("name", "value"), [("_send_by", 5), ("_epoch", True), ("_epoch", 1.0)]
)
def test_the_private_arguments_need_a_float_and_an_int(device, android, name, value):
    with pytest.raises(TypeError, match=name):
        device._wheel_scroll((1, 2), 3, **{name: value})
    assert android.inputs == []


def test_the_usage_is_read_with_the_bare_command(device, fake_adb):
    assert device._wheel_supported() is True
    assert [c for _, _, c in fake_adb.commands if c == "input"] == ["input"]


@pytest.mark.parametrize(
    "usage",
    [
        FakeAndroid.USAGE_OLD,
        "",
        "The sources are:\n  mouse\nscroll <x> <y>\n",  # no axis options
        # no VSCROLL
        "The sources are:\n  mouse\nscroll <x> <y> [--axis HSCROLL,<h>]\n",
        "scroll <x> <y> [--axis VSCROLL,<v>] (Default: mouse)\n",  # no mouse source
        # "mouse" only as a command's default source is not a listed source
        "The sources are:\n  touchscreen\n"
        "scroll <x> <y> [--axis VSCROLL,<v>] (Default: mouse)\n",
        "The sources are:\n  mouse\nscroll [--axis VSCROLL,<v>]\n",  # no <x> <y>
        "The sources are:\n  mouse\nfling <x> <y> VSCROLL\n",
        "tap <x> <y>\n",
    ],
)
def test_a_usage_that_does_not_list_all_three_is_no_support(device, android, usage):
    android.input_usage = usage
    assert device._wheel_supported() is False


@pytest.mark.parametrize(
    "usage",
    [
        FakeAndroid.USAGE_WHEEL,
        "The sources are:\n      mouse\n"
        "scroll <x> <y> [--axis VSCROLL,<v>] (Default: mouse)\n",
        "The sources are: mouse, keyboard\n"
        "scroll <x> <y> [--axis HSCROLL,<h>,VSCROLL,<v>] (Default: mouse)\n",
    ],
)
def test_the_mouse_source_a_scroll_line_and_vscroll_are_support(device, android, usage):
    android.input_usage = usage
    assert device._wheel_supported() is True


def test_the_public_swipe_and_scroll_are_not_changed():
    import inspect

    assert "_send_by" not in inspect.signature(Device.swipe).parameters
    assert "_epoch" not in inspect.signature(Device.scroll).parameters


# The axis option list is read exactly: VSCROLL counts only as a word of the
# bracketed ``[--axis ...]`` list of the scroll line, with its value placeholder.
MENTIONED = [
    # VSCROLL named outside the option list
    "The sources are:\n  mouse\n"
    "scroll <x> <y> [--axis HSCROLL,<h>] (VSCROLL unsupported)\n",
    "The sources are:\n  mouse\nscroll <x> <y> [--axis HSCROLL,<h>] VSCROLL\n",
    # VSCROLL in the list but said to be unsupported
    "The sources are:\n  mouse\n"
    "scroll <x> <y> [--axis HSCROLL,<h>,VSCROLL unsupported] (Default: mouse)\n",
    # a longer word that holds it
    "The sources are:\n  mouse\n"
    "scroll <x> <y> [--axis XVSCROLL,<v>] (Default: mouse)\n",
    "The sources are:\n  mouse\n"
    "scroll <x> <y> [--axis VSCROLLS,<v>] (Default: mouse)\n",
    "The sources are:\n  mouse\n"
    "scroll <x> <y> [--axis HSCROLL,<h>,NOVSCROLL,<v>] (Default: mouse)\n",
    # the list is not the one of the scroll line
    "The sources are:\n  mouse\n"
    "scroll <x> <y> [--axis HSCROLL,<h>] (Default: mouse)\n"
    "fling <x> <y> [--axis VSCROLL,<v>]\n",
]


@pytest.mark.parametrize("usage", MENTIONED)
def test_a_usage_that_only_mentions_vscroll_is_no_support(device, android, usage):
    android.input_usage = usage
    assert device._wheel_supported() is False


def test_vscroll_as_a_word_of_the_axis_list_is_support(device, android):
    for usage in (
        "The sources are:\n  mouse\n"
        "scroll <x> <y> [--axis VSCROLL,<v>] (Default: mouse)\n",
        "The sources are:\n  mouse\n"
        "scroll <x> <y> [--axis HSCROLL,<h>,VSCROLL,<v>] (Default: mouse)\n",
        "The sources are:\n  mouse\n"
        "scroll <x> <y> [--axis VSCROLL,<v>,HSCROLL,<h>] (Default: mouse)\n",
        "The sources are: mouse\n"
        "scroll <x> <y> [ --axis VSCROLL,<v> ] (Default: mouse)\n",
    ):
        android.input_usage = usage
        assert device._wheel_supported() is True, usage


# The usage is read per source: the scroll line must be the mouse source's.
OTHER_SOURCE = [
    # VSCROLL listed for the rotary source only
    "The sources are:\n  mouse\n  rotary\n"
    "scroll <x> <y> [--axis VSCROLL,<v>] (Default: rotary)\n",
    "The sources are:\n  mouse\n  rotary\nrotary scroll <x> <y> [--axis VSCROLL,<v>]\n",
    # a mouse line without VSCROLL next to a rotary line with it
    "The sources are:\n  mouse\n  rotary\n"
    "mouse scroll <x> <y> [--axis HSCROLL,<h>]\n"
    "rotary scroll <x> <y> [--axis VSCROLL,<v>]\n",
    # no source named for the line
    "The sources are:\n  mouse\nscroll <x> <y> [--axis VSCROLL,<v>]\n",
]


@pytest.mark.parametrize("usage", OTHER_SOURCE)
def test_vscroll_listed_for_another_source_is_no_support(device, android, usage):
    android.input_usage = usage
    assert device._wheel_supported() is False


@pytest.mark.parametrize(
    "usage",
    [
        "The sources are:\n  mouse\n  rotary\n"
        "mouse scroll <x> <y> [--axis VSCROLL,<v>]\n",
        "The sources are:\n  mouse\n  rotary\n"
        "rotary scroll <x> <y> [--axis HSCROLL,<h>]\n"
        "scroll <x> <y> [--axis VSCROLL,<v>] (Default: mouse)\n",
    ],
)
def test_vscroll_on_the_mouse_line_is_support_whatever_other_lines_say(
    device, android, usage
):
    android.input_usage = usage
    assert device._wheel_supported() is True
