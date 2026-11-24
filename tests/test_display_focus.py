"""Tests of the foreground and focus readers on a device with two displays.

The shapes follow a foldable at API 37: `dumpsys window` prints a block per
display, in no fixed order, each with its own ``mCurrentFocus`` and
``mFocusedApp``, and one ``mTopFocusedDisplayId`` further on. The values are
made up.
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
# What `dumpsys activity activities` prints on such a device: one
# mFocusedApp per display, one topResumedActivity and ResumedActivity.
ACTIVITY_DUMP = (
    f"  mFocusedApp={LAUNCHER_RECORD}\n"
    f"    ResumedActivity: {APP_RECORD}\n"
    f"  mFocusedApp={APP_RECORD}\n"
    f"  topResumedActivity={APP_RECORD}\n"
)


def make(fake_adb: FakeAdbServer, window: str) -> tuple[FakeAndroid, Device]:
    screens = (Screen("main", f"{APP}/.MainActivity", (El((0, 0, 9, 9), text="Hi"),)),)
    android = FakeAndroid(fake_adb, SERIAL, screens, start="main", api=36)
    android.override(WINDOW, window.encode())
    android.override(WINDOWS, APP_BLOCK.encode())
    android.override(ACTIVITIES, ACTIVITY_DUMP.encode())
    return android, Device(adb_server=fake_adb.address)


def read_activities(fake_adb: FakeAdbServer) -> bool:
    return any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_open_reads_display_0(fake_adb, newline):
    _, device = make(fake_adb, OPEN.replace("\n", newline))
    assert device.foreground() == APP
    assert device.focused_window() == APP_WINDOW
    assert not read_activities(fake_adb)


def test_open_with_display_0_first_and_display_1_unfocused_is_unknown(fake_adb):
    # Display 1's lines follow display 0's block and name another window.
    _, device = make(
        fake_adb,
        window_dump(
            display(0, APP_FOCUS, APP_RECORD), display(1, "null", LAUNCHER_RECORD)
        ),
    )
    assert device.foreground() is None
    assert device.focused_window() is None
    assert not read_activities(fake_adb)


def test_closed_is_unknown_without_fallback(fake_adb):
    _, device = make(fake_adb, CLOSED)
    assert device.foreground() is None
    assert device.focused_window() is None
    assert not read_activities(fake_adb)


FORGED_TAIL = (
    f"x}}\n  mFocusedApp={FORGED_RECORD}\n  Display: mDisplayId=7 (organized)\n  z"
)


@pytest.mark.parametrize(
    "window",
    [
        # A title on display 0 forges a focus line, then a header that would
        # end display 0's block before its genuine mFocusedApp line.
        window_dump(
            display(1, "null", LAUNCHER_RECORD),
            display(0, f"Window{{abc123 u0 {FORGED_TAIL}}}", APP_RECORD),
        ),
        # A title on display 1 forges a header of display 0 and its lines.
        window_dump(
            display(
                1,
                f"Window{{bcd234 u0 y}}\n  Display: mDisplayId=0 (organized)\n"
                f"  mCurrentFocus={APP_FOCUS}\n  mFocusedApp={FORGED_RECORD}\n  w",
                LAUNCHER_RECORD,
            ),
            display(0, APP_FOCUS, APP_RECORD),
        ),
        # Two blocks of display 0.
        window_dump(
            display(0, APP_FOCUS, APP_RECORD),
            display(1, "null", LAUNCHER_RECORD),
            display(0, APP_FOCUS, APP_RECORD),
        ),
        # No display 0.
        window_dump(
            display(1, "null", LAUNCHER_RECORD), display(2, APP_FOCUS, APP_RECORD)
        ),
        # A header claim of another shape.
        window_dump(
            display(1, "null", LAUNCHER_RECORD, "  Display: mDisplayId=1 (other)"),
            display(0, APP_FOCUS, APP_RECORD),
        ),
        window_dump(
            display(1, "null", LAUNCHER_RECORD, "  Display: mDisplayId=01"),
            display(0, APP_FOCUS, APP_RECORD),
        ),
        window_dump(
            display(1, "null", LAUNCHER_RECORD, "Display: mDisplayId=1"),
            display(0, APP_FOCUS, APP_RECORD),
        ),
        # mTopFocusedDisplayId: two that disagree, malformed, missing.
        window_dump(
            display(1, "null", LAUNCHER_RECORD),
            display(0, APP_FOCUS, APP_RECORD),
            top=("0", "1"),
        ),
        window_dump(
            display(1, "null", LAUNCHER_RECORD),
            display(0, APP_FOCUS, APP_RECORD),
            top=("1", "0"),
        ),
        window_dump(
            display(1, "null", LAUNCHER_RECORD),
            display(0, APP_FOCUS, APP_RECORD),
            top=("0", "0x"),
        ),
        window_dump(
            display(1, "null", LAUNCHER_RECORD),
            display(0, APP_FOCUS, APP_RECORD),
            top=("00",),
        ),
        window_dump(
            display(1, "null", LAUNCHER_RECORD),
            display(0, APP_FOCUS, APP_RECORD),
            top=(),
        ),
    ],
    ids=[
        "forged-header-in-display-0",
        "forged-display-0-in-display-1",
        "duplicate-display-0",
        "no-display-0",
        "header-other-suffix",
        "header-leading-zero",
        "header-no-indent",
        "top-disagrees",
        "top-disagrees-first",
        "top-malformed",
        "top-leading-zero",
        "top-missing",
    ],
)
def test_untrusted_two_display_dump_is_unknown(fake_adb, window):
    _, device = make(fake_adb, window)
    assert device.foreground() is None
    assert device.focused_window() is None
    assert not read_activities(fake_adb)


def test_a_forged_header_alone_cannot_hide_display_0s_lines(fake_adb):
    # The genuine lines after the forged header still count, and agree.
    title = "x}\n  Display: mDisplayId=7 (organized)\n  z"
    window = window_dump(
        display(1, "null", LAUNCHER_RECORD),
        display(0, f"Window{{abc123 u0 {title}}}", APP_RECORD),
    )
    _, device = make(fake_adb, window)
    assert device.foreground() == APP
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
    assert not read_activities(fake_adb)


# A dump without genuine display headers (older versions, the fakes): the
# genuine focus lines come first, then a window title forges two headers,
# display 0's lines and mTopFocusedDisplayId.
NO_HEADER_FORGED = (
    "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
    f"  mCurrentFocus={LAUNCHER_FOCUS}\n"
    f"  mFocusedApp={LAUNCHER_RECORD}\n"
    "  mInputMethodTarget=Window{1 u0 x}\n"
    "  Display: mDisplayId=1 (organized)\n"
    "  Display: mDisplayId=0 (organized)\n"
    f"  mCurrentFocus={APP_FOCUS}\n"
    f"  mFocusedApp={APP_RECORD}\n"
    "  mTopFocusedDisplayId=0\n"
    "  y}\n"
)


def test_headers_forged_after_the_genuine_focus_lines_give_unknown(fake_adb):
    _, device = make(fake_adb, NO_HEADER_FORGED)
    assert device.foreground() is None
    assert device.focused_window() is None
    assert not read_activities(fake_adb)


@pytest.mark.parametrize("app", ["null", None])
def test_display_0_without_an_app_is_unknown_without_fallback(fake_adb, app):
    # The activity dump's resumed activity names no display, so it is not
    # read: it can be the cover screen's.
    block = display(0, "null", "null")
    if app is None:
        block = block.replace("  mFocusedApp=null\n", "")
    _, device = make(fake_adb, window_dump(display(1, "null", LAUNCHER_RECORD), block))
    assert device.foreground() is None
    assert not read_activities(fake_adb)


def with_line(line: str, *, after: str = APP_FOCUS) -> str:
    """OPEN with `line` added after the line that holds `after`."""
    marker = f"  mCurrentFocus={after}\n"
    assert OPEN.count(marker) == 1
    return OPEN.replace(marker, marker + line + "\n")


@pytest.mark.parametrize(
    "window",
    [
        with_line("  mTopFocusedDisplayId = 1"),
        with_line("  mTopFocusedDisplayId=1 "),
        with_line("\tmTopFocusedDisplayId=0"),
        with_line("  Display:  mDisplayId=0"),
        with_line("Display:\tmDisplayId=0 (organized)"),
        with_line("  x Display: mDisplayId=0 (organized)"),
        with_line(f"\tmFocusedApp={FORGED_RECORD}"),
        with_line(f"  mFocusedApp = {FORGED_RECORD}"),
        with_line(f"   mFocusedApp={APP_RECORD}"),
        with_line(f"  mFocusedApp={APP_RECORD} "),
        with_line(f"\tmCurrentFocus={APP_FOCUS}"),
        with_line(f"  mCurrentFocus: {APP_FOCUS}"),
        with_line(f"  mCurrentFocus={APP_FOCUS} "),
        with_line("  mCurrentFocus=nul"),
        # A malformed claim on display 1 counts too.
        OPEN.replace(
            f"  mFocusedApp={LAUNCHER_RECORD}\n",
            f"  mFocusedApp={LAUNCHER_RECORD}\n\tmFocusedApp=x\n",
        ),
        # A focus line before the first header.
        f"  mCurrentFocus={APP_FOCUS}\n" + OPEN,
        f"  mFocusedApp={APP_RECORD}\n" + OPEN,
    ],
    ids=[
        "top-spaced",
        "top-trailing-space",
        "top-tab",
        "header-two-spaces",
        "header-tab",
        "header-mid-line",
        "app-tab",
        "app-spaced",
        "app-three-spaces",
        "app-trailing-space",
        "focus-tab",
        "focus-colon",
        "focus-trailing-space",
        "focus-not-null",
        "malformed-on-display-1",
        "focus-before-first-header",
        "app-before-first-header",
    ],
)
def test_malformed_claims_on_two_displays_give_unknown(fake_adb, window):
    _, device = make(fake_adb, window)
    assert device.foreground() is None
    assert device.focused_window() is None
    assert not read_activities(fake_adb)


def test_activity_dump_with_two_focused_apps_reads_the_resumed_activity(fake_adb):
    _, device = make(fake_adb, "WINDOW MANAGER WINDOWS (dumpsys window windows)\n")
    assert device.foreground() == APP
    assert read_activities(fake_adb)


@pytest.mark.parametrize(
    "header",
    [
        "  Display: mDisplayId=0 (organized)",
        "  Display: mDisplayId=0",
        # One header claim keeps today's reading, whatever its shape.
        "  Display: mDisplayId=0 rootTasks=2",
    ],
)
def test_one_display_block_reads_as_before(fake_adb, header):
    # No mTopFocusedDisplayId is needed, and lines outside the block count.
    window = (
        "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
        f"{header}\n"
        f"  mCurrentFocus={APP_FOCUS}\n"
        "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
        f"  mFocusedApp={APP_RECORD}\n"
    )
    android, device = make(fake_adb, window)
    assert device.foreground() == APP
    assert device.focused_window() == APP_WINDOW
    android.override(WINDOW, (window + f"  mFocusedApp={FORGED_RECORD}\n").encode())
    assert device.foreground() is None
    assert not read_activities(fake_adb)


# Dumps with no display header line of the narrow form (single-display
# devices, older versions, the fakes) read exactly as before display blocks
# were told apart: these results were checked against that earlier reader
# (a7e8052), and must not change.
GENUINE = (
    "WINDOW MANAGER WINDOWS (dumpsys window windows)\n"
    f"  mCurrentFocus={APP_FOCUS}\n"
    f"  mFocusedApp={APP_RECORD}\n"
)
TOKEN_RECORD = f"Token{{2 ActivityRecord{{3 u0 {APP}/.MainActivity t14}}}}"
AS_BEFORE = [
    (GENUINE, APP, APP_WINDOW),
    (GENUINE.replace("\n", "\r\n"), APP, APP_WINDOW),
    (GENUINE + f"\tmFocusedApp={FORGED_RECORD}\n", APP, APP_WINDOW),
    (GENUINE + f"   mFocusedApp={FORGED_RECORD}\n", APP, APP_WINDOW),
    (GENUINE + f"  mFocusedApp = {FORGED_RECORD}\n", APP, APP_WINDOW),
    (GENUINE + "  mTopFocusedDisplayId = 1\n", APP, APP_WINDOW),
    (GENUINE + "  mTopFocusedDisplayId=1\n", APP, APP_WINDOW),
    (GENUINE + "  Display:  mDisplayId=0\n", APP, APP_WINDOW),
    (GENUINE + "  Display:  mDisplayId=0\n  Display:  mDisplayId=1\n", APP, APP_WINDOW),
    (GENUINE + "  Display: mDisplayId=1 (organized)\n", APP, APP_WINDOW),
    (GENUINE + f"  mFocusedApp={FORGED_RECORD}\n", None, APP_WINDOW),
    (GENUINE + "  mCurrentFocus=Window{1 u0 y}\n", APP, None),
    (GENUINE + "\tmCurrentFocus=Window{1 u0 y}\n", APP, None),
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
    (NO_HEADER_FORGED.split("  Display:")[0], LAUNCHER, None),
]


@pytest.mark.parametrize(("window", "foreground", "focus"), AS_BEFORE)
def test_dumps_without_display_headers_read_as_before(
    fake_adb, window, foreground, focus
):
    _, device = make(fake_adb, window)
    assert device.foreground() == foreground
    assert device.focused_window() == focus
