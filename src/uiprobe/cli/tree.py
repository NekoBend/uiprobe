"""The tree command: show a crawl run folder as a tree. It reads files only."""

from __future__ import annotations

import argparse  # noqa: TC003 (a runtime import in the former cli.py)
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, Literal

from uiprobe._json import dumps
from uiprobe.cli.output import _emit, _printable
from uiprobe.cli.parser import _UsageError
from uiprobe.cli.tree_html import (
    FOLD_OTHER,
    FOLD_POPUP,
    HFold,
    HPage,
    HRow,
    HSection,
    anchor_id,
    render_page,
)
from uiprobe.crawl_tree import (
    Row,
    RunTree,
    ScreenNode,
    TreeError,
    read_tree,
    tree_to_dict,
)
from uiprobe.htmlfile import HtmlFileError, check_html_path, save_html
from uiprobe.settings import _shown

LEGEND: Final = "· = not pressed, so nothing below it was looked at"

# The kinds of reason that the normal view (without `--why`) shows in brackets
# in place of the dot, as (reason, label, legend line), in the order they are
# written. No other reason is a kind; `--why` shows every reason as it is.
KIND_LABELS: Final = (
    (
        "toggle",
        "toggle",
        "[toggle] = it holds a switch or checkbox, so it was not pressed",
    ),
    (
        "navigate_up",
        "back",
        "[back] = the screen's navigate-up button, which is not pressed",
    ),
    (
        "slider",
        "slider",
        "[slider] = it is or overlaps a slider, so it was not pressed",
    ),
    ("disabled", "disabled", "[disabled] = it was disabled, so it was not pressed"),
)
NO_CHANGE_MARK: Final = " [no change]"
LEGEND_NO_CHANGE: Final = "[no change] = pressing it left the same screen"
LEGEND_DESC: Final = (
    '"..." = the element has no visible text; this is its accessibility description'
)
LEGEND_ARROW: Final = "→ PACKAGE = pressing it opened that app"
LEGEND_SEE: Final = "→ see: X = it led to a screen already shown under X"
LEGEND_EMPTY: Final = (
    "(screen with no named rows) = the screen it led to has no row with a name "
    "(see --json)"
)
LEGEND_AGAIN: Final = (
    "(pressed again) = the row above was pressed again and led to the screen below"
)
LEGEND_POPUP: Final = "[popup] = pressing it opened a popup; its contents are in --json"
LEGEND_POPUP_HTML: Final = (
    "[popup] = pressing it opened a popup; its rows are in the fold below it"
)
POPUP_MARK: Final = " [popup]"
_NO_TEXT: Final = "screen texts were not recorded in this run (record_text = false)"
_FIRST_SCREEN: Final = "first screen"
MAX_LABEL: Final = 200
_SURROGATE: Final = re.compile("[\ud800-\udfff]")


def _label(text: str) -> str:
    """A device text for the text tree: at most `MAX_LABEL` characters, with a
    visible marker for what was cut, and escaped for the terminal. (`--json`
    keeps the full text.)"""
    if len(text) <= MAX_LABEL:
        return _printable(text)
    return f"{_printable(text[:MAX_LABEL])}…(+{len(text) - MAX_LABEL} chars)"


def _json_text(tree: RunTree) -> str:
    """The JSON of the tree with lone surrogates written as escapes, so the
    output never holds a character that cannot be encoded."""
    return _SURROGATE.sub(lambda m: f"\\u{ord(m[0]):04x}", dumps(tree_to_dict(tree)))


# Which notes after a pressed row's name stay in the text tree; the others are
# in `--json` only (those the last line counts are counted there).
# Kinds: "other_app", "same_screen", "not_sent", "unknown", "not_read" and
# "screen_above" (the screen was already drawn: `→ see: NAME`).
NOTES_IN_TEXT: Final = frozenset({"other_app", "screen_above"})

# Rows named only by a content description (no visible text): "show" lists them
# by the description, "quoted" puts it in double quotes, "json_only" leaves
# them out of the text tree. A pressed one that led to a screen stays in the
# tree under any value, so that screen keeps its place.
DESC_ONLY_ROWS: Final = "quoted"

_COUNTED: Final = ("other_app", "same_screen", "not_sent", "unknown", "not_read")


