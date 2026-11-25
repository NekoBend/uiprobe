"""Tests for the dump itself: the temporary file, failures and bad XML."""

from __future__ import annotations

import re
from datetime import datetime

import pytest

import uiprobe
from tests.device.core.helpers import (
    SERIAL,
    requests_of,
    script_dump,
)
from tests.fake_adb_server import Fail
from uiprobe.device import Device
from uiprobe.errors import (
    DumpError,
)


def test_dump_end_to_end(phone):
    script_dump(phone)
    screen = Device(adb_server=phone.address).dump()
    assert screen.serial == SERIAL
    assert screen.package == "com.example.shop"
    assert screen.activity == ".LoginActivity"
    assert screen.api_level == 36
    assert screen.display == (1080, 2400)
    assert [e.id for e in screen.elements][:2] == ["e1", "e2"]
    assert screen.elements[1].text == "ようこそ"
    assert screen.captured_at.utcoffset() is not None
    assert screen.captured_at.microsecond == 0
    assert isinstance(screen.captured_at, datetime)


def test_dump_removes_its_temporary_file(phone):
    script_dump(phone)
    Device(adb_server=phone.address).dump()
    (dump,) = requests_of(phone, "shell:uiautomator dump ")
    path = dump.rsplit(" ", 1)[1]
    assert path.startswith("/data/local/tmp/uiprobe-")
    assert requests_of(phone, "shell:rm ") == [f"shell:rm -f {path}"]
    assert requests_of(phone, "exec:") == [f"exec:cat {path}"]


def test_dump_uses_a_fresh_path_each_time(phone):
    script_dump(phone)
    device = Device(adb_server=phone.address)
    device.dump()
    device.dump()
    paths = {r.rsplit(" ", 1)[1] for r in requests_of(phone, "shell:rm ")}
    assert len(paths) == 2


def test_dump_removes_file_when_cat_fails(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), Fail("read failed"))
    with pytest.raises(uiprobe.AdbError):
        Device(adb_server=phone.address).dump()
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_dump_failure_keeps_the_original_error(phone):
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), b"ERROR: " + b"x" * 800)
    with pytest.raises(DumpError) as caught:
        Device(adb_server=phone.address).dump()
    output = caught.value.details["output"]
    assert isinstance(output, str)
    assert output.startswith("ERROR: x")
    assert len(output) == 500
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_dump_ignores_failing_cleanup(phone):
    script_dump(phone)
    phone.on_shell(SERIAL, re.compile(r"^rm "), Fail("rm refused"))
    # The scripts are matched in order: put the failing rm first.
    phone._shell[SERIAL].insert(0, phone._shell[SERIAL].pop())
    assert Device(adb_server=phone.address).dump().package == "com.example.shop"


def test_dump_cleanup_error_does_not_mask_dump_error(phone):
    phone.on_shell(SERIAL, re.compile(r"^rm "), Fail("rm refused"))
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), b"ERROR: no")
    with pytest.raises(DumpError):
        Device(adb_server=phone.address).dump()


def test_dump_bad_xml_is_a_dump_error(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), b"<hierarchy><node")
    with pytest.raises(DumpError):
        Device(adb_server=phone.address).dump()
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_dump_empty_hierarchy(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), b"<hierarchy rotation='0'/>")
    screen = Device(adb_server=phone.address).dump()
    assert (screen.package, screen.display, screen.elements) == ("", (0, 0), ())
