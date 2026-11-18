"""Tests for the no-press rules (offline, screens built by hand)."""

from __future__ import annotations

import ast
import itertools
import random
import time
import unicodedata
from collections import Counter
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest

from uiprobe import guards
from uiprobe.errors import ElementNotVisibleError
from uiprobe.guards import (
    CONFIRM_WORDS,
    DEFAULT_DENY_WORDS,
    REASONS,
    Detail,
    Judgement,
    Policy,
    api36_fallback_areas,
    deny_word_in,
    edge_areas,
    judge,
    normalize,
    parse_deny_file,
    word_matches,
)
from uiprobe.model import Bounds, Element, Screen

PKG = "com.example.shop"
DISPLAY = (320, 640)
CLICK = ("clickable",)
BUTTON = "android.widget.Button"
GUARDS_PY = Path(guards.__file__)


def root(bounds=(0, 0, 320, 640), *, package=PKG):
    return Element(
        "e1", None, "android.widget.FrameLayout", Bounds(*bounds), package=package
    )


def el(
    ident,
    *,
    parent="e1",
    text="",
    desc="",
    hint="",
    rid="",
    cls=BUTTON,
    bounds=(50, 100, 250, 150),
    flags=CLICK,
    package=PKG,
):
    return Element(
        ident,
        parent,
        cls,
        Bounds(*bounds),
        text=text,
        resource_id=rid,
        content_desc=desc,
        hint=hint,
        package=package,
        flags=tuple(flags),
    )


def make_screen(elements, *, package=PKG):
    return Screen(
        serial="emulator-5554",
        captured_at=datetime(2026, 11, 17, tzinfo=UTC),
        package=package,
        activity=".Main",
        api_level=36,
        display=DISPLAY,
        elements=tuple(elements),
    )


def run(
    elements,
    *,
    policy=None,
    foreground=PKG,
    target=PKG,
    package=PKG,
    root_bounds=(0, 0, 320, 640),
    root_package=PKG,
):
    elements = [root(root_bounds, package=root_package), *elements]
    return judge(
        make_screen(elements, package=package),
        target,
        policy or Policy(),
        foreground=foreground,
    )


def reasons(judgement: Judgement, ident: str):
    for decision in judgement.decisions:
        if decision.element.id == ident:
            return decision.reasons
    msg = f"{ident} is not a candidate"
    raise AssertionError(msg)


def decision(judgement: Judgement, ident: str):
    return next(d for d in judgement.decisions if d.element.id == ident)


# -- lists and policy ------------------------------------------------------------------


def test_default_lists_hold_expected_words() -> None:
    for word in ("削除", "ログアウト", "sign up", "log out", "check out", "無料体験"):
        assert word in DEFAULT_DENY_WORDS
    for word in ("OK", "はい", "yes", "done", "許可"):
        assert word in CONFIRM_WORDS
    assert len(set(DEFAULT_DENY_WORDS)) == len(DEFAULT_DENY_WORDS)


def test_policy_defaults() -> None:
    policy = Policy()
    assert policy.deny_words == DEFAULT_DENY_WORDS
    assert policy.confirm_words == CONFIRM_WORDS
    assert policy.tap_unlabeled is False
    assert policy.avoid is None
    assert policy.skip_number_only is True
    assert policy.skip_toggles is True


def test_with_deny_words_adds_and_dedupes() -> None:
    policy = Policy(deny_words=("buy",)).with_deny_words(["rent", "buy"])
    assert policy.deny_words == ("buy", "rent")


def test_with_deny_words_copies_every_other_field() -> None:
    # The copy is built by explicit construction: a field added later and not
    # passed on would silently fall back to its default.
    original = Policy(
        deny_words=("buy",),
        confirm_words=("go",),
        tap_unlabeled=True,
        avoid=(Bounds(0, 0, 10, 10),),
        skip_number_only=False,
        skip_toggles=False,
        refuse_dialog_confirm=False,
        edges=(1, 2, 3, 4),
        min_visible_px=3,
    )
    copy = original.with_deny_words(["rent"])
    assert copy is not original
    for f in fields(Policy):
        if f.name == "deny_words":
            assert copy.deny_words == ("buy", "rent")
        else:
            assert getattr(copy, f.name) == getattr(original, f.name), f.name


def test_confirm_detail_copies_every_other_field(monkeypatch) -> None:
    values = {f.name: f"value-{f.name}" for f in fields(Detail)}
    values["reason"] = "confirm_button"
    original = Detail(**values)
    monkeypatch.setattr(guards._Judge, "screen_deny", lambda _self: original)
    ok = el("e2", text="OK", bounds=(50, 300, 250, 340))
    copy = decision(run([ok]), "e2").details[0]
    assert copy is not original
    for f in fields(Detail):
        if f.name == "note":
            assert copy.note != original.note
            assert copy.note
        else:
            assert getattr(copy, f.name) == getattr(original, f.name), f.name


@pytest.mark.parametrize("word", ["", "   ", " \u3000 ", "\n"])
def test_empty_word_is_rejected(word) -> None:
    with pytest.raises(ValueError, match="empty"):
        Policy().with_deny_words([word])
    with pytest.raises(ValueError, match="empty"):
        Policy(deny_words=(word,))
    with pytest.raises(ValueError, match="empty"):
        Policy(confirm_words=(word,))
    with pytest.raises(ValueError, match="empty"):
        word_matches("x", word)


def test_extra_deny_word_blocks() -> None:
    elements = [el("e2", text="Rent a car")]
    assert reasons(run(elements), "e2") == ()
    policy = Policy().with_deny_words(["rent"])
    assert reasons(run(elements, policy=policy), "e2") == ("deny_word",)


