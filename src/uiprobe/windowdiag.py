"""Why the tap window list of a device cannot be read, told without its values.

`diagnose_tap_window` runs the same checks as the crawl's tap window check
(`uiprobe.inputwindows.explain_input_windows`) on the whole text of
``dumpsys input`` and reports the result so that it can be shared: whether
the list reads or which rule refused it and at which line, how many window
rows each list has, and the shape of the lines around the failure.

No text of the output is copied into the report, by construction: a line is
shown in detail only when the reader itself accepted its structure, and then
only through the reader's own boundaries and regular expressions:

* a window line the reader read into a window, cut by
  `uiprobe.inputwindows.window_line_fields` (the name as its length, each
  field after it as a known key with a type tag, a known value word or a
  length; a quoted value as one length);
* a ``Viewport`` line the reader read for the orientation of the display
  asked for (`Verdict.viewports`), shown part by part with the orientation
  it read (quoted values as one length);
* the headings and lines the reader matches by its own patterns: its
  header, ``Windows:``, ``Display: N``, the lines a list ends at,
  ``logicalSize=``, transform lines and their matrix rows.

Every other line, a window line the reader refused or did not reach and
any other Viewport-shaped line included, is shown as its kind and one
``<text:LEN>``. No number of the output is shown but the row number of a
row the reader read (it is the row's place, which the reader checked), the
orientation of a Viewport line it read, and a display number written ``0``
when display 0 was asked for; every other number is shown as its width
(``<num:W>``, a minus sign kept). Counts, lengths and indents stop at
`CAP`. So every string is a constant, a member of a fixed vocabulary of this
module, a tag or such a number. Text an app sets that holds line breaks can
still make lines the reader matches by its patterns (a transform line, a
heading); the words of this module's vocabulary in them are then shown as
such (``SCALE``), never other text and never their numbers.

The one exception is opt-in: with ``show_flag_names`` an unknown name in the
``flags=``, ``inputConfig=`` or ``inputFeatures=`` field of a row of a found
window list whose name's end can be told is shown when it is an upper-case
word (`SHOWABLE_FLAG`), as ``<unknown_flag:NAME>``, also in a row the reader
refused. Any device text, a window name included, may end up there; the
report then says so (`NOTICE_SHOWN`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from uiprobe.inputwindows import (
    _DISPLAY,
    _FLAG_NAMES,
    _HEADER,
    _INDEX,
    _INPUT_CONFIG_NAMES,
    _LIST_END,
    _MATRIX_ROW,
    _TRANSFORM,
    _VIEWPORT,
    RULES,
    RowFields,
    Verdict,
    explain_input_windows,
    window_line_fields,
)

__all__ = [
    "CAP",
    "COMMAND",
    "NOTICE",
    "NOTICE_SHOWN",
    "SCHEMA",
    "Form",
    "LineShape",
    "ListRows",
    "TapWindowDiagnosis",
    "diagnose_tap_window",
    "shape_line",
]

SCHEMA: Final = "uiprobe.diagnose_tap_window/1"
COMMAND: Final = "dumpsys input"
NOTICE: Final = (
    "this output contains no device serial, app or window names, coordinates,"
    " accounts or network values"
)
NOTICE_SHOWN: Final = (
    "this output contains no device serial, app or window names, coordinates,"
    " accounts or network values, except that --show-flag-names prints"
    " unknown upper-case flag names verbatim from the device: any device text,"
    " numbers included, and a window name with or without a line break, may"
    " appear as such a name, so this output may include a serial"
)
DISPATCHER: Final = "dispatcher"
INPUT_READER: Final = "input_reader"
# The whole output, numbered from its top: without exactly one dispatcher
# header, or for a line after the dispatcher's part.
OUTPUT: Final = "output"
# How many lines before and after the failing one are shown.
_CONTEXT: Final = 2
# The most lines an outline (when no one line failed) and a forms summary show.
_OUTLINE_LINES: Final = 40
_MAX_FORMS: Final = 20
# The largest count, length or indent shown, and the most digits a verified
# row or display number is shown with.
CAP: Final = 9999
_SMALL_DIGITS: Final = 4

LINE_KINDS: Final = (
    "window_row",
    "transform",
    "matrix_row",
    "heading",
    "viewport",
    "other",
    "blank",
)
# The field names of the window lines the captures hold (API 24 to 36 and
# the Samsung form).
KNOWN_KEYS: Final = frozenset(
    {
        "1HXY",
        "alpha",
        "applicationInfo.name",
        "applicationInfo.token",
        "canReceiveKeys",
        "dispatchingTimeout",
        "displayId",
        "displayOrientation",
        "flags",
        "focusable",
        "frame",
        "globalScale",
        "hasFocus",
        "hasToken",
        "hasWallpaper",
        "id",
        "inputConfig",
        "inputFeatures",
        "layer",
        "name",
        "ownerPid",
        "ownerUid",
        "pTouchableRegion",
        "paused",
        "portalToDisplayId",
        "samsungFlags",
        "scale",
        "token",
        "touchOcclusionMode",
        "touchableRegion",
        "trustedOverlay",
        "type",
        "visible",
        "windowScale",
    }
)
# Values of those fields shown as they are (in rows the reader read).
KNOWN_VALUES: Final = frozenset(
    {
        "<empty>",
        "<null>",
        "ALLOW",
        "BLOCK_UNTRUSTED",
        "USE_OPACITY",
        "false",
        "true",
    }
)
# The names of ``inputFeatures=`` seen in the captures (API 31 and 32 print
# names); the reader does not read the field.
INPUT_FEATURE_NAMES: Final = frozenset({"NO_INPUT_CHANNEL"})
# The fields whose value is a list of flag names (``A | B``), each with the
# names known in that field, so a name of one field in another is unknown.
FLAG_VOCABULARIES: Final = {
    "flags": _FLAG_NAMES,
    "inputConfig": _INPUT_CONFIG_NAMES,
    "inputFeatures": INPUT_FEATURE_NAMES,
    "samsungFlags": frozenset({"FORCE_TRUSTED_OVERLAY"}),
}
KNOWN_FLAGS: Final = frozenset().union(*FLAG_VOCABULARIES.values())
# Fields whose value is text an app sets (or a window's title): always shown
# as its length, never as a known word.
TEXT_KEYS: Final = frozenset({"name", "applicationInfo.name"})
# The words of a ``transform (ROT_<n>) (<kind>)`` line.
TRANSFORM_WORDS: Final = frozenset(
    {
        "IDENTITY",
        "ROTATE",
        "ROT_0",
        "ROT_180",
        "ROT_270",
        "ROT_90",
        "SCALE",
        "TRANSLATE",
        "UNKNOWN",
    }
)
# The kinds of ``Viewport <KIND>:`` lines shown as they are.
VIEWPORT_KINDS: Final = frozenset({"INTERNAL", "EXTERNAL", "VIRTUAL"})
# The headings shown as they are (the reader's own lines; `_LIST_END`).
HEADINGS: Final = frozenset(
    {_HEADER, "Windows:", "Display:", "MonitoringChannels:", "Connections:"}
)
# An unknown flag name shown with show_flag_names.
SHOWABLE_FLAG: Final = re.compile(r"[A-Z][A-Z0-9_]{0,47}")
_OBSCURING: Final = "mMaximumObscuringOpacityForTouch: "
_MONITORS = re.compile(r"Global monitors (in|on) display -?(\d+):")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_DECIMAL = r"-?\d+\.\d+"
_NUMBER_LIST = re.compile(r"\[(-?\d+(?:\.\d+)?(?:, ?-?\d+(?:\.\d+)?)*)\]")
_WORD = re.compile(r"[A-Z][A-Z0-9_]*")
# (pattern, tag) for values of one simple type, tried in order.
_SIMPLE: Final = (
    (re.compile(r"-?\d+"), "<num>"),
    (re.compile(_DECIMAL), "<dec>"),
    (_HEX, "<hex>"),
    (re.compile(r"\[-?\d+,-?\d+\]\[-?\d+,-?\d+\]"), "<rect>"),
    (re.compile(r"\d+x\d+"), "<size>"),
    (re.compile(r"-?\d+(?:\.\d+)?ms"), "<ms>"),
)
# The most hex digits of a flags=0x... value the reader reads.
_FLAGS_HEX: Final = len("0x") + 8


@dataclass(frozen=True, slots=True)
class LineShape:
    """The shape of one line: its number (from 1, in its part), kind (one of
    `LINE_KINDS`), indent, length, tokens, and whether it is the line the
    check refused at."""

    number: int
    kind: str
    indent: int
    length: int
    tokens: tuple[str, ...]
    failing: bool = False


@dataclass(frozen=True, slots=True)
class ListRows:
    """One window list: its place among the lists (from 1), whether it is the
    list of one ``Display:`` section (``per_display``; False for the one list
    of every display, API 24 to 28), that display's number (only 0, when
    display 0 was asked for, see the module; else None), how many digits the
    number has, how many window rows the list has, whether the check
    refused in it and whether the number has a minus sign."""

    position: int
    per_display: bool
    display: int | None
    display_digits: int
    rows: int
    failed: bool
    negative: bool = False


@dataclass(frozen=True, slots=True)
class Form:
    """Rows of one list that look alike: whether the name is quoted, which
    fields follow it (`KNOWN_KEYS`, an unknown one as ``<unknown_key>``),
    whether transform lines follow the row, and how many rows. A row the
    reader did not read into a window, or whose name's end cannot be told,
    is of no recognised form (``recognised`` False, no fields)."""

    position: int
    quoted: bool
    fields: tuple[str, ...]
    transform: bool
    rows: int
    recognised: bool = True


@dataclass(frozen=True, slots=True)
class TapWindowDiagnosis:
    """What `diagnose_tap_window` found (see the module for what may be in
    it).

    ``reason`` and ``rule`` are None when the list reads; ``part`` is
    `DISPATCHER`, `INPUT_READER` (the Viewport lines above it) or `OUTPUT`
    (the whole output, when there is not exactly one dispatcher header or
    the line is after the dispatcher's part) and ``line`` the line number in
    it (from 1; the dispatcher's header is line 1 of `DISPATCHER`) where the
    check refused, both None when no one line is to blame.
    ``show_flag_names`` says whether unknown flag names may be in it (and so
    which notice it carries).
    """

    reason: str | None
    rule: str | None
    part: str | None
    line: int | None
    lists: tuple[ListRows, ...]
    lines: tuple[LineShape, ...]
    forms: tuple[Form, ...]
    show_flag_names: bool = False

    @property
    def readable(self) -> bool:
        """Whether the check would read the list (and go on to the point)."""
        return self.reason is None

    @property
    def notice(self) -> str:
        """What the report holds: `NOTICE`, or `NOTICE_SHOWN` with
        ``show_flag_names``."""
        return NOTICE_SHOWN if self.show_flag_names else NOTICE

    def to_dict(self) -> dict[str, object]:
        """Return the ``uiprobe.diagnose_tap_window/1`` JSON object."""
        return {
            "schema": SCHEMA,
            "notice": self.notice,
            "show_flag_names": self.show_flag_names,
            "command": COMMAND,
            "readable": self.readable,
            "reason": self.reason,
            "rule": self.rule,
            "part": self.part,
            "line": self.line,
            "lists": [
                {
                    "position": item.position,
                    "per_display": item.per_display,
                    "display": item.display,
                    "display_digits": _capped(item.display_digits),
                    "display_negative": item.negative,
                    "rows": _capped(item.rows),
                    "failed": item.failed,
                }
                for item in self.lists
            ],
            "lines": [
                {
                    "number": shape.number,
                    "failing": shape.failing,
                    "kind": shape.kind,
                    "indent": _capped(shape.indent),
                    "length": _capped(shape.length),
                    "tokens": list(shape.tokens),
                }
                for shape in self.lines
            ],
            "forms": [
                {
                    "position": form.position,
                    "rows": _capped(form.rows),
                    "recognised": form.recognised,
                    "quoted": form.quoted,
                    "transform": form.transform,
                    "fields": list(form.fields),
                }
                for form in self.forms
            ],
        }

    def to_text(self) -> str:
        """Return the report as lines of text, the notice first."""
        lines = [f"uiprobe diagnose tap-window: {self.notice}; it read: {COMMAND}"]
        if self.reason is None:
            lines.append("result: readable")
        else:
            where = (
                f" at line {self.line} of the {self.part} part"
                if self.line is not None
                else ""
            )
            lines.append(
                f"result: not readable: {self.reason}, rule {self.rule}{where}"
            )
        if self.lists:
            lines.append("lists:")
            lines.extend(f"  {_list_text(item)}" for item in self.lists)
        else:
            lines.append("lists: none found")
        if self.lines:
            lines.append("lines (number, kind, indent, length: tokens):")
            lines.extend(_shape_text(shape) for shape in self.lines)
        if self.forms:
            lines.append("forms:")
            lines.extend(f"  {_form_text(form)}" for form in self.forms)
        return "\n".join(lines)


def _count(number: int) -> str:
    """A count, length or indent as text: ``9999+`` past `CAP`."""
    return str(number) if number <= CAP else f"{CAP}+"


def _capped(number: int) -> int:
    """A count, length or indent as a JSON number: at most `CAP`."""
    return min(number, CAP)


def _list_text(item: ListRows) -> str:
    if not item.per_display:
        display = "every display"
    elif item.display is None:
        sign = "-" if item.negative else ""
        display = f"display {sign}<num:{_count(item.display_digits)}>"
    else:
        display = f"display {item.display}"
    failed = ", the check refused here" if item.failed else ""
    return f"list {item.position} ({display}): {_count(item.rows)} window rows{failed}"


def _shape_text(shape: LineShape) -> str:
    mark = ">" if shape.failing else " "
    tokens = " ".join(shape.tokens)
    head = f"{mark} {shape.number} {shape.kind} {_count(shape.indent)}"
    return f"{head} {_count(shape.length)}: {tokens}"


def _form_text(form: Form) -> str:
    transform = "transform lines" if form.transform else "no transform lines"
    if not form.recognised:
        return (
            f"list {form.position}: {_count(form.rows)} rows of no recognised form"
            f" (not read into a window, or the name's end cannot be told), {transform}"
        )
    name = "quoted name" if form.quoted else "bare name"
    return (
        f"list {form.position}: {_count(form.rows)} rows, {name}, {transform};"
        f" fields: {' '.join(form.fields)}"
    )


def _tag(name: str, number: int) -> str:
    """A tag of a constant `name` and a count, such as ``<text:12>``."""
    return f"<{name}:{_count(number)}>"


def _number(digits: str, *, verified: bool) -> str:
    """A row or display number from its digits (counted, never converted
    unless short): the number when `verified` and of at most four digits,
    else ``<num:DIGITS>``."""
    if verified and len(digits) <= _SMALL_DIGITS:
        return str(int(digits))
    return _tag("num", len(digits))


def _flag(name: str, known: frozenset[str], *, show: bool) -> str:
    if name in known:
        return name
    if show and SHOWABLE_FLAG.fullmatch(name):
        return f"<unknown_flag:{name}>"
    return _tag("unknown_flag", len(name))


def _flag_value(key: str, value: str, *, show: bool) -> str:
    """The token of a flags field's value, by the rules of that field: only
    ``0x0`` is a number in ``inputConfig=`` (no names), up to eight hex
    digits in ``flags=``; any other value is read as names ``A | B``."""
    if key == "inputConfig":
        if value == "0x0":
            return "0x0"
    elif _HEX.fullmatch(value) and (key != "flags" or len(value) <= _FLAGS_HEX):
        return "<hex>"
    known = FLAG_VOCABULARIES[key]
    return "|".join(
        _flag(name, known, show=show) if name else "<blank>"
        for name in value.split(" | ")
    )


def _plain(value: str, *, words: bool) -> str:
    """The token of a value: a known word (only with `words`), a type tag,
    or the value's length."""
    if value == "":
        return "<blank>"
    if words and value in KNOWN_VALUES:
        return value
    for pattern, tag in _SIMPLE:
        if pattern.fullmatch(value):
            return tag
    numbers = _NUMBER_LIST.fullmatch(value)
    if numbers is not None:
        return _tag("list", numbers[1].count(",") + 1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return _tag("quoted", len(value) - 2)
    return _tag("word" if _WORD.fullmatch(value) else "text", len(value))


def _field(key: str, value: str, *, show: bool, words: bool) -> str:
    """The token of one field of a cut window line."""
    shown = key if key in KNOWN_KEYS else _tag("unknown_key", len(key))
    if key in TEXT_KEYS:
        token = "<blank>" if value == "" else _tag("text", len(value))
    elif key in FLAG_VOCABULARIES:
        token = _flag_value(key, value, show=show)
    else:
        token = _plain(value, words=words and key in KNOWN_KEYS)
    return f"{shown}={token}"


def _row_tokens(cut: RowFields, *, show: bool, words: bool) -> list[str]:
    name = _tag("quoted" if cut.quoted else "text", cut.name_length)
    fields = [_field(k, v, show=show, words=words) for k, v in cut.fields]
    return [f"name={name}", *fields]


def _window_row(text: str, *, accepted: bool, show: bool) -> list[str]:
    """The tokens of a window line (stripped `text`): its fields when the
    reader read it into a window (`accepted`) or, with `show`, when it is a
    row of a found list; only when its name's end can be told
    (`window_line_fields`). Otherwise the number's width and one length."""
    head = _INDEX.match(text)
    digits = text[: head.end() - 1] if head is not None else ""
    rest = (text[head.end() :] if head is not None else text).removeprefix(" ")
    number = "row:" + _number(digits, verified=accepted)
    cut = window_line_fields(text) if accepted or show else None
    if cut is None:
        return [number, _tag("text", len(rest))]
    return [number, *_row_tokens(cut, show=show, words=accepted)]


def _end_tokens(text: str) -> list[str]:
    """The tokens of a line a window list ends at (`_LIST_END`)."""
    if text in HEADINGS:
        return [text]
    if text.startswith(_OBSCURING):
        return [_OBSCURING.strip(), _tag("text", len(text) - len(_OBSCURING))]
    monitors = _MONITORS.fullmatch(text)
    if monitors is not None:
        where = "in" if monitors[1] == "in" else "on"
        return [f"Global monitors {where} display", _tag("num", len(monitors[2])) + ":"]
    return [_tag("text", len(text))]


def _transform_tokens(match: re.Match[str]) -> list[str]:
    """A transform line's tokens: the rotation's width only (its digits are
    device text; the rule says whether the reader found it rotated) and the
    kind words."""
    tokens = ["transform", "ROT_" + _tag("num", len(match["rot"]))]
    words = match["kind"].split()
    tokens += [w if w in TRANSFORM_WORDS else _tag("word", len(w)) for w in words]
    return tokens


def _viewport_tokens(match: re.Match[str]) -> list[str]:
    """The parts of a Viewport line the reader's form matched whole."""
    kind = match["kind"]
    if kind is None:
        head = "Viewport:"
    elif kind in VIEWPORT_KINDS:
        head = f"Viewport {kind}:"
    else:
        head = "Viewport " + _tag("word", len(kind)) + ":"
    tokens = [head, "displayId=<num>"]
    if match["unique"] is not None:
        unique = match["unique"]
        tokens.append(
            "uniqueId=" + ("<blank>" if not unique else _tag("text", len(unique)))
        )
        tokens.append("port=<none>" if match["port"] == "<none>" else "port=<num>")
    tokens.append(f"orientation={match['orientation']}")
    if match["density"] is not None:
        tokens += ["densityDpi=<num>", "xDpi=<dec>", "yDpi=<dec>"]
    if match["quoted"] is not None:
        tokens.append("uniqueId=" + _tag("quoted", len(match["quoted"])))
    tokens += ["logicalFrame=<list:4>", "physicalFrame=<list:4>", "deviceSize=<list:2>"]
    if match["active"] is not None:
        tokens.append("isActive=<list:1>")
    return tokens


def _display_number(number: str, *, display_id: int) -> str:
    """The number of a ``Display:`` line: ``0`` when it is written ``0`` and
    display 0 was asked for, else its sign and width only."""
    if number == "0" and display_id == 0:
        return "0"
    digits = number.removeprefix("-")
    return ("-" if number.startswith("-") else "") + _tag("num", len(digits))


def _heading(text: str, *, display_id: int) -> list[str] | None:
    """The tokens of a heading the reader matches by its own patterns: its
    header, ``Windows:``, ``Display: N``, the lines a list ends at and
    ``logicalSize=``; None for any other line."""
    if text in (_HEADER, "Windows:"):
        return [text]
    if _DISPLAY.match(text):
        number = text.removeprefix("Display: ")
        return ["Display:", _display_number(number, display_id=display_id)]
    if _LIST_END.match(text):
        return _end_tokens(text)
    if text.startswith("logicalSize="):
        return ["logicalSize=" + _tag("text", len(text) - len("logicalSize="))]
    return None


def _reader_line(
    line: str, text: str, *, viewport: bool, display_id: int
) -> tuple[str, list[str]] | None:
    """The kind and tokens of a line the reader matches by its own patterns
    (no window line), or None. A Viewport line shows its parts only when the
    reader read it for the display's orientation (`viewport`)."""
    heading = _heading(text, display_id=display_id)
    if heading is not None:
        return "heading", heading
    transform = _TRANSFORM.match(text)
    if transform is not None:
        return "transform", _transform_tokens(transform)
    if _MATRIX_ROW.match(text):
        return "matrix_row", ["<dec>", "<dec>", "<dec>"]
    match = _VIEWPORT.fullmatch(line) if viewport else None
    if match is not None:
        return "viewport", _viewport_tokens(match)
    return None


def shape_line(
    line: str,
    *,
    accepted: bool = False,
    viewport: bool = False,
    display_id: int = 0,
    show_flag_names: bool = False,
    in_list: bool = False,
) -> tuple[str, list[str]]:
    """Return the kind (one of `LINE_KINDS`) and tokens of one line of
    ``dumpsys input``; no text of the line is in them (see the module).

    `accepted` says that the reader read the window line into a window;
    `viewport` that it read the Viewport line for the orientation of
    `display_id`, the display asked for. A window line shows its fields
    only when accepted, or with both `show_flag_names` and `in_list` (a row
    of a found list), which also lets unknown flag names show.
    """
    text = line.strip()
    if not text:
        return "blank", []
    if _INDEX.match(text):
        show = show_flag_names and in_list
        return "window_row", _window_row(text, accepted=accepted, show=show)
    known = _reader_line(line, text, viewport=viewport, display_id=display_id)
    if known is not None:
        return known
    return "other", [_tag("text", len(text))]


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


@dataclass(frozen=True, slots=True)
class _Dump:
    """The lines of the output, what the reader concluded, the display asked
    for, the lines of the lists it found (only rows there may show unknown
    flag names, and only with `show`)."""

    lines: list[str]
    verdict: Verdict
    display_id: int
    listed: frozenset[int]
    show: bool


def _shape(dump: _Dump, index: int, *, failing: bool) -> LineShape:
    line = dump.lines[index]
    kind, tokens = shape_line(
        line,
        accepted=index in dump.verdict.accepted,
        viewport=index in dump.verdict.viewports,
        display_id=dump.display_id,
        show_flag_names=dump.show,
        in_list=index in dump.listed,
    )
    number = _part(index, dump.verdict)[1]
    return LineShape(number, kind, _indent(line), len(line), tuple(tokens), failing)


def _part(index: int, verdict: Verdict) -> tuple[str, int]:
    """The part a line index is in and its number there: the dispatcher's
    part from its header to `Verdict.end`, the input reader's part above
    it, and the whole output (numbered from its top) otherwise."""
    start, end = verdict.start, verdict.end
    if start is None or end is None or index >= end:
        return OUTPUT, index + 1
    if index >= start:
        return DISPATCHER, index - start + 1
    return INPUT_READER, index + 1


def _bounds(index: int, verdict: Verdict, size: int) -> tuple[int, int]:
    """The indices (from, to) of the lines of the part `index` is in."""
    start, end = verdict.start, verdict.end
    if start is None or end is None:
        return 0, size
    if index >= end:
        return end, size
    if index >= start:
        return start, end
    return 0, start


def _around(dump: _Dump) -> tuple[LineShape, ...]:
    """The failing line with up to `_CONTEXT` lines either side, in its part."""
    failing = dump.verdict.line
    if failing is None:
        return ()
    low, high = _bounds(failing, dump.verdict, len(dump.lines))
    first = max(low, failing - _CONTEXT)
    last = min(high, failing + _CONTEXT + 1)
    return tuple(_shape(dump, i, failing=i == failing) for i in range(first, last))


def _outline(dump: _Dump) -> tuple[LineShape, ...]:
    """When no one line is to blame: the dispatcher's top-level lines (with
    no dispatcher header, the lines at indent 0; for the Viewport rules, the
    lines above the dispatcher that name a Viewport)."""
    lines, verdict = dump.lines, dump.verdict
    start, end = verdict.start, verdict.end
    if verdict.reason is None:
        return ()
    if verdict.rule == "viewport_missing":
        top = len(lines) if start is None else start
        chosen = [i for i in range(top) if "viewport" in lines[i].lower()]
    elif start is None or end is None:
        chosen = [
            i for i, line in enumerate(lines) if line.strip() and _indent(line) == 0
        ]
    else:
        under = [i for i in range(start + 1, end) if lines[i].strip()]
        level = min((_indent(lines[i]) for i in under), default=0)
        chosen = [start, *(i for i in under if _indent(lines[i]) == level)]
    return tuple(_shape(dump, i, failing=False) for i in chosen[:_OUTLINE_LINES])


def _lists(verdict: Verdict, display_id: int) -> tuple[ListRows, ...]:
    """The lists found; a display number is shown only when it is 0, written
    ``0``, and display 0 was asked for (see the module)."""
    rows: list[ListRows] = []
    for position, span in enumerate(verdict.lists, start=1):
        per_display = span.display is not None
        zero = span.display == 0 and span.digits == 1 and not span.negative
        shown = 0 if zero and display_id == 0 else None
        failed = verdict.list_head is not None and verdict.list_head == span.head
        rows.append(
            ListRows(
                position,
                per_display,
                shown,
                span.digits,
                span.rows,
                failed,
                negative=span.negative,
            )
        )
    return tuple(rows)


def _row_form(
    line: str, after: str | None, *, accepted: bool
) -> tuple[bool, bool, tuple[str, ...], bool]:
    """(recognised, quoted name, fields, followed by transform lines) of a
    row `line`, `after` being the list's next line (None: the last)."""
    transform = after is not None and _TRANSFORM.match(after.strip()) is not None
    cut = window_line_fields(line) if accepted else None
    if cut is None:
        return False, False, (), transform
    fields = tuple(
        "<unknown_key>" if key not in KNOWN_KEYS else key for key, _ in cut.fields
    )
    return True, cut.quoted, fields, transform


def _forms(lines: list[str], verdict: Verdict) -> tuple[Form, ...]:
    """The forms of the rows of each list (the rows the reader counts), in
    order of their first row."""
    forms: list[Form] = []
    for position, span in enumerate(verdict.lists, start=1):
        counts: dict[tuple[bool, bool, tuple[str, ...], bool], int] = {}
        for i, at in enumerate(span.lines):
            if _INDEX.match(lines[at].strip()):
                after = lines[span.lines[i + 1]] if i + 1 < len(span.lines) else None
                form = _row_form(lines[at], after, accepted=at in verdict.accepted)
                counts[form] = counts.get(form, 0) + 1
        forms.extend(
            Form(position, quoted, fields, transform, rows, recognised=recognised)
            for (recognised, quoted, fields, transform), rows in counts.items()
        )
    return tuple(forms[:_MAX_FORMS])


def diagnose_tap_window(
    text: str, *, show_flag_names: bool = False, display_id: int = 0
) -> TapWindowDiagnosis:
    """Diagnose the tap window check on the whole text of ``dumpsys input``.

    The result holds no text of `text` (see the module); with
    `show_flag_names` an unknown flag name that is an upper-case word, in a
    flags field of a row of a window list the reader found, is shown.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    verdict = explain_input_windows(text, display_id)
    if verdict.rule is not None and verdict.rule not in RULES[verdict.reason or ""]:
        msg = "the reader refused with a rule outside RULES"  # a bug, not the text
        raise AssertionError(msg)
    listed = frozenset(i for span in verdict.lists for i in span.lines)
    dump = _Dump(lines, verdict, display_id, listed, show_flag_names)
    part: str | None = None
    number: int | None = None
    if verdict.line is not None:
        part, number = _part(verdict.line, verdict)
    shapes = _around(dump) or _outline(dump)
    return TapWindowDiagnosis(
        verdict.reason,
        verdict.rule,
        part,
        number,
        _lists(verdict, display_id),
        shapes,
        _forms(lines, verdict),
        show_flag_names=show_flag_names,
    )
