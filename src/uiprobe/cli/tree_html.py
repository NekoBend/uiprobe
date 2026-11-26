"""The crawl tree as one self-contained HTML page: collapsible rows, no script.

`cli/tree.py` builds the model below from the same walk that draws the text
tree; this module only turns the model into text. Every string in the model
is already made safe for display (control and format characters shown as
escapes, as in the text tree); here each one also goes through
`html.escape(s, quote=True)`. The only attribute values that are not escaped
text are the ids, which `anchor_id` builds from uiprobe's own screen ids and
reduces to [A-Za-z0-9_-].
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

TITLE_PREFIX: Final = "uiprobe tree"
FOLD_OTHER: Final = "Other items on this screen"
FOLD_POPUP: Final = "Rows inside this popup"
CSP: Final = "default-src 'none'; style-src 'unsafe-inline'"
_ID_CHARS: Final = re.compile(r"[^A-Za-z0-9_-]")

_CSS: Final = """\
:root{color-scheme:light dark}
body{margin:0;padding:12px 16px;font:15px/1.5 system-ui,sans-serif}
main{max-width:70em;margin:0 auto}
h1{font-size:1.15em;margin:0 0 .6em;overflow-wrap:anywhere}
h2{font-size:1em;margin:1.2em 0 .4em;overflow-wrap:anywhere}
ul{list-style:none;margin:0;padding-left:1.1em}
ul.tree{padding-left:0}
li{margin:.1em 0}
.line,.item{font-family:ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
summary{cursor:pointer}
.unpressed{color:#6b7280}
@media (prefers-color-scheme:dark){.unpressed{color:#9ca3af}}
.fold>summary{font-style:italic}
footer{margin-top:1.5em;border-top:1px solid #8884;padding-top:.6em}
footer ul{padding-left:0}
.summary{font-weight:600;overflow-wrap:anywhere}
:target>.line,:target>details>summary>.line,:target>.head{background:#fd04}
"""


@dataclass(slots=True)
class HRow:
    """One line of the tree. `children` hold rows and folds."""

    label: str
    note: str = ""
    dot: bool = False
    reasons: str = ""
    kinds: str = ""  # shown in place of the dot
    popup: bool = False
    see: tuple[str, str] | None = None  # (anchor id, name shown)
    anchor: str | None = None
    children: list[HRow | HFold] = field(default_factory=list)


@dataclass(slots=True)
class HFold:
    """A closed fold: rows, or plain text lines."""

    label: str
    children: list[HRow | HFold | str] = field(default_factory=list)


@dataclass(slots=True)
class HSection:
    """A heading line and the tree below it."""

    heading: str
    anchor: str | None = None
    popup: bool = False
    children: list[HRow | HFold] = field(default_factory=list)


@dataclass(slots=True)
class HPage:
    package: str
    head: HSection
    orphans: list[HSection]
    legend: list[str]
    summary: str | None


def anchor_id(screen_id: str, used: set[str]) -> str:
    """An id for a screen id: [A-Za-z0-9_-] only, never used twice."""
    base = "screen-" + _ID_CHARS.sub("_", screen_id)
    found = base
    number = 1
    while found in used:
        number += 1
        found = f"{base}-{number}"
    used.add(found)
    return found


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _line(row: HRow) -> str:
    parts = [f'<span class="name">{_esc(row.label)}</span>']
    if row.note:
        parts.append(_esc(row.note))
    if row.kinds:
        parts.append(_esc(row.kinds))
    elif row.dot:
        parts.append(" ·")
    if row.reasons:
        parts.append(_esc(row.reasons))
    if row.popup:
        parts.append(" [popup]")
    if row.see is not None:
        target, name = row.see
        parts.append(f' → see: <a href="#{target}">{_esc(name)}</a>')
    css = "line" if row.dot is False else "line unpressed"
    return f'<span class="{css}">{"".join(parts)}</span>'


def _items(children: Sequence[HRow | HFold | str], out: list[str]) -> None:
    """Write `<ul>` of `children`. The rows of the first level that have
    something below them are open; every fold and every deeper row is closed.
    Walks with a stack: a run folder can be deeper than the recursion limit."""
    out.append('<ul class="tree">')
    stack = [(iter(children), 0, "</ul>")]
    while stack:
        iterator, level, close = stack[-1]
        child = next(iterator, None)
        if child is None:
            out.append(close)
            stack.pop()
            continue
        if isinstance(child, str):
            out.append(f'<li class="item">{_esc(child)}</li>')
            continue
        if isinstance(child, HFold):
            summary = f'<span class="head">{_esc(child.label)}</span>'
            item, kind, opened, nested = "<li>", ' class="fold"', "", level
        else:
            summary = _line(child)
            item = f'<li id="{child.anchor}">' if child.anchor else "<li>"
            kind, opened, nested = "", " open" if level == 0 else "", level + 1
        if not child.children:
            out.append(f"{item}{summary}</li>")
            continue
        out.append(f"{item}<details{kind}{opened}><summary>{summary}</summary><ul>")
        stack.append((iter(child.children), nested, "</ul></details></li>"))


def _section(section: HSection, *, first: bool) -> list[str]:
    out: list[str] = []
    tag = "h1" if first else "h2"
    attr = f' id="{section.anchor}"' if section.anchor else ""
    mark = " [popup]" if section.popup else ""
    out.append(
        f'<{tag}{attr}><span class="line">{_esc(section.heading + mark)}</span></{tag}>'
    )
    if section.children:
        _items(section.children, out)
    return out


def render_page(page: HPage) -> str:
    """The whole page as text."""
    title = f"{TITLE_PREFIX}: {page.package}"
    out = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title>",
        f"<style>\n{_CSS}</style>",
        "</head>",
        "<body>",
        "<main>",
        *_section(page.head, first=True),
    ]
    for orphan in page.orphans:
        out.extend(_section(orphan, first=False))
    out.append("<footer>")
    if page.legend:
        out.append('<ul class="legend">')
        out.extend(f'<li class="item">{_esc(text)}</li>' for text in page.legend)
        out.append("</ul>")
    if page.summary is not None:
        out.append(f'<p class="summary">{_esc(page.summary)}</p>')
    out.extend(["</footer>", "</main>", "</body>", "</html>", ""])
    return "\n".join(out)
