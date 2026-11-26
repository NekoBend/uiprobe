"""Tests for the pressing crawl's scrolling: the gate, the send and the records."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import (
    SERIAL,
    SHOP,
    TRANSPORT,
    end_lines,
    lines,
)
from tests.crawl_driver.helpers import run as run_crawl
from tests.crawl_driver.scroll_screens import screens
from tests.fake_device import (
    El,
    FakeAndroid,
    assert_input_kinds,
    assert_inputs_only_in,
    assert_no_long_press,
    assert_no_text_input,
    assert_only_back_keys,
)
from uiprobe import crawl_run, crawl_tree
from uiprobe.crawl import CrawlEngine, Limits, Scroll
from uiprobe.crawl_tree import read_tree
from uiprobe.device import (
    PIN_NOT_PROVEN,
    SEND_STOPPED,
    Device,
)
from uiprobe.errors import DeviceNotFoundError
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

OVERLAY = "com.example.chat"
# The line on the fake list (window 0,100,320,600): see `scroll_screens`.
# The wheel point on the fake list (window 0,100,320,600): its center; the
# next point of the grid is the left quarter.
POINT = (160, 350)
NEXT = (80, 350)
ALL = Limits(actions=200)
# Scrolling is off by default; these tests turn it on.
ON = Policy(scroll_lists=True)


def run(*args, policy=None, **kwargs):
    return run_crawl(*args, policy=ON if policy is None else policy, **kwargs)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(
        fake_adb,
        SERIAL,
        screens(),
        start="home",
        transport_id=TRANSPORT,
        wheel=True,
    )


def starts(out: Path) -> list[dict]:
    return [
        r
        for r in lines(out / "transitions.jsonl")
        if r["schema"] == "uiprobe.transition_start/1"
    ]


def scrolls(out: Path) -> list[dict]:
    return [r for r in starts(out) if r["action"]["kind"] == "scroll"]


def wheels(android: FakeAndroid) -> list[tuple[str, ...]]:
    return [i.args for i in android.inputs if i.kind == "wheel"]


def tapped(out: Path) -> list[str]:
    return [
        r["action"]["element"]["text"]
        for r in starts(out)
        if r["action"]["kind"] == "tap"
    ]


# -- a long list is scrolled, pressed and recorded -------------------------------


def test_the_rows_below_the_fold_are_pressed_after_scrolling(
    tmp_path, device, android, clock
):
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL)
    assert result.stop_reason == "exhausted"
    assert tapped(out) == [f"Row {n:02d}" for n in range(1, 15)]
    assert len(scrolls(out)) == 3
    assert_input_kinds(android, {"tap", "key", "wheel"})
    assert_only_back_keys(android)
    assert_no_text_input(android)
    assert_no_long_press(android)
    assert_inputs_only_in(android, SHOP)


def test_the_wheel_is_turned_at_one_point_with_the_negative_amount(
    tmp_path, device, android, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert (
        wheels(android)
        == [("mouse", "scroll", "160", "350", "--axis", "VSCROLL,-3")] * 3
    )
    assert all(i.screen == "main" for i in android.inputs if i.kind == "wheel")
    # no swipe is ever sent, and no touch for a scroll
    assert not [i for i in android.inputs if i.kind == "swipe"]


def test_the_start_line_of_a_scroll_has_the_screen_and_both_points(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    first = scrolls(out)[0]
    assert first["action"] == {
        "kind": "scroll",
        "point": list(POINT),
        "amount": 3,
    }
    assert re.fullmatch(r"s-[0-9a-f]{12}", first["from_screen_id"])
    assert "element" not in first["action"]
    assert not {"replay", "restart", "off_target", "after_scroll"} & set(first)
    ends = {r["action_id"]: r for r in end_lines(out)}
    end = ends[first["action_id"]]
    assert end["to_screen_id"] == first["from_screen_id"]  # the list kept its id
    assert end["dump_failed"] is False


def test_a_scroll_is_one_action_with_its_own_id_and_count(
    tmp_path, device, android, clock
):
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL)
    ids = [r["action_id"] for r in starts(out)]
    assert ids == list(range(1, len(ids) + 1))
    assert result.actions == len(ids) == 1 + 14 * 2 + 3
    assert len(wheels(android)) == 3


def test_the_run_start_says_that_the_run_may_scroll(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3, scrolls_per_screen=7))
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["scroll_lists"] is True
    assert begin["scrolls_per_screen"] == 7
    assert begin["limits"] == {"depth": 5, "actions": 3, "minutes": 30}


def test_each_scroll_writes_the_screen_it_read(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    ids = [r["action_id"] for r in scrolls(out)]
    files = sorted(p.name for p in (out / "screens").iterdir())
    assert sorted(f for f in files if f.startswith("scroll-")) == sorted(
        f"scroll-{n}.json" for n in ids
    )
    after_first = json.loads((out / "screens" / f"scroll-{ids[0]}.json").read_bytes())
    texts = [e.get("text") for e in after_first["elements"] if e.get("text")]
    assert "Row 05" in texts
    assert "Row 01" not in texts
    assert after_first["schema"] == "uiprobe.screen/1"
    # the screen's own file is the first read of its id, not overwritten
    first_file = next(
        json.loads(p.read_bytes())
        for p in (out / "screens").glob("s-*.json")
        if "Row 01" in p.read_text("utf-8")
    )
    assert first_file["screen_id"] == after_first["screen_id"]


def test_the_summary_lists_the_rows_found_after_scrolling(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    summary = (out / "summary.txt").read_text("utf-8")
    labels = [line.strip() for line in summary.splitlines() if "Row " in line]
    assert labels == [f'"Row {n:02d}"' for n in range(1, 15)]


def test_the_tap_after_a_scroll_names_the_read_it_was_decided_on(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    by_text = {
        r["action"]["element"]["text"]: r
        for r in starts(out)
        if r["action"]["kind"] == "tap"
    }
    ids = [r["action_id"] for r in scrolls(out)]
    assert "after_scroll" not in by_text["Row 01"]
    assert "after_scroll" not in by_text["Row 08"]
    assert by_text["Row 09"]["after_scroll"] == ids[0]
    assert by_text["Row 12"]["after_scroll"] == ids[0]
    assert by_text["Row 13"]["after_scroll"] == ids[1]
    assert by_text["Row 14"]["after_scroll"] == ids[1]


def test_the_tree_lists_every_row_once_and_marks_the_scrolled_ones(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    tree = read_tree(out)
    assert tree.warnings == []
    assert tree.root is not None
    rows = [r for r in tree.screens[tree.root].rows if r.kind == "row"]
    assert [r.name for r in rows] == [f"Row {n:02d}" for n in range(1, 15)]
    assert all(r.pressed for r in rows)
    ids = [r["action_id"] for r in scrolls(out)]
    assert [r.after_scroll for r in rows] == [None] * 8 + [ids[0]] * 4 + [ids[1]] * 2
    assert all(r.to is not None and "screen_id" in r.to for r in rows)


def test_the_tree_as_seen_lists_what_each_read_showed(
    tmp_path, device, clock, monkeypatch
):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    monkeypatch.setattr(crawl_tree, "SCROLLED_REPEATS", "as_seen")
    tree = read_tree(out)
    assert tree.root is not None
    names = [r.name for r in tree.screens[tree.root].rows if r.kind == "row"]
    # the file's 8 rows, then the 8 rows of each of the three reads
    assert len(names) == 8 * 4
    assert names[:8] == [f"Row {n:02d}" for n in range(1, 9)]
    assert names[8:16] == [f"Row {n:02d}" for n in range(5, 13)]
    assert names[16:24] == names[24:32] == [f"Row {n:02d}" for n in range(7, 15)]


# -- the switch --------------------------------------------------------------------


def test_with_the_switch_off_nothing_is_swiped_and_nothing_is_added_to_the_records(
    tmp_path, device, android, clock
):
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL, policy=Policy(scroll_lists=False))
    assert result.stop_reason == "exhausted"
    assert tapped(out) == [f"Row {n:02d}" for n in range(1, 9)]
    assert wheels(android) == []
    assert [i.kind for i in android.inputs if i.kind not in {"tap", "key"}] == []
    assert not [p for p in (out / "screens").iterdir() if p.name.startswith("scroll")]
    begin = json.loads((out / "run-start.json").read_bytes())
    assert "scroll_lists" not in begin
    assert "scrolls_per_screen" not in begin
    assert not any(
        "after_scroll" in r or "end_point" in r.get("action", {}) for r in starts(out)
    )
    assert result.actions == 1 + 8 * 2


def test_the_cap_is_the_limit_of_the_run(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=200, scrolls_per_screen=1))
    assert len(scrolls(out)) == 1
    assert tapped(out) == [f"Row {n:02d}" for n in range(1, 13)]


def test_at_the_depth_limit_the_list_is_scrolled_and_nothing_is_pressed(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(depth=1, actions=200))
    assert tapped(out) == []
    assert len(scrolls(out)) == 3
    tree = read_tree(out)
    assert tree.root is not None
    names = [r.name for r in tree.screens[tree.root].rows if r.kind == "row"]
    assert names == [f"Row {n:02d}" for n in range(1, 15)]
    assert not any(r.pressed for r in tree.screens[tree.root].rows)


# -- the gate: the same checks as a tap, at the one point ---------------------------


def cover(android: FakeAndroid, region: tuple[int, int, int, int]) -> None:
    android.overlays.append(("ChatHead", OVERLAY, region))


def not_sent(out: Path) -> list[dict]:
    return [r for r in end_lines(out) if r.get("not_sent")]


def test_a_window_of_another_app_over_the_point_refuses_the_scroll(
    tmp_path, device, android, clock
):
    # a wheel event goes to the window under the pointer, so that window must
    # be the app's: this one takes touches at (160, 350), the wheel's point
    cover(android, (140, 340, 180, 360))
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL)
    assert wheels(android) == []
    refused = [r for r in not_sent(out) if r["reason"] == "covered_by_other"]
    assert len(refused) == 3  # the engine stops asking after three refusals
    assert refused[0]["details"] == {
        "window": "ChatHead",
        "owner_uid": android.uid_of(OVERLAY),
    }
    kinds = {r["action_id"]: r["action"]["kind"] for r in starts(out)}
    assert {kinds[r["action_id"]] for r in refused} == {"scroll"}
    assert result.stop_reason == "exhausted"
    assert result.not_sent == (("covered_by_other", 3),)
    assert tapped(out) == [f"Row {n:02d}" for n in range(1, 9)]


def test_a_window_elsewhere_does_not_stop_the_scroll(tmp_path, device, android, clock):
    cover(android, (140, 262, 180, 278))  # between rows, away from the point
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert len(wheels(android)) == 3
    assert not not_sent(out)


def test_the_window_check_reads_the_one_point(tmp_path, device, clock, monkeypatch):
    points: list[tuple[int, int]] = []
    original = device.window_at

    def spy(point, **kwargs):
        points.append(tuple(point))
        return original(point, **kwargs)

    monkeypatch.setattr(device, "window_at", spy)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert points.count(POINT) == 3  # once per scroll, nothing for a second point


def test_with_the_window_check_off_a_scroll_goes_out_unread(
    tmp_path, device, android, clock, fake_adb
):
    cover(android, (140, 340, 180, 360))
    out = tmp_path / "run"
    run(
        device,
        clock,
        out,
        limits=ALL,
        policy=Policy(scroll_lists=True, check_tap_window=False),
    )
    assert len(wheels(android)) == 3
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]


def test_a_keyboard_that_shows_refuses_the_scroll(
    tmp_path, device, android, clock, monkeypatch
):
    # The keyboard comes up between the engine's read and the gate's: the
    # first read of the keyboard after the engine handed out a scroll says so.
    armed = {"on": False, "done": False}
    chosen = CrawlEngine.next

    def next_(self, observation):
        action = chosen(self, observation)
        if isinstance(action, Scroll) and not armed["done"]:
            armed["on"] = True
        return action

    original = device.keyboard_shown

    def shown():
        if armed["on"]:
            armed.update(on=False, done=True)
            return True
        return original()

    monkeypatch.setattr(CrawlEngine, "next", next_)
    monkeypatch.setattr(device, "keyboard_shown", shown)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    # The refused scroll's fresh read shows the keyboard, so the engine asks
    # for the keyboard BACK; the keyboard is gone by then, the BACK is refused
    # (its screen changed), and a refused BACK ends the run.
    refused = not_sent(out)
    assert [r["reason"] for r in refused] == ["keyboard_shown", "changed_before_send"]
    kinds = {r["action_id"]: r["action"]["kind"] for r in starts(out)}
    assert [kinds[r["action_id"]] for r in refused] == ["scroll", "back"]
    assert wheels(android) == []


def test_a_text_that_changed_before_the_send_is_changed_before_press(
    tmp_path, device, android, clock, monkeypatch
):
    # between the engine's read and the gate's, a row of the list changes
    # its text (the layout, so the screen id, stays): nothing is sent
    chosen = CrawlEngine.next
    state = {"done": False}

    def next_(self, observation):
        action = chosen(self, observation)
        if isinstance(action, Scroll) and not state["done"]:
            state["done"] = True
            main = android._screens["main"]
            rows = main.scrolling.rows
            changed = (replace(rows[0], text="Changed"), *rows[1:])
            android._screens["main"] = replace(
                main, scrolling=replace(main.scrolling, rows=changed)
            )
        return action

    monkeypatch.setattr(CrawlEngine, "next", next_)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    refused = not_sent(out)
    assert refused
    assert refused[0]["reason"] == "changed_before_press"
    kinds = {r["action_id"]: r["action"]["kind"] for r in starts(out)}
    assert kinds[refused[0]["action_id"]] == "scroll"


def test_the_engine_releases_a_refused_scroll_and_the_crawl_goes_on(
    tmp_path, device, android, clock
):
    cover(android, (140, 340, 180, 360))
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL)
    assert result.stop_reason == "exhausted"
    assert result.actions == 1 + 8 * 2 + 3  # three refused start lines
    assert result.not_sent == (("covered_by_other", 3),)


# -- the send ------------------------------------------------------------------------


def test_the_wheel_goes_with_the_approved_epoch_and_the_deadline(
    tmp_path, device, clock, monkeypatch
):
    calls: list[tuple[tuple, dict]] = []
    original = device._wheel_scroll

    def wheel(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(device, "_wheel_scroll", wheel)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert len(calls) == 3
    for args, kwargs in calls:
        assert args == (POINT, 3)
        assert type(kwargs["_send_by"]) is float
        assert type(kwargs["_epoch"]) is int
        assert set(kwargs) == {"_send_by", "_epoch"}


def test_a_wheel_refused_for_the_pin_is_taken_back_and_sent_again(
    tmp_path, device, android, clock, monkeypatch
):
    original = device._wheel_scroll
    state = {"failed": False}

    def wheel(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise DeviceNotFoundError(
                "proven again", details={"serial": SERIAL, "reason": PIN_NOT_PROVEN}
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(device, "_wheel_scroll", wheel)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    refused = [r for r in not_sent(out) if r["reason"] == "device_reconnected"]
    assert len(refused) == 1
    assert len(wheels(android)) == 3  # the refused one was decided again
    assert tapped(out) == [f"Row {n:02d}" for n in range(1, 15)]
    kinds = [r["action"]["kind"] for r in starts(out)]
    assert kinds.count("scroll") == 4  # the refused start line and three sent


def test_a_wheel_that_comes_after_the_time_limit_is_not_sent(
    tmp_path, device, android, clock, monkeypatch
):
    def late(*_args, **_kwargs):
        raise DeviceNotFoundError(
            "late",
            details={"serial": SERIAL, "reason": SEND_STOPPED, "why": "time_limit"},
        )

    monkeypatch.setattr(device, "_wheel_scroll", late)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL)
    assert result.stop_reason == "max_duration"
    assert wheels(android) == []
    assert not_sent(out)[-1]["reason"] == "time_limit"


def test_the_end_of_the_list_is_the_end_of_the_scrolling(
    tmp_path, device, android, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert android.offsets["main"] == 320  # the last scroll moved it no more
    assert len(wheels(android)) == 3


def test_the_taps_after_a_scroll_pass_the_window_check_with_a_pointer_about(
    tmp_path, device, clock
):
    # A mouse pointer may stay on screen after a wheel turn. It is not a window
    # in the fake (nor in `dumpsys input` on the emulators checked), so the
    # check of the window at each tap's point is not affected: every tap after
    # the scrolls is sent.
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL)
    assert not not_sent(out)
    assert result.not_sent == ()
    assert tapped(out)[-2:] == ["Row 13", "Row 14"]


# -- the point -------------------------------------------------------------------------

REFUSING = [
    El((150, 340, 170, 360), cls="android.widget.SeekBar"),
    El((150, 340, 170, 360), cls="android.widget.NumberPicker"),
    El((150, 340, 170, 360), cls="android.widget.Switch", checkable=True),
    El((150, 340, 170, 360), cls="android.widget.EditText"),
    El((150, 340, 170, 360), cls="android.webkit.WebView"),
]


@pytest.mark.parametrize("element", REFUSING, ids=lambda e: e.cls)
def test_an_element_at_the_center_moves_the_wheel_to_the_next_point(
    tmp_path, fake_adb, clock, element
):
    shelf = FakeAndroid(
        fake_adb,
        SERIAL,
        screens(extra=(element,)),
        start="home",
        transport_id=TRANSPORT,
        wheel=True,
    )
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    # the first turn is at the next point; later turns, over rows that moved,
    # may find the grid refused elsewhere
    assert wheels(shelf)[0] == ("mouse", "scroll", "80", "350", "--axis", "VSCROLL,-3")


# -- whether the device can scroll ---------------------------------------------------


def usage_reads(fake_adb) -> int:
    return len([c for _, _, c in fake_adb.commands if c == "input"])


def test_the_usage_of_input_is_read_once_before_the_first_scroll(
    tmp_path, device, clock, fake_adb
):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert usage_reads(fake_adb) == 1
    commands = [c for _, _, c in fake_adb.commands]
    assert commands.index("input") < next(
        i for i, c in enumerate(commands) if c.startswith("input mouse scroll")
    )
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["scroll_input"] is True


def test_a_device_whose_usage_has_no_scroll_does_not_scroll(tmp_path, fake_adb, clock):
    old = FakeAndroid(
        fake_adb, SERIAL, screens(), start="home", transport_id=TRANSPORT, wheel=False
    )
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=ALL)
    assert result.stop_reason == "exhausted"
    assert wheels(old) == []
    assert tapped(out) == [f"Row {n:02d}" for n in range(1, 9)]
    assert usage_reads(fake_adb) == 1
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["scroll_lists"] is True
    assert begin["scroll_input"] is False
    assert not scrolls(out)


@pytest.mark.parametrize("usage", ["", "scroll <x> <y>\n", "mouse\n", "garbage\n"])
def test_a_usage_that_does_not_list_both_is_no_support(
    tmp_path, device, android, clock, usage
):
    android.input_usage = usage
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert wheels(android) == []
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["scroll_input"] is False


def test_a_usage_read_that_fails_is_no_support(
    tmp_path, device, android, clock, monkeypatch
):
    from uiprobe.errors import AdbError

    def fail(_command, **_kwargs):
        raise AdbError("no usage")

    original = device._shell_text

    def shell_text(command, **kwargs):
        if command == "input":
            return fail(command)
        return original(command, **kwargs)

    monkeypatch.setattr(device, "_shell_text", shell_text)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert wheels(android) == []
    assert json.loads((out / "run-start.json").read_bytes())["scroll_input"] is False


def test_with_the_switch_off_the_usage_is_not_read(tmp_path, device, clock, fake_adb):
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL, policy=Policy(scroll_lists=False))
    assert usage_reads(fake_adb) == 0


def test_a_caller_cannot_claim_support(tmp_path, device, android, fake_adb):
    import inspect

    assert "scroll_supported" not in inspect.signature(crawl_run).parameters
    with pytest.raises(TypeError, match="scroll_supported"):
        crawl_run(
            device,
            SHOP,
            tmp_path / "run",
            policy=Policy(),
            limits=ALL,
            scroll_supported=True,  # ty: ignore[unknown-argument]
        )
    assert wheels(android) == []
    assert not [c for _, _, c in fake_adb.commands if c.startswith("input mouse")]


def test_a_device_that_lists_only_the_horizontal_axis_does_not_scroll(
    tmp_path, device, android, clock
):
    android.input_usage = FakeAndroid.USAGE_OLD + (
        "      scroll <x> <y> [--axis HSCROLL,<h>] (Default: mouse)\n"
    )
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert wheels(android) == []
    assert json.loads((out / "run-start.json").read_bytes())["scroll_input"] is False


# -- what the wheel changed ------------------------------------------------------------


def with_switch(android, *, on_wheel):
    """The list screen with a switch, and a device whose wheel turn does
    `on_wheel(android)` as well as scrolling."""
    main = android._screens["main"]
    box = El((10, 10, 60, 40), cls="android.widget.Switch", checkable=True)
    android._screens["main"] = replace(main, elements=(*main.elements, box))
    original = android._wheeled

    def wheeled(x, y, amount):
        original(x, y, amount)
        on_wheel(android)

    android._wheeled = wheeled


def test_a_wheel_that_flipped_a_switch_is_marked_and_scrolling_stops_there(
    tmp_path, device, android, clock
):
    def flip(a):
        main = a._screens["main"]
        flipped = tuple(
            replace(e, checked=True) if e.checkable else e for e in main.elements
        )
        a._screens["main"] = replace(main, elements=flipped)

    with_switch(android, on_wheel=flip)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    assert len(wheels(android)) == 1  # never again on that screen
    ends = {r["action_id"]: r for r in end_lines(out)}
    scroll = scrolls(out)[0]
    assert ends[scroll["action_id"]]["scroll_effect"] == "changed"
    assert [r["action_id"] for r in end_lines(out) if "scroll_effect" in r] == [
        scroll["action_id"]
    ]
    assert read_tree(out).warnings == []


def test_a_wheel_that_opened_another_screen_is_marked_as_navigation(
    tmp_path, device, android, clock
):
    with_switch(android, on_wheel=lambda a: a.show("leaf"))
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    ends = {r["action_id"]: r for r in end_lines(out)}
    scroll = scrolls(out)[0]
    assert ends[scroll["action_id"]]["scroll_effect"] == "navigation"
    assert len(wheels(android)) == 1


# -- the read after a scroll survives a failed dump ------------------------------------


def test_the_screen_after_a_scroll_is_kept_when_the_first_dump_fails(
    tmp_path, device, clock, monkeypatch
):
    from uiprobe.errors import DumpError

    state = {"armed": False}
    original_wheel = device._wheel_scroll
    original_dump = device.dump

    def wheel(*args, **kwargs):
        result = original_wheel(*args, **kwargs)
        state["armed"] = True
        return result

    def dump(*args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise DumpError("one dump fails")
        return original_dump(*args, **kwargs)

    monkeypatch.setattr(device, "_wheel_scroll", wheel)
    monkeypatch.setattr(device, "dump", dump)
    out = tmp_path / "run"
    run(device, clock, out, limits=ALL)
    ids = [r["action_id"] for r in scrolls(out)]
    files = sorted(
        p.name for p in (out / "screens").iterdir() if p.name.startswith("scroll-")
    )
    assert files == sorted(f"scroll-{n}.json" for n in ids)
    first = json.loads((out / "screens" / f"scroll-{ids[0]}.json").read_bytes())
    assert "Row 05" in [e.get("text") for e in first["elements"]]
    tree = read_tree(out)
    assert tree.root is not None
    rows = tree.screens[tree.root].rows
    assert [r.name for r in rows if r.kind == "row"] == [
        f"Row {n:02d}" for n in range(1, 15)
    ]
