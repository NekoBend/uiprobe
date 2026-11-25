"""Fixtures of the repin tests: the fake device and the fake clock."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import (
    SCREENS,
    SERIAL,
    TRANSPORT,
    Clock,
)
from tests.fake_device import FakeAndroid

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def clock() -> Clock:
    return Clock()
