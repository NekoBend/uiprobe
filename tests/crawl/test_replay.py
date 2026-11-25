"""Tests for the path replay and the badge-free tried marks of the crawl engine."""

from __future__ import annotations

from dataclasses import asdict, fields

from tests.crawl.helpers import (
    OTHER,
    PKG,
    Node,
    Phone,
    clone,
    drive,
    engine,
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
    Relaunch,
    Stop,
    Tap,
    Transition,
    fingerprint_of,
)
from uiprobe.guards import MAX_ELEMENTS, Policy
from uiprobe.model import Bounds, Element, Screen

OFF = Policy(replay_path=False)
TOP = ["A", "B", "C"]
SUB = ["a1", "a2", "a3"]


# -- the run2 shape on a phone made of a graph ----------------------------------


class LoggingPhone(Phone):
    """A phone that logs (screen, label, replay) for every tap it is sent."""

    eng: CrawlEngine

    def __init__(self, nodes, start="n0"):
        super().__init__(nodes, start)
        self.pressed: list[tuple[str, str, bool]] = []

    def send(self, action):
        if isinstance(action, Tap):
            node = self.current()
            index = (action.point[1] - 60) // 60
            self.pressed.append(
                (node.name, node.buttons[index][0], self.eng.is_replay(action))
            )
        super().send(action)


def run2_nodes():
    """Top rows A, B, C; A opens a sub-page whose a1 leaves the app."""
    return [
        Node("n0", (("A", "sub"), ("B", "leafb"), ("C", "leafc"))),
        Node("sub", (("a1", "ext"), ("a2", "leaf2"), ("a3", "leaf3"))),
        Node("leafb"),
        Node("leafc"),
        Node("leaf2"),
        Node("leaf3"),
        Node("ext", package=OTHER),
    ]


def crawl_run2(policy, limits=None, nodes=None):
    eng = engine(limits, policy=policy)
    phone = LoggingPhone(nodes or run2_nodes())
    phone.eng = eng
    log = drive(eng, phone, policy=policy, depth=(limits or Limits()).depth)
    return eng, phone, log


def test_run2_shape_with_replay_presses_a2_and_a3_after_the_relaunch() -> None:
    eng, phone, log = crawl_run2(Policy())
    assert phone.pressed == [
        ("n0", "A", False),
        ("sub", "a1", False),  # leaves the app: a relaunch
        ("n0", "A", True),  # the way back down
        ("sub", "a2", False),
        ("sub", "a3", False),
        ("n0", "B", False),
        ("n0", "C", False),
    ]
    assert sum(isinstance(a, Relaunch) for a in log) == 1
    assert isinstance(log[-1], Stop)
    assert log[-1].reason == "exhausted"
    replays = [t for t in eng.records.transitions if t.action_id in eng.replay_actions]
    assert [t.kind for t in replays] == ["tap"]


def test_run2_shape_without_replay_never_presses_a2_or_a3() -> None:
    _eng, phone, log = crawl_run2(OFF)
    assert phone.pressed == [
        ("n0", "A", False),
        ("sub", "a1", False),
        ("n0", "B", False),
        ("n0", "C", False),
    ]
    assert sum(isinstance(a, Relaunch) for a in log) == 1


def test_replay_taps_count_as_actions() -> None:
    eng_on, _, log_on = crawl_run2(Policy())
    eng_off, _, _log_off = crawl_run2(OFF)
    # launch, A, a1, relaunch, A (replay), a2, a3 + their BACKs, B, C
    assert eng_on.actions_sent == len([a for a in log_on if not isinstance(a, Stop)])
    assert eng_on.actions_sent > eng_off.actions_sent
    assert eng_on.records.actions_sent == eng_on.actions_sent


def test_depth_holds_for_every_replay_tap() -> None:
    # drive(depth=3) checks the real stack of the phone for every tap.
    _eng, phone, _log = crawl_run2(Policy(), Limits(depth=3))
    assert ("n0", "A", True) in phone.pressed


