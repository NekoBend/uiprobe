"""Tests for ``Device.swipe``: the command, validation and the shell timeout."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.device.gestures.helpers import (
    SUFFIX,
    SWIPE_COMMAND,
    no_request,
    sent_inputs,
)
from uiprobe.adb import AdbClient

if TYPE_CHECKING:
    from tests.fake_device import (
        FakeAndroid,
    )


def swipe_args(android: FakeAndroid) -> list[tuple[str, ...]]:
    assert [i.kind for i in android.inputs] == ["swipe"] * len(android.inputs)
    return [i.args[1:] for i in android.inputs]


# --- Device.swipe ---


def test_swipe_sends_the_exact_command_once(device, android, fake_adb):
    assert device.swipe((10, 20), [30, 40]) == ((10, 20), (30, 40))
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]
    assert [(i.kind, i.args) for i in android.inputs] == [
        ("swipe", ("swipe", "10", "20", "30", "40", "300"))
    ]


def test_swipe_sends_the_given_duration(device, fake_adb):
    device.swipe((0, 32767), (32767, 0), duration_ms=10000)
    assert sent_inputs(fake_adb) == ["input swipe 0 32767 32767 0 10000" + SUFFIX]
    device.swipe((1, 2), (1, 3), duration_ms=1)
    assert sent_inputs(fake_adb)[-1] == "input swipe 1 2 1 3 1" + SUFFIX


def test_swipe_uses_the_int_value_of_an_int_subclass(device, fake_adb):
    class Odd(int):
        def __str__(self) -> str:
            return "7"

    assert device.swipe((Odd(10), 20), (30, Odd(40)), duration_ms=Odd(300)) == (
        (10, 20),
        (30, 40),
    )
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]


def test_a_swipe_that_does_not_move_is_not_a_press(device, android, fake_adb):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="use tap"):
        device.swipe((5, 5), (5, 5))
    with pytest.raises(ValueError, match="use tap"):
        device.swipe((5, 5), [5, 5], duration_ms=800)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_the_fake_still_reads_a_marker_long_press_and_a_marker_key(device, android):
    device._shell("input swipe 1 2 1 2 800" + SUFFIX)
    device._shell("input swipe 1 2 3 4 800" + SUFFIX)
    device._shell("input keyevent KEYCODE_HOME" + SUFFIX)
    assert [(i.kind, i.args) for i in android.inputs] == [
        ("long_press", ("swipe", "1", "2", "1", "2", "800")),
        ("swipe", ("swipe", "1", "2", "3", "4", "800")),
        ("key", ("KEYCODE_HOME",)),
    ]


@pytest.mark.parametrize(
    "point",
    [True, 1.5, "1", None, (1, True), (1.0, 2), (1, "2"), (1,), (1, 2, 3), 7],
)
def test_swipe_rejects_a_bad_point_type(device, android, fake_adb, point):
    for first, second in ((point, (1, 2)), ((1, 2), point)):
        no_request(
            android,
            fake_adb,
            lambda a=first, b=second: device.swipe(a, b),
            TypeError,
        )


@pytest.mark.parametrize("point", [(-1, 5), (5, -1), (32768, 5), (5, 32768)])
def test_swipe_rejects_a_coordinate_out_of_range(device, android, fake_adb, point):
    for first, second in ((point, (1, 2)), ((1, 2), point)):
        no_request(
            android,
            fake_adb,
            lambda a=first, b=second: device.swipe(a, b),
            ValueError,
        )


@pytest.mark.parametrize("duration", [True, False, 1.5, "300", None])
def test_swipe_rejects_a_bad_duration_type(device, android, fake_adb, duration):
    no_request(
        android,
        fake_adb,
        lambda: device.swipe((1, 2), (3, 4), duration_ms=duration),
        TypeError,
    )


@pytest.mark.parametrize("duration", [0, -5, 10001])
def test_swipe_rejects_a_duration_out_of_range(device, android, fake_adb, duration):
    no_request(
        android,
        fake_adb,
        lambda: device.swipe((1, 2), (3, 4), duration_ms=duration),
        ValueError,
    )


# --- the timeout of the shell call ---


@pytest.mark.parametrize(
    ("duration", "expected"), [(300, 10.3), (5000, 15.0), (10000, 20.0)]
)
def test_swipe_read_timeout_follows_the_duration(
    device, monkeypatch, duration, expected
):
    seen: list[tuple[str, float | None]] = []
    original = AdbClient.shell

    def spy(self, serial, command, *, timeout=None, **kwargs):
        seen.append((command, timeout))
        return original(self, serial, command, timeout=timeout, **kwargs)

    monkeypatch.setattr(AdbClient, "shell", spy)
    device.swipe((1, 2), (3, 4), duration_ms=duration)
    assert len(seen) == 1
    assert seen[0][0].startswith("input swipe 1 2 3 4 ")
    assert seen[0][1] == pytest.approx(expected)


def test_key_uses_the_client_timeout(device, monkeypatch):
    seen: list[float | None] = []
    original = AdbClient.shell

    def spy(self, serial, command, *, timeout=None, **kwargs):
        seen.append(timeout)
        return original(self, serial, command, timeout=timeout, **kwargs)

    monkeypatch.setattr(AdbClient, "shell", spy)
    device.key("back")
    assert seen == [None]