# -- matching --------------------------------------------------------------------------

MATCHES = [
    ("sign up", "sign_up"),
    ("sign up", "signUp"),
    ("sign up", "sign-up"),
    ("sign up", "signup"),
    ("sign up", "SIGN UP"),
    ("sign up", "Si gn up"),
    ("sign up", "Sign\nup"),
    ("sign up", "HTMLSignUp"),
    ("sign up", "sign.up"),
    ("sign up", "a/sign:up"),
    ("log out", "log_out"),
    ("log out", "LogOut"),
    ("log out", "LOGOUT"),
    ("logout", "log out"),
    ("ログアウト", "ﾛｸﾞｱｳﾄ"),
    ("ログアウト", "アカウントからログアウト"),
    ("buy", "\uff22\uff35\uff39"),
    ("buy", "Buy now"),
    ("delete", "\nDelete"),
    ("delete", "\u00a0Delete\u3000"),
    ("削除", "削除する"),
    ("check out", "CheckOut"),
    ("post", "Posture"),
    ("order", "Recorder"),
]
NO_MATCHES = [
    ("pay", "Home"),
    ("sign up", "sign in"),
    ("buy", "Bus"),
    ("削除", "追加"),
    ("log out", "log in"),
]


@pytest.mark.parametrize(("word", "value"), MATCHES)
def test_word_matches(word, value) -> None:
    assert word_matches(value, word)


@pytest.mark.parametrize(("word", "value"), NO_MATCHES)
def test_word_does_not_match(word, value) -> None:
    assert not word_matches(value, word)


def test_normalize() -> None:
    assert normalize("\uff2f\uff2b") == "ok"
    assert normalize("ﾛｸﾞｱｳﾄ") == "ログアウト"
    assert normalize("  A\t\n B\u3000C  ") == "a b c"
    assert normalize("") == ""


RESOURCE_IDS = [
    ("com.example:id/btn_delete", "delete", ("delete", "resource_id")),
    ("com.example:id/btnDelete", "delete", ("delete", "resource_id")),
    ("btn_delete", "delete", ("delete", "resource_id")),
    ("com.example.pay:id/next", "pay", None),
    ("com.android.soundrecorder:id/title", "order", None),
    ("com.pay.app:id/title", "pay", None),
]


@pytest.mark.parametrize(("resource_id", "word", "expected"), RESOURCE_IDS)
def test_resource_id_part_only(resource_id, word, expected) -> None:
    element = el("e2", rid=resource_id)
    assert deny_word_in(element, [word]) == expected


@pytest.mark.parametrize("field", ["text", "desc", "hint"])
def test_every_field_is_matched(field) -> None:
    element = el("e2", **{field: "Delete"})
    expected = {"text": "text", "desc": "content_desc", "hint": "hint"}[field]
    assert deny_word_in(element, ["delete"]) == ("delete", expected)


def test_deny_word_detail_names_word_field_and_element() -> None:
    elements = [el("e2", text="Hello", desc="Please Log_Out")]
    found = decision(run(elements), "e2")
    detail = next(d for d in found.details if d.reason == "deny_word")
    assert (detail.word, detail.field, detail.element_id) == (
        "log out",
        "content_desc",
        "e2",
    )


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
    seen = {
        r
        for j in (first, second, third, fourth, huge, fifth)
        for d in j.decisions
        for r in d.reasons
    }
    assert seen == set(REASONS)
    assert REASONS[0] == "screen_too_large"


def test_reasons_follow_the_order_of_reasons_constant() -> None:
    assert get_args(guards.Reason) == REASONS
    assert len(set(REASONS)) == len(REASONS) == 16
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


# -- parse_deny_file -------------------------------------------------------------------


def test_parse_deny_file_basic() -> None:
    data = "# list\n\n  rent  \n\t# note\n購入\r\nsign up\r\n".encode()
    assert parse_deny_file(data) == ("rent", "購入", "sign up")


def test_parse_deny_file_bom() -> None:
    assert parse_deny_file(b"\xef\xbb\xbfrent\nbuy") == ("rent", "buy")
    assert parse_deny_file(b"") == ()
    assert parse_deny_file(b"\xef\xbb\xbf") == ()


def test_parse_deny_file_does_not_split_on_other_separators() -> None:
    assert parse_deny_file("a\u2028b\u3000c".encode()) == ("a\u2028b\u3000c",)


def test_parse_deny_file_invalid_utf8_names_the_line() -> None:
    with pytest.raises(ValueError, match="line 3"):
        parse_deny_file(b"ok\nfine\nbad\xff\n")
    with pytest.raises(ValueError, match="line 1"):
        parse_deny_file(b"\xef\xbb\xbf\xff")
    with pytest.raises(ValueError, match="line 2"):
        parse_deny_file(b"ok\r\n" + "削除".encode("cp932"))


