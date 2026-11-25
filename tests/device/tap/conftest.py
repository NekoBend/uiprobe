"""Fixtures of the tap tests: the fake device and the marker check."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.fake_device import (
    FakeAndroid,
)
from uiprobe.device import Device

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.model import Screen as ScreenModel
from tests.device.tap.helpers import (
    COVER_SCREEN,
    COVER_SERIAL,
    MARKER_FORMS,
    MATCH_SCREEN,
    MATCH_SERIAL,
    SCREENS,
    SERIAL,
    sent_inputs,
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


@pytest.fixture(autouse=True)
def _only_marker_inputs(fake_adb: FakeAdbServer):
    """After each test, every input command received had a marker form."""
    yield
    for command in sent_inputs(fake_adb):
        assert command.endswith("; echo uiprobe-exit:$?"), command
        assert any(form.fullmatch(command) for form in MARKER_FORMS), command


@pytest.fixture
def screen(device: Device) -> ScreenModel:
    return device.dump()


@pytest.fixture
def cover(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, COVER_SERIAL, (COVER_SCREEN,), start="c")


@pytest.fixture
def matches(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, MATCH_SERIAL, (MATCH_SCREEN,), start="m")
