"""Tests for following a renumbered transport (fake adb server, fake device)."""

from __future__ import annotations

import contextlib
import json
import re
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_adb_server import Reset
from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import (
    SCREENS,
    SERIAL,
    SHOP,
    TRANSPORT,
    Clock,
    lines,
    run,
)
from uiprobe import crawl_driver
from uiprobe.crawl import Limits
from uiprobe.crawl_driver import driver as driver_module
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
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DumpError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer, Reply

OTHER_BOOT = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def clock() -> Clock:
    return Clock()


class Waits:
    """The device's clock and pause: each pause runs the next change."""

    def __init__(self, device: Device, changes: list[Callable[[], None]]) -> None:
        self.now = 100.0
        self.pauses: list[float] = []
        self.changes = changes
        device._clock = self.clock  # the test seam of Device
        device._sleep = self.sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds
        if self.changes:
            self.changes.pop(0)()


def listing(fake_adb: FakeAdbServer, *entries: tuple[int, str]) -> None:
    """Make the server list `SERIAL` under each (transport id, state)."""
    fake_adb.devices[:] = [
        {"serial": SERIAL, "state": state, "transport_id": number}
        for number, state in entries
    ]


def renumber(fake_adb: FakeAdbServer, number: int) -> Callable[[], None]:
    return lambda: listing(fake_adb, (number, "device"))


def through(fake_adb: FakeAdbServer, number: int, since: int = 0) -> list[str]:
    """The services asked for right after selecting transport `number`."""
    requests = fake_adb.requests[since:]
    selector = f"host:transport-id:{number}"
    return [
        requests[i + 1]
        for i, request in enumerate(requests[:-1])
        if request == selector
    ]


def pinned(fake_adb: FakeAdbServer) -> Device:
    return Device(adb_server=fake_adb.address, pin=True)


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


# --- the crawl ---


def _renumber_after(
    monkeypatch: pytest.MonkeyPatch,
    fake_adb: FakeAdbServer,
    device: Device,
    method: str,
    calls: int,
    number: int,
) -> None:
    """Renumber the transport right after the `calls`-th call of `method`."""
    original = getattr(device, method)
    count = [0]

    def wrapped(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)
        count[0] += 1
        if count[0] == calls:
            listing(fake_adb, (number, "device"))
        return result

    monkeypatch.setattr(device, method, wrapped)


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
    assert not [c for _, _, c in fake_adb.commands if c.startswith("input")]
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


def _covered_after_launch(monkeypatch, device, android) -> None:
    """Every launch ends with a screen of another app in front (a relaunch
    follows by default)."""
    original_launch = device._launch_component

    def launch_under(*args: object, **kwargs: object) -> object:
        result = original_launch(*args, **kwargs)
        android.show("ext1")
        return result

    monkeypatch.setattr(device, "_launch_component", launch_under)


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


# --- a failed request, the watch, the server, the first pin ---


def test_a_failed_request_swallowed_by_the_dump_is_proven_before_a_tap(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The dump's cleanup fails with "offline" after a ready listing (the dump
    # swallows it), and the same number then leads to another boot id. The
    # next read proves the device again and fails; no tap goes out.
    device = pinned(fake_adb)
    original = device._client.shell
    removes = [0]

    def shell(serial: str, command: str, **kwargs: object) -> bytes:
        if command.startswith("rm -f "):
            removes[0] += 1
            if removes[0] == 2:  # 1: the observation; 2: the tap's gate
                android.boot_id = OTHER_BOOT
                raise DeviceOfflineError("device offline", details={"serial": serial})
        return original(serial, command, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(device._client, "shell", shell)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError) as caught:
        run(device, clock, out)
    assert caught.value.details["reason"] == "device_changed"
    assert not [c for _, _, c in fake_adb.commands if c.startswith("input")]
    assert android.inputs == []


def test_a_failed_request_with_the_same_device_refuses_only_that_action(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The same failure, but the device is still the same: it is proven again,
    # the tap chosen before is refused, and the crawl goes on.
    device = pinned(fake_adb)
    original = device._client.shell
    removes = [0]

    def shell(serial: str, command: str, **kwargs: object) -> bytes:
        if command.startswith("rm -f "):
            removes[0] += 1
            if removes[0] == 2:
                raise DeviceOfflineError("device offline", details={"serial": serial})
        return original(serial, command, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(device._client, "shell", shell)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=4))
    assert result.stop_reason in {"max_actions", "exhausted"}
    records = lines(out / "transitions.jsonl")
    assert [r["reason"] for r in records if r.get("not_sent")] == ["device_reconnected"]
    assert [i for i in android.inputs if i.kind == "tap"]  # the crawl went on


@pytest.mark.usefixtures("android")
def test_a_watch_that_sends_an_input_sends_nothing(fake_adb, android):
    device = pinned(fake_adb)
    Waits(device, [])

    def watch() -> bool:
        with contextlib.suppress(DeviceNotFoundError):
            device.key("back")  # swallowed on purpose
        return False

    device.set_pin_watch(watch)
    listing(fake_adb, (TRANSPORT, "offline"))
    with pytest.raises(DeviceNotFoundError):
        device.foreground()
    assert android.inputs == []
    assert device.transport_id == TRANSPORT
    with pytest.raises(DeviceNotFoundError):
        device.key("back")
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_a_server_error_while_waiting_fails_the_pin(fake_adb, monkeypatch, android):
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb)
    original = device._client.devices
    calls = [0]

    def devices() -> object:
        calls[0] += 1
        if calls[0] >= 2:  # the first listing inside the wait
            raise AdbServerUnavailableError("gone", details={})
        return original()

    monkeypatch.setattr(device._client, "devices", devices)
    with contextlib.suppress(AdbServerUnavailableError):
        device.api_level()  # a reader that swallows errors
    monkeypatch.setattr(device._client, "devices", original)
    listing(fake_adb, (TRANSPORT, "device"))
    with pytest.raises(DeviceNotFoundError):
        device.key("back")
    assert android.inputs == []


