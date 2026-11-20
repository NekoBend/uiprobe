"""Which window a touch at a point would reach, read from ``dumpsys input``.

The input dispatcher lists the windows of a display from the top down, each
with whether it takes touches, the region that takes them and the uid of the
app that owns it. The first window that takes touches at a point is the one a
tap there reaches; a window of another app can take touches without taking
the input focus (an overlay drawn over other apps, the keyboard, a floating
button), so the focused window alone does not tell. Windows that only watch
touches (spy windows such as the system's gesture monitors) and copies of a
touch sent to the wallpaper are not looked at.

Four forms of the window line were seen on emulators (API 24 to 36):

* API 24 to 30: ``name='Window{<hash> u0 <title>}'``, ``visible=true|false``,
  ``flags=0x<hex>`` (window manager flags: ``FLAG_NOT_FOCUSABLE`` 0x8,
  ``FLAG_NOT_TOUCHABLE`` 0x10, ``FLAG_NOT_TOUCH_MODAL`` 0x20); API 29 and 30
  add ``portalToDisplayId=``.
* API 31 and 32: ``name='<hash> <title>'``, ``visible=true|false``,
  ``flags=NOT_FOCUSABLE | NOT_TOUCHABLE | ...`` (names), ``portalToDisplayId=``;
  each window line is followed by a ``transform (ROT_<n>) (<kind>)`` line and,
  unless the kind is ``IDENTITY``, three rows of its matrix. API 32 adds
  ``displayOrientation=`` to the window line.
* API 33 and 34: the quoted name of API 31 with ``inputConfig=NOT_VISIBLE |
  NOT_TOUCHABLE | NO_INPUT_CHANNEL | SPY | ...`` (names; no ``visible=`` and
  no ``flags=``) and the same ``transform`` lines.
* API 35 and 36: ``name=<hash> <title>`` (not quoted) with ``inputConfig=``
  (names, or ``0x0``) and the same ``transform`` lines (API 35 prints them at
  the indent of ``Windows:``, so they are taken as part of the list at any
  indent).

All four have ``displayId=``, ``touchableRegion=[l,t][r,b]`` or ``<empty>``
and ``ownerUid=``. On API 34 to 36 the windows are listed per display
(``Display: <id>``, on API 36 with the display's own ``transform`` line above
its ``Windows:``); on API 24 to 28 there is one list of every display; on API
29 to 33 the lines above ``Windows:`` were not captured, and both layouts are
read. Anything that does not read as one of these forms makes the whole list
unknown, because a line that cannot be read could be the window on top: a
window line of no known form, one with a read field twice (a window's title
and ``applicationInfo.name`` are printed as the app gave them, so text there
must not pass for a field), a flag name that is not known, row numbers that
do not run 0, 1, 2, ..., any other line inside the list, or more than one
list that could be the display's.

The legacy dispatcher (API 24 to 32) gives a touch anywhere on the display to
a visible touchable window that is "touch modal", one with neither
``NOT_FOCUSABLE`` nor ``NOT_TOUCH_MODAL`` (AOSP ``findTouchedWindowAtLocked``
of Android 11 and 12), whatever its touchable region; such a window holds
every point here. The ``inputConfig`` forms have no such flag, and their
region is read as printed. A window with ``portalToDisplayId`` other than -1
forwards a touch to another display, whose windows are not read: a hit on it
is `PORTAL`.

The display's rotation: on API 36 the touchable regions are printed in the
display's unrotated coordinates and the dispatcher turns a tap's point by the
display's transform before it compares (a rotated API 36 emulator printed the
status bar at [296,0][320,640] on a display 640 wide and 320 high), so they
cannot be compared with the tap's point as given. On API 24 the regions follow
the rotation (the same emulator printed [0,0][640,24]), so a rotated display
would be read right there; but the forms in between were not seen rotated, so
every form is treated alike and a rotated display gives `ROTATION`. The
orientation comes from the input reader's ``Viewport`` lines of the display,
which are part of the same ``dumpsys input`` (API 24: ``Viewport: displayId=0,
orientation=N, ...``; API 33 to 36: ``Viewport INTERNAL: displayId=0, ...,
orientation=N, ...``, on API 34 to 36 more than once, one per input device;
lines of other displays, such as ``displayId=-1``, are not read). Only lines
above the dispatcher's part are read, where no window title is printed. Every
such line must say 0: none (not checked on API 26 to 32, whose captures kept
only the window list) or lines that disagree are `ROTATION` as well. In
addition a ``transform`` line in the list whose rotation is not ``ROT_0``, a
``displayOrientation=`` other than 0 (API 32) or a display transform that is
not ``(ROT_0) (IDENTITY)`` (API 36; a display scaled with ``wm size`` is not
the identity either) is `ROTATION`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "LINE_FORMAT",
    "NO_LIST",
    "NO_WINDOW",
    "PORTAL",
    "REGION_FORMAT",
    "ROTATION",
    "InputWindow",
    "display_orientation",
    "parse_input_windows",
    "read_window_at",
    "window_at",
]

# Why the window at a point is not known (a short word for the records).
NO_LIST: Final = "no_list"
LINE_FORMAT: Final = "line_format"
REGION_FORMAT: Final = "region_format"
NO_WINDOW: Final = "no_window"
ROTATION: Final = "rotation"
PORTAL: Final = "portal"

_FLAG_NOT_FOCUSABLE: Final = 0x8
_FLAG_NOT_TOUCHABLE: Final = 0x10
_FLAG_NOT_TOUCH_MODAL: Final = 0x20
# inputConfig names of windows that do not take a touch at their region: not
# shown, refusing touches, without an input channel, or only watching (a spy
# window sees touches that still go on to the window below).
_NOT_TAKING: Final = frozenset(
    {"NOT_VISIBLE", "NOT_TOUCHABLE", "NO_INPUT_CHANNEL", "SPY"}
)
# The inputConfig names (API 33 on) that are read: those seen on the API 33 to
# 36 emulators (NOT_FOCUSABLE, NOT_TOUCHABLE, NOT_VISIBLE, NO_INPUT_CHANNEL,
# SPY, TRUSTED_OVERLAY, SLIPPERY, IS_WALLPAPER, INTERCEPTS_STYLUS,
# WATCH_OUTSIDE_TOUCH, DUPLICATE_TOUCH_TO_WALLPAPER, PREVENT_SPLITTING) and
# the other names of AOSP's WindowInfo::InputConfig.
_INPUT_CONFIG_NAMES: Final = frozenset(
    {
        "NO_INPUT_CHANNEL",
        "NOT_VISIBLE",
        "NOT_FOCUSABLE",
        "NOT_TOUCHABLE",
        "PREVENT_SPLITTING",
        "DUPLICATE_TOUCH_TO_WALLPAPER",
        "IS_WALLPAPER",
        "PAUSE_DISPATCHING",
        "TRUSTED_OVERLAY",
        "WATCH_OUTSIDE_TOUCH",
        "SLIPPERY",
        "DISABLE_USER_ACTIVITY",
        "DROP_INPUT",
        "DROP_INPUT_IF_OBSCURED",
        "SPY",
        "INTERCEPTS_STYLUS",
        "GLOBAL_STYLUS_BLOCKS_TOUCH",
        "SENSITIVE_FOR_PRIVACY",
    }
)
# The flag names of API 31 and 32 (WindowManager.LayoutParams.FLAG_* without
# the prefix): those seen on the emulators (NOT_FOCUSABLE, NOT_TOUCHABLE,
# NOT_TOUCH_MODAL, LAYOUT_IN_SCREEN, LAYOUT_NO_LIMITS, LAYOUT_INSET_DECOR,
# WATCH_OUTSIDE_TOUCH, SHOW_WALLPAPER, SPLIT_TOUCH, HARDWARE_ACCELERATED,
# DRAWS_SYSTEM_BAR_BACKGROUNDS, SCALED, SLIPPERY) and the other flags.
_FLAG_NAMES: Final = frozenset(
    {
        "ALLOW_LOCK_WHILE_SCREEN_ON",
        "DIM_BEHIND",
        "BLUR_BEHIND",
        "NOT_FOCUSABLE",
        "NOT_TOUCHABLE",
        "NOT_TOUCH_MODAL",
        "TOUCHABLE_WHEN_WAKING",
        "KEEP_SCREEN_ON",
        "LAYOUT_IN_SCREEN",
        "LAYOUT_NO_LIMITS",
        "FULLSCREEN",
        "FORCE_NOT_FULLSCREEN",
        "DITHER",
        "SECURE",
        "SCALED",
        "IGNORE_CHEEK_PRESSES",
        "LAYOUT_INSET_DECOR",
        "ALT_FOCUSABLE_IM",
        "WATCH_OUTSIDE_TOUCH",
        "SHOW_WHEN_LOCKED",
        "SHOW_WALLPAPER",
        "TURN_SCREEN_ON",
        "DISMISS_KEYGUARD",
        "SPLIT_TOUCH",
        "HARDWARE_ACCELERATED",
        "LAYOUT_IN_OVERSCAN",
        "TRANSLUCENT_STATUS",
        "TRANSLUCENT_NAVIGATION",
        "LOCAL_FOCUS_MODE",
        "SLIPPERY",
        "LAYOUT_ATTACHED_IN_DECOR",
        "DRAWS_SYSTEM_BAR_BACKGROUNDS",
    }
)
# Fields that must appear at most once in a window line: a second one is text
# of the title or of applicationInfo.name that reads like a field.
_ONCE: Final = (
    "ownerUid=",
    "touchableRegion=",
    "inputConfig=",
    "flags=",
    "visible=",
    "displayId=",
    "portalToDisplayId=",
)
_HEADER: Final = "Input Dispatcher State:"
_LINE = re.compile(
    r"^(?P<index>0|[1-9]\d{0,4}): name=(?:'(?P<quoted>[^']*)'|(?P<bare>.*?)), "
    r"(?:id=\d{1,10}, )?displayId=(?P<display>-?\d{1,10}),"
)
_NUMBER: Final = r"-?\d{1,7}"
_REGION = re.compile(
    rf", touchableRegion=(?:(?P<empty><empty>)|\[(?P<l>{_NUMBER}),(?P<t>{_NUMBER})\]"
    rf"\[(?P<r>{_NUMBER}),(?P<b>{_NUMBER})\])(?:, |$)"
)
_UID = re.compile(r", ownerUid=(?P<uid>\d{1,10})(?:,|$)")
_INPUT_CONFIG = re.compile(r", inputConfig=(?P<names>[^,]*),")
_HEX_FLAGS = re.compile(r", flags=0x(?P<hex>[0-9a-fA-F]{1,8}),")
_NAMED_FLAGS = re.compile(r", flags=(?P<names>[^,]*),")
_VISIBLE = re.compile(r", visible=(?P<visible>true|false),")
_PORTAL = re.compile(r", portalToDisplayId=(?P<to>-?\d{1,10}),")
_LINE_ORIENTATION = re.compile(r"displayOrientation=(\d+)")
_TRANSFORM = re.compile(r"^transform \(ROT_(?P<rot>\d+)\) \((?P<kind>[A-Z ]+)\)$")
_MATRIX_ROW = re.compile(r"^-?\d+\.\d+(?: +-?\d+\.\d+){2}$")
_INDEX = re.compile(r"^\d+:")
_DISPLAY = re.compile(r"^Display: -?\d{1,10}$")
_VIEWPORT = re.compile(
    r"^\s*Viewport(?: [A-Z]+)?: displayId=(?P<display>-?\d{1,10}), "
    r"(?:.*?, )?orientation=(?P<orientation>\d{1,2})(?:,|$)"
)
_WINDOW_TITLE = re.compile(r"^Window\{[0-9a-f]+ u\d+ (?P<title>.*)\}$")
_HASH_TITLE = re.compile(r"^[0-9a-f]+ (?P<title>.*)$")
_IDENTITY: Final = "transform (ROT_0) (IDENTITY)"
_MATRIX_ROWS: Final = 3


@dataclass(frozen=True, slots=True)
class InputWindow:
    """One window of the dispatcher's list.

    ``region`` is the touchable region as (left, top, right, bottom), right
    and bottom exclusive, or None when it is empty. ``takes_touch`` says
    whether the window takes a touch inside its region at all. ``modal``
    (API 24 to 32 only) says that it takes a touch anywhere on its display,
    whatever its region; ``portal`` that it forwards touches to another
    display.
    """

    title: str
    takes_touch: bool
    region: tuple[int, int, int, int] | None
    owner_uid: int
    modal: bool = False
    portal: bool = False

    def holds(self, point: tuple[int, int]) -> bool:
        """Whether a touch at `point` goes to this window."""
        if not self.takes_touch:
            return False
        if self.modal:
            return True
        if self.region is None:
            return False
        left, top, right, bottom = self.region
        x, y = point
        return left <= x < right and top <= y < bottom


@dataclass(frozen=True, slots=True)
class _List:
    """The lines of one window list and what is known about its display."""

    lines: list[str]
    display: int | None  # the list's ``Display:`` section; None: one list
    rotated: bool  # the section's display transform is not the identity


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _under(lines: list[str], start: int) -> tuple[list[int], int]:
    """The non-blank lines under ``lines[start]`` (more indented, up to the
    next line that is not), and the indent of the nearest (-1 when none)."""
    level = _indent(lines[start])
    found: list[int] = []
    for i in range(start + 1, len(lines)):
        if not lines[i].strip():
            continue
        if _indent(lines[i]) <= level:
            break
        found.append(i)
    nearest = min((_indent(lines[i]) for i in found), default=-1)
    return found, nearest


def _listed(lines: list[str], head: int) -> list[str]:
    """The non-blank lines of the window list that ``lines[head]`` begins:
    those more indented than it, and ``transform`` lines at any indent (API
    35 prints them at the indent of ``Windows:``), up to the first other."""
    level = _indent(lines[head])
    found: list[str] = []
    for line in lines[head + 1 :]:
        if not line.strip():
            continue
        if _indent(line) <= level and not line.lstrip().startswith("transform ("):
            break
        found.append(line)
    return found


def _dispatcher_start(lines: list[str]) -> int | None:
    """The index of the dispatcher's header line, None unless exactly one."""
    found = [i for i, line in enumerate(lines) if line.strip() == _HEADER]
    return found[0] if len(found) == 1 else None


