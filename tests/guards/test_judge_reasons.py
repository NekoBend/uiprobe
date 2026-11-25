"""Tests for the single reasons of judge and their fixed order."""

from __future__ import annotations

from typing import get_args

import pytest

from tests.guards.helpers import (
    DISPLAY,
    PKG,
    _detail,
    decision,
    el,
    reasons,
    run,
    t_mobile_row,
)
from uiprobe import guards
from uiprobe.guards import (
    REASONS,
    Policy,
    api36_fallback_areas,
    edge_areas,
)
from uiprobe.model import Bounds

# -- each reason, alone ----------------------------------------------------------------


def test_plain_button_is_pressable() -> None:
    judgement = run([el("e2", text="Settings")])
    found = decision(judgement, "e2")
    assert found.reasons == ()
    assert found.pressable
    assert found.point == (150, 125)
    assert judgement.pressable == (found,)
    assert judgement.skipped == ()


def test_only_clickable_elements_are_candidates() -> None:
    judgement = run(
        [
            el("e2", text="Settings", flags=()),
            el("e3", text="Home", bounds=(0, 200, 90, 240)),
        ]
    )
    assert [d.element.id for d in judgement.decisions] == ["e3"]


SINGLE_REASON = [
    ("outside_target_app", {"package": "com.other"}),
    ("not_visible", {"bounds": (400, 100, 500, 150)}),
    ("not_visible", {"bounds": (250, 100, 50, 150)}),
    ("disabled", {"flags": ("clickable", "disabled")}),
    ("text_input", {"cls": "android.widget.EditText"}),
    ("text_input", {"cls": "androidx.appcompat.widget.AppCompatEditText"}),
    (
        "text_input",
        {"cls": "com.google.android.material.textfield.TextInputEditText"},
    ),
    ("text_input", {"cls": "android.widget.AutoCompleteTextView"}),
    (
        "text_input",
        {"cls": "androidx.appcompat.widget.SearchView$SearchAutoComplete"},
    ),
    ("text_input", {"cls": "SearchAutoComplete"}),
    ("unlabeled", {"text": "", "rid": "com.example:id/menu_icon"}),
    ("unlabeled", {"text": " \t\u3000\n"}),
    ("deny_word", {"text": "Delete"}),
]


@pytest.mark.parametrize(("reason", "kwargs"), SINGLE_REASON)
def test_single_reason(reason, kwargs) -> None:
    kwargs = {"text": "Item", **kwargs}
    judgement = run([el("e2", **kwargs)])
    assert reasons(judgement, "e2") == (reason,)


@pytest.mark.parametrize(
    ("foreground", "target", "package"),
    [
        (None, PKG, PKG),
        ("com.other", PKG, PKG),
        (PKG, PKG, "com.other"),
        (PKG, "com.other", PKG),
    ],
)
def test_nothing_is_pressable_outside_the_target(foreground, target, package) -> None:
    elements = [el("e2", text="A"), el("e3", text="B", bounds=(0, 200, 90, 240))]
    judgement = run(elements, foreground=foreground, target=target, package=package)
    assert judgement.pressable == ()
    assert [d.reasons for d in judgement.decisions] == [("outside_target_app",)] * 2


@pytest.mark.parametrize("package", sorted(guards.PERMISSION_PACKAGES))
def test_permission_screen(package) -> None:
    allow = el(
        "e2",
        text="Not now",
        rid="com.android.packageinstaller:id/permission_deny_button",
        package=package,
    )
    judgement = run(
        [allow],
        foreground=package,
        target=package,
        package=package,
        root_package=package,
    )
    assert reasons(judgement, "e2") == ("permission_screen",)


def test_permission_elements_in_the_target_app() -> None:
    allow = el("e2", text="Not now", package="com.google.android.packageinstaller")
    other = el("e3", text="Home", bounds=(0, 200, 90, 240))
    judgement = run([allow, other])
    assert reasons(judgement, "e2") == ("outside_target_app", "permission_screen")
    assert reasons(judgement, "e3") == ()


def test_password_flag_is_text_input_and_password_field() -> None:
    judgement = run([el("e2", text="Secret", flags=("clickable", "password"))])
    assert reasons(judgement, "e2") == ("text_input", "password_field")


