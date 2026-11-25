"""Tests for constructing a `Device`, `devices()` and the device's small reads."""

from __future__ import annotations

import pytest

from tests.device.core.helpers import (
    SERIAL,
)
from tests.fake_adb_server import Fail, FakeAdbServer
from tests.helpers import unused_address
from uiprobe import adb
from uiprobe.adb import ServerAddress
from uiprobe.device import Device, devices
from uiprobe.errors import (
    AdbServerUnavailableError,
    DeviceNotFoundError,
    MultipleDevicesError,
)


def phone_server(phone: FakeAdbServer, *, default: bool) -> ServerAddress:
    """Point the default server at the fake (is_default keeps its meaning)."""
    return ServerAddress("127.0.0.1", phone.port, is_default=default)


def test_devices_lists_every_state(fake_adb):
    fake_adb.add_device("a", model="M")
    fake_adb.add_device("b", "unauthorized")
    found = devices(adb_server=fake_adb.address)
    assert [(d.serial, d.state, d.model) for d in found] == [
        ("a", "device", "M"),
        ("b", "unauthorized", None),
    ]


def test_device_selects_at_construction(phone):
    device = Device(adb_server=phone.address)
    assert device.serial == SERIAL
    assert device.info.model == "Pixel_8"


def test_device_fails_immediately_when_none_matches(phone):
    with pytest.raises(DeviceNotFoundError):
        Device("other", adb_server=phone.address)


def test_device_multiple_devices(phone):
    phone.add_device("emulator-5556")
    with pytest.raises(MultipleDevicesError):
        Device(adb_server=phone.address)
    assert Device("emulator-5556", adb_server=phone.address).serial == "emulator-5556"


def test_device_reports_unreachable_server():
    with pytest.raises(AdbServerUnavailableError):
        Device(adb_server=unused_address(), start_server=False)


def test_device_rejects_bad_server_address():
    with pytest.raises(ValueError, match="adb server argument"):
        Device(adb_server="localhost:5037")


def test_device_rejects_missing_adb_file(tmp_path):
    with pytest.raises(ValueError, match="adb argument"):
        devices(adb=tmp_path / "nope")


def test_device_rejects_non_string_arguments(phone):
    with pytest.raises(TypeError, match="adb_server must be a string"):
        Device(adb_server=5037)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="serial must be a string"):
        Device(5554, adb_server=phone.address)  # ty: ignore[invalid-argument-type]


def test_device_rejects_an_empty_server_address_and_serial(phone):
    with pytest.raises(ValueError, match="adb server argument"):
        Device(adb_server="")
    with pytest.raises(DeviceNotFoundError):
        Device("", adb_server=phone.address)


def test_a_stale_uiprobe_adb_does_not_break_an_explicit_server(
    phone, monkeypatch, tmp_path
):
    # adb is only ever run to start a default server nobody chose.
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    assert Device(adb_server=phone.address).serial == SERIAL
    assert [d.serial for d in devices(adb_server=phone.address)] == [SERIAL]
    monkeypatch.setenv("ADB_SERVER_SOCKET", phone.address)
    assert Device().serial == SERIAL


def test_a_stale_uiprobe_adb_does_not_break_no_start_server(
    phone, monkeypatch, tmp_path
):
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    assert Device(start_server=False).serial == SERIAL


def test_a_stale_uiprobe_adb_does_not_break_a_blocked_start(
    phone, monkeypatch, tmp_path
):
    # A non-empty ANDROID_ADB_SERVER_ADDRESS means adb is never started.
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "192.0.2.1")
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    assert Device().serial == SERIAL


def test_an_empty_android_adb_server_address_does_not_hide_a_stale_adb(
    phone, monkeypatch, tmp_path
):
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "")
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    with pytest.raises(ValueError, match="UIPROBE_ADB"):
        Device()


def test_a_stale_uiprobe_adb_still_fails_when_it_could_be_used(
    phone, monkeypatch, tmp_path
):
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    with pytest.raises(ValueError, match="UIPROBE_ADB"):
        Device()


def test_an_explicit_adb_argument_is_checked_even_when_unused(phone, tmp_path):
    with pytest.raises(ValueError, match="adb argument"):
        Device(adb_server=phone.address, adb=tmp_path / "nope")
    with pytest.raises(ValueError, match="adb argument"):
        devices(adb_server=phone.address, adb="")


def test_shell_decodes_with_replacement(phone):
    phone.on_shell(SERIAL, "echo hi", b"caf\xc3\xa9 \xff")
    assert Device(adb_server=phone.address)._shell("echo hi") == "café �"


def test_api_level_is_cached(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    device = Device(adb_server=phone.address)
    assert device.api_level() == 36
    assert device.api_level() == 36
    assert phone.requests.count("shell:getprop ro.build.version.sdk") == 1


def test_api_level_unknown_when_not_a_number(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"\n")
    device = Device(adb_server=phone.address)
    assert device.api_level() is None
    assert device.api_level() is None
    assert phone.requests.count("shell:getprop ro.build.version.sdk") == 1


@pytest.mark.parametrize(("output", "expected"), [(b"1\n", True), (b"\n", False)])
def test_is_emulator(phone, output, expected):
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", output)
    assert Device(adb_server=phone.address).is_emulator() is expected


def test_api_level_failure_is_none_and_not_remembered(phone):
    device = Device(adb_server=phone.address)
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", Fail("closed"))
    assert device.api_level() is None
    phone._shell[SERIAL].clear()
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"35\n")
    assert device.api_level() == 35


def test_api_level_ignores_absurdly_long_numbers(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"1" * 5000 + b"\n")
    assert Device(adb_server=phone.address).api_level() is None
