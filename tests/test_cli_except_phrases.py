"""``uiprobe crawl --press`` with ``[deny] except_phrases``: the plan line, the
run folder and the empty list that changes nothing."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import SERIAL, SHOP, TRANSPORT, button
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import cli
from uiprobe.settings import SETTINGS_FILE, render_default_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

FAST = ("--settle", "0")
SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((0, 0, 320, 100), text="Shop"),
            button(200, "Quick Share", "done"),
            button(260, "Share", "done"),
        ),
    ),
    Screen("done", f"{SHOP}/.Done", (El((0, 0, 320, 100), text="Done"),)),
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def settings_with(here: Path, listed: str | None) -> None:
    text = render_default_settings()
    if listed is not None:
        text = text.replace(
            "remove = []\n", f"except_phrases = {listed}\nremove = []\n", 1
        )
    (here / SETTINGS_FILE).write_text(text, encoding="utf-8", newline="\n")


def crawl(capsys, fake_adb, *argv: str) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_plan_names_the_phrases_and_the_press_is_marked(
    fake_adb, android, capsys, here
):
    settings_with(here, '["Quick Share"]')
    out = here / "run"
    code, text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--quiet", "--out", str(out)
    )
    assert (code, err) == (0, "")
    assert (
        "letting rows named exactly 'Quick Share' through for the deny words inside"
        " the name (except_phrases in the settings file; any other deny word still"
        " refuses the row)"
    ) in text.splitlines()
    assert [i.point for i in android.inputs if i.kind == "tap" and i.screen == "main"]
    start = json.loads((out / "run-start.json").read_text("utf-8"))
    assert start["deny_except_phrases"] == ["Quick Share"]
    marks = [
        json.loads(line).get("except_phrase")
        for line in (out / "transitions.jsonl").read_text("utf-8").splitlines()
    ]
    assert marks.count("Quick Share") == 1


@pytest.mark.usefixtures("android")
def test_the_json_plan_has_the_phrases_only_when_there_are_some(fake_adb, capsys, here):
    settings_with(here, '["Quick Share"]')
    code, text, _ = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "a"), "--json"
    )
    assert code == 0
    assert "letting rows named" not in text
    settings_with(here, "[]")
    code, text, _ = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "b"), "--json"
    )
    assert code == 0
    assert "deny_except_phrases" not in text
    assert "except_phrase" not in (here / "b" / "transitions.jsonl").read_text("utf-8")
    start = json.loads((here / "b" / "run-start.json").read_text("utf-8"))
    assert "deny_except_phrases" not in start


@pytest.mark.usefixtures("android")
def test_an_empty_list_prints_no_plan_line(fake_adb, capsys, here):
    settings_with(here, "[]")
    code, text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--quiet", "--out", str(here / "r")
    )
    assert (code, err) == (0, "")
    assert "letting rows named" not in text
