"""Tests for screens that warn that nothing can be undone."""

from __future__ import annotations

import random
import time
from dataclasses import replace

import pytest

from tests.guards.helpers import (
    LAYOUT,
    PKG,
    TEXT_VIEW,
    _random_screen,
    _restart_screen,
    decision,
    el,
    make_screen,
    reasons,
    run,
)
from uiprobe import guards
from uiprobe.guards import (
    REASONS,
    Policy,
    judge,
)

# -- screens that warn that nothing can be undone ----------------------------------

WARNING = " You can't undo this"


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
        ("\U0001aff0", False),  # kana extended-B is Taiwanese, not Japanese
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
