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
from typing import Final

from uiprobe.crawl import fingerprint_of
from uiprobe.model import Bounds, Element, Screen

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
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_PASSWORD_NAME: Final = "[password field]"  # noqa: S105 (a marker, not a secret)

Ending = dict[str, object]


class TreeError(Exception):
    """The folder is not a run folder this command can read."""


@dataclass(slots=True, kw_only=True)
class Row:
    """One line of a screen: a clickable row, or a text outside any row.

    ``kind`` is ``row`` or ``heading``. ``texts`` are all the texts of the row
    in document order, ``name`` first (the name itself is not always a text:
    it can be a content description or a marker such as ``[icon: ImageButton]``).
    ``to`` is the ending of a tap that was attempted: ``{"screen_id": ...}``,
    ``{"same_screen": True}``, ``{"other_app": package or None}``,
    ``{"not_read": True}`` or ``{"not_sent": reason}``; None when no tap on
    this row is recorded. A row whose tap was not sent is not ``pressed``.
    """

    element_id: str
    kind: str
    class_name: str
    name: str
    texts: tuple[str, ...]
    hidden: bool
    pressed: bool = False
    action_id: int | None = None
    to: Ending | None = None
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


class _UnreadableError(Exception):
    """A file that is refused or cannot be read; the text says why."""


def _read_bytes(path: Path, limit: int) -> bytes:
    """Read a regular file inside the run folder, never following a link.

    Raises:
        FileNotFoundError: Nothing is there.
        _UnreadableError: A link, a file that is not regular (a FIFO, a folder), a
            file over `limit` bytes, or a read error.
    """
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
    if before.st_size > limit:
        raise _UnreadableError(
            f"is over {limit // (1024 * 1024)} MiB, so it is skipped"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _UnreadableError(exc.strerror or "cannot be read") from None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev)
            and before.st_ino != 0
        ):
            raise _UnreadableError("changed while it was opened")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise _UnreadableError(exc.strerror or "cannot be read") from None
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise _UnreadableError(
            f"is over {limit // (1024 * 1024)} MiB, so it is skipped"
        )
    return data


def _loads(data: bytes) -> object:
    """Parse one JSON text; raises ValueError for anything that is not JSON
    (also bytes that are not UTF-8, and nesting too deep to parse)."""
    try:
        return json.loads(data.decode("utf-8"))
    except RecursionError:
        msg = "nested too deeply"
        raise ValueError(msg) from None


def _read_json(path: Path) -> object:
    """Parse a JSON file; raises FileNotFoundError, _UnreadableError or ValueError."""
    return _loads(_read_bytes(path, MAX_FILE_BYTES))


