"""Tests for ``uiprobe crawl --press`` (fake adb server and fake device)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT
from uiprobe import cli
from uiprobe.device import Device
from uiprobe.settings import SETTINGS_FILE, render_default_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / SETTINGS_FILE).write_text(render_default_settings(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def press(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.usefixtures("here")
def test_press_runs_a_short_crawl_on_an_emulator(fake_adb, android, capsys, tmp_path):
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert (code, err) == (0, "")
    lines = text.splitlines()
    assert lines[0] == f"pressing on {SERIAL} (emulator): {SHOP}"
    assert lines[1] == "limits: depth 3, actions 30, minutes 5"
    assert lines[2] == f"run folder: {out}"
    assert lines[3] == "BACK on screens of other apps: no (restart the app)"
    assert lines[4] == "check the window at each tap point: yes"
    assert lines[5].startswith("stopped: ")
    assert any(line.startswith("actions: ") for line in lines)
    assert f"run folder: {out}" in lines[6:]
    assert [i for i in android.inputs if i.kind == "tap"]
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["limits"] == {"depth": 3, "actions": 30, "minutes": 5}
    assert begin["check_tap_window"] is True
    assert begin["back_on_other_screens"] is False
    assert (out / "run.json").exists()


@pytest.mark.usefixtures("here", "android")
def test_press_in_json(fake_adb, capsys, tmp_path):
    out = tmp_path / "run"
    code, text, err = press(
        capsys,
        fake_adb,
        "--press",
        "--out",
        str(out),
        "--actions",
        "4",
        "--back-on-other-screens",
        "--no-tap-window-check",
        "--json",
    )
    assert (code, err) == (0, "")
    result = json.loads(text)
    assert result["schema"] == "uiprobe.crawl_run/1"
    assert result["package"] == SHOP
    assert result["device"] == {"serial": SERIAL, "emulator": True}
    assert result["limits"] == {"depth": 3, "actions": 4, "minutes": 5}
    assert result["back_on_other_screens"] is True
    assert result["check_tap_window"] is False
    assert result["out"] == str(out)
    assert result["stop_reason"] == "max_actions"
    assert result["actions"] == 4
    assert isinstance(result["not_sent"], dict)
    assert result["repin"] is True
    assert result["notes"] == []
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["check_tap_window"] is False
    assert begin["back_on_other_screens"] is True


@pytest.mark.usefixtures("here")
def test_a_real_device_is_refused_without_the_option(
    fake_adb, android, capsys, tmp_path
):
    android.props["ro.kernel.qemu"] = "0"
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert (code, text) == (17, "")
    assert "--allow-real-device" in err
    assert "nothing was pressed" in err
    assert not out.exists()
    assert android.inputs == []


@pytest.mark.usefixtures("here")
def test_a_real_device_runs_with_the_option(fake_adb, android, capsys, tmp_path):
    android.props["ro.kernel.qemu"] = "0"
    out = tmp_path / "run"
    code, text, err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--allow-real-device"
    )
    assert (code, err) == (0, "")
    assert text.splitlines()[0] == f"pressing on {SERIAL} (real device): {SHOP}"
    assert [i for i in android.inputs if i.kind == "tap"]


@pytest.mark.usefixtures("here")
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--out", "x"], "--out is used only with --press"),
        (["--actions", "3"], "--actions is used only with --press"),
        (["--allow-real-device"], "--allow-real-device is used only with --press"),
        (["--no-tap-window-check"], "--no-tap-window-check is used only with --press"),
        (["--press"], "--press needs --out DIR, the new folder to record the run into"),
        (
            ["--press", "--out", "x", "--actions", "0"],
            "--actions must be from 1 to 10000, got 0",
        ),
        (
            ["--press", "--out", "x", "--depth", "51"],
            "--depth must be from 1 to 50, got 51",
        ),
        (
            ["--press", "--out", "x", "--settle", "-1"],
            "--settle must be from 0 to 60 seconds, got -1.0",
        ),
        (
            ["--press", "--out", "x", "--settle", "nan"],
            "--settle must be from 0 to 60 seconds, got nan",
        ),
        (
            ["--press", "--dry-run", "--out", "x"],
            "--press and --dry-run cannot be combined",
        ),
    ],
)
def test_wrong_press_options_are_refused_before_any_contact(
    fake_adb, android, capsys, argv, message
):
    code, text, err = press(capsys, fake_adb, *argv)
    assert (code, text) == (2, "")
    assert err == f"uiprobe: error: {message}\n"
    assert android.inputs == []
    assert not [r for r in fake_adb.requests if r.startswith("shell:")]


@pytest.mark.usefixtures("here")
def test_an_existing_out_folder_is_refused(fake_adb, android, capsys, tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    code, _text, err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert code != 0
    assert "run" in err
    assert android.inputs == []


@pytest.mark.usefixtures("here", "android")
def test_ctrl_c_writes_the_run_and_exits_130(fake_adb, capsys, tmp_path, monkeypatch):
    original = Device.tap
    taps = [0]

    def tap_then_interrupt(self: Device, *args: object, **kwargs: object) -> object:
        result = original(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]
        taps[0] += 1
        if taps[0] == 1:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(Device, "tap", tap_then_interrupt)
    out = tmp_path / "run"
    code, _text, err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert code == 130
    assert err == f"uiprobe: error: interrupted; the run so far is in {out}\n"
    run_json = json.loads((out / "run.json").read_bytes())
    assert run_json["stop_reason"] == "interrupted"


@pytest.mark.usefixtures("here")
def test_an_unreadable_tap_window_explains_the_option(
    fake_adb, android, capsys, tmp_path, monkeypatch
):
    monkeypatch.setattr(Device, "window_at", lambda _self, _point: "line_format")
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert (code, err) == (0, "")
    assert [i for i in android.inputs if i.kind == "tap"] == []
    notes = [line for line in text.splitlines() if line.startswith("note: ")]
    assert any("--no-tap-window-check" in n and "line_format" in n for n in notes), text
    assert "cover_unknown" in text


@pytest.mark.usefixtures("here")
def test_an_unreadable_boot_id_is_explained(fake_adb, android, capsys, tmp_path):
    android.boot_id = None
    out = tmp_path / "run"
    code, text, _err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--actions", "3"
    )
    assert code == 0
    assert any(
        "boot id" in line for line in text.splitlines() if line.startswith("note: ")
    )


@pytest.mark.usefixtures("here", "android")
def test_dumps_that_keep_failing_name_the_screen(
    fake_adb, capsys, tmp_path, monkeypatch
):
    from uiprobe.errors import DumpError

    def failing_dump(_self: Device, **_kwargs: object) -> object:
        raise DumpError("uiautomator dump failed", details={"output": "x"})

    monkeypatch.setattr(Device, "dump", failing_dump)
    out = tmp_path / "run"
    code, text, _err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert code == 0
    assert "stopped: dump_failed_repeatedly" in text
    note = next(
        line for line in text.splitlines() if "uiautomator dump kept failing" in line
    )
    assert f"app in front: {SHOP}" in note
    assert "last screen read: none" in note


@pytest.mark.usefixtures("here")
def test_a_settings_file_with_press_true_alone_does_not_press(
    fake_adb, android, capsys, here
):
    text = render_default_settings().replace("press = false", "press = true")
    (here / SETTINGS_FILE).write_text(text, encoding="utf-8")
    code, out, err = press(capsys, fake_adb)
    assert (code, out) == (2, "")
    assert "only when --press is given" in err
    assert android.inputs == []


@pytest.mark.usefixtures("here")
def test_an_unreadable_emulator_check_is_refused(
    fake_adb, android, capsys, tmp_path, monkeypatch
):
    from uiprobe.errors import AdbError

    def broken(_self: Device) -> bool:
        raise AdbError("getprop failed")

    monkeypatch.setattr(Device, "is_emulator", broken)
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out), "--json")
    assert (code, err) == (17, "")
    error = json.loads(text)["error"]
    assert error["details"]["reason"] == "unreadable"
    assert "--allow-real-device" in error["message"]
    assert not out.exists()
    assert android.inputs == []
    code, text, _err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--allow-real-device"
    )
    assert code == 0
    assert "(emulator check unreadable)" in text.splitlines()[0]
