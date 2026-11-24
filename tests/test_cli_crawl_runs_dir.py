"""Tests for runs_dir: the settings key that gives a pressing crawl its run folder."""

from __future__ import annotations

import json
import ntpath
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_device import FakeAndroid
from tests.test_cli_crawl_settings_press import (
    FAST,
    device_writes,
    settings_text,
    write_settings,
)
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT
from uiprobe import cli, runlog
from uiprobe import settings as settings_module
from uiprobe.device import IDENTITY_COMMAND
from uiprobe.errors import AdbError
from uiprobe.settings import (
    SETTINGS_FILE,
    SettingsError,
    load_settings,
    render_default_settings,
)

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

STAMP = "20261125-101500"
MOUNT_POINT = 0xA0000003  # IO_REPARSE_TAG_MOUNT_POINT: a junction
NAME = f"{SHOP}-{STAMP}"
PRESS_NEEDS_OUT = "--press needs --out DIR, the new folder to record the run into"


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_run_stamp", lambda: STAMP)
    return tmp_path


def with_runs_dir(value: str, *, press: bool = True) -> str:
    """The generated file with press and an active runs_dir line."""
    text = settings_text(press=press)
    line = '# runs_dir = "uiprobe-runs"\n'
    assert text.count(line) == 1
    return text.replace(line, f"runs_dir = {json.dumps(value)}\n")


