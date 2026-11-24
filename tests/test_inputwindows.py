"""Tests for reading the window a touch reaches from ``dumpsys input``.

The data files are lines copied unchanged from emulators, in their order, with
lines left out but none changed or renumbered:

* api24.txt and api36.txt: excerpts of a whole ``dumpsys input`` (the reader's
  ``Viewport`` lines of display 0 and the dispatcher's part down to the end
  of the window list).
* samsung36.txt: lines of a Samsung phone (Android 16, API 36) with every
  device-identifying value replaced; its first lines say which.
* api30.txt to api35.txt: the window list (``Windows:`` and its lines) of one
  capture, with the ``Input Dispatcher State:`` and ``Display: 0`` lines added
  above it (an emulator of the same image printed them there right above
  ``Windows:``; on API 33 to 35 also ``logicalSize=320x640`` and ``transform
  (ROT_0) (IDENTITY)``, as another boot of those images did), and a line the
  capture script stopped at added below it (``Connections:``; the script
  stopped at that line or at ``MonitoringChannels:``, ``mMaximumObscuring...``
  or ``Global monitors``, and did not record which). The ``Viewport`` lines of
  display 0 above them come from a search of the same output (API 33 to 35)
  or, for API 30 to 32, from another boot of an emulator of the same image.

Other text in the tests (a rotated ``Viewport`` line, a rotated transform)
is copied from the rotated emulators; changed lines are changed in place.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

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
    explain_input_windows,
    parse_input_windows,
    read_window_at,
    window_at,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

DATA = Path(__file__).parent / "data" / "inputwindows"
# A window line of the list (its row number and a colon).
_INDEX_LINE = re.compile(r"^ +[0-9]+: name=")


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
SAMSUNG36 = _capture("samsung36")

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
    "        transform (ROT_0) (IDENTITY)\n"
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


# Python's default limit of digits `int` converts (`_digit_limit` sets it):
# one more digit raises ValueError, which the parser turns into a refusal.
LIMIT = 4300
OVERLONG = ["1" * (LIMIT + 1), "0" * (LIMIT + 1)]
LONGEST_ZERO = "0" * LIMIT  # still converts: read as 0, as before


@pytest.fixture
def _digit_limit() -> Iterator[None]:
    """Use Python's default int-to-str digit limit, whatever the host set."""
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(LIMIT)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(before)


def _row_rotation(text: str, row: str, rotation: str) -> str:
    """`text` with the transform line right below the row holding `row`
    saying ``ROT_<rotation>`` instead of ``ROT_0``."""
    line = _line(text, row)
    after = line + "\n      transform (ROT_0) (IDENTITY)"
    assert text.count(after) == 1
    return text.replace(after, f"{line}\n      transform (ROT_{rotation}) (IDENTITY)")


STYLUS = "0: name=stylus-handwriting-event-receiver-0"


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize("digits", OVERLONG)
def test_a_row_rotation_too_long_to_read_is_rotation(digits):
    text = _row_rotation(API36, STYLUS, digits)
    assert parse_input_windows(text) == ROTATION
    assert read_window_at(text, (160, 320)) == ROTATION


@pytest.mark.usefixtures("_digit_limit")
def test_a_row_rotation_of_the_longest_convertible_zero_is_read_as_before():
    text = _row_rotation(API36, STYLUS, LONGEST_ZERO)
    assert parse_input_windows(text) == parse_input_windows(API36)
    assert read_window_at(text, (160, 320)) == read_window_at(API36, (160, 320))


def _viewport_display(text: str, digits: str) -> str:
    """`text` with its first Viewport line of display 0 naming display
    `digits` (the second one still says display 0, orientation 0)."""
    marker = "Viewport INTERNAL: displayId=0,"
    assert text.count(marker) == 2
    return text.replace(marker, f"Viewport INTERNAL: displayId={digits},", 1)


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize("digits", OVERLONG)
def test_a_viewport_display_number_too_long_to_read_is_rotation(digits):
    # The line could be of display 0 too.
    text = _viewport_display(API36, digits)
    assert parse_input_windows(text) == parse_input_windows(API36)  # list fine
    assert display_orientation(text) is None
    assert read_window_at(text, (160, 320)) == ROTATION


@pytest.mark.usefixtures("_digit_limit")
def test_a_viewport_display_number_of_the_longest_convertible_length_is_read():
    # As before: display 0 written with 4300 digits is a line of display 0
    # of no known form (displayId= has at most 10 digits there); display 1
    # written so is another display's line, not read.
    assert display_orientation(_viewport_display(API36, LONGEST_ZERO)) is None
    text = _viewport_display(API36, "0" * (LIMIT - 1) + "1")
    assert display_orientation(text) == 0
    assert read_window_at(text, (160, 320)) == read_window_at(API36, (160, 320))


