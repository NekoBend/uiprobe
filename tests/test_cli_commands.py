"""Tests for the individual commands of the command line."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import Fail
from uiprobe import cli

if TYPE_CHECKING:
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


def test_json_output_escapes_terminal_controls_in_every_command(phone, capsys):
    phone.add_device("odd\x9b2J\u202e", model="M")
    code, out, _ = invoke(capsys, "devices", "--json", "--adb-server", phone.address)
    assert code == 0
    assert "\x9b" not in out
    assert "\\u009b" in out
    assert "\\u202e" in out
    assert {d["serial"] for d in json.loads(out)["devices"]} >= {"odd\x9b2J\u202e"}


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