def _heads(lines: list[str], found: list[int], level: int) -> list[int]:
    """The lines of `found` at indent `level` that begin a window list."""
    return [
        i
        for i in found
        if _indent(lines[i]) == level and lines[i].strip().startswith("Windows:")
    ]


def _window_list(lines: list[str], display_id: int) -> _List | None:
    """The window list of `display_id` in the dispatcher's part, or None when
    there is none or it cannot be told which one it is.

    Either one ``Windows:`` list directly under the header (API 24 to 28,
    every display in one list) or ``Display: <id>`` sections there, each with
    one ``Windows:`` list (API 36): never both, the display's at most once.
    A ``Windows:`` list deeper down (the windows a touch in progress went to)
    is not one of them.
    """
    start = _dispatcher_start(lines)
    if start is None:
        return None
    found, level = _under(lines, start)
    lists = _heads(lines, found, level)
    sections = [
        i
        for i in found
        if _indent(lines[i]) == level and _DISPLAY.match(lines[i].strip())
    ]
    if lists and not sections:
        if len(lists) != 1 or lines[lists[0]].strip() != "Windows:":
            return None
        return _List(_listed(lines, lists[0]), None, rotated=False)
    ours = [i for i in sections if lines[i].strip() == f"Display: {display_id}"]
    if lists or len(ours) != 1:
        return None
    section, inner = _under(lines, ours[0])
    heads = _heads(lines, section, inner)
    if len(heads) != 1 or lines[heads[0]].strip() != "Windows:":
        return None
    transforms = [
        lines[i].strip()
        for i in section
        if i < heads[0] and lines[i].strip().startswith("transform ")
    ]
    rotated = len(transforms) > 1 or any(line != _IDENTITY for line in transforms)
    return _List(_listed(lines, heads[0]), display_id, rotated=rotated)


