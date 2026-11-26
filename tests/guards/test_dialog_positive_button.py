"""Tests for the dialog's positive button and the confirm words added in version 9."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.guards.helpers import PKG, decision, el, reasons, run
from uiprobe.guards import Policy
from uiprobe.guards.constants import CONFIRM_WORDS

POSITIVE = "android:id/button1"
NEGATIVE = "android:id/button2"
NEUTRAL = "android:id/button3"
DIALOG = (20, 200, 300, 440)
PLAIN = (0, 0, 320, 640)
BOTTOM = (150, 340, 280, 390)
REFUSED = ("dialog_confirm_button",)
NEW_WORDS = ("turn on", "enable", "オンにする", "有効にする")


@pytest.mark.parametrize(
    "label", ["ON", "Turn on", "Enable", "Proceed now", "Go", "オンにする", "Cancel"]
)
def test_the_positive_button_of_a_dialog_is_refused_whatever_its_label(label) -> None:
    button = el("e2", text=label, rid=POSITIVE, bounds=BOTTOM)
    assert reasons(run([button], root_bounds=DIALOG), "e2") == REFUSED


@pytest.mark.parametrize("label", ["ON", "Turn on", "Enable", "Proceed now", "Go"])
def test_the_same_labels_outside_a_dialog_are_unchanged(label) -> None:
    button = el("e2", text=label, rid=POSITIVE, bounds=BOTTOM)
    assert reasons(run([button], root_bounds=PLAIN), "e2") == ()


def test_the_detail_says_it_is_the_positive_button() -> None:
    button = el("e2", text="Go", rid=POSITIVE, bounds=BOTTOM)
    detail = decision(run([button], root_bounds=DIALOG), "e2").details[0]
    assert detail.reason == "dialog_confirm_button"
    assert "positive button" in detail.note
    assert POSITIVE in detail.note


def test_a_confirm_word_on_the_positive_button_gives_one_reason() -> None:
    button = el("e2", text="OK", rid=POSITIVE, bounds=BOTTOM)
    found = decision(run([button], root_bounds=DIALOG), "e2")
    assert found.reasons == REFUSED
    assert len(found.details) == 2


def test_the_negative_and_neutral_buttons_stay_pressable() -> None:
    cancel = el("e2", text="Cancel", rid=NEGATIVE, bounds=(40, 340, 140, 390))
    later = el("e3", text="Later", rid=NEUTRAL, bounds=(40, 300, 140, 330))
    judgement = run([cancel, later], root_bounds=DIALOG)
    assert reasons(judgement, "e2") == ()
    assert reasons(judgement, "e3") == ()


def test_the_positive_button_alone_is_refused_in_a_dialog_with_both() -> None:
    turn_on = el("e2", text="Turn on", rid=POSITIVE, bounds=(160, 340, 280, 390))
    cancel = el("e3", text="Cancel", rid=NEGATIVE, bounds=(40, 340, 150, 390))
    judgement = run([turn_on, cancel], root_bounds=DIALOG)
    assert reasons(judgement, "e2") == REFUSED
    assert reasons(judgement, "e3") == ()


def test_anything_inside_the_positive_button_is_refused() -> None:
    box = el("e2", rid=POSITIVE, cls="android.widget.LinearLayout", bounds=BOTTOM)
    label = el(
        "e3",
        parent="e2",
        text="Go",
        cls="android.widget.TextView",
        bounds=(160, 345, 270, 385),
    )
    judgement = run([box, label], root_bounds=DIALOG)
    assert reasons(judgement, "e2") == REFUSED
    assert reasons(judgement, "e3") == REFUSED


@pytest.mark.parametrize("rid", ["android:id/button1x", "app:id/button1", "button1"])
def test_only_the_exact_platform_id_counts(rid) -> None:
    button = el("e2", text="Go", rid=rid, bounds=BOTTOM)
    assert reasons(run([button], root_bounds=DIALOG), "e2") == ()


@pytest.mark.parametrize("word", NEW_WORDS)
def test_the_new_words_are_default_confirm_words(word) -> None:
    assert word in CONFIRM_WORDS


@pytest.mark.parametrize("label", ["Turn on", "Enable", "オンにする", "有効にする"])
def test_a_new_word_is_refused_on_a_dialog_without_the_id(label) -> None:
    button = el("e2", text=label, bounds=BOTTOM)
    assert reasons(run([button], root_bounds=DIALOG), "e2") == REFUSED
    assert reasons(run([button], root_bounds=PLAIN), "e2") == ()


def test_refuse_dialog_confirm_false_turns_both_off() -> None:
    by_id = el("e2", text="Go", rid=POSITIVE, bounds=BOTTOM)
    by_word = el("e3", text="Turn on", bounds=(40, 300, 140, 330))
    on = run([by_id, by_word], root_bounds=DIALOG)
    assert reasons(on, "e2") == REFUSED
    assert reasons(on, "e3") == REFUSED
    policy = replace(Policy(), refuse_dialog_confirm=False)
    off = run([by_id, by_word], root_bounds=DIALOG, policy=policy)
    assert reasons(off, "e2") == ()
    assert reasons(off, "e3") == ()


def _container_over_button(*, linked: bool = True):
    container = el("e2", cls="android.widget.LinearLayout", bounds=(30, 210, 290, 430))
    button = el(
        "e3",
        rid=POSITIVE,
        cls="android.widget.LinearLayout",
        flags=(),
        bounds=(100, 300, 220, 340),
    )
    label = el(
        "e4",
        parent="e3" if linked else "e99",
        text="ON",
        cls="android.widget.TextView",
        flags=(),
        bounds=(120, 305, 200, 335),
    )
    return [container, button, label]


@pytest.mark.parametrize("linked", [True, False])
def test_a_container_whose_press_point_lands_on_the_positive_button(linked) -> None:
    elements = _container_over_button(linked=linked)
    judgement = run(elements, root_bounds=DIALOG)
    assert REFUSED[0] in reasons(judgement, "e2")
    off = replace(Policy(), refuse_dialog_confirm=False)
    assert REFUSED[0] not in reasons(
        run(elements, root_bounds=DIALOG, policy=off), "e2"
    )
    assert REFUSED[0] not in reasons(run(elements, root_bounds=PLAIN), "e2")


def test_a_container_away_from_the_positive_button_stays_pressable() -> None:
    container = el(
        "e2",
        text="Panel",
        cls="android.widget.LinearLayout",
        bounds=(30, 210, 290, 290),
    )
    button = el("e3", rid=POSITIVE, text="Go", bounds=BOTTOM)
    judgement = run([container, button], root_bounds=DIALOG)
    assert reasons(judgement, "e2") == ()
    assert reasons(judgement, "e3") == REFUSED


@pytest.mark.parametrize(
    ("rid", "refused"),
    [
        ("android:id/button1", True),
        (f"{PKG}:id/button1", True),
        ("com.other.app:id/button1", False),
        ("app:id/button1x", False),
        (f"{PKG}:id/button1x", False),
        ("android:id/button10", False),
        ("android:id/Button1", False),
        (f"{PKG}:xml/button1", False),
    ],
)
def test_the_positive_id_is_button1_under_android_or_the_screen_package(
    rid, refused
) -> None:
    button = el("e2", text="Go", rid=rid, bounds=BOTTOM)
    expected = REFUSED if refused else ()
    assert reasons(run([button], root_bounds=DIALOG), "e2") == expected


def test_the_new_words_are_dialog_only_in_the_policy_and_the_constant() -> None:
    from uiprobe.guards.constants import DIALOG_ONLY_CONFIRM_WORDS

    assert DIALOG_ONLY_CONFIRM_WORDS == NEW_WORDS


def _enable_with_warning(root_bounds, policy=None):
    warning = el(
        "e3",
        text="Forget saved device?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(50, 200, 250, 240),
    )
    button = el("e2", text="Enable", bounds=(50, 300, 250, 340))
    return run([button, warning], root_bounds=root_bounds, policy=policy)


def test_a_new_word_does_not_make_the_screen_wide_confirm_button() -> None:
    plain = _enable_with_warning(PLAIN)
    assert reasons(plain, "e2") == ()
    ok = el("e2", text="OK", bounds=(50, 300, 250, 340))
    warning = el(
        "e3",
        text="Forget saved device?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(50, 200, 250, 240),
    )
    assert reasons(run([ok, warning], root_bounds=PLAIN), "e2") == ("confirm_button",)


def test_a_new_word_on_a_dialog_with_a_deny_word_gives_only_the_dialog_reason() -> None:
    assert reasons(_enable_with_warning((20, 150, 300, 440)), "e2") == REFUSED
    off = replace(Policy(), refuse_dialog_confirm=False)
    assert reasons(_enable_with_warning((20, 150, 300, 440), off), "e2") == ()
    assert reasons(_enable_with_warning(PLAIN, off), "e2") == ()


@pytest.mark.parametrize("label", ["有効にする場合", "オンにするか確認", "有効にする"])
def test_a_japanese_new_word_needs_no_boundary_like_the_existing_ones(label) -> None:
    button = el("e2", text=label, bounds=BOTTOM)
    assert reasons(run([button], root_bounds=DIALOG), "e2") == REFUSED
    assert reasons(run([button], root_bounds=PLAIN), "e2") == ()
    # The existing word behaves the same way (docs/crawl.md).
    existing = el("e2", text="確認画面", bounds=BOTTOM)
    assert reasons(run([existing], root_bounds=DIALOG), "e2") == REFUSED


@pytest.mark.parametrize("label", ["Enabled", "Enablement", "Turn online"])
def test_a_latin_new_word_needs_a_boundary(label) -> None:
    button = el("e2", text=label, bounds=BOTTOM)
    assert reasons(run([button], root_bounds=DIALOG), "e2") == ()
