"""Shared helpers of the launch tests: commands, captures and a fake clock."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import TYPE_CHECKING

from tests.fake_device import El, Screen

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"


SHOP = "com.example.shop"


MISSING = "com.example.missing"


SETTINGS = "com.android.settings"


LAUNCHER = "android.intent.category.LAUNCHER"


RESOLVE_PREFIX = f"cmd package resolve-activity --brief -c {LAUNCHER} "


MARK = "; echo uiprobe-exit:$?"


RESOLVE = RESOLVE_PREFIX + SHOP + MARK


START_PREFIX = f"am start -W -a android.intent.action.MAIN -c {LAUNCHER} -n "


START = f"{START_PREFIX}{SHOP}/.Main; echo uiprobe-exit:$?"


STOP = f"am force-stop {SHOP}; echo uiprobe-exit:$?"


DATA = Path(__file__).parents[2] / "data" / "launch"


OK_36 = (
    "Starting: Intent { act=android.intent.action.MAIN"
    f" cat=[{LAUNCHER}] cmp={SHOP}/.Main }}\n"
    "Status: ok\nLaunchState: COLD\nActivity: {activity}\nTotalTime: 5\n"
    "WaitTime: 6\nComplete\nuiprobe-exit:0\n"
)


SCREENS = (
    Screen("launcher", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen("main", f"{SHOP}/.Main", (El((0, 0, 320, 100), text="Shop"),)),
    Screen("detail", f"{SHOP}/.Detail", (El((0, 0, 320, 100), text="Item"),)),
    Screen("settings", f"{SETTINGS}/.Settings", (El((0, 0, 320, 100), text="S"),)),
    Screen(
        "homepage",
        f"{SETTINGS}/.homepage.SettingsHomepageActivity",
        (El((0, 0, 320, 100), text="Settings"),),
    ),
)


def capture(name: str, *, marker: bool | None = None) -> bytes:
    """The output in tests/data/launch/`name` without its ``#`` header.

    `marker` False drops a last ``uiprobe-exit:`` line (the capture's own
    suffix, which uiprobe does not send with resolve-activity); True appends
    ``uiprobe-exit:0`` to a capture made without the suffix.
    """
    lines = (DATA / name).read_bytes().splitlines(keepends=True)
    body = list(itertools.dropwhile(lambda line: line.startswith(b"#"), lines))
    if marker is False and body and body[-1].startswith(b"uiprobe-exit:"):
        body.pop()
    if marker is True:
        body.append(b"uiprobe-exit:0\n")
    return b"".join(body)


def start_of(component: str) -> str:
    return f"{START_PREFIX}{component}; echo uiprobe-exit:$?"


class Clock:
    """A fake clock whose pause moves it forward."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def sent(fake_adb: FakeAdbServer, start: int = 0) -> list[str]:
    """The shell commands received after the first `start`, without the reads
    of the screen and the device (the launcher activity read stays)."""
    reads = ("dumpsys", "getprop", "wm size", "uiautomator", "rm ", "cat ")
    return [c for _, _, c in fake_adb.commands[start:] if not c.startswith(reads)]


def no_monkey(fake_adb: FakeAdbServer) -> bool:
    return not any("monkey" in c for _, _, c in fake_adb.commands)
