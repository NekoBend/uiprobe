"""Shared helpers of the dry-run tests: screens, a strict fake device, CLI calls.

The device is a strict `FakeAndroid`: any command beyond the reads a dump, the
foreground and the keyboard need fails the test."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import Device, cli
from uiprobe.settings import (
    SETTINGS_FILE,
)

if TYPE_CHECKING:
    import pytest

    from tests.fake_adb_server import FakeAdbServer


SERIAL = "emulator-5554"


SHOP = "com.example.shop"


OTHER = "com.other.app"


WINDOW = re.compile(r"^dumpsys window\Z")


ACTIVITIES = re.compile(r"^dumpsys activity activities\Z")


OTHER_APP = f"  mFocusedApp=ActivityRecord{{1 u0 {OTHER}/.Main t3}}\n"


# What a preview may send: the reads of dump, foreground and keyboard_shown.
READ_ONLY = (
    re.compile(r"^uiautomator dump /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml\Z"),
    re.compile(r"^rm -f /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml\Z"),
    re.compile(r"^cat /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml\Z"),
    re.compile(r"^wm size\Z"),
    re.compile(r"^getprop (?:ro\.build\.version\.sdk|ro\.kernel\.qemu)\Z"),
    re.compile(r"^dumpsys (?:window|activity activities|input_method)\Z"),
)


FORBIDDEN = ("input", "am ", "monkey", "pm ", "cmd ", "settings ", "svc", "screencap")


# Document order gives the ids: e1 is the root, e2.. follow the tuple.
FIXTURE = Screen(
    "main",
    f"{SHOP}/.Main",
    (
        El((40, 100, 280, 150), text="Browse", rid=f"{SHOP}:id/browse", go="main"),
        El((40, 160, 280, 210), text="Delete account", go="main"),
        El((40, 220, 280, 270), text="Open row", go="main"),
        El((50, 230, 270, 260), text="Delete"),
        El((40, 280, 280, 330), go="main"),
        El((40, 340, 280, 390), text="Settings", go="main", enabled=False),
        El((40, 700, 280, 750), text="Offscreen", go="main"),
        El((40, 400, 280, 450), text="Help", go="main"),
    ),
    keyboard=False,
)


EMPTY = Screen("empty", f"{SHOP}/.Empty", (El((40, 100, 280, 150), text="Label"),))


ALL_SKIPPED = Screen(
    "locked", f"{SHOP}/.Locked", (El((40, 100, 280, 150), text="Delete", go="main"),)
)


KEYBOARD = Screen(
    "typing",
    f"{SHOP}/.Typing",
    (El((40, 100, 280, 150), text="Browse", go="main"),),
    keyboard=True,
)


RULES = Screen(
    "rules",
    f"{SHOP}/.Rules",
    (
        El((40, 100, 280, 150), text="200", go="main"),
        El((40, 160, 280, 210), text="Sound", go="main", checkable=True, checked=True),
        El((40, 220, 280, 270), text="Page 2", go="main"),
        El((40, 280, 280, 330), text="7", go="main", checkable=True),
    ),
)


SCREENS = (FIXTURE, EMPTY, ALL_SKIPPED, KEYBOARD, RULES)


def device_of(fake_adb: FakeAdbServer) -> Device:
    return Device(adb_server=fake_adb.address)


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def device_commands(fake_adb: FakeAdbServer) -> list[str]:
    """The shell and exec commands the device received, in order."""
    return [
        request.partition(":")[2]
        for request in fake_adb.requests
        if request.startswith(("shell:", "exec:"))
    ]


def assert_read_only(fake_adb: FakeAdbServer, android: FakeAndroid) -> None:
    commands = device_commands(fake_adb)
    assert commands
    assert android.inputs == []
    for command in commands:
        assert any(pattern.match(command) for pattern in READ_ONLY), command
        probe = re.sub(r"^dumpsys input_method\Z", "", command)
        assert not any(word in probe for word in FORBIDDEN), command
        assert not command.startswith("wm size "), command


def crawl(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    return invoke(capsys, fake_adb, "crawl", SHOP, "--dry-run", *argv)


def crawl_json(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> dict[str, Any]:
    code, out, err = crawl(capsys, fake_adb, "--json", *argv)
    assert (code, err) == (0, "")
    return json.loads(out)


def skipped_by_text(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry.get("text", ""): entry for entry in payload["skip"]}


DRY_RUN_LAST = "nothing was pressed: this was a preview (--dry-run)"


# The switch lines after a preview on an emulator with the file's press = false.
GUIDANCE = [
    "to press this time: add --press --out DIR to the same command",
    f"to always press: set press = true in {SETTINGS_FILE}, and runs_dir (the folder"
    " that gets the run folders) or --out DIR on each run",
]


def pop_ending(lines: list[str], last: str = DRY_RUN_LAST) -> None:
    """Check and remove the closing line and the switch lines of a preview."""
    to_press, to_always = GUIDANCE
    if last == DRY_RUN_LAST:
        to_press = to_press.replace("add --press", "remove --dry-run and add --press")
    assert lines[-3:] == [last, to_press, to_always]
    del lines[-3:]


# --- rows with a second target ---

ROWS_SCREEN = Screen(
    "rows",
    f"{SHOP}/.Rows",
    # The fake renders its elements flat; `rows_xml` gives the nested tree.
    (El((232, 206, 304, 285), desc="Settings", cls="android.widget.ImageView"),),
)


def rows_xml(android: FakeAndroid) -> bytes:
    """A dump of two rows: the "T-Mobile" row of AOSP Settings (a two-target
    row: a divider, then the gear) and a mail row with a star button and no
    divider, each a clickable row with nested children."""
    node = android._node
    gear = node(
        0,
        6,
        El(
            (232, 206, 304, 285),
            desc="Settings",
            rid="com.android.settings:id/settings_button",
            cls="android.widget.ImageView",
            go="rows",
        ),
        SHOP,
    )
    frame = El(
        (232, 206, 304, 285),
        rid="android:id/widget_frame",
        cls="android.widget.LinearLayout",
    )
    divider = El(
        (215, 206, 232, 285),
        rid="com.android.settings:id/two_target_divider",
        cls="android.widget.LinearLayout",
    )
    title = El((72, 222, 148, 250), text="T-Mobile", rid="android:id/title")
    summary = El((72, 250, 90, 269), text="5G", rid="android:id/summary")
    row = El((0, 206, 320, 285), cls="android.widget.LinearLayout", go="rows")
    first = node(
        0,
        1,
        row,
        SHOP,
        node(0, 2, title, SHOP)
        + node(1, 3, summary, SHOP)
        + node(2, 4, divider, SHOP)
        + node(3, 5, frame, SHOP, gear),
    )
    mail = El((0, 300, 320, 380), cls="android.widget.LinearLayout", go="rows")
    subject = El((24, 320, 200, 360), text="Inbox: hello")
    star = El(
        (250, 310, 310, 370), desc="Star", cls="android.widget.ImageView", go="rows"
    )
    second = node(1, 7, mail, SHOP, node(0, 8, subject, SHOP) + node(1, 9, star, SHOP))
    root = El((0, 0, 320, 640), cls="android.widget.FrameLayout")
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
        f'<hierarchy rotation="0">{node(0, 0, root, SHOP, first + second)}</hierarchy>'
    ).encode()
