"""Tests for the records of a path replay: the ``replay`` key of the start line."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.crawl_driver.helpers import (
    OTHER,
    SERIAL,
    SHOP,
    TRANSPORT,
    Clock,
    button,
    lines,
    run,
)
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe.crawl import Limits
from uiprobe.device import Device
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

REPLAY_SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((0, 0, 320, 100), text="Shop"),
            button(200, "Alpha", "sub"),
            button(260, "Bravo", "leaf_b"),
            button(320, "Charlie", "leaf_c"),
        ),
    ),
    Screen(
        "sub",
        f"{SHOP}/.Sub",
        (
            El((0, 0, 320, 100), text="Sub"),
            button(200, "one", "ext"),
            button(260, "two", "leaf_two"),
            button(320, "three", "leaf_three"),
        ),
    ),
    Screen("leaf_b", f"{SHOP}/.LeafB", (El((0, 0, 320, 100), text="Leaf B"),)),
    Screen("leaf_c", f"{SHOP}/.LeafC", (El((0, 0, 320, 100), text="Leaf C"),)),
    Screen("leaf_two", f"{SHOP}/.LeafTwo", (El((0, 0, 320, 100), text="Two"),)),
    Screen("leaf_three", f"{SHOP}/.LeafThree", (El((0, 0, 320, 100), text="Three"),)),
    Screen("ext", f"{OTHER}/.One", (El((0, 0, 320, 640)),)),
)


def crawl_replay(
    fake_adb: FakeAdbServer, clock: Clock, out: Path, policy: Policy
) -> FakeAndroid:
    android = FakeAndroid(
        fake_adb, SERIAL, REPLAY_SCREENS, start="home", transport_id=TRANSPORT
    )
    device = Device(adb_server=fake_adb.address)
    result = run(device, clock, out, limits=Limits(actions=40), policy=policy)
    assert result.stop_reason == "exhausted"
    return android


def starts(out: Path) -> list[dict]:
    return [
        r
        for r in lines(out / "transitions.jsonl")
        if r["schema"] == "uiprobe.transition_start/1"
    ]


def tap_texts(out: Path) -> list[tuple[str, bool]]:
    return [
        (r["action"]["element"]["text"], r.get("replay", False))
        for r in starts(out)
        if r["action"]["kind"] == "tap"
    ]


def test_a_replay_tap_has_replay_true_on_its_start_line(tmp_path, fake_adb, clock):
    out = tmp_path / "run"
    crawl_replay(fake_adb, clock, out, Policy())
    assert tap_texts(out) == [
        ("Alpha", False),
        ("one", False),  # leaves the app: a relaunch
        ("Alpha", True),  # the way back down
        ("two", False),
        ("three", False),
        ("Bravo", False),
        ("Charlie", False),
    ]
    for record in starts(out):
        if record.get("replay"):
            assert record["replay"] is True
            assert record["action"]["kind"] == "tap"
        else:
            assert "replay" not in record  # absent, never false


def test_without_replay_no_start_line_has_the_key_and_a2_is_not_pressed(
    tmp_path, fake_adb, clock
):
    out = tmp_path / "run"
    crawl_replay(fake_adb, clock, out, Policy(replay_path=False))
    assert tap_texts(out) == [
        ("Alpha", False),
        ("one", False),
        ("Bravo", False),
        ("Charlie", False),
    ]
    assert all("replay" not in r for r in starts(out))


def test_the_replay_tap_goes_through_the_gate_and_is_sent(tmp_path, fake_adb, clock):
    out = tmp_path / "run"
    android = crawl_replay(fake_adb, clock, out, Policy())
    taps = [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]
    # Alpha twice (the second is the replay), then the rows of the sub-page.
    assert [screen for screen, _ in taps][:5] == ["main", "sub", "main", "sub", "sub"]
    assert taps[0] == taps[2]
    ends = {
        r["action_id"]: r
        for r in lines(out / "transitions.jsonl")
        if r["schema"] == "uiprobe.transition_end/1"
    }
    replayed = [r for r in starts(out) if r.get("replay")]
    assert len(replayed) == 1
    assert "not_sent" not in ends[replayed[0]["action_id"]]
