"""Tests for the crawl engine: off-target screens, relaunches and refusal counts."""

from __future__ import annotations

import random
from dataclasses import fields

import pytest

from tests.crawl.helpers import (
    OTHER,
    PERMISSION,
    PKG,
    Node,
    Phone,
    back_engine,
    back_started,
    drive,
    engine,
    make_screen,
    off_screen,
    on_screen,
    press,
    random_graph,
    reachable_targets,
    seen,
    started,
)
from uiprobe import crawl
from uiprobe.crawl import (
    Back,
    Limits,
    Observation,
    Relaunch,
    SkippedElement,
    Stop,
    Tap,
    fingerprint_of,
)
from uiprobe.guards import Policy

# -- range ---------------------------------------------------------------------


def test_off_target_is_back_back_relaunch_then_relaunch_limit() -> None:
    eng = back_started()
    actions = [eng.next(off_screen()) for _ in range(3 * 3 + 3)]
    expected = [Back(), Back(), Relaunch()] * 3 + [
        Back(),
        Back(),
        Stop("relaunch_limit"),
    ]
    assert actions == expected
    assert eng.records.actions_sent == 1 + 3 * 3 + 2
    assert eng.next(off_screen()) == Stop("relaunch_limit")  # stays stopped


def test_off_target_is_a_relaunch_then_relaunch_limit_by_default() -> None:
    # Nothing is sent to a screen of another app: no BACK, only relaunches of
    # the target app, and no 4th relaunch without a sent tap.
    eng = started()
    actions = [eng.next(off_screen()) for _ in range(4)]
    assert actions == [Relaunch(), Relaunch(), Relaunch(), Stop("relaunch_limit")]
    assert eng.records.actions_sent == 1 + 3
    assert [t.kind for t in eng.records.transitions] == [
        "launch",
        "relaunch",
        "relaunch",
        "relaunch",
    ]


def test_a_sent_tap_resets_the_relaunch_count_by_default() -> None:
    eng = started()
    assert eng.next(off_screen()) == Relaunch()
    assert eng.next(off_screen()) == Relaunch()
    press(eng, on_screen(["Item a"]))
    for _ in range(3):
        assert eng.next(off_screen()) == Relaunch()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_a_back_that_leaves_the_app_is_followed_by_a_relaunch_by_default() -> None:
    # The engine took B for a screen above the root, but B was the bottom of
    # the real back stack: its navigation BACK leaves the app. The launcher
    # in front then gets nothing; the app is relaunched.
    eng = started()
    press(eng, on_screen(["Go"], name="A"))
    assert eng.next(on_screen([], name="B")) == Back()  # navigation BACK
    assert eng.next(off_screen()) == Relaunch()
    assert eng.records.transitions[-1].kind == "back"
    eng = back_started()
    press(eng, on_screen(["Go"], name="A"))
    assert eng.next(on_screen([], name="B")) == Back()
    assert eng.next(off_screen()) == Back()  # with BACK on other screens


def test_a_permission_dialog_after_a_tap_gets_a_relaunch_by_default() -> None:
    eng = started()
    tap = press(eng, on_screen(["Item a"]))
    dialog = seen(
        make_screen(["Allow", "Deny"], name="dialog", package=PERMISSION),
        fg=PERMISSION,
    )
    assert eng.next(dialog) == Relaunch()
    # the pressed element stays tried, so the relaunch does not repeat it
    after = on_screen(["Item a"])
    assert eng.next(after) == Stop("exhausted")
    assert tap.fingerprint == fingerprint_of(after.screen, after.screen.elements[1])


@pytest.mark.parametrize(
    ("screen_package", "foreground"),
    [(PKG, None), (PKG, OTHER), (OTHER, PKG), (PERMISSION, PERMISSION)],
)
def test_every_off_target_screen_gets_a_relaunch_by_default(
    screen_package, foreground
) -> None:
    eng = started()
    screen = make_screen(["Item a"], package=screen_package)
    action = eng.next(Observation(screen, foreground, keyboard_shown=False, now=0.0))
    assert action == Relaunch()


def test_random_graphs_never_send_back_off_target_by_default() -> None:
    rng = random.Random(20261120)  # noqa: S311
    offs = 0
    for _ in range(300):
        nodes = random_graph(rng)
        phone = Phone(nodes)
        eng = engine(Limits(depth=rng.randint(1, 6), actions=200, minutes=30))
        observations: list[Observation] = []
        log = drive(eng, phone, watch=observations.append)
        # log[0] is the launch; each later action was decided on one observation
        for observation, action in zip(observations, log[1:], strict=True):
            screen = observation.screen
            assert screen is not None
            if screen.package != PKG or observation.foreground != PKG:
                offs += 1
                assert isinstance(action, Relaunch | Stop), action
    assert offs > 50  # the graphs really reach permission screens


