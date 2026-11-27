"""Normalizing text and matching deny words in it, without exception words."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Final

from uiprobe.guards.constants import (
    _APOSTROPHES,
    _DEFAULT_IGNORABLE,
    _EMPTY_CATEGORIES,
    _JAPANESE_RANGES,
    _SEPARATOR_CHARS,
    _SPACES_RE,
    DEFAULT_DENY_WORDS,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


# -- normalization and matching ------------------------------------------------


def normalize(value: str) -> str:
    """Return NFKC, case-folded, with every whitespace run made one space.

    NFKC maps full-width Latin letters to ASCII and half-width katakana to
    the full-width forms. The result has no leading or trailing space.
    """
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _SPACES_RE.sub(" ", folded).strip()


def _is_default_ignorable(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _DEFAULT_IGNORABLE)


def _is_invisible(char: str) -> bool:
    """True for a character that carries no readable label (not whitespace)."""
    if char.isspace():
        return False
    return unicodedata.category(char) in _EMPTY_CATEGORIES or _is_default_ignorable(
        char
    )


def _strip_invisible(value: str) -> str:
    """Remove characters that carry no readable label (whitespace is kept)."""
    return "".join(char for char in value if not _is_invisible(char))


def _separators_to_spaces(value: str) -> str:
    return "".join(
        " " if char in _SEPARATOR_CHARS or unicodedata.category(char) == "Pd" else char
        for char in value
    )


def _split_camel(value: str) -> str:
    """Insert a space at camelCase boundaries (``signUp``, ``HTMLParser``)."""
    out: list[str] = []
    for index, char in enumerate(value):
        if index and char.isupper():
            prev = value[index - 1]
            following = value[index + 1 : index + 2]
            if prev.islower() or (prev.isupper() and following.islower()):
                out.append(" ")
        out.append(char)
    return "".join(out)


def _form_triple(value: str) -> tuple[str, str, str]:
    """Return the normalized, spaced and joined forms of a value, in order."""
    visible = _strip_invisible(unicodedata.normalize("NFKC", value))
    plain = normalize(visible)
    spaced = normalize(_separators_to_spaces(_split_camel(visible)))
    joined = "".join(spaced.split(" "))
    return plain, spaced, joined


def _forms(value: str) -> tuple[str, ...]:
    """Return the distinct normalized, spaced and joined forms of a value."""
    # The spaced form is redundant with plain + joined (removing spaces on
    # both sides keeps a substring a substring); it is kept for clarity.
    return tuple(dict.fromkeys(_form_triple(value)))


def _is_word_char(char: str) -> bool:
    """True for a letter, a digit or a combining mark."""
    return char.isalnum() or unicodedata.category(char)[0] == "M"


def _visible_end(text: str, *, last: bool) -> str:
    """Return the first (or last) visible character of `text`, or ``""``."""
    chars = reversed(text) if last else iter(text)
    return next((c for c in chars if not _is_invisible(c)), "")


def _tight_separators(parts: Sequence[str]) -> list[str]:
    """Return the separator put before each part in the tight reading.

    The space between two parts is uiprobe's, not the app's. Where the last
    visible character before it and the first visible character after it
    are both letters, digits or combining marks, in any script, the parts
    are joined directly (``recorder`` + ``s`` reads ``recorders``,
    ``削除済み`` + ``にする`` reads ``削除済みにする``). A part with no visible
    character is joined directly and does not count as either side, so
    ``削除済み`` + U+200B + ``にする`` reads as one word too, and a first part
    with no visible character adds no space before the next one.
    """
    separators: list[str] = []
    last = ""
    for part in parts:
        first = _visible_end(part, last=False)
        if not separators:
            separators.append("")
        elif first and last and not (_is_word_char(last) and _is_word_char(first)):
            separators.append(" ")
        else:
            separators.append("")
        tail = _visible_end(part, last=True)
        if tail:
            last = tail
    return separators


def _join(parts: Sequence[str], separators: Sequence[str]) -> tuple[str, list[int]]:
    """Join the parts with the separators; return the text and where each
    part starts in it."""
    out: list[str] = []
    starts: list[int] = []
    length = 0
    for separator, part in zip(separators, parts, strict=True):
        out.append(separator)
        length += len(separator)
        starts.append(length)
        out.append(part)
        length += len(part)
    return "".join(out), starts


def _alone_in_join(raw: str, start: int, end: int, part: tuple[int, int]) -> bool:
    """True when ``raw[start:end]``, an exception run inside the value at
    ``raw[part[0]:part[1]]``, has real boundaries in the values written one
    after another (`raw`, with no separator added).

    Inside the value, the run already stands alone (`_Excuser`). Where the
    run reaches the start of the value (past invisible characters and
    combining marks) or its end (past invisible characters), the nearest
    visible character of the neighbouring values must not be a letter, a
    digit or a combining mark; values with only invisible characters are
    passed over.
    """
    first, last = part
    before = start - 1
    while before >= first and (
        _is_invisible(raw[before]) or unicodedata.category(raw[before])[0] == "M"
    ):
        before -= 1
    if before < first:
        before = first - 1
        while before >= 0 and _is_invisible(raw[before]):
            before -= 1
        if before >= 0 and _is_word_char(raw[before]):
            return False
    after = end
    while after < last and _is_invisible(raw[after]):
        after += 1
    if after >= last:
        after = last
        while after < len(raw) and _is_invisible(raw[after]):
            after += 1
        if after < len(raw) and _is_word_char(raw[after]):
            return False
    return True


def _is_japanese_letter(char: str) -> bool:
    """True for a letter of the Japanese script (kana, ``ー``, ``々``, kanji)."""
    if not unicodedata.category(char).startswith("L"):
        return False
    code = ord(char)
    return any(low <= code <= high for low, high in _JAPANESE_RANGES)


def _phrase_key(phrase: str) -> str:
    """Return what decides which texts an irreversibility phrase matches.

    The phrase normalized with curly apostrophes read as plain ones and with
    every space and separator removed: the matching always compares this
    form with the same form of the text, and every other form that matches
    implies that this one does. Two phrases with the same key match the
    same texts.
    """
    visible = _strip_invisible(
        unicodedata.normalize("NFKC", phrase.translate(_APOSTROPHES))
    )
    spaced = normalize(_separators_to_spaces(_split_camel(visible)))
    return "".join(spaced.split(" "))


def _check_word(word: str) -> str:
    if not normalize(word) or not _has_content(word):
        msg = f"word is empty after normalization: {word!r}"
        raise ValueError(msg)
    return word


MAX_EXCEPT_PHRASE_LENGTH: Final = 80


def _except_phrase_key(phrase: str) -> str:
    """Check an exception phrase; return `normalize` of it, the form it is
    compared with (two phrases with the same form are one phrase).

    Raises:
        ValueError: The phrase is not 1 to 80 characters long, holds a control
            or an invisible character, or has no letter.
    """
    if not 1 <= len(phrase) <= MAX_EXCEPT_PHRASE_LENGTH:
        msg = (
            f"phrase must be 1 to {MAX_EXCEPT_PHRASE_LENGTH} characters long,"
            f" this one has {len(phrase)}"
        )
        raise ValueError(msg)
    for char in phrase:
        if unicodedata.category(char) == "Cc" or _is_invisible(char):
            msg = f"phrase holds a control or invisible character (U+{ord(char):04X})"
            raise ValueError(msg)
    key = normalize(phrase)
    if not any(char.isalpha() for char in key):
        msg = f"phrase has no letter: {phrase!r}"
        raise ValueError(msg)
    return key


def _forms_match(word_forms: tuple[str, ...], field_forms: tuple[str, ...]) -> bool:
    return any(w in f for w in word_forms if w for f in field_forms)


# -- exception words ---------------------------------------------------------------


def _in_word(char: str, *, inside: bool) -> bool:
    """True for a character that belongs to a word run.

    A run is a maximal sequence of letters and digits (`str.isalnum`) with the
    combining marks (category M) that follow them; a mark starts no run.
    """
    return char.isalnum() or (inside and unicodedata.category(char)[0] == "M")


def _word_runs(text: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` index pairs of the word runs of `text`."""
    runs: list[tuple[int, int]] = []
    start = -1
    for index, char in enumerate(text):
        if _in_word(char, inside=start != -1):
            if start == -1:
                start = index
        elif start != -1:
            runs.append((start, index))
            start = -1
    if start != -1:
        runs.append((start, len(text)))
    return runs


