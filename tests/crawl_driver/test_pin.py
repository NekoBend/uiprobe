"""Tests for the device behind the pin and the order of checks in the gate."""

from __future__ import annotations

import json

import pytest

from tests.crawl_driver.helpers import (
    BACK_POLICY,
    SHOP,
    STOP_REASONS,
    TRANSPORT,
    _gate_run,
    commands,
    cover_after_browse,
    lines,
    run,
    spy,
    start_requests,
)
from uiprobe import crawl_driver
from uiprobe.crawl import (
    Limits,
)
from uiprobe.device import Device
from uiprobe.errors import (
    AdbError,
    DeviceNotFoundError,
)
from uiprobe.runlog import RunFolderError, RunWriter

# --- round two: the device behind the pin, the order in the gate ---


def _after_first_tap(monkeypatch, device, then):
    original = device.tap

    def tap(point, _send_by=None, _epoch=None):
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        then()
        return result

    monkeypatch.setattr(device, "tap", tap)


@pytest.mark.parametrize("change", ["server_restart", "fingerprint", "not_emulator"])
def test_a_different_device_behind_the_pin_receives_nothing_more(
    tmp_path, device, android, fake_adb, clock, monkeypatch, change
):
    def swap() -> None:
        if change == "server_restart":
            # A restarted server gave the same transport number to another device.
            fake_adb.devices[:] = [
                {"serial": "R58M1234", "state": "device", "transport_id": TRANSPORT}
            ]
        elif change == "fingerprint":
            android.props["ro.build.fingerprint"] = "other/build:36/X/9:user/k"
        else:
            android.props["ro.kernel.qemu"] = "0"

    _after_first_tap(monkeypatch, device, swap)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError) as info:
        run(device, clock, out)
    assert info.value.details["reason"] == "device_changed"
    assert len(android.inputs) == 1  # only the tap before the change
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"


def test_a_real_device_allowed_may_report_qemu_0_but_not_change(
    tmp_path, device, android, clock
):
    android.props["ro.kernel.qemu"] = "0"
    result = run(device, clock, tmp_path / "run", allow_real_device=True)
    assert result.stop_reason in STOP_REASONS


def test_nothing_runs_between_the_last_read_and_the_tap(
    tmp_path, device, clock, monkeypatch
):
    events: list[str] = []
    for name in ("dump", "foreground", "focused_window", "keyboard_shown", "tap"):
        original = getattr(device, name)

        def logged(*args, _name=name, _original=original, **kwargs):
            events.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(device, name, logged)

    def stop() -> bool:
        events.append("stop")
        return False

    run(device, clock, tmp_path / "run", stop=stop)
    taps = [i for i, e in enumerate(events) if e == "tap"]
    assert taps
    for index in taps:
        # dump, foreground, focused window, keyboard: then the tap, at once.
        assert events[index - 4 : index] == [
            "dump",
            "foreground",
            "focused_window",
            "keyboard_shown",
        ]


def test_a_stop_that_takes_long_cannot_slip_a_dialog_under_the_tap(
    tmp_path, device, android, clock
):
    covered = []

    def slow_stop() -> bool:
        # While the user's stop() "runs", a dialog comes up once a tap is due.
        if android.inputs and not covered:
            covered.append(True)
            android.show("cart")
        return False

    run(device, clock, tmp_path / "run", stop=slow_stop)
    assert covered
    assert not [i for i in android.inputs if i.kind == "tap" and i.screen == "cart"]


def test_a_stop_seen_once_is_remembered(tmp_path, device, android, clock):
    answers = iter([False, False, True])

    def flaky_stop() -> bool:
        return next(answers, False)

    result = run(device, clock, tmp_path / "run", stop=flaky_stop)
    assert result.stop_reason == "interrupted"
    assert android.inputs == []  # the launch went out, nothing was pressed