def _orientation(digits: str) -> str:
    return _edit(
        API32,
        "2c0698f ScreenDecorOverlayBottom",
        "displayOrientation=0",
        f"displayOrientation={digits}",
    )


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize("digits", OVERLONG)
def test_a_display_orientation_too_long_to_read_is_rotation(digits):
    text = _orientation(digits)
    assert parse_input_windows(text) == ROTATION
    assert read_window_at(text, (160, 320)) == ROTATION


@pytest.mark.usefixtures("_digit_limit")
def test_a_display_orientation_of_the_longest_convertible_zero_is_read_as_before():
    text = _orientation(LONGEST_ZERO)
    assert parse_input_windows(text) == parse_input_windows(API32)
    assert read_window_at(text, (160, 320)) == read_window_at(API32, (160, 320))


def _with_display1(rotation: str = "0", field: str = "") -> str:
    """API 36 with the list of display 1 (`DISPLAY1`) above display 0's, its
    row's transform saying ``ROT_<rotation>`` and `field` added to its row."""
    block = DISPLAY1.replace(
        "      transform (ROT_0) (IDENTITY)\n",
        f"      transform (ROT_{rotation}) (IDENTITY)\n",
    ).replace(", ownerUid=10999", f"{field}, ownerUid=10999")
    return API36.replace("  Display: 0\n", block + "  Display: 0\n")


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize(
    ("rotation", "field"),
    [("0" * (LIMIT + 1), ""), ("0", f", displayOrientation={'0' * (LIMIT + 1)}")],
    ids=["row_rotation", "display_orientation"],
)
def test_a_number_too_long_to_read_in_another_displays_list_is_rotation(
    rotation, field
):
    text = _with_display1(rotation, field)
    assert parse_input_windows(text) == ROTATION
    assert read_window_at(text, (160, 320)) == ROTATION


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize(
    ("rotation", "field"),
    [("1", ""), ("0", ", displayOrientation=1"), (LONGEST_ZERO, "")],
    ids=["row_rotation_1", "display_orientation_1", "longest_zero"],
)
def test_a_readable_rotation_in_another_displays_list_leaves_display_0_as_before(
    rotation, field
):
    # The other display's own rotation does not matter for display 0.
    text = _with_display1(rotation, field)
    assert parse_input_windows(text) == parse_input_windows(_with_display1())
    assert parse_input_windows(text) == parse_input_windows(API36)
    assert read_window_at(text, (160, 320)) == read_window_at(API36, (160, 320))


def _combined(rotation: str) -> str:
    """API 24 (one list of every display) with a transform line below every
    row and its StatusBar row on display 1, whose transform says
    ``ROT_<rotation>``."""
    rows = [line for line in API24.split("\n") if _INDEX_LINE.match(line)]
    text = API24
    for row in rows:
        text = text.replace(row + "\n", row + "\n      transform (ROT_0) (IDENTITY)\n")
    text = _edit(text, "u0 StatusBar}", "displayId=0", "displayId=1")
    return _row_rotation(text, "u0 StatusBar}", rotation)


@pytest.mark.usefixtures("_digit_limit")
def test_a_rotation_too_long_to_read_on_a_skipped_row_is_rotation():
    # A list of every display: the row of display 1 is skipped, but its
    # transform was read first.
    base = parse_input_windows(_combined("0"))
    assert isinstance(base, tuple)
    assert "StatusBar" not in [w.title for w in base]
    assert parse_input_windows(_combined("1")) == base  # as before: skipped
    assert parse_input_windows(_combined(LONGEST_ZERO)) == base
    text = _combined("0" * (LIMIT + 1))
    assert parse_input_windows(text) == ROTATION
    assert read_window_at(text, (160, 320)) == ROTATION


@pytest.mark.usefixtures("_digit_limit")
def test_an_orientation_too_long_to_read_on_a_skipped_row_is_not_read():
    # A list of every display: the orientation of display 1's row is not
    # read for display 0 (as before, when int() never saw it), unlike its
    # transform line, so display 0's window is found as without it.
    base = _combined("0")
    text = _edit(
        base,
        "u0 StatusBar}",
        ", ownerUid=",
        f", displayOrientation={'0' * (LIMIT + 1)}, ownerUid=",
    )
    assert text != base
    found = read_window_at(text, (160, 320))
    assert isinstance(found, InputWindow)
    assert found.title == SETTINGS
    assert found == read_window_at(base, (160, 320))


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


# --- round 3: display ids, the display transform, one form per list ---


def test_two_sections_of_the_same_numeric_display_are_no_list():
    block = API36[API36.index("  Display: 0\n") : API36.index("  mMaximumObscuring")]
    twice = API36.replace(block, block.replace("Display: 0", "Display: 00") + block)
    assert parse_input_windows(twice) == NO_LIST
    assert read_window_at(twice, (140, 225)) == NO_LIST


def test_a_missing_display_transform_is_rotation_where_it_is_printed():
    # API 36 prints logicalSize= and then the display's transform.
    bare = API36.replace(
        "        transform (ROT_0) (IDENTITY)\n    Windows:", "    Windows:"
    )
    assert parse_input_windows(bare) == ROTATION
    assert read_window_at(bare, (140, 225)) == ROTATION
    # API 29 to 32 print neither line (API 30 here): nothing is required.
    assert isinstance(read_window_at(API30, (160, 320)), InputWindow)


