"""Judging a screen: which elements may be pressed, and why not."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from uiprobe.guards.constants import (
    _ACTION_BAR_ID,
    _CHECKABLE,
    _CLICKABLE,
    _DIALOG_POSITIVE_ENTRY,
    _DIALOG_POSITIVE_ID,
    _DISABLED,
    _MIN_JOINED_PARTS,
    _NAVIGATE_UP,
    _PASSWORD,
    _TOOLBAR_CLASS,
    _TWO_TARGET_DIVIDER,
    _UP_CLASSES,
    _UP_IDS,
    DIALOG_ONLY_CONFIRM_WORDS,
    MAX_ELEMENTS,
    PERMISSION_PACKAGES,
    Reason,
)
from uiprobe.guards.matching import _Matcher, _PhraseMatcher
from uiprobe.guards.policy import (
    Decision,
    Detail,
    Judgement,
    Policy,
    api36_fallback_areas,
    edge_areas,
)
from uiprobe.guards.scroll import _is_decoration, elements_at
from uiprobe.guards.tree import (
    _CONFIRM,
    _DENY,
    _EMPTY_AGG,
    _EXACT,
    _JOINED,
    _NONE,
    _PASSWORD_AT,
    _PERM,
    _PKG,
    _TEXT,
    ROW_NAME_MEMBERS,
    _add_part,
    _Agg,
    _Grid,
    _is_dialog,
    _is_number_only,
    _is_slider,
    _is_text_field,
    _merge,
    _Tree,
)
from uiprobe.guards.words import (
    _clean,
    _confirm_form,
    _has_content,
    _is_japanese_letter,
    _is_mark,
    _strip_invisible,
    normalize,
)
from uiprobe.model import Bounds  # noqa: TC001 (a runtime import in the former guards.py)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from uiprobe.model import Element, Screen


class _Judge:
    """State shared by the decisions of one screen."""

    def __init__(
        self,
        screen: Screen,
        target_package: str,
        policy: Policy,
        foreground: str | None,
    ) -> None:
        self.screen = screen
        self.policy = policy
        # The exception phrases by their normalized form (see `_excusing_phrase`).
        self._phrases = {normalize(phrase): phrase for phrase in policy.except_phrases}
        self.tree = _Tree(screen)
        self.elements = screen.elements
        self.matcher = _Matcher(policy.deny_words, policy.deny_exceptions)
        self.confirm = frozenset(_confirm_form(w) for w in policy.confirm_words)
        # The words that count only as the whole label of a button on a dialog
        # (`Policy.dialog_exact_labels`). They take no part in the confirm
        # matching below; `exact_label` reads them on their own.
        self._exact = frozenset(_confirm_form(w) for w in policy.dialog_exact_labels)
        # The lengths of the confirm words, shortest first: only a start of a
        # label of one of these lengths can be a confirm word.
        self._dialog_only = frozenset(
            _confirm_form(w) for w in DIALOG_ONLY_CONFIRM_WORDS
        )
        self._confirm_lengths = sorted({len(form) for form in self.confirm})
        # A confirm word that ends in a letter of the Japanese script can be
        # followed by anything; the others need a boundary (`_is_confirm`).
        self._open_ended = frozenset(
            form for form in self.confirm if _is_japanese_letter(form[-1])
        )
        # A screen of a listed also_target package is judged as the target's
        # own: its package is then the target of this screen (an element of
        # any other package, the app's own included, is outside it).
        self.target = (
            foreground
            if foreground is not None
            and foreground != target_package
            and foreground in policy.also_target
            else target_package
        )
        self._confirm_forms: dict[str, str] = {}
        self.visible = [self._visible_part(e) for e in self.elements]
        self.clickable = [_CLICKABLE in e.flags for e in self.elements]
        self.meaningful = [e.is_meaningful for e in self.elements]
        self._tops: dict[tuple[int, int], int] = {}
        self._cell_tops: dict[int, int] = {}
        self._avoid = self._edge_rectangles()
        self.screen_note = ""
        if foreground is None:
            self.screen_note = "foreground app unknown"
        elif foreground != self.target:
            self.screen_note = f"foreground is {foreground}"
        elif screen.package != self.target:
            self.screen_note = f"screen package is {screen.package}"
        self.dialog = _is_dialog(self.tree)
        self._positive = [
            i
            for i, e in enumerate(self.elements)
            if self._is_positive_id(e.resource_id)
        ]
        self._positive_set = frozenset(self._positive)
        self._screen_hit: Detail | Literal[False] | None = False
        self._irreversible: Detail | Literal[False] | None = False
        self._summarize()
        self._joined_hit: dict[int, str | None] = {}
        # Per clickable root: the first clickable element below it whose own
        # joined text holds a deny word (only used with exception words).
        self._joined_below: dict[int, int] = {}
        self._labels: dict[int, str] = {}
        self._wide_labels: dict[int, str] = {}
        self._exact_labels: dict[int, str] = {}
        self._joined_label: dict[int, tuple[str, ...]] = {}
        self._contrib: dict[int, _Agg] = {}
        self._everywhere: _Agg | None = None
        self._cells: dict[int, _Agg] = {}
        self._points: dict[tuple[int, int], _Agg] = {}
        # The hit targets keep slivers: a part under 8 pixels is not a visible
        # element (see `visible`) but a press that lands on it still reaches it.
        parts = (screen._clip(e) for e in self.elements)  # noqa: SLF001
        rects = [(i, rect) for i, rect in enumerate(parts) if rect is not None]
        self.grid = _Grid(screen.display, rects)

    def _visible_part(self, element: Element) -> Bounds | None:
        """Return the part of `element` on the display, or None for a sliver."""
        clipped = self.screen._clip(element)  # noqa: SLF001
        if clipped is None:
            return None
        if min(clipped.width, clipped.height) < self.policy.min_visible_px:
            return None
        return clipped

    def _edge_rectangles(self) -> tuple[Bounds, ...]:
        policy = self.policy
        if policy.avoid is not None:
            return policy.avoid
        if policy.edges is None:
            return api36_fallback_areas(self.screen.display)
        top, bottom, left, right = policy.edges
        return edge_areas(
            self.screen.display, top=top, bottom=bottom, left=left, right=right
        )

    def _summarize(self) -> None:
        """Compute, in a few linear passes over the tree, what the decisions
        need to know about every element and its subtree.
        """
        count = len(self.elements)
        # One pass over the tree, children before parents: the first deny
        # hit in an element's own fields and in its subtree, whether the
        # subtree holds a password flag, and whether it has a visible label.
        self.own_hit = [self.matcher.match(e) for e in self.elements]
        self.sub_deny = [i if self.own_hit[i] else _NONE for i in range(count)]
        self.sub_password = [_PASSWORD in e.flags for e in self.elements]
        self.sub_labeled = [self._is_labeled(e) for e in self.elements]
        # The document position of the first checkable element of the subtree.
        self.sub_checkable = [
            self.tree.tin[i] if _CHECKABLE in e.flags else _NONE
            for i, e in enumerate(self.elements)
        ]
        # The document position of the first two-target divider and of the
        # first clickable element among the descendants (the element itself
        # not counted).
        own_divider = [
            self.tree.tin[i] if e.resource_id.endswith(_TWO_TARGET_DIVIDER) else _NONE
            for i, e in enumerate(self.elements)
        ]
        # The document position of the first slider of the subtree, and the
        # part on the display of every slider of the screen.
        self.sub_slider = [
            self.tree.tin[i] if _is_slider(e) else _NONE
            for i, e in enumerate(self.elements)
        ]
        clips = (
            (i, self.screen._clip(e))  # noqa: SLF001
            for i, e in enumerate(self.elements)
            if _is_slider(e)
        )
        self.slider_parts = [(i, rect) for i, rect in clips if rect is not None]
        own_clickable = [
            self.tree.tin[i] if _CLICKABLE in e.flags else _NONE
            for i, e in enumerate(self.elements)
        ]
        self.below_divider = [_NONE] * count
        self.below_clickable = [_NONE] * count
        # The first text or content_desc with content among the descendants of
        # each element in document order (None when there is none).
        self.below_label: list[str | None] = [None] * count
        # The document position of the first element of the subtree with a
        # confirm word in one of its own fields, and how many elements of the
        # subtree have a text (a content_desc) with a real character.
        self.sub_confirm = [
            self.tree.tin[i] if self._own_confirm(e) else _NONE
            for i, e in enumerate(self.elements)
        ]
        # The same for the exact-label words, only where they can count: on a
        # dialog that is refused, with such words in use.
        self._exact_on = bool(
            self._exact and self.dialog and self.policy.refuse_dialog_confirm
        )
        self.sub_exact = [
            self.tree.tin[i] if self._exact_on and self._own_exact(e) else _NONE
            for i, e in enumerate(self.elements)
        ]
        self.core_text = [int(self._is_core(e.text)) for e in self.elements]
        self.core_desc = [int(self._is_core(e.content_desc)) for e in self.elements]
        self.sub_core_text = list(self.core_text)
        self.sub_core_desc = list(self.core_desc)
        parent = self.tree.parent
        for i in reversed(self.tree.pre):
            up = parent[i]
            if up != -1:
                self.sub_deny[up] = min(self.sub_deny[up], self.sub_deny[i])
                self.sub_password[up] |= self.sub_password[i]
                self.sub_labeled[up] |= self.sub_labeled[i]
                self.sub_checkable[up] = min(
                    self.sub_checkable[up], self.sub_checkable[i]
                )
                self.sub_slider[up] = min(self.sub_slider[up], self.sub_slider[i])
                self.below_divider[up] = min(
                    self.below_divider[up], own_divider[i], self.below_divider[i]
                )
                self.below_clickable[up] = min(
                    self.below_clickable[up], own_clickable[i], self.below_clickable[i]
                )
                first = self._own_label(self.elements[i]) or self.below_label[i]
                if first is not None:
                    self.below_label[up] = first
                self.sub_confirm[up] = min(self.sub_confirm[up], self.sub_confirm[i])
                self.sub_exact[up] = min(self.sub_exact[up], self.sub_exact[i])
                self.sub_core_text[up] += self.sub_core_text[i]
                self.sub_core_desc[up] += self.sub_core_desc[i]
        # The smallest index of an own-field hit in the subtrees of the
        # clickable ancestors of each element.
        self.above_deny = [_NONE] * count
        for i in self.tree.pre:
            up = parent[i]
            if up != -1:
                self.above_deny[i] = self.above_deny[up]
                if self.clickable[up]:
                    self.above_deny[i] = min(self.above_deny[i], self.sub_deny[up])
        # The highest clickable element at or above each element. Without
        # exception words, a deny word in the joined text of a clickable
        # subtree is also in the joined text of every clickable ancestor, so
        # only this one is matched (see `joined_root` for exception words).
        self.top = [-1] * count
        for i in self.tree.pre:
            up = parent[i]
            inherited = self.top[up] if up != -1 else -1
            self.top[i] = (
                inherited if inherited != -1 else (i if self.clickable[i] else -1)
            )

    @staticmethod
    def _is_labeled(element: Element) -> bool:
        return any(
            _has_content(value)
            for value in (element.text, element.content_desc, element.hint)
        )

    @staticmethod
    def _own_label(element: Element) -> str | None:
        """Return the text, else the content_desc, that has content, or None."""
        for value in (element.text, element.content_desc):
            if _has_content(value):
                return value
        return None

    def _number_label(self, i: int) -> str:
        """Return the label that the ``number_only`` rule reads, or ``""``.

        The candidate's own text, else its content_desc, else its hint, else
        the first text or content_desc with content among its descendants in
        document order.
        """
        element = self.elements[i]
        for value in (element.text, element.content_desc, element.hint):
            if _has_content(value):
                return value
        return self.below_label[i] or ""

    def _is_confirm(self, form: str, *, wide: bool = False) -> bool:
        """True when the form is a confirm word or starts with one.

        A confirm word at the start counts when a boundary follows it: a
        character that is not a letter, or nothing. A word ending in a
        letter of the Japanese script needs no boundary. With ``wide``, the
        dialog-only words (`DIALOG_ONLY_CONFIRM_WORDS`) do not count.
        """
        if not form:
            return False
        if form in self.confirm and not (wide and form in self._dialog_only):
            return True
        for end in self._confirm_lengths:
            if end >= len(form):
                break
            head = form[:end]
            if (
                head in self.confirm
                and not (wide and head in self._dialog_only)
                and (head in self._open_ended or not form[end].isalpha())
            ):
                return True
        return False

    def _form(self, value: str) -> str:
        form = self._confirm_forms.get(value)
        if form is None:
            form = self._confirm_forms[value] = _confirm_form(value)
        return form

    # -- screen-wide deny words ----------------------------------------------------

    def screen_deny(self) -> Detail | None:
        """Return the first deny word match anywhere on the screen.

        Each field of each element is matched alone, in element order. When
        none matches, the texts, the content_descs and the text and
        content_desc sequence of the whole screen in document order, each
        joined by one space, are matched (field ``joined``, no element).
        The exception words are not used here: a deny word anywhere on the
        screen counts, even inside an exception word.
        """
        if self._screen_hit is False:
            self._screen_hit = None
            plain = self._plain_matcher()
            hits = (
                self.own_hit
                if plain is self.matcher
                else [plain.match(e) for e in self.elements]
            )
            for i, hit in enumerate(hits):
                if hit:
                    self._screen_hit = Detail(
                        "confirm_button",
                        word=hit[0],
                        field=hit[1],
                        element_id=self.elements[i].id,
                    )
                    break
            else:
                word = self._joined_word(
                    [self.elements[i] for i in self.tree.pre], plain
                )
                if word is not None:
                    self._screen_hit = Detail(
                        "confirm_button", word=word, field="joined"
                    )
        return self._screen_hit

    def _plain_matcher(self) -> _Matcher:
        """Return the deny words without exceptions (the matcher itself when
        the policy has no exception words).
        """
        if not self.policy.deny_exceptions:
            return self.matcher
        return _Matcher(self.policy.deny_words)

    def _joined_word(
        self, elements: Iterable[Element], matcher: _Matcher | None = None
    ) -> str | None:
        """Return the deny word found in the joined text of the elements.

        Three strings are matched, each joined by one space: the text and
        content_desc sequence, the texts alone and the content_descs alone, so
        that a field between two parts of a phrase does not hide it.
        """
        parts: list[str] = []
        texts: list[str] = []
        descs: list[str] = []
        for element in elements:
            if element.text:
                parts.append(element.text)
                texts.append(element.text)
            if element.content_desc:
                parts.append(element.content_desc)
                descs.append(element.content_desc)
        matcher = matcher or self.matcher
        for joined in (parts, texts, descs):
            if not matcher.excuses:
                word = matcher.match_text(" ".join(joined))
            else:
                word = matcher.match_joined(joined) if joined else None
            if word is not None:
                return word
        return None

    def joined_hit(self, root: int) -> str | None:
        """Return the deny word found in the joined text of a subtree, if any."""
        if root not in self._joined_hit:
            tree = self.tree
            self._joined_hit[root] = self._joined_word(
                self.elements[i] for i in tree.subtree(root)
            )
        return self._joined_hit[root]

    def joined_root(self, i: int) -> int:
        """Return the clickable root whose joined subtree text holds a deny
        word and whose subtree holds the element, or ``_NONE``.

        The scope is the subtree of the highest clickable element at or above
        it. Without exception words its joined text is enough: a deny word in
        the joined text of a subtree is also in that of every clickable
        ancestor. With exception words that does not hold (a longer exception
        word can form in the ancestor's text), so the joined text of every
        clickable element in that subtree is matched as well, the root first,
        then in document order; the first one with a hit is returned.
        """
        root = self.top[i]
        if root == -1:
            return _NONE
        if self.joined_hit(root) is not None:
            return root
        if not self.matcher.excuses:
            return _NONE
        if root not in self._joined_below:
            self._joined_below[root] = next(
                (
                    j
                    for j in self.tree.subtree(root)[1:]
                    if self.clickable[j] and self.joined_hit(j) is not None
                ),
                _NONE,
            )
        return self._joined_below[root]

    # -- confirm labels ------------------------------------------------------------

    def confirm_label(self, i: int) -> str:
        """Return the confirm form that labels a clickable element, or ``""``.

        The sources are its own text, content_desc and hint, the same three
        fields of each descendant, and the descendants' texts (and likewise
        content_descs) joined in document order (`_joined_forms`).
        """
        cached = self._labels.get(i)
        if cached is None:
            cached = self._labels[i] = self._find_label(i)
        return cached

    def _find_label(self, i: int) -> str:
        position = self.sub_confirm[i]
        if position != _NONE:
            element = self.elements[self.tree.pre[position]]
            for value in (element.text, element.content_desc, element.hint):
                form = self._form(value)
                if self._is_confirm(form):
                    return form
        for form in self._joined_label_forms(i):
            if self._is_confirm(form):
                return form
        return ""

    def _joined_label_forms(self, i: int) -> tuple[str, ...]:
        """Return the forms of the descendants' texts, then content_descs,
        joined in document order (`_joined_forms`), for each of the two that
        has at least two parts. Kept, so that the confirm words and the
        exact-label words read the same forms without another walk."""
        cached = self._joined_label.get(i)
        if cached is None:
            below = self.tree.subtree(i)[1:]
            forms: list[str] = []
            # A single part is a field that was matched on its own.
            for getter, count in (
                (lambda e: e.text, self.sub_core_text[i] - self.core_text[i]),
                (lambda e: e.content_desc, self.sub_core_desc[i] - self.core_desc[i]),
            ):
                if count >= _MIN_JOINED_PARTS:
                    values = (getter(self.elements[j]) for j in below)
                    forms.extend(self._joined_forms(values))
            cached = self._joined_label[i] = tuple(forms)
        return cached

    def exact_label(self, i: int) -> str:
        """Return the form of the label of a clickable element when it is
        exactly one of the exact-label words, or ``""``.

        The label is the one `confirm_label` reads, with the same sources and
        the same order (the first element of the subtree, in document order,
        with such a form in its own text, content_desc or hint; else the
        joined descendant texts or content_descs), but a form counts only when
        it equals a word whole, and a label of two or more parts below the
        element is judged as a whole (a word in one part does not count). Only
        on a dialog that is refused. It never changes `confirm_label`.
        """
        if not self._exact_on:
            return ""
        cached = self._exact_labels.get(i)
        if cached is None:
            cached = self._exact_labels[i] = self._find_exact_label(i)
        return cached

    def _find_exact_label(self, i: int) -> str:
        joined = self._joined_label_forms(i)
        position = self.sub_exact[i]
        # A label of two or more parts below the element is a whole: a word in
        # one part (`オン` of `オン` + `ライン`) is not the label, so only the
        # element's own fields and the joined forms count then.
        if position != _NONE and (self.tree.pre[position] == i or not joined):
            element = self.elements[self.tree.pre[position]]
            for value in (element.text, element.content_desc, element.hint):
                form = self._form(value)
                if form in self._exact:
                    return form
        for form in joined:
            if form in self._exact:
                return form
        return ""

    def _own_exact(self, element: Element) -> bool:
        return any(
            self._form(value) in self._exact
            for value in (element.text, element.content_desc, element.hint)
        )

    def wide_label(self, i: int) -> str:
        """Return the confirm form of a clickable element that counts on any
        screen, or ``""``: `confirm_label` without the dialog-only words.
        """
        cached = self._wide_labels.get(i)
        if cached is None:
            cached = self._wide_labels[i] = self._find_wide_label(i)
        return cached

    def _find_wide_label(self, i: int) -> str:
        subtree = self.tree.subtree(i)
        for j in subtree:
            element = self.elements[j]
            for value in (element.text, element.content_desc, element.hint):
                form = self._form(value)
                if self._is_confirm(form, wide=True):
                    return form
        below = subtree[1:]
        for getter, count in (
            (lambda e: e.text, self.sub_core_text[i] - self.core_text[i]),
            (lambda e: e.content_desc, self.sub_core_desc[i] - self.core_desc[i]),
        ):
            if count < _MIN_JOINED_PARTS:
                continue
            values = (getter(self.elements[j]) for j in below)
            for form in self._joined_forms(values):
                if self._is_confirm(form, wide=True):
                    return form
        return ""

    def _joined_forms(self, values: Iterable[str]) -> tuple[str, str]:
        """Return the confirm forms of the values joined in document order.

        The first form joins the cleaned values with one space. The second
        joins them with no space where one value ends and the next starts
        with a letter of the Japanese script (``確`` + ``認してください``),
        which is written without spaces, and with one space elsewhere.
        """
        spaced: list[str] = []
        tight: list[str] = []
        for value in values:
            part = _clean(value)
            if part:
                _add_part(spaced, tight, part)
        return _confirm_form("".join(spaced)), _confirm_form("".join(tight))

    def _own_confirm(self, element: Element) -> bool:
        return any(
            self._is_confirm(form)
            for form in map(
                self._form, (element.text, element.content_desc, element.hint)
            )
        )

    def _is_core(self, value: str) -> bool:
        """True when the value has a character that is not a mark or a space."""
        form = _strip_invisible(normalize(_clean(value)))
        return any(not _is_mark(char) for char in form)

    # -- hit targets ---------------------------------------------------------------

    def contribution(self, i: int) -> _Agg:
        """Return what the element adds to a point it holds."""
        cached = self._contrib.get(i)
        if cached is None:
            element = self.elements[i]
            clickable = self.clickable[i]
            deny = min(self.above_deny[i], i if self.own_hit[i] else _NONE)
            if clickable:
                deny = min(deny, self.sub_deny[i])
            cached = self._contrib[i] = (
                i if element.package != self.target else _NONE,
                i if element.package in PERMISSION_PACKAGES else _NONE,
                i if _is_text_field(element) else _NONE,
                i
                if _PASSWORD in element.flags or (clickable and self.sub_password[i])
                else _NONE,
                i if clickable and self.confirm_label(i) else _NONE,
                deny,
                self.joined_root(i),
                i if clickable and self.exact_label(i) else _NONE,
            )
        return cached

    def _fold(self, indexes: Iterable[int]) -> _Agg:
        result = _EMPTY_AGG
        for i in indexes:
            result = _merge(result, self.contribution(i))
        return result

    def point_aggregate(self, point: tuple[int, int]) -> _Agg:
        """Return the aggregate over every element whose visible part holds the
        point (the hit targets).
        """
        cached = self._points.get(point)
        if cached is None:
            grid = self.grid
            if self._everywhere is None:
                self._everywhere = self._fold(grid.everywhere)
            key = grid.cell(point)
            cell = self._cells.get(key)
            if cell is None:
                cell = self._cells[key] = self._fold(grid.full.get(key, ()))
            x, y = point
            cached = _merge(self._everywhere, cell)
            cached = _merge(
                cached,
                self._fold(
                    i
                    for i, rect in grid.partial.get(key, ())
                    if rect.left <= x < rect.right and rect.top <= y < rect.bottom
                ),
            )
            self._points[point] = cached
        return cached

    def topmost(self, point: tuple[int, int]) -> int:
        """Return the index of the last meaningful element whose visible part
        holds the point (it is drawn on top), or ``-1`` when there is none.
        This is ``Screen.hit_target`` on the grid. The grid also holds the
        slivers (see `__init__`); they count here too, as in ``hit_target``,
        which uses the part on the display with no size threshold.

        A candidate is always clickable, so it is meaningful, and its visible
        part holds its own press point: the result is never ``-1`` for the
        point of a candidate (the ``Screen.tap_point`` refusal ``no_target``
        cannot happen here).
        """
        cached = self._tops.get(point)
        if cached is None:
            grid = self.grid
            key = grid.cell(point)
            base = self._cell_tops.get(key)
            if base is None:
                # The highest meaningful element covering the whole cell (or
                # the whole display): no point of the cell can be below it.
                base = -1
                for i in (*grid.everywhere, *grid.full.get(key, ())):
                    if self.meaningful[i] and i > base:
                        base = i
                self._cell_tops[key] = base
            x, y = point
            top = base
            # The partial rectangles are in document order: from the last one
            # down, the first hit is the answer, and one at or below `base`
            # cannot beat it.
            for i, rect in reversed(grid.partial.get(key, ())):
                if i <= base:
                    break
                if (
                    self.meaningful[i]
                    and rect.left <= x < rect.right
                    and rect.top <= y < rect.bottom
                ):
                    top = i
                    break
            cached = self._tops[point] = top
        return cached

    # -- one candidate -------------------------------------------------------------

    def _hit(self, own: _Agg, found: _Agg, kind: int, i: int) -> tuple[int, bool]:
        """Return the element that causes a reason and whether it is another
        element than the candidate (the candidate itself is named first).
        """
        if own[kind] != _NONE:
            return i, False
        return found[kind], True

    def _cover(self, i: int, point: tuple[int, int] | None) -> Detail | None:
        """Return the ``covered`` detail when another element is on top of the
        press point: the topmost meaningful element there is neither the
        candidate nor one of its descendants.
        """
        if point is None:
            return None
        top = self.topmost(point)
        tree = self.tree
        if top in (-1, i) or tree.tin[i] <= tree.tin[top] < tree.tin[i] + tree.size[i]:
            return None
        ident = self.elements[top].id
        return Detail("covered", note=f"covered by {ident}", element_id=ident)

    def decide(self, i: int) -> Decision:
        """Judge one candidate."""
        element = self.elements[i]
        visible = self.visible[i]
        point = None if visible is None else visible.center
        own = self.contribution(i)
        found = own if point is None else _merge(own, self.point_aggregate(point))
        reasons: list[Reason] = []
        details: list[Detail] = []

        def other(reason: Reason, kind: int) -> None:
            hit, elsewhere = self._hit(own, found, kind, i)
            if elsewhere:
                ident = self.elements[hit].id
                details.append(Detail(reason, note="hit target", element_id=ident))

        if self.screen_note:
            reasons.append("outside_target_app")
            details.append(Detail("outside_target_app", note=self.screen_note))
        elif found[_PKG] != _NONE:
            reasons.append("outside_target_app")
            hit, elsewhere = self._hit(own, found, _PKG, i)
            note = f"element package is {self.elements[hit].package!r}"
            ident = self.elements[hit].id if elsewhere else ""
            details.append(Detail("outside_target_app", note=note, element_id=ident))
        if found[_PERM] != _NONE:
            reasons.append("permission_screen")
            other("permission_screen", _PERM)
        if visible is None:
            reasons.append("not_visible")
        elif (cover := self._cover(i, point)) is not None:
            reasons.append("covered")
            details.append(cover)
        if _DISABLED in element.flags:
            reasons.append("disabled")
        if found[_TEXT] != _NONE:
            reasons.append("text_input")
            other("text_input", _TEXT)
        if found[_PASSWORD_AT] != _NONE:
            reasons.append("password_field")
            other("password_field", _PASSWORD_AT)
        if not self.policy.tap_unlabeled and not self.sub_labeled[i]:
            reasons.append("unlabeled")
        self._crawl_rules(i, point, reasons, details)
        self._word_rules(found, reasons, details)
        if found[_CONFIRM] != _NONE:
            self._confirm_reasons(i, own, found, reasons, details)
        if found[_EXACT] != _NONE:
            self._exact_reasons(i, own, found, reasons, details)
        self._positive_button_reason(i, point, reasons, details)
        if point is not None and any(_contains(area, point) for area in self._avoid):
            reasons.append("screen_edge")
        return Decision(element, point, tuple(reasons), tuple(details))

    def _crawl_rules(
        self,
        i: int,
        point: tuple[int, int] | None,
        reasons: list[Reason],
        details: list[Detail],
    ) -> None:
        """Add the ``number_only``, ``toggle``, row, ``navigate_up`` and
        ``slider`` reasons, each its own check (of the two row reasons, only
        ``two_target_row`` when both match)."""
        if self.policy.skip_number_only:
            label = self._number_label(i)
            if _is_number_only(label):
                reasons.append("number_only")
                note = _clean(label)
                details.append(Detail("number_only", note=note, word=note))
        if self.policy.skip_toggles and self.sub_checkable[i] != _NONE:
            reasons.append("toggle")
            checkable = self.elements[self.tree.pre[self.sub_checkable[i]]]
            details.append(Detail("toggle", element_id=checkable.id))
        if self.policy.skip_two_target_rows and self.below_divider[i] != _NONE:
            reasons.append("two_target_row")
            divider = self.elements[self.tree.pre[self.below_divider[i]]]
            details.append(Detail("two_target_row", element_id=divider.id))
        elif self.policy.skip_rows_with_buttons and self.below_clickable[i] != _NONE:
            reasons.append("row_with_button")
            button = self.elements[self.tree.pre[self.below_clickable[i]]]
            details.append(Detail("row_with_button", element_id=button.id))
        if self.policy.skip_navigate_up:
            sign = self._up_sign(i)
            if sign:
                reasons.append("navigate_up")
                ident = self.elements[i].id
                details.append(Detail("navigate_up", note=sign, element_id=ident))
        if self.policy.skip_sliders:
            slider = self._slider_detail(i, point)
            if slider is not None:
                reasons.append("slider")
                details.append(slider)

    def _slider_detail(self, i: int, point: tuple[int, int] | None) -> Detail | None:
        """Return the ``slider`` detail: a slider in the subtree, else a slider
        on the screen whose part on the display holds the press point.
        """
        if self.sub_slider[i] != _NONE:
            slider = self.elements[self.tree.pre[self.sub_slider[i]]]
            return Detail("slider", note="subtree", element_id=slider.id)
        if point is not None:
            x, y = point
            for j, rect in self.slider_parts:
                if rect.left <= x < rect.right and rect.top <= y < rect.bottom:
                    ident = self.elements[j].id
                    return Detail("slider", note="press point", element_id=ident)
        return None

    def _up_sign(self, i: int) -> str:
        """Return which sign makes the element a toolbar's up button, or ``""``.

        In this order: the structure (an image button that is the first
        clickable child of a toolbar), the resource id (``:id/home`` or
        ``:id/up``), then the label ``Navigate up``.
        """
        element = self.elements[i]
        up = self.tree.parent[i]
        if up != -1 and element.class_name.endswith(_UP_CLASSES):
            parent = self.elements[up]
            if parent.class_name.endswith(_TOOLBAR_CLASS) or (
                parent.resource_id.endswith(_ACTION_BAR_ID)
            ):
                first = next(
                    (j for j in self.tree.children[up] if self.clickable[j]), -1
                )
                if first == i:
                    return "toolbar up button"
        if element.resource_id.endswith(_UP_IDS):
            return f"resource id {element.resource_id}"
        if _NAVIGATE_UP in (element.content_desc, self._number_label(i)):
            return f"label {_NAVIGATE_UP!r}"
        return ""

    def _word_rules(
        self, found: _Agg, reasons: list[Reason], details: list[Detail]
    ) -> None:
        """Add the ``deny_word`` and ``irreversible_screen`` reasons."""
        deny = self._deny_detail(found)
        if deny:
            reasons.append("deny_word")
            details.append(deny)
        irreversible = self.irreversible_detail()
        if irreversible is not None:
            reasons.append("irreversible_screen")
            details.append(irreversible)

    def phrase_name(self, i: int) -> tuple[str, int] | None:
        """The cheap part of the test for a phrase: ``(phrase, name index)`` when
        exactly one element of row `i` has any text, description or hint, that
        is the name N, N has a text, its description and hint are empty or say
        the same as the text, and the text equals a phrase after `normalize`.
        Nothing is scanned: it is the test that runs before any point scan."""
        labeled = [
            j for j in self.tree.subtree(i) if self._has_any_text(self.elements[j])
        ]
        if len(labeled) != 1:
            return None  # only the name may carry text: nothing to join with
        name = labeled[0]
        element = self.elements[name]
        if not element.text.strip() or any(
            value.strip() and normalize(value) != normalize(element.text)
            for value in (element.content_desc, element.hint)
        ):
            return None
        phrase = self._phrases.get(normalize(element.text), "")
        return (phrase, name) if phrase else None

    def phrase_geometry(self, i: int, name: int) -> bool:
        """True when row `i`, whose name element is `name` (`phrase_name`), is
        structurally a row an exception phrase may let through; the reasons are
        not looked at (see `_apply_phrases`).

        The row must be plain: it is not long-clickable, checkable,
        scrollable, editable or a text or password field, none of its
        descendants is clickable or any of those, it has at most
        `ROW_NAME_MEMBERS` elements and no ancestor is clickable (the judge
        joins the texts of a clickable's whole subtree, so no hit can be formed
        with text outside the row). The center of N's full bounds is on the
        display and inside the row's drawn part. The last element drawn at the
        row's tap point and at that center, seen or not, is in the row (at the
        center it is N), except Samsung's empty ``round_corner`` view, and no
        interactive element (clickable, long-clickable, checkable, scrollable,
        editable, a text or password field) outside the row and its ancestors
        is drawn there.
        """
        members = self.tree.subtree(i)
        if not self._plain_row(i, members):
            return False
        center = self.elements[name].bounds.center
        width, height = self.screen.display
        row_part = self.screen._clip(self.elements[i])  # noqa: SLF001
        visible = self.visible[i]
        if (
            not (0 <= center[0] < width and 0 <= center[1] < height)
            or row_part is None
            or visible is None
            or not _contains(row_part, center)
        ):
            return False
        inside = frozenset(members)
        return all(
            self._only_row_at(point, i, inside, last=name if point == center else None)
            for point in (visible.center, center)
        )

    @staticmethod
    def _has_any_text(element: Element) -> bool:
        return any(
            value.strip()
            for value in (element.text, element.content_desc, element.hint)
        )

    def _plain_row(self, i: int, members: list[int]) -> bool:
        """The shape rules of `phrase_candidate`."""
        tree = self.tree
        if (
            len(members) > ROW_NAME_MEMBERS
            or self.visible[i] is None
            or _interactive(self.elements[i], clickable_ok=True)
            or any(_interactive(self.elements[j]) for j in members[1:])
        ):
            return False
        up = tree.parent[i]
        while up != -1:
            if self.clickable[up]:
                return False  # inside another clickable: its texts are joined
            up = tree.parent[up]
        return True

    def _only_row_at(
        self,
        point: tuple[int, int],
        i: int,
        inside: frozenset[int],
        *,
        last: int | None,
    ) -> bool:
        """The last element in document order whose drawn part holds `point`,
        seen or not, is in the row (is `last`, when given), and no interactive
        element outside the row (other than its ancestors) has a drawn part
        there."""
        tree = self.tree
        held = elements_at(self.screen, point)
        # The one decoration the user allows (Samsung's `round_corner`) is
        # drawn over the list and takes nothing: it is passed over.
        drawn = [e for e in held if not _is_decoration(self.screen, e)]
        if not drawn:
            return False
        end = tree.index[drawn[-1].id]
        if end not in inside or (last is not None and end != last):
            return False
        for element in held:
            j = tree.index[element.id]
            if (
                _interactive(element)
                and j not in inside
                and not tree.tin[j] <= tree.tin[i] < tree.tin[j] + tree.size[j]
            ):
                return False
        return True

    def phrase_words_agree(self, i: int, name: int) -> bool:
        """True when the deny words found in the row `i` are exactly the ones
        found in the name text alone (same matcher, exceptions and readings), so
        no word is made with the rest of the row: ``Log`` + ``out`` reads
        ``log out`` only together."""
        members = self.tree.subtree(i)
        elements = [self.elements[j] for j in members]
        alone = [replace(self.elements[name], content_desc="", hint="", resource_id="")]
        for word in self.policy.deny_words:
            matcher = _Matcher([word], self.policy.deny_exceptions)
            in_row = any(matcher.match(e) for e in elements) or (
                self._joined_word(elements, matcher) is not None
            )
            in_name = matcher.match(alone[0]) is not None or (
                self._joined_word(alone, matcher) is not None
            )
            if in_row != in_name:
                return False
        return True

    def unsafe_ids(self) -> frozenset[str]:
        """The ids of every element of the screen, clickable or not, that the tap
        rules would refuse to press for what it is or says: it is of a package
        other than this screen's target, or its own text, description or hint
        holds a deny word (the same matcher, exceptions included) or, with the
        irreversibility rule on, one of its phrases. The decisions of `judge`
        exist only for clickable elements, so the wheel scroll asks this for every
        element at its point."""
        phrases = (
            _PhraseMatcher(self.policy.irreversible_phrases)
            if self.policy.skip_irreversible and self.policy.irreversible_phrases
            else None
        )
        found = set()
        for i, element in enumerate(self.elements):
            hit = self.own_hit[i]
            if (
                element.package != self.target
                or (hit is not None and hit[1] != "resource_id")
                or (phrases is not None and phrases.find([element]) is not None)
            ):
                found.add(element.id)
        return frozenset(found)

    def irreversible_detail(self) -> Detail | None:
        """Return the irreversibility phrase found on the screen, if any.

        Computed once per screen; always None when the rule is off.
        """
        if self._irreversible is False:
            self._irreversible = None
            if self.policy.skip_irreversible and self.policy.irreversible_phrases:
                hit = _PhraseMatcher(self.policy.irreversible_phrases).find(
                    self.elements
                )
                if hit is not None:
                    phrase, field, ident = hit
                    self._irreversible = Detail(
                        "irreversible_screen",
                        word=phrase,
                        field=field,
                        element_id=ident,
                    )
        return self._irreversible

    def _deny_detail(self, found: _Agg) -> Detail | None:
        """Return the deny word of the scope: own fields first, then joined."""
        if found[_DENY] != _NONE:
            hit = self.own_hit[found[_DENY]]
            if hit:
                return Detail(
                    "deny_word",
                    word=hit[0],
                    field=hit[1],
                    element_id=self.elements[found[_DENY]].id,
                )
        if found[_JOINED] != _NONE:
            root = found[_JOINED]
            word = self.joined_hit(root)
            if word is not None:
                return Detail(
                    "deny_word",
                    word=word,
                    field="joined",
                    element_id=self.elements[root].id,
                )
        return None

    def _positive_button_reason(
        self,
        i: int,
        point: tuple[int, int] | None,
        reasons: list[Reason],
        details: list[Detail],
    ) -> None:
        if not self._in_positive_button(i, point):
            return
        if "dialog_confirm_button" not in reasons:
            reasons.append("dialog_confirm_button")
        note = f"the dialog's positive button ({_DIALOG_POSITIVE_ID})"
        details.append(Detail("dialog_confirm_button", note=note))

    def _is_positive_id(self, resource_id: str) -> bool:
        """True for ``button1`` under ``android`` or the screen's own package."""
        package, separator, rest = resource_id.partition(":id/")
        return (
            bool(separator)
            and rest == _DIALOG_POSITIVE_ENTRY
            and package in ("android", self.screen.package)
        )

    def _in_positive_button(self, i: int, point: tuple[int, int] | None) -> bool:
        """True on a dialog screen (with ``refuse_dialog_confirm``) when the
        element, one of its ancestors, the element its press point resolves
        to (or one of that element's ancestors) or any element whose visible
        part holds the press point is the dialog's positive button, whatever
        its label. The last check does not rely on parent links.
        """
        if not (self.dialog and self.policy.refuse_dialog_confirm):
            return False
        if not self._positive:
            return False
        starts = [i]
        if point is not None:
            top = self.topmost(point)
            if top != -1:
                starts.append(top)
            x, y = point
            for j in self._positive:
                rect = self.screen._clip(self.elements[j])  # noqa: SLF001
                if (
                    rect is not None
                    and rect.left <= x < rect.right
                    and rect.top <= y < rect.bottom
                ):
                    return True
        parent = self.tree.parent
        for start in starts:
            at = start
            while at != -1:
                if at in self._positive_set:
                    return True
                at = parent[at]
        return False

    def _confirm_reasons(
        self,
        i: int,
        own: _Agg,
        found: _Agg,
        reasons: list[Reason],
        details: list[Detail],
    ) -> None:
        hit, elsewhere = self._hit(own, found, _CONFIRM, i)
        label = self.confirm_label(hit)
        note = f"{label} (hit target {self.elements[hit].id})" if elsewhere else label
        screen_deny = self.screen_deny() if self.wide_label(hit) else None
        if screen_deny:
            reasons.append("confirm_button")
            details.append(
                Detail(
                    reason=screen_deny.reason,
                    note=note,
                    word=screen_deny.word,
                    field=screen_deny.field,
                    element_id=screen_deny.element_id,
                )
            )
        if self.dialog and self.policy.refuse_dialog_confirm:
            reasons.append("dialog_confirm_button")
            details.append(Detail("dialog_confirm_button", note=note))

    def _exact_reasons(
        self,
        i: int,
        own: _Agg,
        found: _Agg,
        reasons: list[Reason],
        details: list[Detail],
    ) -> None:
        """Add ``dialog_confirm_button`` for an exact-label word (see
        `exact_label`), on its own: it never touches ``confirm_button``."""
        hit, elsewhere = self._hit(own, found, _EXACT, i)
        label = self.exact_label(hit)
        note = f"{label} (hit target {self.elements[hit].id})" if elsewhere else label
        if "dialog_confirm_button" not in reasons:
            reasons.append("dialog_confirm_button")
        details.append(Detail("dialog_confirm_button", note=note))


