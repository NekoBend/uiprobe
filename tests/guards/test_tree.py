"""Tests for the element tree, covering and the hit target."""

from __future__ import annotations

import random
import unicodedata
from typing import TYPE_CHECKING

import pytest

from tests.guards.helpers import (
    BUTTON,
    CLICK,
    DISPLAY,
    EDIT,
    LAYOUT,
    PKG,
    TEXT_VIEW,
    _detail,
    _random_screen,
    decision,
    el,
    make_screen,
    reasons,
    root,
    run,
)
from uiprobe import guards
from uiprobe.errors import ElementNotVisibleError
from uiprobe.guards import (
    DEFAULT_DENY_WORDS,
    REASONS,
    Policy,
    judge,
    word_matches,
)

if TYPE_CHECKING:
    from uiprobe.model import Element, Screen


def _oracle_scope(screen: Screen, candidate: Element, point):
    by_id = {e.id: e for e in screen.elements}
    kids: dict[str, list[str]] = {}
    for e in screen.elements:
        if e.parent is not None:
            kids.setdefault(e.parent, []).append(e.id)

    def below(ident):
        out = {ident}
        for kid in kids.get(ident, []):
            out |= below(kid)
        return out

    def closure(element):
        out = {element.id}
        if "clickable" in element.flags:
            out |= below(element.id)
        up = element.parent
        while up is not None:
            if "clickable" in by_id[up].flags:
                out |= below(up)
            up = by_id[up].parent
        return out

    scope = closure(candidate)
    for e in screen.elements:
        visible = screen.visible_bounds(e)
        if (
            visible is not None
            and visible.left <= point[0] < visible.right
            and visible.top <= point[1] < visible.bottom
        ):
            scope |= closure(e)
    return scope


def _oracle_joined_roots(screen: Screen, candidate: Element, point):
    """Clickable elements whose joined subtree text is in the deny scope."""
    by_id = {e.id: e for e in screen.elements}

    def clickable_up(element):
        out = set()
        up = element.parent
        while up is not None:
            if "clickable" in by_id[up].flags:
                out.add(up)
            up = by_id[up].parent
        return out

    roots = set()
    holders = [candidate]
    for e in screen.elements:
        visible = screen.visible_bounds(e)
        if (
            visible is not None
            and visible.left <= point[0] < visible.right
            and visible.top <= point[1] < visible.bottom
        ):
            holders.append(e)
    for e in holders:
        if "clickable" in e.flags:
            roots.add(e.id)
        roots |= clickable_up(e)
    return roots


def _joined_deny(screen: Screen, root_id: str) -> bool:
    """True when a deny word is in the joined texts and descs of the subtree."""
    by_id = {e.id: e for e in screen.elements}

    def in_subtree(element):
        up = element.parent
        while up is not None:
            if up == root_id:
                return True
            up = by_id[up].parent
        return False

    parts = []
    for e in screen.elements:
        if e.id == root_id or in_subtree(e):
            parts.extend(v for v in (e.text, e.content_desc) if v)
    joined = " ".join(parts)
    return any(word_matches(joined, word) for word in DEFAULT_DENY_WORDS)


def _assert_hit_targets_are_harmless(screen: Screen, candidate: Element, point):
    """A pressed candidate has no protected element at its press point."""
    by_id = {e.id: e for e in screen.elements}

    def password_below(ident):
        return any(
            "password" in e.flags
            for e in screen.elements
            if e.id == ident or _is_below(screen, e, by_id[ident])
        )

    for e in screen.elements:
        visible = screen.visible_bounds(e)
        if not (
            visible is not None
            and visible.left <= point[0] < visible.right
            and visible.top <= point[1] < visible.bottom
        ):
            continue
        name = e.class_name.rsplit(".", 1)[-1].casefold()
        assert e.package == PKG
        assert "password" not in e.flags
        assert "edittext" not in name
        if "clickable" in e.flags:
            assert not password_below(e.id)
    assert not password_below(candidate.id)


