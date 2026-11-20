"""Tests for the window a crawl's tap would reach (fake adb server and device)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT, Clock, lines, run
from tests.test_repin import Waits, listing, renumber
from uiprobe.crawl import Limits
from uiprobe.device import Device
from uiprobe.guards import Policy
from uiprobe.packageuid import PackageUid

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
    assert device.package_uid(SHOP) == PackageUid(android.uid_of(SHOP), shared=False)


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


def test_package_uid_reads_the_package_stanza(device, android):
    # The fake prints another package's stanza first and, for a shared uid, a
    # "Shared users:" section after; userId= below API 34, appId= from it.
    android.uids[SHOP] = 1000
    android.uids[SHOP + "x"] = 10123
    android.shared_users[SHOP] = "android.uid.system"
    assert device.package_uid(SHOP) == PackageUid(1000, shared=True)
    android.shared_users.clear()
    android.uids[SHOP] = 10500
    assert device.package_uid(SHOP) == PackageUid(10500, shared=False)


def _covered(out: Path) -> list[dict[str, object]]:
    return [r for r in lines(out / "transitions.jsonl") if r.get("not_sent")]


def test_a_package_sharing_an_app_uid_is_not_the_app(
    tmp_path: Path, device, android, clock
):
    # Two packages share the ordinary uid 10500 (android:sharedUserId); a
    # window of the other one over the point has the app's uid but does not
    # name the app.
    helper = "com.example.shophelper"
    for package in (SHOP, helper):
        android.uids[package] = 10500
        android.shared_users[package] = "com.example.shared"
    android.overlays.append(("HelperBubble", helper, (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    refused = _covered(out)
    assert refused[0]["reason"] == "covered_by_other"
    assert refused[0]["details"] == {"window": "HelperBubble", "owner_uid": 10500}


def test_an_app_sharing_its_uid_taps_its_own_window(
    tmp_path: Path, device, android, clock
):
    android.uids[SHOP] = 10500
    android.shared_users[SHOP] = "com.example.shared"
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)  # its window's title is SHOP/...


def test_a_system_uid_window_of_a_longer_package_name_is_not_the_app(
    tmp_path: Path, device, android, clock
):
    # Both run as uid 1000; the other package's name begins with the app's.
    longer = SHOP + "x"
    for package in (SHOP, longer):
        android.uids[package] = 1000
        android.shared_users[package] = "android.uid.system"
    title = f"{longer}/{longer}.Bubble"
    android.overlays.append((title, longer, (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    refused = _covered(out)
    assert refused[0]["details"] == {"window": title, "owner_uid": 1000}


@pytest.mark.parametrize("orientation", [1, 3, None])
def test_a_rotated_or_unknown_display_refuses_every_tap(
    tmp_path: Path, device, android, clock, orientation
):
    android.orientation = orientation
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert {r["reason"] for r in refused} == {"cover_unknown"}
    assert refused[0]["details"] == {"unreadable": "rotation"}


def test_a_device_away_during_the_window_read_is_device_reconnected(
    tmp_path: Path, device, android, clock, fake_adb, monkeypatch
):
    # The device drops out while the gate reads the window at the point and
    # comes back on its number, proven the same; the window read then shows
    # another app's window there. The pin moved during the gate, so the tap
    # is refused as device_reconnected, without the cover's details, and is
    # not sent.
    original = device.window_at
    first: list[tuple[int, int]] = []

    def away_once(point: tuple[int, int]) -> object:
        if not first:
            first.append(point)
            x, y = point
            android.overlays.append(("ChatHead", OVERLAY, (x - 1, y - 1, x + 1, y + 1)))
            Waits(device, [renumber(fake_adb, TRANSPORT)])
            listing(fake_adb, (TRANSPORT, "offline"))
        return original(point)

    monkeypatch.setattr(device, "window_at", away_once)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert device.reconnects == 1
    assert first
    assert first[0] not in _taps(android)
    refused = _covered(out)
    assert refused[0]["reason"] == "device_reconnected"
    assert "details" not in refused[0]
