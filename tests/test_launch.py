"""Tests for ``Device.launch`` and ``uiprobe launch``.

Every device is a strict `FakeAndroid`, so a command outside its allowlist
(monkey, another am start option, a component it does not declare) fails the
test. Time is fake: nothing here sleeps, except the one test of the start's
read timeout, which waits 0.05 s of real time. Real device outputs come from
tests/data/launch (each file says where it was captured).
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fake_adb_server import FakeAdbServer, Reset, Stall
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import cli
from uiprobe import device as device_module
from uiprobe.device import Device
from uiprobe.errors import AdbError, LaunchFailedError

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
MISSING = "com.example.missing"
SETTINGS = "com.android.settings"
LAUNCHER = "android.intent.category.LAUNCHER"
RESOLVE_PREFIX = f"cmd package resolve-activity --brief -c {LAUNCHER} "
RESOLVE = RESOLVE_PREFIX + SHOP
START_PREFIX = f"am start -W -a android.intent.action.MAIN -c {LAUNCHER} -n "
START = f"{START_PREFIX}{SHOP}/.Main; echo uiprobe-exit:$?"
STOP = f"am force-stop {SHOP}; echo uiprobe-exit:$?"
DATA = Path(__file__).parent / "data" / "launch"
OK_36 = (
    "Starting: Intent { act=android.intent.action.MAIN"
    f" cat=[{LAUNCHER}] cmp={SHOP}/.Main }}\n"
    "Status: ok\nLaunchState: COLD\nActivity: {activity}\nTotalTime: 5\n"
    "WaitTime: 6\nComplete\nuiprobe-exit:0\n"
)

SCREENS = (
    Screen("launcher", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen("main", f"{SHOP}/.Main", (El((0, 0, 320, 100), text="Shop"),)),
    Screen("detail", f"{SHOP}/.Detail", (El((0, 0, 320, 100), text="Item"),)),
    Screen("settings", f"{SETTINGS}/.Settings", (El((0, 0, 320, 100), text="S"),)),
    Screen(
        "homepage",
        f"{SETTINGS}/.homepage.SettingsHomepageActivity",
        (El((0, 0, 320, 100), text="Settings"),),
    ),
)


def capture(name: str, *, marker: bool | None = None) -> bytes:
    """The output in tests/data/launch/`name` without its ``#`` header.

    `marker` False drops a last ``uiprobe-exit:`` line (the capture's own
    suffix, which uiprobe does not send with resolve-activity); True appends
    ``uiprobe-exit:0`` to a capture made without the suffix.
    """
    lines = (DATA / name).read_bytes().splitlines(keepends=True)
    body = list(itertools.dropwhile(lambda line: line.startswith(b"#"), lines))
    if marker is False and body and body[-1].startswith(b"uiprobe-exit:"):
        body.pop()
    if marker is True:
        body.append(b"uiprobe-exit:0\n")
    return b"".join(body)


def start_of(component: str) -> str:
    return f"{START_PREFIX}{component}; echo uiprobe-exit:$?"


class Clock:
    """A fake clock whose pause moves it forward."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(
        fake_adb, SERIAL, SCREENS, start="launcher", extra_packages=(MISSING,)
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(
        device_module,
        "time",
        SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep),
    )
    return fake


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer, clock: Clock) -> Device:
    assert android.serial == SERIAL
    assert clock.now == 1000.0
    return Device(adb_server=fake_adb.address)


def sent(fake_adb: FakeAdbServer, start: int = 0) -> list[str]:
    """The shell commands received after the first `start`, without the reads
    of the screen and the device (the launcher activity read stays)."""
    reads = ("dumpsys", "getprop", "wm size", "uiautomator", "rm ", "cat ")
    return [c for _, _, c in fake_adb.commands[start:] if not c.startswith(reads)]


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def no_monkey(fake_adb: FakeAdbServer) -> bool:
    return not any("monkey" in c for _, _, c in fake_adb.commands)


# --- the fixtures ---


def test_every_capture_says_where_it_comes_from_and_that_it_is_verbatim():
    names = sorted(path.name for path in DATA.glob("*.txt"))
    assert len(names) == 15
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


def test_restart_sends_the_read_then_force_stop_then_the_start(
    device, android, fake_adb
):
    device.launch(SHOP, restart=True)
    assert sent(fake_adb) == [RESOLVE, STOP, START]
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


