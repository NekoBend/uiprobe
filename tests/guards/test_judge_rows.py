"""Tests for the number-only, toggle and row rules."""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import replace

import pytest

from tests.guards.helpers import (
    DISPLAY,
    PKG,
    _random_screen,
    decision,
    el,
    make_screen,
    reasons,
    run,
    t_mobile_row,
)
from uiprobe.guards import (
    REASONS,
    Policy,
    edge_areas,
    judge,
)

# -- number_only and toggle ---------------------------------------------------

SWITCH = ("clickable", "checkable")


@pytest.mark.parametrize(
    "label",
    [
        "200",
        "1",
        "00",
        "$5",
        "1,000",
        "12:30",
        "+1",
        "#3",
        "\u0663",
        "4.5",
        # Fractions and Roman numerals count as numerals on purpose.
        "\u00bd",
        "\u2163",
        "\u00bd2",
    ],
)
def test_number_only_labels_are_refused(label: str) -> None:
    found = decision(run([el("e2", text=label)]), "e2")
    assert found.reasons == ("number_only",)
    assert found.details[0].reason == "number_only"
    assert found.details[0].note == label
    assert found.details[0].word == label


@pytest.mark.parametrize("label", ["8:30 AM", "Page 2", "2 items", "x", "Go"])
def test_labels_with_other_words_are_not_number_only(label: str) -> None:
    assert reasons(run([el("e2", text=label)]), "e2") == ()


def test_empty_label_is_unlabeled_not_number_only() -> None:
    assert reasons(run([el("e2", text="")]), "e2") == ("unlabeled",)
    assert reasons(run([el("e2", text=" - ")]), "e2") == ()


def test_number_only_label_source_order() -> None:
    assert reasons(run([el("e2", desc="200")]), "e2") == ("number_only",)
    assert reasons(run([el("e2", hint="200")]), "e2") == ("number_only",)
    # The own text wins over a numeric description, and the reverse.
    assert reasons(run([el("e2", text="Coins", desc="200")]), "e2") == ()
    assert reasons(run([el("e2", text="200", desc="Coins")]), "e2") == ("number_only",)
    assert reasons(run([el("e2", text=" ", desc="Coins", hint="5")]), "e2") == ()


def test_number_only_from_a_descendant_label() -> None:
    row = el("e2", bounds=(50, 100, 250, 200))
    icon = el("e3", parent="e2", cls="android.widget.ImageView", flags=())
    amount = el("e4", parent="e2", text="200", cls="android.widget.TextView", flags=())
    later = el("e5", parent="e2", text="Coins", cls="android.widget.TextView", flags=())
    found = decision(run([row, icon, amount, later]), "e2")
    assert found.reasons == ("number_only",)
    assert found.details[0].note == "200"
    # The first non-empty label in document order decides.
    named = el("e4", parent="e2", text="Coins", cls="android.widget.TextView", flags=())
    after = el("e5", parent="e2", text="200", cls="android.widget.TextView", flags=())
    assert reasons(run([row, icon, named, after]), "e2") == ()
    # A description counts when the element has no text.
    desc = el("e4", parent="e2", desc="7", cls="android.widget.ImageView", flags=())
    assert reasons(run([row, icon, desc, later]), "e2") == ("number_only",)
    # A descendant's text comes before that element's own description.
    both = el("e3", parent="e2", text="Coins", desc="7", cls="android.widget.TextView")
    assert reasons(run([row, both]), "e2") == ()
    swapped = el(
        "e3", parent="e2", text="7", desc="Coins", cls="android.widget.TextView"
    )
    assert reasons(run([row, swapped]), "e2") == ("number_only",)
    # A nested descendant comes after its parent, and before the next sibling.
    nested = el("e3", parent="e2", cls="android.widget.LinearLayout", flags=())
    deep = el("e4", parent="e3", text="9", cls="android.widget.TextView", flags=())
    sibling = el("e5", parent="e2", text="Go", cls="android.widget.TextView", flags=())
    assert reasons(run([row, nested, deep, sibling]), "e2") == ("number_only",)
    named_parent = el("e3", parent="e2", text="Coins", cls="android.widget.TextView")
    assert reasons(run([row, named_parent, deep]), "e2") == ()


def test_number_only_ignores_invisible_characters() -> None:
    assert reasons(run([el("e2", text="2\u200b0\u00ad0\ufe0f")]), "e2") == (
        "number_only",
    )
    assert reasons(run([el("e2", text="\u200b\u2060")]), "e2") == ("unlabeled",)
    # An invisible-only text falls through to the next source.
    assert reasons(run([el("e2", text="\u200b", desc="12")]), "e2") == ("number_only",)
    assert reasons(run([el("e2", text="1\u200bx")]), "e2") == ()


def test_number_only_can_be_switched_off() -> None:
    off = Policy(skip_number_only=False)
    assert reasons(run([el("e2", text="200")], policy=off), "e2") == ()
    assert reasons(run([el("e2", text="200")]), "e2") == ("number_only",)


