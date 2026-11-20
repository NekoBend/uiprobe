"""The rule of every request the crawl sends that changes the device.

From the crawl's final pin comparison until the request reaches the device,
no code the caller supplied runs: not the clock, `stop` or `sleep`, not a
method or finalizer of a value they returned, not an accessor of the policy,
and no write of the run folder either. The time limit is checked in that
window only by uiprobe's own clock, inside the device code.

The test logs one ordered list of events:

* ``caller``: every call of the caller's clock, `stop` and `sleep`, every
  hook of the values they return (``__float__``, ``__bool__``, comparisons,
  ``__del__``), every field read of a `Policy` subclass, every method of the
  words in it, and every write of the run writer;
* ``decided``: the final comparison itself, logged by a wrapper around
  `_Run.final_check` (which holds only that comparison) when it approves,
  with the sending path it was made for;
* ``send``: every request that changes the device, as the fake device
  receives it.

Each send must be paired with its own decision (the last event of either
kind before it is that decision, of the path whose command it is), and no
``caller`` event may lie between them. The rows are the sending paths, each
checked on its own; the set of rows must equal the paths the crawl declares
(`crawl_driver._SEND_PATHS`), and a send without a decision fails, so a new
sending path cannot be added without a row. Two meta-tests show that the
test fails when a clock call is inserted right after the comparison and when
the row of a path is removed.
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

# The command family each sending path sends.
FAMILY = {
    "tap": "input swipe",
    "back": "input keyevent",
    "force-stop": "am force-stop",
    "launch": "am start",
    "start": "am start",
}
# The paths whose gate reads the screen right before the decision.
UI_READ_PATHS = frozenset({"tap", "back"})
# Changing command families the strict fake admits that the crawl never
# sends, with the reason.
NOT_SENT_BY_CRAWL = {
    "input tap": "the crawl taps with a short `input swipe` in place",
    "input text": "the crawl never types text",
}


def family(command: str) -> str:
    """The first two words of a command: ``am start``, ``input swipe``."""
    return " ".join(command.split()[:2])


def cover_after_browse(device: Device, android: FakeAndroid, monkeypatch) -> None:
    """Screens of another app come over the app after the tap on Browse, so
    the engine relaunches (force-stop, then start)."""
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int],
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)


def plain(device: Device, android: FakeAndroid, monkeypatch) -> None:
    """The small app as it is: launch, taps and BACK."""
    del device, android, monkeypatch


# One row per sending path, with the scenario that makes the crawl send it.
ROWS: dict[str, Callable[..., None]] = {
    "tap": plain,
    "back": plain,
    "launch": plain,
    "force-stop": cover_after_browse,
    "start": cover_after_browse,
}


class _Log:
    """The ordered event list; `caller` notes a caller hook."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def caller(self, what: str) -> None:
        self.events.append(("caller", what))


def hooked_seconds(log: _Log) -> type[float]:
    """A float subclass whose hooks log as caller code."""

    class Seconds(float):
        def __float__(self) -> float:
            log.caller("clock.__float__")
            return float.__float__(self)

        def __bool__(self) -> bool:
            log.caller("clock.__bool__")
            return float.__bool__(self)

        def __ge__(self, other: float) -> bool:
            log.caller("clock.__ge__")
            return float.__ge__(self, other)

        def __lt__(self, other: float) -> bool:
            log.caller("clock.__lt__")
            return float.__lt__(self, other)

        def __sub__(self, other: float) -> float:
            log.caller("clock.__sub__")
            return float.__sub__(self, other)

        def __del__(self) -> None:
            log.caller("clock.__del__")

    return Seconds


def hooked_policy(log: _Log) -> Policy:
    """A `Policy` subclass whose field reads, and whose words' methods, log
    as caller code."""

    class Word(str):
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            log.caller("word.__eq__")
            return str.__eq__(self, other)

        def lower(self) -> str:
            log.caller("word.lower")
            return str.lower(self)

        def __str__(self) -> str:
            log.caller("word.__str__")
            return str.__str__(self)

    class Hooked(Policy):
        def __getattribute__(self, name: str) -> object:
            if not name.startswith("_"):
                log.caller(f"policy.{name}")
            return object.__getattribute__(self, name)

    base = Policy()
    return Hooked(
        deny_words=tuple(Word(w) for w in base.deny_words),
        confirm_words=tuple(Word(w) for w in base.confirm_words),
    )


