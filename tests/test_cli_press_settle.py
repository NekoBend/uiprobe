"""The pause after each press that the crawl command hands to the driver.

These tests look only at the value passed to `crawl_run`; the driver then runs
with no pause, so they do not wait.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import SCREENS, SERIAL, SHOP, TRANSPORT
from tests.fake_device import FakeAndroid
from uiprobe import cli
from uiprobe.settings import SETTINGS_FILE, render_default_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / SETTINGS_FILE).write_text(render_default_settings(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _settle_given(
    fake_adb: FakeAdbServer,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    out: Path,
    *extra: str,
) -> object:
    import uiprobe.cli.crawl_press as cli_module

    seen: list[object] = []
    original = cli_module.crawl_run

    def spy(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("settle"))
        kwargs["settle"] = 0.0  # run the fake crawl without waiting
        return original(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(cli_module, "crawl_run", spy)
    argv = ["--press", "--out", str(out), "--actions", "2", "--quiet", *extra]
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    capsys.readouterr()
    assert code == 0
    assert len(seen) == 1
    return seen[0]


@pytest.mark.usefixtures("here", "android")
def test_the_default_pause_after_a_press_is_one_second(
    fake_adb, capsys, monkeypatch, tmp_path
):
    given = _settle_given(fake_adb, capsys, monkeypatch, tmp_path / "run")
    assert type(given) is float
    assert given == 1.0


@pytest.mark.usefixtures("here", "android")
def test_the_settle_option_is_passed_as_given(fake_adb, capsys, monkeypatch, tmp_path):
    given = _settle_given(
        fake_adb, capsys, monkeypatch, tmp_path / "run", "--settle", "0.25"
    )
    assert type(given) is float
    assert given == 0.25
