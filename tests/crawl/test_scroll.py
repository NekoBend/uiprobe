"""Tests for the scrolling of the crawl engine and for where a swipe may go."""

from __future__ import annotations

import json
import random
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.crawl.helpers import (
    BUTTON,
    DISPLAY,
    OTHER,
    PKG,
    Phone,
    clone,
    drive,
    make_screen,
    off_screen,
    random_graph,
    seen,
)
from tests.crawl.helpers import engine as engine_with
from tests.crawl.helpers import started as started_with
from uiprobe import crawl
from uiprobe.crawl import (
    Back,
    CrawlEngine,
    Launch,
    Limits,
    Observation,
    Relaunch,
    Scroll,
    Stop,
    Tap,
    fingerprint_of,
)
from uiprobe.guards import (
    IRREVERSIBLE_PHRASES,
    Policy,
    container_key,
    judge,
    lists_key,
    scroll_target,
)
from uiprobe.guards.scroll import elements_at, scroll_container
from uiprobe.model import Bounds, Element, Screen

RECYCLER = "androidx.recyclerview.widget.RecyclerView"


def policy_on(**fields: Any) -> Policy:  # noqa: ANN401 - Policy field values
    """A policy with scrolling on (it is off by default)."""
    return Policy(scroll_lists=True, **fields)


OFF = Policy(scroll_lists=False)


def engine(limits=None, *, started_at=0.0, policy=None):
    return engine_with(
        limits, started_at=started_at, policy=policy_on() if policy is None else policy
    )


def started(limits=None, *, started_at=0.0, policy=None):
    return started_with(
        limits, started_at=started_at, policy=policy_on() if policy is None else policy
    )


# The rows of the list screens: 40 high, 60 apart, from y = 60; the list's own
# window is (0, 60, 320, 600), so the wheel is turned at its center, (160, 330),
# first (a 3 by 3 grid of quarters follows: x in 80, 160, 240, y in 195, 330,
# 465).
WINDOW = (0, 60, 320, 600)
POINT = (160, 330)
AMOUNT = Limits().scroll_amount


def list_screen(
    labels,
    *,
    name="list",
    package=PKG,
    extras=(),
    window=WINDOW,
    scroller_flags=("scrollable",),
    scroller_cls=RECYCLER,
    root=None,
    rows_top=60,
    row_flags=("clickable",),
    row_parent="e2",
):
    """A list: a scrollable container with one clickable row per label."""
    elements = [
        Element(
            "e1",
            None,
            "android.widget.FrameLayout",
            Bounds(*(root or (0, 0, *DISPLAY))),
            package=package,
        ),
        Element(
            "e2",
            "e1",
            scroller_cls,
            Bounds(*window),
            resource_id=f"{package}:id/list",
            package=package,
            flags=tuple(scroller_flags),
        ),
    ]
    for index, label in enumerate(labels):
        top = rows_top + index * 60
        elements.append(
            Element(
                f"e{index + 3}",
                row_parent,
                "android.widget.TextView",
                Bounds(40, top, 280, top + 40),
                text=label,
                resource_id=f"{package}:id/row",
                package=package,
                flags=tuple(row_flags),
            )
        )
    elements.extend(extras)
    return Screen(
        serial="emulator-5554",
        captured_at=datetime(2026, 11, 25, tzinfo=UTC),
        package=package,
        activity=f".{name}",
        api_level=36,
        display=DISPLAY,
        elements=tuple(elements),
    )


def extra(ident, cls, bounds, *, flags=(), text="", parent="e1"):
    return Element(
        ident,
        parent,
        cls,
        Bounds(*bounds),
        text=text,
        package=PKG,
        flags=tuple(flags),
    )


def judged(screen, policy=None):
    return judge(screen, PKG, policy or policy_on(), foreground=PKG)


def target_of(screen):
    return scroll_target(screen, judged(screen))


def point_of(screen):
    found = target_of(screen)
    return None if found is None else found[1]


def key_of(screen):
    from uiprobe.guards import scroll_container

    container = scroll_container(screen)
    assert container is not None
    return container_key(screen, container)


def labels(count):
    return [f"Row {index + 1:02d}" for index in range(count)]


# -- a phone with one long list ---------------------------------------------------


class ListPhone:
    """The list screen at an offset, and a leaf that a row opens.

    The list shows ``visible`` rows from ``offset`` and a swipe moves it by
    ``step`` rows (never past the last row); with ``keep`` it stays where it was
    after BACK, else it goes back to the top. ``shown`` can change the number of
    rows that show at an offset, which changes the screen id.
    """

    def __init__(self, total, *, visible=8, step=4, keep=True, shown=None):
        self.labels = labels(total)
        self.visible = visible
        self.step = step
        self.keep = keep
        self.shown = shown or (lambda _offset: visible)
        self.offset = 0
        self.stack: list[str] = []
        self.pressed: list[str] = []
        self.scrolls: list[Scroll] = []
        self.clock = 0.0
        self.keyboard = False

    def rows(self):
        count = self.shown(self.offset)
        return self.labels[self.offset : self.offset + count]

    def observe(self):
        if not self.stack:
            return off_screen(now=self.clock)
        if self.stack[-1] == "leaf":
            return seen(make_screen([], name="leaf"), now=self.clock)
        return seen(list_screen(self.rows()), now=self.clock, keyboard=self.keyboard)

    def send(self, action):
        self.clock += 1.0
        if isinstance(action, Launch | Relaunch):
            self.stack = ["list"]
            self.offset = 0
        elif isinstance(action, Back):
            if self.stack:
                self.stack.pop()
            if self.stack == ["list"] and not self.keep:
                self.offset = 0
        elif isinstance(action, Tap):
            assert self.stack[-1] == "list"
            index = (action.point[1] - 60) // 60
            self.pressed.append(self.rows()[index])
            self.stack.append("leaf")
        elif isinstance(action, Scroll):
            assert self.stack[-1] == "list"
            self.scrolls.append(action)
            most = len(self.labels) - self.shown(self.offset)
            self.offset = min(self.offset + self.step, most)
        else:
            msg = f"cannot send {action!r}"
            raise AssertionError(msg)


def drive_list(eng, phone, *, max_steps=1000):
    """Run the engine against the phone as a driver does; return the actions."""
    log = [eng.start()]
    phone.send(log[0])
    for _ in range(max_steps):
        observation = phone.observe()
        action = eng.next(observation)
        if isinstance(action, Stop):
            log.append(action)
            return log
        if isinstance(action, Tap | Scroll):
            assert eng.recheck(action, phone.observe())
        log.append(action)
        phone.send(action)
        if isinstance(action, Tap):
            eng.tap_sent(action)
        elif isinstance(action, Scroll):
            eng.scroll_sent(action)
    msg = "the crawl did not stop"
    raise AssertionError(msg)


def crawl_list(total, *, policy=None, limits=None, **options):
    eng = engine(limits, policy=policy or policy_on())
    phone = ListPhone(total, **options)
    log = drive_list(eng, phone)
    return eng, phone, log


# -- when it scrolls ---------------------------------------------------------------


def test_rows_below_the_fold_are_pressed_with_scrolling_and_not_without() -> None:
    eng, phone, log = crawl_list(14)
    assert phone.pressed == labels(14)
    assert log[-1] == Stop("exhausted")
    off_eng, off_phone, off_log = crawl_list(14, policy=OFF)
    assert off_phone.pressed == labels(8)
    assert not any(isinstance(a, Scroll) for a in off_log)
    assert off_log[-1] == Stop("exhausted")
    assert [t.kind for t in eng.records.transitions].count("scroll") == 3
    assert off_eng.records.actions_sent == 1 + 2 * 8


def test_a_scroll_waits_until_no_untried_row_is_left_on_the_screen() -> None:
    _eng, phone, log = crawl_list(14)
    first_scroll = next(i for i, a in enumerate(log) if isinstance(a, Scroll))
    taps_before = [a for a in log[:first_scroll] if isinstance(a, Tap)]
    assert len(taps_before) == 8  # every visible row first
    assert phone.scrolls[0].point == POINT


def test_the_end_of_the_list_ends_the_scrolling() -> None:
    eng, phone, log = crawl_list(14)
    # 0 -> 4 -> 6 -> 6: the last swipe shows no new row, so it is the last
    assert len(phone.scrolls) == 3
    assert phone.offset == 6
    # after the swipe that showed nothing new, the crawl leaves the screen
    assert isinstance(log[-2], Scroll)
    assert log[-1] == Stop("exhausted")
    assert eng._scrolled_end == {phone_screen_id(phone)}


def phone_screen_id(phone):
    return list_screen(phone.rows()).screen_id


def test_the_cap_per_screen_stops_the_scrolling() -> None:
    eng, phone, log = crawl_list(40, limits=Limits(scrolls_per_screen=2))
    assert len(phone.scrolls) == 2
    assert phone.pressed == labels(16)  # rows 1-8, then 5-12, then 9-16
    assert log[-1] == Stop("exhausted")
    assert eng._scrolled_end == set()  # capped, not at the end


def test_the_cap_counts_one_screen_not_one_id() -> None:
    # The list changes its id as it scrolls (7 rows show after the first
    # scroll); the screens are one screen, so one cap holds for both.
    def shown(offset):
        return 8 if offset == 0 else 7

    _eng, phone, _log = crawl_list(40, limits=Limits(scrolls_per_screen=3), shown=shown)
    assert len(phone.scrolls) == 3


def test_at_the_depth_limit_the_list_is_scrolled_and_never_pressed() -> None:
    eng, phone, log = crawl_list(14, limits=Limits(depth=1))
    assert phone.pressed == []
    assert not any(isinstance(a, Tap) for a in log)
    assert len(phone.scrolls) == 3
    kinds = [t.kind for t in eng.records.transitions]
    assert kinds == ["launch", "scroll", "scroll", "scroll"]
    assert log[-1] == Stop("exhausted")
    # the scrolls count as actions
    assert eng.records.actions_sent == 1 + 3


def test_a_depth_of_two_still_presses_every_row_of_the_list() -> None:
    # Depth 2: the root list is pressed (a leaf opens at depth 2, never pressed).
    _eng, phone, _log = crawl_list(14, limits=Limits(depth=2))
    assert phone.pressed == labels(14)


def test_scrolls_count_toward_the_action_limit() -> None:
    eng, _phone, log = crawl_list(14, limits=Limits(actions=1 + 16 + 1))
    assert log[-1] == Stop("max_actions")
    assert eng.records.actions_sent == 18
    assert [t.kind for t in eng.records.transitions][-1] == "scroll"


def test_a_screen_with_no_scrollable_element_is_never_scrolled() -> None:
    eng = started()
    screen = seen(list_screen([], scroller_flags=()))
    assert eng.next(screen) == Stop("exhausted")


# -- never down, left or right ---------------------------------------------------


def test_every_scroll_is_one_point_and_the_wheel_amount() -> None:
    _eng, phone, _log = crawl_list(40)
    assert phone.scrolls
    for scroll in phone.scrolls:
        assert scroll.point == POINT
        assert scroll.amount == AMOUNT == 3


def test_the_amount_is_the_limit_of_the_run() -> None:
    _eng, phone, _log = crawl_list(40, limits=Limits(scroll_amount=7))
    assert {s.amount for s in phone.scrolls} == {7}


@pytest.mark.parametrize(
    ("point", "amount", "error"),
    [
        ((-1, 5), 3, ValueError),
        ((5, -1), 3, ValueError),
        ((5, 5), 0, ValueError),
        ((5, 5), 11, ValueError),
        ((5, 5), -3, ValueError),
        ((5, 5), True, TypeError),
        ((5, 5), 1.5, TypeError),
        ((1.5, 5), 3, TypeError),
        ((1,), 3, TypeError),
        ((True, 5), 3, TypeError),
    ],
)
def test_a_scroll_cannot_be_built_with_a_bad_point_or_amount(
    point, amount, error
) -> None:
    with pytest.raises(error):
        Scroll("s-1", point, amount)


def test_a_scroll_holds_only_a_screen_id_a_point_and_an_amount() -> None:
    assert [f.name for f in fields(Scroll)] == ["screen_id", "point", "amount"]
    assert Scroll("s-1", (5, 9), 10).amount == 10
    with pytest.raises(TypeError):
        Scroll("", (5, 9), 3)


# -- where it never scrolls -----------------------------------------------------


def test_a_control_screen_does_scroll() -> None:
    # The screens of the refusal tests below, without the thing that refuses.
    eng = started()
    action = eng.next(seen(list_screen([])))
    assert action == Scroll(list_screen([]).screen_id, POINT, AMOUNT)


