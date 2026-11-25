"""Tests for the crawl driver's tap protocol and the gate that judges each action."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

import pytest

from tests.fake_device import (
    FakeAndroid,
    assert_no_press_inside,
)
from uiprobe import crawl_driver, runlog
from uiprobe.crawl import (
    TAP_NOT_SENT,
    Back,
    CrawlEngine,
    Limits,
    Observation,
    Relaunch,
    Tap,
)
from uiprobe.crawl_driver import crawl
from uiprobe.crawl_driver import driver as driver_module
from uiprobe.crawl_driver import gate as gate_module
from uiprobe.crawl_driver import observe as observe_module
from uiprobe.device import Device, FocusedWindow
from uiprobe.errors import (
    AdbError,
    DeviceNotFoundError,
)
from uiprobe.guards import Policy
from uiprobe.runlog import RunWriter

if TYPE_CHECKING:
    from uiprobe.crawl import Action
    from uiprobe.model import Screen as ModelScreen

from tests.crawl_driver.helpers import (
    _WRITERS,
    SCREENS,
    SERIAL,
    SHADE,
    SHOP,
    STOP_REASONS,
    TRANSPORT,
    _gate_run,
    _not_sent_reason,
    commands,
    end_lines,
    lines,
    run,
    spy,
)

# --- the engine's tap protocol: recheck, send once, then tap_sent ---


@dataclass(frozen=True)
class Event:
    """One call the driver made: to the engine, or a tap or dump on the device."""

    kind: str
    tap: Tap | None = None
    allowed: bool | None = None
    point: tuple[int, int] | None = None


class EngineLog:
    """The engines `crawl` built, and the calls in the order they were made."""

    def __init__(self) -> None:
        self.engines: list[CrawlEngine] = []
        self.events: list[Event] = []

    def proposed(self) -> list[tuple[int, Tap]]:
        """The taps next() returned, with the index of that event."""
        return [
            (index, event.tap)
            for index, event in enumerate(self.events)
            if event.kind == "next" and event.tap is not None
        ]

    def reported(self) -> list[Tap]:
        return [e.tap for e in self.events if e.kind == "tap_sent" and e.tap]


@pytest.fixture
def engine_log(monkeypatch: pytest.MonkeyPatch, device: Device) -> EngineLog:
    log = EngineLog()

    class RecordingEngine(CrawlEngine):
        def __init__(
            self, package: str, policy: Policy, limits: Limits, *, started_at: float
        ) -> None:
            super().__init__(package, policy, limits, started_at=started_at)
            log.engines.append(self)

        def next(self, observation: Observation) -> Action:
            action = super().next(observation)
            tap = action if isinstance(action, Tap) else None
            log.events.append(Event("next", tap=tap))
            return action

        def recheck(self, tap: Tap, observation: Observation) -> bool:
            allowed = super().recheck(tap, observation)
            log.events.append(Event("recheck", tap=tap, allowed=allowed))
            return allowed

        def tap_sent(self, tap: Tap) -> None:
            log.events.append(Event("tap_sent", tap=tap))
            super().tap_sent(tap)

    # `_crawl_on` reads `CrawlEngine` in the module that holds it.
    monkeypatch.setattr(driver_module, "CrawlEngine", RecordingEngine)

    def on_tap(_number: int, *args: object, **_kwargs: object) -> None:
        log.events.append(Event("send", point=cast("tuple[int, int]", args[0])))

    def on_dump(_number: int, *_args: object, **_kwargs: object) -> None:
        log.events.append(Event("dump"))

    spy(monkeypatch, device, "tap", on_tap)
    spy(monkeypatch, device, "dump", on_dump)
    return log


def test_an_allowed_tap_is_sent_once_and_reported_once(
    tmp_path, device, android, clock, engine_log
):
    result = run(device, clock, tmp_path / "run")
    assert result.stop_reason == "exhausted"
    events = engine_log.events
    proposed = engine_log.proposed()
    assert proposed
    for index, tap in proposed:
        # next() -> fresh read -> recheck(same object) -> send -> tap_sent(same
        # object) -> the next observation.
        assert events[index + 1].kind == "dump"
        recheck = events[index + 2]
        assert recheck.kind == "recheck"
        assert recheck.tap is tap
        assert recheck.allowed is True
        assert events[index + 3] == Event("send", point=tap.point)
        assert events[index + 4].kind == "tap_sent"
        assert events[index + 4].tap is tap
        assert events[index + 5].kind == "dump"
    reported = engine_log.reported()
    assert [id(t) for t in reported] == [id(t) for _, t in proposed]
    sent = [i for i in android.inputs if i.kind == "tap"]
    assert len(sent) == len(reported)
    engine = engine_log.engines[0]
    assert engine.actions_sent == result.actions
    assert not any(TAP_NOT_SENT in s.reasons for s in engine.records.skipped)


def test_a_refused_recheck_sends_nothing_and_reports_nothing(
    tmp_path, device, android, clock, engine_log
):
    # Dump 1 follows the launch; dump 2 is the re-read before the first tap.
    android.switch_before_dump(2, "cart")
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason in STOP_REASONS
    events = engine_log.events
    refused = [
        (index, e.tap)
        for index, e in enumerate(events)
        if e.kind == "recheck" and e.allowed is False
    ]
    assert len(refused) == 1
    index, tap = refused[0]
    assert tap is not None
    assert events[index + 1].kind == "next"  # no send, no report in between
    assert all(t is not tap for t in engine_log.reported())
    allowed = [e.tap for e in events if e.kind == "recheck" and e.allowed]
    assert [id(t) for t in engine_log.reported()] == [id(t) for t in allowed]
    sends = [e for e in events if e.kind == "send"]
    assert len(sends) == len(allowed)
    assert tap.point not in [e.point for e in sends]
    rows = lines(out / "skipped.jsonl")
    assert [r["reasons"] for r in rows if r["fingerprint"] == tap.fingerprint] == [
        ["changed_before_press"]
    ]
    assert not [r for r in rows if TAP_NOT_SENT in r["reasons"]]


def test_a_tap_whose_send_fails_is_not_reported_and_the_engine_releases_it(
    tmp_path, device, android, monkeypatch, clock, engine_log
):
    def fail(call: int, *_args: object, **_kwargs: object) -> None:
        if call == 2:
            raise AdbError("the adb server answered FAIL")

    spy(monkeypatch, device, "tap", fail)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    proposed = engine_log.proposed()
    assert len(proposed) == 2
    first, failed = proposed[0][1], proposed[1][1]
    assert [id(t) for t in engine_log.reported()] == [id(first)]
    assert [e.allowed for e in engine_log.events if e.kind == "recheck"] == [
        True,
        True,
    ]
    assert len([i for i in android.inputs if i.kind == "tap"]) == 1
    engine = engine_log.engines[0]
    counted = engine.actions_sent
    transitions = len(engine.records.transitions)
    # The start line of the failed tap is on disk with the next number; the
    # engine did not count it.
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    assert starts[-1]["action"]["kind"] == "tap"
    assert starts[-1]["action_id"] == counted + 1
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"
    assert run_json["actions"] == starts[-1]["action_id"]
    # The run ended at the failed send: the failed tap was never reported, so
    # the engine still counts only what was sent.
    assert all(t is not failed for t in engine_log.reported())
    assert engine.actions_sent == counted
    assert len(engine.records.transitions) == transitions


def test_the_clock_values_given_to_the_engine_stay_in_range(
    tmp_path, device, monkeypatch
):
    nows: list[float] = []
    real = Observation

    def observation(
        screen: ModelScreen | None,
        foreground: str | None,
        keyboard_shown: bool | None,  # noqa: FBT001 - the driver passes it so
        now: float,
    ) -> Observation:
        nows.append(now)
        return real(screen, foreground, keyboard_shown, now)

    # The gate and `observe` read `Observation` in their own modules.
    monkeypatch.setattr(gate_module, "Observation", observation)
    monkeypatch.setattr(observe_module, "Observation", observation)
    result = crawl(
        device,
        SHOP,
        tmp_path / "run",
        policy=Policy(),
        limits=Limits(),
        settle=0.0,
        clock=time.monotonic,
        sleep=lambda _s: None,
    )
    assert result.stop_reason == "exhausted"
    assert nows
    assert all(0 <= now < 2**40 for now in nows)
    assert nows == sorted(nows)


def test_a_clock_out_of_range_leaves_no_folder(tmp_path, device, fake_adb):
    for number, value in enumerate((-1.0, float(2**40), float("inf"))):
        out = tmp_path / f"run{number}"
        with pytest.raises(ValueError, match="below 2"):
            crawl(
                device,
                SHOP,
                out,
                policy=Policy(),
                limits=Limits(),
                clock=lambda value=value: value,
                sleep=lambda _s: None,
            )
        assert not out.exists()
    assert not any(c.startswith(("monkey", "am ")) for c in commands(fake_adb))


def test_a_dump_with_duplicate_element_ids_is_recorded_as_a_failed_dump(
    tmp_path, device, monkeypatch, clock
):
    original = device.dump
    calls: list[int] = []

    def dump() -> ModelScreen:
        screen = original()
        calls.append(1)
        if len(calls) != 3:
            return screen
        # Dump 3 follows the first tap: give two elements the same id.
        first, second, *rest = screen.elements
        twin = replace(second, id=first.id)
        return replace(screen, elements=(first, twin, *rest))

    monkeypatch.setattr(device, "dump", dump)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "exhausted"
    ends = end_lines(out)
    assert ends[1]["action_id"] == 2
    assert ends[1]["dump_failed"] is True
    assert [e for e in ends if e["dump_failed"]] == [ends[1]]


def _make_action(kind: str, engine: CrawlEngine, observation: Observation):
    if kind == "tap":
        action = engine.next(observation)
        assert isinstance(action, Tap)
        return action
    return {"back": Back(), "relaunch": Relaunch()}[kind]


# Each condition, how to bring it about, and the reason it gives per kind.
_CONDITIONS = {
    "stopped": ("stop", None),
    "time_limit": ("clock", None),
    "foreground_unknown": ("foreground", None),
    "focus_unknown": ("focused_window", None),
    "focus_other": ("focused_window", SHADE),
    "keyboard_unknown": ("keyboard_shown", None),
    "keyboard_shown": ("keyboard_shown", True),
    "focus_title": (
        "focused_window",
        FocusedWindow(SHOP, f"{SHOP}/{SHOP}.Other", "a1b2c3"),
    ),
}


_CASES = [
    ("tap", "stopped", "stopped"),
    ("tap", "time_limit", "time_limit"),
    ("tap", "focus_unknown", "focus_unknown"),
    ("tap", "focus_other", "focus_other"),
    ("tap", "keyboard_unknown", "keyboard_unknown"),
    ("tap", "keyboard_shown", "keyboard_shown"),
    ("back", "stopped", "stopped"),
    ("back", "time_limit", "time_limit"),
    ("back", "foreground_unknown", "foreground_unknown"),
    ("back", "focus_unknown", "focus_unknown"),
    ("back", "focus_other", "focus_other"),
    ("back", "keyboard_unknown", "keyboard_unknown"),
    ("back", "keyboard_shown", "changed_before_send"),
    ("back", "focus_title", "changed_before_send"),
    ("relaunch", "focus_title", "changed_before_send"),
    ("relaunch", "stopped", "stopped"),
    ("relaunch", "time_limit", "time_limit"),
    ("relaunch", "foreground_unknown", "foreground_unknown"),
    ("relaunch", "focus_unknown", "focus_unknown"),
    ("relaunch", "focus_other", "changed_before_send"),
]


@pytest.mark.parametrize(("kind", "condition", "reason"), _CASES)
def test_the_gate_sends_nothing_when_a_condition_fails(
    tmp_path, device, android, clock, monkeypatch, kind, condition, reason
):
    stopping = []
    driver, engine, observation = _gate_run(
        tmp_path, device, clock, stop=lambda: bool(stopping)
    )
    action = _make_action(kind, engine, observation)
    inputs, stopped = len(android.inputs), list(android.stopped)
    how, value = _CONDITIONS[condition]
    if how == "stop":
        stopping.append(True)
    elif how == "clock":
        clock.now = driver.deadline
    else:
        monkeypatch.setattr(device, how, lambda: value)
    driver.send(action, observation)
    assert len(android.inputs) == inputs  # no tap, no key
    assert android.stopped == stopped  # no relaunch
    assert _not_sent_reason(tmp_path) == reason


@pytest.mark.parametrize("condition", ["stopped", "time_limit"])
def test_the_gate_holds_back_the_launch_too(
    tmp_path, device, android, clock, condition
):
    device.pin_transport()
    engine = CrawlEngine(SHOP, Policy(), Limits(minutes=1), started_at=clock.now)
    writer = RunWriter.create(
        tmp_path / "run", package=SHOP, limits=Limits(), deny_words=[], serial=SERIAL
    )
    _WRITERS.append(writer)
    driver = crawl_driver._Run(
        device,
        SHOP,
        writer,
        engine,
        Policy(),
        emulator=True,
        app_uid=device.package_uid(SHOP),
        screenshots=False,
        settle=0.0,
        deadline=clock.now + 60,
        stop=lambda: condition == "stopped",
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    if condition == "time_limit":
        clock.now += 60
    driver.send(engine.start(), None)
    assert android.screen.name == "home"  # nothing was launched
    assert _not_sent_reason(tmp_path) == condition


@pytest.mark.parametrize("kind", ["tap", "back", "relaunch"])
def test_the_gate_lets_an_unchanged_action_through(
    tmp_path, device, android, clock, kind
):
    driver, engine, observation = _gate_run(tmp_path, device, clock)
    action = _make_action(kind, engine, observation)
    inputs, stopped = len(android.inputs), len(android.stopped)
    driver.send(action, observation)
    assert len(android.inputs) + len(android.stopped) == inputs + stopped + 1
    assert _not_sent_reason(tmp_path) is None


# --- the device stays the one that was checked ---


def test_a_device_that_takes_over_the_serial_receives_nothing(
    tmp_path, device, android, fake_adb, clock, monkeypatch
):
    def swap(_number: int, *_args: object, **_kwargs: object) -> None:
        # Right after the emulator check: another device (another boot id)
        # takes the serial under a new transport number.
        fake_adb.devices[:] = [
            {"serial": SERIAL, "state": "device", "transport_id": TRANSPORT + 1}
        ]
        android.boot_id = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"

    original = device.is_emulator

    def check_then_swap() -> bool:
        answer = original()
        swap(0)
        return answer

    monkeypatch.setattr(device, "is_emulator", check_then_swap)
    before = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError):
        run(device, clock, tmp_path / "run")
    after = fake_adb.requests[before:]
    selectors = [r for r in after if r.startswith("host:transport")]
    assert selectors
    # The new number was asked only for the identity, which did not match.
    assert set(selectors) <= {
        f"host:transport-id:{TRANSPORT}",
        f"host:transport-id:{TRANSPORT + 1}",
    }
    assert android.inputs == []
    assert android.stopped == []
    assert not [c for _, _, c in fake_adb.commands if c.startswith(("monkey", "input"))]


def test_the_emulator_check_goes_through_the_pinned_transport(
    tmp_path, device, fake_adb, clock
):
    before = len(fake_adb.requests)
    run(device, clock, tmp_path / "run", limits=Limits(actions=1))
    first = [r for r in fake_adb.requests[before:] if r.startswith("host:transport")]
    assert first[0] == f"host:transport-id:{TRANSPORT}"
    assert all(r == f"host:transport-id:{TRANSPORT}" for r in first)


def test_a_real_device_allowed_is_pinned_too(
    tmp_path, device, fake_adb, android, clock
):
    android.props["ro.kernel.qemu"] = "0"
    before = len(fake_adb.requests)
    run(
        device,
        clock,
        tmp_path / "run",
        allow_real_device=True,
        limits=Limits(actions=1),
    )
    selectors = [
        r for r in fake_adb.requests[before:] if r.startswith("host:transport")
    ]
    assert selectors
    assert set(selectors) == {f"host:transport-id:{TRANSPORT}"}


def test_no_transport_id_means_nothing_is_written_or_sent(tmp_path, fake_adb, clock):
    android = FakeAndroid(fake_adb, SERIAL, SCREENS, start="home")  # no id listed
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    with pytest.raises(AdbError, match="no transport id"):
        run(device, clock, out)
    assert not out.exists()
    assert android.inputs == []
    assert fake_adb.commands == []


# --- the rest of the review's cases ---


def test_a_lost_reply_to_a_tap_is_not_resent(tmp_path, device, android, clock):
    android.drop_after_input(1)  # the device takes the tap, the reply is lost
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    assert len([i for i in android.inputs if i.kind == "tap"]) == 1
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    assert [r["action"]["kind"] for r in starts].count("tap") == 1


def test_a_custom_deny_word_is_honoured(tmp_path, device, android, clock):
    out = tmp_path / "run"
    crawl(
        device,
        SHOP,
        out,
        policy=Policy(deny_words=("browse",)),
        limits=Limits(),
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    assert_no_press_inside(android, "browse")
    rows = lines(out / "skipped.jsonl")
    assert any(r.get("element", {}).get("text") == "Browse" for r in rows)


def test_screens_are_counted_when_observed(tmp_path, device, clock):
    screens = tmp_path / "run" / "screens"

    def stop() -> bool:  # as soon as the first screen is written
        return screens.is_dir() and any(screens.glob("*.json"))

    result = run(device, clock, tmp_path / "run", stop=stop)
    assert result.stop_reason == "interrupted"
    assert result.screens == len(list((tmp_path / "run" / "screens").glob("*.json")))
    assert result.screens == 1


def test_the_transition_end_comes_before_a_screenshot_that_fails(
    tmp_path, device, clock, monkeypatch
):
    def fail(*_args: object, **_kwargs: object) -> bytes:
        raise AdbError("the adb server answered FAIL")

    monkeypatch.setattr(device, "screenshot", fail)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out, screenshots=True)
    records = lines(out / "transitions.jsonl")
    assert records[0]["action"] == {"kind": "launch"}
    assert records[1]["action_id"] == 1
    assert "to_screen_id" in records[1]


def test_a_screenshot_of_another_app_is_not_saved(
    tmp_path, device, android, clock, monkeypatch
):
    original = device.screenshot

    def shot_then_cover() -> bytes:
        data = original()
        android.show("ext1")  # another app comes forward as the image is taken
        return data

    monkeypatch.setattr(device, "screenshot", shot_then_cover)
    out = tmp_path / "run"
    run(device, clock, out, screenshots=True, limits=Limits(actions=1))
    assert list((out / "screens").glob("*.png")) == []
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "the app was not in front" in summary


def test_an_interrupt_that_breaks_a_write_is_raised_even_with_stop_set(
    tmp_path, device, clock, monkeypatch
):
    stopping = []
    original = RunWriter.write_transition_start
    count = []

    def interrupted(self, *args, **kwargs):
        count.append(1)
        if len(count) == 2:
            stopping.append(True)
            monkeypatch.setattr(runlog, "_put", _interrupting_put)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RunWriter, "write_transition_start", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, tmp_path / "run", stop=lambda: bool(stopping))
    assert not (tmp_path / "run" / "run.json").exists()


def _interrupting_put(_handle, _data):
    raise KeyboardInterrupt
