"""Tests for confirm buttons, dialogs and confirm labels."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.guards.helpers import (
    DIALOG,
    DIALOG_ROOT,
    LAYOUT,
    PKG,
    _detail,
    _restart_screen,
    decision,
    el,
    make_screen,
    reasons,
    run,
)
from uiprobe.guards import (
    Policy,
    judge,
    word_matches,
)
from uiprobe.model import Bounds, Element

# -- confirm buttons and dialogs -------------------------------------------------------


def test_ok_outside_a_dialog_depends_on_deny_words_on_the_screen() -> None:
    ok = el("e2", text="OK", bounds=(50, 300, 250, 340))
    assert reasons(run([ok]), "e2") == ()
    warning = el(
        "e3",
        text="Delete this item?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(50, 200, 250, 240),
    )
    judgement = run([ok, warning])
    assert reasons(judgement, "e2") == ("confirm_button",)
    detail = decision(judgement, "e2").details[0]
    assert (detail.word, detail.element_id) == ("delete", "e3")


def test_ok_in_a_dialog_is_never_pressed() -> None:
    dialog = (20, 200, 300, 400)
    ok = el("e2", text="OK", bounds=(150, 340, 280, 390))
    cancel = el("e3", text="Cancel", bounds=(30, 340, 140, 390))
    judgement = run([ok, cancel], root_bounds=dialog)
    assert reasons(judgement, "e2") == ("dialog_confirm_button",)
    assert reasons(judgement, "e3") == ()


def test_dialog_with_a_deny_word() -> None:
    dialog = (20, 200, 300, 400)
    question = el(
        "e4",
        text="Delete all?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(30, 210, 290, 250),
    )
    ok = el("e2", text="OK", bounds=(150, 340, 280, 390))
    cancel = el("e3", text="Cancel", bounds=(30, 340, 140, 390))
    judgement = run([question, ok, cancel], root_bounds=dialog)
    assert reasons(judgement, "e2") == ("confirm_button", "dialog_confirm_button")
    assert reasons(judgement, "e3") == ()


@pytest.mark.parametrize("bounds", [(0, 0, 320, 600), (0, 0, 300, 640)])
def test_dialog_is_smaller_in_either_direction(bounds) -> None:
    ok = el("e2", text="Yes", bounds=(50, 300, 250, 340))
    assert reasons(run([ok], root_bounds=bounds), "e2") == ("dialog_confirm_button",)


@pytest.mark.parametrize("label", ["\uff2f\uff2b", "ok", " Yes ", "はい", "Done"])
def test_confirm_label_forms(label) -> None:
    ok = el("e2", text=label)
    assert reasons(run([ok], root_bounds=(20, 200, 300, 400)), "e2") == (
        "dialog_confirm_button",
    )


def test_confirm_label_from_content_desc_and_descendants() -> None:
    by_desc = el("e2", desc="Confirm")
    row = el("e3", cls="android.widget.LinearLayout", bounds=(0, 300, 320, 360))
    child = el(
        "e4",
        parent="e3",
        text="Yes",
        cls="android.widget.TextView",
        flags=(),
        bounds=(60, 310, 300, 350),
    )
    judgement = run([by_desc, row, child], root_bounds=(20, 200, 300, 400))
    assert reasons(judgement, "e2") == ("dialog_confirm_button",)
    assert reasons(judgement, "e3") == ("dialog_confirm_button",)


@pytest.mark.parametrize(
    "label", ["Book", "Okay then", "Okinawa", "Yesterday", "Continued", "Doneness"]
)
def test_label_that_only_starts_like_a_confirm_word_is_not_one(label) -> None:
    judgement = run([el("e2", text=label)], root_bounds=(20, 200, 300, 400))
    assert "dialog_confirm_button" not in reasons(judgement, "e2")
    assert "confirm_button" not in reasons(judgement, "e2")


def test_book_is_pressable_in_a_dialog_with_a_deny_word() -> None:
    elements = [
        el("e2", text="Book", bounds=(50, 300, 250, 340)),
        el(
            "e3",
            text="Reset",
            cls="android.widget.TextView",
            flags=(),
            bounds=(50, 200, 250, 240),
        ),
    ]
    assert reasons(run(elements, root_bounds=(20, 200, 300, 400)), "e2") == ()


def test_custom_confirm_words() -> None:
    policy = Policy(confirm_words=("sure",))
    elements = [el("e2", text="Sure"), el("e3", text="OK", bounds=(0, 300, 100, 340))]
    judgement = run(elements, policy=policy, root_bounds=(20, 200, 300, 400))
    assert reasons(judgement, "e2") == ("dialog_confirm_button",)
    assert reasons(judgement, "e3") == ()


# -- confirm labels, separators and nested scopes --------------------------------------


def _dialog_button(**kwargs):
    question = el(
        "e9",
        text="Delete account?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(30, 210, 290, 250),
    )
    button = el("e2", bounds=(200, 380, 280, 420), **kwargs)
    return run([question, button], root_bounds=DIALOG)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "", "desc": "OK"},
        {"text": "\u200eOK"},
        {"text": "Yes\u200b"},
        {"text": "\ue900", "desc": "Confirm"},
        {"text": "\ue900 OK"},
        {"text": "OK!"},
        {"text": "Yes."},
        {"text": "\u300c\u306f\u3044\u300d"},
    ],
)
def test_confirm_label_ignores_invisible_characters_and_marks(kwargs) -> None:
    found = reasons(_dialog_button(**kwargs), "e2")
    assert found == ("confirm_button", "dialog_confirm_button")


def test_confirm_label_from_row_children_with_icon() -> None:
    row = el("e2", cls="android.widget.LinearLayout", bounds=(200, 380, 280, 420))
    icon = el("e3", parent="e2", text="\ue900", flags=(), bounds=(200, 380, 220, 420))
    label = el("e4", parent="e2", text="OK", flags=(), bounds=(220, 380, 280, 420))
    judgement = run([row, icon, label], root_bounds=DIALOG)
    assert reasons(judgement, "e2") == ("dialog_confirm_button",)


def test_confirm_label_falls_back_to_descendant_content_desc() -> None:
    warning = el(
        "e2", text="Delete all photos", cls="android.widget.TextView", flags=()
    )
    box = el("e3", cls="android.widget.FrameLayout", bounds=(200, 500, 280, 540))
    icon = el(
        "e4",
        parent="e3",
        desc="Confirm",
        cls="android.widget.ImageView",
        flags=(),
        bounds=(200, 500, 280, 540),
    )
    assert reasons(run([warning, box, icon]), "e3") == ("confirm_button",)


def test_soft_hyphen_delete_button_is_not_pressed() -> None:
    assert reasons(run([el("e2", text="De\u00adlete")]), "e2") == ("deny_word",)


def test_nested_clickable_ancestors_are_all_in_scope() -> None:
    card = el("e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 300))
    title = el(
        "e3",
        parent="e2",
        text="Delete account",
        cls="android.widget.TextView",
        flags=(),
        bounds=(0, 100, 320, 150),
    )
    row = el(
        "e4",
        parent="e2",
        cls="android.widget.LinearLayout",
        bounds=(0, 200, 320, 300),
    )
    details = el(
        "e5", parent="e4", text="Details", cls="android.widget.TextView", flags=()
    )
    icon = el("e6", parent="e4", desc="Go", bounds=(250, 220, 300, 280))
    judgement = run([card, title, row, details, icon])
    assert reasons(judgement, "e6") == ("deny_word",)
    assert reasons(judgement, "e4") == ("deny_word",)


def _layout(ident, bounds, parent=None):
    return Element(
        ident, parent, "android.widget.FrameLayout", Bounds(*bounds), package=PKG
    )


def test_dialog_uses_first_parentless_element() -> None:
    ok = el("e3", parent="e2", text="OK", bounds=(200, 380, 280, 420))
    small = _layout("e2", DIALOG)
    full = _layout("e1", (0, 0, 320, 640))
    dialog = judge(make_screen([small, ok, full]), PKG, Policy(), foreground=PKG)
    assert reasons(dialog, "e3") == ("dialog_confirm_button",)
    full_first = judge(make_screen([full, small, ok]), PKG, Policy(), foreground=PKG)
    assert reasons(full_first, "e3") == ()


def test_dialog_with_unknown_parent_first_element() -> None:
    lost = replace(_layout("e1", (0, 0, 320, 640)), parent="missing")
    ok = el("e3", parent="e2", text="OK", bounds=(200, 380, 280, 420))
    found = judge(
        make_screen([lost, _layout("e2", DIALOG), ok]), PKG, Policy(), foreground=PKG
    )
    assert reasons(found, "e3") == ("dialog_confirm_button",)


def test_dialog_with_two_top_level_elements_dialog_sized_first() -> None:
    ok = el("e3", parent="e2", text="OK", bounds=(200, 380, 280, 420))
    small = _layout("e2", DIALOG)
    full = _layout("e1", (0, 0, 320, 640))
    found = judge(make_screen([small, full, ok]), PKG, Policy(), foreground=PKG)
    assert reasons(found, "e3") == ("dialog_confirm_button",)


# -- confirm label sources, ignorable characters, split phrases, edges -----------------


def _confirm_row(*children, row_kwargs=None):
    row = el(
        "e2",
        cls="android.widget.LinearLayout",
        bounds=(200, 380, 280, 420),
        **(row_kwargs or {}),
    )
    kids = [
        el(
            f"e{3 + index}",
            parent="e2",
            cls="android.widget.TextView",
            flags=(),
            bounds=(200, 380, 280, 420),
            **kwargs,
        )
        for index, kwargs in enumerate(children)
    ]
    return [row, *kids]


def test_glyph_text_with_confirm_content_desc_is_a_dialog_confirm_button() -> None:
    found = reasons(_dialog_button(text="✓", desc="Confirm"), "e2")
    assert found == ("confirm_button", "dialog_confirm_button")


def test_arrow_glyph_with_a_confirm_child_is_a_dialog_confirm_button() -> None:
    question = el(
        "e9",
        text="Delete account?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(30, 210, 290, 250),
    )
    elements = [
        question,
        *_confirm_row({"text": "→"}, {"text": "Continue"}),
    ]
    elements[1] = replace(elements[1], text="→")
    found = reasons(run(elements, root_bounds=DIALOG), "e2")
    assert found == ("confirm_button", "dialog_confirm_button")


def test_confirm_label_from_a_descendant_hint_or_desc_or_joined_text() -> None:
    for kids in (
        [{"hint": "OK"}],
        [{"desc": "Confirm"}],
        [{"text": "→"}, {"text": "Done"}],
    ):
        elements = _confirm_row(*kids)
        judgement = run(elements, root_bounds=DIALOG)
        assert "dialog_confirm_button" in reasons(judgement, "e2"), kids


def test_full_screen_positive_button_with_ok_child_is_a_confirm_button() -> None:
    warning = el(
        "e9",
        text="Delete account?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(30, 210, 290, 250),
    )
    button = el(
        "e2",
        cls="android.widget.FrameLayout",
        desc="Positive button",
        bounds=(200, 380, 280, 420),
    )
    label = el(
        "e3",
        parent="e2",
        text="OK",
        cls="android.widget.TextView",
        flags=(),
        bounds=(200, 380, 280, 420),
    )
    judgement = run([warning, button, label])
    assert reasons(judgement, "e2") == ("confirm_button",)


def test_button_labelled_only_by_hint_ok_in_a_dialog() -> None:
    found = reasons(_dialog_button(hint="OK"), "e2")
    assert found == ("confirm_button", "dialog_confirm_button")


def test_book_is_still_not_a_confirm_word_from_any_source() -> None:
    for kwargs in ({"hint": "Book"}, {"text": "✓", "desc": "Book"}):
        found = reasons(_dialog_button(**kwargs), "e2")
        assert found == (), kwargs
    elements = _confirm_row({"text": "Book"}, {"desc": "Book"})
    assert reasons(run(elements, root_bounds=DIALOG), "e2") == ()


@pytest.mark.parametrize(
    "label",
    ["Del͏ete", "Dele️te", "Del᠋ete", "Deㅤlete"],
)
def test_default_ignorable_characters_do_not_hide_a_deny_word(label) -> None:
    judgement = run([el("e2", text=label)])
    assert reasons(judgement, "e2") == ("deny_word",)
    assert word_matches(label, "delete")


@pytest.mark.parametrize("label", ["OK️", "O͏K", "OKㅤ", "᠋OK"])
def test_default_ignorable_characters_do_not_hide_a_confirm_word(label) -> None:
    found = reasons(_dialog_button(text=label), "e2")
    assert found == ("confirm_button", "dialog_confirm_button")


@pytest.mark.parametrize("label", ["͏", "️", "ㅤ", "ᅟᅠ"])
def test_default_ignorable_only_label_counts_as_empty(label) -> None:
    assert reasons(run([el("e2", text=label)]), "e2") == ("unlabeled",)


@pytest.mark.parametrize(
    ("parts", "word"),
    [(("Sign", "up"), "sign up"), (("Log", "out"), "log out")],
)
def test_deny_phrase_split_over_text_views_of_a_clickable_row(parts, word) -> None:
    row = el("e2", cls="android.widget.LinearLayout", bounds=(0, 300, 320, 360))
    kids = [
        el(
            f"e{3 + index}",
            parent="e2",
            text=part,
            cls="android.widget.TextView",
            flags=(),
            bounds=(60 + 100 * index, 310, 160 + 100 * index, 350),
        )
        for index, part in enumerate(parts)
    ]
    judgement = run([row, *kids])
    assert reasons(judgement, "e2") == ("deny_word",)
    found = decision(judgement, "e2").details[0]
    assert (found.word, found.field, found.element_id) == (word, "joined", "e2")


def test_split_phrase_reaches_a_button_inside_a_clickable_row() -> None:
    row = el("e2", cls="android.widget.LinearLayout", bounds=(0, 300, 320, 360))
    first = el(
        "e3",
        parent="e2",
        text="Log",
        cls="android.widget.TextView",
        flags=(),
        bounds=(0, 300, 100, 360),
    )
    second = el(
        "e4",
        parent="e2",
        desc="out",
        cls="android.widget.TextView",
        flags=(),
        bounds=(100, 300, 200, 360),
    )
    icon = el("e5", parent="e2", text="Go", bounds=(250, 310, 300, 350))
    judgement = run([row, first, second, icon])
    assert reasons(judgement, "e5") == ("deny_word",)


def _text_view(ident, parent, bounds, **kwargs):
    return el(
        ident,
        parent=parent,
        cls="android.widget.TextView",
        flags=(),
        bounds=bounds,
        **kwargs,
    )


def test_confirm_phrase_split_over_text_views_is_a_dialog_confirm_button() -> None:
    button = el("e2", bounds=(150, 340, 280, 420))
    elements = [
        button,
        _text_view("e3", "e2", (150, 340, 200, 420), text="Go"),
        _text_view("e4", "e2", (200, 340, 280, 420), text="ahead"),
    ]
    judgement = run(
        elements,
        policy=Policy(confirm_words=("go ahead",)),
        root_bounds=(20, 200, 300, 440),
    )
    assert reasons(judgement, "e2") == ("dialog_confirm_button",)


def test_deny_phrase_split_in_an_overlay_over_the_press_point() -> None:
    elements = [
        el(
            "e2",
            text="Item",
            cls="android.widget.LinearLayout",
            bounds=(200, 100, 320, 200),
        ),
        el("e3", bounds=(250, 150, 310, 190)),
        _text_view("e4", "e3", (250, 150, 280, 190), text="Log"),
        _text_view("e5", "e3", (280, 150, 310, 190), text="out"),
    ]
    judgement = run(elements)
    assert decision(judgement, "e2").point == (260, 150)
    assert reasons(judgement, "e2") == ("covered", "deny_word")
    found = _detail(judgement, "e2", "deny_word")
    assert (found.word, found.field, found.element_id) == ("log out", "joined", "e3")


def test_deny_phrase_split_in_a_clickable_ancestor_not_holding_the_point() -> None:
    elements = [
        el("e2", cls="android.widget.LinearLayout", bounds=(0, 300, 100, 360)),
        _text_view("e3", "e2", (0, 300, 50, 360), text="Log"),
        _text_view("e4", "e2", (50, 300, 100, 360), text="out"),
        el("e5", parent="e2", text="Go", bounds=(250, 310, 300, 350)),
    ]
    judgement = run(elements)
    assert reasons(judgement, "e5") == ("deny_word",)
    found = decision(judgement, "e5").details[0]
    assert (found.word, found.field, found.element_id) == ("log out", "joined", "e2")


def test_yes_i_am_sure_on_a_full_screen_restart_question_is_refused() -> None:
    found = run(_restart_screen("Yes I'm sure!"))
    assert reasons(found, "e4") == ("confirm_button",)
    assert reasons(found, "e5") == ()
    detail = decision(found, "e4").details[0]
    assert detail.word in {"restart", "undo"}


def test_the_same_screen_in_a_dialog_is_refused_as_a_dialog_button() -> None:
    found = run(_restart_screen("Yes I'm sure!"), root_bounds=DIALOG_ROOT)
    assert "dialog_confirm_button" in reasons(found, "e4")


@pytest.mark.parametrize(
    "label",
    [
        "Yes I'm sure!",
        "OK, delete",
        "Yes, continue",
        "Continue anyway",
        "ok - go",
        "OK.",
        "Done 2",
        "はい、削除します",
        "はい削除します",
        "確認してください",
        "続ける。",
    ],
)
def test_a_label_starting_with_a_confirm_word_is_a_confirm_button(label) -> None:
    found = run([el("e2", text=label)], root_bounds=(20, 200, 300, 400))
    assert "dialog_confirm_button" in reasons(found, "e2")


@pytest.mark.parametrize(
    "label",
    ["Okinawa", "Yesterday", "Continued", "Allowance", "Applying", "Donegal", "Cancel"],
)
def test_a_longer_word_is_not_a_confirm_word(label) -> None:
    found = run([el("e2", text=label)], root_bounds=(20, 200, 300, 400))
    assert reasons(found, "e2") == ()


def test_the_prefix_rule_reads_a_label_split_over_children() -> None:
    row = el("e2", cls=LAYOUT, bounds=(50, 300, 250, 340))
    first = el("e3", parent="e2", cls="android.widget.TextView", text="Yes", flags=())
    second = el(
        "e4", parent="e2", cls="android.widget.TextView", text="I'm sure!", flags=()
    )
    found = run([row, first, second], root_bounds=(20, 200, 300, 400))
    assert "dialog_confirm_button" in reasons(found, "e2")
    long_tail = [
        el("e5", parent="e2", cls="android.widget.TextView", text="x" * 40, flags=())
    ]
    found = run([row, second, *long_tail], root_bounds=(20, 200, 300, 400))
    assert reasons(found, "e2") == ()


def test_a_custom_confirm_word_follows_the_prefix_rule() -> None:
    policy = Policy(confirm_words=("go ahead",))
    dialog = (20, 200, 300, 400)
    assert reasons(
        run([el("e2", text="Go ahead, please")], policy=policy, root_bounds=dialog),
        "e2",
    ) == ("dialog_confirm_button",)
    assert (
        reasons(
            run([el("e2", text="Go aheads")], policy=policy, root_bounds=dialog), "e2"
        )
        == ()
    )