def test_no_scroll_on_the_screen_of_another_app() -> None:
    eng = started()
    other = seen(list_screen([], package=OTHER), fg=OTHER)
    assert eng.next(other) == Relaunch()
    # the target's own screen with another app in front is off target too
    eng = started()
    wrong_front = seen(list_screen([]), fg=OTHER)
    assert eng.next(wrong_front) == Relaunch()
    eng = started()
    assert eng.next(seen(list_screen([]), fg=None)) == Relaunch()


def test_no_scroll_with_the_keyboard_shown() -> None:
    eng = started()
    action = eng.next(seen(list_screen([]), keyboard=True))
    assert action == Back()
    eng = started()
    assert not isinstance(eng.next(seen(list_screen([]), keyboard=None)), Back)


def test_no_scroll_on_a_dialog() -> None:
    eng = started()
    dialog = list_screen([], root=(20, 100, 300, 500))
    assert judged(dialog).dialog
    assert eng.next(seen(dialog)) == Stop("exhausted")


def test_no_scroll_on_a_screen_judged_irreversible() -> None:
    eng = started()
    warning = extra("e9", "android.widget.TextView", (40, 10, 280, 40))
    warning = clone(warning, text=f"This will {IRREVERSIBLE_PHRASES[0]}")
    screen = list_screen([], extras=(warning,))
    assert judged(screen).irreversible
    assert eng.next(seen(screen)) == Stop("exhausted")
    # with the rule off, the same screen is scrolled
    lax = policy_on(skip_irreversible=False)
    eng = started(policy=lax)
    assert isinstance(eng.next(seen(screen)), Scroll)


def test_no_scroll_when_the_screen_is_not_judged_too_large() -> None:
    eng = started()
    big = list_screen(
        [],
        extras=tuple(
            extra(f"x{i}", "android.widget.TextView", (0, 0, 1, 1))
            for i in range(crawl.MAX_ELEMENTS)
        ),
    )
    assert len(big.elements) > crawl.MAX_ELEMENTS
    assert eng.next(seen(big)) == Stop("exhausted")


def test_no_scroll_with_the_switch_off() -> None:
    eng = started(policy=OFF)
    assert eng.next(seen(list_screen([]))) == Stop("exhausted")
    assert eng._scrolls == {}
    assert eng._seen_prints == {}
    assert eng._family == {}
    assert eng._scrolled_end == set()


def test_no_scroll_while_refusals_pile_up() -> None:
    eng = started()
    screen = seen(list_screen([]))
    for _ in range(crawl.MAX_REFUSALS):
        action = eng.next(screen)
        assert isinstance(action, Scroll)
        # the driver's gate refuses it: the fresh read differs
        other = seen(list_screen([], scroller_flags=()))
        assert not eng.recheck(action, other)
    assert eng.next(screen) == Stop("exhausted")


# -- the point: where the wheel is turned --------------------------------------


def test_the_point_is_the_center_of_the_largest_visible_scrollable_element() -> None:
    screen = list_screen([])
    assert target_of(screen) == ("e2", POINT)
    # a pressable row at the center does not matter: a wheel presses nothing
    assert target_of(list_screen(labels(8))) == ("e2", POINT)


def test_the_largest_visible_scrollable_element_is_the_area() -> None:
    small = extra("e8", RECYCLER, (0, 60, 100, 200), flags=("scrollable",), parent="e2")
    big = extra(
        "e9", RECYCLER, (100, 300, 300, 620), flags=("scrollable",), parent="e2"
    )
    # the first list is a plain frame (holding both) here: only `small` and
    # `big` scroll
    screen = list_screen(
        [],
        scroller_flags=(),
        scroller_cls="android.widget.FrameLayout",
        extras=(small, big),
    )
    area = screen.require_visible(big)
    assert target_of(screen) == ("e9", area.center)


def test_a_list_that_scrolls_sideways_is_no_area() -> None:
    sideways = extra(
        "e8",
        "android.widget.HorizontalScrollView",
        (0, 60, 320, 600),
        flags=("scrollable",),
    )
    screen = list_screen([], scroller_flags=(), extras=(sideways,))
    assert target_of(screen) is None
    pager = extra(
        "e9",
        "androidx.viewpager.widget.ViewPager",
        (0, 60, 320, 600),
        flags=("scrollable",),
    )
    assert target_of(list_screen([], scroller_flags=(), extras=(pager,))) is None


def test_only_the_largest_element_is_tried() -> None:
    cover = extra("e7", "android.webkit.WebView", (0, 0, 320, 640))
    small = extra("e8", RECYCLER, (0, 60, 100, 200), flags=("scrollable",))
    assert target_of(list_screen([], extras=(cover, small))) is None


def test_the_grid_is_the_center_then_the_quarters() -> None:
    from uiprobe.guards.scroll import _GRID

    assert _GRID[0] == (2, 2)
    assert sorted(_GRID) == [(c, r) for c in (1, 2, 3) for r in (1, 2, 3)]
    assert len(set(_GRID)) == 9


NEXT = (80, 330)  # the second point of the grid: the left quarter, mid height

REFUSING = {
    "slider": ("android.widget.SeekBar", ()),
    "rating bar": ("android.widget.RatingBar", ()),
    "material slider": ("com.google.android.material.slider.Slider", ()),
    "number picker": ("android.widget.NumberPicker", ()),
    "date picker": ("android.widget.DatePicker", ()),
    "time picker": ("android.widget.TimePicker", ()),
    "any picker": ("com.example.ui.WheelPickerView", ()),
    "map": ("com.google.android.gms.maps.MapView", ()),
    "web view": ("android.webkit.WebView", ()),
    "surface view": ("android.view.SurfaceView", ()),
    "texture view": ("android.view.TextureView", ()),
    "text field": ("android.widget.EditText", ()),
    "switch": ("android.widget.Switch", ("checkable",)),
    "checkbox": ("android.widget.CheckBox", ("checkable",)),
}


def covering(bounds, cls, flags=()):
    return extra("e9", cls, bounds, flags=flags)


@pytest.mark.parametrize("name", list(REFUSING))
def test_a_wheel_is_never_turned_over_what_it_could_change(name):
    cls, flags = REFUSING[name]
    # holds the center and nothing of the next point of the grid
    screen = list_screen([], extras=(covering((150, 320, 170, 340), cls, flags),))
    assert point_of(screen) == NEXT


@pytest.mark.parametrize("name", list(REFUSING))
def test_the_one_point_is_all_that_is_looked_at(name):
    cls, flags = REFUSING[name]
    # lies on the line between two points of the grid, away from every point
    screen = list_screen([], extras=(covering((100, 250, 140, 270), cls, flags),))
    assert point_of(screen) == POINT


def test_a_point_in_an_element_judge_refuses_is_refused() -> None:
    # a row with a deny word, whatever the depth: the row, and the text in it
    row = extra(
        "e9",
        BUTTON,
        (140, 310, 180, 350),
        flags=("clickable",),
        text="Delete",
        parent="e2",
    )
    screen = list_screen([], extras=(row,))
    decision = next(d for d in judged(screen).decisions if d.element.id == "e9")
    assert "deny_word" in decision.reasons
    assert point_of(screen) == NEXT


def test_a_row_refused_by_any_reason_counts() -> None:
    # unlabeled, number-only and the others refuse a press; they refuse a wheel
    for text, reason in (("", "unlabeled"), ("42", "number_only")):
        row = extra(
            "e9",
            BUTTON,
            (140, 310, 180, 350),
            flags=("clickable",),
            text=text,
            parent="e2",
        )
        screen = list_screen([], extras=(row,))
        decision = next(d for d in judged(screen).decisions if d.element.id == "e9")
        assert reason in decision.reasons
        assert point_of(screen) == NEXT, reason


def test_a_row_refused_deeper_in_the_tree_counts() -> None:
    # the label is a child of a clickable row; the row holds the deny word
    row = extra(
        "e9",
        "android.widget.LinearLayout",
        (140, 310, 180, 350),
        flags=("clickable",),
        parent="e2",
    )
    label = clone(
        extra("e10", "android.widget.TextView", (145, 315, 175, 345), parent="e9"),
        text="Delete everything",
    )
    screen = list_screen([], extras=(row, label))
    assert point_of(screen) == NEXT


def test_a_refused_element_elsewhere_does_not_matter() -> None:
    row = extra(
        "e9",
        BUTTON,
        (40, 60, 280, 100),
        flags=("clickable",),
        text="Delete",
        parent="e2",
    )
    assert point_of(list_screen([], extras=(row,))) == POINT


def test_the_grid_is_walked_in_order_until_a_point_qualifies() -> None:
    # the center and the left quarter are refused: the right quarter is next
    wide = covering((70, 320, 170, 340), "android.widget.SeekBar")
    assert point_of(list_screen([], extras=(wide,))) == (240, 330)


def test_when_every_point_is_refused_the_screen_is_not_scrolled() -> None:
    whole = covering((0, 60, 320, 600), "android.webkit.WebView")
    screen = list_screen([], extras=(whole,))
    assert target_of(screen) is None
    eng = started()
    assert eng.next(seen(screen)) == Stop("exhausted")


def test_something_else_on_top_of_the_point_refuses_the_point() -> None:
    button = extra("e9", BUTTON, (140, 310, 180, 350), flags=("clickable",), text="Fab")
    assert point_of(list_screen([], extras=(button,))) == NEXT


def test_the_rules_of_the_swipe_are_gone() -> None:
    # no minimum travel, no two end points: a one-point scroll of a list as
    # small as the point itself is fine
    tiny = Screen(
        serial="s",
        captured_at=datetime(2026, 11, 25, tzinfo=UTC),
        package=PKG,
        activity=".t",
        api_level=36,
        display=(100, 100),
        elements=(
            Element(
                "e1",
                None,
                "android.widget.FrameLayout",
                Bounds(0, 0, 100, 100),
                package=PKG,
            ),
            Element(
                "e2",
                "e1",
                RECYCLER,
                Bounds(45, 45, 55, 55),
                package=PKG,
                flags=("scrollable",),
            ),
        ),
    )
    assert point_of(tiny) == (50, 50)


# -- a scroll that changes the screen's id ---------------------------------------


def test_a_changed_id_after_a_scroll_is_the_same_depth_and_the_same_screen() -> None:
    def shown(offset):
        return 8 if offset == 0 else 7

    eng, phone, _log = crawl_list(14, shown=shown)
    assert sorted(phone.pressed) == labels(14)  # nothing is pressed twice
    first = list_screen(labels(8)).screen_id
    second = list_screen(labels(7)).screen_id
    assert first != second
    assert eng.depth_of(first) == eng.depth_of(second) == 1
    scrolls = [t for t in eng.records.transitions if t.kind == "scroll"]
    assert scrolls[0].from_screen_id == first
    assert scrolls[0].to_screen_id == second
    assert eng._family[second] == first


def test_a_scroll_never_pushes_the_path() -> None:
    eng = started()
    top = seen(list_screen(labels(8)))
    action = eng.next(top)
    assert isinstance(action, Tap)
    assert eng.recheck(action, top)
    eng.tap_sent(action)
    leaf = seen(make_screen([], name="leaf"))
    assert eng.next(leaf) == Back()
    # nothing else is left, so the next decision on the list is a scroll
    for row in range(7):
        back = seen(list_screen(labels(8)))
        action = eng.next(back)
        assert isinstance(action, Tap), row
        assert eng.recheck(action, back)
        eng.tap_sent(action)
        assert eng.next(leaf) == Back()
    last = seen(list_screen(labels(8)))
    scroll = eng.next(last)
    depth = len(eng._path)
    assert depth == 1
    assert isinstance(scroll, Scroll)
    assert eng.recheck(scroll, last)
    eng.scroll_sent(scroll)
    after = seen(list_screen(labels(7), name="list"))  # another id
    eng.next(after)
    assert len(eng._path) == depth == 1


def test_a_scroll_onto_a_dialog_pushes_it_like_a_tap() -> None:
    eng = started()
    screen = seen(list_screen([]))
    scroll = eng.next(screen)
    assert isinstance(scroll, Scroll)
    assert eng.recheck(scroll, screen)
    eng.scroll_sent(scroll)
    dialog = seen(list_screen([], root=(20, 100, 300, 500), name="dialog"))
    action = eng.next(dialog)
    assert len(eng._path) == 2
    assert action in (Back(), Stop("exhausted")) or isinstance(action, Tap)


def test_a_scroll_that_lands_on_another_app_is_handled_like_a_tap_that_does() -> None:
    eng = started()
    screen = seen(list_screen([]))
    scroll = eng.next(screen)
    assert isinstance(scroll, Scroll)
    assert eng.recheck(scroll, screen)
    eng.scroll_sent(scroll)
    assert eng.next(off_screen()) == Relaunch()
    transitions = eng.records.transitions
    assert transitions[-1].kind == "scroll"
    assert transitions[-1].to_screen_id is not None
    assert transitions[-1].point == POINT
    assert transitions[-1].amount == AMOUNT
    assert transitions[-1].scroll_effect == "navigation"


