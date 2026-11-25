"""Tests for word matching, normalisation and joined text."""

from __future__ import annotations

import pytest

from tests.guards.helpers import (
    CLICK,
    WIFI_SUMMARY,
    decision,
    deny_of,
    el,
    reasons,
    run,
)
from uiprobe import guards
from uiprobe.guards import (
    Policy,
    deny_word_in,
    normalize,
    word_matches,
)

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


# -- joined fragments of a script written without spaces -----------------------------


def _fragment_row(parts):
    row = el(
        "e2", text="", cls="android.widget.LinearLayout", bounds=(0, 100, 320, 200)
    )
    children = [
        el(
            f"e{3 + index}",
            parent="e2",
            text=part,
            cls="android.widget.TextView",
            flags=(),
            bounds=(10 + index * 100, 110, 100 + index * 100, 140),
        )
        for index, part in enumerate(parts)
    ]
    return [row, *children]


@pytest.mark.parametrize(
    "parts",
    [
        ("\u524a\u9664\u6e08\u307f", "\u306b\u3059\u308b"),
        ("\u524a\u9664\u6e08\u307f\u200b", "\u306b\u3059\u308b"),
        ("\u524a\u9664\u6e08\u307f", "\u200b\u306b\u3059\u308b"),
        ("\u524a\u9664\u6e08\u307f", "abc"),
    ],
)
def test_touching_fragments_of_an_unspaced_script_are_one_word(parts) -> None:
    # 削除済み + にする reads 削除済みにする: the space the joined text puts
    # between the two texts is no word boundary, so 削除 counts.
    policy = Policy(deny_exceptions=("\u524a\u9664\u6e08\u307f",))
    found = run(_fragment_row(parts), policy=policy)
    detail = deny_of(found, "e2")
    assert detail is not None
    assert (detail.word, detail.field) == ("\u524a\u9664", "joined")
    single = run([el("e2", text="".join(parts))], policy=policy)
    assert reasons(single, "e2") == ("deny_word",)


@pytest.mark.parametrize(
    "parts",
    [
        ("\u524a\u9664\u6e08\u307f",),
        ("\u524a\u9664\u6e08\u307f", "\u3001\u4eca\u65e5"),
        ("\u524a\u9664\u6e08\u307f ", "\u306b\u3059\u308b"),
    ],
)
def test_a_real_boundary_between_fragments_keeps_the_exception(parts) -> None:
    policy = Policy(deny_exceptions=("\u524a\u9664\u6e08\u307f",))
    assert reasons(run(_fragment_row(parts), policy=policy), "e2") == ()


@pytest.mark.parametrize(
    ("parts", "exception", "word"),
    [
        (("recorder", "s"), "recorder", "order"),
        (("recorder", "\u0332"), "recorder", "order"),
        (
            ("\u524a\u9664\u6e08\u307f2", "3"),
            "\u524a\u9664\u6e08\u307f2",
            "\u524a\u9664",
        ),
        (
            ("\u524a\u9664\u6e08\u307f", "\u200b", "\u306b\u3059\u308b"),
            "\u524a\u9664\u6e08\u307f",
            "\u524a\u9664",
        ),
        (("Turn on", "automatically"), "automatically", "call"),
    ],
)
def test_touching_fragments_are_one_word_in_every_script(
    parts, exception, word
) -> None:
    # The space between two texts is uiprobe's: where letters, digits or
    # marks touch on both sides it is no word boundary, so the exception does
    # not stand alone and the hit counts, as in the single label.
    policy = Policy(deny_exceptions=(exception,))
    detail = deny_of(run(_fragment_row(parts), policy=policy), "e2")
    assert detail is not None
    assert (detail.word, detail.field) == (word, "joined")
    single = run([el("e2", text="".join(parts))], policy=policy)
    assert reasons(single, "e2") == ("deny_word",)


@pytest.mark.parametrize(
    "parts",
    [
        ("Network preferences", WIFI_SUMMARY),
        ("recorder", " s"),
        ("recorder", "!"),
        ("Voice", " recorder"),
    ],
)
def test_real_separators_between_fragments_keep_the_exception(parts) -> None:
    policy = Policy(deny_exceptions=("recorder", "automatically"))
    assert reasons(run(_fragment_row(parts), policy=policy), "e2") == ()