def crawl(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def error_of(text: str) -> dict[str, Any]:
    return json.loads(text)["error"]


# --- where the run folder goes ------------------------------------------------


@pytest.mark.usefixtures("android")
def test_a_relative_runs_dir_counts_from_the_settings_file(fake_adb, capsys, here):
    folder = here / "project"
    (folder / "runs").mkdir(parents=True)
    write_settings(folder, with_runs_dir("runs"))
    elsewhere = here / "elsewhere"
    elsewhere.mkdir()
    os.chdir(elsewhere)
    config = str(Path("..", "project", SETTINGS_FILE))
    code, text, err = crawl(capsys, fake_adb, "--config", config, *FAST, "--json")
    assert code == 0
    run = Path("..", "project", "runs", NAME)
    assert err.splitlines()[3] == f"run folder: {run} (runs_dir in {config})"
    result = json.loads(text)
    assert result["press_source"] == "file"
    assert result["out_source"] == "file"
    assert (folder / "runs" / NAME / "run.json").exists()
    assert sorted(p.name for p in elsewhere.iterdir()) == []


@pytest.mark.usefixtures("android")
def test_an_absolute_runs_dir_is_used_as_is(fake_adb, capsys, here):
    runs = here / "all-runs"
    runs.mkdir()
    write_settings(here, with_runs_dir(str(runs)))
    code, text, _err = crawl(capsys, fake_adb, *FAST, "--actions", "2")
    assert code == 0
    assert f"run folder: {runs / NAME} (runs_dir in {SETTINGS_FILE})" in text
    assert (runs / NAME / "run.json").exists()


@pytest.mark.usefixtures("android")
def test_press_without_out_uses_runs_dir(fake_adb, capsys, here):
    (here / "runs").mkdir()
    write_settings(here, with_runs_dir("runs", press=False))
    code, text, _err = crawl(capsys, fake_adb, "--press", *FAST, "--json")
    assert code == 0
    result = json.loads(text)
    assert (result["press_source"], result["out_source"]) == ("option", "file")
    assert Path(result["out"]) == here / "runs" / NAME
    assert (here / "runs" / NAME / "run.json").exists()


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("switch", [[], ["--press"]])
def test_out_wins_over_runs_dir(fake_adb, capsys, here, switch):
    (here / "runs").mkdir()
    write_settings(here, with_runs_dir("runs"))
    out = here / "mine"
    code, text, err = crawl(
        capsys, fake_adb, *switch, "--out", str(out), *FAST, "--json"
    )
    assert code == 0
    assert err.splitlines()[3] == f"run folder: {out} (--out)"
    assert json.loads(text)["out_source"] == "option"
    assert (out / "run.json").exists()
    assert list((here / "runs").iterdir()) == []


@pytest.mark.parametrize(
    ("press", "switch", "message"),
    [
        (
            True,
            [],
            f"the settings file {SETTINGS_FILE} says press = true, so this crawl"
            " presses and needs --out DIR, the new folder to record the run into;"
            " add --dry-run to preview instead",
        ),
        (False, ["--press"], PRESS_NEEDS_OUT),
    ],
)
def test_without_out_or_runs_dir_the_run_asks_for_out(
    fake_adb, android, capsys, here, connections, press, switch, message
):
    write_settings(here, settings_text(press=press))
    code, text, err = crawl(capsys, fake_adb, *switch)
    assert (code, text) == (2, "")
    assert err == f"uiprobe: error: {message}\n"
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


@pytest.mark.usefixtures("here")
def test_press_without_out_and_without_a_file_gets_the_file_guidance(
    fake_adb, android, capsys
):
    code, _text, err = crawl(capsys, fake_adb, "--press")
    assert code == 2
    assert "crawl needs a settings file and none was found" in err
    assert "add --config PATH to the same command" in err
    assert fake_adb.requests == []
    assert android.inputs == []


# --- the parent folder ----------------------------------------------------------


@pytest.mark.parametrize("as_json", [False, True])
def test_a_missing_runs_dir_says_what_to_do(
    fake_adb, android, capsys, here, connections, as_json
):
    write_settings(here, with_runs_dir("runs"))
    code, text, err = crawl(capsys, fake_adb, *(["--json"] if as_json else []))
    assert code == 1
    message = (
        f"the folder for run folders runs (runs_dir in {SETTINGS_FILE}) does not"
        " exist; create that folder, or add --out DIR"
    )
    if as_json:
        error = error_of(text)
        assert (error["code"], error["message"]) == ("run_folder_error", message)
        assert error["details"] == {"path": "runs"}
    else:
        assert err == f"uiprobe: error: {message}\n"
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []
    assert sorted(p.name for p in here.iterdir()) == [SETTINGS_FILE]


@pytest.mark.usefixtures("android")
def test_a_runs_dir_that_is_a_file_is_refused(fake_adb, capsys, here):
    (here / "runs").write_text("x", encoding="utf-8")
    write_settings(here, with_runs_dir("runs"))
    code, _text, err = crawl(capsys, fake_adb)
    assert code == 1
    assert "runs (runs_dir in uiprobe.toml) is not a folder" in err
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_a_runs_dir_that_is_a_link_is_refused(fake_adb, capsys, here):
    (here / "real").mkdir()
    try:
        (here / "runs").symlink_to(here / "real", target_is_directory=True)
    except OSError:
        pytest.skip("this system cannot make a link here")
    write_settings(here, with_runs_dir("runs"))
    code, _text, err = crawl(capsys, fake_adb)
    assert code == 1
    assert "runs (runs_dir in uiprobe.toml) is a link" in err
    assert fake_adb.requests == []
    assert list((here / "real").iterdir()) == []


@pytest.mark.usefixtures("android")
def test_a_runs_dir_that_is_a_junction_is_refused(fake_adb, capsys, here, monkeypatch):
    (here / "runs").mkdir()
    write_settings(here, with_runs_dir("runs"))
    original = os.path.isjunction

    def junction_at_runs(path: str) -> bool:
        return path == "runs" or original(path)

    monkeypatch.setattr(os.path, "isjunction", junction_at_runs)
    code, _text, err = crawl(capsys, fake_adb)
    assert code == 1
    assert err == (
        f"uiprobe: error: the folder for run folders runs (runs_dir in"
        f" {SETTINGS_FILE}) is a link or junction; give the folder it points to"
        " in runs_dir, or add --out DIR\n"
    )
    assert fake_adb.requests == []
    assert list((here / "runs").iterdir()) == []


@pytest.mark.usefixtures("android")
def test_another_reparse_point_is_accepted(fake_adb, capsys, here, monkeypatch):
    # A folder with the reparse point attribute that is neither a link nor a
    # junction (a cloud-file placeholder, say) is an ordinary folder here.
    runs = here / "runs"
    runs.mkdir()
    write_settings(here, with_runs_dir("runs"))
    original = os.lstat

    class Placeholder:
        st_mode = stat.S_IFDIR | 0o700
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        st_reparse_tag = 0x9000001A  # a cloud-file tag, not a mount point

    def placeholder_at_runs(path: str, *args: object, **kwargs: object) -> object:
        if path == "runs":
            return Placeholder()
        return original(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(os, "lstat", placeholder_at_runs)
    code, _text, _err = crawl(capsys, fake_adb, *FAST, "--actions", "2")
    assert code == 0
    assert (runs / NAME / "run.json").exists()


@pytest.mark.usefixtures("android")
def test_a_mount_point_in_the_first_lstat_is_refused(
    fake_adb, capsys, here, monkeypatch
):
    # One read decides: a junction seen by lstat is refused even when
    # isjunction, asked afterwards, says no (the folder was swapped back).
    (here / "runs").mkdir()
    write_settings(here, with_runs_dir("runs"))
    original = os.lstat

    class Junction:
        st_mode = stat.S_IFDIR | 0o700
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
        st_reparse_tag = MOUNT_POINT

    def junction_at_runs(path: str, *args: object, **kwargs: object) -> object:
        if path == "runs":
            return Junction()
        return original(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(os, "lstat", junction_at_runs)
    monkeypatch.setattr(os.path, "isjunction", lambda _path: False)
    # The constant exists only on Windows; give it its Windows value here.
    monkeypatch.setattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", MOUNT_POINT, raising=False)
    code, _text, err = crawl(capsys, fake_adb)
    assert code == 1
    assert "is a link or junction" in err
    assert fake_adb.requests == []


@pytest.mark.parametrize("from_file", [False, True])
@pytest.mark.parametrize("parent_kind", ["missing", "file"])
def test_an_out_without_a_parent_folder_is_refused_before_the_device(
    fake_adb, android, capsys, here, connections, from_file, parent_kind
):
    write_settings(here, settings_text(press=from_file))
    if parent_kind == "file":
        (here / "parent").write_text("x", encoding="utf-8")
    out = here / "parent" / "child"
    switch = [] if from_file else ["--press"]
    code, text, _err = crawl(capsys, fake_adb, *switch, "--out", str(out), "--json")
    assert code == 1
    error = error_of(text)
    assert error["code"] == "run_folder_error"
    assert str(here / "parent") in error["message"]
    assert "out" not in error["details"]
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []
    assert not out.exists()


@pytest.mark.parametrize("from_file", [False, True])
def test_an_existing_out_is_refused_before_the_device(
    fake_adb, android, capsys, here, connections, from_file
):
    write_settings(here, settings_text(press=from_file))
    taken = here / "taken"
    taken.mkdir()
    switch = [] if from_file else ["--press"]
    code, text, _err = crawl(capsys, fake_adb, *switch, "--out", str(taken), "--json")
    assert code == 1
    error = error_of(text)
    assert error["code"] == "run_folder_error"
    assert "already exists" in str(error["message"])
    assert "out" not in error["details"]
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_a_drive_relative_run_folder_is_refused_before_the_device(
    fake_adb, capsys, here, connections, monkeypatch
):
    # On Windows, --config D:rules.toml with a relative runs_dir names a folder
    # relative to drive D's own working folder; the early check refuses it as
    # the run would. Faked here, where such paths do not exist.
    (here / "runs").mkdir()
    write_settings(here, with_runs_dir("runs"))
    monkeypatch.setattr(
        runlog, "depends_on_a_drive_folder", lambda path: path.startswith("runs")
    )
    code, _text, err = crawl(capsys, fake_adb)
    assert code == 1
    assert "on Windows give a full path with its drive" in err
    assert fake_adb.requests == []
    assert connections == []
    assert list((here / "runs").iterdir()) == []


@pytest.mark.usefixtures("android")
def test_a_link_above_the_last_part_is_followed(fake_adb, capsys, here):
    (here / "real" / "runs").mkdir(parents=True)
    try:
        (here / "via").symlink_to(here / "real", target_is_directory=True)
    except OSError:
        pytest.skip("this system cannot make a link here")
    write_settings(here, with_runs_dir("via/runs"))
    code, _text, _err = crawl(capsys, fake_adb, *FAST, "--actions", "2")
    assert code == 0
    assert (here / "real" / "runs" / NAME / "run.json").exists()


# --- the run folder's name -------------------------------------------------------


@pytest.mark.usefixtures("android")
def test_a_taken_name_gets_the_next_number(fake_adb, capsys, here):
    runs = here / "runs"
    runs.mkdir()
    (runs / NAME).write_text("not a folder", encoding="utf-8")
    try:
        (runs / f"{NAME}-2").symlink_to(here / "missing")  # a broken link
    except OSError:
        (runs / f"{NAME}-2").mkdir()
    write_settings(here, with_runs_dir("runs"))
    code, text, _err = crawl(capsys, fake_adb, *FAST, "--json")
    assert code == 0
    assert Path(json.loads(text)["out"]) == runs / f"{NAME}-3"
    assert (runs / f"{NAME}-3" / "run.json").exists()
    assert (runs / NAME).read_text(encoding="utf-8") == "not a folder"


def test_every_name_taken_is_an_error(fake_adb, android, capsys, here):
    runs = here / "runs"
    runs.mkdir()
    (runs / NAME).mkdir()
    for number in range(2, 100):
        (runs / f"{NAME}-{number}").mkdir()
    write_settings(here, with_runs_dir("runs"))
    code, text, _err = crawl(capsys, fake_adb, "--json")
    assert code == 1
    error = error_of(text)
    assert error["code"] == "run_folder_error"
    assert f"{NAME} and {NAME}-2 to {NAME}-99 all exist" in str(error["message"])
    assert fake_adb.requests == []
    assert android.inputs == []
    assert len(list(runs.iterdir())) == 99


def test_a_folder_made_in_a_race_ends_the_run_unshared(
    fake_adb, android, capsys, here, monkeypatch
):
    runs = here / "runs"
    runs.mkdir()
    write_settings(here, with_runs_dir("runs"))
    original = cli.crawl_run

    def someone_else_first(device: object, package: str, out: str, **kwargs: object):
        Path(out).mkdir()  # another program takes the chosen name
        (Path(out) / "theirs.txt").write_text("theirs", encoding="utf-8")
        return original(device, package, out, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(cli, "crawl_run", someone_else_first)
    code, text, _err = crawl(capsys, fake_adb, *FAST, "--json")
    assert code == 1
    error = error_of(text)
    assert error["code"] == "run_folder_error"
    assert "already exists" in str(error["message"])
    assert "out" not in error["details"]  # the folder is not this run's
    assert device_writes(fake_adb) == []
    assert android.inputs == []
    assert sorted(p.name for p in (runs / NAME).iterdir()) == ["theirs.txt"]


def test_the_name_uses_local_time_without_a_zone():
    assert re.fullmatch(r"[0-9]{8}-[0-9]{6}", cli._run_stamp())


# --- the device ------------------------------------------------------------------


def test_a_real_device_still_needs_the_option(fake_adb, android, capsys, here):
    android.props["ro.kernel.qemu"] = "0"
    (here / "runs").mkdir()
    write_settings(here, with_runs_dir("runs"))
    code, text, err = crawl(capsys, fake_adb)
    assert (code, text) == (17, "")
    assert "--allow-real-device" in err
    # The same reads as without runs_dir: the pin's identity, then the check.
    assert [r for r in fake_adb.requests if r.startswith("shell:")] == [
        f"shell:{IDENTITY_COMMAND}",
        "shell:getprop ro.kernel.qemu",
    ]
    assert list((here / "runs").iterdir()) == []
    assert android.inputs == []
    code, _text, _err = crawl(capsys, fake_adb, "--allow-real-device", *FAST)
    assert code == 0
    assert (here / "runs" / NAME / "run.json").exists()


# --- the switch lines after a dry run ----------------------------------------------

ALWAYS = (
    f"to always press: set press = true in {SETTINGS_FILE}, and runs_dir (the"
    " folder that gets the run folders) or --out DIR on each run"
)


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize(
    ("runs_dir", "dry_run", "line"),
    [
        (True, False, "to press this time: add --press to the same command"),
        (
            True,
            True,
            "to press this time: remove --dry-run and add --press to the same command",
        ),
        (False, False, "to press this time: add --press --out DIR to the same command"),
        (
            False,
            True,
            "to press this time: remove --dry-run and add --press --out DIR to the"
            " same command",
        ),
    ],
)
def test_the_switch_line_follows_runs_dir(
    fake_adb, capsys, here, runs_dir, dry_run, line
):
    text = with_runs_dir("runs", press=False) if runs_dir else settings_text()
    write_settings(here, text)
    code, out, _err = crawl(capsys, fake_adb, *(["--dry-run"] * dry_run))
    assert code == 0
    assert out.splitlines()[-2:] == [line, ALWAYS]


# --- the settings key -----------------------------------------------------------


def test_init_writes_runs_dir_only_as_a_comment(here):
    text = render_default_settings()
    assert '\n# runs_dir = "uiprobe-runs"\n' in text
    assert "\nruns_dir" not in text
    path = write_settings(here, text)
    assert load_settings(path).runs_dir is None


def test_runs_dir_is_read(here):
    path = write_settings(here, with_runs_dir("runs/x"))
    assert load_settings(path).runs_dir == "runs/x"


@pytest.mark.parametrize(
    ("value", "read"),
    [
        ("runs/", "runs"),
        ("runs//", "runs"),
        ("/", "/"),
        ("//", "//"),
        ("///", "///"),
        (".", "."),
        ("runs/..", "runs/.."),
    ],
)
def test_trailing_separators_of_runs_dir_are_dropped(here, value, read):
    path = write_settings(here, with_runs_dir(value))
    assert load_settings(path).runs_dir == read


POSIX_ENDINGS = [
    ("/", "/"),
    ("//", "//"),
    ("///", "///"),
    ("/runs/", "/runs"),
    ("runs//", "runs"),
    ("runs\\", "runs\\"),  # a backslash is no separator here
]
WINDOWS_ENDINGS = [
    ("C:\\", "C:\\"),
    ("C:\\\\", "C:\\"),
    ("C:\\runs\\", "C:\\runs"),
    ("C:runs\\", "C:runs"),
    ("\\\\", "\\\\"),
    ("//", "//"),
    ("/\\/", "/\\/"),
    ("runs/\\", "runs"),
    ("\\\\server\\share\\", "\\\\server\\share\\"),
    ("\\\\server\\share\\runs\\", "\\\\server\\share\\runs"),
]


@pytest.mark.parametrize(("given", "kept"), POSIX_ENDINGS)
def test_trailing_separators_by_posix_rules(given, kept):
    strip = settings_module._without_trailing_separators
    assert strip(given, windows=False) == kept


@pytest.mark.parametrize(("given", "kept"), WINDOWS_ENDINGS)
def test_trailing_separators_by_windows_rules(given, kept):
    strip = settings_module._without_trailing_separators
    assert strip(given, windows=True, splitdrive=ntpath.splitdrive) == kept


def test_the_ending_is_scanned_once():
    long = "a" + "/." * 200_000 + "/" * 200_000
    assert settings_module._folder_form_base(long) == "a"
    assert settings_module._without_trailing_separators("b" + "/" * 400_000) == "b"
    assert settings_module._without_trailing_separators("/" * 400_000) == "/" * 400_000


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("5", "runs_dir: must be a string (a folder path)"),
        ("true", "runs_dir: must be a string (a folder path)"),
        ('["runs"]', "runs_dir: must be a string (a folder path)"),
        ('""', "runs_dir: must not be empty"),
        ('"runs\\u0000x"', "runs_dir: must not hold a null character"),
        ('"runs/."', "runs_dir: must not end with \"/.\"; write 'runs'"),
        ('"runs/./"', "runs_dir: must not end with \"/.\"; write 'runs'"),
        ('"/."', 'runs_dir: must not end with "/."'),
    ],
)
def test_invalid_runs_dir_values_are_refused(here, value, message):
    text = render_default_settings().replace(
        '# runs_dir = "uiprobe-runs"\n', f"runs_dir = {value}\n"
    )
    path = write_settings(here, text)
    with pytest.raises(SettingsError) as caught:
        load_settings(path)
    assert str(caught.value) == f"{path}: {message}"


# --- naming the run folder only when it is this run's ----------------------------


@pytest.mark.usefixtures("android")
def test_a_folder_made_by_another_program_during_an_interrupt_is_not_named(
    fake_adb, capsys, here, monkeypatch
):
    write_settings(here, settings_text(press=True))
    out = here / "run"

    def interrupted_before_the_folder(*_args: object, **_kwargs: object) -> object:
        out.mkdir()  # another program, while the run had not made its own
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "crawl_run", interrupted_before_the_folder)
    code, text, _err = crawl(capsys, fake_adb, "--out", str(out), "--json")
    assert code == 130
    error = error_of(text)
    assert error["message"] == "interrupted"
    assert "out" not in error.get("details", {})


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("wrote_start", [False, True])
def test_a_failed_run_names_only_a_folder_it_wrote_into(
    fake_adb, capsys, here, monkeypatch, wrote_start
):
    write_settings(here, settings_text(press=True))
    out = here / "run"

    def fails_after(*_args: object, **_kwargs: object) -> object:
        out.mkdir()
        if wrote_start:  # as the run does right after making its folder
            (out / "run-start.json").write_text("{}", encoding="utf-8")
        raise AdbError("the device went away")

    monkeypatch.setattr(cli, "crawl_run", fails_after)
    code, text, _err = crawl(capsys, fake_adb, "--out", str(out), "--json")
    assert code != 0
    details = error_of(text).get("details", {})
    assert ("out" in details) is wrote_start
    if wrote_start:
        assert details["out"] == str(out)


# --- the package name ---------------------------------------------------------------


def test_a_str_subclass_cannot_change_the_package(fake_adb, android, here):
    (here / "runs").mkdir()
    write_settings(here, with_runs_dir("runs"))

    class Other(str):
        __slots__ = ()

        def __str__(self) -> str:
            return "com.other.app"

    args = cli._parse(["--adb-server", fake_adb.address, "crawl", SHOP])
    args.package = Other(SHOP)
    setup = cli._crawl_policy(args)
    assert type(args.package) is str
    assert args.package == SHOP
    assert setup.out is not None
    assert Path(setup.out).name == NAME
    assert fake_adb.requests == []
    assert android.inputs == []
