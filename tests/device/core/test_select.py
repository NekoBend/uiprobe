"""Tests for `select_device`: which device a serial or the environment picks."""

from __future__ import annotations

import pytest

from uiprobe.adb import DeviceInfo
from uiprobe.device import select_device
from uiprobe.errors import (
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    MultipleDevicesError,
)


def info(serial: str, state: str = "device") -> DeviceInfo:
    return DeviceInfo(serial=serial, state=state)


# --- select_device ---


def test_select_single_ready_device():
    chosen = select_device([info("a"), info("b", "offline")], environ={})
    assert chosen.serial == "a"


def test_select_requested_serial_argument_wins_over_environment():
    found = [info("a"), info("b")]
    assert select_device(found, "b", {"ANDROID_SERIAL": "a"}).serial == "b"


def test_select_uses_android_serial():
    found = [info("a"), info("b")]
    assert select_device(found, None, {"ANDROID_SERIAL": "b"}).serial == "b"


def test_select_empty_android_serial_counts_as_unset():
    assert select_device([info("a")], None, {"ANDROID_SERIAL": ""}).serial == "a"


def test_select_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("ANDROID_SERIAL", "b")
    assert select_device([info("a"), info("b")]).serial == "b"


def test_select_empty_serial_argument_is_a_request_not_unset():
    # `-s "$UNSET"` must not silently pick the only device or ANDROID_SERIAL.
    with pytest.raises(DeviceNotFoundError) as caught:
        select_device([info("a")], "", {"ANDROID_SERIAL": "a"})
    assert caught.value.details["serial"] == ""


@pytest.mark.parametrize("serial", [5554, b"a", ["a"]])
def test_select_rejects_a_serial_that_is_not_a_string(serial):
    with pytest.raises(TypeError, match="serial must be a string"):
        select_device([info("5554")], serial, {})


def test_select_requested_missing():
    with pytest.raises(DeviceNotFoundError) as caught:
        select_device([info("a", "offline")], "z", {})
    assert caught.value.details == {
        "serial": "z",
        "devices": [{"serial": "a", "state": "offline"}],
    }


@pytest.mark.parametrize(
    ("state", "error"),
    [("offline", DeviceOfflineError), ("unauthorized", DeviceUnauthorizedError)],
)
def test_select_requested_not_ready(state, error):
    with pytest.raises(error) as caught:
        select_device([info("a", state)], "a", {})
    assert caught.value.details == {"serial": "a", "state": state}


def test_select_requested_other_state_is_offline():
    with pytest.raises(DeviceOfflineError):
        select_device([info("a", "no permissions (udev)")], "a", {})


def test_select_multiple_ready():
    with pytest.raises(MultipleDevicesError) as caught:
        select_device([info("a"), info("b"), info("c", "offline")], None, {})
    assert caught.value.details == {"serials": ["a", "b"]}
    assert "--serial" in caught.value.message


def test_select_nothing_connected():
    with pytest.raises(DeviceNotFoundError) as caught:
        select_device([], None, {})
    assert caught.value.details == {"devices": []}


def test_select_only_unusable_devices_prefers_unauthorized():
    found = [info("a", "offline"), info("b", "unauthorized")]
    with pytest.raises(DeviceUnauthorizedError) as caught:
        select_device(found, None, {})
    assert caught.value.details == {
        "devices": [
            {"serial": "a", "state": "offline"},
            {"serial": "b", "state": "unauthorized"},
        ]
    }


def test_select_only_offline_devices():
    with pytest.raises(DeviceOfflineError):
        select_device([info("a", "offline")], None, {})
