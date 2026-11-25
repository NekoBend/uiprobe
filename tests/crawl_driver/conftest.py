"""Fixtures of the crawl driver tests: the fake device, the fake clock, run cleanup."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import (
    FakeAndroid,
)
from uiprobe.device import Device
from uiprobe.runlog import RunFolderError

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_driver.helpers import (
    _WRITERS,
    SCREENS,
    SERIAL,
    TRANSPORT,
    Clock,
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(autouse=True)
def _close_writers():
    yield
    while _WRITERS:
        with contextlib.suppress(RunFolderError):
            _WRITERS.pop().close()
