"""The element tree, the element predicates and the press-point grid."""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Final

from uiprobe.guards.constants import (
    _HALF_WIDTH_VOICED,
    _NUMBER_CATEGORIES,
    _PASSWORD,
    _SEARCH_AUTOCOMPLETE,
    _SLIDER_CLASSES,
    _TEXT_FIELD_PARTS,
)
from uiprobe.guards.words import _is_japanese_letter, _strip_invisible
from uiprobe.model import Bounds  # noqa: TC001 (a runtime import in the former guards.py)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from uiprobe.model import Element, Screen


class _Tree:
    """Index-based links of a screen; unknown parents count as roots.

    Indexes are positions in ``screen.elements``. ``pre`` lists every element in
    document (pre-)order, children in element order, so the subtree of ``i`` is
    ``pre[tin[i] : tin[i] + size[i]]``. A parent cycle is cut at its entry
    point, which then counts as a root.
    """

    def __init__(self, screen: Screen) -> None:
        self.screen = screen
        elements = screen.elements
        self.elements = elements
        count = len(elements)
        self.index = {element.id: i for i, element in enumerate(elements)}
        if len(self.index) != count:
            msg = "element ids on the screen are not unique"
            raise ValueError(msg)
        self.parent = [-1] * count
        for i, element in enumerate(elements):
            parent = self.index.get(element.parent, -1) if element.parent else -1
            self.parent[i] = -1 if parent == i else parent
        self._cut_cycles()
        self.children: list[list[int]] = [[] for _ in range(count)]
        self.roots: list[int] = []
        for i, parent in enumerate(self.parent):
            if parent == -1:
                self.roots.append(i)
            else:
                self.children[parent].append(i)
        self.pre: list[int] = []
        self.tin = [0] * count
        self.size = [1] * count
        stack = list(reversed(self.roots))
        while stack:
            current = stack.pop()
            self.tin[current] = len(self.pre)
            self.pre.append(current)
            stack.extend(reversed(self.children[current]))
        for i in reversed(self.pre):
            if self.parent[i] != -1:
                self.size[self.parent[i]] += self.size[i]

    def _cut_cycles(self) -> None:
        state = [0] * len(self.parent)
        for start in range(len(self.parent)):
            if state[start]:
                continue
            path: list[int] = []
            current = start
            while current != -1 and state[current] == 0:
                state[current] = 1
                path.append(current)
                current = self.parent[current]
            if current != -1 and state[current] == 1:
                self.parent[current] = -1
            for seen in path:
                state[seen] = 2

    def subtree(self, i: int) -> list[int]:
        """Return the element and its descendants in document order."""
        return self.pre[self.tin[i] : self.tin[i] + self.size[i]]


# The most elements of a row whose texts name it (the tree's rule too).
ROW_NAME_MEMBERS: Final = 500


def row_members(i: int, children: Callable[[int], Iterable[int]]) -> list[int]:
    """Return the element `i` and what is below it, in document order, at most
    `ROW_NAME_MEMBERS` of them (a parent cycle cannot loop either).

    `children` gives the indexes of the children of an index, in element
    order. This is the walk `uiprobe tree` makes to name a row, and the judge's
    `[deny] except_phrases` rule uses the same one.
    """
    found = [i]
    seen = {i}
    stack = [i]
    while stack and len(found) < ROW_NAME_MEMBERS:
        for child in children(stack.pop()):
            if child not in seen:
                seen.add(child)
                found.append(child)
                stack.append(child)
    return sorted(found)[:ROW_NAME_MEMBERS]


def row_name(elements: Sequence[Element], members: Iterable[int]) -> str:
    """Return the name of a row: the first text (stripped) that is not empty
    among its `members` in document order, else the first content description
    (stripped) that is not empty, else ``""``. This is the name `uiprobe tree`
    shows, apart from its placeholders for a password row and for a run that
    recorded no text."""
    found = list(members)
    for j in found:
        if text := elements[j].text.strip():
            return text
    for j in found:
        if desc := elements[j].content_desc.strip():
            return desc
    return ""


def _is_slider(element: Element) -> bool:
    """True when the class names an adjustable control (see ``slider``)."""
    name = element.class_name.rsplit(".", 1)[-1].rsplit("$", 1)[-1]
    return name.endswith(_SLIDER_CLASSES)


def _is_text_field(element: Element) -> bool:
    if _PASSWORD in element.flags:
        return True
    name = element.class_name.rsplit(".", 1)[-1].rsplit("$", 1)[-1].casefold()
    return name == _SEARCH_AUTOCOMPLETE or any(p in name for p in _TEXT_FIELD_PARTS)


