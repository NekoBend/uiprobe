"""Tests for ``uiprobe crawl --press`` (fake adb server and fake device)."""

from __future__ import annotations

import io
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
    assert code == 0
    # The plan goes to stderr with --json, so stdout is one JSON document.
    assert err.splitlines()[0] == f"pressing on {SERIAL} (emulator): {SHOP}"
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
            "--settle must be from 0 to 60 seconds, got -1",
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
    assert fake_adb.requests == []


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
    assert f"app in front at the last read: {SHOP}" in note
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
    assert device_writes(fake_adb) == []
    assert not out.exists()
    assert android.inputs == []
    code, text, _err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--allow-real-device"
    )
    assert code == 0
    assert "(emulator check unreadable)" in text.splitlines()[0]


def device_writes(fake_adb: FakeAdbServer) -> list[str]:
    """The shell commands that start, stop or press something."""
    words = ("monkey", "am start", "force-stop", "input ")
    return [
        r
        for r in fake_adb.requests
        if r.startswith(("shell:", "exec:")) and any(w in r for w in words)
    ]


@pytest.mark.usefixtures("here", "android")
@pytest.mark.parametrize("as_json", [False, True])
def test_the_plan_is_out_before_the_app_is_started(
    fake_adb, capsys, tmp_path, monkeypatch, as_json
):
    seen: list[tuple[str, str]] = []
    original = Device.launch

    def launch_after_reading(self: Device, *args: object, **kwargs: object) -> object:
        captured = capsys.readouterr()
        seen.append((captured.out, captured.err))
        return original(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Device, "launch", launch_after_reading)
    out = tmp_path / "run"
    argv = ["--press", "--out", str(out), "--actions", "2"]
    code, _text, _err = press(capsys, fake_adb, *argv, *(["--json"] if as_json else []))
    assert code == 0
    assert seen, "the app was launched"
    plan_out, plan_err = seen[0]
    plan = plan_err if as_json else plan_out
    assert plan.splitlines()[0] == f"pressing on {SERIAL} (emulator): {SHOP}"
    assert "check the window at each tap point: yes" in plan
    if as_json:
        assert plan_out == ""


@pytest.mark.usefixtures("here")
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["--actions", "0", "--actions", "2"],
            "--actions must be from 1 to 10000, got 0",
        ),
        (["--depth", "x", "--depth", "2"], "--depth must be a whole number, got x"),
        (
            ["--minutes", "0", "--minutes", "1"],
            "--minutes must be from 1 to 1440, got 0",
        ),
        (
            ["--settle", "nan", "--settle", "1"],
            "--settle must be from 0 to 60 seconds, got nan",
        ),
    ],
)
def test_a_wrong_value_given_before_a_good_one_is_refused(
    fake_adb, android, capsys, tmp_path, argv, message
):
    code, text, err = press(
        capsys, fake_adb, "--press", "--out", str(tmp_path / "r"), *argv
    )
    assert (code, text) == (2, "")
    assert err == f"uiprobe: error: {message}\n"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.usefixtures("here", "android")
def test_an_existing_file_is_not_named_as_the_run_folder(fake_adb, capsys, tmp_path):
    taken = tmp_path / "notes.txt"
    taken.write_text("mine", encoding="utf-8")
    code, text, _err = press(capsys, fake_adb, "--press", "--out", str(taken), "--json")
    assert code != 0
    error = json.loads(text)["error"]
    assert "out" not in error.get("details", {})
    assert taken.read_text(encoding="utf-8") == "mine"
    assert device_writes(fake_adb) == []


@pytest.mark.usefixtures("here")
def test_refusals_start_and_stop_nothing(fake_adb, android, capsys, tmp_path):
    android.props["ro.kernel.qemu"] = "0"
    code, _text, _err = press(capsys, fake_adb, "--press", "--out", str(tmp_path / "a"))
    assert code == 17
    existing = tmp_path / "b"
    existing.mkdir()
    android.props["ro.kernel.qemu"] = "1"
    code, _text, _err = press(capsys, fake_adb, "--press", "--out", str(existing))
    assert code != 0
    assert device_writes(fake_adb) == []


@pytest.mark.usefixtures("here", "android")
def test_dumps_that_fail_later_name_the_last_screen_read(
    fake_adb, capsys, tmp_path, monkeypatch
):
    from uiprobe.errors import DumpError

    original = Device.dump
    calls = [0]

    def dump_then_fail(self: Device, **kwargs: object) -> object:
        calls[0] += 1
        if calls[0] > 2:
            raise DumpError("uiautomator dump failed", details={"output": "x"})
        return original(self, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Device, "dump", dump_then_fail)
    code, text, _err = press(capsys, fake_adb, "--press", "--out", str(tmp_path / "r"))
    assert code == 0
    assert "stopped: dump_failed_repeatedly" in text
    note = next(line for line in text.splitlines() if "kept failing" in line)
    assert "last screen read: s-" in note


@pytest.mark.usefixtures("here")
def test_a_rotated_display_refuses_every_tap(fake_adb, android, capsys, tmp_path):
    android.orientation = 1
    code, text, _err = press(capsys, fake_adb, "--press", "--out", str(tmp_path / "r"))
    assert code == 0
    assert [i for i in android.inputs if i.kind == "tap"] == []
    assert "cover_unknown" in text
    assert any(
        "rotation" in line for line in text.splitlines() if line.startswith("note: ")
    )


@pytest.mark.usefixtures("here")
def test_every_pin_check_still_requires_an_emulator(
    fake_adb, android, capsys, tmp_path, monkeypatch
):
    # The standalone check says emulator, the identity read through the pin
    # says otherwise: without --allow-real-device nothing may be started.
    monkeypatch.setattr(Device, "is_emulator", lambda _self: True)
    android.props["ro.kernel.qemu"] = "0"
    out = tmp_path / "r"
    code, text, _err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert code != 0
    assert device_writes(fake_adb) == []
    assert not out.exists()  # refused before the plan and the folder
    assert "pressing on" not in text


@pytest.mark.usefixtures("here", "android")
def test_a_closed_plan_stream_refuses_before_anything_is_sent(
    fake_adb, capsys, tmp_path, monkeypatch
):
    monkeypatch.setattr("sys.stderr", None)
    out = tmp_path / "r"
    code = cli.run(
        [
            "--adb-server",
            fake_adb.address,
            "crawl",
            SHOP,
            "--press",
            "--out",
            str(out),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    error = json.loads(captured.out)["error"]
    assert "stderr is closed" in error["message"]
    assert device_writes(fake_adb) == []
    assert not out.exists()


class _BrokenOutput(io.StringIO):
    """An output whose writes fail as a closed pipe does."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


@pytest.mark.usefixtures("here", "android")
@pytest.mark.parametrize("as_json", [False, True])
def test_a_plan_that_cannot_be_written_stops_before_the_launch(
    fake_adb, tmp_path, monkeypatch, as_json
):
    monkeypatch.setattr("sys.stderr" if as_json else "sys.stdout", _BrokenOutput())
    argv = ["crawl", SHOP, "--press", "--out", str(tmp_path / "r")]
    code = cli.run(
        ["--adb-server", fake_adb.address, *argv, *(["--json"] if as_json else [])]
    )
    assert code != 0
    assert device_writes(fake_adb) == []
