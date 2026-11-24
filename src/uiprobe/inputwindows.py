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

A Samsung phone on Android 16 (API 36) printed the API 35 and 36 form with
three more fields: ``samsungFlags=`` right after ``inputConfig=``, and
``pTouchableRegion=`` and ``1HXY=`` right after ``touchableRegion=``; its
``inputConfig=`` also named ``DO_NOT_PILFER``. The
three fields are read only at those places of the fields after the name (not
in the name or ``applicationInfo.name``), come all together or not at all,
and must have one of the values the phone printed: ``samsungFlags=`` ``0x0``,
``0x8`` or ``FORCE_TRUSTED_OVERLAY``, ``pTouchableRegion=<empty>`` and
``1HXY=`` ``[0, 0, 0]`` or ``[0, 0, 1]``. With those values the window took
touches as its other fields say; what other values do to the delivery of a
touch is not known, so a row with one is `LINE_FORMAT`. The fields are named
by case (``samsungFlags=`` holds no ``flags=``, ``pTouchableRegion=`` no
``touchableRegion=``).

One more ``samsungFlags=`` value, ``0x1``, was printed by two Samsung phones
on the target app's own window only (Settings, with its own uid). What it
means is not known, so it is read only there: on a window of the list of the
display asked for that the caller says is the app's (``owned`` of
`read_window_at`, the same owner uid and title test the crawl uses for the
window at the point). Any other window with it, at the point or not, taking
touches or not, makes the list `LINE_FORMAT` (``samsung_flags``), and in
another display's list it is ``samsung_fields`` like any value not seen.
Other values (``FORCE_TRUSTED_OVERLAY | 0x400000``, ``0x40000``) stay
refused.

All of them have ``displayId=``, ``touchableRegion=[l,t][r,b]`` or ``<empty>``
and ``ownerUid=``. On API 24 to 28 there is one list of every display; on API
29 to 36 the windows are listed per display (``Display: <id>``, right above
``Windows:`` on API 29 to 32; on API 33 to 36 with ``logicalSize=`` and the
display's own ``transform`` line in between). Anything that does not read as
one of these forms makes the whole list unknown, because a line that cannot be
read could be the window on top: a window line of no known form, one with a
read field twice (a window's title and ``applicationInfo.name`` are printed as
the app gave them, so text there must not pass for a field), a field whose
value is followed by more text before the next ``, key=`` field, a flag name
that is not known, row numbers that do not run 0, 1, 2, ..., any other line
inside the list, more than one list that could be the display's, a list that
does not end at a line it is known to end at (`_LIST_END`), rows of one list
that differ in form (a quoted or bare name, or which of `_FORM_FIELDS` they
print after the name, so field-like text in a name or in
``applicationInfo.name`` does not count; on the hex form ``scale=`` alone for
API 24 to 28, or ``globalScale=``, ``windowScale=`` and ``portalToDisplayId=``
together for API 29 and 30) or in having transform lines, a list per display
with rows of API 24 to 28 (``scale=``), a field named in a line that does not
read as that field, two ``Display:`` sections of the same number (``Display:
00`` and ``Display: 0``), a line outside every window list that holds
``ownerUid=``, or a line of another display's list of no known form (those
lists are read too). The list's end, the rows' form, the stray owner and the
other displays' lists matter because a window name may hold line breaks: it
could fake a whole row and the end of the list, and the real rest of its line,
with the real owner and display, would then land outside the list or in
another display's list, or start a row whose number another row has. One case
remains (a documented limit): the lowest window of the list, when no window
above it takes the point, can fake a whole row in its name, and the real rest
of its line then reads as one more row below it.

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
``logicalSize=`` without a display transform line, or a list of the
``inputConfig`` forms (API 33 to 36, which printed ``logicalSize=`` and the
display transform above every list on the emulators) without both.
A rotation or orientation that is read but has more digits than `int`
converts (Python's default limit is 4300) is `ROTATION` too, wherever it is
read: in another display's list as well, and on the transform line of
another display's row in a list of every display (read before the row is
skipped; the ``displayOrientation=`` of such a row is not read, as before).
So is a ``Viewport`` line whose ``displayId=`` is that long. With leading
zeros such a number could still be 0, or display 0.

