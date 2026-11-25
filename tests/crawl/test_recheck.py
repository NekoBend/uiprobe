"""Tests for the crawl engine's re-check before a tap."""

from __future__ import annotations

import pytest

from tests.crawl.helpers import (
    BUTTON,
    DISPLAY,
    OTHER,
    PKG,
    POLICY,
    ROW,
    back_started,
    clone,
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
    Limits,
    Observation,
    Relaunch,
    Reread,
    SkippedElement,
    Stop,
    Tap,
    fingerprint_of,
)
from uiprobe.guards import MAX_ELEMENTS, Policy, judge
from uiprobe.model import Bounds, Element, Screen

# -- re-check ------------------------------------------------------------------


def planned_tap(labels=("Item a",)):
    eng = started()
    first = on_screen(list(labels))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    return eng, first, tap


def test_recheck_allows_the_same_element() -> None:
    eng, first, tap = planned_tap()
    assert eng.recheck(tap, first)
    eng.tap_sent(tap)
    assert eng.records.skipped == ()
    assert eng.actions_sent == 2


def _changed_screen(*, display=DISPLAY, **changes):
    screen = make_screen(["Item a"])
    return Screen(
        screen.serial,
        screen.captured_at,
        screen.package,
        screen.activity,
        screen.api_level,
        display,
        (screen.elements[0], clone(screen.elements[1], **changes)),
    )


@pytest.mark.parametrize(
    "fresh",
    [
        pytest.param(seen(_changed_screen(resource_id="x:id/y")), id="resource_id"),
        pytest.param(seen(_changed_screen(class_name=ROW)), id="class"),
        pytest.param(
            seen(_changed_screen(bounds=Bounds(40, 61, 280, 101))), id="bounds"
        ),
        # same centre, so judge gives the same point: only the bounds differ
        pytest.param(
            seen(_changed_screen(bounds=Bounds(30, 50, 290, 110))),
            id="bounds-same-point",
        ),
        # same bounds, but the display is narrower, so judge's point moves
        pytest.param(seen(_changed_screen(display=(200, 640))), id="point-moved"),
        pytest.param(
            seen(_changed_screen(flags=("clickable", "disabled"))), id="judge"
        ),
        pytest.param(seen(make_screen(["Item a"]), keyboard=True), id="keyboard"),
        pytest.param(seen(_changed_screen(flags=())), id="not-clickable"),
        pytest.param(seen(make_screen(["Item a"], name="other")), id="screen_id"),
        pytest.param(
            seen(make_screen(["Item a"], package=OTHER), fg=OTHER), id="package"
        ),
        pytest.param(seen(make_screen(["Item a"]), fg=OTHER), id="foreground"),
        pytest.param(seen(make_screen(["Item a"]), fg=None), id="foreground-unknown"),
        pytest.param(
            Observation(None, PKG, keyboard_shown=None, now=0.0), id="failed-dump"
        ),
        pytest.param(seen(make_screen([])), id="gone"),
    ],
)
def test_recheck_refuses_a_changed_screen_and_records_the_skip(fresh) -> None:
    eng, first, tap = planned_tap()
    assert not eng.recheck(tap, fresh)
    assert eng.actions_sent == 1  # the tap was not sent
    assert eng.records.skipped == (
        SkippedElement(tap.screen_id, tap.fingerprint, ("changed_before_press",)),
    )
    # the element is tried: the same screen does not give the same tap again
    assert eng.next(first) == Stop("exhausted")


@pytest.mark.parametrize(
    "changes",
    [{"text": "Other"}, {"content_desc": "d"}, {"hint": "h"}],
    ids=["text", "content_desc", "hint"],
)
def test_recheck_refuses_a_label_change_and_marks_the_element_volatile(
    changes,
) -> None:
    eng, first, tap = planned_tap()
    assert not eng.recheck(tap, seen(_changed_screen(**changes)))
    assert eng.actions_sent == 1
    assert eng.records.skipped == (
        SkippedElement(tap.screen_id, tap.fingerprint, ("changed_before_press",)),
        SkippedElement(tap.screen_id, tap.fingerprint, ("label_changes",)),
    )
    assert eng.next(first) == Stop("exhausted")


def test_recheck_refusal_is_not_a_transition_and_does_not_count() -> None:
    eng, _first, tap = planned_tap()
    assert not eng.recheck(tap, seen(_changed_screen(text="Other")))
    assert [t.kind for t in eng.records.transitions] == ["launch"]


