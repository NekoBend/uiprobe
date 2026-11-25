"""Tests for failed dumps, skip records, the up button and slider rows."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
)
from uiprobe.crawl import (
    Limits,
)
from uiprobe.crawl_driver import CrawlResult, crawl
from uiprobe.device import Device, FocusedWindow
from uiprobe.errors import (
    AdbError,
)
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_driver.helpers import (
    BUTTON,
    SERIAL,
    SHADE,
    SHOP,
    TRANSPORT,
    Clock,
    button,
    commands,
    end_lines,
    lines,
    run,
)


def test_a_failed_dump_is_recorded_and_read_again(tmp_path, device, android, clock):
    android.fail_dump(3)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "exhausted"
    ends = end_lines(out)
    failed = [e for e in ends if e["dump_failed"]]
    assert len(failed) == 1
    transitions = lines(out / "transitions.jsonl")
    ids = [t["action_id"] for t in transitions]
    assert ids == [i for n in range(1, result.actions + 1) for i in (n, n)]


def test_dumps_that_keep_failing_end_the_crawl(tmp_path, device, android, clock):
    android.fail_dump(3, 4, 5, 6, 7, 8)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "dump_failed_repeatedly"
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "dump_failed_repeatedly"


def test_an_adb_error_from_the_dump_is_not_a_failed_dump(
    tmp_path, device, monkeypatch, clock
):
    def broken() -> None:
        raise AdbError("gone")

    monkeypatch.setattr(device, "dump", broken)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"
    assert end_lines(out) == []


def test_skipped_rows_are_not_repeated(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out)
    rows = lines(out / "skipped.jsonl")
    keys = [(r["screen_id"], r["fingerprint"]) for r in rows]
    assert keys
    assert len(keys) == len(set(keys))


def test_the_row_of_a_screen_that_is_too_large_has_no_element():
    from uiprobe.crawl import SkippedElement
    from uiprobe.crawl_driver import _skipped_element

    item = SkippedElement("s-abc", "s-abc", ("screen_too_large",))
    assert _skipped_element(item, (), {}, package=SHOP, policy=Policy()) == {}


def test_a_skip_row_names_the_element_whose_reasons_match(tmp_path, fake_adb, clock):
    # Two identical unlabeled tiles share a fingerprint; the second one is
    # listed first in the dump but only the one at the top edge gets
    # screen_edge, so each row must name its own tile.
    screens = (
        Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
        Screen(
            "main",
            f"{SHOP}/.Main",
            (
                El((20, 200, 300, 240), cls=BUTTON, go="main"),
                El((20, 0, 300, 40), cls=BUTTON, go="main"),
            ),
        ),
    )
    FakeAndroid(fake_adb, SERIAL, screens, start="home", transport_id=TRANSPORT)
    out = tmp_path / "run"
    run(Device(adb_server=fake_adb.address), clock, out)
    rows = lines(out / "skipped.jsonl")
    by_reasons = {tuple(r["reasons"]): r for r in rows}
    assert set(by_reasons) == {("unlabeled", "screen_edge"), ("unlabeled",)}
    edge = by_reasons[("unlabeled", "screen_edge")]["element"]["bounds"]
    plain = by_reasons[("unlabeled",)]["element"]["bounds"]
    assert edge == [20, 0, 300, 40]
    assert plain == [20, 200, 300, 240]


def test_a_clock_that_returns_a_bad_value_leaves_no_folder(tmp_path, device, fake_adb):
    out = tmp_path / "run"
    bad = cast("Callable[[], float]", lambda: "x")
    with pytest.raises(TypeError, match="number of seconds"):
        crawl(
            device,
            SHOP,
            out,
            policy=Policy(),
            limits=Limits(),
            clock=bad,
            sleep=lambda _s: None,
        )
    assert not out.exists()
    assert not any(c.startswith(("monkey", "am ")) for c in commands(fake_adb))


# --- skip details: the matched word in skipped.jsonl ---

WORD_SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((0, 0, 320, 100), text="Shop"),
            button(200, "Browse", "warn"),
            button(260, "Delete account", "main"),
            button(320, "Automatically", "main"),
            El((20, 380, 300, 420), cls=BUTTON, go="main"),
        ),
    ),
    Screen(
        "warn",
        f"{SHOP}/.Warn",
        (
            El((0, 0, 320, 100), text="You can't undo this"),
            El((0, 100, 320, 150), text="Delete files?"),
            button(200, "Cancel", "main"),
            button(260, "OK", "main"),
        ),
    ),
)


def test_skip_lines_carry_the_details_of_word_reasons(tmp_path, fake_adb, clock):
    FakeAndroid(fake_adb, SERIAL, WORD_SCREENS, start="home", transport_id=TRANSPORT)
    out = tmp_path / "run"
    policy = Policy(deny_exceptions=("automatically",))
    run(Device(adb_server=fake_adb.address), clock, out, policy=policy)
    rows = lines(out / "skipped.jsonl")
    by_text = {r["element"].get("text", ""): r for r in rows}
    delete = by_text["Delete account"]
    assert delete["reasons"] == ["deny_word"]
    assert delete["details"] == [
        {
            "reason": "deny_word",
            "word": "delete",
            "field": "text",
            "element_id": delete["element"]["id"],
        }
    ]
    cancel = by_text["Cancel"]
    assert cancel["reasons"] == ["irreversible_screen"]
    assert [(d["reason"], d["word"], d["field"]) for d in cancel["details"]] == [
        ("irreversible_screen", "can't undo", "text")
    ]
    assert cancel["details"][0]["element_id"] != cancel["element"]["id"]
    ok = by_text["OK"]
    assert ok["reasons"] == ["irreversible_screen", "confirm_button"]
    assert [d["reason"] for d in ok["details"]] == [
        "irreversible_screen",
        "confirm_button",
    ]
    # The first deny word on the screen, "undo" in "You can't undo this".
    assert (ok["details"][1]["word"], ok["details"][1]["note"]) == ("undo", "ok")
    # Other reasons carry no details.
    unlabeled = by_text[""]
    assert unlabeled["reasons"] == ["unlabeled"]
    assert "details" not in unlabeled
    # The word excused by the exception is not refused and named nowhere.
    assert "Automatically" not in by_text
    assert all(d.get("word") != "call" for r in rows for d in r.get("details", []))


def test_the_result_names_the_owner_of_the_last_focus(
    tmp_path, device, clock, monkeypatch
):
    # A window of System UI holds the focus throughout (as a lock screen
    # would): the result says so, for the CLI's note.
    monkeypatch.setattr(device, "focused_window", lambda: SHADE)
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=4))
    assert result.last_focus == SHADE.owner == "com.android.systemui"


def test_the_result_names_the_app_as_the_last_focus_when_it_had_it(
    tmp_path, device, clock
):
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=4))
    assert result.last_focus == SHOP


@pytest.mark.parametrize("last_is_system_ui", [True, False])
def test_the_result_names_the_focus_of_the_last_observation_only(
    tmp_path, device, clock, monkeypatch, last_is_system_ui
):
    # The owner changes during the run: the result names the last one read,
    # not the first.
    app = FocusedWindow(SHOP, f"{SHOP}/.Main", "1a2b3c4")
    first, last = (app, SHADE) if last_is_system_ui else (SHADE, app)
    calls = [0]

    def focus() -> FocusedWindow:
        calls[0] += 1
        return first if calls[0] <= 2 else last

    monkeypatch.setattr(device, "focused_window", focus)
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=4))
    assert calls[0] > 2
    assert result.last_focus == last.owner


def test_skip_lines_name_the_row_reasons(tmp_path, fake_adb, clock, monkeypatch):
    from tests.crawl_dry.helpers import ROWS_SCREEN, rows_xml

    android = FakeAndroid(
        fake_adb, SERIAL, (ROWS_SCREEN,), start="rows", transport_id=TRANSPORT
    )
    monkeypatch.setattr(android, "render_xml", lambda: rows_xml(android))
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    run(device, clock, out, policy=Policy(skip_rows_with_buttons=True))
    rows = {r["element"]["id"]: r for r in lines(out / "skipped.jsonl")}
    assert rows["e2"]["reasons"] == ["two_target_row"]
    assert rows["e8"]["reasons"] == ["row_with_button"]
    # Not word reasons: the lines carry no details.
    assert "details" not in rows["e2"]
    assert "details" not in rows["e8"]
    # Exactly the gear (232..304 x 206..285) and the star (250..310 x
    # 310..370) were tapped; neither row.
    taps = {i.point for i in android.inputs if i.kind == "tap"}
    assert taps == {(268, 245), (280, 340)}


# --- the up button of a sub-page ---

UP = "android.widget.ImageButton"


def up_button() -> El:
    # The API 37 emulator's Settings records it at (16, 24, 72, 80), outside
    # the screen-edge areas; the fake renders its elements flat, so the rule
    # finds it here by its label.
    return El((16, 24, 72, 80), desc="Navigate up", cls=UP, go="back")


SUBPAGE_SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "top",
        f"{SHOP}/.Top",
        (
            El((0, 0, 320, 100), text="Settings"),
            button(200, "Network", "network"),
        ),
    ),
    Screen(
        "network",
        f"{SHOP}/.Network",
        (
            up_button(),
            El((80, 24, 300, 80), text="Network"),
            button(200, "Wi-Fi", "wifi"),
            button(260, "Mobile", "mobile"),
        ),
    ),
    Screen(
        "wifi",
        f"{SHOP}/.Wifi",
        (
            up_button(),
            El((80, 24, 300, 80), text="Wi-Fi"),
            button(200, "Saved", "saved"),
        ),
    ),
    Screen(
        "saved", f"{SHOP}/.Saved", (up_button(), El((80, 24, 300, 80), text="Saved"))
    ),
    Screen(
        "mobile", f"{SHOP}/.Mobile", (up_button(), El((80, 24, 300, 80), text="Mobile"))
    ),
)


def crawl_subpages(
    fake_adb: FakeAdbServer, clock: Clock, out: Path, policy: Policy
) -> tuple[FakeAndroid, CrawlResult]:
    android = FakeAndroid(
        fake_adb, SERIAL, SUBPAGE_SCREENS, start="home", transport_id=TRANSPORT
    )
    device = Device(adb_server=fake_adb.address)
    result = run(device, clock, out, limits=Limits(actions=12), policy=policy)
    return android, result


def test_the_rows_of_a_subpage_are_tapped_before_its_up_button(
    tmp_path, fake_adb, clock
):
    android, result = crawl_subpages(fake_adb, clock, tmp_path / "run", Policy())
    taps = [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]
    # Never the up button (center 44, 52): every row of every sub-page, the
    # deepest first.
    assert taps == [
        ("top", (160, 220)),
        ("network", (160, 220)),
        ("wifi", (160, 220)),
        ("network", (160, 280)),
    ]
    assert (result.stop_reason, result.screens) == ("exhausted", 5)
    rows = {r["element"]["id"]: r for r in lines(tmp_path / "run" / "skipped.jsonl")}
    assert {tuple(r["reasons"]) for r in rows.values()} == {("navigate_up",)}


def test_with_the_up_rule_off_the_crawl_presses_up_and_stays_shallow(
    tmp_path, fake_adb, clock
):
    android, result = crawl_subpages(
        fake_adb, clock, tmp_path / "run", Policy(skip_navigate_up=False)
    )
    taps = [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]
    # As before the rule: the up button is the first candidate of the sub-page,
    # so it is pressed at once. It lands on the parent, which the engine takes
    # for a deeper screen; its BACK there leaves the app, and after the
    # relaunch every candidate is already tried: no row of a sub-page is
    # pressed and only 2 screens are seen (5 above).
    assert taps == [("top", (160, 220)), ("network", (44, 52))]
    assert result.stop_reason == "exhausted"
    assert result.screens == 2


# --- a row with a slider ---

SLIDER_SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "sound",
        f"{SHOP}/.Sound",
        (
            El((0, 0, 320, 100), text="Sound"),
            # The fake renders its elements flat: the SeekBar is drawn first,
            # under the row, and the row's press point (160, 230) lies on it,
            # as on the phone where a tap on the row moved the slider.
            El(
                (60, 215, 320, 245),
                desc="Notification vibration",
                cls="android.widget.SeekBar",
                focusable=True,
            ),
            El(
                (0, 200, 320, 260),
                text="Vibration",
                cls="android.widget.LinearLayout",
                go="sound",
            ),
            button(300, "Ringtone", "ringtone"),
        ),
    ),
    Screen("ringtone", f"{SHOP}/.Ringtone", (El((0, 0, 320, 100), text="Ringtone"),)),
)


def crawl_sound(
    fake_adb: FakeAdbServer, clock: Clock, out: Path, policy: Policy
) -> FakeAndroid:
    android = FakeAndroid(
        fake_adb, SERIAL, SLIDER_SCREENS, start="home", transport_id=TRANSPORT
    )
    device = Device(adb_server=fake_adb.address)
    run(device, clock, out, limits=Limits(actions=12), policy=policy)
    return android


def test_a_row_whose_press_point_is_on_a_slider_is_not_tapped(
    tmp_path, fake_adb, clock
):
    android = crawl_sound(fake_adb, clock, tmp_path / "run", Policy())
    taps = [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]
    assert taps == [("sound", (160, 320))]
    rows = lines(tmp_path / "run" / "skipped.jsonl")
    assert [
        r["reasons"] for r in rows if r["element"]["bounds"] == [0, 200, 320, 260]
    ] == [["slider"]]


def test_with_the_slider_rule_off_the_slider_row_is_tapped(tmp_path, fake_adb, clock):
    android = crawl_sound(fake_adb, clock, tmp_path / "run", Policy(skip_sliders=False))
    taps = {(i.screen, i.point) for i in android.inputs if i.kind == "tap"}
    assert taps == {("sound", (160, 230)), ("sound", (160, 320))}