def test_property_pressed_elements_never_touch_a_deny_word() -> None:
    rng = random.Random(20261117)  # noqa: S311
    pressed = skipped = 0
    for _ in range(1000):
        screen, denies = _random_screen(rng)
        result = judge(
            screen, PKG, Policy(tap_unlabeled=True, avoid=()), foreground=PKG
        )
        for found in result.decisions:
            visible = screen.visible_bounds(found.element)
            if found.point is None:
                assert visible is None
                assert found.reasons
                continue
            assert visible is not None
            assert visible.left <= found.point[0] < visible.right
            assert visible.top <= found.point[1] < visible.bottom
            _assert_cover_agrees_with_tap_point(screen, found)
            if found.reasons:
                skipped += 1
                continue
            pressed += 1
            scope = _oracle_scope(screen, found.element, found.point)
            assert not scope & denies
            for root_id in _oracle_joined_roots(screen, found.element, found.point):
                assert not _joined_deny(screen, root_id)
            _assert_hit_targets_are_harmless(screen, found.element, found.point)
    assert pressed > 50
    assert skipped > 100


def _assert_cover_agrees_with_tap_point(screen: Screen, found) -> None:
    """The guards and ``Screen.tap_point`` are two copies of one rule."""
    named = [d.element_id for d in found.details if d.reason == "covered"]
    try:
        point = screen.tap_point(found.element)
    except ElementNotVisibleError as error:
        details = error.details
    else:
        assert point == found.point
        assert not named
        assert "covered" not in found.reasons
        return
    assert details["reason"] == "covered"
    assert named == [details["covered_by"]]
    assert "covered" in found.reasons


def _real_text(value: str) -> bool:
    return any(
        not c.isspace() and unicodedata.category(c) not in {"Cc", "Cf", "Co"}
        for c in value
    )


def _is_below(screen: Screen, candidate: Element, ancestor: Element) -> bool:
    by_id = {e.id: e for e in screen.elements}
    up = candidate.parent
    while up is not None:
        if up == ancestor.id:
            return True
        up = by_id[up].parent
    return False


def test_property_only_labeled_elements_are_pressed_by_default() -> None:
    rng = random.Random(7)  # noqa: S311
    pressed = unlabeled = 0
    for _ in range(400):
        screen, _denies = _random_screen(rng)
        result = judge(screen, PKG, Policy(avoid=()), foreground=PKG)
        for found in result.decisions:
            element = found.element
            below = [e for e in screen.elements if _is_below(screen, e, element)]
            labeled = any(
                _real_text(v)
                for e in (element, *below)
                for v in (e.text, e.content_desc, e.hint)
            )
            assert labeled == ("unlabeled" not in found.reasons)
            unlabeled += not labeled
            if found.pressable:
                pressed += 1
                assert labeled
                assert element.package == PKG
                assert "disabled" not in element.flags
                assert "password" not in element.flags
    assert pressed > 20
    assert unlabeled > 50


# -- covered and the 8 x 8 pixel rule -------------------------------------------------


def test_covered_comes_right_after_not_visible() -> None:
    assert REASONS.index("covered") == REASONS.index("not_visible") + 1
    assert REASONS[REASONS.index("covered") + 1] == "disabled"


def test_a_sibling_on_top_of_the_press_point_covers_the_candidate() -> None:
    under = el("e2", text="Item", bounds=(0, 100, 320, 200))
    over = el("e3", text="Sale", flags=(), cls=TEXT_VIEW, bounds=(100, 120, 220, 180))
    found = run([under, over])
    assert reasons(found, "e2") == ("covered",)
    detail = _detail(found, "e2", "covered")
    assert (detail.element_id, detail.note) == ("e3", "covered by e3")


def test_a_descendant_on_top_does_not_cover_the_candidate() -> None:
    button = el("e2", text="Item", bounds=(0, 100, 320, 200))
    label = el(
        "e3",
        parent="e2",
        text="Item label",
        flags=(),
        cls=TEXT_VIEW,
        bounds=(100, 120, 220, 180),
    )
    assert reasons(run([button, label]), "e2") == ()


