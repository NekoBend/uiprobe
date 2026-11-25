"""Tests for ``Device.tap``, ``Screen.tap_point`` and the tap's output."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from tests.device.tap.helpers import (
    LONG_COMMAND,
    SERIAL,
    SHOP,
    TAP_COMMAND,
    element,
    sent_inputs,
)
from tests.fake_adb_server import Reset
from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
    assert_no_resend_after_drop,
)
from uiprobe.device import Device
from uiprobe.errors import (
    AdbError,
    ElementNotVisibleError,
    InputFailedError,
    UiprobeError,
)
from uiprobe.model import Bounds, Element

INPUT_REQUEST = re.compile(r"^shell:input\b")


def tap_failure(device: Device, *, long: bool = False) -> InputFailedError:
    with pytest.raises(InputFailedError) as info:
        device.tap((80, 35), long=long)
    return info.value


# --- exact commands and what the device recorded ---


def test_tap_sends_the_exact_command_once(device, android, fake_adb):
    assert device.tap((80, 35)) == (80, 35)
    assert sent_inputs(fake_adb) == [TAP_COMMAND]
    assert [(i.kind, i.point) for i in android.inputs] == [("tap", (80, 35))]
    assert android.stack == ["main", "next"]


def test_long_press_sends_the_exact_command_once(device, android, fake_adb):
    assert device.tap([80, 35], long=True) == (80, 35)
    assert sent_inputs(fake_adb) == [LONG_COMMAND]
    assert [(i.kind, i.point) for i in android.inputs] == [("long_press", (80, 35))]


def test_a_long_press_reaches_the_long_click_target(device, android):
    device.tap((20, 80), long=True)
    assert android.stack == ["main", "menu"]


# --- Device.tap targets ---


def test_tap_with_an_element_uses_its_center(device, android, screen):
    assert device.tap(element(screen, "Login")) == (80, 35)
    assert [i.point for i in android.inputs] == [(80, 35)]


@pytest.mark.parametrize("text", ["Login", "Cut", "Negative", "Edge"])
def test_tap_with_an_element_presses_the_screen_tap_point(
    device, android, screen, text
):
    item = element(screen, text)
    assert device.tap(item) == screen.tap_point(item)
    assert [i.point for i in android.inputs] == [screen.tap_point(item)]


def test_tap_with_an_element_past_the_right_edge_uses_the_visible_part(
    device, android, screen
):
    # display 320x640, bounds (300,10,330,60): the visible part is (300,10,320,60)
    assert screen.display == (320, 640)
    assert element(screen, "Edge").bounds == Bounds(300, 10, 330, 60)
    assert device.tap(element(screen, "Edge")) == (310, 35)
    assert [i.point for i in android.inputs] == [(310, 35)]


def test_tap_with_an_element_cut_at_the_bottom_uses_the_visible_part(
    device, android, screen
):
    assert device.tap(element(screen, "Cut")) == (310, 600)
    assert [i.point for i in android.inputs] == [(310, 600)]


def test_tap_uses_the_int_value_of_an_int_subclass(device, fake_adb):
    class Odd(int):
        def __str__(self) -> str:
            return "7"

    assert device.tap((Odd(80), 35)) == (80, 35)
    assert sent_inputs(fake_adb) == [TAP_COMMAND]


@pytest.mark.parametrize("text", ["Inverted", "Flat", "Below"])
def test_tap_with_an_element_without_a_visible_part(
    device, android, fake_adb, text, screen
):
    before = len(fake_adb.requests)
    with pytest.raises(ElementNotVisibleError) as info:
        device.tap(element(screen, text))
    assert info.value.code == "element_not_visible"
    assert info.value.exit_code == 12
    assert info.value.details["element"] == element(screen, text).id
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_tap_with_an_element_of_an_older_dump_is_a_value_error(
    device, android, fake_adb
):
    old = element(device.dump(), "Login")
    latest = device.dump()
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.tap(old)
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert device.tap(element(latest, "Login")) == (80, 35)


def test_tap_with_a_hand_made_element_is_a_value_error(
    device, android, fake_adb, screen
):
    made = Element("e99", None, "X", Bounds(11, 11, 150, 60))
    twin = Element("e2", None, "Login", element(screen, "Login").bounds)
    before = len(fake_adb.requests)
    for bad in (made, twin):
        with pytest.raises(ValueError, match=r"screen\.tap_point"):
            device.tap(bad)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_tap_with_an_element_before_any_dump_is_a_value_error(
    device, android, fake_adb
):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.tap(Element("e1", None, "X", Bounds(0, 0, 10, 10)))
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_tap_with_an_element_of_another_device_is_a_value_error(
    device, android, fake_adb
):
    other = Device(adb_server=fake_adb.address)
    item = element(other.dump(), "Login")
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.tap(item)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_a_failed_dump_keeps_the_previous_screen_as_the_latest(device, android):
    screen = device.dump()
    android.override(re.compile(r"^uiautomator dump"), b"error\n")
    with pytest.raises(UiprobeError):
        device.dump()
    assert device.tap(element(screen, "Login")) == (80, 35)
    assert [i.point for i in android.inputs] == [(80, 35)]


def test_an_element_whose_point_is_out_of_range_is_a_value_error(fake_adb):
    # `wm size` over 32768 is not trusted, so the display falls back to the
    # widest top-level bounds, which can put the press point out of range.
    wide = Screen(
        "wide",
        f"{SHOP}/.Wide",
        (El((0, 0, 40000, 100), text="Top"), El((39000, 0, 40000, 10), text="Far")),
    )
    android = FakeAndroid(fake_adb, SERIAL, (wide,), start="wide", display=(40000, 640))
    device = Device(adb_server=fake_adb.address)
    screen = device.dump()
    far = element(screen, "Far")
    assert screen.tap_point(far) == (39500, 5)
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="32767"):
        device.tap(far)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_a_point_needs_no_dump(device, android):
    assert device.tap((5, 6)) == (5, 6)
    assert [i.point for i in android.inputs] == [(5, 6)]


@pytest.mark.parametrize(
    "target", [(True, 1), (1, False), (1.0, 2), ("1", "2"), (1, 2, 3), (1,), 5, None]
)
def test_tap_rejects_a_bad_target_type(device, android, fake_adb, target):
    before = len(fake_adb.requests)
    with pytest.raises(TypeError):
        device.tap(target)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize("long", [1, 0, None, "yes"])
def test_tap_rejects_a_non_bool_long(device, android, fake_adb, long):
    before = len(fake_adb.requests)
    with pytest.raises(TypeError):
        device.tap((1, 2), long=long)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize("point", [(-1, 0), (0, -1), (32768, 0), (0, 32768)])
def test_tap_rejects_a_coordinate_out_of_range(device, android, fake_adb, point):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="32767"):
        device.tap(point)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_tap_accepts_the_edges_of_the_range(device, android):
    assert device.tap((0, 32767)) == (0, 32767)
    assert [i.point for i in android.inputs] == [(0, 32767)]


# --- Screen.tap_point ---


def test_tap_point_matches_the_center_find_reports(screen):
    for found in screen.find(text="Cut"), screen.find(text="Login"):
        (item,) = found
        assert list(screen.tap_point(item)) == screen._element_dict(item)["center"]
    assert screen.tap_point(element(screen, "Cut")) == (310, 600)
    assert screen.tap_point(element(screen, "Negative")) == (50, 525)


def test_tap_point_rounds_the_center_down(screen):
    # In an empty part of the screen, where no other element is on top: each
    # element is itself the topmost meaningful one at its center.
    odd = Element("e99", None, "X", Bounds(161, 401, 300, 450), text="odd")
    small = Element("e98", None, "X", Bounds(160, 500, 169, 509), text="x")
    full = replace(screen, elements=(*screen.elements, odd, small))
    assert full.tap_point(odd) == (230, 425)
    assert full.tap_point(small) == (164, 504)


@pytest.mark.parametrize("text", ["Below", "Flat", "Inverted"])
def test_tap_point_without_a_visible_part(screen, text):
    with pytest.raises(ElementNotVisibleError) as info:
        screen.tap_point(element(screen, text))
    item = element(screen, text)
    assert info.value.details == {
        "element": item.id,
        "bounds": item.bounds.to_list(),
        "reason": "no_visible_part",
    }
    assert "center" not in screen._element_dict(item)


# --- reading the output ---


def test_a_nonzero_exit_status(device, android):
    android.answer_input(status=255, output="Error: bad\n")
    error = tap_failure(device)
    assert error.code == "input_failed"
    assert error.exit_code == 14
    assert error.details == {
        "command": "tap",
        "point": [80, 35],
        "reason": "exit_status",
        "exit_status": 255,
        "output": "Error: bad",
    }
    assert len(android.inputs) == 1


def test_a_missing_marker(device, android):
    android.answer_input(marker=False)
    error = tap_failure(device, long=True)
    assert error.details == {
        "command": "long_tap",
        "point": [80, 35],
        "reason": "no_exit_status",
    }
    assert len(android.inputs) == 1


def test_extra_output_before_a_zero_marker(device, android):
    android.answer_input(output="Usage: input ...\n")
    error = tap_failure(device)
    assert error.details == {
        "command": "tap",
        "point": [80, 35],
        "reason": "unexpected_output",
        "output": "Usage: input ...",
    }


def test_long_output_is_cut(device, android):
    android.answer_input(output="x" * 500 + "\n")
    error = tap_failure(device)
    assert error.details["output"] == "x" * 200


@pytest.mark.parametrize(
    "answer",
    [
        b"uiprobe-exit:\n",
        b"uiprobe-exit:x\n",
        b"uiprobe-exit:-1\n",
        b"uiprobe-exit:1 \n",
        b"uiprobe-exit:\xd9\xa0\n",  # an Arabic-Indic digit
        b"uiprobe-exit:0\nmore\n",  # the marker is not the last line
    ],
)
def test_a_marker_that_is_not_plain_decimal_is_no_exit_status(device, android, answer):
    android.override(re.compile(r"^input (?:tap|swipe)"), answer)
    error = tap_failure(device)
    assert error.details["reason"] == "no_exit_status"
    assert error.details["command"] == "tap"
    assert error.details["point"] == [80, 35]


@pytest.mark.parametrize("value", ["00", "000", "007", "9" * 5000])
def test_a_zero_padded_or_huge_marker_is_no_exit_status(device, android, value):
    android.override(
        re.compile(r"^input (?:tap|swipe)"), f"uiprobe-exit:{value}\n".encode()
    )
    error = tap_failure(device)
    assert error.details["reason"] == "no_exit_status"


def test_output_is_stripped_after_the_cut(device, android):
    android.answer_input(output="x" * 199 + " y\n")
    assert tap_failure(device).details["output"] == "x" * 199


def test_empty_lines_are_ignored_and_a_trailing_cr_is_removed(device, android):
    android.override(re.compile(r"^input (?:tap|swipe)"), b"\r\nuiprobe-exit:0\r\n\r\n")
    assert device.tap((80, 35)) == (80, 35)
    android.override(re.compile(r"^input (?:tap|swipe)"), b"\n\nuiprobe-exit:0\n\n")
    assert device.tap((80, 35)) == (80, 35)


def test_a_whitespace_only_line_is_other_output(device, android):
    android.override(re.compile(r"^input (?:tap|swipe)"), b" \nuiprobe-exit:0\n")
    assert tap_failure(device).details["reason"] == "unexpected_output"


def test_a_second_marker_line_is_unexpected_output(device, android):
    android.override(
        re.compile(r"^input (?:tap|swipe)"), b"uiprobe-exit:0\r\nuiprobe-exit:0\r\n"
    )
    assert tap_failure(device).details["reason"] == "unexpected_output"


# --- never resent ---


def test_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.tap((80, 35))
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == [TAP_COMMAND]
    assert android.dropped == [TAP_COMMAND]
    assert_no_resend_after_drop(android, fake_adb)


def test_no_resend_when_the_marker_is_missing(device, android, fake_adb):
    android.answer_input(marker=False)
    with pytest.raises(InputFailedError):
        device.tap((80, 35))
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == [TAP_COMMAND]


def test_no_resend_after_a_reset_reply(device, android, fake_adb):
    android.override(re.compile(r"^input (?:tap|swipe)"), Reset())
    with pytest.raises(UiprobeError):
        device.tap((80, 35))
    assert sent_inputs(fake_adb) == [TAP_COMMAND]
