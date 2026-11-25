"""Tests for the crawl engine: exploring, the keyboard back and failed dumps."""

from __future__ import annotations

from tests.crawl.helpers import (
    PKG,
    POLICY,
    Node,
    Phone,
    back_engine,
    back_started,
    drive,
    engine,
    failed,
    make_screen,
    off_screen,
    on_screen,
    press,
    small_graph,
    started,
)
from uiprobe.crawl import (
    Back,
    Limits,
    Observation,
    Relaunch,
    Reread,
    Stop,
    Tap,
)
from uiprobe.guards import judge

# -- exploring -----------------------------------------------------------------


def test_visits_every_reachable_screen_and_ends_exhausted_on_the_start() -> None:
    phone = Phone(small_graph())
    eng = engine()
    log = drive(eng, phone)
    assert log[-1] == Stop("exhausted")
    assert {"n0", "n1", "n2", "n3", "perm"} <= set(phone.visited) | {"n0"}
    records = eng.records
    assert records.screens_seen == 4  # the permission screen is another package
    assert records.actions_sent == len(log) - 1
    start = eng.start_screen_id
    assert start is not None
    backs = [t for t in records.transitions if t.kind == "back"]
    assert backs
    assert all(t.from_screen_id != start for t in backs)
    assert all(t.to_screen_id is not None for t in records.transitions)
    assert [t.action_id for t in records.transitions] == list(
        range(1, records.actions_sent + 1)
    )
    assert records.transitions[0].kind == "launch"
    assert records.transitions[0].from_screen_id is None


def test_tap_follows_document_order_and_goes_depth_first() -> None:
    phone = Phone(small_graph())
    log = drive(engine(), phone)
    taps = [a for a in log if isinstance(a, Tap)]
    assert taps[0].element_id == "e2"  # the first button of the start screen
    assert phone.visited[:3] == ["n0", "n1", "n3"]  # down the first branch first


def test_never_taps_a_permission_screen_and_presses_back_there() -> None:
    phone = Phone(small_graph())
    log = drive(back_engine(), phone)
    assert "perm" in phone.visited
    i = phone.visited.index("perm")  # visited[k] is the result of log[k]
    assert isinstance(log[i + 1], Back)


def test_the_tap_point_is_the_one_judge_gave_not_the_centre_of_the_bounds() -> None:
    eng = started()
    narrow = on_screen(["Item a"], display=(200, 640))
    tap = eng.next(narrow)
    assert isinstance(tap, Tap)
    assert narrow.screen is not None
    judgement = judge(narrow.screen, PKG, POLICY, foreground=PKG)
    assert tap.point == judgement.pressable[0].point == (120, 80)
    assert tap.point != (160, 80)  # the centre of the bounds (40, 60, 280, 100)


def test_start_screen_with_nothing_to_try_is_exhausted_without_back() -> None:
    eng = started()
    assert eng.next(on_screen([])) == Stop("exhausted")
    assert eng.next(on_screen(["Item a"])) == Stop("exhausted")  # stays stopped
    assert eng.records.actions_sent == 1


def test_a_relaunch_that_lands_elsewhere_makes_that_screen_the_root() -> None:
    eng = back_started()
    press(eng, on_screen(["Item a"], name="n0"))
    alt = on_screen(["Item b", "Item c"], name="alt")
    press(eng, alt)  # alt is known at depth 2
    assert eng.depth_of(alt.screen.screen_id) == 2
    for expected in (Back(), Back(), Relaunch()):
        assert eng.next(off_screen()) == expected
    # the relaunch lands on alt: it is the root now, whatever depth it had
    second = press(eng, alt)
    assert second.screen_id == alt.screen.screen_id
    assert eng.depth_of(alt.screen.screen_id) == 1
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(alt) == Stop("exhausted")  # the root: no BACK, no loop
    assert eng.records.actions_sent == 1 + 1 + 1 + 3 + 1 + 1


