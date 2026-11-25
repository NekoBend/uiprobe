"""Tests for the start's wait and the foreground check."""

from __future__ import annotations

import re

import pytest

from tests.device.launch.helpers import (
    RESOLVE,
    SHOP,
    START,
    sent,
)
from tests.fake_adb_server import Stall
from uiprobe.device import apps as device_apps
from uiprobe.errors import LaunchFailedError

# --- the start's wait ---


def test_the_start_waits_as_long_as_the_launch_timeout(device, monkeypatch):
    waits: list[tuple[str, float | None]] = []
    real = device._client.shell

    def spy(serial, command, *, timeout=None, **pin):
        waits.append((command, timeout))
        return real(serial, command, timeout=timeout, **pin)

    monkeypatch.setattr(device._client, "shell", spy)
    device.launch(SHOP, timeout=7)
    device.launch(SHOP, timeout=0)
    device.launch(SHOP, timeout=0.5)
    starts = [timeout for command, timeout in waits if command.startswith("am ")]
    assert starts == [7.0, 1.0, 1.0]  # at least START_MIN_WAIT
    reads = [timeout for command, timeout in waits if command.startswith("cmd ")]
    assert reads == [None, None, None]


def test_a_start_that_stays_silent_fails_with_start_timeout(
    device, android, fake_adb, monkeypatch
):
    monkeypatch.setattr(device_apps, "START_MIN_WAIT", 0.05)
    android.override(re.compile(r"^am start"), Stall(b"Starting: Intent\n"))
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=0)
    assert info.value.details == {
        "package": SHOP,
        "reason": "start_timeout",
        "timeout": 0.05,
    }
    assert info.value.exit_code == 15
    assert sent(fake_adb) == [RESOLVE, START]  # never resent


# --- the foreground check ---


def test_launch_returns_at_once_when_the_app_is_already_in_front(
    device, android, clock
):
    android.show("main")
    device.launch(SHOP)
    assert clock.pauses == []


def test_launch_waits_for_the_app_with_half_second_polls(
    device, android, clock, monkeypatch
):
    reads = []
    real = device.foreground

    def late() -> str | None:
        reads.append(clock.now)
        if len(reads) == 3:
            android.show("main")  # the app comes forward after two polls
        return real()

    android.launch_in_front = False
    monkeypatch.setattr(device, "foreground", late)
    device.launch(SHOP)
    assert clock.pauses == [0.5, 0.5]
    assert len(reads) == 3


def test_the_foreground_check_is_needed_after_a_reported_success(
    device, android, fake_adb
):
    android.launch_in_front = False  # am start reports ok, the app stays behind
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=1)
    assert info.value.details["reason"] == "not_in_front"
    assert sent(fake_adb) == [RESOLVE, START]


def test_launch_not_in_front_after_the_timeout(device, android, clock, fake_adb):
    android.launch_in_front = False
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=2)
    error = info.value
    assert error.code == "launch_failed"
    assert error.exit_code == 15
    assert error.details == {
        "package": SHOP,
        "reason": "not_in_front",
        "foreground": "com.example.launcher",
        "timeout": 2.0,
    }
    assert clock.pauses == [0.5, 0.5, 0.5, 0.5]
    assert sent(fake_adb) == [RESOLVE, START]  # never sent twice


def test_launch_default_timeout_is_ten_seconds(device, android, clock):
    android.launch_in_front = False
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["timeout"] == 10.0
    assert sum(clock.pauses) == 10.0


def test_launch_timeout_zero_reads_once(device, android, clock, monkeypatch):
    android.launch_in_front = False
    reads = []
    real = device.foreground

    def counted() -> str | None:
        reads.append(clock.now)
        return real()

    monkeypatch.setattr(device, "foreground", counted)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=0)
    assert info.value.details["reason"] == "not_in_front"
    assert clock.pauses == []
    assert reads == [1000.0]


def test_launch_last_pause_is_cut_to_the_deadline(device, android, clock):
    android.launch_in_front = False
    with pytest.raises(LaunchFailedError):
        device.launch(SHOP, timeout=0.75)
    assert clock.pauses == [0.5, 0.25]


def test_launch_escapes_the_last_foreground(device, android, monkeypatch):
    android.launch_in_front = False
    monkeypatch.setattr(device, "foreground", lambda: "bad\x1b[2Jname\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=0)
    assert info.value.details["foreground"] == "bad\\x1b[2Jname\\n"
    assert "\x1b" not in info.value.message


def test_launch_with_an_unknown_foreground(device, android, monkeypatch):
    android.launch_in_front = False
    monkeypatch.setattr(device, "foreground", lambda: None)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=0)
    assert info.value.details["foreground"] is None
    assert "(foreground: unknown)" in info.value.message


@pytest.mark.parametrize(
    "package",
    [
        "",
        "shop",
        "com..shop",
        ".com.shop",
        "com.shop.",
        "1com.shop",
        "com.1shop",
        "com.sh-op",
        "com.shop;reboot",
        "com.shop && reboot",
        "com.shop$(reboot)",
        "com.shop`reboot`",
        "com.shop|id",
        "com.shop\nreboot",
        "com.shop com.other",
        "com.shop'",
        'com.shop"',
        "com.shöp",
        "com." + "a" * 252,
    ],
)
def test_launch_refuses_a_bad_package_before_any_request(device, fake_adb, package):
    before = list(fake_adb.requests)
    with pytest.raises(ValueError, match="not a valid package name"):
        device.launch(package, restart=True)
    assert fake_adb.requests == before


@pytest.mark.parametrize("package", [None, 5, b"com.shop", ["com.shop"]])
def test_launch_refuses_a_non_str_package(device, fake_adb, package):
    before = list(fake_adb.requests)
    with pytest.raises(TypeError):
        device.launch(package)
    assert fake_adb.requests == before


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"restart": 1}, TypeError),
        ({"restart": None}, TypeError),
        ({"timeout": "5"}, TypeError),
        ({"timeout": True}, TypeError),
        ({"timeout": None}, TypeError),
        ({"timeout": -1}, ValueError),
        ({"timeout": float("nan")}, ValueError),
        ({"timeout": float("inf")}, ValueError),
        ({"timeout": 601}, ValueError),
    ],
)
def test_launch_refuses_bad_options_before_any_request(device, fake_adb, kwargs, error):
    before = list(fake_adb.requests)
    with pytest.raises(error):
        device.launch(SHOP, **kwargs)
    assert fake_adb.requests == before
