"""Data model of a screen: bounds, elements, and the uiautomator XML parser.

The XML a device returns is controlled by the app on screen, so it is treated
as untrusted: documents that declare a DOCTYPE or an ENTITY are rejected, and
every app-provided string is escaped before it is printed in the tree view.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from uiprobe._json import dumps
from uiprobe.errors import (
    DumpError,
    ElementNotFoundError,
    ElementNotVisibleError,
    MultipleElementsError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

__all__ = [
    "MIN_VISIBLE_PX",
    "SCREEN_SCHEMA",
    "Bounds",
    "Element",
    "Screen",
    "UiautomatorDump",
    "parse_uiautomator_dump",
    "parse_uiautomator_xml",
    "shorten_activity",
]

SCREEN_SCHEMA = "uiprobe.screen/1"
MAX_NODES = 100_000
# Deepest element nesting accepted (the hierarchy element counts). Real screens
# stay far below it; the limit keeps the tree view, whose indent grows with the
# depth, from exploding.
MAX_DEPTH = 256
MAX_CANDIDATES = 20
# An element counts as visible only when the part of it on the display is at
# least this many pixels wide and this many pixels high. Thinner slivers (two
# pixels of a list row at the bottom edge, the thin strip Android reports for a
# WebView field scrolled out of view) are not visible: nothing can be pressed
# there on purpose.
MIN_VISIBLE_PX: Final = 8
_FEED_CHUNK = 64 * 1024

_BOUNDS_RE = re.compile(
    r"\[(-?[0-9]{1,9}),(-?[0-9]{1,9})\]\[(-?[0-9]{1,9}),(-?[0-9]{1,9})\]"
)
# App-provided names that may be printed without quotes.
_PLAIN_NAME_RE = re.compile(r"[A-Za-z0-9_.$]+")
_DOCTYPE_RE = re.compile(rb"<\s*!\s*(doctype|entity)", re.IGNORECASE)
_ID_SEPARATOR = ":id/"

# XML attribute -> flag name, in the fixed order flags are reported.
_FLAG_ATTRIBUTES = (
    ("checkable", "checkable"),
    ("checked", "checked"),
    ("clickable", "clickable"),
    ("long-clickable", "long_clickable"),
    ("scrollable", "scrollable"),
    ("focusable", "focusable"),
    ("focused", "focused"),
    ("selected", "selected"),
    ("password", "password"),
)

# Flags that appear as tags in the tree view.
_FLAG_TAGS = (
    ("clickable", "[tap]"),
    ("long_clickable", "[long]"),
    ("scrollable", "[scroll]"),
    ("checked", "[checked]"),
    ("password", "[password]"),
    ("disabled", "[disabled]"),
)

# Unicode categories that must not reach a terminal raw: controls, format
# characters (zero-width and bidi controls, tag characters), surrogates, private
# use, unassigned code points, and the line and paragraph separators.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


@dataclass(frozen=True, slots=True)
class Bounds:
    """A rectangle on the display, in pixels."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        """Width in pixels."""
        return self.right - self.left

    @property
    def height(self) -> int:
        """Height in pixels."""
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        """Center point as ``(x, y)``, rounded down."""
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def to_list(self) -> list[int]:
        """Return ``[left, top, right, bottom]``."""
        return [self.left, self.top, self.right, self.bottom]

    @classmethod
    def parse(cls, text: str) -> Bounds:
        """Parse the uiautomator form ``[0,0][1080,2400]``.

        Raises:
            DumpError: The text is not in that form.
        """
        match = _BOUNDS_RE.fullmatch(text)
        if match is None:
            raise DumpError(
                "Malformed bounds in UI dump", details={"bounds": text[:80]}
            )
        left, top, right, bottom = (int(group) for group in match.groups())
        return cls(left, top, right, bottom)


@dataclass(frozen=True, slots=True)
class Element:
    """One node of the UI hierarchy.

    ``id`` is ``e1``, ``e2``, ... in document pre-order; ``parent`` is the id of
    the enclosing element, or None for a top-level node.
    """

    id: str
    parent: str | None
    class_name: str
    bounds: Bounds
    text: str = ""
    resource_id: str = ""
    content_desc: str = ""
    hint: str = ""
    package: str = ""
    flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return the JSON form; empty optional values are omitted."""
        data: dict[str, object] = {"id": self.id}
        if self.parent is not None:
            data["parent"] = self.parent
        data["class"] = self.class_name
        optional = {
            "text": self.text,
            "resource_id": self.resource_id,
            "content_desc": self.content_desc,
            "hint": self.hint,
        }
        data.update({key: value for key, value in optional.items() if value})
        data["bounds"] = self.bounds.to_list()
        if self.flags:
            data["flags"] = list(self.flags)
        return data

    @property
    def is_meaningful(self) -> bool:
        """True when a person could tell what the element is or does.

        That is the case when it has the ``clickable`` flag, or a text, content
        description or hint that is not empty after whitespace is stripped.
        Only meaningful elements count when `Screen.hit_target` looks for what
        is on top at a point.
        """
        return "clickable" in self.flags or any(
            value.strip() for value in (self.text, self.content_desc, self.hint)
        )

    def matches_resource_id(self, resource_id: str) -> bool:
        """Return True if the full id or the part after ``:id/`` equals it."""
        if self.resource_id == resource_id:
            return True
        _, separator, short = self.resource_id.partition(_ID_SEPARATOR)
        return bool(separator) and short == resource_id


