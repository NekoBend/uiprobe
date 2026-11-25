"""Tests for the crawl engine's rows labelled by children and volatile elements."""

from __future__ import annotations

from tests.crawl.helpers import (
    _row_screen,
    button,
    clone,
    make_screen,
    on_screen,
    seen,
    started,
)
from uiprobe import crawl
from uiprobe.crawl import (
    Stop,
    Tap,
    fingerprint_of,
)
from uiprobe.model import Bounds, Screen


def test_a_changed_child_label_refuses_the_tap() -> None:
    eng = started()
    first = seen(_row_screen("Item a"))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, seen(_row_screen("Item b")))


def test_an_unchanged_child_label_passes_recheck() -> None:
    eng = started()
    first = seen(_row_screen("Item a"))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, seen(_row_screen("Item a")))
    eng.tap_sent(tap)


def test_a_moved_child_refuses_the_tap() -> None:
    eng = started()
    first = seen(_row_screen("Item a"))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    moved = _row_screen("Item a")
    child = moved.elements[2]
    moved = Screen(
        moved.serial,
        moved.captured_at,
        moved.package,
        moved.activity,
        moved.api_level,
        moved.display,
        (*moved.elements[:2], clone(child, bounds=Bounds(11, 60, 300, 100))),
    )
    assert not eng.recheck(tap, seen(moved))


def test_rows_labelled_only_by_children_get_different_fingerprints_and_all_run() -> (
    None
):
    screen = _row_screen("Wi-Fi", "Bluetooth", rows=2)
    rows = [e for e in screen.elements if "clickable" in e.flags]
    assert fingerprint_of(screen, rows[0]) != fingerprint_of(screen, rows[1])
    eng = started()
    observation = seen(screen)
    first = eng.next(observation)
    assert isinstance(first, Tap)
    assert eng.recheck(first, observation)
    eng.tap_sent(first)
    # the tap led nowhere new: the same screen again, the second row is next
    second = eng.next(observation)
    assert isinstance(second, Tap)
    assert second.fingerprint != first.fingerprint


# -- volatile elements --------------------------------------------------


def _ticking(clock):
    base = _row_screen(f"Synced {clock} seconds ago")
    elements = [*base.elements, button("e9", "Other", 4)]
    return Screen(
        base.serial,
        base.captured_at,
        base.package,
        base.activity,
        base.api_level,
        base.display,
        tuple(elements),
    )


def test_a_ticking_row_does_not_end_the_crawl_before_the_other_button() -> None:
    eng = started()
    tapped: list[str] = []
    clock = 0
    for _ in range(10):
        action = eng.next(seen(_ticking(clock)))
        clock += 1
        if isinstance(action, Stop):
            break
        assert isinstance(action, Tap)
        allowed = eng.recheck(action, seen(_ticking(clock)))
        clock += 1
        if allowed:
            tapped.append(action.element_id)
    assert action == Stop("exhausted")
    assert tapped == ["e9"]
    reasons = [r.reasons for r in eng.records.skipped]
    assert reasons.count(("label_changes",)) == 1


def test_two_rows_labelled_by_children_are_both_tried_when_the_labels_change_too() -> (
    None
):
    # rows of a list share a structure but not their bounds: the first row's
    # label changes before the press, the second row is still tried
    eng = started()
    first = eng.next(seen(_row_screen("Wi-Fi", "Bluetooth", rows=2)))
    assert isinstance(first, Tap)
    assert not eng.recheck(first, seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2)))
    second = eng.next(seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2)))
    assert isinstance(second, Tap)
    assert second.element_id != first.element_id
    assert eng.recheck(second, seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2)))
    eng.tap_sent(second)
    assert eng.next(seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2))) == Stop(
        "exhausted"
    )
    reasons = [r.reasons for r in eng.records.skipped]
    assert reasons.count(("label_changes",)) == 1


def _moving_ticker(index, clock):
    """One clickable counter that moves with ``index``; the screen id stays."""
    base = make_screen(["x"], name="mover", rid="x:id/b")
    button_at = clone(
        base.elements[1],
        text=f"T {clock}",
        bounds=Bounds(40, 60 + index * 20, 280, 100 + index * 20),
    )
    return Screen(
        base.serial,
        base.captured_at,
        base.package,
        base.activity,
        base.api_level,
        base.display,
        (base.elements[0], button_at),
    )


def test_label_only_refusals_do_not_use_up_the_refusal_count() -> None:
    # three ticking counters and an Other button: Other is still tapped
    def screen(clock):
        return on_screen(
            [f"Steps {clock}", f"Battery {clock}%", f"Time {clock}", "Other"]
        )

    eng = started()
    tapped = []
    clock = 0
    for _ in range(12):
        action = eng.next(screen(clock))
        clock += 1
        if isinstance(action, Stop):
            break
        assert isinstance(action, Tap)
        if eng.recheck(action, screen(clock)):
            tapped.append(action.element_id)
        clock += 1
    assert action == Stop("exhausted")
    assert tapped == ["e5"]


def test_a_screen_with_a_new_volatile_key_on_every_read_reaches_the_bound() -> None:
    eng = started()
    steps = 0
    action = eng.next(seen(_moving_ticker(0, 0)))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, seen(_moving_ticker(steps - 1, steps)))
        action = eng.next(seen(_moving_ticker(steps, steps)))
    assert steps == crawl.MAX_VOLATILE
    assert action == Stop("exhausted")
    assert eng.actions_sent == 1


def test_a_ticking_row_inside_a_list_does_not_block_the_other_rows() -> None:
    eng = started()
    first = eng.next(seen(_row_screen("T 0", "B", "C", rows=3)))
    assert isinstance(first, Tap)
    assert first.element_id == "e2"
    assert not eng.recheck(first, seen(_row_screen("T 1", "B", "C", rows=3)))
    taps = []
    for clock in range(2, 8):
        action = eng.next(seen(_row_screen(f"T {clock}", "B", "C", rows=3)))
        if isinstance(action, Stop):
            break
        assert isinstance(action, Tap)
        assert eng.recheck(action, seen(_row_screen(f"T {clock}x", "B", "C", rows=3)))
        eng.tap_sent(action)
        taps.append(action.element_id)
    assert taps == ["e4", "e6"]
    assert action == Stop("exhausted")


def test_a_volatile_mark_belongs_to_one_screen_id() -> None:
    eng = started()
    tap = eng.next(seen(_row_screen("T 0", name="one")))
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, seen(_row_screen("T 1", name="one")))
    # the same structure on a screen with another id is its own element
    again = eng.next(seen(_row_screen("T 2", name="two")))
    assert isinstance(again, Tap)
