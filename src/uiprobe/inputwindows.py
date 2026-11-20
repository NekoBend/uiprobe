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
and ``ownerUid=``. On API 24 to 28 there is one list of every display; on API
29 to 36 the windows are listed per display (``Display: <id>``, right above
``Windows:`` on API 29 to 32; on API 36 with ``logicalSize=`` and the
display's own ``transform`` line in between; API 33 to 35 also print two lines
there, which were not captured). Anything that does not read as one of these
forms makes the whole list unknown, because a line that cannot be read could
be the window on top: a window line of no known form, one with a read field
twice (a window's title and ``applicationInfo.name`` are printed as the app
gave them, so text there must not pass for a field), a field whose value is
followed by more text before the next ``, key=`` field, a flag name that is
not known, row numbers that do not run 0, 1, 2, ..., any other line inside the
list, more than one list that could be the display's, a list that does not end
at a line it is known to end at (`_LIST_END`), rows of one list that differ in
form (a quoted or bare name, or which of `_FORM_FIELDS` they print; on the hex
form ``scale=`` alone for API 24 to 28, or ``globalScale=``, ``windowScale=``
and ``portalToDisplayId=`` together for API 29 and 30) or in having transform
lines, two ``Display:`` sections of the same number (``Display: 00`` and
``Display: 0``), a line outside every window list that holds ``ownerUid=``, or
a line of another display's list of no known form (those lists are read too).
The list's end, the rows' form, the stray owner and the other displays' lists
matter because a window name may hold line breaks: it could fake a whole row
and the end of the list, and the real rest of its line, with the real owner
and display, would then land outside the list or in another display's list, or
start a row whose number another row has. One case remains (a documented
limit): the lowest window of the list, when no window above it takes the
point, can fake a whole row in its name, and the real rest of its line then
reads as one more row below it.

The legacy dispatcher (API 24 to 32) gives a touch anywhere on the display to
a visible touchable window that is "touch modal", one with neither
``NOT_FOCUSABLE`` nor ``NOT_TOUCH_MODAL`` (AOSP ``findTouchedWindowAtLocked``
of Android 11 and 12), whatever its touchable region; such a window holds
every point here. The ``inputConfig`` forms have no such flag, and their
region is read as printed. A window with ``portalToDisplayId`` other than -1
forwards a touch to another display, whose windows are not read: a hit on it
is `PORTAL`; on the forms that print the field (API 29 to 32: the hex form
with ``globalScale=``, and the named flags form) a line without it is
`LINE_FORMAT`.