@pytest.mark.parametrize(
    ("bom", "codec"), [(b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
)
def test_parse_deny_file_utf16_bom(bom, codec) -> None:
    data = bom + "buy\n".encode(codec)
    with pytest.raises(ValueError, match=r"^the file is UTF-16; save it as UTF-8$"):
        parse_deny_file(data)


def test_parse_deny_file_then_policy() -> None:
    words = parse_deny_file(b"rent\n")
    assert Policy().with_deny_words(words).deny_words[-1] == "rent"
    with pytest.raises(ValueError, match="empty"):
        Policy().with_deny_words((*parse_deny_file(b"x\n#c\n"), "\u3000"))


# -- seeded property test --------------------------------------------------------------

SAFE = [
    "Home",
    "Back",
    "Open",
    "Help",
    "Profile",
    "Search",
    "Settings",
    "Menu",
    "listItem",
    "tab_one",
    "ホーム",
    "設定",
    "Photos",
    "OK",
    "Yes",
    "Done",
    "はい",
]
DENY = [
    "Delete",
    "delete account",
    "signUp",
    "sign-up",
    "SIGN UP",
    "Sign\nup",
    "\uff22\uff35\uff39",
    "ﾛｸﾞｱｳﾄ",
    "Log\nOut",
    "log_out",
    "削除",
    "Pay now",
    "購入",
    "\nSend",
    "Signup",
    "LOGOFF",
    "signoff",
    "Sign\u2010up",
    "De\u00adlete",
    "Log\u200bout",
    "Logoff",
    "Si gn up",
    "SignOff",
]
BLANK = [" ", "\u200b", "\ue900", "\u3000\u200e"]
SAFE_IDS = ["com.x:id/icon", "com.x:id/title", "com.example.pay:id/next", ""]
DENY_IDS = [
    "com.x:id/btn_delete",
    "com.x:id/signUpButton",
    "com.x:id/log_out",
    "com.x:id/signupbtn",
]
CLASSES = [
    "android.widget.Button",
    "android.widget.TextView",
    "android.widget.LinearLayout",
    "android.widget.EditText",
    "android.widget.ImageView",
]


def _random_screen(rng: random.Random):
    elements: list[Element] = []
    denies: set[str] = set()

    def pick(safe, deny, blanks=BLANK):
        roll = rng.random()
        if roll < 0.35:
            return "", False
        if roll < 0.45:
            return rng.choice(blanks), False
        if roll < 0.8:
            return rng.choice(safe), False
        return rng.choice(deny), True

    def add(parent, depth):
        ident = f"e{len(elements) + 1}"
        left = rng.randint(-20, 300)
        top = rng.randint(-20, 620)
        bounds = Bounds(
            left, top, left + rng.randint(0, 200), top + rng.randint(0, 200)
        )
        text, d1 = pick(SAFE, DENY)
        desc, d2 = pick(SAFE, DENY)
        hint, d3 = pick(SAFE, DENY)
        rid, d4 = pick(SAFE_IDS, DENY_IDS, ("",))
        flags = tuple(
            flag
            for flag, chance in (
                ("clickable", 0.55),
                ("disabled", 0.1),
                ("password", 0.05),
            )
            if rng.random() < chance
        )
        elements.append(
            Element(
                ident,
                parent,
                rng.choice(CLASSES),
                bounds,
                text=text,
                resource_id=rid,
                content_desc=desc,
                hint=hint,
                package=PKG if rng.random() < 0.9 else "com.other",
                flags=flags,
            )
        )
        if d1 or d2 or d3 or d4:
            denies.add(ident)
        if depth < 3:
            for _ in range(rng.randint(0, 3)):
                if len(elements) < 30:
                    add(ident, depth + 1)

    for _ in range(rng.randint(1, 3)):
        add(None, 0)
    return make_screen(elements), denies


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


# -- imports ---------------------------------------------------------------------------


def test_guards_imports_only_allowed_modules() -> None:
    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "re",
        "typing",
        "unicodedata",
        "uiprobe.model",
    }
    tree = ast.parse(GUARDS_PY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module or "")
    assert imported <= allowed
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"open", "exec", "eval", "__import__"}


def test_replace_keeps_validation() -> None:
    with pytest.raises(ValueError, match="empty"):
        replace(Policy(), deny_words=(" ",))


# -- confirm labels, separators and nested scopes --------------------------------------

DIALOG = (20, 200, 300, 440)


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


@pytest.mark.parametrize(
    ("value", "word"),
    [
        ("De\u00adlete account", "delete"),
        ("Sign\u2011up", "sign up"),
        ("Log\u2010out", "log out"),
        ("Un\u200bsubscribe", "unsubscribe"),
        ("Check\u2013out", "checkout"),
        ("\u30ed\u30b0\u30fb\u30a2\u30a6\u30c8", "\u30ed\u30b0\u30a2\u30a6\u30c8"),
        ("Sign\u2060Out", "sign out"),
        ("Log\u200bout", "log out"),
        ("\u524a\u200b\u9664", "\u524a\u9664"),
        ("Sign\ue900up", "sign up"),
    ],
)
def test_word_matches_through_invisible_and_unicode_separators(value, word) -> None:
    assert word_matches(value, word)


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


def test_duplicate_element_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        run([el("e2", text="Hello"), el("e2", text="Delete all")])


def test_parse_deny_file_rejects_nul_and_control_characters() -> None:
    with pytest.raises(ValueError, match=r"line 1.*UTF-16"):
        parse_deny_file("delete\nbuy\n".encode("utf-16-le"))
    with pytest.raises(ValueError, match="line 2"):
        parse_deny_file(b"ok\ndel\x00ete\n")
    with pytest.raises(ValueError, match="line 1"):
        parse_deny_file(b"delete\rbuy\rpay\n")


@pytest.mark.parametrize("word", ["\x00", "\u200b", "\ue900", "\u200b \ue900"])
def test_policy_rejects_words_without_visible_characters(word) -> None:
    with pytest.raises(ValueError, match="empty"):
        Policy().with_deny_words([word])


def test_with_deny_words_rejects_a_bare_string() -> None:
    with pytest.raises(TypeError):
        Policy().with_deny_words("rent")


def test_dialog_with_two_top_level_elements_dialog_sized_first() -> None:
    ok = el("e3", parent="e2", text="OK", bounds=(200, 380, 280, 420))
    small = _layout("e2", DIALOG)
    full = _layout("e1", (0, 0, 320, 640))
    found = judge(make_screen([small, full, ok]), PKG, Policy(), foreground=PKG)
    assert reasons(found, "e3") == ("dialog_confirm_button",)


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


