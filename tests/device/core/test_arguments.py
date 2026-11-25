"""Tests that argument types are checked before the adb server is contacted."""

from __future__ import annotations

import pytest

import uiprobe
from tests.helpers import unused_address
from uiprobe.device import Device, device_details, devices

# --- argument types are checked before the adb server is contacted ---


@pytest.mark.parametrize("serial", [5554, b"emulator-5554", ["a"]])
def test_device_rejects_a_serial_that_is_not_a_string_before_any_io(
    serial, connections
):
    with pytest.raises(TypeError, match="serial must be a string"):
        Device(serial, adb_server=unused_address())
    assert connections == []


@pytest.mark.parametrize("start_server", [1, 0, "no", None])
def test_device_rejects_a_start_server_that_is_not_a_bool_before_any_io(
    start_server, connections
):
    with pytest.raises(TypeError, match="start_server must be a bool"):
        Device(adb_server=unused_address(), start_server=start_server)
    assert connections == []


@pytest.mark.parametrize("adb_server", [5037, b"tcp:127.0.0.1:1", ["x"]])
def test_device_rejects_an_adb_server_that_is_not_a_string_before_any_io(
    adb_server, connections
):
    with pytest.raises(TypeError, match="adb_server must be a string"):
        Device(adb_server=adb_server)
    assert connections == []


def test_device_rejects_a_bad_adb_server_value_before_any_io(connections):
    with pytest.raises(ValueError, match="invalid address"):
        Device(adb_server="127.0.0.1:1")
    assert connections == []


@pytest.mark.parametrize("function", [devices, device_details])
def test_listing_rejects_wrong_argument_types_before_any_io(function, connections):
    address = unused_address()
    with pytest.raises(TypeError, match="start_server must be a bool"):
        function(adb_server=address, start_server=1)
    with pytest.raises(TypeError, match="adb_server must be a string"):
        function(adb_server=5037)
    assert connections == []


def test_wrong_types_never_start_adb_or_reach_a_running_server(phone, processes):
    with pytest.raises(TypeError):
        Device(5554, adb_server=phone.address)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        devices(adb_server=phone.address, start_server="yes")  # ty: ignore[invalid-argument-type]
    assert phone.requests == []
    assert processes == []


# --- package exports ---


def test_package_exports():
    for name in uiprobe.__all__:
        assert hasattr(uiprobe, name)
    assert uiprobe.Device is Device
    assert issubclass(uiprobe.DumpError, uiprobe.UiprobeError)
