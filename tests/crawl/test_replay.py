"""Tests for the path replay and the badge-free tried marks of the crawl engine."""

from __future__ import annotations

from dataclasses import fields

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
    fingerprint_of,
)
from uiprobe.guards import Policy

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
    replays = [t for t in eng.records.transitions if t.replay]
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
    assert eng.records.transitions[-1].replay is True
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
    assert not any(t.replay for t in eng.records.transitions)


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