def test_a_replay_through_two_screens() -> None:
    nodes = [
        Node("n0", (("A", "s1"), ("B", "leafb"))),
        Node("s1", (("x", "s2"),)),
        Node("s2", (("y1", "ext"), ("y2", "leaf2"))),
        Node("leafb"),
        Node("leaf2"),
        Node("ext", package=OTHER),
    ]
    _eng, phone, _log = crawl_run2(Policy(), Limits(depth=5), nodes)
    assert phone.pressed == [
        ("n0", "A", False),
        ("s1", "x", False),
        ("s2", "y1", False),
        ("n0", "A", True),
        ("s1", "x", True),
        ("s2", "y2", False),
        ("n0", "B", False),
    ]


# -- abandoning, on the engine alone ---------------------------------------------


def to_relaunch(eng=None, policy=None):
    """Press A, then a1 (a tap that leaves the app); return the engine, the
    screens and the Relaunch action."""
    eng = eng or started(policy=policy or Policy())
    root = on_screen(TOP)
    press(eng, root)
    sub = on_screen(SUB, name="sub")
    press(eng, sub)
    assert eng.next(off_screen()) == Relaunch()
    return eng, root, sub


def replay_tap(eng, root):
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    assert eng.is_replay(tap)
    return tap


def send(eng, tap, fresh):
    assert eng.recheck(tap, fresh)
    eng.tap_sent(tap)


def test_the_replay_taps_the_recorded_row_and_is_flagged() -> None:
    eng, root, sub = to_relaunch()
    tap = replay_tap(eng, root)
    first = eng.records.transitions[1]
    assert tap.fingerprint == first.fingerprint
    send(eng, tap, root)
    # landing on the recorded screen ends the replay: a2 is next, a plain tap
    nxt = eng.next(sub)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert eng.records.transitions[-1].action_id in eng.replay_actions
    assert eng._goal is None


def test_the_route_screen_differs() -> None:
    eng, root, _sub = to_relaunch()
    tap = replay_tap(eng, root)
    send(eng, tap, root)
    other = on_screen(["z1", "z2"], name="other")
    nxt = eng.next(other)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)  # the crawl goes on from here as before
    assert eng._replay_fails == {on_screen(SUB, name="sub").screen.screen_id: 1}


def test_a_deeper_step_needs_the_recorded_screen_id() -> None:
    eng = started()
    r = on_screen(["A"])
    press(eng, r)
    s1 = on_screen(["x"], name="s1")
    press(eng, s1)
    s2 = on_screen(["y1", "y2"], name="s2")
    press(eng, s2)
    assert eng.next(off_screen()) == Relaunch()
    send(eng, replay_tap(eng, r), r)
    # lands on s1 as recorded, then the next step meets another id: abandoned
    changed = on_screen(["x", "extra"], name="s1")
    nxt = eng.next(changed)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert list(eng._replay_fails.values()) == [1]


def test_the_element_is_gone() -> None:
    eng, _root, _sub = to_relaunch()
    gone = on_screen(["B", "C"])
    nxt = eng.next(gone)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert list(eng._replay_fails.values()) == [1]


def test_the_element_is_refused_now() -> None:
    eng, root, _sub = to_relaunch()
    assert root.screen is not None
    first = root.screen.elements[1]
    # same fingerprint, but a checkable row: judge refuses it as a toggle
    refused = seen(
        make_screen(TOP),
    )
    assert refused.screen is not None
    elements = tuple(
        clone(e, flags=("clickable", "checkable")) if e.id == first.id else e
        for e in refused.screen.elements
    )
    screen = type(refused.screen)(
        **{
            **{f.name: getattr(refused.screen, f.name) for f in fields(refused.screen)},
            "elements": elements,
        }
    )
    nxt = eng.next(seen(screen))
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert list(eng._replay_fails.values()) == [1]


def test_the_recheck_refuses_the_replay_tap() -> None:
    eng, root, _sub = to_relaunch()
    tap = replay_tap(eng, root)
    fresh = on_screen(["A!", "B", "C"])
    assert not eng.recheck(tap, fresh)
    assert list(eng._replay_fails.values()) == [1]
    assert eng._replay is None
    nxt = eng.next(fresh)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)


