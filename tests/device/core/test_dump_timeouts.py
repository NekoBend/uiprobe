"""Tests for the dump's timeouts and hostile XML."""

from __future__ import annotations

import re
import time

import pytest

import uiprobe
from tests.device.core.helpers import (
    SERIAL,
    requests_of,
    script_dump,
)
from tests.fake_adb_server import Fail, FakeAdbServer, Stall, Trickle
from uiprobe import adb
from uiprobe.device import Device
from uiprobe.errors import (
    DumpError,
)


def slow_dump(server: FakeAdbServer, seconds: float) -> list[str]:
    """Make the dump command slow; return the commands the slow reply served.

    The fake server uses the first script that matches, so the slow reply is
    put in front (``first=True``) of the immediate one that `script_dump`
    registered.
    """
    served: list[str] = []

    def respond(command: str) -> Trickle:
        served.append(command)
        data = b"UI hierchary dumped to: /data/local/tmp/x.xml\n"
        return Trickle(data, chunk_size=len(data), lead=seconds)

    server.on_shell(
        SERIAL,
        re.compile(r"^uiautomator dump /data/local/tmp/uiprobe-"),
        respond,
        first=True,
    )
    return served


def test_a_slow_dump_outlasts_the_client_timeout(phone, monkeypatch):
    # The client's per-read timeout (10 s, shortened here) must not cut a dump
    # that is merely slow: uiautomator is silent until it has finished.
    monkeypatch.setitem(adb.AdbClient.__init__.__kwdefaults__, "timeout", 0.2)
    monkeypatch.setattr("uiprobe.device.reads.DUMP_TIMEOUT", 5.0)
    script_dump(phone)
    served = slow_dump(phone, 0.6)
    started = time.perf_counter()
    assert Device(adb_server=phone.address).dump().package == "com.example.shop"
    assert len(served) == 1
    # perf_counter, not monotonic: Python 3.12's monotonic() moves in 15.6 ms
    # steps on Windows and read this 0.6 s dump as 0.594 s. The bound stays
    # below 0.6 s because the fake server's Event.wait (Trickle.lead) can end
    # slightly early on Windows timers; 0.5 s is still far above the 0.2 s
    # client timeout.
    assert time.perf_counter() - started >= 0.5


def test_a_dump_that_never_finishes_is_a_dump_error_not_an_adb_error(
    phone, monkeypatch
):
    monkeypatch.setattr("uiprobe.device.reads.DUMP_TIMEOUT", 0.2)
    script_dump(phone)
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), Stall())
    phone._shell[SERIAL].insert(0, phone._shell[SERIAL].pop())
    with pytest.raises(DumpError) as caught:
        Device(adb_server=phone.address).dump()
    assert caught.value.code == "dump_failed"
    assert caught.value.exit_code == 11
    assert caught.value.details == {"reason": "timeout", "timeout_seconds": 0.2}
    assert isinstance(caught.value.__cause__, uiprobe.AdbError)
    assert caught.value.__cause__.details["stage"] == "output"
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_a_handshake_timeout_during_the_dump_stays_an_adb_error(phone, monkeypatch):
    monkeypatch.setitem(adb.AdbClient.__init__.__kwdefaults__, "timeout", 0.2)
    script_dump(phone)
    device = Device(adb_server=phone.address)
    phone.stall(f"host:transport:{SERIAL}")
    with pytest.raises(uiprobe.AdbError) as caught:
        device.dump()
    assert not isinstance(caught.value, DumpError)
    assert caught.value.exit_code == 10
    assert caught.value.details["reason"] == "timeout"
    assert caught.value.details["request"] == f"host:transport:{SERIAL}"


def test_a_stalled_shell_okay_during_the_dump_stays_an_adb_error(phone, monkeypatch):
    # The shell service never answers OKAY: nothing has been read from
    # uiautomator yet, so this is a stalled handshake and not a slow dump.
    monkeypatch.setitem(adb.AdbClient.__init__.__kwdefaults__, "timeout", 0.2)
    monkeypatch.setattr("secrets.token_hex", lambda _nbytes: "0123456789abcdef")
    script_dump(phone)
    request = "shell:uiautomator dump /data/local/tmp/uiprobe-0123456789abcdef.xml"
    phone.stall(request)
    device = Device(adb_server=phone.address)
    with pytest.raises(uiprobe.AdbError) as caught:
        device.dump()
    assert not isinstance(caught.value, DumpError)
    assert caught.value.exit_code == 10
    assert caught.value.details["reason"] == "timeout"
    assert caught.value.details["request"] == request
    assert caught.value.details["timeout_seconds"] == 0.2
    assert "stage" not in caught.value.details


def test_other_adb_errors_during_the_dump_stay_adb_errors(phone):
    script_dump(phone)
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), Fail("closed"))
    phone._shell[SERIAL].insert(0, phone._shell[SERIAL].pop())
    with pytest.raises(uiprobe.AdbError) as caught:
        Device(adb_server=phone.address).dump()
    assert not isinstance(caught.value, DumpError)


def test_the_dump_command_gets_the_dump_timeout_and_other_commands_do_not(
    phone, monkeypatch
):
    script_dump(phone)
    device = Device(adb_server=phone.address)
    seen: dict[str, float | None] = {}
    real = device._client.shell

    def spy(serial, command, *, timeout=None):
        seen[command.split()[0]] = timeout
        return real(serial, command, timeout=timeout)

    monkeypatch.setattr(device._client, "shell", spy)
    device.dump()
    assert seen["uiautomator"] == 60.0
    assert seen["dumpsys"] is None
    assert seen["rm"] is None


def test_dump_rejects_hostile_xml_with_dump_error(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(
        SERIAL,
        re.compile(r"^cat "),
        b'<?xml version="1.0" encoding="bogus"?><hierarchy/>',
    )
    with pytest.raises(DumpError):
        Device(adb_server=phone.address).dump()
