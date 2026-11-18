"""Tests for the command line."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import Fail
from tests.helpers import unused_address
from uiprobe import cli
from uiprobe.device import Device
from uiprobe.errors import DumpError, UiprobeError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.fake_adb_server import FakeAdbServer

DATA = Path(__file__).parent / "data"
SERIAL = "emulator-5554"


def invoke(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def error_code(out: str) -> str:
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.error/1"
    return payload["error"]["code"]


@pytest.fixture
def phone(fake_adb: FakeAdbServer) -> FakeAdbServer:
    fake_adb.add_device(SERIAL, model="Pixel_8")
    fake_adb.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    fake_adb.on_shell(SERIAL, "getprop ro.kernel.qemu", b"1\n")
    fake_adb.on_shell(
        SERIAL,
        "dumpsys window",
        b"  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3}\n",
    )
    fake_adb.on_shell(
        SERIAL,
        re.compile(r"^uiautomator dump "),
        b"UI hierchary dumped to: /data/local/tmp/x.xml\n",
    )
    fake_adb.on_exec(
        SERIAL, re.compile(r"^cat "), (DATA / "dump_login.xml").read_bytes()
    )
    return fake_adb


# --- devices ---


def test_devices_text_has_aligned_columns(fake_adb, capsys):
    fake_adb.add_device("emulator-5554", model="Pixel_8")
    fake_adb.add_device("R58M123", "unauthorized")
    code, out, err = invoke(capsys, "devices", "--adb-server", fake_adb.address)
    assert code == 0
    assert err == ""
    assert out.splitlines() == [
        "SERIAL         STATE         MODEL",
        "emulator-5554  device        Pixel_8",
        "R58M123        unauthorized",
    ]


def test_devices_text_does_not_query_the_devices(phone, capsys):
    code, _, _ = invoke(capsys, "devices", "--adb-server", phone.address)
    assert code == 0
    assert not any(r.startswith("host:transport:") for r in phone.requests)


def test_devices_text_with_no_devices_prints_the_header(fake_adb, capsys):
    code, out, _ = invoke(capsys, "devices", "--adb-server", fake_adb.address)
    assert code == 0
    assert out.split() == ["SERIAL", "STATE", "MODEL"]


def test_devices_json(phone, capsys):
    phone.add_device("emulator-5556", "offline")
    phone.add_device("R58M123", model="SM_G991")
    phone.on_shell("R58M123", "getprop ro.build.version.sdk", b"34\n")
    code, out, err = invoke(capsys, "devices", "--json", "--adb-server", phone.address)
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "schema": "uiprobe.devices/1",
        "devices": [
            {
                "serial": SERIAL,
                "state": "device",
                "model": "Pixel_8",
                "api_level": 36,
                "emulator": True,
            },
            {"serial": "emulator-5556", "state": "offline"},
            {
                "serial": "R58M123",
                "state": "device",
                "model": "SM_G991",
                "api_level": 34,
                "emulator": False,
            },
        ],
    }


def test_devices_json_omits_unknown_api_level(fake_adb, capsys):
    fake_adb.add_device("a")
    code, out, _ = invoke(capsys, "devices", "--json", "--adb-server", fake_adb.address)
    assert code == 0
    assert json.loads(out)["devices"] == [
        {"serial": "a", "state": "device", "emulator": False}
    ]


def test_devices_escapes_control_characters(fake_adb, capsys):
    fake_adb.add_device("a\x1b[31m")
    _, out, _ = invoke(capsys, "devices", "--adb-server", fake_adb.address)
    assert "\x1b" not in out
    assert "\\x1b" in out


def test_options_may_precede_or_follow_each_other(phone, capsys):
    code, out, _ = invoke(
        capsys, "devices", "--json", "-s", "x", "--adb-server", phone.address
    )
    assert code == 0
    assert json.loads(out)["schema"] == "uiprobe.devices/1"


# --- dump ---


def test_dump_text(phone, capsys):
    code, out, err = invoke(capsys, "dump", "--adb-server", phone.address)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert re.fullmatch(r"com\.example\.shop/\.Main  \(s-[0-9a-f]{12}\)", lines[0])
    assert any('TextView "ようこそ"' in line for line in lines)
    assert any("EditText id=email" in line and "[tap]" in line for line in lines)


def test_dump_json(phone, capsys):
    code, out, err = invoke(capsys, "dump", "--json", "--adb-server", phone.address)
    assert (code, err) == (0, "")
    assert out.startswith('{\n  "schema": "uiprobe.screen/1"')
    payload = json.loads(out)
    assert payload["device"] == {"serial": SERIAL, "api_level": 36}
    assert payload["app"] == {"package": "com.example.shop", "activity": ".Main"}
    assert payload["display"] == {"width": 1080, "height": 2400}
    assert "ようこそ" in out


def test_dump_selects_by_serial(phone, capsys):
    phone.add_device("emulator-5556")
    code, out, _ = invoke(
        capsys, "dump", "-s", SERIAL, "--json", "--adb-server", phone.address
    )
    assert code == 0
    assert json.loads(out)["device"]["serial"] == SERIAL


def test_dump_uses_adb_server_socket_variable(phone, capsys, monkeypatch):
    monkeypatch.setenv("ADB_SERVER_SOCKET", phone.address)
    code, _, _ = invoke(capsys, "dump", "--json")
    assert code == 0


# --- errors ---


def test_multiple_devices_json(phone, capsys):
    phone.add_device("emulator-5556")
    code, out, err = invoke(capsys, "dump", "--json", "--adb-server", phone.address)
    assert (code, err) == (4, "")
    payload = json.loads(out)
    assert payload["error"]["code"] == "multiple_devices"
    assert payload["error"]["details"] == {"serials": [SERIAL, "emulator-5556"]}


def test_multiple_devices_text(phone, capsys):
    phone.add_device("emulator-5556")
    code, out, err = invoke(capsys, "dump", "--adb-server", phone.address)
    assert (code, out) == (4, "")
    assert err == "uiprobe: error: 2 devices are connected; choose one with --serial\n"


def test_device_not_found(fake_adb, capsys):
    code, out, _ = invoke(capsys, "dump", "--json", "--adb-server", fake_adb.address)
    assert code == 4
    assert error_code(out) == "device_not_found"


def test_device_unauthorized_and_offline(fake_adb, capsys):
    fake_adb.add_device("a", "offline")
    code, out, _ = invoke(capsys, "dump", "--json", "--adb-server", fake_adb.address)
    assert (code, error_code(out)) == (5, "device_offline")
    fake_adb.add_device("b", "unauthorized")
    code, out, _ = invoke(capsys, "dump", "--json", "--adb-server", fake_adb.address)
    assert (code, error_code(out)) == (5, "device_unauthorized")


def test_server_unavailable(capsys):
    code, out, err = invoke(
        capsys, "devices", "--json", "--adb-server", unused_address()
    )
    assert (code, err) == (3, "")
    assert error_code(out) == "adb_server_unavailable"


def test_server_unavailable_text(capsys):
    code, out, err = invoke(
        capsys, "devices", "--no-start-server", "--adb-server", unused_address()
    )
    assert (code, out) == (3, "")
    assert err.startswith("uiprobe: error: adb server is unavailable")


def test_dump_failure_exit_code(phone, capsys):
    phone._shell[SERIAL].insert(0, (re.compile(r"^uiautomator dump "), b"ERROR\n"))
    code, out, _ = invoke(capsys, "dump", "--json", "--adb-server", phone.address)
    assert (code, error_code(out)) == (11, "dump_failed")


@pytest.mark.parametrize(
    "argv",
    [
        ["dump", "--bogus"],
        ["nosuch"],
        [],
        ["dump", "-s", "a", "--serial", "b"],
        ["dump", "--adb-server", "localhost:5037"],
        ["dump", "--adb", "/nonexistent/adb"],
        ["devices", "--adb-server", "tcp:127.0.0.1:99999"],
    ],
)
def test_usage_errors_text(capsys, argv):
    code, out, err = invoke(capsys, *argv)
    assert (code, out) == (2, "")
    assert err.startswith("uiprobe: error: ")


@pytest.mark.parametrize(
    "argv",
    [
        ["dump", "--json", "--bogus"],
        ["--json", "nosuch"],
        ["dump", "--json", "-s", "a", "-s", "b"],
        ["devices", "--json", "--adb-server", "nonsense"],
        ["dump", "--json", "--serial"],
    ],
)
def test_usage_errors_json(capsys, argv):
    code, out, err = invoke(capsys, *argv)
    assert (code, err) == (2, "")
    assert error_code(out) == "usage_error"


def test_abbreviated_options_are_not_accepted(capsys):
    code, _, _ = invoke(capsys, "dump", "--js")
    assert code == 2


def test_help_exits_zero(capsys):
    code, out, _ = invoke(capsys, "--help")
    assert code == 0
    assert "dump" in out


def test_keyboard_interrupt_json(monkeypatch, capsys):
    def interrupted(_args) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(cli._HANDLERS, "dump", interrupted)
    code, out, err = invoke(capsys, "dump", "--json")
    assert (code, err) == (130, "")
    assert error_code(out) == "interrupted"


def test_keyboard_interrupt_text(monkeypatch, capsys):
    def interrupted(_args) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(cli._HANDLERS, "dump", interrupted)
    code, out, err = invoke(capsys, "dump")
    assert (code, out) == (130, "")
    assert err == "uiprobe: error: interrupted\n"


def test_internal_error_json(monkeypatch, capsys):
    def broken(_args) -> int:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setitem(cli._HANDLERS, "dump", broken)
    code, out, _ = invoke(capsys, "dump", "--json")
    assert code == 1
    payload = json.loads(out)
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["details"] == {"type": "RuntimeError"}
    assert "boom" in payload["error"]["message"]


def test_internal_error_text_has_no_traceback(monkeypatch, capsys):
    def broken(_args) -> int:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setitem(cli._HANDLERS, "dump", broken)
    code, out, err = invoke(capsys, "dump")
    assert (code, out) == (1, "")
    assert err == "uiprobe: error: unexpected RuntimeError: boom\n"


@pytest.mark.parametrize("error", [ValueError, TypeError])
@pytest.mark.parametrize("as_json", [True, False])
def test_library_argument_errors_are_usage_errors(monkeypatch, capsys, error, as_json):
    def bad(_args) -> int:
        raise error("bad value")

    monkeypatch.setitem(cli._HANDLERS, "dump", bad)
    code, out, err = invoke(capsys, "dump", *(["--json"] if as_json else []))
    assert code == 2
    if as_json:
        assert error_code(out) == "usage_error"
    else:
        assert err == "uiprobe: error: bad value\n"


def test_a_type_error_from_the_library_reaches_the_user_as_usage_error(
    phone, capsys, monkeypatch
):
    monkeypatch.setattr(
        "uiprobe.cli.Device",
        lambda *_a, **_k: Device(5554, adb_server=phone.address),  # ty: ignore[invalid-argument-type]
    )
    code, out, _ = invoke(capsys, "dump", "--json")
    assert (code, error_code(out)) == (2, "usage_error")
    assert "serial must be a string" in json.loads(out)["error"]["message"]


def test_error_message_escapes_control_characters(monkeypatch, capsys):
    def bad(_args) -> int:
        msg = "x\x1b[2Jy"
        raise ValueError(msg)

    monkeypatch.setitem(cli._HANDLERS, "dump", bad)
    _, _, err = invoke(capsys, "dump")
    assert "\x1b" not in err


def test_main_exits_with_the_run_code(monkeypatch):
    monkeypatch.setattr("sys.argv", ["uiprobe", "nosuch"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2


def test_no_connection_leaves_loopback(phone, capsys, connections):
    invoke(capsys, "dump", "--adb-server", phone.address)
    assert connections
    assert {target[0] for target in connections} == {"127.0.0.1"}


# --- options before the command ---


def test_options_may_come_before_the_command(phone, capsys):
    phone.add_device("emulator-5556")
    code, out, err = invoke(
        capsys, "-s", SERIAL, "--json", "--adb-server", phone.address, "dump"
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["device"]["serial"] == SERIAL


def test_json_before_devices(phone, capsys):
    code, out, _ = invoke(capsys, "--json", "devices", "--adb-server", phone.address)
    assert code == 0
    assert json.loads(out)["schema"] == "uiprobe.devices/1"


def test_options_before_and_after_the_command_combine(phone, capsys):
    code, out, _ = invoke(capsys, "--adb-server", phone.address, "devices", "--json")
    assert code == 0
    assert json.loads(out)["schema"] == "uiprobe.devices/1"


def test_an_option_after_the_command_wins(phone, capsys):
    code, out, _ = invoke(
        capsys,
        "--adb-server",
        unused_address(),
        "devices",
        "--json",
        "--adb-server",
        phone.address,
    )
    assert code == 0
    assert json.loads(out)["devices"][0]["serial"] == SERIAL


def test_the_defaults_survive_when_no_option_is_given(phone, capsys, monkeypatch):
    monkeypatch.setenv("ADB_SERVER_SOCKET", phone.address)
    code, out, _ = invoke(capsys, "devices")
    assert code == 0
    assert out.startswith("SERIAL")


def test_no_start_server_before_the_command_is_honoured(capsys):
    code, out, err = invoke(
        capsys, "--no-start-server", "--adb-server", unused_address(), "devices"
    )
    assert (code, out) == (3, "")
    assert "adb server is unavailable" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["-s", "a", "dump", "-s", "b"],
        ["-s", "a", "-s", "b", "dump"],
        ["--serial", "a", "dump", "--serial", "b"],
        ["-s", "a", "dump", "--serial", "a"],
    ],
)
def test_serial_given_twice_in_any_position_is_a_usage_error(capsys, argv):
    code, out, _ = invoke(capsys, "--json", *argv)
    assert (code, error_code(out)) == (2, "usage_error")
    assert "only once" in json.loads(out)["error"]["message"]


def test_json_before_the_command_makes_usage_errors_json(capsys):
    code, out, _ = invoke(capsys, "--json", "dump", "--bogus")
    assert (code, error_code(out)) == (2, "usage_error")


# --- empty values ---


def test_empty_adb_server_is_a_usage_error_not_the_default_server(capsys):
    code, out, _ = invoke(capsys, "devices", "--json", "--adb-server", "")
    assert (code, error_code(out)) == (2, "usage_error")
    assert "adb server argument" in json.loads(out)["error"]["message"]


def test_empty_adb_is_a_usage_error(phone, capsys):
    code, out, _ = invoke(
        capsys, "devices", "--json", "--adb", "", "--adb-server", phone.address
    )
    assert (code, error_code(out)) == (2, "usage_error")


def test_empty_serial_is_a_missing_device_not_auto_selection(phone, capsys):
    code, out, _ = invoke(
        capsys, "dump", "--json", "-s", "", "--adb-server", phone.address
    )
    assert (code, error_code(out)) == (4, "device_not_found")


# --- devices --json ---


def test_devices_json_lists_a_device_that_cannot_be_probed(phone, capsys):
    phone.add_device("flaky")
    phone.on_shell("flaky", "getprop ro.build.version.sdk", b"33\n")
    phone.fail_request("host:transport:flaky", "device offline")
    code, out, _ = invoke(capsys, "devices", "--json", "--adb-server", phone.address)
    assert code == 0
    by_serial = {d["serial"]: d for d in json.loads(out)["devices"]}
    assert by_serial["flaky"] == {"serial": "flaky", "state": "device"}
    assert by_serial[SERIAL]["api_level"] == 36
    assert by_serial[SERIAL]["emulator"] is True


def test_devices_json_keeps_api_level_when_the_emulator_probe_fails(phone, capsys):
    phone._shell[SERIAL].clear()
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", Fail("closed"))
    _, out, _ = invoke(capsys, "devices", "--json", "--adb-server", phone.address)
    assert json.loads(out)["devices"][0] == {
        "serial": SERIAL,
        "state": "device",
        "model": "Pixel_8",
        "api_level": 36,
    }


def test_devices_json_needs_a_single_listing(phone, capsys):
    for serial in ("b", "c", "d"):
        phone.add_device(serial)
    invoke(capsys, "devices", "--json", "--adb-server", phone.address)
    assert phone.requests.count("host:devices-l") == 1
    assert len([r for r in phone.requests if r.startswith("host:transport:")]) == 8


# --- output encoding and closed pipes ---

EMOJI_XML = (
    "<hierarchy><node class='android.widget.TextView' package='p' "
    "text='Café 😀 ようこそ' bounds='[0,0][1,1]'/></hierarchy>"
).encode()


class _Capture:
    """A stdout that, like a piped Windows stdout, uses a legacy code page."""

    def __init__(self, encoding: str) -> None:
        self.buffer = io.BytesIO()
        self.stream = io.TextIOWrapper(self.buffer, encoding=encoding)

    def output(self) -> bytes:
        self.stream.flush()
        return self.buffer.getvalue()


def run_main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["uiprobe", *argv])
    try:
        with pytest.raises(SystemExit) as caught:
            cli.main()
    except KeyboardInterrupt:
        # Without this, pytest would abort the whole session.
        pytest.fail("main() let KeyboardInterrupt escape")
    assert isinstance(caught.value.code, int)
    return caught.value.code


@pytest.mark.parametrize("encoding", ["cp932", "cp1252", "ascii"])
def test_json_output_is_utf8_whatever_the_locale_encoding(phone, monkeypatch, encoding):
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), EMOJI_XML)
    capture = _Capture(encoding)
    monkeypatch.setattr(sys, "stdout", capture.stream)
    code = run_main(monkeypatch, "dump", "--json", "--adb-server", phone.address)
    assert code == 0
    payload = json.loads(capture.output().decode("utf-8"))
    assert payload["elements"][0]["text"] == "Café 😀 ようこそ"


@pytest.mark.parametrize("encoding", ["cp932", "cp1252"])
def test_devices_and_error_json_are_utf8_too(phone, monkeypatch, encoding):
    phone.add_device("日本語-😀", model="M")
    capture = _Capture(encoding)
    monkeypatch.setattr(sys, "stdout", capture.stream)
    run_main(monkeypatch, "devices", "--json", "--adb-server", phone.address)
    devices = json.loads(capture.output().decode("utf-8"))["devices"]
    assert devices[1]["serial"] == "日本語-😀"
    failed = _Capture(encoding)
    monkeypatch.setattr(sys, "stdout", failed.stream)
    code = run_main(
        monkeypatch, "dump", "--json", "-s", "日本語-😀x", "--adb-server", phone.address
    )
    assert code == 4
    message = json.loads(failed.output().decode("utf-8"))["error"]["message"]
    assert "日本語-😀x" in message


def test_text_output_keeps_the_locale_encoding_and_never_crashes(phone, monkeypatch):
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), EMOJI_XML)
    capture = _Capture("cp1252")
    monkeypatch.setattr(sys, "stdout", capture.stream)
    code = run_main(monkeypatch, "dump", "--adb-server", phone.address)
    assert code == 0
    assert "Café".encode("cp1252") in capture.output()
    assert b"\\U0001f600" in capture.output()


@pytest.fixture
def closed_pipe_stdout() -> Iterator[io.TextIOWrapper]:
    """A text stream on a pipe whose reader is gone.

    The teardown points the descriptor at the null device and then closes the
    stream, even when the test failed first. Otherwise the buffered text would
    be flushed to the dead pipe when the stream is garbage collected; on
    Windows that fails and surfaces as an unraisable-exception warning in an
    unrelated later test.
    """
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    stream = io.TextIOWrapper(os.fdopen(write_fd, "wb"), encoding="utf-8")
    try:
        yield stream
    finally:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, stream.fileno())
            finally:
                os.close(devnull)
        except (OSError, ValueError):
            pass
        with contextlib.suppress(OSError):
            stream.close()


@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_closed_stdout_after_a_small_output_exits_with_a_defined_code(
    phone, monkeypatch, capfd, closed_pipe_stdout, flag
):
    stdout = closed_pipe_stdout
    monkeypatch.setattr(sys, "stdout", stdout)
    code = run_main(monkeypatch, "dump", *flag, "--adb-server", phone.address)
    assert code == 1  # not 120, Python's code for a failed flush at shutdown
    assert capfd.readouterr().err == ""
    stdout.write("nothing is left to fail")
    stdout.flush()  # the descriptor now leads to /dev/null


@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_closed_stdout_in_the_middle_of_a_large_output(
    monkeypatch, capfd, closed_pipe_stdout, flag
):
    def big(_args) -> int:
        cli._emit("x" * 1_000_000)
        return 0

    monkeypatch.setitem(cli._HANDLERS, "dump", big)
    monkeypatch.setattr(sys, "stdout", closed_pipe_stdout)
    code = run_main(monkeypatch, "dump", *flag)
    assert code == 1
    assert capfd.readouterr().err == ""


def test_broken_pipe_inside_a_command_prints_no_error_object(monkeypatch, capsys):
    def gone(_args) -> int:
        raise BrokenPipeError

    monkeypatch.setitem(cli._HANDLERS, "dump", gone)
    code, out, err = invoke(capsys, "dump", "--json")
    assert (code, out, err) == (1, "", "")


def test_main_passes_the_exit_code_through_when_stdout_is_fine(
    phone, monkeypatch, capsys
):
    code = run_main(monkeypatch, "devices", "--adb-server", phone.address)
    assert code == 0
    assert capsys.readouterr().out.startswith("SERIAL")


def test_host_name_is_a_usage_error_before_any_lookup(capsys, monkeypatch, connections):
    def refuse(*_args, **_kwargs):
        msg = "name resolution attempted"
        raise AssertionError(msg)

    monkeypatch.setattr("socket.getaddrinfo", refuse)
    code, out, err = invoke(
        capsys, "devices", "--adb-server", "tcp:adb.example.invalid:5037"
    )
    assert (code, out) == (2, "")
    assert "never resolved" in err
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:adb.example.invalid:5037")
    code, out, err = invoke(capsys, "devices")
    assert (code, out) == (2, "")
    assert "ADB_SERVER_SOCKET" in err
    assert connections == []


class _ClosedPipe(io.StringIO):
    """A stdout whose reader is gone: every write fails."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


