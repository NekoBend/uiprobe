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
    _ID_SEPARATOR,
    DECORATION_CLASS,
    DECORATION_IDS,
    PERMISSION_PACKAGES,
    SCROLL_CONTAINER_CLASSES,
    SCROLL_POINT_CLASSES,
)
from uiprobe.guards.tree import _is_dialog, _is_text_field, _Tree

if TYPE_CHECKING:
    from collections.abc import Iterable

    from uiprobe.guards.policy import Judgement
    from uiprobe.model import Bounds, Element, Screen

_SCROLLABLE = "scrollable"
_LONG_CLICKABLE = "long_clickable"
_PASSWORD = "password"  # noqa: S105 (a flag name)
# The grid of candidate points, in quarters of the container's visible width and
# height: the center first, then the others.
_GRID = ((2, 2), (1, 2), (3, 2), (2, 1), (2, 3), (1, 1), (3, 1), (1, 3), (3, 3))

Point = tuple[int, int]


def scroll_container(screen: Screen) -> Element | None:
    """The container of a wheel scroll: the innermost list container of the
    largest scrollable element.

    The largest scrollable element with a visible part (the first of equals, in
    document order) must have exactly one of `SCROLL_CONTAINER_CLASSES`; None
    when there is none or it has another class (no other element is tried). Then,
    if scrollable elements of those classes lie inside it (a RecyclerView in the
    ScrollView that is the page, as on the Settings screens), the container is
    the largest of the innermost ones: the scrollable list container with no
    scrollable list container inside it that has a visible part. Otherwise it is
    that element itself. A scrollable element of another class inside the
    container does not change the choice: it is refused at the point (see
    `scroll_target`)."""
    best = _largest_scrollable(screen, screen.elements)
    if best is None or best.class_name not in SCROLL_CONTAINER_CLASSES:
        return None
    below = [
        e
        for e in screen.elements
        if e.id != best.id
        and _SCROLLABLE in e.flags
        and e.class_name in SCROLL_CONTAINER_CLASSES
        and screen.visible_bounds(e) is not None
        and screen._is_within(e, best)  # noqa: SLF001
    ]
    innermost = [
        e
        for e in below
        if not any(o.id != e.id and screen._is_within(o, e) for o in below)  # noqa: SLF001
    ]
    return _largest_scrollable(screen, innermost) or best


def _largest_scrollable(screen: Screen, elements: Iterable[Element]) -> Element | None:
    """The scrollable element with the largest visible part (the first of
    equals), or None."""
    best: tuple[int, Element] | None = None
    for element in elements:
        if _SCROLLABLE not in element.flags:
            continue
        part = screen.visible_bounds(element)
        if part is None:
            continue
        area = part.width * part.height
        if best is None or area > best[0]:
            best = (area, element)
    return None if best is None else best[1]


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


def _is_decoration(screen: Screen, element: Element) -> bool:
    """Whether the element is the one decoration allowed beside the list:
    exactly `DECORATION_CLASS`, a resource id of the form ``<package>:id/<name>``
    with a name in `DECORATION_IDS`, no flag at all, no text, description or
    hint, not editable and no child element in the dump. (The checks of
    `_allowed` for package, judge and deny words apply to it as to any element.)
    """
    owner, separator, name = element.resource_id.partition(_ID_SEPARATOR)
    return (
        element.class_name == DECORATION_CLASS
        and bool(owner)
        and bool(separator)
        and name in DECORATION_IDS
        and not element.flags
        and not (element.text or element.content_desc or element.hint)
        and not element.editable
        and not any(e.parent == element.id for e in screen.elements)
    )


def _allowed(
    screen: Screen,
    element: Element,
    container: Element,
    refused_ids: frozenset[str],
) -> bool:
    """Whether an element at the point leaves the point qualified.

    Where it stands decides its class. The container itself is fine. Inside the
    container it must have exactly one of `SCROLL_POINT_CLASSES` (or be a list
    container class that is not scrollable, a view that cannot scroll) and not be
    scrollable. An ancestor of the container (the page the list lies in) may
    have a list container class, scrollable or not, or a plain class that is not
    scrollable: a ViewPager, a HorizontalScrollView or a plain layout flagged
    scrollable above the list is refused. Anything that is neither the container,
    inside it nor above it is refused. In every place the element must not be
    long-clickable, checkable, editable, a password field, a text field or refused
    by judge. A plain clickable row is allowed.
    """
    scrollable = _SCROLLABLE in element.flags
    if element.id == container.id:
        pass
    elif screen._is_within(element, container):  # noqa: SLF001
        if scrollable or not (
            element.class_name in SCROLL_POINT_CLASSES
            or element.class_name in SCROLL_CONTAINER_CLASSES
        ):
            return False
    elif screen._is_within(container, element):  # noqa: SLF001
        if not (
            element.class_name in SCROLL_CONTAINER_CLASSES
            or (element.class_name in SCROLL_POINT_CLASSES and not scrollable)
        ):
            return False
    else:
        return False
    return not (
        element.id in refused_ids
        or _LONG_CLICKABLE in element.flags
        or _CHECKABLE in element.flags
        or _PASSWORD in element.flags
        or element.editable
        or _is_text_field(element)
    )