def test_a_given_activity_is_started_without_a_read(device, android, fake_adb):
    device.launch(SHOP, activity=f"{SHOP}/.Main")
    assert sent(fake_adb) == [START]
    android.show("launcher")
    device.launch(SHOP, restart=True, activity=f"{SHOP}/{SHOP}.Main")
    assert sent(fake_adb)[1:] == [STOP, start_of(f"{SHOP}/{SHOP}.Main")]


@pytest.mark.parametrize(
    ("activity", "error"),
    [
        (5, TypeError),
        (b"com.example.shop/.Main", TypeError),
        ("com.example.other/.Main", ValueError),
        ("com.example.sho/.Main", ValueError),
        ("com.example.shopx/.Main", ValueError),
        (f"{SHOP}/", ValueError),
        (f"{SHOP}/.Main;reboot", ValueError),
        (f"{SHOP}/.Main reboot", ValueError),
        (f"{SHOP}/..Main", ValueError),
        (f"{SHOP}/.Main.", ValueError),
        (f"{SHOP}/.M\u00e4in", ValueError),
        (f"{SHOP}/." + "a" * 255, ValueError),
        (f"{SHOP}/.Main/x", ValueError),
        (SHOP, ValueError),
    ],
)
def test_a_given_activity_is_checked_before_any_request(
    device, fake_adb, activity, error
):
    before = list(fake_adb.requests)
    with pytest.raises(error):
        device.launch(SHOP, activity=activity)
    assert fake_adb.requests == before


def test_a_given_activity_subclass_is_used_as_plain_text(device, fake_adb):
    class Odd(str):
        __hash__ = str.__hash__

        def __str__(self) -> str:
            return "com.attacker/.X"

    device.launch(SHOP, activity=Odd(f"{SHOP}/.Main"))
    assert sent(fake_adb) == [START]


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
    android.override(re.compile(r"^cmd package"), capture(name, marker=False))
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
    assert sent(fake_adb) == [RESOLVE_PREFIX + MISSING]


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
    android.override(re.compile(r"^cmd package"), output)
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
    android.override(re.compile(r"^cmd package"), output)
    assert device.launcher_activity(SHOP) == component


@pytest.mark.parametrize(
    "name",
    [
        "emu36-resolve-settings.txt",
        "emu24-resolve-settings.txt",
        "samsung36-resolve-settings.txt",
    ],
)
def test_the_launcher_read_of_real_captures(device, android, name):
    android.override(re.compile(r"^cmd package"), capture(name, marker=False))
    assert device.launcher_activity(SETTINGS) == f"{SETTINGS}/.Settings"


