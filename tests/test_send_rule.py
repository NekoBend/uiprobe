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
* ``decided``: the final comparison itself. `_Run.epoch_is` must be exactly
  ``return self.device.pin_epoch == approved`` (checked on its source), so the
  read of its operand `Device.pin_epoch` is the comparison; that read is
  logged inside it, and becomes ``decided`` (with the path `final_check` was
  asked for) when the comparison holds;
* ``send``: every request that changes the device, as the fake device
  receives it.

Each send must be paired with its own decision (the last event of either
kind before it is that decision, of the path whose command it is), and no
``caller`` event may lie between them. Between the gate's reads and the
decision the caller's clock does run, once, as on main: the time is checked
again after the reads (limit L1 in docs/safety.md). The rows are scenarios,
each with the sending path it must send and checked on its own (BACK has two:
on a screen of the app, and on a screen of another app with
``back_on_other_screens``); the set of the rows' paths must equal the paths
the crawl declares (`crawl_driver._SEND_PATHS`), and a send without a
decision fails, so a new sending path cannot be added without a row. The
first launch force-stops the app by default (``restart=True``), through the
``force-stop`` and ``start`` paths of a relaunch: every row but those of
the ``launch`` path runs with it, so each of them checks that force-stop and
start as well, and the ``first-force-stop`` row checks that they are the
first two sends. The rows of the ``launch`` path (``launch`` and
``launch-two-displays``) run with ``restart=False``, whose first launch is a
start alone.
Meta-tests show that the test fails, for every row, when a clock call is
inserted into the production `_Run.final_check` right after its comparison
or into `_Run.epoch_is` right after the equality, and when the row of a path
is removed.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from typing import TYPE_CHECKING, NamedTuple

import pytest

from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT, Clock
from uiprobe import crawl_driver
from uiprobe.crawl import Limits, Relaunch, Tap
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
        # Until the first relaunch: the first launch's own force-stop is the
        # only one before it (its rows run with restart).
        if len(android.stopped) <= 1 and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)


def plain(device: Device, android: FakeAndroid, monkeypatch) -> None:
    """The small app as it is: launch, taps and BACK."""
    del device, android, monkeypatch


# The package in front at each BACK of the off-target row.
OFF_TARGET_BACKS: list[str] = []


def off_target_back(device: Device, android: FakeAndroid, monkeypatch) -> None:
    """As `cover_after_browse`, and BACK is sent to the other app's screens
    (the row's policy has ``back_on_other_screens``)."""
    cover_after_browse(device, android, monkeypatch)
    OFF_TARGET_BACKS.clear()
    original = device.key

    def key(
        name: str,
        refused: object = None,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> object:
        OFF_TARGET_BACKS.append(android.screen.package)
        return original(name, refused=refused, _send_by=_send_by, _epoch=_epoch)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(device, "key", key)


# Added to the fake's `dumpsys window`: a second display without focus.
SECOND_DISPLAY = (
    "  Display: mDisplayId=0 (organized)\n"
    "  Display: mDisplayId=1 (organized)\n"
    "  mCurrentFocus=null\n"
    "  mTopFocusedDisplayId=0\n"
)


def two_displays(device: Device, android: FakeAndroid, monkeypatch) -> None:
    """The small app on a device with two displays, the tap window check off
    (the row's policy): the launch goes out, no tap or BACK does."""
    del device, monkeypatch
    android.extra_dumpsys["window"] = SECOND_DISPLAY


class Row(NamedTuple):
    """A scenario, the sending path it must send, its policy options, and
    whether the first launch force-stops the app (`crawl`'s ``restart``)."""

    path: str
    scenario: Callable[..., None]
    options: dict[str, bool]
    restart: bool = True


# The rows: every sending path has at least one.
ROWS: dict[str, Row] = {
    "tap": Row("tap", plain, {}),
    "back": Row("back", plain, {}),
    "back-off-target": Row("back", off_target_back, {"back_on_other_screens": True}),
    "launch": Row("launch", plain, {}, restart=False),
    "first-force-stop": Row("force-stop", plain, {}),
    "force-stop": Row("force-stop", cover_after_browse, {}),
    "start": Row("start", cover_after_browse, {}),
    "launch-two-displays": Row(
        "launch", two_displays, {"check_tap_window": False}, restart=False
    ),
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


def hooked_policy(log: _Log, **options: bool) -> Policy:
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
        **options,  # ty: ignore[invalid-argument-type]
    )


def instrument_decisions(log: _Log, monkeypatch: pytest.MonkeyPatch) -> None:
    """Log the final comparison: the operand read inside `_Run.epoch_is`
    becomes ``decided`` when the comparison holds."""
    final_check = crawl_driver._Run.final_check
    epoch_is = crawl_driver._Run.epoch_is
    asked: list[str] = []  # the path final_check is deciding for

    def with_path(self, path: str, approved: int) -> bool:
        asked.append(path)
        try:
            return final_check(self, path, approved)
        finally:
            asked.pop()

    inside: list[int] = []  # the index of the operand read in epoch_is
    pin_epoch = Device.pin_epoch

    def operand(device: Device) -> int:
        value = pin_epoch.fget(device)
        if asked and inside == [-1]:
            inside[0] = len(log.events)
            log.events.append(("operand", asked[-1]))
        return value

    def compared(self, approved: int) -> bool:
        inside.append(-1)
        try:
            holds = epoch_is(self, approved)
            at = inside[-1]
        finally:
            inside.pop()
        if holds and asked and at >= 0:
            # The operand read was the comparison: it is the decision.
            log.events[at] = ("decided", asked[-1])
        return holds

    monkeypatch.setattr(crawl_driver._Run, "final_check", with_path)
    monkeypatch.setattr(crawl_driver._Run, "epoch_is", compared)
    monkeypatch.setattr(Device, "pin_epoch", property(operand))


def instrumented_run(
    tmp_path: Path,
    fake_adb: FakeAdbServer,
    monkeypatch: pytest.MonkeyPatch,
    row: Row,
) -> list[Event]:
    """Run a crawl with caller code, decisions and sends logged in order."""
    log = _Log()
    android = FakeAndroid(
        fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT
    )
    device = Device(adb_server=fake_adb.address)
    row.scenario(device, android, monkeypatch)

    def received(command: str):
        log.events.append(("send", command))
        return android._shell(command)

    android.override(re.compile(r"^(?:input |am )"), received)

    def read(command: str):
        log.events.append(("read", command))
        return android._shell(command)

    android.override(re.compile(r"^(?:dumpsys |uiautomator |cat )"), read)

    instrument_decisions(log, monkeypatch)

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
        policy=hooked_policy(log, **row.options),
        limits=Limits(),
        settle=0.0,
        stop=caller_stop,
        clock=caller_clock,
        sleep=caller_sleep,
        restart=row.restart,
    )
    return log.events