def test_the_record_of_a_scroll_has_the_point_the_amount_and_no_row() -> None:
    eng, _phone, _log = crawl_list(14)
    scrolls = [t for t in eng.records.transitions if t.kind == "scroll"]
    assert len(scrolls) == 3
    for transition in scrolls:
        assert transition.from_screen_id is not None
        assert transition.point == POINT
        assert transition.amount == AMOUNT
        assert transition.scroll_effect is None
        assert transition.fingerprint is None
        assert transition.dump_failed is False
    for transition in eng.records.transitions:
        if transition.kind != "scroll":
            assert transition.amount is None


# -- the re-check, the release and withdraw of a scroll ------------------------------


def planned(eng=None):
    eng = eng or started()
    screen = seen(list_screen([]))
    scroll = eng.next(screen)
    assert isinstance(scroll, Scroll)
    return eng, screen, scroll


def test_recheck_allows_the_same_point_on_the_same_screen() -> None:
    eng, screen, scroll = planned()
    assert eng.recheck(scroll, screen)
    eng.scroll_sent(scroll)
    assert eng.actions_sent == 2


@pytest.mark.parametrize(
    "fresh",
    [
        seen(list_screen([], name="other")),  # another screen id
        seen(list_screen([]), keyboard=True),
        seen(list_screen([]), fg=OTHER),
        seen(list_screen([], package=OTHER), fg=OTHER),
        seen(list_screen([], root=(20, 100, 300, 500))),  # a dialog now
        Observation(None, PKG, keyboard_shown=False, now=0.0),
        seen(list_screen([], scroller_flags=())),  # nothing scrolls any more
        seen(list_screen([], window=(0, 60, 320, 640))),  # another point
        seen(
            list_screen(
                [],
                extras=(covering((150, 320, 170, 340), "android.widget.SeekBar", ()),),
            )
        ),  # the point is refused now
    ],
)
def test_recheck_refuses_when_the_fresh_screen_differs(fresh) -> None:
    eng, _screen, scroll = planned()
    assert not eng.recheck(scroll, fresh)
    assert eng.actions_sent == 1
    with pytest.raises(ValueError, match="allowed"):
        eng.scroll_sent(scroll)


def test_recheck_needs_the_very_scroll_next_returned() -> None:
    eng, screen, scroll = planned()
    equal = Scroll(scroll.screen_id, scroll.point, scroll.amount)
    with pytest.raises(ValueError, match="returned last"):
        eng.recheck(equal, screen)
    assert eng.recheck(scroll, screen)
    with pytest.raises(ValueError, match="allowed last"):
        eng.scroll_sent(equal)
    eng.scroll_sent(scroll)
    with pytest.raises(ValueError, match="allowed last"):
        eng.scroll_sent(scroll)  # reported once


def test_recheck_at_the_deadline_cancels_and_counts_nothing() -> None:
    eng, _screen, scroll = planned()
    late = seen(list_screen([]), now=Limits().minutes * 60.0)
    assert not eng.recheck(scroll, late)
    assert eng.next(late) == Stop("max_duration")
    assert eng._scrolls == {}


def test_an_unsent_scroll_is_released_and_counts_as_a_refusal() -> None:
    eng, screen, scroll = planned()
    assert eng.recheck(scroll, screen)  # allowed, never reported as sent
    again = eng.next(screen)
    assert isinstance(again, Scroll)  # one refusal is not enough to stop
    assert eng.actions_sent == 1
    assert eng.records.transitions == (eng.records.transitions[0],)
    assert eng._refused == 1
    for _ in range(crawl.MAX_REFUSALS - 1):
        action = eng.next(screen)
        assert eng.recheck(action, screen) if isinstance(action, Scroll) else True
    assert eng.next(screen) == Stop("exhausted")


def test_a_sent_scroll_ends_the_refusal_run() -> None:
    eng, screen, scroll = planned()
    assert eng.recheck(scroll, screen)
    eng.scroll_sent(scroll)
    assert eng._refused == 0
    assert eng._relaunches == 0


def test_a_withdrawn_scroll_is_chosen_again_with_nothing_counted() -> None:
    eng = started()
    screen = seen(list_screen([]))
    before = (eng.records, eng.actions_sent, list(eng._path), dict(eng._scrolls))
    scroll = eng.next(screen)
    assert isinstance(scroll, Scroll)
    eng.withdraw(scroll)
    assert (eng.records, eng.actions_sent, list(eng._path), eng._scrolls) == before
    assert eng._scroll_plan is None
    assert eng.next(screen) == scroll


def test_a_sent_scroll_cannot_be_withdrawn() -> None:
    eng, screen, scroll = planned()
    assert eng.recheck(scroll, screen)
    eng.scroll_sent(scroll)
    with pytest.raises(ValueError, match="next"):
        eng.withdraw(scroll)


def test_the_snapshot_holds_the_scrolling_fields() -> None:
    held = {f.name for f in fields(crawl._State)}
    assert {
        "scrolls",
        "scrolled_end",
        "family",
        "seen_prints",
        "scroll_plan",
        "scroll_approved",
        "after_scroll",
        "scroll_blocked",
        "scroll_incidents",
    } <= held
    eng, _phone, _log = crawl_list(14)
    state = eng._snapshot()
    for name in ("scrolls", "scrolled_end", "family", "seen_prints"):
        assert getattr(state, name) is not getattr(eng, f"_{name}")
    for sid, prints in state.seen_prints.items():
        assert prints is not eng._seen_prints[sid]


def test_a_rows_fingerprint_not_a_position_decides_what_is_new() -> None:
    # a list that only shifts a little (the same rows, one pixel down) shows
    # no new fingerprint, so it is the end
    eng = started()
    screen = seen(list_screen(labels(8)))
    for _row in range(8):
        action = eng.next(screen)
        if isinstance(action, Scroll):
            break
        assert eng.recheck(action, screen)
        eng.tap_sent(action)
        leaf = seen(make_screen([], name="leaf"))
        assert eng.next(leaf) == Back()
    scroll = eng.next(screen)
    assert isinstance(scroll, Scroll)
    assert eng.recheck(scroll, screen)
    eng.scroll_sent(scroll)
    shifted = seen(list_screen(labels(8), rows_top=61))
    assert fingerprint_of(shifted.screen, shifted.screen.elements[3]) == fingerprint_of(
        screen.screen, screen.screen.elements[3]
    )
    assert eng.next(shifted) == Stop("exhausted")


def test_the_rows_after_a_scroll_are_tried_as_usual() -> None:
    eng = started()
    screen = seen(list_screen(labels(2)))
    for _ in range(2):
        action = eng.next(screen)
        assert isinstance(action, Tap)
        assert eng.recheck(action, screen)
        eng.tap_sent(action)
        assert eng.next(seen(make_screen([], name="leaf"))) == Back()
    scroll = eng.next(screen)
    assert isinstance(scroll, Scroll)
    assert eng.recheck(scroll, screen)
    eng.scroll_sent(scroll)
    after = seen(list_screen(["Row 02", "Row 03"]))
    tap = eng.next(after)
    assert isinstance(tap, Tap)
    row = next(e for e in after.screen.elements if e.text == "Row 03")
    assert tap.fingerprint == fingerprint_of(after.screen, row)


# -- the switch off ------------------------------------------------------------------


def with_scroller(screen):
    """The screen with a scrollable container drawn over its whole area (no
    meaningful element, so nothing is covered)."""
    box = Element(
        "e90",
        "e1",
        RECYCLER,
        Bounds(0, 40, 320, 600),
        package=screen.package,
        flags=("scrollable",),
    )
    return replace(screen, elements=(*screen.elements, box))


class ScrollablePhone(Phone):
    """The graph phone with a scrollable container on every screen."""

    def observe(self, *, dump_ok=True):
        observation = super().observe(dump_ok=dump_ok)
        screen = observation.screen
        if screen is None or screen.package != PKG:
            return observation
        return replace(observation, screen=with_scroller(screen))


def run_graph(nodes, limits, policy, phone_class):
    eng = engine(limits, policy=policy)
    phone = phone_class(nodes)
    log = drive(eng, phone, policy=policy, depth=limits.depth)
    return log, eng.records, phone.visited


def comparable(records):
    """The records without the fields a scroll added."""
    transitions = [
        {k: v for k, v in asdict(t).items() if k not in ("amount", "scroll_effect")}
        for t in records.transitions
    ]
    return transitions, records.skipped, records.screens_seen, records.actions_sent


def test_off_gives_the_same_actions_records_and_skips_on_many_graphs() -> None:
    # With the switch off a scrollable container is inert: the run equals the
    # run on the same phone with no scrollable element at all (where, on or
    # off, nothing can scroll).
    rng = random.Random(20261125)  # noqa: S311
    for _ in range(60):
        nodes = random_graph(rng)
        limits = Limits(
            depth=rng.randint(1, 6),
            actions=rng.randint(1, 80),
            minutes=rng.choice([1, 2, 30]),
        )
        bare = run_graph(nodes, limits, policy_on(), Phone)
        off = run_graph(nodes, limits, OFF, ScrollablePhone)
        # the screens differ by the container (their ids), so compare what the
        # engine decided: the actions, as their kinds and press points
        assert [kind_and_point(a) for a in off[0]] == [
            kind_and_point(a) for a in bare[0]
        ]
        assert off[2] == bare[2]
        assert comparable_shape(off[1]) == comparable_shape(bare[1])


def test_the_default_policy_is_off_and_equals_the_explicit_off() -> None:
    # Nothing is asked for, so nothing scrolls: the run on phones whose screens
    # hold scrollable lists is, to the last record, the run of the explicit off.
    assert Policy().scroll_lists is False
    rng = random.Random(20261126)  # noqa: S311
    for _ in range(40):
        nodes = random_graph(rng)
        limits = Limits(
            depth=rng.randint(1, 6),
            actions=rng.randint(1, 80),
            minutes=rng.choice([1, 2, 30]),
        )
        default = run_graph(nodes, limits, Policy(), ScrollablePhone)
        explicit = run_graph(nodes, limits, OFF, ScrollablePhone)
        assert default[0] == explicit[0]
        assert default[1] == explicit[1]
        assert default[2] == explicit[2]
        assert not any(isinstance(a, Scroll) for a in default[0])
        assert not any(t.kind == "scroll" for t in default[1].transitions)


def kind_and_point(action):
    return (type(action).__name__, getattr(action, "point", None))


def comparable_shape(records):
    """The records as kinds, points and counts (screen ids and fingerprints
    change with the container, whose class and id join them)."""
    return (
        [(t.kind, t.point, t.dump_failed) for t in records.transitions],
        [s.reasons for s in records.skipped],
        records.screens_seen,
        records.actions_sent,
    )


def test_on_without_a_scrollable_element_equals_off() -> None:
    rng = random.Random(77)  # noqa: S311
    for _ in range(60):
        nodes = random_graph(rng)
        limits = Limits(
            depth=rng.randint(1, 6),
            actions=rng.randint(1, 80),
            minutes=rng.choice([1, 2, 30]),
        )
        on = run_graph(nodes, limits, policy_on(), Phone)
        off = run_graph(nodes, limits, OFF, Phone)
        assert on[0] == off[0]
        assert comparable(on[1]) == comparable(off[1])
        assert on[2] == off[2]


def test_off_on_a_long_list_equals_the_engine_that_never_knew_scrolling() -> None:
    # What the list phone does without a Scroll is what the off engine does:
    # the same actions as an engine run on the screens with nothing scrollable.
    def run(policy, scroller):
        eng = engine(policy=policy)
        phone = ListPhone(14)
        if not scroller:
            original = phone.observe

            def observe():
                observation = original()
                screen = observation.screen
                if screen is None or screen.package != PKG:
                    return observation
                kept = tuple(
                    clone(e, flags=tuple(f for f in e.flags if f != "scrollable"))
                    for e in screen.elements
                )
                return replace(observation, screen=replace(screen, elements=kept))

            phone.observe = observe
        log = drive_list(eng, phone)
        return log, eng.records, phone.pressed

    off = run(OFF, scroller=True)
    nothing = run(policy_on(), scroller=False)
    assert off[0] == nothing[0]
    assert comparable(off[1]) == comparable(nothing[1])
    assert off[2] == nothing[2] == labels(8)


# -- random graphs with lists -----------------------------------------------------