class _FailingOutput(io.StringIO):
    """An output whose writes and flushes fail with a given OSError errno.

    `descriptor` is what `fileno()` reports; without one the stream has no
    descriptor, like an in-memory stream.
    """

    def __init__(
        self,
        descriptor: int | None = None,
        *,
        error: int = errno.EINVAL,
        fail_write: bool = True,
        fail_flush: bool = True,
    ) -> None:
        super().__init__()
        self._descriptor = descriptor
        self._error = error
        self._fail_write = fail_write
        self._fail_flush = fail_flush

    def fileno(self) -> int:
        if self._descriptor is None:
            return super().fileno()
        return self._descriptor

    def write(self, text: str) -> int:
        if self._fail_write:
            raise OSError(self._error, os.strerror(self._error))
        return super().write(text)

    def flush(self) -> None:
        if self._fail_flush:
            raise OSError(self._error, os.strerror(self._error))


@pytest.fixture
def pipe_descriptor() -> Iterator[int]:
    """The write end of a real pipe; Windows reports a pipe as a FIFO as well."""
    read_fd, write_fd = os.pipe()
    try:
        yield write_fd
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.fixture
def file_descriptor(tmp_path) -> Iterator[int]:
    """The descriptor of a regular file, which is not a pipe."""
    with (tmp_path / "out.txt").open("w") as handle:
        yield handle.fileno()


