"""Tests of the adb socket client against the fake adb server."""

from __future__ import annotations

import _thread
import errno
import importlib
import os
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from tests.fake_adb_server import Fail, FakeAdbServer, Stall, Trickle
from tests.helpers import recording_adb, unused_port
from uiprobe import adb
from uiprobe.adb import (
    DEFAULT_SERVER,
    AdbClient,
    DeviceInfo,
    ServerAddress,
    find_adb,
    parse_devices_l,
    quote_args,
    resolve_server_address,
)
from uiprobe.errors import (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
)


def client_for(server: FakeAdbServer) -> AdbClient:
    host, port = server.address.removeprefix("tcp:").rsplit(":", 1)
    return AdbClient(ServerAddress(host, int(port)), timeout=2)


# --- ServerAddress and resolve_server_address ---


def test_server_address_str() -> None:
    assert str(DEFAULT_SERVER) == "tcp:127.0.0.1:5037"
    assert str(ServerAddress("::1", 9)) == "tcp:[::1]:9"
    assert DEFAULT_SERVER.is_default


def test_resolve_default_and_empty_environment_values() -> None:
    assert resolve_server_address(None, {}) is DEFAULT_SERVER
    env = {"ADB_SERVER_SOCKET": "", "ANDROID_ADB_SERVER_PORT": ""}
    assert resolve_server_address(None, env) is DEFAULT_SERVER


def test_resolve_empty_argument_is_an_error_not_the_default() -> None:
    # `--adb-server "$UNSET"` must not fall back to (and start) the local server.
    with pytest.raises(ValueError, match="adb server argument"):
        resolve_server_address("", {})
    with pytest.raises(ValueError, match="adb server argument"):
        resolve_server_address("", {"ADB_SERVER_SOCKET": "tcp:10.0.0.1:1"})


@pytest.mark.parametrize("value", [5037, b"tcp:10.0.0.1:1", ["tcp:10.0.0.1:1"]])
def test_resolve_rejects_non_string_argument(value: object) -> None:
    with pytest.raises(TypeError, match="adb_server must be a string"):
        resolve_server_address(value, {})  # ty: ignore[invalid-argument-type]


def test_resolve_rejects_huge_port_numbers() -> None:
    with pytest.raises(ValueError, match="invalid port"):
        resolve_server_address("tcp:127.0.0.1:" + "1" * 5000, {})


def test_resolve_uses_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:10.0.0.2:6000")
    assert resolve_server_address() == ServerAddress("10.0.0.2", 6000)


def test_resolve_order() -> None:
    env = {"ADB_SERVER_SOCKET": "tcp:10.0.0.3:1111", "ANDROID_ADB_SERVER_PORT": "2222"}
    assert resolve_server_address("tcp:10.0.0.4:1000", env) == ServerAddress(
        "10.0.0.4", 1000
    )
    assert resolve_server_address(None, env) == ServerAddress("10.0.0.3", 1111)
    port_only = {"ANDROID_ADB_SERVER_PORT": "2222"}
    address = resolve_server_address(None, port_only)
    assert address == ServerAddress("127.0.0.1", 2222)
    assert not address.is_default


def test_resolve_explicit_default_value_is_not_default() -> None:
    assert not resolve_server_address("tcp:127.0.0.1:5037", {}).is_default


def _no_lookup(*args: object, **_kwargs: object) -> None:
    msg = f"name resolution attempted: {args!r}"
    raise AssertionError(msg)


