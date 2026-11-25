"""Tests for the dry run's options, usage errors and the press refusals."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import pytest

from uiprobe import cli
from uiprobe.settings import (
    SETTINGS_FILE,
    _shown,
    render_default_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_dry.helpers import (
    DRY_RUN_LAST,
    SERIAL,
    SHOP,
    assert_read_only,
    crawl,
    crawl_json,
    invoke,
    pop_ending,
    skipped_by_text,
)

# --- CLI: options ---


def test_deny_adds_a_word(fake_adb, android, capsys):
    payload = crawl_json(capsys, fake_adb, "--deny", "browse")
    assert [e["text"] for e in payload["press"]] == ["Help"]
    entry = skipped_by_text(payload)["Browse"]
    assert entry["reasons"] == ["deny_word"]
    assert entry["details"][0]["word"] == "browse"
    assert_read_only(fake_adb, android)


@pytest.mark.usefixtures("android")
def test_deny_is_repeatable(fake_adb, capsys):
    payload = crawl_json(capsys, fake_adb, "--deny", "BROWSE", "--deny", "help")
    assert payload["press"] == []


def test_tap_unlabeled_makes_an_unlabeled_element_pressable(fake_adb, android, capsys):
    payload = crawl_json(capsys, fake_adb, "--tap-unlabeled")
    assert [e["id"] for e in payload["press"]] == ["e2", "e6", "e9"]
    assert "unlabeled" not in [r for e in payload["skip"] for r in e["reasons"]]
    assert_read_only(fake_adb, android)


@pytest.mark.usefixtures("android")
def test_options_work_before_the_command(fake_adb, capsys):
    code, out, err = invoke(
        capsys, fake_adb, "-s", SERIAL, "--json", "crawl", SHOP, "--dry-run"
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["schema"] == "uiprobe.crawl_preview/1"


def test_serial_after_the_command(fake_adb, android, capsys):
    code, out, _ = crawl(capsys, fake_adb, "--serial", SERIAL)
    assert code == 0
    assert "would press (2):" in out
    assert_read_only(fake_adb, android)


# --- CLI: usage errors, all before the adb server is contacted ---


def usage_error(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> str:
    before = len(fake_adb.requests)
    code, out, err = invoke(capsys, fake_adb, *argv)
    assert (code, err) == (2, "")
    assert len(fake_adb.requests) == before
    error = json.loads(out)
    assert error["schema"] == "uiprobe.error/1"
    assert error["error"]["code"] == "usage_error"
    return error["error"]["message"]


def crawl_usage_error(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> str:
    return usage_error(capsys, fake_adb, "crawl", "--json", *argv)


PRESS_MESSAGE = "--press needs --out DIR, the new folder to record the run into"


def file_press_message(path: str) -> str:
    return (
        f"the settings file {path} says press = true, so this crawl presses and"
        " needs --out DIR, the new folder to record the run into; add --dry-run"
        " to preview instead"
    )


DEFAULT_LAST = "nothing was pressed: this was a preview (the default)"


def set_press(folder: Path) -> None:
    path = folder / SETTINGS_FILE
    text = path.read_text(encoding="utf-8")
    assert text.count("press = false") == 1
    path.write_text(text.replace("press = false", "press = true"), encoding="utf-8")


def test_crawl_without_a_switch_previews(fake_adb, android, capsys):
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP)
    assert (code, err) == (0, "")
    code2, out_dry, err2 = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run")
    assert (code2, err2) == (0, "")
    lines, lines_dry = out.splitlines(), out_dry.splitlines()
    pop_ending(lines, DEFAULT_LAST)
    pop_ending(lines_dry, DRY_RUN_LAST)
    assert lines == lines_dry
    assert_read_only(fake_adb, android)


@pytest.mark.usefixtures("android")
def test_preview_json_says_not_pressed(fake_adb, capsys):
    for argv in (("crawl", SHOP, "--json"), ("crawl", SHOP, "--dry-run", "--json")):
        code, out, err = invoke(capsys, fake_adb, *argv)
        assert (code, err) == (0, "")
        payload = json.loads(out)
        assert payload["pressed"] is False
        assert payload["mode"] == "preview"


@pytest.mark.parametrize("as_json", [False, True])
def test_press_is_refused_before_any_contact(
    fake_adb, android, capsys, connections, as_json
):
    argv = ["crawl", SHOP, "--press", *(["--json"] if as_json else [])]
    code, out, err = invoke(capsys, fake_adb, *argv)
    assert code == 2
    if as_json:
        assert err == ""
        error = json.loads(out)["error"]
        assert (error["code"], error["message"]) == ("usage_error", PRESS_MESSAGE)
    else:
        assert out == ""
        assert err == f"uiprobe: error: {PRESS_MESSAGE}\n"
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


def test_press_in_the_file_without_out_is_refused(
    fake_adb, android, capsys, settings_dir, connections
):
    set_press(settings_dir)
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP)
    assert (code, out) == (2, "")
    assert err == f"uiprobe: error: {file_press_message(SETTINGS_FILE)}\n"
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


def test_press_in_the_file_with_press_names_the_file_and_dry_run(
    fake_adb, android, capsys, settings_dir, connections
):
    set_press(settings_dir)
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--press")
    assert (code, out) == (2, "")
    assert err == f"uiprobe: error: {PRESS_MESSAGE}\n"  # before the file is read
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []
    # Each suggested retry: replacing --press with --dry-run previews; removing
    # only --press is still refused because the file presses and needs --out.
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP)
    assert (code, out) == (2, "")
    assert err == f"uiprobe: error: {file_press_message(SETTINGS_FILE)}\n"
    assert fake_adb.requests == []
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run")
    assert (code, err) == (0, "")
    assert out.rstrip("\n").endswith(DRY_RUN_LAST)
    assert_read_only(fake_adb, android)
    # Removing --press and setting press = false in the file previews too.
    (settings_dir / SETTINGS_FILE).write_text(
        render_default_settings(), encoding="utf-8"
    )
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP)
    assert (code, err) == (0, "")
    pop_ending(out.splitlines(), DEFAULT_LAST)


@pytest.mark.parametrize("name", ["odd\u202e.toml", "two\nlines.toml"])
@pytest.mark.parametrize("switch", [[], ["--press"]])
def test_press_refusals_show_an_unambiguous_path(
    fake_adb, android, capsys, tmp_path, connections, name, switch
):
    other = tmp_path / name
    text = render_default_settings()
    try:
        other.write_text(
            text.replace("press = false", "press = true"), encoding="utf-8"
        )
    except OSError:
        pytest.skip("this file system cannot hold that file name")
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, *switch, "--config", str(other)
    )
    assert (code, out) == (2, "")
    assert "\u202e" not in err
    assert err.count("\n") == 1
    shown = _shown(str(other))
    expected = PRESS_MESSAGE if switch else file_press_message(shown)
    assert err == f"uiprobe: error: {expected}\n"
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


def test_missing_file_guidance_keeps_press(fake_adb, android, capsys, tmp_path):
    here = tmp_path / "empty"
    here.mkdir()
    os.chdir(here)
    message = crawl_usage_error(
        capsys, fake_adb, SHOP, "--press", "--out", str(tmp_path / "run")
    )
    assert message.splitlines()[-1] == (
        "or name one with:  add --config PATH to the same command"
    )
    plain = crawl_usage_error(capsys, fake_adb, SHOP)
    assert (
        plain.splitlines()[-1]
        == f"or name one with:  uiprobe crawl {SHOP} --config PATH"
    )
    assert android.inputs == []


def test_press_in_another_file_names_that_file(
    fake_adb, android, capsys, tmp_path, connections
):
    other = tmp_path / "other.toml"
    text = render_default_settings()
    assert text.count("press = false") == 1
    other.write_text(text.replace("press = false", "press = true"), encoding="utf-8")
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--config", str(other))
    assert (code, out) == (2, "")
    assert err == f"uiprobe: error: {file_press_message(_shown(str(other)))}\n"
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


def test_press_in_the_file_allows_dry_run(fake_adb, android, capsys, settings_dir):
    set_press(settings_dir)
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run")
    assert (code, err) == (0, "")
    # --dry-run is what keeps this run from pressing: no switch lines follow.
    assert out.splitlines()[-1] == DRY_RUN_LAST
    assert "to press this time" not in out
    assert "to always press" not in out
    assert "--allow-real-device" not in out
    assert_read_only(fake_adb, android)


@pytest.mark.parametrize("switch", [[], ["--dry-run"], ["--press", "--out", "run-x"]])
@pytest.mark.usefixtures("android")
def test_press_is_refused_after_the_file_is_checked(
    fake_adb, capsys, tmp_path, connections, switch
):
    bad = tmp_path / "bad.toml"
    text = render_default_settings()
    assert text.count("press = false") == 1
    bad.write_text(text.replace("press = false", "press = 3"), encoding="utf-8")
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, *switch, "--config", str(bad)
    )
    assert (code, out) == (2, "")
    assert err == f"uiprobe: error: {bad}: press: must be true or false\n"
    assert fake_adb.requests == []
    assert connections == []


def test_press_in_the_file_without_out_is_refused_in_json(
    fake_adb, android, capsys, settings_dir, connections
):
    set_press(settings_dir)
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--json")
    assert (code, err) == (2, "")
    error = json.loads(out)["error"]
    assert (error["code"], error["message"]) == (
        "usage_error",
        file_press_message(SETTINGS_FILE),
    )
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


def test_press_with_dry_run_is_refused(fake_adb, android, capsys, connections):
    message = crawl_usage_error(capsys, fake_adb, SHOP, "--press", "--dry-run")
    assert message == "--press and --dry-run cannot be combined"
    assert connections == []
    assert android.inputs == []


@pytest.mark.parametrize("equals_form", [False, True])
def test_crawl_refuses_an_out_folder(fake_adb, android, capsys, tmp_path, equals_form):
    run = tmp_path / "run"
    out_args = [f"--out={run}"] if equals_form else ["--out", str(run)]
    crawl_usage_error(capsys, fake_adb, SHOP, "--dry-run", *out_args)
    assert not run.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["work"]
    assert list((tmp_path / "work").iterdir()) == [tmp_path / "work" / "uiprobe.toml"]
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_crawl_needs_a_package(fake_adb, capsys):
    usage_error(capsys, fake_adb, "crawl", "--dry-run", "--json")


@pytest.mark.parametrize(
    "package", ["com", "1com.x", "com..x", "com.x.", "com.ex-ample", "com." + "a" * 252]
)
@pytest.mark.usefixtures("android")
def test_crawl_bad_package(fake_adb, capsys, package):
    message = crawl_usage_error(capsys, fake_adb, package, "--dry-run")
    assert "package" in message


@pytest.mark.usefixtures("android")
def test_crawl_empty_package(fake_adb, capsys):
    message = crawl_usage_error(capsys, fake_adb, "", "--dry-run")
    assert "package" in message


@pytest.mark.parametrize("word", ["", "   ", "\u200b"])
@pytest.mark.usefixtures("android")
def test_crawl_empty_deny_word(fake_adb, capsys, word):
    message = crawl_usage_error(capsys, fake_adb, SHOP, "--dry-run", "--deny", word)
    assert message == f"--deny word is empty after normalization: {word!r}"


@pytest.mark.usefixtures("android")
def test_crawl_usage_error_text_is_escaped(fake_adb, capsys, tmp_path):
    path = tmp_path / "gone\u202e.toml"
    code, out, err = invoke(
        capsys, fake_adb, "crawl", SHOP, "--dry-run", "--config", str(path)
    )
    assert (code, out) == (2, "")
    assert "\u202e" not in err
    assert "gone\\u202e.toml" in err
    assert fake_adb.requests == []


def test_usage_errors_do_not_depend_on_the_adb_server(capsys):
    code = cli.run(["--adb-server", "tcp:127.0.0.1:1", "crawl", "com.x.y", "--press"])
    assert code == 2
    assert capsys.readouterr().out == ""
