"""Tests of the read-only readers: foreground, keyboard_shown, is_emulator."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import Fail
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe.device import Device
from uiprobe.errors import AdbError, AdbServerUnavailableError

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
OTHER = "com.other.app"
APIS = [24, 26, 28, 29, 30, 31, 32, 36]

SCREENS = (
    Screen("main", f"{SHOP}/.Main", (El((0, 0, 320, 100), text="Hi"),)),
    Screen(
        "editing", f"{SHOP}/.Edit", (El((0, 0, 320, 100), text="Name"),), keyboard=True
    ),
)
OTHER_APP = f"  mFocusedApp=ActivityRecord{{1 u0 {OTHER}/.Main t3}}"
WINDOW = re.compile(r"^dumpsys window\Z")
INPUT_METHOD = re.compile(r"^dumpsys input_method\Z")


def make(
    fake_adb: FakeAdbServer,
    *,
    api: int = 36,
    start: str = "main",
    focused_app_line: bool = True,
) -> tuple[FakeAndroid, Device]:
    """A strict FakeAndroid (the default mode) and a Device on it."""
    android = FakeAndroid(
        fake_adb,
        SERIAL,
        SCREENS,
        start=start,
        api=api,
        focused_app_line=focused_app_line,
    )
    return android, Device(adb_server=fake_adb.address)


def assert_no_input(fake_adb: FakeAdbServer, android: FakeAndroid) -> None:
    assert android.inputs == []
    assert not [r for r in fake_adb.requests if re.match(r"(shell|exec):input\b", r)]


def input_method(*lines: str) -> str:
    return "\n".join(lines) + "\n"


# --- foreground ---


@pytest.mark.parametrize("focused_app_line", [True, False])
@pytest.mark.parametrize("api", APIS)
def test_foreground_returns_the_package_in_front(fake_adb, api, focused_app_line):
    android, device = make(fake_adb, api=api, focused_app_line=focused_app_line)
    assert device.foreground() == SHOP
    assert_no_input(fake_adb, android)


def test_foreground_falls_back_to_the_activity_source_when_window_is_absent(fake_adb):
    android, device = make(fake_adb, focused_app_line=False)
    assert device.foreground() == SHOP
    assert any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)
    assert_no_input(fake_adb, android)


def test_foreground_does_not_fall_back_when_window_source_is_present(fake_adb):
    android, device = make(fake_adb)
    assert device.foreground() == SHOP
    assert not any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)
    assert_no_input(fake_adb, android)


def test_foreground_names_another_package_without_filtering(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, (OTHER_APP + "\n").encode())
    assert device.foreground() == OTHER
    assert_no_input(fake_adb, android)


@pytest.mark.parametrize("api", [24, 36])
def test_foreground_forged_extra_line_gives_none(fake_adb, api):
    android, device = make(fake_adb, api=api)
    android.extra_dumpsys["window"] = OTHER_APP + "\n"
    assert device.foreground() is None
    assert not any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)
    assert_no_input(fake_adb, android)


def test_foreground_malformed_lone_line_counts_as_absent(fake_adb):
    android, device = make(fake_adb, focused_app_line=False)
    android.extra_dumpsys["window"] = "  mFocusedApp=ActivityRecord{1 u0 bad t3}\n"
    assert device.foreground() == SHOP  # from the activity source
    assert_no_input(fake_adb, android)


def test_foreground_malformed_line_beside_another_gives_none(fake_adb):
    android, device = make(fake_adb)
    android.extra_dumpsys["window"] = "  mFocusedApp=ActivityRecord{1 u0 bad t3}\n"
    assert device.foreground() is None
    assert_no_input(fake_adb, android)


def test_foreground_none_when_nothing_names_it(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, b"nothing\n")
    android.override(re.compile(r"^dumpsys activity activities\Z"), b"nothing\n")
    assert device.foreground() is None
    assert_no_input(fake_adb, android)


def test_foreground_error_propagates(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, Fail("closed"))
    with pytest.raises(AdbError):
        device.foreground()
    assert_no_input(fake_adb, android)


def test_foreground_error_in_the_fallback_propagates(fake_adb):
    android, device = make(fake_adb, focused_app_line=False)
    android.override(re.compile(r"^dumpsys activity activities\Z"), Fail("closed"))
    with pytest.raises(AdbError):
        device.foreground()
    assert_no_input(fake_adb, android)


def test_foreground_unreachable_server_propagates(fake_adb):
    android, device = make(fake_adb)
    fake_adb.stop()
    with pytest.raises(AdbServerUnavailableError):
        device.foreground()
    assert_no_input(fake_adb, android)


# --- dump keeps its package filter ---


def test_dump_activity_still_requires_the_dumped_package(fake_adb):
    android, device = make(fake_adb)
    assert device.dump().activity == ".Main"
    android.override(WINDOW, (OTHER_APP + "\n").encode())
    screen = device.dump()
    assert screen.package == SHOP
    assert screen.activity is None
    assert device.foreground() == OTHER
    assert_no_input(fake_adb, android)


def test_dump_activity_error_is_still_unknown_but_foreground_raises(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, Fail("closed"))
    assert device.dump().activity is None
    with pytest.raises(AdbError):
        device.foreground()
    assert_no_input(fake_adb, android)


# --- keyboard_shown ---


@pytest.mark.parametrize("api", APIS)
def test_keyboard_shown_true_and_false_on_every_api(fake_adb, api):
    android, device = make(fake_adb, api=api, start="editing")
    assert device.keyboard_shown() is True
    android.show("main")
    assert device.keyboard_shown() is False
    assert_no_input(fake_adb, android)


OLD_SHOWN = (
    "  mShowRequested=true mShowExplicitlyRequested=true mShowForced=false"
    " mInputShown=true"
)
OLD_HIDDEN = (
    "  mShowRequested=false mShowExplicitlyRequested=false mShowForced=false"
    " mInputShown=false"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (OLD_SHOWN, True),
        (OLD_HIDDEN, False),
        ("      mInputShown=true", True),
        ("      mInputShown=false", False),
        ("    mInputShown=true", True),  # API 33-35 indentation is not captured
        ("  mIsInputViewShown=true mStatusIcon=0\n" + OLD_HIDDEN, False),
        (OLD_SHOWN + "\n" + OLD_SHOWN, True),  # the same value twice
    ],
)
def test_keyboard_shown_captured_lines(fake_adb, text, expected):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, (text + "\n").encode())
    assert device.keyboard_shown() is expected
    assert_no_input(fake_adb, android)


def test_keyboard_shown_ignores_is_input_view_shown(fake_adb):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, b"  mIsInputViewShown=true mStatusIcon=0\n")
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nothing here",
        OLD_SHOWN + "\n" + OLD_HIDDEN,  # disagreeing, old shape
        OLD_HIDDEN + "\n      mInputShown=true",  # disagreeing across shapes
        "      mInputShown=false\n      mInputShown=true",
        "    hintText=x\n  mShowRequested=true mShowExplicitlyRequested=true"
        " mShowForced=false mInputShown=true\n" + "      mInputShown=false",
        "      mInputShown=true\n    hintText=a mInputShown=true",  # inside hintText
        "    hintText=mInputShown=true",
        "      mInputShown=maybe",
        "      mInputShown=true x",
        "      mInputShown=True",
        OLD_SHOWN + " extra",
        OLD_SHOWN.replace("  mShow", "mShow", 1),  # wrong indentation
        " " * 5000 + "mInputShown=true",  # well-formed but over-long
        OLD_SHOWN.replace("  mShow", " " * 5000 + "mShow", 1),
        "      mInputShown=true " + "x" * 5000,  # over-long
        "      mInputShown=" + "true" * 2000,
    ],
)
def test_keyboard_shown_unknown(fake_adb, text):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, (text + "\n").encode())
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


FORGED = {
    "old": "  mShowRequested=false mShowExplicitlyRequested=false mShowForced=false"
    " mInputShown={}",
    "own": "      mInputShown={}",
}


@pytest.mark.parametrize("shape", ["old", "own"])
@pytest.mark.parametrize("api", [24, 36])
@pytest.mark.parametrize("keyboard", [True, False])
def test_keyboard_shown_forged_disagreeing_line_gives_none(
    fake_adb, api, keyboard, shape
):
    android, device = make(fake_adb, api=api, start="editing" if keyboard else "main")
    assert device.keyboard_shown() is keyboard
    forged = FORGED[shape].format("false" if keyboard else "true")
    android.extra_dumpsys["input_method"] = forged + "\n"
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


def test_keyboard_shown_forged_line_inside_hint_text_gives_none(fake_adb):
    android, device = make(fake_adb, start="editing")
    android.extra_dumpsys["input_method"] = "      hintText=a\n b mInputShown=false\n"
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


def test_keyboard_shown_handles_crlf(fake_adb):
    android, device = make(fake_adb)
    for text, expected in ((OLD_SHOWN, True), ("      mInputShown=false", False)):
        android.override(INPUT_METHOD, (text + "\r\n  other\r\n").encode())
        assert device.keyboard_shown() is expected
    assert_no_input(fake_adb, android)


def test_keyboard_shown_error_propagates(fake_adb):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, Fail("closed"))
    with pytest.raises(AdbError):
        device.keyboard_shown()
    assert_no_input(fake_adb, android)


def test_keyboard_shown_unreachable_server_propagates(fake_adb):
    android, device = make(fake_adb)
    fake_adb.stop()
    with pytest.raises(AdbServerUnavailableError):
        device.keyboard_shown()
    assert_no_input(fake_adb, android)


# --- is_emulator ---

GETPROP = re.compile(r"^getprop ro\.kernel\.qemu\Z")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"1\n", True),
        (b"1\r\n", True),
        (b"  1 \r\n", True),
        (b"1", True),
        (b"", False),
        (b"\n", False),
        (b"0\n", False),
        (b"true\n", False),
        (b"11\n", False),
        (b"1 1\n", False),
        (b"01\n", False),
    ],
)
def test_is_emulator_strict_value(fake_adb, output, expected):
    android, device = make(fake_adb)
    android.override(GETPROP, output)
    assert device.is_emulator() is expected
    assert_no_input(fake_adb, android)


def test_is_emulator_default_fake_is_an_emulator(fake_adb):
    android, device = make(fake_adb)
    assert device.is_emulator() is True
    assert_no_input(fake_adb, android)


def test_is_emulator_error_propagates(fake_adb):
    android, device = make(fake_adb)
    android.override(GETPROP, Fail("closed"))
    with pytest.raises(AdbError):
        device.is_emulator()
    assert_no_input(fake_adb, android)


def test_is_emulator_unreachable_server_propagates(fake_adb):
    android, device = make(fake_adb)
    fake_adb.stop()
    with pytest.raises(AdbServerUnavailableError):
        device.is_emulator()
    assert_no_input(fake_adb, android)


# --- focused_window ---


def focus(title: str) -> str:
    return f"  mCurrentFocus=Window{{4c2f1a8 u0 {title}}}\n"


@pytest.mark.parametrize("api", APIS)
def test_focused_window_names_the_app_in_front(fake_adb, api):
    _, device = make(fake_adb, api=api)
    assert device.focused_window() == SHOP


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        (focus(f"{SHOP}/{SHOP}.Main"), SHOP),
        (focus("NotificationShade"), ""),
        (focus("StatusBar"), ""),
        (focus("PopupWindow:1a2b3c"), ""),
        ("  mCurrentFocus=null\n", None),
        ("", None),
        (focus(f"{SHOP}/.Main") + focus("NotificationShade"), None),
        (focus(f"{SHOP}/.Main") * 2, SHOP),
        ("  mCurrentFocus=Window{zz u0 x/y}\n", None),
        ("  mCurrentFocus=Window{1 u0 " + "a" * 600 + "}\n", None),
    ],
    ids=[
        "app",
        "shade",
        "status-bar",
        "popup",
        "null",
        "absent",
        "forged-disagrees",
        "same-twice",
        "bad-hash",
        "too-long",
    ],
)
def test_focused_window_reads_the_focus_line(fake_adb, text, answer):
    android, device = make(fake_adb)
    android.override(WINDOW, text.encode())
    assert device.focused_window() == answer
    assert_no_input(fake_adb, android)