def test_a_relaunch_landing_on_a_different_screen_in_a_phone_does_not_loop() -> None:
    class Elsewhere(Phone):
        def send(self, action):
            super().send(action)
            if isinstance(action, Relaunch):
                self.stack = ["alt"]
                self.visited[-1] = "alt"

    nodes = [
        Node("n0", (("Item a", "trap"),)),
        Node("trap"),
        Node("alt", (("Item a", "leaf"),)),
        Node("leaf"),
    ]

    class Leaving(Elsewhere):
        def send(self, action):
            before = list(self.stack)
            super().send(action)
            if isinstance(action, Back) and before == ["n0", "trap"]:
                self.stack = []  # this BACK leaves the app
                self.visited[-1] = "launcher"

    phone = Leaving(nodes)
    log = drive(engine(), phone)
    assert log[-1] == Stop("exhausted")
    assert "leaf" in phone.visited
    assert len(log) < 20


def test_start_screen_is_the_first_target_screen_after_off_target() -> None:
    eng = back_started()
    assert isinstance(eng.next(off_screen()), Back)
    first = eng.next(on_screen(["Item a"], name="late"))
    assert isinstance(first, Tap)
    assert eng.depth_of(first.screen_id) == 1
    assert eng.start_screen_id == first.screen_id


# -- keyboard ------------------------------------------------------------------


def test_keyboard_shown_is_back_once_then_keyboard_stuck() -> None:
    # Pinned reading: the BACK that hides the keyboard is not navigation, so it
    # may be sent on the start screen (see the module docstring).
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Stop("keyboard_stuck")


def test_the_keyboard_back_counts_as_an_action() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.actions_sent == 2
    eng = started(Limits(actions=2))
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(on_screen(["Item a"])) == Stop("max_actions")


def test_navigation_backs_do_not_count_toward_the_off_target_backs() -> None:
    eng = back_started()
    press(eng, on_screen(["Item a"]))
    assert eng.next(on_screen([], name="leaf")) == Back()  # navigation BACK
    assert [eng.next(off_screen()) for _ in range(3)] == [Back(), Back(), Relaunch()]


def test_the_keyboard_back_does_not_count_toward_the_off_target_backs() -> None:
    eng = back_started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert [eng.next(off_screen()) for _ in range(3)] == [Back(), Back(), Relaunch()]


def test_the_keyboard_back_keeps_the_path() -> None:
    eng = started(Limits(depth=2))
    press(eng, on_screen(["Item a"]))
    deep = on_screen(["Item a"], name="deep", keyboard=True)
    assert eng.next(deep) == Back()
    # still the second screen of the path: nothing is pressed at the limit
    assert eng.next(on_screen(["Item a"], name="deep")) == Back()


def test_keyboard_gone_after_back_continues() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    press(eng, on_screen(["Item a"]))


def test_keyboard_back_is_forgotten_after_a_good_reading() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    press(eng, on_screen(["Item a"]))
    assert eng.next(on_screen(["Item b"], name="two", keyboard=True)) == Back()


def test_unknown_keyboard_state_counts_as_not_shown() -> None:
    eng = started()
    screen = make_screen(["Item a"])
    press(eng, Observation(screen, PKG, None, 0.0))


def test_keyboard_stuck_survives_a_failed_dump_between() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(Observation(None, PKG, None, 1.0)) == Reread()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Stop("keyboard_stuck")


def test_failed_dumps_ask_for_a_reread_and_stop_at_three() -> None:
    eng = started()
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Stop("dump_failed_repeatedly")
    assert eng.records.actions_sent == 1  # a re-read sends nothing


def test_a_good_dump_resets_the_failure_count() -> None:
    eng = started()
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    press(eng, on_screen(["Item a"]))
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    press(eng, on_screen(["Item b"], name="two"))
    assert eng.stopped is None


def test_a_failed_dump_after_an_action_is_recorded_once_and_resolved_later() -> None:
    eng = started()
    tap = press(eng, on_screen(["Item a"]))
    assert eng.next(failed()) == Reread()
    assert eng.next(on_screen([], name="leaf")) == Back()
    first, second = eng.records.transitions[:2]
    assert (first.kind, first.to_screen_id, first.dump_failed) == (
        "launch",
        tap.screen_id,
        False,
    )
    assert second.kind == "tap"
    assert second.to_screen_id is None
    assert second.dump_failed is True
    assert eng.depth_of(make_screen([], name="leaf").screen_id) == 2  # a tap led there