def test_the_tap_lands_elsewhere_and_two_failures_drop_the_goal() -> None:
    eng, root, sub = to_relaunch()
    goal = sub.screen.screen_id
    tap = replay_tap(eng, root)
    send(eng, tap, root)
    assert eng.next(off_screen()) == Relaunch()  # the replay led off target
    assert eng._replay_fails == {goal: 1}
    assert goal not in eng._replay_dropped
    tap = replay_tap(eng, root)  # the goal is kept: tried again
    send(eng, tap, root)
    assert eng.next(off_screen()) == Relaunch()
    assert eng._replay_fails == {goal: 2}
    assert goal in eng._replay_dropped
    nxt = eng.next(root)  # dropped: an ordinary tap on the next untried row
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert nxt.fingerprint == fingerprint_of(root.screen, root.screen.elements[2])  # B


def test_a_dropped_goal_is_not_chosen_again_from_a_later_path() -> None:
    eng, root, _sub = to_relaunch()
    for _ in range(2):
        send(eng, replay_tap(eng, root), root)
        assert eng.next(off_screen()) == Relaunch()
    # B, then back at the top page
    press(eng, root)
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(root)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)


def test_an_unsent_replay_tap_abandons_the_replay() -> None:
    eng, root, _sub = to_relaunch()
    tap = replay_tap(eng, root)
    assert eng.recheck(tap, root)  # allowed, never reported as sent
    nxt = eng.next(root)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert list(eng._replay_fails.values()) == [1]


def test_a_withdrawn_replay_tap_is_chosen_again() -> None:
    eng, root, _sub = to_relaunch()
    tap = replay_tap(eng, root)
    eng.withdraw(tap)
    again = replay_tap(eng, root)
    assert again == tap
    assert eng._replay_fails == {}


def test_the_root_may_change_its_id_but_not_the_route() -> None:
    eng, _root, sub = to_relaunch()
    other_root = on_screen(TOP, name="renamed")
    tap = replay_tap(eng, other_root)
    send(eng, tap, other_root)
    nxt = eng.next(sub)
    assert not eng.is_replay(nxt)
    assert eng._replay_fails == {}


# -- limits ---------------------------------------------------------------------


def test_the_action_limit_stops_before_a_replay_tap() -> None:
    # launch, A, a1, relaunch: four actions
    eng = started(Limits(actions=4))
    root = on_screen(TOP)
    press(eng, root)
    press(eng, on_screen(SUB, name="sub"))
    assert eng.next(off_screen()) == Relaunch()
    assert eng.next(root) == Stop("max_actions")


def test_the_action_limit_counts_a_replay_tap() -> None:
    eng = started(Limits(actions=5))
    eng, root, _sub = to_relaunch(eng)
    send(eng, replay_tap(eng, root), root)
    assert eng.actions_sent == 5
    assert eng.next(on_screen(SUB, name="sub")) == Stop("max_actions")


def test_the_time_limit_stops_before_a_replay_tap() -> None:
    eng = started(Limits(minutes=1))
    eng, _root, _sub = to_relaunch(eng)
    late = on_screen(TOP, now=60.0)
    assert eng.next(late) == Stop("max_duration")


def test_a_replay_tap_cancelled_at_the_deadline_stays_tried() -> None:
    eng = started(Limits(minutes=1))
    eng, root, _sub = to_relaunch(eng)
    tap = replay_tap(eng, root)
    assert not eng.recheck(tap, on_screen(TOP, now=60.0))
    assert (tap.screen_id, tap.fingerprint) in eng._tried
    assert eng.next(on_screen(TOP, now=60.0)) == Stop("max_duration")


def test_the_relaunch_limit_is_unchanged() -> None:
    # replay taps that are sent reset the run of relaunches (as every sent
    # tap does); a replay that is never sent does not
    eng, root, _sub = to_relaunch()
    for _ in range(2):
        send(eng, replay_tap(eng, root), root)
        assert eng.next(off_screen()) == Relaunch()
    # the goal is dropped now; three relaunches in a row still stop the run
    assert eng.next(off_screen()) == Relaunch()
    assert eng.next(off_screen()) == Relaunch()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_depth_caps_the_route() -> None:
    shallow = started(Limits(depth=2))
    press(shallow, on_screen(["A"]))
    sub_id = on_screen(["x"], name="sub").screen.screen_id
    shallow.next(on_screen(["x"], name="sub"))
    assert shallow._route_to(sub_id) is None  # depth 2: nothing is pressed there
    deeper = started(Limits(depth=3))
    press(deeper, on_screen(["A"]))
    deeper.next(on_screen(["x"], name="sub"))
    assert deeper._route_to(sub_id) is not None


