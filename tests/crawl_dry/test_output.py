"""Tests for the dry run's text and JSON output, escaping, errors and interrupts."""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import Fail
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import Device, Policy, cli, preview
from uiprobe.guards import REASONS, judge
from uiprobe.settings import (
    SETTINGS_FILE,
)

if TYPE_CHECKING:
    from uiprobe.model import Screen as ModelScreen

from tests.crawl_dry.helpers import (
    ACTIVITIES,
    OTHER,
    OTHER_APP,
    SERIAL,
    SHOP,
    WINDOW,
    assert_read_only,
    crawl,
    crawl_json,
    device_of,
    invoke,
    pop_ending,
    skipped_by_text,
)


def spy_on_dump(monkeypatch: pytest.MonkeyPatch) -> list[ModelScreen]:
    """Record every Screen that Device.dump returns."""
    seen: list[ModelScreen] = []
    original = Device.dump

    def spy(self, *args, **kwargs):
        screen = original(self, *args, **kwargs)
        seen.append(screen)
        return screen

    monkeypatch.setattr(Device, "dump", spy)
    return seen


# --- CLI: text ---


@pytest.mark.usefixtures("android")
def test_text_output(fake_adb, capsys):
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    pop_ending(lines)
    assert re.fullmatch(
        rf"preview for {re.escape(SHOP)} on screen s-[0-9a-f]+; nothing was pressed",
        lines[0],
    )
    assert lines[1] == f"settings: {SETTINGS_FILE}"
    assert lines[2] == f"foreground: {SHOP}, keyboard: hidden"
    assert lines[3] == "would press (2):"
    assert lines[4:6] == [
        '  e2 TextView "Browse" id=browse [tap] center=160,125',
        '  e9 TextView "Help" [tap] center=160,425',
    ]
    assert lines[6] == "would not press (5):"
    skipped = lines[7:]
    assert skipped == [
        '  e3 TextView "Delete account" [tap] center=160,185:'
        ' deny_word ("delete" in text)',
        # e5 is on top of e4, so e4 is covered and has no center.
        '  e4 TextView "Open row" [tap]: covered, deny_word ("delete" in text of e5)',
        "  e6 TextView [tap] center=160,305: unlabeled",
        '  e7 TextView "Settings" [tap] [disabled] center=160,365: disabled',
        '  e8 TextView "Offscreen" [tap]: not_visible',
    ]


def test_text_output_lists_every_skipped_reason(fake_adb, android, capsys):
    code, out, _ = crawl(capsys, fake_adb)
    assert code == 0
    assert "deny_word" in out
    assert "unlabeled" in out
    assert "disabled" in out
    assert "not_visible" in out
    assert_read_only(fake_adb, android)


def test_text_output_with_empty_lists(fake_adb, android, capsys):
    android.show("empty")
    code, out, _ = crawl(capsys, fake_adb)
    assert code == 0
    lines = out.splitlines()
    pop_ending(lines)
    assert lines[3:] == ["would press (0):", "would not press (0):"]


def test_text_output_with_nothing_pressable(fake_adb, android, capsys):
    android.show("locked")
    code, out, _ = crawl(capsys, fake_adb)
    assert code == 0
    lines = out.splitlines()
    assert lines[3:5] == ["would press (0):", "would not press (1):"]
    assert 'deny_word ("delete" in text)' in lines[5]


def test_text_output_shows_the_keyboard(fake_adb, android, capsys):
    android.show("typing")
    _, out, _ = crawl(capsys, fake_adb)
    assert out.splitlines()[2] == f"foreground: {SHOP}, keyboard: shown"


def test_text_output_unknown_foreground_and_keyboard(fake_adb, android, capsys):
    android.override(WINDOW, b"nothing\n")
    android.override(ACTIVITIES, b"nothing\n")
    android.override(re.compile(r"^dumpsys input_method\Z"), b"nothing\n")
    code, out, _ = crawl(capsys, fake_adb)
    assert code == 0
    assert out.splitlines()[2] == "foreground: unknown, keyboard: unknown"
    assert "would press (0):" in out


def test_other_app_in_front_exits_zero_with_everything_skipped(
    fake_adb, android, capsys
):
    android.override(WINDOW, OTHER_APP.encode())
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    pop_ending(lines)
    assert lines[2] == f"foreground: {OTHER}, keyboard: hidden"
    assert lines[3:5] == ["would press (0):", "would not press (7):"]
    for line in lines[5:]:
        assert "outside_target_app" in line
    assert_read_only(fake_adb, android)


def test_other_app_in_front_json(fake_adb, android, capsys):
    android.override(WINDOW, OTHER_APP.encode())
    payload = crawl_json(capsys, fake_adb)
    assert payload["foreground"] == OTHER
    assert payload["press"] == []
    assert len(payload["skip"]) == 7
    for entry in payload["skip"]:
        assert "outside_target_app" in entry["reasons"]


