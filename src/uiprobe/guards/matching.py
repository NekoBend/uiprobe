"""Exception words (the traced forms), the matchers and the word checks."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Final

from uiprobe.guards.constants import (
    _APOSTROPHES,
    _CACHED_VALUE_LENGTH,
    _ID_SEPARATOR,
    _SEPARATOR_CHARS,
    DEFAULT_DENY_EXCEPTIONS,
)
from uiprobe.guards.words import (
    _alone_in_join,
    _check_collection,
    _check_no_conflict,
    _check_word,
    _exception_keys,
    _form_triple,
    _forms,
    _forms_match,
    _is_builtin_word,
    _is_invisible,
    _join,
    _stands_alone,
    _tight_separators,
    _word_runs,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from uiprobe.model import Element


def word_matches(value: str, word: str, *, exceptions: Iterable[str] = ()) -> bool:
    """Return True if the deny `word` matches the field `value`.

    The word matches when one of its forms is a substring of a form of the
    value (see the module docstring). With `exceptions` (exception words such
    as ``("automatically",)``), an occurrence of the word that lies
    completely inside a whole word of `value` equal to an exception word does
    not count; every other occurrence still does (see `_Excuser`). This is
    the rule of the text, content description and hint fields and of the
    joined texts; a resource id is matched without exceptions.

    A built-in exception word (equal to one of `DEFAULT_DENY_EXCEPTIONS`,
    which is empty) would apply only when `word` is a built-in deny word
    (equal to one of `DEFAULT_DENY_WORDS` after `normalize`); any other
    exception word applies to every deny word.

    Raises:
        TypeError: `exceptions` is one string, not a collection of words.
        ValueError: The word is empty after normalization, an exception word
            is not one word of letters and digits (or holds an invisible
            character), or an exception word equals `word` (see `Policy`).
    """
    _check_word(word)
    exceptions = tuple(_check_collection(exceptions))
    excuser = _Excuser(_exception_keys(exceptions))
    _check_no_conflict(exceptions, (word,))
    word_forms = _forms(word)
    if not _forms_match(word_forms, _forms(value)):
        return False
    return excuser.counts(value, word_forms, builtin=_is_builtin_word(word))


# The same as `_exception_key` of each word (a test checks it); that function
# needs helpers defined further down.
_BUILTIN_EXCEPTION_KEYS: Final = frozenset(
    unicodedata.normalize("NFKC", word).casefold() for word in DEFAULT_DENY_EXCEPTIONS
)


def _fold_traced(pairs: Iterable[tuple[str, int]]) -> list[tuple[str, int]]:
    """Case-fold, make every whitespace run one space and strip, with sources."""
    out: list[tuple[str, int]] = []
    for char, source in pairs:
        for folded in char.casefold():
            if not folded.isspace():
                out.append((folded, source))
            elif out and out[-1][0] != " ":
                out.append((" ", source))
    if out and out[-1][0] == " ":
        out.pop()
    return out


def _traced_forms(text: str) -> list[tuple[str, list[int]]] | None:
    """Return the three forms of an NFKC text, each with the index in `text`
    of the character that every form character comes from.

    The forms are built character by character the way `_form_triple` builds
    them; None when the result differs from `_form_triple` (for example when
    removing an invisible character lets NFKC compose two characters), so
    that the caller can count every hit.
    """
    visible = [
        (char, index) for index, char in enumerate(text) if not _is_invisible(char)
    ]
    camel: list[tuple[str, int]] = []
    for position, (char, index) in enumerate(visible):
        if position and char.isupper():
            prev = visible[position - 1][0]
            following = visible[position + 1][0] if position + 1 < len(visible) else ""
            if prev.islower() or (prev.isupper() and following.islower()):
                camel.append((" ", index))
        camel.append((char, index))
    separated = [
        (
            " "
            if char in _SEPARATOR_CHARS or unicodedata.category(char) == "Pd"
            else char,
            index,
        )
        for char, index in camel
    ]
    plain = _fold_traced(visible)
    spaced = _fold_traced(separated)
    joined = [pair for pair in spaced if pair[0] != " "]
    traced = [
        ("".join(char for char, _ in pairs), [index for _, index in pairs])
        for pairs in (plain, spaced, joined)
    ]
    if tuple(form for form, _ in traced) != _form_triple(text):
        return None
    return traced


def _hit_outside(
    form: str, sources: list[int], word_form: str, runs: list[tuple[int, int]]
) -> bool:
    """True when an occurrence of `word_form` in `form` is outside every run.

    `runs` are disjoint and in text order, and the sources of later
    occurrences do not start earlier (the traced forms keep text order), so
    one index moves forward through the runs: the work is linear in the
    number of occurrences plus runs. An occurrence that starts before the
    current run counts, which can only block more.
    """
    index = 0
    start = form.find(word_form)
    while start != -1:
        span = sources[start : start + len(word_form)]
        low, high = min(span), max(span)
        while index < len(runs) and runs[index][1] <= low:
            index += 1
        if index == len(runs) or not runs[index][0] <= low or high >= runs[index][1]:
            return True
        start = form.find(word_form, start + 1)
    return False


# The traced forms of a field (`_traced_forms`) and its exception runs, each
# as ``(start, end, builtin)``: builtin is True when the run equals a built-in
# exception word.
_Traced = tuple[list[tuple[str, list[int]]], list[tuple[int, int, bool]]]


class _Excuser:
    """Decides whether a deny-word hit in a field lies inside an exception word.

    An exception applies only where it is one whole word of the field: a word
    run (see `_word_runs`) of the field after NFKC, with the invisible
    characters still in place (they end a run), whose case-folded text equals
    the key of an exception word, and whose characters NFKC left unchanged
    (a circled or full-width letter in the run fails it). The deny words are
    found on the normalized forms of the field (`_form_triple`); every hit is
    traced back to the characters it comes from, and it is excused only when
    all of them lie inside one such run. Every hit is checked on its own, so
    one hit outside every exception run counts. When tracing is not certain
    (NFKC of the characters one by one differs from NFKC of the whole field,
    or a traced form differs from the real one), every hit counts. A run that
    equals a built-in exception word (`DEFAULT_DENY_EXCEPTIONS`) excuses only
    hits of a built-in deny word; a run equal to any other exception word
    excuses hits of every deny word.
    """

    def __init__(self, keys: frozenset[str]) -> None:
        self.keys = keys
        self._cache: dict[str, _Traced | None] = {}
        # The last long value (not cached): a joined text is matched against
        # every deny word that hits it.
        self._last: tuple[str, _Traced | None] | None = None

    def counts(self, value: str, word_forms: tuple[str, ...], *, builtin: bool) -> bool:
        """Return True when a hit of the word forms in `value` is not excused.

        `builtin` tells whether the deny word is a built-in one
        (`_is_builtin_word`); otherwise the built-in exception words do not
        excuse it. The caller has already found at least one hit.
        """
        traced = self._trace(value)
        if traced is None:
            return True
        forms, tagged = traced
        runs = [(first, end) for first, end, own in tagged if builtin or not own]
        if not runs:
            return True
        for form, sources in forms:
            for word_form in word_forms:
                if word_form and _hit_outside(form, sources, word_form, runs):
                    return True
        return False

    def _trace(self, value: str) -> _Traced | None:
        """Return the traced forms and the exception runs, or None when no
        hit can be excused (no exception run, or tracing is not certain).
        """
        if value in self._cache:
            return self._cache[value]
        if self._last is not None and self._last[0] == value:
            return self._last[1]
        result = self._compute(value)
        if len(value) <= _CACHED_VALUE_LENGTH:
            self._cache[value] = result
        else:
            self._last = (value, result)
        return result

    def _compute(self, value: str) -> _Traced | None:
        if not self.keys:
            return None
        traced = _nfkc_traced(value)
        if traced is None:
            return None
        text, unchanged, origin = traced
        runs = self._runs(value, text, unchanged, origin)
        if not runs:
            return None
        forms = _traced_forms(text)
        if forms is None:
            return None
        return forms, runs

    def _runs(
        self, value: str, text: str, unchanged: list[bool], origin: list[int]
    ) -> list[tuple[int, int, bool]]:
        """Return the exception runs of `text` (the NFKC of `value`).

        A run counts only when it is a whole word both after NFKC and in the
        original text: NFKC can create a boundary (U+013F becomes "L" and a
        middle dot), and invisible characters can hide a longer word. The
        characters of a counted run are unchanged by NFKC, so the run is the
        same span of the original text.
        """
        return [
            (start, end, text[start:end].casefold() in _BUILTIN_EXCEPTION_KEYS)
            for start, end in _word_runs(text)
            if text[start:end].casefold() in self.keys
            and all(unchanged[start:end])
            and _stands_alone(text, start, end)
            and _stands_alone(value, origin[start], origin[end - 1] + 1)
        ]

    def part_runs(self, value: str) -> list[tuple[int, int, bool]]:
        """Return the exception runs of one field value as spans of the value
        itself (before NFKC); none when tracing is not certain."""
        if not self.keys:
            return []
        traced = _nfkc_traced(value)
        if traced is None:
            return []
        text, unchanged, origin = traced
        return [
            (origin[start], origin[end - 1] + 1, builtin)
            for start, end, builtin in self._runs(value, text, unchanged, origin)
        ]


def _nfkc_traced(value: str) -> tuple[str, list[bool], list[int]] | None:
    """Return the NFKC of `value` with, per character, whether NFKC left its
    source character unchanged and the index of that source character; None
    when NFKC of the characters one by one differs from NFKC of the whole."""
    chars: list[str] = []
    unchanged: list[bool] = []
    origin: list[int] = []
    for index, char in enumerate(value):
        mapped = unicodedata.normalize("NFKC", char)
        chars.extend(mapped)
        unchanged.extend([mapped == char] * len(mapped))
        origin.extend([index] * len(mapped))
    text = "".join(chars)
    if text != unicodedata.normalize("NFKC", value):
        return None
    return text, unchanged, origin


class _Joined:
    """The joined text of several field values, matched with exceptions.

    An exception may excuse a hit only when the hit and the exception word
    around it both lie inside the value of one field, and the exception word
    is a whole word there (`_Excuser`) with real boundaries in the joined
    text too (`_alone_in_join`: the neighbouring value's adjacent visible
    character is not a letter, digit or combining mark). Hits are found in
    two readings, the values joined by one space and the tight reading
    (`_tight_separators`), and traced back to the characters of the values;
    a hit that spans two values, or one that cannot be traced, counts. A
    hit found in either reading counts unless it is excused there.
    """

    def __init__(self, parts: Sequence[str], excuser: _Excuser) -> None:
        self._parts = tuple(parts)
        self._excuser = excuser
        # The exception spans, as (part, start, end, builtin) in the part's own
        # text; found on the first hit, so a text without hits costs nothing
        # more than the matching without exceptions.
        self._spans: list[tuple[int, int, int, bool]] | None = None
        # Per reading: its forms, where each part starts in it, and its traced
        # forms (built only when a hit there needs an exception span).
        self._readings: list[tuple[str, tuple[str, ...], list[int]]] = []
        self._traced: dict[str, list[tuple[str, list[int]]] | None] = {}
        seen: set[str] = set()
        for separators in ([" "] * len(parts), _tight_separators(parts)):
            separators[0] = ""
            text, starts = _join(parts, separators)
            if text in seen:
                continue
            seen.add(text)
            self._readings.append((text, _forms(text), starts))

    def _exception_spans(self) -> list[tuple[int, int, int, bool]]:
        if self._spans is None:
            parts = self._parts
            raw, raw_starts = _join(parts, [""] * len(parts))
            spans: list[tuple[int, int, int, bool]] = []
            for index, part in enumerate(parts):
                for start, end, builtin in self._excuser.part_runs(part):
                    offset = raw_starts[index]
                    bounds = (offset, offset + len(part))
                    if _alone_in_join(raw, offset + start, offset + end, bounds):
                        spans.append((index, start, end, builtin))
            self._spans = spans
        return self._spans

    def counts(self, word_forms: tuple[str, ...], *, builtin: bool) -> bool:
        """Return True when a hit of the word counts in either reading."""
        for text, field_forms, starts in self._readings:
            if not _forms_match(word_forms, field_forms):
                continue
            usable = [
                (starts[i] + a, starts[i] + b)
                for i, a, b, own in self._exception_spans()
                if builtin or not own
            ]
            if not usable:
                return True
            if text not in self._traced:
                self._traced[text] = _original_forms(text)
            traced = self._traced[text]
            if traced is None:
                return True
            for form, sources in traced:
                for word_form in word_forms:
                    if word_form and _hit_outside(form, sources, word_form, usable):
                        return True
        return False


def _original_forms(value: str) -> list[tuple[str, list[int]]] | None:
    """Return the three forms of `value`, each character with the index of the
    character of `value` (before NFKC) it comes from; None when not certain."""
    traced = _nfkc_traced(value)
    if traced is None:
        return None
    text, _, origin = traced
    forms = _traced_forms(text)
    if forms is None:
        return None
    return [(form, [origin[i] for i in sources]) for form, sources in forms]


def _part_at(starts: Sequence[int], index: int) -> int:
    """The index of the last part that starts at or before `index`."""
    return max(sum(1 for start in starts if start <= index) - 1, 0)


def _id_name(resource_id: str) -> str:
    """Return the part of a resource id after ``:id/`` (all of it if none)."""
    _, separator, name = resource_id.partition(_ID_SEPARATOR)
    return name if separator else resource_id


def _fields(element: Element) -> tuple[tuple[str, str], ...]:
    return (
        ("text", element.text),
        ("content_desc", element.content_desc),
        ("hint", element.hint),
        ("resource_id", _id_name(element.resource_id)),
    )


class _Matcher:
    """Deny words with their forms computed once, and the exception words."""

    def __init__(self, words: Iterable[str], exceptions: Iterable[str] = ()) -> None:
        self._words = tuple(
            (word, _forms(word), _is_builtin_word(word)) for word in words
        )
        self._excuser = _Excuser(_exception_keys(exceptions))
        self._cache: dict[str, str | None] = {}
        self._plain_cache: dict[str, str | None] = {}

    @property
    def excuses(self) -> bool:
        """True when there are exception words."""
        return bool(self._excuser.keys)

    def surviving_owners(
        self, parts: Sequence[str], owners: Sequence[int], word: str
    ) -> list[frozenset[int]] | None:
        """Return, for every occurrence of the deny `word` in the joined text of
        `parts` that is not excused by an exception word (the occurrences that
        count, see `_Joined`), the owners (`owners[k]` owns `parts[k]`) of the
        values it touches; None when an occurrence cannot be traced, so the
        caller must count it as touching everything."""
        word_forms = _forms(word)
        builtin = _is_builtin_word(word)
        joined = _Joined(parts, self._excuser)
        spans = joined._exception_spans() if self._excuser.keys else []  # noqa: SLF001
        found: list[frozenset[int]] = []
        for text, field_forms, starts in joined._readings:  # noqa: SLF001
            if not _forms_match(word_forms, field_forms):
                continue
            usable = [
                (starts[i] + a, starts[i] + b)
                for i, a, b, own in spans
                if builtin or not own
            ]
            traced = _original_forms(text)
            if traced is None:
                return None
            for form, sources in traced:
                for word_form in word_forms:
                    if not word_form:
                        continue
                    at = form.find(word_form)
                    while at != -1:
                        span = sources[at : at + len(word_form)]
                        low, high = min(span), max(span)
                        if not any(a <= low and high < b for a, b in usable):
                            first, last = _part_at(starts, low), _part_at(starts, high)
                            found.append(frozenset(owners[first : last + 1]))
                        at = form.find(word_form, at + 1)
        return found

    def match_joined(self, parts: Sequence[str]) -> str | None:
        """Return the first deny word that counts in the joined text of the
        field values `parts` (see `_Joined`), or None."""
        joined = _Joined(parts, self._excuser)
        return next(
            (
                word
                for word, word_forms, builtin in self._words
                if joined.counts(word_forms, builtin=builtin)
            ),
            None,
        )

    def match(self, element: Element) -> tuple[str, str] | None:
        """Return ``(word, field)`` of the first match in the element.

        The resource id is matched without the exception words.
        """
        for field, value in _fields(element):
            if value:
                word = self.match_text(value, excuse=field != "resource_id")
                if word is not None:
                    return word, field
        return None

    def match_text(self, value: str, *, excuse: bool = True) -> str | None:
        """Return the first deny word found in one string, or None.

        With `excuse` (the default), a word whose every hit lies inside an
        exception word of the string that applies to it does not count
        (`_Excuser`).
        """
        excuse = excuse and bool(self._excuser.keys)
        cache = self._cache if excuse else self._plain_cache
        if value in cache:
            return cache[value]
        field_forms = _forms(value)
        found = next(
            (
                w
                for w, word_forms, builtin in self._words
                if _forms_match(word_forms, field_forms)
                and (
                    not excuse
                    or self._excuser.counts(value, word_forms, builtin=builtin)
                )
            ),
            None,
        )
        if len(value) <= _CACHED_VALUE_LENGTH:
            cache[value] = found
        return found


class _PhraseMatcher:
    """Irreversibility phrases, matched like deny words in three fields."""

    def __init__(self, phrases: Iterable[str]) -> None:
        self._matcher = _Matcher(p.translate(_APOSTROPHES) for p in phrases)

    def find(self, elements: Iterable[Element]) -> tuple[str, str, str] | None:
        """Return ``(phrase, field, element id)`` of the first match, or None."""
        for element in elements:
            for field, value in (
                ("text", element.text),
                ("content_desc", element.content_desc),
                ("hint", element.hint),
            ):
                if value:
                    phrase = self._matcher.match_text(value.translate(_APOSTROPHES))
                    if phrase is not None:
                        return phrase, field, element.id
        return None


def deny_word_in(
    element: Element, words: Iterable[str], *, exceptions: Iterable[str] = ()
) -> tuple[str, str] | None:
    """Return ``(word, field)`` of the first deny word found in the element.

    The fields are ``text``, ``content_desc``, ``hint`` and the part of
    ``resource_id`` after ``:id/``. Only the element itself is looked at.
    `exceptions` are exception words (see `word_matches`); they apply to the
    text, content description and hint, not to the resource id.

    Raises:
        TypeError: `exceptions` is one string, not a collection of words.
        ValueError: An exception word is not one word of letters and digits
            (or holds an invisible character), or equals a deny word.
    """
    words = tuple(words)
    exceptions = tuple(_check_collection(exceptions))
    matcher = _Matcher(words, exceptions)
    _check_no_conflict(exceptions, words)
    return matcher.match(element)