# -- the switch -------------------------------------------------------------------


def test_off_keeps_no_replay_state_and_no_extra_marks() -> None:
    eng = started(policy=OFF)
    eng, root, _sub = to_relaunch(eng)
    nxt = eng.next(root)
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert eng._route == {}
    assert eng._untried == {}
    assert eng._bare_tried == set()
    assert eng._replay is None
    assert eng._goal is None


def test_off_gives_the_actions_of_main_on_the_run2_shape() -> None:
    # Pinned from the engine of main (9e322cd, before the replay): the same
    # fake run, the same actions and the same screens in the same order.
    eng = engine(policy=OFF)
    phone = LoggingPhone(run2_nodes())
    phone.eng = eng
    log = drive(eng, phone, policy=OFF)
    assert [type(a).__name__ for a in log] == [
        "Launch",
        "Tap",
        "Tap",
        "Relaunch",
        "Tap",
        "Back",
        "Tap",
        "Back",
        "Stop",
    ]
    assert phone.visited == ["n0", "sub", "ext", "n0", "leafb", "n0", "leafc", "n0"]
    assert eng.replay_actions == frozenset()


# -- the badge part ---------------------------------------------------------------


def badge_screen(*, badge: bool, labels=("Wi-Fi", "Bluetooth")):
    """A top page whose first row may carry a badge element."""
    screen = make_screen(list(labels))
    if not badge:
        return seen(screen)
    from uiprobe.model import Bounds, Element

    badge_el = Element(
        "e90",
        "e1",
        "android.widget.FrameLayout",
        Bounds(280, 0, 300, 20),
        resource_id=f"{PKG}:id/Badge_frame",
        package=PKG,
    )
    child = Element(
        "e91",
        "e90",
        "android.widget.TextView",
        Bounds(280, 0, 300, 20),
        text="New",
        package=PKG,
    )
    return seen(
        type(screen)(
            **{
                **{f.name: getattr(screen, f.name) for f in fields(screen)},
                "elements": (*screen.elements, badge_el, child),
            }
        )
    )


def test_a_row_tried_with_a_badge_is_not_pressed_again_without_it() -> None:
    eng = started()
    with_badge = badge_screen(badge=True)
    tap = press(eng, with_badge)  # Wi-Fi, under both marks
    without = badge_screen(badge=False)
    assert with_badge.screen.screen_id != without.screen.screen_id
    assert (without.screen.screen_id, tap.fingerprint) in eng._bare_tried
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(without)
    assert isinstance(nxt, Tap)
    assert nxt.fingerprint != tap.fingerprint  # Bluetooth, not Wi-Fi again
    # the skip because of the badge-free mark added the full mark too
    assert (without.screen.screen_id, tap.fingerprint) in eng._tried


def test_a_row_tried_without_a_badge_is_not_pressed_again_with_it() -> None:
    eng = started()
    without = badge_screen(badge=False)
    tap = press(eng, without)
    with_badge = badge_screen(badge=True)
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(with_badge)
    assert isinstance(nxt, Tap)
    assert nxt.fingerprint != tap.fingerprint
    assert (with_badge.screen.screen_id, tap.fingerprint) in eng._tried


def test_the_marks_are_added_under_both_keys() -> None:
    eng = started()
    with_badge = badge_screen(badge=True)
    tap = press(eng, with_badge)
    full = with_badge.screen.screen_id
    bare = badge_screen(badge=False).screen.screen_id
    assert (full, tap.fingerprint) in eng._tried
    assert (bare, tap.fingerprint) in eng._bare_tried


def test_a_refused_recheck_marks_both_keys_and_a_deadline_cancel_clears_both() -> None:
    eng = started(Limits(minutes=1))
    with_badge = badge_screen(badge=True)
    tap = eng.next(with_badge)
    assert isinstance(tap, Tap)
    bare = badge_screen(badge=False).screen.screen_id
    assert not eng.recheck(tap, badge_screen(badge=True, labels=("Wi-Fi!", "B")))
    assert (bare, tap.fingerprint) in eng._bare_tried
    eng2 = started(Limits(minutes=1))
    tap2 = eng2.next(with_badge)
    assert isinstance(tap2, Tap)
    assert not eng2.recheck(tap2, seen(with_badge.screen, now=60.0))
    assert (tap2.screen_id, tap2.fingerprint) not in eng2._tried
    assert (bare, tap2.fingerprint) not in eng2._bare_tried