def test_default_ignorable_ranges_cover_known_characters() -> None:
    chars = [
        "­",
        "͏",
        "؜",
        "ᅟ",
        "ᅠ",
        "឴",
        "឵",
        "᠋",
        "᠏",
        "​",
        "‏",
        "‪",
        "‮",
        "⁠",
        "⁯",
        "ㅤ",
        "︀",
        "️",
        "﻿",
        "ﾠ",
        "\U0001bca0",
        "\U0001bca3",
        "\U0001d173",
        "\U0001d17a",
        "\U000e0000",
        "\U000e0fff",
    ]
    for char in chars:
        assert word_matches(f"De{char}lete", "delete"), hex(ord(char))


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


# -- hit targets, joined strings, size cap, deny file, word lists ----------------------

LAYOUT = "android.widget.LinearLayout"
TEXT_VIEW = "android.widget.TextView"
EDIT = "android.widget.EditText"
DIALOG_ROOT = (20, 200, 300, 500)


def _detail(found, ident, reason):
    return next(d for d in decision(found, ident).details if d.reason == reason)


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


@pytest.mark.parametrize(
    "data",
    [
        b"delete\r",
        b"\x0bdelete\n",
        b"delete\x0c\n",
        b"# note\x01\n",
        b"delete\x7f\n",
        b"a\rb\n",
        b"a\r\r\n",
        b"# note\x1b[0m\n",
        b"\x1fdelete\n",
    ],
)
def test_parse_deny_file_rejects_controls_before_stripping(data) -> None:
    with pytest.raises(ValueError, match="line 1"):
        parse_deny_file(data)


def test_parse_deny_file_names_the_line_of_a_comment_control() -> None:
    with pytest.raises(ValueError, match="line 2"):
        parse_deny_file(b"ok\n# x\x01\n")


def test_parse_deny_file_accepts_tabs_and_crlf() -> None:
    assert parse_deny_file(b"\tdelete\t\r\n# a\tb\r\nbuy\n") == ("delete", "buy")
    assert parse_deny_file(b"# note\r\n") == ()


BASE_DENY = [
    *[
        "購入",
        "買う",
        "支払",
        "決済",
        "注文",
        "課金",
        "定期購入",
        "登録",
        "送信",
        "投稿",
        "共有",
        "削除",
        "消去",
    ],
    *[
        "ログアウト",
        "サインアウト",
        "退会",
        "解約",
        "初期化",
        "リセット",
        "通報",
        "ブロック",
        "電話",
        "発信",
    ],
    *["buy", "purchase", "pay", "checkout", "order", "subscribe", "register"],
    "sign up",
    *["send", "post", "share", "delete", "remove", "erase"],
    "log out",
    "logout",
    "sign out",
    "signout",
    *["unsubscribe", "reset", "report", "block", "call"],
]
EXTRA_DENY = [
    *[
        "購読",
        "申込",
        "申し込",
        "予約",
        "契約",
        "送金",
        "振込",
        "振り込",
        "寄付",
        "招待",
        "公開",
    ],
    *["アップロード", "返信", "同意", "インストール", "破棄", "クリア", "フォーマット"],
    *["アップグレード", "プレミアム", "無料体験", "チェックアウト"],
    *["やり直", "元に戻"],
    "check out",
    *[
        "submit",
        "subscription",
        "trial",
        "upgrade",
        "premium",
        "install",
        "uninstall",
        "clear",
    ],
    *[
        "wipe",
        "format",
        "discard",
        "transfer",
        "withdraw",
        "donate",
        "invite",
        "publish",
        "upload",
    ],
    *["reply", "accept", "agree"],
    "log off",
    "sign off",
    "restart",
    "undo",
]
SETTINGS_DENY = [
    *["無効", "強制停止", "ログイン", "サインイン"],
    *["disable", "force stop", "sign in", "log in", "login"],
]
SHOP_DENY = ["カート", "アカウント", "cart", "account"]
EXPECTED_CONFIRM = [
    *[
        "OK",
        "はい",
        "確認",
        "確定",
        "続ける",
        "続行",
        "同意",
        "許可",
        "実行",
        "適用",
        "完了",
    ],
    *[
        "yes",
        "confirm",
        "continue",
        "proceed",
        "accept",
        "agree",
        "allow",
        "apply",
        "done",
    ],
]


def test_default_word_lists_match_the_expected_lists() -> None:
    assert len(BASE_DENY) == 23 + 23
    assert len(SETTINGS_DENY) == 9
    assert len(SHOP_DENY) == 4
    assert DEFAULT_DENY_WORDS[-13:-4] == tuple(SETTINGS_DENY)
    assert DEFAULT_DENY_WORDS[-4:] == tuple(SHOP_DENY)
    assert Counter(DEFAULT_DENY_WORDS) == Counter(
        BASE_DENY + EXTRA_DENY + SETTINGS_DENY + SHOP_DENY
    )
    assert Counter(CONFIRM_WORDS) == Counter(EXPECTED_CONFIRM)


@pytest.mark.parametrize("word", BASE_DENY + EXTRA_DENY + SETTINGS_DENY + SHOP_DENY)
def test_every_deny_word_refuses_a_button(word) -> None:
    found = run([el("e2", text=word)])
    assert "deny_word" in reasons(found, "e2")


def test_clock_app_info_page_refuses_disable_and_force_stop() -> None:
    found = run(
        [
            el("e2", text="Open", bounds=(10, 100, 100, 150)),
            el("e3", text="Disable", bounds=(110, 100, 200, 150)),
            el("e4", text="Force stop", bounds=(210, 100, 300, 150)),
        ]
    )
    assert reasons(found, "e2") == ()
    assert "deny_word" in reasons(found, "e3")
    assert "deny_word" in reasons(found, "e4")


