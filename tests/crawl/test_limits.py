"""Tests for the crawl engine: limits, depth, tried marks and skipped records."""

from __future__ import annotations

from typing import Any

import pytest

from tests.crawl.helpers import (
    DISPLAY,
    PERMISSION,
    PKG,
    POLICY,
    ROW,
    Node,
    Phone,
    back_started,
    clone,
    drive,
    engine,
    failed,
    make_screen,
    off_screen,
    on_screen,
    press,
    seen,
    started,
)
from uiprobe import crawl
from uiprobe.crawl import (
    Back,
    CrawlEngine,
    Limits,
    Observation,
    Relaunch,
    Reread,
    Stop,
    Tap,
    fingerprint_of,
)
from uiprobe.model import Bounds, Element, Screen

# The classes as untyped callables, to pass arguments of a wrong type on purpose.
LooseLimits: Any = Limits


# -- limits --------------------------------------------------------------------


def test_limits_validation() -> None:
    assert Limits() == Limits(depth=5, actions=300, minutes=30)
    for bad in (0, -1):
        for name in ("depth", "actions", "minutes"):
            with pytest.raises(ValueError, match="at least 1"):
                LooseLimits(**{name: bad})
    for bad in (True, False, 1.0, "3", None):
        for name in ("depth", "actions", "minutes"):
            with pytest.raises(TypeError):
                LooseLimits(**{name: bad})


def test_max_actions_counts_launch_taps_backs_and_relaunches() -> None:
    eng = back_started(Limits(actions=4))
    assert eng.next(off_screen()) == Back()  # 2
    assert eng.next(off_screen()) == Back()  # 3
    assert eng.next(off_screen()) == Relaunch()  # 4
    assert eng.next(off_screen()) == Stop("max_actions")
    eng = back_started(Limits(actions=3))
    press(eng, on_screen(["Item a", "Item b"]))  # 2
    press(eng, on_screen(["Item a"], name="two"))  # 3
    assert eng.next(on_screen(["Item a"], name="three")) == Stop("max_actions")


def test_the_launch_alone_can_use_the_action_limit() -> None:
    eng = started(Limits(actions=1))
    assert eng.next(on_screen(["Item a"])) == Stop("max_actions")


def test_failed_dumps_do_not_count_as_actions() -> None:
    eng = started(Limits(actions=2))
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    press(eng, on_screen(["Item a"]))  # the 2nd action


def test_max_duration_uses_the_clock_in_the_observation() -> None:
    eng = started(Limits(minutes=2), started_at=1000.0)
    press(eng, on_screen(["Item a"], now=1119.9))
    assert eng.next(on_screen(["Item a"], name="two", now=1120.0)) == Stop(
        "max_duration"
    )


def test_max_duration_applies_to_a_failed_dump_too() -> None:
    eng = started(Limits(minutes=1), started_at=5.0)
    assert eng.next(failed(now=65.0)) == Stop("max_duration")


def test_depth_limits_each_path_only() -> None:
    nodes = [
        Node("n0", (("Item a", "c1"), ("Item b", "leaf"))),
        Node("c1", (("Item a", "c2"),)),
        Node("c2", (("Item a", "c3"),)),
        Node("c3", (("Item a", "c4"),)),
        Node("c4", (("Item a", "c5"),)),
        Node("c5", (("Item a", "c6"),)),
        Node("c6", (("Item a", "c7"),)),
        Node("c7"),
        Node("leaf"),
    ]
    phone = Phone(nodes)
    eng = engine(Limits(depth=5))
    log = drive(eng, phone)
    assert log[-1] == Stop("exhausted")
    assert "c4" in phone.visited  # the fifth screen of the path
    assert "c5" not in phone.visited
    assert "leaf" in phone.visited  # the crawl went on elsewhere
    assert eng.depth_of(phone.screen_id("c4")) == 5
    assert eng.depth_of(phone.screen_id("c5")) is None
    taps = [a for a in log if isinstance(a, Tap)]
    assert phone.screen_id("c4") not in {t.screen_id for t in taps}


