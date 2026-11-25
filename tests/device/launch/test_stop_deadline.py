"""Tests for the stop of a restart and the deadline."""

from __future__ import annotations

import pytest

from tests.device.launch.helpers import (
    RESOLVE,
    SHOP,
    STOP,
    Clock,
    sent,
)
from uiprobe.errors import LaunchFailedError

# --- the stop of restart ---


@pytest.mark.parametrize(
    ("answer", "extra"),
    [
        ({"status": 1}, {"exit_status": 1}),
        ({"status": 255, "output": "Error\n"}, {"exit_status": 255, "output": "Error"}),
        ({"output": "Permission denied\n"}, {"output": "Permission denied"}),
        ({"output": "bad\x1b[2J\n"}, {"output": "bad\\x1b[2J"}),
        ({"marker": False}, {}),
    ],
)
def test_restart_stop_failure_sends_no_start(device, android, fake_adb, answer, extra):
    android.show("main")
    android.answer_input(**answer)
    before = len(fake_adb.commands)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, restart=True)
    assert info.value.details == {"package": SHOP, "reason": "stop_failed", **extra}
    assert info.value.exit_code == 15
    assert sent(fake_adb, before) == [RESOLVE, STOP]
    assert not any(c.startswith("am start") for _, _, c in fake_adb.commands)
    assert "\x1b" not in info.value.message


def test_a_fresh_start_keeps_the_screens_of_other_apps(device, android):
    android.show("main")
    android.stack = ["main"]
    android._stop(SHOP)  # the task is gone, its screen still shows
    android.show("launcher")  # another app comes to the front
    device.launch(SHOP)
    assert android.stack == ["launcher", "main"]


def test_restart_starts_fresh_when_every_screen_is_the_app(device, android):
    android.show("main")
    android.show("detail")
    android.stack = ["main", "detail"]  # no screen of another app below
    device.launch(SHOP, restart=True)
    assert android.stack == ["main"]
    assert android.screen.name == "main"


# --- the deadline ---


class _Reads:
    """A foreground that records when each read starts."""

    def __init__(self, clock: Clock, answer, *, takes: float = 0.0) -> None:
        self.clock = clock
        self.answer = answer
        self.takes = takes
        self.started: list[float] = []

    def __call__(self) -> str | None:
        self.started.append(self.clock.now)
        self.clock.now += self.takes
        return self.answer(self.clock.now)


@pytest.mark.usefixtures("android")
def test_a_read_that_starts_at_the_deadline_counts(device, clock, monkeypatch):
    reads = _Reads(clock, lambda now: SHOP if now >= 1001.0 else "com.example.launcher")
    monkeypatch.setattr(device, "foreground", reads)
    device.launch(SHOP, timeout=1)
    assert reads.started == [1000.0, 1000.5, 1001.0]


@pytest.mark.usefixtures("android")
def test_no_read_starts_after_the_deadline(device, clock, monkeypatch):
    def oversleep(seconds: float) -> None:
        clock.pauses.append(seconds)
        clock.now += seconds + 0.25  # the pause runs late

    monkeypatch.setattr(device, "_sleep", oversleep)
    # The app would be in front from 1001.0 on, but no read starts after it.
    reads = _Reads(clock, lambda now: SHOP if now > 1001.0 else "com.example.launcher")
    monkeypatch.setattr(device, "foreground", reads)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=1)
    assert info.value.details["reason"] == "not_in_front"
    assert reads.started == [1000.0, 1000.75]
    assert clock.pauses == [0.5, 0.25]


@pytest.mark.usefixtures("android")
def test_a_slow_read_started_in_time_counts(device, clock, monkeypatch):
    reads = _Reads(clock, lambda _now: SHOP, takes=2.0)
    monkeypatch.setattr(device, "foreground", reads)
    device.launch(SHOP, timeout=1)
    assert reads.started == [1000.0]
    assert clock.pauses == []


@pytest.mark.usefixtures("android")
def test_a_slow_read_past_the_deadline_ends_the_wait(device, clock, monkeypatch):
    reads = _Reads(clock, lambda _now: "com.example.launcher", takes=2.0)
    monkeypatch.setattr(device, "foreground", reads)
    with pytest.raises(LaunchFailedError):
        device.launch(SHOP, timeout=1)
    assert reads.started == [1000.0]
    assert clock.pauses == []
