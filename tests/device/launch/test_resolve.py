"""Tests for what ``Device.launch`` sends and the launcher activity it reads."""

from __future__ import annotations

import itertools
import re

import pytest

from tests.device.launch.helpers import (
    DATA,
    MARK,
    MISSING,
    RESOLVE,
    RESOLVE_PREFIX,
    SERIAL,
    SETTINGS,
    SHOP,
    START,
    STOP,
    capture,
    no_monkey,
    sent,
)
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe.device import Device
from uiprobe.errors import LaunchFailedError

# --- the fixtures ---


def test_every_capture_says_where_it_comes_from_and_that_it_is_verbatim():
    names = sorted(path.name for path in DATA.glob("*.txt"))
    assert len(names) == 18
    for name in names:
        raw = (DATA / name).read_bytes()
        head = b"".join(
            itertools.takewhile(
                lambda line: line.startswith(b"#"), raw.splitlines(keepends=True)
            )
        )
        assert b"# Source: " in head
        assert b"# Command: " in head
        assert b"verbatim" in head
        assert capture(name).strip()


# --- Device.launch: what is sent ---


def test_launch_reads_the_launcher_then_sends_one_am_start(device, android, fake_adb):
    before = len(fake_adb.commands)
    assert device.launch(SHOP) is None
    assert sent(fake_adb, before) == [RESOLVE, START]
    assert android.stopped == []
    assert android.screen.package == SHOP
    assert device.foreground() == SHOP


def test_restart_reads_before_and_after_the_force_stop_then_starts(
    device, android, fake_adb
):
    device.launch(SHOP, restart=True)
    assert sent(fake_adb) == [RESOLVE, STOP, RESOLVE, START]
    assert android.stopped == [SHOP]
    assert no_monkey(fake_adb)


def test_launch_never_sends_monkey_or_another_start_option(device, android, fake_adb):
    device.launch(SHOP)
    android.show("launcher")
    device.launch(SHOP, restart=True)
    starts = [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    assert starts == [START, START]
    assert no_monkey(fake_adb)


def test_the_start_is_sent_as_the_service_shell_request(device, fake_adb):
    device.launch(SHOP)
    assert f"shell:{START}" in fake_adb.requests
    assert f"shell:{RESOLVE}" in fake_adb.requests


def test_restart_starts_a_fresh_task(device, android):
    device.launch(SHOP)
    android.show("detail")
    device.launch(SHOP)  # without restart the app resumes where it was
    assert android.screen.name == "detail"
    device.launch(SHOP, restart=True)
    assert android.screen.name == "main"
    assert android.stack == ["launcher", "main"]


def test_launcher_activity_is_read_with_resolve_activity(device, fake_adb):
    assert device.launcher_activity(SHOP) == f"{SHOP}/.Main"
    assert sent(fake_adb) == [RESOLVE]


# --- the launcher activity read ---


@pytest.mark.parametrize(
    "name",
    [
        "emu36-resolve-no-such-package.txt",
        "emu24-resolve-no-such-package.txt",
        "samsung36-resolve-no-such-package.txt",
    ],
)
def test_no_activity_found_stops_the_launch_before_anything_is_sent(
    device, android, fake_adb, name
):
    marker = True if name.startswith("samsung") else None  # captured without
    android.override(re.compile(r"^cmd package"), capture(name, marker=marker))
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, restart=True)
    assert info.value.details == {
        "package": SHOP,
        "reason": "launcher_unreadable",
        "output": "No activity found",
    }
    assert info.value.exit_code == 15
    assert "could not be read on this device" in info.value.message
    assert sent(fake_adb) == [RESOLVE]  # no stop, no start
    assert android.stopped == []