def scroll_target(screen: Screen, judgement: Judgement) -> tuple[str, Point] | None:
    """Return ``(container id, point)`` for a wheel turned on ``screen``, or
    None when it must not be turned.

    None when the screen is a dialog, is judged irreversible or belongs to a
    permission package (`PERMISSION_PACKAGES`), when there is no
    container (`scroll_container`), or when no candidate point qualifies. The
    candidates are the container's visible center and then the other points of a
    3 by 3 grid (quarters of its visible part), in that order. A point qualifies
    when EVERY element whose part on the display holds it (labelled or not,
    clickable or not, whatever its size, siblings of the list included) passes
    `_allowed` and is the container, inside it or one of its ancestors, and the
    topmost labelled element there is the container or inside it. The allow
    list is redundant with the checks of the flags, which are kept as an extra
    layer: scrollable (other than the container), long-clickable, checkable,
    password, editable and text field are refused whatever their class. A point
    in a screen-edge area of the judge (`Judgement.edges`, where a tap gets
    ``screen_edge``) never qualifies.
    """
    if judgement.dialog or judgement.irreversible:
        return None
    if screen.package in PERMISSION_PACKAGES:
        return None  # a tap there gets `permission_screen`; the wheel gets nothing
    container = scroll_container(screen)
    if container is None:
        return None
    area = screen.visible_bounds(container)
    if area is None:
        return None
    refused = judgement.unsafe | frozenset(
        d.element.id for d in judgement.decisions if d.reasons
    )
    for col, row in _GRID:
        point = (
            area.left + area.width * col // 4,
            area.top + area.height * row // 4,
        )
        if any(
            e.left <= point[0] < e.right and e.top <= point[1] < e.bottom
            for e in judgement.edges
        ):
            continue  # the judge's screen_edge areas, as for a tap
        held = elements_at(screen, point)
        if not all(_allowed(screen, e, container, refused) for e in held):
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


def lists_key(screen: Screen) -> str:
    """A name for the screen by its lists, not by which of them is the largest
    and not by anything else on it: the activity and, sorted, the class and
    resource id of every scrollable element with the classes of its ancestors.
    The per-screen scroll cap is also counted under it, so a header that changes
    the screen id when BACK returns to the screen, together with another list
    becoming the largest, does not give the screen a new count."""
    by_id = {e.id: e for e in screen.elements}
    names = []
    for element in screen.elements:
        if _SCROLLABLE not in element.flags:
            continue
        parts = [element.class_name, element.resource_id]
        seen = {element.id}
        parent = element.parent
        while parent is not None and parent not in seen and parent in by_id:
            seen.add(parent)
            parts.append(by_id[parent].class_name)
            parent = by_id[parent].parent
        names.append("|".join(parts))
    return "\n".join([screen.activity or "", *sorted(set(names))])


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


def changed_by_wheel(
    before: Screen,
    after: Screen,
    key: str | None = None,
    container: str | None = None,
) -> bool:
    """Whether the screen shows that a wheel turn changed something other than
    the scroll position, comparing the screen read before it with the one after.

    ``container`` is the id, on `before`, of the element that was scrolled, and
    ``key`` its `container_key`. Only what is below that one element is left out
    of the comparison (on `after`: below the element with that key whose bounds
    overlap the container's most), not everything below any element with the same
    key: a sibling list that was not scrolled is compared. Two signs. (1) A
    checkable element outside the list with the same text-inclusive identity
    (class, resource id, text, description, hint and the ancestors' classes and
    resource ids) on both screens whose checked states differ (the sorted states
    of all such elements are compared per identity, when there are as many of
    them on both screens: identical rows that scrolled in or out are no change).
    (2) Outside the list: elements with the same text-free identity (class,
    resource id and the ancestors' classes and resource ids), taken in document
    order, where the n-th text or description or checked state differs on the two
    screens (groups of another size on the two screens cannot be paired and are
    not compared). What is inside the scrolled list is not compared by text,
    because its rows move and change as it scrolls: a sole row replaced by
    another is no change. Without ``key`` and ``container`` the list is not
    known, and only elements whose identity is unique on both screens are
    compared, as rows are told apart by their identity not being unique. A clock
    or a counter that changes between the reads is taken as a change too, which
    only costs a scroll.
    """
    skips: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
    if key is not None and container is not None:
        skips = scrolled_below(before, after, key, container)
    known = key is not None and container is not None
    return _checked_changed(before, after, skips) or _text_changed(
        before, after, skips, known=known
    )