def instrumented_run(
    tmp_path: Path,
    fake_adb: FakeAdbServer,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[..., None],
    *,
    after_comparison: Callable[[object], object] | None = None,
) -> list[Event]:
    """Run a crawl with caller code, decisions and sends logged in order.

    `after_comparison`, for the meta-test, runs right after an approving
    final comparison (inside the window the rule protects).
    """
    log = _Log()
    android = FakeAndroid(
        fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT
    )
    device = Device(adb_server=fake_adb.address)
    scenario(device, android, monkeypatch)

    def received(command: str):
        log.events.append(("send", command))
        return android._shell(command)

    android.override(re.compile(r"^(?:input |am )"), received)

    def read(command: str):
        log.events.append(("read", command))
        return android._shell(command)

    android.override(re.compile(r"^(?:dumpsys |uiautomator |cat )"), read)

    final_check = crawl_driver._Run.final_check

    def logged_check(self, path: str, approved: int) -> bool:
        approves = final_check(self, path, approved)
        if approves:
            log.events.append(("decided", path))
            if after_comparison is not None:
                after_comparison(self)
        return approves

    monkeypatch.setattr(crawl_driver._Run, "final_check", logged_check)

    for name in [n for n in vars(RunWriter) if n.startswith("write_")] + [
        "finish",
        "close",
    ]:
        original = getattr(RunWriter, name)

        def written(self, *args, _original=original, _name=name, **kwargs):
            log.caller(f"writer.{_name}")
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(RunWriter, name, written)

    seconds = hooked_seconds(log)
    clock = Clock()

    class Answer:
        def __bool__(self) -> bool:
            log.caller("stop.__bool__")
            return False

        def __del__(self) -> None:
            log.caller("stop.__del__")

    def caller_clock() -> float:
        log.caller("clock")
        return seconds(clock.monotonic())

    def caller_sleep(time: float) -> None:
        log.caller("sleep")
        clock.sleep(time)
        Answer()  # a temporary of the caller's, finalized at once

    def caller_stop() -> bool:
        log.caller("stop")
        return Answer()  # ty: ignore[invalid-explicit-return]

    crawl(
        device,
        SHOP,
        tmp_path / "run",
        policy=hooked_policy(log),
        limits=Limits(),
        settle=0.0,
        stop=caller_stop,
        clock=caller_clock,
        sleep=caller_sleep,
    )
    return log.events


def check_rule(events: list[Event], path: str) -> int:
    """Assert the rule for every send of `path`; return how many were checked.

    Every send must follow its own decision directly (no other decision or
    send between), of the path whose command family it is; for the sends of
    `path`, no caller event may lie between the decision and the send. For a
    tap and BACK, whose gate reads the screen, none may lie between the last
    of those reads and the send either (the caller's clock and `stop` come
    before the reads; the policy is a plain copy).
    """
    checked = 0
    last: tuple[int, str] | None = None  # the open decision: index, path
    for index, (kind, what) in enumerate(events):
        if kind == "decided":
            last = (index, what)
        elif kind == "send":
            assert last is not None, f"{what!r} was sent without its own decision"
            decided_at, decided_path = last
            assert family(what) == FAMILY[decided_path], (
                f"{what!r} was sent on a decision for {decided_path}"
            )
            if decided_path == path:
                window = events[decided_at + 1 : index]
                callers = [w for k, w in window if k == "caller"]
                assert callers == [], f"{path}: {callers} ran before {what!r}"
                if path in UI_READ_PATHS:
                    reads = [i for i in range(decided_at) if events[i][0] == "read"]
                    assert reads, f"{path}: no screen read before {what!r}"
                    window = events[reads[-1] + 1 : index]
                    callers = [w for k, w in window if k == "caller"]
                    assert callers == [], (
                        f"{path}: {callers} ran after the last read before {what!r}"
                    )
                checked += 1
            last = None  # one decision, one send
    return checked


def check_complete(rows: dict[str, Callable[..., None]]) -> None:
    """Every sending path the crawl declares has its own row, and no more."""
    declared = set(crawl_driver._SEND_PATHS)
    assert set(rows) == declared, f"rows {sorted(rows)} != paths {sorted(declared)}"
    assert set(FAMILY) == declared


def test_every_sending_path_has_a_row():
    check_complete(ROWS)


def test_every_changing_command_the_fake_admits_is_a_path_or_never_sent(fake_adb):
    android = FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", general_swipes=True)
    families = set()
    for pattern in android.allowed()["shell"]:
        source = pattern.pattern.removeprefix("^")
        if source.startswith(_CHANGING_COMMANDS):
            families.add(" ".join(source.replace("\\", "").split()[:2]))
    assert families - set(FAMILY.values()) - set(NOT_SENT_BY_CRAWL) == set()


@pytest.mark.parametrize("path", list(ROWS))
def test_no_caller_code_runs_between_the_final_comparison_and_the_send(
    tmp_path, fake_adb, monkeypatch, path
):
    events = instrumented_run(tmp_path, fake_adb, monkeypatch, ROWS[path])
    # The indirect caller code did run, before the windows.
    assert any(kind == "caller" and what.startswith("policy.") for kind, what in events)
    # The words' own methods never run at all: they are copied as plain str.
    assert not any(what.startswith("word.") for _, what in events)
    assert any(kind == "caller" and what == "clock.__float__" for kind, what in events)
    assert any(kind == "caller" and what.endswith("__del__") for kind, what in events)
    assert check_rule(events, path) > 0, f"the scenario sent no {path}"


# --- tests of the test ---


@pytest.mark.parametrize("path", list(ROWS))
def test_the_rule_test_fails_on_a_clock_call_right_after_the_comparison(
    tmp_path, fake_adb, monkeypatch, path
):
    events = instrumented_run(
        tmp_path,
        fake_adb,
        monkeypatch,
        ROWS[path],
        after_comparison=lambda run: run.clock(),  # ty: ignore[missing-attribute]
    )
    with pytest.raises(AssertionError, match="ran before"):
        check_rule(events, path)


def test_the_completeness_test_fails_without_the_relaunch_start_row():
    rows = {path: scenario for path, scenario in ROWS.items() if path != "start"}
    with pytest.raises(AssertionError, match="rows"):
        check_complete(rows)