@pytest.mark.parametrize("outer_clickable", [True, False])
def test_an_outer_clickable_cannot_hide_an_inner_buttons_refusal(
    outer_clickable,
) -> None:
    # The inner button reads 削除済みにする (refused); the outer container
    # also holds 方法, so its joined text is the longer exception
    # 削除済みにする方法. The inner button stays refused either way, and so
    # does the clickable container that holds it.
    words = (
        "\u524a\u9664\u6e08\u307f",
        "\u524a\u9664\u6e08\u307f\u306b\u3059\u308b\u65b9\u6cd5",
    )
    policy = Policy(deny_exceptions=words)
    flags = CLICK if outer_clickable else ()
    elements = [
        el(
            "e2",
            cls="android.widget.LinearLayout",
            bounds=(0, 100, 320, 300),
            flags=flags,
        ),
        el(
            "e3",
            parent="e2",
            cls="android.widget.LinearLayout",
            bounds=(0, 100, 320, 200),
        ),
        el("e4", parent="e3", text=words[0], flags=(), bounds=(10, 110, 100, 130)),
        el(
            "e5",
            parent="e3",
            text="\u306b\u3059\u308b",
            flags=(),
            bounds=(100, 110, 200, 130),
        ),
        el(
            "e6", parent="e2", text="\u65b9\u6cd5", flags=(), bounds=(10, 220, 100, 240)
        ),
    ]
    found = run(elements, policy=policy)
    inner = deny_of(found, "e3")
    assert inner is not None
    # The longer exception would span two texts, so it never excuses: the
    # clickable container's own joined text holds the hit when there is one.
    holder = "e2" if outer_clickable else "e3"
    assert (inner.word, inner.field, inner.element_id) == (
        "\u524a\u9664",
        "joined",
        holder,
    )
    if outer_clickable:
        outer = deny_of(found, "e2")
        assert outer is not None
        assert (outer.field, outer.element_id) == ("joined", "e2")


# -- a hit is excused only inside one field of the joined text -------------------------


@pytest.mark.parametrize(
    ("parts", "exceptions"),
    [
        # The tight reading would excuse "recorders", a word over two texts.
        (("recorder", "s"), ("recorder", "recorders")),
        # NFKC composes r + U+0301 in the tight reading and the hit is gone
        # there; the spaced reading still has it, and the mark joins.
        (("recorder", "\u0301"), ("recorder",)),
        # A combining mark at the end of the previous text joins.
        (("\u0332", "recorder"), ("recorder",)),
        (("x\u0332", "\u200b", "recorder"), ("recorder",)),
    ],
)
def test_a_hit_is_not_excused_across_a_field_boundary(parts, exceptions) -> None:
    policy = Policy(deny_exceptions=exceptions)
    detail = deny_of(run(_fragment_row(parts), policy=policy), "e2")
    assert detail is not None
    assert (detail.word, detail.field) == ("order", "joined")


@pytest.mark.parametrize(
    "parts",
    [
        ("\u200b", "Settings"),
        ("\u200b", "recorder"),
        ("\u200b",),
        ("", "\u200b", "Help"),
    ],
)
def test_texts_with_only_invisible_characters_are_passed_over(parts) -> None:
    # A first text without a visible character used to raise TypeError.
    policy = Policy(deny_exceptions=("recorder",))
    elements = _fragment_row(parts)
    found = run(elements, policy=policy, root_bounds=(0, 0, 320, 640))
    assert "deny_word" not in reasons(found, "e2")


def _count_calls(monkeypatch, name):
    calls: list[str] = []
    original = getattr(guards.matching, name)

    def counted(*args):
        calls.append(name)
        return original(*args)

    monkeypatch.setattr(guards.matching, name, counted)
    return calls


def test_a_joined_text_without_hits_builds_no_source_maps(monkeypatch) -> None:
    traced = _count_calls(monkeypatch, "_original_forms")
    nfkc = _count_calls(monkeypatch, "_nfkc_traced")
    policy = Policy(deny_exceptions=("recorder",))
    long_text = "Hello world, nothing to see here. " * 300
    found = run(_fragment_row((long_text, long_text)), policy=policy)
    assert reasons(found, "e2") == ()
    assert traced == []
    assert nfkc == []


def test_source_maps_are_built_only_for_a_hit_with_an_exception(monkeypatch) -> None:
    traced = _count_calls(monkeypatch, "_original_forms")
    policy = Policy(deny_exceptions=("recorder",))
    # A hit, but no exception word in the texts: it counts without a map.
    found = run(_fragment_row(("Delete", "files")), policy=policy)
    assert reasons(found, "e2") == ("deny_word",)
    assert traced == []
    # A hit inside an exception word: the map decides, and the hit is excused.
    found = run(_fragment_row(("Voice recorder", "!")), policy=policy)
    assert reasons(found, "e2") == ()
    assert traced