def _fail_with(monkeypatch, exc: BaseException) -> None:
    def fail(_args) -> int:
        raise exc

    monkeypatch.setitem(cli._HANDLERS, "dump", fail)


@pytest.fixture
def redirected(monkeypatch):
    """Record `_discard_output` calls so no test touches real descriptors."""
    calls: list[object] = []
    monkeypatch.setattr(cli, "_discard_output", calls.append)
    return calls


def test_broken_pipe_while_reporting_an_error_exits_1_without_redirecting(
    monkeypatch, redirected
):
    _fail_with(monkeypatch, ValueError("bad value"))
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    assert cli.run(["dump", "--json"]) == 1
    assert redirected == []  # run() leaves descriptors to main()


def test_broken_pipe_on_stderr_while_reporting_a_message_exits_1(
    monkeypatch, redirected
):
    _fail_with(monkeypatch, ValueError("bad value"))
    monkeypatch.setattr(sys, "stderr", _ClosedPipe())
    assert cli.run(["dump"]) == 1
    assert redirected == []


@pytest.mark.usefixtures("redirected")
def test_broken_pipe_while_reporting_keeps_one_exit_code_for_every_error(monkeypatch):
    assert DumpError.exit_code != 1
    _fail_with(monkeypatch, DumpError("boom"))
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    assert cli.run(["dump", "--json"]) == 1