@pytest.mark.parametrize(
    "value",
    [
        "tcp:adb.example.invalid:5037",
        "tcp:example:5037",
        "tcp:127.0.0.1.example:5037",
        "tcp:[adb.example.invalid]:5037",
        "tcp:[127.0.0.1]:5037",
        "tcp:[localhost]:5037",
        "tcp:[LocalHost]:5037",
        "tcp:localhost.:5037",
        "tcp:1.2.3:5037",
    ],
)
def test_resolve_rejects_host_names_without_any_lookup(
    value: str, monkeypatch: pytest.MonkeyPatch, connections: list[tuple[object, ...]]
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    monkeypatch.setattr(socket, "gethostbyname", _no_lookup)
    with pytest.raises(ValueError, match=r"adb server argument.*never resolved"):
        resolve_server_address(value, {})
    with pytest.raises(ValueError, match="ADB_SERVER_SOCKET"):
        resolve_server_address(None, {"ADB_SERVER_SOCKET": value})
    assert connections == []


@pytest.mark.parametrize("host", ["localhost", "LocalHost", "LOCALHOST"])
def test_resolve_maps_localhost_without_resolution(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    address = resolve_server_address(f"tcp:{host}:5037", {})
    assert address == ServerAddress("127.0.0.1", 5037)
    assert not address.is_default
    env = {"ADB_SERVER_SOCKET": f"tcp:{host}:6000"}
    assert resolve_server_address(None, env) == ServerAddress("127.0.0.1", 6000)


def test_resolve_accepts_ip_literals() -> None:
    assert resolve_server_address("tcp:192.0.2.7:1", {}).host == "192.0.2.7"
    assert resolve_server_address("tcp:[2001:db8::1]:1", {}).host == "2001:db8::1"


def test_server_address_refuses_a_host_name() -> None:
    with pytest.raises(ValueError, match="never resolved"):
        ServerAddress("adb.example.invalid", 5037)
    assert ServerAddress("localhost", 1).host == "127.0.0.1"


def test_connecting_never_resolves_names(
    fake_adb: FakeAdbServer,
    monkeypatch: pytest.MonkeyPatch,
    connections: list[tuple[object, ...]],
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    monkeypatch.setattr(socket, "gethostbyname", _no_lookup)
    client = AdbClient(resolve_server_address(f"tcp:localhost:{fake_adb.port}", {}))
    assert client.server_version() == fake_adb.version
    assert connections == [("127.0.0.1", fake_adb.port)]


def test_connecting_to_an_ipv6_literal_uses_an_ipv6_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected: list[tuple[object, ...]] = []

    class Refusing:
        def __init__(self, family: int) -> None:
            connected.append(("family", family))

        def settimeout(self, value: float | None) -> None:
            pass

        def connect_ex(self, address: tuple[object, ...]) -> int:
            connected.append(address)
            return errno.ECONNREFUSED

        def close(self) -> None:
            pass

    monkeypatch.setattr(adb, "_new_socket", Refusing)
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    client = AdbClient(ServerAddress("::1", 9), start_server=False, timeout=0.2)
    with pytest.raises(AdbServerUnavailableError):
        client.server_version()
    assert connected == [("family", socket.AF_INET6), ("::1", 9)]


def _fake_if_nametoindex(name: str) -> int:
    if name == "eth0":
        return 7
    raise OSError("no such device")


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("tcp:[fe80::1%eth0]:5037", "fe80::1%eth0"),
        ("tcp:[fe80::1%3]:5037", "fe80::1%3"),
    ],
)
def test_resolve_accepts_scoped_ipv6(
    value: str, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    monkeypatch.setattr(socket, "if_nametoindex", _fake_if_nametoindex)
    assert resolve_server_address(value, {}) == ServerAddress(host, 5037)


@pytest.mark.parametrize(("zone", "index"), [("eth0", 7), ("3", 3)])
def test_connecting_to_a_scoped_literal_passes_the_scope_index(
    zone: str, index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    connected: list[tuple[object, ...]] = []

    class Refusing:
        def __init__(self, family: int) -> None:
            connected.append(("family", family))

        def settimeout(self, value: float | None) -> None:
            pass

        def connect_ex(self, address: tuple[object, ...]) -> int:
            connected.append(address)
            return errno.ECONNREFUSED

        def close(self) -> None:
            pass

    monkeypatch.setattr(adb, "_new_socket", Refusing)
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    monkeypatch.setattr(socket, "if_nametoindex", _fake_if_nametoindex)
    address = resolve_server_address(f"tcp:[fe80::1%{zone}]:9", {})
    client = AdbClient(address, start_server=False, timeout=0.2)
    with pytest.raises(AdbServerUnavailableError):
        client.server_version()
    assert connected == [("family", socket.AF_INET6), ("fe80::1", 9, 0, index)]


def test_unknown_interface_is_rejected_without_connecting(
    monkeypatch: pytest.MonkeyPatch, connections: list[tuple[object, ...]]
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    monkeypatch.setattr(socket, "if_nametoindex", _fake_if_nametoindex)
    with pytest.raises(ValueError, match=r"adb server argument.*unknown network"):
        resolve_server_address("tcp:[fe80::1%nope0]:5037", {})
    with pytest.raises(ValueError, match="ADB_SERVER_SOCKET"):
        resolve_server_address(None, {"ADB_SERVER_SOCKET": "tcp:[fe80::1%nope0]:5037"})
    assert connections == []


def test_start_server_child_environment_has_no_address_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[dict[str, str]] = []

    def fake_run(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        seen.append(env)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:1")
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", "1")
    monkeypatch.setenv("ADB_MDNS", "1")
    monkeypatch.setenv("UIPROBE_TEST_KEEP", "yes")
    client = AdbClient(
        ServerAddress("127.0.0.1", unused_port(), is_default=True),
        adb_path=tmp_path / "adb",
    )
    assert client._run_start_server() is None
    (env,) = seen
    assert not set(env) & {
        "ADB_SERVER_SOCKET",
        "ANDROID_ADB_SERVER_ADDRESS",
        "ANDROID_ADB_SERVER_PORT",
    }
    assert env["ADB_MDNS"] == "0"
    assert env["UIPROBE_TEST_KEEP"] == "yes"


@pytest.mark.parametrize(
    "zone",
    ["a..b", ".a", "a.", "a\x00b", "x" * 64, "e\u0301th0", "\u0663", "a b", "a%b", ""],
)
def test_bad_zone_names_are_rejected_before_any_lookup(
    zone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(name: str) -> int:
        msg = f"if_nametoindex called with {name!r}"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "if_nametoindex", lookup)
    monkeypatch.setattr(socket, "getaddrinfo", _no_lookup)
    with pytest.raises(ValueError, match="ADB_SERVER_SOCKET"):
        resolve_server_address(
            None, {"ADB_SERVER_SOCKET": f"tcp:[fe80::1%{zone}]:5037"}
        )


@pytest.mark.parametrize("zone", ["eth0", "wlan0.100", "br-lan_1", "a" * 63, "12"])
def test_well_formed_zone_names_reach_the_lookup(
    zone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up: list[str] = []

    def lookup(name: str) -> int:
        looked_up.append(name)
        return 2

    monkeypatch.setattr(socket, "if_nametoindex", lookup)
    address = resolve_server_address(f"tcp:[fe80::1%{zone}]:5037", {})
    assert address.host == f"fe80::1%{zone}"
    assert set(looked_up) == (set() if zone.isdigit() else {zone})


def test_resolve_ipv6() -> None:
    assert resolve_server_address("tcp:[::1]:5037", {}) == ServerAddress("::1", 5037)


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1:5037",
        "tcp:127.0.0.1",
        "tcp::5037",
        "tcp:127.0.0.1:",
        "tcp:127.0.0.1:0",
        "tcp:127.0.0.1:65536",
        "tcp:127.0.0.1:abc",
        "tcp:127.0.0.1:-1",
        "tcp:::1:5037",
        "tcp:[::1]5037",
        "tcp:[::1:5037",
        "tcp:[]:5037",
        "tcp:fe80::1%eth0:5037",
        "unix:/tmp/x",
    ],
)
def test_resolve_rejects_invalid_argument(value: str) -> None:
    with pytest.raises(ValueError, match="adb server argument"):
        resolve_server_address(value, {})


def test_resolve_invalid_values_name_their_source() -> None:
    with pytest.raises(ValueError, match="ADB_SERVER_SOCKET"):
        resolve_server_address(None, {"ADB_SERVER_SOCKET": "bad"})
    with pytest.raises(ValueError, match="ANDROID_ADB_SERVER_PORT"):
        resolve_server_address(None, {"ANDROID_ADB_SERVER_PORT": "99999"})


# --- find_adb ---


ADB_NAME = "adb.exe" if sys.platform == "win32" else "adb"


def make_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_adb_none_found(tmp_path: Path) -> None:
    assert find_adb(None, {"PATH": str(tmp_path)}) is None
    assert find_adb(None, {}) is None


def test_find_adb_argument_wins(tmp_path: Path) -> None:
    arg = make_file(tmp_path / "arg" / "adb")
    other = make_file(tmp_path / "env" / "adb")
    env = {"UIPROBE_ADB": str(other), "PATH": str(other.parent)}
    assert find_adb(arg, env) == arg
    assert find_adb(str(arg), env) == arg


def test_find_adb_environment_before_path(tmp_path: Path) -> None:
    configured = make_file(tmp_path / "env" / ADB_NAME)
    on_path = make_file(tmp_path / "bin" / ADB_NAME)
    env = {"UIPROBE_ADB": str(configured), "PATH": str(on_path.parent)}
    assert find_adb(None, env) == configured
    assert find_adb(None, {"PATH": str(on_path.parent)}) == on_path


def test_find_adb_sdk_directories(tmp_path: Path) -> None:
    name = "adb.exe" if sys.platform == "win32" else "adb"
    home = make_file(tmp_path / "home" / "platform-tools" / name)
    root = make_file(tmp_path / "root" / "platform-tools" / name)
    both = {
        "ANDROID_HOME": str(tmp_path / "home"),
        "ANDROID_SDK_ROOT": str(tmp_path / "root"),
    }
    assert find_adb(None, both) == home
    assert find_adb(None, {"ANDROID_SDK_ROOT": str(tmp_path / "root")}) == root
    missing = {
        "ANDROID_HOME": str(tmp_path / "nowhere"),
        "ANDROID_SDK_ROOT": str(tmp_path / "root"),
    }
    assert find_adb(None, missing) == root


def test_find_adb_windows_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    exe = make_file(tmp_path / "platform-tools" / "adb.exe")
    assert find_adb(None, {"ANDROID_HOME": str(tmp_path)}) == exe


def test_find_adb_invalid_explicit_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="adb argument"):
        find_adb(tmp_path / "missing", {})
    with pytest.raises(ValueError, match="adb argument"):
        find_adb(tmp_path, {})
    with pytest.raises(ValueError, match="UIPROBE_ADB"):
        find_adb(None, {"UIPROBE_ADB": str(tmp_path / "missing")})


def test_find_adb_empty_environment_values_count_as_unset() -> None:
    assert find_adb(None, {"UIPROBE_ADB": "", "ANDROID_HOME": ""}) is None


def test_find_adb_empty_argument_is_an_error_not_a_search() -> None:
    with pytest.raises(ValueError, match="adb argument"):
        find_adb("", {})


def test_find_adb_reads_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = make_file(tmp_path / "adb")
    monkeypatch.setenv("UIPROBE_ADB", str(exe))
    assert find_adb() == exe


def test_find_adb_path_wins_over_the_sdk_directories(tmp_path: Path) -> None:
    on_path = make_file(tmp_path / "bin" / ADB_NAME)
    make_file(tmp_path / "sdk" / "platform-tools" / ADB_NAME)
    env = {"PATH": str(on_path.parent), "ANDROID_HOME": str(tmp_path / "sdk")}
    assert find_adb(None, env) == on_path


def test_find_adb_searches_every_path_entry_in_order(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    second = make_file(tmp_path / "b" / ADB_NAME)
    make_file(tmp_path / "c" / ADB_NAME)
    path = os.pathsep.join(str(tmp_path / d) for d in ("empty", "b", "c"))
    assert find_adb(None, {"PATH": path}) == second


def test_find_adb_ignores_a_directory_or_non_executable_named_adb(
    tmp_path: Path,
) -> None:
    (tmp_path / "dir" / "adb").mkdir(parents=True)
    plain = tmp_path / "plain" / "adb"
    plain.parent.mkdir()
    plain.write_text("")
    plain.chmod(0o644)
    env = {"PATH": os.pathsep.join([str(tmp_path / "dir"), str(plain.parent)])}
    assert find_adb(None, env) is None


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_find_adb_never_uses_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    # On Windows `shutil.which` looks in the current directory first, so a
    # downloaded folder holding adb.exe could be run by `adb start-server`.
    monkeypatch.setattr(sys, "platform", platform)
    name = "adb.exe" if platform == "win32" else "adb"
    make_file(tmp_path / name)
    monkeypatch.chdir(tmp_path)
    for path in ("", ".", str(Path()), os.pathsep + "bin", "relative/dir"):
        assert find_adb(None, {"PATH": path}) is None


def test_find_adb_skips_relative_entries_but_finds_later_absolute_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_file(tmp_path / "here" / ADB_NAME)
    real = make_file(tmp_path / "real" / ADB_NAME)
    monkeypatch.chdir(tmp_path)
    path = os.pathsep.join(["here", str(real.parent)])
    assert find_adb(None, {"PATH": path}) == real


def test_find_adb_windows_name_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    make_file(tmp_path / "adb.bat")
    exe = make_file(tmp_path / "adb.exe")
    assert find_adb(None, {"PATH": str(tmp_path)}) == exe


@pytest.mark.skipif(sys.platform == "win32", reason="uses shell scripts")
def test_find_adb_returns_the_file_that_was_checked_not_a_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative name would be searched on PATH when run, and the file that
    # was validated in the current directory would be ignored.
    here = make_file(tmp_path / "work" / "adb")
    monkeypatch.chdir(here.parent)
    for found in (find_adb("adb", {}), find_adb(None, {"UIPROBE_ADB": "adb"})):
        assert found == here
        assert found is not None
        assert found.is_absolute()


def test_find_adb_sdk_result_is_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "adb.exe" if sys.platform == "win32" else "adb"
    make_file(tmp_path / "sdk" / "platform-tools" / name)
    monkeypatch.chdir(tmp_path)
    found = find_adb(None, {"ANDROID_HOME": "sdk"})
    assert found == tmp_path / "sdk" / "platform-tools" / name


def test_argument_type_errors_come_before_any_connection(
    connections: list[tuple[object, ...]],
) -> None:
    with pytest.raises(TypeError):
        resolve_server_address(5037)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        find_adb(5037)  # ty: ignore[invalid-argument-type]
    assert connections == []


@pytest.mark.parametrize("value", [5037, b"adb", ["adb"]])
def test_find_adb_rejects_a_value_that_is_not_a_path(value: object) -> None:
    with pytest.raises(TypeError):
        find_adb(value, {})  # ty: ignore[invalid-argument-type]


# --- parse_devices_l ---


def test_parse_devices_l_full_line() -> None:
    text = (
        "emulator-5554          device product:sdk model:Pixel_6 device:emu64 "
        "transport_id:1\n"
        "0123456789ABCDEF       device usb:1-1 product:p model:M device:d "
        "transport_id:12\n"
    )
    first, second = parse_devices_l(text)
    assert first == DeviceInfo("emulator-5554", "device", "Pixel_6", "sdk", "emu64", 1)
    assert second.transport_id == 12
    assert second.model == "M"


def test_parse_devices_l_states_with_spaces() -> None:
    text = (
        "abc no permissions (user in plugdev group; are your udev rules wrong?); "
        "see [http://developer.android.com/tools/device.html] usb:1-2 transport_id:5\n"
        "def unauthorized usb:1-3 transport_id:6\n"
        "ghi offline\n"
    )
    first, second, third = parse_devices_l(text)
    assert first.state.startswith("no permissions (user in plugdev group;")
    assert first.state.endswith("device.html]")
    assert first.transport_id == 5
    assert second.state == "unauthorized"
    assert third == DeviceInfo("ghi", "offline")


def test_parse_devices_l_skips_bad_lines_and_unknown_keys() -> None:
    text = "\nlonely\nxyz device model:M color:red transport_id:x\nproduct:a model:b\n"
    assert parse_devices_l(text) == [DeviceInfo("xyz", "device", model="M")]
    assert parse_devices_l("") == []


def test_parse_devices_l_ignores_a_huge_transport_id() -> None:
    # `int()` refuses more than 4300 digits; a bad field must not fail the list.
    text = "abc device model:M transport_id:" + "1" * 5000 + "\n"
    assert parse_devices_l(text) == [DeviceInfo("abc", "device", model="M")]


def test_parse_devices_l_accepts_nine_digit_transport_ids_only() -> None:
    (ok,) = parse_devices_l("a device transport_id:123456789\n")
    (long,) = parse_devices_l("a device transport_id:1234567890\n")
    assert (ok.transport_id, long.transport_id) == (123456789, None)


def test_parse_devices_l_an_unknown_key_before_product_does_not_join_the_state() -> (
    None
):
    text = "abc device future_key:1 product:p model:M device:d transport_id:3\n"
    assert parse_devices_l(text) == [DeviceInfo("abc", "device", "M", "p", "d", 3)]


def test_parse_devices_l_keeps_the_whole_multi_word_state() -> None:
    state = (
        "no permissions (missing udev rules? user is in the plugdev group); "
        "see [http://developer.android.com/tools/device.html]"
    )
    (listed,) = parse_devices_l(f"abc {state} usb:1-2 unknown_x:y transport_id:5\n")
    assert listed.state == state
    assert listed.transport_id == 5


def test_parse_devices_l_accepts_the_no_serial_placeholder() -> None:
    text = (
        "(no serial number)     no permissions (user is in the plugdev group); "
        "see [http://developer.android.com/tools/device.html] usb:1-1 "
        "transport_id:2\n"
        "(no serial number)     offline\n"
    )
    first, second = parse_devices_l(text)
    assert first.serial == "(no serial number)"
    assert first.state.startswith("no permissions (")
    assert first.transport_id == 2
    assert second == DeviceInfo("(no serial number)", "offline")


def test_parse_devices_l_no_serial_placeholder_needs_a_state() -> None:
    assert parse_devices_l("(no serial number)\n") == []


# --- quote_args ---


def test_quote_args() -> None:
    assert quote_args("echo", "a b", "c'd") == "echo 'a b' 'c'\"'\"'d'"
    assert quote_args() == ""


# --- protocol against the fake server ---


def test_server_version(fake_adb: FakeAdbServer) -> None:
    assert client_for(fake_adb).server_version() == 41
    fake_adb.version = 0xABCD
    assert client_for(fake_adb).server_version() == 0xABCD
    assert fake_adb.requests == ["host:version"] * 2


def test_devices(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("emu-1", model="Pixel", transport_id=2)
    fake_adb.add_device("usb-1", "unauthorized")
    assert client_for(fake_adb).devices() == [
        DeviceInfo("emu-1", "device", model="Pixel", transport_id=2),
        DeviceInfo("usb-1", "unauthorized"),
    ]


def test_shell_and_exec_use_one_connection_per_request(
    fake_adb: FakeAdbServer, connections: list[tuple[object, ...]]
) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "echo hi", b"hi\n")
    fake_adb.on_exec("d1", "cat f", b"\x00\xff\n")
    client = client_for(fake_adb)
    assert client.shell("d1", "echo hi") == b"hi\n"
    assert client.exec_out("d1", "cat f") == b"\x00\xff\n"
    assert fake_adb.requests == [
        "host:transport:d1",
        "shell:echo hi",
        "host:transport:d1",
        "exec:cat f",
    ]
    assert len(connections) == 2
    assert {c[0] for c in connections} == {"127.0.0.1"}


def test_non_ascii_command_length_is_in_bytes(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "echo 日本語", "日本語\n".encode())
    assert client_for(fake_adb).shell("d1", "echo 日本語") == "日本語\n".encode()


@pytest.mark.parametrize(
    ("state", "error"),
    [("offline", DeviceOfflineError), ("unauthorized", DeviceUnauthorizedError)],
)
def test_transport_states_map_to_errors(
    fake_adb: FakeAdbServer, state: str, error: type[Exception]
) -> None:
    fake_adb.add_device("d1", state)
    with pytest.raises(error):
        client_for(fake_adb).shell("d1", "id")


def test_transport_not_found(fake_adb: FakeAdbServer) -> None:
    with pytest.raises(DeviceNotFoundError) as info:
        client_for(fake_adb).shell("ghost", "id")
    assert info.value.details["serial"] == "ghost"


def test_transport_other_failure_is_adb_error(fake_adb: FakeAdbServer) -> None:
    fake_adb.fail_request("host:transport:d1", "protocol fault")
    with pytest.raises(AdbError) as info:
        client_for(fake_adb).shell("d1", "id")
    assert type(info.value) is AdbError
    assert info.value.details == {
        "request": "host:transport:d1",
        "message": "protocol fault",
    }


def test_fail_on_host_request_and_service(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.fail_request("host:version", "no")
    fake_adb.on_shell("d1", "bad", Fail("closed"))
    client = client_for(fake_adb)
    with pytest.raises(AdbError) as info:
        client.server_version()
    assert info.value.details == {"request": "host:version", "message": "no"}
    with pytest.raises(AdbError) as info:
        client.shell("d1", "bad")
    assert info.value.details == {"request": "shell:bad", "message": "closed"}


@pytest.mark.parametrize(
    "raw",
    [
        b"OKA",
        b"OKAY",
        b"OKAY00",
        b"OKAY0004",
        b"OKAY0004ab",
        b"OKAYzzzz",
        b"OKAY+004abcd",
        b"OKAY0x04abcd",
        b"WHAT0000",
        b"FAIL",
        b"FAIL00",
        b"FAILxxxx",
        b"",
        b"\xff\xfe\x00\x01garbage",
    ],
)
def test_broken_responses_raise_adb_error(fake_adb: FakeAdbServer, raw: bytes) -> None:
    fake_adb.send_raw("host:devices-l", raw)
    with pytest.raises(AdbError):
        client_for(fake_adb).devices()


def test_invalid_version_payload(fake_adb: FakeAdbServer) -> None:
    fake_adb.send_raw("host:version", b"OKAY0004zzzz")
    with pytest.raises(AdbError, match="version"):
        client_for(fake_adb).server_version()


def test_broken_response_in_the_middle_of_a_transport(
    fake_adb: FakeAdbServer,
) -> None:
    fake_adb.add_device("d1")
    fake_adb.send_raw("host:transport:d1", b"OK")
    with pytest.raises(AdbError):
        client_for(fake_adb).shell("d1", "id")


def test_unknown_service_status(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.send_raw("shell:id", b"NOPE")
    with pytest.raises(AdbError, match="unknown status"):
        client_for(fake_adb).shell("d1", "id")


def test_read_timeout_after_connecting(fake_adb: FakeAdbServer) -> None:
    fake_adb.stall("host:version")
    client = AdbClient(
        ServerAddress("127.0.0.1", fake_adb.port), timeout=0.2, start_server=False
    )
    with pytest.raises(AdbError, match="timed out"):
        client.server_version()


def test_output_size_limit(
    fake_adb: FakeAdbServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_exec("d1", "cat big", b"x" * 1000)
    fake_adb.on_exec("d1", "cat ok", b"x" * 100)
    monkeypatch.setattr(adb, "MAX_OUTPUT_BYTES", 500)
    client = client_for(fake_adb)
    assert client.exec_out("d1", "cat ok") == b"x" * 100
    with pytest.raises(AdbError, match="larger than"):
        client.exec_out("d1", "cat big")


def test_request_too_long() -> None:
    with FakeAdbServer() as server:
        server.add_device("d1")
        client = client_for(server)
        with pytest.raises(ValueError, match="too long"):
            client.shell("d1", "x" * 70000)


def test_sockets_are_closed(
    fake_adb: FakeAdbServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_adb.add_device("d1")
    created: list[socket.socket] = []
    real = adb._new_socket

    def spy(family: socket.AddressFamily) -> socket.socket:
        sock = real(family)
        created.append(sock)
        return sock

    monkeypatch.setattr(adb, "_new_socket", spy)
    client = client_for(fake_adb)
    client.server_version()
    client.devices()
    client.shell("d1", "id")
    fake_adb.fail_request("host:version", "x")
    with pytest.raises(AdbError):
        client.server_version()
    assert len(created) == 4
    assert all(sock.fileno() == -1 for sock in created)


# --- framing, short reads, timeouts ---


def test_requests_are_framed_with_four_hex_digits(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    client = client_for(fake_adb)
    client.server_version()
    client.shell("d1", "id")
    assert fake_adb.wire == [
        b"000chost:version",
        b"0011host:transport:d1",
        b"0008shell:id",
    ]


def test_replies_arriving_one_byte_at_a_time(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1", model="M")
    fake_adb.on_shell("d1", "id", b"uid=0(root)\n")
    fake_adb.chunk_size = 1
    client = client_for(fake_adb)
    assert client.server_version() == 41
    assert [d.serial for d in client.devices()] == ["d1"]
    assert client.shell("d1", "id") == b"uid=0(root)\n"


def test_output_arriving_in_pieces_with_pauses(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_exec("d1", "cat x", Trickle(b"0123456789", chunk_size=3, delay=0.02))
    assert client_for(fake_adb).exec_out("d1", "cat x") == b"0123456789"


def test_stall_in_the_middle_of_the_output_is_an_adb_error(
    fake_adb: FakeAdbServer,
) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "slow", Stall(prefix=b"partial"))
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=0.2)
    with pytest.raises(AdbError, match="timed out reading output") as info:
        client.shell("d1", "slow")
    assert info.value.details == {
        "reason": "timeout",
        "request": "shell:slow",
        "stage": "output",
        "timeout_seconds": 0.2,
    }


def test_a_slow_command_is_a_silence_after_okay(fake_adb: FakeAdbServer) -> None:
    # Real adb answers OKAY at once; a slow command only delays its output.
    fake_adb.add_device("d1")

    slow = Trickle(b"done", chunk_size=4, lead=0.5)

    fake_adb.on_shell("d1", "slow", slow)
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=0.2)
    with pytest.raises(AdbError, match="timed out reading output"):
        client.shell("d1", "slow")


def test_per_call_timeout_outlasts_the_client_timeout(
    fake_adb: FakeAdbServer,
) -> None:
    fake_adb.add_device("d1")

    slow = Trickle(b"done", chunk_size=4, lead=0.5)

    fake_adb.on_shell("d1", "slow", slow)
    fake_adb.on_exec("d1", "slow", slow)
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=0.2)
    assert client.shell("d1", "slow", timeout=5) == b"done"
    assert client.exec_out("d1", "slow", timeout=5) == b"done"


def test_per_call_timeout_can_be_shorter_and_does_not_stick(
    fake_adb: FakeAdbServer,
) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "slow", Stall())
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=5)
    started = time.monotonic()
    with pytest.raises(AdbError, match="timed out reading output"):
        client.shell("d1", "slow", timeout=0.2)
    assert time.monotonic() - started < 3
    fake_adb.on_shell("d1", "fast", b"ok")
    assert client.shell("d1", "fast") == b"ok"


def test_handshake_timeout_is_marked_as_a_timeout(fake_adb: FakeAdbServer) -> None:
    fake_adb.stall("host:version")
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=0.2)
    with pytest.raises(AdbError) as info:
        client.server_version()
    # No "stage": the timeout hit the handshake, not a service's output.
    assert info.value.details == {
        "reason": "timeout",
        "request": "host:version",
        "timeout_seconds": 0.2,
    }


def test_output_timeout_reports_the_timeout_in_effect(
    fake_adb: FakeAdbServer,
) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "slow", Stall())
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=5)
    started = time.perf_counter()
    with pytest.raises(AdbError) as info:
        client.shell("d1", "slow", timeout=0.3)
    assert info.value.details["stage"] == "output"
    assert info.value.details["timeout_seconds"] == 0.3
    assert time.perf_counter() - started < 5


def test_a_stalled_shell_okay_is_a_handshake_timeout(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.stall("shell:slow")
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=0.2)
    with pytest.raises(AdbError) as info:
        client.shell("d1", "slow", timeout=5)
    assert info.value.details["reason"] == "timeout"
    assert info.value.details["request"] == "shell:slow"
    assert info.value.details["timeout_seconds"] == 0.2
    assert "stage" not in info.value.details


class _CountingSocket:
    """Wraps a socket and counts the `recv` calls that returned data."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self.data_reads = 0

    def recv(self, size: int) -> bytes:
        data = self._sock.recv(size)
        self.data_reads += bool(data)
        return data

    def __enter__(self) -> _CountingSocket:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._sock.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._sock, name)


def test_a_response_arriving_in_separate_segments_is_reassembled(
    fake_adb: FakeAdbServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_adb.add_device("d1", model="Pixel_8", product="sdk", device="emu")
    fake_adb.chunk_size = 4
    fake_adb.chunk_delay = 0.01
    client = client_for(fake_adb)
    wrappers: list[_CountingSocket] = []
    real_open = client._open

    def counting_open() -> socket.socket:
        wrappers.append(_CountingSocket(real_open()))
        return cast("socket.socket", wrappers[-1])

    monkeypatch.setattr(client, "_open", counting_open)
    (listed,) = client.devices()
    assert listed == DeviceInfo("d1", "device", "Pixel_8", "sdk", "emu")
    # About 80 bytes in 4-byte writes, 10 ms apart, with TCP_NODELAY on the
    # server: they cannot all have been read by a handful of recv calls.
    assert wrappers[0].data_reads >= 8


class _ChunkedSocket:
    """A socket stand-in whose `recv` returns at most `size` bytes per call."""

    def __init__(self, data: bytes, size: int) -> None:
        self._data = data
        self._size = size
        self.reads = 0

    def settimeout(self, value: float | None) -> None:
        pass

    def gettimeout(self) -> float | None:
        return None

    def recv(self, limit: int) -> bytes:
        self.reads += 1
        chunk = self._data[: min(limit, self._size)]
        self._data = self._data[len(chunk) :]
        return chunk


def test_length_prefixed_and_eof_reads_join_many_recv_calls() -> None:
    fake = _ChunkedSocket(b"000bhello world", 2)
    sock = cast("socket.socket", fake)
    assert adb._read_length_prefixed(sock, "host:x") == b"hello world"
    assert fake.reads >= 8
    tail = _ChunkedSocket(b"0123456789", 3)
    assert adb._read_to_eof(cast("socket.socket", tail), "shell:x") == b"0123456789"
    assert tail.reads == 5


class _BrokenSocket:
    """A socket stand-in whose calls fail with `error`."""

    def __init__(self, error: OSError) -> None:
        self._error = error

    def sendall(self, _data: bytes) -> None:
        raise self._error

    def settimeout(self, value: float | None) -> None:
        pass

    def gettimeout(self) -> float | None:
        return None

    def recv(self, _size: int) -> bytes:
        raise self._error


def broken(error: OSError) -> socket.socket:
    return cast("socket.socket", _BrokenSocket(error))


def test_send_failure_is_an_adb_error() -> None:
    with pytest.raises(AdbError, match="sending to the adb server failed") as info:
        adb._send_request(broken(BrokenPipeError("pipe")), "host:version")
    assert info.value.details == {"request": "host:version"}


def test_receive_failure_in_a_handshake_is_an_adb_error() -> None:
    with pytest.raises(AdbError, match="connection to the adb server failed"):
        adb._read_exact(broken(ConnectionResetError("reset")), 4)


def test_receive_failure_while_reading_output_is_an_adb_error() -> None:
    with pytest.raises(AdbError, match="connection to the adb server failed") as info:
        adb._read_to_eof(broken(ConnectionResetError("reset")), "shell:id")
    assert info.value.details == {"request": "shell:id"}


def test_connection_reset_by_the_server_is_an_adb_error(
    fake_adb: FakeAdbServer,
) -> None:
    fake_adb.add_device("d1")
    fake_adb.reset("shell:id")
    with pytest.raises(AdbError):
        client_for(fake_adb).shell("d1", "id")


# --- unavailable server and start-server ---


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb, "START_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(adb, "START_RETRY_INTERVAL", 0.02)


def test_unavailable_without_adb_path() -> None:
    address = ServerAddress("127.0.0.1", unused_port(), is_default=True)
    with pytest.raises(AdbServerUnavailableError) as info:
        AdbClient(address).server_version()
    assert info.value.details["address"] == str(address)
    assert info.value.details["start_attempted"] is False
    assert "reason" in info.value.details


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
@pytest.mark.parametrize(
    ("is_default", "start_server", "use_path"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_start_server_conditions_not_met(
    tmp_path: Path, *, is_default: bool, start_server: bool, use_path: bool
) -> None:
    script, log = recording_adb(tmp_path)
    address = ServerAddress("127.0.0.1", unused_port(), is_default=is_default)
    client = AdbClient(
        address, adb_path=script if use_path else None, start_server=start_server
    )
    with pytest.raises(AdbServerUnavailableError) as info:
        client.devices()
    assert info.value.details["start_attempted"] is False
    assert not log.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_start_server_runs_once_per_client(tmp_path: Path) -> None:
    script, log = recording_adb(tmp_path)
    address = ServerAddress("127.0.0.1", unused_port(), is_default=True)
    client = AdbClient(address, adb_path=script)
    for _ in range(2):
        with pytest.raises(AdbServerUnavailableError) as info:
            client.devices()
        assert info.value.details["start_attempted"] is True
    assert log.read_text().splitlines() == ["start-server"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_start_server_failing_exit_status(tmp_path: Path) -> None:
    script, log = recording_adb(tmp_path, exit_code=1)
    address = ServerAddress("127.0.0.1", unused_port(), is_default=True)
    with pytest.raises(AdbServerUnavailableError) as info:
        AdbClient(address, adb_path=script).devices()
    assert "status 1" in str(info.value.details["reason"])
    assert log.exists()


def test_start_server_cannot_run(tmp_path: Path) -> None:
    address = ServerAddress("127.0.0.1", unused_port(), is_default=True)
    client = AdbClient(address, adb_path=tmp_path / "missing-adb")
    with pytest.raises(AdbServerUnavailableError) as info:
        client.devices()
    assert info.value.details["start_attempted"] is True
    assert "could not run" in str(info.value.details["reason"])


def test_start_server_then_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port = unused_port()
    server = FakeAdbServer(port=port)
    calls: list[list[str]] = []

    def fake_run(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == 30
        assert "shell" not in kwargs
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["ADB_MDNS"] == "0"
        assert env["PATH"] == os.environ["PATH"]
        assert not set(env) & {
            "ADB_SERVER_SOCKET",
            "ANDROID_ADB_SERVER_ADDRESS",
            "ANDROID_ADB_SERVER_PORT",
        }
        server.start()
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    adb_path = tmp_path / "adb"
    client = AdbClient(
        ServerAddress("127.0.0.1", port, is_default=True), adb_path=adb_path
    )
    try:
        assert client.server_version() == 41
        assert client.server_version() == 41
    finally:
        server.stop()
    assert calls == [[str(adb_path), "start-server"]]


def test_retry_keeps_trying_until_the_server_listens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    connections: list[tuple[object, ...]],
) -> None:
    monkeypatch.setattr(adb, "START_RETRY_SECONDS", 3.0)
    port = unused_port()
    server = FakeAdbServer(port=port)
    timer = threading.Timer(0.25, server.start)

    def fake_run(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        timer.start()  # the server comes up a little after start-server returns
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    client = AdbClient(
        ServerAddress("127.0.0.1", port, is_default=True), adb_path=tmp_path / "adb"
    )
    try:
        assert client.server_version() == 41
    finally:
        timer.join()
        server.stop()
    attempts = [c for c in connections if c[1] == port]
    assert len(attempts) >= 3


def test_retry_gives_up_after_the_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(
        args: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    address = ServerAddress("127.0.0.1", unused_port(), is_default=True)
    with pytest.raises(AdbServerUnavailableError) as info:
        AdbClient(address, adb_path=tmp_path / "adb").devices()
    assert "did not come up" in str(info.value.details["reason"])
    assert info.value.details["start_attempted"] is True


@pytest.mark.parametrize(
    ("address_is_default", "start_server", "adb_path", "skipped", "hint"),
    [
        (True, False, "adb", "disabled", "disabled"),
        (False, True, "adb", "address_not_default", "address you chose"),
        (
            True,
            True,
            None,
            "adb_not_found",
            "install platform-tools or set UIPROBE_ADB",
        ),
    ],
)
def test_unavailable_says_why_the_server_was_not_started(
    tmp_path: Path,
    *,
    address_is_default: bool,
    start_server: bool,
    adb_path: str | None,
    skipped: str,
    hint: str,
) -> None:
    address = ServerAddress("127.0.0.1", unused_port(), is_default=address_is_default)
    client = AdbClient(
        address,
        adb_path=None if adb_path is None else tmp_path / adb_path,
        start_server=start_server,
    )
    with pytest.raises(AdbServerUnavailableError) as info:
        client.devices()
    assert info.value.details["start_skipped"] == skipped
    assert hint in info.value.message
    assert str(address) in info.value.message


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_unavailable_after_a_failed_start_has_no_skip_reason(tmp_path: Path) -> None:
    script, _log = recording_adb(tmp_path, exit_code=1)
    address = ServerAddress("127.0.0.1", unused_port(), is_default=True)
    client = AdbClient(address, adb_path=script)
    for _ in range(2):  # the second call finds the one attempt already used
        with pytest.raises(AdbServerUnavailableError) as info:
            client.devices()
        assert "start_skipped" not in info.value.details
        assert info.value.message == f"adb server is unavailable at {address}"


def test_connects_only_to_the_configured_address(
    fake_adb: FakeAdbServer, connections: list[tuple[object, ...]]
) -> None:
    client_for(fake_adb).server_version()
    assert [c[:2] for c in connections] == [("127.0.0.1", fake_adb.port)]


# --- waiting in slices: Ctrl+C within about a second, same timeout semantics ---


@pytest.fixture
def python_sigint():
    """Make sure Python handles SIGINT, which interrupt_main needs."""
    old = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old)


class _Interrupter:
    """Interrupt the main thread once `entered` is set (Ctrl+C on Windows).

    interrupt_main sets a flag the main thread only sees when its blocking
    call returns, which is how Ctrl+C behaves on Windows. The stubbed wait sets
    `entered` when it begins, so the interrupt cannot land anywhere else
    however the threads are scheduled. `watchdog` only bounds how long the
    helper thread lives if the wait is never entered.
    """

    def __init__(self, entered: threading.Event, watchdog: float = 30.0) -> None:
        self.entered = entered
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(watchdog,), daemon=True)
        self._thread.start()

    def _run(self, watchdog: float) -> None:
        if self.entered.wait(watchdog) and not self._cancelled.is_set():
            _thread.interrupt_main()

    def cancel(self) -> None:
        self._cancelled.set()
        self.entered.set()  # releases the helper thread without interrupting
        self._thread.join(5)


def _recv_entered_when(monkeypatch, entered: threading.Event, size: int | None):
    """Set `entered` when `adb._recv` starts a read (of `size`, if given)."""
    real = adb._recv

    def wrapper(sock: socket.socket, n: int) -> bytes:
        if size is None or n == size:
            entered.set()
        return real(sock, n)

    monkeypatch.setattr(adb, "_recv", wrapper)


@pytest.mark.usefixtures("python_sigint")
def test_interrupt_arrives_while_a_read_is_stalled(
    fake_adb: FakeAdbServer, monkeypatch
) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "slow", Stall())
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=60)
    entered = threading.Event()
    _recv_entered_when(monkeypatch, entered, 65536)  # the output read
    interrupter = _Interrupter(entered)
    started = time.perf_counter()
    try:
        with pytest.raises(KeyboardInterrupt):
            client.shell("d1", "slow", timeout=60)
    finally:
        interrupter.cancel()
    assert entered.is_set()
    assert time.perf_counter() - started < 5


@pytest.mark.usefixtures("python_sigint")
def test_interrupt_arrives_while_the_handshake_is_stalled(
    fake_adb: FakeAdbServer, monkeypatch
) -> None:
    fake_adb.stall("host:version")
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=60)
    entered = threading.Event()
    _recv_entered_when(monkeypatch, entered, None)  # the first read stalls
    interrupter = _Interrupter(entered)
    started = time.perf_counter()
    try:
        with pytest.raises(KeyboardInterrupt):
            client.server_version()
    finally:
        interrupter.cancel()
    assert entered.is_set()
    assert time.perf_counter() - started < 5


def test_a_timeout_shorter_than_a_slice_is_honoured(fake_adb: FakeAdbServer) -> None:
    fake_adb.add_device("d1")
    fake_adb.on_shell("d1", "slow", Stall())
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=5)
    started = time.perf_counter()
    with pytest.raises(AdbError) as info:
        client.shell("d1", "slow", timeout=0.05)
    assert info.value.details["timeout_seconds"] == 0.05
    assert time.perf_counter() - started < adb.WAIT_SLICE * 4 + 2


def test_data_trickling_within_the_timeout_does_not_time_out(
    fake_adb: FakeAdbServer,
) -> None:
    # 12 pieces 0.1 s apart take longer than the 0.4 s timeout in total, but
    # the silence between pieces never reaches it.
    fake_adb.add_device("d1")
    data = b"0123456789ab"
    fake_adb.on_exec("d1", "cat x", Trickle(data, chunk_size=1, delay=0.1))
    client = AdbClient(ServerAddress("127.0.0.1", fake_adb.port), timeout=0.4)
    assert client.exec_out("d1", "cat x") == data


class _SlicingSocket:
    """A socket stub whose recv always runs into the timeout it was given."""

    def __init__(self, timeout: float) -> None:
        self.timeout: float | None = timeout
        self.set_to: list[float | None] = []

    def settimeout(self, value: float | None) -> None:
        self.timeout = value
        self.set_to.append(value)

    def gettimeout(self) -> float | None:
        return self.timeout

    def recv(self, _size: int) -> bytes:
        msg = "timed out"
        raise TimeoutError(msg)


def _recv_until_timeout(timeout: float) -> _SlicingSocket:
    sock = _SlicingSocket(timeout)
    with pytest.raises(TimeoutError):
        adb._recv(cast("socket.socket", sock), 1)
    return sock


def test_a_timeout_shorter_than_a_slice_is_never_waited_past() -> None:
    sock = _recv_until_timeout(0.05)
    *slices, restored = sock.set_to
    assert slices
    assert all(s is not None and 0 < s <= 0.05 for s in slices)
    assert restored == 0.05
    assert sock.gettimeout() == 0.05


def test_a_longer_timeout_is_waited_in_slices() -> None:
    sock = _recv_until_timeout(1.0)
    *slices, restored = sock.set_to
    assert len(slices) >= 2
    assert all(s is not None and 0 < s <= adb.WAIT_SLICE for s in slices)
    assert restored == 1.0


def test_recv_with_no_timeout_still_waits_in_slices() -> None:
    class _Once(_SlicingSocket):
        calls = 0

        def recv(self, _size: int) -> bytes:
            self.calls += 1
            if self.calls < 3:
                msg = "timed out"
                raise TimeoutError(msg)
            return b"x"

    sock = _Once(0.0)
    sock.timeout = None
    assert adb._recv(cast("socket.socket", sock), 1) == b"x"
    assert sock.set_to[:-1] == [adb.WAIT_SLICE] * 3
    assert sock.gettimeout() is None


def test_a_real_etimedout_is_not_retried() -> None:
    class _Dead(_SlicingSocket):
        def recv(self, _size: int) -> bytes:
            raise TimeoutError(errno.ETIMEDOUT, "connection timed out")

    sock = _Dead(5.0)
    with pytest.raises(TimeoutError) as info:
        adb._recv(cast("socket.socket", sock), 1)
    assert info.value.errno == errno.ETIMEDOUT
    assert sock.gettimeout() == 5.0


class _PendingSocket:
    """A socket whose connection never completes."""

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def connect_ex(self, _address: object) -> int:
        # The code a real non-blocking connect reports while it is pending:
        # WSAEWOULDBLOCK on Windows (errno.EINPROGRESS is WSAEINPROGRESS
        # there, which a real connect never returns), EINPROGRESS elsewhere.
        if sys.platform == "win32":
            return getattr(errno, "WSAEWOULDBLOCK", errno.EWOULDBLOCK)
        return errno.EINPROGRESS

    def fileno(self) -> int:
        return 0


def _pending() -> socket.socket:
    return cast("socket.socket", _PendingSocket())


def test_the_pending_socket_reports_a_code_the_product_treats_as_pending() -> None:
    code = _PendingSocket().connect_ex(("127.0.0.1", 1))
    assert code in adb._CONNECT_PENDING


class _SlowSelector:
    """A selector whose wait blocks like a native call, deaf to interrupt_main."""

    def __init__(self, entered: threading.Event | None = None) -> None:
        self.entered = entered
        self.closed = False
        self.registered: list[tuple[object, int]] = []

    def register(self, fileobj: object, events: int) -> None:
        self.registered.append((fileobj, events))

    def select(self, timeout: float | None = None) -> list:
        if self.entered is not None:
            self.entered.set()
        time.sleep(timeout or 0)
        return []

    def close(self) -> None:
        self.closed = True


def _use_slow_selector(
    monkeypatch, entered: threading.Event | None = None
) -> list[_SlowSelector]:
    made: list[_SlowSelector] = []

    def factory() -> _SlowSelector:
        made.append(_SlowSelector(entered))
        return made[-1]

    monkeypatch.setattr(adb.selectors, "DefaultSelector", factory)
    return made


@pytest.mark.usefixtures("python_sigint")
def test_interrupt_arrives_while_the_connect_is_pending(monkeypatch) -> None:
    entered = threading.Event()
    made = _use_slow_selector(monkeypatch, entered)
    interrupter = _Interrupter(entered)
    started = time.perf_counter()
    try:
        with pytest.raises(KeyboardInterrupt):
            adb._connect_sliced(_pending(), ("127.0.0.1", 1), 60)
    finally:
        interrupter.cancel()
    assert entered.is_set()
    assert time.perf_counter() - started < 5
    assert made[0].closed


def test_a_pending_connect_times_out_after_the_logical_timeout(monkeypatch) -> None:
    made = _use_slow_selector(monkeypatch)
    sock = _PendingSocket()
    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        adb._connect_sliced(cast("socket.socket", sock), ("127.0.0.1", 1), 0.1)
    assert 0.05 <= time.perf_counter() - started < 5
    assert sock.timeout == 0.1
    assert made[0].closed
    assert made[0].registered == [(sock, selectors.EVENT_WRITE)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor numbers")
def test_connect_wait_works_for_a_descriptor_beyond_select_limit() -> None:
    # select.select raises ValueError for a descriptor >= FD_SETSIZE (1024).
    # One descriptor placed above that limit is enough to show the wait does
    # not use it; 1024 real descriptors are not needed.
    # Loaded by name: type checkers on Windows have no stubs for these.
    fcntl: Any = importlib.import_module("fcntl")
    resource: Any = importlib.import_module("resource")

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 2048
    raised = False
    if soft != resource.RLIM_INFINITY and soft < wanted:
        if hard != resource.RLIM_INFINITY and hard < wanted:
            pytest.skip(f"hard descriptor limit {hard} is below {wanted}")
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        raised = True
    left, right = socket.socketpair()
    high = -1
    try:
        try:
            high = fcntl.fcntl(left.fileno(), fcntl.F_DUPFD, 1500)
        except OSError:
            pytest.skip("cannot allocate a descriptor above 1500")

        class _HighSocket(_PendingSocket):
            def fileno(self) -> int:
                return high

            def getsockopt(self, _level: int, _name: int) -> int:
                return 0  # SO_ERROR: connected

        sock = _HighSocket()
        adb._connect_sliced(cast("socket.socket", sock), ("127.0.0.1", 1), 5)
        assert sock.timeout == 5
    finally:
        if high >= 0:
            os.close(high)
        left.close()
        right.close()
        if raised:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_connect_to_a_closed_port_still_fails_as_unavailable() -> None:
    client = AdbClient(
        ServerAddress("127.0.0.1", unused_port()), start_server=False, timeout=2
    )
    with pytest.raises(AdbServerUnavailableError, match="unavailable"):
        client.server_version()


@pytest.mark.skipif(sys.platform != "win32", reason="Winsock error text")
def test_a_refused_connect_has_a_readable_reason_on_windows() -> None:
    # The timeout is long so that a refusal cannot be mistaken for one: a
    # failed connect must be noticed through the exceptional set.
    client = AdbClient(
        ServerAddress("127.0.0.1", unused_port()), start_server=False, timeout=10
    )
    started = time.perf_counter()
    with pytest.raises(AdbServerUnavailableError) as info:
        client.server_version()
    reason = str(info.value.details["reason"])
    assert time.perf_counter() - started < 8
    assert "Unknown error" not in reason
    assert "timed out" not in reason
    assert "10061" in reason or "refused" in reason.lower()