def test_off_marks_only_the_full_screen_id() -> None:
    eng = started(policy=OFF)
    with_badge = badge_screen(badge=True)
    tap = press(eng, with_badge)
    assert eng._bare_tried == set()
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(badge_screen(badge=False))
    assert isinstance(nxt, Tap)
    assert nxt.fingerprint == tap.fingerprint  # as on main: Wi-Fi again


def test_back_to_the_switch_state_snapshot_holds_the_new_fields() -> None:
    # the generic check lives in test_crawl_engine; this names the new ones
    held = {f.name for f in fields(crawl._State)}
    assert {
        "bare_tried",
        "route",
        "untried",
        "prior",
        "goal",
        "replay",
        "replay_fails",
        "replay_dropped",
        "replay_tap",
    } <= held


def test_the_back_press_after_a_replay_is_a_navigation_back() -> None:
    eng, root, sub = to_relaunch()
    send(eng, replay_tap(eng, root), root)
    tap = eng.next(sub)
    assert isinstance(tap, Tap)
    eng.recheck(tap, sub)
    eng.tap_sent(tap)
    leaf = on_screen([], name="leaf")
    assert eng.next(leaf) == Back()


# -- the principle: (a) a verified route, or (b) untried rows that may be pressed ------


def rebuilt(screen: Screen, change) -> Screen:
    values = {f.name: getattr(screen, f.name) for f in fields(screen)}
    values["elements"] = tuple(change(e) for e in screen.elements)
    return Screen(**values)


def test_replay_a_unrooted_chain_sets_no_goal_and_presses_no_tried_row() -> None:
    # A keyboard that hides changes the id of a sub-page (s2 -> s2b). The tap
    # that left s2b is recorded on s2b, which no tap led to: the chain does not
    # reach the root. It must not be replayed from "the root" by fingerprint:
    # the root row "y" has the fingerprint of the row pressed on s2b, and it was
    # tried before.
    eng = started()
    root = on_screen(["y", "A"])
    press(eng, root)  # y: opens a leaf
    leaf = on_screen([], name="leaf")
    assert eng.next(leaf) == Back()
    tap = eng.next(root)  # A
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, root)
    eng.tap_sent(tap)
    s1 = on_screen(["go"], name="s1")
    press(eng, s1)
    s2 = on_screen(["w", "v"], name="s2", keyboard=True)
    assert eng.next(s2) == Back()  # hides the keyboard
    s2b = on_screen(["y", "w"], name="s2b")  # the same page, another id
    press(eng, s2b)  # y: the fingerprint of the root row y
    s3 = on_screen(["z1", "z2"], name="s3")
    press(eng, s3)  # z1 leaves the app
    assert eng.next(off_screen()) == Relaunch()
    s3_id = s3.screen.screen_id
    assert eng._route_to(s3_id) is None
    assert eng._route_to(s2b.screen.screen_id) is None
    assert eng.next(root) == Stop("exhausted")  # nothing replayed, nothing untried
    assert eng.replay_actions == frozenset()
    assert eng._goal is None


def test_replay_a_a_broken_link_sets_no_goal() -> None:
    eng = started()
    r = on_screen(["A"])
    press(eng, r)
    s1 = on_screen(["x"], name="s1")
    press(eng, s1)
    s2 = on_screen(["y1", "y2"], name="s2")
    press(eng, s2)
    eng.next(on_screen([], name="s3"))
    goal = s2.screen.screen_id
    assert eng._route_to(goal) is not None
    assert [w.from_root for w in eng._route_to(goal)] == [True, False]
    del eng._route[s1.screen.screen_id]  # the first link is unknown
    assert eng._route_to(goal) is None