def test_password_in_a_descendant() -> None:
    elements = [
        el(
            "e2",
            text="Profile",
            cls="android.widget.LinearLayout",
            bounds=(0, 100, 320, 300),
        ),
        el(
            "e3",
            parent="e2",
            text="x",
            cls="android.widget.EditText",
            flags=("password",),
            bounds=(10, 120, 300, 160),
        ),
    ]
    assert reasons(run(elements), "e2") == ("password_field",)


def test_clickable_container_of_a_text_field_is_not_a_text_input() -> None:
    elements = [
        el(
            "e2",
            text="Row",
            cls="android.widget.LinearLayout",
            bounds=(0, 100, 320, 300),
        ),
        el("e3", parent="e2", text="x", cls="android.widget.EditText", flags=()),
    ]
    assert reasons(run(elements), "e2") == ()


def test_empty_input_field_with_hint_shown_as_text() -> None:
    field = el(
        "e2",
        text="Search settings",
        hint="Search settings",
        cls="android.widget.EditText",
    )
    assert reasons(run([field]), "e2") == ("text_input",)


def test_hint_alone_labels_a_button() -> None:
    assert reasons(run([el("e2", hint="Search")]), "e2") == ()


# -- labels ----------------------------------------------------------------------------


def test_icon_only_button_needs_tap_unlabeled() -> None:
    icon = el("e2", rid="com.example:id/icon_more")
    assert reasons(run([icon]), "e2") == ("unlabeled",)
    allowed = Policy(tap_unlabeled=True)
    assert reasons(run([icon], policy=allowed), "e2") == ()


@pytest.mark.parametrize(
    "label", ["", "\ue900", "\ue900 \ue901", " \u200b ", "\U000f0001"]
)
def test_private_use_only_label_counts_as_empty(label) -> None:
    assert reasons(run([el("e2", text=label)]), "e2") == ("unlabeled",)
    assert reasons(run([el("e2", desc=label)]), "e2") == ("unlabeled",)


def test_private_use_with_real_text_is_labeled() -> None:
    assert reasons(run([el("e2", text="\ue900 Home")]), "e2") == ()