def test_a_failed_first_pin_keeps_every_request_off_the_device(fake_adb, android):
    device = Device(adb_server=fake_adb.address)
    listing(fake_adb, (TRANSPORT, "offline"))
    with pytest.raises(DeviceNotFoundError):
        device.pin_transport()
    listing(fake_adb, (TRANSPORT, "device"))
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError):
        device.key("back")
    assert fake_adb.requests[since:] == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_an_unauthorized_entry_during_the_wait_fails_the_pin(fake_adb):
    device = pinned(fake_adb)
    Waits(
        device,
        [
            lambda: listing(fake_adb, (TRANSPORT, "unauthorized")),
            renumber(fake_adb, TRANSPORT),
        ],
    )
    listing(fake_adb, (TRANSPORT, "offline"))
    with pytest.raises(DeviceNotFoundError, match="not usable"):
        device.foreground()


def test_no_probe_starts_at_the_deadline(fake_adb, android, monkeypatch):
    device = pinned(fake_adb)
    waits = Waits(device, [])
    listing(fake_adb, (TRANSPORT, "offline"))
    original = device._client.devices

    def late_listing() -> object:
        found = original()
        waits.now += REPIN_WAIT  # the listing itself took the whole budget
        listing(fake_adb, (TRANSPORT + 1, "device"))
        return found

    monkeypatch.setattr(device._client, "devices", late_listing)
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError, match="did not come back"):
        device.foreground()
    assert through(fake_adb, TRANSPORT + 1, since) == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_the_kept_failure_holds_no_exception(fake_adb):
    device = pinned(fake_adb)
    listing(fake_adb, (TRANSPORT, "unauthorized"))
    with pytest.raises(DeviceNotFoundError):
        device.foreground()
    kept = device._broken
    assert isinstance(kept, tuple)
    assert not any(isinstance(part, BaseException) for part in kept)


def _fail_once(
    monkeypatch: pytest.MonkeyPatch,
    device: Device,
    command: str,
    make: Callable[[], Exception],
    at_failure: Callable[[], None] = lambda: None,
) -> list[bool]:
    """Fail the first shell request of `command` with `make()`; call
    `at_failure` right before. The list holds True once it failed."""
    original = device._client.shell
    failed: list[bool] = []

    def shell(serial: str, sent: str, **kwargs: object) -> bytes:
        if sent == command and not failed:
            failed.append(True)
            at_failure()
            raise make()
        return original(serial, sent, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(device._client, "shell", shell)
    return failed


def _offline() -> Exception:
    return DeviceOfflineError("device offline", details={"serial": SERIAL})


def test_without_a_boot_id_an_input_after_a_failure_ends_the_pin(
    fake_adb, android, monkeypatch
):
    # Without a boot id the device cannot be proven again after a failed
    # request (another emulator of the same image would pass the same
    # reads), so the input is refused and the pin fails.
    android.boot_id = None
    device = pinned(fake_adb)
    _fail_once(monkeypatch, device, "wm size", _offline)
    with contextlib.suppress(DeviceOfflineError):
        device._shell("wm size")
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError) as caught:
        device.key("back")
    assert caught.value.details["reason"] == "device_changed"
    assert len(fake_adb.requests) == since  # not even the listing is read
    assert android.inputs == []


def test_without_a_boot_id_a_read_after_a_failure_ends_the_pin(
    fake_adb, android, monkeypatch
):
    android.boot_id = None
    device = pinned(fake_adb)
    epoch = device.pin_epoch
    _fail_once(monkeypatch, device, "wm size", _offline)
    with contextlib.suppress(DeviceOfflineError):
        device._shell("wm size")
    since = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()  # not proven in place: the identity is not read
    assert caught.value.details["reason"] == "device_changed"
    refused_at = len(fake_adb.requests)
    assert refused_at == since  # not even the listing is read
    assert device.pin_epoch > epoch
    assert device.reconnects == 0
    with pytest.raises(DeviceNotFoundError) as again:
        device.key("back")  # the failed pin is kept
    assert again.value.details["reason"] == "device_changed"
    assert str(again.value) == str(caught.value)
    assert len(fake_adb.requests) == refused_at
    assert android.inputs == []