def test_a_missing_package_is_not_started(device, fake_adb):
    with pytest.raises(LaunchFailedError) as info:
        device.launch(MISSING)
    assert info.value.details["reason"] == "launcher_unreadable"
    assert sent(fake_adb) == [RESOLVE_PREFIX + MISSING + MARK]


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"\n\n",
        b"No activity found\n",
        b"com.example.other/.Main\n",
        b"com.example.shopx/.Main\n",
        b"com.example.sho/.Main\n",
        b"com.example.shop/.Main\ncom.example.shop/.Other\n",
        b"com.example.shop/.Main\npriority=0 preferredOrder=0 match=0x108000"
        b" specificIndex=-1 isDefault=true\n",
        b"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n",
        b"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1"
        b" isDefault=maybe\ncom.example.shop/.Main\n",
        b"priority=0 match=0x108000\ncom.example.shop/.Main\n",
        b"Warning: something\ncom.example.shop/.Main\n",
        b"com.example.shop/.Main extra\n",
        b" com.example.shop/.Main\n",
        b"com.example.shop/.Main \n",
        b"com.example.shop/\n",
        b"com.example.shop\n",
        b"com.example.shop/.Ma;in\n",
        b"com.example.shop/..Main\n",
        b"com.example.shop/.M\xc3\xa4in\n",
        b"com.example.shop/." + b"a" * 255 + b"\n",
        b"Error: java.lang.SecurityException\n",
        b"com.example.shop/.Main\x0bcom.example.shop/.Main\n",
    ],
)
def test_the_launcher_read_accepts_only_the_known_shape(
    device, android, fake_adb, output
):
    # The body is at fault: the exit marker after it is in order.
    android.override(re.compile(r"^cmd package"), output + b"uiprobe-exit:0\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["reason"] == "launcher_unreadable"
    assert sent(fake_adb) == [RESOLVE]


@pytest.mark.parametrize(
    ("output", "component"),
    [
        (b"com.example.shop/.Main\n", f"{SHOP}/.Main"),
        (b"com.example.shop/.Main", f"{SHOP}/.Main"),
        (b"com.example.shop/.Main\r\n", f"{SHOP}/.Main"),
        (b"\ncom.example.shop/.Main\n\n", f"{SHOP}/.Main"),
        (b"com.example.shop/com.example.shop.Main\n", f"{SHOP}/{SHOP}.Main"),
        (b"com.example.shop/a.b.C$Inner_1\n", f"{SHOP}/a.b.C$Inner_1"),
        (b"com.example.shop/.9a\n", f"{SHOP}/.9a"),
        (b"com.example.shop/." + b"a" * 254 + b"\n", f"{SHOP}/." + "a" * 254),
        (
            b"priority=-5 preferredOrder=0 match=0x108000 specificIndex=-1"
            b" isDefault=false\npriority=0 preferredOrder=1 match=0xfff"
            b" specificIndex=-2 isDefault=true\ncom.example.shop/.Main\n",
            f"{SHOP}/.Main",
        ),
    ],
)
def test_the_launcher_read_accepts(device, android, output, component):
    android.override(re.compile(r"^cmd package"), output + b"\nuiprobe-exit:0\n")
    assert device.launcher_activity(SHOP) == component


@pytest.mark.parametrize(
    "output",
    [
        b"com.example.shop/.Main\n",  # the marker never came: cut short
        b"com.example.shop/.Main",
        b"com.example.shop/.Main\nuiprobe-exit:",
        b"com.example.shop/.Main\nuiprobe-exit:1\n",
        b"com.example.shop/.Main\nuiprobe-exit:255\n",
        b"com.example.shop/.Main\nuiprobe-exit:00\n",
        b"com.example.shop/.Main\nuiprobe-exit:0 \n",
        b"com.example.shop/.Main\nuiprobe-exit:0\nmore\n",
        b"com.example.shop/.Main\nuiprobe-exit:0\nuiprobe-exit:0\n",
        b"com.example.shop/.Mainuiprobe-exit:0\n",
        b"uiprobe-exit:0\n",
    ],
)
def test_the_launcher_read_needs_its_exit_marker_last(
    device, android, fake_adb, output
):
    android.override(re.compile(r"^cmd package"), output)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, restart=True)
    assert info.value.details["reason"] == "launcher_unreadable"
    assert sent(fake_adb) == [RESOLVE]  # no stop, no start
    assert android.stopped == []


def test_a_launcher_read_cut_short_starts_no_other_activity(fake_adb, clock):
    # The app's launcher is .MainActivity; a reply cut after a component of
    # the same app (.Main) must not make that one start.
    screens = (
        Screen("launcher", "com.example.launcher/.Home", (El((0, 0, 9, 9)),)),
        Screen("real", f"{SHOP}/.MainActivity", (El((0, 0, 9, 9)),)),
        Screen("other", f"{SHOP}/.Main", (El((0, 0, 9, 9)),)),
    )
    phone = FakeAndroid(fake_adb, SERIAL, screens, start="launcher")
    assert clock.now == 1000.0
    device = Device(adb_server=fake_adb.address)
    phone.override(re.compile(r"^cmd package"), f"{SHOP}/.Main".encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, restart=True)
    assert info.value.details["reason"] == "launcher_unreadable"
    assert sent(fake_adb) == [RESOLVE]
    assert phone.stopped == []
    assert phone.screen.name == "launcher"


@pytest.mark.parametrize(
    "name",
    [
        "emu36-resolve-settings.txt",
        "emu24-resolve-settings.txt",
        "samsung36-resolve-settings.txt",
    ],
)
def test_the_launcher_read_of_real_captures(device, android, name):
    marker = True if name.startswith("samsung") else None  # captured without
    android.override(re.compile(r"^cmd package"), capture(name, marker=marker))
    assert device.launcher_activity(SETTINGS) == f"{SETTINGS}/.Settings"


def test_the_launcher_read_escapes_what_it_shows(device, android):
    android.override(re.compile(r"^cmd package"), b"\x1b[2Jbad\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["output"] == "\\x1b[2Jbad"
    assert "\x1b" not in info.value.message