def _contains(bounds: Bounds, point: tuple[int, int]) -> bool:
    x, y = point
    return bounds.left <= x < bounds.right and bounds.top <= y < bounds.bottom


# Flags (and kinds of element) that make a row or what is inside it more than a
# plain label for `[deny] except_phrases`.
_INTERACTIVE = frozenset(
    {_CLICKABLE, _CHECKABLE, _PASSWORD, "long_clickable", "scrollable"}
)
# What replaces the name text when a phrase is tried: a label with no deny word,
# so that the label rules (unlabeled, number_only) decide as before.
_PLACEHOLDER = "\u00b7"
MAX_PHRASE_CANDIDATES = 8
MAX_PHRASE_NAMED = 16


def _interactive(element: Element, *, clickable_ok: bool = False) -> bool:
    """True for an element that is more than a plain label: long-clickable,
    checkable, scrollable, editable or a text or password field, and, unless
    `clickable_ok`, clickable."""
    flags = _INTERACTIVE - {_CLICKABLE} if clickable_ok else _INTERACTIVE
    return (
        any(flag in element.flags for flag in flags)
        or element.editable
        or _is_text_field(element)
    )


def _apply_phrases(
    state: _Judge,
    decisions: tuple[Decision, ...],
    target_package: str,
    foreground: str | None,
) -> tuple[Decision, ...]:
    """Let through the rows that an exception phrase excuses.

    Candidates are the rows whose only reason is ``deny_word`` and that
    `_Judge.phrase_candidate` accepts (a plain row whose name equals a phrase).
    With more than `MAX_PHRASE_CANDIDATES` of them on the screen no phrase
    applies. Each candidate is judged once more on the same screen with only
    its name text replaced by a neutral label: when it then has no reason at
    all, the hits were made of the phrase's own characters and it is let
    through, marked with the phrase. The judge's own matching decides, with its
    exceptions and readings; any other deny word anywhere still refuses, and a
    hit made with a neighbouring text may over-refuse.
    """
    found: list[tuple[int, int, str, int]] = []
    named = 0
    for k, decision in enumerate(decisions):
        i = state.tree.index[decision.element.id]
        candidate = state.phrase_name(i)
        if candidate is None:
            continue
        named += 1
        if named > MAX_PHRASE_NAMED:
            return decisions  # too many rows to examine: no phrase on this screen
        if state.phrase_geometry(i, candidate[1]):
            found.append((k, i, candidate[0], candidate[1]))
            if len(found) > MAX_PHRASE_CANDIDATES:
                return decisions  # counted before the reasons; stop at once
    found = [
        item
        for item in found
        if decisions[item[0]].reasons == ("deny_word",)
        and state.phrase_words_agree(item[1], item[3])
    ]
    if not found:
        return decisions
    policy = replace(state.policy, except_phrases=())
    result = list(decisions)
    for k, i, phrase, name in found:
        elements = list(state.elements)
        # A description or hint of the name is the name again (checked in
        # `phrase_candidate`): it goes with it.
        elements[name] = replace(
            elements[name], text=_PLACEHOLDER, content_desc="", hint=""
        )
        other = _Judge(
            replace(state.screen, elements=tuple(elements)),
            target_package,
            policy,
            foreground,
        ).decide(i)
        if not other.reasons:
            before = decisions[k]
            result[k] = Decision(before.element, before.point, (), (), phrase)
    return tuple(result)


