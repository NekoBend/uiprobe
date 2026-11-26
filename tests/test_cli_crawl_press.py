"""Tests for ``uiprobe crawl --press`` (fake adb server and fake device)."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import OTHER, SCREENS, SERIAL, SHOP, TRANSPORT
from tests.fake_device import FakeAndroid
from tests.helpers import no_out_message
from uiprobe import cli
from uiprobe.crawl_driver import CrawlResult
from uiprobe.device import Device
from uiprobe.settings import SETTINGS_FILE, _shown, render_default_settings

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / SETTINGS_FILE).write_text(render_default_settings(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


NOT_PRESSING = (
    "is used only when the crawl presses: add --press, or set press = true in"
    f" the settings file {SETTINGS_FILE}"
)
DRY_RUN_PRESS_ONLY = "is used only when the crawl presses, and --dry-run never presses"


def press(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.usefixtures("here")
def test_press_runs_a_short_crawl_on_an_emulator(fake_adb, android, capsys, tmp_path):
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out), "--quiet")
    assert (code, err) == (0, "")
    lines = text.splitlines()
    assert lines[0] == f"pressing on {SERIAL} (emulator): {SHOP}"
    assert lines[1] == "pressing because: --press"
    assert lines[2] == (
        "limits: depth 5 (settings file), actions 300 (settings file),"
        " minutes 30 (settings file)"
    )
    assert lines[3] == f"run folder: {out} (--out)"
    assert lines[4] == "BACK on screens of other apps: no (restart the app)"
    assert lines[5] == "check the window at each tap point: yes"
    assert lines[6] == (
        "start: the app is stopped first (am force-stop), then its launcher"
        " activity is started; what it had not saved is lost (--no-restart"
        " starts from the screen it shows now)"
    )
    assert lines[7].startswith("stopped: ")
    assert any(line.startswith("actions: ") for line in lines)
    assert f"run folder: {out}" in lines[8:]
    assert [i for i in android.inputs if i.kind == "tap"]
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["limits"] == {"depth": 5, "actions": 300, "minutes": 30}
    assert begin["check_tap_window"] is True
    assert begin["back_on_other_screens"] is False
    assert begin["restart"] is True
    assert android.stopped == [SHOP]  # the first launch's force-stop
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
        "--no-restart",
        "--json",
    )
    assert code == 0
    # The plan goes to stderr with --json, so stdout is one JSON document.
    assert err.splitlines()[0] == f"pressing on {SERIAL} (emulator): {SHOP}"
    result = json.loads(text)
    assert result["schema"] == "uiprobe.crawl_run/1"
    assert result["package"] == SHOP
    assert result["device"] == {"serial": SERIAL, "emulator": True}
    assert result["press_source"] == "option"
    assert result["limits"] == {"depth": 5, "actions": 4, "minutes": 30}
    assert result["limits_source"] == {
        "depth": "file",
        "actions": "option",
        "minutes": "file",
    }
    assert result["back_on_other_screens"] is True
    assert result["check_tap_window"] is False
    assert result["restart"] is False
    assert result["out"] == str(out)
    assert result["stop_reason"] == "max_actions"
    assert result["actions"] == 4
    assert isinstance(result["not_sent"], dict)
    assert result["repin"] is True
    assert result["notes"] == []
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["check_tap_window"] is False
    assert begin["back_on_other_screens"] is True
    assert begin["restart"] is False


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
        capsys, fake_adb, "--press", "--out", str(out), "--allow-real-device", "--quiet"
    )
    assert (code, err) == (0, "")
    assert text.splitlines()[0] == f"pressing on {SERIAL} (real device): {SHOP}"
    assert [i for i in android.inputs if i.kind == "tap"]


@pytest.mark.usefixtures("here")
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--out", "x"], f"--out {NOT_PRESSING}"),
        (["--actions", "3"], f"--actions {NOT_PRESSING}"),
        (["--allow-real-device"], f"--allow-real-device {NOT_PRESSING}"),
        (["--no-tap-window-check"], f"--no-tap-window-check {NOT_PRESSING}"),
        (["--no-restart"], f"--no-restart {NOT_PRESSING}"),
        (["--no-record-text"], f"--no-record-text {NOT_PRESSING}"),
        (["--dry-run", "--out", "x"], f"--out {DRY_RUN_PRESS_ONLY}"),
        (["--dry-run", "--minutes", "3"], f"--minutes {DRY_RUN_PRESS_ONLY}"),
        (["--dry-run", "--screenshots"], f"--screenshots {DRY_RUN_PRESS_ONLY}"),
        (["--dry-run", "--no-restart"], f"--no-restart {DRY_RUN_PRESS_ONLY}"),
        (
            ["--dry-run", "--no-record-text"],
            f"--no-record-text {DRY_RUN_PRESS_ONLY}",
        ),
        (["--press"], no_out_message(SETTINGS_FILE, press_in_file=False)),
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
    code, _text, err = press(capsys, fake_adb, "--press", "--out", str(out), "--quiet")
    assert code == 130
    assert (
        err == f"uiprobe: error: interrupted; the run so far is in {_shown(str(out))}\n"
    )
    run_json = json.loads((out / "run.json").read_bytes())
    assert run_json["stop_reason"] == "interrupted"


@pytest.mark.usefixtures("here")
def test_an_unreadable_tap_window_explains_the_option(
    fake_adb, android, capsys, tmp_path, monkeypatch
):
    monkeypatch.setattr(Device, "window_at", lambda _self, _point, **_kw: "line_format")
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out), "--quiet")
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
def test_press_true_without_out_names_out_and_the_file(fake_adb, android, capsys, here):
    text = render_default_settings().replace("press = false", "press = true")
    (here / SETTINGS_FILE).write_text(text, encoding="utf-8")
    code, out, err = press(capsys, fake_adb)
    assert (code, out) == (2, "")
    message = no_out_message(SETTINGS_FILE, press_in_file=True)
    assert err == f"uiprobe: error: {message}\n"
    assert fake_adb.requests == []
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
    # The first request that changes the device is the force-stop of the
    # first launch (the default), then its start.
    seen: list[tuple[str, str]] = []
    for name in ("stop_app", "_launch_component"):
        original = getattr(Device, name)

        def after_reading(
            self: Device, *args: object, _original=original, **kwargs: object
        ) -> object:
            captured = capsys.readouterr()
            seen.append((captured.out, captured.err))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Device, name, after_reading)
    out = tmp_path / "run"
    argv = ["--press", "--out", str(out), "--actions", "2"]
    code, _text, _err = press(capsys, fake_adb, *argv, *(["--json"] if as_json else []))
    assert code == 0
    assert seen, "the app was launched"
    plan_out, plan_err = seen[0]
    plan = plan_err if as_json else plan_out
    assert plan.splitlines()[0] == f"pressing on {SERIAL} (emulator): {SHOP}"
    assert "check the window at each tap point: yes" in plan
    assert "start: the app is stopped first (am force-stop)" in plan
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


@pytest.mark.usefixtures("here")
def test_an_unreadable_launcher_activity_starts_nothing(
    fake_adb, android, capsys, tmp_path
):
    android.override(re.compile(r"^cmd package"), b"No activity found\n")
    out = tmp_path / "run"
    code, text, _err = press(capsys, fake_adb, "--press", "--out", str(out), "--json")
    assert code == 15
    error = json.loads(text)["error"]
    assert error["code"] == "launch_failed"
    assert error["details"]["reason"] == "launcher_unreadable"
    # The launcher is read before the first launch's force-stop: nothing was
    # stopped, started or pressed.
    assert device_writes(fake_adb) == []
    assert android.inputs == []
    records = [
        json.loads(line)
        for line in (out / "transitions.jsonl").read_text("utf-8").splitlines()
    ]
    assert records[-1]["reason"] == "launcher_unreadable"


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
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out))
    assert code == 17
    assert "--allow-real-device" in err
    assert device_writes(fake_adb) == []
    assert not out.exists()  # refused before the plan and the folder
    assert "pressing on" not in text


@pytest.mark.usefixtures("here", "android")
def test_the_driver_gets_the_callers_own_real_device_choice(
    fake_adb, capsys, tmp_path, monkeypatch
):
    import uiprobe.cli.crawl_press as cli_module

    seen: list[object] = []
    original = cli_module.crawl_run

    def spy(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("allow_real_device"))
        return original(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(cli_module, "crawl_run", spy)
    argv = ["--press", "--out", str(tmp_path / "a"), "--actions", "2"]
    assert press(capsys, fake_adb, *argv)[0] == 0
    argv = [
        "--press",
        "--out",
        str(tmp_path / "b"),
        "--actions",
        "2",
        "--allow-real-device",
    ]
    assert press(capsys, fake_adb, *argv)[0] == 0
    assert seen == [False, True]


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


@pytest.mark.usefixtures("here")
def test_a_changing_identity_answer_cannot_undo_the_refusal(
    fake_adb, android, capsys, tmp_path, monkeypatch
):
    # The pinned identity says not an emulator; a later fresh read would say
    # emulator. The refusal rests on the verified identity, before any plan.
    monkeypatch.setattr(Device, "is_emulator", lambda _self: True)
    real_identity = Device.identity
    real_check = Device.check_pin
    checked = [False]

    def check_then_flip(self: Device, **kwargs: object) -> None:
        checked[0] = False
        real_check(self, **kwargs)  # ty: ignore[invalid-argument-type]
        checked[0] = True  # reads after a completed pin check answer "emulator"

    def flipping(self: Device) -> object:
        found = real_identity(self)
        if found is not None and checked[0]:
            return found._replace(emulator=True)
        return found

    monkeypatch.setattr(Device, "check_pin", check_then_flip)
    monkeypatch.setattr(Device, "identity", flipping)
    android.props["ro.kernel.qemu"] = "0"
    out = tmp_path / "r"
    code, text, _err = press(capsys, fake_adb, "--press", "--out", str(out), "--json")
    error = json.loads(text)["error"]
    assert (code, error["code"], error["details"]["reason"]) == (
        17,
        "not_emulator",
        "not_emulator",
    )
    assert not out.exists()
    assert device_writes(fake_adb) == []


# --- the hint when restarts keep meeting another app ---


def _launch_into_other_app(monkeypatch, android: FakeAndroid) -> None:
    original = Device._launch_component

    def launch_then_cover(self: Device, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]
        android.show("ext1")  # every start ends on a screen of another app

    monkeypatch.setattr(Device, "_launch_component", launch_then_cover)


@pytest.mark.usefixtures("here")
def test_a_relaunch_limit_on_another_app_points_to_back(
    fake_adb, android, capsys, tmp_path, monkeypatch
):
    _launch_into_other_app(monkeypatch, android)
    code, text, err = press(
        capsys, fake_adb, "--press", "--out", str(tmp_path / "r"), "--quiet"
    )
    assert (code, err) == (0, "")  # the stop reason and exit code are unchanged
    assert "stopped: relaunch_limit" in text
    [note] = [line for line in text.splitlines() if "--back-on-other-screens" in line]
    assert note.startswith("note: ")
    assert f"of other apps ({OTHER} 4 times); --back-on-other-screens" in note
    assert "reaches whichever window has the input focus" in note
    assert android.inputs == []  # no tap or key: only the launch and restarts


@pytest.mark.usefixtures("here")
def test_the_relaunch_limit_hint_is_in_the_json_notes(
    fake_adb, android, capsys, tmp_path, monkeypatch
):
    _launch_into_other_app(monkeypatch, android)
    code, text, _err = press(
        capsys, fake_adb, "--press", "--out", str(tmp_path / "r"), "--json"
    )
    assert code == 0
    result = json.loads(text)
    assert result["stop_reason"] == "relaunch_limit"
    assert len([n for n in result["notes"] if "--back-on-other-screens" in n]) == 1
    assert result["other_apps"] == [{"package": OTHER, "count": 4}]


def _result(
    stop_reason: str,
    front: str | None,
    focus: str | None = None,
    other_apps: tuple[tuple[str, int], ...] | None = None,
) -> CrawlResult:
    return CrawlResult(
        Path("r"),
        stop_reason,
        0,
        4,
        repin=True,
        last_foreground=front,
        last_focus=focus,
        other_apps=(
            ((front, 1),) if front is not None and other_apps is None else other_apps
        )
        or (),
    )


def _parenthesis(other_apps: tuple[tuple[str, int], ...]) -> str:
    result = _result("relaunch_limit", OTHER, other_apps=other_apps)
    [hint] = [
        h
        for h in cli._press_hints(result, package=SHOP, back=False)
        if "--back-on-other-screens" in h
    ]
    head, _, tail = hint.partition(" (")
    assert head == "the restarts kept ending on screens of other apps"
    inside, _, rest = tail.partition("); ")
    assert rest.startswith("--back-on-other-screens presses BACK on such a screen")
    return inside


def test_the_relaunch_limit_hint_names_every_package_with_its_count():
    many = (("com.settings.intel", 3), ("com.launcher", 1))
    assert _parenthesis(many) == "com.settings.intel 3 times, com.launcher once"
    assert _parenthesis((("com.launcher", 1),)) == "com.launcher once"
    assert _parenthesis((("com.a", 2), ("com.b", 2))) == "com.a 2 times, com.b 2 times"


def test_the_relaunch_limit_hint_escapes_package_names():
    assert _parenthesis((("a\u202eb", 1),)) == "a\\u202eb once"


def test_the_relaunch_limit_hint_needs_a_landing():
    result = _result("relaunch_limit", OTHER, other_apps=())
    assert cli._press_hints(result, package=SHOP, back=False) == []


@pytest.mark.parametrize(
    ("stop_reason", "front", "back", "shown"),
    [
        ("relaunch_limit", OTHER, False, True),
        ("relaunch_limit", OTHER, True, False),  # BACK was already allowed
        ("relaunch_limit", SHOP, False, False),  # the app itself was in front
        ("relaunch_limit", None, False, False),  # unknown
        ("exhausted", OTHER, False, False),
        ("max_actions", OTHER, False, False),
    ],
)
def test_the_relaunch_limit_hint_only_when_it_applies(stop_reason, front, back, shown):
    hints = cli._press_hints(_result(stop_reason, front), package=SHOP, back=back)
    assert any("--back-on-other-screens" in h for h in hints) is shown


def test_the_relaunch_limit_hint_escapes_the_package():
    hints = cli._press_hints(
        _result("relaunch_limit", "com.evil\x1b[2J"), package=SHOP, back=False
    )
    hint = next(h for h in hints if "--back-on-other-screens" in h)
    assert "\x1b" not in hint
    assert "com.evil\\x1b[2J" in hint


# --- the hint when System UI held the focus (locked or dark screen) ---

SYSTEM_UI = "com.android.systemui"


@pytest.mark.parametrize(
    ("stop_reason", "front", "focus", "shown"),
    [
        ("relaunch_limit", SHOP, SYSTEM_UI, True),
        ("relaunch_limit", SHOP, SHOP, False),  # the app had the focus
        ("relaunch_limit", SHOP, None, False),  # unknown
        ("relaunch_limit", SHOP, OTHER, False),  # another app, not System UI
        ("relaunch_limit", OTHER, SYSTEM_UI, False),  # the app was not in front
        ("relaunch_limit", None, SYSTEM_UI, False),
        ("exhausted", SHOP, SYSTEM_UI, False),
    ],
)
def test_the_locked_screen_hint_only_when_it_applies(stop_reason, front, focus, shown):
    for back in (False, True):
        hints = cli._press_hints(
            _result(stop_reason, front, focus), package=SHOP, back=back
        )
        assert len([h for h in hints if "unlock it" in h]) == int(shown)


def test_the_locked_screen_hint_says_what_to_do():
    [hint] = [
        h
        for h in cli._press_hints(
            _result("relaunch_limit", SHOP, SYSTEM_UI), package=SHOP, back=False
        )
        if "unlock" in h
    ]
    assert f"a window of {SYSTEM_UI} had the input focus" in hint
    assert "if the screen is off or locked, turn it on and unlock it" in hint
    assert "if the notification shade or another System UI window is open" in hint
    # the BACK hint does not apply: the app itself was in front
    assert not any(
        "--back-on-other-screens" in h
        for h in cli._press_hints(
            _result("relaunch_limit", SHOP, SYSTEM_UI), package=SHOP, back=False
        )
    )


# --- the first launch stops the app first (--no-restart keeps the screen) ---


def _first_start_line(out: Path) -> dict[str, object]:
    first = (out / "transitions.jsonl").read_text("utf-8").splitlines()[0]
    return json.loads(first)


@pytest.mark.usefixtures("here")
def test_by_default_the_app_is_stopped_before_the_first_start(
    fake_adb, android, capsys, tmp_path
):
    android.show("browse")  # the app is open on a screen below its first one
    out = tmp_path / "run"
    argv = ["--press", "--out", str(out), "--actions", "1", "--json"]
    code, text, err = press(capsys, fake_adb, *argv)
    assert code == 0
    assert "start: the app is stopped first (am force-stop)" in err
    result = json.loads(text)
    assert result["restart"] is True
    assert result["actions"] == 1  # force-stop and start are one action
    writes = device_writes(fake_adb)
    assert [w.split(";")[0] for w in writes] == [
        f"shell:am force-stop {SHOP}",
        f"shell:am start -W -a android.intent.action.MAIN -c"
        f" android.intent.category.LAUNCHER -n {SHOP}/.Main",
    ]
    assert android.screen.name == "main"  # the app's first screen
    first = _first_start_line(out)
    assert first["action"] == {"kind": "launch"}
    assert first["restart"] is True
    assert json.loads((out / "run-start.json").read_bytes())["restart"] is True


@pytest.mark.usefixtures("here")
def test_no_restart_starts_from_the_screen_the_app_shows(
    fake_adb, android, capsys, tmp_path
):
    android.show("browse")
    out = tmp_path / "run"
    argv = ["--press", "--out", str(out), "--actions", "1", "--no-restart"]
    code, text, _err = press(capsys, fake_adb, *argv)
    assert code == 0
    assert text.splitlines()[6] == (
        "start: the app is not stopped first; the crawl starts from the screen"
        " the app shows now"
    )
    assert not [w for w in device_writes(fake_adb) if "force-stop" in w]
    assert android.stopped == []
    assert android.screen.name == "browse"  # brought to the front as it was
    first = _first_start_line(out)
    assert first["action"] == {"kind": "launch"}
    assert "restart" not in first
    assert json.loads((out / "run-start.json").read_bytes())["restart"] is False


@pytest.mark.usefixtures("here", "android")
def test_the_driver_gets_the_restart_choice(fake_adb, capsys, tmp_path, monkeypatch):
    import uiprobe.cli.crawl_press as cli_module

    seen: list[object] = []
    original = cli_module.crawl_run

    def spy(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("restart"))
        return original(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(cli_module, "crawl_run", spy)
    argv = ["--press", "--out", str(tmp_path / "a"), "--actions", "2"]
    assert press(capsys, fake_adb, *argv)[0] == 0
    argv = ["--press", "--out", str(tmp_path / "b"), "--actions", "2", "--no-restart"]
    assert press(capsys, fake_adb, *argv)[0] == 0
    assert seen == [True, False]


def test_the_plan_text_names_the_start_choice():
    plan: dict[str, object] = {
        "package": SHOP,
        "device": {"serial": SERIAL, "emulator": True},
        "press_source": "option",
        "limits": {"depth": 5, "actions": 30, "minutes": 30},
        "limits_source": {"depth": "file", "actions": "option", "minutes": "file"},
        "out": "run",
        "out_source": "option",
        "back_on_other_screens": False,
        "check_tap_window": True,
        "screenshots": False,
    }
    settings = SETTINGS_FILE
    restart = cli._press_plan_text(
        {**plan, "restart": True}, settings=settings
    ).splitlines()
    kept = cli._press_plan_text({**plan, "restart": False}, settings=settings)
    kept = kept.splitlines()
    assert restart[-1] == (
        "start: the app is stopped first (am force-stop), then its launcher"
        " activity is started; what it had not saved is lost (--no-restart"
        " starts from the screen it shows now)"
    )
    assert kept[-1] == (
        "start: the app is not stopped first; the crawl starts from the screen"
        " the app shows now"
    )
    assert restart[:-1] == kept[:-1]


# -- record_text -------------------------------------------------------------------

OFF_IN_FILE = ("record_text = true", "record_text = false")


@pytest.mark.usefixtures("here", "android")
def test_the_option_keeps_texts_out_of_the_records_and_says_so_in_the_plan(
    fake_adb, capsys, tmp_path
):
    out = tmp_path / "run"
    code, text, err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--no-record-text", "--quiet"
    )
    assert (code, err) == (0, "")
    plan = text.splitlines()
    wanted = (
        "texts of the screens will not be recorded (password fields are never"
        " recorded); the run folder holds no screen text"
    )
    assert wanted in plan
    assert plan.index(wanted) < next(
        i for i, line in enumerate(plan) if line.startswith("stopped: ")
    )
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["record_text"] is False
    shown = "".join(
        p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file()
    )
    for word in ("Delete account", "Help page"):
        assert word not in shown


@pytest.mark.usefixtures("here", "android")
def test_the_plan_is_silent_about_texts_when_they_are_recorded(
    fake_adb, capsys, tmp_path
):
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out), "--quiet")
    assert (code, err) == (0, "")
    assert "recorded" not in text.split("stopped: ")[0]
    assert json.loads((out / "run-start.json").read_bytes())["record_text"] is True


@pytest.mark.usefixtures("android")
def test_the_file_key_turns_texts_off(fake_adb, capsys, tmp_path, here):
    (here / SETTINGS_FILE).write_text(
        render_default_settings().replace(*OFF_IN_FILE), encoding="utf-8"
    )
    out = tmp_path / "run"
    code, text, err = press(capsys, fake_adb, "--press", "--out", str(out), "--quiet")
    assert (code, err) == (0, "")
    assert "texts of the screens will not be recorded" in text
    assert json.loads((out / "run-start.json").read_bytes())["record_text"] is False


@pytest.mark.usefixtures("here", "android")
def test_the_json_result_says_whether_texts_were_recorded(fake_adb, capsys, tmp_path):
    out = tmp_path / "run"
    code, text, _err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--no-record-text", "--json"
    )
    assert code == 0
    assert json.loads(text)["record_text"] is False


@pytest.mark.usefixtures("here")
def test_screenshots_with_the_option_are_refused_before_any_contact(
    fake_adb, android, capsys, tmp_path
):
    out = tmp_path / "run"
    code, text, err = press(
        capsys,
        fake_adb,
        "--press",
        "--out",
        str(out),
        "--screenshots",
        "--no-record-text",
    )
    assert (code, text) == (2, "")
    assert err.startswith("uiprobe: error: --screenshots cannot be used with")
    assert "--no-record-text" in err
    assert "shows the text" in err
    assert not out.exists()
    assert android.inputs == []
    assert fake_adb.requests == []


def test_screenshots_with_the_file_key_are_refused_before_any_contact(
    fake_adb, android, capsys, tmp_path, here
):
    (here / SETTINGS_FILE).write_text(
        render_default_settings().replace(*OFF_IN_FILE), encoding="utf-8"
    )
    out = tmp_path / "run"
    code, text, err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--screenshots"
    )
    assert (code, text) == (2, "")
    assert "record_text = false in" in err
    assert "shows the text" in err
    assert not out.exists()
    assert android.inputs == []
    assert fake_adb.requests == []


# -- device text in errors ---------------------------------------------------------

REPLY = "MARKERreply 'Window{abc u0 MARKERtitle}'"
HIDDEN_REPLY = "(device text not shown: record_text = false)"


@pytest.fixture
def failing_after_main(fake_adb, android, monkeypatch):
    """Once the first app screen shows, the adb server answers FAIL with REPLY."""
    original = android.show

    def show(name: str) -> None:
        original(name)
        if name == "main":
            for request in (
                f"host:transport-id:{TRANSPORT}",
                f"host:transport:{SERIAL}",
            ):
                fake_adb.fail_request(request, REPLY)

    monkeypatch.setattr(android, "show", show)


@pytest.mark.usefixtures("here", "failing_after_main")
def test_an_error_reply_is_shown_when_texts_are_recorded(fake_adb, capsys, tmp_path):
    code, _text, err = press(capsys, fake_adb, "--press", "--out", str(tmp_path / "r"))
    assert code == 10
    assert REPLY in err


@pytest.mark.usefixtures("here", "failing_after_main")
def test_an_error_reply_is_hidden_when_texts_are_not_recorded(
    fake_adb, capsys, tmp_path
):
    out = tmp_path / "r"
    code, text, err = press(
        capsys, fake_adb, "--press", "--out", str(out), "--no-record-text"
    )
    assert code == 10
    assert err == (f"uiprobe: error: adb server refused the request: {HIDDEN_REPLY}\n")
    found = (
        text
        + err
        + "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    )
    assert "MARKER" not in found


@pytest.mark.usefixtures("here", "failing_after_main")
def test_an_error_reply_is_hidden_in_json_too(fake_adb, capsys, tmp_path):
    code, text, err = press(
        capsys,
        fake_adb,
        "--press",
        "--out",
        str(tmp_path / "r"),
        "--no-record-text",
        "--json",
    )
    assert code == 10
    assert "MARKER" not in text + err
    error = json.loads(err.strip().splitlines()[-1] if not text.strip() else text)
    body = error["error"]
    assert body["code"] == "adb_error"
    assert HIDDEN_REPLY in body["message"]
    assert body["details"]["message"] == HIDDEN_REPLY