def test_without_a_boot_id_an_unreachable_server_still_ends_the_pin(
    fake_adb, android, monkeypatch
):
    # A reader swallows a failed request while the adb server cannot be
    # reached; the server stays away. The next request fails the pin at once,
    # without asking the server for its list, and the failure is kept.
    android.boot_id = None
    device = pinned(fake_adb)
    fake_adb.stop()
    original = device._client.devices
    listings = [0]

    def devices() -> object:
        listings[0] += 1
        return original()

    monkeypatch.setattr(device._client, "devices", devices)
    assert device.api_level() is None  # the failed read is swallowed
    assert listings[0] == 1
    with pytest.raises(DeviceNotFoundError) as caught:
        device.foreground()
    assert caught.value.details["reason"] == "device_changed"
    assert listings[0] == 1  # no new host request
    assert device._broken is not None
    with pytest.raises(DeviceNotFoundError) as again:
        device.foreground()
    assert str(again.value) == str(caught.value)
    assert again.value.details == caught.value.details
    assert listings[0] == 1
    assert android.inputs == []


def test_with_a_boot_id_a_read_after_a_failure_still_proves_the_device(
    fake_adb, android, monkeypatch
):
    device = pinned(fake_adb)
    Waits(device, [])
    _fail_once(monkeypatch, device, "wm size", _offline)
    with contextlib.suppress(DeviceOfflineError):
        device._shell("wm size")
    since = len(fake_adb.requests)
    assert device.foreground() is not None  # proven again, then read
    assert through(fake_adb, TRANSPORT, since)[0] == f"shell:{IDENTITY_COMMAND}"
    assert device.reconnects == 1
    device.key("back")
    assert [i.kind for i in android.inputs] == ["key"]


def test_a_crawl_without_a_boot_id_sends_nothing_after_a_lost_read(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The review's case: the boot id cannot be read; the API-level read loses
    # the connection, and at that moment another emulator of the same image
    # takes the same serial, transport number, fingerprint and ro.serialno.
    # Nothing can tell it from the first one, so no input may reach it.
    android.boot_id = None
    device = pinned(fake_adb)
    sent_before: list[int] = []
    failed = _fail_once(
        monkeypatch,
        device,
        "getprop ro.build.version.sdk",
        lambda: AdbError("connection closed", details={}),
        lambda: sent_before.append(len(fake_adb.commands)),
    )
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError) as caught:
        run(device, clock, out)
    assert failed
    assert caught.value.details["reason"] == "device_changed"
    after = [c for _, _, c in fake_adb.commands[sent_before[0] :]]
    assert not [c for c in after if c.startswith(("input ", "monkey ", "am "))]
    assert after == []  # not even the identity is read again
    end = json.loads((out / "run.json").read_bytes())
    assert end["stop_reason"] == "error"


@pytest.mark.usefixtures("android")
def test_a_move_during_the_check_before_the_run_is_counted(
    tmp_path: Path, fake_adb, clock
):
    device = pinned(fake_adb)
    Waits(device, [])
    listing(fake_adb, (TRANSPORT + 1, "device"))  # moves at the emulator check
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=1))
    assert json.loads((out / "run.json").read_bytes())["repins"] == 1


@pytest.mark.usefixtures("android")
def test_dumps_that_keep_failing_still_end_with_the_engine_limit(
    tmp_path: Path, fake_adb, clock, monkeypatch
):
    # A dump that times out every time also moves the pin's epoch (the
    # request failed); the observation is not reread forever: the failed
    # dump goes to the engine, which stops after three.
    device = pinned(fake_adb)

    def failing_dump() -> object:
        device._epoch += 1  # what a failed request on the pin does
        raise DumpError("timed out", details={})

    monkeypatch.setattr(device, "dump", failing_dump)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "dump_failed_repeatedly"
    records = lines(out / "transitions.jsonl")
    ends = [r for r in records if "action" not in r]
    assert ends
    assert ends[0]["dump_failed"] is True


@pytest.mark.usefixtures("android")
def test_a_failed_keyboard_read_keeps_the_destination_read_before_it(
    tmp_path: Path, fake_adb, clock, monkeypatch
):
    device = pinned(fake_adb)

    def no_keyboard() -> object:
        raise AdbError("connection closed", details={})

    monkeypatch.setattr(device, "keyboard_shown", no_keyboard)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    records = lines(out / "transitions.jsonl")
    ends = [r for r in records if "action" not in r]
    assert [e["action_id"] for e in ends] == [1]  # the launch got its end line
    assert ends[0]["dump_failed"] is False


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
    from tests.test_launch import OK_36

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
