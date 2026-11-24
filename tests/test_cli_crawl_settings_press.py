"""Tests for a crawl that follows the settings file: press = true and [limits]."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT
from uiprobe import cli
from uiprobe.crawl import Limits
from uiprobe.settings import (
    DEFAULTS_VERSION,
    SETTINGS_FILE,
    load_settings,
    render_default_settings,
)

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

DATA = Path(__file__).parent / "data"
# No pause after a press, so that a pressing run in a test ends quickly.
FAST = ("--settle", "0")
LIMITS_TEXT = "depth = 5\nactions = 300\nminutes = 30\n"
NO_OUT = (
    f"the settings file {SETTINGS_FILE} says press = true, so this crawl presses"
    " and needs --out DIR, the new folder to record the run into; add --dry-run"
    " to preview instead"
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def settings_text(
    *, press: bool = False, limits: tuple[int, int, int] | None = None
) -> str:
    """The generated file, with press and the [limits] values replaced."""
    return edited(render_default_settings(), press=press, limits=limits)


def edited(
    text: str, *, press: bool = False, limits: tuple[int, int, int] | None = None
) -> str:
    assert text.count("press = false\n") == 1
    assert text.count(LIMITS_TEXT) == 1
    if press:
        text = text.replace("press = false\n", "press = true\n")
    if limits is not None:
        depth, actions, minutes = limits
        text = text.replace(
            LIMITS_TEXT, f"depth = {depth}\nactions = {actions}\nminutes = {minutes}\n"
        )
    return text


def write_settings(folder: Path, text: str) -> Path:
    path = folder / SETTINGS_FILE
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def crawl(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def taps(android: FakeAndroid) -> list[object]:
    return [i for i in android.inputs if i.kind == "tap"]


def device_writes(fake_adb: FakeAdbServer) -> list[str]:
    """The shell commands that start, stop or press something."""
    words = ("monkey", "am start", "force-stop", "input ")
    return [
        r
        for r in fake_adb.requests
        if r.startswith(("shell:", "exec:")) and any(w in r for w in words)
    ]


# --- press = true -------------------------------------------------------------


def test_press_true_presses_without_the_option(fake_adb, android, capsys, here):
    write_settings(here, settings_text(press=True))
    out = here / "run"
    code, text, err = crawl(
        capsys, fake_adb, *FAST, "--out", str(out), "--actions", "6"
    )
    assert (code, err) == (0, "")
    lines = text.splitlines()
    assert lines[0] == f"pressing on {SERIAL} (emulator): {SHOP}"
    assert lines[1] == f"pressing because: press = true in {SETTINGS_FILE}"
    assert lines[2] == (
        "limits: depth 5 (settings file), actions 6 (--actions),"
        " minutes 30 (settings file)"
    )
    assert taps(android)
    assert (out / "run.json").exists()


def test_press_true_in_json_names_the_file(fake_adb, android, capsys, here):
    write_settings(here, settings_text(press=True))
    out = here / "run"
    code, text, err = crawl(
        capsys, fake_adb, *FAST, "--out", str(out), "--actions", "4", "--json"
    )
    assert code == 0
    assert err.splitlines()[1] == f"pressing because: press = true in {SETTINGS_FILE}"
    result = json.loads(text)
    assert result["schema"] == "uiprobe.crawl_run/1"
    assert result["press_source"] == "file"
    assert result["limits"] == {"depth": 5, "actions": 4, "minutes": 30}
    assert result["limits_source"] == {
        "depth": "file",
        "actions": "option",
        "minutes": "file",
    }
    assert taps(android)


def test_press_true_in_another_file_names_that_file(fake_adb, android, capsys, here):
    other = here / "ci"
    other.mkdir()
    path = write_settings(other, settings_text(press=True))
    code, text, _err = crawl(
        capsys,
        fake_adb,
        "--config",
        str(path),
        *FAST,
        "--out",
        str(here / "run"),
        "--actions",
        "3",
    )
    assert code == 0
    assert text.splitlines()[1] == f"pressing because: press = true in {path}"
    assert taps(android)


@pytest.mark.parametrize("as_json", [False, True])
def test_dry_run_wins_over_press_true(fake_adb, android, capsys, here, as_json):
    write_settings(here, settings_text(press=True))
    argv = ["--dry-run", *(["--json"] if as_json else [])]
    code, text, err = crawl(capsys, fake_adb, *argv)
    assert (code, err) == (0, "")
    if as_json:
        assert json.loads(text)["pressed"] is False
    else:
        assert text.splitlines()[-1] == (
            "nothing was pressed: this was a preview (--dry-run)"
        )
    assert android.inputs == []
    assert device_writes(fake_adb) == []
    assert sorted(p.name for p in here.iterdir()) == [SETTINGS_FILE]


@pytest.mark.parametrize(
    "argv",
    [
        ["--out", "run"],
        ["--screenshots"],
        ["--allow-real-device"],
        ["--no-tap-window-check"],
        ["--no-restart"],
    ],
)
def test_press_true_with_dry_run_still_refuses_press_options(
    fake_adb, android, capsys, here, argv
):
    write_settings(here, settings_text(press=True))
    code, text, err = crawl(capsys, fake_adb, "--dry-run", *argv)
    assert (code, text) == (2, "")
    assert err == (
        f"uiprobe: error: {argv[0]} is used only when the crawl presses,"
        " and --dry-run never presses\n"
    )
    assert fake_adb.requests == []
    assert android.inputs == []


def test_press_true_allows_the_press_options(fake_adb, android, capsys, here):
    write_settings(here, settings_text(press=True))
    out = here / "run"
    argv = [
        "--out",
        str(out),
        "--actions",
        "3",
        "--depth",
        "2",
        "--minutes",
        "4",
        "--settle",
        "0",
        "--screenshots",
        "--back-on-other-screens",
        "--no-tap-window-check",
        "--no-restart",
        "--json",
    ]
    code, text, _err = crawl(capsys, fake_adb, *argv)
    assert code == 0
    result = json.loads(text)
    assert result["press_source"] == "file"
    assert result["limits"] == {"depth": 2, "actions": 3, "minutes": 4}
    assert set(result["limits_source"].values()) == {"option"}
    assert result["back_on_other_screens"] is True
    assert result["check_tap_window"] is False
    assert result["screenshots"] is True
    assert result["restart"] is False
    assert android.stopped == []  # --no-restart: no force-stop before the start


@pytest.mark.parametrize("as_json", [False, True])
def test_press_true_without_out_is_a_usage_error(
    fake_adb, android, capsys, here, connections, as_json
):
    write_settings(here, settings_text(press=True))
    code, text, err = crawl(capsys, fake_adb, *(["--json"] if as_json else []))
    assert code == 2
    if as_json:
        assert err == ""
        error = json.loads(text)["error"]
        assert (error["code"], error["message"]) == ("usage_error", NO_OUT)
    else:
        assert (text, err) == ("", f"uiprobe: error: {NO_OUT}\n")
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_the_out_choice_is_made_in_one_place(fake_adb, capsys, here, monkeypatch):
    # O2 or O3 would replace only cli._file_press_out: what it returns is the
    # run folder of a run that presses by the file without --out.
    write_settings(here, settings_text(press=True))
    chosen = here / "chosen"
    monkeypatch.setattr(cli, "_file_press_out", lambda _settings: str(chosen))
    code, text, _err = crawl(capsys, fake_adb, *FAST, "--actions", "3")
    assert code == 0
    assert f"run folder: {chosen}" in text.splitlines()
    assert (chosen / "run.json").exists()


def test_press_true_needs_the_real_device_option_on_every_run(
    fake_adb, android, capsys, here
):
    android.props["ro.kernel.qemu"] = "0"
    write_settings(here, settings_text(press=True))
    out = here / "run"
    code, text, err = crawl(capsys, fake_adb, *FAST, "--out", str(out))
    assert (code, text) == (17, "")
    assert "--allow-real-device" in err
    assert "nothing was pressed" in err
    assert not out.exists()
    assert android.inputs == []
    assert device_writes(fake_adb) == []
    code, text, err = crawl(
        capsys,
        fake_adb,
        *FAST,
        "--out",
        str(out),
        "--actions",
        "3",
        "--allow-real-device",
    )
    assert (code, err) == (0, "")
    assert text.splitlines()[0] == f"pressing on {SERIAL} (real device): {SHOP}"
    assert taps(android)
    # The option counted for that run only: the next run is refused again.
    code, _text, _err = crawl(capsys, fake_adb, *FAST, "--out", str(here / "again"))
    assert code == 17
    assert not (here / "again").exists()


@pytest.mark.usefixtures("android")
def test_a_closed_plan_stream_refuses_a_press_true_run(
    fake_adb, capsys, here, monkeypatch
):
    write_settings(here, settings_text(press=True))
    monkeypatch.setattr("sys.stderr", None)
    out = here / "run"
    code = cli.run(
        [
            "--adb-server",
            fake_adb.address,
            "crawl",
            SHOP,
            *FAST,
            "--out",
            str(out),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "stderr is closed" in json.loads(captured.out)["error"]["message"]
    assert fake_adb.requests == []
    assert not out.exists()


# --- limits -------------------------------------------------------------------


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize(
    ("argv", "values", "sources", "text"),
    [
        (
            [],
            {"depth": 2, "actions": 7, "minutes": 9},
            {"depth": "file", "actions": "file", "minutes": "file"},
            "depth 2 (settings file), actions 7 (settings file),"
            " minutes 9 (settings file)",
        ),
        (
            ["--minutes", "11"],
            {"depth": 2, "actions": 7, "minutes": 11},
            {"depth": "file", "actions": "file", "minutes": "option"},
            "depth 2 (settings file), actions 7 (settings file),"
            " minutes 11 (--minutes)",
        ),
        (
            ["--depth", "4", "--actions", "3", "--minutes", "1"],
            {"depth": 4, "actions": 3, "minutes": 1},
            {"depth": "option", "actions": "option", "minutes": "option"},
            "depth 4 (--depth), actions 3 (--actions), minutes 1 (--minutes)",
        ),
    ],
)
def test_limits_come_from_the_option_else_the_file(
    fake_adb, capsys, here, argv, values, sources, text
):
    write_settings(here, settings_text(limits=(2, 7, 9)))
    out = here / "run"
    code, result_text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(out), *argv, "--json"
    )
    assert code == 0
    assert err.splitlines()[2] == f"limits: {text}"
    result = json.loads(result_text)
    assert result["press_source"] == "option"
    assert result["limits"] == values
    assert result["limits_source"] == sources
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["limits"] == values


@pytest.mark.usefixtures("android")
def test_the_file_limits_end_the_run(fake_adb, capsys, here):
    write_settings(here, settings_text(limits=(5, 2, 30)))
    code, text, _err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "run"), "--json"
    )
    assert code == 0
    result = json.loads(text)
    assert result["stop_reason"] == "max_actions"
    assert result["actions"] == 2


def _args(**given: int | None) -> argparse.Namespace:
    values = {"depth": None, "actions": None, "minutes": None} | given
    return argparse.Namespace(**values)


def test_limits_without_a_file_value_take_the_defaults():
    limits, sources = cli._press_limits(_args(), None)
    assert (limits.depth, limits.actions, limits.minutes) == (5, 300, 30)
    assert sources == {"depth": "default", "actions": "default", "minutes": "default"}
    limits, sources = cli._press_limits(_args(actions=12), None)
    assert (limits.depth, limits.actions, limits.minutes) == (5, 12, 30)
    assert sources == {"depth": "default", "actions": "option", "minutes": "default"}


def test_the_plan_names_a_default_limit():
    plan: dict[str, object] = {
        "limits": {"depth": 5, "actions": 12, "minutes": 30},
        "limits_source": {"depth": "default", "actions": "option", "minutes": "file"},
    }
    assert cli._press_limits_text(plan) == (
        "limits: depth 5 (default), actions 12 (--actions), minutes 30 (settings file)"
    )


def test_init_writes_the_no_flag_defaults(tmp_path, capsys):
    target = tmp_path / SETTINGS_FILE
    assert cli.run(["init", str(target)]) == 0
    capsys.readouterr()
    written = load_settings(target)
    no_flag, _sources = cli._press_limits(_args(), None)
    assert (written.depth, written.actions, written.minutes) == (
        no_flag.depth,
        no_flag.actions,
        no_flag.minutes,
    )
    assert (no_flag.depth, no_flag.actions, no_flag.minutes) == (5, 300, 30)
    from_file, _sources = cli._press_limits(_args(), written)
    assert from_file == no_flag
    # The Python default of crawl_run() agrees too.
    assert Limits() == no_flag


def test_the_help_states_the_defaults(capsys):
    assert cli.run(["crawl", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    for name, value in (("depth", 5), ("actions", 300), ("minutes", 30)):
        assert (
            f"(default: [limits] {name} of the settings file; uiprobe init writes"
            f" {value})"
        ) in text


# --- files written by older versions ------------------------------------------


def _old_file(version: int) -> str:
    """The file `uiprobe init` wrote at `version`; the current one is rendered."""
    if version == DEFAULTS_VERSION:
        text = render_default_settings()
    else:
        text = (DATA / f"settings_v{version}.toml").read_text(encoding="utf-8")
    assert f"defaults_version = {version}\n" in text
    return text


# Every defaults version: the files kept under tests/data and the current one.
VERSIONS = list(range(1, DEFAULTS_VERSION + 1))


def test_every_older_version_has_a_kept_file():
    for version in VERSIONS[:-1]:
        assert (DATA / f"settings_v{version}.toml").is_file(), version
    assert DEFAULTS_VERSION >= 4


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("version", VERSIONS)
def test_older_files_use_their_written_limits(fake_adb, capsys, here, version):
    write_settings(here, edited(_old_file(version), limits=(3, 5, 7)))
    code, text, _err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "run"), "--json"
    )
    assert code == 0
    result = json.loads(text)
    assert result["limits"] == {"depth": 3, "actions": 5, "minutes": 7}
    assert set(result["limits_source"].values()) == {"file"}


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("version", VERSIONS)
def test_older_files_unchanged_give_the_generated_limits(
    fake_adb, capsys, here, version
):
    write_settings(here, _old_file(version))
    code, text, _err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "run"), "--actions", "2"
    )
    assert code == 0
    assert text.splitlines()[2] == (
        "limits: depth 5 (settings file), actions 2 (--actions),"
        " minutes 30 (settings file)"
    )


@pytest.mark.parametrize("version", VERSIONS)
def test_older_files_do_not_start_pressing(fake_adb, android, capsys, here, version):
    write_settings(here, _old_file(version))
    code, text, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    assert re.match(rf"preview for {re.escape(SHOP)} on screen ", text)
    assert android.inputs == []
    assert device_writes(fake_adb) == []


# --- guidance after a preview -------------------------------------------------

LAST = "nothing was pressed: this was a preview (the default)"
TO_PRESS = f"to press this time: uiprobe crawl {SHOP} --press --out DIR"
TO_ALWAYS = f"to always press: set press = true in {SETTINGS_FILE}"
REAL = "on a real device also add --allow-real-device"


def _ending(text: str) -> list[str]:
    lines = text.splitlines()
    return lines[lines.index(LAST) :]


@pytest.mark.usefixtures("android")
def test_a_preview_on_an_emulator_says_how_to_press(fake_adb, capsys, here):
    write_settings(here, settings_text())
    code, text, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    assert _ending(text) == [LAST, TO_PRESS, TO_ALWAYS]


def test_a_preview_on_a_real_device_adds_the_option(fake_adb, android, capsys, here):
    android.props["ro.kernel.qemu"] = "0"
    write_settings(here, settings_text())
    code, text, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    assert _ending(text) == [LAST, TO_PRESS, TO_ALWAYS, REAL]
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_a_preview_with_dry_run_and_press_false_says_how_to_press(
    fake_adb, capsys, here
):
    write_settings(here, settings_text())
    code, text, _err = crawl(capsys, fake_adb, "--dry-run")
    assert code == 0
    lines = text.splitlines()
    assert lines[-3:] == [
        "nothing was pressed: this was a preview (--dry-run)",
        TO_PRESS,
        TO_ALWAYS,
    ]


@pytest.mark.usefixtures("android")
def test_the_guidance_keeps_the_named_file(fake_adb, capsys, here):
    folder = here / "ci"
    folder.mkdir()
    path = write_settings(folder, settings_text())
    code, text, _err = crawl(capsys, fake_adb, "--config", str(path))
    assert code == 0
    assert _ending(text) == [
        LAST,
        f"{TO_PRESS} --config {path}",
        f"to always press: set press = true in {path}",
    ]


@pytest.mark.usefixtures("android")
def test_the_guidance_does_not_print_a_path_that_needs_quoting(fake_adb, capsys, here):
    folder = here / "my settings"
    folder.mkdir()
    path = write_settings(folder, settings_text())
    code, text, _err = crawl(capsys, fake_adb, "--config", str(path))
    assert code == 0
    assert _ending(text)[1] == f"{TO_PRESS} --config PATH"


@pytest.mark.usefixtures("android")
def test_the_json_preview_has_no_guidance(fake_adb, capsys, here):
    write_settings(here, settings_text())
    code, text, _err = crawl(capsys, fake_adb, "--json")
    assert code == 0
    payload = json.loads(text)
    assert "to press" not in text
    assert payload["mode"] == "preview"
    # The emulator check is made only for the text guidance.
    assert not any("ro.kernel.qemu" in r for r in fake_adb.requests)
