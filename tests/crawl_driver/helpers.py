"""Shared helpers of the crawl driver tests: a fake app, a fake clock and runs.

Every device is a strict `FakeAndroid` behind a fake adb server, so an input
that is not a tap, the BACK key or a launcher launch fails the test. Nothing
sleeps: the clock and the pause are injected.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from tests.fake_device import (
    El,
    Screen,
)
from uiprobe import crawl_driver
from uiprobe import device as device_module
from uiprobe.crawl import (
    CrawlEngine,
    Limits,
    Observation,
)
from uiprobe.crawl_driver import CrawlResult, crawl
from uiprobe.device import Device, FocusedWindow
from uiprobe.guards import Policy
from uiprobe.runlog import RunWriter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from tests.fake_adb_server import FakeAdbServer


SERIAL = "emulator-5554"


TRANSPORT = 7


SHOP = "com.example.shop"


OTHER = "com.example.other"


BUTTON = "android.widget.Button"


LAUNCHER = "android.intent.category.LAUNCHER"


STOP_REASONS = {
    "exhausted",
    "max_actions",
    "max_duration",
    "relaunch_limit",
    "keyboard_stuck",
    "dump_failed_repeatedly",
}


def button(top: int, text: str, go: str) -> El:
    return El((20, top, 300, top + 40), text=text, cls=BUTTON, go=go)


SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((0, 0, 320, 100), text="Shop"),
            button(200, "Browse", "browse"),
            button(260, "Help", "cart"),
            button(320, "Delete account", "main"),
        ),
    ),
    Screen(
        "browse",
        f"{SHOP}/.Browse",
        (El((0, 0, 320, 100), text="Items"), button(200, "Item", "detail")),
    ),
    Screen("detail", f"{SHOP}/.Detail", (El((0, 0, 320, 100), text="Detail"),)),
    Screen(
        "cart",
        f"{SHOP}/.Cart",
        (El((0, 0, 320, 100), text="Help page"),),
        protected=True,
    ),
    Screen("ext1", f"{OTHER}/.One", (El((0, 0, 320, 640)),)),
    Screen("ext2", f"{OTHER}/.Two", (El((0, 0, 320, 640)),)),
    Screen("ext3", f"{OTHER}/.Three", (El((0, 0, 320, 640)),)),
)


# BACK on screens of other apps allowed, as before it became a choice.
BACK_POLICY = Policy(back_on_other_screens=True)


class Clock:
    """A fake clock that only the pause moves."""

    def __init__(self) -> None:
        self.now = 5000.0
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


def run(
    device: Device,
    clock: Clock,
    out: Path,
    *,
    limits: Limits | None = None,
    screenshots: bool = False,
    allow_real_device: bool = False,
    settle: float = 1.0,
    stop: Callable[[], bool] | None = None,
    policy: Policy | None = None,
    restart: bool = True,
    record_text: bool = True,
) -> CrawlResult:
    return crawl(
        device,
        SHOP,
        out,
        policy=policy or Policy(),
        limits=limits or Limits(),
        screenshots=screenshots,
        allow_real_device=allow_real_device,
        settle=settle,
        stop=stop,
        clock=clock.monotonic,
        sleep=clock.sleep,
        restart=restart,
        record_text=record_text,
    )


def lines(path: Path):
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def spy(
    monkeypatch: pytest.MonkeyPatch,
    device: Device,
    name: str,
    before: Callable[..., None],
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Wrap a device method; `before(number, *args, **kwargs)` runs first."""
    original = getattr(device, name)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        before(len(calls), *args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(device, name, wrapper)
    return calls


def commands(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands]


def share_clock(monkeypatch, clock) -> None:
    """Make uiprobe's own clock (the one the device checks right before a
    request goes out) the test's clock, so that a test can move both."""
    monkeypatch.setattr(
        device_module,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )


def cover_after_browse(monkeypatch, device, android) -> None:
    """Bring screens of another app over the app after the tap on Browse,
    so that the engine relaunches."""
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)


def start_requests(fake_adb: FakeAdbServer) -> list[int]:
    """The positions of the start requests in the server's request log."""
    return [
        i for i, r in enumerate(fake_adb.requests) if r.startswith("shell:am start ")
    ]


# --- failed dumps, skips, a bad clock ---


def end_lines(out: Path) -> list[dict[str, object]]:
    return [
        r
        for r in lines(out / "transitions.jsonl")
        if r["schema"] == "uiprobe.transition_end/1"
    ]


# --- the gate: every action is judged right before it is sent ---


def _gate_run(
    tmp_path: Path,
    device: Device,
    clock: Clock,
    *,
    stop: Callable[[], bool] | None = None,
    policy: Policy | None = None,
) -> tuple[crawl_driver._Run, CrawlEngine, Observation]:
    """A run after its launch, with the first observation, for direct sends."""
    policy = policy or Policy()
    device.pin_transport()
    engine = CrawlEngine(SHOP, policy, Limits(minutes=1), started_at=clock.now)
    writer = RunWriter.create(
        tmp_path / "run",
        package=SHOP,
        limits=Limits(),
        deny_words=[],
        serial=SERIAL,
        back_on_other_screens=policy.back_on_other_screens,
    )
    _WRITERS.append(writer)
    driver = crawl_driver._Run(
        device,
        SHOP,
        writer,
        engine,
        policy,
        emulator=True,
        app_uid=device.package_uid(SHOP),
        screenshots=False,
        settle=0.0,
        deadline=clock.now + 60,
        stop=stop,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    driver.send(engine.start(), None)
    return driver, engine, driver.observe()


_WRITERS: list[RunWriter] = []


def _not_sent_reason(tmp_path: Path) -> str | None:
    records = lines(tmp_path / "run" / "transitions.jsonl")
    return records[-1].get("reason") if records[-1].get("not_sent") else None


SHADE = FocusedWindow("com.android.systemui", "NotificationShade", "2f7e017")


# --- the first launch stops the app first (restart, the default) ---

RESOLVE = (
    f"cmd package resolve-activity --brief -c {LAUNCHER} {SHOP}; echo uiprobe-exit:$?"
)


FORCE_STOP = f"am force-stop {SHOP}; echo uiprobe-exit:$?"


def changing_commands(fake_adb: FakeAdbServer) -> list[str]:
    """The starts and stops the device received, each with its launcher read."""
    return [
        c
        for c in commands(fake_adb)
        if c.startswith(("am ", "input ", "monkey")) or c == RESOLVE
    ]