@dataclass(slots=True)
class _Tally:
    """What the text tree leaves out, counted for its last line."""

    ends: dict[str, int] = field(default_factory=dict)
    texts: int = 0
    desc_only: int = 0
    unnamed: int = 0
    unmatched: int = 0
    popup_rows: int = 0  # lines of popup screens that the tree does not draw
    marks: set[str] = field(default_factory=set)  # dot, desc, arrow, see, again
    names: dict[str, int] = field(default_factory=dict)  # row names as listed


@dataclass(slots=True)
class _Entry:
    text: str
    to: dict[str, object] | None = None
    again: bool = False  # a further sent tap on the same row
    more: tuple[dict[str, object], ...] = ()
    name: str = ""
    # The parts of the line, for the HTML page (`text` is their text form).
    label: str | None = None
    note: str = ""
    dot: bool = False
    reasons: str = ""
    kinds: str = ""  # " [toggle, disabled]": shown in place of the dot
    # For the HTML page: the row starts a new group of the screen's list, and
    # the text between the rows there (already made safe for display).
    gap: tuple[str, ...] | None = None


def _heading_text(tree: RunTree, sid: str) -> str:
    node = tree.screens[sid]
    activity = _printable(node.activity) if node.activity else "-"
    return f"(screen {_printable(sid)}  {activity})"


def _kind_of(to: dict[str, object]) -> str:
    """The kind of a destination: "screen", or the kind of a note."""
    for key in ("screen_id", "same_screen", "other_app", "not_sent", "unknown"):
        if key in to:
            return "screen" if key == "screen_id" else key
    return "not_read"


def _note(to: dict[str, object], kind: str) -> str | None:
    """The note after a row's name for a destination with no screen below it,
    or None when there is nothing to say (another app with no package)."""
    if kind == "same_screen":
        return " → (same screen)"
    if kind == "other_app":
        package = to["other_app"]
        return f" → {_printable(package)}" if isinstance(package, str) else None
    if kind == "not_sent":
        reason = to["not_sent"]
        return f" → (not sent: {_printable(reason) if isinstance(reason, str) else ''})"
    if kind == "unknown":
        return " → (result unknown)"
    return " → (could not be read)"


def _endings(row: Row) -> list[dict[str, object]]:
    """Every ending of the taps on a row: the main one, then the retries."""
    return ([row.to] if row.to is not None else []) + list(row.also_to)


def _more(row: Row) -> tuple[dict[str, object], ...]:
    """The further endings of a row for the text tree, each once (a row made of
    two versions can hold the same ending twice, for its two taps)."""
    more: list[dict[str, object]] = []
    for ending in row.also_to:
        if ending != row.to and ending not in more:
            more.append(ending)
    return tuple(more)


def _keeps_line(row: Row, *, folded: bool) -> bool:
    """Whether a row has a line in the text tree. A row that has no name there
    (or none by `DESC_ONLY_ROWS`) still has one when any of its taps reached a
    screen, so that a pressed path never disappears; so has a line with folded
    elements when any part of it was pressed."""
    if row.name_from == "none" or (
        row.name_from == "content_desc" and DESC_ONLY_ROWS == "json_only"
    ):
        return (folded and row.pressed) or any("screen_id" in e for e in _endings(row))
    return True


def _no_change(row: Row, *, kept: bool) -> bool:
    """Whether a line ends in ` [no change]`: the row (with its folded
    elements) was pressed and every attempt on it was sent, finished and ended
    on the same screen. It is not used while `NOTES_IN_TEXT` writes that note
    instead."""
    endings = _endings(row)
    return (
        kept
        and row.pressed
        and bool(endings)
        and not row.other_attempt
        and "same_screen" not in NOTES_IN_TEXT
        and all(_kind_of(to) == "same_screen" for to in endings)
    )


