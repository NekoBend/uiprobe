"""Shared helpers of the swipe, scroll and key tests."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
)
from uiprobe import cli

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.model import Element
    from uiprobe.model import Screen as ScreenModel

SERIAL = "emulator-5554"


SHOP = "com.example.shop"


SUFFIX = "; echo uiprobe-exit:$?"


SWIPE_COMMAND = "input swipe 10 20 30 40 300" + SUFFIX


MARKER_FORMS = (
    re.compile(r"^input tap [0-9]+ [0-9]+; echo uiprobe-exit:\$\?\Z"),
    re.compile(r"^input swipe ([0-9]+) ([0-9]+) \1 \2 800; echo uiprobe-exit:\$\?\Z"),
    *FakeAndroid.SWIPE_ALLOWED,
    re.compile(r"^input keyevent KEYCODE_[A-Z0-9_]{1,40}; echo uiprobe-exit:\$\?\Z"),
)


# Display 320x640: 5% edge margins are 16 and 32 pixels.
SCREENS = (
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((10, 100, 310, 300), text="Pane", rid=f"{SHOP}:id/pane"),
            El((0, 0, 100, 100), text="Corner"),
            El((1, 1, 100, 100), text="Offset"),
            El((100, 100, 101, 300), text="Thin"),
            El((0, 0, 100, 40), text="Short"),
            El((10, 400, 100, 400), text="Flat"),
            El((200, 500, 250, 550), text="Twin", rid=f"{SHOP}:id/twin"),
            El((250, 500, 300, 550), text="Twin", rid=f"{SHOP}:id/twin"),
            El((10, 320, 150, 370), text="Off", enabled=False),
        ),
    ),
)


def sent_inputs(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands if c.startswith("input")]


def element(screen: ScreenModel, text: str) -> Element:
    return next(e for e in screen.elements if e.text == text)


# --- validation: nothing reaches the server ---


def no_request(android, fake_adb, call, error):
    before = len(fake_adb.requests)
    with pytest.raises(error):
        call()
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


# --- the command line ---


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err