def test_main_discards_a_broken_stdout_after_reporting_an_error(
    monkeypatch, redirected
):
    _fail_with(monkeypatch, KeyError("k"))
    stdout = _ClosedPipe()
    monkeypatch.setattr(sys, "stdout", stdout)
    assert run_main(monkeypatch, "dump", "--json") == 1
    assert redirected == [stdout]


def test_main_discards_only_the_stream_that_broke(monkeypatch, redirected):
    _fail_with(monkeypatch, ValueError("bad value"))
    stderr = _ClosedPipe()
    monkeypatch.setattr(sys, "stderr", stderr)
    assert run_main(monkeypatch, "dump") == 1
    assert redirected == [stderr]


def test_closed_stderr_while_reporting_a_message_exits_1_silently(
    monkeypatch, capfd, closed_pipe_stdout
):
    _fail_with(monkeypatch, ValueError("bad value"))
    stderr = closed_pipe_stdout
    monkeypatch.setattr(sys, "stderr", stderr)
    assert run_main(monkeypatch, "dump") == 1
    assert capfd.readouterr().out == ""
    stderr.write("nothing is left to fail")
    stderr.flush()  # the descriptor now leads to /dev/null


def test_a_missing_stdout_object_keeps_the_exit_code(monkeypatch):
    # With descriptor 1 closed at startup Python sets sys.stdout to None.
    _fail_with(monkeypatch, ValueError("bad value"))
    monkeypatch.setattr(sys, "stdout", None)
    assert run_main(monkeypatch, "dump", "--json") == 2


