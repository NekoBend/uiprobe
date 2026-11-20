"""Tests for following a renumbered transport (fake adb server, fake device)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

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
from uiprobe.crawl import Limits
from uiprobe.device import (
    IDENTITY_COMMAND,
    MAX_REPINS,
    PIN_WAIT_STOPPED,
    REPIN_POLL,
    REPIN_WAIT,
    Device,
)
from uiprobe.errors import AdbError, DeviceNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

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
        (lambda d: d.launch(SHOP), "monkey"),
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
    assert result.stop_reason == "max_actions"
    assert device.repins == ((TRANSPORT, TRANSPORT + 1),)
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
def test_stop_during_the_wait_ends_the_crawl_as_interrupted(
    tmp_path: Path, fake_adb, clock, monkeypatch
):
    device = pinned(fake_adb)
    stopping: list[bool] = []
    original = device.foreground
    count = [0]

    def gone_after_second() -> object:
        result = original()
        count[0] += 1
        if count[0] == 2:
            listing(fake_adb)  # the device goes away
            stopping.append(True)  # and the caller asks to stop
        return result

    monkeypatch.setattr(device, "foreground", gone_after_second)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: bool(stopping))
    assert result.stop_reason == "interrupted"
    end = json.loads((out / "run.json").read_bytes())
    assert end["stop_reason"] == "interrupted"
    assert device.repins == ()


def test_stop_during_a_wait_in_the_gate_closes_the_action_as_not_sent(
    tmp_path: Path, fake_adb, android, clock, monkeypatch
):
    # The device goes away while the gate reads for an action; the caller
    # asks to stop during the wait: that action was never sent.
    device = pinned(fake_adb)
    stopping: list[bool] = []
    original = device.focused_window
    count = [0]

    def gone_in_the_gate() -> object:
        count[0] += 1
        if count[0] == 3:  # 1: after the launch; 2 and 3: the first tap's gate
            listing(fake_adb)
            stopping.append(True)
        return original()

    monkeypatch.setattr(device, "focused_window", gone_in_the_gate)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: bool(stopping))
    assert result.stop_reason == "interrupted"
    records = lines(out / "transitions.jsonl")
    starts = {r["action_id"] for r in records if "action" in r}
    ends = {r["action_id"]: r for r in records if "action" not in r}
    unsent = [ends[i] for i in starts if i in ends and ends[i].get("not_sent")]
    assert unsent
    assert unsent[-1]["reason"] == "stopped"
    assert [i.kind for i in android.inputs] == []
