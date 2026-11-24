"""Tests of the foreground and focus readers on a device with two displays.

The shapes follow a foldable at API 37: `dumpsys window` prints a block per
display, in no fixed order, each with its own ``mCurrentFocus`` and
``mFocusedApp``, and one ``mTopFocusedDisplayId`` further on; `dumpsys
activity activities` prints ``mFocusedApp`` once per display but
``topResumedActivity`` and ``ResumedActivity`` once each. The values are made
up.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import El, FakeAndroid, Screen
from uiprobe.device import Device, FocusedWindow

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
APP = "com.example.app"
LAUNCHER = "com.example.launcher"
WINDOW = re.compile(r"^dumpsys window\Z")
WINDOWS = re.compile(r"^dumpsys window windows\Z")
ACTIVITIES = re.compile(r"^dumpsys activity activities\Z")

APP_TITLE = f"{APP}/{APP}.MainActivity"
APP_FOCUS = f"Window{{abc123 u0 {APP_TITLE}}}"
APP_RECORD = f"ActivityRecord{{def456 u0 {APP}/.MainActivity t14}}"
LAUNCHER_FOCUS = f"Window{{bcd234 u0 {LAUNCHER}/{LAUNCHER}.Home}}"
LAUNCHER_RECORD = f"ActivityRecord{{fed654 u0 {LAUNCHER}/.Home t4}}"
FORGED_RECORD = "ActivityRecord{1 u0 com.example.forged/.Fake t3}"


def display(number: int, focus: str, app: str, header: str | None = None) -> str:
    """One display's block of DISPLAY CONTENTS."""
    if header is None:
        header = f"  Display: mDisplayId={number} (organized)"
    return (
        f"{header}\n"
        "    init=1080x2400 420dpi cur=1080x2400 app=1080x2400 rng=1x1-1x1\n"
        f"  mCurrentFocus={focus}\n"
        f"  mFocusedApp={app}\n"
    )


def window_dump(*blocks: str, top: tuple[str, ...] = ("0",)) -> str:
    """`dumpsys window` with these display blocks; one top line per entry."""
    tops = "".join(f"  mTopFocusedDisplayId={value}\n" for value in top)
    return (
        "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
        + "".join(blocks)
        + "-" * 79
        + "\n"
        "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
        f"{tops}"
        "  mInTouchMode=true\n"
    )


# Open: the cover screen (display 1, off) first, the app on display 0.
OPEN = window_dump(
    display(1, "null", LAUNCHER_RECORD), display(0, APP_FOCUS, APP_RECORD)
)
# Closed: display 0 first, without focus; the cover screen has it.
CLOSED = window_dump(
    display(0, "null", APP_RECORD),
    display(1, LAUNCHER_FOCUS, LAUNCHER_RECORD),
    top=("1",),
)
APP_BLOCK = (
    f"  Window #3 Window{{abc123 u0 {APP_TITLE}}}:\n"
    "    mDisplayId=0 rootTaskId=14 mSession=Session{1 2:u0a150} mClient=x\n"
    f"    mOwnerUid=10150 showForAllUsers=false package={APP} appop=NONE\n"
)
APP_WINDOW = FocusedWindow(APP, APP_TITLE, "abc123", 0, 10150)
# The cover screen's launcher window has its own block and owner.
LAUNCHER_TITLE = f"{LAUNCHER}/{LAUNCHER}.Home"
LAUNCHER_BLOCK = (
    f"  Window #1 Window{{bcd234 u0 {LAUNCHER_TITLE}}}:\n"
    "    mDisplayId=1 rootTaskId=4 mSession=Session{3 4:u0a120} mClient=y\n"
    f"    mOwnerUid=10120 showForAllUsers=false package={LAUNCHER} appop=NONE\n"
)
LAUNCHER_WINDOW = FocusedWindow(LAUNCHER, LAUNCHER_TITLE, "bcd234", 0, 10120)
# Another package, named only by the activity dump.
OTHER = "com.example.other"
OTHER_RECORD = f"ActivityRecord{{aaa111 u0 {OTHER}/.Main t20}}"
# What `dumpsys activity activities` prints on such a device: one
# mFocusedApp per display, one topResumedActivity and ResumedActivity.
ACTIVITY_DUMP = (
    f"  mFocusedApp={LAUNCHER_RECORD}\n"
    f"    ResumedActivity: {APP_RECORD}\n"
    f"  mFocusedApp={APP_RECORD}\n"
    f"  topResumedActivity={APP_RECORD}\n"
)