# --- CLI: JSON ---


@pytest.mark.usefixtures("android")
def test_json_shape_and_values(fake_adb, capsys, monkeypatch):
    seen = spy_on_dump(monkeypatch)
    payload = crawl_json(capsys, fake_adb)
    (screen,) = seen
    assert payload["captured_at"] == screen.captured_at.isoformat(timespec="seconds")
    expected = judge(screen, SHOP, Policy(), foreground=SHOP)
    assert payload["press"] == [
        screen._element_dict(d.element) for d in expected.pressable
    ]
    assert [set(e) for e in payload["skip"]] == [
        set(screen._element_dict(d.element)) | {"reasons", "details"}
        for d in expected.skipped
    ]
    for entry, decision in zip(payload["skip"], expected.skipped, strict=True):
        base = screen._element_dict(decision.element)
        assert {
            k: v for k, v in entry.items() if k not in ("reasons", "details")
        } == base
        assert entry["reasons"] == list(decision.reasons)
    assert list(payload) == [
        "schema",
        "package",
        "screen_id",
        "captured_at",
        "settings",
        "pressed",
        "mode",
        "foreground",
        "keyboard_shown",
        "press",
        "skip",
    ]
    assert payload["settings"] == SETTINGS_FILE
    assert payload["schema"] == "uiprobe.crawl_preview/1"
    assert payload["package"] == SHOP
    assert payload["screen_id"].startswith("s-")
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", payload["captured_at"]
    )
    assert payload["foreground"] == SHOP
    assert payload["keyboard_shown"] is False
    assert [e["text"] for e in payload["press"]] == ["Browse", "Help"]
    press = payload["press"][0]
    assert press["bounds"] == [40, 100, 280, 150]
    assert press["center"] == [160, 125]
    assert list(press)[-1] == "center"


@pytest.mark.usefixtures("android")
def test_json_skip_entries(fake_adb, capsys):
    payload = crawl_json(capsys, fake_adb)
    skipped = skipped_by_text(payload)
    assert [e["id"] for e in payload["skip"]] == ["e3", "e4", "e6", "e7", "e8"]
    own = skipped["Delete account"]
    assert own["reasons"] == ["deny_word"]
    assert own["details"] == [
        {"reason": "deny_word", "word": "delete", "field": "text", "element_id": "e3"}
    ]
    assert own["center"] == [160, 185]
    other = skipped["Open row"]
    assert other["reasons"] == ["covered", "deny_word"]
    assert other["details"] == [
        {"reason": "covered", "note": "covered by e5", "element_id": "e5"},
        {"reason": "deny_word", "word": "delete", "field": "text", "element_id": "e5"},
    ]
    assert "center" not in other
    unlabeled = skipped[""]
    assert unlabeled["id"] == "e6"
    assert unlabeled["reasons"] == ["unlabeled"]
    disabled = skipped["Settings"]
    assert disabled["reasons"] == ["disabled"]
    assert disabled["center"] == [160, 365]
    invisible = skipped["Offscreen"]
    assert invisible["reasons"] == ["not_visible"]
    assert "center" not in invisible
    for entry in payload["skip"]:
        assert [r for r in REASONS if r in entry["reasons"]] == entry["reasons"]
        assert isinstance(entry["details"], list)
        for detail in entry["details"]:
            assert set(detail) <= {"reason", "word", "field", "element_id", "note"}
            assert all(detail.values())


@pytest.mark.usefixtures("android")
def test_preview_output_does_not_rescan_the_screen_per_element(
    fake_adb, capsys, monkeypatch
):
    # The centers come from the decisions; a per-element tap_point would make
    # the preview quadratic on a large screen.
    from uiprobe.model import Screen as RealScreen

    # captured_at comes from the clock, so two runs can differ by a second;
    # everything else must be the same.
    expected = crawl_json(capsys, fake_adb)
    expected.pop("captured_at")
    expected_text = crawl(capsys, fake_adb)

    def forbidden(*_args: object) -> None:
        raise AssertionError("tap_point called by the preview")

    monkeypatch.setattr(RealScreen, "tap_point", forbidden)
    actual = crawl_json(capsys, fake_adb)
    assert actual.pop("captured_at")
    assert actual == expected
    assert crawl(capsys, fake_adb) == expected_text


def test_json_empty_arrays_are_written(fake_adb, android, capsys):
    android.show("empty")
    payload = crawl_json(capsys, fake_adb)
    assert payload["press"] == []
    assert payload["skip"] == []