def test_every_row_of_a_list_must_have_the_same_form():
    # API 30 launcher row without portalToDisplayId and globalScale: it still
    # has windowScale=, and the other rows are of the portal form.
    marker = "4f4eb39 u0 com.google"
    damaged = _edit(API30, marker, " portalToDisplayId=-1,", "")
    damaged = _edit(damaged, marker, " globalScale=1.000000,", "")
    assert parse_input_windows(damaged) == LINE_FORMAT
    assert read_window_at(damaged, (160, 320)) == LINE_FORMAT
    # One API 24 row without a field the others print.
    layerless = _edit(API24, "u0 InputMethod}", " layer=21015,", "")
    assert parse_input_windows(layerless) == LINE_FORMAT


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (" portalToDisplayId=-1,", ""),  # API 29/30 fields without the portal
        (" scale=1.000000,", " globalScale=1.000000, scale=1.000000,"),
    ],
)
def test_a_hex_flags_row_must_be_wholly_of_one_version(old, new):
    text = API30 if "portal" in old else API24
    marker = "4f4eb39 u0 com.google" if "portal" in old else "u0 InputMethod}"
    rows = text.split("\n")
    edited = _edit(text, marker, old, new)
    assert edited != text
    assert len(edited.split("\n")) == len(rows)
    assert parse_input_windows(edited) == LINE_FORMAT


# --- names that fake fields or rows (each form, each place in the list) ---

COVER_ALL = "touchableRegion=[0,0][320,640]"


@pytest.mark.parametrize(
    ("text", "marker", "old", "uid", "point"),
    [
        # API 24 to 30, hex flags: the keyboard above Settings (uid 1000)
        (API24, "u0 InputMethod}", "InputMethod}'", 1000, (160, 500)),
        # API 31 and 32, named flags: the status bar above the launcher
        (API31, "d145dce StatusBar", "StatusBar'", 10133, (160, 10)),
        # API 33 and 34, quoted name with inputConfig
        (API33, "bfd95f5 StatusBar", "StatusBar'", 10153, (160, 10)),
        (API34, "fd38730 StatusBar", "StatusBar'", 10169, (160, 10)),
        # API 35 and 36, bare name with inputConfig
        (API35, "66391f2 StatusBar", "StatusBar,", 10179, (160, 10)),
        (API36, "5ebeeda FloatingMenu", "FloatingMenu,", 1000, (296, 458)),
    ],
)
def test_a_name_that_closes_its_quote_and_fakes_the_owner_is_line_format(
    text, marker, old, uid, point
):
    # The window right above the app's window ends its name (a quote, or
    # nothing in the bare form) and writes the app's uid and a region over
    # the point before its real fields, on the same line.
    quote = "'" if old.endswith("'") else ""
    fake = f"{old[:-1]}{quote}, ownerUid={uid}, {COVER_ALL}, x{old[-1]}"
    edited = _edit(text, marker, old, fake)
    assert read_window_at(edited, point) == LINE_FORMAT


def _fake_row(real: str, index: int, uid_old: str, uid_new: str) -> str:
    """A whole row `index` made of `real`'s fields with another owner, as a
    window name could print it (the hash and title of the Settings window)."""
    indent = real[: len(real) - len(real.lstrip())]
    fields = real[real.index(", id=") :].replace(uid_old, uid_new)
    title = "6461878 com.android.settings/com.android.settings.Settings"
    return f"{indent}{index}: name={title}{fields}"


def _fields(real: str) -> str:
    return real[real.index(", id=") :]


def test_a_middle_row_that_fakes_a_whole_row_is_line_format():
    # Row 2 (FloatingMenu, uid 10186) names itself so that the text shows a
    # row 2 owned by uid 1000 and then "3: name=x" with its real fields; the
    # real row 3 (Taskbar) follows, so row 3 comes twice.
    real = _line(API36, "5ebeeda FloatingMenu")
    fake = _fake_row(real, 2, "ownerUid=10186", "ownerUid=1000")
    edited = API36.replace(real, f"{fake}\n      3: name=x{_fields(real)}")
    assert read_window_at(edited, (296, 458)) == LINE_FORMAT
    # With a transform line after the fake row too.
    with_transform = API36.replace(
        real,
        f"{fake}\n      transform (ROT_0) (IDENTITY)\n      3: name=x{_fields(real)}",
    )
    assert read_window_at(with_transform, (296, 458)) == LINE_FORMAT