def test_a_stop_between_force_stop_and_start_sends_no_start(
    tmp_path, device, android, monkeypatch, clock
):
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
    stopping = []
    stop_app = device.stop_app

    def stop_then_ask(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        stop_app(package, _send_by=_send_by, _epoch=_epoch)
        stopping.append(True)

    monkeypatch.setattr(device, "stop_app", stop_then_ask)
    launches = spy(monkeypatch, device, "_launch_component", lambda *_a, **_k: None)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, stop=lambda: bool(stopping), restart=False)
    assert result.stop_reason == "interrupted"
    assert android.stopped == [SHOP]
    assert len(launches) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    relaunch = [r for r in records if r.get("action", {}).get("kind") == "relaunch"]
    end = [r for r in records if r.get("action_id") == relaunch[0]["action_id"]][-1]
    assert (end["not_sent"], end["reason"]) == (True, "start_not_sent")
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again" in summary


def test_a_back_that_is_not_sent_ends_the_run(
    tmp_path, device, android, clock, monkeypatch
):
    original_launch = device._launch_component

    def launch_into_another_app(*args, **kwargs):
        original_launch(*args, **kwargs)
        android.show("ext1")  # the engine will want BACK

    monkeypatch.setattr(device, "_launch_component", launch_into_another_app)
    monkeypatch.setattr(device, "keyboard_shown", lambda: None)
    out = tmp_path / "run"
    result = run(device, clock, out, policy=BACK_POLICY)
    assert result.stop_reason == "not_sent"
    assert not [i for i in android.inputs if i.kind == "key"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "back was not sent: keyboard_unknown" in summary
    assert android.stopped == [SHOP]  # the first launch's; no relaunch followed


def test_a_clock_that_does_not_move_cannot_hold_the_pause(tmp_path, device, clock):
    driver, _, _ = _gate_run(tmp_path, device, clock)
    driver.settle = 1.0
    sleeps = []
    driver.sleep = sleeps.append  # the clock never moves
    driver.pause()
    assert 0 < len(sleeps) <= int(1.0 / crawl_driver.PAUSE_STEP) + 2


# --- round three ---


def test_a_window_that_takes_focus_during_the_dump_is_not_pressed(
    tmp_path, device, android, clock, monkeypatch
):
    out = tmp_path / "run"
    original_dump = device.dump
    popped = []

    def in_a_tap_gate() -> bool:
        # One more tap start line than taps sent: the gate is reading for it.
        path = out / "transitions.jsonl"
        records = lines(path) if path.exists() else []
        started = sum(r.get("action", {}).get("kind") == "tap" for r in records)
        sent = sum(i.kind == "tap" for i in android.inputs)
        return (
            started == sent + 1 and records[-1].get("action", {}).get("kind") == "tap"
        )

    def dump_then_popup(*args, **kwargs):
        gate = in_a_tap_gate()
        screen = original_dump(*args, **kwargs)
        if gate and android.inputs and not popped:  # once, after a press
            popped.append(True)
            android.focus_window = ("PopupWindow:9f8e7d", SHOP)  # the app's own
        return screen

    monkeypatch.setattr(device, "dump", dump_then_popup)
    run(device, clock, out)
    assert popped
    ends = [r for r in lines(out / "transitions.jsonl") if r.get("not_sent")]
    assert ends
    assert ends[0]["reason"] == "focus_changed"


def test_a_device_without_a_fingerprint_is_not_started(
    tmp_path, device, android, clock
):
    android.props["ro.build.fingerprint"] = ""
    out = tmp_path / "run"
    with pytest.raises(AdbError, match="fingerprint"):
        run(device, clock, out)
    assert not out.exists()
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_a_transport_number_taken_before_the_run_is_refused(
    tmp_path, device, fake_adb, clock
):
    # Between making the Device and the crawl, a restarted server gave the
    # number to another device: nothing at all goes to that transport.
    fake_adb.devices[:] = [
        {"serial": "R58M1234", "state": "device", "transport_id": TRANSPORT}
    ]
    before = len(fake_adb.requests)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError):
        run(device, clock, out)
    assert not [r for r in fake_adb.requests[before:] if r.startswith("host:transport")]
    assert not out.exists()


def test_every_device_request_checks_the_listing_first(
    tmp_path, device, fake_adb, clock
):
    run(device, clock, tmp_path / "run", limits=Limits(actions=2))
    requests = fake_adb.requests
    for index, request in enumerate(requests):
        if request.startswith("host:transport-id:"):
            assert requests[index - 1] == "host:devices-l"


