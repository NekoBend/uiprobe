"""Shared helpers of the tap tests."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from tests.fake_device import (
    El,
    Screen,
)
from uiprobe import cli

if TYPE_CHECKING:
    import pytest

    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.model import Element
    from uiprobe.model import Screen as ScreenModel

SERIAL = "emulator-5554"


SHOP = "com.example.shop"


TAP_COMMAND = "input swipe 80 35 80 35 10; echo uiprobe-exit:$?"


LONG_COMMAND = "input swipe 80 35 80 35 800; echo uiprobe-exit:$?"


MARKER_FORMS = (
    re.compile(r"^input tap [0-9]+ [0-9]+; echo uiprobe-exit:\$\?\Z"),
    re.compile(r"^input swipe ([0-9]+) ([0-9]+) \1 \2 800; echo uiprobe-exit:\$\?\Z"),
    re.compile(
        r"^input swipe ([0-9]+) ([0-9]+) \1 \2"
        r" (?:[1-9][0-9]?|1[0-9]{2}|200); echo uiprobe-exit:\$\?\Z"
    ),
)


SCREENS = (
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((10, 10, 150, 60), text="Login", rid=f"{SHOP}:id/login", go="next"),
            El((10, 70, 150, 120), text="Hold", desc="menu", long_go="menu"),
            El((200, 200, 250, 250), text="Twin", rid=f"{SHOP}:id/twin"),
            El((250, 200, 300, 250), text="Twin", rid=f"{SHOP}:id/twin"),
            El((300, 560, 400, 700), text="Cut", desc="cut"),
            El((-20, 500, 100, 550), text="Negative", desc="neg"),
            El((203, 300, 80, 350), text="Inverted"),
            El((10, 400, 100, 400), text="Flat"),
            El((0, 700, 100, 760), text="Below"),
            El((10, 300, 150, 350), text="Off", enabled=False, go="next"),
            El((300, 10, 330, 60), text="Edge"),
            El((0, 150, 40, 190), text="Pair", rid=f"{SHOP}:id/ra"),
            El((50, 150, 90, 190), text="Pair", rid=f"{SHOP}:id/rb"),
            El((100, 150, 140, 190), text="Other", rid=f"{SHOP}:id/ra"),
            El((150, 150, 190, 190), text="Dd", desc="da"),
            El((200, 150, 240, 190), text="Dd", desc="db"),
            El((250, 150, 290, 190), text="De", desc="da"),
        ),
    ),
    Screen("next", f"{SHOP}/.Next", (El((0, 0, 320, 100), text="Next"),)),
    Screen("menu", f"{SHOP}/.Menu", (El((0, 0, 320, 100), text="Menu"),)),
)


def sent_inputs(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands if c.startswith("input")]


def element(screen: ScreenModel, text: str) -> Element:
    return next(e for e in screen.elements if e.text == text)


# --- the command line ---


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the 8 x 8 pixel rule and the cover check ---

COVER_SERIAL = "emulator-5558"


COVER_SCREEN = Screen(
    "c",
    f"{SHOP}/.Cover",
    (
        El((20, 100, 300, 160), text="Pay", rid=f"{SHOP}:id/pay", go="c"),
        El((100, 110, 200, 150), text="Promo"),
        El((20, 300, 300, 360), text="Clear", rid=f"{SHOP}:id/clear", go="c"),
        El((0, 280, 320, 380)),  # an empty layer on top: looked through
        El((20, 633, 300, 700), text="Sliver", rid=f"{SHOP}:id/sliver", go="c"),
        El((20, 632, 300, 700), text="Eight", rid=f"{SHOP}:id/eight", go="c"),
    ),
)


# --- selector matches are counted without the 8-pixel rule ---

MATCH_SERIAL = "emulator-5559"


MATCH_SCREEN = Screen(
    "m",
    f"{SHOP}/.Match",
    (
        El((20, 100, 120, 200), text="Item"),  # 100 x 100
        El((20, 633, 120, 640), text="Item"),  # 100 x 7 at the bottom edge
        El((200, 100, 300, 200), text="Lonely"),
        El((200, 633, 300, 640), text="Lonely", rid=f"{SHOP}:id/lonely"),  # 7 high
        El((20, 300, 100, 380), rid=f"{SHOP}:id/isolated"),  # no label, no flag
    ),
)
