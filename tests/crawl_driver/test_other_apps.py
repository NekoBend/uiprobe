"""Tests for crawling when a screen of another app is in front."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from uiprobe.crawl import (
    Back,
    Limits,
    Relaunch,
)
from uiprobe.device import FocusedWindow

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_driver.helpers import (
    BACK_POLICY,
    OTHER,
    SHADE,
    SHOP,
    _gate_run,
    _not_sent_reason,
    lines,
    run,
)

# --- screens of other apps ---


def _launch_under(monkeypatch, device, android, *names):
    """Make every launch end with `names` (screens of another app) in front."""
    original_launch = device._launch_component

    def launch_then_cover(*args, **kwargs):
        original_launch(*args, **kwargs)
        for name in names:
            android.show(name)

    monkeypatch.setattr(device, "_launch_component", launch_then_cover)


def _inputs_sent(fake_adb: FakeAdbServer) -> list[str]:
    return [r for r in fake_adb.requests if "input " in r]


def test_by_default_a_screen_of_another_app_gets_no_input(
    tmp_path, device, android, fake_adb, clock, monkeypatch
):
    _launch_under(monkeypatch, device, android, "ext1")
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "relaunch_limit"
    # not one input request reached the adb server, only relaunches of the app
    assert _inputs_sent(fake_adb) == []
    assert android.inputs == []
    assert android.stopped == [SHOP] * 4  # the first launch and 3 relaunches
    records = lines(out / "transitions.jsonl")
    starts = [r for r in records if "action" in r]
    assert [r["action"]["kind"] for r in starts] == [
        "launch",
        "relaunch",
        "relaunch",
        "relaunch",
    ]
    assert "off_target" not in starts[0]
    assert starts[0]["restart"] is True
    assert not any("restart" in start for start in starts[1:])
    for start in starts[1:]:
        assert start["off_target"] == {
            "package": OTHER,
            "foreground": OTHER,
            "focus": OTHER,
        }
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["back_on_other_screens"] is False


def test_with_back_on_other_screens_back_goes_to_the_other_app(
    tmp_path, device, android, fake_adb, clock, monkeypatch
):
    # Three screens of the other app over the shop: two BACKs leave one, so
    # every round is BACK, BACK, relaunch, as before the choice existed.
    _launch_under(monkeypatch, device, android, "ext1", "ext2", "ext3")
    out = tmp_path / "run"
    result = run(device, clock, out, policy=BACK_POLICY)
    assert result.stop_reason == "relaunch_limit"
    keys = [i for i in android.inputs if i.kind == "key"]
    assert len(keys) == 2 * 4
    assert {i.screen for i in keys} == {"ext3", "ext2"}
    assert len(_inputs_sent(fake_adb)) == 2 * 4
    assert android.stopped == [SHOP] * 4  # the first launch and 3 relaunches
    records = lines(out / "transitions.jsonl")
    starts = [r for r in records if "action" in r]
    assert [r["action"]["kind"] for r in starts] == [
        "launch",
        *(["back", "back", "relaunch"] * 3),
        "back",
        "back",
    ]
    for start in starts[1:]:
        assert start["off_target"] == {
            "package": OTHER,
            "foreground": OTHER,
            "focus": OTHER,
        }
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["back_on_other_screens"] is True


def test_a_back_that_leaves_the_app_is_followed_by_a_relaunch(
    tmp_path, device, android, clock, monkeypatch
):
    # Browse replaces the shop's task, so it is the bottom of the real stack
    # while the engine counts it one above the root: its navigation BACK
    # leaves the app for the launcher. By default the launcher gets nothing.
    original_tap = device.tap

    def tap_then_replace(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original_tap(point, _send_by=_send_by, _epoch=_epoch)
        if android.screen.name == "browse":
            android.stack[:] = ["home", "browse"]
        return result

    monkeypatch.setattr(device, "tap", tap_then_replace)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=12))
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    on_launcher = [
        i for i, r in enumerate(starts) if r.get("off_target", {}).get("package")
    ]
    assert on_launcher  # the BACK left the app
    first = on_launcher[0]
    assert starts[first - 1]["action"]["kind"] == "back"
    for i in on_launcher:
        assert starts[i]["action"]["kind"] == "relaunch"
        assert starts[i]["off_target"] == {
            "package": "com.example.launcher",
            "foreground": "com.example.launcher",
            "focus": "com.example.launcher",
        }
    # every key went to a screen of the shop, none to the launcher
    keys = [i for i in android.inputs if i.kind == "key"]
    assert keys
    assert all(i.screen in {"main", "browse", "detail", "cart"} for i in keys)


def _start_lines(tmp_path: Path) -> list[dict[str, object]]:
    return [r for r in lines(tmp_path / "run" / "transitions.jsonl") if "action" in r]


def test_a_back_on_the_apps_screen_is_not_sent_while_another_window_has_focus(
    tmp_path, device, android, clock, monkeypatch
):
    # The dump and the foreground name the app, but a window of System UI has
    # held the input focus since before the BACK was chosen: the BACK would go
    # to that window, so it is not sent.
    monkeypatch.setattr(device, "focused_window", lambda: SHADE)
    driver, _, observation = _gate_run(tmp_path, device, clock)
    assert observation.foreground == SHOP
    inputs = len(android.inputs)
    driver.send(Back(), observation)
    assert len(android.inputs) == inputs
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert "off_target" not in _start_lines(tmp_path)[-1]


THIRD = FocusedWindow("com.example.third", "com.example.third/.Pop", "5d6e7f8")


@pytest.mark.parametrize("focus_moves", [False, True])
def test_an_opted_in_back_on_another_apps_screen_needs_no_app_focus(
    tmp_path, device, android, clock, monkeypatch, focus_moves
):
    # Dump, foreground and focus name three different apps: the start line
    # must take the focus owner from the focused window, where a BACK goes.
    driver, _, _ = _gate_run(tmp_path, device, clock, policy=BACK_POLICY)
    android.show("ext1")
    monkeypatch.setattr(device, "focused_window", lambda: THIRD)
    observation = driver.observe()
    if focus_moves:
        monkeypatch.setattr(device, "focused_window", lambda: SHADE)
    keys = len([i for i in android.inputs if i.kind == "key"])
    driver.send(Back(), observation)
    sent = len([i for i in android.inputs if i.kind == "key"]) - keys
    assert _start_lines(tmp_path)[-1]["off_target"] == {
        "package": OTHER,
        "foreground": OTHER,
        "focus": "com.example.third",
    }
    if focus_moves:
        assert sent == 0
        assert _not_sent_reason(tmp_path) == "changed_before_send"
    else:
        assert sent == 1
        assert _not_sent_reason(tmp_path) is None
    begin = json.loads((tmp_path / "run" / "run-start.json").read_bytes())
    assert begin["back_on_other_screens"] is True


def test_off_target_names_the_dump_and_the_foreground_apart(
    tmp_path, device, android, clock, monkeypatch
):
    # The dump shows the shop while another app is said to be in front.
    driver, _, _ = _gate_run(tmp_path, device, clock)
    monkeypatch.setattr(device, "foreground", lambda: OTHER)
    observation = driver.observe()
    assert observation.screen is not None
    assert observation.screen.package == SHOP
    driver.send(Relaunch(), observation)
    assert _start_lines(tmp_path)[-1]["off_target"] == {
        "package": SHOP,
        "foreground": OTHER,
        "focus": SHOP,
    }
    assert android.stopped == [SHOP]


def test_off_target_with_an_unknown_foreground_has_no_foreground(
    tmp_path, device, android, clock, monkeypatch
):
    driver, _, _ = _gate_run(tmp_path, device, clock)
    monkeypatch.setattr(device, "foreground", lambda: None)
    observation = driver.observe()
    driver.send(Relaunch(), observation)
    assert _start_lines(tmp_path)[-1]["off_target"] == {
        "package": SHOP,
        "focus": SHOP,
    }
    # the gate cannot read the foreground, so nothing was force-stopped
    assert _not_sent_reason(tmp_path) == "foreground_unknown"
    assert android.stopped == []


def test_a_relaunch_decided_on_the_apps_screen_has_no_off_target(
    tmp_path, device, android, clock
):
    driver, _, observation = _gate_run(tmp_path, device, clock)
    driver.send(Relaunch(), observation)
    assert "off_target" not in _start_lines(tmp_path)[-1]
    assert android.stopped == [SHOP]


# --- the packages the run landed on ---


def test_a_relaunch_limit_counts_every_landing_on_another_app(
    tmp_path, device, android, clock, monkeypatch
):
    _launch_under(monkeypatch, device, android, "ext1")
    result = run(device, clock, tmp_path / "run")
    assert result.stop_reason == "relaunch_limit"
    # 3 relaunches, each decided on the other app's screen, and the stop on the 4th
    assert result.other_apps == ((OTHER, 4),)


def test_an_exhausted_run_has_no_other_apps(tmp_path, device, clock):
    result = run(device, clock, tmp_path / "run")
    assert result.other_apps == ()


def test_landings_are_ranked_by_count_then_first_seen():
    from uiprobe.crawl_driver.driver import _ranked

    counts = {"b.first": 1, "a.second": 3, "c.third": 1, "d.fourth": 3}
    assert _ranked(counts) == (
        ("a.second", 3),
        ("d.fourth", 3),
        ("b.first", 1),
        ("c.third", 1),
    )
    assert _ranked({}) == ()
