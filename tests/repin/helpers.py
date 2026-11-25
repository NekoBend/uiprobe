"""Shared helpers of the repin tests: a fake wait clock and adb listings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.crawl_driver.helpers import (
    SERIAL,
)
from uiprobe.device import (
    Device,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from tests.fake_adb_server import FakeAdbServer


OTHER_BOOT = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"


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


def _covered_after_launch(monkeypatch, device, android) -> None:
    """Every launch ends with a screen of another app in front (a relaunch
    follows by default)."""
    original_launch = device._launch_component

    def launch_under(*args: object, **kwargs: object) -> object:
        result = original_launch(*args, **kwargs)
        android.show("ext1")
        return result

    monkeypatch.setattr(device, "_launch_component", launch_under)
