"""Tests for the dialog's positive button and the confirm words added in version 9."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.guards.helpers import decision, el, reasons, run
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
    policy = replace(Policy(), refuse_dialog_confirm=False)
    by_id = el("e2", text="Go", rid=POSITIVE, bounds=BOTTOM)
    by_word = el("e3", text="Turn on", bounds=(40, 300, 140, 330))
    judgement = run([by_id, by_word], root_bounds=DIALOG, policy=policy)
    assert reasons(judgement, "e2") == ()
    assert reasons(judgement, "e3") == ()