def make(
    fake_adb: FakeAdbServer, window: str, activities: str = ACTIVITY_DUMP
) -> tuple[FakeAndroid, Device]:
    screens = (Screen("main", f"{APP}/.MainActivity", (El((0, 0, 9, 9), text="Hi"),)),)
    android = FakeAndroid(fake_adb, SERIAL, screens, start="main", api=36)
    android.override(WINDOW, window.encode())
    android.override(WINDOWS, (LAUNCHER_BLOCK + APP_BLOCK).encode())
    android.override(ACTIVITIES, activities.encode())
    return android, Device(adb_server=fake_adb.address)


def read_activities(fake_adb: FakeAdbServer) -> bool:
    return any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)


def resumed(record: str, top: str | None = None) -> str:
    """An activity dump: two mFocusedApp lines, one of each resumed line."""
    return (
        f"  mFocusedApp={LAUNCHER_RECORD}\n"
        f"    ResumedActivity: {record}\n"
        f"  mFocusedApp={APP_RECORD}\n"
        f"  topResumedActivity={record if top is None else top}\n"
    )


# --- several displays: what is read ---


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_open_reads_the_resumed_activity_and_the_one_focus(fake_adb, newline):
    _, device = make(fake_adb, OPEN.replace("\n", newline))
    assert device.foreground() == APP
    assert device.focused_window() == APP_WINDOW
    assert read_activities(fake_adb)


@pytest.mark.parametrize(
    "window",
    [
        # Display 0 first, and without mFocusedApp: display 1's is not used.
        window_dump(
            display(0, APP_FOCUS, APP_RECORD).replace(
                f"  mFocusedApp={APP_RECORD}\n", ""
            ),
            display(1, "null", LAUNCHER_RECORD),
        ),
        # mFocusedApp of display 0 is not used either.
        window_dump(
            display(1, "null", LAUNCHER_RECORD), display(0, APP_FOCUS, FORGED_RECORD)
        ),
        window_dump(display(1, "null", LAUNCHER_RECORD), display(0, APP_FOCUS, "null")),
    ],
    ids=["display-0-first-without-app", "display-0-app-other", "display-0-app-null"],
)
def test_focused_app_lines_are_not_used(fake_adb, window):
    _, device = make(fake_adb, window)
    assert device.foreground() == APP
    assert device.focused_window() == APP_WINDOW


@pytest.mark.parametrize(
    "activities",
    [
        resumed(APP_RECORD, top=LAUNCHER_RECORD),
        resumed(LAUNCHER_RECORD, top=APP_RECORD),
        f"  mFocusedApp={APP_RECORD}\n",
        "",
        resumed(APP_RECORD) + f"  topResumedActivity={FORGED_RECORD}\n",
        resumed(APP_RECORD) + "  topResumedActivity=ActivityRecord{1 u0 bad t3}\n",
    ],
    ids=[
        "top-differs",
        "resumed-differs",
        "only-focused-app",
        "empty",
        "forged",
        "bad",
    ],
)
def test_resumed_activity_absent_or_disagreeing_is_unknown(fake_adb, activities):
    _, device = make(fake_adb, OPEN, activities)
    assert device.foreground() is None
    assert device.focused_window() == APP_WINDOW


def test_both_screens_with_focus_give_an_unknown_focus(fake_adb):
    # Two windows hold focus, one per display: which one has input is not
    # told. The foreground still comes from the resumed activity.
    window = window_dump(
        display(1, LAUNCHER_FOCUS, LAUNCHER_RECORD), display(0, APP_FOCUS, APP_RECORD)
    )
    _, device = make(fake_adb, window)
    assert device.focused_window() is None
    assert device.foreground() == APP