def _is_dialog(tree: _Tree) -> bool:
    # The first element without a parent, and the first root of the
    # tree (an element with an unknown parent also counts): either one smaller
    # than the display makes the screen a dialog.
    width, height = tree.screen.display
    tops = [e for e in tree.elements if e.parent is None][:1]
    tops.extend(tree.elements[i] for i in tree.roots[:1])
    return any(e.bounds.width < width or e.bounds.height < height for e in tops)


def _add_part(spaced: list[str], tight: list[str], part: str) -> None:
    """Append a cleaned label part to both joins.

    ``spaced`` puts one space before every part but the first. ``tight`` does
    the same, except where the previous part ends and this one starts with a
    letter of the Japanese script: there it joins them directly. A half-width
    voiced sound mark is never joined directly, because NFKC would merge it
    into the letter before it.
    """
    if not spaced:
        spaced.append(part)
        tight.append(part)
        return
    previous = spaced[-1]
    spaced.extend((" ", part))
    if (
        _is_japanese_letter(previous[-1])
        and _is_japanese_letter(part[0])
        and part[0] not in _HALF_WIDTH_VOICED
    ):
        tight.extend(("", part))
    else:
        tight.extend((" ", part))


def _is_number_only(label: str) -> bool:
    """True when the label has a numeral and nothing but numerals, whitespace,
    punctuation and symbols (categories N, Z, P and S).

    A numeral is any character of the categories Nd, Nl and No, so fractions
    (``½``) and Roman numerals (``Ⅳ``) count, as decimal digits do.
    """
    found = False
    for char in _strip_invisible(label):
        category = unicodedata.category(char)
        if category in _NUMBER_CATEGORIES:
            found = True
        elif category[0] not in "NZPS" and not char.isspace():
            return False
    return found


# An aggregate holds, for the hit targets of one press point, the smallest
# element index that has each property (_NONE when no hit target has it).
_PKG, _PERM, _TEXT, _PASSWORD_AT, _CONFIRM, _DENY, _JOINED = range(7)
_NONE: Final = 1 << 30
_Agg = tuple[int, int, int, int, int, int, int]
_EMPTY_AGG: Final[_Agg] = (_NONE, _NONE, _NONE, _NONE, _NONE, _NONE, _NONE)
_CELLS_X: Final = 24
_CELLS_Y: Final = 48


def _merge(first: _Agg, second: _Agg) -> _Agg:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
        min(first[4], second[4]),
        min(first[5], second[5]),
        min(first[6], second[6]),
    )


class _Grid:
    """A grid over the display that finds the rectangles holding a point.

    A rectangle is listed in every cell it touches, as ``full`` when it covers
    the cell completely (nothing to test per point) or ``partial``. A
    rectangle covering the whole display is listed once, in ``everywhere``.
    """

    def __init__(self, display: tuple[int, int], rects: list[tuple[int, Bounds]]):
        width, height = display
        self.cell_w = max(1, -(-width // _CELLS_X))
        self.cell_h = max(1, -(-height // _CELLS_Y))
        self.cols = -(-width // self.cell_w) if width > 0 else 1
        self.everywhere: list[int] = []
        self.full: dict[int, list[int]] = {}
        self.partial: dict[int, list[tuple[int, Bounds]]] = {}
        for i, rect in rects:
            if (
                rect.left <= 0
                and rect.top <= 0
                and rect.right >= width
                and rect.bottom >= height
            ):
                self.everywhere.append(i)
                continue
            self._add(i, rect, width, height)

    def _add(self, i: int, rect: Bounds, width: int, height: int) -> None:
        cell_w, cell_h = self.cell_w, self.cell_h
        for row in range(rect.top // cell_h, (rect.bottom - 1) // cell_h + 1):
            top = row * cell_h
            bottom = min(top + cell_h, height)
            tall = rect.top <= top and rect.bottom >= bottom
            for col in range(rect.left // cell_w, (rect.right - 1) // cell_w + 1):
                left = col * cell_w
                right = min(left + cell_w, width)
                key = row * self.cols + col
                if tall and rect.left <= left and rect.right >= right:
                    self.full.setdefault(key, []).append(i)
                else:
                    self.partial.setdefault(key, []).append((i, rect))

    def cell(self, point: tuple[int, int]) -> int:
        """Return the key of the cell holding the point."""
        return (point[1] // self.cell_h) * self.cols + point[0] // self.cell_w