def test_the_launcher_read_escapes_what_it_shows(device, android):
    android.override(re.compile(r"^cmd package"), b"\x1b[2Jbad\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["output"] == "\\x1b[2Jbad"
    assert "\x1b" not in info.value.message


# --- the start: real outputs ---


@pytest.mark.parametrize(
    ("resolve", "start", "marker"),
    [
        ("emu36-resolve-settings.txt", "emu36-start-after-force-stop.txt", None),
        ("emu36-resolve-settings.txt", "emu36-start-running.txt", None),
        ("emu24-resolve-settings.txt", "emu24-start-after-force-stop.txt", None),
        ("emu24-resolve-settings.txt", "emu24-start-running.txt", None),
        ("samsung36-resolve-settings.txt", "samsung36-start-running.txt", True),
        (
            "samsung36-resolve-settings.txt",
            "samsung36-start-after-force-stop.txt",
            True,
        ),
    ],
)
def test_real_successful_starts_are_accepted(
    device, android, fake_adb, resolve, start, marker
):
    android.override(re.compile(r"^cmd package"), capture(resolve, marker=False))
    android.override(re.compile(r"^am start"), capture(start, marker=marker))
    android.show("homepage")  # what the scripted start brought to the front
    device.launch(SETTINGS)
    assert sent(fake_adb) == [
        RESOLVE_PREFIX + SETTINGS,
        start_of(f"{SETTINGS}/.Settings"),
    ]


@pytest.mark.parametrize(
    ("name", "marker", "status"),
    [
        ("emu36-start-no-such-class.txt", None, 1),
        ("emu24-start-no-such-class.txt", None, None),  # exit status 0 on API 24
        ("samsung36-start-no-such-class.txt", True, None),
    ],
)
def test_real_error_outputs_fail_whatever_the_exit_status(
    device, android, name, marker, status
):
    android.override(re.compile(r"^am start"), capture(name, marker=marker))
    android.show("settings")  # even with the app in front
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SETTINGS)
    details = info.value.details
    output = details["output"]
    assert details["reason"] == "start_failed"
    assert isinstance(output, str)
    assert output.startswith("Error: Activity class {com.android.settings/")
    assert output.endswith("} does not exist.")
    assert details.get("exit_status") == status
    assert info.value.exit_code == 15


# --- the start: the success rule ---


def scripted_start(android: FakeAndroid, output: bytes) -> None:
    android.override(re.compile(r"^am start"), output)
    android.show("main")  # the foreground check alone would pass


@pytest.mark.parametrize(
    "activity",
    [f"{SHOP}/.Main", f"{SHOP}/.Other", f"{SHOP}/{SHOP}.Main", f"{SHOP}/.a.B$C"],
)
def test_the_reported_activity_may_have_another_class(device, android, activity):
    scripted_start(android, OK_36.replace("{activity}", activity).encode())
    device.launch(SHOP)


@pytest.mark.parametrize(
    ("output", "reason", "shown"),
    [
        # another package, or not a component
        (
            OK_36.replace("{activity}", "com.example.other/.Main"),
            "unexpected_output",
            "Activity: com.example.other/.Main",
        ),
        (
            OK_36.replace("{activity}", "com.example.shopx/.Main"),
            "unexpected_output",
            "Activity: com.example.shopx/.Main",
        ),
        (
            OK_36.replace("{activity}", SHOP),
            "unexpected_output",
            f"Activity: {SHOP}",
        ),
        (
            OK_36.replace("{activity}", f"{SHOP}/.Main extra"),
            "unexpected_output",
            f"Activity: {SHOP}/.Main extra",
        ),
        (
            OK_36.replace("Activity: {activity}", f"Activity:{SHOP}/.Main"),
            "unexpected_output",
            f"Activity:{SHOP}/.Main",
        ),
        # missing or repeated lines
        (
            OK_36.replace("Activity: {activity}\n", ""),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace(
                "Activity: {activity}\n",
                f"Activity: {SHOP}/.Main\nActivity: {SHOP}/.Main\n",
            ),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace("Status: ok\n", ""),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: ok\nStatus: ok\n"),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: timeout\n"),
            "unexpected_output",
            "Status: timeout",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: ok \n"),
            "unexpected_output",
            "Status: ok ",
        ),
        # Error lines fail whatever the exit status
        (
            OK_36.replace("Complete\n", "Complete\nError: it broke\n"),
            "start_failed",
            "Error: it broke",
        ),
        (
            OK_36.replace("Status: ok\n", "Error type 3\nStatus: ok\n"),
            "start_failed",
            "Error type 3",
        ),
        # the exit status and its marker
        (
            OK_36.replace("uiprobe-exit:0", "uiprobe-exit:1"),
            "start_failed",
            "Starting: Intent",
        ),
        (
            OK_36.replace("uiprobe-exit:0\n", ""),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace("uiprobe-exit:0", "uiprobe-exit:x"),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36 + "more\n",
            "unexpected_output",
            "Starting: Intent",
        ),
        # an unknown line
        (
            OK_36.replace("Complete\n", "Complete\nhello\n"),
            "unexpected_output",
            "hello",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: ok\x0bActivity: x\n"),
            "unexpected_output",
            "Status: ok\\x0bActivity: x",
        ),
        ("uiprobe-exit:0\n", "unexpected_output", ""),
        ("", "unexpected_output", ""),
    ],
)
def test_the_start_must_report_the_known_success(
    device, android, fake_adb, output, reason, shown
):
    scripted_start(android, output.replace("{activity}", f"{SHOP}/.Main").encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    details = info.value.details
    assert details["reason"] == reason
    assert details["package"] == SHOP
    output = details["output"]
    assert isinstance(output, str)
    assert output.startswith(shown)
    assert sent(fake_adb) == [RESOLVE, START]  # once, never again


def test_a_non_zero_exit_status_is_reported(device, android):
    scripted_start(android, b"Starting: x\nuiprobe-exit:255\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details == {
        "package": SHOP,
        "reason": "start_failed",
        "output": "Starting: x",
        "exit_status": 255,
    }
    assert "exit status 255" in info.value.message


def test_the_start_accepts_crlf_and_empty_lines(device, android):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main").replace("\n", "\r\n\r\n")
    scripted_start(android, output.encode())
    device.launch(SHOP)


def test_the_start_failure_escapes_what_it_shows(device, android):
    scripted_start(android, b"Error: \x1b]0;x\x07 oops\nuiprobe-exit:0\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["output"] == "Error: \\x1b]0;x\\x07 oops"
    assert "\x1b" not in info.value.message


@pytest.mark.parametrize("separator", ["\v", "\f", "\x1c", "\x85", "\u2028"])
def test_only_a_line_feed_ends_a_line(device, android, separator):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main").replace(
        "\nStatus: ok", f"{separator}Status: ok"
    )
    scripted_start(android, output.encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["reason"] == "unexpected_output"


def test_the_fake_answers_the_api_24_and_36_shapes(fake_adb):
    for api, serial in ((24, "emulator-5556"), (36, "emulator-5558")):
        FakeAndroid(fake_adb, serial, SCREENS, start="launcher", api=api)
        phone = Device(serial, adb_server=fake_adb.address)
        phone.launch(SHOP)
        phone.launch(SHOP)  # running: a Warning line
        assert phone.foreground() == SHOP


def test_launch_does_not_resend_after_a_connection_error(device, android, fake_adb):
    android.override(re.compile(r"^am start"), Reset())
    before = len(fake_adb.commands)
    with pytest.raises(AdbError):
        device.launch(SHOP)
    assert sent(fake_adb, before) == [RESOLVE, START]


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
    monkeypatch.setattr(device_module, "START_MIN_WAIT", 0.05)
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


def test_a_str_subclass_is_used_as_plain_text(device, android, fake_adb, monkeypatch):
    android.launch_in_front = False
    monkeypatch.setattr(device, "foreground", lambda: SHOP.upper())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(_OddText(SHOP), restart=True, timeout=0)
    assert sent(fake_adb) == [RESOLVE, STOP, START]
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
    assert sent(fake_adb) == [RESOLVE, STOP, START]


# --- uiprobe launch ---


@pytest.mark.usefixtures("clock")
def test_cli_launch_text(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "launch", SHOP)
    assert (code, out, err) == (0, f"launched {SHOP}\n", "")
    assert sent(fake_adb) == [RESOLVE, START]
    assert no_monkey(fake_adb)
    assert android.screen.package == SHOP


@pytest.mark.usefixtures("android", "clock")
def test_cli_launch_json(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "launch", SHOP, "--json")
    assert (code, err) == (0, "")
    data = json.loads(out)
    assert re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}",
        data.pop("captured_at"),
    )
    assert data == {
        "schema": "uiprobe.action/1",
        "action": "launch",
        "package": SHOP,
        "device": {"serial": SERIAL},
        "restart": False,
    }


