"""The tree command: show a crawl run folder as a tree. It reads files only."""

from __future__ import annotations

import argparse  # noqa: TC003 (a runtime import in the former cli.py)
import sys
from dataclasses import dataclass
from typing import Final

from uiprobe._json import dumps
from uiprobe.cli.output import _emit, _printable
from uiprobe.cli.parser import _UsageError
from uiprobe.crawl_tree import (
    Row,
    RunTree,
    TreeError,
    read_tree,
    tree_to_dict,
)
from uiprobe.settings import _shown

LEGEND: Final = "· = 押していないので、その下は見ていない行"
_NO_TEXT: Final = "このランでは画面の文字を記録していません (record_text = false)"
_FIRST_SCREEN: Final = "最初の画面"


@dataclass(slots=True)
class _Entry:
    text: str
    row: Row | None = None


def _heading_text(tree: RunTree, sid: str) -> str:
    node = tree.screens[sid]
    activity = _printable(node.activity) if node.activity else "-"
    return f"(画面 {_printable(sid)}  {activity})"


def _entries(tree: RunTree, sid: str, *, ids: bool, why: bool) -> list[_Entry]:
    node = tree.screens[sid]
    entries: list[_Entry] = []
    if ids:
        entries.append(_Entry(_heading_text(tree, sid)))
    for row in node.rows:
        name = _printable(row.name)
        if row.kind == "heading":
            entries.append(_Entry(f"[見出し] {name}"))
        elif row.to is None:
            line = f"{name} ·"
            if why and row.reasons:
                line += " [" + ", ".join(_printable(r) for r in row.reasons) + "]"
            entries.append(_Entry(line))
        else:
            entries.append(_Entry(name, row))
    if node.unmatched:
        entries.append(_Entry(f"({len(node.unmatched)} 件は対応する行が見つからない)"))
    return entries


def _ending(row: Row, *, ids: bool) -> str | None:
    """The text after a pressed row's name, or None when a screen goes below."""
    to = row.to or {}
    if "same_screen" in to:
        return " → (同じ画面)"
    if "other_app" in to:
        package = to["other_app"]
        suffix = f" ({_printable(package)})" if ids and isinstance(package, str) else ""
        return " → 別のアプリ" + suffix
    if "not_sent" in to:
        reason = to["not_sent"]
        shown = _printable(reason) if isinstance(reason, str) else ""
        return f" → (送らなかった: {shown})"
    if "screen_id" in to:
        return None
    return " → (読めなかった)"


def _orphan_heading(tree: RunTree, sid: str) -> str:
    """The line above a screen that no pressed row led to (it was reached by a
    BACK or a relaunch, or is another reading of a screen that changes)."""
    node = tree.screens[sid]
    activity = _printable(node.activity) if node.activity else "-"
    return f"(押した行からは辿れなかった画面 {_printable(sid)}  {activity})"


def render_text(tree: RunTree, *, ids: bool = False, why: bool = False) -> list[str]:
    """The lines of the text tree."""
    label = tree.label if tree.label is not None else tree.package
    first = f"{_printable(label)} ({_printable(tree.package)})"
    if not tree.record_text:
        first += f"  {_NO_TEXT}"
    lines = [first]
    expanded: dict[str, str] = {}
    entry_stack: list[tuple[list[_Entry], int, str]] = []

    def start(sid: str, prefix: str, name: str) -> None:
        expanded[sid] = name
        entry_stack.append((_entries(tree, sid, ids=ids, why=why), 0, prefix))

    def run_frames() -> None:
        while entry_stack:
            entries, index, prefix = entry_stack.pop()
            if index >= len(entries):
                continue
            entry_stack.append((entries, index + 1, prefix))
            entry = entries[index]
            last = index == len(entries) - 1
            lines.append(prefix + ("└─ " if last else "├─ ") + entry.text)
            row = entry.row
            if row is None:
                continue
            ending = _ending(row, ids=ids)
            if ending is not None:
                lines[-1] += ending
                continue
            target = (row.to or {}).get("screen_id")
            if not isinstance(target, str):
                continue
            if target in expanded:
                lines[-1] += f" → (上に出た画面: {expanded[target]})"
                if ids:
                    lines[-1] += f" {_printable(target)}"
            else:
                start(target, prefix + ("   " if last else "│  "), _printable(row.name))

    if tree.root is not None:
        start(tree.root, "", _FIRST_SCREEN)
        run_frames()
    for sid in tree.unreached:
        if sid not in expanded:
            lines.append(_orphan_heading(tree, sid))
            start(sid, "", _FIRST_SCREEN)
            run_frames()
    lines.append(LEGEND)
    return lines


def _cmd_tree(args: argparse.Namespace) -> int:
    path = str.__str__(args.run_dir)
    try:
        tree = read_tree(path)
    except TreeError as exc:
        msg = f"{_shown(path)}: {exc}"
        raise _UsageError(msg) from None
    if args.json:
        _emit(dumps(tree_to_dict(tree)))
        return 0
    for warning in tree.warnings:
        _emit(f"uiprobe: warning: {_printable(warning)}", stream=sys.stderr)
    for line in render_text(tree, ids=args.ids, why=args.why):
        _emit(line)
    return 0
