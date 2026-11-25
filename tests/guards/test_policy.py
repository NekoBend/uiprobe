"""Tests for Policy, the default word lists and parse_deny_file."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

import pytest

from tests.guards.helpers import decision, el, reasons, run
from uiprobe import guards
from uiprobe.guards import (
    CONFIRM_WORDS,
    DEFAULT_DENY_WORDS,
    Detail,
    Policy,
    parse_deny_file,
    word_matches,
)
from uiprobe.model import Bounds


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
        back_on_other_screens=True,
        check_tap_window=False,
        skip_two_target_rows=False,
        skip_rows_with_buttons=True,
        skip_navigate_up=False,
        skip_sliders=False,
    )
    copy = original.with_deny_words(["rent"])
    assert copy is not original
    for f in fields(Policy):
        if f.name == "deny_words":
            assert copy.deny_words == ("buy", "rent")
        else:
            assert getattr(copy, f.name) == getattr(original, f.name), f.name


def test_check_tap_window_is_on_by_default_and_must_be_a_bool() -> None:
    assert Policy().check_tap_window is True
    loose: Any = Policy
    for value in (0, "no", None):
        with pytest.raises(TypeError, match="check_tap_window must be a bool"):
            loose(check_tap_window=value)


def test_back_on_other_screens_is_off_by_default_and_must_be_a_bool() -> None:
    assert Policy().back_on_other_screens is False
    assert Policy(back_on_other_screens=True).back_on_other_screens is True
    loose: Any = Policy  # to pass values of a wrong type on purpose
    for value in (1, "yes", None):
        with pytest.raises(TypeError, match="back_on_other_screens must be a bool"):
            loose(back_on_other_screens=value)


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


def test_replace_keeps_validation() -> None:
    with pytest.raises(ValueError, match="empty"):
        replace(Policy(), deny_words=(" ",))


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