@pytest.mark.parametrize(
    "label",
    ["Sign in", "Log in", "LOGIN", "ログイン", "サインイン", "無効にする", "強制停止"],
)
def test_sign_in_and_disable_labels_are_refused(label) -> None:
    found = run([el("e2", text=label)])
    assert "deny_word" in reasons(found, "e2")


@pytest.mark.parametrize(
    ("label", "word"),
    [
        ("Designing", "sign in"),
        ("Signing out", "sign in"),
        ("Catalog in stock", "log in"),
        ("Log info", "log in"),
        ("Dialog input", "log in"),
        ("Design info", "sign in"),
        ("Sign inbox", "sign in"),
        ("ブログインポート", "ログイン"),
        ("Disabled", "disable"),
        ("Wi-Fi 無効", "無効"),
    ],
)
def test_dry_run_words_over_block_as_documented(label, word) -> None:
    assert word_matches(label, word)
    found = run([el("e2", text=label)])
    assert "deny_word" in reasons(found, "e2")


def test_disabled_status_text_does_not_match_the_japanese_word() -> None:
    assert not word_matches("Disabled", "無効")


def test_cart_and_account_buttons_are_refused_and_products_is_not() -> None:
    found = run(
        [
            el("e2", text="Add to cart", bounds=(10, 100, 100, 150)),
            el("e3", text="My account", bounds=(110, 100, 200, 150)),
            el("e4", text="カートに入れる", bounds=(210, 100, 300, 150)),
            el("e5", text="アカウント", bounds=(10, 160, 100, 210)),
            el("e6", text="Products", bounds=(110, 160, 200, 210)),
        ]
    )
    for ident in ("e2", "e3", "e4", "e5"):
        assert reasons(found, ident) == ("deny_word",)
    assert reasons(found, "e6") == ()


@pytest.mark.parametrize(
    ("label", "word"),
    [
        ("Accounts", "account"),
        ("Accounting", "account"),
        ("Cartoon", "cart"),
        ("Cartridge", "cart"),
        ("Descartes", "cart"),
        ("Music artists", "cart"),
        ("Comic art", "cart"),
        ("Electric Artwork", "cart"),
        ("Ac count", "account"),
        ("カートゥーン", "カート"),
        ("アカウント情報", "アカウント"),
    ],
)
def test_cart_and_account_words_over_block_as_documented(label, word) -> None:
    assert word_matches(label, word)
    found = run([el("e2", text=label)])
    assert "deny_word" in reasons(found, "e2")


@pytest.mark.parametrize("word", EXPECTED_CONFIRM)
def test_every_confirm_word_is_a_dialog_confirm_button(word) -> None:
    found = run([el("e2", text=word)], root_bounds=DIALOG_ROOT)
    assert "dialog_confirm_button" in reasons(found, "e2")


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
    assert REASONS[REASONS.index("toggle") + 1] == "deny_word"
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


# -- confirm labels that start with a confirm word ---------------------------------


def _restart_screen(yes: str, no: str = "Wait, cancel!", warning: str = ""):
    """A full-screen confirmation: the title, the text and two buttons."""
    text = "android.widget.TextView"
    return [
        el("e2", cls=text, flags=(), text="Restart game?", bounds=(20, 100, 300, 140)),
        el(
            "e3",
            cls=text,
            flags=(),
            text="Are you sure you want to restart?" + warning,
            bounds=(20, 150, 300, 250),
        ),
        el("e4", text=yes, bounds=(20, 300, 300, 360)),
        el("e5", text=no, bounds=(20, 380, 300, 440)),
    ]


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


@pytest.mark.parametrize("word", ["restart", "undo", "やり直", "元に戻"])
def test_the_start_over_words_are_default_deny_words(word) -> None:
    assert word in DEFAULT_DENY_WORDS


@pytest.mark.parametrize(
    "label", ["Restart", "UNDO", "Undo last edit", "もう一度やり直す", "元に戻す"]
)
def test_start_over_labels_are_refused(label) -> None:
    assert "deny_word" in reasons(run([el("e2", text=label)]), "e2")


# -- screens that warn that nothing can be undone ----------------------------------

WARNING = " You can't undo this"
TEXT_VIEW = "android.widget.TextView"


def _warning_screen(text: str, *, field: str = "text", element=None):
    """A full-screen screen with one warning element and two buttons."""
    if element is None:
        kwargs = {field: text}
        element = el(
            "e3", cls=TEXT_VIEW, flags=(), bounds=(20, 150, 300, 250), **kwargs
        )
    return [
        element,
        el("e4", text="Home", bounds=(20, 300, 300, 360)),
        el("e5", text="Back", bounds=(20, 380, 300, 440)),
    ]


def test_the_restart_screen_refuses_both_buttons() -> None:
    found = run(_restart_screen("Yes I'm sure!", warning=WARNING))
    assert reasons(found, "e4") == ("irreversible_screen", "confirm_button")
    assert reasons(found, "e5") == ("irreversible_screen",)
    detail = decision(found, "e5").details[0]
    assert detail.reason == "irreversible_screen"
    assert detail.word == "can't undo"
    assert detail.field == "text"
    assert detail.element_id == "e3"


def test_the_reason_stands_between_deny_word_and_confirm_button() -> None:
    assert REASONS.index("irreversible_screen") == REASONS.index("deny_word") + 1
    assert REASONS[REASONS.index("irreversible_screen") + 1] == "confirm_button"
    row = el("e2", text="Delete", bounds=(20, 300, 300, 360))
    found = run([*_warning_screen("It is irreversible"), row])
    assert reasons(found, "e2") == ("deny_word", "irreversible_screen")


