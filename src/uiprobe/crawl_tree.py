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

A scroll is no edge and no row. The screen read after a sent scroll
(``screens/scroll-<action id>.json``) adds its rows under the screen the
scroll was sent on, after the rows already listed, in the order of the
actions; each such row has ``after_scroll`` (the action id). What happens to a
row that the earlier reads of the screen already listed is the one switch
`SCROLLED_REPEATS`.
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

from uiprobe.also_target import AlsoTargetError, check_also_target
from uiprobe.crawl import _Prints
from uiprobe.guards import MAX_SCROLL_AMOUNT, PERMISSION_PACKAGES
from uiprobe.guards.tree import _is_dialog, _Tree
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

# What a row read after a scroll does when the screen's earlier reads listed it
# already (the same fingerprint): ``"once"`` shows it once, where it was first
# listed; ``"as_seen"`` lists it again, every time it was seen. The owner has
# not decided; this is the only place that says. A run that did not record
# texts has no way to tell rows apart and always lists them as seen.
SCROLLED_REPEATS: Final = "once"

_SCREEN_ID: Final = re.compile(r"s-[0-9a-f]{12}")
_SCROLL_FILE: Final = re.compile(r"scroll-([1-9][0-9]{0,9})\.json")
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
    # In memory only (not in the JSON): some attempt on the element was not sent,
    # was not finished, or did not end on the same screen. `also_to` leaves
    # such attempts out when a sent one exists.
    other_attempt: bool = False
    # The action id of the scroll whose result screen this row was read from;
    # None for a row of the screen's own file.
    after_scroll: int | None = None
    # In memory only: the read the row came from (a scroll id, None for the
    # screen's own file, a negative number for the file of another id of the
    # same screen).
    view_key: int | None = None
    # The group of the screen's list the row lies in (see `_assign_groups`),
    # or None: the row is outside the list, or the screen has no separator.
    group: int | None = None


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
    popup: bool = False  # the crawl's own dialog rule holds for the screen
    # The text of the separator that starts each group, by group number (group
    # 0 has none): empty when the screen has no separator in its list.
    groups: list[str | None] = field(default_factory=list)


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
    # The packages besides `package` whose screens the run counted as the
    # app's own (``also_target`` of run-start.json); empty for most runs.
    also_target: tuple[str, ...] = ()


@dataclass(slots=True)
class _Tap:
    action_id: int
    from_screen_id: str
    element_id: str
    end: dict[str, object] | None
    element: dict[str, object]
    # The scroll whose result screen the tap was decided on, when the start
    # line says (``after_scroll``).
    after_scroll: int | None = None


@dataclass(slots=True)
class _Scroll:
    action_id: int
    from_screen_id: str
    end: dict[str, object] | None


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
        self._scroll_cache: dict[int, tuple[Screen, str, str | None] | None] = {}
        self.names: dict[str, str] = {}
        # scroll action id -> the exact file name found in the listing
        self.scroll_names: dict[int, str] = {}
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
            self.scroll_names = {
                int(found[1]): name
                for name in listed
                if (found := _SCROLL_FILE.fullmatch(name)) is not None
            }

    def load(self, screen_id: str) -> tuple[Screen, str, str | None] | None:
        """The screen, its package and its activity, or None when no usable file."""
        if screen_id in self._cache:
            return self._cache[screen_id]
        found = self._read(self.names.get(screen_id))
        self._cache[screen_id] = found
        return found

    def load_scroll(self, action_id: int) -> tuple[Screen, str, str | None] | None:
        """The screen read after the scroll with this action id (its
        ``screens/scroll-<id>.json``), or None when there is no usable file."""
        if action_id in self._scroll_cache:
            return self._scroll_cache[action_id]
        found = self._read(self.scroll_names.get(action_id))
        self._scroll_cache[action_id] = found
        return found

    def _read(self, name: str | None) -> tuple[Screen, str, str | None] | None:
        if name is None:
            return None
        try:
            return _parse_screen(_read_json(self._dir / name))
        except FileNotFoundError:
            pass
        except _UnreadableError as exc:
            self._warnings.append(f"screens/{name} {exc}")
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            self._warnings.append(f"screens/{name} cannot be read")
        return None


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


def _read_start(base: Path) -> tuple[dict[str, object], bool]:
    if not base.is_dir():
        msg = "the run folder does not exist or is not a folder"
        raise TreeError(msg)
    try:
        data = _read_bytes(base / "run-start.json", MAX_FILE_BYTES)
        start = _loads(data)
        try:
            _loads_strict(data)
            repeated = False
        except ValueError:
            repeated = True  # a key twice in an object: not as uiprobe writes it
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
    return start, repeated