def test_the_topmost_touch_taking_row_that_fakes_a_whole_row_is_line_format():
    # API 24 row 0 (the status bar, uid 10027) fakes a row 0 of Settings.
    real = _line(API24, "u0 StatusBar}")
    rest = real[real.index("', displayId") :]
    fake = real.replace("ownerUid=10027", "ownerUid=1000").replace(
        "u0 StatusBar}", "u0 com.android.settings/com.android.settings.Settings}"
    )
    edited = API24.replace(real, f"{fake}\n    1: name='Window{{x u0 y}}{rest}")
    assert read_window_at(edited, (160, 10)) == LINE_FORMAT


def test_a_fake_row_whose_rest_is_not_a_row_is_line_format():
    real = _line(API36, "5ebeeda FloatingMenu")
    fake = _fake_row(real, 2, "ownerUid=10186", "ownerUid=1000")
    # The rest without a row number: a line of no known form in the list.
    orphan = API36.replace(real, f"{fake}\n      x{_fields(real)}")
    assert read_window_at(orphan, (296, 458)) == LINE_FORMAT
    # The rest shaped like the last row of a transform's matrix.
    matrix = API36.replace(
        real,
        f"{fake}\n      transform (ROT_0) (TRANSLATE)\n"
        "          1.0000  0.0000  0.0000\n          0.0000  1.0000  0.0000\n"
        f"          0.0000  0.0000  1.0000{_fields(real)}",
    )
    assert read_window_at(matrix, (296, 458)) == LINE_FORMAT


def test_the_lowest_row_faking_a_whole_row_is_the_documented_limit():
    # Documented limit: a window of another app that is the lowest in the
    # list, with no window above it taking the point, and whose name fakes a
    # whole window line, is not told apart. The text shows the fake row (the
    # app's uid) and then a row 13 with the real fields. The real window here
    # takes touches over the whole display (made so from the wallpaper row).
    real = _line(API36, "ec45fba com.android.systemui.wallpapers.ImageWallpaper")
    taking = real.replace(
        "inputConfig=NOT_VISIBLE | NOT_FOCUSABLE | NOT_TOUCHABLE | IS_WALLPAPER",
        "inputConfig=TRUSTED_OVERLAY",
    ).replace("touchableRegion=<empty>", "touchableRegion=[0,0][1000,1000]")
    fake = _fake_row(taking, 12, "ownerUid=10186", "ownerUid=1000")
    lowest = API36.replace(
        real,
        f"{fake}\n      transform (ROT_0) (IDENTITY)\n"
        f"      13: name=y{_fields(taking)}",
    )
    found = read_window_at(lowest, (400, 900))
    assert isinstance(found, InputWindow)
    assert found.owner_uid == 1000  # the fake row: the check cannot tell


# --- round 4: display metadata, legacy rows per display, form from fields ---


@pytest.mark.parametrize("text", [API33, API34, API35, API36])
def test_the_display_metadata_is_required_for_the_input_config_forms(text):
    header = "    logicalSize=320x640\n        transform (ROT_0) (IDENTITY)\n"
    assert header in text
    bare = text.replace(header, "")
    assert parse_input_windows(bare) == ROTATION
    point = (140, 225) if text is API36 else (160, 320)
    assert read_window_at(bare, point) == ROTATION
    unsized = text.replace("    logicalSize=320x640\n", "")
    assert parse_input_windows(unsized) == ROTATION


def test_a_list_per_display_of_api_24_rows_is_line_format():
    rows = API24[API24.index("  Windows:\n") : API24.index("  MonitoringChannels:")]
    per_display = API24.replace(
        rows,
        "  Display: 0\n" + "\n".join("  " + r for r in rows.split("\n") if r) + "\n",
    )
    assert "  Display: 0\n    Windows:\n      0: name=" in per_display
    assert parse_input_windows(per_display) == LINE_FORMAT


def test_field_text_in_a_name_does_not_change_the_form():
    titled = _edit(
        API36, "5ebeeda FloatingMenu", "FloatingMenu,", "FloatingMenu, scale=100%,"
    )
    found = read_window_at(titled, (296, 458))
    assert isinstance(found, InputWindow)
    assert found.title == "FloatingMenu, scale=100%"
    assert found.owner_uid == 10186
    # The same in applicationInfo.name.
    named = _edit(
        API36,
        "5ebeeda FloatingMenu",
        "applicationInfo.name=,",
        "applicationInfo.name=a, scale=1,",
    )
    assert isinstance(read_window_at(named, (296, 458)), InputWindow)
    # On the hex form too (API 24): a title with scale= and globalScale=.
    hexed = _edit(
        API24, "u0 InputMethod}", "InputMethod}", "InputMethod, globalScale=2}"
    )
    found = read_window_at(hexed, (160, 500))
    assert isinstance(found, InputWindow)
    assert found.owner_uid == 10053


FAKES = (
    "ownerUid={uid}",
    COVER_ALL,
    "inputConfig=0x0",
    "displayId=0",
    "ownerUid={uid}, " + COVER_ALL + ", inputConfig=0x0, displayId=0",
)