def test_closed_is_unknown(fake_adb):
    _, device = make(fake_adb, CLOSED)
    assert device.foreground() is None
    assert device.focused_window() is None
    assert not read_activities(fake_adb)


def test_only_the_top_display_line_makes_it_unknown(fake_adb):
    # The same dump reads display 0 while mTopFocusedDisplayId says 0.
    blocks = (display(1, "null", LAUNCHER_RECORD), display(0, APP_FOCUS, APP_RECORD))
    android, device = make(fake_adb, window_dump(*blocks))
    assert device.foreground() == APP
    assert device.focused_window() == APP_WINDOW
    for top in ("1", "-1", "2"):
        android.override(WINDOW, window_dump(*blocks, top=(top,)).encode())
        assert device.foreground() is None
        assert device.focused_window() is None


@pytest.mark.parametrize(
    "window",
    [
        window_dump(
            *(display(1, "null", LAUNCHER_RECORD), display(0, APP_FOCUS, APP_RECORD)),
            top=top,
        )
        for top in [
            ("0", "1"),
            ("1", "0"),
            ("0 ",),
            ("00",),
            ("0x",),
            (" 0",),
            (),
        ]
    ]
    + [
        OPEN.replace("  mTopFocusedDisplayId=0", "  mTopFocusedDisplayId = 0"),
        OPEN.replace("  mTopFocusedDisplayId=0", "\tmTopFocusedDisplayId=0"),
        OPEN.replace("  mTopFocusedDisplayId=0", "  mTopFocusedDisplayId: 0"),
        OPEN + "  see mTopFocusedDisplayId=0\n",
        # An exact header and a spaced one, with top 1, take this path too.
        "  Display: mDisplayId=0 (organized)\n"
        f"  mCurrentFocus={APP_FOCUS}\n"
        f"  mFocusedApp={APP_RECORD}\n"
        "  Display:  mDisplayId=1\n"
        "  mCurrentFocus=null\n"
        "  mTopFocusedDisplayId=1\n",
    ],
    ids=[
        "top-disagrees",
        "top-disagrees-first",
        "top-trailing-space",
        "top-leading-zero",
        "top-hex",
        "top-three-spaces",
        "top-missing",
        "top-spaced",
        "top-tab",
        "top-colon",
        "top-mid-line",
        "spaced-header-top-1",
    ],
)
def test_malformed_or_missing_top_line_is_unknown(fake_adb, window):
    _, device = make(fake_adb, window)
    assert device.foreground() is None
    assert device.focused_window() is None
    assert not read_activities(fake_adb)


@pytest.mark.parametrize(
    "line",
    [
        f"\tmCurrentFocus={APP_FOCUS}",
        f"  mCurrentFocus = {APP_FOCUS}",
        f"  mCurrentFocus: {APP_FOCUS}",
        f"   mCurrentFocus={APP_FOCUS}",
        f"  mCurrentFocus={APP_FOCUS} ",
        "  mCurrentFocus=nul",
        "  mCurrentFocus=null ",
        "\tmCurrentFocus=null",
        "  x mCurrentFocus=Window{1 u0 y}",
    ],
)
def test_a_malformed_focus_claim_gives_an_unknown_focus(fake_adb, line):
    _, device = make(fake_adb, OPEN + line + "\n")
    assert device.focused_window() is None


# --- several displays: forged lines never give a forged answer ---

FORGED_TAIL = (
    f"x}}\n  mFocusedApp={FORGED_RECORD}\n  Display: mDisplayId=7 (organized)\n  z"
)
# A dump without genuine headers: the launcher is in front, and window titles
# on both sides of its focus lines forge headers, a focus and top 0.
BOTH_SIDES = (
    "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
    "  Window #4 Window{1 u0 a}\n"
    "  Display: mDisplayId=1 (organized)\n"
    "  b}:\n"
    f"  mCurrentFocus={LAUNCHER_FOCUS}\n"
    f"  mFocusedApp={LAUNCHER_RECORD}\n"
    "  mInputMethodTarget=Window{2 u0 c}\n"
    "  Display: mDisplayId=0 (organized)\n"
    f"  mCurrentFocus={APP_FOCUS}\n"
    f"  mFocusedApp={APP_RECORD}\n"
    "  mTopFocusedDisplayId=0\n"
    "  d}\n"
)