def test_json_omits_unknown_foreground_and_keyboard(fake_adb, android, capsys):
    android.override(WINDOW, b"nothing\n")
    android.override(ACTIVITIES, b"nothing\n")
    android.override(re.compile(r"^dumpsys input_method\Z"), b"nothing\n")
    payload = crawl_json(capsys, fake_adb)
    assert "foreground" not in payload
    assert "keyboard_shown" not in payload
    assert list(payload)[-2:] == ["press", "skip"]


def test_json_keyboard_true_is_written(fake_adb, android, capsys):
    android.show("typing")
    assert crawl_json(capsys, fake_adb)["keyboard_shown"] is True


@pytest.mark.usefixtures("android")
def test_json_matches_the_python_judgement(fake_adb, capsys):
    payload = crawl_json(capsys, fake_adb)
    result = preview(device_of(fake_adb), SHOP)
    assert [e["id"] for e in payload["press"]] == [
        d.element.id for d in result.judgement.pressable
    ]
    assert [e["id"] for e in payload["skip"]] == [
        d.element.id for d in result.judgement.skipped
    ]
    assert payload["screen_id"] == result.screen.screen_id


# --- CLI: escaping, errors, interrupt ---


def test_app_text_is_escaped_in_text_and_json(fake_adb, capsys):
    nasty = "a\x9b2J\u202eb\u2028c\x7f"
    FakeAndroid(
        fake_adb,
        SERIAL,
        (
            Screen(
                "nasty",
                f"{SHOP}/.Nasty",
                (
                    El((40, 100, 280, 150), text=nasty, go="nasty"),
                    El((40, 160, 280, 210), text="Delete" + nasty, go="nasty"),
                ),
            ),
        ),
        start="nasty",
    )
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    for char in "\x9b\u202e\u2028\x7f":
        assert char not in out
    assert "\\u009b" in out
    assert "\\u202e" in out
    code, out, err = crawl(capsys, fake_adb, "--json")
    assert (code, err) == (0, "")
    for char in "\x9b\u202e\u2028\x7f":
        assert char not in out
    assert "\\u009b" in out
    assert "\\u202e" in out
    payload = json.loads(out)
    assert payload["press"][0]["text"] == nasty


def test_escape_in_app_text_reaches_neither_output(
    fake_adb, android, capsys, monkeypatch
):
    # An XML dump cannot carry ESC, so hand the CLI a screen that has it.
    original = Device.dump

    def with_esc(self, *args, **kwargs):
        screen = original(self, *args, **kwargs)
        first = dataclasses.replace(screen.elements[1], text="x\x1b[2Jy")
        return dataclasses.replace(screen, elements=(screen.elements[0], first))

    monkeypatch.setattr(Device, "dump", with_esc)
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    assert "\x1b" not in out
    assert "\\u001b" in out
    code, out, err = crawl(capsys, fake_adb, "--json")
    assert (code, err) == (0, "")
    assert "\x1b" not in out
    assert json.loads(out)["press"][0]["text"] == "x\x1b[2Jy"
    assert android.inputs == []


def test_text_line_equals_the_find_line_for_ideographic_space(fake_adb, capsys):
    label = "\u30db\u30fc\u30e0\u3000\u3059\u308b"
    FakeAndroid(
        fake_adb,
        SERIAL,
        (Screen("jp", f"{SHOP}/.Jp", (El((40, 100, 280, 150), text=label, go="jp"),)),),
        start="jp",
    )
    code, find_out, err = invoke(capsys, fake_adb, "find", "--text", label)
    assert (code, err) == (0, "")
    line = next(x for x in find_out.splitlines() if label in x)
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    assert "  " + line.strip() in out.splitlines()


def test_escape_in_a_usage_error_message(fake_adb, capsys):
    code, out, err = invoke(
        capsys, fake_adb, "crawl", "com.x\x1b[2J\u202e", "--dry-run"
    )
    assert (code, out) == (2, "")
    assert "\x1b" not in err
    assert "\u202e" not in err
    assert fake_adb.requests == []
    code, out, err = invoke(
        capsys, fake_adb, "crawl", "com.x\x1b[2J\u202e", "--dry-run", "--json"
    )
    assert (code, err) == (2, "")
    assert "\x1b" not in out
    assert "\u202e" not in out
    assert "\\\\x1b" in out
    assert fake_adb.requests == []


def test_device_error_is_reported(fake_adb, android, capsys):
    android.override(re.compile(r"^dumpsys input_method\Z"), Fail("closed"))
    code, out, err = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json")
    assert code == 10
    assert err == ""
    error = json.loads(out)
    assert error["schema"] == "uiprobe.error/1"
    assert error["error"]["code"] == "adb_error"
    assert android.inputs == []