def test_api36_settings_row_is_labeled_through_its_child() -> None:
    elements = [
        el("e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 160)),
        el(
            "e3",
            parent="e2",
            text="Network & internet",
            cls="android.widget.TextView",
            rid="android:id/title",
            flags=(),
            bounds=(60, 110, 300, 150),
        ),
    ]
    assert reasons(run(elements), "e2") == ()


def test_deny_word_found_through_the_child_of_the_row() -> None:
    elements = [
        el("e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 160)),
        el(
            "e3",
            parent="e2",
            text="Reset options",
            cls="android.widget.TextView",
            flags=(),
            bounds=(60, 110, 300, 150),
        ),
    ]
    judgement = run(elements)
    assert reasons(judgement, "e2") == ("deny_word",)
    detail = decision(judgement, "e2").details[0]
    assert (detail.word, detail.field, detail.element_id) == ("reset", "text", "e3")


def test_unlabeled_icon_in_a_row_with_a_deny_word() -> None:
    elements = [
        el("e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 160)),
        el(
            "e3",
            parent="e2",
            text="Delete account",
            cls="android.widget.TextView",
            flags=(),
            bounds=(60, 110, 240, 150),
        ),
        el(
            "e4",
            parent="e2",
            cls="android.widget.ImageButton",
            bounds=(250, 110, 300, 150),
        ),
    ]
    judgement = run(elements)
    assert reasons(judgement, "e4") == ("unlabeled", "deny_word")
    assert decision(judgement, "e4").details[-1].element_id == "e3"
    allowed = run(elements, policy=Policy(tap_unlabeled=True))
    assert reasons(allowed, "e4") == ("deny_word",)


def test_floating_button_over_a_row() -> None:
    def build(row_bounds):
        return [
            el("e2", text="Item", cls="android.widget.LinearLayout", bounds=row_bounds),
            el("e3", text="Send", bounds=(250, 150, 310, 190)),
        ]

    covered = run(build((200, 100, 320, 200)))
    assert decision(covered, "e2").point == (260, 150)
    # The button is on top of the row, so the row is also covered.
    assert reasons(covered, "e2") == ("covered", "deny_word")
    assert _detail(covered, "e2", "covered").element_id == "e3"
    assert _detail(covered, "e2", "deny_word").element_id == "e3"
    clear = run(build((0, 100, 100, 200)))
    assert reasons(clear, "e2") == ()
    assert reasons(clear, "e3") == ("deny_word",)


def test_deny_word_elsewhere_in_another_row_does_not_block() -> None:
    elements = [
        el("e2", text="Item", bounds=(0, 100, 320, 160)),
        el("e3", text="Delete", bounds=(0, 200, 320, 260)),
    ]
    judgement = run(elements)
    assert reasons(judgement, "e2") == ()
    assert reasons(judgement, "e3") == ("deny_word",)


# -- screen edges ----------------------------------------------------------------------


def test_edge_areas() -> None:
    assert edge_areas(DISPLAY) == (Bounds(0, 0, 320, 24),)
    assert edge_areas(DISPLAY, top=0) == ()
    assert edge_areas(DISPLAY, top=24, bottom=32, left=30, right=30) == (
        Bounds(0, 0, 320, 24),
        Bounds(0, 608, 320, 640),
        Bounds(0, 0, 30, 640),
        Bounds(290, 0, 320, 640),
    )
    assert api36_fallback_areas(DISPLAY) == edge_areas(
        DISPLAY, top=24, bottom=32, left=30, right=30
    )
    with pytest.raises(ValueError, match="negative"):
        edge_areas(DISPLAY, left=-1)


@pytest.mark.parametrize(
    ("bounds", "edge"),
    [
        ((50, 0, 250, 30), True),  # center y 15, status bar
        ((50, 20, 250, 30), False),  # center y 25, just below the bar
        ((50, 18, 250, 29), True),  # center y 23
        ((0, 616, 320, 640), True),  # API 36 navigation bar
        ((50, 580, 250, 620), False),  # center y 600
        ((50, 590, 250, 628), True),  # center y 609
        ((0, 300, 40, 340), True),  # center x 20, left gesture area
        ((23, 300, 36, 340), True),  # center x 29
        ((24, 300, 36, 340), False),  # center x 30, edge is exclusive
        ((280, 300, 320, 340), True),  # center x 300, right gesture area
        ((280, 300, 298, 340), False),  # center x 289
        ((50, 300, 250, 340), False),
    ],
)
def test_screen_edge(bounds, edge) -> None:
    policy = Policy(avoid=api36_fallback_areas(DISPLAY))
    judgement = run([el("e2", text="Item", bounds=bounds)], policy=policy)
    assert reasons(judgement, "e2") == (("screen_edge",) if edge else ())


def test_screen_edge_is_off_with_empty_avoid() -> None:
    judgement = run(
        [el("e2", text="Item", bounds=(50, 0, 250, 30))], policy=Policy(avoid=())
    )
    assert reasons(judgement, "e2") == ()


def test_status_bar_only_policy() -> None:
    policy = Policy(avoid=edge_areas(DISPLAY))
    judgement = run(
        [
            el("e2", text="Clock", bounds=(0, 0, 100, 24)),
            el("e3", text="Home", bounds=(0, 616, 320, 640)),
        ],
        policy=policy,
    )
    assert reasons(judgement, "e2") == ("screen_edge",)
    assert reasons(judgement, "e3") == ()


# -- combined reasons and fixed order --------------------------------------------------


def test_every_reason_together_in_the_fixed_order() -> None:
    policy = Policy(avoid=edge_areas(DISPLAY))
    everything = el(
        "e2",
        cls="android.widget.EditText",
        flags=("clickable", "disabled", "password"),
        bounds=(50, 0, 250, 30),
        package="com.android.packageinstaller",
    )
    cover = el(
        "e3",
        text="Delete",
        cls="android.widget.TextView",
        flags=(),
        bounds=(50, 0, 250, 30),
    )
    first = run([everything, cover], policy=policy)
    assert reasons(first, "e2") == (
        "outside_target_app",
        "permission_screen",
        "covered",
        "disabled",
        "text_input",
        "password_field",
        "unlabeled",
        "deny_word",
        "screen_edge",
    )

    hidden = el(
        "e2",
        text="Hi",
        flags=("clickable", "disabled"),
        bounds=(400, 0, 500, 30),
        package="com.android.permissioncontroller",
    )
    second = run([hidden])
    assert reasons(second, "e2") == (
        "outside_target_app",
        "permission_screen",
        "not_visible",
        "disabled",
    )

    ok = el("e2", text="OK", bounds=(50, 0, 250, 30))
    warning = el(
        "e3",
        text="Block",
        cls="android.widget.TextView",
        flags=(),
        bounds=(50, 200, 250, 240),
    )
    third = run([ok, warning], policy=policy, root_bounds=(20, 0, 300, 400))
    assert reasons(third, "e2") == (
        "confirm_button",
        "dialog_confirm_button",
        "screen_edge",
    )

    huge = run([el(f"b{i}", text="Item") for i in range(guards.MAX_ELEMENTS)])
    numeric = el("e2", text="200", flags=("clickable", "checkable"))
    fourth = run([numeric])
    assert reasons(fourth, "e2") == ("number_only", "toggle")
    warned = el("e2", text="Browse", bounds=(50, 100, 250, 150))
    note = el(
        "e3",
        text="This cannot be undone",
        cls="android.widget.TextView",
        flags=(),
        bounds=(20, 300, 300, 340),
    )
    fifth = run([warned, note])
    assert reasons(fifth, "e2") == ("irreversible_screen",)
    sixth = run(t_mobile_row())
    assert reasons(sixth, "e2") == ("two_target_row",)
    wide = Policy(skip_rows_with_buttons=True)
    seventh = run(t_mobile_row(divider=False), policy=wide)
    assert reasons(seventh, "e2") == ("row_with_button",)
    eighth = run([el("e2", desc="Navigate up")])
    assert reasons(eighth, "e2") == ("navigate_up",)
    ninth = run([el("e2", desc="Volume", cls="android.widget.SeekBar")])
    assert reasons(ninth, "e2") == ("slider",)
    seen = {
        r
        for j in (
            first,
            second,
            third,
            fourth,
            huge,
            fifth,
            sixth,
            seventh,
            eighth,
            ninth,
        )
        for d in j.decisions
        for r in d.reasons
    }
    assert seen == set(REASONS)
    assert REASONS[0] == "screen_too_large"


def test_reasons_follow_the_order_of_reasons_constant() -> None:
    assert get_args(guards.Reason) == REASONS
    assert len(set(REASONS)) == len(REASONS) == 20
    order = {reason: index for index, reason in enumerate(REASONS)}
    policy = Policy(avoid=edge_areas(DISPLAY))
    elements = [
        el(
            "e2",
            text="Delete",
            bounds=(50, 0, 250, 30),
            flags=("clickable", "disabled"),
        ),
        el("e3", text="OK", bounds=(50, 100, 250, 130), package="x"),
    ]
    judgement = run(elements, policy=policy, root_bounds=(20, 0, 300, 400))
    for found in judgement.decisions:
        indexes = [order[r] for r in found.reasons]
        assert indexes == sorted(indexes)


def test_point_is_center_of_visible_part_rounded_down() -> None:
    clipped = el("e2", text="Item", bounds=(-10, 600, 51, 700))
    found = decision(run([clipped]), "e2")
    assert found.point == (25, 620)
    assert decision(run([el("e2", text="Item", bounds=(0, 0, 9, 9))]), "e2").point == (
        4,
        4,
    )
    hidden = decision(run([el("e2", text="Item", bounds=(0, 700, 5, 710))]), "e2")
    assert hidden.point is None


def test_duplicate_element_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        run([el("e2", text="Hello"), el("e2", text="Delete all")])


def test_default_policy_avoids_the_screen_edges() -> None:
    assert Policy().avoid is None
    for bounds in ((0, 616, 320, 640), (50, 0, 250, 30), (0, 300, 40, 340)):
        found = reasons(run([el("e2", text="Item", bounds=bounds)]), "e2")
        assert found == ("screen_edge",), bounds


def test_explicit_empty_avoid_turns_the_edge_rule_off() -> None:
    policy = Policy(avoid=())
    for bounds in ((0, 616, 320, 640), (50, 0, 250, 30)):
        judgement = run([el("e2", text="Item", bounds=bounds)], policy=policy)
        assert reasons(judgement, "e2") == (), bounds