@pytest.mark.parametrize("fake", FAKES)
@pytest.mark.parametrize(
    ("text", "marker", "old", "uid", "point"),
    [
        (API24, "u0 InputMethod}", "InputMethod}'", 1000, (160, 500)),
        (API31, "d145dce StatusBar", "StatusBar'", 10133, (160, 10)),
        (API33, "bfd95f5 StatusBar", "StatusBar'", 10153, (160, 10)),
        (API34, "fd38730 StatusBar", "StatusBar'", 10169, (160, 10)),
        (API35, "66391f2 StatusBar", "StatusBar,", 10179, (160, 10)),
        (API36, "5ebeeda FloatingMenu", "FloatingMenu,", 1000, (296, 458)),
    ],
)
def test_each_same_line_fake_is_line_format_on_each_form(
    text, marker, old, uid, point, fake
):
    quote = "'" if old.endswith("'") else ""
    written = fake.format(uid=uid)
    edited = _edit(text, marker, old, f"{old[:-1]}{quote}, {written}, x{old[-1]}")
    assert read_window_at(edited, point) == LINE_FORMAT


# --- round 5: an apostrophe in a quoted name ---

SETTINGS24_ROW = "4: name='Window{faa132d u0 com.android.settings/"


@pytest.mark.parametrize(
    ("text", "marker", "old", "new", "point", "title"),
    [
        (
            API24,
            SETTINGS24_ROW,
            "com.android.settings.Settings}'",
            "Today's Orders}'",
            (160, 100),
            "com.android.settings/Today's Orders",
        ),
        (
            API30,
            "u0 StatusBar}",
            "StatusBar}'",
            "Today's Bar}'",
            (160, 10),
            "Today's Bar",
        ),
        (
            API31,
            "d145dce StatusBar",
            "StatusBar'",
            "Today's Bar'",
            (160, 10),
            "Today's Bar",
        ),
        (
            API33,
            "bfd95f5 StatusBar",
            "StatusBar'",
            "'Today's' Bar'",
            (160, 10),
            "'Today's' Bar",
        ),
        (
            API34,
            "fd38730 StatusBar",
            "StatusBar'",
            "Today's Bar, x'",
            (160, 10),
            "Today's Bar, x",
        ),
    ],
)
def test_an_apostrophe_in_a_quoted_name_stays_in_the_title(
    text, marker, old, new, point, title
):
    edited = _edit(text, marker, old, new)
    plain = read_window_at(text, point)
    found = read_window_at(edited, point)
    assert isinstance(plain, InputWindow)
    assert isinstance(found, InputWindow)
    assert found.title == title
    assert found.owner_uid == plain.owner_uid


# The quoted forms with the fields that follow each name: API 24 to 30 print
# displayId= right after the name, API 31 to 34 id= and then displayId=.
QUOTED = [
    (API24, "u0 InputMethod}", "InputMethod}'", 1000, (160, 500), ""),
    (API30, "u0 StatusBar}", "StatusBar}'", 10153, (160, 10), ""),
    (API31, "d145dce StatusBar", "StatusBar'", 10133, (160, 10), "id=5, "),
    (API33, "bfd95f5 StatusBar", "StatusBar'", 10153, (160, 10), "id=5, "),
    (API34, "fd38730 StatusBar", "StatusBar'", 10169, (160, 10), "id=5, "),
]
BOUNDARY_FAKES = (
    # the whole rest of a window line, as the target's
    "{id}displayId=0, ownerUid={uid}, " + COVER_ALL + ", x",
    # a row of another display: it would be skipped, uncovering the point
    "{id}displayId=1, x",
    # just the boundary, nothing else faked
    "{id}displayId=0, x",
    # the boundary with the other key spelling and no id
    "displayId=0, ownerUid={uid}, x",
)


@pytest.mark.parametrize("fake", BOUNDARY_FAKES)
@pytest.mark.parametrize(("text", "marker", "old", "uid", "point", "id_"), QUOTED)
def test_a_quoted_name_that_fakes_its_closing_boundary_is_line_format(
    text, marker, old, uid, point, id_, fake
):
    # The window above the target ends its name with a quote followed by
    # metadata of its own; the name is now taken up to the first quote that
    # metadata follows, so its fake fields come before the real ones.
    written = fake.format(id=id_, uid=uid)
    edited = _edit(text, marker, old, f"{old[:-1]}', {written}'")
    assert read_window_at(edited, point) == LINE_FORMAT


@pytest.mark.parametrize(
    ("text", "marker", "old", "uid", "point"), [row[:5] for row in QUOTED]
)
def test_an_apostrophe_name_that_fakes_the_owner_is_still_line_format(
    text, marker, old, uid, point
):
    # The apostrophe that now stays in the title does not let field text
    # after it pass: the owner and region in the name are still read twice.
    fake = f"{old[:-1]}'s, ownerUid={uid}, {COVER_ALL}{old[-1]}"
    edited = _edit(text, marker, old, fake)
    assert read_window_at(edited, point) == LINE_FORMAT


# --- a Samsung phone (API 36): samsungFlags=, pTouchableRegion=, 1HXY= ---