def _title(name: str) -> str:
    match = _WINDOW_TITLE.match(name) or _HASH_TITLE.match(name)
    return match["title"] if match else name


def _names(value: str, known: frozenset[str]) -> frozenset[str] | None:
    """The names of a ``A | B | C`` value, None when one is not in `known`
    (``0x0`` alone is no names)."""
    if value == "0x0":
        return frozenset()
    names = value.split(" | ")
    if not all(name in known for name in names):
        return None
    return frozenset(names)


def _touch(line: str) -> tuple[bool, bool] | None:
    """Whether the window takes touches and whether it is touch modal; None
    when the line has no known form."""
    config = _INPUT_CONFIG.search(line)
    visible = _VISIBLE.search(line)
    hex_flags = _HEX_FLAGS.search(line)
    named = _NAMED_FLAGS.search(line)
    if config is not None:  # API 36
        names = _names(config["names"], _INPUT_CONFIG_NAMES)
        if names is None or visible is not None or named is not None:
            return None
        return not names & _NOT_TAKING, False
    if visible is None:
        return None
    shown = visible["visible"] == "true"
    if hex_flags is not None:  # API 24 to 30
        bits = int(hex_flags["hex"], 16)
        takes = shown and not bits & _FLAG_NOT_TOUCHABLE
        not_modal = bits & (_FLAG_NOT_FOCUSABLE | _FLAG_NOT_TOUCH_MODAL)
        return takes, takes and not not_modal
    names = None if named is None else _names(named["names"], _FLAG_NAMES)
    if names is None:  # neither form
        return None
    takes = shown and "NOT_TOUCHABLE" not in names  # API 31 and 32
    return takes, takes and not names & {"NOT_FOCUSABLE", "NOT_TOUCH_MODAL"}