The display's rotation: on API 36 the touchable regions are printed in the
display's unrotated coordinates and the dispatcher turns a tap's point by the
display's transform before it compares (a rotated API 36 emulator printed the
status bar at [296,0][320,640] on a display 640 wide and 320 high), so they
cannot be compared with the tap's point as given. On API 24 the regions follow
the rotation (the same emulator printed [0,0][640,24]), so a rotated display
would be read right there; but the forms in between were not seen rotated, so
every form is treated alike and a rotated display gives `ROTATION`. The
orientation comes from the input reader's ``Viewport`` lines of the display,
which are part of the same ``dumpsys input`` (API 24 to 28: ``Viewport:
displayId=0, orientation=N, ...``, on API 26 and 28 also with ``uniqueId=''``;
API 29 to 36: ``Viewport INTERNAL: displayId=0, ..., orientation=N, ...``;
on most versions more than once; lines of other displays, such as
``displayId=-1``, are not read). Only lines above the dispatcher's part are
read, where no window title is printed. Every line of the display must be of
one of these forms, say 0 and have its ``logicalFrame`` equal to its
``physicalFrame`` (a display that is not scaled or moved); a line of another
form, none at all, or lines that disagree are `ROTATION` as well. In
addition a ``transform`` line in the list whose rotation is not ``ROT_0``, a
``displayOrientation=`` other than 0 (API 32) or a display transform that is
not ``(ROT_0) (IDENTITY)`` (API 36; a display scaled with ``wm size`` is not
the identity either) is `ROTATION`, and so is a display section that prints
``logicalSize=`` (as API 36 does) without a display transform line.
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
# Fields every row of one version printed: which of them a row has tells its
# form, and all rows of a list must have the same (`_form`).
_FORM_FIELDS: Final = (
    "id",
    "portalToDisplayId",
    "paused",
    "hasFocus",
    "focusable",
    "hasWallpaper",
    "visible",
    "canReceiveKeys",
    "alpha",
    "flags",
    "inputConfig",
    "type",
    "layer",
    "frame",
    "scale",
    "globalScale",
    "windowScale",
    "applicationInfo.name",
    "inputFeatures",
    "ownerPid",
)
# A field's value ends where the next field begins (or the line ends), so a
# value is read whole: text after it that is not another field is no form.
_NEXT = r"(?=, [A-Za-z][A-Za-z0-9_.]*=|$)"
_LINE = re.compile(
    r"^(?P<index>0|[1-9]\d{0,4}): name=(?:'(?P<quoted>[^']*)'|(?P<bare>.*?)), "
    rf"(?:id=\d{{1,10}}, )?displayId=(?P<display>-?\d{{1,10}}){_NEXT}"
)
_NUMBER: Final = r"-?\d{1,7}"
_REGION = re.compile(
    rf", touchableRegion=(?:(?P<empty><empty>)|\[(?P<l>{_NUMBER}),(?P<t>{_NUMBER})\]"
    rf"\[(?P<r>{_NUMBER}),(?P<b>{_NUMBER})\]){_NEXT}"
)
_UID = re.compile(rf", ownerUid=(?P<uid>\d{{1,10}}){_NEXT}")
_INPUT_CONFIG = re.compile(rf", inputConfig=(?P<names>[^,]*){_NEXT}")
_HEX_FLAGS = re.compile(rf", flags=0x(?P<hex>[0-9a-fA-F]{{1,8}}){_NEXT}")
_NAMED_FLAGS = re.compile(rf", flags=(?P<names>[^,]*){_NEXT}")
_VISIBLE = re.compile(rf", visible=(?P<visible>true|false){_NEXT}")
_PORTAL = re.compile(rf", portalToDisplayId=(?P<to>-?\d{{1,10}}){_NEXT}")
_LINE_ORIENTATION = re.compile(r"displayOrientation=(\d+)")
_TRANSFORM = re.compile(r"^transform \(ROT_(?P<rot>\d+)\) \((?P<kind>[A-Z ]+)\)$")
_MATRIX_ROW = re.compile(r"^-?\d+\.\d+(?: +-?\d+\.\d+){2}$")
_INDEX = re.compile(r"^\d+:")
_DISPLAY = re.compile(r"^Display: -?\d{1,10}$")
_RECT: Final = r"-?\d{1,7}, -?\d{1,7}, -?\d{1,7}, -?\d{1,7}"
# The input reader's Viewport line of a display, in the forms of API 24 to 28
# (API 26 and 28 also list ``Viewport: ..., uniqueId='', ...``) and of API 29
# on (``Viewport INTERNAL: ..., uniqueId=..., port=...``, isActive from 30).
_VIEWPORT_CLAIM = re.compile(r"^\s*Viewport\b.*?\bdisplayId=(?P<display>-?\d+)\b")
_VIEWPORT = re.compile(
    r"\s*Viewport(?: [A-Z]+)?: displayId=(?P<display>-?\d{1,10}), "
    r"(?:uniqueId=[^,\s]*, port=(?:\d{1,10}|<none>), )?"
    r"orientation=(?P<orientation>[0-3]), (?:uniqueId='[^']*', )?"
    rf"logicalFrame=\[(?P<logical>{_RECT})\], "
    rf"physicalFrame=\[(?P<physical>{_RECT})\], deviceSize=\[\d{{1,7}}, \d{{1,7}}\]"
    r"(?:, isActive=\[[01]\])?"
)
# The lines that may follow a window list (the captures' lists all ended at
# one of the first four; a list per display may be followed by the next).
_LIST_END = re.compile(
    r"^(?:MonitoringChannels:|mMaximumObscuringOpacityForTouch: .*|"
    r"Global monitors (?:in|on) display -?\d{1,10}:|Connections:|Display: -?\d{1,10})$"
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
    problem: str | None = None  # why the list cannot be read as a whole
    others: tuple[_List, ...] = ()  # the lists of the other displays


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


def _listed(lines: list[str], head: int) -> tuple[list[int], bool]:
    """The window list that ``lines[head]`` begins, and whether it ends well.

    The list is the non-blank lines more indented than ``lines[head]``, and
    ``transform`` lines at any indent (API 35 prints them at the indent of
    ``Windows:``), up to the first other line. That line must be one a list
    is known to end at (`_LIST_END`); the end of the text is not.
    """
    level = _indent(lines[head])
    taken: list[int] = []
    for i in range(head + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if _indent(line) <= level and not line.lstrip().startswith("transform ("):
            return taken, _LIST_END.match(line.strip()) is not None
        taken.append(i)
    return taken, False


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
    at most one ``Windows:`` list (API 29 on): never both, the display's
    section exactly once. A ``Windows:`` list deeper down (the windows a
    touch in progress went to) is not one of them.

    A window name is printed as the app gave it and may hold line breaks, so
    a name could fake a whole row and the end of the list (or a new section):
    the real rest of its line, with the real ``displayId=`` and
    ``ownerUid=``, then lands outside the list or in another display's list.
    So every list must end at a line it is known to end at, the other
    displays' lists are read too (`_List.others`), and a line outside every
    list that holds ``ownerUid=`` (no other line of ``dumpsys input`` has it
    in the captures) makes the list `LINE_FORMAT`.
    """
    start = _dispatcher_start(lines)
    if start is None:
        return None
    found, level = _under(lines, start)
    tops = [i for i in found if _indent(lines[i]) == level]
    lists = _heads(lines, tops, level)
    sections = [i for i in tops if _DISPLAY.match(lines[i].strip())]
    if lists and not sections:
        if len(lists) != 1 or lines[lists[0]].strip() != "Windows:":
            return None
        taken, ended = _listed(lines, lists[0])
        problem = _stray(lines, taken, ended=ended)
        return _List([lines[i] for i in taken], None, rotated=False, problem=problem)
    ids = [_display_of(lines[i]) for i in sections]
    if len(set(ids)) != len(ids):  # "Display: 00" and "Display: 0" included
        return None
    ours = [i for i, shown in zip(sections, ids, strict=True) if shown == display_id]
    if lists or len(ours) != 1:
        return None
    return _sections(lines, sections, ours[0], display_id)


def _sections(
    lines: list[str], sections: list[int], ours: int, display_id: int
) -> _List | None:
    """The list of the ``Display:`` section at `ours`, with the lists of the
    other sections at `sections` (see `_window_list`)."""
    every: list[int] = []
    ended_all = True
    mine: _List | None = None
    others: list[_List] = []
    for head in sections:
        section, inner = _under(lines, head)
        heads = _heads(lines, section, inner)
        listed = [i for i in heads if lines[i].strip() == "Windows:"]
        if len(heads) > 1 or (head == ours and len(listed) != 1):
            return None
        if not listed:
            continue
        taken, ended = _listed(lines, listed[0])
        every += taken
        ended_all = ended_all and ended
        above = [lines[i].strip() for i in section if i < listed[0]]
        transforms = [line for line in above if line.startswith("transform ")]
        sized = any(line.startswith("logicalSize=") for line in above)
        rotated = (
            len(transforms) > 1
            or (sized and not transforms)
            or any(line != _IDENTITY for line in transforms)
        )
        shown = _display_of(lines[head])
        read = _List([lines[i] for i in taken], shown, rotated=rotated)
        if head == ours:
            mine = read
        else:
            others.append(read)
    if mine is None:
        return None
    problem = _stray(lines, every, ended=ended_all)
    return _List(
        mine.lines,
        display_id,
        rotated=mine.rotated,
        problem=problem,
        others=tuple(others),
    )


def _display_of(line: str) -> int:
    """The display id of a ``Display: <id>`` line, as a number."""
    return int(line.strip().removeprefix("Display: "))


def _stray(lines: list[str], inside: list[int], *, ended: bool) -> str | None:
    """`LINE_FORMAT` when a list did not end well or a line outside the lists
    (indices `inside`) holds ``ownerUid=``; None otherwise."""
    taken = set(inside)
    stray = any("ownerUid=" in line for i, line in enumerate(lines) if i not in taken)
    return LINE_FORMAT if stray or not ended else None


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


def _hex_form(line: str) -> bool | None:
    """Whether a line of the hex flags form is of API 29 and 30 (with
    ``globalScale=``, ``windowScale=`` and ``portalToDisplayId=``) rather
    than of API 24 to 28 (with ``scale=``); None when it is neither whole."""
    legacy = ", scale=" in line
    newer = [
        ", globalScale=" in line,
        ", windowScale=" in line,
        ", portalToDisplayId=" in line,
    ]
    if legacy and not any(newer):
        return False
    if all(newer) and not legacy:
        return True
    return None


def _touch(line: str) -> tuple[bool, bool, bool] | None:
    """Whether the window takes touches, whether it is touch modal and whether
    its form prints ``portalToDisplayId`` (API 29 to 32); None when the line
    has no known form."""
    config = _INPUT_CONFIG.search(line)
    visible = _VISIBLE.search(line)
    hex_flags = _HEX_FLAGS.search(line)
    named = _NAMED_FLAGS.search(line)
    if config is not None:  # API 36
        names = _names(config["names"], _INPUT_CONFIG_NAMES)
        if names is None or visible is not None or named is not None:
            return None
        return not names & _NOT_TAKING, False, False
    if visible is None:
        return None
    shown = visible["visible"] == "true"
    if hex_flags is not None:  # API 24 to 30
        bits = int(hex_flags["hex"], 16)
        takes = shown and not bits & _FLAG_NOT_TOUCHABLE
        not_modal = bits & (_FLAG_NOT_FOCUSABLE | _FLAG_NOT_TOUCH_MODAL)
        newer = _hex_form(line)
        return None if newer is None else (takes, takes and not not_modal, newer)
    names = None if named is None else _names(named["names"], _FLAG_NAMES)
    if names is None:  # neither form
        return None
    takes = shown and "NOT_TOUCHABLE" not in names  # API 31 and 32
    return takes, takes and not names & {"NOT_FOCUSABLE", "NOT_TOUCH_MODAL"}, True


def _window(line: str, name: str) -> InputWindow | str:
    """Read one window line (stripped) whose name field is `name`."""
    touch = _touch(line)
    uid = _UID.search(line)
    portal = _PORTAL.search(line)
    if touch is None or uid is None:
        return LINE_FORMAT
    if portal is None and ("portalToDisplayId=" in line or touch[2]):
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
    takes, modal, _ = touch
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
    if found is None:
        return NO_LIST
    if found.problem is not None:
        return found.problem
    for other in found.others:  # read as well: a name could have made them
        read = _read(other, display_id if other.display is None else other.display)
        if read in {LINE_FORMAT, REGION_FORMAT}:
            return read
    return _read(found, display_id)


def _form(line: str) -> tuple[bool, tuple[str, ...]]:
    """What tells the form of a window line apart: whether its name is quoted
    and which of `_FORM_FIELDS` it has. Every row of a list must agree (they
    did in every capture)."""
    quoted = line.partition(": name=")[2].startswith("'")
    return quoted, tuple(key for key in _FORM_FIELDS if f", {key}=" in line)


def _read(found: _List, display_id: int) -> tuple[InputWindow, ...] | str:
    """Read the windows of `display_id` in the list `found` (see
    `parse_input_windows`)."""
    groups = _group(found.lines)
    if (
        groups is None
        or len({_form(line) for line, _ in groups}) > 1
        or len({bool(rest) for _, rest in groups}) > 1  # transform lines: all or none
    ):
        return LINE_FORMAT
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
    dispatcher's part of ``dumpsys input``. Every line that names the display
    must be of a known form and have its logical frame equal to its physical
    frame (a display that is not scaled or moved), and all must say the same
    orientation.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    start = _dispatcher_start(lines)
    if start is None:
        return None
    said: set[int] = set()
    for line in lines[:start]:
        claim = _VIEWPORT_CLAIM.match(line)
        if claim is None or int(claim["display"]) != display_id:
            continue
        match = _VIEWPORT.fullmatch(line)
        if match is None or match["logical"] != match["physical"]:
            return None
        said.add(int(match["orientation"]))
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
