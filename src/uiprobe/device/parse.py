"""Parsers for the display size and the activity name."""

from __future__ import annotations

import re
from typing import Final

from uiprobe.model import shorten_activity

_WM_SIZE_LINE: Final = re.compile(
    r"(Physical|Override) size: ([0-9]{1,5})x([0-9]{1,5})"
)


_MAX_DISPLAY_SIDE: Final = 32768


_DISPLAY_ZERO_LINE: Final = re.compile(r"\s*Display: mDisplayId=0(?:\s|$)")


_CURRENT_SIZE: Final = re.compile(r"cur=([0-9]{1,5})x([0-9]{1,5})")


def _parse_wm_size(text: str) -> tuple[int, int] | None:
    """Parse ``wm size`` output; the override wins over the physical size.

    Returns None unless every non-blank line is a well-formed size line, none
    is repeated, and at least one is present.
    """
    sizes: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _WM_SIZE_LINE.fullmatch(line.strip())
        if match is None or match[1] in sizes:
            return None
        width, height = int(match[2]), int(match[3])
        if not (0 < width <= _MAX_DISPLAY_SIDE and 0 < height <= _MAX_DISPLAY_SIDE):
            return None
        sizes[match[1]] = (width, height)
    return sizes.get("Override") or sizes.get("Physical")


def _parse_display_size(text: str) -> tuple[int, int] | None:
    """Parse the current size of display 0 from ``dumpsys window displays``.

    The block starts at a line ``Display: mDisplayId=0`` that ends there or
    goes on after whitespace, and runs up to the next ``Display:`` line. The
    size is read only from the summary line of the block, the first line that
    starts (after indentation) with ``init=``, and is already rotated. Returns
    None for anything unexpected: no such block, no summary line before the
    next display, no ``cur=`` token on it, a malformed one, several with
    different values, or a side that is zero or over the bound `wm size`
    parsing uses.
    """
    inside = False
    for line in text.splitlines():
        if not inside:
            inside = _DISPLAY_ZERO_LINE.match(line) is not None
            continue
        if line.lstrip().startswith("Display:"):
            return None
        if not line.lstrip().startswith("init="):
            continue
        sizes: set[tuple[int, int]] = set()
        for token in line.split():
            if not token.startswith("cur="):
                continue
            match = _CURRENT_SIZE.fullmatch(token)
            if match is None:
                return None
            width, height = int(match[1]), int(match[2])
            if not (0 < width <= _MAX_DISPLAY_SIDE and 0 < height <= _MAX_DISPLAY_SIDE):
                return None
            sizes.add((width, height))
        if len(sizes) != 1:
            return None
        return sizes.pop()
    return None


def _activity_name(package: str, component: tuple[str, str]) -> str:
    """Write the focused activity relative to the dumped package.

    A relative name (``.Main``) is first expanded with the component's package,
    and the result is then shortened against the dumped `package`.
    """
    component_package, name = component
    if name.startswith("."):
        name = component_package + name
    return shorten_activity(package, name)