def _window(line: str, name: str) -> InputWindow | str:
    """Read one window line (stripped) whose name field is `name`."""
    touch = _touch(line)
    uid = _UID.search(line)
    portal = _PORTAL.search(line)
    if touch is None or uid is None:
        return LINE_FORMAT
    if portal is None and "portalToDisplayId=" in line:
        return LINE_FORMAT
    region = _REGION.search(line)
    if region is None:
        return REGION_FORMAT
    bounds = None
    if not region["empty"]:
        left, top = int(region["l"]), int(region["t"])
        right, bottom = int(region["r"]), int(region["b"])
        if left > right or top > bottom:
            return REGION_FORMAT
        bounds = (left, top, right, bottom)
    takes, modal = touch
    return InputWindow(
        _title(name),
        takes_touch=takes,
        region=bounds,
        owner_uid=int(uid["uid"]),
        modal=modal,
        portal=portal is not None and int(portal["to"]) != -1,
    )


def _group(lines: list[str]) -> list[tuple[str, list[str]]] | None:
    """Each window line of a list (stripped) with the lines that follow it up
    to the next window line; None when a line comes before the first."""
    groups: list[tuple[str, list[str]]] = []
    for raw in lines:
        line = raw.strip()
        if _INDEX.match(line):
            groups.append((line, []))
        elif not groups:
            return None
        else:
            groups[-1][1].append(line)
    return groups


