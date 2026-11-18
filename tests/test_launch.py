"""Tests for ``Device.launch`` and ``uiprobe launch``.

Every device is a strict `FakeAndroid`, so a command outside its allowlist
(any other monkey option, a count other than 1) fails the test. Time is fake:
nothing here sleeps.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from tests.fake_adb_server import FakeAdbServer, Reset
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import cli
from uiprobe import device as device_module
from uiprobe.device import Device
from uiprobe.errors import AdbError, LaunchFailedError

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
MISSING = "com.example.missing"
MONKEY = f"monkey -p {SHOP} -c android.intent.category.LAUNCHER 1"
STOP = f"am force-stop {SHOP}"

SCREENS = (
    Screen("launcher", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen("main", f"{SHOP}/.Main", (El((0, 0, 320, 100), text="Shop"),)),
    Screen("detail", f"{SHOP}/.Detail", (El((0, 0, 320, 100), text="Item"),)),
)


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
    """The shell commands received after the first `start`, without reads."""
    reads = ("dumpsys", "getprop", "wm size", "uiautomator", "rm ", "cat ")
    return [c for _, _, c in fake_adb.commands[start:] if not c.startswith(reads)]


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- Device.launch ---


def test_launch_sends_exactly_one_monkey_command(device, android, fake_adb):
    before = len(fake_adb.commands)
    assert device.launch(SHOP) is None
    assert sent(fake_adb, before) == [MONKEY]
    assert android.stopped == []
    assert android.screen.package == SHOP
    assert device.foreground() == SHOP


def test_launch_never_sends_another_monkey_option(device, fake_adb):
    device.launch(SHOP, restart=True)
    monkeys = [c for _, _, c in fake_adb.commands if c.startswith("monkey")]
    assert monkeys == [MONKEY]
    assert all(" -c android.intent.category.LAUNCHER 1" in c for c in monkeys)
    assert not any(re.search(r"--|-v|-s |--throttle", c) for c in monkeys)


def test_restart_sends_force_stop_first(device, android, fake_adb):
    device.launch(SHOP, restart=True)
    assert sent(fake_adb) == [STOP, MONKEY]
    assert android.stopped == [SHOP]


def test_restart_starts_a_fresh_task(device, android):
    device.launch(SHOP)
    android.show("detail")
    device.launch(SHOP)  # without restart the app resumes where it was
    assert android.screen.name == "detail"
    device.launch(SHOP, restart=True)
    assert android.screen.name == "main"
    assert android.stack == ["launcher", "main"]


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

    monkeypatch.setattr(android, "_launch", lambda *_: True)
    monkeypatch.setattr(device, "foreground", late)
    device.launch(SHOP)
    assert clock.pauses == [0.5, 0.5]
    assert len(reads) == 3


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
    assert sent(fake_adb) == [MONKEY]  # never sent twice


def test_launch_default_timeout_is_ten_seconds(device, android, clock):
    android.launch_in_front = False
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["timeout"] == 10.0
    assert sum(clock.pauses) == 10.0


def test_launch_accepts_padding_around_the_events_line(device, android):
    android.override(re.compile(r"^monkey"), b"  Events injected: 1  \r\n")
    android.show("main")
    device.launch(SHOP)


def test_launch_timeout_zero_reads_once(device, android, clock):
    android.launch_in_front = False
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=0)
    assert info.value.details["reason"] == "not_in_front"
    assert clock.pauses == []


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


def test_launch_of_a_missing_package(device, fake_adb):
    with pytest.raises(LaunchFailedError) as info:
        device.launch(MISSING)
    assert info.value.details == {
        "package": MISSING,
        "reason": "not_installed_or_no_launcher",
    }
    assert info.value.exit_code == 15
    assert sent(fake_adb) == [
        f"monkey -p {MISSING} -c android.intent.category.LAUNCHER 1"
    ]


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"** Error: Unable to connect\n",
        b"Events injected: 2\n",
        b"Events injected: 0\n",
        b"Events injected: 1\nEvents injected: 1\n",
        b"Events injected: 10\n",
        b"events injected: 1\n",
        b"xx Events injected: 1\n",
        b"Events injected: 1 and more\n",
    ],
)
def test_launch_unexpected_output(device, android, output):
    android.override(re.compile(r"^monkey"), output)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    details = info.value.details
    assert details["reason"] == "unexpected_output"
    assert details["package"] == SHOP
    expected = output.decode().splitlines()[0] if output else ""
    assert details["output"] == expected


def test_launch_unexpected_output_escapes_the_first_line(device, android):
    android.override(re.compile(r"^monkey"), b"\x1b]0;x\x07 oops\nEvents injected: 3\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["output"] == "\\x1b]0;x\\x07 oops"
    assert "\x1b" not in info.value.message


def test_launch_accepts_monkey_preamble_and_crlf(device, android):
    android.override(
        re.compile(r"^monkey"),
        b"  bash arg: -p\r\nargs: [-p]\r\nEvents injected: 1\r\n## Network stats\r\n",
    )
    android.show("main")
    device.launch(SHOP)


def test_launch_does_not_resend_after_a_connection_error(device, android, fake_adb):
    android.override(re.compile(r"^monkey"), Reset())
    before = len(fake_adb.commands)
    with pytest.raises(AdbError):
        device.launch(SHOP)
    assert sent(fake_adb, before) == [MONKEY]


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


# --- uiprobe launch ---


@pytest.mark.usefixtures("clock")
def test_cli_launch_text(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "launch", SHOP)
    assert (code, out, err) == (0, f"launched {SHOP}\n", "")
    assert sent(fake_adb) == [MONKEY]
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
    assert sent(fake_adb) == [STOP, MONKEY]


@pytest.mark.usefixtures("android", "clock")
def test_cli_launch_options_before_the_command(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "--json", "-s", SERIAL, "launch", SHOP)
    assert code == 0
    assert json.loads(out)["device"] == {"serial": SERIAL}


@pytest.mark.usefixtures("android", "clock")
def test_cli_launch_missing_package_exits_15(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "launch", MISSING, "--json")
    assert (code, err) == (15, "")
    error = json.loads(out)["error"]
    assert error["code"] == "launch_failed"
    assert error["details"]["reason"] == "not_installed_or_no_launcher"


@pytest.mark.usefixtures("clock")
def test_cli_launch_text_error_goes_to_stderr(capsys, fake_adb, android):
    android.override(re.compile(r"^monkey"), b"hello\n")
    code, out, err = invoke(capsys, fake_adb, "launch", SHOP)
    assert (code, out) == (15, "")
    assert err.startswith("uiprobe: error: monkey did not report one injected event")


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