def test_random_graphs_of_one_app_visit_every_reachable_screen_by_default() -> None:
    # Without screens of other apps the default changes nothing.
    rng = random.Random(7)  # noqa: S311
    checked = 0
    for _ in range(300):
        nodes = random_graph(rng, links_to_start=rng.random() < 0.5)
        if any(node.package != PKG for node in nodes):
            continue
        checked += 1
        phone = Phone(nodes)
        eng = engine(Limits(depth=40, actions=600, minutes=30))
        log = drive(eng, phone, depth=40)
        assert not any(isinstance(a, Relaunch) for a in log)
        assert log[-1] == Stop("exhausted")
        assert {"n0", *phone.visited} >= reachable_targets(nodes)
    assert checked > 50


def test_a_relaunch_off_target_leaves_the_rest_of_that_screen_unvisited() -> None:
    # Pinned cost of the default (a stated limit): a tap on A opens a dialog
    # of another app; the relaunch starts again at the root, whose only button
    # is tried already, so A's other buttons are never pressed. With BACK on
    # other screens the dialog is closed and A's buttons are tried.
    nodes = [
        Node("n0", (("Item a", "a"),)),
        Node("a", (("Item a", "perm"), ("Item b", "b"))),
        Node("b"),
        Node("perm", (("Allow", "b"),), package=PERMISSION),
    ]
    # (the path replay, Policy.replay_path, is what removes this cost)
    phone = Phone(nodes)
    log = drive(engine(policy=Policy(replay_path=False)), phone)
    assert log[-1] == Stop("exhausted")
    assert "b" not in phone.visited
    phone = Phone(nodes)
    log = drive(engine(), phone)
    assert log[-1] == Stop("exhausted")
    assert "b" in phone.visited
    phone = Phone(nodes)
    log = drive(back_engine(), phone)
    assert log[-1] == Stop("exhausted")
    assert "b" in phone.visited


def test_a_sent_tap_resets_the_relaunch_count() -> None:
    eng = back_started()
    relaunches = 0
    for _ in range(2):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
        relaunches += 1
    screen = on_screen(["Item a"])
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, screen)
    eng.tap_sent(tap)
    # the tap led off target; three more relaunches fit before the limit
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
        relaunches += 1
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")
    assert relaunches == 5


def test_a_target_screen_alone_does_not_reset_the_relaunch_count() -> None:
    eng = back_started()
    for _ in range(3):
        assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
    # a target screen is seen between the relaunches, but no tap was sent
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_an_unsent_tap_does_not_reset_the_relaunch_count() -> None:
    eng = back_started()
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
    tap = eng.next(on_screen(["Item a"]))
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, on_screen(["Item changed"]))  # refused: not sent
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_an_allowed_recheck_and_a_reported_send_reset_the_relaunch_count() -> None:
    eng = back_started()
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
    screen = on_screen(["Item a"])
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, screen)
    eng.tap_sent(tap)
    for expected in (Back(), Back(), Relaunch()):
        assert eng.next(off_screen()) == expected


def test_an_allowed_recheck_without_a_reported_send_does_not_reset() -> None:
    eng = back_started()
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
    screen = on_screen(["Item a"])
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, screen)  # allowed, but the send failed
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_a_dropped_tap_does_not_permit_a_fourth_relaunch() -> None:
    eng = back_started()
    actions = [eng.next(off_screen()) for _ in range(9)]
    assert actions == [Back(), Back(), Relaunch()] * 3
    tap = eng.next(on_screen(["Item a"]))
    assert isinstance(tap, Tap)  # the driver drops it: no recheck, never sent
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_an_unreported_allowed_tap_does_not_reset_the_refusal_count() -> None:
    eng = started()
    screen = on_screen(["Item a", "Item b", "Item c", "Item d", "Item e"])
    for _ in range(2):
        tap = eng.next(screen)
        assert isinstance(tap, Tap)
        assert not eng.recheck(tap, off_screen())  # a counted refusal
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, screen)  # allowed, then dropped by the driver
    # the drop does not reset the count and itself counts as the third
    assert eng.next(screen) == Stop("exhausted")  # nothing more is pressed


def test_a_driver_that_drops_every_tap_still_ends_the_crawl() -> None:
    eng = started()
    for count in range(100):
        # a new screen id every time on a frozen clock, and the tap is dropped
        observation = on_screen([f"Item {count}", "Next"], name=f"s{count}")
        action = eng.next(observation)
        if isinstance(action, Stop):
            break
        if isinstance(action, Tap):
            assert eng.recheck(action, observation)
    assert eng.stopped is not None