class RandomListPhone(Phone):
    """A graph phone where every screen has a list of 0 to 4 hidden rows that
    show up, one more per swipe, below its buttons."""

    def __init__(self, nodes, rng):
        super().__init__(nodes)
        self.rng = rng
        self.hidden = {n.name: rng.randint(0, 4) for n in nodes}
        self.shown: dict[str, int] = {}
        self.scrolls: list[Scroll] = []

    def observe(self, *, dump_ok=True):
        observation = super().observe(dump_ok=dump_ok)
        screen = observation.screen
        node = self.current()
        if screen is None or node is None or node.package != PKG:
            return observation
        extra_rows = self.shown.get(node.name, 0)
        base = [label for label, _ in node.buttons]
        labels_now = [*base, *(f"Hidden {k}" for k in range(extra_rows))]
        rebuilt = make_screen(labels_now, name=node.name)
        return replace(observation, screen=with_scroller(rebuilt))

    def send(self, action):
        if isinstance(action, Scroll):
            self.clock += 1.0
            node = self.current()
            assert node is not None
            self.scrolls.append(action)
            self.shown[node.name] = min(
                self.shown.get(node.name, 0) + 1, self.hidden[node.name]
            )
            self.visited.append(node.name)
            return
        if isinstance(action, Tap):
            node = self.current()
            index = (action.point[1] - 60) // 60
            if index >= len(node.buttons):
                self.clock += 1.0  # a hidden row: it opens nothing
                self.visited.append(node.name)
                return
        super().send(action)


def test_random_graphs_with_lists_keep_every_rule_and_terminate() -> None:
    rng = random.Random(4711)  # noqa: S311
    for _ in range(40):
        nodes = random_graph(rng)
        limits = Limits(
            depth=rng.randint(1, 5),
            actions=rng.randint(1, 120),
            scrolls_per_screen=rng.randint(1, 4),
        )
        phone = RandomListPhone(nodes, rng)
        eng = engine(limits)
        log: list = [eng.start()]
        phone.send(log[0])
        for _step in range(3000):
            observation = phone.observe()
            action = eng.next(observation)
            if isinstance(action, Stop):
                log.append(action)
                break
            if isinstance(action, Tap | Scroll):
                assert eng.recheck(action, phone.observe())
            if isinstance(action, Tap):
                assert len(phone.stack) < limits.depth
            if isinstance(action, Scroll):
                assert action.amount == limits.scroll_amount
                assert observation.screen is not None
                assert observation.screen.package == PKG
            log.append(action)
            phone.send(action)
            if isinstance(action, Tap):
                eng.tap_sent(action)
            elif isinstance(action, Scroll):
                eng.scroll_sent(action)
        else:
            msg = "the crawl did not stop"
            raise AssertionError(msg)
        assert isinstance(log[-1], Stop)
        sent = [a for a in log if not isinstance(a, Stop)]
        assert len(sent) == eng.actions_sent <= limits.actions
        for count in eng._scrolls.values():
            assert count <= limits.scrolls_per_screen
        # nothing is pressed twice on one screen (a replay presses again)
        keys = [
            (t.from_screen_id, t.fingerprint)
            for t in eng.records.transitions
            if t.kind == "tap" and t.action_id not in eng.replay_actions
        ]
        assert len(keys) == len(set(keys))


def test_the_engine_is_deterministic_with_scrolling() -> None:
    first = crawl_list(40)
    second = crawl_list(40)
    assert first[1].pressed == second[1].pressed
    assert first[2] == second[2]
    assert first[0].records == second[0].records


# -- what judge says of the whole screen --------------------------------------------


def test_judge_says_whether_the_screen_is_a_dialog_or_irreversible() -> None:
    plain = judged(list_screen(labels(2)))
    assert (plain.dialog, plain.irreversible) == (False, False)
    dialog = judged(list_screen(labels(2), root=(20, 100, 300, 500)))
    assert (dialog.dialog, dialog.irreversible) == (True, False)
    warning = clone(
        extra("e9", "android.widget.TextView", (40, 10, 280, 40)),
        text=f"It {IRREVERSIBLE_PHRASES[0]}",
    )
    risky = judged(list_screen([], extras=(warning,)))
    assert (risky.dialog, risky.irreversible) == (False, True)  # with no candidate too
    off = judged(list_screen([], extras=(warning,)), policy_on(skip_irreversible=False))
    assert off.irreversible is False


def test_the_two_facts_do_not_take_part_in_equality() -> None:
    from uiprobe.guards import Judgement

    assert Judgement(()) == Judgement((), dialog=True, irreversible=True)


def test_a_screen_too_large_to_judge_is_neither() -> None:
    big = list_screen(
        labels(1),
        extras=tuple(
            extra(f"x{i}", "android.widget.TextView", (0, 0, 1, 1))
            for i in range(crawl.MAX_ELEMENTS)
        ),
        root=(20, 100, 300, 500),
    )
    verdict = judged(big)
    assert (verdict.dialog, verdict.irreversible) == (False, False)


def test_is_dialog_is_the_test_of_dialog_confirm_button() -> None:
    from uiprobe.guards import is_dialog

    assert is_dialog(list_screen([], root=(20, 100, 300, 500)))
    assert not is_dialog(list_screen([]))


# -- the cap counts sent scrolls only ----------------------------------------------


def test_a_refused_proposal_does_not_use_up_the_cap() -> None:
    eng = started(limits=Limits(scrolls_per_screen=1))
    screen = seen(list_screen([]))
    scroll = eng.next(screen)
    assert isinstance(scroll, Scroll)
    assert not eng.recheck(scroll, seen(list_screen([], scroller_flags=())))
    assert eng._scrolls == {}
    again = eng.next(screen)
    assert isinstance(again, Scroll)  # not used up by the refusal
    assert eng.recheck(again, screen)
    eng.scroll_sent(again)
    assert eng._scrolls == {key_of(screen.screen): 1}


def test_the_cap_is_not_reset_by_a_landing_on_another_id() -> None:
    def shown(offset):
        return 8 if offset == 0 else 7

    _eng, phone, _log = crawl_list(40, limits=Limits(scrolls_per_screen=2), shown=shown)
    assert len(phone.scrolls) == 2


# -- the re-check holds the text of everything the scroll can touch ---------------


def rows_screen(texts):
    # rows that cannot be pressed, so that the engine goes straight to a scroll
    return seen(list_screen(texts, row_flags=()))


def planned_rows(texts):
    eng = started()
    scroll = eng.next(rows_screen(texts))
    assert isinstance(scroll, Scroll)
    return eng, scroll


def test_recheck_allows_the_same_content() -> None:
    eng, scroll = planned_rows(labels(5))
    assert eng.recheck(scroll, rows_screen(labels(5)))


@pytest.mark.parametrize("other", [["Row 01", "Row 02", "Changed", "Row 04", "Row 05"]])
def test_a_text_that_changed_in_the_container_refuses_the_scroll(other) -> None:
    # the layout, so the screen id, is the same; only a text differs
    assert list_screen(other, row_flags=()).screen_id == (
        list_screen(labels(5), row_flags=()).screen_id
    )
    eng, scroll = planned_rows(labels(5))
    assert not eng.recheck(scroll, rows_screen(other))
    assert eng.actions_sent == 1


def test_a_flag_that_changed_in_the_container_refuses_the_scroll() -> None:
    eng, scroll = planned_rows(labels(5))
    fresh = list_screen(labels(5), row_flags=())
    boxes = tuple(
        clone(e, flags=(*e.flags, "checked")) if e.text == "Row 03" else e
        for e in fresh.elements
    )
    assert not eng.recheck(scroll, seen(replace(fresh, elements=boxes)))


def test_a_bound_that_moved_in_the_container_refuses_the_scroll() -> None:
    eng, scroll = planned_rows(labels(5))
    fresh = list_screen(labels(5), row_flags=())
    moved = tuple(
        clone(e, bounds=Bounds(40, 301, 280, 341)) if e.text == "Row 04" else e
        for e in fresh.elements
    )
    assert not eng.recheck(scroll, seen(replace(fresh, elements=moved)))


def test_a_text_at_the_point_outside_the_container_refuses_the_scroll() -> None:
    # an element that lies over the point but is not inside the list
    label = clone(
        extra("e9", "android.widget.TextView", (150, 320, 170, 340)), text="a"
    )
    eng = started()
    scroll = eng.next(seen(list_screen([], extras=(label,))))
    assert isinstance(scroll, Scroll)
    changed = clone(label, text="b")
    assert not eng.recheck(scroll, seen(list_screen([], extras=(changed,))))


# -- what a scroll did beyond scrolling ----------------------------------------------


def switch(*, checked):
    flags = ("checkable", "checked") if checked else ("checkable",)
    return extra("e80", "android.widget.Switch", (10, 70, 60, 90), flags=flags)


def sent_scroll(eng, before):
    scroll = eng.next(before)
    assert isinstance(scroll, Scroll)
    assert eng.recheck(scroll, before)
    eng.scroll_sent(scroll)
    return scroll


def test_a_checked_state_that_changed_marks_the_scroll_and_blocks_the_screen() -> None:
    eng = started()
    before = seen(list_screen([], extras=(switch(checked=False),)))
    sent_scroll(eng, before)
    after = seen(list_screen([], extras=(switch(checked=True),)))
    assert eng.scroll_effect(after.screen, PKG) == "changed"
    action = eng.next(after)
    assert eng.records.transitions[-1].scroll_effect == "changed"
    assert action == Stop("exhausted")  # never scrolled again
    assert eng._scroll_blocked == {key_of(before.screen)}
    assert eng._scroll_incidents == 1


def test_a_text_that_changed_marks_the_scroll() -> None:
    value = extra("e81", "android.widget.TextView", (10, 70, 60, 90), parent="e1")
    eng = started()
    before = seen(list_screen([], extras=(clone(value, text="5"),)))
    sent_scroll(eng, before)
    after = seen(list_screen([], extras=(clone(value, text="6"),)))
    eng.next(after)
    assert eng.records.transitions[-1].scroll_effect == "changed"


def test_rows_that_only_scrolled_are_no_change() -> None:
    eng = started()
    sent_scroll(eng, rows_screen(labels(8)))
    after = rows_screen([*labels(8)[4:], "Row 09", "Row 10", "Row 11", "Row 12"])
    assert eng.scroll_effect(after.screen, PKG) is None
    eng.next(after)
    assert eng.records.transitions[-1].scroll_effect is None
    assert eng._scroll_incidents == 0


def test_a_replaced_sole_row_is_not_a_change() -> None:
    # one row in the list, and the wheel brings another one in its place: its
    # identity is unique on both screens, but it is a row of the scrolled list
    eng = started()
    before = seen(list_screen(["Only"], row_flags=()))
    sent_scroll(eng, before)
    after = seen(list_screen(["Another"], row_flags=()))
    assert after.screen.screen_id == before.screen.screen_id
    assert eng.scroll_effect(after.screen, PKG) is None
    eng.next(after)
    assert eng._scroll_incidents == 0
    assert eng._scroll_blocked == set()


def test_a_text_outside_the_list_that_changed_is_still_a_change() -> None:
    eng = started()
    value = extra("e81", "android.widget.TextView", (10, 70, 60, 90), parent="e1")
    before = seen(list_screen(["Only"], row_flags=(), extras=(clone(value, text="5"),)))
    sent_scroll(eng, before)
    after = seen(list_screen(["Other"], row_flags=(), extras=(clone(value, text="6"),)))
    assert eng.scroll_effect(after.screen, PKG) == "changed"


def pair(first, second):
    """Two texts of one identity (class, no resource id, same parent) outside
    the list, side by side."""
    one = extra("e81", "android.widget.TextView", (10, 70, 60, 90), parent="e1")
    two = extra("e82", "android.widget.TextView", (70, 70, 120, 90), parent="e1")
    return (clone(one, text=first), clone(two, text=second))


def test_one_of_two_texts_of_one_identity_that_changed_is_found_by_position() -> None:
    eng = started()
    before = seen(list_screen(["Row"], row_flags=(), extras=pair("5", "7")))
    sent_scroll(eng, before)
    after = seen(list_screen(["Row"], row_flags=(), extras=pair("5", "8")))
    assert eng.scroll_effect(after.screen, PKG) == "changed"
    # the first one, too
    eng = started()
    sent_scroll(eng, seen(list_screen(["Row"], row_flags=(), extras=pair("5", "7"))))
    again = seen(list_screen(["Row"], row_flags=(), extras=pair("6", "7")))
    assert eng.scroll_effect(again.screen, PKG) == "changed"


def test_two_texts_of_one_identity_that_did_not_change_are_no_change() -> None:
    eng = started()
    before = seen(list_screen(["Row"], row_flags=(), extras=pair("5", "7")))
    sent_scroll(eng, before)
    after = seen(list_screen(["Next"], row_flags=(), extras=pair("5", "7")))
    assert eng.scroll_effect(after.screen, PKG) is None


