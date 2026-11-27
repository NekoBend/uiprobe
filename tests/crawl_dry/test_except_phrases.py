"""The dry run with ``[deny] except_phrases`` in the settings file."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.crawl_dry.helpers import SERIAL, SHOP, crawl, crawl_json, pop_ending
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe.settings import SETTINGS_FILE

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("android")

SCREEN = Screen(
    "main",
    f"{SHOP}/.Main",
    (
        El((40, 100, 280, 150), text="Quick Share", go="main"),
        El((40, 160, 280, 210), text="Share", go="main"),
        El((40, 220, 280, 270), text="Browse", go="main"),
    ),
)


@pytest.fixture
def android(fake_adb) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, (SCREEN,), start="main")


def with_phrases(folder: Path, items: str) -> None:
    path = folder / SETTINGS_FILE
    text = path.read_text(encoding="utf-8")
    assert text.count("remove = []\n") >= 1
    path.write_text(
        text.replace("remove = []\n", f"except_phrases = {items}\nremove = []\n", 1),
        encoding="utf-8",
    )


def test_the_preview_lists_the_row_as_pressable_with_a_note(
    fake_adb, capsys, settings_dir
):
    with_phrases(settings_dir, '["Quick Share"]')
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    pop_ending(lines)
    assert lines[3] == "would press (2):"
    assert lines[4:6] == [
        '  e2 TextView "Quick Share" [tap] center=160,125'
        " (let through by except_phrases: 'Quick Share')",
        '  e4 TextView "Browse" [tap] center=160,245',
    ]
    assert lines[6] == "would not press (1):"
    assert lines[7].startswith('  e3 TextView "Share"')
    assert 'deny_word ("share" in text)' in lines[7]


def test_the_json_preview_carries_the_phrase_only_on_that_row(
    fake_adb, capsys, settings_dir
):
    with_phrases(settings_dir, '["Quick Share"]')
    payload = crawl_json(capsys, fake_adb)
    marked = {e["text"]: e.get("except_phrase") for e in payload["press"]}
    assert marked == {"Quick Share": "Quick Share", "Browse": None}
    assert [e["text"] for e in payload["skip"]] == ["Share"]


@pytest.mark.parametrize("items", ["[]", None])
def test_an_empty_or_missing_list_previews_as_before(
    fake_adb, capsys, settings_dir, items
):
    if items is not None:
        with_phrases(settings_dir, items)
    code, out, _ = crawl(capsys, fake_adb)
    assert code == 0
    assert "let through" not in out
    lines = out.splitlines()
    pop_ending(lines)
    assert lines[3] == "would press (1):"
    assert lines[4].startswith('  e4 TextView "Browse"')
    payload = crawl_json(capsys, fake_adb)
    assert all("except_phrase" not in e for e in payload["press"])