def _row_line(
    row: Row, note: str | None, *, why: bool, tally: _Tally, no_change: bool = False
) -> _Entry:
    """The entry of a row: its line (`text`) and the parts of the line. The marks
    it uses are noted in `tally`, for the legend."""
    name = _label(row.name)
    if row.name_from == "content_desc" and DESC_ONLY_ROWS == "quoted":
        name = '"' + name.translate({ord('"'): '\\"'}) + '"'
        tally.marks.add("desc")
    if no_change:
        note = NO_CHANGE_MARK
        tally.marks.add("no_change")
    line = name + (note or "")
    if note is not None and row.to is not None and "other_app" in row.to:
        tally.marks.add("arrow")
    reasons = ""
    kinds = ""
    if not row.pressed:
        labels = [] if why else [k for k in KIND_LABELS if k[0] in row.reasons]
        if labels:
            kinds = " [" + ", ".join(k[1] for k in labels) + "]"
            tally.marks.update("kind:" + k[0] for k in labels)
            line += kinds
        else:
            tally.marks.add("dot")
            line += " ·"
        if why and row.reasons:
            reasons = " [" + ", ".join(_printable(r) for r in row.reasons) + "]"
            line += reasons
    elif why and row.reasons and row.merged_ids:
        # a pressed row made of two versions of one row keeps the reasons of the
        # version that was not pressed
        reasons = " [" + ", ".join(_printable(r) for r in row.reasons) + "]"
        line += reasons
    return _Entry(
        line,
        row.to,
        name=name,
        label=name,
        note=note or "",
        dot=not row.pressed,
        reasons=reasons,
        kinds=kinds,
    )


def _merged(row: Row, folded: list[Row]) -> Row:
    """The row with the taps of the clickable elements folded into it, as if
    they had been on the row itself: the endings of the pressed ones first, and
    pressed when any of them was."""
    if not folded:
        return row
    group = [row, *folded]
    ordered = sorted(group, key=lambda r: not r.pressed)  # stable
    endings: list[dict[str, object]] = []
    for r in ordered:
        for ending in _endings(r):
            if ending not in endings:
                endings.append(ending)
    reasons = tuple(dict.fromkeys(x for r in group for x in r.reasons))
    # The name: a visible text first, then a description, the row's own before
    # a folded element's; else the row's own (a placeholder).
    rank = {"text": 0, "content_desc": 1}
    named = [r for r in group if r.name_from in rank]
    best = min(named, key=lambda r: rank[r.name_from]) if named else row
    if row.name_from == "password":
        best = row
    return replace(
        row,
        name=best.name,
        name_from=best.name_from,
        pressed=any(r.pressed for r in group),
        to=endings[0] if endings else None,
        also_to=endings[1:],
        reasons=reasons,
        other_attempt=any(r.other_attempt for r in group),
        merged_ids=tuple(dict.fromkeys(x for r in group for x in r.merged_ids)),
    )


def _left_out_lines(row: Row, name: str, *, kept: bool) -> list[str]:
    """What the text tree leaves out of a row's endings, for the HTML page."""
    lines: list[str] = []
    if _no_change(row, kept=kept):
        return lines  # the line says it
    for to in _endings(row):
        kind = _kind_of(to)
        if kind not in _COUNTED:
            continue
        note = _note(to, kind)
        if kept and kind in NOTES_IN_TEXT and note is not None:
            continue
        result = note if note is not None else " → (another app)"
        line = f"result of pressing {name}:{result}"
        if line not in lines:
            lines.append(line)
    return lines


