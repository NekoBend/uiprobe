"""Taps, swipes and scrolls: the limits and the checks on their arguments."""

from __future__ import annotations

from typing import Final

from uiprobe.model import Bounds, Element, Screen

LONG_PRESS_MS: Final = 800


# A tap holds still this long by default (``input swipe X Y X Y 10``), so that
# the "Pointer location" developer option shows it; a plain ``input tap`` is
# barely visible there (measured on one device, a Galaxy S25 Ultra at 60 Hz:
# 10 ms visible, 6 ms not). The cap stays below Android's usual default
# long-press time (about 400 to 500 ms, longer with accessibility settings);
# an app may use its own shorter threshold.
DEFAULT_PRESS_MS: Final = 10


MAX_PRESS_MS: Final = 200


_MAX_COORDINATE: Final = 32767


DEFAULT_SWIPE_MS: Final = 300


_MAX_SWIPE_MS: Final = 10000


# Seconds added to a swipe's own duration for the shell call's read timeout.
_SWIPE_SLACK: Final = 10.0


# A scroll starts and ends this share of the area's width (height) inside its
# left and right (top and bottom) edges, then keeps this share of the display
# away from the display edges, where the system gestures live.
SCROLL_INSET_PERCENT: Final = 20


SCROLL_EDGE_PERCENT: Final = 5


_DIRECTIONS: Final = frozenset({"up", "down", "left", "right"})


def _check_coordinate(value: object) -> int:
    """Return `value` when it is a usable coordinate, else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"a coordinate must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    value = int.__index__(value)  # an exact int, whatever the subclass overrides
    if not 0 <= value <= _MAX_COORDINATE:
        msg = f"a coordinate must be from 0 to {_MAX_COORDINATE}, not {value}"
        raise ValueError(msg)
    return value


def _press_point(target: object) -> tuple[int, int]:
    """Return the point to press for `target` (an ``(x, y)`` pair)."""
    if not isinstance(target, tuple | list):
        name = type(target).__name__
        msg = f"target must be an Element or an (x, y) pair, not {name}"
        raise TypeError(msg)
    if len(target) != 2:
        msg = f"a point needs exactly two coordinates, not {len(target)}"
        raise TypeError(msg)
    return _check_coordinate(target[0]), _check_coordinate(target[1])


def _check_duration(value: object) -> int:
    """Return `value` when it is a usable swipe duration in ms, else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"duration_ms must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    value = int.__index__(value)
    if not 1 <= value <= _MAX_SWIPE_MS:
        msg = f"duration_ms must be from 1 to {_MAX_SWIPE_MS}, not {value}"
        raise ValueError(msg)
    return value


def _check_press_ms(value: object) -> int:
    """Return `value` when it is a usable tap hold in ms (0 to 200), else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"press_ms must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    value = int.__index__(value)
    if not 0 <= value <= MAX_PRESS_MS:
        msg = f"press_ms must be from 0 to {MAX_PRESS_MS}, not {value}"
        raise ValueError(msg)
    return value


def _scroll_rectangle(area: Bounds, display: tuple[int, int]) -> Bounds:
    """Shrink `area` and keep it away from the display edges (floor division)."""
    inset_x = area.width * SCROLL_INSET_PERCENT // 100
    inset_y = area.height * SCROLL_INSET_PERCENT // 100
    margin_x = display[0] * SCROLL_EDGE_PERCENT // 100
    margin_y = display[1] * SCROLL_EDGE_PERCENT // 100
    return Bounds(
        max(area.left + inset_x, margin_x),
        max(area.top + inset_y, margin_y),
        min(area.right - inset_x, display[0] - margin_x),
        min(area.bottom - inset_y, display[1] - margin_y),
    )


def _scroll_points(
    direction: str, area: Bounds, display: tuple[int, int]
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return the swipe points for `direction` in `area` of a `display`.

    Raises ValueError when the area is too small to scroll.
    """
    box = _scroll_rectangle(area, display)
    # Under 20 pixels the 5% margin floors to 0, and a far edge would be
    # one past the last pixel; such a display is not a real one.
    if min(display) < 20 or box.width < 2 or box.height < 2:
        msg = "the area is too small to scroll"
        raise ValueError(msg)
    middle_x, middle_y = box.center
    # Right and bottom are exclusive, so the last pixel is one before them.
    last_x, last_y = box.right - 1, box.bottom - 1
    match direction:
        case "up":
            return (middle_x, last_y), (middle_x, box.top)
        case "down":
            return (middle_x, box.top), (middle_x, last_y)
        case "left":
            return (last_x, middle_y), (box.left, middle_y)
        case _:
            return (box.left, middle_y), (last_x, middle_y)


def _check_direction(direction: str) -> None:
    if not isinstance(direction, str):
        msg = f"direction must be a str, not {type(direction).__name__}"
        raise TypeError(msg)
    if direction not in _DIRECTIONS:
        msg = "direction must be one of up, down, left, right"
        raise ValueError(msg)


class DisplaySizeUnknownError(ValueError):
    """`Device.scroll_display` could not read the display size."""


def _scroll_area(screen: Screen, within: Element | None) -> Bounds:
    """Return the area a scroll works in: the visible part of `within` or all."""
    if within is None:
        return Bounds(0, 0, screen.display[0], screen.display[1])
    if not any(e is within for e in screen.elements):
        msg = (
            "this element is not from the latest dump of this device; "
            "dump again and use an element of that screen"
        )
        raise ValueError(msg)
    return screen.require_visible(within)
