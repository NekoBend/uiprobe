"""Tests for the confirm labels "on" and "オン" (defaults version 12).

They count only as the whole label of a button, and only on a dialog.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.guards.helpers import el, reasons, run
from uiprobe.guards import Policy
from uiprobe.guards.constants import (
    CONFIRM_WORDS,
    DIALOG_ONLY_EXACT_LABELS,
)

DIALOG = (20, 200, 300, 440)
PLAIN = (0, 0, 320, 640)
BOTTOM = (150, 340, 280, 390)
REFUSED = ("dialog_confirm_button",)
TEXT = "android.widget.TextView"


def _button(label: str, **fields):
    return el("e2", text=label, bounds=BOTTOM, **fields)


def test_the_exact_labels_are_default_confirm_words() -> None:
    assert DIALOG_ONLY_EXACT_LABELS == ("on", "オン")
    for word in DIALOG_ONLY_EXACT_LABELS:
        assert word in CONFIRM_WORDS


@pytest.mark.parametrize(
    "label",
    [
        "ON",
        "on",
        "On",
        "On ",
        "  on  ",
        "\uff2f\uff2e",
        "\uff4f\uff4e",
        "オン",
        "\uff75\uff9d",
    ],
)
def test_a_dialog_button_labelled_just_on_is_refused(label) -> None:
    assert reasons(run([_button(label)], root_bounds=DIALOG), "e2") == REFUSED


@pytest.mark.parametrize("field", ["text", "desc", "hint"])
def test_every_label_source_counts(field) -> None:
    button = el("e2", bounds=BOTTOM, **{field: "ON"})
    assert reasons(run([button], root_bounds=DIALOG), "e2") == REFUSED


def test_a_label_in_a_child_counts() -> None:
    box = el("e2", cls="android.widget.LinearLayout", bounds=BOTTOM)
    label = el("e3", parent="e2", text="ON", cls=TEXT, flags=(), bounds=BOTTOM)
    assert reasons(run([box, label], root_bounds=DIALOG), "e2") == REFUSED


@pytest.mark.parametrize("label", ["On", "ON", "オン"])
def test_a_full_screen_row_labelled_on_is_unaffected(label) -> None:
    assert reasons(run([_button(label)], root_bounds=PLAIN), "e2") == ()


def test_a_full_screen_row_with_a_summary_on_is_unaffected() -> None:
    row = el("e2", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 180))
    title = el(
        "e3", parent="e2", text="Wi-Fi", cls=TEXT, flags=(), bounds=(0, 100, 320, 140)
    )
    summary = el(
        "e4", parent="e2", text="On", cls=TEXT, flags=(), bounds=(0, 140, 320, 180)
    )
    judgement = run([row, title, summary], root_bounds=PLAIN)
    assert reasons(judgement, "e2") == ()


@pytest.mark.parametrize(
    "label",
    [
        "Online",
        "Only",
        "Location",
        "Onboarding",
        "On-screen",
        "On notifications",
        "On or off",
        "オンライン",
        "ログオン",
        "オンです",
    ],
)
def test_a_longer_label_is_unaffected_on_a_dialog(label) -> None:
    assert reasons(run([_button(label)], root_bounds=DIALOG), "e2") == ()


def test_done_is_still_refused_as_before() -> None:
    # "done" is an existing confirm word, so the whole word still counts.
    assert reasons(run([_button("Done")], root_bounds=DIALOG), "e2") == REFUSED
    assert reasons(run([_button("Done")], root_bounds=PLAIN), "e2") == ()


@pytest.mark.parametrize("label", ["Turn on", "Turn on Wi-Fi", "turn on notifications"])
def test_turn_on_is_covered_by_its_own_word_as_before(label) -> None:
    assert reasons(run([_button(label)], root_bounds=DIALOG), "e2") == REFUSED
    assert reasons(run([_button(label)], root_bounds=PLAIN), "e2") == ()


def test_refuse_dialog_confirm_false_turns_it_off() -> None:
    policy = replace(Policy(), refuse_dialog_confirm=False)
    judgement = run([_button("ON")], root_bounds=DIALOG, policy=policy)
    assert reasons(judgement, "e2") == ()


def test_a_policy_without_the_words_does_not_refuse() -> None:
    words = tuple(w for w in CONFIRM_WORDS if w not in DIALOG_ONLY_EXACT_LABELS)
    policy = replace(Policy(), confirm_words=words)
    assert reasons(run([_button("ON")], root_bounds=DIALOG, policy=policy), "e2") == ()


def test_the_exact_label_never_gives_the_screen_wide_reason() -> None:
    warning = el(
        "e3",
        text="Forget saved device?",
        cls=TEXT,
        flags=(),
        bounds=(50, 200, 250, 240),
    )
    judgement = run([_button("ON"), warning], root_bounds=PLAIN)
    assert reasons(judgement, "e2") == ()
    judgement = run([_button("ON"), warning], root_bounds=DIALOG)
    assert reasons(judgement, "e2") == REFUSED