Every refusal also names the check that made it, one of `RULES` (the
``rule`` of `Unreadable`, which `parse_input_windows` and `read_window_at`
return); `explain_input_windows` adds the line it refused at, for
``uiprobe diagnose tap-window`` (`uiprobe.windowdiag`).
"""

from __future__ import annotations

import re

# Imported at run time, not only for type checking, so that
# typing.get_type_hints() resolves the ``owned`` parameters.
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass, field, replace
from typing import Final, Self

__all__ = [
    "LINE_FORMAT",
    "NO_LIST",
    "NO_WINDOW",
    "PORTAL",
    "REGION_FORMAT",
    "ROTATION",
    "RULES",
    "InputWindow",
    "ListSpan",
    "RowFields",
    "Unreadable",
    "Verdict",
    "display_orientation",
    "explain_input_windows",
    "parse_input_windows",
    "read_window_at",
    "window_at",
    "window_line_fields",
]

# Why the window at a point is not known (a short word for the records).
NO_LIST: Final = "no_list"
LINE_FORMAT: Final = "line_format"
REGION_FORMAT: Final = "region_format"
NO_WINDOW: Final = "no_window"
ROTATION: Final = "rotation"
PORTAL: Final = "portal"
# The checks that can refuse, each under its reason (`Unreadable.rule`,
# `Verdict.rule`); docs/commands.md says what each one means.
RULES: Final = {
    NO_LIST: (
        "no_dispatcher",
        "dispatcher_twice",
        "no_list",
        "lists_twice",
        "list_heading",
        "list_and_sections",
        "display_twice",
        "display_missing",
        "no_rows",
    ),
    LINE_FORMAT: (
        "list_end",
        "stray_owner",
        "orphan_line",
        "legacy_row_in_display_list",
        "form_mismatch",
        "transform_mismatch",
        "unknown_line",
        "transform_shape",
        "row_shape",
        "row_number",
        "field_twice",
        "display_mismatch",
        "field_value",
        "field_missing",
        "unknown_flag",
        "mixed_flags",
        "hex_form",
        "portal_missing",
        "samsung_fields",
        "samsung_flags",
    ),
    REGION_FORMAT: ("region_missing", "region_format", "region_reversed"),
    ROTATION: (
        "display_transform_missing",
        "display_transform",
        "row_rotated",
        "row_orientation",
        "viewport_malformed",
        "scaled",
        "viewport_missing",
        "viewport_disagree",
        "rotated",
        "number_too_long",
    ),
}

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
        # Printed by a Samsung phone (API 36) on spy windows, which do not take
        # a touch anyway; it does not change whether a window takes one.
        "DO_NOT_PILFER",
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
    "samsungFlags=",
    "pTouchableRegion=",
    "1HXY=",
)
# The fields of the Samsung form (API 36): all of them or none.
_SAMSUNG_KEYS: Final = ("samsungFlags=", "pTouchableRegion=", "1HXY=")
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
    "samsungFlags",
    "pTouchableRegion",
    "1HXY",
)
# A field's value ends where the next field begins (or the line ends), so a
# value is read whole: text after it that is not another field is no form.
_NEXT = r"(?=, [A-Za-z][A-Za-z0-9_.]*=|$)"
# A quoted name ends at the first quote followed by the metadata that comes
# after every name (``, id=N`` or ``, displayId=N``), so a quote in the title
# (``Today's Orders``) stays in it. A name that fakes that metadata inside it
# prints ``displayId=`` twice, which `_ONCE` refuses.
_LINE = re.compile(
    r"^(?P<index>0|[1-9]\d{0,4}): name=(?:'(?P<quoted>.*?)'|(?P<bare>.*?)), "
    rf"(?P<id>id=\d{{1,10}}, )?(?P<fields>displayId=(?P<display>-?\d{{1,10}})){_NEXT}"
)
_NUMBER: Final = r"-?\d{1,7}"
_APP_NAME = re.compile(r", applicationInfo\.name=.*?(?=, applicationInfo\.token=)")
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
# The Samsung fields (API 36) at their places in the fields after the name
# (`_meta`), with only the values the phone printed (see the module's text).
_SAMSUNG_FLAGS = re.compile(
    r", inputConfig=[^,]*, samsungFlags=(?P<value>0x0|0x8|0x1|FORCE_TRUSTED_OVERLAY)"
    + _NEXT
)
# The samsungFlags value seen only on the app's own window (see the module's
# text): accepted only on a window the caller says is the app's (``owned``).
_SAMSUNG_OWN_ONLY: Final = "0x1"
_SAMSUNG_REGION = re.compile(
    rf", touchableRegion=(?:<empty>|\[{_NUMBER},{_NUMBER}\]\[{_NUMBER},{_NUMBER}\]), "
    r"pTouchableRegion=<empty>, 1HXY=\[0, 0, [01]\]" + _NEXT
)
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
# The named groups other than display, orientation, logical and physical
# only tell `viewport_parts` which parts a line has; they match nothing more.
_VIEWPORT = re.compile(
    r"\s*Viewport(?: (?P<kind>[A-Z]+))?: displayId=(?P<display>-?\d{1,10}), "
    r"(?:uniqueId=(?P<unique>[^,\s]*), port=(?P<port>\d{1,10}|<none>), )?"
    r"orientation=(?P<orientation>[0-3]), "
    # A Samsung phone (API 36) printed the display's density here.
    r"(?P<density>densityDpi=\d{1,5} xDpi=\d{1,5}\.\d{1,6}, yDpi=\d{1,5}\.\d{1,6}, )?"
    r"(?:uniqueId='(?P<quoted>[^']*)', )?"
    rf"logicalFrame=\[(?P<logical>{_RECT})\], "
    rf"physicalFrame=\[(?P<physical>{_RECT})\], deviceSize=\[\d{{1,7}}, \d{{1,7}}\]"
    r"(?P<active>, isActive=\[[01]\])?"
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
    display. ``samsung_0x1`` that it prints ``samsungFlags=0x1`` (the
    Samsung form), which is read only on a window of the app the check is
    for (see `read_window_at`).
    """

    title: str
    takes_touch: bool
    region: tuple[int, int, int, int] | None
    owner_uid: int
    modal: bool = False
    portal: bool = False
    samsung_0x1: bool = False

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


class Unreadable(str):
    """Why the window at a point is not known: equal to one of the reasons
    (`NO_LIST`, `LINE_FORMAT`, `REGION_FORMAT`, `ROTATION`), and carrying in
    ``rule`` the check that refused (one of `RULES`)."""

    rule: str

    def __new__(cls, reason: str, rule: str) -> Self:
        made = super().__new__(cls, reason)
        made.rule = rule
        return made

    def __reduce__(self) -> tuple[type[Unreadable], tuple[str, str]]:
        """Copy and pickle with the rule (``str``'s own would drop it)."""
        return type(self), (str(self), self.rule)


@dataclass(frozen=True, slots=True)
class _Refusal:
    """Why a list is not read: the public reason, the rule that refused (one
    of `RULES`), the index, in the text's lines, of the line it refused at
    (None when no one line is to blame) and the index of the ``Windows``
    line of the list it refused in (None when it was no one list)."""

    reason: str
    rule: str
    line: int | None = None
    head: int | None = None


@dataclass(slots=True)
class _Trace:
    """What the reader read, for `explain_input_windows`; it changes no
    decision. ``accepted``: the window lines it read into a window;
    ``viewports``: the Viewport lines of the display it read whole for the
    orientation; ``kinds``: the kind of each line it read and accepted
    (``window_row`` and the ``transform`` and ``matrix_row`` lines after it)
    or matched as a Viewport line of the display (``viewport``);
    ``reached``: the ``Windows:`` line of each list it read; ``chosen``:
    the ``Windows:`` line of the list it took as the display's."""

    accepted: set[int] = field(default_factory=set)
    viewports: set[int] = field(default_factory=set)
    kinds: dict[int, str] = field(default_factory=dict)
    reached: set[int] = field(default_factory=set)
    chosen: int | None = None


@dataclass(frozen=True, slots=True)
class _List:
    """The lines of one window list and what is known about its display."""

    lines: list[str]
    at: list[int]  # the index of each line in the whole text
    head: int  # the index of the list's ``Windows:`` line
    display: int | None  # the list's ``Display:`` section; None: one list
    rotated: _Refusal | None  # why the section's display transform turns
    problem: _Refusal | None = None  # why the list cannot be read as a whole
    others: tuple[_List, ...] = ()  # the lists of the other displays


@dataclass(frozen=True, slots=True)
class _Group:
    """A window line of a list (stripped) with the lines that follow it up to
    the next window line, each with its index in the whole text."""

    line: str
    at: int
    rest: list[str]
    rest_at: list[int]