def _children(screen: Screen) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for element in screen.elements:
        if element.parent is not None:
            children.setdefault(element.parent, []).append(element.id)
    return children


def _below(screen: Screen, root: str | None) -> frozenset[str]:
    """The ids of the elements below the element `root` (not itself)."""
    if root is None:
        return frozenset()
    children = _children(screen)
    found: set[str] = set()
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found and child != root:
                found.add(child)
                stack.append(child)
    return frozenset(found)


def _overlap(a: Bounds, b: Bounds) -> int:
    width = min(a.right, b.right) - max(a.left, b.left)
    height = min(a.bottom, b.bottom) - max(a.top, b.top)
    return width * height if width > 0 and height > 0 else 0


def scrolled_container_on(
    screen: Screen, key: str, container: Element
) -> Element | None:
    """The element of `screen` that is the scrolled container of another read:
    a scrollable element with the same `container_key` whose bounds overlap
    the container's most (a scroll does not move the container); None when no
    such element overlaps it at all."""
    best: tuple[int, Element] | None = None
    for element in screen.elements:
        if _SCROLLABLE not in element.flags or container_key(screen, element) != key:
            continue
        area = _overlap(element.bounds, container.bounds)
        if area > 0 and (best is None or area > best[0]):
            best = (area, element)
    return None if best is None else best[1]


def scrolled_below(
    before: Screen, after: Screen, key: str, container: str
) -> tuple[frozenset[str], frozenset[str]]:
    """The ids below the scrolled container on `before` and on `after`."""
    planned = next((e for e in before.elements if e.id == container), None)
    if planned is None:
        return frozenset(), frozenset()
    found = scrolled_container_on(after, key, planned)
    return _below(before, planned.id), _below(
        after, None if found is None else found.id
    )


def _checked_changed(
    before: Screen, after: Screen, skips: tuple[frozenset[str], frozenset[str]]
) -> bool:
    def states(
        screen: Screen, skip: frozenset[str]
    ) -> dict[tuple[object, ...], list[bool]]:
        by_id = {e.id: e for e in screen.elements}
        found: dict[tuple[object, ...], list[bool]] = {}
        for element in screen.elements:
            if _CHECKABLE not in element.flags or element.id in skip:
                continue
            identity = (
                element.class_name,
                element.resource_id,
                element.text,
                element.content_desc,
                element.hint,
                _ancestry(element, by_id),
            )
            found.setdefault(identity, []).append("checked" in element.flags)
        return found

    old, new = states(before, skips[0]), states(after, skips[1])
    # Groups of another size cannot be paired: rows of identical identity that
    # scrolled in or out (two identical switch rows scrolling down to one) are
    # no change.
    return any(
        identity in new
        and len(flags) == len(new[identity])
        and sorted(flags) != sorted(new[identity])
        for identity, flags in old.items()
    )


def _text_changed(
    before: Screen,
    after: Screen,
    skips: tuple[frozenset[str], frozenset[str]],
    *,
    known: bool,
) -> bool:
    def identities(
        screen: Screen, skip: frozenset[str]
    ) -> dict[tuple[object, ...], list[Element]]:
        by_id = {e.id: e for e in screen.elements}
        found: dict[tuple[object, ...], list[Element]] = {}
        for element in screen.elements:
            if element.id in skip:
                continue
            identity = (
                element.class_name,
                element.resource_id,
                _ancestry(element, by_id),
            )
            found.setdefault(identity, []).append(element)
        return found

    old, new = identities(before, skips[0]), identities(after, skips[1])
    for identity, elements in old.items():
        now = new.get(identity, [])
        if len(elements) != len(now) or (not known and len(elements) != 1):
            continue
        for was, here in zip(elements, now, strict=True):
            if (
                was.text != here.text
                or was.content_desc != here.content_desc
                or ("checked" in was.flags) != ("checked" in here.flags)
            ):
                return True
    return False