def _also_target(start: dict[str, object], package: str) -> tuple[str, ...] | None:
    """The ``also_target`` of run-start.json, checked with the rules of the
    settings (`check_also_target`: package format and length, distinct, not
    `package`, not the system UI, not a fixed home app), and not empty (the
    writer leaves an empty list out). A missing key is the empty tuple;
    anything else that is not such a list is None."""
    if "also_target" not in start:
        return ()
    listed = start["also_target"]
    if not isinstance(listed, list) or not listed:
        return None
    try:
        return check_also_target(package, listed)
    except AlsoTargetError:
        return None


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
    after = start_line.get("after_scroll")
    return _Tap(
        action_id,
        from_id,
        element_id,
        end,
        element,
        after if isinstance(after, int) else None,
    )


@dataclass(slots=True)
class _Moves:
    taps: dict[tuple[str, str], list[_Tap]]
    root: str | None
    first_from: str | None
    count: int
    off_after: dict[int, str]
    scrolls: list[_Scroll] = field(default_factory=list)
    # A screen id that a scroll led to from another id -> the first id of its
    # screen (one list whose id changes as it scrolls is one screen).
    family: dict[str, str] = field(default_factory=dict)


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
_SCROLL_EFFECTS: Final = ("changed", "navigation")
# The largest amount of a wheel turn: the one constant the writer
# (`RunWriter.write_transition_start`) checks against too.
_MAX_AMOUNT: Final = MAX_SCROLL_AMOUNT
_KINDS: Final = frozenset({"launch", "tap", "scroll", "back", "relaunch"})
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
        action, {"kind"}, {"point", "element", "amount"}
    ):
        return "action"
    kind = action["kind"]
    point = action.get("point")
    amount = action.get("amount")
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
        (
            "amount" in action
            and not (_is_int(amount) and 1 <= int(str(amount)) <= _MAX_AMOUNT),
            "action.amount",
        ),
        ("element" in action and not _element_ok(action["element"]), "action.element"),
        (has_from and _screen_id(line["from_screen_id"]) is None, "from_screen_id"),
        (
            kind == "scroll"
            and isinstance(point, list)
            and any(_is_int(n) and int(str(n)) < 0 for n in point),
            "action.point",
        ),
        (kind == "tap" and not (has_from and "point" in action), "tap fields"),
        (
            kind == "scroll"
            and not (has_from and "point" in action and "amount" in action),
            "scroll fields",
        ),
        (
            kind not in ("tap", "scroll")
            and ("point" in action or "element" in action),
            "tap fields",
        ),
        (kind != "scroll" and "amount" in action, "scroll fields"),
        (kind == "scroll" and "element" in action, "scroll fields"),
        (kind == "launch" and (has_from or "off_target" in line), "launch fields"),
        (
            "restart" in line and not (line["restart"] is True and kind == "launch"),
            "restart",
        ),
        (
            "replay" in line and not (line["replay"] is True and kind == "tap"),
            "replay",
        ),
        (
            "after_scroll" in line
            and not (
                kind == "tap"
                and _is_int(line["after_scroll"])
                and int(str(line["after_scroll"])) >= 1
            ),
            "after_scroll",
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
        {"from_screen_id", "off_target", "restart", "replay", "after_scroll"},
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
        ok = _keys_ok(line, base | {"dump_failed", "to_screen_id"}, {"scroll_effect"})
        ok = ok and _screen_id(line.get("to_screen_id")) is not None
        ok = ok and line.get("scroll_effect", "changed") in _SCROLL_EFFECTS
        return None if ok else "end line with a screen"
    return "end line without a result"


def _line_problem(line: dict[str, object]) -> str | None:
    schema = line.get("schema")
    if schema == _START:
        return _start_problem(line)
    if schema == _END:
        return _end_problem(line)
    return "schema"


def _after_scroll_problem(
    line: dict[str, object], scrolls: dict[int, str | None]
) -> str | None:
    """Why the ``after_scroll`` of a start line (if it has one) is not as uiprobe
    writes it: it names no scroll sent before, or a scroll whose read is
    another screen than the one the tap was decided on."""
    after = line.get("after_scroll")
    if after is None:
        return None
    if int(str(after)) not in scrolls:
        return "an after_scroll that names no earlier scroll"
    read = scrolls[int(str(after))]
    if read is not None and read != line.get("from_screen_id"):
        return "an after_scroll that names a scroll read of another screen"
    return None


def _sequence_problem(entries: list[dict[str, object]]) -> tuple[int, str] | None:
    """The first line (counted from 1) that breaks the strict alternation of
    start and end lines, ids rising: ``S1 E1 S2 E2 ...`` with a last start line
    that may have no end line. Also: a ``scroll_effect`` only on the end line of
    a scroll, and an ``after_scroll`` only naming a scroll that was sent before
    the tap and whose read is the screen the tap was decided on (the scroll's
    end line names that screen; when it names none, the read came on a later
    try and nothing says otherwise)."""
    open_id: object = None
    open_kind: object = None
    last_id = 0
    # the sent scrolls, and the screen each one's end line says was read
    scrolls: dict[int, str | None] = {}
    for number, line in enumerate(entries, 1):
        action_id = int(str(line["action_id"]))
        if line["schema"] == _START:
            if open_id is not None:
                return number, "a start line while an action is open"
            if action_id <= last_id:
                return number, "an action id that does not rise"
            action = line["action"]
            kind = action["kind"] if isinstance(action, dict) else None
            problem = _after_scroll_problem(line, scrolls)
            if problem is not None:
                return number, problem
            if kind == "scroll":
                scrolls[action_id] = None
            open_id = last_id = action_id
            open_kind = kind
        elif open_id != action_id:
            return number, "an end line that does not close the open action"
        else:
            if "scroll_effect" in line and open_kind != "scroll":
                return number, "a scroll_effect on the end line of another action"
            if line.get("not_sent") or line.get("failed"):
                scrolls.pop(action_id, None)  # a scroll that was not sent
            elif action_id in scrolls:
                scrolls[action_id] = _screen_id(line.get("to_screen_id"))
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
    scrolls: list[_Scroll] = []
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
        if kind == "scroll":
            from_id = _screen_id(start_line.get("from_screen_id"))
            if from_id is not None:
                scrolls.append(_Scroll(action_id, from_id, end))
            continue
        if kind != "tap":
            continue
        tap = _tap_of(action_id, start_line, action, end)
        if tap is None:
            continue
        first_from = first_from or tap.from_screen_id
        taps.setdefault((tap.from_screen_id, tap.element_id), []).append(tap)
    return _Moves(
        taps,
        root,
        first_from,
        len(lines.order),
        lines.off_after,
        scrolls,
    )


def _scroll_target(scroll: _Scroll, reader: _Reader) -> str | None:
    """The screen id a sent scroll led to: its end line's, or, when the end line
    names none (the dump failed and the screen was read on a later try), the id
    of the screen saved as its snapshot."""
    if scroll.end is None:
        return None
    named = _screen_id(scroll.end.get("to_screen_id"))
    if named is not None:
        return named
    saved = reader.load_scroll(scroll.action_id)
    return None if saved is None else saved[0].screen_id


def _families(moves: _Moves, reader: _Reader) -> dict[str, str]:
    """Which screen ids are one screen: member id -> its one representative.

    A sent scroll that did not navigate and led from one id to another links the
    two. The links are taken as they come into groups (a union, so a chain, a
    cycle or a hostile folder cannot leave a dangling or looping alias), and each
    group keeps ONE representative that is never mapped away: its first id, in the
    order the scrolls name them, that has a readable file. Only ids whose files
    agree on package and activity are joined; an id with another package or
    activity stays a screen of its own, and an id with no readable file is mapped
    to the representative (it has nothing to say of its own). A group with no
    readable id is not joined at all. The map has no key that is also a value."""
    parent: dict[str, str] = {}
    order: list[str] = []

    def find(one: str) -> str:
        while parent[one] != one:
            parent[one] = parent[parent[one]]
            one = parent[one]
        return one

    for scroll in moves.scrolls:
        if not _scroll_read(scroll) or scroll.end is None or _scroll_navigated(scroll):
            continue
        target = _scroll_target(scroll, reader)
        if target is None or target == scroll.from_screen_id:
            continue
        # An edge between two readable ids that disagree on package or activity
        # is no link at all, so nothing can join them through it.
        left = reader.load(scroll.from_screen_id)
        right = reader.load(target)
        if (
            left is not None
            and right is not None
            and (left[1], left[2]) != (right[1], right[2])
        ):
            continue
        for one in (scroll.from_screen_id, target):
            if one not in parent:
                parent[one] = one
                order.append(one)
        parent[find(target)] = find(scroll.from_screen_id)
    groups: dict[str, list[str]] = {}
    for one in order:
        groups.setdefault(find(one), []).append(one)
    family: dict[str, str] = {}
    for members in groups.values():
        readable = [(one, reader.load(one)) for one in members]
        known = [(one, found) for one, found in readable if found is not None]
        if not known:
            continue
        rep, home = known[0]
        for one, found in readable:
            if one == rep:
                continue
            if found is None or (found[1], found[2]) == (home[1], home[2]):
                family[one] = rep
    return family


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
    named.extend(scroll.from_screen_id for scroll in moves.scrolls)
    named.extend(_sent_target(tap) for taps in moves.taps.values() for tap in taps)
    for sid in named:
        if sid is not None and sid not in wanted:
            wanted.append(sid)
    wanted.extend(sid for sid in sorted(reader.names) if sid not in wanted)
    return wanted


_View = tuple[int | None, Screen]


def _scroll_views(
    sid: str,
    screen: Screen,
    moves: _Moves,
    reader: _Reader,
    members: set[str],
    activity: str | None,
    owners: dict[int | None, str | None],
) -> list[_View]:
    """The reads of a screen: its own file (scroll id None), then the
    screen read after each sent scroll that was sent on it (or on another id of
    the same screen, ``members``), in action order, then the files of the other
    ids. ``owners`` is filled with the screen id each read is a read of."""
    views: list[_View] = [(None, screen)]
    owners[None] = sid
    for scroll in moves.scrolls:
        if (
            scroll.from_screen_id not in members
            or not _scroll_read(scroll)
            or _scroll_navigated(scroll)
        ):
            continue
        loaded = reader.load_scroll(scroll.action_id)
        # A read of another activity (a scroll whose end line says the dump failed,
        # then a read of another page) is not more of the list.
        if loaded is not None and loaded[2] == activity:
            views.append((scroll.action_id, loaded[0]))
            owners[scroll.action_id] = _scroll_target(scroll, reader)
    # The files of the other ids of the screen: what a BACK or a relaunch ended
    # on, or a crawl pressed in, under the id the list got as it scrolled. They are
    # reads of this screen too (a negative key), so that nothing found only there
    # is lost when no node is made for such an id.
    for number, other in enumerate(sorted(members - {sid}), 1):
        loaded = reader.load(other)
        if loaded is not None and loaded[2] == activity:
            views.append((-number, loaded[0]))
            owners[-number] = other
    return views


def _scroll_navigated(scroll: _Scroll) -> bool:
    """Whether the scroll ended on another page (its end line says
    ``scroll_effect: navigation``): the screen read after it is that page, not
    more of the list, so its rows are not listed under the screen scrolled on
    and its id is not made part of that screen."""
    return scroll.end is not None and scroll.end.get("scroll_effect") == "navigation"


def _scroll_read(scroll: _Scroll) -> bool:
    """Whether a scroll was sent. The screen after it may have been read only
    on a later try (the end line then says ``dump_failed``): its file is
    written when it is read, so it is used whenever the scroll was sent."""
    end = scroll.end
    return end is not None and not end.get("failed") and not end.get("not_sent")


def _same_element(
    recorded: dict[str, object], element: Element, *, texts: bool
) -> bool:
    """Whether the element a tap recorded is this element of a read: the same
    class, resource id and bounds, and (when texts are recorded and this one
    is not hidden) the same text, description and hint."""
    same = (
        recorded.get("class") == element.class_name
        and recorded.get("resource_id", "") == element.resource_id
        and recorded.get("bounds") == element.bounds.to_list()
    )
    if same and texts and "hidden" not in recorded:
        same = (
            recorded.get("text", "") == element.text
            and recorded.get("content_desc", "") == element.content_desc
            and recorded.get("hint", "") == element.hint
        )
    return same


def _view_of(
    tap: _Tap,
    views: list[_View],
    *,
    texts: bool,
    owners: dict[int | None, str | None],
) -> tuple[bool, int | None]:
    """Whether a read of the screen holds the element the tap recorded, and the
    scroll id of that read (None: the screen's own file).

    The read the start line names (``after_scroll``) comes first. Otherwise the
    first read that has the element's id with the fields the tap recorded; for
    a row whose text sits in a child, the row itself says nothing of the text,
    so a tap that does not name its read can then be put on the wrong read; the
    reads of the screen id the tap was decided on are tried before the others.
    """
    for scroll_id, screen in views:
        if (
            scroll_id is not None
            and scroll_id == tap.after_scroll
            and any(e.id == tap.element_id for e in screen.elements)
        ):
            return True, scroll_id
    # The reads of the id the tap was decided on first, its own file before the
    # reads that scrolls brought (an unmarked tap is of the file the id was first
    # written from), then the others.
    own = sorted(
        (v for v in views if owners.get(v[0]) == tap.from_screen_id),
        key=lambda v: v[0] is not None and v[0] > 0,
    )
    rest = [v for v in views if v not in own]
    for scroll_id, screen in (*own, *rest):
        for element in screen.elements:
            if element.id == tap.element_id and _same_element(
                tap.element, element, texts=texts
            ):
                return True, scroll_id
    # No read has the element as the tap recorded it (its label changed since):
    # an attempt is still an attempt of the element of that id, in the reads of
    # the id it was decided on.
    for scroll_id, screen in own:
        if any(e.id == tap.element_id for e in screen.elements):
            return True, scroll_id
    return False, None


def _scrolled_rows(
    views: list[_View],
    first: list[Row],
    taps_by: dict[tuple[int | None, str], list[_Tap]],
    *,
    record_text: bool,
) -> tuple[list[Row], list[tuple[Row, Screen]], dict[tuple[int | None, str], Row]]:
    """The rows of all the reads, in order, and for each read after a scroll
    the rows it adds: `SCROLLED_REPEATS` says whether one that an earlier read
    listed already is added again.

    Returns the rows, each (row, its read) pair for matching skip reasons, and
    for each row dropped as a repeat the row that stands for it.
    """
    rows = list(first)
    owner: list[tuple[Row, Screen]] = [(row, views[0][1]) for row in first]
    stand_in: dict[tuple[int | None, str], Row] = {}
    once = SCROLLED_REPEATS == "once" and record_text
    # The rows listed so far by kind and fingerprint, in order: a row of a later
    # read is a repeat of the n-th of them when it is the n-th of its kind and
    # fingerprint in its own read, so two identical rows that show up together
    # stay two. A label that became clickable is a row where it was a text: it
    # is not a repeat of the text, so its press and destination are kept.
    listed: dict[tuple[str, str, str], list[Row]] = {}
    if once and len(views) > 1:
        listed = _first_by_print(views[0][1], first)
    for scroll_id, screen in views[1:]:
        tapped = {eid for (vid, eid) in taps_by if vid == scroll_id}
        later = _rows_of(screen, tapped, record_text=record_text)
        prints = _Prints(screen) if once else None
        by_id = {e.id: e for e in screen.elements}
        here: dict[tuple[str, str, str], int] = {}
        added: list[tuple[tuple[str, str, str], Row]] = []
        for row in later:
            row.view_key = scroll_id
            row.after_scroll = (
                scroll_id if scroll_id is not None and scroll_id > 0 else None
            )
            if prints is not None:
                identity = _identity(prints, by_id, row)
                number = here.get(identity, 0)
                here[identity] = number + 1
                earlier = listed.get(identity, [])
                if number < len(earlier):
                    stand_in[scroll_id, row.element_id] = earlier[number]
                    continue
                added.append((identity, row))
            rows.append(row)
            owner.append((row, screen))
        for identity, row in added:
            listed.setdefault(identity, []).append(row)
    return rows, owner, stand_in


def _identity(
    prints: _Prints, by_id: dict[str, Element], row: Row
) -> tuple[str, str, str]:
    """What makes a row of a later read a repeat of an earlier one: its kind and
    fingerprint, and the fingerprint of the outer row it is folded into in its own
    read (none for a row that is not folded): the same label under another
    parent is another row."""
    outer = by_id.get(row.folded_into) if row.folded_into is not None else None
    return (
        row.kind,
        prints.of(by_id[row.element_id]),
        "" if outer is None else prints.of(outer),
    )


def _first_by_print(
    screen: Screen, rows: list[Row]
) -> dict[tuple[str, str, str], list[Row]]:
    prints = _Prints(screen)
    by_id = {e.id: e for e in screen.elements}
    found: dict[tuple[str, str, str], list[Row]] = {}
    for row in rows:
        found.setdefault(_identity(prints, by_id, row), []).append(row)
    return found


def _match_skips(
    owner: list[tuple[Row, Screen]],
    by_fp: dict[str, list[str]],
    skips: _Skips,
    ids: list[str],
    owners: dict[int | None, str | None],
    *,
    record_text: bool,
) -> set[str]:
    """Put the skip reasons on the rows whose fingerprint they name; return the
    fingerprints that found a row. `ids` are the screen ids that make one screen
    (the first is the screen's own): the skip lines decided on any of them count,
    since a row read after a scroll may belong to the screen under another id.
    Where the texts were blanked, the element id of a line is all there is, and it
    is looked up only under the id that owns the read the row came from."""
    seen: set[str] = set()
    indexes: dict[int, tuple[_Prints, dict[str, Element]]] = {}
    for row, view in owner:
        if row.kind != "row":
            continue
        held = indexes.get(id(view))
        if held is None:
            held = indexes[id(view)] = (
                _Prints(view),  # one index of the read, built once
                {e.id: e for e in view.elements},
            )
        prints, by_id = held
        element = by_id[row.element_id]
        fp = prints.of(element)
        blanked = not record_text or "password" in element.flags
        if len(ids) > 1 and blanked:
            # Several ids make one screen and the texts are blanked: a
            # fingerprint says nothing, so only the lines of the id that owns the
            # read the row came from can name this row, by its element id.
            home = owners.get(row.view_key) or ids[0]
            fp = skips.by_element.get(home, {}).get(row.element_id, fp)
            reasons = skips.by_screen.get(home, {}).get(fp)
            if reasons is not None:
                row.reasons = tuple(reasons)
                seen.add(fp)
            continue
        if fp not in by_fp and blanked:
            # The texts were blanked when the line was written, so the
            # fingerprint cannot match; the element id of the line does.
            fp = skips.by_element.get(ids[0], {}).get(row.element_id, fp)
        if fp in by_fp:
            row.reasons = tuple(by_fp[fp])
            seen.add(fp)
    return seen


def _unmatched(
    skips: _Skips, ids: list[str], seen: set[str]
) -> list[tuple[str, tuple[str, ...]]]:
    """The skip entries of every id of a screen that found no row: each id's own
    reasons for a fingerprint, in the order of the ids, once each."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for one in ids:
        for fingerprint, reasons in skips.by_screen.get(one, {}).items():
            entry = (fingerprint, tuple(reasons))
            if fingerprint not in seen and entry not in found:
                found.append(entry)
    return found


def _check_folds(rows: list[Row]) -> None:
    """Keep a row's fold only where it still means what it said: the outer row of
    its own read is the one row listed with that element id. The rows of several
    reads can share element ids (two reads each have an outer ``e1``), and the
    renderers look a fold up by element id, so a fold that is not unambiguous is
    dropped: the row is then listed on its own line."""
    by_id: dict[str, list[Row]] = {}
    for row in rows:
        by_id.setdefault(row.element_id, []).append(row)
    for row in rows:
        if row.folded_into is None:
            continue
        outer = by_id.get(row.folded_into, [])
        if len(outer) != 1 or outer[0].view_key != row.view_key:
            row.folded_into = None


def _holds_clickable(screen: Screen, index: dict[str, int]) -> list[bool]:
    """For each element: it is clickable, or something below it is."""
    elements = screen.elements
    holds = ["clickable" in e.flags for e in elements]
    for element in elements:
        if "clickable" not in element.flags:
            continue
        node = index.get(element.parent) if element.parent is not None else None
        while node is not None and not holds[node]:
            holds[node] = True
            parent = elements[node].parent
            node = index.get(parent) if parent is not None else None
    return holds


def _separator_text(
    screen: Screen, i: int, children: dict[str, list[int]], *, record_text: bool
) -> str | None:
    """The first non-empty text of the element and what is below it, in
    document order; None when there is none, or when texts were not recorded."""
    if not record_text:
        return None
    for j in _subtree(i, children, screen):
        element = screen.elements[j]
        text = element.text.strip()
        if text:
            return _PASSWORD_NAME if "password" in element.flags else text
    return None


def _read_groups(
    screen: Screen, *, record_text: bool
) -> tuple[dict[str, int], list[str | None]]:
    """The groups of one read: the group number of every element under the
    list's children, and the text of each separator (group 1, 2, ...).

    The list is the element with the most direct children that are or hold a
    clickable, at least 3 (the first one on a tie). Among its children, one that
    holds no clickable and lies between two that do is a separator; it starts a
    new group, and the children before the first separator are group 0."""
    index = {e.id: i for i, e in enumerate(screen.elements)}
    children: dict[str, list[int]] = {}
    for i, element in enumerate(screen.elements):
        if element.parent in index:
            children.setdefault(element.parent, []).append(i)  # type: ignore[arg-type]
    holds = _holds_clickable(screen, index)
    best = -1
    best_count = 2  # a list has at least 3
    for i, element in enumerate(screen.elements):
        count = sum(1 for c in children.get(element.id, ()) if holds[c])
        if count > best_count:
            best, best_count = i, count
    if best < 0:
        return {}, []
    kids = children[screen.elements[best].id]
    marked = [n for n, c in enumerate(kids) if holds[c]]
    first, last = marked[0], marked[-1]
    members: dict[str, int] = {}
    texts: list[str | None] = []
    for n, child in enumerate(kids):
        if not holds[child] and first < n < last:
            texts.append(
                _separator_text(screen, child, children, record_text=record_text)
            )
        stack = [child]
        while stack:
            i = stack.pop()
            element_id = screen.elements[i].id
            if element_id not in members:
                members[element_id] = len(texts)
                stack.extend(children.get(element_id, ()))
    return members, texts


def _assign_groups(
    rows: list[Row], views: list[_View], *, record_text: bool
) -> list[str | None]:
    """Give each row its group and return the group texts (by group number);
    nothing is changed, and the result is empty, when no read of the screen has
    a separator.

    Each read is grouped on its own. The first read keeps its numbers. For a
    read after a scroll: its group 0 (what comes before its first separator)
    continues the last group listed so far; its other groups take the number of
    the group already listed with the same text, and a new number otherwise (a
    separator with no text is always a new group)."""
    found = [
        (key, *_read_groups(screen, record_text=record_text)) for key, screen in views
    ]
    if not any(texts for _, _, texts in found):
        return []
    listed: list[str | None] = [None]
    numbers: dict[int | None, dict[int, int]] = {}
    for position, (key, _, texts) in enumerate(found):
        mapping = {0: 0 if position == 0 else len(listed) - 1}
        for local, text in enumerate(texts, 1):
            same = None
            if position > 0 and text is not None:
                taken = {n for local_n, n in mapping.items() if local_n}
                same = next(
                    (
                        n
                        for n, t in enumerate(listed)
                        if n and t == text and n not in taken
                    ),
                    None,
                )
            if same is None:
                listed.append(text)
                same = len(listed) - 1
            mapping[local] = same
        numbers[key] = mapping
    members = {key: element_ids for key, element_ids, _ in found}
    for row in rows:
        local = members.get(row.view_key, {}).get(row.element_id)
        if local is not None:
            row.group = numbers[row.view_key][local]
    return listed


def _node(  # noqa: PLR0912 - one step per kind of read
    sid: str,
    loaded: tuple[Screen, str, str | None],
    moves: _Moves,
    skips: _Skips,
    reader: _Reader,
    own: frozenset[str],
    warnings: _Warnings,
    *,
    record_text: bool,
) -> ScreenNode:
    screen, screen_package, activity = loaded
    members = {sid, *(a for a, first in moves.family.items() if first == sid)}
    owners: dict[int | None, str | None] = {}
    views = _scroll_views(sid, screen, moves, reader, members, activity, owners)
    taps_by: dict[tuple[int | None, str], list[_Tap]] = {}
    for (from_id, element_id), taps in moves.taps.items():
        if from_id not in members:
            continue
        for tap in taps:
            found, view = (
                _view_of(tap, views, texts=record_text, owners=owners)
                if len(views) > 1
                else (False, None)
            )
            if from_id != sid and not found:
                # another id of this screen, and none of its reads has the
                # element: it cannot be hung on a row of another id's read, so it
                # is said instead of dropped quietly
                warnings.append(
                    f"a tap names {element_id}, which is not in the file of {from_id}"
                )
                continue
            taps_by.setdefault((view, element_id), []).append(tap)
    own_ids = {eid for (vid, eid) in taps_by if vid is None}
    first = _rows_of(screen, own_ids, record_text=record_text)
    rows, owner, stand_in = _scrolled_rows(
        views, first, taps_by, record_text=record_text
    )
    ids = [sid, *sorted(members - {sid})]
    by_fp: dict[str, list[str]] = {}
    for one in ids:
        for fingerprint, reasons in skips.by_screen.get(one, {}).items():
            # one fingerprint decided under two ids: the reasons of both
            merged = [*by_fp.get(fingerprint, []), *reasons]
            by_fp[fingerprint] = list(dict.fromkeys(merged))
    unmatched: list[tuple[str, tuple[str, ...]]] = []
    unchecked = False
    if by_fp and any(len(v.elements) > _MAX_FINGERPRINTED for _, v in views):
        unchecked = True
        unmatched = _unmatched(skips, ids, set())
    elif by_fp:
        seen = _match_skips(owner, by_fp, skips, ids, owners, record_text=record_text)
        unmatched = _unmatched(skips, ids, seen)
    # Every tap of a row, also those decided on a later read of a row that is
    # shown once (its repeat), so that no destination is lost.
    attached: dict[int, tuple[Row, list[_Tap]]] = {}
    for row in rows:
        key = (row.view_key, row.element_id)
        if key in taps_by:
            attached.setdefault(id(row), (row, []))[1].extend(taps_by[key])
    for key, row in stand_in.items():
        if key in taps_by:
            attached.setdefault(id(row), (row, []))[1].extend(taps_by[key])
    for row, taps in attached.values():
        _attach(row, taps, sid, own, reader, moves)
    _check_folds(rows)
    groups = _assign_groups(rows, views, record_text=record_text)
    return ScreenNode(
        sid,
        screen_package,
        activity,
        rows,
        unmatched,
        tuple(
            dict.fromkeys(
                reason for one in ids for reason in skips.whole_screen.get(one, ())
            )
        ),
        "too_many_elements" if unchecked else None,
        _is_dialog(_Tree(screen)),
        groups,
    )


def _read_moves_checked(
    base: Path,
    warnings: _Warnings,
    reader: _Reader,
    own: frozenset[str],
    *,
    foreign: bool,
) -> _Moves:
    """The moves of the log, with the gate failed (`distrust`) when a screen file
    of another package is in the folder. A scroll snapshot counts only when a
    scroll action of the log names it: a stray screens/scroll-<n>.json that no
    action refers to changes nothing."""
    first = _Warnings()
    moves = _read_moves(base, first, distrust=foreign)
    named = {scroll.action_id for scroll in moves.scrolls}
    if not foreign and any(
        found is not None and found[1] not in own
        for found in (reader.load_scroll(n) for n in sorted(named))
    ):
        return _read_moves(base, warnings, distrust=True)
    warnings.kept.extend(first.kept)
    warnings.count += first.count
    return moves


def read_tree(run_dir: str | Path) -> RunTree:
    """Read the run folder `run_dir` and return its tree.

    Raises:
        TreeError: `run_dir` is not a folder, or its ``run-start.json`` is
            missing or not a JSON object with a ``package``. Anything else
            that cannot be read becomes a warning, and the rest is built.
    """
    base = Path(run_dir)
    warnings = _Warnings()
    start, repeated = _read_start(base)
    package = str(start["package"])
    record_text = start.get("record_text") is not False
    label = _str_or_none(start.get("app_label"))
    listed = None if repeated else _also_target(start, package)
    if repeated:
        warnings.append("run-start.json repeats a key, so its also_target is not used")
    elif listed is None:
        warnings.append(
            "run-start.json has an also_target that is not a list of other"
            " packages; it is not used"
        )
    also = listed or ()
    # A permission package never owns screen files, listed or not: the crawl
    # never writes them, so one in the folder fails the check below.
    own = frozenset((package, *(n for n in also if n not in PERMISSION_PACKAGES)))
    ended = _read_end(base, warnings)
    skips = _read_skips(base, warnings)
    reader = _Reader(base, warnings)
    # uiprobe writes only screens of the target app and of the packages the
    # run listed in also_target: another package's file means the folder is
    # not as uiprobe wrote it.
    read = [reader.load(sid) for sid in sorted(reader.names)]
    foreign = listed is None or any(
        found is not None and found[1] not in own for found in read
    )
    moves = _read_moves_checked(base, warnings, reader, own, foreign=foreign)
    moves.family = _families(moves, reader)
    screens: dict[str, ScreenNode] = {}
    family = moves.family
    for sid in _screen_ids(reader, moves):
        if sid in family:
            continue  # another id of a screen listed under its first id
        loaded = reader.load(sid)
        if loaded is not None:
            screens[sid] = _node(
                sid,
                loaded,
                moves,
                skips,
                reader,
                own,
                warnings,
                record_text=record_text,
            )
    first_root = family.get(moves.root, moves.root) if moves.root else None
    root = first_root if first_root in screens else moves.first_from
    root = family.get(root, root) if root else None
    if root not in screens:
        root = None
    for sid, element_id in moves.taps:
        if sid in family:
            continue  # the screen's node said it when it could not hang the tap
        node = screens.get(sid)
        if node is None:
            warnings.append(f"a tap names the screen {sid}, which has no readable file")
        elif all(r.element_id != element_id for r in node.rows):
            warnings.append(
                f"a tap names {element_id}, which is not in the file of {sid}"
            )
    reached = _reachable(root, screens)
    led_to = {
        family.get(target, target)  # a joined id is its first one
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
            and node.package in own
            and sid != root
            and sid not in led_to
        ],
        warnings=warnings.result(),
        also_target=also,
    )


def _attach(
    row: Row,
    taps: list[_Tap],
    sid: str,
    own: frozenset[str],
    reader: _Reader,
    moves: _Moves,
) -> None:
    """Put the taps on an element on its row: the best one stands for the row,
    and each further sent tap that led elsewhere is an `also_to`."""
    best = min(taps, key=_rank)  # the first of the best rank
    row.action_id = best.action_id
    row.to = _ending(best, sid, own, reader, moves)
    row.pressed = "not_sent" not in row.to and "unknown" not in row.to
    if row.pressed:
        _rename(row, best.element)
    shown = [row.to]
    row.other_attempt = any(
        _rank(tap) != 0 or "same_screen" not in _ending(tap, sid, own, reader, moves)
        for tap in taps
    )
    for tap in taps:
        if tap is best or _rank(tap) != 0:
            continue
        ending = _ending(tap, sid, own, reader, moves)
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
    tap: _Tap, from_id: str, own: frozenset[str], reader: _Reader, moves: _Moves
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
    family = moves.family
    if target == from_id or any(
        family.get(target, target) == family.get(one, one)
        for one in (from_id, tap.from_screen_id)
    ):
        # The tap stayed on the screen it was pressed on, also when that screen
        # has another id than the one it is listed under (a list whose id
        # changed as it scrolled is one screen).
        return {"same_screen": True}
    return _led_to(target, own, reader, moves, tap.action_id)


def _led_to(
    target: str,
    own: frozenset[str],
    reader: _Reader,
    moves: _Moves,
    action_id: int,
) -> Ending:
    target = moves.family.get(target, target)  # a joined id is its first one
    if reader.load(target) is not None:
        # a file of the run's package or of a listed one (see the gate)
        return {"screen_id": target}
    # No file: another app's screen (uiprobe writes only the app's own screens,
    # and those of the packages the run listed), but only when the start line
    # right after this action's end line was decided on this very screen and
    # names a non-empty package that is not one of the run's own.
    named = moves.off_after.get(action_id)
    if named is None:
        return {"not_read": True}
    return {"unknown": True} if named in own else {"other_app": named}


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
    if row.after_scroll is not None:
        data["after_scroll"] = row.after_scroll
    data["group"] = row.group
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
    if tree.also_target:
        run["also_target"] = list(tree.also_target)
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
        if node.popup:
            entry["popup"] = True
        if node.groups:
            entry["groups"] = [
                {"index": n, "text": text} for n, text in enumerate(node.groups)
            ]
        screens[sid] = entry
    return {
        "schema": TREE_SCHEMA,
        "run": run,
        "root": tree.root,
        "screens": screens,
        "unreached": list(tree.unreached),
        "warnings": list(tree.warnings),
    }