def shorten_activity(package: str, activity: str) -> str:
    """Write ``pkg.Name`` as ``.Name`` when the activity lives in the package."""
    prefix = package + "."
    if package and activity.startswith(prefix):
        return "." + activity[len(prefix) :]
    return activity


@dataclass(frozen=True)
class Screen:
    """A captured screen: where it came from and its elements."""

    serial: str
    captured_at: datetime
    package: str
    activity: str | None
    api_level: int | None
    display: tuple[int, int]
    elements: tuple[Element, ...]

    @property
    def screen_id(self) -> str:
        """A short name for the screen layout.

        Built from the package, the activity, and the class and resource id of
        every element in order. Text and bounds are left out, so a changing
        clock keeps the same id. The number of elements counts: a list that
        gains or loses rows gets another id.
        """
        parts = [self.package, self.activity or ""]
        for element in self.elements:
            parts.append(element.class_name)
            parts.append(element.resource_id)
        digest = hashlib.sha256("\n".join(parts).encode("utf-8", "replace"))
        return "s-" + digest.hexdigest()[:12]

    def to_dict(self) -> dict[str, object]:
        """Return the ``uiprobe.screen/1`` JSON object."""
        device: dict[str, object] = {"serial": self.serial}
        if self.api_level is not None:
            device["api_level"] = self.api_level
        app: dict[str, object] = {"package": self.package}
        if self.activity is not None:
            app["activity"] = self.activity
        return {
            "schema": SCREEN_SCHEMA,
            "captured_at": self.captured_at.isoformat(timespec="seconds"),
            "device": device,
            "app": app,
            "screen_id": self.screen_id,
            "display": {"width": self.display[0], "height": self.display[1]},
            "elements": [element.to_dict() for element in self.elements],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialize `to_dict` as JSON.

        Readable non-ASCII text is kept; terminal controls, line separators
        and bidirectional controls are written as ``\\uXXXX`` escapes.
        """
        return dumps(self.to_dict(), indent=indent)

    def to_tree(self) -> str:
        """Render the indented, human-readable view of the screen."""
        head = _name(self.package)
        if self.activity is not None:
            head += "/" + _name(self.activity)
        lines = [f"{head}  ({self.screen_id})"]
        lines.extend(self._tree_lines())
        return "\n".join(lines)

    def _tree_lines(self) -> list[str]:
        known = {element.id for element in self.elements}
        children: dict[str | None, list[Element]] = {}
        for element in self.elements:
            parent = element.parent if element.parent in known else None
            children.setdefault(parent, []).append(element)
        lines: list[str] = []
        # Each entry: element, prefix of its own line, prefix for its children.
        stack = [(top, "", "") for top in reversed(children.get(None, []))]
        while stack:
            element, line_prefix, child_prefix = stack.pop()
            lines.append(line_prefix + _describe(element))
            kids = children.get(element.id, [])
            for index in range(len(kids) - 1, -1, -1):
                last = index == len(kids) - 1
                branch = "└ " if last else "├ "
                cont = "  " if last else "│ "
                stack.append((kids[index], child_prefix + branch, child_prefix + cont))
        return lines

    def _clip(self, element: Element) -> Bounds | None:
        """Return the element's bounds cut to the display, or None when empty."""
        bounds = element.bounds
        width, height = self.display
        left = max(bounds.left, 0)
        top = max(bounds.top, 0)
        right = min(bounds.right, width)
        bottom = min(bounds.bottom, height)
        if right <= left or bottom <= top:
            return None
        return Bounds(left, top, right, bottom)

    def visible_bounds(self, element: Element) -> Bounds | None:
        """Return the part of the element that is on the display, or None.

        The part is the element's bounds intersected with the display. An
        element has none when that part is empty (zero width or height, left
        and right or top and bottom the wrong way round as seen on API 36,
        ``[80,623][203,616]``, or entirely outside the display) and also when
        it is narrower than `MIN_VISIBLE_PX` pixels or lower than
        `MIN_VISIBLE_PX` pixels: a sliver is not visible. Right and bottom are
        exclusive, so a part from 10 to 18 is 8 pixels wide.
        """
        clipped = self._clip(element)
        if clipped is None or min(clipped.width, clipped.height) < MIN_VISIBLE_PX:
            return None
        return clipped

    def require_visible(self, element: Element) -> Bounds:
        """Return `visible_bounds` of the element; raise when there is none.

        Raises:
            ElementNotVisibleError: The element has no visible part. ``details``
                hold ``element`` (the id), ``bounds`` and ``reason``:
                ``no_visible_part`` when nothing of it is on the display, or
                ``too_small`` (plus ``visible_size``, ``[width, height]`` of the
                part on the display) when the part is under `MIN_VISIBLE_PX`
                pixels wide or high.
        """
        visible = self.visible_bounds(element)
        if visible is not None:
            return visible
        details: dict[str, object] = {
            "element": element.id,
            "bounds": element.bounds.to_list(),
        }
        clipped = self._clip(element)
        if clipped is None:
            details["reason"] = "no_visible_part"
            msg = f"element {element.id} has no visible part"
        else:
            details["reason"] = "too_small"
            details["visible_size"] = [clipped.width, clipped.height]
            msg = (
                f"element {element.id} is too small to press: "
                f"{clipped.width}x{clipped.height} pixels show, "
                f"at least {MIN_VISIBLE_PX}x{MIN_VISIBLE_PX} are needed"
            )
        raise ElementNotVisibleError(msg, details=details)

    def hit_target(self, point: tuple[int, int]) -> Element | None:
        """Return the topmost meaningful element on the display at the point.

        Elements are drawn in document order, so the last element in the list
        is on top. Only meaningful elements count (see `Element.is_meaningful`:
        clickable, or a non-empty text, content description or hint), so an
        empty layout container on top is looked through. Any meaningful element
        on top counts, whatever its size: the part on the display is the
        bounds cut to the display with no size threshold, so a sliver under
        `MIN_VISIBLE_PX` still hits, because it would receive the touch. (The
        8-pixel minimum applies to the element being pressed, not to what
        covers it.) The part is half-open: ``left <= x < right`` and
        ``top <= y < bottom``; an empty part never holds a point. Returns None
        when no meaningful element holds the point.
        """
        x, y = point
        for element in reversed(self.elements):
            if not element.is_meaningful:
                continue
            part = self._clip(element)
            if (
                part is not None
                and part.left <= x < part.right
                and part.top <= y < part.bottom
            ):
                return element
        return None

    def _is_within(self, element: Element, ancestor: Element) -> bool:
        """True when `element` is `ancestor` or one of its descendants."""
        by_id = {e.id: e for e in self.elements}
        current: Element | None = element
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            if current is ancestor or current.id == ancestor.id:
                return True
            seen.add(current.id)
            current = None if current.parent is None else by_id.get(current.parent)
        return False

    def tap_point(self, element: Element) -> tuple[int, int]:
        """Return the point to press for `element`, as ``(x, y)``.

        It is the center of the visible part (see `visible_bounds`), rounded
        down. This is the ``center`` that ``uiprobe find`` prints and the point
        ``uiprobe tap`` presses.

        The element must also be on top there: `hit_target` at the point must
        be the element or one of its descendants (a label inside a button is
        fine). When another meaningful element is on top, a press would land
        on that one, so the element is refused. That includes an ancestor: an
        element that is not meaningful (an unlabeled icon) inside a clickable
        row is refused, because the row, not the icon, would receive the press
        (``covered_by`` is the row; the message says so). This also holds for an
        element selected by its resource id inside a clickable row when it has no
        text, description or hint and is not clickable: press the row instead.
        Any meaningful element on top counts, whatever its size; the 8-pixel
        minimum applies only to the element being pressed. When no meaningful
        element holds the point (only possible for an element that is not
        meaningful itself, for example an isolated one with only a resource id)
        nothing at the point can receive a press, so the element is refused
        with the reason ``no_target``.

        Raises:
            ElementNotVisibleError: The element has no visible part, is too
                small, is covered, or has no press target. ``details`` hold
                ``element`` (the id), ``bounds`` and ``reason``
                (``no_visible_part``, ``too_small`` with ``visible_size``,
                ``covered`` with ``covered_by``, the id of the element on top,
                and ``point``, or ``no_target`` with ``point``).
        """
        point = self.require_visible(element).center
        top = self.hit_target(point)
        where = f"{point[0]},{point[1]}"
        details: dict[str, object] = {
            "element": element.id,
            "bounds": element.bounds.to_list(),
            "point": list(point),
        }
        if top is None:
            details["reason"] = "no_target"
            msg = (
                f"element {element.id} cannot be pressed: nothing that can "
                f"receive a press is at {where}"
            )
            raise ElementNotVisibleError(msg, details=details)
        if top is element or self._is_within(top, element):
            return point
        if self._is_within(element, top):
            msg = (
                f"element {element.id} is not meaningful and its ancestor "
                f"{top.id} would receive the press at {where}"
            )
        else:
            msg = f"element {element.id} is covered by {top.id} at {where}"
        details["reason"] = "covered"
        details["covered_by"] = top.id
        raise ElementNotVisibleError(msg, details=details)

    def find(
        self,
        *,
        text: str | None = None,
        resource_id: str | None = None,
        desc: str | None = None,
    ) -> list[Element]:
        """Return the visible elements matching every given criterion.

        ``text`` is compared exactly after leading and trailing whitespace is
        removed from both sides. ``resource_id`` matches the full id or the
        part after ``:id/``. ``desc`` is compared exactly. An element with no
        visible part (see `visible_bounds`, which includes the `MIN_VISIBLE_PX`
        rule) never matches. A covered element still matches and is listed
        (see `tap_point`): ``find`` counts visible elements, and only a press
        is refused.

        Raises:
            ValueError: No criterion was given.
        """
        _criteria(text, resource_id, desc)
        return [
            element
            for element in self._matching(text, resource_id, desc)
            if self.visible_bounds(element) is not None
        ]

    def _matching(
        self, text: str | None, resource_id: str | None, desc: str | None
    ) -> list[Element]:
        """Return the elements matching every given criterion that have a
        non-empty part on the display, whatever its size (no 8-pixel minimum).
        """
        wanted = None if text is None else text.strip()
        return [
            element
            for element in self.elements
            if self._clip(element) is not None
            and (wanted is None or element.text.strip() == wanted)
            and (resource_id is None or element.matches_resource_id(resource_id))
            and (desc is None or element.content_desc == desc)
        ]

    def _element_dict(self, element: Element) -> dict[str, object]:
        """Return the element's JSON form plus ``center``, the point to press.

        ``center`` is ``[x, y]``, the center of the visible part rounded down;
        it is left out for an element that has no visible part (see
        `visible_bounds`), for a covered one and for one with no press target
        (see `tap_point`).
        """
        return self._element_dict_at(element, self._press_center(element))

    def _press_center(self, element: Element) -> tuple[int, int] | None:
        """Return `tap_point`, or None when the element is not pressable."""
        try:
            return self.tap_point(element)
        except ElementNotVisibleError:
            return None

    def _element_dict_at(
        self, element: Element, center: tuple[int, int] | None
    ) -> dict[str, object]:
        """Return the element's JSON form, with ``center`` when it is given.

        For a caller that knows the press point already (the crawl preview
        takes it from its decisions), so no per-element scan of the screen
        is needed.
        """
        data = element.to_dict()
        if center is not None:
            data["center"] = list(center)
        return data

    def _describe_line(self, element: Element) -> str:
        """Render one element as a line of the tree view plus its center.

        As for `_element_dict`, the center is left out when the element is not
        visible or is covered.
        """
        return self._describe_line_at(element, self._press_center(element))

    def _describe_line_at(
        self, element: Element, center: tuple[int, int] | None
    ) -> str:
        """Like `_describe_line`, for a caller that knows the press point."""
        line = _describe(element)
        if center is None:
            return line
        return f"{line} center={center[0]},{center[1]}"

    def _near_misses(
        self,
        *,
        text: str | None = None,
        resource_id: str | None = None,
        desc: str | None = None,
    ) -> list[Element]:
        """Return the visible elements to suggest when nothing matches.

        These are the elements where at least one given criterion appears in
        the element's own field (text, resource id, description) after NFKC
        normalization and case folding. When there are none, they are the
        visible clickable elements that are not disabled.

        Raises:
            ValueError: No criterion was given.
        """
        _criteria(text, resource_id, desc)
        needles = [
            (field, _fold(value))
            for field, value in (
                ("text", text),
                ("resource_id", resource_id),
                ("content_desc", desc),
            )
            if value is not None and _fold(value)
        ]
        visible = [e for e in self.elements if self.visible_bounds(e) is not None]
        partial = [
            element
            for element in visible
            if any(
                needle in _fold(getattr(element, field)) for field, needle in needles
            )
        ]
        if partial:
            return partial
        return [
            element
            for element in visible
            if "clickable" in element.flags and "disabled" not in element.flags
        ]

    def find_one(
        self,
        *,
        text: str | None = None,
        resource_id: str | None = None,
        desc: str | None = None,
    ) -> Element:
        """Return the single element matching the criteria.

        Matches are counted over every element with a non-empty part on the
        display, whatever its size, so a thin sliver counts as a second match
        and the choice stays ambiguous (the 8-pixel minimum applies only to
        the element chosen).
        Exactly one match under `MIN_VISIBLE_PX` is refused with
        ``ElementNotVisibleError`` (``too_small``, see `require_visible`).

        The ``details`` of ``ElementNotFoundError`` and
        ``MultipleElementsError`` hold ``criteria``, ``total`` (the number
        of candidates) and ``candidates`` (at most `MAX_CANDIDATES`): for
        several matches, the matches; for none, the elements that partly match
        or, failing that, the clickable ones that are not disabled. A candidate
        is the element's JSON form plus ``center`` (left out when the element is
        not pressable, see `tap_point`).

        Raises:
            ValueError: No criterion was given.
            ElementNotFoundError: Nothing matches.
            MultipleElementsError: More than one element matches.
            ElementNotVisibleError: The only match is under `MIN_VISIBLE_PX`
                pixels wide or high (``too_small``).
        """
        criteria = _criteria(text, resource_id, desc)
        found = self._matching(text, resource_id, desc)
        if len(found) == 1:
            self.require_visible(found[0])
            return found[0]
        if found:
            error: type[ElementNotFoundError | MultipleElementsError] = (
                MultipleElementsError
            )
            message = f"{len(found)} elements match the criteria"
            shown = found
        else:
            error = ElementNotFoundError
            message = "No element matches the criteria"
            shown = self._near_misses(text=text, resource_id=resource_id, desc=desc)
        raise error(
            message,
            details={
                "criteria": criteria,
                "total": len(shown),
                "candidates": [
                    self._element_dict(element) for element in shown[:MAX_CANDIDATES]
                ],
            },
        )


def _fold(value: str) -> str:
    """Normalize text for a partial comparison: strip, NFKC, then casefold."""
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def _criteria(
    text: str | None, resource_id: str | None, desc: str | None
) -> dict[str, str]:
    """Collect the given criteria, rejecting an empty selection."""
    criteria = {
        key: value
        for key, value in (
            ("text", text),
            ("resource_id", resource_id),
            ("content_desc", desc),
        )
        if value is not None
    }
    if not criteria:
        raise ValueError("give at least one of text, resource_id, desc")
    return criteria


def _escape_char(char: str) -> str:
    code = ord(char)
    if unicodedata.category(char) not in _UNSAFE_CATEGORIES:
        return char
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _escape(value: str) -> str:
    """Escape unsafe characters; the result has no quotes around it."""
    return "".join(
        _escape_char(char) for char in json.dumps(value, ensure_ascii=False)[1:-1]
    )


def _name(value: str) -> str:
    """Return an app-provided identifier, quoted unless it is a plain name.

    Quoting stops an app from forging tags such as ``[tap]`` or a second screen
    id with spaces and brackets in a resource id, class name or package.
    """
    if not value or _PLAIN_NAME_RE.fullmatch(value):
        return value
    return _quote(value)


def _quote(value: str) -> str:
    """Return a double-quoted, escaped string for the tree view."""
    return f'"{_escape(value)}"'


def _describe(element: Element) -> str:
    """Render one element as a tree line without its tree prefix."""
    parts = [element.id, _name(element.class_name.rsplit(".", 1)[-1])]
    if element.text:
        parts.append(_quote(element.text))
    if element.resource_id:
        short = element.resource_id.partition(_ID_SEPARATOR)[2] or element.resource_id
        parts.append(f"id={_name(short)}")
    if element.content_desc:
        parts.append(f"desc={_quote(element.content_desc)}")
    if element.hint:
        parts.append(f"hint={_quote(element.hint)}")
    parts.extend(tag for flag, tag in _FLAG_TAGS if flag in element.flags)
    return " ".join(parts)


def _check_declarations(data: bytes | str) -> None:
    """Refuse DOCTYPE and ENTITY declarations.

    The check runs on the raw bytes, also with NUL bytes removed so that
    UTF-16 and UTF-32 documents are covered. Expat does not autodetect other
    encodings that hide ASCII text, so those fail as malformed XML.
    """
    raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
    if _DOCTYPE_RE.search(raw) or _DOCTYPE_RE.search(raw.replace(b"\x00", b"")):
        raise DumpError("UI dump contains a DOCTYPE or ENTITY declaration")


class _Limits:
    """Watches the parser's events for elements, nesting and the document element."""

    def __init__(self) -> None:
        self.nodes = 0
        self.depth = 0
        self.root: ET.Element | None = None

    def consume(self, parser: ET.XMLPullParser) -> None:
        """Check every event the parser has produced since the last call."""
        # With events=("start", "end") every event is (name, element).
        events = cast("Iterator[tuple[str, ET.Element]]", parser.read_events())
        for event, element in events:
            if event == "end":
                self.depth -= 1
                continue
            self.depth += 1
            if self.root is None:
                self.root = element
            self._check()

    def _check(self) -> None:
        # Every element counts, whatever its tag: unknown ones are ignored by
        # the model but still cost memory while parsing.
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise DumpError(
                "UI dump has too many nodes", details={"max_nodes": MAX_NODES}
            )
        if self.depth > MAX_DEPTH:
            raise DumpError(
                "UI dump is nested too deeply", details={"max_depth": MAX_DEPTH}
            )


def _parse_root(data: bytes | str) -> ET.Element:
    """Parse the document in chunks and stop as soon as a limit is passed.

    Feeding the parser in chunks and checking the node count and depth after
    each one stops a hostile document before its whole tree is built.
    """
    _check_declarations(data)
    parser = ET.XMLPullParser(events=("start", "end"))
    limits = _Limits()
    try:
        for start in range(0, len(data), _FEED_CHUNK):
            parser.feed(data[start : start + _FEED_CHUNK])
            limits.consume(parser)
        parser.close()
        limits.consume(parser)
    except (ET.ParseError, LookupError, ValueError) as error:
        raise DumpError(
            "UI dump is not well-formed XML", details={"reason": str(error)}
        ) from error
    if limits.root is None:
        raise DumpError("UI dump has no document element")
    return limits.root


def _flags(node: ET.Element) -> tuple[str, ...]:
    flags = [
        name for attribute, name in _FLAG_ATTRIBUTES if node.get(attribute) == "true"
    ]
    if node.get("enabled") == "false":
        flags.append("disabled")
    return tuple(flags)


def _element(node: ET.Element, element_id: str, parent: str | None) -> Element:
    return Element(
        id=element_id,
        parent=parent,
        class_name=node.get("class", ""),
        bounds=Bounds.parse(node.get("bounds", "")),
        text=node.get("text", ""),
        resource_id=node.get("resource-id", ""),
        content_desc=node.get("content-desc", ""),
        hint=node.get("hint", ""),
        package=node.get("package", ""),
        flags=_flags(node),
    )


@dataclass(frozen=True, slots=True)
class UiautomatorDump:
    """A parsed ``uiautomator dump``: its elements and the screen rotation.

    `rotation` is the ``rotation`` attribute of the hierarchy element (0 to 3,
    in quarter turns); it is 0 when the attribute is missing or invalid.
    """

    elements: list[Element]
    rotation: int


def parse_uiautomator_xml(data: bytes | str) -> list[Element]:
    """Parse a ``uiautomator dump`` document into elements in pre-order.

    Raises:
        DumpError: The XML is malformed, declares a DOCTYPE or ENTITY, has the
            wrong root element, has bad bounds, has too many nodes, or is nested
            too deeply.
    """
    return parse_uiautomator_dump(data).elements


def parse_uiautomator_dump(data: bytes | str) -> UiautomatorDump:
    """Parse a ``uiautomator dump`` document into elements and the rotation.

    Raises:
        DumpError: As for `parse_uiautomator_xml`.
    """
    root = _parse_root(data)
    if root.tag != "hierarchy":
        raise DumpError("UI dump has no hierarchy element", details={"root": root.tag})
    elements: list[Element] = []
    stack: list[tuple[ET.Element, str | None]] = [
        (child, None) for child in reversed(list(root)) if child.tag == "node"
    ]
    while stack:
        node, parent = stack.pop()
        element = _element(node, f"e{len(elements) + 1}", parent)
        elements.append(element)
        stack.extend(
            (child, element.id) for child in reversed(list(node)) if child.tag == "node"
        )
    rotation = root.get("rotation", "")
    return UiautomatorDump(
        elements, int(rotation) if rotation in {"0", "1", "2", "3"} else 0
    )