@pytest.mark.parametrize("flags", [SWITCH, ("clickable", "checkable", "checked")])
def test_checkable_candidate_is_a_toggle(flags) -> None:
    found = decision(run([el("e2", text="Wi-Fi", flags=flags)]), "e2")
    assert found.reasons == ("toggle",)
    assert found.details[0].element_id == "e2"


def test_row_holding_a_switch_is_a_toggle() -> None:
    row = el("e2", text="Sounds", bounds=(0, 100, 320, 200))
    switch = el(
        "e3",
        parent="e2",
        cls="android.widget.Switch",
        flags=("checkable", "checked"),
        bounds=(250, 120, 310, 180),
    )
    found = decision(run([row, switch]), "e2")
    assert found.reasons == ("toggle",)
    assert found.details[0].element_id == "e3"
    # A deeper switch counts too.
    box = el("e3", parent="e2", cls="android.widget.LinearLayout", flags=())
    deep = el("e4", parent="e3", cls="android.widget.CheckBox", flags=("checkable",))
    assert reasons(run([row, box, deep]), "e2") == ("toggle",)


def test_non_checkable_row_is_still_pressable() -> None:
    row = el("e2", text="Sounds", bounds=(0, 100, 320, 200))
    label = el("e3", parent="e2", text="On", cls="android.widget.TextView", flags=())
    assert reasons(run([row, label]), "e2") == ()
    # Only the checked flag, without checkable, does not count.
    odd = el("e2", text="Sounds", flags=("clickable", "checked"))
    assert reasons(run([odd]), "e2") == ()


def test_toggle_can_be_switched_off() -> None:
    off = Policy(skip_toggles=False)
    assert reasons(run([el("e2", text="Wi-Fi", flags=SWITCH)], policy=off), "e2") == ()
    assert reasons(run([el("e2", text="Wi-Fi", flags=SWITCH)]), "e2") == ("toggle",)


def test_both_rules_off_leave_the_old_outcome() -> None:
    off = Policy(skip_number_only=False, skip_toggles=False)
    both = el("e2", text="200", flags=SWITCH)
    assert reasons(run([both], policy=off), "e2") == ()
    assert reasons(run([both]), "e2") == ("number_only", "toggle")


def test_number_only_and_toggle_follow_unlabeled_and_precede_deny_word() -> None:
    assert REASONS.index("number_only") == REASONS.index("unlabeled") + 1
    assert REASONS.index("toggle") == REASONS.index("number_only") + 1
    assert REASONS[REASONS.index("toggle") + 1] == "two_target_row"
    both = el("e2", text="5", flags=SWITCH, bounds=(0, 0, 320, 40))
    other = el("e3", text="Delete", bounds=(50, 300, 250, 350))
    found = run([both, other], policy=Policy(avoid=edge_areas(DISPLAY)))
    assert reasons(found, "e2") == ("number_only", "toggle", "screen_edge")


def test_emitted_reasons_follow_unlabeled_and_precede_deny_word() -> None:
    # A checkable row that holds a deny word: toggle, then deny_word.
    row = el("e2", bounds=(50, 100, 250, 200), flags=SWITCH)
    word = el("e3", parent="e2", text="Delete", cls="android.widget.TextView", flags=())
    assert reasons(run([row, word]), "e2") == ("toggle", "deny_word")
    # An unlabeled checkable element: unlabeled, then toggle.
    blank = el("e2", flags=SWITCH)
    assert reasons(run([blank]), "e2") == ("unlabeled", "toggle")
    # A numeric label on a row that holds a deny word: number_only first.
    child = el(
        "e3", parent="e2", text="Delete", cls="android.widget.TextView", flags=()
    )
    both = el("e2", text="5", bounds=(50, 100, 250, 200))
    assert reasons(run([both, child]), "e2") == ("number_only", "deny_word")


def test_property_new_rules_only_add_refusals() -> None:
    rng = random.Random(20261119)  # noqa: S311
    labels = ["200", "1,000", "$5", "12:30", "\u0663", "Coins", "Page 2", "8:30 AM"]
    off = Policy(skip_number_only=False, skip_toggles=False, avoid=())
    on = Policy(avoid=())
    added = Counter()
    for _ in range(400):
        base, _denies = _random_screen(rng)
        elements = []
        for e in base.elements:
            flags = (*e.flags, "checkable") if rng.random() < 0.15 else e.flags
            text = rng.choice(labels) if rng.random() < 0.3 else e.text
            elements.append(replace(e, flags=flags, text=text))
        screen = make_screen(elements)
        after = judge(screen, PKG, on, foreground=PKG)
        before = judge(screen, PKG, off, foreground=PKG)
        for new, old in zip(after.decisions, before.decisions, strict=True):
            assert new.element.id == old.element.id
            assert set(old.reasons) <= set(new.reasons)
            extra = tuple(r for r in new.reasons if r not in old.reasons)
            assert set(extra) <= {"number_only", "toggle"}
            assert tuple(r for r in new.reasons if r in old.reasons) == old.reasons
            assert list(new.reasons) == sorted(new.reasons, key=REASONS.index)
            if old.reasons:
                assert new.reasons
            added.update(extra)
    assert added["number_only"] > 20
    assert added["toggle"] > 20