def _is_folder(path: Path) -> bool:
    """Whether `path` is a real folder, not a link to one."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _lines(path: Path, warnings: list[str]) -> list[dict[str, object]]:
    """The JSON objects of a ``.jsonl`` file; a bad line is a warning."""
    try:
        data = _read_bytes(path, MAX_LOG_BYTES)
    except FileNotFoundError:
        return []  # written at the first line, so absent when there was none
    except _UnreadableError as exc:
        warnings.append(f"{path.name} {exc}")
        return []
    found: list[dict[str, object]] = []
    for number, raw in enumerate(data.split(b"\n"), 1):
        if not raw.strip():
            continue
        try:
            value = _loads(raw)
        except ValueError:
            warnings.append(f"{path.name} line {number} is not valid UTF-8 JSON")
            continue
        if isinstance(value, dict):
            found.append(value)
        else:
            warnings.append(f"{path.name} line {number} is not a JSON object")
    return found


def _screen_id(value: object) -> str | None:
    if isinstance(value, str) and _SCREEN_ID.fullmatch(value):
        return value
    return None


def _action_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _Reader:
    """Loads screen files on demand, once each."""

    def __init__(self, run_dir: Path, warnings: list[str]) -> None:
        self._dir = run_dir / "screens"
        self._warnings = warnings
        if os.path.lexists(self._dir) and not _is_folder(self._dir):
            warnings.append("screens is a link or not a folder, so it is not read")
            self.usable = False
        else:
            self.usable = True
        self._cache: dict[str, tuple[Screen, str, str | None] | None] = {}

    def load(self, screen_id: str) -> tuple[Screen, str, str | None] | None:
        """The screen, its package and its activity, or None when no usable file."""
        if screen_id in self._cache:
            return self._cache[screen_id]
        if not self.usable:
            return None
        found: tuple[Screen, str, str | None] | None = None
        path = self._dir / f"{screen_id}.json"
        try:
            found = _parse_screen(_read_json(path))
        except FileNotFoundError:
            pass
        except _UnreadableError as exc:
            self._warnings.append(f"screens/{screen_id}.json {exc}")
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            self._warnings.append(f"screens/{screen_id}.json cannot be read")
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
) -> Row:
    return Row(
        element_id=element.id,
        kind=kind,
        class_name=element.class_name,
        name=name,
        texts=texts,
        hidden=hidden,
    )


def _short_class(class_name: str) -> str:
    return class_name.rsplit(".", 1)[-1] or class_name


def _rows_of(
    screen: Screen,
    tapped: set[str],
    *,
    record_text: bool,
) -> list[Row]:
    """The rows and headings of a screen, in document order."""
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
    rows: list[Row] = []
    for i, element in enumerate(elements):
        if is_row[i]:
            rows.append(_row(screen, i, children, record_text=record_text))
        elif not under_row[i]:
            heading = _heading(element, record_text=record_text)
            if heading is not None:
                rows.append(heading)
    return rows


def _subtree(i: int, children: dict[str, list[int]], screen: Screen) -> list[int]:
    found = [i]
    seen = {i}
    stack = [i]
    while stack:
        for child in children.get(screen.elements[stack.pop()].id, []):
            if child not in seen:  # a parent cycle must not loop
                seen.add(child)
                found.append(child)
                stack.append(child)
    return sorted(found)


def _row(
    screen: Screen, i: int, children: dict[str, list[int]], *, record_text: bool
) -> Row:
    element = screen.elements[i]
    members = [screen.elements[j] for j in _subtree(i, children, screen)]
    short = _short_class(element.class_name)
    password = any("password" in m.flags for m in members)
    if password:
        return _row_of(element, "row", _PASSWORD_NAME, (), hidden=True)
    if not record_text:
        marker = "icon" if len(members) == 1 else "row"
        return _row_of(element, "row", f"[{marker}: {short}]", (), hidden=True)
    texts = tuple(m.text.strip() for m in members if m.text.strip())
    if texts:
        name = texts[0]
    else:
        described = [m.content_desc.strip() for m in members if m.content_desc.strip()]
        name = f'"{described[0]}"' if described else f"[icon: {short}]"
    return _row_of(element, "row", name, texts, hidden=False)


def _heading(element: Element, *, record_text: bool) -> Row | None:
    if not record_text:
        return None
    if "password" in element.flags:
        if not element.text.strip():
            return None
        return _row_of(element, "heading", _PASSWORD_NAME, (), hidden=True)
    text = element.text.strip()
    if not text:
        return None
    return _row_of(element, "heading", text, (text,), hidden=False)


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
    if not isinstance(start, dict) or not isinstance(start.get("package"), str):
        msg = "run-start.json is not a run start record (no package)"
        raise TreeError(msg)
    return start


def _read_end(base: Path, warnings: list[str]) -> dict[str, object] | None:
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
    taps: dict[tuple[str, str], _Tap]
    root: str | None
    count: int
    off_target: dict[str, str]


def _note_off_target(start_line: dict[str, object], found: dict[str, str]) -> None:
    """Remember the package of a start line decided on a screen of another app,
    under the id of that screen."""
    other = start_line.get("off_target")
    decided_on = _screen_id(start_line.get("from_screen_id"))
    if isinstance(other, dict) and decided_on is not None:
        package = _str_or_none(other.get("package"))
        if package:
            found.setdefault(decided_on, package)


def _rank(tap: _Tap) -> int:
    """Which tap stands for an element: sent and read first, then one whose
    delivery is unknown, then one that was not sent."""
    if _not_sent(tap):
        return 2
    return 1 if _unknown(tap) else 0


def _read_moves(base: Path, warnings: list[str]) -> _Moves:
    """The taps of transitions.jsonl, and the screen the launch led to."""
    starts: dict[int, dict[str, object]] = {}
    ends: dict[int, dict[str, object]] = {}
    order: list[int] = []
    for line in _lines(base / "transitions.jsonl", warnings):
        action_id = _action_id(line.get("action_id"))
        schema = line.get("schema")
        if action_id is None or schema not in (
            "uiprobe.transition_start/1",
            "uiprobe.transition_end/1",
        ):
            warnings.append("transitions.jsonl has a line that is not a transition")
        elif schema == "uiprobe.transition_start/1":
            if action_id not in starts:
                order.append(action_id)
            starts[action_id] = line
        else:
            ends[action_id] = line
    # The tap that stands for each (screen, element): see `_rank`; the first
    # one of the best rank.
    taps: dict[tuple[str, str], _Tap] = {}
    off_target: dict[str, str] = {}
    root: str | None = None
    first_from: str | None = None
    for action_id in order:
        start_line = starts[action_id]
        action = start_line.get("action")
        if not isinstance(action, dict):
            warnings.append(f"action {action_id} has no action record")
            continue
        end = ends.get(action_id)
        kind = action.get("kind")
        _note_off_target(start_line, off_target)
        if kind == "launch" and root is None and end is not None:
            root = _screen_id(end.get("to_screen_id"))
        if kind != "tap":
            continue
        tap = _tap_of(action_id, start_line, action, end)
        if tap is None:
            warnings.append(f"action {action_id} is a tap with no screen or element")
            continue
        first_from = first_from or tap.from_screen_id
        old = taps.get((tap.from_screen_id, tap.element_id))
        if old is None or _rank(tap) < _rank(old):
            taps[tap.from_screen_id, tap.element_id] = tap
    return _Moves(taps, root or first_from, len(order), off_target)


@dataclass(slots=True)
class _Skips:
    by_screen: dict[str, dict[str, list[str]]]
    whole_screen: dict[str, list[str]]


def _read_skips(base: Path, warnings: list[str]) -> _Skips:
    skips = _Skips({}, {})
    for line in _lines(base / "skipped.jsonl", warnings):
        sid = _screen_id(line.get("screen_id"))
        fingerprint = line.get("fingerprint")
        reasons = line.get("reasons")
        if sid is None or not isinstance(fingerprint, str):
            warnings.append("skipped.jsonl has a line with no screen id or fingerprint")
            continue
        listed = (
            [r for r in reasons if isinstance(r, str)]
            if isinstance(reasons, list)
            else []
        )
        if fingerprint == sid:
            bucket = skips.whole_screen.setdefault(sid, [])
        else:
            bucket = skips.by_screen.setdefault(sid, {}).setdefault(fingerprint, [])
        bucket.extend(r for r in listed if r not in bucket)
    return skips


def _screen_ids(base: Path, moves: _Moves) -> list[str]:
    """The ids of the screens to read: the ones the taps name, then the rest."""
    wanted: list[str] = []
    named = [moves.root, *(key[0] for key in moves.taps)]
    named.extend(
        _screen_id(tap.end.get("to_screen_id")) if tap.end else None
        for tap in moves.taps.values()
    )
    for sid in named:
        if sid is not None and sid not in wanted:
            wanted.append(sid)
    names: list[str] = []
    if _is_folder(base / "screens"):
        try:
            names = sorted(f.name for f in (base / "screens").iterdir())
        except OSError:
            names = []
    wanted.extend(
        name[:-5]
        for name in names
        if name.endswith(".json")
        and _SCREEN_ID.fullmatch(name[:-5])
        and name[:-5] not in wanted
    )
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
    if by_fp:
        by_id = {e.id: e for e in screen.elements}
        seen: set[str] = set()
        for row in rows:
            if row.kind != "row":
                continue
            fp = fingerprint_of(screen, by_id[row.element_id])
            if fp in by_fp:
                row.reasons = tuple(by_fp[fp])
                seen.add(fp)
        unmatched = [(fp, tuple(r)) for fp, r in by_fp.items() if fp not in seen]
    for row in rows:
        tap = moves.taps.get((sid, row.element_id))
        if tap is None:
            continue
        row.action_id = tap.action_id
        row.to = _ending(tap, sid, package, reader, moves)
        row.pressed = "not_sent" not in row.to and "unknown" not in row.to
        if record_text:
            _rename(row, tap.element)
    return ScreenNode(
        sid,
        screen_package,
        activity,
        rows,
        unmatched,
        tuple(skips.whole_screen.get(sid, ())),
    )


def read_tree(run_dir: str | Path) -> RunTree:
    """Read the run folder `run_dir` and return its tree.

    Raises:
        TreeError: `run_dir` is not a folder, or its ``run-start.json`` is
            missing or not a JSON object with a ``package``. Anything else
            that cannot be read becomes a warning, and the rest is built.
    """
    base = Path(run_dir)
    warnings: list[str] = []
    start = _read_start(base)
    package = str(start["package"])
    record_text = start.get("record_text") is not False
    label = _str_or_none(start.get("app_label"))
    ended = _read_end(base, warnings)
    moves = _read_moves(base, warnings)
    skips = _read_skips(base, warnings)
    reader = _Reader(base, warnings)
    screens: dict[str, ScreenNode] = {}
    for sid in _screen_ids(base, moves):
        loaded = reader.load(sid)
        if loaded is not None:
            screens[sid] = _node(
                sid, loaded, moves, skips, reader, package, record_text=record_text
            )
    for sid, element_id in moves.taps:
        node = screens.get(sid)
        if node is None:
            warnings.append(f"a tap names the screen {sid}, which has no readable file")
        elif all(r.element_id != element_id for r in node.rows):
            warnings.append(
                f"a tap names {element_id}, which is not in the file of {sid}"
            )
    reached = _reachable(moves.root, screens)
    led_to = {
        row.to["screen_id"]
        for node in screens.values()
        for row in node.rows
        if row.to and isinstance(row.to.get("screen_id"), str)
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
        root=moves.root if moves.root in screens else None,
        screens=screens,
        unreached=[
            sid
            for sid, node in screens.items()
            if sid not in reached
            and node.package == package
            and sid != moves.root
            and sid not in led_to
        ],
        warnings=_capped(warnings),
    )


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
    elif isinstance(desc, str) and desc.strip():
        row.name = f'"{desc.strip()}"'


def _unknown(tap: _Tap) -> bool:
    """A tap whose start line has no end line, or whose sending failed: it may
    or may not have reached the device."""
    return tap.end is None or bool(tap.end.get("failed"))


def _capped(warnings: list[str]) -> list[str]:
    if len(warnings) <= MAX_WARNINGS:
        return warnings
    more = len(warnings) - MAX_WARNINGS
    return [*warnings[:MAX_WARNINGS], f"and {more} more warnings"]


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
    return _led_to(target, package, reader, moves)


def _led_to(target: str, package: str, reader: _Reader, moves: _Moves) -> Ending:
    found = reader.load(target)
    if found is not None:
        return {"screen_id": target} if found[1] == package else {"other_app": found[1]}
    # No file: a screen of another app (the crawl writes only the target
    # app's screens). Say so only when a start line decided on this very
    # screen (`from_screen_id` is the end line's screen) with `off_target`.
    if target in moves.off_target:
        return {"other_app": moves.off_target[target]}
    return {"not_read": True}


def _reachable(root: str | None, screens: dict[str, ScreenNode]) -> set[str]:
    reached: set[str] = set()
    stack = [root] if root in screens else []
    while stack:
        sid = stack.pop()
        if sid is None or sid in reached:
            continue
        reached.add(sid)
        for row in screens[sid].rows:
            target = row.to.get("screen_id") if row.to else None
            if isinstance(target, str) and target in screens:
                stack.append(target)
    return reached


def _element_dict(row: Row) -> dict[str, object]:
    data: dict[str, object] = {
        "element_id": row.element_id,
        "kind": row.kind,
        "class": row.class_name,
        "name": row.name,
        "texts": list(row.texts),
    }
    if row.hidden:
        data["hidden"] = True
    if row.kind == "row":
        data["pressed"] = row.pressed
        data["action_id"] = row.action_id
        if row.to is not None:
            data["to"] = dict(row.to)
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