@dataclass(frozen=True, slots=True)
class _Span:
    """Where a list is: the index of its ``Windows:`` line, of its lines,
    of its end line (None: the end of the text) and whether that line is one
    a list is known to end at."""

    head: int
    taken: list[int]
    end: int | None
    ended: bool


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


class _TooLongError(Exception):
    """A number in ``dumpsys input`` too long to read (see `_number`), on the
    line with index ``line`` when it is known."""

    def __init__(self, line: int | None = None) -> None:
        super().__init__(line)
        self.line = line


def _number(digits: str) -> int:
    """`digits` (a digit run a pattern matched, maybe with ``-``) as a number.

    Raises `_TooLongError` where `int` raises `ValueError`: on a run of more
    digits than `sys.get_int_max_str_digits` (Python's default is 4300).
    Every run `int` reads is read the same. The callers turn the error into
    a refusal (`parse_input_windows`, `display_orientation`): such a number
    could be anything, 0 or display 0 included (with leading zeros).
    """
    try:
        return int(digits)
    except ValueError as error:
        raise _TooLongError from error


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


def _listed(lines: list[str], head: int) -> _Span:
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
            return _Span(head, taken, i, _LIST_END.match(line.strip()) is not None)
        taken.append(i)
    return _Span(head, taken, None, ended=False)


def _dispatcher_start(lines: list[str]) -> int | _Refusal:
    """The index of the dispatcher's header line, unless there is not exactly
    one (`NO_LIST`)."""
    found = [i for i, line in enumerate(lines) if line.strip() == _HEADER]
    if not found:
        return _Refusal(NO_LIST, "no_dispatcher")
    if len(found) > 1:
        return _Refusal(NO_LIST, "dispatcher_twice", found[1])
    return found[0]


def _heads(lines: list[str], found: list[int], level: int) -> list[int]:
    """The lines of `found` at indent `level` that begin a window list."""
    return [
        i
        for i in found
        if _indent(lines[i]) == level and lines[i].strip().startswith("Windows:")
    ]


