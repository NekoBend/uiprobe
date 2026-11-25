"""Fixtures of the dry-run tests: a settings folder and the fake device."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.fake_device import FakeAndroid
from uiprobe.settings import (
    SETTINGS_FILE,
    write_default_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_dry.helpers import (
    SCREENS,
    SERIAL,
)


@pytest.fixture(autouse=True)
def settings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in a folder that holds the generated default settings file."""
    folder = tmp_path / "work"
    folder.mkdir()
    write_default_settings(folder / SETTINGS_FILE)
    monkeypatch.chdir(folder)
    return folder


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")