def test_a_reported_tap_resets_the_refusal_count() -> None:
    eng = started()
    screen = on_screen(["Item a", "Item b", "Item c", "Item d", "Item e"])
    for _ in range(2):
        tap = eng.next(screen)
        assert isinstance(tap, Tap)
        assert not eng.recheck(tap, off_screen())
    press(eng, screen)  # sent: the tap stays on the same screen
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, off_screen())
    assert isinstance(eng.next(screen), Tap)  # one refusal in a row only


@pytest.mark.parametrize("rechecked", [True, False])
def test_a_dropped_tap_is_not_counted_recorded_or_placed(rechecked) -> None:
    eng = started(Limits(actions=2))
    screen = on_screen(["Item a", "Item b"])
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    if rechecked:
        assert eng.recheck(tap, screen)
    assert eng.actions_sent == 1
    # the driver drops the tap and reads the same screen again
    second = eng.next(screen)
    assert isinstance(second, Tap)  # no max_actions: only the launch was sent
    assert second.element_id == "e3"  # the dropped element stays tried
    assert eng.actions_sent == 1
    records = eng.records
    assert [t.kind for t in records.transitions] == ["launch"]
    assert SkippedElement(tap.screen_id, tap.fingerprint, (crawl.TAP_NOT_SENT,)) in (
        records.skipped
    )
    assert eng._path == [screen.screen.screen_id]  # the path did not move
    # the second tap is sent: now it is counted, recorded and placed
    assert eng.recheck(second, screen)
    eng.tap_sent(second)
    assert eng.actions_sent == 2
    assert eng.next(on_screen([], name="leaf")) == Stop("max_actions")
    transitions = eng.records.transitions
    assert [(t.kind, t.action_id) for t in transitions] == [("launch", 1), ("tap", 2)]
    assert transitions[1].fingerprint == second.fingerprint


def test_recheck_and_tap_sent_accept_only_the_issued_tap_instance() -> None:
    a = back_started()
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert a.next(off_screen()) == expected
    screen = on_screen(["Item a"])
    tap_a = a.next(screen)
    b = back_started()
    tap_b = b.next(screen)
    assert isinstance(tap_a, Tap)
    assert isinstance(tap_b, Tap)
    assert tap_a == tap_b
    assert tap_a is not tap_b
    with pytest.raises(ValueError, match="returned last"):
        a.recheck(tap_b, screen)
    assert a.recheck(tap_a, screen)
    with pytest.raises(ValueError, match="allowed last"):
        a.tap_sent(tap_b)
    with pytest.raises(ValueError, match="allowed last"):
        a.tap_sent(Tap(*(getattr(tap_a, f.name) for f in fields(Tap))))
    # nothing was reported for engine A: its fourth relaunch is still refused
    assert a.next(off_screen()) == Back()
    assert a.next(off_screen()) == Back()
    assert a.next(off_screen()) == Stop("relaunch_limit")


def test_tap_sent_needs_the_tap_recheck_allowed_last() -> None:
    eng = started()
    screen = on_screen(["Item a", "Item b"])
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    with pytest.raises(ValueError, match="allowed last"):
        eng.tap_sent(tap)  # not rechecked yet
    assert eng.recheck(tap, screen)
    other = Tap(tap.screen_id, "e3", tap.fingerprint, tap.point)
    with pytest.raises(ValueError, match="allowed last"):
        eng.tap_sent(other)
    eng.tap_sent(tap)
    with pytest.raises(ValueError, match="allowed last"):
        eng.tap_sent(tap)  # reported once only


def test_a_target_screen_resets_the_back_count() -> None:
    eng = back_started()
    assert eng.next(off_screen()) == Back()
    press(eng, on_screen(["Item a"]))
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Relaunch()


@pytest.mark.parametrize(
    ("screen_package", "foreground"),
    [
        (PKG, None),
        (PKG, OTHER),
        (OTHER, PKG),
        (PERMISSION, PERMISSION),
        (OTHER, None),
    ],
)
def test_target_needs_dump_package_and_foreground_to_match(
    screen_package, foreground
) -> None:
    eng = back_started()
    screen = make_screen(["Item a"], package=screen_package)
    action = eng.next(Observation(screen, foreground, keyboard_shown=False, now=0.0))
    assert action == Back()
    assert eng.start_screen_id is None


def test_a_tap_that_opens_a_permission_dialog_is_answered_with_back() -> None:
    eng = back_started()
    press(eng, on_screen(["Item a"]))
    dialog = seen(
        make_screen(["Allow", "Deny"], name="dialog", package=PERMISSION),
        fg=PERMISSION,
    )
    assert eng.next(dialog) == Back()
    assert eng.records.transitions[-1].to_screen_id == dialog.screen.screen_id