def test_discard_output_redirects_the_descriptor_to_the_null_device(tmp_path):
    path = tmp_path / "out.txt"
    with path.open("w") as stream:
        cli._discard_output(stream)
        stream.write("lost")
    assert path.read_text() == ""


def test_json_output_escapes_terminal_controls_in_every_command(phone, capsys):
    phone.add_device("odd\x9b2J\u202e", model="M")
    code, out, _ = invoke(capsys, "devices", "--json", "--adb-server", phone.address)
    assert code == 0
    assert "\x9b" not in out
    assert "\\u009b" in out
    assert "\\u202e" in out
    assert {d["serial"] for d in json.loads(out)["devices"]} >= {"odd\x9b2J\u202e"}


def test_error_json_escapes_controls(monkeypatch, capsys):
    def fail(_args) -> int:
        raise UiprobeError("bad \x9b2J \u202e text")

    monkeypatch.setitem(cli._HANDLERS, "dump", fail)
    code, out, _ = invoke(capsys, "dump", "--json")
    assert code != 0
    assert "\x9b" not in out
    assert "\u202e" not in out
    assert json.loads(out)["error"]["message"] == "bad \x9b2J \u202e text"


def test_dump_json_escapes_controls_from_the_screen(phone, capsys):
    xml = (
        (DATA / "dump_login.xml")
        .read_bytes()
        .replace("ようこそ".encode(), b"&#x9b;2J&#x202e;")
    )
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), xml)
    code, out, _ = invoke(capsys, "dump", "--json", "--adb-server", phone.address)
    assert code == 0
    assert "\x9b" not in out
    assert "\u202e" not in out
    assert "\\u009b2J" in out


