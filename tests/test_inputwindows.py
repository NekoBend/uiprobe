"""Tests for reading the window a touch reaches from ``dumpsys input``.

The data files are lines copied unchanged from emulators, in their order, with
lines left out but none changed or renumbered:

* api24.txt and api36.txt: excerpts of a whole ``dumpsys input`` (the reader's
  ``Viewport`` lines of display 0 and the dispatcher's part down to the end
  of the window list).
* api30.txt to api35.txt: the window list (``Windows:`` and its lines) of
  one capture, with the ``Input Dispatcher State:`` and ``Display: 0`` lines
  added above it (an emulator of the same image printed them there right
  above ``Windows:``), and a line the capture script stopped at added below
  it (``Connections:``; the script stopped at that line or at
  ``MonitoringChannels:``, ``mMaximumObscuring...`` or ``Global monitors``,
  and did not record which). The ``Viewport`` lines of display 0 above them
  come from a search of the same output (API 33 to 35) or, for API 30 to 32,
  from another boot of an emulator of the same image.

Other text in the tests (a rotated ``Viewport`` line, a rotated transform)
is copied from the rotated emulators; changed lines are changed in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from uiprobe.inputwindows import (
    LINE_FORMAT,
    NO_LIST,
    NO_WINDOW,
    PORTAL,
    REGION_FORMAT,
    ROTATION,
    InputWindow,
    display_orientation,
    parse_input_windows,
    read_window_at,
    window_at,
)

DATA = Path(__file__).parent / "data" / "inputwindows"


def _capture(name: str) -> str:
    return (DATA / f"{name}.txt").read_text(encoding="utf-8")


API24 = _capture("api24")
API30 = _capture("api30")
API31 = _capture("api31")
API32 = _capture("api32")
API33 = _capture("api33")
API34 = _capture("api34")
API35 = _capture("api35")
API36 = _capture("api36")

LAUNCHER = (
    "com.google.android.apps.nexuslauncher/"
    "com.google.android.apps.nexuslauncher.NexusLauncherActivity"
)
SETTINGS = "com.android.settings/com.android.settings.Settings"
# Lines of the rotated emulators (API 24 and 36, display turned once).
VIEWPORT24_TURNED = (
    "      Viewport: displayId=0, orientation=1, logicalFrame=[0, 0, 640, 320],"
    " physicalFrame=[0, 0, 640, 320], deviceSize=[640, 320]"
)
VIEWPORT36_TURNED = (
    "      Viewport INTERNAL: displayId=0, uniqueId=local:4619827259835644672,"
    " port=0, orientation=1, logicalFrame=[0, 0, 640, 320], physicalFrame=[0, 0,"
    " 640, 320], deviceSize=[640, 320], isActive=[1]"
)
VIEWPORT_NONE = (
    "      Viewport INTERNAL: displayId=-1, uniqueId=, port=<none>, orientation=0,"
    " logicalFrame=[0, 0, 0, 0], physicalFrame=[0, 0, 0, 0], deviceSize=[0, 0],"
    " isActive=[0]"
)
TURNED_TRANSFORM = (
    "      transform (ROT_270) (ROTATE TRANSLATE)\n"
    "          0.0000  1.0000  0.0000\n"
    "          -1.0000  0.0000  320.0000\n"
    "          0.0000  0.0000  1.0000"
)
# The API 24 Viewport line, put above a list whose capture has none.
VIEWPORT24 = API24.split("\n")[1]
# The display transform line of API 36, right above "Windows:".
DISPLAY_TRANSFORM = "        transform (ROT_0) (IDENTITY)\n    Windows:"


def _line(text: str, marker: str) -> str:
    """The one line of `text` that holds `marker`."""
    found = [line for line in text.split("\n") if marker in line]
    assert len(found) == 1, (marker, len(found))
    return found[0]


def _edit(text: str, marker: str, old: str, new: str) -> str:
    """`text` with `old` replaced by `new` in the one line holding `marker`."""
    line = _line(text, marker)
    assert old in line, (old, line)
    return text.replace(line, line.replace(old, new))


def _drop(text: str, marker: str) -> str:
    """`text` without the one line holding `marker`."""
    return text.replace(_line(text, marker) + "\n", "")


def _set_line(text: str, index: int, line: str) -> str:
    """`text` with its line number `index` (from 0) replaced by `line`."""
    lines = text.split("\n")
    lines[index] = line
    return "\n".join(lines)


def _with_viewport(text: str) -> str:
    return VIEWPORT24 + "\n" + text


def _drop_viewports(text: str) -> str:
    """`text` without its Viewport lines."""
    return "\n".join(line for line in text.split("\n") if "Viewport" not in line)


def _at(text: str, point: tuple[int, int]) -> InputWindow | None:
    windows = parse_input_windows(text)
    assert not isinstance(windows, str), windows
    return window_at(windows, point)


@pytest.mark.parametrize(
    ("text", "point", "title", "uid"),
    [
        (API24, (160, 500), "InputMethod", 10053),  # the keyboard covers it
        (API24, (160, 100), SETTINGS, 1000),
        (API24, (160, 10), "StatusBar", 10027),
        (API30, (160, 320), LAUNCHER, 10153),
        (API30, (160, 10), "StatusBar", 10157),
        (API31, (160, 320), LAUNCHER, 10133),
        (API32, (160, 320), LAUNCHER, 10138),
        (API32, (160, 10), "StatusBar", 10139),  # the screen decor is <empty>
        (API33, (160, 320), LAUNCHER, 10153),
        (API33, (160, 10), "StatusBar", 10157),  # the gesture monitor is a spy
        (API34, (160, 320), LAUNCHER, 10169),
        (API34, (160, 10), "StatusBar", 10168),
        (API35, (160, 320), LAUNCHER, 10179),
        (API35, (160, 10), LAUNCHER, 10179),  # the status bar is NOT_VISIBLE
        (API36, (296, 458), "FloatingMenu", 10186),  # the floating button
        (API36, (160, 320), SETTINGS, 1000),
        (API36, (271, 458), SETTINGS, 1000),
        (API36, (296, 482), SETTINGS, 1000),
        (API36, (160, 10), "StatusBar", 10186),
    ],
)
def test_the_topmost_window_taking_the_touch_is_found(text, point, title, uid):
    found = _at(text, point)
    assert found is not None
    assert (found.title, found.owner_uid) == (title, uid)


def test_windows_that_do_not_take_touches_are_skipped():
    # API 24: the keyguard scrim is not visible; API 30: the first launcher
    # window has FLAG_NOT_TOUCHABLE (0x10 in 0x...18); API 31: NOT_TOUCHABLE;
    # API 36: spy windows, NOT_VISIBLE, NOT_TOUCHABLE and windows without an
    # input channel do not take the touch.
    takes = {}
    for name, text in (("24", API24), ("30", API30), ("31", API31), ("36", API36)):
        windows = parse_input_windows(text)
        assert not isinstance(windows, str)
        takes[name] = [w.takes_touch for w in windows]
    assert takes["24"] == [True, False, False, True, True, False, False]
    assert takes["30"] == [True, False, True, False]
    assert takes["31"] == [True, False, True, True, True, False]
    assert takes["36"] == [False, False, True, True, True, True] + [False] * 7
    windows = parse_input_windows(API36)
    assert not isinstance(windows, str)
    assert windows[3].region is None  # <empty>


def test_a_point_no_window_takes_is_none():
    assert _at(API36, (400, 900)) is None
    assert read_window_at(API36, (400, 900)) == NO_WINDOW


@pytest.mark.parametrize("text", [API24, API33, API34, API35, API36])
def test_the_whole_output_reads_the_window(text):
    found = read_window_at(text, (160, 320))
    assert isinstance(found, InputWindow)
    assert found.title in {LAUNCHER, SETTINGS}


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


# --- text of a title or applicationInfo.name that reads like a field ---


@pytest.mark.parametrize(
    ("text", "marker", "old", "new"),
    [
        # API 24, 30 and 31: a quoted legacy title
        (API24, "u0 InputMethod}", "InputMethod}'", "InputMethod, ownerUid=1000}'"),
        (
            API24,
            "u0 InputMethod}",
            "InputMethod}'",
            "InputMethod, visible=false, flags=0x10, x}'",
        ),
        (
            API24,
            "u0 StatusBar}",
            "StatusBar}'",
            "StatusBar, touchableRegion=<empty>, x}'",
        ),
        (API30, "u0 StatusBar}", "StatusBar}'", "StatusBar, portalToDisplayId=-1, x}'"),
        (
            API31,
            "d145dce StatusBar",
            "StatusBar'",
            "StatusBar, flags=NOT_TOUCHABLE, x'",
        ),
        (API31, "d145dce StatusBar", "StatusBar'", "StatusBar, displayId=1, x'"),
        # API 33: a quoted title with inputConfig
        (API33, "bfd95f5 StatusBar", "StatusBar'", "StatusBar, inputConfig=SPY, x'"),
        # API 36: a bare title
        (
            API36,
            "5ebeeda FloatingMenu",
            "FloatingMenu,",
            "FloatingMenu, inputConfig=NOT_TOUCHABLE, x,",
        ),
        (
            API36,
            "5ebeeda FloatingMenu",
            "FloatingMenu,",
            "FloatingMenu, id=1, displayId=1, x,",
        ),
        (
            API36,
            "5ebeeda FloatingMenu",
            "FloatingMenu,",
            "FloatingMenu, ownerUid=1000, x,",
        ),
        # applicationInfo.name, printed as the app gave it
        (
            API36,
            "5ebeeda FloatingMenu",
            "applicationInfo.name=,",
            "applicationInfo.name=x, ownerUid=1000,",
        ),
        (
            API36,
            "5ebeeda FloatingMenu",
            "applicationInfo.name=,",
            "applicationInfo.name=x, touchableRegion=<empty>,",
        ),
        (
            API31,
            "d145dce StatusBar",
            "applicationInfo.name=,",
            "applicationInfo.name=x, visible=false,",
        ),
    ],
)
def test_a_field_twice_in_a_window_line_is_line_format(text, marker, old, new):
    assert parse_input_windows(_edit(text, marker, old, new)) == LINE_FORMAT


# --- every row, in order, and nothing else in the list ---


def test_a_missing_row_makes_the_list_unknown():
    assert parse_input_windows(_drop(API24, "u0 InputMethod}")) == LINE_FORMAT
    # a list that starts at row 1 (row 0, on top, is missing)
    assert parse_input_windows(_drop(API24, "u0 StatusBar}")) == LINE_FORMAT
    assert parse_input_windows(_drop(API36, "5ebeeda FloatingMenu")) == LINE_FORMAT


@pytest.mark.parametrize(
    ("marker", "old", "new"),
    [
        ("u0 StatusBar}", "    0: name=", "    0: nom="),  # damaged
        ("u0 StatusBar}", "    0: name=", "    name="),  # no row number
        ("u0 StatusBar}", "    0: name=", "    00: name="),
        ("u0 InputMethod}", "    3: name=", "    4: name="),  # out of order
        ("u0 StatusBar}", "flags=0x81840048", "flags=0xq1840048"),
    ],
)
def test_a_damaged_higher_row_makes_the_list_unknown(marker, old, new):
    # The app's window below must not be taken when a row above it cannot be
    # read: it could be the window on top.
    assert parse_input_windows(_edit(API24, marker, old, new)) == LINE_FORMAT


def test_only_transform_lines_and_their_rows_may_follow_a_window_line():
    statusbar = _line(API36, "fd782c1 StatusBar")
    cases = [
        API36.replace(statusbar, statusbar + "\n      touchOcclusionMode=x"),
        # a TRANSLATE matrix missing a row
        API36.replace("          0.0000  1.0000  -616.0000\n", ""),
        API36.replace(statusbar, statusbar + "\n          1.0000  0.0000  0.0000"),
        # two transform lines after one window line
        API36.replace(statusbar, statusbar + "\n      transform (ROT_0) (IDENTITY)"),
        API24.replace("  Windows:\n", "  Windows:\n    transform (ROT_0) (IDENTITY)\n"),
        API36.replace(
            "      transform (ROT_0) (TRANSLATE)", "      transform (ROT_0) TRANSLATE"
        ),
    ]
    for text in cases:
        assert parse_input_windows(text) == LINE_FORMAT


def test_transform_lines_at_the_indent_of_the_list_belong_to_it():
    # API 35 prints them at the indent of "Windows:"; the rows below them are
    # still read.
    windows = parse_input_windows(API35)
    assert not isinstance(windows, str)
    assert len(windows) == 8


# --- which list ---


DISPLAY1 = (
    "  Display: 1\n"
    "    logicalSize=100x100\n"
    "    Windows:\n"
    "      0: name=abc Other, id=5, displayId=1, inputConfig=0x0, alpha=1,"
    " frame=[0,0][100,100], globalScale=1, applicationInfo.name=,"
    " applicationInfo.token=<null>, touchableRegion=[0,0][100,100], ownerPid=1,"
    " ownerUid=10999, dispatchingTimeout=5000ms\n"
    "      transform (ROT_0) (IDENTITY)\n"
)


def test_the_list_of_display_0_is_taken_when_it_comes_second():
    two = API36.replace("  Display: 0\n", DISPLAY1 + "  Display: 0\n")
    assert parse_input_windows(two) == parse_input_windows(API36)
    other = parse_input_windows(two, display_id=1)
    assert not isinstance(other, str)
    assert [(w.title, w.owner_uid) for w in other] == [("Other", 10999)]
    assert parse_input_windows(API36, display_id=7) == NO_LIST


def test_lists_that_cannot_be_told_apart_are_no_list():
    block = API36[API36.index("  Display: 0\n") : API36.index("  mMaximumObscuring")]
    assert parse_input_windows(API36.replace(block, block + block)) == NO_LIST
    statusbar = _line(API24, "u0 StatusBar}")
    both = API36.replace(
        "  Display: 0\n", "  Windows:\n" + statusbar + "\n  Display: 0\n"
    )
    assert parse_input_windows(both) == NO_LIST
    lists = API24.replace("  MonitoringChannels:", "  Windows:\n  MonitoringChannels:")
    assert parse_input_windows(lists) == NO_LIST
    assert parse_input_windows(API24 + "Input Dispatcher State:\n") == NO_LIST


def test_a_window_list_of_a_touch_in_progress_is_not_the_display_list():
    # A touch state lists the windows a touch went to, deeper down (shaped
    # as the dispatcher prints it; not captured).
    touched = API24.replace(
        "  TouchStates: <no displays touched>\n",
        "  TouchStates:\n"
        "    0: down=true, split=false, deviceId=1, source=0x00001002\n"
        "      Windows:\n"
        "        0: name='Window{6c8fba5 u0 InputMethod}', pointerIds=0x80000000,"
        " targetFlags=0x105\n",
    )
    assert parse_input_windows(touched) == parse_input_windows(API24)


def test_only_the_asked_display_counts():
    # One list of every display (API 24): another display's line is skipped.
    other = _edit(API24, "u0 StatusBar}", "displayId=0", "displayId=1")
    windows = parse_input_windows(other)
    assert not isinstance(windows, str)
    assert "StatusBar" not in [w.title for w in windows]
    # A list per display (API 36): another display's line in it is an error.
    wrong = _edit(API36, "5ebeeda FloatingMenu", "displayId=0", "displayId=1")
    assert parse_input_windows(wrong) == LINE_FORMAT


# --- rotation ---


def test_the_orientation_comes_from_the_viewport_lines():
    assert display_orientation(API24) == 0
    assert display_orientation(API36) == 0
    assert display_orientation(API34) == 0  # two lines, both 0
    assert display_orientation(API30) == 0  # API 29 on: Viewport INTERNAL
    assert display_orientation(_drop_viewports(API30)) is None
    assert display_orientation(API24.replace(VIEWPORT24, VIEWPORT24_TURNED)) == 1
    assert display_orientation(_set_line(API34, 0, VIEWPORT36_TURNED)) is None


@pytest.mark.parametrize(
    "text",
    [
        API24.replace(VIEWPORT24, VIEWPORT24_TURNED),
        # both display-0 lines of API 36 (the second holds the first)
        API36.replace(API36.split("\n")[1], VIEWPORT36_TURNED),
        # one of two lines turned: they disagree
        _set_line(API34, 0, VIEWPORT36_TURNED),
        _set_line(API35, 1, "  " + VIEWPORT36_TURNED),
        # no line of display 0 (only another display's)
        API24.replace(VIEWPORT24, VIEWPORT_NONE),
        _drop(API24, "Viewport"),
        # a line below the dispatcher's header is not read
        _drop(API24, "Viewport") + VIEWPORT24 + "\n",
    ],
)
def test_a_rotated_or_unknown_orientation_is_rotation(text):
    assert read_window_at(text, (160, 320)) == ROTATION


def test_a_rotated_transform_in_the_list_is_rotation():
    settings_row = _line(API36, "Settings, id=121")
    after = settings_row + "\n      transform (ROT_0) (IDENTITY)"
    turned = API36.replace(after, settings_row + "\n" + TURNED_TRANSFORM)
    assert parse_input_windows(turned) == ROTATION
    display = API36.replace(
        DISPLAY_TRANSFORM, "        transform (ROT_90) (ROTATE TRANSLATE)\n    Windows:"
    )
    assert parse_input_windows(display) == ROTATION
    scaled = API36.replace(
        DISPLAY_TRANSFORM, "        transform (ROT_0) (SCALE )\n    Windows:"
    )
    assert parse_input_windows(scaled) == ROTATION
    orientation = _edit(
        API32, "bab5115 StatusBar", "displayOrientation=0", "displayOrientation=1"
    )
    assert parse_input_windows(orientation) == ROTATION


def test_without_a_viewport_line_every_form_is_rotation():
    for text in (API24, API30, API31, API32, API33, API34, API35, API36):
        assert isinstance(read_window_at(text, (160, 320)), InputWindow)
        assert read_window_at(_drop_viewports(text), (160, 320)) == ROTATION


def test_the_api_26_and_28_viewport_form_is_read():
    # API 26 and 28 also list "Viewport: ..., uniqueId='', ..." lines.
    line = (
        "          Viewport: displayId=0, orientation=0, uniqueId='',"
        " logicalFrame=[0, 0, 320, 640], physicalFrame=[0, 0, 320, 640],"
        " deviceSize=[320, 640]"
    )
    assert display_orientation(line + "\n" + API24) == 0
    turned = line.replace("orientation=0", "orientation=1")
    assert display_orientation(turned + "\n" + API24) is None


# --- touch modal windows and portals (API 24 to 32) ---


def test_a_legacy_touch_modal_window_takes_every_point():
    # Visible, touchable, neither NOT_FOCUSABLE (0x8) nor NOT_TOUCH_MODAL
    # (0x20): it takes the touch outside its region too.
    scrim = _edit(API24, "u0 KeyguardScrim}", "visible=false", "visible=true")
    found = _at(scrim, (160, 320))
    assert found is not None
    assert (found.title, found.modal) == ("KeyguardScrim", True)
    bar = _edit(API24, "u0 StatusBar}", "flags=0x81840048", "flags=0x81840040")
    found = _at(bar, (160, 320))
    assert found is not None
    assert found.title == "StatusBar"
    # NOT_FOCUSABLE alone, or NOT_TOUCH_MODAL alone, is not touch modal.
    windows = parse_input_windows(API24)
    assert not isinstance(windows, str)
    assert [w.modal for w in windows] == [False] * 7


def test_a_named_flags_touch_modal_window_takes_every_point():
    bar = _edit(API31, "d145dce StatusBar", "flags=NOT_FOCUSABLE | ", "flags=")
    found = _at(bar, (160, 320))
    assert found is not None
    assert found.title == "StatusBar"
    # an empty region, but touch modal
    decor = _edit(
        API32,
        "2c0698f ScreenDecorOverlayBottom",
        "flags=NOT_FOCUSABLE | NOT_TOUCH_MODAL | ",
        "flags=",
    )
    found = _at(decor, (160, 320))
    assert found is not None
    assert found.title == "ScreenDecorOverlayBottom"
    # not visible: no touch at all
    hidden = _edit(bar, "d145dce StatusBar", "visible=true", "visible=false")
    found = _at(hidden, (160, 320))
    assert found is not None
    assert found.title == LAUNCHER


def test_input_config_has_no_touch_modal():
    bar = _edit(
        API36,
        "fd782c1 StatusBar",
        "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY",
        "inputConfig=TRUSTED_OVERLAY",
    )
    found = _at(bar, (160, 320))
    assert found is not None
    assert found.title == SETTINGS


def test_a_hit_on_a_portal_is_portal():
    launcher = "4f4eb39 u0 com.google"
    portal = _edit(API30, launcher, "portalToDisplayId=-1", "portalToDisplayId=2")
    assert read_window_at(_with_viewport(portal), (160, 320)) == PORTAL
    # a portal elsewhere does not matter
    bar = _edit(API30, "u0 StatusBar}", "portalToDisplayId=-1", "portalToDisplayId=2")
    found = read_window_at(_with_viewport(bar), (160, 320))
    assert isinstance(found, InputWindow)
    assert found.title == LAUNCHER
    broken = _edit(
        API30, "u0 StatusBar}", "portalToDisplayId=-1", "portalToDisplayId=x"
    )
    assert parse_input_windows(broken) == LINE_FORMAT


# --- flag names and regions ---


@pytest.mark.parametrize(
    ("text", "marker", "old", "new"),
    [
        (
            API36,
            "5ebeeda FloatingMenu",
            "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY",
            "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY | MYSTERY",
        ),
        (API36, "Settings, id=121", "inputConfig=0x0", "inputConfig=0x8"),
        (API36, "Settings, id=121", "inputConfig=0x0", "inputConfig="),
        (
            API36,
            "Settings, id=121",
            "inputConfig=0x0",
            "inputConfig=0x0 | SPY",
        ),
        (
            API33,
            "1465d93 com.google",
            "inputConfig=DUPLICATE_TOUCH_TO_WALLPAPER",
            "inputConfig=DUPLICATE_TOUCH_TO_WALLPAPER|SPY",
        ),
        (
            API31,
            "cc3bf00 com.google",
            "flags=NOT_TOUCH_MODAL | ",
            "flags=NOT_TOUCH_MODAL | FUTURE_FLAG | ",
        ),
        (API31, "cc3bf00 com.google", "flags=NOT_TOUCH_MODAL", "flags=not_touch_modal"),
        # both forms in one line
        (
            API36,
            "Settings, id=121",
            "inputConfig=0x0,",
            "inputConfig=0x0, visible=true,",
        ),
    ],
)
def test_an_unknown_flag_name_is_line_format(text, marker, old, new):
    assert parse_input_windows(_edit(text, marker, old, new)) == LINE_FORMAT


@pytest.mark.parametrize(
    "region",
    ["[320,434][272,482]", "[272,482][320,434]", "[272,434][99999999,482]"],
)
def test_a_reversed_or_huge_region_is_region_format(region):
    text = _edit(API36, "5ebeeda FloatingMenu", "[272,434][320,482]", region)
    assert parse_input_windows(text) == REGION_FORMAT


def test_an_empty_rectangle_holds_no_point():
    text = _edit(
        API36, "5ebeeda FloatingMenu", "[272,434][320,482]", "[272,434][272,482]"
    )
    found = _at(text, (272, 458))
    assert found is not None
    assert found.title == SETTINGS


# --- round 2: the end of the list, Viewport lines, portals, whole values ---


def test_a_name_that_fakes_a_row_and_the_end_of_the_list_is_line_format():
    # A window name may hold line breaks. Quoted form (API 24): the name
    # makes a whole row that does not take touches, then a line the list
    # ends at; the real rest of the line, with the real owner, comes after.
    real = _line(API24, "u0 InputMethod}")
    fake = real.replace("visible=true", "visible=false")
    rest = "x" + real[real.index("}', displayId") :]
    quoted = API24.replace(real, f"{fake}\n  MonitoringChannels:\n{rest}")
    assert parse_input_windows(quoted) == LINE_FORMAT
    # Bare form (API 36).
    real = _line(API36, "5ebeeda FloatingMenu")
    fake = real.replace(
        "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY", "inputConfig=NOT_TOUCHABLE"
    )
    rest = "x" + real[real.index(", id=133") :]
    bare = API36.replace(
        real,
        f"{fake}\n      transform (ROT_0) (IDENTITY)\n"
        f"  mMaximumObscuringOpacityForTouch: 0.800000\n{rest}",
    )
    assert parse_input_windows(bare) == LINE_FORMAT
    # The rest inside the list is a line of no known form.
    inside = API36.replace(real, f"{fake}\n{rest}")
    assert parse_input_windows(inside) == LINE_FORMAT


@pytest.mark.parametrize(
    "end",
    [
        "  MonitoringChannels:",
        "  mMaximumObscuringOpacityForTouch: 0.800000",
        "  Global monitors in display 0:",
        "  Global monitors on display 0:",
        "  Connections:",
    ],
)
def test_the_list_ends_at_a_known_line(end):
    text = API24.replace("  MonitoringChannels:", end)
    assert not isinstance(parse_input_windows(text), str)


def test_a_list_without_a_known_end_is_line_format():
    unknown = API24.replace("  MonitoringChannels:", "  Mystery:")
    assert parse_input_windows(unknown) == LINE_FORMAT
    cut = API24[: API24.index("  MonitoringChannels:")]
    assert parse_input_windows(cut) == LINE_FORMAT
    cut36 = API36[: API36.index("  mMaximumObscuring")]
    assert parse_input_windows(cut36) == LINE_FORMAT


def test_a_malformed_or_scaled_viewport_line_is_rotation():
    lines = API34.split("\n")
    malformed = _set_line(API34, 1, lines[1].replace("orientation=0", "orientation=x"))
    assert display_orientation(malformed) is None
    assert read_window_at(malformed, (160, 320)) == ROTATION
    scaled = API24.replace(
        "physicalFrame=[0, 0, 320, 640]", "physicalFrame=[0, 0, 640, 1280]"
    )
    assert display_orientation(scaled) is None
    assert read_window_at(scaled, (160, 320)) == ROTATION
    moved = API24.replace(
        "logicalFrame=[0, 0, 320, 640]", "logicalFrame=[0, 8, 320, 648]"
    )
    assert read_window_at(moved, (160, 320)) == ROTATION
    extra = API24.replace("deviceSize=[320, 640]", "deviceSize=[320, 640], more=1")
    assert read_window_at(extra, (160, 320)) == ROTATION


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        (API30, "4f4eb39 u0 com.google"),
        (API31, "cc3bf00 com.google"),
        (API32, "84341af com.google"),
    ],
)
def test_a_missing_portal_field_is_line_format_where_the_form_has_it(text, marker):
    assert parse_input_windows(_edit(text, marker, " portalToDisplayId=-1,", "")) == (
        LINE_FORMAT
    )


@pytest.mark.parametrize(
    ("text", "marker", "old", "new", "reason"),
    [
        (
            API36,
            "5ebeeda FloatingMenu",
            "touchableRegion=[272,434][320,482]",
            "touchableRegion=[272,434][320,482], [0,0][10,10]",
            REGION_FORMAT,
        ),
        (
            API36,
            "5ebeeda FloatingMenu",
            "ownerUid=10186,",
            "ownerUid=10186, x,",
            LINE_FORMAT,
        ),
        (
            API36,
            "5ebeeda FloatingMenu",
            "TRUSTED_OVERLAY, alpha",
            "TRUSTED_OVERLAY, SPY, alpha",
            LINE_FORMAT,
        ),
        (API36, "5ebeeda FloatingMenu", "displayId=0,", "displayId=0, 1,", LINE_FORMAT),
        (
            API24,
            "u0 InputMethod}",
            "visible=true,",
            "visible=true, false,",
            LINE_FORMAT,
        ),
        (
            API24,
            "u0 InputMethod}",
            "flags=0x01800108,",
            "flags=0x01800108, 0x10,",
            LINE_FORMAT,
        ),
        (
            API30,
            "4f4eb39 u0 com.google",
            "portalToDisplayId=-1,",
            "portalToDisplayId=-1, 2,",
            LINE_FORMAT,
        ),
    ],
)
def test_a_value_with_more_text_before_the_next_field_is_refused(
    text, marker, old, new, reason
):
    assert parse_input_windows(_edit(text, marker, old, new)) == reason


def test_a_name_that_fakes_another_display_section_is_line_format():
    # The real rest of the line (with the real displayId=0) lands in a
    # section of another display, where it is not a row of that display.
    real = _line(API36, "5ebeeda FloatingMenu")
    fake = real.replace(
        "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY", "inputConfig=NOT_TOUCHABLE"
    )
    rest = "      0: name=x" + real[real.index(", id=133") :]
    moved = API36.replace(
        real,
        f"{fake}\n      transform (ROT_0) (IDENTITY)\n"
        f"  Display: 1\n    Windows:\n{rest}",
    )
    assert parse_input_windows(moved) == LINE_FORMAT
    # A damaged row in another display's list makes the whole output unknown.
    two = API36.replace("  Display: 0\n", DISPLAY1 + "  Display: 0\n")
    damaged = two.replace("0: name=abc Other", "0: nom=abc Other")
    assert parse_input_windows(damaged) == LINE_FORMAT