@pytest.mark.parametrize(
    ("window", "activities"),
    [
        # A title on display 0 forges a focus line and a header.
        (
            window_dump(
                display(1, "null", LAUNCHER_RECORD),
                display(0, f"Window{{abc123 u0 {FORGED_TAIL}}}", APP_RECORD),
            ),
            ACTIVITY_DUMP,
        ),
        # A title on display 1 forges a header of display 0 and its lines.
        (
            window_dump(
                display(
                    1,
                    f"Window{{bcd234 u0 y}}\n  Display: mDisplayId=0 (organized)\n"
                    f"  mCurrentFocus=Window{{ffff u0 {APP_TITLE}}}\n"
                    f"  mFocusedApp={FORGED_RECORD}\n  w",
                    LAUNCHER_RECORD,
                ),
                display(0, APP_FOCUS, APP_RECORD),
            ),
            ACTIVITY_DUMP,
        ),
        # Two blocks of display 0, and extra headers.
        (
            window_dump(
                display(0, APP_FOCUS, APP_RECORD),
                display(1, "null", LAUNCHER_RECORD),
                display(0, APP_FOCUS, FORGED_RECORD),
                display(7, "null", FORGED_RECORD),
            ),
            ACTIVITY_DUMP,
        ),
        # Forged top 0 on a closed phone.
        (CLOSED + "  mTopFocusedDisplayId=0\n", ACTIVITY_DUMP),
        # Forged headers on both sides of the genuine focus lines.
        (BOTH_SIDES, resumed(LAUNCHER_RECORD)),
        # Forged top 0 in a dump without headers: the genuine lines count.
        (
            f"  mCurrentFocus={LAUNCHER_FOCUS}\n  mFocusedApp={LAUNCHER_RECORD}\n"
            f"  mTopFocusedDisplayId=0\n  mCurrentFocus={APP_FOCUS}\n",
            resumed(LAUNCHER_RECORD),
        ),
        (
            f"  mCurrentFocus={LAUNCHER_FOCUS}\n  mFocusedApp={LAUNCHER_RECORD}\n"
            f"  mTopFocusedDisplayId=0\n  mFocusedApp={APP_RECORD}\n",
            resumed(LAUNCHER_RECORD),
        ),
    ],
    ids=[
        "forged-header-in-display-0",
        "forged-display-0-in-display-1",
        "extra-headers",
        "forged-top-on-closed",
        "headers-on-both-sides",
        "forged-top-focus-no-headers",
        "forged-top-app-no-headers",
    ],
)
def test_forged_lines_never_give_a_forged_answer(fake_adb, window, activities):
    _, device = make(fake_adb, window, activities)
    genuine = LAUNCHER if LAUNCHER_RECORD in activities.split("\n")[1] else APP
    assert device.foreground() in {genuine, None}
    focus = device.focused_window()
    assert focus is None or focus.owner == genuine


def test_headers_on_both_sides_give_the_genuine_foreground(fake_adb):
    _, device = make(fake_adb, BOTH_SIDES, resumed(LAUNCHER_RECORD))
    assert device.foreground() == LAUNCHER
    assert device.focused_window() is None


# --- the focused app record ---


@pytest.mark.parametrize(
    "line",
    [
        f"  mFocusedApp={APP_RECORD}}}",
        f"  mFocusedApp=AppWindowToken{{1 token=Token{{2 {APP_RECORD}}}",
        f"  mFocusedApp=AppWindowToken{{1 token=Token{{2 {APP_RECORD}}}}}}}",
    ],
    ids=["record-extra-brace", "token-one-brace-short", "token-extra-brace"],
)
def test_a_focused_app_record_with_wrong_braces_is_not_read(fake_adb, line):
    _, device = make(fake_adb, line + "\n", "nothing\n")
    assert device.foreground() is None


