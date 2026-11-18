"""Tests for ``uiprobe crawl`` (the preview) and ``uiprobe.preview``.

The device is a strict `FakeAndroid`: any command beyond the reads a dump, the
foreground and the keyboard need fails the test. On top of that every test of a
preview checks the commands the device received and that no input arrived.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_adb_server import Fail
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import Device, Policy, Preview, cli, preview
from uiprobe.errors import AdbError
from uiprobe.guards import REASONS, judge
from uiprobe.settings import (
    SETTINGS_FILE,
    _shown,
    render_default_settings,
    write_default_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.model import Screen as ModelScreen

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
OTHER = "com.other.app"
WINDOW = re.compile(r"^dumpsys window\Z")
ACTIVITIES = re.compile(r"^dumpsys activity activities\Z")
OTHER_APP = f"  mFocusedApp=ActivityRecord{{1 u0 {OTHER}/.Main t3}}\n"

# What a preview may send: the reads of dump, foreground and keyboard_shown.
READ_ONLY = (
    re.compile(r"^uiautomator dump /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml\Z"),
    re.compile(r"^rm -f /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml\Z"),
    re.compile(r"^cat /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml\Z"),
    re.compile(r"^wm size\Z"),
    re.compile(r"^getprop (?:ro\.build\.version\.sdk|ro\.kernel\.qemu)\Z"),
    re.compile(r"^dumpsys (?:window|activity activities|input_method)\Z"),
)
FORBIDDEN = ("input", "am ", "monkey", "pm ", "cmd ", "settings ", "svc", "screencap")

# Document order gives the ids: e1 is the root, e2.. follow the tuple.
FIXTURE = Screen(
    "main",
    f"{SHOP}/.Main",
    (
        El((40, 100, 280, 150), text="Browse", rid=f"{SHOP}:id/browse", go="main"),
        El((40, 160, 280, 210), text="Delete account", go="main"),
        El((40, 220, 280, 270), text="Open row", go="main"),
        El((50, 230, 270, 260), text="Delete"),
        El((40, 280, 280, 330), go="main"),
        El((40, 340, 280, 390), text="Settings", go="main", enabled=False),
        El((40, 700, 280, 750), text="Offscreen", go="main"),
        El((40, 400, 280, 450), text="Help", go="main"),
    ),
    keyboard=False,
)
EMPTY = Screen("empty", f"{SHOP}/.Empty", (El((40, 100, 280, 150), text="Label"),))
ALL_SKIPPED = Screen(
    "locked", f"{SHOP}/.Locked", (El((40, 100, 280, 150), text="Delete", go="main"),)
)
KEYBOARD = Screen(
    "typing",
    f"{SHOP}/.Typing",
    (El((40, 100, 280, 150), text="Browse", go="main"),),
    keyboard=True,
)
RULES = Screen(
    "rules",
    f"{SHOP}/.Rules",
    (
        El((40, 100, 280, 150), text="200", go="main"),
        El((40, 160, 280, 210), text="Sound", go="main", checkable=True, checked=True),
        El((40, 220, 280, 270), text="Page 2", go="main"),
        El((40, 280, 280, 330), text="7", go="main", checkable=True),
    ),
)
SCREENS = (FIXTURE, EMPTY, ALL_SKIPPED, KEYBOARD, RULES)


@pytest.fixture(autouse=True)
def settings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in a folder that holds the generated default settings file."""
    folder = tmp_path / "work"
    folder.mkdir()
    write_default_settings(folder / SETTINGS_FILE)
    monkeypatch.chdir(folder)
    return folder


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")


def device_of(fake_adb: FakeAdbServer) -> Device:
    return Device(adb_server=fake_adb.address)


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def device_commands(fake_adb: FakeAdbServer) -> list[str]:
    """The shell and exec commands the device received, in order."""
    return [
        request.partition(":")[2]
        for request in fake_adb.requests
        if request.startswith(("shell:", "exec:"))
    ]


def assert_read_only(fake_adb: FakeAdbServer, android: FakeAndroid) -> None:
    commands = device_commands(fake_adb)
    assert commands
    assert android.inputs == []
    for command in commands:
        assert any(pattern.match(command) for pattern in READ_ONLY), command
        probe = re.sub(r"^dumpsys input_method\Z", "", command)
        assert not any(word in probe for word in FORBIDDEN), command
        assert not command.startswith("wm size "), command


def crawl(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    return invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run", *argv)


def crawl_json(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> dict[str, Any]:
    code, out, err = crawl(capsys, fake_adb, "--json", *argv)
    assert (code, err) == (0, "")
    return json.loads(out)


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


def skipped_by_text(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry.get("text", ""): entry for entry in payload["skip"]}


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


# --- CLI: text ---


@pytest.mark.usefixtures("android")
def test_text_output(fake_adb, capsys):
    code, out, err = crawl(capsys, fake_adb)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines.pop() == DRY_RUN_LAST
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
    assert lines.pop() == DRY_RUN_LAST
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
    assert lines.pop() == DRY_RUN_LAST
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


PRESS_MESSAGE = (
    "pressing crawls are not available yet; this version only previews"
    " (run without --press)"
)


def file_press_message(path: str) -> str:
    return (
        "pressing crawls are not available yet; this version only previews"
        f" (the settings file {path} says press = true: add --dry-run, or set"
        " press = false there)"
    )


def both_press_message(path: str) -> str:
    return (
        "pressing crawls are not available yet; this version only previews"
        f" (--press is given and the settings file {path} says press = true:"
        " replace --press with --dry-run, or remove --press and set"
        " press = false there)"
    )


DEFAULT_LAST = "nothing was pressed: this was a preview (the default)"
DRY_RUN_LAST = "nothing was pressed: this was a preview (--dry-run)"


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
    body, last = out.rstrip("\n").rsplit("\n", 1)
    body_dry, last_dry = out_dry.rstrip("\n").rsplit("\n", 1)
    assert body == body_dry
    assert last == DEFAULT_LAST
    assert last_dry == DRY_RUN_LAST
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


def test_press_in_the_file_is_refused_without_dry_run(
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
    assert err == f"uiprobe: error: {both_press_message(SETTINGS_FILE)}\n"
    assert "--dry-run" in err
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []
    # Each suggested retry: replacing --press with --dry-run previews; removing
    # only --press is still refused because the file asks for pressing.
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
    assert out.rstrip("\n").endswith(DEFAULT_LAST)


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
    expected = (both_press_message if switch else file_press_message)(shown)
    assert err == f"uiprobe: error: {expected}\n"
    assert fake_adb.requests == []
    assert connections == []
    assert android.inputs == []


def test_missing_file_guidance_keeps_press(fake_adb, android, capsys, tmp_path):
    here = tmp_path / "empty"
    here.mkdir()
    os.chdir(here)
    message = crawl_usage_error(capsys, fake_adb, SHOP, "--press")
    assert message.splitlines()[-1] == (
        f"or name one with:  uiprobe crawl {SHOP} --press --config PATH"
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
    assert out.rstrip("\n").endswith(DRY_RUN_LAST)
    assert_read_only(fake_adb, android)


@pytest.mark.parametrize("switch", [[], ["--dry-run"], ["--press"]])
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


def test_press_in_the_file_is_refused_in_json(
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
    assert "not available yet" in out
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
    assert lines.pop() == DRY_RUN_LAST
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
