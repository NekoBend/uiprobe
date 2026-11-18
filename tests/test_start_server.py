"""``--no-start-server`` and the conditions of ``adb start-server``, end to end.

The default server address is replaced by an unused loopback port that keeps
``is_default=True``, so nothing here touches port 5037, and the adb executable
is a script that records its arguments.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from tests.helpers import recording_adb, unused_address, unused_port
from uiprobe import adb, cli
from uiprobe.adb import ServerAddress
from uiprobe.device import Device, device_details, devices
from uiprobe.errors import AdbServerUnavailableError

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the fake adb is a shell script"
)


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adb, "START_RETRY_SECONDS", 0.1)
    monkeypatch.setattr(adb, "START_RETRY_INTERVAL", 0.02)


@pytest.fixture
def recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, ServerAddress]:
    """A recording adb found through UIPROBE_ADB, and a default server that is down."""
    script, log = recording_adb(tmp_path)
    monkeypatch.setenv("UIPROBE_ADB", str(script))
    down = ServerAddress("127.0.0.1", unused_port(), is_default=True)
    monkeypatch.setattr(adb, "DEFAULT_SERVER", down)
    return script, log, down


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = cli.run([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_cli_without_the_flag_starts_the_default_server_once(recorder, capsys):
    _, log, _ = recorder
    code, payload = run_json(capsys, "devices")
    assert code == 3
    assert payload["error"]["details"]["start_attempted"] is True
    assert log.read_text().splitlines() == ["start-server"]
    assert (log.parent / "env.log").read_text().splitlines() == [
        "ADB_MDNS=0",
        "ADDRESS=||",
    ]


def test_cli_no_start_server_never_runs_adb(recorder, capsys, processes):
    _, log, _ = recorder
    code, payload = run_json(capsys, "devices", "--no-start-server")
    assert code == 3
    details = payload["error"]["details"]
    assert details["start_attempted"] is False
    assert details["start_skipped"] == "disabled"
    assert not log.exists()
    assert processes == []


def test_cli_dump_no_start_server_never_runs_adb(recorder, capsys):
    _, log, _ = recorder
    code, payload = run_json(capsys, "dump", "--no-start-server")
    assert code == 3
    assert payload["error"]["details"]["start_attempted"] is False
    assert not log.exists()


def test_cli_no_start_server_before_the_command(recorder, capsys):
    _, log, _ = recorder
    code, _ = run_json(capsys, "--no-start-server", "devices")
    assert code == 3
    assert not log.exists()


def test_library_start_server_false_never_runs_adb(recorder):
    _, log, _ = recorder
    with pytest.raises(AdbServerUnavailableError) as info:
        Device(start_server=False)
    assert info.value.details["start_attempted"] is False
    with pytest.raises(AdbServerUnavailableError):
        devices(start_server=False)
    with pytest.raises(AdbServerUnavailableError):
        device_details(start_server=False)
    assert not log.exists()


def test_library_default_starts_the_server_once_per_call(recorder):
    _, log, _ = recorder
    with pytest.raises(AdbServerUnavailableError) as info:
        Device()
    assert info.value.details["start_attempted"] is True
    assert log.read_text().splitlines() == ["start-server"]


@pytest.mark.parametrize("variable", ["ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_PORT"])
def test_an_address_from_the_environment_never_runs_adb(
    recorder, monkeypatch, capsys, variable
):
    _, log, _ = recorder
    unused = unused_address()
    value = unused if variable == "ADB_SERVER_SOCKET" else unused.rsplit(":", 1)[1]
    monkeypatch.setenv(variable, value)
    code, payload = run_json(capsys, "devices")
    assert code == 3
    assert payload["error"]["details"]["start_skipped"] == "address_not_default"
    assert not log.exists()


def test_an_explicit_address_never_runs_adb(recorder, capsys):
    _, log, _ = recorder
    code, _ = run_json(capsys, "devices", "--adb-server", unused_address())
    assert code == 3
    assert not log.exists()


def test_the_error_tells_how_to_fix_a_missing_adb(monkeypatch, capsys):
    monkeypatch.setattr(
        adb,
        "DEFAULT_SERVER",
        ServerAddress("127.0.0.1", unused_port(), is_default=True),
    )
    code = cli.run(["devices"])
    err = capsys.readouterr().err
    assert code == 3
    assert "adb was not found" in err
    assert "UIPROBE_ADB" in err


def test_a_set_android_adb_server_address_never_runs_adb(
    recorder, monkeypatch, capsys, processes
):
    _, log, _ = recorder
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "192.0.2.1")
    code, payload = run_json(capsys, "devices")
    assert code == 3
    error = payload["error"]
    assert error["details"]["start_skipped"] == "android_adb_server_address_set"
    assert error["details"]["start_attempted"] is False
    assert "ANDROID_ADB_SERVER_ADDRESS is set" in error["message"]
    assert "ADB_SERVER_SOCKET=tcp:IP:PORT" in error["message"]
    assert not log.exists()
    assert processes == []


def test_an_empty_android_adb_server_address_counts_as_unset(
    recorder, monkeypatch, capsys
):
    _, log, _ = recorder
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "")
    code, payload = run_json(capsys, "devices")
    assert code == 3
    details = payload["error"]["details"]
    assert details["start_attempted"] is True
    assert "start_skipped" not in details
    assert log.read_text().splitlines() == ["start-server"]
    assert (log.parent / "env.log").read_text().splitlines() == [
        "ADB_MDNS=0",
        "ADDRESS=||",
    ]


def test_android_adb_server_address_is_not_read_as_an_address(recorder, monkeypatch):
    _, _, down = recorder
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "192.0.2.1:1")
    assert adb.resolve_server_address(None) == down
