"""Tests for the dry run's Python API, what the device sees, and exception words."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_adb_server import Fail
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import Policy, Preview, preview
from uiprobe.errors import AdbError
from uiprobe.guards import judge
from uiprobe.settings import (
    SETTINGS_FILE,
    render_default_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_dry.helpers import (
    ACTIVITIES,
    OTHER,
    OTHER_APP,
    ROWS_SCREEN,
    SERIAL,
    SHOP,
    WINDOW,
    assert_read_only,
    crawl,
    crawl_json,
    device_commands,
    device_of,
    rows_xml,
    skipped_by_text,
)

# --- the device sees only reads ---


@pytest.mark.usefixtures("android")
def test_cli_preview_sends_only_read_commands(fake_adb, android, capsys):
    code, _, _ = crawl(capsys, fake_adb)
    assert code == 0
    assert_read_only(fake_adb, android)
    commands = device_commands(fake_adb)
    assert any(c.startswith("uiautomator dump ") for c in commands)
    assert "dumpsys input_method" in commands


def test_python_preview_sends_only_read_commands(fake_adb, android):
    preview(device_of(fake_adb), SHOP)
    assert_read_only(fake_adb, android)


@pytest.mark.parametrize("flags", [[], ["--json"]])
def test_cli_json_preview_sends_only_read_commands(fake_adb, android, capsys, flags):
    code, _, _ = crawl(capsys, fake_adb, *flags)
    assert code == 0
    assert_read_only(fake_adb, android)


# --- Python: arguments ---


@pytest.mark.parametrize("package", [None, 5, b"com.x", ["com.x"]])
def test_preview_rejects_a_package_that_is_not_a_str(fake_adb, android, package):
    device = device_of(fake_adb)
    before = len(fake_adb.requests)
    with pytest.raises(TypeError):
        preview(device, package)  # type: ignore[arg-type]
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize(
    "package",
    [
        "",
        "com",
        "1com.x",
        "com..x",
        "com.x.",
        ".com.x",
        "com.ex-ample",
        "com.1x",
        "com.x y",
        "com.x\n",
        "com.\u00e9x",
        "com." + "a" * 252,
    ],
)
def test_preview_rejects_a_bad_package(fake_adb, android, package):
    device = device_of(fake_adb)
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="package"):
        preview(device, package)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_preview_accepts_the_longest_package(fake_adb, android):
    package = "com." + "a" * 251
    assert len(package) == 255
    assert preview(device_of(fake_adb), package).package == package
    assert_read_only(fake_adb, android)


@pytest.mark.parametrize("policy", ["Policy()", {}, object(), ("x",), 0])
def test_preview_rejects_a_policy_that_is_not_a_policy(fake_adb, android, policy):
    device = device_of(fake_adb)
    before = len(fake_adb.requests)
    with pytest.raises(TypeError):
        preview(device, SHOP, policy)  # type: ignore[arg-type]
    assert len(fake_adb.requests) == before
    assert android.inputs == []


# --- Python: behavior ---


def test_preview_reads_in_order_then_judges(fake_adb, android, monkeypatch):
    device = device_of(fake_adb)
    calls: list[str] = []
    for name in ("dump", "foreground", "keyboard_shown"):
        original = getattr(device, name)

        def spy(original=original, name=name):
            calls.append(name)
            return original()

        monkeypatch.setattr(device, name, spy)
    result = preview(device, SHOP)
    assert calls == ["dump", "foreground", "keyboard_shown"]
    assert isinstance(result, Preview)
    assert_read_only(fake_adb, android)


def test_preview_fields(fake_adb, android):
    device = device_of(fake_adb)
    result = preview(device, SHOP)
    assert result.package == SHOP
    assert result.foreground == SHOP
    assert result.keyboard_shown is False
    assert result.screen.package == SHOP
    assert result.screen.screen_id.startswith("s-")
    assert len(result.judgement.decisions) == 7
    assert result.judgement == judge(result.screen, SHOP, Policy(), foreground=SHOP)
    assert_read_only(fake_adb, android)


def test_preview_reports_the_keyboard(fake_adb, android):
    android.show("typing")
    assert preview(device_of(fake_adb), SHOP).keyboard_shown is True


@pytest.mark.usefixtures("android")
def test_preview_press_and_skip_equal_a_direct_judge(fake_adb):
    result = preview(device_of(fake_adb), SHOP)
    direct = judge(result.screen, SHOP, Policy(), foreground=SHOP)
    assert result.judgement.pressable == direct.pressable
    assert result.judgement.skipped == direct.skipped
    assert [d.element.text for d in direct.pressable] == ["Browse", "Help"]


@pytest.mark.usefixtures("android")
def test_preview_passes_the_policy(fake_adb):
    policy = Policy(tap_unlabeled=True).with_deny_words(["browse"])
    result = preview(device_of(fake_adb), SHOP, policy)
    direct = judge(result.screen, SHOP, policy, foreground=SHOP)
    assert result.judgement == direct
    assert [d.element.id for d in direct.pressable] == ["e6", "e9"]


def test_preview_other_app_in_front_skips_everything(fake_adb, android):
    android.override(WINDOW, OTHER_APP.encode())
    result = preview(device_of(fake_adb), SHOP)
    assert result.foreground == OTHER
    assert result.judgement.pressable == ()
    assert result.judgement.decisions
    for decision in result.judgement.decisions:
        assert "outside_target_app" in decision.reasons
    assert_read_only(fake_adb, android)


def test_preview_unknown_foreground_skips_everything(fake_adb, android):
    android.override(WINDOW, b"nothing\n")
    android.override(ACTIVITIES, b"nothing\n")
    result = preview(device_of(fake_adb), SHOP)
    assert result.foreground is None
    assert result.judgement.pressable == ()
    for decision in result.judgement.decisions:
        assert "outside_target_app" in decision.reasons
    assert_read_only(fake_adb, android)


def test_preview_device_errors_propagate(fake_adb, android):
    android.override(re.compile(r"^dumpsys input_method\Z"), Fail("closed"))
    with pytest.raises(AdbError):
        preview(device_of(fake_adb), SHOP)
    assert android.inputs == []


# --- exception words in the preview ---

AUTO = Screen(
    "auto",
    f"{SHOP}/.Auto",
    (
        El((40, 100, 280, 150), text="Automatically", go="auto"),
        El((40, 160, 280, 210), text="Automatically pay", go="auto"),
        El((40, 220, 280, 270), text="Bill my card automatically", go="auto"),
        El((40, 280, 280, 330), text="Voice Recorder", go="auto"),
        El((40, 340, 280, 390), text="Job order", go="auto"),
    ),
)


def _with_except(folder: Path, items: str) -> None:
    text = render_default_settings()
    assert text.count("except = []\n") == 1
    path = folder / SETTINGS_FILE
    path.write_text(text.replace("except = []\n", f"except = {items}\n"), "utf-8")


def _words(payload: dict[str, Any]) -> dict[str, str]:
    skip = skipped_by_text(payload)
    return {text: entry["details"][0]["word"] for text, entry in skip.items()}


def test_the_default_preview_has_no_exceptions(fake_adb, capsys):
    android = FakeAndroid(fake_adb, SERIAL, (AUTO,), start="auto")
    payload = crawl_json(capsys, fake_adb)
    assert payload["press"] == []
    assert _words(payload) == {
        "Automatically": "call",
        "Automatically pay": "pay",
        "Bill my card automatically": "call",
        "Voice Recorder": "order",
        "Job order": "order",
    }
    assert_read_only(fake_adb, android)


def test_the_preview_lets_an_exception_word_through(fake_adb, capsys, settings_dir):
    _with_except(settings_dir, '["automatically", "recorder"]')
    android = FakeAndroid(fake_adb, SERIAL, (AUTO,), start="auto")
    payload = crawl_json(capsys, fake_adb)
    assert [e["text"] for e in payload["press"]] == [
        "Automatically",
        "Bill my card automatically",
        "Voice Recorder",
    ]
    assert _words(payload) == {"Automatically pay": "pay", "Job order": "order"}
    assert_read_only(fake_adb, android)


def test_deny_blocks_a_word_that_is_also_an_exception(fake_adb, capsys, settings_dir):
    _with_except(settings_dir, '["automatically", "recorder"]')
    FakeAndroid(fake_adb, SERIAL, (AUTO,), start="auto")
    payload = crawl_json(capsys, fake_adb, "--deny", "automatically")
    assert [e["text"] for e in payload["press"]] == ["Voice Recorder"]
    entry = skipped_by_text(payload)["Automatically"]
    assert entry["reasons"] == ["deny_word"]
    # The exception equal to the added word is dropped, so the first default
    # word found ("call") refuses it again, as it does without exceptions.
    assert entry["details"][0]["word"] == "call"


def test_an_exception_also_excuses_a_deny_word(fake_adb, capsys, settings_dir):
    # A user exception excuses every deny word, the added ones too.
    _with_except(settings_dir, '["automatically"]')
    FakeAndroid(fake_adb, SERIAL, (AUTO,), start="auto")
    payload = crawl_json(capsys, fake_adb, "--deny", "matic")
    assert "Automatically" in [e["text"] for e in payload["press"]]
    assert _words(payload)["Automatically pay"] == "pay"


def rows_device(
    fake_adb: FakeAdbServer, monkeypatch: pytest.MonkeyPatch
) -> FakeAndroid:
    android = FakeAndroid(fake_adb, SERIAL, (ROWS_SCREEN,), start="rows")
    monkeypatch.setattr(android, "render_xml", lambda: rows_xml(android))
    return android


def test_row_reasons_in_json(fake_adb, capsys, monkeypatch):
    android = rows_device(fake_adb, monkeypatch)
    payload = crawl_json(capsys, fake_adb)
    skipped = skipped_by_text(payload)
    row = next(e for e in payload["skip"] if e["id"] == "e2")
    assert row["reasons"] == ["two_target_row"]
    assert row["details"] == [{"reason": "two_target_row", "element_id": "e5"}]
    # The gear inside it and the mail row (no divider) are pressed by default.
    assert {e["id"] for e in payload["press"]} == {"e7", "e8", "e10"}
    assert "Inbox: hello" not in skipped
    assert_read_only(fake_adb, android)


def test_row_with_button_in_json_when_turned_on(
    fake_adb, capsys, monkeypatch, settings_dir
):
    rows_device(fake_adb, monkeypatch)
    text = render_default_settings().replace(
        "skip_rows_with_buttons = false", "skip_rows_with_buttons = true"
    )
    (settings_dir / SETTINGS_FILE).write_text(text, encoding="utf-8")
    payload = crawl_json(capsys, fake_adb)
    by_id = {e["id"]: e for e in payload["skip"]}
    assert by_id["e2"]["reasons"] == ["two_target_row"]  # only the narrow reason
    assert by_id["e8"]["reasons"] == ["row_with_button"]
    assert by_id["e8"]["details"] == [
        {"reason": "row_with_button", "element_id": "e10"}
    ]
    assert {e["id"] for e in payload["press"]} == {"e7", "e10"}


def test_row_reasons_in_text(fake_adb, capsys, monkeypatch):
    rows_device(fake_adb, monkeypatch)
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    refused = [line for line in out.splitlines() if line.startswith("  e2 ")]
    assert len(refused) == 1
    assert refused[0].endswith(": two_target_row")
