"""Tests for the argument values ``Device.launch`` accepts."""

from __future__ import annotations

import re
from typing import Any

import pytest

from tests.device.launch.helpers import (
    RESOLVE,
    SHOP,
    START,
    STOP,
    sent,
    start_of,
)
from tests.fake_adb_server import Reset
from uiprobe.errors import AdbError, LaunchFailedError

# --- argument values ---


class _OddText(str):
    """Text that is false, compares without case and prints as something else."""

    __hash__ = str.__hash__

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and str.lower(self) == str.lower(other)

    def __str__(self) -> str:
        return ""

    def __format__(self, spec: str) -> str:
        return ""


def test_a_restart_starts_the_launcher_read_after_the_stop(device, android, fake_adb):
    # The launcher changes from .Main to .Detail while the app stops: the
    # start names .Detail, as monkey on main picked the launcher after the
    # stop.
    reads: list[str] = []

    def launcher(command: str):
        reads.append(command)
        name = ".Main" if len(reads) == 1 else ".Detail"
        return f"{SHOP}/{name}\nuiprobe-exit:0\n".encode()

    android.override(re.compile(r"^cmd package"), launcher)
    device.launch(SHOP, restart=True)
    assert sent(fake_adb) == [RESOLVE, STOP, RESOLVE, start_of(f"{SHOP}/.Detail")]


def test_a_restart_whose_first_read_fails_stops_nothing(device, android, fake_adb):
    android.override(re.compile(r"^cmd package"), b"No activity found\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, restart=True)
    assert info.value.details == {
        "package": SHOP,
        "reason": "launcher_unreadable",
        "output": "No activity found",
    }
    assert sent(fake_adb) == [RESOLVE]
    assert android.stopped == []


def test_a_restart_whose_second_read_fails_says_stopped_not_started(
    device, android, fake_adb
):
    reads: list[str] = []

    def launcher(command: str):
        reads.append(command)
        if len(reads) == 1:
            return android._shell(command)
        return b"No activity found\n"

    android.override(re.compile(r"^cmd package"), launcher)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, restart=True)
    assert info.value.details == {
        "package": SHOP,
        "reason": "launcher_unreadable",
        "output": "No activity found",
        "force_stopped": True,
    }
    assert "force-stopped but not started again" in str(info.value)
    assert sent(fake_adb) == [RESOLVE, STOP, RESOLVE]
    assert android.stopped == [SHOP]


def test_a_restart_whose_second_read_fails_in_transport_says_stopped(
    device, android, fake_adb, monkeypatch
):
    real = device._launcher_activity
    calls: list[str] = []

    def read(package: str, *, strict: bool) -> str:
        calls.append(package)
        if len(calls) == 1:
            return real(package, strict=strict)
        raise AdbError("the reply did not come", details={"reason": "timeout"})

    monkeypatch.setattr(device, "_launcher_activity", read)
    with pytest.raises(AdbError) as info:
        device.launch(SHOP, restart=True)
    assert info.value.details == {"reason": "timeout", "force_stopped": True}
    assert sent(fake_adb) == [RESOLVE, STOP]
    assert android.stopped == [SHOP]


def test_launch_takes_no_activity_argument(device, fake_adb):
    loose: Any = device.launch
    with pytest.raises(TypeError, match="activity"):
        loose(SHOP, activity=f"{SHOP}/.Main")
    assert sent(fake_adb) == []


def test_a_str_subclass_is_used_as_plain_text(device, android, fake_adb, monkeypatch):
    android.launch_in_front = False
    monkeypatch.setattr(device, "foreground", lambda: SHOP.upper())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(_OddText(SHOP), restart=True, timeout=0)
    assert sent(fake_adb) == [RESOLVE, STOP, RESOLVE, START]
    assert type(info.value.details["package"]) is str
    assert info.value.details["package"] == SHOP


class _HugeFloat(int):
    def __float__(self) -> float:
        return float("inf")


def test_timeout_value_comes_from_the_base_type(device, android, clock):
    android.launch_in_front = False
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=_HugeFloat(1))
    assert info.value.details["timeout"] == 1.0
    assert sum(clock.pauses) == 1.0


class _IntPosingAsFloat(int):
    @property
    def __class__(self):
        return float


def test_an_int_that_claims_to_be_a_float_is_read_as_an_int(device, android, clock):
    android.launch_in_front = False
    timeout = _IntPosingAsFloat(1)
    assert isinstance(timeout, float)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=timeout)
    assert info.value.details["timeout"] == 1.0
    assert sum(clock.pauses) == 1.0


def test_a_timeout_too_large_for_a_float_is_refused(device, fake_adb):
    before = list(fake_adb.requests)
    with pytest.raises(ValueError, match="from 0 to 600"):
        device.launch(SHOP, timeout=10**400)
    assert fake_adb.requests == before


@pytest.mark.parametrize(
    ("length", "shown_length", "cut"),
    [(255, 255, False), (256, 256, False), (1000, 1000, False), (1001, 1003, True)],
)
@pytest.mark.usefixtures("android")
def test_the_last_foreground_is_cut_only_past_1000_characters(
    device, monkeypatch, length, shown_length, cut
):
    name = "com." + "a" * (length - 4)
    monkeypatch.setattr(device, "foreground", lambda: name)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=0)
    shown = info.value.details["foreground"]
    assert isinstance(shown, str)
    assert len(shown) == shown_length
    assert shown.endswith("...") is cut
    assert shown == (name[:1000] + "..." if cut else name)


def test_launch_does_not_resend_after_an_error_while_waiting(device, android, fake_adb):
    android.override(re.compile(r"^dumpsys"), Reset())
    with pytest.raises(AdbError):
        device.launch(SHOP, restart=True)
    assert sent(fake_adb) == [RESOLVE, STOP, RESOLVE, START]
