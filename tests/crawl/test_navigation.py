"""Tests for the crawl engine's navigation backs, roots and deferred landings."""

from __future__ import annotations

import pytest

from tests.crawl.helpers import (
    BACK_POLICY,
    PERMISSION,
    POLICY,
    Node,
    Phone,
    _row_screen,
    back_engine,
    back_started,
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
    Launch,
    Limits,
    Relaunch,
    Stop,
    Tap,
    fingerprint_of,
)


def test_a_link_back_to_the_start_screen_does_not_end_the_crawl() -> None:
    # The start screen higher up the path is not the root: it gets BACK like any
    # other screen, so the screen "n2" below it is still visited.
    nodes = [
        Node("n0", (("Item a", "n1"),)),
        Node("n1", (("Item a", "n0"), ("Item b", "n2"))),
        Node("n2"),
    ]
    phone = Phone(nodes)
    log = drive(engine(), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "n2" in phone.visited


def test_p1_buttons_that_stay_on_the_start_screen_do_not_hide_the_leaves() -> None:
    nodes = [
        Node(
            "n0",
            (
                ("One", "n0"),
                ("Two", "n0"),
                ("Three", "n0"),
                ("Four", "n0"),
                ("Five", "leaf1"),
                ("Six", "leaf2"),
            ),
        ),
        Node("leaf1"),
        Node("leaf2"),
    ]
    phone = Phone(nodes)
    eng = engine()
    log = drive(eng, phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert {"leaf1", "leaf2"} <= set(phone.visited)
    assert eng.depth_of(phone.screen_id("n0")) == 1


def test_p3_a_cycle_back_to_the_start_still_reaches_the_other_leaf() -> None:
    nodes = [
        Node("n0", (("Item a", "n1"), ("Item b", "leafB"))),
        Node("n1", (("Item a", "n2"),)),
        Node("n2", (("Item a", "n3"),)),
        Node("n3", (("Item a", "n0"),)),
        Node("leafB"),
    ]

    class Tracking(Phone):
        deepest = 0

        def send(self, action):
            super().send(action)
            self.deepest = max(self.deepest, len(self.stack))

    phone = Tracking(nodes)
    eng = engine(Limits(depth=5))
    log = drive(eng, phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "leafB" in phone.visited
    assert phone.deepest <= 5  # the real stack never passes the depth limit
    n0 = phone.screen_id("n0")
    n3 = phone.screen_id("n3")
    # the BACK on the upper n0 (path of 5, at the limit) was sent
    assert any(
        t.kind == "back" and t.from_screen_id == n0 and t.to_screen_id == n3
        for t in eng.records.transitions
    )


def test_a_back_that_returns_the_same_screen_leads_to_a_relaunch() -> None:
    eng = started()
    root = on_screen(["Item a"], name="root")
    press(eng, root)
    stuck = on_screen([], name="stuck")
    assert eng.next(stuck) == Back()
    assert eng.next(stuck) == Relaunch()  # the BACK did nothing
    assert eng.next(root) == Stop("exhausted")
    assert eng.actions_sent == 4


def test_backs_that_alternate_between_two_screens_lead_to_a_relaunch() -> None:
    eng = started()
    root = on_screen(["Item a"], name="root")
    press(eng, root)
    first = on_screen([], name="first")
    second = on_screen([], name="second")
    assert eng.next(first) == Back()
    assert eng.next(second) == Back()
    assert eng.next(first) == Back()
    assert eng.next(second) == Relaunch()  # second was landed on before
    assert eng.next(root) == Stop("exhausted")
    assert eng.actions_sent == 6


def test_a_sent_tap_clears_the_screens_that_backs_landed_on() -> None:
    eng = started()
    root = on_screen(["Item a", "Item b"], name="root")
    mid = on_screen(["Item a"], name="mid")
    leaf = on_screen([], name="leaf")
    press(eng, root)
    assert eng.next(leaf) == Back()
    press(eng, root)  # a tap: the set starts again
    press(eng, mid)
    assert eng.next(leaf) == Back()
    assert eng.next(mid) == Back()
    assert eng.next(root) == Stop("exhausted")


def test_unwinding_a_path_that_repeats_a_screen_id_is_not_a_relaunch() -> None:
    # n0 -> m -> n0 (again) -> x: the BACKs land on n0, m, n0 in turn. The last
    # landing is the screen the path predicts, so it is a normal step back.
    nodes = [
        Node("n0", (("Item a", "m"), ("Item b", "x"))),
        Node("m", (("Item a", "n0"),)),
        Node("x"),
    ]
    phone = Phone(nodes)
    log = drive(engine(Limits(depth=6)), phone, depth=6)
    assert log[-1] == Stop("exhausted")
    assert not any(isinstance(a, Relaunch) for a in log)
    assert "x" in phone.visited


def test_a_landing_on_a_screen_landed_on_before_the_last_tap_is_not_a_relaunch() -> (
    None
):
    eng = started()
    root = on_screen(["Item a", "Item b"], name="root")
    mid = on_screen(["Item a"], name="mid")
    leaf = on_screen([], name="leaf")
    press(eng, root)  # a
    assert eng.next(leaf) == Back()
    press(eng, root)  # b; the BACK landed on root before
    press(eng, mid)
    assert eng.next(leaf) == Back()
    # The BACK jumps to root, past mid, which the path does not predict. The tap
    # sent since cleared the set, so this is no repeat landing.
    assert eng.next(root) == Stop("exhausted")


def test_an_off_target_back_is_not_a_navigation_back() -> None:
    eng = back_started()
    root = on_screen(["Item a"], name="root")
    mid = on_screen(["Item a"], name="mid")
    leaf = on_screen([], name="leaf")
    press(eng, root)
    press(eng, mid)
    assert eng.next(leaf) == Back()
    assert eng.next(mid) == Back()  # a navigation BACK landed on mid
    assert eng.next(off_screen()) == Back()  # the app was left; an off-target BACK
    # lands on mid again: that is no repeat landing of a navigation BACK (no
    # Relaunch), and the same frame replaces the root in place, so nothing
    # more is pressed (its button was tried) and there is no BACK on the root
    assert eng.next(mid) == Stop("exhausted")


def test_dialogs_over_a_new_target_screen_place_it_at_its_true_depth() -> None:
    eng = back_started(Limits(depth=3))
    root = on_screen(["Item a"], name="root")
    press(eng, root)
    # the tap opened a target screen with two dialogs over it
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    opened = on_screen(["Item a"], name="opened")
    # not the root: it takes the place of the mark the tap left, at depth 2
    assert eng.next(opened).__class__ is Tap
    assert eng.depth_of(opened.screen.screen_id) == 2


def test_a_tap_that_pushes_more_than_one_screen_keeps_the_depth_limit() -> None:
    chain = [Node(f"t{i}", (("Item a", f"t{i + 1}"),)) for i in range(1, 5)]
    nodes = [
        Node("n0", (("Item a", "T"), ("Item b", "leafR"))),
        Node("T", (("Item a", "t1"),)),
        *chain,
        Node("t5"),
        Node("leafR"),
        Node("D1", (("Allow", "n0"),), package=PERMISSION),
        Node("D2", (("Allow", "n0"),), package=PERMISSION),
    ]

    class Dialogs(Phone):
        def send(self, action):
            before = list(self.stack)
            super().send(action)
            if isinstance(action, Tap) and self.stack[-1] == "T" and before == ["n0"]:
                self.stack += ["D2", "D1"]  # two dialogs over the new screen
                self.visited[-1] = "D1"

    phone = Dialogs(nodes)
    log = drive(engine(Limits(depth=5)), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "leafR" in phone.visited  # the second button of the root was pressed
    assert phone.stack == ["n0"]  # and the crawl ended on the real root


def test_dialogs_over_the_pressed_screen_leave_no_extra_depth() -> None:
    # n1 links back to the start screen n0, and the app shows a permission
    # dialog whenever n0 is opened by a tap.
    nodes = [
        Node("n0", (("Item a", "n1"), ("Item b", "n2"))),
        Node("n1", (("Item a", "n0"), ("Item b", "z"))),
        Node("n2", (("Item a", "n3"),)),
        Node("n3", (("Item a", "n4"),)),
        Node("n4", (("Item a", "n5"),)),
        Node("n5"),
        Node("z"),
        Node("D1", (("Allow", "n0"),), package=PERMISSION),
    ]

    class Perm(Phone):
        def send(self, action):
            super().send(action)
            if isinstance(action, Tap) and self.stack[-1] == "n0":
                self.stack.append("D1")
                self.visited[-1] = "D1"

    phone = Perm(nodes)
    log = drive(back_engine(Limits(depth=5)), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "z" in phone.visited
    assert phone.stack == ["n0"]


@pytest.mark.parametrize("dialogs", [1, 2])
def test_a_reopened_screen_with_new_labels_keeps_the_real_stack_in_limit(
    dialogs: int,
) -> None:
    # T shows a feed whose label changes each time T is opened (same screen id
    # is kept, the fingerprint is new). T is first reached from n0 (depth 2),
    # and later from b (real depth 3) with dialogs over it.
    nodes = [
        Node("n0", (("Item a", "T"), ("Item b", "a"))),
        Node("a", (("Item a", "b"),)),
        Node("b", (("Item a", "T"),)),
        Node("T", (("Feed 0", "U"),)),
        Node("U", (("Item a", "V"),)),
        Node("V", (("Item a", "W"),)),
        Node("W", (("Item a", "X"),)),
        Node("X"),
        Node("D1", (("Allow", "n0"),), package=PERMISSION),
        Node("D2", (("Allow", "n0"),), package=PERMISSION),
    ]

    class Feed(Phone):
        opened = 0

        def send(self, action):
            before = list(self.stack)
            super().send(action)
            if isinstance(action, Tap) and self.stack[-1] == "T":
                self.opened += 1
                for name, nxt in (("T", "U"), ("U", "V"), ("V", "W"), ("W", "X")):
                    label = f"Feed {self.opened}"
                    self.nodes[name] = Node(name, ((label, nxt),))
                if before[-1:] == ["b"]:
                    self.stack += ["D2", "D1"][-dialogs:]
                    self.visited[-1] = self.stack[-1]

    phone = Feed(nodes)
    log = drive(engine(Limits(depth=5)), phone, depth=5)
    assert isinstance(log[-1], Stop)


def test_the_keyboard_back_is_not_a_navigation_back() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    # the same screen again without the keyboard: no relaunch
    press(eng, on_screen(["Item a"]))


def test_the_keyboard_back_on_the_root_is_still_allowed() -> None:
    eng = started()
    root = on_screen(["Item a"], keyboard=True)
    assert eng.next(root) == Back()
    press(eng, on_screen(["Item a"]))


def test_a_screen_with_the_roots_id_higher_up_gets_back_when_it_is_done() -> None:
    eng = started(Limits(depth=3))
    root = on_screen(["Item a"], name="root")
    press(eng, root)
    mid = on_screen(["Item a"], name="mid")
    press(eng, mid)
    # mid links back to the root screen: path of 3, at the limit
    assert eng.next(root) == Back()
    assert len(eng._path) == 3


# -- same frame after a BACK that is not navigation -------------------


def test_a_keyboard_back_on_the_root_that_changes_the_screen_id_is_the_same_frame() -> (
    None
):
    eng = started()
    first = eng.next(on_screen(["Item a"], name="root", keyboard=True))
    assert first == Back()  # the keyboard BACK
    second = eng.next(on_screen(["Item a", "Item b"], name="root"))
    assert isinstance(second, Tap)  # not a BACK on the root
    assert len(eng._path) == 1
    assert second.screen_id == eng._path[0]


def test_a_keyboard_that_returns_after_every_relaunch_never_backs_out_of_the_root() -> (
    None
):
    eng = started()
    with_keyboard = on_screen(["Item a"], name="root", keyboard=True)
    full = on_screen(["Item a", "Item b"], name="root")
    gone = off_screen()
    state = "keyboard"
    kinds: list[str] = []
    action = eng.next(with_keyboard)
    for _ in range(40):
        kinds.append(type(action).__name__)
        if isinstance(action, Stop):
            break
        if isinstance(action, Launch | Relaunch):
            state = "keyboard"
        elif isinstance(action, Back):
            state = {"keyboard": "full", "full": "gone", "gone": "gone"}[state]
        current = {"keyboard": with_keyboard, "full": full, "gone": gone}[state]
        if isinstance(action, Tap):
            assert eng.recheck(action, current)
            eng.tap_sent(action)
            state = "full"
            current = full
        action = eng.next(current)
    assert action == Stop("exhausted")
    assert kinds.count("Back") == 1  # only the one that hides the keyboard
    assert "Relaunch" not in kinds


def test_an_off_target_back_landing_without_a_mark_is_the_same_frame() -> None:
    eng = back_started()
    tap = eng.next(on_screen(["Item a"], name="root"))
    assert isinstance(tap, Tap)
    # the recheck sees a dialog and refuses; nothing was sent
    dialog = seen(
        make_screen(["Allow"], name="perm", package=PERMISSION), fg=PERMISSION
    )
    assert not eng.recheck(tap, dialog)
    assert eng.next(dialog) == Back()  # off-target BACK, no mark on the path
    after = eng.next(on_screen(["Item a", "Item b"], name="root"))
    assert isinstance(after, Tap)  # the root is still the root: no BACK
    assert len(eng._path) == 1


def test_a_keyboard_back_that_lands_on_the_screen_below_pops_the_top() -> None:
    eng = started()
    root = on_screen(["Item a"], name="root")
    press(eng, root)
    mid = on_screen(["Item a"], name="mid", keyboard=True)
    assert eng.next(mid) == Back()  # keyboard BACK on mid, path is [root, mid]
    assert len(eng._path) == 2
    # the BACK went further than the keyboard: the root is shown, the top is gone
    assert eng.next(root) == Stop("exhausted")
    assert len(eng._path) == 1


def test_a_keyboard_back_landing_replaces_the_top_in_place_and_keeps_the_depth() -> (
    None
):
    eng = started(Limits(depth=4))
    steps = [
        on_screen(["Go", "Two"], name="R"),
        on_screen(["Home", "Next"], name="A"),
        on_screen(["Go", "Two", "Extra"], name="R", keyboard=True),
    ]
    real = 1
    for observation in steps:
        action = eng.next(observation)
        if isinstance(action, Tap):
            assert eng.recheck(action, observation)
            eng.tap_sent(action)
            real += 1
    assert real == 3
    # the keyboard BACK; the screen comes back with the first row labels
    action = press(eng, on_screen(["Go", "Two"], name="R"))
    assert len(eng._path) == 3  # the real stack: R, A, R
    real += 1
    # the tap opens a screen at the real depth 4, the limit: nothing is pressed
    action = eng.next(on_screen(["Deep"], name="A2"))
    assert not isinstance(action, Tap)
    assert real == 4


# -- keyboard after a navigation BACK, same frame pops ----------------


def test_a_keyboard_after_a_navigation_back_does_not_resync_the_path() -> None:
    # Real stack: R, then A on top. BACK from A returns to R, which shows the
    # keyboard and an extra row while it is up (so another screen id).
    eng = started()
    root = on_screen(["Go", "Two"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    eng.tap_sent(go)
    assert eng.next(on_screen([], name="A")) == Back()  # navigation BACK
    shown = on_screen(["Go", "Two", "Extra"], name="R", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK, not navigation
    action = eng.next(root)  # the screen under A: the predicted landing
    assert isinstance(action, Tap)
    assert action.element_id == "e3"  # "Two"
    assert eng.recheck(action, root)
    eng.tap_sent(action)
    assert len(eng._path) == 1


def test_a_refused_recheck_reread_below_the_top_pops_instead_of_a_back() -> None:
    eng = started()
    root = on_screen(["Go", "Two"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    eng.tap_sent(go)
    inner = eng.next(on_screen(["x"], name="A"))
    assert isinstance(inner, Tap)
    assert not eng.recheck(inner, root)  # A closed by itself; R is shown
    action = eng.next(root)
    assert isinstance(action, Tap)  # still the root: never a BACK
    assert action.element_id == "e3"
    assert len(eng._path) == 1  # A was popped, not replaced


def test_label_only_refusals_in_a_row_across_screens_are_bounded() -> None:
    # a frozen clock, another screen id on every next(), labels change at
    # every recheck: nothing is ever sent, yet the crawl stops
    def screen(index, tick):
        return seen(_row_screen(f"Synced {tick} s ago", name=f"s{index}"))

    eng = started()
    steps = 0
    action = eng.next(screen(0, 0))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, screen(steps - 1, steps))
        action = eng.next(screen(steps, steps))
    assert steps == crawl.MAX_VOLATILE
    assert action == Stop("exhausted")
    assert eng.actions_sent == 1


def test_label_only_refusals_in_a_row_on_an_inner_screen_end_with_a_back() -> None:
    eng = started()
    root = on_screen(["Go"], name="root")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    eng.tap_sent(go)

    def screen(index, tick):
        return seen(_row_screen(f"Synced {tick} s ago", name=f"i{index}"))

    steps = 0
    action = eng.next(screen(0, 0))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, screen(steps - 1, steps))
        action = eng.next(screen(steps, steps))
    assert steps == crawl.MAX_VOLATILE
    assert action == Back()


def _deferred_base(policy=POLICY):
    eng = started(policy=policy)
    root = on_screen(["Go"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    eng.tap_sent(go)
    assert eng.next(on_screen([], name="X")) == Back()  # navigation BACK
    return eng


def test_a_deferred_landing_where_back_did_nothing_gets_a_relaunch() -> None:
    # the BACK did nothing; the keyboard came up over X with an extra row
    eng = _deferred_base()
    shown = on_screen(["Extra"], name="X", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK
    assert eng.next(on_screen([], name="X")) == Relaunch()


def test_a_deferred_unpredicted_landing_is_resynced_to_the_depth_limit() -> None:
    eng = _deferred_base()
    shown = on_screen(["Y1", "Extra"], name="Y", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK
    assert eng.next(on_screen(["Y1"], name="Y")) == Back()
    assert len(eng._path) == eng._limits.depth


def test_off_target_after_the_keyboard_back_drops_the_deferral() -> None:
    eng = _deferred_base(BACK_POLICY)
    shown = on_screen(["Y1", "Extra"], name="Y", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK
    assert eng.next(off_screen()) == Back()  # off target: the deferral is gone
    assert eng._deferred is None
    # a later screen X is not taken as the deferred navigation BACK's landing
    assert eng.next(on_screen([], name="X")) != Relaunch()


def test_off_target_after_the_keyboard_back_relaunches_by_default() -> None:
    eng = _deferred_base()
    shown = on_screen(["Y1", "Extra"], name="Y", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK, on a target screen
    assert eng.next(off_screen()) == Relaunch()  # nothing goes to the launcher
    assert eng._deferred is None


def test_a_stuck_keyboard_after_a_deferred_landing_stops() -> None:
    # the navigation BACK lands on X with the keyboard shown; the keyboard BACK
    # leaves it shown, so the crawl stops instead of relaunching
    eng = _deferred_base()
    assert eng.next(on_screen([], name="X", keyboard=True)) == Back()
    assert eng.next(on_screen([], name="X", keyboard=True)) == Stop("keyboard_stuck")


def test_a_former_root_reached_by_back_is_not_a_root() -> None:
    eng = back_started()
    a = on_screen(["a1"], name="A")
    tap = eng.next(a)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, a)
    eng.tap_sent(tap)
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Relaunch()
    c = on_screen(["c1", "c2"], name="C")
    tap = eng.next(c)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, c)
    eng.tap_sent(tap)
    assert eng.next(on_screen([], name="D")) == Back()  # navigation BACK
    # the BACK lands on A, the former root: it is above the real root
    assert eng.next(a) == Back()
    tap = eng.next(c)
    assert isinstance(tap, Tap)
    assert tap.fingerprint != fingerprint_of(c.screen, c.screen.elements[1])


def _root_replaced_then_unpredicted_landing(eng, root):
    # The root was replaced in place. Then root -> X -> Y (navigation BACK) and
    # an unpredicted landing Z is left by BACK onto the root: it stays the root.
    press(eng, root)
    press(eng, on_screen(["x1"], name="X"))
    assert eng.next(on_screen([], name="Y")) == Back()
    assert eng.next(on_screen([], name="Z")) == Back()
    last = eng.next(root)
    assert isinstance(last, Tap)  # the root still has its second element
    assert eng.recheck(last, root)
    eng.tap_sent(last)
    assert eng.next(on_screen([], name="W")) == Back()
    assert eng.next(root) == Stop("exhausted")


def test_a_root_replaced_by_a_refused_recheck_stays_the_root() -> None:
    eng = started()
    first = on_screen(["r1", "r2"], name="A")
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    fresh = on_screen(["r1", "r2"], name="B")
    assert not eng.recheck(tap, fresh)
    _root_replaced_then_unpredicted_landing(eng, fresh)


def test_a_root_replaced_by_the_keyboard_back_stays_the_root() -> None:
    eng = started()
    assert eng.next(on_screen(["r1", "r2"], name="R", keyboard=True)) == Back()
    _root_replaced_then_unpredicted_landing(eng, on_screen(["r1", "r2"], name="R2"))
