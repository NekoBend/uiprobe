"""Fixtures of the device core tests: the phone server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.device.core.helpers import (
    SERIAL,
)

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

# --- Device and devices() ---


@pytest.fixture
def phone(fake_adb: FakeAdbServer) -> FakeAdbServer:
    fake_adb.add_device(SERIAL, model="Pixel_8")
    return fake_adb
