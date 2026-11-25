"""Tests for the waits around the first launch's force-stop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.crawl_driver.helpers import (
    SHOP,
    TRANSPORT,
    Clock,
    lines,
    run,
)
from uiprobe import crawl_driver

if TYPE_CHECKING:
    from pathlib import Path


from tests.repin.helpers import (
    Waits,
    _covered_after_launch,
    listing,
    pinned,
    renumber,
)


def test_the_time_limit_inside_the_wait_after_force_stop_is_recorded(
    tmp_path: Path, fake_adb, android, monkeypatch
):
    # The device drops out right after the force-stop and the time limit
    # passes while it is waited for before the start.
    device = pinned(fake_adb)
    _covered_after_launch(monkeypatch, device, android)
    dropped: list[bool] = []
    original_stop = device.stop_app

    def stop_then_drop(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        original_stop(package, _send_by=_send_by, _epoch=_epoch)
        listing(fake_adb)
        dropped.append(True)

    class LateClock(Clock):
        def monotonic(self) -> float:
            # past the limit only once the wait has begun, not in halt()
            if dropped and device._repinning:
                return self.now + 3600
            return self.now

    monkeypatch.setattr(device, "stop_app", stop_then_drop)
    out = tmp_path / "run"
    # restart=False: the first force-stop is the relaunch's.
    result = run(device, LateClock(), out, restart=False)
    assert result.stop_reason == "max_duration"
    assert android.stopped == [SHOP]
    starts = [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    assert len(starts) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["start_not_sent"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again (time_limit)" in summary


def test_a_device_proven_again_after_force_stop_gets_no_start(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The device comes back on a new number right after the force-stop and
    # is proven the same; the start is still not sent on the old decision.
    device = pinned(fake_adb)
    _covered_after_launch(monkeypatch, device, android)
    original_stop = device.stop_app

    def stop_then_renumber(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        original_stop(package, _send_by=_send_by, _epoch=_epoch)
        listing(fake_adb, (TRANSPORT + 1, "device"))

    monkeypatch.setattr(device, "stop_app", stop_then_renumber)
    out = tmp_path / "run"
    # restart=False: the first force-stop is the relaunch's.
    result = run(device, clock, out, restart=False)
    assert result.stop_reason == "not_sent"
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    starts = [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    assert len(starts) == 1
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["start_not_sent"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "not started again (device_reconnected)" in summary


def test_a_back_chosen_before_the_device_was_away_is_not_sent(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # On the detail screen the engine chooses a navigation BACK; the device
    # drops out once, during that BACK's gate (its keyboard read), and comes
    # back on its number. The BACK is not sent: the engine takes it back and,
    # on a fresh read, chooses it again, and then it is sent.
    device = pinned(fake_adb)
    original = device.keyboard_shown
    reads = {"detail": 0}

    def away_once() -> object:
        if android.screen.name == "detail":
            reads["detail"] += 1
            if reads["detail"] == 2:  # 1: the observation; 2: the BACK's gate
                Waits(device, [renumber(fake_adb, TRANSPORT)])
                listing(fake_adb, (TRANSPORT, "offline"))
        return original()

    monkeypatch.setattr(device, "keyboard_shown", away_once)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "exhausted"  # the crawl went on to its end
    assert device.reconnects == 1  # the wait happened
    records = lines(out / "transitions.jsonl")
    refused = [r for r in records if r.get("not_sent")]
    assert [r["reason"] for r in refused] == ["device_reconnected"]
    keys = [i for i in android.inputs if i.kind == "key"]
    assert any(i.screen == "detail" for i in keys)  # the BACK, sent after proof


# --- the first launch's force-stop (restart, the default) ---


def test_a_pin_moved_before_the_first_force_stop_sends_nothing(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The device comes back on a new number right before the force-stop goes
    # out: the device refuses the request on the epoch the gate approved.
    device = pinned(fake_adb)
    original = device.stop_app

    def renumber_then_stop(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        listing(fake_adb, (TRANSPORT + 1, "device"))
        original(package, _send_by=_send_by, _epoch=_epoch)

    monkeypatch.setattr(device, "stop_app", renumber_then_stop)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "not_sent"
    assert result.actions == 1
    assert android.stopped == []
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am ")]
    records = lines(out / "transitions.jsonl")
    assert records[0]["restart"] is True
    assert [r["reason"] for r in records if r.get("not_sent")] == ["device_reconnected"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "launch was not sent: device_reconnected" in summary
    assert "force-stopped" not in summary


def test_a_pin_moved_in_the_first_force_stop_gate_fails_its_last_comparison(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # stop() proves the device again on a new number after the gate's pin
    # check: the last comparison before the force-stop fails, nothing is sent.
    device = pinned(fake_adb)
    in_gate: list[bool] = []
    gate = crawl_driver._Run.restart_gate

    def marked_gate(self: crawl_driver._Run) -> str | None:
        in_gate.append(True)
        return gate(self)

    moved: list[bool] = []

    def stop() -> bool:
        if in_gate and not moved:
            moved.append(True)
            listing(fake_adb, (TRANSPORT + 1, "device"))
            device.foreground()  # proves the device again on the new number
        return False

    monkeypatch.setattr(crawl_driver._Run, "restart_gate", marked_gate)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=stop)
    assert moved
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    assert result.stop_reason == "not_sent"
    assert android.stopped == []
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am ")]
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["device_reconnected"]


def test_a_pin_moved_after_the_first_force_stop_holds_back_the_start(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The device comes back on a new number right after the force-stop and
    # is proven the same; the start is not sent on the earlier decision.
    device = pinned(fake_adb)
    original_stop = device.stop_app

    def stop_then_renumber(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        original_stop(package, _send_by=_send_by, _epoch=_epoch)
        listing(fake_adb, (TRANSPORT + 1, "device"))

    monkeypatch.setattr(device, "stop_app", stop_then_renumber)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "not_sent"
    assert result.actions == 1
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    assert android.stopped == [SHOP]
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["start_not_sent"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert (
        f"{SHOP} was force-stopped at the start of the run but not started"
        " (device_reconnected)" in summary
    )