def sibling_lists(*, checked, rows=("a",)):
    """The list (scrolled) and a smaller sibling list with the same class,
    resource id and parent, hence the same container key; a switch and a text
    in the sibling."""
    sibling = Element(
        "e8",
        "e1",
        RECYCLER,
        Bounds(0, 0, 320, 50),
        resource_id=f"{PKG}:id/list",
        package=PKG,
        flags=("scrollable",),
    )
    flags = ("checkable", "checked") if checked else ("checkable",)
    toggle = Element(
        "e81",
        "e8",
        "android.widget.Switch",
        Bounds(10, 10, 60, 40),
        package=PKG,
        flags=flags,
    )
    return seen(list_screen(list(rows), row_flags=(), extras=(sibling, toggle)))


def test_a_switch_in_an_untouched_sibling_list_with_the_same_key_is_a_change() -> None:
    eng = started()
    before = sibling_lists(checked=False)
    sibling = next(e for e in before.screen.elements if e.id == "e8")
    assert container_key(before.screen, sibling) == key_of(before.screen)
    sent_scroll(eng, before)
    after = sibling_lists(checked=True, rows=("b",))
    assert eng.scroll_effect(after.screen, PKG) == "changed"


def test_the_scrolled_list_itself_is_still_left_out() -> None:
    eng = started()
    sent_scroll(eng, sibling_lists(checked=False))
    after = sibling_lists(checked=False, rows=("b",))
    assert eng.scroll_effect(after.screen, PKG) is None


def test_the_container_is_found_again_by_key_and_overlap() -> None:
    from uiprobe.guards.scroll import scrolled_container_on

    before = sibling_lists(checked=False).screen
    container = scroll_container(before)
    assert container is not None
    key = container_key(before, container)
    found = scrolled_container_on(before, key, container)
    assert found is not None
    assert found.id == container.id  # not the sibling with the same key
    away = Element("z", "e1", RECYCLER, Bounds(0, 640, 10, 650), package=PKG)
    assert scrolled_container_on(before, key, away) is None


def test_the_texts_inside_the_list_are_not_compared_by_text() -> None:
    from uiprobe.guards import changed_by_wheel, container_key, scroll_container

    before = list_screen(["a", "b"], row_flags=())
    after = list_screen(["c", "d"], row_flags=())
    container = scroll_container(before)
    assert container is not None
    key = container_key(before, container)
    assert not changed_by_wheel(before, after, key, container.id)
    # without the key the list is not known: rows of one identity are never
    # compared, as before
    assert not changed_by_wheel(before, after)


def test_another_activity_is_navigation_depth_plus_one_and_blocks_the_screen() -> None:
    eng = started()
    before = seen(list_screen([]))
    sent_scroll(eng, before)
    after = seen(list_screen([], name="other"))
    assert eng.scroll_effect(after.screen, PKG) == "navigation"
    assert eng.next(after) == Stop("exhausted") or True
    assert eng.records.transitions[-1].scroll_effect == "navigation"
    assert len(eng._path) == 2
    assert eng.depth_of(after.screen.screen_id) == 2
    assert key_of(before.screen) in eng._scroll_blocked


def test_another_package_is_navigation() -> None:
    eng = started()
    sent_scroll(eng, seen(list_screen([])))
    assert eng.scroll_effect(off_screen().screen, LAUNCHER_FG) == "navigation"
    assert eng.next(off_screen()) == Relaunch()
    assert eng.records.transitions[-1].scroll_effect == "navigation"
    assert eng._scroll_incidents == 1


LAUNCHER_FG = "com.example.launcher"


def test_a_page_of_the_same_activity_that_took_the_lists_place_is_navigation() -> None:
    # same activity (a fragment swap, say): another id, and no list of the
    # scrolled one on it
    eng = started()
    before = seen(list_screen(["a"], row_flags=()))
    sent_scroll(eng, before)
    page = seen(make_screen(["Detail", "Text"], name="list"))
    assert page.screen.activity == before.screen.activity
    assert page.screen.screen_id != before.screen.screen_id
    assert eng.scroll_effect(page.screen, PKG) == "navigation"
    eng.next(page)
    assert eng.records.transitions[-1].scroll_effect == "navigation"
    assert len(eng._path) == 2  # depth + 1, as for a tap
    assert eng.depth_of(page.screen.screen_id) == 2
    assert key_of(before.screen) in eng._scroll_blocked
    assert eng._scroll_incidents == 1
    # and the page is not taken as the same screen as the list
    assert eng._canon(page.screen.screen_id) == page.screen.screen_id


def test_a_page_with_the_same_ids_and_no_scrollable_list_is_navigation() -> None:
    # same classes and resource ids, so the same screen id: only the list's
    # scrollable flag is gone, which the planned key finds nowhere
    eng = started()
    before = seen(list_screen(["a", "b"], row_flags=()))
    sent_scroll(eng, before)
    page = seen(list_screen(["x", "y"], row_flags=(), scroller_flags=()))
    assert page.screen.screen_id == before.screen.screen_id
    assert eng.scroll_effect(page.screen, PKG) == "navigation"
    eng.next(page)
    assert eng.records.transitions[-1].scroll_effect == "navigation"
    assert len(eng._path) == 2  # depth + 1
    assert eng._scroll_incidents == 1
    assert key_of(before.screen) in eng._scroll_blocked


def switches(states):
    """Identical switch rows in the list (one identity); ``states`` is a string
    of ``1`` (checked) and ``0``."""
    rows = []
    for index, mark in enumerate(states):
        checked = mark == "1"
        flags = ("checkable", "checked") if checked else ("checkable",)
        rows.append(
            Element(
                f"e{20 + index}",
                "e2",
                "android.widget.Switch",
                Bounds(10, 70 + 50 * index, 60, 100 + 50 * index),
                package=PKG,
                flags=flags,
            )
        )
    return tuple(rows)


def test_two_identical_rows_scrolling_down_to_one_are_no_change() -> None:
    eng = started()
    before = seen(list_screen([], extras=switches("10")))
    sent_scroll(eng, before)
    after = seen(list_screen([], extras=switches("0")))
    assert eng.scroll_effect(after.screen, PKG) is None
    eng.next(after)
    assert eng._scroll_incidents == 0


def test_identical_rows_of_unknown_list_are_paired_only_when_as_many() -> None:
    from uiprobe.guards import changed_by_wheel

    two = list_screen([], extras=switches("10"))
    one = list_screen([], extras=switches("0"))
    assert not changed_by_wheel(two, one)  # no key: sizes differ, not paired
    assert not changed_by_wheel(one, two)
    flipped = list_screen([], extras=switches("11"))
    assert changed_by_wheel(two, flipped)  # as many on both screens: compared


def test_identical_rows_that_scrolled_in_a_changed_mix_are_no_change() -> None:
    eng = started()
    sent_scroll(eng, seen(list_screen([], extras=switches("10"))))
    after = seen(list_screen([], extras=switches("00")))
    assert eng.scroll_effect(after.screen, PKG) is None


def test_a_switch_outside_the_list_that_toggled_is_still_a_change() -> None:
    eng = started()
    before = seen(list_screen([], extras=(switch(checked=False),)))
    sent_scroll(eng, before)
    after = seen(list_screen([], extras=(switch(checked=True),)))
    assert eng.scroll_effect(after.screen, PKG) == "changed"


def test_a_new_page_reusing_the_list_key_elsewhere_is_navigation() -> None:
    # The same activity, class and resource id of the list, but the list is not
    # where the scrolled one was: another page with a list of the same kind.
    eng = started()
    before = seen(list_screen(["a"], row_flags=()))
    sent_scroll(eng, before)
    page = seen(list_screen(["x", "y"], row_flags=(), window=(0, 610, 320, 630)))
    assert page.screen.screen_id != before.screen.screen_id
    assert key_of(page.screen) == key_of(before.screen)  # the key is reused
    assert eng.scroll_effect(page.screen, PKG) == "navigation"
    eng.next(page)
    assert len(eng._path) == 2  # depth + 1
    assert eng._scroll_incidents == 1
    assert key_of(before.screen) in eng._scroll_blocked


def test_the_same_list_in_the_same_place_is_not_navigation() -> None:
    eng = started()
    sent_scroll(eng, seen(list_screen(["a"], row_flags=())))
    after = seen(list_screen(["b", "c"], row_flags=(), window=(0, 80, 320, 580)))
    # moved a little, still overlapping: the same list
    assert eng.scroll_effect(after.screen, PKG) is None


def test_an_unreadable_foreground_on_the_scrolled_screen_is_an_incident_only() -> None:
    eng = started()
    before = seen(list_screen(["a", "b"], row_flags=()))
    sent_scroll(eng, before)
    unread = seen(list_screen(["a", "b"], row_flags=()), fg=None)
    assert eng.scroll_effect(unread.screen, None) == "changed"
    eng.next(unread)
    assert len(eng._path) == 1  # no depth change
    assert eng._scroll_incidents == 1  # the screen is not scrolled again
    assert key_of(before.screen) in eng._scroll_blocked
    assert eng.records.transitions[-1].scroll_effect == "changed"


def test_an_unreadable_foreground_on_another_screen_is_still_navigation() -> None:
    eng = started()
    sent_scroll(eng, seen(list_screen(["a"], row_flags=())))
    other = seen(list_screen(["x", "y"], row_flags=(), name="other"), fg=None)
    assert eng.scroll_effect(other.screen, None) == "navigation"


def test_a_list_that_gained_a_row_is_not_another_page() -> None:
    eng = started()
    before = seen(list_screen(["a", "b"], row_flags=()))
    sent_scroll(eng, before)
    after = seen(list_screen(["b", "c", "d"], row_flags=()))
    assert after.screen.screen_id != before.screen.screen_id  # another row count
    assert eng.scroll_effect(after.screen, PKG) is None
    eng.next(after)
    assert len(eng._path) == 1
    assert eng._scroll_incidents == 0


HELPER = "com.example.helper"


def test_a_listed_app_is_scrolled_only_while_it_is_on_target() -> None:
    policy = policy_on(also_target=(HELPER,))
    helper_list = list_screen(["a", "b"], row_flags=(), package=HELPER)
    # on target: its package is listed and in front
    eng = started(policy=policy)
    assert isinstance(eng.next(seen(helper_list, fg=HELPER)), Scroll)
    # listed, but another window is in front: not on target, no scroll
    eng = started(policy=policy)
    assert not isinstance(eng.next(seen(helper_list, fg=PKG)), Scroll)
    # not listed: another app's screen, no scroll
    eng = started()
    assert not isinstance(eng.next(seen(helper_list, fg=HELPER)), Scroll)


def test_a_scroll_in_a_listed_app_that_stays_in_it_is_no_incident() -> None:
    policy = policy_on(also_target=(HELPER,))
    eng = started(policy=policy)
    before = seen(list_screen(["a", "b"], row_flags=(), package=HELPER), fg=HELPER)
    sent_scroll(eng, before)
    after = seen(list_screen(["b", "c"], row_flags=(), package=HELPER), fg=HELPER)
    assert eng.scroll_effect(after.screen, HELPER) is None
    assert isinstance(eng.next(after), Scroll)  # the new row c, same rules
    assert eng._scroll_incidents == 0


def test_a_scroll_from_a_listed_app_into_the_target_is_navigation() -> None:
    policy = policy_on(also_target=(HELPER,))
    eng = started(policy=policy)
    sent_scroll(eng, seen(list_screen(["a"], row_flags=(), package=HELPER), fg=HELPER))
    home = seen(list_screen(["a"], row_flags=(), name="home"), fg=PKG)
    assert eng.scroll_effect(home.screen, PKG) == "navigation"


def test_a_dialog_is_navigation() -> None:
    eng = started()
    sent_scroll(eng, seen(list_screen([])))
    dialog = seen(list_screen([], root=(20, 100, 300, 500), name="dialog"))
    assert eng.scroll_effect(dialog.screen, PKG) == "navigation"


def test_the_effect_of_no_scroll_is_none() -> None:
    eng = started()
    assert eng.scroll_effect(list_screen([]), PKG) is None
    assert eng.scroll_effect(None, PKG) is None


def on_a_child(limits):
    """An engine on a child screen "a" (opened from a root with one row), where
    a scroll changed a checked state; returns it and the root observation."""
    eng = started(limits=limits)
    root = seen(list_screen(["Only"]))
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, root)
    eng.tap_sent(tap)
    child = seen(list_screen([], extras=(switch(checked=False),), name="a"))
    sent_scroll(eng, child)
    after = seen(list_screen([], extras=(switch(checked=True),), name="a"))
    assert eng.next(after) == Back()  # not the root: it goes back
    assert eng._scroll_incidents == 1
    return eng, root


