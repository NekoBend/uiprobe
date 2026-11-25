"""Tests for ``uiprobe launch``."""

from __future__ import annotations

import json
import re
import sys

import pytest

from tests.device.launch.helpers import (
    MARK,
    MISSING,
    RESOLVE,
    RESOLVE_PREFIX,
    SCREENS,
    SERIAL,
    SHOP,
    START,
    STOP,
    no_monkey,
    sent,
)
from tests.fake_adb_server import FakeAdbServer
from tests.fake_device import FakeAndroid
from uiprobe import cli
from uiprobe.device import Device
from uiprobe.errors import AdbError


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


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
    assert sent(fake_adb) == [RESOLVE, STOP, RESOLVE, START]


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
    assert sent(fake_adb) == [RESOLVE_PREFIX + MISSING + MARK]


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


# --- an unpinned launch selects the device by its serial ---


@pytest.mark.usefixtures("clock")
def test_an_unpinned_launch_selects_the_device_by_serial_for_every_request(
    fake_adb,
):
    # As on main: each request goes to host:transport:SERIAL, also when the
    # server lists a transport id (no binding to a transport number).
    FakeAndroid(fake_adb, SERIAL, SCREENS, start="launcher", transport_id=7)
    device = Device(adb_server=fake_adb.address)
    assert device.transport_id is None  # not pinned
    device.launch(SHOP, restart=True)
    requests = fake_adb.requests
    selectors = [
        requests[i - 1]
        for i, r in enumerate(requests)
        if r in (f"shell:{RESOLVE}", f"shell:{STOP}", f"shell:{START}")
    ]
    assert selectors == [f"host:transport:{SERIAL}"] * 4  # read, stop, read, start
    assert not [r for r in requests if r.startswith("host:transport-id:")]


def test_there_is_no_public_send_check():
    assert not hasattr(Device, "set_send_check")
    assert not hasattr(Device, "send_check")


# --- a launch never goes on after an observed failure ---


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_a_launch_never_goes_on_through_a_server_started_again(
    tmp_path, monkeypatch, clock
):
    # The launcher read goes to the first server. Then that server is gone:
    # the next connection is refused, and the client would start a server
    # (here a second fake with the same serial on transport 8). The launch
    # ends instead, and neither the stop nor the start reaches the second.
    from tests.helpers import recording_adb, unused_port
    from uiprobe import adb as adb_module
    from uiprobe.adb import AdbClient, ServerAddress
    from uiprobe.errors import AdbServerUnavailableError

    assert clock.now == 1000.0
    port = unused_port()
    first = FakeAdbServer(port=port)
    first.start()
    FakeAndroid(first, SERIAL, SCREENS, start="launcher", transport_id=7)
    second = FakeAdbServer(port=port)
    replacement = FakeAndroid(second, SERIAL, SCREENS, start="launcher", transport_id=8)
    monkeypatch.setattr(
        adb_module, "DEFAULT_SERVER", ServerAddress("127.0.0.1", port, is_default=True)
    )
    monkeypatch.delenv("ANDROID_ADB_SERVER_ADDRESS", raising=False)
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    started: list[bool] = []

    def start_server(client: AdbClient) -> str | None:
        assert isinstance(client, AdbClient)
        started.append(True)
        second.start()
        return None

    monkeypatch.setattr(AdbClient, "_run_start_server", start_server)
    script, _ = recording_adb(tmp_path)
    try:
        device = Device(adb=script)
        original = device._launcher_activity

        def read_then_lose(package: str, **kwargs: bool) -> str:
            component = original(package, **kwargs)
            first.stop()  # the next connection is refused
            return component

        monkeypatch.setattr(device, "_launcher_activity", read_then_lose)
        with pytest.raises(AdbServerUnavailableError) as info:
            device.launch(SHOP, restart=True)
        assert info.value.details["start_skipped"] == "in_launch"
        assert started == []
        assert second.requests == []
        assert replacement.stopped == []
        assert not [c for _, _, c in first.commands if c.startswith("am ")]
    finally:
        first.stop()
        second.stop()


def test_a_launch_ends_when_a_read_swallows_a_failure(
    device, android, monkeypatch, fake_adb
):
    # A foreground read that hides a failed request still ends the launch:
    # nothing more is read or sent after it (the request trace ends there).
    android.launch_in_front = False
    original = device.foreground
    after: list[int] = []

    def failing_read() -> str | None:
        seen = original()
        device._failed_requests += 1  # as a read that swallowed an error
        after.append(len(fake_adb.requests))
        return seen

    monkeypatch.setattr(device, "foreground", failing_read)
    with pytest.raises(AdbError) as info:
        device.launch(SHOP)
    assert info.value.details["reason"] == "launch_interrupted"
    assert len(after) == 1
    assert len(fake_adb.requests) == after[0]  # no request after the failure


def test_a_launch_on_a_stopped_server_says_what_happened(
    fake_adb,
):
    # No request of a launch starts a server; the refusal says so (it does
    # not claim that the launch already sent a request).
    from uiprobe.errors import AdbServerUnavailableError

    FakeAndroid(fake_adb, SERIAL, SCREENS, start="launcher")
    device = Device(adb_server=fake_adb.address)
    fake_adb.stop()
    with pytest.raises(AdbServerUnavailableError) as info:
        device.launch(SHOP)
    assert info.value.details["start_skipped"] == "in_launch"
    assert "a launch never starts an adb server" in info.value.message
    assert "already sent" not in info.value.message


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_a_launch_never_reaches_a_server_started_for_it(tmp_path, monkeypatch, clock):
    # The Device chose its device on server 1, which then stops. A launch
    # with the activity given and restart=True would make the client start a
    # server (here server 2, the same serial on transport 8 with another
    # boot id): no request of the launch starts one, so neither the stop nor
    # the start reaches server 2.
    from tests.helpers import recording_adb, unused_port
    from uiprobe import adb as adb_module
    from uiprobe.adb import AdbClient, ServerAddress
    from uiprobe.errors import AdbServerUnavailableError

    assert clock.now == 1000.0
    port = unused_port()
    first = FakeAdbServer(port=port)
    first.start()
    FakeAndroid(first, SERIAL, SCREENS, start="launcher", transport_id=7)
    second = FakeAdbServer(port=port)
    replacement = FakeAndroid(second, SERIAL, SCREENS, start="launcher", transport_id=8)
    replacement.boot_id = "0f0f0f0f-0000-4000-8000-000000000000"
    monkeypatch.setattr(
        adb_module, "DEFAULT_SERVER", ServerAddress("127.0.0.1", port, is_default=True)
    )
    monkeypatch.delenv("ANDROID_ADB_SERVER_ADDRESS", raising=False)
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    started: list[bool] = []

    def start_server(client: AdbClient) -> str | None:
        assert isinstance(client, AdbClient)
        started.append(True)
        second.start()
        return None

    monkeypatch.setattr(AdbClient, "_run_start_server", start_server)
    script, _ = recording_adb(tmp_path)
    try:
        device = Device(adb=script)
        device.launcher_activity(SHOP)  # a request on server 1 first
        first.stop()
        with pytest.raises(AdbServerUnavailableError) as info:
            device.launch(SHOP, restart=True)
        assert info.value.details["start_skipped"] == "in_launch"
        assert started == []
        assert second.requests == []
        assert replacement.stopped == []
    finally:
        first.stop()
        second.stop()
