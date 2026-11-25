"""Tests for the activity a dump reports and the forgeries it refuses."""

from __future__ import annotations

import re
import time

import pytest

from tests.device.core.helpers import (
    SERIAL,
    script_dump,
)
from tests.fake_adb_server import Fail
from uiprobe.device import Device


def test_dump_activity_falls_back_to_focused_app(phone):
    script_dump(
        phone,
        dumpsys=b"  mCurrentFocus=null\n  mFocusedApp=ActivityRecord{1 u0 "
        b"com.example.shop/.Main t12}\n",
    )
    assert Device(adb_server=phone.address).dump().activity == ".Main"


def test_dump_activity_of_another_package_is_not_accepted(phone):
    script_dump(
        phone,
        dumpsys=b"  mCurrentFocus=Window{1 u0 com.other/com.other.Main}\n",
    )
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_unknown(phone):
    script_dump(phone, dumpsys=b"nothing useful\n")
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_lookup_failure_is_not_fatal(phone):
    script_dump(phone, dumpsys=b"")
    phone._shell[SERIAL].insert(0, (re.compile(r"^dumpsys "), Fail("no dumpsys")))
    assert Device(adb_server=phone.address).dump().activity is None


def dumpsys_with_focus(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def test_dump_activity_of_another_package_in_relative_form_is_not_accepted(phone):
    # The shade has focus; the focused app is another package. It must not be
    # reported for the dumped screen, and `.MainActivity` read against the
    # dumped package would name a class that does not exist.
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  mCurrentFocus=Window{9 u0 NotificationShade}",
            "  mFocusedApp=ActivityRecord{1 u0 com.other.app/.MainActivity t3}",
        ),
    )
    screen = Device(adb_server=phone.address).dump()
    assert screen.package == "com.example.shop"
    assert screen.activity is None


def test_dump_activity_in_the_dumped_package_stays_short(phone):
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  mCurrentFocus=Window{9 u0 Popup}",
            "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3}",
        ),
    )
    assert Device(adb_server=phone.address).dump().activity == ".Main"


REAL_APP = "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Real t3}"


FORGED_FOCUS = "  mCurrentFocus=Window{1 u0 com.example.shop/com.example.shop.Fake}"


FORGED_APP = "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Fake t3}"


def title_window(title: str) -> str:
    return f"    Window #1 Window{{7 u0 {title}}}:"


def activity_of(phone, *lines: str) -> str | None:
    script_dump(phone, dumpsys=dumpsys_with_focus(*lines))
    return Device(adb_server=phone.address).dump().activity


def test_dump_activity_prefers_the_system_record_over_a_window_title(phone):
    # The focused window's own title is shaped like a component.
    title = "com.example.shop/com.example.shop.Fake"
    assert (
        activity_of(phone, f"  mCurrentFocus=Window{{4f2a1c u0 {title}}}", REAL_APP)
        == ".Real"
    )


@pytest.mark.parametrize(
    "focus", ["  mCurrentFocus=Window{9 u0 PopupWindow:3f2a}", "  mCurrentFocus=null"]
)
def test_dump_activity_ignores_a_focus_line_forged_by_another_window(phone, focus):
    forged = f"x\n{FORGED_FOCUS}\n"
    assert activity_of(phone, title_window(forged), focus, REAL_APP) == ".Real"


@pytest.mark.parametrize(
    "focus", ["  mCurrentFocus=Window{9 u0 PopupWindow:3f2a}", "  mCurrentFocus=null"]
)
def test_dump_activity_with_a_forged_record_line_is_never_the_forgery(phone, focus):
    forged = f"x\n{FORGED_FOCUS}\n{FORGED_APP}\n"
    assert activity_of(phone, title_window(forged), focus, REAL_APP) is None


def test_dump_activity_does_not_hand_a_forged_record_over_to_the_window_title(phone):
    forged = f"x\n{FORGED_FOCUS}\n{FORGED_APP}\n"
    assert activity_of(phone, title_window(forged), REAL_APP) is None


REAL_FOCUS = "  mCurrentFocus=Window{4f2a1c u0 com.example.shop/com.example.shop.Real}"


def test_dump_activity_never_comes_from_a_window_title(phone):
    assert activity_of(phone, REAL_FOCUS) is None


