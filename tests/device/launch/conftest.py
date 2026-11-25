"""Fixtures of the launch tests: the fake device and the fake clock."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from tests.device.launch.helpers import (
    MISSING,
    SCREENS,
    SERIAL,
    Clock,
)
from tests.fake_device import FakeAndroid
from uiprobe.device import Device
from uiprobe.device import pin as device_pin
from uiprobe.device import state as device_state

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(
        fake_adb, SERIAL, SCREENS, start="launcher", extra_packages=(MISSING,)
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    fake_time = SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep)
    monkeypatch.setattr(device_pin, "time", fake_time)
    monkeypatch.setattr(device_state, "time", fake_time)
    return fake


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer, clock: Clock) -> Device:
    assert android.serial == SERIAL
    assert clock.now == 1000.0
    return Device(adb_server=fake_adb.address)