def test_depth_limit_holds_for_the_real_path_through_a_shorter_known_depth() -> None:
    # "a2" is known at depth 2, but reaching it again from "x" (depth 4) is a
    # path of 5 screens: nothing may be pressed there.
    nodes = [
        Node("n0", (("Item a", "a2"),)),
        Node("a2", (("Item a", "b"), ("Item b", "c"))),
        Node("b", (("Item a", "x"),)),
        Node("x", (("Item a", "a2"),)),
        Node("c", (("Item a", "d"),)),
        Node("d", (("Item a", "e"),)),
        Node("e", (("Item a", "f"),)),
        Node("f"),
    ]

    class Tracking(Phone):
        deepest = 0

        def send(self, action):
            super().send(action)
            self.deepest = max(self.deepest, len(self.stack))

    phone = Tracking(nodes)
    log = drive(engine(Limits(depth=5)), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert phone.deepest <= 5


def test_depth_numbers_follow_the_path() -> None:
    nodes = [
        Node("n0", (("Item a", "n1"),)),
        Node("n1", (("Item a", "n2"),)),
        Node("n2"),
    ]
    phone = Phone(nodes)
    eng = engine()
    drive(eng, phone)
    depths = {name: eng.depth_of(phone.screen_id(name)) for name in ("n0", "n1", "n2")}
    assert depths == {"n0": 1, "n1": 2, "n2": 3}


def test_a_screen_with_unknown_depth_gets_the_limit_and_nothing_is_pressed() -> None:
    eng = started(Limits(depth=3))
    press(eng, on_screen(["Item a"]))
    assert eng.next(on_screen([], name="leaf")) == Back()
    stranger = on_screen(["Item a", "Item b"], name="stranger")
    assert eng.next(stranger) == Back()
    assert eng.depth_of(stranger.screen.screen_id) == 3


def test_the_smallest_known_depth_is_kept() -> None:
    eng = started(Limits(depth=5))
    press(eng, on_screen(["Item a", "Item b"]))
    mid = on_screen(["Item a", "Item b"], name="mid")
    press(eng, mid)  # mid is depth 2
    far = on_screen([], name="far")
    assert eng.next(far) == Back()  # far is depth 3
    assert eng.depth_of(far.screen.screen_id) == 3
    press(eng, mid)  # back on mid: the second button
    assert eng.next(far) == Back()
    assert eng.depth_of(far.screen.screen_id) == 3
    assert eng.next(mid) == Back()
    start = on_screen(["Item a", "Item b"])
    press(eng, start)  # the start screen's second button
    assert eng.next(far) == Back()  # reached from depth 1: far is now depth 2
    assert eng.depth_of(far.screen.screen_id) == 2


def test_dialogs_later_do_not_raise_the_known_depth_of_a_screen() -> None:
    eng = back_started(Limits(depth=5))
    root = on_screen(["Item a", "Item b"], name="root")
    press(eng, root)
    shared = on_screen([], name="shared")
    assert eng.next(shared) == Back()  # shared is depth 2
    assert eng.depth_of(shared.screen.screen_id) == 2
    press(eng, root)  # the second button
    mid = on_screen(["Item a"], name="mid")
    press(eng, mid)  # mid is depth 2
    # the tap on mid opens shared with two dialogs over it: real length 3
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    assert eng.next(shared) == Back()
    assert eng.depth_of(shared.screen.screen_id) == 2  # not 3


# -- tried marks ---------------------------------------------------------------


def test_a_list_that_changes_text_keeps_different_rows_distinct() -> None:
    eng = started()

    def rows(*labels):
        return on_screen(list(labels), name="list", rid="rows:id/row", cls=ROW)

    first = press(eng, rows("Alpha", "Beta"))
    assert first.element_id == "e2"
    assert eng.next(on_screen([], name="leaf")) == Back()
    second = press(eng, rows("Beta", "Gamma"))  # same screen id, Beta moved up
    assert second.element_id == "e2"  # the same element id is not "tried"
    assert second.fingerprint != first.fingerprint
    assert eng.next(on_screen([], name="leaf")) == Back()
    third = press(eng, rows("Beta", "Gamma"))
    assert third.element_id == "e3"
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(rows("Beta", "Gamma")) == Stop("exhausted")


def test_two_pressable_elements_with_one_fingerprint_try_only_the_first() -> None:
    eng = started()

    def twin():
        return on_screen(["Same", "Same"], rid="x:id/same")

    first = press(eng, twin())
    assert first.element_id == "e2"
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(twin()) == Stop("exhausted")


def test_the_fingerprint_ignores_position_and_element_id_but_not_the_ancestors() -> (
    None
):
    screen = make_screen(["Item a"])
    moved = make_screen(["Item a"])
    element = screen.elements[1]
    shifted = clone(element, id="e9", bounds=Bounds(41, 7, 99, 99))
    assert fingerprint_of(screen, element) == fingerprint_of(moved, moved.elements[1])
    assert fingerprint_of(screen, element) == fingerprint_of(moved, shifted)
    other_parent = Element(
        "e1", None, "android.widget.LinearLayout", Bounds(0, 0, *DISPLAY), package=PKG
    )
    nested = Screen(
        screen.serial,
        screen.captured_at,
        screen.package,
        screen.activity,
        screen.api_level,
        screen.display,
        (other_parent, element),
    )
    assert fingerprint_of(nested, element) != fingerprint_of(screen, element)
    same_class = clone(other_parent, class_name=screen.elements[0].class_name)
    by_id = clone(same_class, resource_id="x:id/other_parent")
    for parent in (same_class, by_id):
        variant = Screen(
            screen.serial,
            screen.captured_at,
            screen.package,
            screen.activity,
            screen.api_level,
            screen.display,
            (parent, element),
        )
        assert (
            fingerprint_of(variant, element) == fingerprint_of(screen, element)
        ) == (parent is same_class)
    for field in ("text", "resource_id", "content_desc", "hint", "class_name"):
        changed = clone(element, **{field: "different"})
        assert fingerprint_of(screen, changed) != fingerprint_of(screen, element)


def test_fingerprints_cannot_be_confused_by_moving_text_between_fields() -> None:
    first = make_screen(["ab"])
    second = make_screen(["a"])
    left = first.elements[1]
    right = clone(left, text="a", content_desc="b")
    assert fingerprint_of(first, left) != fingerprint_of(second, right)


# -- skipped records -----------------------------------------------------------


def test_skipped_elements_are_recorded_once_with_their_reasons() -> None:
    eng = started()
    screen = on_screen(["Delete", "Item a"])
    press(eng, screen)
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(screen) == Stop("exhausted")
    skipped = eng.records.skipped
    assert len(skipped) == 1
    assert "deny_word" in skipped[0].reasons
    assert skipped[0].screen_id == screen.screen.screen_id
    assert skipped[0].fingerprint == fingerprint_of(
        screen.screen, screen.screen.elements[1]
    )


def _timed(minutes=1):
    eng = started(Limits(minutes=minutes))
    first = on_screen(["Item a"], now=59.0)
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    return eng, first, tap


def test_recheck_refuses_at_the_deadline_and_cancels_the_tap() -> None:
    eng, first, tap = _timed()
    at_deadline = on_screen(["Item a"], now=60.0)
    assert not eng.recheck(tap, at_deadline)
    assert eng.actions_sent == 1
    assert eng.records.skipped == ()
    assert [t.kind for t in eng.records.transitions] == ["launch"]
    assert eng.next(first) == Stop("max_duration")  # even on an earlier clock


def test_recheck_just_before_the_deadline_still_allows_the_tap() -> None:
    eng, _first, tap = _timed()
    assert eng.recheck(tap, on_screen(["Item a"], now=59.5))
    eng.tap_sent(tap)
    assert eng.actions_sent == 2


def test_a_deadline_cancelled_tap_is_not_marked_tried() -> None:
    eng, _first, tap = _timed()
    assert not eng.recheck(tap, on_screen(["Item a"], now=61.0))
    assert (tap.screen_id, tap.fingerprint) not in eng._tried


def test_a_huge_clock_value_is_refused_not_raised() -> None:
    with pytest.raises(ValueError, match="below 2"):
        Observation(None, PKG, keyboard_shown=False, now=10**400)
    with pytest.raises(ValueError, match="below 2"):
        CrawlEngine(PKG, POLICY, Limits(), started_at=10**400)


@pytest.mark.parametrize("value", [-1, -0.5, 2**40, float(2**60), 2**60 - 60])
def test_a_clock_outside_the_exact_range_is_refused(value) -> None:
    with pytest.raises(ValueError, match="below 2"):
        Observation(None, PKG, keyboard_shown=False, now=value)
    with pytest.raises(ValueError, match="below 2"):
        CrawlEngine(PKG, POLICY, Limits(), started_at=value)


def test_the_deadline_is_exact_near_the_top_of_the_clock_range() -> None:
    start = 2**40 - 61
    eng = started(Limits(minutes=1), started_at=start)
    screen = make_screen(["Item a"])
    tap = eng.next(seen(screen, now=start + 59.999))
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, seen(screen, now=float(start + 60)))
    assert eng.next(seen(screen, now=float(start + 60))) == Stop("max_duration")


def test_the_depth_limit_has_an_upper_bound() -> None:
    assert Limits(depth=crawl.MAX_DEPTH).depth == crawl.MAX_DEPTH
    with pytest.raises(ValueError, match="at most"):
        Limits(depth=crawl.MAX_DEPTH + 1)
    with pytest.raises(ValueError, match="at most"):
        Limits(depth=10**30)
    # an unpredicted landing at the largest depth still works
    eng = started(Limits(depth=crawl.MAX_DEPTH))
    root = on_screen(["Go"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    eng.tap_sent(go)
    assert eng.next(on_screen([], name="X")) == Back()
    assert isinstance(eng.next(on_screen(["Z"], name="Z")), Back | Tap)


def test_an_oversized_screen_gets_one_skip_row_and_nothing_is_pressed() -> None:
    eng = started()
    big = on_screen([f"B{i}" for i in range(5001)], name="big")
    assert eng.next(big) == Stop("exhausted")
    rows = eng.records.skipped
    assert len(rows) == 1
    assert rows[0].reasons == ("screen_too_large",)
    assert rows[0].screen_id == big.screen.screen_id
