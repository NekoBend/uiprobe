"""Tests for overlays, hit targets and joined strings in judge."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.guards.helpers import (
    CLICK,
    DIALOG_ROOT,
    EDIT,
    LAYOUT,
    TEXT_VIEW,
    _detail,
    decision,
    el,
    reasons,
    run,
)
from uiprobe import guards
from uiprobe.guards import (
    Policy,
)

# -- overlays, sibling scopes and custom confirm words ---------------------------------


def _overlay_screen(*, fab_text="Delete all"):
    row = el("e2", cls="android.widget.LinearLayout", bounds=(0, 500, 320, 600))
    inbox = el(
        "e3",
        parent="e2",
        text="Inbox",
        cls="android.widget.TextView",
        flags=(),
        bounds=(10, 510, 130, 590),
    )
    fab = el("e4", cls="android.widget.FrameLayout", bounds=(140, 530, 300, 570))
    icon = el(
        "e5",
        parent="e4",
        cls="android.widget.ImageView",
        flags=(),
        bounds=(140, 530, 180, 570),
    )
    label = el(
        "e6",
        parent="e4",
        text=fab_text,
        cls="android.widget.TextView",
        flags=(),
        bounds=(185, 530, 300, 570),
    )
    return [row, inbox, fab, icon, label]


def test_overlay_clickable_subtree_is_in_scope() -> None:
    found = run(_overlay_screen())
    # The row is under the button, so it is covered as well as denied.
    assert reasons(found, "e2") == ("covered", "deny_word")
    assert reasons(found, "e4") == ("deny_word",)
    detail = _detail(found, "e2", "deny_word")
    assert (detail.word, detail.element_id) == ("delete", "e6")
    safe = run(_overlay_screen(fab_text="Compose"))
    assert reasons(safe, "e2") == ("covered",)
    assert reasons(safe, "e4") == ()


def test_overlay_non_clickable_container_adds_only_its_own_fields() -> None:
    button = el("e2", text="Settings")
    layer = el(
        "e3",
        cls="android.widget.LinearLayout",
        flags=(),
        bounds=(0, 80, 320, 170),
    )
    far = el(
        "e4",
        parent="e3",
        text="Delete",
        cls="android.widget.TextView",
        flags=(),
        bounds=(0, 300, 320, 340),
    )
    assert reasons(run([button, layer, far]), "e2") == ()
    named = replace(layer, text="Delete")
    assert reasons(run([button, named, far]), "e2") == ("covered", "deny_word")


def test_deny_word_in_a_sibling_container_elsewhere_does_not_block() -> None:
    elements = [
        el("e2", text="Settings", bounds=(0, 100, 320, 160)),
        el(
            "e3",
            cls="android.widget.LinearLayout",
            flags=(),
            bounds=(0, 300, 320, 400),
        ),
        el(
            "e4",
            parent="e3",
            text="Delete",
            cls="android.widget.TextView",
            flags=(),
            bounds=(0, 300, 320, 400),
        ),
    ]
    assert reasons(run(elements), "e2") == ()


def test_overlay_clickable_ancestor_of_a_covering_element_is_in_scope() -> None:
    row = el("e2", text="Item", bounds=(0, 100, 320, 200))
    card = el("e3", cls="android.widget.LinearLayout", bounds=(200, 120, 320, 200))
    sibling = el(
        "e4",
        parent="e3",
        text="Remove",
        cls="android.widget.TextView",
        flags=(),
        bounds=(200, 120, 220, 140),
    )
    cover = el(
        "e5",
        parent="e3",
        cls="android.widget.ImageView",
        flags=(),
        bounds=(100, 100, 320, 200),
    )
    assert reasons(run([row, card, sibling, cover]), "e2") == ("deny_word",)


def test_custom_confirm_words_are_cleaned_like_the_label() -> None:
    policy = Policy(confirm_words=("Sure!",))
    found = run(
        [el("e2", text="Sure!")], policy=policy, root_bounds=(20, 200, 300, 400)
    )
    assert reasons(found, "e2") == ("dialog_confirm_button",)
    spaced = Policy(confirm_words=("\u200b\u300cOK\u300d ",))
    found = run([el("e2", text="OK")], policy=spaced, root_bounds=(20, 200, 300, 400))
    assert reasons(found, "e2") == ("dialog_confirm_button",)


@pytest.mark.parametrize("word", ["!", "...", "\u300c\u300d", " ! "])
def test_confirm_word_without_a_label_form_is_rejected(word) -> None:
    with pytest.raises(ValueError, match="empty"):
        Policy(confirm_words=(word,))


def test_row_over_its_edit_text_child_is_a_text_input() -> None:
    row = el("e2", cls=LAYOUT, text="Search", bounds=(50, 100, 250, 200))
    for flags in ((), CLICK):
        edit = el(
            "e3",
            parent="e2",
            cls=EDIT,
            text="Search query",
            bounds=(60, 120, 240, 180),
            flags=flags,
        )
        found = run([row, edit])
        assert decision(found, "e2").point == (150, 150)
        assert reasons(found, "e2")[:1] == ("text_input",)
        assert _detail(found, "e2", "text_input").element_id == "e3"


def test_button_under_a_password_field_is_refused() -> None:
    under = el("e2", text="Details")
    secret = el(
        "e3",
        cls=EDIT,
        text="Password",
        bounds=(60, 120, 240, 180),
        flags=("clickable", "password"),
    )
    found = run([under, secret])
    assert reasons(found, "e2") == ("covered", "text_input", "password_field")
    assert _detail(found, "e2", "password_field").element_id == "e3"
    assert reasons(found, "e3") == ("text_input", "password_field")


def test_clickable_card_with_password_descendant_taints_sibling_button() -> None:
    card = el("e2", cls=LAYOUT, bounds=(0, 100, 320, 500))
    secret = el(
        "e3",
        parent="e2",
        cls=EDIT,
        bounds=(10, 110, 310, 150),
        flags=("clickable", "password"),
    )
    forgot = el("e4", parent="e2", text="Forgot?", bounds=(100, 400, 220, 460))
    helper = el("e5", text="Help", bounds=(100, 520, 220, 580))
    found = run([card, secret, forgot, helper])
    assert "password_field" in reasons(found, "e4")
    assert _detail(found, "e4", "password_field").element_id == "e2"
    assert "password_field" not in reasons(found, "e5")
    assert decision(found, "e5").reasons == ()


def test_button_under_a_permission_package_button_is_refused() -> None:
    under = el("e2", text="Details")
    grant = el(
        "e3",
        text="Allow",
        bounds=(60, 120, 240, 180),
        package="com.android.permissioncontroller",
    )
    found = run([under, grant])
    assert reasons(found, "e2") == (
        "outside_target_app",
        "permission_screen",
        "covered",
    )
    assert _detail(found, "e2", "outside_target_app").element_id == "e3"
    assert _detail(found, "e2", "permission_screen").element_id == "e3"


def test_non_clickable_overlay_of_another_package_refuses_the_point() -> None:
    under = el("e2", text="Details")
    overlay = el(
        "e3",
        cls=TEXT_VIEW,
        text="Ad",
        bounds=(60, 120, 240, 180),
        flags=(),
        package="com.other",
    )
    found = run([under, overlay])
    assert reasons(found, "e2") == ("outside_target_app", "covered")


def _under_ok_screen():
    return [
        el("e2", text="Details"),
        el("e3", text="OK", bounds=(60, 120, 240, 180)),
        el(
            "e4",
            cls=TEXT_VIEW,
            text="Delete account?",
            bounds=(50, 300, 250, 340),
            flags=(),
        ),
    ]


def test_button_under_a_confirm_button_is_refused() -> None:
    found = run(_under_ok_screen())
    assert reasons(found, "e2") == ("covered", "confirm_button")
    detail = _detail(found, "e2", "confirm_button")
    assert (detail.word, detail.element_id) == ("delete", "e4")
    assert "e3" in detail.note
    assert reasons(found, "e3") == ("confirm_button",)


def test_button_under_a_confirm_button_in_a_dialog_is_refused() -> None:
    found = run(_under_ok_screen(), root_bounds=(20, 0, 300, 500))
    assert reasons(found, "e2") == (
        "covered",
        "confirm_button",
        "dialog_confirm_button",
    )
    assert "e3" in _detail(found, "e2", "dialog_confirm_button").note


def test_non_clickable_confirm_label_is_no_confirm_button_but_covers() -> None:
    elements = _under_ok_screen()
    elements[1] = replace(elements[1], flags=())
    # It is no confirm button for the point, but its text still covers the row.
    assert reasons(run(elements), "e2") == ("covered",)


def test_plain_buttons_stay_pressable_next_to_overlays() -> None:
    elements = [
        el("e2", text="Settings"),
        el("e3", text="Home", bounds=(0, 300, 90, 340)),
        # An empty container (no text, description or hint) is looked through.
        el("e4", cls=LAYOUT, bounds=(0, 250, 320, 400), flags=()),
    ]
    found = run(elements)
    assert reasons(found, "e2") == ()
    assert reasons(found, "e3") == ()


def test_a_labeled_container_over_a_button_covers_it_only() -> None:
    # The labeled overlay covers e3 (its text is meaningful); e2 is clear.
    elements = [
        el("e2", text="Settings"),
        el("e3", text="Home", bounds=(0, 300, 90, 340)),
        el("e4", cls=LAYOUT, text="Group", bounds=(0, 250, 320, 400), flags=()),
    ]
    found = run(elements)
    assert reasons(found, "e2") == ()
    assert reasons(found, "e3") == ("covered",)


def test_password_in_a_non_clickable_container_does_not_refuse_other_buttons() -> None:
    elements = [
        el("e2", text="Forgot it?", bounds=(0, 500, 320, 560)),
        el("e3", cls=EDIT, flags=("password",), bounds=(0, 100, 320, 160)),
    ]
    assert reasons(run(elements), "e2") == ()


def _split_row(first, second):
    return [
        el("e2", cls=LAYOUT, bounds=(50, 100, 250, 200)),
        el(
            "e3",
            parent="e2",
            cls=TEXT_VIEW,
            bounds=(60, 110, 130, 190),
            flags=(),
            **first,
        ),
        el(
            "e4",
            parent="e2",
            cls=TEXT_VIEW,
            bounds=(130, 110, 240, 190),
            flags=(),
            **second,
        ),
    ]


def test_joined_texts_skip_the_descriptions_between_them() -> None:
    elements = _split_row({"text": "Log", "desc": "Photo"}, {"text": "out"})
    found = run(elements)
    assert reasons(found, "e2") == ("deny_word",)
    detail = _detail(found, "e2", "deny_word")
    assert (detail.word, detail.field, detail.element_id) == ("log out", "joined", "e2")


def test_joined_descriptions_skip_the_texts_between_them() -> None:
    elements = _split_row(
        {"desc": "Log", "text": "Photo"}, {"desc": "out", "text": "x"}
    )
    assert reasons(run(elements), "e2") == ("deny_word",)


def test_joined_text_and_desc_sequence_still_matches() -> None:
    elements = _split_row({"text": "Log"}, {"desc": "out"})
    assert reasons(run(elements), "e2") == ("deny_word",)


def test_confirm_button_sees_a_joined_deny_word_on_the_screen() -> None:
    elements = [
        el("e2", cls=LAYOUT, bounds=(50, 100, 250, 200)),
        el(
            "e3",
            parent="e2",
            cls=TEXT_VIEW,
            text="Log",
            bounds=(60, 110, 130, 190),
            flags=(),
        ),
        el(
            "e4",
            parent="e2",
            cls=TEXT_VIEW,
            text="out?",
            bounds=(130, 110, 240, 190),
            flags=(),
        ),
        el("e5", text="OK", bounds=(50, 300, 250, 340)),
    ]
    found = run(elements)
    assert reasons(found, "e2") == ("deny_word",)
    assert reasons(found, "e5") == ("confirm_button",)
    assert _detail(found, "e5", "confirm_button").word == "log out"


@pytest.mark.parametrize("field", ["text", "desc"])
@pytest.mark.parametrize(("first", "second"), [("Delete", "account?"), ("Log", "out?")])
def test_dialog_body_split_over_text_views_refuses_ok(field, first, second) -> None:
    body = [
        el(
            "e2",
            cls=TEXT_VIEW,
            bounds=(50, 220, 250, 260),
            flags=(),
            **{field: first},
        ),
        el(
            "e3",
            cls=TEXT_VIEW,
            bounds=(50, 260, 250, 300),
            flags=(),
            **{field: second},
        ),
        el("e4", text="OK", bounds=(100, 380, 250, 420)),
    ]
    found = run(body, root_bounds=DIALOG_ROOT)
    assert reasons(found, "e4") == ("confirm_button", "dialog_confirm_button")


def test_dialog_body_split_with_another_field_between_refuses_ok() -> None:
    body = [
        el("e2", cls=TEXT_VIEW, text="Log", desc="Photo", flags=()),
        el("e3", cls=TEXT_VIEW, text="out?", bounds=(50, 260, 250, 300), flags=()),
        el("e4", text="OK", bounds=(100, 380, 250, 420)),
    ]
    found = run(body, root_bounds=DIALOG_ROOT)
    assert reasons(found, "e4") == ("confirm_button", "dialog_confirm_button")


@pytest.mark.parametrize("label", ["( ✓ OK ✓ )", "[ OK ]", "「 はい 」"])
def test_confirm_label_trims_marks_around_spaces(label) -> None:
    found = run([el("e2", text=label)], root_bounds=DIALOG_ROOT)
    assert reasons(found, "e2") == ("dialog_confirm_button",)


def test_screen_with_more_than_the_cap_is_too_large() -> None:
    assert guards.MAX_ELEMENTS == 5000
    buttons = [el(f"b{i}", text="Item") for i in range(5000)]
    found = run(buttons)
    assert len(found.decisions) == 5000
    assert {d.reasons for d in found.decisions} == {("screen_too_large",)}
    assert {d.point for d in found.decisions} == {None}


def test_screen_at_the_cap_is_judged() -> None:
    buttons = [el(f"b{i}", text="Item") for i in range(4999)]
    found = run(buttons)
    assert len(found.decisions) == 4999
    assert all("screen_too_large" not in d.reasons for d in found.decisions)
