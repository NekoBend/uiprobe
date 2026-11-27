"""Tests for `[deny] except_phrases` (`Policy.except_phrases`)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.guards.helpers import BUTTON, PKG, decision, el, make_screen, root, run
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


def test_a_description_never_names_a_row() -> None:
    elements = [el("e2", cls=ROW, bounds=ROW_BOUNDS, desc="Quick Share")]
    d = verdict(elements)
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


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


def test_only_the_name_text_is_excused_not_the_rest_of_the_row() -> None:
    # The summary and the id hold `share` too: only N's own text is taken out,
    # so the row stays refused (this is narrower than the earlier rule).
    assert verdict(row("Quick Share", "Share with nearby devices")).reasons == (
        "deny_word",
    )
    assert verdict(row("Quick Share", rid="a:id/quick_share")).reasons == ("deny_word",)


def test_another_deny_word_in_the_row_still_refuses_it() -> None:
    d = verdict(row("Quick Share", "Delete all"))
    assert d.reasons == ("deny_word",)
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
    assert d.except_phrase == ""  # another reason: the row stays as it was
    return d.reasons


def test_a_switch_row_stays_a_toggle() -> None:
    switch = el(
        "e5",
        parent="e2",
        cls="android.widget.Switch",
        flags=("checkable",),
        bounds=(260, 110, 310, 140),
    )
    assert reasons_of([switch]) == ("toggle", "deny_word")


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
    assert d.except_phrase == ""


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
    cover = el(
        "e9", parent="e1", cls=TEXT, flags=(), text="Hello", bounds=(150, 145, 170, 155)
    )
    d = verdict(row(extra=[cover]))
    assert "covered" in d.reasons
    assert d.except_phrase == ""  # the tap's recipient is not the row: no phrase


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
        row("Settings", "x", desc="Other"),
    ]
    words = ("share", "second", "description", "settings")
    for elements in cases:
        screen = make_screen([root(), *elements])
        (tree_row,) = (
            r for r in _rows_of(screen, set(), record_text=True) if r.kind == "row"
        )
        assert tree_row.name_from == "text"
        policy = Policy(deny_words=words, except_phrases=(tree_row.name,))
        got = judge(screen, PKG, policy, foreground=PKG)
        assert decision(got, "e2").except_phrase == tree_row.name, tree_row.name


# -- the name is a visible one; a hit is excused only when the row made it ------------


def test_an_off_screen_first_text_is_not_the_name() -> None:
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 700, 310, 740),
        ),
        el(
            "e4",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Share all",
            bounds=(10, 100, 310, 140),
        ),
    ]
    d = verdict(elements)
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_the_first_visible_text_is_the_name_even_after_an_off_screen_one() -> None:
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Junk",
            bounds=(10, 700, 310, 740),
        ),
        el(
            "e4",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 100, 310, 140),
        ),
    ]
    assert verdict(elements).except_phrase == "Quick Share"


def test_a_row_of_more_than_the_cap_members_gets_no_phrase() -> None:
    elements = [el("e2", cls=ROW, bounds=ROW_BOUNDS)]
    elements.append(
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Preferences",
            bounds=(10, 100, 310, 110),
        )
    )
    elements += [
        el(f"x{n}", parent="e2", cls=TEXT, flags=(), bounds=(10, 110, 310, 120))
        for n in range(498)
    ]
    elements.append(
        el(
            "e9",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 120, 310, 190),
        )
    )
    assert len(elements) == 501
    d = verdict(elements)
    assert d.except_phrase == ""
    assert "deny_word" in d.reasons
    # With one member fewer the same row is within the cap (and its name is
    # "Preferences", so it is refused for the deny word all the same).
    assert verdict(elements[:-1]).except_phrase == ""


def nested(parent_text, child_text, *, sibling="", policy):
    elements = [
        el("e2", cls=ROW, bounds=(0, 100, 320, 400), text=parent_text),
        el("e3", parent="e2", cls=ROW, bounds=(10, 200, 310, 260), text=child_text),
    ]
    if sibling:
        elements.append(
            el(
                "e4",
                parent="e2",
                cls=TEXT,
                flags=(),
                text=sibling,
                bounds=(10, 300, 310, 340),
            )
        )
    return decision(run(elements, policy=policy), "e3")


def test_a_hit_made_of_the_phrase_and_a_neighbour_is_taken_out_with_the_phrase() -> (
    None
):
    # Known and accepted: "Or" + "Der Recorder" read as `order` only with the
    # phrase's own characters; the check takes N's text out, so the hit goes and
    # the row is let through. (`order` is inside the phrase, so the user listed
    # it for that name.)
    words: dict[str, Any] = {
        "deny_words": ("order",),
        "deny_exceptions": ("recorder",),
    }
    assert "deny_word" in nested("Or", "Der Recorder", policy=Policy(**words)).reasons
    d = nested(
        "Or", "Der Recorder", policy=Policy(**words, except_phrases=("Der Recorder",))
    )
    assert d.except_phrase == "Der Recorder"


def test_removing_the_name_can_invent_a_hit_and_refuse() -> None:
    # "De" + [Quick Share] + "lete" read as `delete` once the name is taken
    # out: the row stays refused (an over-refusal, accepted).
    d = nested("De", "Quick Share", sibling="lete", policy=QUICK)
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_a_deny_word_of_the_parents_own_text_still_refuses() -> None:
    d = nested("Share", "Quick Share", policy=QUICK)
    assert "deny_word" in d.reasons


def share_rows(count):
    return [
        el(
            f"r{n}",
            cls=ROW,
            bounds=(0, 30 + n * 17, 320, 30 + n * 17 + 16),
            text="Share (.*)",
        )
        for n in range(count)
    ]


def test_up_to_eight_candidates_cost_eight_more_judgements(monkeypatch) -> None:
    from uiprobe.guards.judge import _Judge

    calls = []
    original = _Judge._summarize

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(_Judge, "_summarize", counting)
    policy = Policy(except_phrases=("Share (.*)",))
    got = run(share_rows(8), policy=policy)
    assert len(calls) == 1 + 8
    assert all(
        d.except_phrase == "Share (.*)" and d.reasons == () for d in got.decisions
    )
    calls.clear()
    run(share_rows(8), policy=Policy())
    assert calls == [1]


def test_more_than_eight_candidates_get_no_phrase(monkeypatch) -> None:
    from uiprobe.guards.judge import _Judge

    calls = []
    original = _Judge._summarize

    def counting(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(_Judge, "_summarize", counting)
    got = run(share_rows(9), policy=Policy(except_phrases=("Share (.*)",)))
    assert calls == [1]
    assert all(
        d.except_phrase == "" and d.reasons == ("deny_word",) for d in got.decisions
    )


# -- the narrow rule: no buttons inside, a visible uncovered text names the row -------


def test_a_row_with_a_clickable_descendant_gets_no_phrase() -> None:
    # A centred clickable child labelled by a confirm word keeps its own
    # refusal; the parent must not be approved for a tap that lands on it.
    child = el("e5", parent="e2", cls=BUTTON, bounds=(100, 120, 220, 180), text="確認")
    d = verdict(row("Quick Share", extra=[child]))
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_the_tap_recipient_must_be_the_row_or_a_plain_descendant() -> None:
    d = verdict(row())
    assert d.except_phrase == "Quick Share"  # the name text holds the point
    over = el(
        "e9", parent="e1", cls=TEXT, flags=(), text="x", bounds=(150, 145, 170, 155)
    )
    assert verdict(row(extra=[over])).except_phrase == ""


def test_a_small_title_still_names_the_row_but_a_description_does_not() -> None:
    policy = Policy(min_visible_px=32, except_phrases=("Quick Share",))
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS, desc="Quick Share"),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Share all",
            bounds=(10, 100, 310, 124),
        ),
    ]
    d = verdict(elements, policy)
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_a_covered_title_does_not_name_the_row() -> None:
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 100, 310, 140),
        ),
        el(
            "e4",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Share all",
            bounds=(10, 150, 310, 190),
        ),
        el(
            "e9",
            parent="e1",
            cls=TEXT,
            flags=(),
            text="Cover",
            bounds=(0, 100, 320, 145),
        ),
    ]
    d = verdict(elements)
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_a_text_with_no_area_does_not_name_the_row() -> None:
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 100, 10, 140),
        ),
        el(
            "e4",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Share all",
            bounds=(10, 150, 310, 190),
        ),
    ]
    assert "deny_word" in verdict(elements).reasons


def test_a_thin_but_drawn_title_names_the_row() -> None:
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 100, 310, 104),
        ),
    ]
    assert verdict(elements).except_phrase == "Quick Share"


def test_a_word_made_of_two_texts_of_the_row_is_not_excused() -> None:
    d = verdict(
        row(
            "Quick Share",
            extra=[
                el(
                    "e5",
                    parent="e2",
                    cls=TEXT,
                    flags=(),
                    text="Sha",
                    bounds=(10, 150, 100, 170),
                ),
                el(
                    "e6",
                    parent="e2",
                    cls=TEXT,
                    flags=(),
                    text="re",
                    bounds=(110, 150, 200, 170),
                ),
            ],
        )
    )
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_only_occurrences_that_survive_exceptions_count_in_a_parent() -> None:
    words: dict[str, Any] = {
        "deny_words": ("order",),
        "deny_exceptions": ("recorder",),
        "except_phrases": ("Order details",),
    }
    d = nested("Recorder.", "Order details", policy=Policy(**words))
    assert d.reasons == ()
    assert d.except_phrase == "Order details"
    d = nested("Order now", "Order details", policy=Policy(**words))
    assert "deny_word" in d.reasons


def test_a_parent_with_many_labels_costs_one_extra_match(monkeypatch) -> None:
    from uiprobe.guards.judge import _Judge

    calls = []
    original = _Judge._joined_word

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(_Judge, "_joined_word", counting)
    elements = [
        el("e2", cls=ROW, bounds=(0, 30, 320, 600), text="Parent"),
        el("e3", parent="e2", cls=ROW, bounds=(10, 40, 310, 60), text="Share (.*)"),
    ]
    elements += [
        el(
            f"l{n}",
            parent="e2",
            cls=TEXT,
            flags=(),
            text=f"Label {n}",
            bounds=(10, 70 + n * 5, 310, 74 + n * 5),
        )
        for n in range(100)
    ]
    run(elements, policy=Policy())
    base = len(calls)
    calls.clear()
    got = run(elements, policy=Policy(except_phrases=("Share (.*)",)))
    assert len(calls) <= base + 2
    assert decision(got, "e3").except_phrase == "Share (.*)"


# -- the three in-memory repros of the third check, and the shape rules ---------------


def test_a_sibling_covering_the_title_refuses_the_row() -> None:
    elements = [
        el("e2", cls=ROW, bounds=ROW_BOUNDS),
        el(
            "e3",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Quick Share",
            bounds=(10, 100, 310, 190),
        ),
        el(
            "e4",
            parent="e2",
            cls=TEXT,
            flags=(),
            text="Share all",
            bounds=(10, 100, 310, 190),
        ),
    ]
    d = verdict(elements)
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_a_text_and_a_description_of_one_element_that_join_to_a_word_refuse() -> None:
    split = el(
        "e5",
        parent="e2",
        cls=TEXT,
        flags=(),
        text="Sha",
        desc="re",
        bounds=(10, 150, 300, 190),
    )
    d = verdict(row(extra=[split]))
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


def test_a_scrollable_plain_view_at_the_tap_point_refuses_the_row() -> None:
    view = el(
        "e5",
        parent="e2",
        cls="android.widget.ScrollView",
        flags=("scrollable",),
        bounds=(10, 100, 310, 190),
    )
    d = verdict(row(extra=[view]))
    assert "deny_word" in d.reasons
    assert d.except_phrase == ""


@pytest.mark.parametrize(
    "flags",
    [
        ("clickable", "long_clickable"),
        ("clickable", "checkable"),
        ("clickable", "scrollable"),
        ("clickable", "password"),
    ],
)
def test_a_row_that_is_more_than_a_plain_button_gets_no_phrase(flags) -> None:
    elements = row()
    elements[0] = el("e2", cls=ROW, bounds=ROW_BOUNDS, flags=flags)
    d = verdict(elements)
    assert d.except_phrase == ""
    assert "deny_word" in d.reasons


@pytest.mark.parametrize(
    "flag", ["long_clickable", "checkable", "scrollable", "password"]
)
def test_a_descendant_that_is_more_than_a_label_gets_no_phrase(flag) -> None:
    inner = el("e5", parent="e2", cls=TEXT, flags=(flag,), bounds=(10, 150, 300, 190))
    d = verdict(row(extra=[inner]))
    assert d.except_phrase == ""


def test_an_editable_descendant_gets_no_phrase() -> None:
    from dataclasses import replace as dc_replace

    inner = dc_replace(
        el("e5", parent="e2", cls=TEXT, flags=(), bounds=(10, 150, 300, 190)),
        editable=True,
    )
    assert verdict(row(extra=[inner])).except_phrase == ""


def test_only_the_judges_own_matching_decides_with_exceptions_and_readings() -> None:
    # `é` denied, `Café` listed: separate children `e` and U+0301 are read by
    # the judge's own matching, whatever that says without the phrase.
    words: dict[str, Any] = {"deny_words": ("é",), "except_phrases": ("Café",)}
    extra = [
        el("e5", parent="e2", cls=TEXT, flags=(), text="e", bounds=(10, 150, 100, 190)),
        el("e6", parent="e2", cls=TEXT, flags=(), text="́", bounds=(110, 150, 200, 190)),
    ]
    with_phrase = verdict(row("Café", extra=extra), Policy(**words))
    plain = verdict(
        [
            *row("·", extra=extra),
        ],
        Policy(deny_words=("é",)),
    )
    # The phrase row is let through exactly when the row without its name is clean.
    assert (with_phrase.except_phrase != "") == (plain.reasons == ())
