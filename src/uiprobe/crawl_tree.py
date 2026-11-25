"""Read a crawl run folder and build the tree of what the crawl saw and pressed.

Only the run folder is read (``run-start.json``, ``run.json``,
``transitions.jsonl``, ``skipped.jsonl`` and ``screens/*.json``): nothing is
sent to a device, no adb server is contacted and no file is written. The
result is `RunTree`, plain data; the command line turns it into text or into
``uiprobe.crawl_tree/1``.

Screens are the nodes. The edges are the taps that were sent: a tap's start
line names the screen and the element, its end line says where it led. BACK
and relaunch are not edges. Every element of a screen is listed under that
screen, pressed or not, so the tree shows what the crawl collected and not
only what it dived into.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from uiprobe.crawl import _Prints
from uiprobe.model import Bounds, Element, Screen
from uiprobe.settings import SettingsError
from uiprobe.settings import _read_bytes as _read_capped

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "TREE_SCHEMA",
    "Row",
    "RunTree",
    "ScreenNode",
    "TreeError",
    "read_tree",
    "tree_to_dict",
]

TREE_SCHEMA: Final = "uiprobe.crawl_tree/1"

_SCREEN_ID: Final = re.compile(r"s-[0-9a-f]{12}")
# The most one file is read: the line files (transitions.jsonl, skipped.jsonl)
# up to 64 MiB; a screen file, run-start.json and run.json up to 8 MiB.
MAX_LOG_BYTES: Final = 64 * 1024 * 1024
MAX_FILE_BYTES: Final = 8 * 1024 * 1024
MAX_WARNINGS: Final = 20
# At most this many texts are kept for a row, and this many elements are looked
# at below it, so nested clickables cannot multiply the work.
MAX_ROW_TEXTS: Final = 50
_MAX_MEMBERS: Final = 500
_MAX_FINGERPRINTED: Final = 2000
_READ_CHUNK: Final = 65536
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_PASSWORD_NAME: Final = "[password field]"  # noqa: S105 (a marker, not a secret)

Ending = dict[str, object]


class TreeError(Exception):
    """The folder is not a run folder this command can read."""


@dataclass(slots=True, kw_only=True)
class Row:
    """One line of a screen: a clickable row, or a text outside any row.

    ``kind`` is ``row`` or ``text``. ``texts`` are all the texts of the row
    in document order, ``name`` first (the name itself is not always a text:
    it can be a content description or a marker such as ``[icon: ImageButton]``).
    ``name_from`` says where the name came from: ``text``, ``content_desc``,
    ``password`` (the marker of a password field) or ``none`` (the name is a
    placeholder such as ``[icon: ImageButton]``, there is no real name).
    ``to`` is the ending of a tap that was attempted: ``{"screen_id": ...}``,
    ``{"same_screen": True}``, ``{"other_app": package or None}``,
    ``{"not_read": True}`` or ``{"not_sent": reason}``; None when no tap on
    this row is recorded; ``unknown`` is a tap with no usable end line. A row
    whose tap was not sent, or is unknown, is not ``pressed``. ``also_to`` holds
    the other endings of further sent taps on the same element.
    ``folded_into`` is the id of the outermost clickable ancestor, for a row
    that has one (the text tree shows it on that ancestor's line).
    """

    element_id: str
    kind: str
    class_name: str
    name: str
    texts: tuple[str, ...]
    hidden: bool
    name_from: str = "none"
    folded_into: str | None = None
    pressed: bool = False
    action_id: int | None = None
    to: Ending | None = None
    also_to: list[Ending] = field(default_factory=list)
    reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class ScreenNode:
    """A screen that has a file in ``screens/``, with its rows."""

    screen_id: str
    package: str
    activity: str | None
    rows: list[Row]
    unmatched: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    screen_reasons: tuple[str, ...] = ()
    unmatched_why: str | None = None  # why they were not matched, when not tried


@dataclass(slots=True)
class RunTree:
    """What a run folder says, as a graph of screens."""

    package: str
    label: str | None
    started_at: str | None
    ended_at: str | None
    stop_reason: str | None
    actions: int
    finished: bool
    record_text: bool
    root: str | None
    screens: dict[str, ScreenNode]
    unreached: list[str]
    warnings: list[str]


@dataclass(slots=True)
class _Tap:
    action_id: int
    from_screen_id: str
    element_id: str
    end: dict[str, object] | None
    element: dict[str, object]


class _Warnings:
    """What could not be read: counted as it happens, only the first
    `MAX_WARNINGS` texts are kept."""

    def __init__(self) -> None:
        self.kept: list[str] = []
        self.count = 0

    def append(self, text: str) -> None:
        self.count += 1
        if len(self.kept) < MAX_WARNINGS:
            self.kept.append(text)

    def result(self) -> list[str]:
        if self.count <= MAX_WARNINGS:
            return list(self.kept)
        return [*self.kept, f"and {self.count - MAX_WARNINGS} more warnings"]


class _UnreadableError(Exception):
    """A file that is refused or cannot be read; the text says why."""


def _read_bytes(path: Path, limit: int) -> bytes:
    """Read a regular file inside the run folder, never following a link.

    The file is checked by `lstat` (not a link, a regular file, one name only,
    not over `limit`), then read through the settings reader, which opens it
    once with read-only flags (a FIFO does not block), checks the open
    descriptor and reads at most `limit` + 1 bytes; then `lstat` must show the
    same file again, so a link put in between is refused.

    Raises:
        FileNotFoundError: Nothing is there.
        _UnreadableError: A link, a file that is not regular (a FIFO, a
            folder), a hard link, a file over `limit` bytes, or a read error.
    """
    before = _checked(path, limit)
    try:
        data = _read_capped(path, path.name, limit)
    except SettingsError as exc:
        text = str(exc).removeprefix(f"{path.name}: ")
        if text.startswith("larger than"):
            text = _over(limit)
        raise _UnreadableError(text) from None
    after = _checked(path, limit)
    if (after.st_ino, after.st_dev) != (before.st_ino, before.st_dev):
        raise _UnreadableError("changed while it was read")
    return data


def _over(limit: int) -> str:
    return f"is over {limit // (1024 * 1024)} MiB, so it is skipped"


def _checked(path: Path, limit: int) -> os.stat_result:
    """`lstat` of `path`, refused when it is a link, not a regular file, a hard
    link or over `limit` bytes."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _UnreadableError(exc.strerror or "cannot be read") from None
    if stat.S_ISLNK(before.st_mode):
        raise _UnreadableError("is a link, which is not followed")
    if not stat.S_ISREG(before.st_mode):
        raise _UnreadableError("is not a regular file")
    if before.st_nlink > 1:
        raise _UnreadableError("has more than one name (a hard link), so it is skipped")
    if before.st_size > limit:
        raise _UnreadableError(_over(limit))
    return before


def _loads(data: bytes) -> object:
    """Parse one JSON text; raises ValueError for anything that is not JSON
    (also bytes that are not UTF-8, and nesting too deep to parse)."""
    try:
        return json.loads(data.decode("utf-8"))
    except RecursionError:
        msg = "nested too deeply"
        raise ValueError(msg) from None


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found: dict[str, object] = {}
    for key, value in pairs:
        if key in found:
            msg = "duplicate key"
            raise ValueError(msg)
        found[key] = value
    return found


def _loads_strict(data: bytes) -> object:
    """Like `_loads`, but a key that appears twice in an object is an error."""
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except RecursionError:
        msg = "nested too deeply"
        raise ValueError(msg) from None


def _json_valid(data: bytes) -> bool:
    """Whether `data` is JSON at all (any shape; a repeated key is allowed)."""
    try:
        json.loads(data.decode("utf-8"))
    except RecursionError:
        return True  # valid, only too deep to read
    except ValueError:
        return False
    return True


def _read_json(path: Path) -> object:
    """Parse a JSON file; raises FileNotFoundError, _UnreadableError or ValueError."""
    return _loads(_read_bytes(path, MAX_FILE_BYTES))


def _is_folder(path: Path) -> bool:
    """Whether `path` is a real folder, not a link or a Windows junction."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode) and not os.path.isjunction(path)
    except OSError:
        return False


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _lines(path: Path, warnings: _Warnings) -> Iterator[dict[str, object] | None]:
    """The non-blank lines of a ``.jsonl`` file, one at a time: the JSON object,
    or None for a line that is not one (that is a warning, and still a line:
    the callers use the physical order)."""
    try:
        data = _read_bytes(path, MAX_LOG_BYTES)
    except FileNotFoundError:
        return  # written at the first line, so absent when there was none
    except _UnreadableError as exc:
        warnings.append(f"{path.name} {exc}")
        return
    start = 0
    number = 0
    while start <= len(data):
        end = data.find(b"\n", start)
        if end < 0:
            end = len(data)
        raw = data[start:end]
        start = end + 1
        number += 1
        if not raw.strip():
            continue
        try:
            value = _loads(raw)
        except ValueError:
            warnings.append(f"{path.name} line {number} is not valid UTF-8 JSON")
            yield None
            continue
        if isinstance(value, dict):
            yield value
        else:
            warnings.append(f"{path.name} line {number} is not a JSON object")
            yield None


def _screen_id(value: object) -> str | None:
    if isinstance(value, str) and _SCREEN_ID.fullmatch(value):
        return value
    return None


def _action_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _Reader:
    """Loads screen files on demand, once each, by the names the folder lists."""

    def __init__(self, run_dir: Path, warnings: _Warnings) -> None:
        self._dir = run_dir / "screens"
        self._warnings = warnings
        self._cache: dict[str, tuple[Screen, str, str | None] | None] = {}
        self.names: dict[str, str] = {}
        if os.path.lexists(self._dir) and not _is_folder(self._dir):
            warnings.append("screens is a link or not a folder, so it is not read")
        elif _is_folder(self._dir):
            try:
                listed = [f.name for f in self._dir.iterdir()]
            except OSError:
                listed = []
                warnings.append("screens cannot be listed")
            # screen id -> the exact file name found in the listing
            self.names = {
                name[:-5]: name
                for name in listed
                if name.endswith(".json") and _SCREEN_ID.fullmatch(name[:-5])
            }

    def load(self, screen_id: str) -> tuple[Screen, str, str | None] | None:
        """The screen, its package and its activity, or None when no usable file."""
        if screen_id in self._cache:
            return self._cache[screen_id]
        found: tuple[Screen, str, str | None] | None = None
        name = self.names.get(screen_id)
        if name is not None:
            try:
                found = _parse_screen(_read_json(self._dir / name))
            except FileNotFoundError:
                pass
            except _UnreadableError as exc:
                self._warnings.append(f"screens/{name} {exc}")
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                self._warnings.append(f"screens/{name} cannot be read")
        self._cache[screen_id] = found
        return found


def _parse_screen(data: object) -> tuple[Screen, str, str | None]:
    if not isinstance(data, dict):
        raise TypeError
    app = data["app"]
    package = app["package"]
    activity = app.get("activity")
    if not isinstance(package, str) or not (
        activity is None or isinstance(activity, str)
    ):
        raise TypeError
    display = data.get("display") or {}
    elements: list[Element] = []
    for raw in data["elements"]:
        left, top, right, bottom = (int(n) for n in raw["bounds"])
        parent = raw.get("parent")
        flags = raw.get("flags") or []
        values = {
            key: raw.get(key, "")
            for key in ("text", "resource_id", "content_desc", "hint")
        }
        hidden = raw.get("hidden", [])
        if not isinstance(hidden, list) or not all(isinstance(h, str) for h in hidden):
            raise TypeError
        if "text" in hidden and "password" in flags:
            # The writer blanked a typed text; the marker stands for it, so
            # the field still shows as a password row.
            values["text"] = _PASSWORD_NAME
        if not (
            isinstance(raw["id"], str)
            and isinstance(raw["class"], str)
            and (parent is None or isinstance(parent, str))
            and all(isinstance(v, str) for v in values.values())
            and all(isinstance(f, str) for f in flags)
        ):
            raise TypeError
        elements.append(
            Element(
                id=raw["id"],
                parent=parent,
                class_name=raw["class"],
                bounds=Bounds(left, top, right, bottom),
                flags=tuple(flags),
                **values,
            )
        )
    if len({e.id for e in elements}) != len(elements):
        raise ValueError
    screen = Screen(
        serial="",
        captured_at=_EPOCH,
        package=package,
        activity=activity,
        api_level=None,
        display=(int(display.get("width", 0)), int(display.get("height", 0))),
        elements=tuple(elements),
    )
    return screen, package, activity


def _row_of(
    element: Element,
    kind: str,
    name: str,
    texts: tuple[str, ...],
    *,
    hidden: bool,
    name_from: str = "none",
) -> Row:
    return Row(
        element_id=element.id,
        kind=kind,
        class_name=element.class_name,
        name=name,
        texts=texts,
        hidden=hidden,
        name_from=name_from,
    )


def _short_class(class_name: str) -> str:
    return class_name.rsplit(".", 1)[-1] or class_name


def _outermost_clickable(screen: Screen, index: dict[str, int]) -> list[int | None]:
    """For each element, the index of its outermost ancestor that is clickable
    in the screen file, or None. An element whose chain of parents loops back
    on itself, and everything below such a loop, has none."""
    elements = screen.elements
    clickable = ["clickable" in e.flags for e in elements]
    parent = [index.get(p) if (p := e.parent) is not None else None for e in elements]
    result: list[int | None] = [None] * len(elements)
    state = [0] * len(elements)  # 0 new, 1 on the walk, 2 done
    loops = [False] * len(elements)  # on a loop of parents, or below one
    for start in range(len(elements)):
        if state[start]:
            continue
        path: list[int] = []
        node: int | None = start
        while node is not None and state[node] == 0:
            state[node] = 1
            path.append(node)
            node = parent[node]
        looped = node is not None and (state[node] == 1 or loops[node])
        for i in path:
            state[i] = 2
            loops[i] = looped
        if looped:
            continue  # the whole path stays None
        for i in reversed(path):  # the top of the chain first
            up = parent[i]
            if up is not None:
                result[i] = (
                    result[up]
                    if result[up] is not None
                    else (up if clickable[up] else None)
                )
    return result


def _rows_of(
    screen: Screen,
    tapped: set[str],
    *,
    record_text: bool,
) -> list[Row]:
    """The rows and the texts outside rows of a screen, in document order."""
    elements = screen.elements
    index = {e.id: i for i, e in enumerate(elements)}
    children: dict[str, list[int]] = {}
    for i, element in enumerate(elements):
        if element.parent in index:
            children.setdefault(element.parent, []).append(i)  # type: ignore[arg-type]
    is_row = [("clickable" in e.flags) or (e.id in tapped) for e in elements]
    under_row = [False] * len(elements)
    for i, element in enumerate(elements):
        # Document order lists a parent before its children.
        if element.parent in index:
            up = index[element.parent]  # type: ignore[index]
            under_row[i] = is_row[up] or under_row[up]
    own = [e.text.strip() for e in elements]  # each element's text, once
    outer = _outermost_clickable(screen, index)
    rows: list[Row] = []
    for i, element in enumerate(elements):
        if is_row[i]:
            row = _row(screen, i, children, own, record_text=record_text)
            above = outer[i]
            if above is not None:
                row.folded_into = elements[above].id
            rows.append(row)
        elif not under_row[i]:
            text_row = _text_row(element, record_text=record_text)
            if text_row is not None:
                rows.append(text_row)
    return rows


def _subtree(i: int, children: dict[str, list[int]], screen: Screen) -> list[int]:
    """The element and what is below it, in document order, at most
    `_MAX_MEMBERS` of them (a parent cycle cannot loop either)."""
    found = [i]
    seen = {i}
    stack = [i]
    while stack and len(found) < _MAX_MEMBERS:
        for child in children.get(screen.elements[stack.pop()].id, []):
            if child not in seen:
                seen.add(child)
                found.append(child)
                stack.append(child)
    return sorted(found)[:_MAX_MEMBERS]


def _row(
    screen: Screen,
    i: int,
    children: dict[str, list[int]],
    own: list[str],
    *,
    record_text: bool,
) -> Row:
    element = screen.elements[i]
    members = _subtree(i, children, screen)
    short = _short_class(element.class_name)
    if any("password" in screen.elements[j].flags for j in members):
        return _row_of(
            element, "row", _PASSWORD_NAME, (), hidden=True, name_from="password"
        )
    if not record_text:
        marker = "icon" if len(members) == 1 else "row"
        return _row_of(element, "row", f"[{marker}: {short}]", (), hidden=True)
    texts: list[str] = []
    for j in members:
        if own[j]:
            texts.append(own[j])
            if len(texts) >= MAX_ROW_TEXTS:
                break
    if texts:
        return _row_of(
            element, "row", texts[0], tuple(texts), hidden=False, name_from="text"
        )
    described = [d for j in members if (d := screen.elements[j].content_desc.strip())]
    if described:
        return _row_of(
            element, "row", described[0], (), hidden=False, name_from="content_desc"
        )
    return _row_of(element, "row", f"[icon: {short}]", (), hidden=False)


def _text_row(element: Element, *, record_text: bool) -> Row | None:
    if not record_text:
        return None
    if "password" in element.flags:
        if not element.text.strip():
            return None
        return _row_of(
            element, "text", _PASSWORD_NAME, (), hidden=True, name_from="password"
        )
    text = element.text.strip()
    if not text:
        return None
    return _row_of(element, "text", text, (text,), hidden=False, name_from="text")


def _read_start(base: Path) -> dict[str, object]:
    if not base.is_dir():
        msg = "the run folder does not exist or is not a folder"
        raise TreeError(msg)
    try:
        start = _read_json(base / "run-start.json")
    except FileNotFoundError:
        msg = "run-start.json is missing, so this is not a run folder"
        raise TreeError(msg) from None
    except _UnreadableError as exc:
        msg = f"run-start.json {exc}"
        raise TreeError(msg) from None
    except ValueError:
        msg = "run-start.json is not valid UTF-8 JSON"
        raise TreeError(msg) from None
    if (
        not isinstance(start, dict)
        or start.get("schema") != "uiprobe.run_start/1"
        or not isinstance(start.get("package"), str)
    ):
        msg = "run-start.json is not a uiprobe.run_start/1 record with a package"
        raise TreeError(msg)
    return start


def _read_end(base: Path, warnings: _Warnings) -> dict[str, object] | None:
    """The content of run.json, or None when it is missing or not usable."""
    try:
        loaded = _read_json(base / "run.json")
    except FileNotFoundError:
        return None
    except _UnreadableError as exc:
        warnings.append(f"run.json {exc} (the run did not end normally)")
        return None
    except ValueError:
        warnings.append(
            "run.json is not valid UTF-8 JSON (the run did not end normally)"
        )
        return None
    if isinstance(loaded, dict) and isinstance(loaded.get("stop_reason"), str):
        return loaded
    warnings.append("run.json is not a run end record (no stop_reason)")
    return None


def _tap_of(
    action_id: int,
    start_line: dict[str, object],
    action: dict[str, object],
    end: dict[str, object] | None,
) -> _Tap | None:
    """The tap a start line records, or None when it names no screen or element."""
    from_id = _screen_id(start_line.get("from_screen_id"))
    element = action.get("element")
    element_id = element.get("id") if isinstance(element, dict) else None
    if (
        from_id is None
        or not isinstance(element, dict)
        or not isinstance(element_id, str)
    ):
        return None
    return _Tap(action_id, from_id, element_id, end, element)


@dataclass(slots=True)
class _Moves:
    taps: dict[tuple[str, str], list[_Tap]]
    root: str | None
    first_from: str | None
    count: int
    off_after: dict[int, str]


def _rank(tap: _Tap) -> int:
    """Which tap stands for an element: sent and read first, then one whose
    delivery is unknown, then one that was not sent."""
    if _unknown(tap):
        return 1
    return 2 if _not_sent(tap) else 0


_START: Final = "uiprobe.transition_start/1"
_END: Final = "uiprobe.transition_end/1"
_STAMP: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}(?::\d{2})?)?"
)
_KINDS: Final = frozenset({"launch", "tap", "back", "relaunch"})
_ELEMENT_STRINGS: Final = ("text", "resource_id", "content_desc", "hint", "parent")
_HIDDEN_FIELDS: Final = ("text", "content_desc", "hint")
GATE_WARNING: Final = (
    "transitions.jsonl is not in the shape uiprobe writes; no press results are shown"
)
CUT_WARNING: Final = (
    "transitions.jsonl ends with a line that was cut short; the line is ignored"
)


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_text(value: object) -> bool:
    return type(value) is str


def _keys_ok(line: dict[str, object], required: set[str], optional: set[str]) -> bool:
    keys = set(line)
    return required <= keys <= required | optional


def _stamped(line: dict[str, object], schema: str) -> bool:
    stamp = line.get("recorded_at")
    return (
        line.get("schema") == schema
        and _is_text(stamp)
        and _STAMP.fullmatch(str(stamp)) is not None
        and _is_int(line.get("action_id"))
        and int(str(line["action_id"])) >= 1
    )


def _hidden_ok(element: dict[str, object]) -> bool:
    """``hidden``, when present, names fields that were blanked: a non-empty
    list of distinct names from `_HIDDEN_FIELDS`, in that order, each written
    as an empty string."""
    if "hidden" not in element:
        return True
    hidden = element["hidden"]
    return (
        isinstance(hidden, list)
        and bool(hidden)
        and hidden == [k for k in _HIDDEN_FIELDS if k in hidden]
        and all(element.get(k) == "" for k in hidden)
    )


def _element_ok(element: object) -> bool:
    """The form `RunWriter` writes for a tap's element: that of
    `Element.to_dict`, plus ``hidden`` for blanked texts."""
    if not isinstance(element, dict):
        return False
    allowed = {"id", "class", "bounds", "flags", "hidden", *_ELEMENT_STRINGS}
    bounds = element.get("bounds")
    flags = element.get("flags", [])
    return (
        _keys_ok(element, {"id", "class", "bounds"}, allowed)
        and _is_text(element["id"])
        and _is_text(element["class"])
        and all(_is_text(element[k]) for k in _ELEMENT_STRINGS if k in element)
        and _hidden_ok(element)
        and isinstance(bounds, list)
        and len(bounds) == 4
        and all(_is_int(n) for n in bounds)
        and isinstance(flags, list)
        and all(_is_text(f) for f in flags)
    )


def _action_ok(line: dict[str, object]) -> str | None:
    """Why the ``action`` and the keys around it are not as uiprobe writes a
    start line, or None."""
    action = line["action"]
    if not isinstance(action, dict) or not _keys_ok(
        action, {"kind"}, {"point", "element"}
    ):
        return "action"
    kind = action["kind"]
    point = action.get("point")
    has_from = "from_screen_id" in line
    checks = (
        (not _is_text(kind) or kind not in _KINDS, "action.kind"),
        (
            "point" in action
            and not (
                isinstance(point, list)
                and len(point) == 2
                and all(_is_int(n) for n in point)
            ),
            "action.point",
        ),
        ("element" in action and not _element_ok(action["element"]), "action.element"),
        (has_from and _screen_id(line["from_screen_id"]) is None, "from_screen_id"),
        (kind == "tap" and not (has_from and "point" in action), "tap fields"),
        (kind != "tap" and ("point" in action or "element" in action), "tap fields"),
        (kind == "launch" and (has_from or "off_target" in line), "launch fields"),
        (
            "restart" in line and not (line["restart"] is True and kind == "launch"),
            "restart",
        ),
        (
            "replay" in line and not (line["replay"] is True and kind == "tap"),
            "replay",
        ),
    )
    return next((why for bad, why in checks if bad), None)


def _off_target_ok(line: dict[str, object]) -> bool:
    if "off_target" not in line:
        return True
    other = line["off_target"]
    action = line["action"]
    kind = action["kind"] if isinstance(action, dict) else None  # type: ignore[index]
    return (
        kind in ("back", "relaunch")
        and "from_screen_id" in line
        and isinstance(other, dict)
        and _keys_ok(other, {"package"}, {"foreground", "focus"})
        and all(_is_text(v) for v in other.values())
    )


def _start_problem(line: dict[str, object]) -> str | None:
    """Why `line` is not a start line as uiprobe writes it, or None."""
    if not _keys_ok(
        line,
        {"schema", "recorded_at", "action_id", "action"},
        {"from_screen_id", "off_target", "restart", "replay"},
    ):
        return "start line keys"
    if not _stamped(line, _START):
        return "start line schema, time or action_id"
    problem = _action_ok(line)
    if problem is None and not _off_target_ok(line):
        problem = "off_target"
    return problem


def _end_problem(line: dict[str, object]) -> str | None:
    """Why `line` is not an end line as uiprobe writes it, or None."""
    base = {"schema", "recorded_at", "action_id"}
    if not _stamped(line, _END):
        return "end line schema, time or action_id"
    if "failed" in line:
        ok = (
            _keys_ok(line, base | {"failed", "reason", "outcome"}, set())
            and line["failed"] is True
            and _is_text(line["reason"])
            and line["outcome"] == "unknown"
        )
        return None if ok else "failed end line"
    if "not_sent" in line:
        details = line.get("details", {"x": 0})
        ok = (
            _keys_ok(line, base | {"not_sent", "reason"}, {"details"})
            and line["not_sent"] is True
            and _is_text(line["reason"])
            and isinstance(details, dict)
            and bool(details)
            and all(_is_text(k) for k in details)
            and all(
                _is_text(v) or _is_int(v) or (k == "hidden" and v == ["window"])
                for k, v in details.items()
            )
            and ("hidden" not in details or details.get("window") == "")
        )
        return None if ok else "not_sent end line"
    if line.get("dump_failed") is True:
        ok = _keys_ok(line, base | {"dump_failed"}, set())
        return None if ok else "dump_failed end line"
    if line.get("dump_failed") is False:
        ok = _keys_ok(line, base | {"dump_failed", "to_screen_id"}, set())
        ok = ok and _screen_id(line.get("to_screen_id")) is not None
        return None if ok else "end line with a screen"
    return "end line without a result"


def _line_problem(line: dict[str, object]) -> str | None:
    schema = line.get("schema")
    if schema == _START:
        return _start_problem(line)
    if schema == _END:
        return _end_problem(line)
    return "schema"


def _sequence_problem(entries: list[dict[str, object]]) -> tuple[int, str] | None:
    """The first line (counted from 1) that breaks the strict alternation of
    start and end lines, ids rising: ``S1 E1 S2 E2 ...`` with a last start line
    that may have no end line."""
    open_id: object = None
    last_id = 0
    for number, line in enumerate(entries, 1):
        action_id = int(str(line["action_id"]))
        if line["schema"] == _START:
            if open_id is not None:
                return number, "a start line while an action is open"
            if action_id <= last_id:
                return number, "an action id that does not rise"
            open_id = last_id = action_id
        elif open_id != action_id:
            return number, "an end line that does not close the open action"
        else:
            open_id = None
    return None


@dataclass(slots=True)
class _Lines:
    """What transitions.jsonl says. `trusted` is False when any line is not in
    the shape uiprobe writes (see `_check_transitions`); then `starts` holds the
    start lines that are valid by themselves, with no end lines, so every tap
    is unknown, and `root_hint` the screen of the first valid launch end."""

    starts: dict[int, dict[str, object]] = field(default_factory=dict)
    ends: dict[int, dict[str, object]] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    # per action: the package of an off_target start line that comes right
    # after its end line, decided on the screen that end line names
    off_after: dict[int, str] = field(default_factory=dict)
    trusted: bool = True
    root_hint: str | None = None
    problem: tuple[int, str] | None = None  # the first line that failed, if any
    cut: bool = False  # the last line was cut short and is ignored


def _split_transitions(data: bytes) -> tuple[list[bytes], bytes | None]:
    """The complete lines of the file, and a last line with no newline (which
    may have been cut short by a crash)."""
    parts = data.split(b"\n")
    return parts[:-1], (parts[-1] or None)


def _parse_line(raw: bytes) -> dict[str, object] | None:
    if raw != raw.strip():
        return None
    try:
        value = _loads_strict(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _check_transitions(data: bytes, *, distrust: bool = False) -> _Lines:
    """Trust the file only if every line is exactly as uiprobe writes it.

    The shapes come from `RunWriter.write_transition_start` and
    `write_transition_end`, and the order from the crawl driver: one start line
    then one end line per action, ids rising, no blank or extra lines. uiprobe
    can end the file in these ways, all accepted: after an end line (a normal,
    dump-failed, not-sent or failed end); after a start line with no end (Ctrl+C,
    the time limit, an error, a power loss after the start line was written;
    only that last tap is unknown); in the middle of a line (the last line has no
    newline and is not complete: it is ignored, and an end line cut short leaves
    its start line without an end line).
    """
    complete, last = _split_transitions(data)
    raws = list(complete)
    cut = False
    if last is not None:
        if _json_valid(last):
            raws.append(last)  # complete JSON, only its newline is missing
        else:
            cut = True  # not JSON: a write that was cut short
    entries: list[dict[str, object]] = []
    parsed: list[dict[str, object]] = []
    problem: tuple[int, str] | None = None
    for number, raw in enumerate(raws, 1):
        line = _parse_line(raw)
        why = "not a JSON object, or blank" if line is None else _line_problem(line)
        if line is not None:
            parsed.append(line)
        if line is None or why is not None:
            problem = problem or (number, why or "line")
            continue
        entries.append(line)
    found = _Lines()
    if problem is None:
        problem = _sequence_problem(entries)
    found.problem = problem
    found.trusted = problem is None and not distrust
    if found.trusted:
        _collect(found, entries)
    else:
        _collect_rough(found, parsed)
    found.cut = cut
    return found


def _collect(found: _Lines, entries: list[dict[str, object]]) -> None:
    """Fill `found` from a file that passed: the start and end lines, and the
    off_target evidence right after an end line."""
    previous: dict[str, object] | None = None
    for line in entries:
        action_id = int(str(line["action_id"]))
        if line["schema"] == _START:
            found.order.append(action_id)
            found.starts[action_id] = line
            _note_off(found, line, previous)
        else:
            found.ends[action_id] = line
        previous = line


def _collect_rough(found: _Lines, parsed: list[dict[str, object]]) -> None:
    """A file that failed: no end line is used, so no tap has a result. Only the
    taps are taken, from any line that names a screen and an element, so that
    their rows show as unknown; the first launch's end line gives the root."""
    launches: set[int] = set()
    for line in parsed:
        action = line.get("action")
        action_id = line.get("action_id")
        if not _is_int(action_id):
            continue
        if line.get("schema") == _START and isinstance(action, dict):
            element = action.get("element")
            if action.get("kind") == "launch":
                launches.add(int(str(action_id)))
            elif (
                action.get("kind") == "tap"
                and _screen_id(line.get("from_screen_id")) is not None
                and isinstance(element, dict)
                and _is_text(element.get("id"))
                and action_id not in found.starts
            ):
                found.order.append(int(str(action_id)))
                found.starts[int(str(action_id))] = line
        elif (
            line.get("schema") == _END
            and found.root_hint is None
            and action_id in launches
        ):
            found.root_hint = _screen_id(line.get("to_screen_id"))


def _note_off(
    found: _Lines, line: dict[str, object], previous: dict[str, object] | None
) -> None:
    """An off_target start line right after an end line, decided on the screen
    that end line names, is the evidence that the screen belongs to another app."""
    other = line.get("off_target")
    if previous is None or previous["schema"] != _END or not isinstance(other, dict):
        return
    to = _screen_id(previous.get("to_screen_id"))
    named = other["package"]
    if to is not None and line.get("from_screen_id") == to and named:
        found.off_after[int(str(previous["action_id"]))] = str(named)


def _read_transitions(
    base: Path, warnings: _Warnings, *, distrust: bool = False
) -> _Lines:
    """Read transitions.jsonl; see `_check_transitions` for what is trusted.
    `distrust` fails the gate whatever the file says (a screen file of another
    package, which uiprobe never writes, is in the folder)."""
    path = base / "transitions.jsonl"
    try:
        data = _read_bytes(path, MAX_LOG_BYTES)
    except FileNotFoundError:
        data = b""  # written at the first line, so absent when there was none
    except _UnreadableError as exc:
        warnings.append(f"{path.name} {exc}")
        data = b""
    found = _check_transitions(data, distrust=distrust)
    if not found.trusted:
        warnings.append(GATE_WARNING)
    elif found.cut:
        warnings.append(CUT_WARNING)
    return found


def _read_moves(base: Path, warnings: _Warnings, *, distrust: bool) -> _Moves:
    """The taps of transitions.jsonl, and the screen the launch led to."""
    lines = _read_transitions(base, warnings, distrust=distrust)
    taps: dict[tuple[str, str], list[_Tap]] = {}
    root: str | None = lines.root_hint
    first_from: str | None = None
    for action_id in lines.order:
        start_line = lines.starts[action_id]
        action = start_line["action"]
        assert isinstance(action, dict)  # noqa: S101 (checked by the gate)
        end = lines.ends.get(action_id)
        kind = action["kind"]
        if kind == "launch" and root is None and end is not None:
            root = _screen_id(end.get("to_screen_id"))
        if kind != "tap":
            continue
        tap = _tap_of(action_id, start_line, action, end)
        if tap is None:
            continue
        first_from = first_from or tap.from_screen_id
        taps.setdefault((tap.from_screen_id, tap.element_id), []).append(tap)
    return _Moves(taps, root, first_from, len(lines.order), lines.off_after)


def _sent_target(tap: _Tap) -> str | None:
    """The screen a sent tap led to, if its end line names one."""
    if _unknown(tap) or tap.end is None or tap.end.get("dump_failed") is True:
        return None
    return _screen_id(tap.end.get("to_screen_id"))


@dataclass(slots=True)
class _Skips:
    by_screen: dict[str, dict[str, list[str]]]
    whole_screen: dict[str, list[str]]
    # screen id -> element id -> fingerprint, from the ``element`` of a line.
    # Used where texts are blanked in the records, so the fingerprint cannot
    # be computed again from the screen file.
    by_element: dict[str, dict[str, str]] = field(default_factory=dict)


def _read_skips(base: Path, warnings: _Warnings) -> _Skips:
    skips = _Skips({}, {})
    for line in _lines(base / "skipped.jsonl", warnings):
        if line is None:
            continue
        sid = _screen_id(line.get("screen_id"))
        fingerprint = line.get("fingerprint")
        reasons = line.get("reasons")
        if sid is None or not isinstance(fingerprint, str):
            warnings.append("skipped.jsonl has a line with no screen id or fingerprint")
            continue
        if not isinstance(reasons, list) or not all(
            isinstance(r, str) for r in reasons
        ):
            warnings.append(
                "skipped.jsonl has a line whose reasons are not a list of strings"
            )
            continue
        listed = list(reasons)
        if fingerprint == sid:
            bucket = skips.whole_screen.setdefault(sid, [])
        else:
            bucket = skips.by_screen.setdefault(sid, {}).setdefault(fingerprint, [])
            element = line.get("element")
            ident = element.get("id") if isinstance(element, dict) else None
            if isinstance(ident, str):
                skips.by_element.setdefault(sid, {}).setdefault(ident, fingerprint)
        bucket.extend(r for r in listed if r not in bucket)
    return skips


def _screen_ids(reader: _Reader, moves: _Moves) -> list[str]:
    """The ids of the screens to read: the ones the taps name, then the rest."""
    wanted: list[str] = []
    named = [moves.root, moves.first_from, *(key[0] for key in moves.taps)]
    named.extend(_sent_target(tap) for taps in moves.taps.values() for tap in taps)
    for sid in named:
        if sid is not None and sid not in wanted:
            wanted.append(sid)
    wanted.extend(sid for sid in sorted(reader.names) if sid not in wanted)
    return wanted


def _node(
    sid: str,
    loaded: tuple[Screen, str, str | None],
    moves: _Moves,
    skips: _Skips,
    reader: _Reader,
    package: str,
    *,
    record_text: bool,
) -> ScreenNode:
    screen, screen_package, activity = loaded
    tapped = {key[1] for key in moves.taps if key[0] == sid}
    rows = _rows_of(screen, tapped, record_text=record_text)
    by_fp = skips.by_screen.get(sid, {})
    unmatched: list[tuple[str, tuple[str, ...]]] = []
    unchecked = False
    if by_fp and len(screen.elements) > _MAX_FINGERPRINTED:
        unchecked = True
        unmatched = [(fp, tuple(r)) for fp, r in by_fp.items()]
    elif by_fp:
        prints = _Prints(screen)  # one index of the screen, built once
        by_id = {e.id: e for e in screen.elements}
        seen: set[str] = set()
        for row in rows:
            if row.kind != "row":
                continue
            element = by_id[row.element_id]
            fp = prints.of(element)
            if fp not in by_fp and (not record_text or "password" in element.flags):
                # The texts were blanked when the line was written, so the
                # fingerprint cannot match; the element id of the line does.
                fp = skips.by_element.get(sid, {}).get(row.element_id, fp)
            if fp in by_fp:
                row.reasons = tuple(by_fp[fp])
                seen.add(fp)
        unmatched = [(fp, tuple(r)) for fp, r in by_fp.items() if fp not in seen]
    for row in rows:
        if (sid, row.element_id) in moves.taps:
            _attach(row, moves.taps[sid, row.element_id], sid, package, reader, moves)
    return ScreenNode(
        sid,
        screen_package,
        activity,
        rows,
        unmatched,
        tuple(skips.whole_screen.get(sid, ())),
        "too_many_elements" if unchecked else None,
    )


def read_tree(run_dir: str | Path) -> RunTree:
    """Read the run folder `run_dir` and return its tree.

    Raises:
        TreeError: `run_dir` is not a folder, or its ``run-start.json`` is
            missing or not a JSON object with a ``package``. Anything else
            that cannot be read becomes a warning, and the rest is built.
    """
    base = Path(run_dir)
    warnings = _Warnings()
    start = _read_start(base)
    package = str(start["package"])
    record_text = start.get("record_text") is not False
    label = _str_or_none(start.get("app_label"))
    ended = _read_end(base, warnings)
    skips = _read_skips(base, warnings)
    reader = _Reader(base, warnings)
    # uiprobe writes only screens of the target app: another package's file
    # means the folder is not as uiprobe wrote it.
    foreign = any(
        found is not None and found[1] != package
        for found in (reader.load(sid) for sid in sorted(reader.names))
    )
    moves = _read_moves(base, warnings, distrust=foreign)
    screens: dict[str, ScreenNode] = {}
    for sid in _screen_ids(reader, moves):
        loaded = reader.load(sid)
        if loaded is not None:
            screens[sid] = _node(
                sid, loaded, moves, skips, reader, package, record_text=record_text
            )
    root = moves.root if moves.root in screens else moves.first_from
    if root not in screens:
        root = None
    for sid, element_id in moves.taps:
        node = screens.get(sid)
        if node is None:
            warnings.append(f"a tap names the screen {sid}, which has no readable file")
        elif all(r.element_id != element_id for r in node.rows):
            warnings.append(
                f"a tap names {element_id}, which is not in the file of {sid}"
            )
    reached = _reachable(root, screens)
    led_to = {
        target
        for taps in moves.taps.values()
        for tap in taps
        if (target := _sent_target(tap)) is not None
    }
    summary = ended or {}
    actions = summary.get("actions")
    return RunTree(
        package=package,
        label=label,
        started_at=_str_or_none(start.get("started_at")),
        ended_at=_str_or_none(summary.get("ended_at")),
        stop_reason=_str_or_none(summary.get("stop_reason")),
        actions=actions if isinstance(actions, int) else moves.count,
        finished=ended is not None,
        record_text=record_text,
        root=root,
        screens=screens,
        unreached=[
            sid
            for sid, node in screens.items()
            if sid not in reached
            and node.package == package
            and sid != root
            and sid not in led_to
        ],
        warnings=warnings.result(),
    )


def _attach(
    row: Row,
    taps: list[_Tap],
    sid: str,
    package: str,
    reader: _Reader,
    moves: _Moves,
) -> None:
    """Put the taps on an element on its row: the best one stands for the row,
    and each further sent tap that led elsewhere is an `also_to`."""
    best = min(taps, key=_rank)  # the first of the best rank
    row.action_id = best.action_id
    row.to = _ending(best, sid, package, reader, moves)
    row.pressed = "not_sent" not in row.to and "unknown" not in row.to
    if row.pressed:
        _rename(row, best.element)
    shown = [row.to]
    for tap in taps:
        if tap is best or _rank(tap) != 0:
            continue
        ending = _ending(tap, sid, package, reader, moves)
        if ending not in shown:
            shown.append(ending)
            row.also_to.append(ending)


def _rename(row: Row, element: dict[str, object]) -> None:
    """Name a pressed row from the element its own tap recorded, when that
    element has a text or a description (the screen file of the id can hold
    older texts). A password row keeps its marker."""
    if row.hidden:
        return
    text = element.get("text")
    desc = element.get("content_desc")
    if isinstance(text, str) and text.strip():
        name = text.strip()
        row.texts = (name, *(t for t in row.texts if t != name))
        row.name = name
        row.name_from = "text"
    elif isinstance(desc, str) and desc.strip() and row.name_from != "text":
        # A visible text anywhere in the row wins over a description.
        row.name = desc.strip()
        row.name_from = "content_desc"


def _unknown(tap: _Tap) -> bool:
    """A tap whose start line has no end line, or whose sending failed: it may
    or may not have reached the device."""
    return tap.end is None or bool(tap.end.get("failed"))


def _not_sent(tap: _Tap) -> bool:
    return tap.end is not None and bool(tap.end.get("not_sent"))


def _ending(
    tap: _Tap, from_id: str, package: str, reader: _Reader, moves: _Moves
) -> Ending:
    end = tap.end
    if _unknown(tap):
        return {"unknown": True}
    assert end is not None  # noqa: S101 (`_unknown` is False)
    if end.get("not_sent"):
        reason = end.get("reason")
        return {"not_sent": reason if isinstance(reason, str) else ""}
    target = _screen_id(end.get("to_screen_id"))
    if end.get("dump_failed") or target is None:
        return {"not_read": True}
    if target == from_id:
        return {"same_screen": True}
    return _led_to(target, package, reader, moves, tap.action_id)


def _led_to(
    target: str, package: str, reader: _Reader, moves: _Moves, action_id: int
) -> Ending:
    if reader.load(target) is not None:
        return {"screen_id": target}  # a file of the run's package (see the gate)
    # No file: another app's screen (uiprobe writes only the app's own screens),
    # but only when the start line right after this action's end line was
    # decided on this very screen and names a non-empty package that is not the
    # run's own.
    named = moves.off_after.get(action_id)
    if named is None:
        return {"not_read": True}
    return {"unknown": True} if named == package else {"other_app": named}


def _reachable(root: str | None, screens: dict[str, ScreenNode]) -> set[str]:
    reached: set[str] = set()
    stack = [root] if root in screens else []
    while stack:
        sid = stack.pop()
        if sid is None or sid in reached:
            continue
        reached.add(sid)
        for row in screens[sid].rows:
            for ending in ([row.to] if row.to else []) + row.also_to:
                target = ending.get("screen_id")
                if isinstance(target, str) and target in screens:
                    stack.append(target)
    return reached


def _element_dict(row: Row) -> dict[str, object]:
    data: dict[str, object] = {
        "element_id": row.element_id,
        "kind": row.kind,
        "class": row.class_name,
        "name": f'"{row.name}"' if row.name_from == "content_desc" else row.name,
        "name_from": row.name_from,
        "texts": list(row.texts),
    }
    if row.hidden:
        data["hidden"] = True
    if row.folded_into is not None:
        data["folded_into"] = row.folded_into
    if row.kind == "row":
        data["pressed"] = row.pressed
        data["action_id"] = row.action_id
        if row.to is not None:
            data["to"] = dict(row.to)
        if row.also_to:
            data["also_to"] = [dict(e) for e in row.also_to]
        data["reasons"] = list(row.reasons)
    return data


def tree_to_dict(tree: RunTree) -> dict[str, object]:
    """The ``uiprobe.crawl_tree/1`` object for `tree`."""
    run: dict[str, object] = {
        "package": tree.package,
        "label": tree.label,
        "started_at": tree.started_at,
        "ended_at": tree.ended_at,
        "stop_reason": tree.stop_reason,
        "actions": tree.actions,
        "finished": tree.finished,
        "record_text": tree.record_text,
    }
    screens: dict[str, object] = {}
    for sid, node in tree.screens.items():
        entry: dict[str, object] = {
            "package": node.package,
            "activity": node.activity,
            "elements": [_element_dict(row) for row in node.rows],
            "unmatched": [
                {"fingerprint": fp, "reasons": list(reasons)}
                | ({"why": node.unmatched_why} if node.unmatched_why else {})
                for fp, reasons in node.unmatched
            ],
        }
        if node.screen_reasons:
            entry["screen_reasons"] = list(node.screen_reasons)
        screens[sid] = entry
    return {
        "schema": TREE_SCHEMA,
        "run": run,
        "root": tree.root,
        "screens": screens,
        "unreached": list(tree.unreached),
        "warnings": list(tree.warnings),
    }
