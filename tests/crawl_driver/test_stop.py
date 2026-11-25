"""Tests for stop requests and the time limit right before a request is sent."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

from uiprobe import adb as adb_module
from uiprobe import crawl_driver
from uiprobe.crawl import (
    Limits,
    Tap,
)
from uiprobe.crawl_driver import crawl
from uiprobe.device import Device
from uiprobe.device import pin as device_pin
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_driver.helpers import (
    SERIAL,
    SHOP,
    cover_after_browse,
    lines,
    run,
    share_clock,
    spy,
    start_requests,
)

# --- stopping ---


def test_stop_between_actions_ends_with_interrupted(tmp_path, device, android, clock):
    def stop() -> bool:
        return len(android.inputs) >= 2  # after the second press

    out = tmp_path / "run"
    result = run(device, clock, out, stop=stop)
    assert result.stop_reason == "interrupted"
    transitions = lines(out / "transitions.jsonl")
    starts = [r for r in transitions if "action" in r]
    assert result.actions == len(starts) == 3  # launch and two presses
    # stop() turned True in the pause after the second press: the run ended
    # there, so that press has no end line (its result is not read).
    assert len(transitions) == 5
    assert "action" in transitions[-1]
    assert len(android.inputs) == 2
    assert (
        json.loads((out / "run.json").read_text(encoding="utf-8"))["stop_reason"]
        == "interrupted"
    )


def test_stop_before_the_first_action_sends_nothing(tmp_path, device, android, clock):
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: True)
    assert result.stop_reason == "interrupted"
    assert (result.screens, result.actions) == (0, 0)
    assert android.inputs == []
    assert android.stopped == []
    assert not (out / "transitions.jsonl").exists()


def interrupt_at_tap(
    monkeypatch: pytest.MonkeyPatch,
    device: Device,
    number: int,
    flag: list[bool],
) -> None:
    def before(call: int, *_args: object, **_kwargs: object) -> None:
        if call == number:
            flag.append(True)
            raise KeyboardInterrupt

    spy(monkeypatch, device, "tap", before)


def test_an_interrupt_is_raised_again_without_a_stop(
    tmp_path, device, android, monkeypatch, clock
):
    out = tmp_path / "run"
    interrupt_at_tap(monkeypatch, device, 2, [])
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out)
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "interrupted"
    transitions = lines(out / "transitions.jsonl")
    assert transitions[-1]["schema"] == "uiprobe.transition_start/1"
    assert run_json["actions"] == transitions[-1]["action_id"]
    assert len([i for i in android.inputs if i.kind == "tap"]) == 1


def test_an_interrupt_with_stop_set_returns(tmp_path, device, monkeypatch, clock):
    out = tmp_path / "run"
    flag: list[bool] = []
    interrupt_at_tap(monkeypatch, device, 2, flag)
    result = run(device, clock, out, stop=lambda: bool(flag))
    assert result.stop_reason == "interrupted"
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "interrupted"
    assert run_json["actions"] == result.actions
    assert (out / "summary.txt").exists()


def test_an_interrupt_with_a_stop_that_says_no_is_raised(
    tmp_path, device, monkeypatch, clock
):
    out = tmp_path / "run"
    interrupt_at_tap(monkeypatch, device, 1, [])
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out, stop=lambda: False)
    assert (out / "run.json").exists()


# --- a start that went out is recorded as sent ---


def _ends_of(records: list[dict], kind: str) -> list[dict]:
    """The end lines of the last action of `kind`."""
    start = [r for r in records if r.get("action", {}).get("kind") == kind][-1]
    return [
        r for r in records if r["action_id"] == start["action_id"] and "action" not in r
    ]


@pytest.mark.parametrize("end", ["time_limit", "stop"])
def test_a_first_start_that_went_out_is_not_recorded_as_unsent(
    tmp_path, device, android, monkeypatch, clock, end
):
    # am start was sent and its reply came; the time limit (or a stop) comes
    # while the launch waits for the app in front. The launch stays a sent
    # action (no not_sent end line); the run ends at the next step.
    share_clock(monkeypatch, clock)
    asked: list[bool] = []

    def start_then_end(command: str):
        reply = android._shell(command)
        if end == "time_limit":
            clock.now += 3600.0
        else:
            asked.append(True)
        return reply

    android.override(re.compile(r"^am start"), start_then_end)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(minutes=1), stop=lambda: bool(asked))
    expected = "max_duration" if end == "time_limit" else "interrupted"
    assert result.stop_reason == expected
    ends = _ends_of(lines(out / "transitions.jsonl"), "launch")
    assert not [e for e in ends if e.get("not_sent")]


@pytest.mark.parametrize("end", ["time_limit", "stop"])
def test_a_relaunch_start_that_went_out_is_not_recorded_as_unsent(
    tmp_path, device, android, monkeypatch, clock, end
):
    share_clock(monkeypatch, clock)
    cover_after_browse(monkeypatch, device, android)
    asked: list[bool] = []
    starts: list[str] = []

    def start_then_end(command: str):
        starts.append(command)
        reply = android._shell(command)
        if len(starts) == 2:  # the relaunch's start went out and was answered
            if end == "time_limit":
                clock.now += 3600.0
            else:
                asked.append(True)
        return reply

    android.override(re.compile(r"^am start"), start_then_end)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(
        device,
        clock,
        out,
        limits=Limits(minutes=1),
        stop=lambda: bool(asked),
        restart=False,
    )
    assert len(starts) == 2
    expected = "max_duration" if end == "time_limit" else "interrupted"
    assert result.stop_reason == expected
    ends = _ends_of(lines(out / "transitions.jsonl"), "relaunch")
    assert not [e for e in ends if e.get("not_sent")]  # no start_not_sent either
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "not started again" not in summary


# --- stop and the time limit right before a request ---


def test_a_stop_asked_during_the_relaunch_pin_check_sends_no_start(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    cover_after_browse(monkeypatch, device, android)
    asked: list[bool] = []
    original = Device.check_pin

    def check_then_stop(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if android.stopped:  # the relaunch's check, after its force-stop
            asked.append(True)

    monkeypatch.setattr(Device, "check_pin", check_then_stop)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, stop=lambda: bool(asked), restart=False)
    assert result.stop_reason == "interrupted"
    assert android.stopped == [SHOP]
    assert len(start_requests(fake_adb)) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    assert records[-1]["reason"] == "start_not_sent"
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again (stopped)" in summary


def test_a_stop_asked_during_the_gate_pin_check_sends_no_tap(
    tmp_path, device, android, monkeypatch, clock
):
    asked: list[bool] = []
    in_gate: list[bool] = []
    gate = crawl_driver._Run.gate

    def marked_gate(self, action, observation):
        in_gate.append(isinstance(action, Tap))
        try:
            return gate(self, action, observation)
        finally:
            in_gate.pop()

    original = Device.check_pin

    def check_then_stop(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if in_gate and in_gate[-1]:
            asked.append(True)

    monkeypatch.setattr(crawl_driver._Run, "gate", marked_gate)
    monkeypatch.setattr(Device, "check_pin", check_then_stop)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: bool(asked))
    assert asked
    assert result.stop_reason == "interrupted"
    assert android.inputs == []
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "stopped")


def test_a_start_is_not_sent_once_the_time_limit_passed_during_the_listing(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    share_clock(monkeypatch, clock)
    original = Device._check_listed
    crossed: list[bool] = []

    def listed_then_late(self: Device, *, strict: bool = False) -> None:
        original(self, strict=strict)
        if strict and not crossed:  # the first request that changes the device
            crossed.append(True)
            clock.now += 3600.0  # the listing took long

    monkeypatch.setattr(Device, "_check_listed", listed_then_late)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert crossed
    assert result.stop_reason == "max_duration"
    assert start_requests(fake_adb) == []
    assert android.screen.name == "home"
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "time_limit")


@pytest.mark.usefixtures("android")
def test_a_start_is_not_sent_once_the_time_limit_passed_selecting_the_transport(
    tmp_path, device, monkeypatch, clock, fake_adb
):
    # The adb server took long to acknowledge host:transport-id:N for the
    # start; the check on that connection, right before the service request,
    # holds it back.
    share_clock(monkeypatch, clock)
    original_listed = Device._check_listed
    original_send = adb_module._send_request
    armed: list[bool] = []

    def listed(self: Device, *, strict: bool = False) -> None:
        original_listed(self, strict=strict)
        if strict:
            armed.append(True)

    def send_request(sock, request: str):
        message = original_send(sock, request)
        if armed and request.startswith("host:transport-id:"):
            armed.clear()
            clock.now += 3600.0  # the acknowledgement came late
        return message

    monkeypatch.setattr(Device, "_check_listed", listed)
    monkeypatch.setattr(adb_module, "_send_request", send_request)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "max_duration"
    assert start_requests(fake_adb) == []
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "time_limit")


def test_a_relaunch_start_is_not_sent_once_the_time_limit_passed(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    share_clock(monkeypatch, clock)
    cover_after_browse(monkeypatch, device, android)
    original = Device._check_listed

    def listed_then_late(self: Device, *, strict: bool = False) -> None:
        original(self, strict=strict)
        if strict and android.stopped:  # the start after the force-stop
            clock.now += 3600.0

    monkeypatch.setattr(Device, "_check_listed", listed_then_late)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, restart=False)
    assert result.stop_reason == "max_duration"
    assert android.stopped == [SHOP]
    assert len(start_requests(fake_adb)) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    assert records[-1]["reason"] == "start_not_sent"
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again (time_limit)" in summary


def test_a_tap_whose_reads_end_past_the_limit_is_not_sent(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # Each dump takes 19 s on the injected clock alone (uiprobe's own clock
    # does not move): the gate's own dump before the second tap crosses the
    # 1-minute limit, and the gate reads the clock after its reads, before
    # its last comparison, so that tap must not go out (as on main).
    original = Device.dump

    def slow_dump(self: Device, **kwargs: object) -> object:
        clock.now += 19.0
        return original(self, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Device, "dump", slow_dump)
    out = tmp_path / "run"
    started = clock.now
    result = run(device, clock, out, limits=Limits(minutes=1))
    assert result.stop_reason == "max_duration"
    taps = [i for i in android.inputs if i.kind == "tap"]
    sent_at = [
        r for r in lines(out / "transitions.jsonl") if r.get("reason") == "time_limit"
    ]
    assert sent_at, "the late tap is closed as not sent"
    assert clock.now - started >= 60
    # Every tap that went out was decided and checked within the limit: the
    # first one (at 38 s), not the second (its reads end at 77 s).
    assert len(taps) == 1
    assert dict(result.not_sent).get("time_limit") == 1


def _starts(fake_adb: FakeAdbServer) -> list[str]:
    return [r for r in fake_adb.requests if r.startswith("shell:am start ")]


def test_a_launch_whose_pin_check_ends_past_the_limit_is_not_sent(
    tmp_path: Path, device, android, clock, monkeypatch, fake_adb
):
    assert android.serial == SERIAL
    original = Device.check_pin
    checks = [0]

    def slow_check(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        checks[0] += 1
        if checks[0] == 1:
            clock.now += 61.0  # the first launch's pin check crosses the limit

    monkeypatch.setattr(Device, "check_pin", slow_check)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(minutes=1))
    assert result.stop_reason == "max_duration"
    assert _starts(fake_adb) == []
    assert dict(result.not_sent) == {"time_limit": 1}


def test_a_restart_whose_pin_check_ends_past_the_limit_is_only_half_sent(
    tmp_path: Path, device, android, clock, monkeypatch, fake_adb
):
    original_tap = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original_tap(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    original_check = Device.check_pin

    def slow_after_stop(self: Device, **kwargs: object) -> None:
        original_check(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if android.stopped:
            clock.now += 3600.0  # the restart's pin check crosses the limit

    monkeypatch.setattr(device, "tap", tap_then_cover)
    monkeypatch.setattr(Device, "check_pin", slow_after_stop)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, restart=False)
    assert android.stopped == [SHOP]
    assert len(_starts(fake_adb)) == 1  # the first launch only
    assert result.stop_reason == "max_duration"
    ends = [r for r in lines(out / "transitions.jsonl") if r.get("not_sent")]
    assert ends[-1]["reason"] == "start_not_sent"


@pytest.mark.usefixtures("android")
def test_nothing_is_hashed_between_the_last_time_check_and_a_tap(
    tmp_path: Path, device, clock, monkeypatch
):
    # The screen id walks the whole screen; it must be computed before the
    # last look at the time, which is the device's own, on the tap's
    # connection right before the request is written.
    from uiprobe import model

    events: list[str] = []
    original_id = model.Screen.screen_id
    original_check = device_pin._monotonic
    original_send = adb_module._send_request

    def recording_id(self: model.Screen) -> str:
        events.append("hash")
        return original_id.fget(self)

    def recording_check() -> float:
        events.append("check")
        return original_check()

    def recording_send(sock, request: str):
        if request.startswith("shell:input swipe"):
            events.append("tap")
        return original_send(sock, request)

    monkeypatch.setattr(model.Screen, "screen_id", property(recording_id))
    monkeypatch.setattr(device_pin, "_monotonic", recording_check)
    monkeypatch.setattr(adb_module, "_send_request", recording_send)
    out = tmp_path / "run"
    crawl(
        device,
        SHOP,
        out,
        policy=Policy(),
        limits=Limits(actions=4),
        settle=1.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    taps = [i for i, e in enumerate(events) if e == "tap"]
    assert taps
    for index in taps:
        assert events[index - 1] == "check"  # the time, then at once the tap
