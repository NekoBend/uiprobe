"""A fake app with a long list, for the tests of the scrolling crawl.

``main`` shows a list of rows ("Row 01", "Row 02", ...) through a window of the
display; only the rows that lie completely inside the window are in the dump,
and a mouse wheel turn at a point in the window moves it (see
`tests.fake_device.Scrolling`).
Every row opens ``leaf``, whose BACK returns to the list at the same offset.
"""

from __future__ import annotations

from tests.fake_device import El, Screen, Scrolling

SHOP = "com.example.shop"
BUTTON = "android.widget.Button"

# The visible window of the list on the 320 x 640 display of the fake device.
WINDOW = (0, 100, 320, 600)
# One wheel step moves the content by 80 pixels, so a scroll of the default
# amount (3) moves it by four rows and the list keeps its screen id (always 8
# rows inside the window).
UNIT = 80
VISIBLE = 8


def rows(count: int, *, go: str = "leaf", prefix: str = "Row") -> tuple[El, ...]:
    """``count`` rows 60 pixels apart, in content coordinates."""
    return tuple(
        El(
            (20, index * 60, 300, index * 60 + 40),
            text=f"{prefix} {index + 1:02d}",
            cls="android.widget.TextView",
            go=go,
        )
        for index in range(count)
    )


def screens(
    count: int = 14,
    *,
    unit: int = UNIT,
    extra: tuple[El, ...] = (),
    window: tuple[int, int, int, int] = WINDOW,
    list_rows: tuple[El, ...] | None = None,
) -> tuple[Screen, ...]:
    """The launcher, the list screen ``main`` (with `extra` elements drawn
    before the list) and ``leaf``."""
    return (
        Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
        Screen(
            "main",
            f"{SHOP}/.Main",
            (El((0, 0, 320, 100), text="Shop"), *extra),
            scrolling=Scrolling(
                rows(count) if list_rows is None else list_rows, window, unit
            ),
        ),
        Screen("leaf", f"{SHOP}/.Leaf", (El((0, 0, 320, 100), text="Leaf"),)),
    )
