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


# -- the new words never take a refusal away (main's matching is kept) ---------------

LAYOUT = "android.widget.LinearLayout"
WARNING = ("e9", "Forget saved device?")
# The policy that stands for main: the same confirm words without the two new ones.
MAIN = replace(
    Policy(),
    confirm_words=tuple(w for w in CONFIRM_WORDS if w not in DIALOG_ONLY_EXACT_LABELS),
)


def _warning():
    return el(
        WARNING[0], text=WARNING[1], cls=TEXT, flags=(), bounds=(50, 160, 250, 190)
    )


def _on_inside_ok():
    parent = el("e2", text="OK", cls=LAYOUT, bounds=(40, 300, 280, 400))
    child = el("e3", parent="e2", text="ON", bounds=(100, 320, 200, 380))
    return [parent, child, _warning()]


@pytest.mark.parametrize("root_bounds", [PLAIN, DIALOG])
@pytest.mark.parametrize("refuse_dialog", [True, False])
def test_a_clickable_on_inside_a_clickable_ok_keeps_confirm_button(
    root_bounds, refuse_dialog
) -> None:
    kept = replace(Policy(), refuse_dialog_confirm=refuse_dialog)
    main = replace(MAIN, refuse_dialog_confirm=refuse_dialog)
    now = run(_on_inside_ok(), root_bounds=root_bounds, policy=kept)
    before = run(_on_inside_ok(), root_bounds=root_bounds, policy=main)
    for ident in ("e2", "e3"):
        assert set(reasons(before, ident)) <= set(reasons(now, ident))
    assert "confirm_button" in reasons(now, "e2")
    assert "confirm_button" in reasons(now, "e3")


def test_the_on_child_alone_gives_no_screen_wide_reason() -> None:
    now = run([_button("ON"), _warning()], root_bounds=PLAIN)
    assert reasons(now, "e2") == ()


_POOL = (
    "OK", "ON", "On", "オン", "Online", "オンライン", "Turn on", "Done", "Go",
    "Forget saved device?", "ライン", "", "Yes, on", "On-screen",
)  # fmt: skip


def _random_screen(rng):
    elements = []
    for n in range(2, rng.randint(4, 9)):
        parent = f"e{rng.randint(1, n - 1)}" if n > 2 and rng.random() < 0.5 else "e1"
        left = rng.randint(0, 200)
        top = rng.randint(0, 500)
        elements.append(
            el(
                f"e{n}",
                parent=parent,
                text=rng.choice(_POOL),
                desc=rng.choice(_POOL) if rng.random() < 0.2 else "",
                cls=rng.choice([TEXT, LAYOUT, "android.widget.Button"]),
                flags=rng.choice([(), ("clickable",)]),
                bounds=(left, top, left + rng.randint(20, 100), top + 40),
            )
        )
    return elements


def test_every_reason_of_main_is_still_given_on_random_screens() -> None:
    import random

    rng = random.Random(20261127)  # noqa: S311
    for _ in range(300):
        elements = _random_screen(rng)
        for root_bounds in (PLAIN, DIALOG):
            for refuse in (True, False):
                now = run(
                    elements,
                    root_bounds=root_bounds,
                    policy=replace(Policy(), refuse_dialog_confirm=refuse),
                )
                before = run(
                    elements,
                    root_bounds=root_bounds,
                    policy=replace(MAIN, refuse_dialog_confirm=refuse),
                )
                for old, new in zip(before.decisions, now.decisions, strict=True):
                    assert old.element.id == new.element.id
                    assert set(old.reasons) <= set(new.reasons)


# -- the assembled label ---------------------------------------------------------------


def _split_label(first: str, second: str):
    box = el("e2", cls=LAYOUT, bounds=BOTTOM)
    one = el("e3", parent="e2", text=first, cls=TEXT, flags=(), bounds=BOTTOM)
    two = el("e4", parent="e2", text=second, cls=TEXT, flags=(), bounds=BOTTOM)
    return [box, one, two]


def test_on_and_a_second_part_in_two_children_is_not_just_on() -> None:
    now = run(_split_label("オン", "ライン"), root_bounds=DIALOG)
    assert reasons(now, "e2") == ()
    now = run(_split_label("On", "line"), root_bounds=DIALOG)
    assert reasons(now, "e2") == ()


def test_an_assembled_label_of_just_on_is_refused() -> None:
    now = run(_split_label("オ", "ン"), root_bounds=DIALOG)
    assert reasons(now, "e2") == REFUSED
    assert reasons(run(_split_label("オ", "ン"), root_bounds=PLAIN), "e2") == ()


# -- a word the user wrote in an older file keeps the plain matching -----------------


@pytest.mark.parametrize("word", ["on", "オン"])
def test_a_legacy_word_keeps_the_prefix_and_screen_wide_matching(word) -> None:
    legacy = replace(Policy(), legacy_confirm_words=(word,))
    label = "On, continue" if word == "on" else "オンライン"
    button = _button(label)
    for root_bounds in (PLAIN, DIALOG):
        now = run([button, _warning()], root_bounds=root_bounds, policy=legacy)
        assert "confirm_button" in reasons(now, "e2")
        plain = run([button, _warning()], root_bounds=root_bounds)
        assert "confirm_button" not in reasons(plain, "e2")


def test_a_legacy_word_also_gets_the_exact_dialog_refusal() -> None:
    legacy = replace(Policy(), legacy_confirm_words=("on",))
    assert reasons(run([_button("ON")], root_bounds=DIALOG, policy=legacy), "e2") == (
        REFUSED
    )


def test_a_legacy_word_that_is_not_in_the_confirm_words_does_nothing() -> None:
    words = tuple(w for w in CONFIRM_WORDS if w != "on")
    policy = replace(Policy(), confirm_words=words, legacy_confirm_words=("on",))
    assert (
        reasons(run([_button("On, go")], root_bounds=DIALOG, policy=policy), "e2") == ()
    )