@pytest.mark.parametrize(
    "app_line",
    [
        "  mFocusedApp=null",
        "  mFocusedApp=SomethingNew{1 u0 com.example.shop/.Fake t3}",
        None,
    ],
    ids=["null", "unrecognized", "absent"],
)
class TestForgedTitleWithoutAFocusedApp:
    def _lines(self, app_line):
        return [FORGED_FOCUS] + ([app_line] if app_line else [])

    def test_the_activity_manager_is_asked_and_wins(self, phone, app_line):
        script_dump(phone, dumpsys=dumpsys_with_focus(*self._lines(app_line)))
        phone.on_shell(
            SERIAL,
            "dumpsys activity activities",
            dumpsys_with_focus(
                "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Real t5}"
            ),
        )
        assert Device(adb_server=phone.address).dump().activity == ".Real"
        assert "shell:dumpsys activity activities" in phone.requests

    def test_no_usable_manager_record_gives_none(self, phone, app_line):
        script_dump(phone, dumpsys=dumpsys_with_focus(*self._lines(app_line)))
        phone.on_shell(
            SERIAL,
            "dumpsys activity activities",
            dumpsys_with_focus("  mResumedActivity: null"),
        )
        assert Device(adb_server=phone.address).dump().activity is None
        assert "shell:dumpsys activity activities" in phone.requests


@pytest.mark.parametrize(
    "line",
    [
        "mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3}",
        "    mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Ma in t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Ma\u202ein t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/ t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3} x",
    ],
)
def test_dump_activity_needs_a_well_formed_line(phone, line):
    script_dump(phone, dumpsys=dumpsys_with_focus(line))
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_mismatch_between_focus_and_dumped_package(phone):
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  mCurrentFocus=Window{1 u0 com.bank/com.bank.Login}",
            "  mFocusedApp=ActivityRecord{1 u0 com.bank/.Login t3}",
        ),
    )
    assert Device(adb_server=phone.address).dump().activity is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "  mFocusedApp=ActivityRecord{110055750 u0 "
            "com.example.shop/.NexusLauncherActivity t2}",
            ".NexusLauncherActivity",
        ),
        (
            "  mFocusedApp=AppWindowToken{59b0789 token=Token{527f542 "
            "ActivityRecord{c10ff8d u0 com.example.shop/.Launcher t9}}}",
            ".Launcher",
        ),
    ],
)
def test_dump_activity_real_output_shapes(phone, line, expected):
    script_dump(phone, dumpsys=dumpsys_with_focus(line))
    assert Device(adb_server=phone.address).dump().activity == expected


def manager_activity(phone, *lines: str) -> str | None:
    script_dump(phone, dumpsys=dumpsys_with_focus("  mCurrentFocus=null"))
    phone.on_shell(SERIAL, "dumpsys activity activities", dumpsys_with_focus(*lines))
    return Device(adb_server=phone.address).dump().activity


REAL_RESUMED = (
    "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Main t5}",
    "    mResumedActivity: ActivityRecord{c1 u0 com.example.shop/.Main t5}",
)


def test_dump_activity_falls_back_to_the_activity_manager(phone):
    assert manager_activity(phone, *REAL_RESUMED) == ".Main"


def test_dump_activity_manager_of_another_package_is_not_accepted(phone):
    line = "  topResumedActivity=ActivityRecord{c1 u0 com.bank/.Login t5}"
    assert manager_activity(phone, line) is None


def test_dump_activity_manager_lines_that_disagree_are_not_accepted(phone):
    forged = "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Fake t5}"
    assert manager_activity(phone, forged, *REAL_RESUMED) is None


def test_dump_activity_manager_forgery_on_a_null_line_is_not_accepted(phone):
    forged = "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Fake t5}"
    assert manager_activity(phone, forged, "    mResumedActivity: null") is None


def test_dump_activity_never_hands_an_ambiguous_window_over_to_the_manager(phone):
    forged = f"x\n{FORGED_FOCUS}\n{FORGED_APP}\n"
    script_dump(phone, dumpsys=dumpsys_with_focus(title_window(forged), REAL_APP))
    phone.on_shell(
        SERIAL, "dumpsys activity activities", dumpsys_with_focus(*REAL_RESUMED)
    )
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_lines_must_start_with_the_field(phone):
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  title mCurrentFocus=Window{1 u0 com.bank/com.bank.Login}",
            "  see mFocusedApp=ActivityRecord{1 u0 com.bank/.Login t3}",
        ),
    )
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_search_is_fast_on_a_hostile_title(phone):
    hostile = "  mCurrentFocus=Window{" + " a" * 300_000
    script_dump(
        phone,
        dumpsys=(hostile + "\n  mFocusedApp=" + " b" * 300_000 + "\n").encode(),
    )
    started = time.monotonic()
    assert Device(adb_server=phone.address).dump().activity is None
    assert time.monotonic() - started < 2


def test_dump_api_level_failure_does_not_lose_the_screen(phone):
    script_dump(phone)
    phone._shell[SERIAL].insert(
        0, ("getprop ro.build.version.sdk", Fail("getprop refused"))
    )
    screen = Device(adb_server=phone.address).dump()
    assert screen.api_level is None
    assert screen.package == "com.example.shop"
    assert screen.to_dict()["device"] == {"serial": SERIAL}
