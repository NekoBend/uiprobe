"""The rule of every request the crawl sends that changes the device.

From the crawl's final pin comparison until the request reaches the device,
no code the caller supplied runs: not the clock, not ``stop``, not ``sleep``,
and no write of the run folder either. The time limit is checked in that
window only by uiprobe's own clock, inside the device code.

The test logs one ordered list of events: every call of a caller callable or
of the run writer (``caller``), the moment the crawl has decided to send
(``decided``: when `_Run.gate`, or for the start of a relaunch
`_Run.relaunch_halt`, returns; the gate's final pin comparison is its last
statement, and the relaunch's comes right after `relaunch_halt`), and every
request that changes the device as the fake device receives it (``send``).
For each send, no ``caller`` event may lie between the last ``decided`` and
the send.

The table is complete by construction: every family of changing commands the
strict fake device admits must have a row here, or be listed as one the crawl
never sends, so a new sending path cannot be added without a row.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT, Clock
from uiprobe import crawl_driver
from uiprobe.crawl import Limits
from uiprobe.crawl_driver import crawl
from uiprobe.device import _CHANGING_COMMANDS, Device
from uiprobe.guards import Policy
from uiprobe.runlog import RunWriter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

Event = tuple[str, str]

# Changing command families the strict fake admits that the crawl never
# sends, with the reason. Everything else needs a row in ROWS.
NOT_SENT_BY_CRAWL = {
    "input tap": "the crawl taps with a short `input swipe` in place",
    "input text": "the crawl never types text",
}


def family(command: str) -> str:
    """The first two words of a command: ``am start``, ``input swipe``."""
    return " ".join(command.split()[:2])


def _pattern_family(pattern: re.Pattern[str]) -> str | None:
    """The family of a changing command an allowlist pattern admits."""
    source = pattern.pattern.removeprefix("^")
    if not source.startswith(_CHANGING_COMMANDS):
        return None
    return " ".join(source.replace("\\", "").split()[:2])


def cover_after_browse(device: Device, android: FakeAndroid, monkeypatch) -> None:
    """Screens of another app come over the app after the tap on Browse, so
    the engine relaunches (force-stop, then start)."""
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None
    ) -> tuple[int, int]:
        result = (
            original(point) if _send_by is None else original(point, _send_by=_send_by)
        )
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)


def plain(device: Device, android: FakeAndroid, monkeypatch) -> None:
    """The small app as it is: launch, taps and BACK."""
    del device, android, monkeypatch


# Each row: a sending path, the family of its command, the scenario that
# makes the crawl send it, and which sends of that family are the row's.
ROWS: dict[str, tuple[str, Callable[..., None], Callable[[list[str], int], bool]]] = {
    "tap": ("input swipe", plain, lambda _before, _i: True),
    "back": ("input keyevent", plain, lambda _before, _i: True),
    "first launch": (
        "am start",
        plain,
        lambda before, _i: not any(c.startswith("am start") for c in before),
    ),
    "force-stop": ("am force-stop", cover_after_browse, lambda _before, _i: True),
    "relaunch start": (
        "am start",
        cover_after_browse,
        lambda before, _i: any(c.startswith("am force-stop") for c in before),
    ),
}


def instrumented_run(
    tmp_path: Path,
    fake_adb: FakeAdbServer,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[..., None],
) -> list[Event]:
    """Run a crawl with every caller callable, the writer, the decision
    points and the device's receipt of changing commands logged in order."""
    events: list[Event] = []
    android = FakeAndroid(
        fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT
    )
    device = Device(adb_server=fake_adb.address)
    scenario(device, android, monkeypatch)

    def received(command: str):
        events.append(("send", command))
        return android._shell(command)

    android.override(re.compile(r"^(?:input |am )"), received)

    for name in ("gate", "relaunch_halt"):
        original = getattr(crawl_driver._Run, name)

        def decided(self, *args, _original=original, _name=name, **kwargs):
            result = _original(self, *args, **kwargs)
            events.append(("decided", _name))
            return result

        monkeypatch.setattr(crawl_driver._Run, name, decided)

    for name in [n for n in vars(RunWriter) if n.startswith("write_")] + [
        "finish",
        "close",
    ]:
        original = getattr(RunWriter, name)

        def written(self, *args, _original=original, _name=name, **kwargs):
            events.append(("caller", f"writer.{_name}"))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(RunWriter, name, written)

    clock = Clock()

    def caller_clock() -> float:
        events.append(("caller", "clock"))
        return clock.monotonic()

    def caller_sleep(seconds: float) -> None:
        events.append(("caller", "sleep"))
        clock.sleep(seconds)

    def caller_stop() -> bool:
        events.append(("caller", "stop"))
        return False

    crawl(
        device,
        SHOP,
        tmp_path / "run",
        policy=Policy(),
        limits=Limits(),
        settle=0.0,
        stop=caller_stop,
        clock=caller_clock,
        sleep=caller_sleep,
    )
    return events


def test_every_changing_command_has_a_row(fake_adb):
    android = FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", general_swipes=True)
    families = {
        found
        for pattern in android.allowed()["shell"]
        if (found := _pattern_family(pattern)) is not None
    }
    rows = {row[0] for row in ROWS.values()}
    assert families - rows - set(NOT_SENT_BY_CRAWL) == set()
    assert rows <= families  # every row is a command the fake admits
    assert not rows & set(NOT_SENT_BY_CRAWL)


@pytest.mark.parametrize("row", list(ROWS))
def test_no_caller_code_runs_between_the_decision_and_the_send(
    tmp_path, fake_adb, monkeypatch, row
):
    wanted, scenario, is_mine = ROWS[row]
    events = instrumented_run(tmp_path, fake_adb, monkeypatch, scenario)
    sends = [(i, command) for i, (kind, command) in enumerate(events) if kind == "send"]
    rows = {spec[0] for spec in ROWS.values()}
    assert {family(c) for _, c in sends} <= rows  # nothing without a row went out
    checked = 0
    for index, command in sends:
        before = [c for i, c in sends if i < index]
        if family(command) != wanted or not is_mine(before, index):
            continue
        decisions = [i for i in range(index) if events[i][0] == "decided"]
        assert decisions, f"{command!r} was sent without a decision"
        window = events[decisions[-1] + 1 : index]
        callers = [what for kind, what in window if kind == "caller"]
        assert callers == [], f"{row}: {callers} ran before {command!r}"
        checked += 1
    assert checked, f"the scenario sent no {row}"