HOME = "com.example.launcher/com.example.launcher.HomeActivity"
STATUS = "abcdef3 StatusBar"  # system UI (10001) above the launcher (10002)


@pytest.mark.parametrize("crlf", [False, True])
@pytest.mark.parametrize(
    ("point", "title", "uid"),
    [
        ((540, 1170), HOME, 10002),
        ((540, 200), HOME, 10002),
        ((540, 2300), "NavigationBar0", 10001),
        ((540, 50), "StatusBar", 10001),
        (
            (1050, 500),
            "com.example.launcher/com.example.launcher.EdgePanelService",
            10002,
        ),
    ],
)
def test_the_samsung_window_list_is_read(point, title, uid, crlf):
    text = SAMSUNG36.replace("\n", "\r\n") if crlf else SAMSUNG36
    windows = parse_input_windows(text)
    assert not isinstance(windows, str), windows
    assert len(windows) == 15
    found = read_window_at(text, point)
    assert isinstance(found, InputWindow), found
    assert (found.title, found.owner_uid) == (title, uid)


def test_the_samsung_viewport_line_with_density_is_read():
    assert display_orientation(SAMSUNG36) == 0
    turned = SAMSUNG36.replace("orientation=0, densityDpi", "orientation=1, densityDpi")
    assert display_orientation(turned) == 1
    assert read_window_at(turned, (540, 1170)) == ROTATION
    odd = SAMSUNG36.replace("densityDpi=480 xDpi", "densityDpi=480, xDpi")
    assert display_orientation(odd) is None


@pytest.mark.parametrize("value", ["0x0", "0x8", "FORCE_TRUSTED_OVERLAY"])
def test_samsung_flags_do_not_change_the_touch(value):
    edited = _edit(SAMSUNG36, STATUS, "samsungFlags=0x0", f"samsungFlags={value}")
    found = read_window_at(edited, (540, 50))
    assert isinstance(found, InputWindow)
    assert (found.title, found.owner_uid) == ("StatusBar", 10001)


@pytest.mark.parametrize("hxy", ["[0, 0, 0]", "[0, 0, 1]"])
def test_the_seen_1hxy_values_do_not_change_the_touch(hxy):
    edited = _edit(SAMSUNG36, STATUS, "1HXY=[0, 0, 1]", f"1HXY={hxy}")
    found = read_window_at(edited, (540, 50))
    assert isinstance(found, InputWindow)
    assert (found.title, found.owner_uid) == ("StatusBar", 10001)


# Values the phone did not print: what they do to a touch is not known (0x1,
# seen on the app's own window only, is tested further down).
@pytest.mark.parametrize(
    "value",
    [
        "0x80000000",
        "0x00",
        "0x8 | FORCE_TRUSTED_OVERLAY",
        "NOT_KNOWN",
        "FORCE_TRUSTED_OVERLAY | NOT_KNOWN",
        "0x0 x",
        "0xg",
        "",
        "force_trusted_overlay",
        "FORCE_TRUSTED_OVERLAY | 0x400000",
        "0x40000",
        "0x10",
        "0x01",
        "0x1 | FORCE_TRUSTED_OVERLAY",
    ],
)
def test_an_unseen_samsung_flags_value_is_line_format(value):
    edited = _edit(SAMSUNG36, STATUS, "samsungFlags=0x0", f"samsungFlags={value}")
    assert read_window_at(edited, (540, 1170)) == LINE_FORMAT
    # Also on a window the caller calls the app's: only 0x1 is read there.
    found = read_window_at(edited, (540, 1170), owned=lambda _window: True)
    assert found == LINE_FORMAT
    assert getattr(found, "rule", None) == "samsung_fields"


@pytest.mark.parametrize(
    "value",
    [
        "[0,0][1080,2340]",
        "[0,0][0,0]",
        "[0,0][1080]",
        "<empty> x",
        "",
        "[0, 0][1, 1]",
    ],
)
def test_an_unseen_p_touchable_region_is_line_format(value):
    edited = _edit(
        SAMSUNG36, STATUS, "pTouchableRegion=<empty>", f"pTouchableRegion={value}"
    )
    assert read_window_at(edited, (540, 1170)) == LINE_FORMAT


@pytest.mark.parametrize(
    "value",
    [
        "[12, 34, 2]",
        "[0, 0, 2]",
        "[1, 0, 0]",
        "[0, 1, 1]",
        "[0, 0]",
        "[0, 0, 0, 0]",
        "[0,0,1]",
        "[0, 0, x]",
        "[0, 0, 1] x",
    ],
)
def test_an_unseen_1hxy_is_line_format(value):
    edited = _edit(SAMSUNG36, STATUS, "1HXY=[0, 0, 1]", f"1HXY={value}")
    assert read_window_at(edited, (540, 1170)) == LINE_FORMAT