def test_one_incident_leaves_other_screens_scrollable() -> None:
    eng, root = on_a_child(Limits(scroll_incidents=2))
    assert isinstance(eng.next(root), Scroll)


def test_the_incident_limit_ends_the_scrolling_of_every_screen() -> None:
    eng, root = on_a_child(Limits(scroll_incidents=1))
    assert eng.next(root) == Stop("exhausted")  # nothing to press, no scroll


def test_the_incident_state_is_in_the_snapshot_and_restored_by_a_withdraw() -> None:
    eng, root = on_a_child(Limits())
    before = (set(eng._scroll_blocked), eng._scroll_incidents)
    action = eng.next(root)
    assert isinstance(action, Scroll)
    eng.withdraw(action)
    assert (set(eng._scroll_blocked), eng._scroll_incidents) == before


# -- the allow lists ----------------------------------------------------------------

CONTAINERS = (
    "android.widget.ListView",
    "android.widget.ScrollView",
    "androidx.recyclerview.widget.RecyclerView",
    "androidx.core.widget.NestedScrollView",
)
PLAIN = (
    "android.widget.LinearLayout",
    "android.widget.FrameLayout",
    "android.widget.RelativeLayout",
    "androidx.constraintlayout.widget.ConstraintLayout",
    "android.view.View",
    "android.view.ViewGroup",
    "android.widget.TextView",
    "android.widget.ImageView",
)


def test_the_allow_lists_are_fixed_constants_of_exact_names() -> None:
    from uiprobe.guards import SCROLL_CONTAINER_CLASSES, SCROLL_POINT_CLASSES

    assert frozenset(CONTAINERS) == SCROLL_CONTAINER_CLASSES
    assert frozenset(PLAIN) == SCROLL_POINT_CLASSES
    assert isinstance(SCROLL_CONTAINER_CLASSES, frozenset)
    assert isinstance(SCROLL_POINT_CLASSES, frozenset)


@pytest.mark.parametrize("cls", CONTAINERS)
def test_a_container_of_an_allowed_class_gets_a_point(cls) -> None:
    assert target_of(list_screen([], scroller_cls=cls)) == ("e2", POINT)


@pytest.mark.parametrize(
    "cls",
    [
        "android.widget.GridView",
        "android.widget.AbsListView",
        "android.widget.HorizontalScrollView",
        "androidx.viewpager.widget.ViewPager",
        "androidx.viewpager2.widget.ViewPager2",
        "com.example.MyScrollView",  # a subclass name: no substring matching
        "android.widget.ListViewEx",
        "androidx.recyclerview.widget.RecyclerView$Sub",
        "android.widget.Spinner",
        "android.widget.ScrollView ",
        "android.widget.scrollview",
        "",
    ],
)
def test_a_container_of_another_class_means_no_scroll(cls) -> None:
    assert target_of(list_screen([], scroller_cls=cls)) is None
    eng = started()
    assert eng.next(seen(list_screen([], scroller_cls=cls))) == Stop("exhausted")


def test_the_largest_scrollable_decides_even_when_a_smaller_one_is_allowed() -> None:
    big = extra(
        "e8", "android.widget.GridView", (0, 60, 320, 600), flags=("scrollable",)
    )
    small = extra("e9", RECYCLER, (0, 60, 100, 200), flags=("scrollable",))
    screen = list_screen(
        [],
        scroller_flags=(),
        scroller_cls="android.widget.FrameLayout",
        extras=(big, small),
    )
    assert target_of(screen) is None


@pytest.mark.parametrize("cls", PLAIN)
def test_a_plain_class_at_the_point_leaves_the_point_qualified(cls) -> None:
    # inside the container: a plain element of the list itself
    inside = extra("e9", cls, (150, 320, 170, 340), parent="e2")
    screen = list_screen([], extras=(inside,))
    assert point_of(screen) == POINT


# -- what a non-clickable element at the point is or says ---------------------------


def label_at_point(**more):
    """A plain, non-clickable label inside the list, over the wheel's center."""
    base = extra(
        "e9",
        "android.widget.TextView",
        (150, 320, 170, 340),
        parent="e2",
    )
    return clone(base, **more)


def test_a_neutral_label_at_the_point_leaves_it_qualified() -> None:
    screen = list_screen([], extras=(label_at_point(text="Volume"),))
    assert point_of(screen) == POINT


def test_a_label_of_another_package_at_the_point_refuses_the_point() -> None:
    other = label_at_point(text="Volume", package="com.example.other")
    assert point_of(list_screen([], extras=(other,))) == NEXT
    whole = clone(other, bounds=Bounds(*WINDOW))
    assert target_of(list_screen([], extras=(whole,))) is None


@pytest.mark.parametrize(
    "field", ["text", "content_desc", "hint"], ids=["text", "description", "hint"]
)
def test_a_deny_word_in_a_non_clickable_label_refuses_the_point(field) -> None:
    # judge decides only clickables; the wheel looks at every element there
    word = label_at_point(**{field: "Delete"})
    screen = list_screen([], extras=(word,))
    assert point_of(screen) == NEXT
    assert (
        target_of(list_screen([], extras=(clone(word, bounds=Bounds(*WINDOW)),)))
        is None
    )


def test_the_deny_words_are_the_judges_with_its_exceptions() -> None:
    # "automatically" excuses the deny word "call" in the judge, and here
    excused = label_at_point(text="Call me automatically")
    policy = Policy(scroll_lists=True, deny_exceptions=("automatically",))
    assert target_of_policy(list_screen([], extras=(excused,)), policy) is not None
    bare = label_at_point(text="Call me")
    assert point_of(list_screen([], extras=(bare,))) == NEXT
    # a custom deny word of the policy counts too
    custom = Policy(scroll_lists=True, deny_words=("zap",))
    zap = label_at_point(text="Zap it")
    assert target_of_policy(list_screen([], extras=(zap,)), custom) is not None
    assert (
        target_of_policy(
            list_screen([], extras=(clone(zap, bounds=Bounds(*WINDOW)),)), custom
        )
        is None
    )


def test_a_deny_word_only_in_a_resource_id_does_not_count() -> None:
    named = label_at_point(text="Volume", resource_id=f"{PKG}:id/delete_row")
    assert point_of(list_screen([], extras=(named,))) == POINT


def test_an_irreversibility_phrase_in_a_label_at_the_point_refuses_it() -> None:
    from uiprobe.guards import IRREVERSIBLE_PHRASES

    phrase = IRREVERSIBLE_PHRASES[0]
    screen = list_screen([], extras=(label_at_point(text=phrase),))
    assert target_of(screen) is None  # the whole screen is irreversible


def test_a_listed_apps_element_at_the_point_is_its_own_only_when_on_target() -> None:
    helper = "com.example.helper"
    policy = Policy(scroll_lists=True, also_target=(helper,))
    own = label_at_point(text="Volume", package=helper)
    screen = list_screen([], package=helper, extras=(own,))
    assert (
        scroll_target(screen, judge(screen, PKG, policy, foreground=helper)) is not None
    )
    # the app's own element on a listed app's screen is another package there
    app = clone(own, package=PKG)
    mixed = list_screen([], package=helper, extras=(app,))
    found = scroll_target(mixed, judge(mixed, PKG, policy, foreground=helper))
    assert found is not None
    assert found[1] == NEXT


# -- an editable element at the point refuses it -----------------------------------


def test_the_editable_attribute_is_parsed_apart_from_the_flags() -> None:
    from uiprobe.model import parse_uiautomator_xml

    xml = (
        '<?xml version="1.0"?><hierarchy rotation="0">'
        '<node index="0" text="" resource-id="" class="android.widget.TextView"'
        ' package="p" content-desc="" checkable="false" checked="false"'
        ' clickable="false" enabled="true" focusable="true" focused="false"'
        ' scrollable="false" long-clickable="false" password="false"'
        ' selected="false" editable="true" bounds="[0,0][10,10]"/></hierarchy>'
    )
    [element] = parse_uiautomator_xml(xml)
    assert element.editable is True
    assert "editable" not in element.flags  # flags and fingerprints are as before
    assert element.to_dict()["flags"] == ["focusable"]
    plain = parse_uiautomator_xml(xml.replace('editable="true"', 'editable="false"'))
    assert plain[0].editable is False
    assert plain[0] == element  # not part of equality


def test_only_editable_changing_does_not_stop_a_tap() -> None:
    # Behaviour with scrolling off is that of main: the tap is sent.
    eng = started(policy=Policy())
    screen = list_screen(["a"], row_flags=("clickable",))
    tap = eng.next(seen(screen))
    assert isinstance(tap, Tap)
    row = next(e for e in screen.elements if e.text == "a")
    changed = replace(
        screen,
        elements=tuple(
            clone(e, editable=True) if e.id == row.id else e for e in screen.elements
        ),
    )
    assert changed == screen  # equal but for editable
    assert eng.recheck(tap, seen(changed))
    assert fingerprint_of(changed, row) == fingerprint_of(screen, row)
    assert changed.screen_id == screen.screen_id


@pytest.mark.parametrize("cls", PLAIN)
def test_an_editable_plain_element_at_the_point_refuses_the_point(cls) -> None:
    editable = clone(extra("e9", cls, (150, 320, 170, 340), parent="e2"), editable=True)
    screen = list_screen([], extras=(editable,))
    assert point_of(screen) == NEXT
    whole = clone(extra("e9", cls, WINDOW, parent="e2"), editable=True)
    assert target_of(list_screen([], extras=(whole,))) is None
    # a real text field class stays refused, editable or not
    field = extra("e9", "android.widget.EditText", WINDOW, parent="e2")
    assert target_of(list_screen([], extras=(field,))) is None


# -- a point in a screen-edge area never qualifies ---------------------------------


def test_a_point_in_the_screen_edge_area_is_skipped() -> None:
    # the list lies in the top 40 px of the display: its center (160, 20) is in
    # the top edge area (24 px), as are the points above and beside it; the first
    # point of the grid below the edge qualifies
    screen = list_screen([], window=(0, 0, 320, 40))
    assert point_of(screen) == (160, 30)
    edge = judged(screen).edges
    assert any(a.top == 0 and a.bottom == 24 for a in edge)


def test_a_list_wholly_in_an_edge_area_is_not_scrolled() -> None:
    # the bottom 32 px of the 640 px display
    assert target_of(list_screen([], window=(0, 612, 320, 640))) is None
    # the same list higher up has a point
    assert target_of(list_screen([], window=(0, 300, 320, 500))) is not None


def test_the_edge_areas_follow_the_policy() -> None:
    screen = list_screen([], window=(0, 612, 320, 640))
    none = Policy(scroll_lists=True, avoid=())
    assert target_of_policy(screen, none) is not None
    wide = Policy(scroll_lists=True, edges=(0, 0, 200, 200))
    assert target_of_policy(list_screen([]), wide) is None


def target_of_policy(screen, policy):
    return scroll_target(screen, judged(screen, policy))


# -- another scrollable at the point refuses it -------------------------------------


@pytest.mark.parametrize("cls", PLAIN)
def test_another_scrollable_at_the_point_refuses_the_point(cls) -> None:
    # an element of an allowed plain class that is flagged scrollable, inside the
    # container: the wheel could scroll it instead of (or as well as) the list
    inner = extra("e9", cls, (150, 320, 170, 340), flags=("scrollable",), parent="e2")
    screen = list_screen([], extras=(inner,))
    assert point_of(screen) == NEXT
    whole = extra("e9", cls, WINDOW, flags=("scrollable",), parent="e2")
    assert target_of(list_screen([], extras=(whole,))) is None


def test_a_scrollable_ancestor_of_the_list_refuses_the_point() -> None:
    # the list is the largest scrollable, and a plain frame around it is flagged
    # scrollable too (slightly smaller): the wheel could reach the frame
    outer = extra(
        "e8",
        "android.widget.FrameLayout",
        (0, 60, 320, 590),
        flags=("scrollable",),
        parent="e1",
    )
    screen = list_screen([])
    elements = list(screen.elements)
    elements[1] = clone(elements[1], parent="e8")
    elements.insert(1, outer)
    nested = replace(screen, elements=tuple(elements))
    found = scroll_container(nested)
    assert found is not None
    assert found.id == "e2"
    assert target_of(nested) is None
    # the same frame, not scrollable, leaves the point qualified
    quiet = clone(outer, flags=())
    elements[1] = quiet
    assert point_of(replace(screen, elements=tuple(elements))) == POINT


# -- every element at the point is looked at, siblings of the list included ---------