def test_a_stop_refusal_ends_the_run_without_another_read(
    tmp_path, device, android, clock
):
    def stop() -> bool:
        return bool(android.inputs)  # true from the first press on

    run(device, clock, tmp_path / "run", stop=stop)
    records = lines(tmp_path / "run" / "transitions.jsonl")
    assert records[-1].get("reason") in (None, "stopped")
    # The run ended right after the refusal or the press: no dump followed a
    # refused action.
    last_start = max(i for i, r in enumerate(records) if "action" in r)
    assert all("action" not in r for r in records[last_start + 1 :])


# --- the two last gaps ---


@pytest.mark.parametrize("restart", [False, True], ids=["relaunch", "first-launch"])
def test_a_changed_identity_after_the_force_stop_sends_no_start(
    tmp_path, device, android, monkeypatch, clock, restart
):
    # The identity changes right after the force-stop (of a relaunch, or of
    # the first launch with restart): the pin check before the start raises
    # device_changed. The start was not sent, so the action is closed as
    # start_not_sent (not failed, not left open), then the error is raised.
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
    stop_app = device.stop_app

    def stop_then_swap(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        stop_app(package, _send_by=_send_by, _epoch=_epoch)
        android.props["ro.build.fingerprint"] = "other/build:36/X/9:user/k"

    monkeypatch.setattr(device, "stop_app", stop_then_swap)
    launches = spy(monkeypatch, device, "_launch_component", lambda *_a, **_k: None)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError) as info:
        # restart=False: android.stopped then counts relaunches only.
        run(device, clock, out, restart=restart)
    assert info.value.details["reason"] == "device_changed"
    assert android.stopped == [SHOP]
    # No start after the force-stop (without restart, the first launch's).
    assert len(launches) == (0 if restart else 1)
    records = lines(out / "transitions.jsonl")
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if restart else "relaunch")
    assert records[-1] == {
        "schema": "uiprobe.transition_end/1",
        "recorded_at": records[-1]["recorded_at"],
        "action_id": started[-1]["action_id"],
        "not_sent": True,
        "reason": "start_not_sent",
    }
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    what = (
        "at the start of the run but not started"
        if restart
        else "but not started again"
    )
    assert f"{SHOP} was force-stopped {what} (device_changed)" in summary
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "error"


@pytest.mark.parametrize("restart", [False, True], ids=["relaunch", "first-launch"])
def test_an_interrupt_after_the_force_stop_closes_the_start_as_not_sent(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # Ctrl+C arrives in the pin check between the force-stop and the start:
    # the start was not sent, the action is closed as start_not_sent, then
    # the interrupt is raised again (stop() does not ask to return).
    # Without restart the first force-stop is the relaunch's (the cover
    # brings it); with restart it is the first launch's.
    cover_after_browse(monkeypatch, device, android)
    original = Device.check_pin
    interrupted: list[bool] = []

    def check_then_interrupt(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if android.stopped and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt

    monkeypatch.setattr(Device, "check_pin", check_then_interrupt)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out, restart=restart)
    assert interrupted
    assert len(start_requests(fake_adb)) == (0 if restart else 1)
    records = lines(out / "transitions.jsonl")
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if restart else "relaunch")
    assert (records[-1]["action_id"], records[-1]["reason"]) == (
        started[-1]["action_id"],
        "start_not_sent",
    )
    assert records[-1]["not_sent"] is True
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped" in summary
    assert "(interrupted)" in summary
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "interrupted"