def _is_combining(char: str) -> bool:
    return unicodedata.category(char)[0] == "M"


def _stands_alone(text: str, start: int, end: int) -> bool:
    """True when ``text[start:end]`` is a whole word even with the invisible
    characters around it removed.

    Deny matching removes invisible characters, so ``recorder`` + U+200B +
    ``s`` reads as ``recorders`` and ``x`` + U+200B + U+0332 + ``recorder``
    as one word. Before the span, invisible characters and combining marks
    are passed over: a letter or digit found there joins the span. After it,
    invisible characters are passed over: a letter, digit or combining mark
    found there joins the span.
    """
    before = start - 1
    while before >= 0 and (_is_invisible(text[before]) or _is_combining(text[before])):
        before -= 1
    if before >= 0 and text[before].isalnum():
        return False
    after = end
    while after < len(text) and _is_invisible(text[after]):
        after += 1
    return not (
        after < len(text) and (text[after].isalnum() or _is_combining(text[after]))
    )


def _exception_key(word: str) -> str:
    """Return the form a word run is compared with: NFKC, then case-folded.

    Raises:
        ValueError: The word is empty after normalization, holds an invisible
            character (a default-ignorable one such as U+200B or the
            variation selector U+FE0F, or a format or control character:
            deny matching removes them, so the exception could equal a deny
            word in disguise), or is not one word of letters and digits (a
            space, a separator or punctuation in it would keep it from ever
            being equal to a word of a text).
    """
    _check_word(word)
    if any(_is_invisible(char) for char in word):
        msg = f"exception word holds an invisible character: {word!r}"
        raise ValueError(msg)
    folded = unicodedata.normalize("NFKC", word)
    if _word_runs(folded) != [(0, len(folded))]:
        msg = f"exception word must be one word of letters and digits: {word!r}"
        raise ValueError(msg)
    return folded.casefold()