# Dumps of a single display (no or one header line, and no
# mTopFocusedDisplayId line or only exact ``  mTopFocusedDisplayId=0`` ones,
# as API 24 and API 37 emulators print) read exactly as before display blocks
# were told apart: these results were checked against that earlier reader
# (a7e8052), and must not change.
GENUINE = (
    "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
    f"  mCurrentFocus={APP_FOCUS}\n"
    f"  mFocusedApp={APP_RECORD}\n"
)
# A single display at API 37: one header and the exact top line.
SINGLE = (
    "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
    "  Display: mDisplayId=0 (organized)\n"
    f"  mCurrentFocus={APP_FOCUS}\n"
    f"  mFocusedApp={APP_RECORD}\n"
    "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
    "  mTopFocusedDisplayId=0\n"
)
TOKEN_RECORD = f"Token{{2 ActivityRecord{{3 u0 {APP}/.MainActivity t14}}}}"
AS_BEFORE = [
    (GENUINE, APP, APP_WINDOW),
    (GENUINE.replace("\n", "\r\n"), APP, APP_WINDOW),
    (GENUINE + f"\tmFocusedApp={FORGED_RECORD}\n", APP, APP_WINDOW),
    (GENUINE + f"   mFocusedApp={FORGED_RECORD}\n", APP, APP_WINDOW),
    (GENUINE + f"  mFocusedApp = {FORGED_RECORD}\n", APP, APP_WINDOW),
    (GENUINE + f"mFocusedApp={FORGED_RECORD}\n", APP, APP_WINDOW),
    (GENUINE + "  Window #2 Window{1 u0 mCurrentFocus=x}:\n", APP, APP_WINDOW),
    (GENUINE + "  Display:  mDisplayId=0\n", APP, APP_WINDOW),
    (GENUINE + "  Display: mDisplayId=1 (organized)\n", APP, APP_WINDOW),
    (GENUINE + f"  mFocusedApp={FORGED_RECORD}\n", None, APP_WINDOW),
    (GENUINE + "  mCurrentFocus=Window{1 u0 y}\n", APP, None),
    (GENUINE + "\tmCurrentFocus=Window{1 u0 y}\n", APP, None),
    (GENUINE + "  mCurrentFocus=null\n", APP, None),
    (GENUINE + "  mFocusedApp=null\n", None, APP_WINDOW),
    ("", APP, None),
    ("  mFocusedApp=null\n", APP, None),
    (f"  mCurrentFocus={APP_FOCUS}\n  mFocusedApp=null\n", APP, APP_WINDOW),
    (f"  mCurrentFocus=null\n  mFocusedApp={APP_RECORD}\n", APP, None),
    (
        f"  mCurrentFocus={APP_FOCUS}\n"
        f"  mFocusedApp=AppWindowToken{{1 token={TOKEN_RECORD}}}\n"
        f"    mFocusedApp={TOKEN_RECORD}\n",
        APP,
        APP_WINDOW,
    ),
    (BOTH_SIDES.split("  mInputMethodTarget")[0], LAUNCHER, LAUNCHER_WINDOW),
    (SINGLE, APP, APP_WINDOW),
    (SINGLE.replace("\n", "\r\n"), APP, APP_WINDOW),
    (SINGLE + "  mTopFocusedDisplayId=0\n", APP, APP_WINDOW),
    (SINGLE + f"  mFocusedApp={FORGED_RECORD}\n", None, APP_WINDOW),
    (SINGLE + "  mCurrentFocus=Window{1 u0 y}\n", APP, None),
    (SINGLE + "  mCurrentFocus=null\n", APP, None),
    (SINGLE.replace(f"  mFocusedApp={APP_RECORD}\n", ""), APP, APP_WINDOW),
]


@pytest.mark.parametrize(("window", "foreground", "focus"), AS_BEFORE)
def test_dumps_without_display_headers_read_as_before(
    fake_adb, window, foreground, focus
):
    _, device = make(fake_adb, window)
    assert device.foreground() == foreground
    assert device.focused_window() == focus


FORGED_FOCUS = "Window{ffff u0 com.example.forged/com.example.forged.Fake}"