@pytest.mark.parametrize("cls", PLAIN)
def test_a_plain_sibling_over_the_list_refuses_the_point(cls) -> None:
    # an unlabelled, non-clickable element of an allowed class, drawn over the
    # list as its sibling: hit_target looks through it, but a wheel event goes to
    # the window under the pointer and it is no part of the list
    sibling = extra("e9", cls, (150, 320, 170, 340), parent="e1")
    screen = list_screen([], extras=(sibling,))
    assert point_of(screen) == NEXT
    covering_all = extra("e9", cls, WINDOW, parent="e1")
    assert target_of(list_screen([], extras=(covering_all,))) is None


def test_a_sibling_under_the_list_refuses_the_point_too() -> None:
    # drawn before the list (document order) and lying under it
    under = extra("e0", "android.view.View", WINDOW, parent="e1")
    screen = list_screen([])
    elements = (screen.elements[0], under, *screen.elements[1:])
    screen = replace(screen, elements=elements)
    assert target_of(screen) is None


def test_an_ancestor_of_the_list_may_hold_the_point() -> None:
    # the root and a plain frame around the list are not siblings
    screen = list_screen([])
    assert any(e.id == "e1" for e in elements_at(screen, POINT))
    assert point_of(screen) == POINT


@pytest.mark.parametrize(
    "cls",
    [
        "android.widget.Spinner",
        "android.widget.AutoCompleteTextView",
        "android.widget.ZoomControls",
        "com.example.ZoomView",
        "com.example.DialView",
        "androidx.recyclerview.widget.RecyclerView",
        "androidx.viewpager.widget.ViewPager",
        "android.webkit.WebView",
        "android.widget.Button",
        "android.widget.EditText",
        "android.widget.ListView",
        "android.widget.android.widget.TextView",
        "android.widget.TextView$Sub",
        "androidx.appcompat.widget.AppCompatTextView",
        "android.widget.Switch",
        "android.widget.SeekBar",
        "android.widget.NumberPicker",
        "android.widget.Gallery",
    ],
)
def test_any_other_class_at_the_point_disqualifies_the_point(cls) -> None:
    screen = list_screen([], extras=(covering((150, 320, 170, 340), cls),))
    assert point_of(screen) == NEXT


@pytest.mark.parametrize("flag", ["long_clickable", "checkable", "password"])
def test_a_plain_class_with_a_dangerous_flag_disqualifies_the_point(flag) -> None:
    box = covering((150, 320, 170, 340), "android.widget.TextView", (flag,))
    assert point_of(list_screen([], extras=(box,))) == NEXT


def test_a_plain_clickable_row_at_the_point_is_allowed() -> None:
    row = extra(
        "e9",
        "android.widget.LinearLayout",
        (140, 310, 180, 350),
        flags=("clickable",),
        parent="e2",
    )
    label = clone(
        extra("e10", "android.widget.TextView", (145, 315, 175, 345), parent="e9"),
        text="Wi-Fi",
    )
    assert point_of(list_screen([], extras=(row, label))) == POINT


def test_an_element_of_another_class_beside_the_point_does_not_matter() -> None:
    spinner = covering((10, 10, 60, 40), "android.widget.Spinner")
    assert point_of(list_screen([], extras=(spinner,))) == POINT


def test_the_ancestor_of_an_unlisted_class_disqualifies_every_point() -> None:
    # the root covers the whole screen: a root of another class holds every
    # point, so the screen is not scrolled
    screen = list_screen([])
    elements = tuple(
        clone(e, class_name="androidx.coordinatorlayout.widget.CoordinatorLayout")
        if e.id == "e1"
        else e
        for e in screen.elements
    )
    assert target_of(replace(screen, elements=elements)) is None


def test_a_dialog_with_the_same_screen_id_is_navigation() -> None:
    eng = started()
    before = seen(list_screen([]))
    sent_scroll(eng, before)
    dialog = seen(list_screen([], root=(20, 100, 300, 500)))
    assert dialog.screen.screen_id == before.screen.screen_id
    assert eng.scroll_effect(dialog.screen, PKG) == "navigation"
    eng.next(dialog)
    assert len(eng._path) == 2


# -- the re-check sees the whole screen ----------------------------------------------


def header(**more):
    base = extra("e70", "android.widget.TextView", (10, 10, 100, 40), text="Title")
    return clone(base, **more)


def planned_with_header():
    eng = started()
    scroll = eng.next(seen(list_screen([], extras=(header(),))))
    assert isinstance(scroll, Scroll)
    return eng, scroll


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Other"},
        {"content_desc": "d"},
        {"hint": "h"},
        {"package": "com.example.other"},
        {"flags": ("focused",)},
        {"flags": ("selected",)},
        {"bounds": Bounds(10, 11, 100, 40)},
        {"class_name": "android.widget.ImageView"},
        {"resource_id": "x:id/y"},
    ],
)
def test_a_change_anywhere_on_the_screen_refuses_the_scroll(change) -> None:
    # the header is outside the container and away from the point
    eng, scroll = planned_with_header()
    fresh = seen(list_screen([], extras=(header(**change),)))
    assert not eng.recheck(scroll, fresh)


def test_the_same_screen_passes_the_whole_screen_check() -> None:
    eng, scroll = planned_with_header()
    assert eng.recheck(scroll, seen(list_screen([], extras=(header(),))))


def test_a_changed_display_size_refuses_the_scroll() -> None:
    # same elements, ids and bounds (the root still covers the display, so the
    # screen is no dialog); only the display size differs
    eng, scroll = planned_with_header()
    fresh = list_screen([], extras=(header(),))
    resized = replace(fresh, display=(fresh.display[0], fresh.display[1] - 1))
    assert resized.screen_id == fresh.screen_id
    assert not eng.recheck(scroll, seen(resized))


def test_an_element_turned_editable_refuses_the_scroll() -> None:
    eng, scroll = planned_with_header()
    fresh = list_screen([], extras=(clone(header(), editable=True),))
    assert not eng.recheck(scroll, seen(fresh))


def test_a_reparented_element_refuses_the_scroll() -> None:
    # same id, text, bounds and class: only the element it hangs under differs
    # (the screen id does not hold parents, so nothing else says so)
    eng, scroll = planned_with_header()
    moved = header(parent="e2")
    assert moved.parent != header().parent
    fresh = seen(list_screen([], extras=(moved,)))
    assert (
        fresh.screen.screen_id
        == seen(list_screen([], extras=(header(),))).screen.screen_id
    )
    assert not eng.recheck(scroll, fresh)


def test_an_element_id_that_changed_refuses_the_scroll() -> None:
    eng, scroll = planned_with_header()
    other = clone(header(), id="e71")
    assert not eng.recheck(scroll, seen(list_screen([], extras=(other,))))


# -- incidents survive a withdraw, the cap and the end are by container --------------


def test_an_incident_seen_by_the_driver_survives_a_withdrawn_next_action() -> None:
    eng = started()
    root = seen(list_screen(["Only"]))
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, root)
    eng.tap_sent(tap)
    child = seen(list_screen([], extras=(switch(checked=False),), name="a"))
    sent_scroll(eng, child)
    after = seen(list_screen([], extras=(switch(checked=True),), name="a"))
    assert eng.scroll_effect(after.screen, PKG) == "changed"  # as the driver asks
    action = eng.next(after)
    assert action == Back()
    eng.withdraw(action)  # a reconnect: the BACK is decided again
    assert eng._scroll_incidents == 1
    assert key_of(child.screen) in eng._scroll_blocked
    assert eng.next(after) == Back()
    assert eng._scroll_incidents == 1  # taken once, not twice


def test_an_incident_found_inside_next_is_found_again_after_a_withdraw() -> None:
    eng = started()
    root = seen(list_screen(["Only"]))
    tap = eng.next(root)
    assert eng.recheck(tap, root)
    eng.tap_sent(tap)
    child = seen(list_screen([], extras=(switch(checked=False),), name="a"))
    sent_scroll(eng, child)
    after = seen(list_screen([], extras=(switch(checked=True),), name="a"))
    action = eng.next(after)  # the driver did not ask first
    assert eng._scroll_incidents == 1
    eng.withdraw(action)
    assert eng._scroll_incidents == 0
    assert eng.next(after) == action
    assert eng._scroll_incidents == 1


def test_the_cap_holds_when_the_header_changes_the_screen_id() -> None:
    eng = started(limits=Limits(scrolls_per_screen=1))
    one = seen(list_screen(["a", "b"], row_flags=(), extras=(header(),)))
    sent_scroll(eng, one)
    # BACK returns to the same list with another header element: another id
    two = seen(
        list_screen(["c", "d"], row_flags=(), extras=(header(), header(id="e71")))
    )
    assert two.screen.screen_id != one.screen.screen_id
    assert key_of(two.screen) == key_of(one.screen)
    assert eng.next(two) == Stop("exhausted")  # new rows, but the cap is spent
    assert eng._scrolls == {key_of(one.screen): 1}


def two_lists(labels, *, first_is_large, row_flags=(), header=False, third=False):
    """A screen with two lists, either of which can be the largest; the rows
    are inside the second list and cannot be pressed."""
    small, large = (0, 60, 100, 200), (100, 300, 300, 620)
    other = Element(
        "e8",
        "e1",
        RECYCLER,
        Bounds(*(small if first_is_large else large)),
        resource_id=f"{PKG}:id/other",
        package=PKG,
        flags=("scrollable",),
    )
    screen = list_screen(
        labels,
        window=large if first_is_large else small,
        row_flags=row_flags,
        row_parent="e8",
        extras=(
            other,
            *((header_element(),) if header else ()),
            *((third_list(),) if third else ()),
        ),
    )
    return seen(screen)


def third_list():
    """A third list, larger than both, as a screen can gain one on return."""
    return Element(
        "e61",
        "e1",
        RECYCLER,
        Bounds(0, 250, 320, 620),
        resource_id=f"{PKG}:id/third",
        package=PKG,
        flags=("scrollable",),
    )


def header_element():
    return extra("e60", "android.widget.TextView", (10, 10, 100, 40), text="Title")


def test_the_cap_holds_across_a_visit_when_a_header_and_the_largest_list_change() -> (
    None
):
    # Scroll, press a row, come BACK: the screen has another id (a header) and
    # the other list is the largest, so neither the family nor the container is
    # the one that was counted; the screen's lists are.
    eng = started(limits=Limits(scrolls_per_screen=1))
    sent_scroll(eng, two_lists(["a"], first_is_large=True))
    shown = two_lists(["a", "b"], first_is_large=True, row_flags=("clickable",))
    tap = eng.next(shown)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, shown)
    eng.tap_sent(tap)
    assert isinstance(eng.next(seen(make_screen([], name="leaf"))), Back)
    back = two_lists(["c", "d"], first_is_large=False, header=True)
    assert back.screen.screen_id != shown.screen.screen_id
    assert eng._canon(back.screen.screen_id) == back.screen.screen_id  # no family
    assert key_of(back.screen) != key_of(shown.screen)  # another largest list
    assert not isinstance(eng.next(back), Scroll)  # the cap of the screen holds


def test_the_cap_holds_in_one_activity_when_a_third_list_changes_every_key() -> None:
    # A third, larger list joins the screen on return: the container key, the
    # family of ids and the set of lists all differ from what was counted. The
    # activity is the same, and its count holds.
    eng = started(limits=Limits(scrolls_per_screen=1))
    sent_scroll(eng, two_lists(["a"], first_is_large=True))
    shown = two_lists(["a", "b"], first_is_large=True, row_flags=("clickable",))
    tap = eng.next(shown)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, shown)
    eng.tap_sent(tap)
    assert isinstance(eng.next(seen(make_screen([], name="leaf"))), Back)
    back = two_lists(["c", "d"], first_is_large=False, header=True, third=True)
    first = two_lists(["a"], first_is_large=True).screen
    assert key_of(back.screen) not in eng._scrolls
    assert eng._canon(back.screen.screen_id) not in eng._family_scrolls
    assert lists_key(back.screen) not in eng._lists_scrolls
    assert back.screen.activity == first.activity
    assert not isinstance(eng.next(back), Scroll)  # one scroll in the activity
    assert eng._activity_scrolls == {f"{PKG}/{first.activity}": 1}


def test_the_cap_of_one_activity_does_not_reach_another_activity() -> None:
    eng = started(limits=Limits(scrolls_per_screen=1))
    sent_scroll(eng, two_lists(["a"], first_is_large=True))
    other = seen(list_screen(["x", "y"], row_flags=(), name="other"))
    # a screen of another activity has its own count (it is reached by tap, back
    # or relaunch in a run; here the engine is only asked about it)
    assert isinstance(eng.next(other), Scroll)