def _window_list(lines: list[str], display_id: int) -> _List | _Refusal:
    """The window list of `display_id` in the dispatcher's part, or why there
    is none or it cannot be told which one it is (`NO_LIST`).

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
    if isinstance(start, _Refusal):
        return start
    found, level = _under(lines, start)
    tops = [i for i in found if _indent(lines[i]) == level]
    lists = _heads(lines, tops, level)
    sections = [i for i in tops if _DISPLAY.match(lines[i].strip())]
    if lists and not sections:
        return _one_list(lines, lists)
    twice = _twice(lines, sections)
    if twice is not None:
        return twice
    if lists:
        return _Refusal(NO_LIST, "list_and_sections", lists[0])
    ours = [i for i in sections if _display_of(lines[i]) == display_id]
    if not ours:
        return _Refusal(NO_LIST, "display_missing" if sections else "no_list")
    return _sections(lines, sections, ours[0], display_id)


def _one_list(lines: list[str], lists: list[int]) -> _List | _Refusal:
    """The one list of every display (API 24 to 28), whose ``Windows:`` line
    must be the only one at `lists` and say nothing more."""
    if len(lists) != 1:
        return _Refusal(NO_LIST, "lists_twice", lists[1], lists[1])
    if lines[lists[0]].strip() != "Windows:":
        return _Refusal(NO_LIST, "list_heading", lists[0], lists[0])
    span = _listed(lines, lists[0])
    taken = span.taken
    problem = _stray(lines, [span])
    return _List([lines[i] for i in taken], taken, lists[0], None, None, problem)


def _twice(lines: list[str], sections: list[int]) -> _Refusal | None:
    """``display_twice`` at the second ``Display:`` section of one number
    (``Display: 00`` and ``Display: 0`` included), or None."""
    seen: set[int] = set()
    for i in sections:
        shown = _display_of(lines[i])
        if shown in seen:
            return _Refusal(NO_LIST, "display_twice", i)
        seen.add(shown)
    return None


def _display_turn(
    lines: list[str], above: list[int], head: int, *, configured: bool
) -> _Refusal | None:
    """Why the display of a section turns its coordinates (`ROTATION`), from
    the section's lines `above` its list (whose ``Windows:`` line is at
    `head`), or None. `configured`: the list has rows of the
    ``inputConfig`` forms (API 33 to 36), which print ``logicalSize=`` and
    the display's transform above every list; without them the coordinates
    are not known."""
    transforms = [i for i in above if lines[i].strip().startswith("transform ")]
    sized = any(lines[i].strip().startswith("logicalSize=") for i in above)
    if (sized or configured) and not (sized and transforms):
        return _Refusal(ROTATION, "display_transform_missing", head, head)
    if len(transforms) > 1:
        return _Refusal(ROTATION, "display_transform", transforms[1], head)
    turned = [i for i in transforms if lines[i].strip() != _IDENTITY]
    return _Refusal(ROTATION, "display_transform", turned[0], head) if turned else None


def _sections(
    lines: list[str], sections: list[int], ours: int, display_id: int
) -> _List | _Refusal:
    """The list of the ``Display:`` section at `ours`, with the lists of the
    other sections at `sections` (see `_window_list`)."""
    spans: list[_Span] = []
    mine: _List | None = None
    others: list[_List] = []
    for head in sections:
        section, inner = _under(lines, head)
        heads = _heads(lines, section, inner)
        listed = [i for i in heads if lines[i].strip() == "Windows:"]
        if len(heads) > 1:
            return _Refusal(NO_LIST, "lists_twice", heads[1], heads[1])
        if head == ours and not listed:
            if heads:
                return _Refusal(NO_LIST, "list_heading", heads[0], heads[0])
            return _Refusal(NO_LIST, "no_list", head)
        if not listed:
            continue
        span = _listed(lines, listed[0])
        spans.append(span)
        taken = span.taken
        above = [i for i in section if i < listed[0]]
        configured = any(", inputConfig=" in _meta(lines[i].strip()) for i in taken)
        rotated = _display_turn(lines, above, listed[0], configured=configured)
        shown = _display_of(lines[head])
        read = _List([lines[i] for i in taken], taken, listed[0], shown, rotated)
        if head == ours:
            mine = read
        else:
            others.append(read)
    if mine is None:  # not reached: the display's own section has a list
        return _Refusal(NO_LIST, "no_list", ours)
    problem = _stray(lines, spans)
    return _List(
        mine.lines,
        mine.at,
        mine.head,
        display_id,
        rotated=mine.rotated,
        problem=problem,
        others=tuple(others),
    )


def _display_of(line: str) -> int:
    """The display id of a ``Display: <id>`` line, as a number."""
    return int(line.strip().removeprefix("Display: "))


def _stray(lines: list[str], spans: list[_Span]) -> _Refusal | None:
    """`LINE_FORMAT` when a list (of `spans`) did not end at a known line
    (``list_end``) or a line outside the lists holds ``ownerUid=``
    (``stray_owner``); None otherwise."""
    for span in spans:
        if not span.ended:
            last = span.taken[-1] if span.taken else None
            end = span.end if span.end is not None else last
            return _Refusal(LINE_FORMAT, "list_end", end, span.head)
    taken = {i for span in spans for i in span.taken}
    stray = [
        i for i, line in enumerate(lines) if i not in taken and "ownerUid=" in line
    ]
    return _Refusal(LINE_FORMAT, "stray_owner", stray[0]) if stray else None


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


def _touch(line: str) -> tuple[bool, bool, bool] | str:
    """Whether the window takes touches, whether it is touch modal and whether
    its form prints ``portalToDisplayId`` (API 29 to 32); the rule that
    refuses the line when it has no known form."""
    config = _INPUT_CONFIG.search(line)
    visible = _VISIBLE.search(line)
    named = _NAMED_FLAGS.search(line)
    # A field named anywhere in the line must read as that field (text in a
    # name such as "inputConfig=0x0, x" is no field and no form).
    if (
        ("inputConfig=" in line) != (config is not None)
        or ("visible=" in line) != (visible is not None)
        or ("flags=" in line) != (named is not None)
    ):
        return "field_value"
    if config is not None:  # API 33 on
        names = _names(config["names"], _INPUT_CONFIG_NAMES)
        if names is None:
            return "unknown_flag"
        if visible is not None or named is not None:
            return "mixed_flags"
        return not names & _NOT_TAKING, False, False
    if visible is None:
        return "field_missing"
    return _flags_touch(line, named, shown=visible["visible"] == "true")


def _flags_touch(
    line: str, named: re.Match[str] | None, *, shown: bool
) -> tuple[bool, bool, bool] | str:
    """`_touch` for the forms with ``visible=`` and ``flags=`` (API 24 to
    32), `shown` being the visible field and `named` the flags field."""
    hex_flags = _HEX_FLAGS.search(line)
    if hex_flags is not None:  # API 24 to 30
        bits = int(hex_flags["hex"], 16)
        takes = shown and not bits & _FLAG_NOT_TOUCHABLE
        not_modal = bits & (_FLAG_NOT_FOCUSABLE | _FLAG_NOT_TOUCH_MODAL)
        newer = _hex_form(_meta(line))
        if newer is None:
            return "hex_form"
        return takes, takes and not not_modal, newer
    if named is None:  # neither form
        return "field_missing"
    names = _names(named["names"], _FLAG_NAMES)
    if names is None:
        return "unknown_flag"
    takes = shown and "NOT_TOUCHABLE" not in names  # API 31 and 32
    return takes, takes and not names & {"NOT_FOCUSABLE", "NOT_TOUCH_MODAL"}, True


def _samsung(line: str) -> str | None:
    """The ``samsungFlags=`` value of a window line of the Samsung form (API
    36), ``""`` when the line has none of its fields; None when one of them
    is named anywhere in the line (its name and ``applicationInfo.name``
    included) but the fields after the name do not hold ``samsungFlags=``
    right after ``inputConfig=`` and ``pTouchableRegion=`` and ``1HXY=`` right
    after ``touchableRegion=``, each with a value the phone printed. They do
    not change whether or where the window takes a touch (``0x1`` is read
    only on the app's own window, see `_read`)."""
    if not any(key in line for key in _SAMSUNG_KEYS):
        return ""
    meta = _meta(line)
    flags = _SAMSUNG_FLAGS.search(meta)
    if flags is None or _SAMSUNG_REGION.search(meta) is None:
        return None
    return flags["value"]


def _missing(line: str, key: str) -> str:
    """The rule for a field `key` that does not read: named but malformed
    (``field_value``) or not there (``field_missing``)."""
    return "field_value" if key in line else "field_missing"


def _window(line: str, name: str, at: int) -> InputWindow | _Refusal:
    """Read one window line (stripped, at index `at`) whose name field is
    `name`."""
    touch = _touch(line)
    if isinstance(touch, str):
        return _Refusal(LINE_FORMAT, touch, at)
    uid = _UID.search(line)
    if uid is None:
        return _Refusal(LINE_FORMAT, _missing(line, "ownerUid="), at)
    portal = _PORTAL.search(line)
    samsung = _samsung(line)
    if samsung is None:
        return _Refusal(LINE_FORMAT, "samsung_fields", at)
    if portal is None and ("portalToDisplayId=" in line or touch[2]):
        rule = "field_value" if "portalToDisplayId=" in line else "portal_missing"
        return _Refusal(LINE_FORMAT, rule, at)
    bounds = _bounds(line, at)
    if isinstance(bounds, _Refusal):
        return bounds
    takes, modal, _ = touch
    return InputWindow(
        _title(name),
        takes_touch=takes,
        region=bounds,
        owner_uid=int(uid["uid"]),
        modal=modal,
        portal=portal is not None and int(portal["to"]) != -1,
        samsung_0x1=samsung == _SAMSUNG_OWN_ONLY,
    )


def _bounds(line: str, at: int) -> tuple[int, int, int, int] | _Refusal | None:
    """The touchable region of a window line (None: empty), or why it does
    not read (`REGION_FORMAT`)."""
    region = _REGION.search(line)
    if region is None:
        rule = "region_format" if "touchableRegion=" in line else "region_missing"
        return _Refusal(REGION_FORMAT, rule, at)
    if region["empty"]:
        return None
    left, top = int(region["l"]), int(region["t"])
    right, bottom = int(region["r"]), int(region["b"])
    if left > right or top > bottom:
        return _Refusal(REGION_FORMAT, "region_reversed", at)
    return left, top, right, bottom


def _group(found: _List) -> list[_Group] | _Refusal:
    """Each window line of a list with the lines that follow it up to the
    next window line; ``orphan_line`` when a line comes before the first."""
    groups: list[_Group] = []
    for raw, at in zip(found.lines, found.at, strict=True):
        line = raw.strip()
        if _INDEX.match(line):
            groups.append(_Group(line, at, [], []))
        elif not groups:
            return _Refusal(LINE_FORMAT, "orphan_line", at)
        else:
            groups[-1].rest.append(line)
            groups[-1].rest_at.append(at)
    return groups


def _misshapen(group: _Group) -> _Refusal | None:
    """Why the lines after a window line are not nothing or a ``transform``
    line with its matrix rows (none for ``IDENTITY``, three for any other
    kind): ``unknown_line`` for a line that is neither, ``transform_shape``
    for matrix rows missing, malformed or too many; None when they are."""
    turned = _turned(group, convert=False)
    return turned if isinstance(turned, _Refusal) else None


def _turned(group: _Group, *, convert: bool = True) -> bool | _Refusal:
    """Whether the lines after a window line hold a rotated transform, or why
    they are no transform (see `_misshapen`). Without `convert` the
    rotation is not read (False)."""
    rest, rest_at = group.rest, group.rest_at
    if not rest:
        return False
    transform = _TRANSFORM.match(rest[0])
    if transform is None:
        return _Refusal(LINE_FORMAT, "unknown_line", rest_at[0])
    rows = 0 if transform["kind"] == "IDENTITY" else _MATRIX_ROWS
    for i in range(1, len(rest)):
        if not _MATRIX_ROW.match(rest[i]):
            rule = "transform_shape" if i <= rows else "unknown_line"
            return _Refusal(LINE_FORMAT, rule, rest_at[i])
        if i > rows:
            return _Refusal(LINE_FORMAT, "transform_shape", rest_at[i])
    if len(rest) != 1 + rows:
        return _Refusal(LINE_FORMAT, "transform_shape", rest_at[0])
    return convert and _number(transform["rot"]) != 0


def _turns(group: _Group) -> bool:
    """Whether a window line has a ``displayOrientation=`` other than 0
    (`_TooLongError` for one too long to read)."""
    try:
        return any(_number(o) != 0 for o in _LINE_ORIENTATION.findall(group.line))
    except _TooLongError as error:
        raise _TooLongError(group.at) from error


def _row(
    group: _Group, index: int, found: _List, display_id: int
) -> InputWindow | _Refusal | None:
    """Read the window line of row `index` of `found`; None when it is of
    another display than `display_id` (only in a list of every display)."""
    line, at = group.line, group.at
    match = _LINE.match(line)
    if match is None:
        return _Refusal(LINE_FORMAT, "row_shape", at)
    if int(match["index"]) != index:
        return _Refusal(LINE_FORMAT, "row_number", at)
    if any(line.count(key) > 1 for key in _ONCE):
        return _Refusal(LINE_FORMAT, "field_twice", at)
    display = int(match["display"])
    if found.display is not None and display != found.display:
        return _Refusal(LINE_FORMAT, "display_mismatch", at)
    if display != display_id:
        return None
    name = match["quoted"] if match["quoted"] is not None else match["bare"]
    return _window(line, name, at)


def _split(text: str) -> list[str]:
    return [line.removesuffix("\r") for line in text.split("\n")]


def _parse(
    lines: list[str],
    display_id: int,
    trace: _Trace | None = None,
    owned: Callable[[InputWindow], bool] | None = None,
) -> tuple[InputWindow, ...] | _Refusal:
    """Read the windows of `display_id` (see `parse_input_windows`), noting
    in `trace` what it read; `owned` tells the app's own windows (`_read`)."""
    found = _window_list(lines, display_id)
    if isinstance(found, _Refusal):
        return found
    if trace is not None:
        trace.chosen = found.head
    if found.problem is not None:
        return found.problem
    reading = found
    try:
        for other in found.others:  # read as well: a name could have made them
            reading = other
            shown = display_id if other.display is None else other.display
            read = _read(other, shown, trace, mine=False)
            if isinstance(read, _Refusal) and read.reason in {
                LINE_FORMAT,
                REGION_FORMAT,
            }:
                return _in(read, other)
        reading = found
        return _in(_read(found, display_id, trace, owned=owned), found)
    except _TooLongError as error:
        # Such a number could be anything, 0 or display 0 included.
        return _Refusal(ROTATION, "number_too_long", error.line, reading.head)


def _in(
    read: tuple[InputWindow, ...] | _Refusal, found: _List
) -> tuple[InputWindow, ...] | _Refusal:
    """`read` with the list `found` named as where a refusal was made."""
    if isinstance(read, _Refusal) and read.head is None:
        return replace(read, head=found.head)
    return read


def parse_input_windows(
    text: str,
    display_id: int = 0,
    *,
    owned: Callable[[InputWindow], bool] | None = None,
) -> tuple[InputWindow, ...] | Unreadable:
    """Read the windows of `display_id` from the top down.

    Returns the windows, or the reason they are not known: `NO_LIST` (no
    window list of that display, or no way to tell which list it is),
    `LINE_FORMAT` (a line in the list of no known form, a read field twice,
    an unknown flag name, a row number out of order), `REGION_FORMAT` (a
    touchable region that is not one rectangle or empty, or is reversed) or
    `ROTATION` (a transform or orientation in the list, or the display's
    transform, that is not the unrotated identity; or a rotation or
    orientation too long to read in any list read, another display's
    included), as an `Unreadable` that also names the rule that refused. The
    ``Viewport`` lines are read by `display_orientation`, not here.

    A window that prints ``samsungFlags=0x1`` (the Samsung form) is read
    only when `owned` says it is a window of the app the check is for;
    otherwise the list is `LINE_FORMAT` (``samsung_flags``), wherever the
    window is and whether or not it takes touches. Without `owned` no window
    is the app's.
    """
    read = _parse(_split(text), display_id, owned=owned)
    return Unreadable(read.reason, read.rule) if isinstance(read, _Refusal) else read


def _meta(line: str) -> str:
    """The fields of a window line after its name, from ``id=`` or
    ``displayId=`` on, with ``, `` in front and the value of
    ``applicationInfo.name`` (text an app sets) left out; the whole line when
    it has no known start."""
    match = _LINE.match(line)
    if match is None:
        return line
    start = match.start("id") if match["id"] is not None else match.start("fields")
    return _APP_NAME.sub(", applicationInfo.name=", ", " + line[start:])


def _form(line: str) -> tuple[bool, tuple[str, ...]]:
    """What tells the form of a window line apart: whether its name is quoted
    and which of `_FORM_FIELDS` its fields (`_meta`, so not text in its name)
    have. Every row of a list must agree (they did in every capture)."""
    quoted = line.partition(": name=")[2].startswith("'")
    meta = _meta(line)
    return quoted, tuple(key for key in _FORM_FIELDS if f", {key}=" in meta)


def _whole(groups: list[_Group], found: _List) -> _Refusal | None:
    """Why the rows of a list do not read as one list: a row of API 24 to 28
    in a list per display, rows that differ in form, or rows that differ in
    having lines after them (all or none): named after the first such lines
    that are no transform with its rows (`_turned`), else
    ``transform_mismatch``."""
    if found.display is not None:
        # A list per display (API 29 on) has no row of API 24 to 28 (scale=).
        legacy = [g for g in groups if ", scale=" in _meta(g.line)]
        if legacy:
            return _Refusal(LINE_FORMAT, "legacy_row_in_display_list", legacy[0].at)
    if not groups:
        return None
    first = groups[0]
    form = _form(first.line)
    for group in groups:
        if _form(group.line) != form:
            return _Refusal(LINE_FORMAT, "form_mismatch", group.at)
    odd = [g for g in groups if bool(g.rest) != bool(first.rest)]
    if not odd:
        return None
    # Some rows have lines after them and some do not. When such lines are
    # not a transform with its rows, name that line (also refused below).
    for group in groups:
        misshapen = _misshapen(group)
        if misshapen is not None:
            return misshapen
    return _Refusal(LINE_FORMAT, "transform_mismatch", odd[0].at)


def _note_rows(trace: _Trace | None, found: _List) -> None:
    """Note in `trace` a list the reader reads."""
    if trace is not None:
        trace.reached.add(found.head)


def _note_window(trace: _Trace | None, group: _Group) -> None:
    """Note in `trace` a window line the reader read into a window, and the
    transform line and matrix rows after it that it matched."""
    if trace is not None:
        trace.accepted.add(group.at)
        trace.kinds[group.at] = "window_row"
        for i, at in enumerate(group.rest_at):
            trace.kinds[at] = "transform" if i == 0 else "matrix_row"


def _own_only(
    window: InputWindow,
    at: int,
    *,
    mine: bool,
    owned: Callable[[InputWindow], bool] | None,
) -> InputWindow | _Refusal:
    """`window` (read at line `at`), or why its ``samsungFlags=0x1`` refuses
    the list (see `_read`)."""
    if not window.samsung_0x1:
        return window
    if not mine:
        return _Refusal(LINE_FORMAT, "samsung_fields", at)
    if owned is None or not owned(window):
        return _Refusal(LINE_FORMAT, "samsung_flags", at)
    return window


def _read(
    found: _List,
    display_id: int,
    trace: _Trace | None = None,
    *,
    mine: bool = True,
    owned: Callable[[InputWindow], bool] | None = None,
) -> tuple[InputWindow, ...] | _Refusal:
    """Read the windows of `display_id` in the list `found` (see
    `parse_input_windows`), noting in `trace` what it read.

    A window with ``samsungFlags=0x1`` passes only in the list of the
    display asked for (`mine`; elsewhere ``samsung_fields`` as for any value
    not seen) and only when `owned` says it is the app's (else
    ``samsung_flags``): that value was seen only on the app's own window,
    and what it does to a touch is not known."""
    _note_rows(trace, found)  # entered, also when it refuses right away
    groups = _group(found)
    if isinstance(groups, _Refusal):
        return groups
    refused = _whole(groups, found)
    if refused is not None:
        return refused
    windows: list[InputWindow] = []
    rotated = found.rotated
    for index, group in enumerate(groups):
        try:
            turned = _turned(group)
        except _TooLongError as error:
            raise _TooLongError(group.rest_at[0]) from error
        if isinstance(turned, _Refusal):
            return turned
        window = _row(group, index, found, display_id)
        if isinstance(window, InputWindow):
            window = _own_only(window, group.at, mine=mine, owned=owned)
        if isinstance(window, _Refusal):
            return window
        if window is None:
            continue
        windows.append(window)
        _note_window(trace, group)
        if rotated is not None:
            continue
        if turned:
            rotated = _Refusal(ROTATION, "row_rotated", group.rest_at[0])
        elif _turns(group):
            rotated = _Refusal(ROTATION, "row_orientation", group.at)
    if not windows:
        return _Refusal(NO_LIST, "no_rows")
    return rotated if rotated is not None else tuple(windows)


def _viewport_of(
    line: str, display_id: int, at: int, trace: _Trace | None = None
) -> int | _Refusal | None:
    """The orientation a line (at index `at`) gives for `display_id`, None
    when it is no Viewport line of that display, or why it cannot be read: a
    display number too long to read (it could be this one), a line of no
    known form, or a scaled or moved display. `trace` notes the line."""
    claim = _VIEWPORT_CLAIM.match(line)
    try:
        if claim is None or _number(claim["display"]) != display_id:
            return None
    except _TooLongError:
        return _Refusal(ROTATION, "number_too_long", at)
    match = _VIEWPORT.fullmatch(line)
    if trace is not None:
        trace.kinds[at] = "viewport"
        if match is not None:
            trace.viewports.add(at)
    if match is None:
        return _Refusal(ROTATION, "viewport_malformed", at)
    if match["logical"] != match["physical"]:
        return _Refusal(ROTATION, "scaled", at)
    return int(match["orientation"])


def _orientation(
    lines: list[str], display_id: int, trace: _Trace | None = None
) -> tuple[int, int] | _Refusal:
    """The orientation of `display_id` (see `display_orientation`) with the
    index of the first line that says it, or why it cannot be read
    (`ROTATION`), noting in `trace` the Viewport lines it read."""
    start = _dispatcher_start(lines)
    if isinstance(start, _Refusal):
        return _Refusal(ROTATION, start.rule, start.line)
    said: dict[int, int] = {}  # orientation: the first line that said it
    for i, line in enumerate(lines[:start]):
        read = _viewport_of(line, display_id, i, trace)
        if isinstance(read, _Refusal):
            return read
        if read is not None:
            said.setdefault(read, i)
    if not said:
        return _Refusal(ROTATION, "viewport_missing")
    if len(said) > 1:
        return _Refusal(ROTATION, "viewport_disagree", sorted(said.values())[1])
    return next(iter(said.items()))


def display_orientation(text: str, display_id: int = 0) -> int | None:
    """Return the orientation of `display_id` (0 to 3, a quarter turn each),
    None when it cannot be read.

    Reads the input reader's ``Viewport`` lines of the display, above the
    dispatcher's part of ``dumpsys input``. Every line that names the display
    must be of a known form and have its logical frame equal to its physical
    frame (a display that is not scaled or moved), and all must say the same
    orientation. A line whose display number is too long to read could name
    the display: None.
    """
    found = _orientation(_split(text), display_id)
    return None if isinstance(found, _Refusal) else found[0]


def _checked(
    lines: list[str],
    display_id: int,
    trace: _Trace | None = None,
    owned: Callable[[InputWindow], bool] | None = None,
) -> tuple[InputWindow, ...] | _Refusal:
    """The windows of `display_id` when both the list and the display's
    orientation read and the orientation is 0, else why not, noting in
    `trace` what it read; `owned` tells the app's own windows (`_read`)."""
    read = _parse(lines, display_id, trace, owned)
    if isinstance(read, _Refusal):
        return read
    turned = _orientation(lines, display_id, trace)
    if isinstance(turned, _Refusal):
        return turned
    orientation, line = turned
    return _Refusal(ROTATION, "rotated", line) if orientation != 0 else read


@dataclass(frozen=True, slots=True)
class ListSpan:
    """One window list of the dispatcher's part, found the way the reader
    finds them (see `explain_input_windows`).

    ``display`` is the number of its ``Display:`` section, None for the one
    list of every display (API 24 to 28), and ``digits`` how many digits
    that number is written with (0 for the one list); ``head`` the index of
    its ``Windows:`` line in the text's lines, ``lines`` the indices of its
    lines, ``rows`` how many of them are window lines, ``section`` the
    index of its ``Display:`` line (None for the one list) and ``negative``
    whether that number is written with a minus sign.
    """

    display: int | None
    digits: int
    head: int
    lines: tuple[int, ...]
    rows: int
    section: int | None = None
    negative: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    """What reading the windows of a display concluded, as in `read_window_at`
    before a point is looked at, with no text of the output in it.

    ``reason`` is None when the list and the display's orientation read
    (the check would go on to the point), else one of `NO_LIST`,
    `LINE_FORMAT`, `REGION_FORMAT` and `ROTATION`; ``rule`` (one of `RULES`)
    the check that refused; ``line`` the index, in ``text.split("\\n")``, of
    the line it refused at, None when no one line is to blame; ``start``
    the index of the dispatcher's header line, None when there is not
    exactly one; ``lists`` the window lists found; ``list_head`` the
    ``head`` of the list it refused in, None when it was no one list;
    ``end`` the index of the first line after the dispatcher's part (the
    first line not more indented than its header), None without ``start``;
    ``accepted`` the indices of the window lines the reader read into a
    window (of any list it read, before it refused); ``viewports`` the
    indices of the Viewport lines of the display it read whole for the
    display's orientation (none when it refused before it read them);
    ``kinds`` the kind of each line the reader matched as a heading when it
    found the lists, read and accepted (``window_row`` and the ``transform``
    and ``matrix_row`` lines after it) or matched as a Viewport line of the
    display (``viewport``); others are not in it; ``reached`` for each of
    ``lists`` whether the reader read it; ``chosen`` the ``head`` of the
    list it took as the display's (None when it took none). The headings
    in ``kinds`` are its header and those of the chosen list only.
    """

    reason: str | None
    rule: str | None
    line: int | None
    start: int | None
    lists: tuple[ListSpan, ...]
    list_head: int | None = None
    end: int | None = None
    accepted: frozenset[int] = frozenset()
    viewports: frozenset[int] = frozenset()
    kinds: dict[int, str] = field(default_factory=dict)
    reached: tuple[bool, ...] = ()
    chosen: int | None = None


def _spans(lines: list[str], start: int) -> tuple[ListSpan, ...]:
    """Every ``Windows`` list directly under the dispatcher's header or under
    one of its ``Display:`` sections, in their order."""
    found, level = _under(lines, start)
    spans: list[ListSpan] = []
    for i in (i for i in found if _indent(lines[i]) == level):
        heading = lines[i].strip()
        if _DISPLAY.match(heading):
            section, inner = _under(lines, i)
            shown = _display_of(heading)
            digits = len(heading.removeprefix("Display: ").lstrip("-"))
            heads = [(shown, digits, h, i) for h in _heads(lines, section, inner)]
        else:
            heads = [(None, 0, h, None) for h in _heads(lines, [i], level)]
        for display, digits, head, at in heads:
            taken = _listed(lines, head).taken
            rows = sum(1 for t in taken if _INDEX.match(lines[t].strip()))
            negative = at is not None and lines[at].strip().startswith("Display: -")
            spans.append(
                ListSpan(display, digits, head, tuple(taken), rows, at, negative)
            )
    return tuple(spans)


def explain_input_windows(
    text: str,
    display_id: int = 0,
    *,
    owned: Callable[[InputWindow], bool] | None = None,
) -> Verdict:
    """Tell whether the windows of `display_id` can be read from the whole
    text of ``dumpsys input``, and if not, which rule refused and where.

    It runs the same checks as `read_window_at` up to the point (the list,
    then the display's orientation) and refuses exactly what they refuse,
    with the same `owned` (without it, a ``samsungFlags=0x1`` window
    refuses: ``samsung_flags``).
    """
    lines = _split(text)
    start = _dispatcher_start(lines)
    first = None if isinstance(start, _Refusal) else start
    lists = () if first is None else _spans(lines, first)
    end = None if first is None else _dispatcher_end(lines, first)
    trace = _Trace()
    read = _checked(lines, display_id, trace, owned)
    chosen = [span for span in lists if span.head == trace.chosen]
    kinds = _heading_kinds(lines, first, tuple(chosen)) | trace.kinds
    reached = tuple(span.head in trace.reached for span in lists)
    found = (
        (read.reason, read.rule, read.line, read.head)
        if isinstance(read, _Refusal)
        else (None, None, None, None)
    )
    return Verdict(
        *found[:3],
        first,
        lists,
        list_head=found[3],
        end=end,
        accepted=frozenset(trace.accepted),
        viewports=frozenset(trace.viewports),
        kinds=kinds,
        reached=reached,
        chosen=trace.chosen,
    )


def _heading_kinds(
    lines: list[str], start: int | None, lists: tuple[ListSpan, ...]
) -> dict[int, str]:
    """The kinds of the lines the reader matches as headings when it finds
    the lists (`_window_list`): its header (every line equal to it), and for
    each of `lists` its ``Display:`` section, ``Windows`` line, the line it
    ended at (`_LIST_END`), and above it in the section the display's
    transform lines and ``logicalSize=``."""
    kinds = {i: "heading" for i, line in enumerate(lines) if line.strip() == _HEADER}
    if start is None:
        return kinds
    for span in lists:
        kinds[span.head] = "heading"
        if span.section is not None:
            kinds[span.section] = "heading"
            section, _ = _under(lines, span.section)
            for i in (i for i in section if i < span.head):
                text = lines[i].strip()
                if text.startswith("transform "):
                    kinds[i] = "transform"
                elif text.startswith("logicalSize="):
                    kinds[i] = "heading"
        listed = _listed(lines, span.head)
        if listed.ended and listed.end is not None:
            kinds[listed.end] = "heading"
    return kinds


def _dispatcher_end(lines: list[str], start: int) -> int:
    """The index of the first line after the dispatcher's part: the first
    non-blank line not more indented than its header (the end of the text
    when none)."""
    level = _indent(lines[start])
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and _indent(lines[i]) <= level:
            return i
    return len(lines)


# The fields that end a window's name or its applicationInfo.name: each may
# be in a window line once, in any spelling (``,id=``, ``,  id =``), or where
# the name ends cannot be told.
_ROW_MARKS: Final = tuple(
    re.compile(rf",\s*{re.escape(key)}\s*=")
    for key in ("id", "displayId", "applicationInfo.name", "applicationInfo.token")
)
# Where a field after the name begins (``1HXY=`` of the Samsung form too).
_FIELD = re.compile(r", ([A-Za-z0-9][A-Za-z0-9_.]*)=")
_QUOTES: Final = "'\""
_APP_NAME_END: Final = ", applicationInfo.token="


@dataclass(frozen=True, slots=True)
class RowFields:
    """A window line cut the way the reader cuts it (`window_line_fields`):
    whether the name is quoted, its length, and the fields after it as
    (key, value), ``applicationInfo.name`` whole up to
    ``, applicationInfo.token=`` and a quoted value whole."""

    quoted: bool
    name_length: int
    fields: tuple[tuple[str, str], ...]


def _value_end(meta: str, key: str, begin: int) -> int | None:
    """Where the value of field `key` that begins at `begin` ends, or None
    when that cannot be told: ``applicationInfo.name`` runs to
    ``, applicationInfo.token=``; a value that opens with a quote runs to
    the same quote, which must end it; any other value runs to the next
    field and must hold no quote."""
    if key == "applicationInfo.name":
        end = meta.find(_APP_NAME_END, begin)
        value = meta[begin:end] if end >= 0 else ""
        if end < 0 or (_has_quote(value) and not _one_quoted(value)):
            return None
        return end
    if begin < len(meta) and meta[begin] in _QUOTES:
        close = meta.find(meta[begin], begin + 1)
        if close < 0:
            return None
        end = close + 1
        return end if end == len(meta) or _FIELD.match(meta, end) else None
    following = _FIELD.search(meta, begin)
    end = following.start() if following is not None else len(meta)
    return None if _has_quote(meta[begin:end]) else end


def _has_quote(value: str) -> bool:
    return any(quote in value for quote in _QUOTES)


def _one_quoted(value: str) -> bool:
    """Whether `value` is one quoted run with no other quote in it."""
    return (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in _QUOTES
        and sum(value.count(quote) for quote in _QUOTES) == 2
    )


def window_line_fields(line: str) -> RowFields | None:
    """Cut a window line where the reader cuts it, or None when where its
    name or a field ends cannot be told.

    The line must match the reader's window line (`_LINE`) with the quoted
    name taken as quoted (a name that starts with a quote read as bare is
    refused); none of `_ROW_MARKS` may be in it twice in any spelling, and
    ``applicationInfo.name`` needs ``, applicationInfo.token=`` after it.
    Each field runs to the next ``, key=`` (`_NEXT`); a quoted value (single
    or double quotes) is read whole first and must be followed by a field or
    the end, and a quote anywhere else refuses the line."""
    text = line.strip()
    match = _LINE.match(text)
    if match is None or any(len(mark.findall(text)) > 1 for mark in _ROW_MARKS):
        return None
    quoted = match["quoted"] is not None
    if not quoted and text.partition(": name=")[2][:1] in _QUOTES:
        return None
    if (", applicationInfo.name=" in text) != (_APP_NAME_END in text):
        return None
    name = match["quoted"] if quoted else match["bare"]
    start = match.start("id") if match["id"] is not None else match.start("fields")
    meta = ", " + text[start:]
    fields: list[tuple[str, str]] = []
    position = 0
    while position < len(meta):
        field = _FIELD.match(meta, position)
        if field is None:
            return None
        end = _value_end(meta, field[1], field.end())
        if end is None:
            return None
        fields.append((field[1], meta[field.end() : end]))
        position = end
    return RowFields(quoted, len(name), tuple(fields))


def window_at(
    windows: tuple[InputWindow, ...], point: tuple[int, int]
) -> InputWindow | None:
    """The topmost window that takes a touch at `point`, None when none does."""
    return next((window for window in windows if window.holds(point)), None)


def read_window_at(
    text: str,
    point: tuple[int, int],
    display_id: int = 0,
    *,
    owned: Callable[[InputWindow], bool] | None = None,
) -> InputWindow | str:
    """Return the window a touch at `point` of `display_id` reaches, read from
    the whole text of ``dumpsys input``, or why that is not known.

    The reason is one of `parse_input_windows`, `ROTATION` (the display's
    orientation is not 0 or cannot be read, see `display_orientation`),
    `NO_WINDOW` (no window takes a touch there) or `PORTAL` (the window there
    forwards the touch to another display). The reasons of the first two are
    an `Unreadable` that names the rule that refused (see `RULES`).

    `owned` tells whether a window is one of the app the check is for (the
    caller's own test of the owner uid and title): a window of the list
    with ``samsungFlags=0x1`` is read only when it says so, else the list is
    `LINE_FORMAT` (``samsung_flags``), whether or not that window is at the
    point (see `parse_input_windows`).
    """
    windows = _checked(_split(text), display_id, owned=owned)
    if isinstance(windows, _Refusal):
        return Unreadable(windows.reason, windows.rule)
    found = window_at(windows, point)
    if found is None:
        return NO_WINDOW
    return PORTAL if found.portal else found