@pytest.mark.parametrize(
    "forged",
    [
        "  Display: mDisplayId=1 (organized)\n"
        f"  mCurrentFocus={FORGED_FOCUS}\n"
        f"  mFocusedApp={FORGED_RECORD}\n",
        "  Display: mDisplayId=0 (organized)\n"
        f"  mCurrentFocus={FORGED_FOCUS}\n"
        f"  mFocusedApp={FORGED_RECORD}\n",
        "  Display: mDisplayId=1 (organized)\n  mCurrentFocus=null\n",
        f"  mTopFocusedDisplayId=0 \n  mFocusedApp={FORGED_RECORD}\n",
        f"  mTopFocusedDisplayId=1\n  mCurrentFocus={FORGED_FOCUS}\n",
    ],
    ids=["header-1-and-focus", "header-0-and-focus", "header-only", "top-bad", "top-1"],
)
def test_forgeries_on_a_single_display_never_give_a_forged_answer(fake_adb, forged):
    # SINGLE and the activity dump name the app; the forged lines another.
    android, device = make(fake_adb, SINGLE + forged)
    assert device.foreground() in {APP, None}
    assert device.focused_window() in {APP_WINDOW, None}
    # They take the reading of several displays: mFocusedApp is not used.
    android.override(ACTIVITIES, b"nothing\n")
    assert device.foreground() is None


# --- the stricter reading of round D ---


def test_the_foreground_comes_from_the_activity_dump_alone(fake_adb):
    # The window dump names the app on display 0; the activity dump another
    # package, which is what is read.
    _, device = make(fake_adb, OPEN, resumed(OTHER_RECORD))
    assert device.foreground() == OTHER
    assert device.focused_window() == APP_WINDOW


def test_the_owner_block_of_each_display_is_told_apart(fake_adb):
    # One display: the launcher window's own block gives its owner. Two
    # displays with top 0: the same window, on display 1, is not read.
    android, device = make(
        fake_adb, f"  mCurrentFocus={LAUNCHER_FOCUS}\n  mFocusedApp={LAUNCHER_RECORD}\n"
    )
    assert device.focused_window() == LAUNCHER_WINDOW
    two = window_dump(
        display(1, LAUNCHER_FOCUS, LAUNCHER_RECORD), display(0, "null", APP_RECORD)
    )
    android.override(WINDOW, two.encode())
    assert device.focused_window() is None


@pytest.mark.parametrize(
    "window",
    [
        # All focus lines null.
        window_dump(
            display(1, "null", LAUNCHER_RECORD), display(0, "null", APP_RECORD)
        ),
        # Top 0, display 0 null, display 1 focused (the reversed case).
        window_dump(
            display(0, "null", APP_RECORD), display(1, LAUNCHER_FOCUS, LAUNCHER_RECORD)
        ),
        # All genuine lines null and one forged focus line alone.
        window_dump(display(1, "null", LAUNCHER_RECORD), display(0, "null", APP_RECORD))
        + f"  mCurrentFocus={APP_FOCUS}\n",
        # A forged focus line beside the genuine one, even naming it again.
        OPEN + f"  mCurrentFocus={APP_FOCUS}\n",
        OPEN + "  mCurrentFocus=Window{ffff u0 x}\n",
        # A forged null line, or a forged header alone: the counts differ.
        OPEN + "  mCurrentFocus=null\n",
        OPEN + "  Display: mDisplayId=5 (organized)\n",
        # A forged header with a forged focus line: two windows.
        OPEN
        + "  Display: mDisplayId=5 (organized)\n  mCurrentFocus=Window{ffff u0 x}\n",
        # One CR too many.
        OPEN.replace("\n", "\r\r\n"),
    ],
    ids=[
        "all-null",
        "reversed-top-0",
        "forged-sole-claim",
        "forged-same-claim",
        "forged-other-claim",
        "forged-null",
        "forged-header",
        "forged-pair-beside-genuine",
        "double-cr",
    ],
)
def test_focus_outside_the_seen_shape_is_unknown(fake_adb, window):
    _, device = make(fake_adb, window)
    assert device.focused_window() is None


def test_a_forged_header_and_null_pair_keeps_the_genuine_focus(fake_adb):
    window = OPEN + "  Display: mDisplayId=5 (organized)\n  mCurrentFocus=null\n"
    _, device = make(fake_adb, window)
    assert device.focused_window() == APP_WINDOW


