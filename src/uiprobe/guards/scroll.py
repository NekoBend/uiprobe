"""Where the pressing crawl may turn a mouse wheel over a list, and what a
wheel turn that changed something looks like.

`scroll_target` picks the one point of a wheel event on a judged screen, or
None. It is pure: it reads the screen and its `Judgement` and nothing else.

A wheel event is not a touch. It goes to the window under the pointer, not to
the focused window, and it does what a wheel does to the view there: it scrolls
a list, but it also moves a slider, steps a picker, or turns a spinner. So the
choice works from allow lists, fixed in `uiprobe.guards.constants` and not
settings: the scrollable element must have one of four exact classes, and every
element at the point must be that container or one of eight plain layout and
label classes. Anything else at the point (a spinner, a custom view, a second
scrollable, a pager, a web view) disqualifies the point. A class an app derives
from a plain layout and makes react to the wheel cannot be told from the plain
one in the dump; that is the limit of this check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from uiprobe.guards.constants import (
    _CHECKABLE,
    SCROLL_CONTAINER_CLASSES,
    SCROLL_POINT_CLASSES,
)
from uiprobe.guards.tree import _is_dialog, _is_text_field, _Tree

if TYPE_CHECKING:
    from uiprobe.guards.policy import Judgement
    from uiprobe.model import Element, Screen

_SCROLLABLE = "scrollable"
_LONG_CLICKABLE = "long_clickable"
_PASSWORD = "password"  # noqa: S105 (a flag name)
# The grid of candidate points, in quarters of the container's visible width and
# height: the center first, then the others.
_GRID = ((2, 2), (1, 2), (3, 2), (2, 1), (2, 3), (1, 1), (3, 1), (1, 3), (3, 3))

Point = tuple[int, int]


def scroll_container(screen: Screen) -> Element | None:
    """The container of a wheel scroll: the scrollable element with the largest
    visible part (the first of equals, in document order), provided its class is
    exactly one of `SCROLL_CONTAINER_CLASSES`; None when there is no scrollable
    element with a visible part or the largest has another class (no other
    element is tried)."""
    best: tuple[int, Element] | None = None
    for element in screen.elements:
        if _SCROLLABLE not in element.flags:
            continue
        part = screen.visible_bounds(element)
        if part is None:
            continue
        area = part.width * part.height
        if best is None or area > best[0]:
            best = (area, element)
    if best is None or best[1].class_name not in SCROLL_CONTAINER_CLASSES:
        return None
    return best[1]


def elements_at(screen: Screen, point: Point) -> list[Element]:
    """The elements whose part on the display holds ``point`` (any size), in
    document order."""
    x, y = point
    found = []
    for element in screen.elements:
        part = screen._clip(element)  # noqa: SLF001 - the part on the display
        if (
            part is not None
            and part.left <= x < part.right
            and part.top <= y < part.bottom
        ):
            found.append(element)
    return found


def _allowed(element: Element, container: Element, refused_ids: frozenset[str]) -> bool:
    """Whether an element at the point leaves the point qualified.

    It must be the container or have exactly one of `SCROLL_POINT_CLASSES`, and
    not be long-clickable, checkable, a password field, a text field or refused
    by judge. A plain clickable row is allowed.
    """
    if element.id != container.id and element.class_name not in SCROLL_POINT_CLASSES:
        return False
    return not (
        element.id in refused_ids
        or _LONG_CLICKABLE in element.flags
        or _CHECKABLE in element.flags
        or _PASSWORD in element.flags
        or _is_text_field(element)
    )


def scroll_target(screen: Screen, judgement: Judgement) -> tuple[str, Point] | None:
    """Return ``(container id, point)`` for a wheel turned on ``screen``, or
    None when it must not be turned.

    None when the screen is a dialog or is judged irreversible, when there is no
    container (`scroll_container`), or when no candidate point qualifies. The
    candidates are the container's visible center and then the other points of a
    3 by 3 grid (quarters of its visible part), in that order. A point qualifies
    when every element that holds it passes `_allowed` and the topmost labelled
    element there is the container or inside it. Redundant with the allow list
    but kept as an extra layer: long-clickable, checkable, password and text
    field are refused whatever their class.
    """
    if judgement.dialog or judgement.irreversible:
        return None
    container = scroll_container(screen)
    if container is None:
        return None
    area = screen.visible_bounds(container)
    if area is None:
        return None
    refused = frozenset(d.element.id for d in judgement.decisions if d.reasons)
    for col, row in _GRID:
        point = (
            area.left + area.width * col // 4,
            area.top + area.height * row // 4,
        )
        if not all(_allowed(e, container, refused) for e in elements_at(screen, point)):
            continue
        top = screen.hit_target(point)
        if top is None or screen._is_within(top, container):  # noqa: SLF001
            return container.id, point
    return None


def container_key(screen: Screen, container: Element) -> str:
    """A name for the container that does not depend on the screen id, the texts
    or the header: the activity, the container's class and resource id and the
    classes and resource ids of its ancestors. The per-screen scroll cap and the
    block after an incident are kept under it, so a header that changes when BACK
    returns to the same list does not reset them."""
    by_id = {e.id: e for e in screen.elements}
    parts = [screen.activity or "", container.class_name, container.resource_id]
    seen = {container.id}
    parent = container.parent
    while parent is not None and parent not in seen and parent in by_id:
        seen.add(parent)
        above = by_id[parent]
        parts.extend((above.class_name, above.resource_id))
        parent = above.parent
    return "|".join(parts)


def is_dialog(screen: Screen) -> bool:
    """Whether the screen is a dialog: its first top-level element is smaller
    than the display (the same test as ``dialog_confirm_button``)."""
    return _is_dialog(_Tree(screen))


def _ancestry(
    element: Element, by_id: dict[str, Element]
) -> tuple[tuple[str, str], ...]:
    chain: list[tuple[str, str]] = []
    seen = {element.id}
    parent = element.parent
    while parent is not None and parent not in seen and parent in by_id:
        seen.add(parent)
        above = by_id[parent]
        chain.append((above.class_name, above.resource_id))
        parent = above.parent
    return tuple(chain)


def changed_by_wheel(before: Screen, after: Screen) -> bool:
    """Whether the screen shows that a wheel turn changed something other than
    the scroll position, comparing the screen read before it with the one after.

    Two signs. (1) A checkable element with the same text-inclusive identity
    (class, resource id, text, description, hint and the ancestors' classes and
    resource ids) on both screens whose checked states differ (the sorted states
    of all such elements are compared per identity). (2) An element whose
    text-free identity (class, resource id and the ancestors' classes and
    resource ids) is unique on both screens and whose text or description
    differs, or whose checked state differs. Rows of a list that merely scrolled
    are told apart from these by their identity not being unique; a clock or a
    counter that changes between the reads is taken as a change too, which only
    costs a scroll.
    """
    return _checked_changed(before, after) or _unique_changed(before, after)


def _checked_changed(before: Screen, after: Screen) -> bool:
    def states(screen: Screen) -> dict[tuple[object, ...], list[bool]]:
        by_id = {e.id: e for e in screen.elements}
        found: dict[tuple[object, ...], list[bool]] = {}
        for element in screen.elements:
            if _CHECKABLE not in element.flags:
                continue
            key = (
                element.class_name,
                element.resource_id,
                element.text,
                element.content_desc,
                element.hint,
                _ancestry(element, by_id),
            )
            found.setdefault(key, []).append("checked" in element.flags)
        return found

    old, new = states(before), states(after)
    return any(
        key in new and sorted(flags) != sorted(new[key]) for key, flags in old.items()
    )


def _unique_changed(before: Screen, after: Screen) -> bool:
    def identities(screen: Screen) -> dict[tuple[object, ...], list[Element]]:
        by_id = {e.id: e for e in screen.elements}
        found: dict[tuple[object, ...], list[Element]] = {}
        for element in screen.elements:
            key = (element.class_name, element.resource_id, _ancestry(element, by_id))
            found.setdefault(key, []).append(element)
        return found

    old, new = identities(before), identities(after)
    for key, elements in old.items():
        if len(elements) != 1 or len(new.get(key, ())) != 1:
            continue
        was, now = elements[0], new[key][0]
        if (
            was.text != now.text
            or was.content_desc != now.content_desc
            or ("checked" in was.flags) != ("checked" in now.flags)
        ):
            return True
    return False
