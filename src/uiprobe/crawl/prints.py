"""Fingerprints of the elements of one screen."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from uiprobe.model import Screen  # noqa: TC001

if TYPE_CHECKING:
    from uiprobe.model import Element


def _part(value: str) -> str:
    return f"{len(value)}:{value}"


def _ids_unique(screen: Screen) -> bool:
    """Whether no two elements share an id (judge raises ValueError if not)."""
    elements = screen.elements
    return len({element.id for element in elements}) == len(elements)


class _Prints:
    """Fingerprints of the elements of one screen."""

    def __init__(self, screen: Screen) -> None:
        self._elements = screen.elements
        self._by_id = {element.id: element for element in screen.elements}
        self._chains: dict[str, tuple[Element, ...]] = {}
        self._below: dict[str, list[Element]] | None = None

    def _chain(self, element: Element) -> tuple[Element, ...]:
        cached = self._chains.get(element.id)
        if cached is not None:
            return cached
        found: list[Element] = []
        seen = {element.id}
        parent = element.parent
        while parent is not None and parent not in seen:
            seen.add(parent)
            ancestor = self._by_id.get(parent)
            if ancestor is None:
                break
            found.append(ancestor)
            parent = ancestor.parent
        chain = tuple(reversed(found))
        self._chains[element.id] = chain
        return chain

    def descendants(self, element: Element) -> tuple[Element, ...]:
        """The elements below ``element``, in document order."""
        if self._below is None:
            below: dict[str, list[Element]] = {}
            for other in self._elements:
                for ancestor in self._chain(other):
                    below.setdefault(ancestor.id, []).append(other)
            self._below = below
        return tuple(self._below.get(element.id, ()))

    def shape(self, element: Element) -> tuple[object, ...]:
        """What stays the same when only labels change: classes, resource ids of
        the element, its descendants and its ancestors, and the element's bounds.
        """
        return (
            element.class_name,
            element.resource_id,
            tuple(element.bounds.to_list()),
            tuple((d.class_name, d.resource_id) for d in self.descendants(element)),
            tuple((a.class_name, a.resource_id) for a in self._chain(element)),
        )

    def labels(self, element: Element) -> tuple[tuple[str, str, str], ...]:
        """Text, description and hint of the element and its descendants."""
        return tuple(
            (e.text, e.content_desc, e.hint)
            for e in (element, *self.descendants(element))
        )

    def of(self, element: Element) -> str:
        parts = [
            element.class_name,
            element.resource_id,
            element.text,
            element.content_desc,
            element.hint,
        ]
        below = self.descendants(element)
        parts.append(str(len(below)))
        for d in below:
            parts.extend((d.class_name, d.resource_id, d.text, d.content_desc, d.hint))
        chain = self._chain(element)
        parts.append(str(len(chain)))
        for ancestor in chain:
            parts.append(ancestor.class_name)
            parts.append(ancestor.resource_id)
        digest = hashlib.sha256(
            "|".join(_part(p) for p in parts).encode("utf-8", "replace")
        )
        return "f-" + digest.hexdigest()[:32]


def fingerprint_of(screen: Screen, element: Element) -> str:
    """Return the fingerprint of an element of the screen.

    It is built from the element's own class, resource id, text, content
    description and hint, then the class, resource id, text, content
    description and hint of its descendants in document order, then the (class,
    resource id) of its ancestors from the top down. No position or bounds go
    in, and neither does the element id, so it survives a list that scrolls or
    changes its element count.
    """
    return _Prints(screen).of(element)
