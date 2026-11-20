"""Tests for the window a crawl's tap would reach (fake adb server and device)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT, Clock, lines, run
from uiprobe.crawl import Limits
from uiprobe.device import Device
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

BROWSE = (160, 220)  # the point the first tap of the fixture presses
OVERLAY = "com.example.chathead"


@pytest.fixture(params=[24, 31, 36], ids=["api24", "api31", "api36"])
def android(fake_adb: FakeAdbServer, request: pytest.FixtureRequest) -> FakeAndroid:
    return FakeAndroid(
        fake_adb,
        SERIAL,
        SCREENS,
        start="home",
        transport_id=TRANSPORT,
        api=request.param,
    )


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL  # the fake device answers first
    return Device(adb_server=fake_adb.address, pin=True)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _taps(android: FakeAndroid) -> list[tuple[int, int] | None]:
    return [i.point for i in android.inputs if i.kind == "tap"]


def test_window_at_reads_each_form(device, android):
    android.overlays.append(("ChatHead", OVERLAY, (140, 200, 180, 240)))
    found = device.window_at(BROWSE)
    assert not isinstance(found, str)
    assert (found.title, found.owner_uid) == ("ChatHead", android.uid_of(OVERLAY))
    found = device.window_at((10, 400))
    assert not isinstance(found, str)
    assert found.owner_uid == android.uid_of("com.example.launcher")
    assert device.package_uid(SHOP) == android.uid_of(SHOP)


def test_an_overlay_over_the_tap_point_refuses_the_tap(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # A window of another app takes touches right where the crawl would tap;
    # the app's window still has the focus.
    original = android.show

    def show_then_cover(name: str) -> None:
        original(name)
        if name == "main":
            android.overlays.append(("ChatHead", OVERLAY, (0, 180, 320, 260)))

    monkeypatch.setattr(android, "show", show_then_cover)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    records = lines(out / "transitions.jsonl")
    covered = [r for r in records if r.get("reason") == "covered_by_other"]
    assert covered
    assert covered[0]["details"] == {
        "window": "ChatHead",
        "owner_uid": android.uid_of(OVERLAY),
    }
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["check_tap_window"] is True


def test_an_overlay_elsewhere_does_not_stop_the_tap(
    tmp_path: Path, device, android, clock
):
    android.overlays.append(("FloatingMenu", OVERLAY, (272, 434, 320, 482)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)
    records = lines(out / "transitions.jsonl")
    assert not [r for r in records if r.get("reason") == "covered_by_other"]


def test_the_check_can_be_turned_off(tmp_path: Path, device, android, clock, fake_adb):
    android.overlays.append(("ChatHead", OVERLAY, (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(
        device,
        clock,
        out,
        limits=Limits(actions=3),
        policy=Policy(check_tap_window=False),
    )
    assert BROWSE in _taps(android)  # the tap went to the overlay's area
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["check_tap_window"] is False
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]


@pytest.mark.parametrize(
    "problem", ["no_list", "line_format", "region_format", "no_window"]
)
def test_a_window_that_cannot_be_read_refuses_the_tap(
    tmp_path: Path, device, android, clock, monkeypatch, problem
):
    monkeypatch.setattr(device, "window_at", lambda _point: problem)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    records = lines(out / "transitions.jsonl")
    unknown = [r for r in records if r.get("reason") == "cover_unknown"]
    assert unknown
    assert unknown[0]["details"] == {"unreadable": problem}


def test_an_app_uid_that_cannot_be_read_refuses_every_tap(
    tmp_path: Path, device, android, clock, monkeypatch
):
    monkeypatch.setattr(device, "package_uid", lambda _package: None)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    records = lines(out / "transitions.jsonl")
    unknown = [r for r in records if r.get("reason") == "cover_unknown"]
    assert unknown[0]["details"] == {"unreadable": "target_uid"}


def test_an_app_with_a_system_uid_needs_its_package_in_the_window_name(
    tmp_path: Path, device, android, clock
):
    # Settings-like: the app runs as uid 1000, which a system window over the
    # point shares. The window's name must name the app's package as well.
    android.uids[SHOP] = 1000
    android.uids["android"] = 1000
    android.overlays.append(("KeyguardScrim", "android", (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    records = lines(out / "transitions.jsonl")
    covered = [r for r in records if r.get("reason") == "covered_by_other"]
    assert covered[0]["details"] == {"window": "KeyguardScrim", "owner_uid": 1000}


def test_an_app_with_a_system_uid_taps_its_own_window(
    tmp_path: Path, device, android, clock
):
    android.uids[SHOP] = 1000
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)