class _Others:
    """What the text tree leaves out of one screen, as lines for the HTML page."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def unmatched(self, node: ScreenNode) -> None:
        self.lines.extend(_unmatched_lines(node))

    def row(self, row: Row, *, kept: bool) -> None:
        self.lines.extend(_left_out(row, kept=kept))


class _NoOthers(_Others):
    """The text path: nothing is collected."""

    def unmatched(self, node: ScreenNode) -> None:
        pass

    def row(self, row: Row, *, kept: bool) -> None:
        pass


_NO_OTHERS: Final = _NoOthers()


def _unmatched_lines(node: ScreenNode) -> list[str]:
    suffix = f" ({_printable(node.unmatched_why)})" if node.unmatched_why else ""
    return [
        "skip record not matched to a row: "
        f"{_printable(fingerprint)} [{', '.join(_printable(r) for r in reasons)}]"
        f"{suffix}"
        for fingerprint, reasons in node.unmatched
    ]


def _left_out(row: Row, *, kept: bool) -> list[str]:
    """The lines of the HTML page for what the text tree leaves out of a row."""
    shown = _label(row.name)
    lines: list[str] = []
    if row.kind == "text":
        lines.append(f"text outside rows: {shown}")
    elif not kept:
        what = (
            "unnamed element"
            if row.name_from == "none"
            else "row named only by a description"
        )
        lines.append(f"{what}: {shown}")
    return [*lines, *_left_out_lines(row, shown, kept=kept)]


def _gap_texts(node: ScreenNode, before: int, group: int) -> tuple[str, ...]:
    """The texts between the rows of group `before` and group `group`: one for
    each separator on the way down the list that has text, in order, each
    shortened and escaped as a label. Going back up the list, the text of the
    separator that starts `group` only."""
    if group > before:
        texts = node.groups[before + 1 : group + 1]
    else:
        texts = node.groups[group : group + 1]
    return tuple(_label(t) for t in texts if t is not None)


def _mark_gap(
    entry: _Entry, node: ScreenNode, previous: int | None, group: int | None
) -> int | None:
    """Note on `entry` that it starts a new group of the list when the line
    drawn before it was in another group; return the group to remember."""
    if group is None:
        return previous
    if previous is not None and group != previous:
        entry.gap = _gap_texts(node, previous, group)
    return group


def _entries(
    tree: RunTree,
    sid: str,
    *,
    ids: bool,
    why: bool,
    tally: _Tally,
    others: _Others | None = None,
) -> list[_Entry]:
    """The entries of a screen. `others`, when given, collects each item the
    text tree leaves out (for the HTML page only)."""
    node = tree.screens[sid]
    entries: list[_Entry] = []
    if ids:
        entries.append(_Entry(_heading_text(tree, sid)))
    tally.unmatched += len(node.unmatched)
    extra = _NO_OTHERS if others is None else others
    extra.unmatched(node)
    previous: int | None = None  # the group of the last line drawn
    folds: dict[str, list[Row]] = {}
    for row in node.rows:
        if row.folded_into is not None:
            folds.setdefault(row.folded_into, []).append(row)
    for line_row in node.rows:
        if line_row.folded_into is not None:
            continue  # it is on the line of the row it folds into
        row = _merged(line_row, folds.get(line_row.element_id, []))
        if row.kind == "text":
            tally.texts += 1
            extra.row(row, kept=False)
            continue
        kept = _keeps_line(row, folded=line_row.element_id in folds)
        extra.row(row, kept=kept)
        note = None
        if row.to is not None and _kind_of(row.to) != "screen":
            kind = _kind_of(row.to)
            note = _note(row.to, kind) if kind in NOTES_IN_TEXT and kept else None
        # Each kind of result that no line shows is counted once for the row.
        no_change = _no_change(row, kept=kept)
        left_out = {
            kind
            for to in ([] if no_change else _endings(row))
            if (kind := _kind_of(to)) in _COUNTED
            and not (kept and kind in NOTES_IN_TEXT and _note(to, kind) is not None)
        }
        for kind in left_out:
            tally.ends[kind] = tally.ends.get(kind, 0) + 1
        if not kept:
            if row.name_from == "none":
                tally.unnamed += 1
            else:
                tally.desc_only += 1
            continue
        entry = _row_line(row, note, why=why, tally=tally, no_change=no_change)
        tally.names[entry.name] = tally.names.get(entry.name, 0) + 1
        entry.more = _more(row)
        previous = _mark_gap(entry, node, previous, line_row.group)
        entries.append(entry)
    return entries


_SUMMARY: Final = (
    ("other_app", "rows that led to another app"),
    ("same_screen", "rows that stayed on the same screen"),
    ("not_sent", "rows not sent"),
    ("unknown", "rows with an unknown result"),
    ("not_read", "rows whose next screen could not be read"),
)


_SUMMARY_TEXT: Final = "left out of the tree (see --json): "
_SUMMARY_HTML: Final = (
    "not shown as rows (in the folds below each screen, and in --json): "
)


def _summary(tally: _Tally, prefix: str = _SUMMARY_TEXT) -> str | None:
    """The last line: what the text tree leaves out, with counts."""
    parts = [
        f"{label}: {tally.ends[kind]}" for kind, label in _SUMMARY if kind in tally.ends
    ]
    for count, label in (
        (tally.popup_rows, "rows inside popups"),
        (tally.desc_only, "rows named only by a description"),
        (tally.texts, "texts outside rows"),
        (tally.unnamed, "unnamed elements"),
        (tally.unmatched, "skip records not matched to a row"),
    ):
        if count:
            parts.append(f"{label}: {count}")
    if not parts:
        return None
    return prefix + ", ".join(parts)


def _orphan_heading(tree: RunTree, sid: str) -> str:
    """The line above a screen that no pressed row led to (it was reached by a
    BACK or a relaunch, or is another reading of a screen that changes)."""
    node = tree.screens[sid]
    activity = _printable(node.activity) if node.activity else "-"
    return f"(screen not reached from a pressed row {_printable(sid)}  {activity})"


def _draw(items: list[tuple[int, str]]) -> list[str]:
    """Add the branch drawing to the lines of a tree, given as (depth, text)
    in the order they are printed."""
    last = [False] * len(items)
    later: set[int] = set()  # depths that still have a line after this one
    for i in range(len(items) - 1, -1, -1):
        depth = items[i][0]
        last[i] = depth not in later
        later = {d for d in later if d < depth} | {depth}
    lines: list[str] = []
    stack: list[bool] = []
    for i, (depth, text) in enumerate(items):
        del stack[depth:]
        prefix = "".join("   " if flag else "│  " for flag in stack)
        lines.append(prefix + ("└─ " if last[i] else "├─ ") + text)
        stack.append(last[i])
    return lines


_SEE: Final = re.compile("\x00([^\x00]*)\x00")  # a note waiting for its name


@dataclass(slots=True)
class _Node:
    """A drawn line, for the HTML page: its entry and what hangs on it."""

    depth: int
    entry: _Entry
    popup: str | None = None  # the popup screen the line ends in ` [popup]` for
    see: str | None = None  # the screen the line ends in `→ see:` for
    opens: str | None = None  # the screen whose rows are drawn below the line


@dataclass(slots=True)
class _Section:
    """The first line of the tree, or a screen no pressed row led to."""

    sid: str | None
    heading: str
    popup: bool = False
    nodes: list[_Node] = field(default_factory=list)


class _Renderer:
    """Draws the tree: each screen is listed once, where it is first reached."""

    def __init__(
        self, tree: RunTree, *, ids: bool, why: bool, html: bool = False
    ) -> None:
        self.html = html  # build what only the HTML page needs
        self.tree = tree
        self.ids = ids
        self.why = why
        self.lines: list[str] = []
        self.expanded: dict[str, str] = {}  # screen id -> name of the opening row
        self.paths: dict[str, tuple[str, ...]] = {}  # ... and the rows above it
        self.tally = _Tally()
        self.hidden: set[str] = set()  # popup screens named by a mark, not drawn
        self.sections: list[_Section] = []
        self.nodes: list[_Node] = []  # the lines of the run being drawn
        self.others: dict[str, _Others] = {}  # what a drawn screen leaves out
        self.html_marks: set[str] = set()  # marks of rows only the HTML shows

    def popup(self, sid: str) -> bool:
        """Whether a row that led to `sid` ends in ` [popup]`: the screen is a
        popup, by the crawl's own rule. Its rows are not drawn (they are
        counted once), unless the screen is drawn anyway as the first screen."""
        if not self.tree.screens[sid].popup:
            return False
        self.tally.marks.add("popup")
        if sid not in self.expanded and sid not in self.hidden:
            self.hidden.add(sid)
            scratch = _Tally()
            lines = _entries(self.tree, sid, ids=False, why=False, tally=scratch)
            self.tally.popup_rows += len(lines)
            # What the popup's screen leaves out is still left out of the tree.
            self.tally.texts += scratch.texts
            self.tally.unnamed += scratch.unnamed
            self.tally.unmatched += scratch.unmatched
        return True

    def open_screen(self, sid: str, name: str, path: tuple[str, ...]) -> list[_Entry]:
        self.expanded[sid] = name
        self.paths[sid] = path
        others = self.others.setdefault(sid, _Others() if self.html else _NO_OTHERS)
        return _entries(
            self.tree,
            sid,
            ids=self.ids,
            why=self.why,
            tally=self.tally,
            others=others,
        )

    def see(self, sid: str) -> str:
        """The note for a row that led to a screen that is already drawn. The
        name waits (as NUL, the id, NUL) until the whole tree is drawn, since
        whether it is ambiguous is known only then."""
        self.tally.marks.add("see")
        return f" → see: \x00{sid}\x00"

    def seen_name(self, sid: str) -> str:
        """The name that `→ see:` gives for the screen `sid`."""
        name = self.expanded[sid]
        path = self.paths[sid]
        if self.tally.names.get(name, 0) > 1 and path:
            return " > ".join(path)
        return name

    def seen_as(self, line: str) -> str:
        """Put the names into the notes of a line."""
        return _SEE.sub(lambda match: self.seen_name(match[1]), line)

    def track(self, depth: int, entry: _Entry) -> None:
        """Remember a drawn line (the HTML page needs it)."""
        if self.html:
            self.nodes.append(_Node(depth, entry))

    def tag(self, what: Literal["popup", "see", "opens"], sid: str) -> None:
        """Say what hangs on the line just drawn."""
        if not self.html:
            return
        node = self.nodes[-1]
        if what == "popup":
            node.popup = sid
        elif what == "see":
            node.see = sid
        else:
            node.opens = sid

    def mark_popup(self, items: list[tuple[int, str]], target: str) -> None:
        depth, text = items[-1]
        items[-1] = (depth, text + POPUP_MARK)
        self.tag("popup", target)

    def mark_see(self, items: list[tuple[int, str]], target: str) -> None:
        depth, text = items[-1]
        items[-1] = (depth, text + self.see(target))
        self.tag("see", target)

    def below_of(
        self, entry: _Entry, items: list[tuple[int, str]], path: tuple[str, ...]
    ) -> list[_Entry]:
        """The entries that go below a drawn row line. `path` is the rows
        above it."""
        to = entry.to
        if to is None:
            return []
        below: list[_Entry] = []
        target = to.get("screen_id")
        if isinstance(target, str):
            if self.popup(target):
                self.mark_popup(items, target)
            elif target not in self.expanded:
                below = self.screen_below(target, entry.name, (*path, entry.name))
                self.tag("opens", target)
            elif "screen_above" in NOTES_IN_TEXT:
                self.mark_see(items, target)
        again: list[_Entry] = []
        for e in entry.more:
            kind = _kind_of(e)
            if kind == "screen":
                again.append(_Entry("(pressed again)", e, again=True, name=entry.name))
            elif kind in NOTES_IN_TEXT and (note := _note(e, kind)) is not None:
                self.tally.marks.update(
                    {"again", "arrow"} if kind == "other_app" else {"again"}
                )
                again.append(_Entry("(pressed again)" + note))
        return [*below, *again]

    def screen_below(self, sid: str, name: str, path: tuple[str, ...]) -> list[_Entry]:
        """The entries of a screen a row led to; a marker when it has none."""
        entries = self.open_screen(sid, name, path)
        if not entries:
            self.tally.marks.add("empty")
            entries = [_Entry("(screen with no named rows)")]
        return entries

    def run(self, sid: str, name: str, path: tuple[str, ...] = ()) -> None:
        """List the screen `sid` and everything first reached below it."""
        items: list[tuple[int, str]] = []
        self.nodes = self.sections[-1].nodes
        self.sections[-1].sid = sid
        frames = [(self.open_screen(sid, name, path), 0, 0, path)]
        while frames:
            entries, index, depth, here = frames.pop()
            if index >= len(entries):
                continue
            frames.append((entries, index + 1, depth, here))
            entry = entries[index]
            if entry.again:
                assert entry.to is not None  # noqa: S101
                target = entry.to["screen_id"]
                assert isinstance(target, str)  # noqa: S101
                self.tally.marks.add("again")
                items.append((depth, entry.text))
                self.track(depth, entry)
                if self.popup(target):
                    self.mark_popup(items, target)
                    continue
                if target in self.expanded:
                    if "screen_above" in NOTES_IN_TEXT:
                        self.mark_see(items, target)
                    continue
                below = self.screen_below(target, entry.name, here)
                self.tag("opens", target)
                child = here
            else:
                items.append((depth, entry.text))
                self.track(depth, entry)
                below = self.below_of(entry, items, here)
                child = (*here, entry.name)
            if below:
                frames.append((below, 0, depth + 1, child))
        self.lines.extend(_draw(items))


def _drawn(tree: RunTree, *, ids: bool, why: bool, html: bool = False) -> _Renderer:
    """Draw the tree: everything above the legend."""
    label = tree.label if tree.label is not None else tree.package
    first = f"{_printable(label)} ({_printable(tree.package)})"
    if tree.also_target:
        listed = ", ".join(_printable(name) for name in tree.also_target)
        first += f"  also target: {listed}"
    if not tree.record_text:
        first += f"  {_NO_TEXT}"
    drawn = _Renderer(tree, ids=ids, why=why, html=html)
    drawn.lines.append(first)
    drawn.sections.append(_Section(None, first))
    if tree.root is not None:
        drawn.run(tree.root, _FIRST_SCREEN)
    # Screens no tap led to come first; then any that only taps among
    # themselves led to (a loop that the root does not reach).
    rest = [
        sid
        for sid, node in tree.screens.items()
        if (node.package == tree.package or node.package in tree.also_target)
        and sid not in tree.unreached
    ]
    for sid in (*tree.unreached, *rest):
        if sid not in drawn.expanded and sid not in drawn.hidden:
            heading = _orphan_heading(tree, sid)
            if drawn.popup(sid):  # the rows of a popup are never drawn
                drawn.lines.append(heading + POPUP_MARK)
                drawn.sections.append(_Section(sid, heading, popup=True))
                continue
            drawn.lines.append(heading)
            drawn.sections.append(_Section(sid, heading))
            name = f"screen {_printable(sid)}"
            drawn.run(sid, name, (name,))
    return drawn


_LEGENDS: Final = (
    ("dot", LEGEND),
    *((f"kind:{reason}", text) for reason, _, text in KIND_LABELS),
    ("no_change", LEGEND_NO_CHANGE),
    ("desc", LEGEND_DESC),
    ("arrow", LEGEND_ARROW),
    ("see", LEGEND_SEE),
    ("popup", LEGEND_POPUP),
    ("again", LEGEND_AGAIN),
    ("empty", LEGEND_EMPTY),
)


def render_text(tree: RunTree, *, ids: bool = False, why: bool = False) -> list[str]:
    """The lines of the text tree."""
    drawn = _drawn(tree, ids=ids, why=why)
    for mark, text in _LEGENDS:
        if mark in drawn.tally.marks:
            drawn.lines.append(text)
    summary = _summary(drawn.tally)
    if summary is not None:
        drawn.lines.append(summary)
    return [drawn.seen_as(line) for line in drawn.lines]


class _HtmlBuilder:
    """Turns what `_Renderer` drew into the model of the HTML page."""

    def __init__(self, drawn: _Renderer) -> None:
        self.drawn = drawn
        self.used: set[str] = set()
        self.anchors: dict[str, str] = {}
        self.popups: dict[str, HFold] = {}

    def anchor(self, sid: str) -> str:
        if sid not in self.anchors:
            self.anchors[sid] = anchor_id(sid, self.used)
        return self.anchors[sid]

    def other_fold(self, others: _Others | None) -> HFold | None:
        if others is None or not others.lines:
            return None
        return HFold(f"{FOLD_OTHER} ({len(others.lines)})", list(others.lines))

    def popup_fold(self, sid: str) -> HFold:
        """The rows of a popup screen, flat, then what it leaves out. Its marks
        are kept for the legend; nothing here changes the counts."""
        if sid not in self.popups:
            drawn = self.drawn
            scratch = _Tally()
            others = _Others()
            entries = _entries(
                drawn.tree, sid, ids=False, why=drawn.why, tally=scratch, others=others
            )
            drawn.html_marks |= scratch.marks - {"popup"}
            children: list[HRow | HFold | str] = [self.retried(e) for e in entries]
            fold = self.other_fold(others)
            if fold is not None:
                children.append(fold)
            self.popups[sid] = HFold(f"{FOLD_POPUP} ({len(entries)})", children)
        return self.popups[sid]

    def retried(self, entry: _Entry) -> HRow:
        """A row of a popup fold with its further results, in the forms the
        text tree uses below a row: `(pressed again)`, with `→ PACKAGE` for
        another app. A further result of another kind is a line of the
        `Other items` fold."""
        row = self.row(entry)
        for ending in entry.more:
            kind = _kind_of(ending)
            note = _note(ending, kind) if kind != "screen" else None
            if kind == "screen" or (kind in NOTES_IN_TEXT and note is not None):
                self.drawn.html_marks.add("again")
                if kind == "other_app":
                    self.drawn.html_marks.add("arrow")
                row.children.append(HRow("(pressed again)", note or ""))
        return row

    def row(self, entry: _Entry) -> HRow:
        if entry.label is None:
            return HRow(entry.text)
        return HRow(
            entry.label,
            entry.note,
            entry.dot,
            entry.reasons,
            entry.kinds,
            gap=entry.gap,
        )

    def forest(self, section: _Section) -> list[HRow | HFold]:
        """The rows of a section, with the folds that hang on them."""
        roots: list[HRow | HFold] = []
        stack: list[tuple[int, HRow, str | None]] = []

        def finish(opens: str | None, row: HRow | None) -> None:
            fold = self.other_fold(self.drawn.others.get(opens)) if opens else None
            if fold is not None:
                (roots if row is None else row.children).append(fold)

        for node in section.nodes:
            while stack and stack[-1][0] >= node.depth:
                _, done, opens = stack.pop()
                finish(opens, done)
            row = self.row(node.entry)
            if node.opens is not None:
                row.anchor = self.anchor(node.opens)
            if node.see is not None:
                row.see = (self.anchor(node.see), self.drawn.seen_name(node.see))
            if node.popup is not None:
                row.popup = True
                row.children.append(self.popup_fold(node.popup))
            (stack[-1][1].children if stack else roots).append(row)
            stack.append((node.depth, row, node.opens))
        while stack:
            _, done, opens = stack.pop()
            finish(opens, done)
        if section.sid is not None:
            finish(section.sid, None)
        return roots

    def section(self, section: _Section) -> HSection:
        built = HSection(section.heading, popup=section.popup)
        if section.sid is not None and not section.popup:
            built.anchor = self.anchor(section.sid)
        built.children = self.forest(section)
        if section.popup and section.sid is not None:
            built.children.append(self.popup_fold(section.sid))
        return built


def render_html(tree: RunTree, *, ids: bool = False, why: bool = False) -> str:
    """The HTML page of the tree: the rows of the text tree, collapsible, with
    the rows of each popup and what the text tree leaves out in closed folds."""
    drawn = _drawn(tree, ids=ids, why=why, html=True)
    builder = _HtmlBuilder(drawn)
    sections = [builder.section(s) for s in drawn.sections]
    marks = drawn.tally.marks | drawn.html_marks
    page = HPage(
        package=_printable(tree.package),
        head=sections[0],
        orphans=sections[1:],
        legend=[
            LEGEND_POPUP_HTML if mark == "popup" else text
            for mark, text in _LEGENDS
            if mark in marks
        ],
        summary=_summary(drawn.tally, _SUMMARY_HTML),
    )
    return render_page(page)


def _html_refusal(out: str, error: Exception) -> _UsageError:
    shown = _printable(out)
    if isinstance(error, FileExistsError):
        return _UsageError(f"--html {shown}: already exists; give a new file name")
    if isinstance(error, FileNotFoundError):
        return _UsageError(f"--html {shown}: the folder does not exist")
    detail = error.strerror if isinstance(error, OSError) else str(error)
    return _UsageError(f"--html {shown}: cannot be created: {_printable(detail or '')}")


def _check_html_target(out: str, run_dir: str) -> None:
    """Refuse an output path `save_html` would refuse, and one inside the run
    folder (both resolved), before the run folder is read."""
    try:
        check_html_path(out)
        inside = Path(out).resolve().is_relative_to(Path(run_dir).resolve())
    except (OSError, ValueError) as error:
        raise _html_refusal(out, error) from None
    if inside:
        shown = _printable(out)
        msg = f"--html {shown}: is inside the run folder; give a path outside it"
        raise _UsageError(msg)


def _save_html(out: str, page: str) -> None:
    try:
        save_html(out, page)
    except (OSError, ValueError) as error:
        if isinstance(error, HtmlFileError):
            raise
        raise _html_refusal(out, error) from None


def _cmd_tree(args: argparse.Namespace) -> int:
    if (
        args.serial is not None
        or args.adb is not None
        or args.adb_server is not None
        or not args.start_server
    ):
        msg = (
            "tree takes no device options (-s, --adb, --adb-server, --no-start-server)"
        )
        raise _UsageError(msg)
    path = str.__str__(args.run_dir)
    out = args.html
    if out is not None:
        if args.json:
            msg = "--html cannot be combined with --json"
            raise _UsageError(msg)
        _check_html_target(out, path)
    try:
        tree = read_tree(path)
    except TreeError as exc:
        msg = f"{_shown(path)}: {exc}"
        raise _UsageError(msg) from None
    if out is not None:
        for warning in tree.warnings:
            _emit(f"uiprobe: warning: {_printable(warning)}", stream=sys.stderr)
        _save_html(out, render_html(tree, ids=args.ids, why=args.why))
        _emit(_printable(out))
        return 0
    if args.json:
        _emit(_json_text(tree))
        return 0
    for warning in tree.warnings:
        _emit(f"uiprobe: warning: {_printable(warning)}", stream=sys.stderr)
    for line in render_text(tree, ids=args.ids, why=args.why):
        _emit(line)
    return 0