@pytest.mark.parametrize("as_json", [True, False])
def test_keyboard_interrupt_during_the_preview(
    fake_adb, android, capsys, monkeypatch, as_json
):
    def interrupted(_self):
        raise KeyboardInterrupt

    monkeypatch.setattr(Device, "dump", interrupted)
    argv = ["crawl", SHOP, "--dry-run", *(["--json"] if as_json else [])]
    code, out, err = invoke(capsys, fake_adb, *argv)
    assert code == 130
    if as_json:
        assert err == ""
        error = json.loads(out)
        assert error["schema"] == "uiprobe.error/1"
        assert error["error"]["code"] == "interrupted"
    else:
        assert out == ""
        assert err == "uiprobe: error: interrupted\n"
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_keyboard_interrupt_while_reading_the_foreground(fake_adb, capsys, monkeypatch):
    def interrupted(_self):
        raise KeyboardInterrupt

    monkeypatch.setattr(Device, "foreground", interrupted)
    code, out, _ = invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run", "--json")
    assert code == 130
    assert json.loads(out)["error"]["code"] == "interrupted"


def test_crawl_help_says_the_preview_is_the_default(capsys):
    code = cli.run(["crawl", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert code == 0
    assert "The preview is the default" in out
    assert "--dry-run forces the preview" in out
    assert "--press, or press = true in the settings file, runs a pressing" in out
    assert "also when the settings file says press = true" in out
    assert "presses nothing" in out
    assert "screen showing now" in out
    assert "cannot be combined with --press" in out
    assert "cannot be combined with --dry-run" in out


@pytest.mark.usefixtures("android")
def test_nothing_is_written(fake_adb, capsys, settings_dir):
    before = {p.name: p.read_bytes() for p in settings_dir.iterdir()}
    crawl(capsys, fake_adb)
    assert {p.name: p.read_bytes() for p in settings_dir.iterdir()} == before
    assert sys.stdout is not None


def test_preview_json_lists_a_sliver_as_not_visible_and_a_cover(fake_adb, capsys):
    FakeAndroid(
        fake_adb,
        SERIAL,
        (
            Screen(
                "rules",
                f"{SHOP}/.Rules",
                (
                    El((40, 100, 280, 150), text="Ok row", go="rules"),
                    El((40, 100, 280, 150), text="Banner", go="rules"),
                    El((40, 633, 280, 700), text="Sliver", go="rules"),
                    El((40, 632, 280, 700), text="Eight", go="rules"),
                ),
            ),
        ),
        start="rules",
    )
    payload = crawl_json(capsys, fake_adb)
    assert [e["text"] for e in payload["press"]] == ["Banner"]
    skipped = skipped_by_text(payload)
    # 8 pixels show: visible (it is skipped only for the screen edge)
    assert "not_visible" not in skipped["Eight"]["reasons"]
    assert "center" in skipped["Eight"]
    assert skipped["Ok row"]["reasons"] == ["covered"]
    assert skipped["Ok row"]["details"] == [
        {"reason": "covered", "note": "covered by e3", "element_id": "e3"}
    ]
    assert "center" not in skipped["Ok row"]
    assert skipped["Sliver"]["reasons"] == ["not_visible"]
    assert "center" not in skipped["Sliver"]


@pytest.mark.usefixtures("android")
def test_number_only_and_toggle_in_json(fake_adb, android, capsys):
    android.show("rules")
    payload = crawl_json(capsys, fake_adb)
    assert [e["text"] for e in payload["press"]] == ["Page 2"]
    skipped = skipped_by_text(payload)
    number = skipped["200"]
    assert number["reasons"] == ["number_only"]
    assert number["details"] == [
        {"reason": "number_only", "word": "200", "note": "200"}
    ]
    switch = skipped["Sound"]
    assert switch["reasons"] == ["toggle"]
    assert switch["details"] == [{"reason": "toggle", "element_id": "e3"}]
    both = skipped["7"]
    assert both["reasons"] == ["number_only", "toggle"]
    assert both["details"][1] == {"reason": "toggle", "element_id": "e5"}
    assert_read_only(fake_adb, android)


@pytest.mark.usefixtures("android")
def test_number_only_and_toggle_in_text(fake_adb, android, capsys):
    android.show("rules")
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    pop_ending(lines)
    assert lines[3:] == [
        "would press (1):",
        '  e4 TextView "Page 2" [tap] center=160,245',
        "would not press (3):",
        '  e2 TextView "200" [tap] center=160,125: number_only',
        '  e3 TextView "Sound" [tap] [checked] center=160,185: toggle',
        '  e5 TextView "7" [tap] center=160,305: number_only, toggle',
    ]


@pytest.mark.usefixtures("android")
def test_preview_policy_turns_the_number_only_and_toggle_rules_off(fake_adb, android):
    android.show("rules")
    policy = Policy(skip_number_only=False, skip_toggles=False)
    result = preview(device_of(fake_adb), SHOP, policy)
    assert [d.element.text for d in result.judgement.pressable] == [
        "200",
        "Sound",
        "Page 2",
        "7",
    ]
