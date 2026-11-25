"""Tests for a failed request, the watch, the server and the first pin."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import (
    SERIAL,
    TRANSPORT,
    lines,
    run,
)
from uiprobe.crawl import Limits
from uiprobe.device import (
    IDENTITY_COMMAND,
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


from tests.repin.helpers import (
    OTHER_BOOT,
    Waits,
    listing,
    pinned,
    renumber,
    through,
)

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