@pytest.mark.usefixtures("android", "clock")
def test_cli_launch_restart_json_and_commands(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "launch", "--restart", SHOP, "--json")
    assert code == 0
    assert json.loads(out)["restart"] is True
    assert sent(fake_adb) == [RESOLVE, STOP, START]


@pytest.mark.usefixtures("android", "clock")
def test_cli_launch_options_before_the_command(capsys, fake_adb):
    fake_adb.add_device("emulator-5556")  # without -s the choice is ambiguous
    code, out, _ = invoke(capsys, fake_adb, "--json", "-s", SERIAL, "launch", SHOP)
    assert code == 0
    assert json.loads(out)["device"] == {"serial": SERIAL}
    assert invoke(capsys, fake_adb, "--json", "launch", SHOP)[0] != 0


@pytest.mark.usefixtures("android", "clock")
def test_cli_launch_missing_package_exits_15(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "launch", MISSING, "--json")
    assert (code, err) == (15, "")
    error = json.loads(out)["error"]
    assert error["code"] == "launch_failed"
    assert error["details"]["reason"] == "launcher_unreadable"
    assert sent(fake_adb) == [RESOLVE_PREFIX + MISSING]


@pytest.mark.usefixtures("clock")
def test_cli_launch_text_error_goes_to_stderr(capsys, fake_adb, android):
    android.override(re.compile(r"^am start"), b"hello\nuiprobe-exit:0\n")
    code, out, err = invoke(capsys, fake_adb, "launch", SHOP)
    assert (code, out) == (15, "")
    assert err.startswith(
        f"uiprobe: error: am start of {SHOP} printed an unknown line; its output:"
        " 'hello'"
    )