@pytest.mark.parametrize(
    "text",
    [
        "You can't undo this",
        "You can\u2019t undo this",
        "CANNOT UNDO",
        "This can not be undone.",
        "It cannot be undone",
        "It can't be undone",
        "This is irreversible",
        "Deleted permanently",
        "This action is permanent",
        "This action is  PERMANENT!",
        "元に戻せません",
        "この操作は取り消せません。",
        "元に戻すことはできません",
        "取り消すことはできません",
        "データを完全に削除します",
        "Can\u200bnot be und\u00adone",
        "取り消すことができません",
        "元に戻すことができません",
        "This action cannot be reversed",
        "It can\u2019t be reversed",
        "Your data will be irreversibly deleted",
        "Deleted files cannot be recovered",
        "削除したデータは復元できません",
    ],
)
def test_every_phrase_refuses_every_candidate(text) -> None:
    found = run(_warning_screen(text))
    assert reasons(found, "e4") == ("irreversible_screen",)
    assert reasons(found, "e5") == ("irreversible_screen",)


@pytest.mark.parametrize("field", ["text", "desc", "hint"])
def test_the_phrase_is_read_in_text_description_and_hint(field) -> None:
    found = run(_warning_screen("This is irreversible", field=field))
    assert reasons(found, "e4") == ("irreversible_screen",)
    names = {"text": "text", "desc": "content_desc", "hint": "hint"}
    assert decision(found, "e4").details[0].field == names[field]


def test_the_phrase_on_a_clickable_element_refuses_it_too() -> None:
    own = el("e3", text="Delete permanently?", bounds=(20, 150, 300, 250))
    found = run(_warning_screen("", element=own))
    assert "irreversible_screen" in reasons(found, "e3")
    assert reasons(found, "e5") == ("irreversible_screen",)


def test_the_resource_id_is_not_read_for_phrases() -> None:
    named = el(
        "e3",
        cls=TEXT_VIEW,
        flags=(),
        rid="com.example:id/cannot_be_undone",
        bounds=(20, 150, 300, 250),
    )
    found = run(_warning_screen("", element=named))
    assert reasons(found, "e4") == ()


def test_the_first_element_in_document_order_is_named() -> None:
    first = el("e3", cls=TEXT_VIEW, flags=(), text="Irreversible", bounds=(0, 0, 9, 9))
    second = el("e6", cls=TEXT_VIEW, flags=(), text="Permanently", bounds=(0, 0, 9, 9))
    found = run([*_warning_screen("", element=first), second])
    detail = decision(found, "e4").details[0]
    assert (detail.word, detail.element_id) == ("irreversible", "e3")


@pytest.mark.parametrize(
    "text",
    ["Undo", "Can I undo?", "Permanent link", "Reverse the order", "Cancel", ""],
)
def test_a_screen_without_a_phrase_is_unchanged(text) -> None:
    on = run(_warning_screen(text))
    off = run(_warning_screen(text), policy=Policy(skip_irreversible=False))
    assert [d.reasons for d in on.decisions] == [d.reasons for d in off.decisions]
    assert all("irreversible_screen" not in d.reasons for d in on.decisions)


def test_the_switch_off_restores_the_old_decisions() -> None:
    off = Policy(skip_irreversible=False)
    found = run(_restart_screen("Yes I'm sure!", warning=WARNING), policy=off)
    assert reasons(found, "e4") == ("confirm_button",)
    assert reasons(found, "e5") == ()


def test_custom_phrases_replace_the_defaults() -> None:
    policy = Policy(irreversible_phrases=("no way back",))
    assert reasons(run(_warning_screen("No way back"), policy=policy), "e4") == (
        "irreversible_screen",
    )
    assert reasons(run(_warning_screen("Irreversible"), policy=policy), "e4") == ()
    empty = Policy(irreversible_phrases=())
    assert reasons(run(_warning_screen("Irreversible"), policy=empty), "e4") == ()


def test_an_empty_phrase_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        Policy(irreversible_phrases=("  ",))


def test_with_deny_words_keeps_the_phrases_and_the_switch() -> None:
    policy = Policy(irreversible_phrases=("x y",), skip_irreversible=False)
    copy = policy.with_deny_words(["rent"])
    assert copy.irreversible_phrases == ("x y",)
    assert copy.skip_irreversible is False


def test_the_other_screens_candidates_all_get_the_reason_when_not_visible() -> None:
    hidden = el("e6", text="Gone", bounds=(400, 0, 500, 30))
    found = run([*_warning_screen("Irreversible"), hidden])
    assert "irreversible_screen" in reasons(found, "e6")


def test_the_default_phrases_are_unique_and_documented_ones() -> None:
    assert len(set(guards.IRREVERSIBLE_PHRASES)) == len(guards.IRREVERSIBLE_PHRASES)
    assert len(guards.IRREVERSIBLE_PHRASES) == 20
    keys = {guards._phrase_key(phrase) for phrase in guards.IRREVERSIBLE_PHRASES}
    assert len(keys) == len(guards.IRREVERSIBLE_PHRASES)
    assert Policy().irreversible_phrases == guards.IRREVERSIBLE_PHRASES