def check_rule(events: list[Event]) -> dict[str, int]:
    """Assert the rule for every send of the run; return the sends per path.

    Every send must follow its own decision directly (no other decision or
    send between), of the path whose command family it is, and no caller
    event may lie between the decision and the send, whatever the path. A
    tap and BACK must also have a screen read before their decision.
    """
    sends: dict[str, int] = {}
    last: tuple[int, str] | None = None  # the open decision: index, path
    for index, (kind, what) in enumerate(events):
        if kind == "decided":
            last = (index, what)
        elif kind == "send":
            assert last is not None, f"{what!r} was sent without its own decision"
            decided_at, path = last
            assert family(what) == FAMILY[path], (
                f"{what!r} was sent on a decision for {path}"
            )
            window = events[decided_at + 1 : index]
            callers = [w for k, w in window if k == "caller"]
            assert callers == [], f"{path}: {callers} ran before {what!r}"
            if path in {"tap", "back"}:
                reads = [i for i in range(decided_at) if events[i][0] == "read"]
                assert reads, f"{path}: no screen read before {what!r}"
            sends[path] = sends.get(path, 0) + 1
            last = None  # one decision, one send
    return sends


def check_complete(rows: dict[str, Row]) -> None:
    """Every sending path the crawl declares has a row, and no row more; BACK
    also has its row on another app's screen, and the first launch has a row
    with its force-stop and one without."""
    declared = set(crawl_driver._SEND_PATHS)
    paths = {row.path for row in rows.values()}
    assert paths == declared, f"rows {sorted(paths)} != paths {sorted(declared)}"
    off_target = rows.get("back-off-target")
    assert off_target == Row(
        "back", off_target_back, {"back_on_other_screens": True}
    ), "rows: no BACK row on another app's screen"
    assert rows.get("first-force-stop") == Row("force-stop", plain, {}), (
        "rows: no row of the first launch's force-stop"
    )
    assert rows.get("launch") == Row("launch", plain, {}, restart=False), (
        "rows: no row of a first launch without a force-stop"
    )
    assert set(FAMILY) == declared


def test_every_sending_path_has_a_row():
    check_complete(ROWS)