@pytest.mark.parametrize(
    "field", ["samsungFlags=0x0, ", "pTouchableRegion=<empty>, ", "1HXY=[0, 0, 1], "]
)
def test_a_row_without_one_samsung_field_is_line_format(field):
    assert read_window_at(_edit(SAMSUNG36, STATUS, field, ""), (540, 1170)) == (
        LINE_FORMAT
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # samsungFlags= away from right after inputConfig=
        (
            "samsungFlags=0x0, alpha=1",
            "alpha=1, samsungFlags=0x0",
        ),
        # pTouchableRegion= and 1HXY= away from right after touchableRegion=
        (
            "pTouchableRegion=<empty>, 1HXY=[0, 0, 1], ownerPid=20002",
            "ownerPid=20002, pTouchableRegion=<empty>, 1HXY=[0, 0, 1]",
        ),
        (
            "pTouchableRegion=<empty>, 1HXY=[0, 0, 1]",
            "1HXY=[0, 0, 1], pTouchableRegion=<empty>",
        ),
    ],
)
def test_a_samsung_field_out_of_its_place_is_line_format(old, new):
    edited = _edit(SAMSUNG36, STATUS, old, new)
    assert read_window_at(edited, (540, 1170)) == LINE_FORMAT


SAMSUNG_TEXT = "samsungFlags=0x0, pTouchableRegion=<empty>, 1HXY=[0, 0, 0]"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("applicationInfo.name=,", f"applicationInfo.name=Label, {SAMSUNG_TEXT},"),
        ("FloatingMenu,", f"FloatingMenu, {SAMSUNG_TEXT},"),
        ("FloatingMenu,", "FloatingMenu 1HXY=[0, 0, 0],"),
    ],
)
def test_samsung_field_text_on_an_emulator_row_is_line_format(old, new):
    # The fields written by an app in its title or label do not make an
    # ordinary API 36 row one of the Samsung form.
    edited = _edit(API36, "5ebeeda FloatingMenu", old, new)
    assert read_window_at(edited, (296, 458)) == LINE_FORMAT


def test_samsung_field_text_in_a_label_on_the_samsung_form_is_line_format():
    edited = _edit(
        SAMSUNG36,
        "abcdef0 com.example",
        "applicationInfo.name=ActivityRecord{",
        f"applicationInfo.name=Label, {SAMSUNG_TEXT}, ActivityRecord{{",
    )
    assert read_window_at(edited, (540, 1170)) == LINE_FORMAT


def test_a_field_name_glued_to_a_title_is_still_line_format():
    # Text in a title that ends in a field's name is refused, as before.
    edited = _edit(
        API36, "5ebeeda FloatingMenu", "FloatingMenu,", "FloatingMenu.auditownerUid=7,"
    )
    assert read_window_at(edited, (296, 458)) == LINE_FORMAT


def test_an_unknown_input_config_name_is_still_line_format():
    edited = _edit(SAMSUNG36, STATUS, "TRUSTED_OVERLAY,", "TRUSTED_OVERLAY | NEW,")
    assert read_window_at(edited, (540, 1170)) == LINE_FORMAT


def test_samsung_fields_on_an_older_form_are_line_format():
    # The named flags form (API 31) with the three fields on every row.
    edited = "\n".join(
        line.replace(
            ", ownerUid=",
            ", samsungFlags=0x0, pTouchableRegion=<empty>, 1HXY=[0, 0, 0], ownerUid=",
        )
        for line in API31.split("\n")
    )
    assert read_window_at(edited, (160, 10)) == LINE_FORMAT


SAMSUNG_FAKES = (
    *FAKES,
    "samsungFlags=0x0, touchableRegion=[0,0][1080,2340]",
    "pTouchableRegion=<empty>, 1HXY=[0, 0, 0], ownerUid={uid}",
    SAMSUNG_TEXT,
)


@pytest.mark.parametrize("fake", SAMSUNG_FAKES)
def test_each_same_line_fake_is_line_format_on_the_samsung_form(fake):
    written = fake.format(uid=10002)
    edited = _edit(SAMSUNG36, STATUS, "StatusBar,", f"StatusBar, {written}, x,")
    assert read_window_at(edited, (540, 50)) == LINE_FORMAT


@pytest.mark.parametrize(
    "fake",
    [
        " samsungFlags=0x0",
        "=samsungFlags=0x0",
        "xsamsungFlags=0x0",
        " touchableRegion=<empty>",
        ".1HXY=[0, 0, 0]",
    ],
)
def test_a_field_name_in_a_name_counts_on_the_samsung_form(fake):
    edited = _edit(SAMSUNG36, STATUS, "StatusBar,", f"StatusBar{fake},")
    assert read_window_at(edited, (540, 50)) == LINE_FORMAT


# --- samsungFlags=0x1: read only on the app's own window ---

APP = "com.example.app/com.example.app.MainActivity"
APP_ROW = "abcdef0 com.example.launcher/com.example.launcher.HomeActivity"
APP_UID = 10123


