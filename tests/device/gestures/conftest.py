"""Fixtures of the swipe, scroll and key tests: the fake device and the marker check."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.device.gestures.helpers import (
    MARKER_FORMS,
    SCREENS,
    SERIAL,
    sent_inputs,
)
from tests.fake_device import (
    FakeAndroid,
)
from uiprobe.device import (
    Device,
)

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.model import Screen as ScreenModel


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", general_swipes=True)


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


@pytest.fixture(autouse=True)
def _only_marker_inputs(fake_adb: FakeAdbServer):
    """After each test, every input command received had a marker form."""
    yield
    for command in sent_inputs(fake_adb):
        assert any(form.fullmatch(command) for form in MARKER_FORMS), command


@pytest.fixture(autouse=True)
def _folder_without_settings(tmp_path, monkeypatch):
    """``uiprobe key`` reads ./uiprobe.toml if there is one: use an empty folder."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def screen(device: Device) -> ScreenModel:
    return device.dump()
