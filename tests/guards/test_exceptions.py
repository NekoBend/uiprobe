"""Tests for deny exception words."""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.guards.helpers import WIFI_SUMMARY, decision, deny_of, el, reasons, run
from uiprobe import guards
from uiprobe.guards import (
    DEFAULT_DENY_EXCEPTIONS,
    DEFAULT_DENY_WORDS,
    Policy,
    deny_word_in,
    word_matches,
)

# The exception words of these tests (there are no built-in ones): words that
# hold a deny word, as a settings file might list them.
EXC = ("automatically", "posture", "recorder", "border", "descartes", "designing")


EXC_POLICY = Policy(deny_exceptions=EXC)


def run_x(elements, *, policy=None, **kwargs):
    """`run` with the test exception words unless a policy is given."""
    return run(elements, policy=policy or EXC_POLICY, **kwargs)


def test_there_are_no_builtin_exception_words() -> None:
    # The defaults decide exactly as substring matching alone.
    assert DEFAULT_DENY_EXCEPTIONS == ()
    assert Policy().deny_exceptions == ()
    assert not guards._BUILTIN_EXCEPTION_KEYS
    for label in ("Automatically", "Posture", "Recorder", "Border", "Designing"):
        assert reasons(run([el("e2", text=label)]), "e2") == ("deny_word",)
    # Each test exception holds a deny word; otherwise it would excuse nothing.
    for exception in EXC:
        assert any(word_matches(exception, w) for w in DEFAULT_DENY_WORDS)