def judge(
    screen: Screen,
    target_package: str,
    policy: Policy,
    *,
    foreground: str | None,
) -> Judgement:
    """Decide for every candidate of the screen whether it may be pressed.

    Candidates are the elements with the ``clickable`` flag; other elements
    are not listed. When `foreground` is None (unknown), differs from
    `target_package`, or the screen's package does, every candidate gets
    ``outside_target_app``, except on a screen with more than `MAX_ELEMENTS`
    elements, where every candidate gets only ``screen_too_large``.

    The hit targets of a candidate are all elements whose visible part holds
    its press point: the candidate itself, its ancestors and every overlay.
    Several reasons look at the hit targets, not only at the candidate, so a
    button under a password field, a permission button or a confirm button is
    refused at the same point where the element on top is. The detail of such a
    reason names the hit target (``element_id``, or the id in ``note``).

    Reasons, in this order, each listed once when it applies:

    * ``screen_too_large``: the screen has more than `MAX_ELEMENTS` elements.
      Every candidate gets only this reason, no point and no other work is
      done (the cost of the other rules grows with the number of elements).
    * ``outside_target_app``: foreground unknown or not the target, or the
      screen is in another package, or any hit target is. A package in
      ``policy.also_target`` counts as the target when it is the foreground
      app: the screen and every hit target must then be in that package.
    * ``permission_screen``: any hit target belongs to a permission package.
    * ``not_visible``: no part of the element is on the display, or the part
      is under 8 pixels wide or under 8 pixels high.
    * ``covered``: the topmost meaningful element (clickable, or with a
      non-empty text, content description or hint; the last in document order)
      whose part on the display holds the press point (any size: a sliver
      on top counts) is neither the candidate nor one of its descendants.
      The detail's ``element_id`` is that element.
    * ``disabled``: the element has the ``disabled`` flag.
    * ``text_input``: any hit target is a text field (password flag or the
      class rule); pressing it raises the keyboard.
    * ``password_field``: any hit target has the password flag, or the
      candidate or a clickable hit target has a descendant with it. A
      non-clickable container holding the point does not count its
      descendants, or one password field would block the whole screen.
    * ``unlabeled``: neither the element nor a descendant has a text,
      content description or hint (unless ``tap_unlabeled``).
    * ``number_only``: the label (own text, else content description, else
      hint, else the first non-empty text or content description of a
      descendant in document order, invisible characters removed) has a
      numeral (a digit, a fraction or a Roman numeral) and otherwise only
      whitespace, punctuation and symbols (unless
      ``skip_number_only`` is False). The detail's ``note`` and ``word`` hold
      the label.
    * ``toggle``: the element or a descendant has the ``checkable`` flag
      (unless ``skip_toggles`` is False). The detail's ``element_id`` is the
      first checkable element in document order. A row that holds a switch is
      refused even when pressing the row opens a sub-screen.
    * ``two_target_row``: a descendant of the element has a resource id
      ending in ``:id/two_target_divider`` (unless ``skip_two_target_rows`` is
      False). The detail's ``element_id`` is the first such element in
      document order. The elements inside the row are judged on their own.
    * ``row_with_button``: the element has a clickable descendant (only when
      ``skip_rows_with_buttons`` is True, and not given when
      ``two_target_row`` is). The detail's ``element_id`` is the first
      clickable descendant in document order.
    * ``navigate_up``: the element is the up button of a toolbar (unless
      ``skip_navigate_up`` is False): its class ends with ``ImageButton`` or
      ``ImageView`` and it is the first clickable child of a parent whose
      class ends with ``Toolbar`` or whose resource id ends with
      ``:id/action_bar``; or its resource id ends with ``:id/home`` or
      ``:id/up``; or its content description or its label (as for
      ``number_only``) is exactly ``Navigate up``. The detail's ``note``
      names the sign and ``element_id`` the element.
    * ``slider``: the element or a descendant is an adjustable control (its
      class, after the last ``.`` and ``$``, ends with ``SeekBar``,
      ``RatingBar`` or ``Slider``), or the press point lies inside the part
      on the display of such a control anywhere on the screen (unless
      ``skip_sliders`` is False). The detail's ``element_id`` is the slider
      and ``note`` is ``subtree`` or ``press point``.
    * ``deny_word``: a deny word is found in the deny scope: the element and
      its descendants, the whole subtree of every clickable ancestor
      (nested rows and cards are all in scope), and, for every hit target, its
      own fields, its whole subtree when it is clickable, and the subtrees of
      its clickable ancestors. Each field is matched alone, then the joined
      subtree text (field ``joined``) of the highest clickable element in
      scope above the candidate and above each clickable hit target: the
      text and content_desc sequence, the texts alone and the content_descs
      alone. With exception words, the joined text of every clickable
      element in that highest subtree is matched as well, siblings of the
      candidate included, and the detail's ``element_id`` names the clickable
      element whose joined text held the hit. A hit inside an exception word
      of ``policy.deny_exceptions`` that stands whole in a text, content_desc
      or hint, or in one field's value inside a joined text with real
      boundaries around it, does not count (see the module docstring); the
      resource id has no exceptions.
      A plain row whose only reason is this one and whose name text equals a
      phrase of ``policy.except_phrases`` is judged once more with only that
      text replaced; with no reason left it is let through (see
      `_apply_phrases` and `Policy`) and ``Decision`` has ``except_phrase``.
    * ``irreversible_screen``: the text, content description or hint of any
      element of the screen holds a phrase of ``policy.irreversible_phrases``
      (unless ``skip_irreversible`` is False). Every candidate gets it. The
      detail's ``word`` is the phrase, ``field`` the field and ``element_id``
      the first element (in document order) where it was found.
    * ``confirm_button``: a confirm word is the label of the candidate or of
      any clickable hit target (any source: own text, content_desc, hint,
      those of any descendant, or the joined descendant texts or
      content_descs) and a deny word is found anywhere on the screen, alone in
      a field or in the joined texts, content_descs or text and content_desc
      sequence of the whole screen (without the exception words).
    * ``dialog_confirm_button``: a confirm word is the label (same sources) and
      the screen is
      a dialog (its first top-level element is smaller than the display; the first
      element with an unknown parent counts as top-level too). On such a screen
      the element whose resource id is exactly ``android:id/button1`` (the
      platform dialog's positive button), and anything inside it, gets the
      reason whatever its label (detail note: the dialog's positive button).
      Both follow ``policy.refuse_dialog_confirm``.
    * ``screen_edge``: the press point lies in a ``policy.avoid`` rectangle
      (None means ``api36_fallback_areas(screen.display)``; ``()`` turns it off).

    Raises:
        ValueError: Two elements of the screen have the same id.
    """
    elements = screen.elements
    if len(elements) > MAX_ELEMENTS:
        if len({element.id for element in elements}) != len(elements):
            msg = "element ids on the screen are not unique"
            raise ValueError(msg)
        note = f"{len(elements)} elements; the limit is {MAX_ELEMENTS}"
        verdict = (Detail("screen_too_large", note=note),)
        return Judgement(
            tuple(
                Decision(element, None, ("screen_too_large",), verdict)
                for element in elements
                if _CLICKABLE in element.flags
            )
        )
    state = _Judge(screen, target_package, policy, foreground)
    decisions = tuple(
        state.decide(i) for i, element in enumerate(elements) if state.clickable[i]
    )
    unsafe = state.unsafe_ids()
    if policy.except_phrases:
        decisions = _apply_phrases(state, decisions, target_package, foreground)
        # A row an exception phrase lets through is still unsafe for the wheel:
        # the phrase lets a tap through, nothing else.
        unsafe |= frozenset(d.element.id for d in decisions if d.except_phrase)
    return Judgement(
        decisions,
        dialog=state.dialog,
        irreversible=state.irreversible_detail() is not None,
        edges=state._avoid,  # noqa: SLF001 - the areas of `screen_edge`
        unsafe=unsafe,
    )