def test_cli_launch_not_in_front_exits_15_with_a_fake_clock(
    capsys, fake_adb, android, clock
):
    android.launch_in_front = False
    code, out, _ = invoke(
        capsys, fake_adb, "launch", SHOP, "--timeout", "1.5", "--json"
    )
    assert code == 15
    error = json.loads(out)["error"]
    assert error["details"]["reason"] == "not_in_front"
    assert error["details"]["timeout"] == 1.5
    assert clock.pauses == [0.5, 0.5, 0.5]


@pytest.mark.parametrize(
    "argv",
    [
        ["launch"],
        ["launch", "shop"],
        ["launch", "com.shop;reboot"],
        ["launch", "com.shop && reboot"],
        ["launch", "com.shop$(reboot)"],
        ["launch", "-c", "com.shop"],
        ["launch", SHOP, "--timeout", "abc"],
        ["launch", SHOP, "--timeout", "-1"],
        ["launch", SHOP, "--timeout", "601"],
        ["launch", SHOP, "--timeout", "1e2"],
        ["launch", SHOP, "--timeout", "600.001"],
        ["launch", SHOP, "--timeout", "0601"],
        ["launch", SHOP, "--timeout"],
        ["launch", SHOP, "--count", "5"],
        ["launch", SHOP, "extra.package"],
    ],
)
@pytest.mark.usefixtures("android")
def test_cli_launch_usage_errors_do_not_contact_the_server(capsys, fake_adb, argv):
    code, out, err = invoke(capsys, fake_adb, *argv, "--json")
    assert (code, err) == (2, "")
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []


@pytest.mark.parametrize(
    ("text", "seconds"), [("0.0001", 0.0001), ("10.0000", 10.0), ("0600", 600.0)]
)
@pytest.mark.usefixtures("clock")
def test_cli_launch_timeout_accepts_any_plain_decimal_in_range(
    capsys, fake_adb, android, text, seconds
):
    android.launch_in_front = False
    code, out, _ = invoke(capsys, fake_adb, "launch", SHOP, "--timeout", text, "--json")
    assert code == 15
    assert json.loads(out)["error"]["details"]["timeout"] == seconds


@pytest.mark.usefixtures("clock")
def test_cli_launch_default_timeout_is_ten_seconds(capsys, fake_adb, android):
    android.launch_in_front = False
    code, out, _ = invoke(capsys, fake_adb, "launch", SHOP, "--json")
    assert code == 15
    assert json.loads(out)["error"]["details"]["timeout"] == 10.0


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("1.0000000000000", 1.0), ("0" * 32, 0.0), ("599.999999999999999999999", 600.0)],
)
@pytest.mark.usefixtures("clock")
def test_cli_launch_timeout_takes_long_plain_decimals(
    capsys, fake_adb, android, text, seconds
):
    android.launch_in_front = False
    code, out, _ = invoke(capsys, fake_adb, "launch", SHOP, "--timeout", text, "--json")
    assert code == 15
    assert json.loads(out)["error"]["details"]["timeout"] == seconds


@pytest.mark.parametrize(
    "text", ["600.0000000000000001", "0" * 33, "0" * 10000, "1." + "0" * 40]
)
@pytest.mark.usefixtures("android")
def test_cli_launch_timeout_refuses_long_or_too_large_text(capsys, fake_adb, text):
    code, out, err = invoke(
        capsys, fake_adb, "launch", SHOP, "--timeout", text, "--json"
    )
    assert (code, err) == (2, "")
    message = json.loads(out)["error"]["message"]
    assert "at most 32 characters" in message
    assert len(message) < 200
    assert fake_adb.requests == []


@pytest.mark.usefixtures("clock")
def test_cli_launch_stop_failure_exits_15(capsys, fake_adb, android):
    android.answer_input(status=1)
    code, out, _ = invoke(capsys, fake_adb, "launch", SHOP, "--restart", "--json")
    assert code == 15
    assert json.loads(out)["error"]["details"]["reason"] == "stop_failed"
    assert sent(fake_adb) == [RESOLVE, STOP]


@pytest.mark.usefixtures("clock")
def test_cli_launch_restart_with_an_unreadable_launcher_sends_no_stop(
    capsys, fake_adb, android
):
    android.override(re.compile(r"^cmd package"), b"No activity found\n")
    code, out, _ = invoke(capsys, fake_adb, "launch", SHOP, "--restart", "--json")
    assert code == 15
    error = json.loads(out)["error"]
    assert error["details"] == {
        "package": SHOP,
        "reason": "launcher_unreadable",
        "output": "No activity found",
    }
    assert sent(fake_adb) == [RESOLVE]
    assert android.stopped == []