@pytest.mark.usefixtures("redirected")
@pytest.mark.parametrize(
    ("stream_name", "flag"), [("stdout", ["--json"]), ("stderr", [])]
)
def test_windows_closed_output_is_handled_like_a_broken_pipe(
    monkeypatch, capfd, pipe_descriptor, stream_name, flag
):
    monkeypatch.setattr(sys, "platform", "win32")
    _fail_with(monkeypatch, ValueError("bad value"))
    monkeypatch.setattr(sys, stream_name, _FailingOutput(pipe_descriptor))
    assert cli.run(["dump", *flag]) == 1
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_windows_closed_stdout_on_the_success_path_exits_1(
    monkeypatch, redirected, pipe_descriptor
):
    def hello(_args) -> int:
        cli._emit("hello")
        return 0

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(cli._HANDLERS, "dump", hello)
    stdout = _FailingOutput(pipe_descriptor, fail_write=False)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert run_main(monkeypatch, "dump") == 1
    assert redirected == [stdout]  # the flush found the reader gone


@pytest.mark.usefixtures("redirected")
@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_windows_closed_stdout_in_the_middle_of_a_large_output(
    monkeypatch, capfd, pipe_descriptor, flag
):
    def big(_args) -> int:
        cli._emit("x" * 1_000_000)
        return 0

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(cli._HANDLERS, "dump", big)
    monkeypatch.setattr(sys, "stdout", _FailingOutput(pipe_descriptor))
    assert cli.run(["dump", *flag]) == 1
    captured = capfd.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_invalid_argument_is_unexpected_on_other_platforms(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    stdout = _FailingOutput()
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(OSError, match="Invalid argument"):
        cli._emit("text")
    code = cli._failure_code(OSError(errno.EINVAL, "Invalid argument"), as_json=False)
    assert code == 1
    assert "unexpected OSError" in capsys.readouterr().err


def test_windows_invalid_argument_from_a_command_is_still_unexpected(
    monkeypatch, capsys
):
    monkeypatch.setattr(sys, "platform", "win32")
    _fail_with(monkeypatch, OSError(errno.EINVAL, "Invalid argument"))
    code, out, err = invoke(capsys, "dump", "--json")
    assert code == 1
    assert json.loads(out)["error"]["details"] == {"type": "OSError"}
    assert err == ""


@pytest.mark.parametrize("descriptor_kind", ["file", "none"])
def test_windows_invalid_argument_on_a_stream_that_is_not_a_pipe_is_unexpected(
    monkeypatch, redirected, request, descriptor_kind
):
    descriptor = (
        request.getfixturevalue("file_descriptor")
        if descriptor_kind == "file"
        else None
    )
    monkeypatch.setattr(sys, "platform", "win32")
    stdout = _FailingOutput(descriptor)
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(OSError, match="Invalid argument"):
        cli._emit("text")
    stdout._fail_write = False
    with pytest.raises(OSError, match="Invalid argument"):
        cli._flush_streams(0)
    assert redirected == []


@pytest.mark.parametrize(
    ("platform", "error", "stream_is_pipe"),
    [
        ("linux", errno.EINVAL, True),
        ("win32", errno.ENOSPC, True),
        ("win32", errno.EINVAL, False),
    ],
)
def test_flush_errors_that_are_not_a_closed_output_propagate(
    monkeypatch,
    redirected,
    pipe_descriptor,
    file_descriptor,
    platform,
    error,
    stream_is_pipe,
):
    monkeypatch.setattr(sys, "platform", platform)
    descriptor = pipe_descriptor if stream_is_pipe else file_descriptor
    stdout = _FailingOutput(descriptor, error=error, fail_write=False)
    monkeypatch.setattr(sys, "stdout", stdout)
    with pytest.raises(OSError, match=os.strerror(error)):
        cli._flush_streams(0)
    assert redirected == []


@pytest.mark.parametrize("flag", [[], ["--json"]])
@pytest.mark.parametrize("command", ["dump", "devices"])
def test_real_commands_write_through_emit_so_a_closed_windows_pipe_exits_1(
    monkeypatch, capsys, redirected, phone, pipe_descriptor, command, flag
):
    emitted: list[str] = []
    real_emit = cli._emit

    def spy(text: str, *, stream=None) -> None:
        emitted.append(text)
        real_emit(text, stream=stream)

    monkeypatch.setattr(cli, "_emit", spy)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", _FailingOutput(pipe_descriptor))
    code = cli.run([command, *flag, "--adb-server", phone.address])
    assert code == 1
    assert capsys.readouterr().err == ""
    assert redirected == []
    # The only thing written is the command's own result, not an error report.
    assert len(emitted) == 1
    assert "uiprobe.error" not in emitted[0]
    assert not emitted[0].startswith("uiprobe: error")


def test_main_interrupt_while_configuring_the_streams(monkeypatch, capsys):
    def interrupted(_argv) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_configure_streams", interrupted)
    code = run_main(monkeypatch, "dump", "--json")
    assert code == 130
    assert error_code(capsys.readouterr().out) == "interrupted"


def test_main_interrupt_while_flushing_the_streams(monkeypatch, capsys):
    calls = []

    def flush(code: int) -> int:
        calls.append(code)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return code

    monkeypatch.setattr(cli, "_flush_streams", flush)
    monkeypatch.setitem(cli._HANDLERS, "dump", lambda _args: 0)
    code = run_main(monkeypatch, "dump", "--json")
    assert code == 130
    assert error_code(capsys.readouterr().out) == "interrupted"


def test_main_interrupt_text_mode(monkeypatch, capsys):
    def interrupted(_argv) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_configure_streams", interrupted)
    code = run_main(monkeypatch, "dump")
    assert code == 130
    assert capsys.readouterr().err == "uiprobe: error: interrupted\n"


def test_main_interrupt_ignores_json_after_double_dash(monkeypatch, capsys):
    def interrupted(_argv) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_configure_streams", interrupted)
    code = run_main(monkeypatch, "find", "--text", "x", "--", "--json")
    assert code == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "uiprobe: error: interrupted\n"


def test_main_interrupt_discards_output_even_when_the_report_fails(
    monkeypatch, redirected
):
    def interrupted(_argv) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_configure_streams", interrupted)
    stderr = _ClosedPipe()
    monkeypatch.setattr(sys, "stderr", stderr)
    assert run_main(monkeypatch, "dump") == 130
    assert redirected == [stderr]


@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_main_interrupt_with_a_dead_reader_redirects_the_descriptor(monkeypatch, flag):
    """The descriptor must point at the null device, or shutdown fails with 120."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    # Line buffering on stderr matches CPython's own stderr; the text-mode
    # failure does not show without it.
    stdout = io.TextIOWrapper(os.fdopen(os.dup(write_fd), "wb"), encoding="utf-8")
    stderr = io.TextIOWrapper(
        os.fdopen(write_fd, "wb"), encoding="utf-8", line_buffering=True
    )
    carrier = stdout if flag else stderr
    try:
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)

        def interrupted(_argv) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_configure_streams", interrupted)
        assert run_main(monkeypatch, "dump", *flag) == 130
        carrier.write("after\n")
        carrier.flush()  # would raise if the descriptor still led to the dead pipe
    finally:
        for stream in (stdout, stderr):
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull, stream.fileno())
                finally:
                    os.close(devnull)
            except (OSError, ValueError):
                pass
            with contextlib.suppress(OSError):
                stream.close()