def test_replay_a_the_route_starts_with_a_tap_on_the_root() -> None:
    eng = started()
    press(eng, on_screen(["A"]))
    s1 = on_screen(["x"], name="s1")
    press(eng, s1)
    eng.next(on_screen(["q"], name="s2"))
    route = eng._route_to(on_screen(["q"], name="s2").screen.screen_id)
    assert route is not None
    assert route[0].from_root
    assert not route[1].from_root
    assert route[1].parent == s1.screen.screen_id


def twins(*, checkable_first: bool):
    screen = make_screen(["T", "T"], rid="same")
    first = screen.elements[1]

    def change(e):
        if checkable_first and e.id == first.id:
            return clone(e, flags=("clickable", "checkable"))
        return e

    return seen(rebuilt(screen, change))


def test_replay_a_a_step_needs_exactly_one_match_at_the_recorded_place() -> None:
    plain = twins(checkable_first=False)
    eng = started()
    press(eng, plain)  # the first twin
    sub = on_screen(SUB, name="sub")
    press(eng, sub)
    assert eng.next(off_screen()) == Relaunch()
    # both twins pressable: more than one match
    nxt = eng.next(plain)
    assert not isinstance(nxt, Tap) or not eng.is_replay(nxt)
    assert list(eng._replay_fails.values()) == [1]


def test_replay_a_the_other_twin_is_not_pressed_when_the_recorded_one_is_refused() -> (
    None
):
    eng = started()
    press(eng, twins(checkable_first=False))
    sub = on_screen(SUB, name="sub")
    press(eng, sub)
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(twins(checkable_first=True))
    # one pressable twin with the fingerprint, but not at the recorded place
    assert nxt == Stop("exhausted")
    assert eng.replay_actions == frozenset()
    assert list(eng._replay_fails.values()) == [1]


def test_replay_a_a_moved_element_is_not_pressed() -> None:
    eng, _root, _sub = to_relaunch()
    moved = seen(make_screen(TOP, cls="android.widget.Button"))
    assert moved.screen is not None

    def shift(e):
        if e.text == "A":
            return clone(e, bounds=Bounds(40, 400, 280, 440))
        return e

    nxt = eng.next(seen(rebuilt(moved.screen, shift)))
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)
    assert list(eng._replay_fails.values()) == [1]


def test_replay_a_cut_short_twice_drops_the_goal() -> None:
    eng, root, sub = to_relaunch()
    goal = sub.screen.screen_id
    shown = on_screen(TOP, keyboard=True)
    assert eng.next(shown) == Back()  # root placed, replay planned, keyboard BACK
    assert eng.next(off_screen()) == Relaunch()
    assert eng.next(shown) == Back()  # failure 1; the goal is tried again
    assert eng._replay_fails == {goal: 1}
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(root)  # failure 2: dropped; an ordinary tap
    assert eng._replay_fails == {goal: 2}
    assert goal in eng._replay_dropped
    assert isinstance(nxt, Tap)
    assert not eng.is_replay(nxt)


def test_replay_b_a_deadline_cancel_keeps_the_marks_that_predate_the_proposal() -> None:
    eng = started(Limits(minutes=1))
    eng, root, _sub = to_relaunch(eng)
    old_id = root.screen.screen_id
    renamed = on_screen(TOP, name="renamed")  # the root has another id now
    tap = replay_tap(eng, renamed)
    fingerprint = tap.fingerprint
    assert (old_id, fingerprint) in eng._tried
    assert (renamed.screen.screen_id, fingerprint) in eng._tried  # the proposal
    assert not eng.recheck(tap, on_screen(TOP, name="renamed", now=60.0))
    assert (old_id, fingerprint) in eng._tried  # predates it: stays
    assert (old_id, fingerprint) in eng._bare_tried
    assert (renamed.screen.screen_id, fingerprint) not in eng._tried
    assert (renamed.screen.screen_id, fingerprint) not in eng._bare_tried


def test_replay_b_an_ordinary_proposal_cancelled_at_the_deadline_leaves_no_mark() -> (
    None
):
    eng = started(Limits(minutes=1))
    screen = on_screen(TOP)
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, on_screen(TOP, now=60.0))
    assert eng._tried == set()
    assert eng._bare_tried == set()