def _is_builtin_word(word: str) -> bool:
    """True for a deny word equal to a built-in one after `normalize`.

    Only such a word is weakened by the built-in exception words.
    """
    return normalize(word) in _BUILTIN_WORD_KEYS


def _check_collection(exceptions: Iterable[str]) -> Iterable[str]:
    if isinstance(exceptions, str):
        msg = "exception words must be a collection of words, not one string"
        raise TypeError(msg)
    return exceptions


def _exception_keys(exceptions: Iterable[str]) -> frozenset[str]:
    return frozenset(_exception_key(word) for word in _check_collection(exceptions))


def _check_no_conflict(exceptions: Iterable[str], deny_words: Iterable[str]) -> None:
    """Raise when an exception word equals a deny word (see `Policy`).

    Raises:
        ValueError: An exception word equals a deny word (any of its forms).
    """
    deny_forms = _deny_forms(deny_words)
    for exception in exceptions:
        word = _exception_conflict(exception, deny_forms)
        if word is not None:
            msg = (
                f"exception word {exception!r} equals the deny word {word!r};"
                " an exception cannot excuse its own deny word"
            )
            raise ValueError(msg)


# What makes a deny word built-in (`_is_builtin_word`) and an exception word
# built-in (its key is one of these): built-in exceptions excuse only hits of
# built-in deny words.
_BUILTIN_WORD_KEYS: Final = frozenset(normalize(word) for word in DEFAULT_DENY_WORDS)


def _deny_forms(words: Iterable[str]) -> dict[str, str]:
    """Return every form (see `_forms`) of the deny words, each with its word."""
    found: dict[str, str] = {}
    for word in words:
        for form in _forms(word):
            found.setdefault(form, word)
    return found


def _exception_conflict(exception: str, deny_forms: dict[str, str]) -> str | None:
    """Return the deny word that the exception word equals, or None.

    `deny_forms` comes from `_deny_forms`. Equal means that one of the deny
    word's forms is the exception's key, so that every whole-word hit of that
    deny word would be excused.
    """
    return deny_forms.get(_exception_key(exception))


def _has_content(value: str) -> bool:
    return any(not char.isspace() and not _is_invisible(char) for char in value)


def _clean(value: str) -> str:
    return _strip_invisible(value).strip()


def _is_mark(char: str) -> bool:
    return char.isspace() or unicodedata.category(char)[0] in "PS"


def _strip_marks(label: str) -> str:
    """Remove whitespace, punctuation and symbols around a label.

    Both ends are trimmed until they stop changing, so ``( OK )``, ``OK!`` and
    ``[ OK ]`` all become ``OK``.
    """
    start, end = 0, len(label)
    while start < end and _is_mark(label[start]):
        start += 1
    while end > start and _is_mark(label[end - 1]):
        end -= 1
    return label[start:end]


def _confirm_form(value: str) -> str:
    """Return the form compared with the confirm words (label and words alike)."""
    return _strip_marks(_strip_invisible(normalize(_clean(value))).strip())