def test_a_refused_tap_leaves_no_pending_transition_behind() -> None:
    eng, _first, tap = planned_tap()
    fresh = seen(_changed_screen(resource_id="x:id/y"))
    assert not eng.recheck(tap, fresh)
    second = press(eng, fresh)
    assert eng.next(on_screen([], name="leaf")) == Back()
    transitions = eng.records.transitions
    assert [t.kind for t in transitions] == ["launch", "tap"]
    assert [t.action_id for t in transitions] == [1, 2]
    assert transitions[1].fingerprint == second.fingerprint


def test_refusals_alternating_between_two_screens_still_reach_the_cap() -> None:
    # The clock never moves and every read has a new label, so only the refusal
    # cap can end this; it counts across screens.
    def screen_a(i):
        return on_screen([f"Live {i}"], name="a", now=0.0)

    def screen_b(i):
        return on_screen([f"Live {i}"], name="b", now=0.0)

    eng = started()
    action = eng.next(screen_a(0))
    assert isinstance(action, Tap)
    assert eng.recheck(action, screen_a(0))  # sent: the count is zero
    eng.tap_sent(action)
    action = eng.next(screen_b(0))
    assert isinstance(action, Tap)  # b is depth 2
    sent = eng.actions_sent
    for i in range(1, 20):
        fresh = (screen_a if i % 2 else screen_b)(i)
        assert not eng.recheck(action, fresh)
        action = eng.next(fresh)
        if not isinstance(action, Tap):
            break
    else:
        pytest.fail("the refusals never reached the cap")
    assert i <= crawl.MAX_REFUSALS
    assert isinstance(action, Back | Stop)
    assert eng.actions_sent <= sent + 1


def test_a_label_that_changes_on_every_read_is_tried_once_and_skipped() -> None:
    eng = started()
    steps = 0
    action = eng.next(on_screen(["Timer 0"]))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, on_screen([f"Timer {steps}x"]))
        action = eng.next(on_screen([f"Timer {steps}"]))
    assert steps == 1  # marked volatile: the later reads are skipped
    assert action == Stop("exhausted")  # the start screen
    assert eng.actions_sent == 1
    reasons = [r.reasons for r in eng.records.skipped]
    assert reasons.count(("label_changes",)) == 1


def test_a_structure_that_changes_on_every_read_does_not_livelock() -> None:
    eng = started()
    steps = 0
    action = eng.next(on_screen(["Timer 0"], rid="x:id/r0"))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, on_screen(["Timer 0"], rid=f"x:id/s{steps}"))
        action = eng.next(on_screen(["Timer 0"], rid=f"x:id/r{steps}"))
    assert steps == 3  # MAX_REFUSALS refusals, then the screen counts as done
    assert action == Stop("exhausted")  # the start screen
    assert eng.actions_sent == 1


def test_refusals_on_an_inner_screen_end_with_back() -> None:
    eng = started()
    press(eng, on_screen(["Item a"]))
    action = eng.next(on_screen(["Timer"], name="inner", rid="x:id/r0"))
    for i in range(3):
        assert isinstance(action, Tap)
        fresh = on_screen(["Timer"], name="inner", rid=f"x:id/s{i}")
        assert not eng.recheck(action, fresh)
        action = eng.next(on_screen(["Timer"], name="inner", rid=f"x:id/r{i + 1}"))
    assert action == Back()


def test_an_allowed_recheck_resets_the_refusal_count() -> None:
    eng = started()
    action = eng.next(on_screen(["Timer", "Item b"], rid="x:id/a0"))
    assert isinstance(action, Tap)
    assert not eng.recheck(action, on_screen(["Timer", "Item b"], rid="x:id/b0"))
    action = eng.next(on_screen(["Timer", "Item b"], rid="x:id/a1"))
    assert isinstance(action, Tap)
    assert not eng.recheck(action, on_screen(["Timer", "Item b"], rid="x:id/b1"))
    action = eng.next(on_screen(["Timer", "Item b"], rid="x:id/a2"))
    assert isinstance(action, Tap)
    same = on_screen(["Timer", "Item b"], rid="x:id/a2")
    assert eng.recheck(action, same)
    eng.tap_sent(action)


def test_recheck_judge_decides_again_on_the_fresh_screen() -> None:
    eng, _, tap = planned_tap()
    fresh = seen(_changed_screen(flags=("clickable", "disabled")))
    assert fresh.screen is not None
    judgement = judge(fresh.screen, PKG, POLICY, foreground=PKG)
    assert not judgement.pressable
    assert not eng.recheck(tap, fresh)