def _turned(rest: list[str]) -> bool | None:
    """Whether the lines after a window line hold a rotated transform; None
    unless they are nothing or a ``transform`` line with its matrix rows
    (none for ``IDENTITY``, three for any other kind)."""
    if not rest:
        return False
    transform = _TRANSFORM.match(rest[0])
    if transform is None:
        return None
    rows = 0 if transform["kind"] == "IDENTITY" else _MATRIX_ROWS
    if len(rest) != 1 + rows or not all(_MATRIX_ROW.match(row) for row in rest[1:]):
        return None
    return int(transform["rot"]) != 0


def _row(
    line: str, index: int, found: _List, display_id: int
) -> InputWindow | str | None:
    """Read the window line of row `index` of `found`; None when it is of
    another display than `display_id` (only in a list of every display)."""
    match = _LINE.match(line)
    if match is None or int(match["index"]) != index:
        return LINE_FORMAT
    if any(line.count(key) > 1 for key in _ONCE):
        return LINE_FORMAT
    display = int(match["display"])
    if found.display is not None and display != found.display:
        return LINE_FORMAT
    if display != display_id:
        return None
    name = match["quoted"] if match["quoted"] is not None else match["bare"]
    return _window(line, name)


def parse_input_windows(
    text: str, display_id: int = 0
) -> tuple[InputWindow, ...] | str:
    """Read the windows of `display_id` from the top down.

    Returns the windows, or the reason they are not known: `NO_LIST` (no
    window list of that display, or no way to tell which list it is),
    `LINE_FORMAT` (a line in the list of no known form, a read field twice,
    an unknown flag name, a row number out of order), `REGION_FORMAT` (a
    touchable region that is not one rectangle or empty, or is reversed) or
    `ROTATION` (a transform or orientation in the list, or the display's
    transform, that is not the unrotated identity). The ``Viewport`` lines are
    read by `display_orientation`, not here.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    found = _window_list(lines, display_id)
    groups = None if found is None else _group(found.lines)
    if found is None or groups is None:
        return NO_LIST if found is None else LINE_FORMAT
    windows: list[InputWindow] = []
    rotated = found.rotated
    for index, (line, rest) in enumerate(groups):
        turned = _turned(rest)
        window = LINE_FORMAT if turned is None else _row(line, index, found, display_id)
        if isinstance(window, str):
            return window
        if window is not None:
            windows.append(window)
            orientations = _LINE_ORIENTATION.findall(line)
            rotated = rotated or turned or any(int(o) != 0 for o in orientations)
    if not windows:
        return NO_LIST
    return ROTATION if rotated else tuple(windows)


def display_orientation(text: str, display_id: int = 0) -> int | None:
    """Return the orientation of `display_id` (0 to 3, a quarter turn each),
    None when it cannot be read.

    Reads the input reader's ``Viewport`` lines of the display, above the
    dispatcher's part of ``dumpsys input``; they must all say the same.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    start = _dispatcher_start(lines)
    if start is None:
        return None
    said = {
        int(match["orientation"])
        for match in map(_VIEWPORT.match, lines[:start])
        if match is not None and int(match["display"]) == display_id
    }
    return next(iter(said)) if len(said) == 1 else None


def window_at(
    windows: tuple[InputWindow, ...], point: tuple[int, int]
) -> InputWindow | None:
    """The topmost window that takes a touch at `point`, None when none does."""
    return next((window for window in windows if window.holds(point)), None)


def read_window_at(
    text: str, point: tuple[int, int], display_id: int = 0
) -> InputWindow | str:
    """Return the window a touch at `point` of `display_id` reaches, read from
    the whole text of ``dumpsys input``, or why that is not known.

    The reason is one of `parse_input_windows`, `ROTATION` (the display's
    orientation is not 0 or cannot be read, see `display_orientation`),
    `NO_WINDOW` (no window takes a touch there) or `PORTAL` (the window there
    forwards the touch to another display).
    """
    windows = parse_input_windows(text, display_id)
    if isinstance(windows, str):
        return windows
    if display_orientation(text, display_id) != 0:
        return ROTATION
    found = window_at(windows, point)
    if found is None:
        return NO_WINDOW
    return PORTAL if found.portal else found