def test_an_empty_container_on_top_does_not_cover_the_candidate() -> None:
    button = el("e2", text="Item", bounds=(0, 100, 320, 200))
    layer = el("e3", flags=(), cls=LAYOUT, bounds=(0, 50, 320, 250))
    assert reasons(run([button, layer]), "e2") == ()


def test_a_grandchild_on_top_does_not_cover_the_candidate() -> None:
    button = el("e2", text="Item", bounds=(0, 100, 320, 200))
    box = el("e3", parent="e2", flags=(), cls=LAYOUT, bounds=(0, 100, 320, 200))
    label = el(
        "e4",
        parent="e3",
        text="Deep label",
        flags=(),
        cls=TEXT_VIEW,
        bounds=(100, 120, 220, 180),
    )
    assert reasons(run([button, box, label]), "e2") == ()


def test_a_labeled_container_that_only_partly_covers_a_cell_covers() -> None:
    # Smaller than a grid cell, around the press point (160, 150).
    button = el("e2", text="Item", bounds=(0, 100, 320, 200))
    small = el("e3", text="Dot", flags=(), cls=TEXT_VIEW, bounds=(150, 140, 170, 160))
    assert reasons(run([button, small]), "e2") == ("covered",)
    plain = el("e3", flags=(), cls=LAYOUT, bounds=(150, 140, 170, 160))
    assert reasons(run([button, plain]), "e2") == ()


@pytest.mark.parametrize(
    ("strip", "expected"),
    [
        (
            {"package": "com.other", "text": "Hi", "flags": ()},
            ("outside_target_app", "covered"),
        ),
        (
            {
                "package": min(guards.PERMISSION_PACKAGES),
                "text": "Allow",
                "flags": (),
            },
            ("outside_target_app", "permission_screen", "covered"),
        ),
        (
            {"cls": EDIT, "flags": ("password",), "hint": "Password"},
            ("covered", "text_input", "password_field"),
        ),
        (
            {"text": "Delete", "flags": CLICK, "cls": BUTTON},
            ("covered", "deny_word"),
        ),
    ],
)
def test_a_sliver_over_the_press_point_still_adds_its_hit_target_reasons(
    strip, expected
) -> None:
    # 7 pixels high: it is not a visible element, but a press at y=150 lands
    # on it, so it covers the button and its own fields refuse the button too.
    button = el("e2", text="Item", bounds=(0, 100, 320, 200))
    sliver = el("e3", bounds=(0, 147, 320, 154), **strip)
    found = run([button, sliver])
    assert reasons(found, "e2") == expected
    assert make_screen([root(), button, sliver]).visible_bounds(sliver) is None


def test_a_later_window_on_top_covers_the_candidate() -> None:
    # The pane is a second top-level element (a window drawn later).
    items = [
        el("e2", text="App", bounds=(40, 100, 120, 180)),
        el("e3", parent=None, text="Home", bounds=(0, 0, 320, 640), flags=CLICK),
    ]
    found = run(items)
    assert reasons(found, "e2") == ("covered",)
    assert _detail(found, "e2", "covered").element_id == "e3"
    assert reasons(found, "e3") == ()


def test_a_candidate_is_not_covered_by_an_element_that_ends_at_its_point() -> None:
    # The center of e2 is (160, 150); the right edge of e3 is exclusive.
    under = el("e2", text="Item", bounds=(0, 100, 320, 200))
    near = el("e3", text="Side", bounds=(0, 100, 160, 200))
    assert reasons(run([under, near]), "e2") == ()
    over = el("e3", text="Side", bounds=(0, 100, 161, 200))
    assert reasons(run([under, over]), "e2") == ("covered",)


@pytest.mark.parametrize(
    ("bounds", "shown"),
    [
        ((50, 100, 57, 150), False),  # 7 wide
        ((50, 100, 58, 150), True),
        ((50, 100, 250, 107), False),  # 7 high
        ((50, 100, 250, 108), True),
        ((50, 633, 250, 700), False),  # 7 pixels at the bottom display edge
        ((50, 632, 250, 700), True),
    ],
)
def test_a_sliver_is_not_visible(bounds, shown) -> None:
    found = run([el("e2", text="Item", bounds=bounds)])
    assert reasons(found, "e2").count("not_visible") == (0 if shown else 1)
    assert (decision(found, "e2").point is not None) is shown


