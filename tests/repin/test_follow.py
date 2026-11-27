"""Tests for following a renumbered transport (fake adb server, fake device)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.crawl_driver.helpers import (
    SERIAL,
    SHOP,
    TRANSPORT,
    Clock,
    lines,
    run,
)
from uiprobe.crawl import Limits
from uiprobe.device import (
    IDENTITY_COMMAND,
    MAX_RECONNECTS,
    MAX_REPINS,
    PIN_NOT_PROVEN,
    PIN_WAIT_STOPPED,
    REPIN_POLL,
    REPIN_WAIT,
    Device,
)
from uiprobe.errors import (
    AdbError,
    DeviceNotFoundError,
)

if TYPE_CHECKING:
    from pathlib import Path


from tests.repin.helpers import (
    OTHER_BOOT,
    Waits,
    _renumber_after,
    listing,
    pinned,
    renumber,
    through,
)

# --- the pin and the identity ---


def test_pin_true_pins_and_reads_the_identity_when_the_device_is_chosen(
    fake_adb, android
):
    device = pinned(fake_adb)
    assert device.transport_id == TRANSPORT
    identity = device.pinned_identity
    assert identity is not None
    assert identity.boot_id == android.boot_id
    assert device.can_repin
    assert device.repins == ()
    assert through(fake_adb, TRANSPORT) == [f"shell:{IDENTITY_COMMAND}"]


@pytest.mark.usefixtures("android")
def test_pin_must_be_a_bool(fake_adb):
    loose: Any = Device
    with pytest.raises(TypeError, match="pin must be a bool"):
        loose(adb_server=fake_adb.address, pin="yes")
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_without_pin_nothing_is_pinned_or_read(fake_adb):
    device = Device(adb_server=fake_adb.address)
    assert device.transport_id is None
    assert device.pinned_identity is None
    assert not device.can_repin
    assert fake_adb.commands == []


def test_an_unreadable_identity_pins_nothing(fake_adb, android):
    android.props["ro.build.fingerprint"] = ""
    device = Device(adb_server=fake_adb.address)
    with pytest.raises(AdbError, match="could not be read"):
        device.pin_transport()
    assert device.transport_id is None
    assert device.pinned_identity is None


# --- following a renumbered transport ---


@pytest.mark.usefixtures("android")
def test_a_renumbered_transport_with_the_same_boot_id_is_followed(fake_adb):
    device = pinned(fake_adb)
    waits = Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))
    since = len(fake_adb.requests)
    assert device.foreground() is not None
    assert device.transport_id == TRANSPORT + 1
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    # the identity was read through the new number before anything else
    assert through(fake_adb, TRANSPORT + 1, since)[0] == f"shell:{IDENTITY_COMMAND}"
    assert through(fake_adb, TRANSPORT, since) == []
    assert waits.pauses == []
    device.check_pin(emulator=True)


@pytest.mark.usefixtures("android")
def test_offline_then_back_waits_and_follows_the_new_number(fake_adb):
    # The order the adb server showed: offline, gone, then a new number.
    device = pinned(fake_adb)
    waits = Waits(
        device,
        [lambda: listing(fake_adb), renumber(fake_adb, TRANSPORT + 1)],
    )
    listing(fake_adb, (TRANSPORT, "offline"))
    device.keyboard_shown()
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    assert waits.pauses == [REPIN_POLL, REPIN_POLL]


@pytest.mark.usefixtures("android")
def test_the_same_number_back_from_offline_is_no_repin(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [renumber(fake_adb, TRANSPORT)])
    listing(fake_adb, (TRANSPORT, "offline"))
    device.keyboard_shown()
    assert device.transport_id == TRANSPORT
    assert device.repins == ()


def test_another_boot_id_behind_the_new_number_is_not_followed(fake_adb, android):
    # The listing alone looks right; the identity read through the number
    # decides.
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))
    android.boot_id = OTHER_BOOT
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()
    assert caught.value.details["reason"] == "device_changed"
    assert device.transport_id == TRANSPORT
    assert device.repins == ()
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_two_ready_entries_of_the_serial_are_not_followed(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"), (TRANSPORT + 2, "device"))
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError, match="more than once"):
        device.foreground()
    assert through(fake_adb, TRANSPORT + 1, since) == []
    assert through(fake_adb, TRANSPORT + 2, since) == []


@pytest.mark.usefixtures("android")
def test_a_device_that_does_not_come_back_ends_after_the_wait(fake_adb):
    device = pinned(fake_adb)
    waits = Waits(device, [])
    listing(fake_adb)
    with pytest.raises(DeviceNotFoundError, match="did not come back"):
        device.foreground()
    assert sum(waits.pauses) == pytest.approx(REPIN_WAIT)
    assert device.transport_id == TRANSPORT


@pytest.mark.usefixtures("android")
def test_no_more_than_max_repins(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [])
    for step in range(1, MAX_REPINS + 1):
        listing(fake_adb, (TRANSPORT + step, "device"))
        device.foreground()
    assert len(device.repins) == MAX_REPINS
    listing(fake_adb, (TRANSPORT + MAX_REPINS + 1, "device"))
    with pytest.raises(DeviceNotFoundError, match="renumbered more than"):
        device.foreground()
    assert device.transport_id == TRANSPORT + MAX_REPINS


def test_without_a_boot_id_a_renumbered_transport_ends_the_pin(fake_adb, android):
    android.boot_id = None
    device = pinned(fake_adb)
    assert not device.can_repin
    waits = Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()
    assert caught.value.details["reason"] == "device_changed"
    assert waits.pauses == []
    assert through(fake_adb, TRANSPORT + 1, since) == []


def test_a_boot_id_that_can_no_longer_be_read_is_a_change(fake_adb, android):
    device = pinned(fake_adb)
    android.boot_id = None
    with pytest.raises(DeviceNotFoundError) as caught:
        device.check_pin(emulator=True)
    assert caught.value.details["reason"] == "device_changed"


@pytest.mark.parametrize(
    ("send", "command"),
    [
        (lambda d: d.key("back"), "input"),
        (lambda d: d.tap((10, 10)), "input"),
        (lambda d: d.stop_app(SHOP), "am"),
        # The launcher read goes out on the same rule as the start: neither
        # follows the renumbered transport.
        (lambda d: d.launch(SHOP), "am start"),
        (lambda d: d.launch(SHOP, restart=True), "am "),
        (lambda d: d.launch(SHOP), "cmd package"),
    ],
)
def test_an_input_never_follows_a_renumbered_transport(
    fake_adb, android, send, command
):
    device = pinned(fake_adb)
    waits = Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError):
        send(device)
    assert waits.pauses == []
    assert device.repins == ()
    assert through(fake_adb, TRANSPORT + 1, since) == []
    assert not [c for _, _, c in fake_adb.commands if c.startswith(command)]
    assert android.inputs == []
    assert android.stopped == []


@pytest.mark.parametrize("restart", [False, True])
def test_a_launch_sends_nothing_when_the_pin_moved_during_the_launcher_read(
    fake_adb, android, monkeypatch, restart
):
    device = pinned(fake_adb)
    original = device._shell_text

    def read_then_doubt(command: str, **kwargs: object) -> str:
        text = original(command, **kwargs)  # ty: ignore[invalid-argument-type]
        if command.startswith("cmd package"):
            device._epoch += 1  # as a failed request in between would
        return text

    monkeypatch.setattr(device, "_shell_text", read_then_doubt)
    with pytest.raises(DeviceNotFoundError) as caught:
        device.launch(SHOP, restart=restart)
    assert caught.value.details["reason"] == PIN_NOT_PROVEN
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am ")]
    assert android.stopped == []


@pytest.mark.usefixtures("android")
def test_the_watch_ends_the_wait(fake_adb):
    device = pinned(fake_adb)
    asked: list[bool] = []

    def watch() -> bool:
        asked.append(True)
        return len(asked) >= 2

    device.set_pin_watch(watch)
    waits = Waits(device, [])
    listing(fake_adb)
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()
    assert caught.value.details["reason"] == PIN_WAIT_STOPPED
    assert waits.pauses == [REPIN_POLL]


@pytest.mark.usefixtures("android")
def test_a_crawl_goes_on_after_a_renumbered_transport(
    tmp_path: Path, fake_adb, clock, monkeypatch
):
    device = pinned(fake_adb)
    _renumber_after(monkeypatch, fake_adb, device, "foreground", 2, TRANSPORT + 1)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=6))
    assert result.stop_reason == "max_actions"  # not an error, nothing lost
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
    # the move came during an observation, which was read again before
    # anything was chosen on it, so nothing was refused
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == []
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["repin"] is True
    end = json.loads((out / "run.json").read_bytes())
    assert end["repins"] == 1
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert (
        f"transport {TRANSPORT} -> {TRANSPORT + 1} for {SERIAL}:"
        " same serial, identity and boot id"
    ) in summary
    assert lines(out / "transitions.jsonl")


def test_a_crawl_without_a_boot_id_ends_on_a_renumbered_transport(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    android.boot_id = None
    device = pinned(fake_adb)
    _renumber_after(monkeypatch, fake_adb, device, "foreground", 2, TRANSPORT + 1)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError):
        run(device, clock, out, limits=Limits(actions=6))
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["repin"] is False
    end = json.loads((out / "run.json").read_bytes())
    assert end["stop_reason"] == "error"
    assert end["repins"] == 0


@pytest.mark.usefixtures("android")
def test_the_time_limit_during_a_wait_ends_the_crawl(
    tmp_path: Path, fake_adb, clock, monkeypatch
):
    # The device goes away and the time limit passes while it is waited for:
    # the run ends with max_duration, without calling stop() in the wait.
    device = pinned(fake_adb)
    asked_in_wait: list[bool] = []
    original = device.foreground
    count = [0]

    def gone_after_second() -> object:
        result = original()
        count[0] += 1
        if count[0] == 2:
            listing(fake_adb)  # the device goes away
            clock.now += 3600  # and the time limit passes
        return result

    def stop() -> bool:
        if device._repinning:
            asked_in_wait.append(True)
        return False

    monkeypatch.setattr(device, "foreground", gone_after_second)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=stop)
    assert result.stop_reason == "max_duration"
    assert asked_in_wait == []
    end = json.loads((out / "run.json").read_bytes())
    assert end["stop_reason"] == "max_duration"
    assert device.repins == ()


def test_the_time_limit_during_a_wait_in_the_gate_closes_the_action(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The device goes away while the gate reads for an action and the time
    # limit passes during the wait: that action was never sent.
    device = pinned(fake_adb)
    original = device.focused_window
    count = [0]

    def gone_in_the_gate() -> object:
        count[0] += 1
        if count[0] == 3:  # 1: after the launch; 2 and 3: the first tap's gate
            listing(fake_adb)
            clock.now += 3600
        return original()

    monkeypatch.setattr(device, "focused_window", gone_in_the_gate)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "max_duration"
    records = lines(out / "transitions.jsonl")
    starts = {r["action_id"] for r in records if "action" in r}
    ends = {r["action_id"]: r for r in records if "action" not in r}
    unsent = [ends[i] for i in starts if i in ends and ends[i].get("not_sent")]
    assert unsent
    assert unsent[-1]["reason"] == "time_limit"
    assert [i.kind for i in android.inputs] == []


# --- after a failed pin, and the edges of the wait ---


@pytest.mark.usefixtures("android")
def test_a_failed_pin_refuses_every_later_request(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb, (TRANSPORT, "unauthorized"))
    with pytest.raises(DeviceNotFoundError):
        device.foreground()
    listing(fake_adb, (TRANSPORT, "device"))  # the device is back
    since = len(fake_adb.requests)
    for request in (device.foreground, lambda: device.key("back")):
        with pytest.raises(DeviceNotFoundError) as caught:
            request()
        assert caught.value.details["reason"] == "device_changed"
    assert fake_adb.requests[since:] == []


def test_a_stopped_wait_swallowed_by_a_dump_read_sends_no_tap(
    tmp_path: Path, fake_adb, android, monkeypatch
):
    # The device drops out while the gate's dump reads the display size (a
    # read whose errors the dump swallows), the time limit ends the wait, and
    # the device comes back on its number right then. No tap may follow.
    device = pinned(fake_adb)
    original = device._shell  # the request helper the dump's reads use
    taps_planned = [0]
    late: list[bool] = []

    def shell(command: str) -> str:
        if command == "wm size" and android.screen.name == "main":
            taps_planned[0] += 1
            if taps_planned[0] == 2:  # 1: the observation; 2: the tap's gate
                listing(fake_adb)
                late.append(True)
        return original(command)

    class LateClock(Clock):
        def monotonic(self) -> float:
            if late:
                listing(fake_adb, (TRANSPORT, "device"))  # back on its number
                return self.now + 3600  # past the time limit
            return self.now

    monkeypatch.setattr(device, "_shell", shell)
    out = tmp_path / "run"
    result = run(device, LateClock(), out)
    assert result.stop_reason == "max_duration"
    assert not [c for _, _, c in fake_adb.commands if c.startswith("input ")]
    assert android.inputs == []
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["time_limit"]


@pytest.mark.usefixtures("android")
def test_a_watch_that_reads_the_device_fails_without_recursion(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [])
    device.set_pin_watch(lambda: device.foreground() is None)
    listing(fake_adb)
    with pytest.raises(DeviceNotFoundError):
        device.foreground()


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize(
    "entry",
    [
        {"serial": "emulator-5556", "state": "device"},  # the number is another's
        {"serial": SERIAL, "state": "unauthorized"},
    ],
)
def test_only_a_gone_or_offline_number_is_waited_for(fake_adb, entry):
    device = pinned(fake_adb)
    waits = Waits(device, [])
    fake_adb.devices[:] = [{**entry, "transport_id": TRANSPORT}]
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()
    assert caught.value.details["reason"] == "device_changed"
    assert waits.pauses == []
    assert device.reconnects == 0


@pytest.mark.usefixtures("android")
def test_an_offline_old_entry_beside_one_ready_new_entry_is_followed(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb, (TRANSPORT, "offline"), (TRANSPORT + 1, "device"))
    device.foreground()
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)


def test_a_slow_identity_read_past_the_deadline_moves_nothing(
    fake_adb, android, monkeypatch
):
    device = pinned(fake_adb)
    waits = Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))
    original = device._client.shell

    def slow(serial: str, command: str, **kwargs: object) -> bytes:
        if command == IDENTITY_COMMAND and kwargs.get("transport_id") == TRANSPORT + 1:
            waits.now += REPIN_WAIT + 5  # the answer comes after the deadline
        return original(serial, command, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(device._client, "shell", slow)
    with pytest.raises(DeviceNotFoundError, match="did not come back"):
        device.foreground()
    assert device.transport_id == TRANSPORT
    assert device.repins == ()
    assert android.boot_id  # the identity itself matched


@pytest.mark.usefixtures("android")
def test_the_watch_is_asked_again_before_the_pin_moves(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [])
    asked: list[bool] = []

    def watch() -> bool:
        asked.append(True)
        return len(asked) >= 2  # no on the first look, yes after the probe

    device.set_pin_watch(watch)
    listing(fake_adb, (TRANSPORT + 1, "device"))
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()
    assert caught.value.details["reason"] == PIN_WAIT_STOPPED
    assert device.transport_id == TRANSPORT


@pytest.mark.usefixtures("android")
def test_after_max_repins_the_same_number_may_still_come_back(fake_adb):
    device = pinned(fake_adb)
    Waits(device, [])
    for step in range(1, MAX_REPINS + 1):
        listing(fake_adb, (TRANSPORT + step, "device"))
        device.foreground()
    current = TRANSPORT + MAX_REPINS
    Waits(device, [renumber(fake_adb, current)])
    listing(fake_adb, (current, "offline"))
    device.foreground()
    assert device.transport_id == current
    assert len(device.repins) == MAX_REPINS


@pytest.mark.usefixtures("android")
def test_no_more_than_max_reconnects(fake_adb):
    device = pinned(fake_adb)
    for _ in range(MAX_RECONNECTS):
        Waits(device, [renumber(fake_adb, TRANSPORT)])
        listing(fake_adb, (TRANSPORT, "offline"))
        device.foreground()
    assert device.reconnects == MAX_RECONNECTS
    Waits(device, [renumber(fake_adb, TRANSPORT)])
    listing(fake_adb, (TRANSPORT, "offline"))
    with pytest.raises(DeviceNotFoundError, match="away more than"):
        device.foreground()


def test_a_boot_id_readable_only_later_keeps_the_three_field_check(fake_adb, android):
    android.boot_id = None
    device = pinned(fake_adb)
    android.boot_id = "3ea6a12b-8379-445f-852a-73a2c4c3ff17"
    device.check_pin(emulator=True)  # the three fields still match
    assert not device.can_repin


@pytest.mark.usefixtures("android")
def test_a_run_counts_only_its_own_repins(tmp_path: Path, fake_adb, clock):
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))
    device.foreground()  # a move before the crawl
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=1))
    assert json.loads((out / "run.json").read_bytes())["repins"] == 0
    assert "transport" not in (out / "summary.txt").read_text(encoding="utf-8")


def test_the_check_before_the_run_does_not_ask_stop_and_restores_the_watch(
    tmp_path: Path, fake_adb, android, clock
):
    device = pinned(fake_adb)
    Waits(device, [])

    def mine() -> bool:
        return False

    device.set_pin_watch(mine)
    listing(fake_adb)  # gone before the crawl starts
    asked: list[bool] = []

    def stop() -> bool:
        asked.append(True)
        return False

    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError, match="did not come back"):
        run(device, clock, out, stop=stop)
    assert asked == []  # stop() is not called while the device is waited for
    assert not out.exists()
    assert device.pin_watch is mine
    assert android.inputs == []
