"""Which physical display is on, read from ``dumpsys display``.

On a phone with two displays (a foldable) ``screencap`` needs a physical
display id (``screencap -p -d ID``). The screen read (``uiautomator dump``) is
of the display that is on: logical display 0 when the phone is open, and
logical display 1 (the outer screen) when it is folded. ``dumpsys display``
tells which display is on, and its physical id, in two places:

- the ``mViewports=[DisplayViewport{..., isActive=true, displayId=0,
  uniqueId='local:ID', ...}, ...]`` line, and
- the ``Logical Displays:`` section, where each display's block has
  ``mDisplayId=0``, ``mPrimaryDisplayDevice=Built-in Screen(local:ID)`` and
  ``mIsEnabled=true``.

A display is on when its viewport has ``isActive=true`` and its block has
``mIsEnabled=true``. `parse_active_display_physical_id` returns an id only
when exactly one display is on, both places are well-formed for every display,
and both name the same device (digits only) for that display. It is a pure
function of the text; the text is never returned or put in an error.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["parse_active_display_physical_id"]

_VIEWPORTS_LINE: Final = re.compile(r"\s*mViewports=\[")
_VIEWPORT: Final = re.compile(r"DisplayViewport\{([^{}]*)\}")
_VIEWPORT_DISPLAY: Final = re.compile(r"(?:^|, )displayId=([0-9]{1,20})(?=,|$)")
_VIEWPORT_UNIQUE: Final = re.compile(r"(?:^|, )uniqueId='local:([^']*)'(?=,|$)")
_VIEWPORT_ACTIVE: Final = re.compile(r"(?:^|, )isActive=(true|false)(?=,|$)")
_LOGICAL_HEADER: Final = re.compile(r"(\s*)Logical Displays:")
_DISPLAY_HEADING: Final = re.compile(r"\s*Display ([0-9]{1,20}):\s*")
_DISPLAY_ID_LINE: Final = re.compile(r"\s*mDisplayId=([0-9]{1,20})\s*")
_DEVICE_LINE: Final = re.compile(r"\s*mPrimaryDisplayDevice=.*\(local:([^()]*)\)\s*")
_ENABLED_LINE: Final = re.compile(r"\s*mIsEnabled=(true|false)\s*")
_PHYSICAL_ID: Final = re.compile(r"[0-9]{1,20}")

# (physical id as written, whether the display is on)
type _Facts = dict[str, tuple[str, bool]]


def _lines(text: str) -> list[str]:
    """Split at LF, dropping at most one CR before each."""
    return [line.removesuffix("\r") for line in text.split("\n")]


def _viewports(lines: list[str]) -> _Facts | None:
    """Every display of the one ``mViewports=`` line, or None if any is unclear."""
    holders = [line for line in lines if _VIEWPORTS_LINE.match(line)]
    if len(holders) != 1:
        return None
    found: _Facts = {}
    for body in _VIEWPORT.findall(holders[0]):
        number = _VIEWPORT_DISPLAY.findall(body)
        unique = _VIEWPORT_UNIQUE.findall(body)
        active = _VIEWPORT_ACTIVE.findall(body)
        if len(number) != 1 or len(unique) != 1 or len(active) != 1:
            return None
        if number[0] in found:
            return None
        found[number[0]] = (unique[0], active[0] == "true")
    return found or None


class _Block:
    """One display of the ``Logical Displays:`` section."""

    def __init__(self, heading: str | None) -> None:
        self.heading = heading
        self.ids: list[tuple[str, int]] = []  # (mDisplayId, indent)
        self.devices: list[tuple[str, int]] = []  # (id in the line, indent)
        self.enabled: list[tuple[str, int]] = []  # ("true" or "false", indent)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _logical_blocks(lines: list[str]) -> list[_Block] | None:
    """Split the one ``Logical Displays:`` section into display blocks."""
    starts = [
        (number, _indent(line))
        for number, line in enumerate(lines)
        if _LOGICAL_HEADER.fullmatch(line.split(" size=")[0])
    ]
    if len(starts) != 1:
        return None
    start, header_indent = starts[0]
    blocks: list[_Block] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        heading = _DISPLAY_HEADING.fullmatch(line)
        # The section ends at the first line not deeper than its fields; a
        # display's own heading sits at the header's indent.
        if _indent(line) <= header_indent + 1 and not heading:
            break
        if heading:
            blocks.append(_Block(heading.group(1)))
        elif match := _DISPLAY_ID_LINE.fullmatch(line):
            if not blocks or blocks[-1].ids:
                blocks.append(_Block(None))
            blocks[-1].ids.append((match.group(1), _indent(line)))
        elif match := _DEVICE_LINE.fullmatch(line):
            if not blocks:
                blocks.append(_Block(None))
            blocks[-1].devices.append((match.group(1), _indent(line)))
        elif match := _ENABLED_LINE.fullmatch(line):
            if not blocks:
                blocks.append(_Block(None))
            blocks[-1].enabled.append((match.group(1), _indent(line)))
    return blocks


def _logical(lines: list[str]) -> _Facts | None:
    """Every display of the ``Logical Displays:`` section, or None if unclear.

    A line counts for the display block it is in, and only at the indent of
    that block's ``mDisplayId=`` line (a deeper line belongs to a nested part).
    """
    blocks = _logical_blocks(lines)
    if not blocks:
        return None
    found: _Facts = {}
    for block in blocks:
        if len(block.ids) != 1:
            return None
        number, indent = block.ids[0]
        if block.heading is not None and block.heading != number:
            return None
        devices = [device for device, at in block.devices if at == indent]
        flags = [flag for flag, at in block.enabled if at == indent]
        if len(devices) != 1 or len(flags) != 1 or number in found:
            return None
        found[number] = (devices[0], flags[0] == "true")
    return found


def parse_active_display_physical_id(text: str) -> str | None:
    """Return the physical id of the display that is on, or None.

    A display is on when its viewport says ``isActive=true`` and its logical
    block says ``mIsEnabled=true``. None is returned (nothing is guessed) unless
    both places are well-formed for every display and name the same displays,
    exactly one display is on, both name the same device for it, and that id is
    1 to 20 ASCII digits.
    """
    lines = _lines(text)
    views = _viewports(lines)
    blocks = _logical(lines)
    if views is None or blocks is None or views.keys() != blocks.keys():
        return None
    on = [
        number for number, (_, active) in views.items() if active and blocks[number][1]
    ]
    if len(on) != 1:
        return None
    first, second = views[on[0]][0], blocks[on[0]][0]
    if first != second or _PHYSICAL_ID.fullmatch(first) is None:
        return None
    return first
