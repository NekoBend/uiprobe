"""Tests for `[deny] except_phrases` (`Policy.except_phrases`)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.guards.helpers import PKG, decision, el, make_screen, root, run
from uiprobe.crawl_tree import _rows_of
from uiprobe.guards import PERMISSION_PACKAGES, Policy, judge, scroll_target
from uiprobe.model import Bounds

QUICK = Policy(except_phrases=("Quick Share",))
ROW = "android.widget.LinearLayout"
TEXT = "android.widget.TextView"
ROW_BOUNDS = (0, 100, 320, 200)


def row(name="Quick Share", summary="", *, rid="", desc="", extra=()):
    """A clickable row e2 with its name e3 and, when given, a summary e4."""
    items = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS, rid=rid, desc=desc),
        el(
            "e3", parent="e2", cls=TEXT, flags=(), text=name, bounds=(10, 100, 310, 140)
        ),
    ]
    if summary:
        items.append(
            el(
                "e4",
                parent="e2",
                cls=TEXT,
                flags=(),
                text=summary,
                bounds=(10, 140, 310, 190),
            )
        )
    return [*items, *extra]


def verdict(elements, policy=QUICK, **kwargs):
    return decision(run(elements, policy=policy, **kwargs), "e2")


def test_an_exact_name_is_let_through_and_marked() -> None:
    d = verdict(row())
    assert d.reasons == ()
    assert d.pressable
    assert d.except_phrase == "Quick Share"


def test_without_the_phrase_the_row_is_refused() -> None:
    assert verdict(row(), Policy()).reasons == ("deny_word",)


@pytest.mark.parametrize(
    "name",
    [
        "quick share",
        "QUICK SHARE",
        "  Quick   Share ",
        "Quick\u00a0Share",
        "\uff31\uff35\uff29\uff23\uff2b\u3000\uff33\uff28\uff21\uff32\uff25",
    ],
)
def test_case_spacing_and_full_width_names_match(name) -> None:
    d = verdict(row(name))
    assert d.reasons == ()
    assert d.except_phrase == "Quick Share"


def test_the_mark_is_the_phrase_as_written() -> None:
    policy = Policy(except_phrases=("quick  SHARE",))
    assert verdict(row(), policy).except_phrase == "quick  SHARE"


@pytest.mark.parametrize(
    "name",
    [
        "Quick Share settings",
        "Share",
        "Share via email",
        "Quick Shares",
        "Quick-Share",
    ],
)
def test_other_names_are_not_let_through(name) -> None:
    d = verdict(row(name))
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_the_phrase_only_inside_the_summary_does_not_count() -> None:
    d = verdict(row("Nearby", "Quick Share"))
    assert d.reasons == ("deny_word",)
    assert d.except_phrase == ""


def test_a_name_from_the_description_counts_when_there_is_no_text() -> None:
    elements = [el("e2", cls=ROW, bounds=ROW_BOUNDS, desc="Quick Share")]
    d = verdict(elements)
    assert d.reasons == ()
    assert d.except_phrase == "Quick Share"


def test_a_visible_text_wins_over_the_description() -> None:
    d = verdict(row("Settings", desc="Quick Share"))
    assert d.reasons == ("deny_word",)


def test_the_name_is_the_first_text_of_the_element_then_its_descendants() -> None:
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el("e3", parent="e2", cls=TEXT, flags=(), bounds=(10, 100, 50, 140)),
        el(
            "e4",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 140, 310, 190),
        ),
        el(
            "e5",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Other",
            bounds=(10, 190, 310, 199),
        ),
    ]
    assert verdict(elements).except_phrase == "Quick Share"


def test_the_deny_words_inside_the_phrase_are_excused_everywhere_in_the_row() -> None:
    d = verdict(row("Quick Share", "Share with nearby devices", rid="a:id/quick_share"))
    assert d.reasons == ()
    assert d.except_phrase == "Quick Share"


def test_another_deny_word_in_the_row_still_refuses_it() -> None:
    d = verdict(row("Quick Share", "Delete all"))
    assert d.reasons == ("deny_word",)
    assert [x.word for x in d.details if x.reason == "deny_word"] == ["delete"]
    assert d.except_phrase == ""


def test_a_phrase_excuses_all_the_deny_words_it_holds() -> None:
    policy = Policy(except_phrases=("Pay Share",))
    d = verdict(row("Pay Share"), policy)
    assert d.reasons == ()
    assert d.except_phrase == "Pay Share"


def test_a_deny_word_in_a_clickable_ancestor_outside_the_row_still_refuses_it() -> None:
    elements = [
        el("e2", cls=ROW, bounds=(0, 100, 320, 300)),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Share all",
            bounds=(10, 100, 310, 140),
        ),
        el("e4", parent="e2", cls=ROW, bounds=(0, 150, 320, 250)),
        el(
            "e5",
            parent="e4",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 150, 310, 200),
        ),
    ]
    d = decision(run(elements, policy=QUICK), "e4")
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_an_overlay_with_the_deny_word_still_refuses_the_row() -> None:
    overlay = el("e9", parent="e1", cls=TEXT, flags=(), text="Share", bounds=ROW_BOUNDS)
    d = verdict(row(extra=[overlay]))
    assert "deny_word" in d.reasons


# -- the guards a phrase never touches -----------------------------------------------


def reasons_of(extra=(), *, policy=QUICK, name="Quick Share", **kwargs):
    d = verdict(row(name, extra=extra), policy, **kwargs)
    assert "deny_word" not in d.reasons
    return d.reasons


def test_a_switch_row_stays_a_toggle() -> None:
    switch = el(
        "e5",
        parent="e2",
        cls="android.widget.Switch",
        flags=("checkable",),
        bounds=(260, 110, 310, 140),
    )
    assert reasons_of([switch]) == ("toggle",)


def test_an_irreversible_screen_stays_refused() -> None:
    note = el(
        "e5",
        cls=TEXT,
        flags=(),
        text="This cannot be undone",
        bounds=(0, 300, 320, 340),
    )
    assert "irreversible_screen" in reasons_of([note])


def test_a_confirm_label_stays_refused_with_a_deny_word_on_the_screen() -> None:
    policy = Policy(except_phrases=("OK Share",))
    assert "confirm_button" in reasons_of(policy=policy, name="OK Share")


def test_the_dialog_check_stays() -> None:
    elements = row()
    elements[0] = el("e2", cls=ROW, bounds=ROW_BOUNDS, rid="android:id/button1")
    d = decision(run(elements, policy=QUICK, root_bounds=(20, 50, 300, 440)), "e2")
    assert "dialog_confirm_button" in d.reasons
    assert "deny_word" not in d.reasons


def test_text_and_password_fields_stay_refused() -> None:
    field = el(
        "e5",
        parent="e2",
        cls="android.widget.EditText",
        flags=(),
        bounds=(10, 150, 300, 190),
    )
    assert "text_input" in reasons_of([field])
    secret = el(
        "e5", parent="e2", cls=TEXT, flags=("password",), bounds=(10, 150, 300, 190)
    )
    assert "password_field" in reasons_of([secret])


def test_a_slider_stays_refused() -> None:
    bar = el(
        "e5",
        parent="e2",
        cls="android.widget.SeekBar",
        flags=(),
        bounds=(10, 150, 300, 190),
    )
    assert "slider" in reasons_of([bar])


def test_the_screen_edge_stays() -> None:
    policy = replace(QUICK, avoid=(Bounds(0, 90, 320, 210),))
    assert "screen_edge" in reasons_of(policy=policy)


def test_a_covered_row_stays_covered() -> None:
    cover = el("e9", parent="e1", cls=TEXT, flags=(), text="Hello", bounds=ROW_BOUNDS)
    assert "covered" in reasons_of([cover])


def test_a_permission_screen_stays_refused() -> None:
    other = el(
        "e5",
        parent="e1",
        cls=TEXT,
        flags=(),
        bounds=ROW_BOUNDS,
        package=next(iter(PERMISSION_PACKAGES)),
    )
    reasons = reasons_of([other])
    assert "permission_screen" in reasons
    assert "outside_target_app" in reasons


def test_outside_the_target_app_stays_refused() -> None:
    assert "outside_target_app" in reasons_of(foreground="com.other.app")


# -- the wheel -------------------------------------------------------------------------


def list_screen(policy):
    elements = [
        root(),
        el(
            "e2",
            cls="androidx.recyclerview.widget.RecyclerView",
            flags=("scrollable",),
            bounds=(0, 60, 320, 600),
        ),
        el("e3", parent="e2", cls=ROW, bounds=(0, 60, 320, 600)),
        el(
            "e4",
            parent="e3",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 60, 310, 600),
        ),
    ]
    screen = make_screen(elements)
    return screen, judge(screen, PKG, policy, foreground=PKG)


def test_the_wheel_never_turns_over_a_row_a_phrase_lets_through() -> None:
    screen, judgement = list_screen(Policy())
    assert scroll_target(screen, judgement) is None  # refused for its deny word
    screen, judgement = list_screen(QUICK)
    assert decision(judgement, "e3").reasons == ()
    assert "e3" in judgement.unsafe
    assert scroll_target(screen, judgement) is None


def test_a_row_that_needs_no_phrase_does_not_become_unsafe() -> None:
    elements = [
        root(),
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el("e3", parent="e2", cls=TEXT, flags=(), text="Settings", bounds=ROW_BOUNDS),
    ]
    judgement = judge(make_screen(elements), PKG, QUICK, foreground=PKG)
    assert judgement.unsafe == frozenset()


# -- the policy ------------------------------------------------------------------------


def test_the_default_has_no_phrases_and_decides_like_before() -> None:
    assert Policy().except_phrases == ()
    elements = row("Quick Share", "Delete all")
    plain = run(elements)
    empty = run(elements, policy=Policy(except_phrases=()))
    assert plain == empty
    assert plain.unsafe == empty.unsafe
    assert all(d.except_phrase == "" for d in plain.decisions)


@pytest.mark.parametrize(
    ("phrase", "needle"),
    [
        ("", "1 to 80"),
        ("x" * 81, "1 to 80"),
        ("Quick​Share", "invisible"),
        ("Quick\tShare", "control"),
        ("Quick\nShare", "control"),
        ("1234 - 5", "no letter"),
        ("  ", "no letter"),
    ],
)
def test_a_bad_phrase_is_refused(phrase, needle) -> None:
    with pytest.raises(ValueError, match=needle):
        Policy(except_phrases=(phrase,))


def test_eighty_characters_and_a_single_letter_are_fine() -> None:
    Policy(except_phrases=("a" * 80, "B"))


def test_phrases_equal_after_normalization_are_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        Policy(except_phrases=("Quick Share", "quick   SHARE"))


def test_phrases_must_be_strings() -> None:
    with pytest.raises(TypeError):
        Policy(except_phrases="Quick Share")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        Policy(except_phrases=(1,))  # ty: ignore[invalid-argument-type]


def test_an_added_deny_word_inside_a_phrase_drops_the_phrase() -> None:
    policy = Policy(except_phrases=("Quick Share", "Pay Later"))
    added = policy.with_deny_words(["quick share"])
    assert added.except_phrases == ("Pay Later",)
    assert policy.with_deny_words(["unrelated"]).except_phrases == policy.except_phrases


# -- the tree and the judge agree on the name ------------------------------------------


def test_the_tree_names_the_row_as_the_judge_does() -> None:
    # The name `uiprobe tree` shows for the row is the name the judge compares
    # with the phrases: a phrase equal to it lets the row through.
    cases = [
        row("Quick Share", "Summary"),
        row("", "Second"),
        row("  ", "", desc="By description"),
        row("Settings", "x", desc="Other"),
    ]
    words = ("share", "second", "description", "settings")
    for elements in cases:
        screen = make_screen([root(), *elements])
        (tree_row,) = (
            r for r in _rows_of(screen, set(), record_text=True) if r.kind == "row"
        )
        assert tree_row.name_from in ("text", "content_desc")
        policy = Policy(deny_words=words, except_phrases=(tree_row.name,))
        got = judge(screen, PKG, policy, foreground=PKG)
        assert decision(got, "e2").except_phrase == tree_row.name, tree_row.name