def test_a_sliver_on_top_covers_what_is_under_it() -> None:
    # The cover check has no size limit for the element on top; the 8 pixel minimum is
    # for the element being pressed.
    under = el("e2", text="Item", bounds=(0, 100, 320, 200))
    sliver = el("e3", text="Thin", bounds=(100, 148, 220, 155))  # 7 high
    found = run([under, sliver])
    assert reasons(found, "e2") == ("covered",)
    assert _detail(found, "e2", "covered").element_id == "e3"
    assert reasons(found, "e3") == ("not_visible",)
    assert decision(found, "e3").point is None


def test_a_one_pixel_strip_on_the_center_row_covers_what_is_under_it() -> None:
    under = el("e2", text="Item", bounds=(0, 100, 320, 200))  # center (160, 150)
    strip = el("e3", text="Line", bounds=(100, 150, 220, 151))
    found = run([under, strip])
    assert reasons(found, "e2") == ("covered",)
    assert _detail(found, "e2", "covered").element_id == "e3"
    off_row = el("e3", text="Line", bounds=(100, 151, 220, 152))
    assert reasons(run([under, off_row]), "e2") == ()


def test_the_judge_and_hit_target_agree_on_what_is_on_top() -> None:
    layouts = [
        [
            el("e2", text="Item", bounds=(0, 100, 320, 200)),
            el("e3", text="Thin", bounds=(100, 148, 220, 155)),
        ],
        [
            el("e2", text="Item", bounds=(0, 100, 320, 200)),
            el("e3", text="Line", bounds=(100, 150, 220, 151)),
        ],
        [
            el("e2", text="Item", bounds=(0, 100, 320, 200)),
            el("e3", text="Edge", bounds=(0, 633, 320, 700)),
            el("e4", text="Gone", bounds=(-50, -50, 0, 30)),
        ],
        [
            el("e2", text="Item", bounds=(0, 100, 320, 200)),
            el("e3", bounds=(0, 100, 320, 200)),
            el("e4", text="Zero", bounds=(150, 100, 150, 200)),
            el("e5", text="Part", bounds=(160, 100, 161, 200)),
        ],
        [
            el("e2", text="Item", bounds=(0, 100, 320, 200)),
            el("e3", text="Side", bounds=(0, 100, 160, 200)),
            el("e4", parent="e2", text="Child", bounds=(100, 140, 220, 160)),
        ],
    ]
    for items in layouts:
        found = run(items)
        screen = make_screen([root(), *items])
        for dec in found.decisions:
            if dec.point is None:
                continue
            top = screen.hit_target(dec.point)
            covered = "covered" in dec.reasons
            if covered:
                assert top is not None
                assert _detail(found, dec.element.id, "covered").element_id == top.id
            else:
                assert top is None or dec.element.id in (top.id, top.parent)


def test_topmost_equals_the_straightforward_hit_target() -> None:
    # `_Judge.topmost` (grid, cached per cell, early exit) must give the index
    # of `Screen.hit_target` (a scan of the whole list) at every point.
    rng = random.Random(20261118)  # noqa: S311
    width, height = DISPLAY
    checked = 0
    for _ in range(60):
        screen, _denies = _random_screen(rng)
        state = guards._Judge(screen, PKG, Policy(), PKG)
        index = {e.id: i for i, e in enumerate(screen.elements)}
        points = [
            (rng.randint(-5, width + 5), rng.randint(-5, height + 5))
            for _ in range(150)
        ]
        # edges of the elements too: half-open bounds are where it goes wrong
        for e in screen.elements[:20]:
            b = e.bounds
            points += [(b.left, b.top), (b.right - 1, b.bottom - 1), (b.right, b.top)]
        for x, y in points:
            if not (0 <= x < width and 0 <= y < height):
                continue  # the judgement only asks about points on the display
            point = (x, y)
            hit = screen.hit_target(point)
            expected = -1 if hit is None else index[hit.id]
            assert state.topmost(point) == expected
            checked += 1
    assert checked > 5_000