def test_recheck_rejects_a_tap_the_engine_did_not_hand_out() -> None:
    eng, first, tap = planned_tap()
    other = Tap(tap.screen_id, tap.element_id, tap.fingerprint, (1, 1))
    with pytest.raises(ValueError, match="last"):
        eng.recheck(other, first)
    assert not eng.recheck(tap, seen(_changed_screen(text="Other")))
    with pytest.raises(ValueError, match="last"):
        eng.recheck(tap, first)  # a refusal is final


def _swapped_twins(screen):
    first, second = screen.elements[1], screen.elements[2]
    return Screen(
        screen.serial,
        screen.captured_at,
        screen.package,
        screen.activity,
        screen.api_level,
        screen.display,
        (
            screen.elements[0],
            clone(first, bounds=second.bounds),
            clone(second, bounds=first.bounds),
        ),
    )


def test_recheck_picks_the_first_pressable_twin() -> None:
    twin = on_screen(["Same", "Same"], rid="x:id/same")
    eng = started()
    tap = eng.next(twin)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, twin)
    eng.tap_sent(tap)
    # the twins swapped places: the first one now has the other one's bounds
    eng = started()
    tap = eng.next(twin)
    assert isinstance(tap, Tap)
    assert twin.screen is not None
    assert not eng.recheck(tap, seen(_swapped_twins(twin.screen)))


def test_recheck_refuses_a_look_alike_twin_at_the_planned_point() -> None:
    # e2 is planned; in the fresh dump the twins swapped bounds and e2 is
    # disabled, so only e3 is pressable, and it sits at e2's planned point.
    twin = on_screen(["Same", "Same"], rid="x:id/same")
    assert twin.screen is not None
    eng = started()
    tap = eng.next(twin)
    assert isinstance(tap, Tap)
    assert tap.element_id == "e2"
    swapped = _swapped_twins(twin.screen)
    root, e2, e3 = swapped.elements
    fresh = _with_elements(
        swapped, (root, clone(e2, flags=("clickable", "disabled")), e3)
    )
    observation = seen(fresh)
    assert fresh.screen_id == tap.screen_id
    pressable = judge(fresh, PKG, POLICY, foreground=PKG).pressable
    assert [(d.element.id, d.point) for d in pressable] == [("e3", tap.point)]
    assert fingerprint_of(fresh, e3) == tap.fingerprint
    assert not eng.recheck(tap, observation)


def _with_elements(screen, elements):
    return Screen(
        screen.serial,
        screen.captured_at,
        screen.package,
        screen.activity,
        screen.api_level,
        screen.display,
        tuple(elements),
    )


def test_recheck_refuses_a_checkbox_that_became_checked() -> None:
    policy = Policy(skip_toggles=False)
    box = on_screen(["Notify me"], cls="android.widget.CheckBox")
    assert box.screen is not None
    root, e2 = box.screen.elements
    unchecked = _with_elements(
        box.screen, (root, clone(e2, flags=("clickable", "checkable")))
    )
    checked = _with_elements(
        box.screen, (root, clone(e2, flags=("clickable", "checkable", "checked")))
    )
    eng = started(policy=policy)
    tap = eng.next(seen(unchecked))
    assert isinstance(tap, Tap)
    assert [
        d.element.id for d in judge(checked, PKG, policy, foreground=PKG).pressable
    ] == ["e2"]
    assert not eng.recheck(tap, seen(checked))
    # the same frame read again is still allowed
    eng = started(policy=policy)
    tap = eng.next(seen(unchecked))
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, seen(unchecked))
    eng.tap_sent(tap)


@pytest.mark.parametrize(
    "change",
    [
        {"flags": ("checkable", "checked")},
        {"flags": ("checkable", "selected")},
        {"flags": ("checkable", "focused")},
        {"package": OTHER},
        {"parent": "e1"},
    ],
)
def test_recheck_refuses_any_change_of_a_descendant(change) -> None:
    policy = Policy(skip_toggles=False)
    row = Element(
        "e2",
        "e1",
        BUTTON,
        Bounds(40, 60, 280, 100),
        text="Wi-Fi",
        resource_id="x:id/row",
        package=PKG,
        flags=("clickable",),
    )
    check = Element(
        "e3",
        "e2",
        "android.widget.CheckBox",
        Bounds(240, 70, 270, 90),
        resource_id="x:id/check",
        package=PKG,
        flags=("checkable",),
    )
    base = make_screen([])
    before = _with_elements(base, (base.elements[0], row, check))
    after = _with_elements(base, (base.elements[0], row, clone(check, **change)))
    eng = started(policy=policy)
    tap = eng.next(seen(before))
    assert isinstance(tap, Tap)
    assert tap.element_id == "e2"
    assert not eng.recheck(tap, seen(after))


