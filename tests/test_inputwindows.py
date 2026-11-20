"""Tests for reading the window a touch reaches from ``dumpsys input``.

The window lines are copied from emulators (API 24, 30, 31 and 36); only the
parts the parser reads matter, the rest stays as captured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uiprobe.inputwindows import (
    LINE_FORMAT,
    NO_LIST,
    REGION_FORMAT,
    InputWindow,
    parse_input_windows,
    window_at,
)

DATA = Path(__file__).parent / "data" / "inputwindows"


def _capture(name: str) -> str:
    return (DATA / f"{name}.txt").read_text(encoding="utf-8")


API24 = _capture("api24")

API30 = _capture("api30")

API31 = _capture("api31")

API36 = _capture("api36")


def _at(text: str, point: tuple[int, int]) -> InputWindow | None:
    windows = parse_input_windows(text)
    assert not isinstance(windows, str), windows
    return window_at(windows, point)


@pytest.mark.parametrize(
    ("text", "point", "title", "uid"),
    [
        (API24, (160, 500), "InputMethod", 10053),  # the keyboard covers it
        (API24, (160, 100), "com.android.settings/com.android.settings.Settings", 1000),
        (API24, (160, 10), "StatusBar", 10027),
        (
            API30,
            (160, 320),
            "com.google.android.apps.nexuslauncher/"
            "com.google.android.apps.nexuslauncher.NexusLauncherActivity",
            10153,
        ),
        (
            API31,
            (160, 320),
            "com.google.android.apps.nexuslauncher/"
            "com.google.android.apps.nexuslauncher.NexusLauncherActivity",
            10133,
        ),
        (API36, (296, 458), "FloatingMenu", 10186),  # the floating button
        (API36, (160, 320), "com.android.settings/com.android.settings.Settings", 1000),
        (API36, (271, 458), "com.android.settings/com.android.settings.Settings", 1000),
        (API36, (296, 482), "com.android.settings/com.android.settings.Settings", 1000),
    ],
)
def test_the_topmost_window_taking_the_touch_is_found(text, point, title, uid):
    found = _at(text, point)
    assert found is not None
    assert (found.title, found.owner_uid) == (title, uid)


def test_windows_that_do_not_take_touches_are_skipped():
    # API 24: the keyguard scrim is not visible; API 30: the first launcher
    # window has FLAG_NOT_TOUCHABLE (0x10 in 0x...18); API 31: NOT_TOUCHABLE;
    # API 36: the spy windows and the gesture monitor only watch.
    found = _at(API24, (160, 100))
    assert found is not None
    assert found.title.startswith("com.android.settings")
    for text in (API30, API31):
        windows = parse_input_windows(text)
        assert not isinstance(windows, str)
        assert [w.takes_touch for w in windows][-2:] == [False, True]
    windows = parse_input_windows(API36)
    assert not isinstance(windows, str)
    assert [w.takes_touch for w in windows] == [False, False, True, True, True, False]
    assert windows[3].region is None  # <empty>


def test_a_point_no_window_takes_is_none():
    assert _at(API36, (400, 900)) is None


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", NO_LIST),
        ("Input Dispatcher State:\n  DispatchEnabled: true\n", NO_LIST),
        ("Input Dispatcher State:\n  Windows: <none>\n", NO_LIST),
        (API36.replace("ownerUid=10186", "ownerUid=x"), LINE_FORMAT),
        (API36.replace("inputConfig=0x0, ", ""), LINE_FORMAT),
        (
            API24.replace("visible=true, canReceiveKeys=true", "canReceiveKeys=true"),
            LINE_FORMAT,
        ),
        (API31.replace("flags=NOT_TOUCH_MODAL", "flags=?NOT_TOUCH_MODAL"), LINE_FORMAT),
        (
            API36.replace(
                "touchableRegion=[272,434][320,482]",
                "touchableRegion=[272,434][320,482][0,0][10,10]",
            ),
            REGION_FORMAT,
        ),
        (
            API36.replace("touchableRegion=<empty>", "touchableRegion=(empty)"),
            REGION_FORMAT,
        ),
        (API36.replace("      2: name=5ebeeda", "      2: nom=5ebeeda"), LINE_FORMAT),
    ],
)
def test_a_list_that_cannot_be_read_says_why(text, reason):
    # A line of no known form makes the whole list unknown: it could be the
    # window on top.
    assert parse_input_windows(text) == reason


def test_only_the_asked_display_counts():
    other = API36.replace("id=133, displayId=0", "id=133, displayId=1")
    windows = parse_input_windows(other)
    assert not isinstance(windows, str)
    assert "FloatingMenu" not in [w.title for w in windows]
    assert parse_input_windows(API36, display_id=7) == NO_LIST
