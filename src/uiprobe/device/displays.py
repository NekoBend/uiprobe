"""Which physical display is logical display 0, read from ``dumpsys display``.

On a phone with two displays (a foldable) ``screencap`` needs a physical
display id (``screencap -p -d ID``), while ``uiautomator dump`` and ``wm size``
read logical display 0. Which physical display that is changes when the phone
is folded or opened. ``dumpsys display`` names it in two places:

- the ``mViewports=[DisplayViewport{..., displayId=0, uniqueId='local:ID',
  ...}, ...]`` line, and
- the ``Logical Displays:`` section, where ``mDisplayId=0`` is followed by
  ``mPrimaryDisplayDevice=Built-in Screen(local:ID)``.

`parse_display_zero_physical_id` accepts an id only when both places name one,
they agree, and it is made of digits. It is a pure function of the text; the
text is never returned or put in an error.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["parse_display_zero_physical_id"]

_VIEWPORTS_LINE: Final = re.compile(r"\s*mViewports=\[")
_VIEWPORT: Final = re.compile(r"DisplayViewport\{([^{}]*)\}")
_VIEWPORT_DISPLAY: Final = re.compile(r"(?:^|, )displayId=([0-9]+)(?=,|$)")
_VIEWPORT_UNIQUE: Final = re.compile(r"(?:^|, )uniqueId='local:([^']*)'(?=,|$)")
_LOGICAL_HEADER: Final = re.compile(r"(\s*)Logical Displays:")
_DISPLAY_HEADING: Final = re.compile(r"\s*Display [0-9]+:\s*")
_DISPLAY_ID_LINE: Final = re.compile(r"\s*mDisplayId=([0-9]+)\s*")
_DEVICE_LINE: Final = re.compile(r"\s*mPrimaryDisplayDevice=.*\(local:([^()]*)\)\s*")
_PHYSICAL_ID: Final = re.compile(r"[0-9]{1,20}")


def _lines(text: str) -> list[str]:
    """Split at LF, dropping at most one CR before each."""
    return [line.removesuffix("\r") for line in text.split("\n")]


def _viewport_id(lines: list[str]) -> str | None:
    """The id of display 0 on the one ``mViewports=`` line, or None."""
    holders = [line for line in lines if _VIEWPORTS_LINE.match(line)]
    if len(holders) != 1:
        return None
    found: list[str | None] = []
    for body in _VIEWPORT.findall(holders[0]):
        shown = _VIEWPORT_DISPLAY.findall(body)
        if len(shown) != 1 or int(shown[0]) != 0:
            continue
        unique = _VIEWPORT_UNIQUE.findall(body)
        found.append(unique[0] if len(unique) == 1 else None)
    return found[0] if len(found) == 1 else None


def _logical_id(lines: list[str]) -> str | None:
    """The id of display 0 in the ``Logical Displays:`` section, or None."""
    starts = [
        (number, match.group(1))
        for number, line in enumerate(lines)
        if (match := _LOGICAL_HEADER.fullmatch(line.split(" size=")[0]))
    ]
    if len(starts) != 1:
        return None
    start, indent = starts[0]
    zero_devices: list[str] = []
    current: int | None = None
    devices_of_current = 0
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        # The section ends at the first line not deeper than its fields: each
        # display's own heading (``Display N:``) sits at the header's indent.
        if len(line) - len(line.lstrip()) <= len(
            indent
        ) + 1 and not _DISPLAY_HEADING.fullmatch(line):
            break
        if match := _DISPLAY_ID_LINE.fullmatch(line):
            current = int(match.group(1))
            devices_of_current = 0
        elif (match := _DEVICE_LINE.fullmatch(line)) and current is not None:
            devices_of_current += 1
            if current == 0:
                zero_devices.append(match.group(1))
        if devices_of_current > 1 and current == 0:
            return None
    return zero_devices[0] if len(zero_devices) == 1 else None


def parse_display_zero_physical_id(text: str) -> str | None:
    """Return the physical id of logical display 0, or None when not certain.

    The id comes from both the ``mViewports=`` line and the ``Logical
    Displays:`` section of ``dumpsys display`` output. None is returned unless
    each names exactly one device for display 0, both name the same id, and the
    id is 1 to 20 ASCII digits. Other displays (a third one, say) are ignored.
    """
    lines = _lines(text)
    first = _viewport_id(lines)
    second = _logical_id(lines)
    if first is None or first != second or _PHYSICAL_ID.fullmatch(first) is None:
        return None
    return first