def test_known_limit_a_forged_pair_where_no_display_has_focus(fake_adb):
    # Documented limit: when no display has a focused window, a title that
    # forges a header and a focus line as a pair is read as the focus. Taps
    # stay guarded by the check of the window at the tap point; a BACK would
    # go to whichever window really has the focus (none here).
    window = window_dump(
        display(1, "null", LAUNCHER_RECORD), display(0, "null", APP_RECORD)
    ) + (f"  Display: mDisplayId=5 (organized)\n  mCurrentFocus={APP_FOCUS}\n")
    _, device = make(fake_adb, window)
    assert device.focused_window() == APP_WINDOW


@pytest.mark.parametrize(
    "activities",
    [
        f"  mResumedActivity: {APP_RECORD}\n",
        f"  topResumedActivity={APP_RECORD}\n",
        f"    ResumedActivity: {APP_RECORD}\n",
        f"  mResumedActivity: {APP_RECORD}\n  topResumedActivity={APP_RECORD}\n",
        resumed(APP_RECORD) + f"    ResumedActivity : {OTHER_RECORD}\n",
        resumed(APP_RECORD) + f"\ttopResumedActivity={APP_RECORD}\n",
        resumed(APP_RECORD) + f"  mResumedActivity: {OTHER_RECORD}\n",
        resumed(APP_RECORD).replace("\n", "\r\r\n"),
        resumed(APP_RECORD).replace(" t14}", " t14}}"),
    ],
    ids=[
        "m-resumed-only",
        "top-only",
        "resumed-only",
        "m-resumed-and-top",
        "spaced-resumed-disagrees",
        "tab-indented",
        "m-resumed-disagrees",
        "double-cr",
        "extra-brace",
    ],
)
def test_resumed_lines_outside_the_seen_shape_give_unknown(fake_adb, activities):
    _, device = make(fake_adb, OPEN, activities)
    assert device.foreground() is None


def test_a_spaced_resumed_line_that_agrees_is_still_not_exact(fake_adb):
    activities = resumed(APP_RECORD) + f"    ResumedActivity : {APP_RECORD}\n"
    _, device = make(fake_adb, OPEN, activities)
    assert device.foreground() is None  # not the exact shape


def test_launch_says_when_another_display_has_the_focus(fake_adb):
    android, device = make(fake_adb, CLOSED)
    android.override(
        re.compile(r"^cmd package resolve-activity .*"),
        f"{APP}/.MainActivity\nuiprobe-exit:0\n".encode(),
    )
    with pytest.raises(Exception) as caught:  # noqa: PT011 - checked below
        device.launch(APP, timeout=0)
    assert "another display, perhaps the cover screen, has the focus" in str(
        caught.value
    )


# --- a crawl on two displays ---

# Added to the fake's `dumpsys window`: a second display without focus.
SECOND_DISPLAY = (
    "  Display: mDisplayId=0 (organized)\n"
    "  Display: mDisplayId=1 (organized)\n"
    "  mCurrentFocus=null\n"
    "  mTopFocusedDisplayId=0\n"
)


@pytest.mark.parametrize("check", [True, False], ids=["checked", "unchecked"])
def test_a_crawl_on_two_displays_needs_the_tap_window_check(tmp_path, fake_adb, check):
    from tests.test_crawl_driver import SCREENS, TRANSPORT, Clock, lines, run
    from uiprobe.crawl import Limits
    from uiprobe.guards import Policy

    android = FakeAndroid(
        fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT, api=36
    )
    android.extra_dumpsys["window"] = SECOND_DISPLAY
    device = Device(adb_server=fake_adb.address, pin=True)
    out = tmp_path / "run"
    run(
        device,
        Clock(),
        out,
        limits=Limits(actions=4),
        policy=Policy(check_tap_window=check),
    )
    records = lines(out / "transitions.jsonl")
    refused = [r for r in records if r.get("reason") == "focus_unknown"]
    if check:
        assert [i for i in android.inputs if i.kind == "tap"]
        assert not refused
    else:
        assert android.inputs == []
        assert refused
        assert refused[0]["details"] == {
            "cause": "several_displays_need_tap_window_check"
        }
