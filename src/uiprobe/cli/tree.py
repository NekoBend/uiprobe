"""The tree command: show a crawl run folder as a tree. It reads files only."""

from __future__ import annotations

import argparse  # noqa: TC003 (a runtime import in the former cli.py)
import re
import sys
from dataclasses import dataclass
from typing import Final

from uiprobe._json import dumps
from uiprobe.cli.output import _emit, _printable
from uiprobe.cli.parser import _UsageError
from uiprobe.crawl_tree import (
    RunTree,
    TreeError,
    read_tree,
    tree_to_dict,
)
from uiprobe.settings import _shown

LEGEND: Final = "· = not pressed, so nothing below it was looked at"
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


@dataclass(slots=True)
class _Entry:
    text: str
    to: dict[str, object] | None = None
    name: str = ""
    again: bool = False  # a further sent tap on the same row
    more: tuple[dict[str, object], ...] = ()


def _heading_text(tree: RunTree, sid: str) -> str:
    node = tree.screens[sid]
    activity = _printable(node.activity) if node.activity else "-"
    return f"(screen {_printable(sid)}  {activity})"


def _entries(tree: RunTree, sid: str, *, ids: bool, why: bool) -> list[_Entry]:
    node = tree.screens[sid]
    entries: list[_Entry] = []
    if ids:
        entries.append(_Entry(_heading_text(tree, sid)))
    for row in node.rows:
        name = _label(row.name)
        if row.kind == "text":
            entries.append(_Entry(f"[text] {name}"))
        elif row.to is None:
            line = f"{name} ·"
            if why and row.reasons:
                line += " [" + ", ".join(_printable(r) for r in row.reasons) + "]"
            entries.append(_Entry(line))
        else:
            entries.append(_Entry(name, row.to, name, more=tuple(row.also_to)))
    if node.unmatched and node.unmatched_why:
        count = len(node.unmatched)
        entries.append(_Entry(f"({count} not matched to rows: too many elements)"))
    elif node.unmatched:
        entries.append(_Entry(f"({len(node.unmatched)} with no matching row)"))
    return entries


def _ending(to: dict[str, object], *, ids: bool) -> str | None:
    """The text after a pressed row's name, or None when a screen goes below."""
    if "same_screen" in to:
        return " → (same screen)"
    if "other_app" in to:
        package = to["other_app"]
        suffix = f" ({_printable(package)})" if ids and isinstance(package, str) else ""
        return " → another app" + suffix
    if "not_sent" in to:
        reason = to["not_sent"]
        shown = _printable(reason) if isinstance(reason, str) else ""
        return f" → (not sent: {shown})"
    if "unknown" in to:
        return " → (result unknown)"
    if "screen_id" in to:
        return None
    return " → (could not be read)"


def _orphan_heading(tree: RunTree, sid: str) -> str:
    """The line above a screen that no pressed row led to (it was reached by a
    BACK or a relaunch, or is another reading of a screen that changes)."""
    node = tree.screens[sid]
    activity = _printable(node.activity) if node.activity else "-"
    return f"(screen not reached from a pressed row {_printable(sid)}  {activity})"


class _Renderer:
    """Draws the tree: each screen is listed once, where it is first reached."""

    def __init__(self, tree: RunTree, *, ids: bool, why: bool) -> None:
        self.tree = tree
        self.ids = ids
        self.why = why
        self.lines: list[str] = []
        self.expanded: dict[str, str] = {}
        self.frames: list[tuple[list[_Entry], int, str]] = []

    def open_screen(self, sid: str, name: str) -> list[_Entry]:
        self.expanded[sid] = name
        return _entries(self.tree, sid, ids=self.ids, why=self.why)

    def children_of(self, entry: _Entry) -> list[_Entry]:
        """Finish the entry's line; return the lines that go below it."""
        to = entry.to
        if to is None:
            return []
        extras = [
            _Entry("(pressed again", e, entry.name, again=True) for e in entry.more
        ]
        ending = _ending(to, ids=self.ids)
        below: list[_Entry] = []
        if ending is None:
            target = to["screen_id"]
            assert isinstance(target, str)  # noqa: S101
            if target in self.expanded:
                ending = f" → (screen shown above: {self.expanded[target]})"
                if self.ids:
                    ending += f" {_printable(target)}"
            else:
                below = self.open_screen(target, entry.name)
                ending = " → next screen" if entry.again else ""
        self.lines[-1] += ending + (")" if entry.again else "")
        return [*below, *extras]

    def run(self, sid: str) -> None:
        """List the screen `sid` and everything first reached below it."""
        self.frames.append((self.open_screen(sid, _FIRST_SCREEN), 0, ""))
        while self.frames:
            entries, index, prefix = self.frames.pop()
            if index >= len(entries):
                continue
            self.frames.append((entries, index + 1, prefix))
            entry = entries[index]
            last = index == len(entries) - 1
            self.lines.append(prefix + ("└─ " if last else "├─ ") + entry.text)
            below = self.children_of(entry)
            if below:
                self.frames.append((below, 0, prefix + ("   " if last else "│  ")))


def render_text(tree: RunTree, *, ids: bool = False, why: bool = False) -> list[str]:
    """The lines of the text tree."""
    label = tree.label if tree.label is not None else tree.package
    first = f"{_printable(label)} ({_printable(tree.package)})"
    if not tree.record_text:
        first += f"  {_NO_TEXT}"
    drawn = _Renderer(tree, ids=ids, why=why)
    drawn.lines.append(first)
    if tree.root is not None:
        drawn.run(tree.root)
    # Screens no tap led to come first; then any that only taps among
    # themselves led to (a loop that the root does not reach).
    rest = [
        sid
        for sid, node in tree.screens.items()
        if node.package == tree.package and sid not in tree.unreached
    ]
    for sid in (*tree.unreached, *rest):
        if sid not in drawn.expanded:
            drawn.lines.append(_orphan_heading(tree, sid))
            drawn.run(sid)
    drawn.lines.append(LEGEND)
    return drawn.lines


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
    try:
        tree = read_tree(path)
    except TreeError as exc:
        msg = f"{_shown(path)}: {exc}"
        raise _UsageError(msg) from None
    if args.json:
        _emit(_json_text(tree))
        return 0
    for warning in tree.warnings:
        _emit(f"uiprobe: warning: {_printable(warning)}", stream=sys.stderr)
    for line in render_text(tree, ids=args.ids, why=args.why):
        _emit(line)
    return 0