def test_with_deny_words_keeps_the_new_switches() -> None:
    policy = Policy(skip_number_only=False, skip_toggles=False)
    copy = policy.with_deny_words(["rent"])
    assert copy.skip_number_only is False
    assert copy.skip_toggles is False


def test_the_t_mobile_row_is_a_two_target_row() -> None:
    found = decision(run(t_mobile_row()), "e2")
    assert found.reasons == ("two_target_row",)
    assert [(d.reason, d.element_id) for d in found.details] == [
        ("two_target_row", "e6")
    ]


def test_the_gear_inside_a_two_target_row_is_judged_on_its_own() -> None:
    judgement = run(t_mobile_row())
    assert reasons(judgement, "e8") == ()
    with_deny = run(t_mobile_row(), policy=Policy(deny_words=("settings",)))
    assert reasons(with_deny, "e8") == ("deny_word",)


def test_a_row_without_the_divider_is_not_a_two_target_row() -> None:
    assert reasons(run(t_mobile_row(divider=False)), "e2") == ()


@pytest.mark.parametrize(
    "rid",
    [
        "com.example:id/not_two_target_divider",
        "two_target_divider",
        "com.android.settings:id/two_target_divider_line",
    ],
)
def test_a_near_miss_divider_id_is_not_a_two_target_row(rid) -> None:
    elements = t_mobile_row()
    elements[4] = replace(elements[4], resource_id=rid)
    assert reasons(run(elements), "e2") == ()


def test_a_two_target_row_does_not_need_a_clickable_gear() -> None:
    found = decision(run(t_mobile_row(gear=False)), "e2")
    assert found.reasons == ("two_target_row",)
    assert found.details[0].element_id == "e6"


def test_a_divider_outside_the_row_does_not_count() -> None:
    # Ancestors are not looked at: a row inside a container that holds a
    # divider somewhere else is pressable.
    box = el(
        "e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 400), flags=()
    )
    row = el("e3", parent="e2", text="Wi-Fi", bounds=(0, 100, 320, 200))
    divider = el(
        "e4",
        parent="e2",
        cls="android.widget.LinearLayout",
        rid="com.android.settings:id/two_target_divider",
        bounds=(0, 300, 320, 310),
        flags=(),
    )
    assert reasons(run([box, row, divider]), "e3") == ()


def test_the_row_rules_have_their_defaults() -> None:
    policy = Policy()
    assert policy.skip_two_target_rows is True
    assert policy.skip_rows_with_buttons is False


def test_the_two_target_rule_can_be_switched_off() -> None:
    off = Policy(skip_two_target_rows=False)
    assert reasons(run(t_mobile_row(), policy=off), "e2") == ()


def test_a_row_with_a_button_is_pressable_by_default() -> None:
    assert reasons(run(t_mobile_row(divider=False)), "e2") == ()


def test_with_the_wide_rule_a_row_with_a_clickable_descendant_is_refused() -> None:
    wide = Policy(skip_rows_with_buttons=True)
    found = decision(run(t_mobile_row(divider=False), policy=wide), "e2")
    assert found.reasons == ("row_with_button",)
    assert [(d.reason, d.element_id) for d in found.details] == [
        ("row_with_button", "e8")
    ]
    # The button itself has no clickable descendant: judged as before.
    assert reasons(run(t_mobile_row(divider=False), policy=wide), "e8") == ()


def test_when_both_row_rules_match_only_two_target_row_is_given() -> None:
    wide = Policy(skip_rows_with_buttons=True)
    found = decision(run(t_mobile_row(), policy=wide), "e2")
    assert found.reasons == ("two_target_row",)
    assert [d.reason for d in found.details] == ["two_target_row"]
    # With the narrow rule off, the wide rule gives its own reason.
    only_wide = Policy(skip_two_target_rows=False, skip_rows_with_buttons=True)
    assert reasons(run(t_mobile_row(), policy=only_wide), "e2") == ("row_with_button",)


def test_a_non_clickable_descendant_does_not_make_a_row_with_a_button() -> None:
    wide = Policy(skip_rows_with_buttons=True)
    found = run(t_mobile_row(divider=False, gear=False), policy=wide)
    assert reasons(found, "e2") == ()


def test_the_row_reasons_follow_toggle_and_precede_deny_word() -> None:
    assert REASONS.index("two_target_row") == REASONS.index("toggle") + 1
    assert REASONS.index("row_with_button") == REASONS.index("two_target_row") + 1
    assert REASONS[REASONS.index("row_with_button") + 1] == "navigate_up"
    # A two-target row that holds a switch and a deny word.
    elements = t_mobile_row()
    elements[3] = replace(elements[3], text="Delete", flags=("checkable",))
    assert reasons(run(elements), "e2") == ("toggle", "two_target_row", "deny_word")