def test_the_rule_only_adds_the_reason_on_random_screens() -> None:
    rng = random.Random(20261120)  # noqa: S311
    on = Policy(avoid=())
    off = Policy(avoid=(), skip_irreversible=False)
    warned = 0
    for _ in range(300):
        base, _denies = _random_screen(rng)
        elements = list(base.elements)
        if rng.random() < 0.5:
            at = rng.randrange(len(elements))
            elements[at] = replace(
                elements[at], text=rng.choice(guards.IRREVERSIBLE_PHRASES)
            )
        screen = make_screen(elements)
        after = judge(screen, PKG, on, foreground=PKG)
        before = judge(screen, PKG, off, foreground=PKG)
        for new, old in zip(after.decisions, before.decisions, strict=True):
            kept = tuple(r for r in new.reasons if r != "irreversible_screen")
            assert kept == old.reasons
            if "irreversible_screen" in new.reasons:
                warned += 1
                assert new.reasons
    assert warned > 100


def test_screens_without_phrases_get_the_old_decisions_on_random_screens() -> None:
    rng = random.Random(20261121)  # noqa: S311
    off = Policy(skip_irreversible=False)
    for _ in range(300):
        screen, _denies = _random_screen(rng)
        texts = " ".join(
            f"{e.text} {e.content_desc} {e.hint}" for e in screen.elements
        ).casefold()
        if any(phrase in texts for phrase in guards.IRREVERSIBLE_PHRASES):
            continue
        assert judge(screen, PKG, Policy(), foreground=PKG) == judge(
            screen, PKG, off, foreground=PKG
        )


def test_a_clickable_over_thousands_of_mark_only_texts_is_judged_quickly() -> None:
    marks = [
        el(
            f"m{n}",
            parent="e2",
            text="!",
            cls="android.widget.TextView",
            flags=(),
            bounds=(60, 110, 70, 120),
        )
        for n in range(3000)
    ]
    row = el("e2", cls="android.widget.LinearLayout", bounds=(50, 100, 250, 150))
    first = el("a", parent="e2", text="a", cls="android.widget.TextView", flags=())
    last = el("b", parent="e2", text="b", cls="android.widget.TextView", flags=())
    start = time.perf_counter()
    judgement = run([row, first, *marks, last], root_bounds=(20, 200, 300, 400))
    assert time.perf_counter() - start < 5
    assert "confirm_button" not in reasons(judgement, "e2")


@pytest.mark.parametrize(
    ("words", "label", "counts"),
    [
        # Japanese: the word needs no boundary, whatever letter ends it.
        (("\u30aa\u30fc\u30b1\u30fc",), "\u30aa\u30fc\u30b1\u30fc", True),
        (("\u30aa\u30fc\u30b1\u30fc",), "\u30aa\u30fc\u30b1\u30fc\u3067\u3059", True),
        (("\u306f\u3044",), "\u306f\u3044\u3069\u3046\u305e", True),
        (("\u78ba\u8a8d",), "\u78ba\u8a8d\u3057\u307e\u3059", True),
        (("\u4eba\u3005",), "\u4eba\u3005\u306e", True),
        (("\u3044\u3059\u309e",), "\u3044\u3059\u309e\u3067\u3059", True),
        (("\uff75\uff70\uff79\uff70",), "\u30aa\u30fc\u30b1\u30fc\u3067\u3059", True),
        # Every other script keeps the boundary.
        (("\u05d0\u05d1",), "\u05d0\u05d1\u05d9\u05d1", False),
        (("\u05d0\u05d1",), "\u05d0\u05d1, \u05db\u05df", True),
        (("\uc608",), "\uc608\uc57d", False),
        (("\uc608",), "\uc608, \uacc4\uc18d", True),
        (("\ud655\uc778",), "\ud655\uc778\ucde8\uc18c", False),
        (("ok",), "OK\u3067\u3059", False),
        (("ok",), "OKAY", False),
    ],
)
def test_only_a_word_ending_in_a_japanese_letter_is_open_ended(
    words, label, counts
) -> None:
    policy = Policy(confirm_words=words)
    judgement = run(
        [el("e2", text=label)], policy=policy, root_bounds=(20, 200, 300, 400)
    )
    assert ("dialog_confirm_button" in reasons(judgement, "e2")) is counts


@pytest.mark.parametrize(
    ("char", "japanese"),
    [
        ("\u3042", True),
        ("\u30fc", True),
        ("\u3005", True),
        ("\u309d", True),
        ("\u30fd", True),
        ("\u6f22", True),
        ("\uff71", True),
        ("\U00020b9f", True),
        ("\u30fb", False),
        ("\u3001", False),
        ("\u3099", False),
        ("\u05d0", False),
        ("\uac00", False),
        ("a", False),
        ("1", False),
    ],
)
def test_the_japanese_letter_predicate(char, japanese) -> None:
    assert guards._is_japanese_letter(char) is japanese


def _split_label_row(field: str, parts: tuple[str, ...]):
    row = el("e2", cls=LAYOUT, bounds=(50, 300, 250, 340))
    kids = [
        el(f"k{n}", parent="e2", cls=TEXT_VIEW, flags=(), **{field: part})
        for n, part in enumerate(parts)
    ]
    return [row, *kids]


@pytest.mark.parametrize("field", ["text", "desc"])
@pytest.mark.parametrize(
    "parts",
    [
        ("\u78ba", "\u8a8d\u3057\u3066\u304f\u3060\u3055\u3044"),
        ("\u306f", "\u3044"),
        ("\u7d9a", "\u3051", "\u308b"),
        ("\u78ba", "\u8a8d"),
    ],
)
def test_a_japanese_confirm_word_split_over_children_is_found(field, parts) -> None:
    found = run(_split_label_row(field, parts), root_bounds=(20, 200, 300, 400))
    assert "dialog_confirm_button" in reasons(found, "e2")


