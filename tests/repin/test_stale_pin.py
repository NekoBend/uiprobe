"""Tests for an input that meets a stale pin and for failed identity reads."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import (
    SHOP,
    TRANSPORT,
    Clock,
    lines,
    run,
)
from tests.fake_adb_server import Reset
from uiprobe import crawl_driver
from uiprobe.crawl import Limits
from uiprobe.crawl_driver import driver as driver_module
from uiprobe.device import (
    IDENTITY_COMMAND,
    MAX_RECONNECTS,
    PIN_NOT_PROVEN,
    REPIN_POLL,
    Device,
)
from uiprobe.errors import (
    AdbError,
    DeviceNotFoundError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer, Reply
    from tests.fake_device import FakeAndroid

from tests.repin.helpers import (
    OTHER_BOOT,
    Waits,
    _covered_after_launch,
    _renumber_after,
    listing,
    pinned,
    renumber,
    through,
)

# --- an input that meets a stale pin, and stop() that reads the device ---


def test_an_input_on_a_stale_pin_sends_nothing_and_keeps_the_pin(fake_adb, android):
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))  # renumbered, not proven yet
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError) as caught:
        device.key("back")
    assert caught.value.details["reason"] == PIN_NOT_PROVEN
    assert android.inputs == []
    assert through(fake_adb, TRANSPORT + 1, since) == []
    assert device._broken is None  # the pin is kept
    # the next read proves the device on the new number; inputs go again
    since = len(fake_adb.requests)
    device.foreground()
    assert through(fake_adb, TRANSPORT + 1, since)[0] == f"shell:{IDENTITY_COMMAND}"
    device.key("back")
    assert [i.kind for i in android.inputs] == ["key"]


def test_a_tap_that_meets_a_renumbered_pin_is_not_sent_and_the_crawl_goes_on(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    device = pinned(fake_adb)
    original = device.tap
    moved: list[bool] = []

    def renumber_then_tap(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        if not moved:
            moved.append(True)
            listing(fake_adb, (TRANSPORT + 1, "device"))  # after the gate
        return original(point, _send_by=_send_by, _epoch=_epoch)

    monkeypatch.setattr(device, "tap", renumber_then_tap)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=6))
    assert result.stop_reason == "max_actions"
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["device_reconnected"]
    refused = next(r for r in records if "action" in r and r["action"]["kind"] == "tap")
    taps = [i for i in android.inputs if i.kind == "tap"]
    # the same element was tapped after the proof: the engine took it back
    assert taps[0].point == tuple(refused["action"]["point"])
    skipped = lines(out / "skipped.jsonl") if (out / "skipped.jsonl").exists() else []
    assert not [r for r in skipped if "tap_not_sent" in r["reasons"]]


def test_a_back_that_meets_a_renumbered_pin_is_not_sent(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    device = pinned(fake_adb)
    original = device.key
    moved: list[bool] = []

    def renumber_then_key(name: str, **kwargs: object) -> str:
        if not moved:
            moved.append(True)
            listing(fake_adb, (TRANSPORT + 1, "device"))
        return original(name, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(device, "key", renumber_then_key)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "exhausted"  # taken back, chosen again, sent
    assert [i for i in android.inputs if i.kind == "key"]
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["device_reconnected"]


@pytest.mark.usefixtures("android")
def test_a_stop_that_reads_the_device_does_not_break_a_wait(
    tmp_path: Path, fake_adb, clock, monkeypatch
):
    # stop() reads the foreground (as a caller naturally writes it); it is
    # not called while the device is waited for, so the wait succeeds.
    device = pinned(fake_adb)
    _renumber_after(monkeypatch, fake_adb, device, "foreground", 2, TRANSPORT + 1)

    def stop() -> bool:
        return device.foreground() is None

    out = tmp_path / "run"
    result = run(device, clock, out, stop=stop, limits=Limits(actions=6))
    assert result.stop_reason == "max_actions"
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    assert device._broken is None


@pytest.mark.usefixtures("android")
def test_typing_on_a_stale_pin_says_the_piece_was_not_sent(fake_adb, android):
    device = pinned(fake_adb)
    listing(fake_adb, (TRANSPORT + 1, "device"))
    with pytest.raises(DeviceNotFoundError) as caught:
        device.type_text("abc")
    assert caught.value.details["reason"] == PIN_NOT_PROVEN
    assert caught.value.details["pieces_done"] == 0
    assert "piece 1 was not sent" in str(caught.value)
    assert "abc" not in str(caught.value)
    assert android.inputs == []


def test_a_restart_launch_on_a_stale_pin_says_it_force_stopped(
    fake_adb, android, monkeypatch
):
    device = pinned(fake_adb)
    original = device._force_stop

    def stop_then_renumber(package: str, dispatch: object = None) -> None:
        original(package, dispatch)  # ty: ignore[invalid-argument-type]
        listing(fake_adb, (TRANSPORT + 1, "device"))

    monkeypatch.setattr(device, "_force_stop", stop_then_renumber)
    with pytest.raises(DeviceNotFoundError) as caught:
        device.launch(SHOP, restart=True)
    assert caught.value.details["reason"] == PIN_NOT_PROVEN
    assert caught.value.details["force_stopped"] is True
    assert android.stopped == [SHOP]
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am start")]


@pytest.mark.usefixtures("android")
def test_an_input_after_the_last_reconnect_fails_the_pin(fake_adb):
    device = pinned(fake_adb)
    for _ in range(MAX_RECONNECTS):
        Waits(device, [renumber(fake_adb, TRANSPORT)])
        listing(fake_adb, (TRANSPORT, "offline"))
        device.foreground()
    listing(fake_adb, (TRANSPORT, "offline"))
    with pytest.raises(DeviceNotFoundError) as caught:
        device.key("back")
    assert caught.value.details["reason"] == "device_changed"
    assert device._broken is not None


def test_a_stop_that_waits_after_force_stop_records_the_half_relaunch(
    tmp_path: Path, fake_adb, android, monkeypatch
):
    # After the force-stop the device drops out; halt() calls stop(), which
    # reads the device and so waits for it, and the time limit ends the wait.
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
            if dropped and device._repinning:
                return self.now + 3600
            return self.now

    def stop() -> bool:
        return bool(dropped) and device.foreground() is None

    monkeypatch.setattr(device, "stop_app", stop_then_drop)
    out = tmp_path / "run"
    # restart=False: the first force-stop is the relaunch's.
    result = run(device, LateClock(), out, stop=stop, restart=False)
    assert result.stop_reason == "max_duration"
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["start_not_sent"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again (time_limit)" in summary


def test_a_relaunch_refused_before_its_force_stop_is_taken_back(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    device = pinned(fake_adb)
    _covered_after_launch(monkeypatch, device, android)
    original = device.stop_app
    moved: list[bool] = []

    def renumber_then_stop(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        if not moved:
            moved.append(True)
            listing(fake_adb, (TRANSPORT + 1, "device"))
        original(package, _send_by=_send_by, _epoch=_epoch)

    monkeypatch.setattr(device, "stop_app", renumber_then_stop)
    out = tmp_path / "run"
    # restart=False: the first force-stop is the relaunch's.
    result = run(device, clock, out, restart=False)
    assert result.stop_reason == "relaunch_limit"  # the run went on to its end
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["device_reconnected"]
    assert android.stopped == [SHOP, SHOP, SHOP]  # three relaunches all sent


def test_withdraws_end_with_the_reconnect_limit(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The device is away once during every gate (never during an
    # observation): every action is taken back and chosen again, and the
    # crawl still ends at the Device's wait limit, with no input sent.
    device = pinned(fake_adb)
    Waits(device, [])
    in_gate = [False]
    original_gate = crawl_driver._Run.gate

    def gate(self: crawl_driver._Run, action: object, observation: object) -> object:
        in_gate[0] = True
        try:
            return original_gate(self, action, observation)  # ty: ignore[invalid-argument-type]
        finally:
            in_gate[0] = False

    original = device.keyboard_shown

    def away_in_the_gate() -> object:
        if in_gate[0]:
            listing(fake_adb, (TRANSPORT, "offline"))
            Waits(device, [renumber(fake_adb, TRANSPORT)])
        return original()

    monkeypatch.setattr(crawl_driver._Run, "gate", gate)
    monkeypatch.setattr(device, "keyboard_shown", away_in_the_gate)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError, match="away more than"):
        run(device, clock, out)
    assert android.inputs == []
    records = lines(out / "transitions.jsonl")
    refused = [r for r in records if r.get("reason") == "device_reconnected"]
    assert len(refused) == MAX_RECONNECTS  # one taken-back attempt per wait


def test_after_a_withdrawal_the_engine_and_the_run_folder_keep_their_landing(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # A tap from the start screen lands on browse; the next action is taken
    # back because the device was away in its gate, and meanwhile the detail
    # screen came up. The run folder keeps the landing it saw (browse); the
    # engine counts the screen of the fresh read (detail).
    engines: list[crawl_driver.CrawlEngine] = []

    class Kept(crawl_driver.CrawlEngine):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # ty: ignore[invalid-argument-type]
            engines.append(self)

    # `_crawl_on` reads `CrawlEngine` in the module that holds it.
    monkeypatch.setattr(driver_module, "CrawlEngine", Kept)
    device = pinned(fake_adb)
    in_gate = [False]
    original_gate = crawl_driver._Run.gate

    def gate(self: crawl_driver._Run, action: object, observation: object) -> object:
        in_gate[0] = True
        try:
            return original_gate(self, action, observation)  # ty: ignore[invalid-argument-type]
        finally:
            in_gate[0] = False

    original = device.keyboard_shown
    done: list[bool] = []

    def away_once_on_browse() -> object:
        if in_gate[0] and not done and android.screen.name == "browse":
            done.append(True)
            listing(fake_adb, (TRANSPORT, "offline"))
            Waits(device, [renumber(fake_adb, TRANSPORT)])
            android.show("detail")
        return original()

    monkeypatch.setattr(crawl_driver._Run, "gate", gate)
    monkeypatch.setattr(device, "keyboard_shown", away_once_on_browse)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert done
    records = lines(out / "transitions.jsonl")
    first_tap = next(
        r["action_id"] for r in records if r.get("action", {}).get("kind") == "tap"
    )
    end = next(r for r in records if "action" not in r and r["action_id"] == first_tap)
    browse = end["to_screen_id"]
    engine_tap = next(t for t in engines[0].records.transitions if t.kind == "tap")
    assert engine_tap.to_screen_id != browse  # the engine took the fresh read


# --- a failed identity read in a wait ends a launch, not a crawl ---


def _identity_fails_once_after(
    monkeypatch: pytest.MonkeyPatch,
    fake_adb: FakeAdbServer,
    android: FakeAndroid,
    device: Device,
    method: str,
    calls: int,
    failure: Reply,
) -> list[str]:
    """Renumber the transport right after the `calls`-th call of `method`, and
    make the first identity read after that fail (`failure`); later reads
    answer as before. Returns the identity reads made after the renumbering."""
    reads: list[str] = []
    armed = [False]

    def identity(command: str) -> Reply:
        if not armed[0]:
            return android._shell(command)
        reads.append(command)
        return failure if len(reads) == 1 else android._shell(command)

    android.override(re.compile(re.escape(IDENTITY_COMMAND)), identity)
    original = getattr(device, method)
    count = [0]

    def wrapped(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        count[0] += 1
        if count[0] == calls:
            listing(fake_adb, (TRANSPORT + 1, "device"))
            armed[0] = True
        return result

    monkeypatch.setattr(device, method, wrapped)
    return reads


@pytest.mark.parametrize(
    "failure",
    [Reset(), b"not an identity\n"],
    ids=["device_error", "unreadable_reply"],
)
def test_a_failed_identity_read_in_a_wait_ends_the_launch(
    fake_adb, android, monkeypatch, failure
):
    device = pinned(fake_adb)
    waits = Waits(device, [])
    reads = _identity_fails_once_after(
        monkeypatch, fake_adb, android, device, "_start", 1, failure
    )
    with pytest.raises(AdbError) as caught:
        device.launch(SHOP)
    assert caught.value.details["reason"] == "launch_interrupted"
    # the wait read the identity again and followed the device, but the
    # launch did not report success after the failure
    assert len(reads) == 2
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    assert waits.pauses == [REPIN_POLL]
    starts = [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    assert len(starts) == 1  # nothing was sent again
    assert android.stopped == []


@pytest.mark.parametrize(
    "failure",
    [Reset(), b"not an identity\n"],
    ids=["device_error", "unreadable_reply"],
)
def test_a_failed_identity_read_in_a_wait_outside_a_launch_is_waited_on(
    tmp_path: Path, fake_adb, android, clock, monkeypatch, failure
):
    device = pinned(fake_adb)
    reads = _identity_fails_once_after(
        monkeypatch, fake_adb, android, device, "foreground", 2, failure
    )
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=6))
    assert result.stop_reason == "max_actions"  # the crawl goes on, as before
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    assert len(reads) >= 2


def test_another_device_behind_the_new_number_ends_the_wait_at_once(fake_adb, android):
    # Not retried: an identity that was read and differs fails the pin.
    device = pinned(fake_adb)
    waits = Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))
    android.boot_id = OTHER_BOOT
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()
    assert caught.value.details["reason"] == "device_changed"
    assert waits.pauses == []


def test_a_failed_identity_read_on_the_last_poll_still_ends_the_launch(
    fake_adb, android, monkeypatch
):
    # The read at the deadline proves the device again after a failed
    # identity read and finds another app in front: launch_interrupted, not
    # not_in_front (on which a crawl would go on to a relaunch).
    from tests.device.launch.helpers import OK_36

    device = pinned(fake_adb)
    Waits(device, [])
    reply = OK_36.replace("{activity}", f"{SHOP}/.Main").encode()
    android.override(re.compile(r"^am start"), reply)  # the app stays behind
    reads = _identity_fails_once_after(
        monkeypatch, fake_adb, android, device, "_start", 1, b"not an identity\n"
    )
    with pytest.raises(AdbError) as caught:
        device.launch(SHOP, timeout=0)
    assert caught.value.details["reason"] == "launch_interrupted"
    assert len(reads) == 2
    starts = [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    assert len(starts) == 1