@pytest.fixture
def builtin_exc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat the test exception words as built-in ones (there are none)."""
    keys = frozenset(guards._exception_key(word) for word in EXC)
    monkeypatch.setattr(guards.matching, "_BUILTIN_EXCEPTION_KEYS", keys)


@pytest.mark.parametrize(
    "label",
    [
        "Automatically",
        WIFI_SUMMARY,
        "Posture check",
        "Recorder",
        "Border",
        "Descartes",
        "Designing",
        "AUTOMATICALLY",
        "(automatically)",
    ],
)
def test_a_deny_word_inside_an_exception_word_does_not_refuse(label) -> None:
    assert reasons(run_x([el("e2", text=label)]), "e2") == ()
    for field in ("desc", "hint"):
        found = run_x([el("e2", **{field: label})])
        assert reasons(found, "e2") == (), field


@pytest.mark.parametrize(
    ("label", "word"),
    [
        ("Automatically pay", "pay"),
        ("Automatically send", "send"),
        ("Delete recorder", "delete"),
        ("Call automatically", "call"),
        ("Automatically call back", "call"),
        ("Send recorder file", "send"),
        ("Remove border", "remove"),
    ],
)
def test_an_action_next_to_an_exception_word_is_refused(label, word) -> None:
    found = run_x([el("e2", text=label)])
    assert reasons(found, "e2") == ("deny_word",)
    detail = deny_of(found, "e2")
    assert detail is not None
    assert detail.word == word


def test_a_second_occurrence_outside_the_exception_counts() -> None:
    # The first "order" lies inside "Recorder", the second does not.
    assert word_matches("Recorder order", "order", exceptions=("recorder",))
    assert word_matches("Automatically recall", "call", exceptions=("automatically",))
    assert reasons(run_x([el("e2", text="Recorder order")]), "e2") == ("deny_word",)


def test_a_hit_that_only_overlaps_an_exception_counts() -> None:
    # "lines" starts inside "Borderline" and ends in "sale" (spaces removed).
    assert word_matches("Borderline sale", "lines", exceptions=("borderline",))
    assert not word_matches("Borderline", "rdel", exceptions=("borderline",))
    policy = Policy(deny_words=("lines",), deny_exceptions=("borderline",))
    found = run_x([el("e2", text="Borderline sale")], policy=policy)
    assert reasons(found, "e2") == ("deny_word",)
    assert reasons(run_x([el("e2", text="Borderline")], policy=policy), "e2") == ()


@pytest.mark.parametrize(
    ("label", "word"),
    [
        ("Job order", "order"),  # spaces removed: "joborder" holds "border"
        ("Web order", "order"),
        ("Club order", "order"),
        ("Guide Sign in Google", "sign in"),  # "guidesigningoogle"
        ("Guides cart estimate", "cart"),  # "guidescartestimate"
    ],
)
def test_an_exception_across_a_word_boundary_does_not_count(label, word) -> None:
    assert word_matches(label, word, exceptions=EXC)
    found = run_x([el("e2", text=label)])
    detail = deny_of(found, "e2")
    assert detail is not None
    assert detail.word == word


@pytest.mark.parametrize(
    "label",
    [
        # The zero-width space ends a word: no word equals "border".
        "Job\u200border",
        "Bor\u200bder",
        # NFKC turns the circled letter into J: the word is "jborder".
        "\u24bfborder",
        # A combining mark belongs to the word before it, so the word is not
        # "border" (no precomposed letter exists for these).
        "b\u0300order",
        "border\u0332",
        # An inflected form is another word.
        "Borders",
        "Postures",
        "Recorders",
        # A letter that NFKC changes (full-width, circled) is not accepted in
        # an exception word, even though the result would be equal.
        "\uff21utomatically",
        "\u24d0utomatically",
        # camelCase and separators: the exception is one word, the deny word
        # stands beside it.
        "AutomaticallyCall",
        "Automatically_call",
    ],
)
def test_an_exception_counts_only_as_one_unchanged_whole_word(label) -> None:
    hits = [w for w in DEFAULT_DENY_WORDS if word_matches(label, w, exceptions=EXC)]
    assert hits, label
    assert reasons(run_x([el("e2", text=label)]), "e2") == ("deny_word",)


def test_a_composed_accent_is_another_letter_and_no_hit_at_all() -> None:
    # NFKC composes r + U+0301 into U+0155, so the text holds no "order" and
    # neither rule refuses it; the exception words play no part.
    for label in ("borde\u0155", "border\u0301"):
        assert not word_matches(label, "order")
        assert reasons(run_x([el("e2", text=label)]), "e2") == ()


def test_joined_children_text_uses_the_exceptions() -> None:
    row = el(
        "e2", text="", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 200)
    )
    title = el(
        "e3",
        parent="e2",
        text="Network preferences",
        flags=(),
        bounds=(10, 110, 300, 140),
    )
    summary = el(
        "e4", parent="e2", text=WIFI_SUMMARY, flags=(), bounds=(10, 150, 300, 190)
    )
    assert reasons(run_x([row, title, summary]), "e2") == ()


def test_a_word_split_over_two_children_is_not_an_exception() -> None:
    row = el(
        "e2", text="", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 200)
    )
    first = el(
        "e3", parent="e2", text="Automatic", flags=(), bounds=(10, 110, 300, 140)
    )
    second = el("e4", parent="e2", text="ally", flags=(), bounds=(10, 150, 300, 190))
    found = run_x([row, first, second])
    detail = deny_of(found, "e2")
    assert detail is not None
    assert (detail.word, detail.field, detail.element_id) == ("call", "joined", "e2")


def test_a_hit_in_the_joined_text_outside_the_exception_counts() -> None:
    row = el(
        "e2", text="", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 200)
    )
    first = el(
        "e3", parent="e2", text="Automatically ca", flags=(), bounds=(10, 110, 300, 140)
    )
    second = el("e4", parent="e2", text="ll back", flags=(), bounds=(10, 150, 300, 190))
    detail = deny_of(run_x([row, first, second]), "e2")
    assert detail is not None
    assert (detail.word, detail.field) == ("call", "joined")


def test_a_resource_id_does_not_use_the_exceptions() -> None:
    found = run_x([el("e2", text="Next", rid="com.example:id/tv_automatically")])
    detail = deny_of(found, "e2")
    assert detail is not None
    assert (detail.word, detail.field) == ("call", "resource_id")
    element = el("e2", rid="com.example:id/tv_automatically")
    assert deny_word_in(element, ("call",), exceptions=("automatically",)) == (
        "call",
        "resource_id",
    )
    assert (
        deny_word_in(
            el("e2", text="Automatically"), ("call",), exceptions=("automatically",)
        )
        is None
    )


def test_the_screen_wide_deny_for_confirm_buttons_has_no_exceptions() -> None:
    # The prompt is not clickable and not a dialog: only the screen-wide deny
    # word refuses the confirm button, and exceptions do not apply there.
    prompt = el(
        "e3",
        text="Debit $50 automatically?",
        cls="android.widget.TextView",
        flags=(),
        bounds=(50, 300, 250, 350),
    )
    found = run_x([el("e2", text="Continue"), prompt])
    assert reasons(found, "e2") == ("confirm_button",)
    detail = next(d for d in decision(found, "e2").details)
    assert (detail.word, detail.field, detail.element_id) == ("call", "text", "e3")
    summary = el("e3", text=WIFI_SUMMARY, flags=(), bounds=(50, 300, 250, 350))
    assert reasons(run_x([el("e2", text="OK"), summary]), "e2") == ("confirm_button",)


def test_the_screen_wide_joined_text_has_no_exceptions() -> None:
    # "Automatic" + "ally" holds "call" only once the texts are joined.
    parts = [
        el("e3", text="Automatic", flags=(), bounds=(50, 300, 150, 350)),
        el("e4", text="ally", flags=(), bounds=(150, 300, 250, 350)),
    ]
    policy = Policy(deny_exceptions=("automatically",))
    found = run_x([el("e2", text="OK"), *parts], policy=policy)
    assert reasons(found, "e2") == ("confirm_button",)
    detail = decision(found, "e2").details[0]
    assert (detail.word, detail.field) == ("call", "joined")


def test_irreversibility_phrases_do_not_use_the_exceptions() -> None:
    policy = Policy(irreversible_phrases=("call",), deny_exceptions=EXC)
    found = run_x([el("e2", text="Automatically")], policy=policy)
    assert reasons(found, "e2") == ("irreversible_screen",)


def test_without_exceptions_the_old_substring_rule_applies() -> None:
    policy = Policy(deny_exceptions=())
    for label in ("Automatically", "Posture", "Recorder", "Border", "Designing"):
        assert reasons(run_x([el("e2", text=label)], policy=policy), "e2") == (
            "deny_word",
        )
    assert word_matches("Automatically", "call")


@pytest.mark.parametrize(
    "word",
    ["auto matically", "auto-matically", "border!", "-", "bor\u200bder", " border"],
)
def test_an_exception_must_be_one_word(word) -> None:
    with pytest.raises(
        ValueError, match=r"one word of letters and digits|empty|invisible"
    ):
        Policy(deny_exceptions=(word,))
    with pytest.raises(
        ValueError, match=r"one word of letters and digits|empty|invisible"
    ):
        word_matches("x", "x", exceptions=(word,))


def test_exceptions_must_be_a_collection() -> None:
    loose: Any = Policy
    with pytest.raises(TypeError, match="collection of words"):
        loose(deny_exceptions="border")
    with pytest.raises(TypeError, match="collection of words"):
        word_matches("Border", "order", exceptions="border")


@pytest.mark.parametrize(
    ("words", "exception"),
    [
        (("call",), "CALL"),
        (("auto-matically",), "automatically"),  # its spaceless form
        (("border",), "\uff42order"),  # NFKC makes it "border"
    ],
)
def test_an_exception_equal_to_a_deny_word_is_refused(words, exception) -> None:
    with pytest.raises(ValueError, match="equals the deny word"):
        Policy(deny_words=words, deny_exceptions=(exception,))


def test_an_added_deny_word_drops_the_equal_exception() -> None:
    policy = EXC_POLICY.with_deny_words(["Automatically"])
    assert "automatically" not in policy.deny_exceptions
    assert "border" in policy.deny_exceptions
    found = run_x([el("e2", text="Automatically")], policy=policy)
    assert reasons(found, "e2") == ("deny_word",)
    assert reasons(run_x([el("e2", text="Border")], policy=policy), "e2") == ()


def test_tracing_that_is_not_certain_counts_the_hit() -> None:
    # Removing the zero-width space lets NFKC compose e + U+0301 differently
    # from the character-by-character trace, so the hit counts.
    label = "Automatically e\u200b\u0301"
    excuser = guards._Excuser(frozenset({"automatically"}))
    assert excuser.counts(label, ("call",), builtin=True)
    # The same text without the trap is excused.
    assert not excuser.counts("Automatically e", ("call",), builtin=True)


# -- which words the exceptions weaken ------------------------------------------------


def test_the_builtin_exception_keys_are_the_exception_keys() -> None:
    keys = {guards._exception_key(word) for word in DEFAULT_DENY_EXCEPTIONS}
    assert keys == guards._BUILTIN_EXCEPTION_KEYS


@pytest.mark.usefixtures("builtin_exc")
def test_builtin_exceptions_do_not_weaken_a_word_the_user_added() -> None:
    assert word_matches("Automatically", "matic", exceptions=EXC)
    assert word_matches("Recorder", "rde", exceptions=EXC)
    assert not word_matches("Automatically", "call", exceptions=EXC)
    policy = EXC_POLICY.with_deny_words(["matic"])
    detail = deny_of(run_x([el("e2", text="Automatically")], policy=policy), "e2")
    assert detail is not None
    assert detail.word == "matic"
    plain = Policy(deny_words=("matic",))
    assert reasons(run_x([el("e2", text="Automatically")], policy=plain), "e2") == (
        "deny_word",
    )


@pytest.mark.usefixtures("builtin_exc")
def test_a_word_equal_to_a_builtin_word_is_builtin() -> None:
    # After normalize, "CALL" is the built-in "call", so the exception applies.
    assert not word_matches("Automatically", "CALL", exceptions=("automatically",))
    policy = Policy(deny_words=("CALL",), deny_exceptions=EXC)
    assert reasons(run_x([el("e2", text="Automatically")], policy=policy), "e2") == ()
    # A different spelling with other forms is not the built-in word.
    assert word_matches("Automatically", "c-all", exceptions=("automatically",))


def test_a_user_exception_excuses_every_word_also_an_added_one() -> None:
    # With no built-in exception words, every exception is the user's: it
    # excuses the built-in deny words and the added ones alike.
    policy = EXC_POLICY.with_deny_words(["matic"])
    for label in ("Automatically", "Border"):
        assert reasons(run_x([el("e2", text=label)], policy=policy), "e2") == (), label
    assert not word_matches("Automatically", "matic", exceptions=EXC)
    # An added word outside the exception still counts.
    assert reasons(run_x([el("e2", text="Cinematic")], policy=policy), "e2") == (
        "deny_word",
    )


@pytest.mark.usefixtures("builtin_exc")
def test_a_user_exception_applies_to_every_word() -> None:
    # "dramatic" is not a built-in exception: it excuses a user word ...
    assert not word_matches("Dramatic", "matic", exceptions=("dramatic",))
    # ... and a built-in word ("post" inside "Postal").
    assert not word_matches("Postal", "post", exceptions=("postal",))
    policy = Policy(deny_exceptions=(*EXC, "postal", "dramatic"))
    policy = policy.with_deny_words(["matic"])
    for label in ("Postal", "Dramatic", "Border"):
        assert reasons(run_x([el("e2", text=label)], policy=policy), "e2") == (), label
    # The built-in "automatically" still does not excuse the user word.
    assert reasons(run_x([el("e2", text="Automatically")], policy=policy), "e2") == (
        "deny_word",
    )


# -- review fixes: user words, invisible characters, public checks, cost -----------


@pytest.mark.usefixtures("builtin_exc")
def test_an_added_word_inside_a_builtin_exception_is_not_weakened() -> None:
    policy = EXC_POLICY.with_deny_words(["record"])
    found = run_x([el("e2", text="Start microphone recorder")], policy=policy)
    detail = deny_of(found, "e2")
    assert detail is not None
    assert detail.word == "record"
    only = Policy(deny_words=("record",), deny_exceptions=EXC)
    assert reasons(run_x([el("e2", text="Recorder")], policy=only), "e2") == (
        "deny_word",
    )


@pytest.mark.parametrize(
    "word",
    ["Pay\ufe0f", "pay\u200b", "\u00adborder", "bor\u034fder", "border\U000e0100"],
)
def test_an_exception_with_an_invisible_character_is_refused(word) -> None:
    with pytest.raises(ValueError, match="invisible character"):
        Policy(deny_exceptions=(word,))
    with pytest.raises(ValueError, match="invisible character"):
        word_matches("x", "y", exceptions=(word,))


@pytest.mark.parametrize(
    "label",
    [
        "Disconnect recorder\u200bs",
        "Disconnect s\u200brecorder",
        "recorder\u200b\u200bs",
        "recorder\u200b\u0332",
        "Automatically\u2060ish",
        # A combining mark after the invisible character: without it the
        # text reads x + U+0332 + recorder, one word.
        "x\u200b\u0332recorder",
        "x\u200b\u200b\u0332\u0301recorder",
    ],
)
def test_an_invisible_character_next_to_the_exception_cannot_hide_a_longer_word(
    label,
) -> None:
    assert reasons(run_x([el("e2", text=label)]), "e2") == ("deny_word",)


@pytest.mark.parametrize(
    "label",
    [
        "recorder\u200b",
        "\u200brecorder",
        "recorder\u200b s",
        # A mark after a non-letter does not join the word.
        "-\u0332recorder",
    ],
)
def test_an_invisible_character_before_a_boundary_keeps_the_exception(label) -> None:
    assert reasons(run_x([el("e2", text=label)]), "e2") == ()


def test_the_public_helpers_refuse_an_exception_equal_to_the_word() -> None:
    with pytest.raises(ValueError, match="equals the deny word"):
        word_matches("Pay", "pay", exceptions=("PAY",))
    with pytest.raises(ValueError, match="equals the deny word"):
        deny_word_in(el("e2", text="Pay"), ("pay",), exceptions=("PAY",))
    with pytest.raises(ValueError, match="equals the deny word"):
        deny_word_in(
            el("e2", text="x"), iter(["auto-matically"]), exceptions=("automatically",)
        )
    with pytest.raises(TypeError, match="collection of words"):
        deny_word_in(el("e2", text="x"), ("pay",), exceptions="border")


def _joined_match_seconds(count: int) -> float:
    value = " ".join(["Border"] * count)
    best = float("inf")
    for _ in range(3):
        matcher = guards._Matcher(("order",), ("border",))
        start = time.perf_counter()
        assert matcher.match_text(value) is None
        best = min(best, time.perf_counter() - start)
    return best


def test_many_exception_words_in_one_text_cost_linear_time() -> None:
    # Every hit lies inside its own run; a scan of all runs per hit would make
    # eight times the text take about sixty-four times as long.
    small = _joined_match_seconds(500)
    large = _joined_match_seconds(4000)
    assert large < 16 * small + 0.02, (small, large)


@pytest.mark.parametrize(
    "label",
    [
        # NFKC turns U+013F into "L" and a middle dot (a separator): the
        # boundary exists only after NFKC, the original is one word.
        "\u013frecorder",
        "Rec\u013frecorder",
        "recorder\u013f",
    ],
)
def test_an_exception_must_be_a_whole_word_in_the_original_text(label) -> None:
    assert word_matches(label, "order", exceptions=("recorder",))
    assert reasons(run_x([el("e2", text=label)]), "e2") == ("deny_word",)


def test_an_exception_with_the_same_combining_mark_matches() -> None:
    # Only an additional or a different mark makes another word.
    assert not word_matches("r\u0308ecorder", "order", exceptions=("r\u0308ecorder",))
    assert word_matches("r\u0308ecorder", "order", exceptions=("recorder",))
    assert word_matches("r\u0308ecorder", "order", exceptions=("r\u0301ecorder",))
