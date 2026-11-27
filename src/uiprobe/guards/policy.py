"""The policy, the edge areas and the decision types."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from uiprobe.guards.constants import (
    _EDGE_SIDES,
    _UTF8_BOM,
    _UTF16_BOMS,
    API36_BOTTOM_PX,
    API36_SIDE_PX,
    API36_TOP_PX,
    CONFIRM_WORDS,
    DEFAULT_DENY_EXCEPTIONS,
    DEFAULT_DENY_WORDS,
    DEFAULT_SCROLL_LISTS,
    IRREVERSIBLE_PHRASES,
    Reason,
)
from uiprobe.guards.words import (
    _check_no_conflict,
    _check_word,
    _confirm_form,
    _deny_forms,
    _except_phrase_key,
    _exception_conflict,
    _exception_keys,
    _forms,
    _forms_match,
)
from uiprobe.model import MIN_VISIBLE_PX, Bounds

if TYPE_CHECKING:
    from collections.abc import Iterable

    from uiprobe.model import Element

# -- policy ----------------------------------------------------------------------


# The package of the lock screen and the notification shade.
SYSTEM_UI: Final = "com.android.systemui"
# Home apps (launchers) that are refused without asking the device: AOSP's,
# Google's Pixel launcher and Samsung's One UI Home.
FIXED_HOME_APPS: Final = (
    "com.android.launcher3",
    "com.google.android.apps.nexuslauncher",
    "com.sec.android.app.launcher",
)


_PACKAGE: Final = re.compile(
    r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", re.ASCII
)
_MAX_PACKAGE: Final = 255


def _package_name(item: object) -> str:
    """`item` when it follows the Android package rule (the rule of
    `uiprobe.crawl_preview.check_package`, which this module cannot import)."""
    if not isinstance(item, str):
        msg = f"also_target: package must be a str, not {type(item).__name__}"
        raise AlsoTargetError(msg)
    if len(item) > _MAX_PACKAGE or not _PACKAGE.fullmatch(item):
        shown = item if len(item) <= 80 else item[:80] + "..."
        msg = (
            f"also_target: not a valid package name: {shown!r} (two or more"
            " segments separated by dots, each an ASCII letter followed by"
            f" letters, digits or underscores; at most {_MAX_PACKAGE} characters)"
        )
        raise AlsoTargetError(msg)
    return item


class AlsoTargetError(ValueError):
    """A package listed in ``also_target`` is refused (nothing was sent)."""


def check_also_target(target: str | None, listed: Iterable[str]) -> tuple[str, ...]:
    """Check the list before any device is contacted; return it as a tuple.

    `target` None skips the check against the target app (`Policy` does not
    know it; `CrawlEngine`, `preview` and `crawl` do).

    Raises:
        AlsoTargetError: An entry is not a valid package name, is listed twice,
            is `target` itself, is the system UI or is in `FIXED_HOME_APPS`.
    """
    if isinstance(listed, str):
        msg = "also_target must be a list of package names, not one string"
        raise AlsoTargetError(msg)
    found: list[str] = []
    for item in listed:
        package = _package_name(item)
        if package in found:
            msg = f"also_target lists {package} more than once"
            raise AlsoTargetError(msg)
        if package == target:
            msg = (
                f"also_target lists {package}, which is the target app itself;"
                " remove it from the list"
            )
            raise AlsoTargetError(msg)
        if package == SYSTEM_UI:
            msg = (
                f"also_target cannot list {package}: it is the system UI (lock"
                " screen, notification shade); nothing was pressed"
            )
            raise AlsoTargetError(msg)
        if package in FIXED_HOME_APPS:
            msg = (
                f"also_target cannot list {package}: it is a home app"
                " (launcher); nothing was pressed"
            )
            raise AlsoTargetError(msg)
        found.append(package)
    return tuple(found)


def edge_areas(
    display: tuple[int, int],
    *,
    top: int = 24,
    bottom: int = 0,
    left: int = 0,
    right: int = 0,
) -> tuple[Bounds, ...]:
    """Build the screen-edge rectangles a press point must not fall in.

    Each size is in pixels; a size of 0 adds no rectangle.

    Raises:
        ValueError: A size is negative.
    """
    width, height = display
    if min(top, bottom, left, right) < 0:
        msg = "edge sizes must not be negative"
        raise ValueError(msg)
    areas = (
        (top, Bounds(0, 0, width, top)),
        (bottom, Bounds(0, height - bottom, width, height)),
        (left, Bounds(0, 0, left, height)),
        (right, Bounds(width - right, 0, width, height)),
    )
    return tuple(area for size, area in areas if size)


def api36_fallback_areas(display: tuple[int, int]) -> tuple[Bounds, ...]:
    """Return `edge_areas` with the sizes measured on an API 36 emulator."""
    return edge_areas(
        display,
        top=API36_TOP_PX,
        bottom=API36_BOTTOM_PX,
        left=API36_SIDE_PX,
        right=API36_SIDE_PX,
    )


@dataclass(frozen=True)
class Policy:
    """What the caller allows: the word lists, the unlabeled, number-only,
    toggle, row and edge rules, and whether a crawl may press BACK on a screen
    of another app.

    ``avoid`` holds the screen-edge rectangles (see `edge_areas`) that a press
    point must not fall in. The default None is the safe default: `judge` uses
    ``api36_fallback_areas(screen.display)`` for each screen. An explicit tuple,
    including ``()`` (no edge rule), is used as given.

    ``skip_number_only`` (default True) applies the ``number_only`` rule: a
    candidate whose label is only a number is not pressed. ``skip_toggles``
    (default True) applies the ``toggle`` rule: a candidate that is, or
    holds, a checkable element is not pressed. False turns the rule off.
    ``skip_two_target_rows`` (default True) applies the ``two_target_row``
    rule: a candidate whose descendants hold an element with a resource id
    ending in ``:id/two_target_divider`` (the divider of an AOSP Settings
    two-target row) is not pressed. ``skip_rows_with_buttons`` (default
    False) applies the ``row_with_button`` rule: a candidate with a clickable
    descendant is not pressed (it also refuses ordinary list rows with their
    own button). When both match, only ``two_target_row`` is given. Only the
    row is refused; its descendants are judged on their own.
    ``skip_navigate_up`` (default True) applies the ``navigate_up`` rule: the
    up button of a toolbar (found by its class, its parent and its resource
    id, or by the label ``Navigate up``) is not pressed, so a crawl presses
    the rows of a sub-page first and goes back up with its own BACK.
    ``skip_sliders`` (default True) applies the ``slider`` rule: a candidate
    that is or holds an adjustable control (a class ending in ``SeekBar``,
    ``RatingBar`` or ``Slider``), or whose press point lies on one, is not
    pressed, because a tap moves the control and changes a value.
    ``refuse_dialog_confirm`` (default True) applies the ``dialog_confirm_button``
    reason, for a confirm label and for the dialog's positive button
    (``android:id/button1``); False drops only that reason (the
    ``confirm_button`` reason, a deny word on the screen, stays).

    ``skip_irreversible`` (default True) applies the ``irreversible_screen``
    reason with the phrases of ``irreversible_phrases``; False turns it off.

    ``deny_exceptions`` (default `DEFAULT_DENY_EXCEPTIONS`, which is ``()``)
    are the exception words: a deny-word hit in a text, content description
    or hint (or in a joined text) that lies completely inside a whole word
    equal to one of them does not count (see `word_matches`), so an element
    holding the word is pressed unless something else refuses it. They
    excuse every deny word (a built-in exception word, of which there are
    none, would excuse only built-in deny words). The screen-wide search
    behind ``confirm_button`` never uses them. An exception word must not
    equal a deny word (any of its forms).

    ``except_phrases`` (default ``()``) are exception phrases (the settings
    file's ``[deny] except_phrases``). A plain clickable row (not
    long-clickable, checkable, scrollable, editable or a text or password
    field; nothing inside it clickable or any of those; at most 500 elements;
    not inside another clickable; its tap point held by it or something
    inside it; the last element drawn at its tap point and at its name's
    middle is in the row, except Samsung's empty ``round_corner`` view; no
    other interactive element drawn over them) whose only reason is
    ``deny_word``, that has only its name as text (one element with any text,
    description or hint, the last two saying what the text says), whose name
    has some area on the display with its middle inside the row and equals a
    phrase after `normalize`, is judged once more with only that text replaced;
    with no reason left it is let through, and its decision
    carries the phrase as written in ``except_phrase``. With more than 8
    structurally eligible rows on a screen (counted before the reasons) no
    phrase applies. The wheel scroll never turns over such an element
    (`Judgement.unsafe`). Each phrase is 1 to 80 characters, with no control
    or invisible character and at least one letter, and phrases must differ
    after `normalize`. `with_deny_words` drops a phrase that holds an added
    word.

    ``back_on_other_screens`` (default False) is read by the pressing crawl
    only, not by `judge`. True lets the crawl press BACK while a screen of
    another app is in front (up to 2 in a row, then a relaunch), and that BACK
    reaches the owner of the focused window, whichever app that is. With the
    default nothing is sent to such a screen: the target app is relaunched
    (force-stop, then start), and when a 4th relaunch in a row without a sent
    tap would be needed the crawl stops.

    ``check_tap_window`` (default True) is read by the pressing crawl only.
    False lets a tap go out without reading which window takes a touch at its
    point; then a window of another app that takes touches there without
    taking the input focus (an overlay drawn over other apps, a floating
    button) gets the tap. With the default the crawl reads that window right
    before each tap and sends the tap only to a window of the app; when it
    cannot be read, nothing is sent.

    ``replay_path`` (default True) is read by the pressing crawl only, not by
    `judge`. True lets the crawl, after a relaunch, press again the rows that it
    pressed before to get back down to a screen that still has rows to try (a
    path replay), and counts a row seen with and without a badge (an element
    whose resource id contains ``badge``) as one row. False gives the crawl
    that does neither.

    ``also_target`` (default ``()``) lists packages besides the target app whose
    screens `judge` and the crawl engine treat as the target's own: a screen
    whose package and foreground app are both listed is judged exactly like
    the target's screens, with every rule. Permission screens stay refused
    (``permission_screen``) and an element of another package than the screen's
    stays refused (``outside_target_app``). With the empty default nothing
    differs from a policy without the field.

    ``scroll_lists`` (default `DEFAULT_SCROLL_LISTS`, False) is read by the
    pressing crawl only, not by `judge`. True lets the crawl turn a mouse wheel
    over a list (content further down comes up) on a screen with nothing left
    to press, to reach the rows below the fold, when the device's own ``input``
    usage lists the wheel scroll (see `uiprobe.guards.scroll_target` for where
    the wheel is turned and what it is never turned over). False gives the
    crawl that sends no wheel input and reads no usage.

    ``edges`` holds the sizes in pixels ``(top, bottom, left, right)`` that
    `judge` turns into `edge_areas` of each screen's display when ``avoid`` is
    None; None keeps the API 36 sizes. An explicit ``avoid`` wins over
    ``edges``. ``min_visible_px`` (default `MIN_VISIBLE_PX`) is the smallest
    width and height of the on-screen part of an element that `judge` counts as
    visible; it applies to the judging only, ``Screen.tap_point``, ``Screen.find`` and
    ``Screen.visible_bounds`` keep the fixed `MIN_VISIBLE_PX`.

    Raises:
        ValueError: An ``also_target`` entry is refused (`AlsoTargetError`: not
            a package name, listed twice, the system UI or a fixed home app), or
            a deny word, confirm word or irreversible phrase is empty
            after normalization, or a confirm word is empty after the label
            cleaning (invisible characters, normalization, surrounding
            punctuation), or an exception word is not one word of letters and
            digits or equals a deny word, or
            ``edges`` is not four non-negative integers, or ``min_visible_px``
            is not an integer of at least 1.
        TypeError: ``edges`` is not a sequence, ``deny_exceptions`` is one
            string, or ``back_on_other_screens``, ``check_tap_window`` or
            ``replay_path`` or ``scroll_lists`` is not a bool, or ``also_target``
            is not a tuple of strings.
    """

    deny_words: tuple[str, ...] = DEFAULT_DENY_WORDS
    confirm_words: tuple[str, ...] = CONFIRM_WORDS
    tap_unlabeled: bool = False
    avoid: tuple[Bounds, ...] | None = None
    skip_number_only: bool = True
    skip_toggles: bool = True
    refuse_dialog_confirm: bool = True
    edges: tuple[int, int, int, int] | None = None
    min_visible_px: int = MIN_VISIBLE_PX
    irreversible_phrases: tuple[str, ...] = IRREVERSIBLE_PHRASES
    skip_irreversible: bool = True
    back_on_other_screens: bool = False
    check_tap_window: bool = True
    deny_exceptions: tuple[str, ...] = DEFAULT_DENY_EXCEPTIONS
    skip_two_target_rows: bool = True
    skip_rows_with_buttons: bool = False
    skip_navigate_up: bool = True
    skip_sliders: bool = True
    replay_path: bool = True
    also_target: tuple[str, ...] = ()
    scroll_lists: bool = DEFAULT_SCROLL_LISTS
    except_phrases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.back_on_other_screens) is not bool:
            msg = "back_on_other_screens must be a bool"
            raise TypeError(msg)
        if type(self.check_tap_window) is not bool:
            msg = "check_tap_window must be a bool"
            raise TypeError(msg)
        if type(self.replay_path) is not bool:
            msg = "replay_path must be a bool"
            raise TypeError(msg)
        if not isinstance(self.also_target, tuple | list) or not all(
            type(name) is str for name in self.also_target
        ):
            msg = "also_target must be a tuple of package names"
            raise TypeError(msg)
        if self.also_target:
            # The static rules (package format and length, duplicates, the
            # system UI, the fixed home apps); a refused entry cannot be built.
            # The target itself is checked where the target is known.
            check_also_target(None, self.also_target)
        if type(self.scroll_lists) is not bool:
            msg = "scroll_lists must be a bool"
            raise TypeError(msg)
        for word in (*self.deny_words, *self.confirm_words, *self.irreversible_phrases):
            _check_word(word)
        _check_except_phrases(self.except_phrases)
        _exception_keys(self.deny_exceptions)
        _check_no_conflict(self.deny_exceptions, self.deny_words)
        if self.edges is not None and (
            len(self.edges) != _EDGE_SIDES
            or any(type(size) is not int or size < 0 for size in self.edges)
        ):
            msg = "edges must be four non-negative integers: top, bottom, left, right"
            raise ValueError(msg)
        if type(self.min_visible_px) is not int or self.min_visible_px < 1:
            msg = "min_visible_px must be an integer of at least 1"
            raise ValueError(msg)
        for word in self.confirm_words:
            if not _confirm_form(word):
                msg = f"confirm word is empty after cleaning: {word!r}"
                raise ValueError(msg)

    def with_deny_words(self, extra: Iterable[str]) -> Policy:
        """Return a copy with `extra` words (for example ``--deny``) added.

        An exception word that equals an added word is dropped from the copy,
        so the added word blocks as given.

        Raises:
            TypeError: `extra` is one string, not a collection of words.
            ValueError: A word is empty after normalization.
        """
        if isinstance(extra, str):
            msg = "extra deny words must be a collection of words, not one string"
            raise TypeError(msg)
        added = tuple(_check_word(word) for word in extra)
        merged = tuple(dict.fromkeys((*self.deny_words, *added)))
        added_forms = _deny_forms(added)
        exceptions = tuple(
            exception
            for exception in self.deny_exceptions
            if _exception_conflict(exception, added_forms) is None
        )
        phrases = tuple(
            phrase
            for phrase in self.except_phrases
            if not any(_forms_match(_forms(word), _forms(phrase)) for word in added)
        )
        return Policy(
            deny_words=merged,
            confirm_words=self.confirm_words,
            tap_unlabeled=self.tap_unlabeled,
            avoid=self.avoid,
            skip_number_only=self.skip_number_only,
            skip_toggles=self.skip_toggles,
            refuse_dialog_confirm=self.refuse_dialog_confirm,
            edges=self.edges,
            min_visible_px=self.min_visible_px,
            irreversible_phrases=self.irreversible_phrases,
            skip_irreversible=self.skip_irreversible,
            back_on_other_screens=self.back_on_other_screens,
            check_tap_window=self.check_tap_window,
            deny_exceptions=exceptions,
            skip_two_target_rows=self.skip_two_target_rows,
            skip_rows_with_buttons=self.skip_rows_with_buttons,
            skip_navigate_up=self.skip_navigate_up,
            skip_sliders=self.skip_sliders,
            replay_path=self.replay_path,
            also_target=self.also_target,
            scroll_lists=self.scroll_lists,
            except_phrases=phrases,
        )


def _check_except_phrases(phrases: object) -> None:
    """Check ``Policy.except_phrases``: a tuple of valid phrases, distinct
    after `normalize`."""
    if not isinstance(phrases, tuple | list) or not all(
        type(phrase) is str for phrase in phrases
    ):
        msg = "except_phrases must be a tuple of strings"
        raise TypeError(msg)
    seen: set[str] = set()
    for phrase in phrases:
        key = _except_phrase_key(phrase)
        if key in seen:
            msg = f"except_phrases lists {phrase!r} twice (after normalization)"
            raise ValueError(msg)
        seen.add(key)


def parse_deny_file(data: bytes) -> tuple[str, ...]:
    """Return the words of a deny file.

    The file is strict UTF-8; a leading UTF-8 BOM is accepted. Lines end at LF;
    a CR is accepted only directly before the LF (CRLF). Every raw line is
    checked before it is stripped or skipped as a comment: a C0 control
    character other than TAB, a DEL or another control character anywhere in a
    line, comment lines included, is an error. TAB counts as whitespace. Blank
    lines and lines whose first non-space character is ``#`` are skipped, and
    each word is stripped.

    Raises:
        ValueError: The data starts with a UTF-16 BOM (the message asks for a
            UTF-8 file), is not valid UTF-8, or a line holds a NUL (the message
            says it looks like UTF-16) or another control character (for
            example a bare CR line end); the message names the line.
    """
    if data.startswith(_UTF16_BOMS):
        msg = "the file is UTF-16; save it as UTF-8"
        raise ValueError(msg)
    data = data.removeprefix(_UTF8_BOM)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        msg = f"line {line} is not valid UTF-8; save the file as UTF-8"
        raise ValueError(msg) from error
    words: list[str] = []
    lines = text.split("\n")
    for number, raw in enumerate(lines, start=1):
        # Only a CR directly before an LF is a line end; the last piece has no LF.
        line_text = raw.removesuffix("\r") if number < len(lines) else raw
        if "\x00" in line_text:
            msg = f"line {number} contains NUL; it looks like UTF-16, save as UTF-8"
            raise ValueError(msg)
        for char in line_text:
            if unicodedata.category(char) == "Cc" and char != "\t":
                msg = (
                    f"line {number} contains the control character "
                    f"U+{ord(char):04X} (a bare CR line end?)"
                )
                raise ValueError(msg)
        stripped = line_text.strip()
        if stripped and not stripped.startswith("#"):
            words.append(stripped)
    return tuple(words)


# -- decisions ---------------------------------------------------------------------


@dataclass(frozen=True)
class Detail:
    """What matched for one reason.

    ``word``, ``field`` and ``element_id`` are set for ``deny_word`` (the word,
    the field and the element where it was found) and ``confirm_button`` (the
    deny word found on the screen); ``note`` is free text. A ``deny_word``
    detail may have field ``joined``: the word was found in the joined text of
    a subtree, and ``element_id`` names the subtree root. A ``confirm_button``
    detail with field ``joined`` and no ``element_id`` found the word in the
    joined text of the whole screen. When a reason comes from a hit target
    other than the candidate (``outside_target_app``, ``permission_screen``,
    ``text_input``, ``password_field``), ``element_id`` is that element; for
    the confirm reasons the hit target is named in ``note``. For ``covered``,
    ``element_id`` is the element on top of the press point. For
    ``number_only``, ``note`` and ``word`` both hold the cleaned label; for
    ``toggle``, ``element_id`` is the first checkable element in document
    order; for ``two_target_row``, the first divider element in document
    order; for ``row_with_button``, the first clickable descendant in
    document order; for ``navigate_up``, ``note`` names the sign that
    matched and ``element_id`` the candidate; for ``slider``, ``element_id``
    is the first slider of the subtree in document order (``note``
    ``subtree``), else the first slider on the screen whose part on the
    display holds the press point (``note`` ``press point``). For
    ``irreversible_screen``,
    ``word`` is the phrase, ``field`` the field and ``element_id`` the
    element where it was found.
    """

    reason: Reason
    note: str = ""
    word: str = ""
    field: str = ""
    element_id: str = ""

    def to_dict(self) -> dict[str, str]:
        """Return the JSON form: ``reason``, then ``word``, ``field``,
        ``element_id`` and ``note`` when set (the form of ``crawl --json`` and
        of ``skipped.jsonl``)."""
        optional = {
            "word": self.word,
            "field": self.field,
            "element_id": self.element_id,
            "note": self.note,
        }
        entry: dict[str, str] = {"reason": self.reason}
        entry.update({key: value for key, value in optional.items() if value})
        return entry


@dataclass(frozen=True)
class Decision:
    """The verdict on one candidate.

    ``point`` is the center of the visible part, rounded down like
    ``Screen.find``, or None without a visible part (under 8 x 8 pixels counts
    as none). ``reasons`` follow the
    order of `REASONS`; empty means the element may be pressed.
    ``except_phrase`` is the phrase of ``Policy.except_phrases`` (as written)
    that let this element through a deny word; it is ``""`` otherwise, always
    with no phrases in the policy.
    """

    element: Element
    point: tuple[int, int] | None
    reasons: tuple[Reason, ...] = ()
    details: tuple[Detail, ...] = ()
    except_phrase: str = ""

    @property
    def pressable(self) -> bool:
        """True when no reason applies."""
        return not self.reasons


@dataclass(frozen=True)
class Judgement:
    """One `Decision` per candidate, in document order.

    ``dialog`` says that the screen is a dialog (see ``dialog_confirm_button``)
    and ``irreversible`` that a phrase of ``Policy.irreversible_phrases`` is on
    it. Both are facts about the whole screen (even with no candidate), are
    False for a screen too large to judge, and are not compared. ``edges`` are
    the screen-edge rectangles that gave ``screen_edge`` to a press point on
    this screen (empty for a screen too large to judge; not compared): the
    crawl's wheel point must not lie in one either. ``unsafe`` holds the ids of
    every element, clickable or not, that is of another package than the
    screen's target or has a deny word or irreversibility phrase in its own text,
    description or hint (empty for a screen too large to judge; not compared).
    """

    decisions: tuple[Decision, ...]
    dialog: bool = field(default=False, compare=False)
    irreversible: bool = field(default=False, compare=False)
    edges: tuple[Bounds, ...] = field(default=(), compare=False)
    unsafe: frozenset[str] = field(default=frozenset(), compare=False)

    @property
    def pressable(self) -> tuple[Decision, ...]:
        """The decisions without a reason."""
        return tuple(d for d in self.decisions if not d.reasons)

    @property
    def skipped(self) -> tuple[Decision, ...]:
        """The decisions with at least one reason."""
        return tuple(d for d in self.decisions if d.reasons)