def test_the_comparison_is_one_equality_and_nothing_else():
    # So that the read of its operand, which the rule test logs, is the
    # comparison itself.
    tree = ast.parse(textwrap.dedent(inspect.getsource(crawl_driver._Run.epoch_is)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    body = function.body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # the docstring
    assert len(body) == 1
    statement = body[0]
    assert isinstance(statement, ast.Return)
    assert ast.unparse(statement) == "return self.device.pin_epoch == approved"


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
    sends = check_rule(events)  # every send of the run, whatever its path
    row = ROWS[path]
    assert sends.get(row.path, 0) > 0, f"the scenario sent no {row.path}"
    if row.scenario is off_target_back:
        # Some of its BACKs went to the other app's screens.
        assert any(package != SHOP for package in OFF_TARGET_BACKS)
    if row.scenario is two_displays:
        assert sends.get("tap", 0) == sends.get("back", 0) == 0
    # The first two sends are the first launch: force-stop and start, each
    # on its own decision, with restart; a start alone without it.
    decided = [w for k, w in events if k == "decided"][:2]
    sent = [family(w) for k, w in events if k == "send"][:2]
    if row.restart:
        assert decided == ["force-stop", "start"]
        assert sent == ["am force-stop", "am start"]
    else:
        assert decided[0] == "launch"
        assert sent[0] == "am start"


# --- tests of the test ---


def clock_after_the_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put ``self.clock()`` into the production `_Run.final_check`, as its
    first statement after the comparison (a violation of the rule)."""
    source = textwrap.dedent(inspect.getsource(crawl_driver._Run.final_check))
    anchor = "    if not self.epoch_is(approved):\n        return False\n"
    assert source.count(anchor) == 1, "the comparison in final_check moved"
    source = source.replace(anchor, anchor + "    self.clock()\n")
    namespace: dict[str, object] = {}
    exec(source, vars(crawl_driver), namespace)  # noqa: S102 - the meta-test edits the method
    monkeypatch.setattr(crawl_driver._Run, "final_check", namespace["final_check"])


def clock_after_the_equality(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put ``self.clock()`` into `_Run.epoch_is`, right after its equality."""
    source = textwrap.dedent(inspect.getsource(crawl_driver._Run.epoch_is))
    anchor = "    return self.device.pin_epoch == approved\n"
    assert source.count(anchor) == 1, "the equality in epoch_is moved"
    source = source.replace(
        anchor,
        "    result = self.device.pin_epoch == approved\n"
        "    self.clock()\n"
        "    return result\n",
    )
    namespace: dict[str, object] = {}
    exec(source, vars(crawl_driver), namespace)  # noqa: S102 - the meta-test edits the method
    monkeypatch.setattr(crawl_driver._Run, "epoch_is", namespace["epoch_is"])


@pytest.mark.parametrize("path", list(ROWS))
def test_the_rule_test_fails_on_a_clock_call_inside_epoch_is(
    tmp_path, fake_adb, monkeypatch, path
):
    clock_after_the_equality(monkeypatch)
    events = instrumented_run(tmp_path, fake_adb, monkeypatch, ROWS[path])
    with pytest.raises(AssertionError, match="ran before"):
        check_rule(events)


@pytest.mark.parametrize("path", list(ROWS))
def test_the_rule_test_fails_on_a_clock_call_inside_final_check(
    tmp_path, fake_adb, monkeypatch, path
):
    clock_after_the_comparison(monkeypatch)
    events = instrumented_run(tmp_path, fake_adb, monkeypatch, ROWS[path])
    with pytest.raises(AssertionError, match="ran before"):
        check_rule(events)


def test_the_rule_test_fails_on_a_clock_call_before_the_tap_after_a_relaunch(
    tmp_path, fake_adb, monkeypatch
):
    # The relaunch rows also check the taps their scenario sends: a caller
    # clock read put right before the first tap after a relaunch (after its
    # gate decided) is caught there.
    relaunched = [False]
    injected: list[bool] = []
    relaunch = crawl_driver._Run.relaunch
    send_input = crawl_driver._Run.send_input

    def mark(self, action_id, action):
        result = relaunch(self, action_id, action)
        # A relaunch, not the first launch (which goes through it too).
        relaunched[0] = relaunched[0] or isinstance(action, Relaunch)
        return result

    def clock_then_send(self, action, action_id):
        if isinstance(action, Tap) and relaunched[0] and not injected:
            injected.append(True)
            self.clock()  # inside the window: after the decision, before the tap
        return send_input(self, action, action_id)

    monkeypatch.setattr(crawl_driver._Run, "relaunch", mark)
    monkeypatch.setattr(crawl_driver._Run, "send_input", clock_then_send)
    events = instrumented_run(tmp_path, fake_adb, monkeypatch, ROWS["start"])
    assert injected
    with pytest.raises(AssertionError, match=r"tap: .* ran before"):
        check_rule(events)


def test_the_completeness_test_fails_without_the_off_target_back_row():
    rows = {name: row for name, row in ROWS.items() if name != "back-off-target"}
    with pytest.raises(AssertionError, match="rows"):
        check_complete(rows)


def test_the_completeness_test_fails_without_the_first_force_stop_row():
    rows = {name: row for name, row in ROWS.items() if name != "first-force-stop"}
    with pytest.raises(AssertionError, match="first launch's force-stop"):
        check_complete(rows)


def test_the_completeness_test_fails_without_the_relaunch_start_row():
    rows = {name: row for name, row in ROWS.items() if row.path != "start"}
    with pytest.raises(AssertionError, match="rows"):
        check_complete(rows)