def test_a_screen_with_duplicate_element_ids_counts_as_a_failed_dump() -> None:
    good = make_screen(["Item a", "Item b"])
    root, a, b = good.elements
    bad = _with_elements(good, (root, a, clone(b, id="e2")))
    with pytest.raises(ValueError, match="unique"):
        judge(bad, PKG, POLICY, foreground=PKG)
    eng = started()
    assert eng.next(seen(bad)) == Reread()
    assert eng.next(seen(bad)) == Reread()
    assert eng.next(seen(bad)) == Stop("dump_failed_repeatedly")
    assert eng.records.transitions[0].dump_failed


def test_recheck_on_duplicate_element_ids_refuses_and_releases_the_tap() -> None:
    good = make_screen(["Item a", "Item b"])
    root, a, b = good.elements
    bad = _with_elements(good, (root, a, clone(b, id="e2")))
    eng = started()
    tap = eng.next(seen(good))
    assert isinstance(tap, Tap)
    assert eng.actions_sent == 1  # a proposed tap is not counted
    assert not eng.recheck(tap, seen(bad))
    assert eng.actions_sent == 1
    with pytest.raises(ValueError, match="allowed last"):
        eng.tap_sent(tap)
    # the engine goes on from the next good frame
    press(eng, seen(good))


def test_an_oversized_screen_without_candidates_gets_its_skip_row() -> None:
    base = make_screen([], name="big")
    rows = [
        Element(f"e{i}", "e1", ROW, Bounds(0, 0, 10, 10), text="x", package=PKG)
        for i in range(2, MAX_ELEMENTS + 3)
    ]
    big = _with_elements(base, (base.elements[0], *rows))
    assert len(big.elements) > MAX_ELEMENTS
    eng = started()
    assert eng.next(seen(big)) == Stop("exhausted")
    skipped = eng.records.skipped
    assert [(r.screen_id, r.reasons) for r in skipped] == [
        (big.screen_id, ("screen_too_large",))
    ]


def test_a_second_recheck_after_an_allowed_one_is_rejected() -> None:
    eng, first, tap = planned_tap()
    assert eng.recheck(tap, first)
    with pytest.raises(ValueError, match="last"):
        eng.recheck(tap, seen(_changed_screen(text="Other")))
    assert eng.actions_sent == 1  # allowed, but not counted before tap_sent
    eng.tap_sent(tap)
    assert eng.actions_sent == 2  # the sent tap is counted


# -- same frame, singleton unknown path, deadline, descendants ----


def test_a_refused_tap_with_a_new_screen_id_is_not_a_back_on_the_root() -> None:
    eng = started()
    first = on_screen(["Item a"])
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    fresh = on_screen(["Item a", "Item b"])
    assert fresh.screen is not None
    assert first.screen is not None
    assert fresh.screen.screen_id != first.screen.screen_id
    assert not eng.recheck(tap, fresh)
    action = eng.next(fresh)
    assert isinstance(action, Tap)  # never a Back on the root
    assert action.screen_id == fresh.screen.screen_id
    assert eng.depth_of(fresh.screen.screen_id) == 1


def test_a_refused_tap_on_a_deeper_screen_keeps_its_depth() -> None:
    eng = started(Limits(depth=3))
    root = on_screen(["Go"], name="root")
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, root)
    eng.tap_sent(tap)
    mid = on_screen(["a"], name="mid")
    tap = eng.next(mid)
    assert isinstance(tap, Tap)
    fresh = on_screen(["a", "b"], name="mid")
    assert not eng.recheck(tap, fresh)
    action = eng.next(fresh)
    assert isinstance(action, Tap)
    assert fresh.screen is not None
    assert eng.depth_of(fresh.screen.screen_id) == 2
    assert eng.recheck(action, fresh)
    eng.tap_sent(action)
    leaf = on_screen(["z"], name="leaf")
    assert isinstance(eng.next(leaf), Back)  # the depth limit 3 binds
    assert leaf.screen is not None
    assert eng.depth_of(leaf.screen.screen_id) == 3


def test_a_singleton_unknown_path_does_not_crash_the_recovery() -> None:
    eng = back_started()
    root = on_screen(["Go"], name="root")
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, root)
    eng.tap_sent(tap)
    assert isinstance(eng.next(on_screen([], name="mid")), Back)
    assert isinstance(eng.next(on_screen([], name="unknown")), Back)
    assert isinstance(eng.next(on_screen([], name="mid")), Back)
    assert isinstance(eng.next(off_screen()), Back)  # off target
    action = eng.next(on_screen([], name="unknown"))  # must not raise
    assert isinstance(action, Back | Relaunch | Stop)