def row_screen(*, badge: bool):
    """Wi-Fi and Bluetooth rows; the Wi-Fi row may hold a badge inside it."""
    root = Element(
        "e1", None, "android.widget.FrameLayout", Bounds(0, 0, 320, 640), package=PKG
    )
    wifi = Element(
        "e2",
        "e1",
        "android.widget.Button",
        Bounds(40, 60, 280, 100),
        text="Wi-Fi",
        resource_id=f"{PKG}:id/wifi",
        package=PKG,
        flags=("clickable",),
    )
    bluetooth = Element(
        "e3",
        "e1",
        "android.widget.Button",
        Bounds(40, 120, 280, 160),
        text="Bluetooth",
        resource_id=f"{PKG}:id/bt",
        package=PKG,
        flags=("clickable",),
    )
    elements = [root, wifi, bluetooth]
    if badge:
        elements.append(
            Element(
                "e4",
                "e2",
                "android.widget.TextView",
                Bounds(240, 60, 280, 80),
                text="New",
                resource_id=f"{PKG}:id/badge_dot",
                package=PKG,
            )
        )
    plain = make_screen([])
    values = {f.name: getattr(plain, f.name) for f in fields(plain)}
    values["elements"] = tuple(elements)
    return seen(Screen(**values))


def test_replay_b_a_badge_inside_the_row_is_left_out_of_its_mark() -> None:
    eng = started()
    with_badge = row_screen(badge=True)
    tap = press(eng, with_badge)  # Wi-Fi, with its badge inside
    without = row_screen(badge=False)
    assert fingerprint_of(without.screen, without.screen.elements[1]) != tap.fingerprint
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(without)
    assert isinstance(nxt, Tap)
    assert nxt.element_id == "e3"  # Bluetooth: the Wi-Fi row is not pressed again


def test_replay_b_off_presses_the_row_again_when_the_badge_inside_it_goes() -> None:
    eng = started(policy=OFF)
    press(eng, row_screen(badge=True))
    assert eng.next(off_screen()) == Relaunch()
    nxt = eng.next(row_screen(badge=False))
    assert isinstance(nxt, Tap)
    assert nxt.element_id == "e2"  # as on main


def test_replay_b_the_badge_walk_finds_children_listed_before_their_parent() -> None:
    from uiprobe.crawl.engine import _BadgeFree
    from uiprobe.crawl.prints import _Prints

    screen = row_screen(badge=True).screen
    badge = Element(
        "e5",
        "e4",
        "android.widget.TextView",
        Bounds(240, 60, 280, 80),
        text="deep",
        package=PKG,
    )
    shuffled = rebuilt(screen, lambda e: e)
    values = {f.name: getattr(shuffled, f.name) for f in fields(shuffled)}
    values["elements"] = (*screen.elements[:3], badge, screen.elements[3])
    mixed = Screen(**values)
    bare = _BadgeFree(mixed, _Prints(mixed))
    assert bare.key_of(badge, "f-x") is None
    assert bare.key_of(mixed.elements[4], "f-x") is None
    plain = row_screen(badge=False).screen
    assert bare.screen_id == plain.screen_id
    wifi = mixed.elements[1]
    assert bare.key_of(wifi, "f-x") == (
        plain.screen_id,
        fingerprint_of(plain, plain.elements[1]),
    )


def test_replay_b_a_screen_that_is_not_judged_gets_no_badge_work(monkeypatch) -> None:
    import uiprobe.crawl.engine as engine_module

    def refuse(*_args, **_kwargs):
        raise AssertionError("badge processing on a screen that is not judged")

    monkeypatch.setattr(engine_module, "_BadgeFree", refuse)
    eng = started()
    big = on_screen([f"row {i}" for i in range(MAX_ELEMENTS + 2)])
    assert len(big.screen.elements) > MAX_ELEMENTS
    nxt = eng.next(big)
    assert nxt == Stop("exhausted")
    assert eng._bare_tried == set()


def test_the_records_of_a_transition_keep_their_old_fields() -> None:
    names = [f.name for f in fields(Transition)]
    assert names == [
        "action_id",
        "kind",
        "from_screen_id",
        "to_screen_id",
        "fingerprint",
        "point",
        "dump_failed",
    ]
    eng, _phone, _log = crawl_run2(Policy())
    for transition in eng.records.transitions:
        assert list(asdict(transition)) == names
    assert eng.replay_actions  # the replay is told apart outside the record
