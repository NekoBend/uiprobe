"""Which window a touch at a point would reach, read from ``dumpsys input``.

The input dispatcher lists the windows of a display from the top down, each
with whether it takes touches, the region that takes them and the uid of the
app that owns it. The first window that takes touches at a point is the one a
tap there reaches; a window of another app can take touches without taking
the input focus (an overlay drawn over other apps, the keyboard, a floating
button), so the focused window alone does not tell.

Three forms of the window line were seen on emulators:

* API 24 to 30: ``name='Window{<hash> u0 <title>}'``, ``visible=true|false``,
  ``flags=0x<hex>`` (window manager flags; ``FLAG_NOT_TOUCHABLE`` is 0x10).
* API 31 and 32: ``name='<hash> <title>'``, ``visible=true|false``,
  ``flags=NOT_FOCUSABLE | NOT_TOUCHABLE | ...`` (names).
* API 36: ``name=<hash> <title>``, ``inputConfig=NOT_VISIBLE | NOT_TOUCHABLE |
  NO_INPUT_CHANNEL | SPY | ...`` (names; no ``visible=``).

All three have ``displayId=``, ``touchableRegion=[l,t][r,b]`` or ``<empty>``
and ``ownerUid=``. API 33 to 35 were not seen. Anything that does not read as
one of these forms makes the whole list unknown: a line that cannot be read
could be the window on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "LINE_FORMAT",
    "NO_LIST",
    "NO_WINDOW",
    "REGION_FORMAT",
    "InputWindow",
    "parse_input_windows",
    "window_at",
]

# Why the window at a point is not known (a short word for the records).
NO_LIST: Final = "no_list"
LINE_FORMAT: Final = "line_format"
REGION_FORMAT: Final = "region_format"
NO_WINDOW: Final = "no_window"

_FLAG_NOT_TOUCHABLE: Final = 0x10
# inputConfig names of windows that do not take a touch at their region: not
# shown, refusing touches, without an input channel, or only watching (a spy
# window sees touches that still go on to the window below).
_NOT_TAKING: Final = frozenset(
    {"NOT_VISIBLE", "NOT_TOUCHABLE", "NO_INPUT_CHANNEL", "SPY"}
)
_HEADER: Final = "Input Dispatcher State:"
_LINE = re.compile(
    r"^\s*(?P<index>\d+): name=(?:'(?P<quoted>[^']*)'|(?P<bare>.*?)), "
    r"(?:id=\d+, )?displayId=(?P<display>-?\d+),"
)
_REGION = re.compile(
    r"touchableRegion=(?:(?P<empty><empty>)|"
    r"\[(?P<l>-?\d+),(?P<t>-?\d+)\]\[(?P<r>-?\d+),(?P<b>-?\d+)\])(?:, |$)"
)
_UID = re.compile(r"ownerUid=(?P<uid>\d+)(?:,|$)")
_INPUT_CONFIG = re.compile(r"inputConfig=(?P<names>[^,]*),")
_HEX_FLAGS = re.compile(r"flags=0x(?P<hex>[0-9a-fA-F]+),")
_NAMED_FLAGS = re.compile(r"flags=(?P<names>[A-Z_][A-Z0-9_ |]*),")
_VISIBLE = re.compile(r"visible=(?P<visible>true|false),")
_WINDOW_TITLE = re.compile(r"^Window\{[0-9a-f]+ u\d+ (?P<title>.*)\}$")
_HASH_TITLE = re.compile(r"^[0-9a-f]+ (?P<title>.*)$")


@dataclass(frozen=True, slots=True)
class InputWindow:
    """One window of the dispatcher's list.

    ``region`` is the touchable region as (left, top, right, bottom), right
    and bottom exclusive, or None when it is empty. ``takes_touch`` says
    whether the window takes a touch inside its region at all.
    """

    title: str
    takes_touch: bool
    region: tuple[int, int, int, int] | None
    owner_uid: int

    def holds(self, point: tuple[int, int]) -> bool:
        """Whether a touch at `point` goes to this window."""
        if not self.takes_touch or self.region is None:
            return False
        left, top, right, bottom = self.region
        x, y = point
        return left <= x < right and top <= y < bottom


def _window_lines(text: str) -> list[str] | None:
    """The window lines of the dispatcher's first ``Windows:`` list."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == _HEADER), None)
    if start is None:
        return None
    header = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip() == "Windows:"),
        None,
    )
    if header is None:
        return None
    indent = len(lines[header]) - len(lines[header].lstrip())
    found: list[str] = []
    for line in lines[header + 1 :]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if re.match(r"^\s*\d+:", line):  # a window line (read or not)
            found.append(line)
    return found


def _title(name: str) -> str:
    match = _WINDOW_TITLE.match(name) or _HASH_TITLE.match(name)
    return match["title"] if match else name


def _takes_touch(line: str) -> bool | None:
    """Whether the window takes touches; None when the line has no known form."""
    config = _INPUT_CONFIG.search(line)
    if config is not None:  # API 36
        names = {name.strip() for name in config["names"].split("|")}
        return not names & _NOT_TAKING
    visible = _VISIBLE.search(line)
    if visible is None:
        return None
    shown = visible["visible"] == "true"
    hex_flags = _HEX_FLAGS.search(line)
    if hex_flags is not None:  # API 24 to 30
        return shown and not int(hex_flags["hex"], 16) & _FLAG_NOT_TOUCHABLE
    named = _NAMED_FLAGS.search(line)
    if named is not None:  # API 31 and 32
        names = {name.strip() for name in named["names"].split("|")}
        return shown and "NOT_TOUCHABLE" not in names
    return None


def parse_input_windows(
    text: str, display_id: int = 0
) -> tuple[InputWindow, ...] | str:
    """Read the windows of `display_id` from the top down.

    Returns the windows, or the reason they are not known: `NO_LIST` (no
    window list), `LINE_FORMAT` (a window line of no known form) or
    `REGION_FORMAT` (a touchable region that is not one rectangle or empty).
    """
    lines = _window_lines(text)
    if not lines:
        return NO_LIST
    windows: list[InputWindow] = []
    for line in lines:
        match = _LINE.match(line)
        if match is None:
            return LINE_FORMAT
        if int(match["display"]) != display_id:
            continue
        takes = _takes_touch(line)
        uid = _UID.search(line)
        if takes is None or uid is None:
            return LINE_FORMAT
        region = _REGION.search(line)
        if region is None:
            return REGION_FORMAT
        bounds = (
            None
            if region["empty"]
            else (
                int(region["l"]),
                int(region["t"]),
                int(region["r"]),
                int(region["b"]),
            )
        )
        name = match["quoted"] if match["quoted"] is not None else match["bare"]
        windows.append(InputWindow(_title(name), takes, bounds, int(uid["uid"])))
    if not windows:
        return NO_LIST
    return tuple(windows)


def window_at(
    windows: tuple[InputWindow, ...], point: tuple[int, int]
) -> InputWindow | None:
    """The topmost window that takes a touch at `point`, None when none does."""
    return next((window for window in windows if window.holds(point)), None)
