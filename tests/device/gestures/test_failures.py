"""Tests for input failures and that an input is never resent."""

from __future__ import annotations

import re

import pytest

from tests.device.gestures.helpers import (
    SUFFIX,
    SWIPE_COMMAND,
    sent_inputs,
)
from tests.fake_adb_server import Reset
from tests.fake_device import (
    assert_no_resend_after_drop,
)
from uiprobe.errors import (
    AdbError,
    InputFailedError,
    UiprobeError,
)

# --- input_failed ---


def test_swipe_input_failed_details(device, android):
    android.answer_input(status=1, output="boom\n")
    with pytest.raises(InputFailedError) as info:
        device.swipe((10, 20), (30, 40), duration_ms=500)
    assert info.value.code == "input_failed"
    assert info.value.exit_code == 14
    assert info.value.details == {
        "command": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 500,
        "reason": "exit_status",
        "exit_status": 1,
        "output": "boom",
    }


def test_swipe_missing_marker(device, android):
    android.answer_input(marker=False)
    with pytest.raises(InputFailedError) as info:
        device.swipe((10, 20), (30, 40))
    assert info.value.details == {
        "command": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 300,
        "reason": "no_exit_status",
    }


@pytest.mark.usefixtures("screen")
def test_scroll_failure_reports_the_swipe(device, android):
    android.answer_input(output="usage: input\n")
    with pytest.raises(InputFailedError) as info:
        device.scroll("up")
    assert info.value.details == {
        "command": "swipe",
        "from": [160, 511],
        "to": [160, 128],
        "duration_ms": 300,
        "reason": "unexpected_output",
        "output": "usage: input",
    }


def test_key_input_failed_details(device, android):
    android.answer_input(status=255, output="Exception\n")
    with pytest.raises(InputFailedError) as info:
        device.key("home")
    assert info.value.details == {
        "command": "key",
        "key": "KEYCODE_HOME",
        "reason": "exit_status",
        "exit_status": 255,
        "output": "Exception",
    }


def test_key_extra_output_before_a_zero_marker(device, android):
    android.answer_input(output="hello\n")
    with pytest.raises(InputFailedError) as info:
        device.key("back")
    assert info.value.details["reason"] == "unexpected_output"
    assert len(android.inputs) == 1


# --- never resent ---


def test_swipe_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.swipe((10, 20), (30, 40))
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]
    assert android.dropped == [SWIPE_COMMAND]
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("screen")
def test_scroll_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.scroll("up")
    assert len(android.inputs) == 1
    assert len(sent_inputs(fake_adb)) == 1
    assert_no_resend_after_drop(android, fake_adb)


def test_key_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.key("home")
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_HOME" + SUFFIX]
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("screen")
def test_no_resend_when_the_marker_is_missing(device, android, fake_adb):
    android.answer_input(marker=False)
    with pytest.raises(InputFailedError):
        device.swipe((10, 20), (30, 40))
    with pytest.raises(InputFailedError):
        device.key("back")
    with pytest.raises(InputFailedError):
        device.scroll("left")
    assert len(android.inputs) == 3
    assert len(sent_inputs(fake_adb)) == 3


def test_no_resend_after_a_reset_reply(device, android, fake_adb):
    android.override(re.compile(r"^input (swipe|keyevent)"), Reset())
    with pytest.raises(UiprobeError):
        device.swipe((10, 20), (30, 40))
    with pytest.raises(UiprobeError):
        device.key("back")
    assert len(sent_inputs(fake_adb)) == 2