@pytest.mark.parametrize("field", ["text", "desc"])
def test_latin_parts_are_still_joined_with_a_space(field) -> None:
    found = run(_split_label_row(field, ("O", "K")), root_bounds=(20, 200, 300, 400))
    assert "dialog_confirm_button" not in reasons(found, "e2")
    found = run(
        _split_label_row(field, ("OK", "Cancel")), root_bounds=(20, 200, 300, 400)
    )
    assert "dialog_confirm_button" in reasons(found, "e2")


def test_a_half_width_voiced_mark_is_not_joined_to_the_kana_before_it() -> None:
    # NFKC would merge the two into one letter; the join keeps a space there.
    policy = Policy(confirm_words=("\u30ab",))
    found = run(
        _split_label_row("text", ("\uff76", "\uff9e")),
        policy=policy,
        root_bounds=(20, 200, 300, 400),
    )
    assert "dialog_confirm_button" in reasons(found, "e2")


def _nested_rows(count: int, text: str):
    """A chain of nested clickable rows, each with one text child."""
    rows = [el("c0", cls=LAYOUT, bounds=(50, 100, 250, 150))]
    for n in range(1, count):
        rows.append(
            el(f"c{n}", parent=f"c{n - 1}", cls=LAYOUT, bounds=(50, 100, 250, 150))
        )
        rows.append(
            el(
                f"t{n}",
                parent=f"c{n - 1}",
                cls=TEXT_VIEW,
                flags=(),
                text=text,
                bounds=(50, 100, 250, 150),
            )
        )
    return rows


def test_long_child_texts_are_judged_quickly() -> None:
    # Every row reads only the start of its descendants' texts. Reading all
    # of them (about 110 000 characters for each of the 1000 rows) took about
    # 15 seconds.
    rows = _nested_rows(1000, "lorem ipsum dolor sit amet " * 4)
    start = time.perf_counter()
    judgement = run(rows, root_bounds=(20, 50, 300, 400))
    assert time.perf_counter() - start < 5
    assert all("dialog_confirm_button" not in d.reasons for d in judgement.decisions)
    one_long = _nested_rows(3, "lorem ipsum " * 5000)
    start = time.perf_counter()
    run(one_long, root_bounds=(20, 50, 300, 400))
    assert time.perf_counter() - start < 5


def _all_joined_forms(self, values):
    """`_Judge._joined_forms` without the early stop: every value is read."""
    parts = [guards._clean(value) for value in values]
    parts = [part for part in parts if part]
    spaced = " ".join(parts)
    tight = parts[0] if parts else ""
    for before, part in itertools.pairwise(parts):
        join = (
            guards._is_japanese_letter(before[-1])
            and guards._is_japanese_letter(part[0])
            and part[0] not in "\uff9e\uff9f"
        )
        tight += ("" if join else " ") + part
    del self
    return guards._confirm_form(spaced), guards._confirm_form(tight)


def test_the_early_stop_gives_the_same_decisions(monkeypatch) -> None:
    rng = random.Random(20261119)  # noqa: S311
    pieces = [
        "OK",
        "ok",
        "Yes",
        "\u306f",
        "\u3044",
        "\u78ba",
        "\u8a8d",
        "!",
        " ",
        "-",
        "x",
        "Continue",
        "\uff76",
        "\uff9e",
        "e\u0301",
        "\u0301",
        "lorem ipsum dolor",
        "!!!!!!!!!!",
        "( - ) ( - ) ( - )",
        "Continued",
        "OK OK OK OK OK",
    ]
    marks = "!!!!!!!!!! ( - ) ( - ) ( - )"
    fixed = [
        (marks, "\u78ba", "\u8a8d"),
        (marks, "\u306f", "\u3044\u3069\u3046\u305e"),
        (marks + " \u78ba", "\u8a8d"),
        (marks, "O", "K"),
        (marks, "ok", "!", "x"),
        (marks, "\uff76", "\uff9e"),
        ("x" * 40, "\u78ba", "\u8a8d"),
    ]
    screens = [
        [
            el("e2", cls=LAYOUT, bounds=(50, 300, 250, 340)),
            *(
                el(f"k{n}", parent="e2", cls=TEXT_VIEW, flags=(), text=label)
                for n, label in enumerate(labels)
            ),
        ]
        for labels in fixed
    ]
    for _ in range(300):
        rows = [el("e2", cls=LAYOUT, bounds=(50, 300, 250, 340))]
        for n in range(rng.randint(2, 8)):
            label = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 4)))
            field = rng.choice(["text", "desc"])
            rows.append(
                el(f"k{n}", parent="e2", cls=TEXT_VIEW, flags=(), **{field: label})
            )
        screens.append(rows)
    policy = Policy(confirm_words=(*guards.CONFIRM_WORDS, "e\u0301", "\u30ac"))
    fast = [run(rows, policy=policy, root_bounds=DIALOG_ROOT) for rows in screens]
    monkeypatch.setattr(guards._Judge, "_joined_forms", _all_joined_forms)
    full = [run(rows, policy=policy, root_bounds=DIALOG_ROOT) for rows in screens]
    assert fast == full


@pytest.mark.parametrize("label", ["Undoubtedly", "Undock", "Restarted", "Undone"])
def test_start_over_words_refuse_longer_words_that_contain_them(label) -> None:
    assert "deny_word" in reasons(run([el("e2", text=label)]), "e2")


def test_text_output_names_the_phrase_that_refused_a_screen() -> None:
    from uiprobe.cli import _reasons_text

    warning = el(
        "e3",
        text="Saved permanently",
        cls="android.widget.TextView",
        flags=(),
        bounds=(50, 300, 250, 350),
    )
    judgement = run([el("e2", text="Cancel"), warning])
    assert _reasons_text(decision(judgement, "e2")) == (
        'irreversible_screen ("permanently" in text of e3)'
    )