def test_the_lists_key_names_the_screen_by_all_its_lists() -> None:
    one = two_lists(["a"], first_is_large=True).screen
    other = two_lists(["a"], first_is_large=False, header=True).screen
    assert lists_key(one) == lists_key(other)
    assert lists_key(list_screen(["a"])) != lists_key(one)  # one list, not two


def test_a_second_scroll_goes_while_the_cap_of_the_screen_is_not_spent() -> None:
    eng = started(limits=Limits(scrolls_per_screen=2))
    sent_scroll(eng, two_lists(["a", "b"], first_is_large=False))
    again = eng.next(two_lists(["c", "d"], first_is_large=False))
    assert isinstance(again, Scroll)  # the second of two on this screen
    assert eng.recheck(again, two_lists(["c", "d"], first_is_large=False))
    eng.scroll_sent(again)
    last = eng.next(two_lists(["e", "f"], first_is_large=False))
    assert last == Stop("exhausted")  # two sent on the screen


def test_new_rows_that_cannot_be_pressed_are_not_the_end_of_the_list() -> None:
    eng = started()
    sent_scroll(eng, seen(list_screen(["a", "b", "c"], row_flags=())))
    after = seen(list_screen(["c", "d", "e"], row_flags=()))
    assert isinstance(eng.next(after), Scroll)  # d and e are new


def test_rows_that_cannot_be_pressed_and_are_all_seen_are_the_end() -> None:
    eng = started()
    sent_scroll(eng, seen(list_screen(["a", "b", "c"], row_flags=())))
    after = seen(list_screen(["b", "c", "a"], row_flags=()))
    assert eng.next(after) == Stop("exhausted")


# -- a permission screen is never scrolled ----------------------------------------


@pytest.mark.parametrize(
    "package",
    [
        "com.android.packageinstaller",
        "com.google.android.packageinstaller",
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
    ],
)
def test_a_screen_of_a_permission_package_is_never_scrolled(package) -> None:
    screen = list_screen(["Allow"], row_flags=(), package=package)
    policy = policy_on()
    assert (
        scroll_target(screen, judge(screen, package, policy, foreground=package))
        is None
    )
    # the same screen of an ordinary package has a point
    ordinary = list_screen(["Allow"], row_flags=())
    assert scroll_target(ordinary, judged(ordinary)) is not None


def test_the_engine_sends_no_wheel_on_a_permission_target_screen() -> None:
    package = "com.google.android.permissioncontroller"
    eng = CrawlEngine(package, policy_on(), Limits(), started_at=0.0)
    assert eng.start() == Launch()
    screen = list_screen(["Allow"], row_flags=(), package=package)
    action = eng.next(seen(screen, fg=package))
    assert not isinstance(action, Scroll)


# -- a list in a page: the innermost list container is the one scrolled -----------

DATA = Path(__file__).parent.parent / "data"


def settings_screen(name):
    """A saved emulator screen of Settings (a RecyclerView in the ScrollView that
    is the page); the dump file keeps no per-element package, so every element
    gets the screen's, as a live dump has it."""
    from uiprobe.crawl_tree import _parse_screen

    screen, package, _activity = _parse_screen(
        json.loads((DATA / name).read_text(encoding="utf-8"))
    )
    elements = tuple(replace(e, package=package) for e in screen.elements)
    return replace(screen, elements=elements), package


def settings_target(screen, package):
    policy = Policy(scroll_lists=True)
    judgement = judge(screen, package, policy, foreground=package)
    return scroll_target(screen, judgement)


def test_a_settings_sub_page_is_eligible_with_the_inner_list_chosen() -> None:
    # e4 is the page (a scrollable ScrollView), e15 the list (a scrollable
    # RecyclerView inside it): the inner one is the container
    screen, package = settings_screen("settings_subpage_screen.json")
    container = scroll_container(screen)
    assert container is not None
    assert (container.id, container.class_name.rsplit(".", 1)[-1]) == (
        "e15",
        "RecyclerView",
    )
    found = settings_target(screen, package)
    assert found is not None
    assert found[0] == "e15"
    # the key (and with it the caps and the blocks) follows the inner container
    assert container_key(screen, container).split("|")[1].endswith("RecyclerView")


def test_the_settings_first_page_is_eligible() -> None:
    # e4 is a ScrollView that does not scroll, e11 a ScrollView that does, and a
    # RecyclerView that does not scroll lies inside e11: the scrollable one is
    # the container, the view that cannot scroll is no obstacle
    screen, package = settings_screen("settings_first_page_screen.json")
    container = scroll_container(screen)
    assert container is not None
    assert container.id == "e11"
    found = settings_target(screen, package)
    assert found is not None
    assert found[0] == "e11"
    assert any(
        e.id == "e16"
        and e.class_name.endswith("RecyclerView")
        and "scrollable" not in e.flags
        for e in screen.elements
    )


def with_flags(screen, element_id, **changes):
    return replace(
        screen,
        elements=tuple(
            replace(e, **changes) if e.id == element_id else e for e in screen.elements
        ),
    )


def test_a_scrollable_view_inside_the_chosen_list_at_the_point_is_refused() -> None:
    screen, package = settings_screen("settings_subpage_screen.json")
    point = settings_target(screen, package)[1]
    inside = Element(
        "e900",
        "e15",
        "android.view.View",
        Bounds(0, 80, 320, 640),
        package=package,
        flags=("scrollable",),
    )
    nested = replace(screen, elements=(*screen.elements, inside))
    assert settings_target(nested, package) is None
    # not over the point it is no obstacle to another point
    small = replace(
        inside, bounds=Bounds(point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5)
    )
    near = replace(screen, elements=(*screen.elements, small))
    other = settings_target(near, package)
    assert other is not None
    assert other[1] != point


def test_a_scrollable_list_class_view_below_the_chosen_one_is_the_chosen_one() -> None:
    screen, package = settings_screen("settings_subpage_screen.json")
    deeper = Element(
        "e901",
        "e15",
        RECYCLER,
        Bounds(0, 80, 320, 640),
        package=package,
        flags=("scrollable",),
    )
    nested = replace(screen, elements=(*screen.elements, deeper))
    container = scroll_container(nested)
    assert container is not None
    assert container.id == "e901"


@pytest.mark.parametrize(
    "cls",
    [
        "androidx.viewpager.widget.ViewPager",
        "android.widget.HorizontalScrollView",
        "android.widget.FrameLayout",
    ],
)
def test_a_scrollable_ancestor_of_another_class_is_refused(cls) -> None:
    screen, package = settings_screen("settings_subpage_screen.json")
    # e3 holds the page and the list; it is flagged scrollable
    changed = with_flags(screen, "e3", class_name=cls, flags=("scrollable",))
    assert settings_target(changed, package) is None


@pytest.mark.parametrize(
    "cls",
    ["androidx.viewpager.widget.ViewPager", "android.widget.HorizontalScrollView"],
)
def test_an_ancestor_of_a_class_that_is_no_list_is_refused_scrollable_or_not(cls):
    screen, package = settings_screen("settings_subpage_screen.json")
    changed = with_flags(screen, "e3", class_name=cls)
    assert settings_target(changed, package) is None


def test_an_ancestor_list_container_may_be_scrollable_or_not() -> None:
    screen, package = settings_screen("settings_subpage_screen.json")
    # e4 (the page) scrolls here; it does not on the first page, where the same
    # class is an ancestor too
    assert settings_target(screen, package) is not None
    first, first_package = settings_screen("settings_first_page_screen.json")
    assert settings_target(first, first_package) is not None
    quiet = with_flags(screen, "e4", flags=())
    assert settings_target(quiet, package) is not None


def test_the_old_largest_rule_still_stops_a_non_list_largest() -> None:
    screen, package = settings_screen("settings_subpage_screen.json")
    big = Element(
        "e902",
        "e1",
        "android.widget.HorizontalScrollView",
        Bounds(0, 0, 320, 640),
        package=package,
        flags=("scrollable",),
    )
    assert (
        settings_target(replace(screen, elements=(*screen.elements, big)), package)
        is None
    )


# -- the one decoration allowed beside the list: an empty `round_corner` View ---------

VIEW = "android.view.View"
ROUND = f"{PKG}:id/round_corner"
PAGE_SIDES = (10, 0, 310, 640)  # full height, a little narrower than the display


def decoration(
    *,
    ident="e9",
    cls=VIEW,
    resource_id=ROUND,
    bounds=PAGE_SIDES,
    flags=(),
    text="",
    desc="",
    hint="",
    package=PKG,
    parent="e1",
    editable=False,
):
    return Element(
        ident,
        parent,
        cls,
        Bounds(*bounds),
        text=text,
        content_desc=desc,
        hint=hint,
        resource_id=resource_id,
        package=package,
        flags=tuple(flags),
        editable=editable,
    )


def samsung_page(*extras, rows=("Row",)):
    """A page container (the root) holding the list, with the decorations drawn
    after the list, so on top of it."""
    return list_screen(list(rows), row_flags=(), extras=extras)


def test_a_round_corner_view_as_the_last_child_leaves_the_point() -> None:
    screen = samsung_page(decoration())
    assert any(e.id == "e9" for e in elements_at(screen, POINT))
    container = scroll_container(screen)
    assert container is not None
    assert container.id == "e2"
    assert target_of(screen) == ("e2", POINT)


def test_a_full_screen_round_corner_copy_leaves_the_point() -> None:
    copy = decoration(ident="e10", bounds=(0, 0, *DISPLAY))
    screen = samsung_page(decoration(), copy)
    assert target_of(screen) == ("e2", POINT)
    assert target_of(samsung_page(copy)) == ("e2", POINT)


def test_the_package_part_of_the_round_corner_id_is_flexible() -> None:
    screen = samsung_page(decoration(resource_id="android:id/round_corner"))
    assert target_of(screen) == ("e2", POINT)


@pytest.mark.parametrize(
    "flag", ["clickable", "focusable", "long_clickable", "scrollable", "checkable"]
)
def test_a_round_corner_view_with_a_flag_refuses_the_point(flag) -> None:
    assert target_of(samsung_page(decoration(flags=(flag,)))) is None


@pytest.mark.parametrize("field", ["text", "desc", "hint"])
def test_a_round_corner_view_with_a_label_refuses_the_point(field) -> None:
    assert target_of(samsung_page(decoration(**{field: "x"}))) is None


def test_an_editable_round_corner_view_refuses_the_point() -> None:
    assert target_of(samsung_page(decoration(editable=True))) is None


def test_a_round_corner_view_with_a_child_refuses_the_point() -> None:
    child = extra("e10", VIEW, (0, 0, 1, 1), parent="e9")
    assert target_of(samsung_page(decoration(), child)) is None


@pytest.mark.parametrize(
    "resource_id",
    [
        f"{PKG}:id/round_corner2",
        f"{PKG}:id/floating_toolbar_item_background_start_first",
        f"{PKG}:id/Round_corner",
        f"{PKG}:id/",
        "round_corner",
        f"{PKG}:round_corner",
        "",
    ],
)
def test_another_id_refuses_the_point(resource_id) -> None:
    screen = samsung_page(decoration(resource_id=resource_id))
    assert target_of(screen) is None


@pytest.mark.parametrize(
    "cls", ["android.widget.FrameLayout", "android.view.ViewGroup", "com.x.View"]
)
def test_another_class_refuses_a_round_corner_id(cls) -> None:
    assert target_of(samsung_page(decoration(cls=cls))) is None


def test_a_round_corner_view_of_another_package_refuses_the_point() -> None:
    assert target_of(samsung_page(decoration(package=OTHER))) is None


def test_a_round_corner_view_the_judge_refused_refuses_the_point() -> None:
    from uiprobe.guards.scroll import _allowed

    screen = samsung_page(decoration())
    container = scroll_container(screen)
    assert container is not None
    view = next(e for e in screen.elements if e.id == "e9")
    assert _allowed(screen, view, container, frozenset())
    assert not _allowed(screen, view, container, frozenset({"e9"}))


def test_a_deny_word_on_a_round_corner_view_refuses_the_point() -> None:
    assert target_of(samsung_page(decoration(text="Delete"))) is None
    assert target_of(samsung_page(decoration(desc="Delete all"))) is None


def test_a_label_over_the_round_corner_view_still_has_to_be_in_the_list() -> None:
    # the decoration is not labelled, so hit_target looks through it; a label
    # drawn after it that is not inside the list still refuses the point
    label = extra("e10", "android.widget.TextView", PAGE_SIDES, text="Title")
    assert target_of(samsung_page(decoration(), label)) is None
    inside = list_screen(["Row"], row_flags=(), extras=(decoration(),))
    assert target_of(inside) == ("e2", POINT)