@pytest.mark.parametrize("restart", [False, True], ids=["no-restart", "restart"])
def test_a_failed_write_of_the_not_sent_line_keeps_the_read_error(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # The adb connection is lost in the first launcher read, and the end line
    # that closes the launch cannot be written: the error raised is still the
    # read's, not the run folder's.
    def lost(package: str) -> str:
        del package
        raise AdbError("connection closed", details={})

    original = RunWriter.write_transition_end

    def refuse_not_sent(self, *args, **kwargs):
        if kwargs.get("not_sent") is not None:
            raise RunFolderError("disk full", details={"path": "x"})
        return original(self, *args, **kwargs)

    monkeypatch.setattr(device, "_start_component", lost)
    monkeypatch.setattr(RunWriter, "write_transition_end", refuse_not_sent)
    out = tmp_path / "run"
    with pytest.raises(AdbError, match="connection closed"):
        run(device, clock, out, restart=restart)
    assert not [c for c in commands(fake_adb) if c.startswith(("am ", "input "))]
    assert android.stopped == []
    assert len(lines(out / "transitions.jsonl")) == 1  # the end line failed
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "error"


@pytest.mark.parametrize("restart", [False, True], ids=["relaunch", "first-launch"])
def test_an_interrupt_in_the_last_comparison_before_the_start_closes_it(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # Ctrl+C arrives in the last comparison of the pin before the start,
    # after the force-stop went out: the start is not sent, the action is
    # closed as start_not_sent with the note, and the interrupt is raised.
    cover_after_browse(monkeypatch, device, android)
    final_check = crawl_driver._Run.final_check
    interrupted: list[bool] = []

    def interrupt_at_start(self, path: str, approved: int) -> bool:
        if path == "start" and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return final_check(self, path, approved)

    monkeypatch.setattr(crawl_driver._Run, "final_check", interrupt_at_start)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out, restart=restart)
    assert interrupted
    assert android.stopped == [SHOP]
    assert len(start_requests(fake_adb)) == (0 if restart else 1)
    records = lines(out / "transitions.jsonl")
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if restart else "relaunch")
    assert records[-1] == {
        "schema": "uiprobe.transition_end/1",
        "recorded_at": records[-1]["recorded_at"],
        "action_id": started[-1]["action_id"],
        "not_sent": True,
        "reason": "start_not_sent",
    }
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    what = (
        "at the start of the run but not started"
        if restart
        else "but not started again"
    )
    assert f"{SHOP} was force-stopped {what} (interrupted)" in summary
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "interrupted"


@pytest.mark.parametrize("restart", [False, True], ids=["no-restart", "restart"])
def test_a_lost_connection_in_the_first_launcher_read_closes_the_launch(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # The adb connection is lost in the first launcher read, before anything
    # was sent: the launch is closed as not sent (adb_error), then the error
    # is raised.
    def lost(package: str) -> str:
        del package
        raise AdbError("connection closed", details={})

    monkeypatch.setattr(device, "_start_component", lost)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out, restart=restart)
    assert not [c for c in commands(fake_adb) if c.startswith(("am ", "input "))]
    assert android.stopped == []
    records = lines(out / "transitions.jsonl")
    assert len(records) == 2
    assert records[1] == {
        "schema": "uiprobe.transition_end/1",
        "recorded_at": records[1]["recorded_at"],
        "action_id": 1,
        "not_sent": True,
        "reason": "adb_error",
    }
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "error"


@pytest.mark.parametrize("kind", ["tap", "back"])
def test_a_stop_seen_in_the_pause_sends_no_further_request(
    tmp_path, device, android, fake_adb, clock, monkeypatch, kind
):
    stopping = []
    marks: list[int] = []
    if kind == "back":
        original_launch = device._launch_component

        def launch_into_another_app(*args, **kwargs):
            original_launch(*args, **kwargs)
            android.show("ext1")  # the engine will press BACK

        monkeypatch.setattr(device, "_launch_component", launch_into_another_app)
    sender = device.tap if kind == "tap" else device.key

    def send_then_stop(*args, **kwargs):
        result = sender(*args, **kwargs)
        stopping.append(True)  # stop() turns True during the pause after it
        marks.append(len(fake_adb.requests))
        return result

    monkeypatch.setattr(device, kind if kind == "tap" else "key", send_then_stop)
    out = tmp_path / "run"
    result = run(
        device,
        clock,
        out,
        stop=lambda: bool(stopping),
        settle=1.0,
        policy=BACK_POLICY if kind == "back" else None,
    )
    assert result.stop_reason == "interrupted"
    assert fake_adb.requests[marks[0] :] == []  # not one request after it
    records = lines(out / "transitions.jsonl")
    assert "action" in records[-1]  # its start line has no end line