def _app_on_samsung(flags: str = "0x0") -> str:
    """samsung36.txt with its full-screen activity row (row 11) made the
    window of a made-up app (`APP`, uid `APP_UID`) printing
    ``samsungFlags=<flags>``."""
    text = _edit(SAMSUNG36, APP_ROW, "samsungFlags=0x0", f"samsungFlags={flags}")
    text = _edit(text, APP_ROW, "ownerUid=10002", f"ownerUid={APP_UID}")
    return _edit(text, APP_ROW, APP_ROW, f"abc123 {APP}")


def _is_app(window: InputWindow) -> bool:
    """The crawl's test for a window of an app with a uid of its own."""
    return window.owner_uid == APP_UID


def test_samsung_flags_0x1_on_the_apps_own_window_is_read():
    text = _app_on_samsung("0x1")
    found = read_window_at(text, (540, 1170), owned=_is_app)
    assert isinstance(found, InputWindow), found
    assert (found.title, found.owner_uid, found.samsung_0x1) == (APP, APP_UID, True)
    assert found.takes_touch
    # The same as without the value, but for the mark.
    plain = read_window_at(_app_on_samsung(), (540, 1170), owned=_is_app)
    assert isinstance(plain, InputWindow)
    assert not plain.samsung_0x1
    windows = parse_input_windows(text, owned=_is_app)
    assert not isinstance(windows, str)
    assert explain_input_windows(text, owned=_is_app).reason is None


def test_samsung_flags_0x1_without_knowing_the_app_is_refused():
    text = _app_on_samsung("0x1")
    for found in (
        read_window_at(text, (540, 1170)),
        parse_input_windows(text),
        read_window_at(text, (540, 1170), owned=lambda _window: False),
    ):
        assert found == LINE_FORMAT
        assert getattr(found, "rule", None) == "samsung_flags"
    verdict = explain_input_windows(text)
    assert (verdict.reason, verdict.rule) == (LINE_FORMAT, "samsung_flags")
    assert verdict.line == text.split("\n").index(_line(text, APP))


@pytest.mark.parametrize(
    ("marker", "point"),
    [
        (STATUS, (540, 50)),  # another app's window at the point
        (STATUS, (540, 1170)),  # above the app's window, not at the point
        ("abcdef1 NavigationBar0", (540, 1170)),
        ("13: name=Wallpaper wrapper#66", (540, 1170)),  # below it, taking no touch
        ("abcdef7 com.example.wallpaper", (540, 1170)),
    ],
)
def test_samsung_flags_0x1_on_another_apps_window_is_refused(marker, point):
    base = _app_on_samsung("0x1")
    row = _line(base, marker)
    text = base.replace(row, re.sub(r"samsungFlags=[^,]*", "samsungFlags=0x1", row))
    assert _line(text, marker).count("samsungFlags=0x1,") == 1
    found = read_window_at(text, point, owned=_is_app)
    assert found == LINE_FORMAT
    assert getattr(found, "rule", None) == "samsung_flags"
    # Only on another app's window: the app's own 0x1 alone is read.
    alone = read_window_at(base, point, owned=_is_app)
    assert isinstance(alone, InputWindow)


def test_samsung_flags_0x1_needs_the_owner_test_not_the_title():
    # A window titled like the app but owned by another uid is not the app's.
    text = _edit(_app_on_samsung("0x1"), APP, f"ownerUid={APP_UID}", "ownerUid=10001")
    found = read_window_at(text, (540, 1170), owned=_is_app)
    assert found == LINE_FORMAT
    assert getattr(found, "rule", None) == "samsung_flags"


def test_samsung_flags_0x1_in_another_displays_list_is_refused():
    cover = (
        "  Display: 1\n"
        "    logicalSize=100x100\n"
        "        transform (ROT_0) (IDENTITY)\n"
        "    Windows:\n"
        + _line(_app_on_samsung("0x1"), APP)
        .replace("      11: ", "      0: ")
        .replace("displayId=0", "displayId=1")
        + "\n      transform (ROT_0) (IDENTITY)\n"
    )
    text = SAMSUNG36.replace("  Display: 0\n", cover + "  Display: 0\n")
    found = read_window_at(text, (540, 1170), owned=lambda _window: True)
    assert found == LINE_FORMAT
    assert getattr(found, "rule", None) == "samsung_fields"


@pytest.mark.parametrize(
    "text",
    [API24, API30, API31, API32, API33, API34, API35, API36, SAMSUNG36],
    ids=["api24", "api30", "api31", "api32", "api33", "api34", "api35", "api36", "s36"],
)
@pytest.mark.parametrize("owned", [None, lambda _w: True, lambda _w: False])
def test_the_owner_test_changes_nothing_without_samsung_flags_0x1(text, owned):
    assert parse_input_windows(text, owned=owned) == parse_input_windows(text)
    for point in [(160, 320), (160, 10), (540, 1170), (540, 50)]:
        found = read_window_at(text, point, owned=owned)
        assert found == read_window_at(text, point)
        assert getattr(found, "rule", None) == getattr(
            read_window_at(text, point), "rule", None
        )
