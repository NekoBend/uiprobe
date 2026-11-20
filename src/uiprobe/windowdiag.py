"""Why the tap window list of a device cannot be read, told without its values.

`diagnose_tap_window` runs the same checks as the crawl's tap window check
(`uiprobe.inputwindows.explain_input_windows`) on the whole text of
``dumpsys input`` and reports the result so that it can be shared: whether
the list reads or which rule refused it and at which line, how many window
rows each list has, and the shape of the lines around the failure.

No text of the output is ever copied into the report. Every string in it is
one of:

* a constant of this module (the notice, line kinds, tags such as
  ``<num>``, the rule and reason names of `uiprobe.inputwindows`);
* a member of a fixed vocabulary of this module: the field names, labels,
  value words and flag names the reader knows (`KNOWN_KEYS`,
  `KNOWN_LABELS`, `KNOWN_VALUES`, `KNOWN_FLAGS`, `TRANSFORM_WORDS`,
  `KNOWN_LINES`);
* an integer (line numbers, indents, lengths, counts, small row and display
  numbers), or a tag built from a constant and an integer
  (``<text:12>``, ``<unknown_key:7>``).

The one exception is opt-in: with ``show_flag_names`` an unknown name in a
flags field (``flags=``, ``inputConfig=``, ``inputFeatures=``) is shown when
it is an upper-case word (`SHOWABLE_FLAG`), as ``<unknown_flag:NAME>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from uiprobe.inputwindows import (
    _FLAG_NAMES,
    _INPUT_CONFIG_NAMES,
    RULES,
    ListSpan,
    Verdict,
    explain_input_windows,
)

__all__ = [
    "COMMAND",
    "NOTICE",
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
DISPATCHER: Final = "dispatcher"
INPUT_READER: Final = "input_reader"
# How many lines before and after the failing one are shown.
_CONTEXT: Final = 2
# The most lines an outline (when no one line failed) and a forms summary show.
_OUTLINE_LINES: Final = 40
_MAX_FORMS: Final = 20
# Row and display numbers are shown up to this many digits; longer ones are
# not numbers a dispatcher prints and are shown as <num>.
_SMALL: Final = 9999

LINE_KINDS: Final = (
    "window_row",
    "transform",
    "matrix_row",
    "heading",
    "fields",
    "other",
    "blank",
)
# The field names of the dispatcher's lines (window rows of API 24 to 36,
# focus lines, connections) and of the input reader's Viewport lines.
KNOWN_KEYS: Final = frozenset(
    {
        "action",
        "alpha",
        "applicationInfo.name",
        "applicationInfo.token",
        "canReceiveKeys",
        "channelName",
        "deviceId",
        "deviceSize",
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
        "isActive",
        "layer",
        "length",
        "logicalFrame",
        "logicalSize",
        "monitor",
        "name",
        "orientation",
        "ownerPid",
        "ownerUid",
        "paused",
        "physicalFrame",
        "port",
        "portalToDisplayId",
        "responsive",
        "result",
        "scale",
        "source",
        "status",
        "token",
        "touchOcclusionMode",
        "touchableRegion",
        "trustedOverlay",
        "type",
        "uniqueId",
        "visible",
        "windowName",
        "windowScale",
    }
)
# Labels of ``Label: ...`` lines, with each run of digits as <num>.
KNOWN_LABELS: Final = frozenset(
    {
        "AppSwitch",
        "Configuration",
        "Connections",
        "Current Window with Pointer Capture",
        "CursorStatesByDisplay",
        "DispatchEnabled",
        "DispatchFrozen",
        "Display",
        "FocusRequests",
        "FocusedApplication",
        "FocusedApplications",
        "FocusedDisplayId",
        "FocusedWindow",
        "FocusedWindows",
        "Global monitors in display <num>",
        "Global monitors on display <num>",
        "InboundQueue",
        "Input Dispatcher State",
        "InputFilterEnabled",
        "KeyRepeatDelay",
        "KeyRepeatTimeout",
        "MonitoringChannels",
        "PendingEvent",
        "Pointer Capture Requested",
        "RecentQueue",
        "ReplacedKeys",
        "TouchModePerDisplay",
        "TouchStates",
        "TouchStatesByDisplay",
        "Viewport",
        "Viewport EXTERNAL",
        "Viewport INTERNAL",
        "Viewport VIRTUAL",
        "Windows",
        "mMaximumObscuringOpacityForTouch",
    }
)
# Whole lines shown as they are.
KNOWN_LINES: Final = frozenset({"INPUT MANAGER (dumpsys input)"})
# Values shown as they are.
KNOWN_VALUES: Final = frozenset(
    {
        "<empty>",
        "<no displays touched by cursor>",
        "<no displays touched>",
        "<none>",
        "<null>",
        "ALLOW",
        "BLOCK_UNTRUSTED",
        "None",
        "OK",
        "USE_OPACITY",
        "false",
        "true",
    }
)
# The fields whose value is a list of flag names (``A | B``).
FLAG_KEYS: Final = frozenset({"flags", "inputConfig", "inputFeatures"})
KNOWN_FLAGS: Final = _FLAG_NAMES | _INPUT_CONFIG_NAMES
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
# An unknown flag name shown with show_flag_names.
SHOWABLE_FLAG: Final = re.compile(r"[A-Z][A-Z0-9_]{0,47}")

_ROW_HEAD = re.compile(r"(\d+): ")
_LABEL = re.compile(r"([A-Za-z][A-Za-z0-9 ()_-]*?):(?= |$)")
_KEY = re.compile(r"(?:^|, )([A-Za-z][A-Za-z0-9_.]*)=")
_TRANSFORM_LINE = re.compile(r"transform \(([^()]*)\) \(([^()]*)\)(.*)")
_DECIMAL = r"-?\d+\.\d+"
_MATRIX = re.compile(rf"{_DECIMAL}(?: +{_DECIMAL})*")
_DIGITS = re.compile(r"\d+")
_NUMBER_LIST = re.compile(r"\[(-?\d+(?:\.\d+)?(?:, ?-?\d+(?:\.\d+)?)*)\]")
_WORD = re.compile(r"[A-Z][A-Z0-9_]*")
# (pattern, tag) for values of one simple type, tried in order.
_SIMPLE: Final = (
    (re.compile(r"-?\d+"), "<num>"),
    (re.compile(_DECIMAL), "<dec>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\[-?\d+,-?\d+\]\[-?\d+,-?\d+\]"), "<rect>"),
    (re.compile(r"\d+x\d+"), "<size>"),
    (re.compile(r"-?\d+(?:\.\d+)?ms"), "<ms>"),
)


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
    """One window list: its place among the lists (from 1), its display
    (None: the one list of every display, or a number of more than four
    digits), how many window rows it has and whether the check refused in
    it."""

    position: int
    display: int | None
    rows: int
    failed: bool


@dataclass(frozen=True, slots=True)
class Form:
    """Rows of one list that look alike: whether the name is quoted, which
    fields follow it (`KNOWN_KEYS`, an unknown one as ``<unknown_key>``),
    whether transform lines follow the row, and how many rows."""

    position: int
    display: int | None
    quoted: bool
    fields: tuple[str, ...]
    transform: bool
    rows: int


@dataclass(frozen=True, slots=True)
class TapWindowDiagnosis:
    """What `diagnose_tap_window` found (see the module for what may be in
    it).

    ``reason`` and ``rule`` are None when the list reads; ``part`` is
    `DISPATCHER` or `INPUT_READER` (the Viewport lines above it) and
    ``line`` the line number in it (from 1, the dispatcher's header being
    line 1) where the check refused, both None when no one line is to blame.
    """

    reason: str | None
    rule: str | None
    part: str | None
    line: int | None
    lists: tuple[ListRows, ...]
    lines: tuple[LineShape, ...]
    forms: tuple[Form, ...]

    @property
    def readable(self) -> bool:
        """Whether the check would read the list (and go on to the point)."""
        return self.reason is None

    def to_dict(self) -> dict[str, object]:
        """Return the ``uiprobe.diagnose_tap_window/1`` JSON object."""
        return {
            "schema": SCHEMA,
            "notice": NOTICE,
            "command": COMMAND,
            "readable": self.readable,
            "reason": self.reason,
            "rule": self.rule,
            "part": self.part,
            "line": self.line,
            "lists": [
                {
                    "position": item.position,
                    "display": item.display,
                    "rows": item.rows,
                    "failed": item.failed,
                }
                for item in self.lists
            ],
            "lines": [
                {
                    "number": shape.number,
                    "failing": shape.failing,
                    "kind": shape.kind,
                    "indent": shape.indent,
                    "length": shape.length,
                    "tokens": list(shape.tokens),
                }
                for shape in self.lines
            ],
            "forms": [
                {
                    "position": form.position,
                    "display": form.display,
                    "rows": form.rows,
                    "quoted": form.quoted,
                    "transform": form.transform,
                    "fields": list(form.fields),
                }
                for form in self.forms
            ],
        }

    def to_text(self) -> str:
        """Return the report as lines of text, the notice first."""
        lines = [f"uiprobe diagnose tap-window: {NOTICE}; it read: {COMMAND}"]
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


def _display_text(display: int | None) -> str:
    return "every display" if display is None else f"display {display}"


def _list_text(item: ListRows) -> str:
    failed = ", the check refused here" if item.failed else ""
    return (
        f"list {item.position} ({_display_text(item.display)}):"
        f" {item.rows} window rows{failed}"
    )


def _shape_text(shape: LineShape) -> str:
    mark = ">" if shape.failing else " "
    tokens = " ".join(shape.tokens)
    return f"{mark} {shape.number} {shape.kind} {shape.indent} {shape.length}: {tokens}"


def _form_text(form: Form) -> str:
    name = "quoted name" if form.quoted else "bare name"
    transform = "transform lines" if form.transform else "no transform lines"
    return (
        f"list {form.position} ({_display_text(form.display)}): {form.rows} rows,"
        f" {name}, {transform}; fields: {' '.join(form.fields)}"
    )


def _small(number: int) -> str:
    """A row or display number: as it is up to four digits, else <num>."""
    return str(number) if -_SMALL <= number <= _SMALL else "<num>"


def _tag(name: str, number: int) -> str:
    """A tag of a constant `name` and an integer, such as ``<text:12>``."""
    return f"<{name}:{number}>"


def _flag(name: str, *, show: bool) -> str:
    if name in KNOWN_FLAGS:
        return name
    if show and SHOWABLE_FLAG.fullmatch(name):
        return f"<unknown_flag:{name}>"
    return _tag("unknown_flag", len(name))


def _flags(value: str, *, show: bool) -> str:
    """A ``A | B`` value as flag tokens joined by ``|``."""
    return "|".join(
        _flag(name, show=show) if name else "<blank>" for name in value.split(" | ")
    )


def _typed(value: str) -> str | None:
    """The token of a value that is a known word or of one simple type."""
    if value == "":
        return "<blank>"
    if value in KNOWN_VALUES:
        return value
    for pattern, tag in _SIMPLE:
        if pattern.fullmatch(value):
            return tag
    numbers = _NUMBER_LIST.fullmatch(value)
    return None if numbers is None else _tag("list", numbers[1].count(",") + 1)


def _value(key: str | None, value: str, *, show: bool) -> str:
    """The token of a value: a known word, a type tag, flag tokens for a
    flags field, or ``<text:LEN>``."""
    typed = _typed(value)
    if typed is not None:
        return typed
    if key in FLAG_KEYS:
        return _flags(value, show=show)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return _tag("quoted", len(value) - 2)
    return _tag("word" if _WORD.fullmatch(value) else "text", len(value))


def _fields(text: str, *, show: bool) -> list[str]:
    """The tokens of ``key=value, key=value`` text: each key (known, or
    ``<unknown_key:LEN>``) with its value's token; text before the first key
    as one value."""
    keys = list(_KEY.finditer(text))
    if not keys:
        return [_value(None, text, show=show)] if text else []
    tokens: list[str] = []
    if keys[0].start() > 0:
        tokens.append(_value(None, text[: keys[0].start()], show=show))
    for i, key in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(text)
        name = key[1]
        shown = name if name in KNOWN_KEYS else _tag("unknown_key", len(name))
        tokens.append(f"{shown}={_value(name, text[key.end() : end], show=show)}")
    return tokens


def _transform(text: str) -> list[str]:
    match = _TRANSFORM_LINE.fullmatch(text)
    if match is None:
        return ["transform", _tag("text", len(text) - len("transform "))]
    words = [match[1], *match[2].split(" ")]
    tokens = ["transform"]
    tokens += [w if w in TRANSFORM_WORDS else _tag("word", len(w)) for w in words]
    if match[3]:
        tokens.append(_tag("text", len(match[3])))
    return tokens


def _label(text: str) -> str:
    shown = _DIGITS.sub("<num>", text)
    return f"{shown}:" if shown in KNOWN_LABELS else _tag("label", len(text)) + ":"


def _simple_line(text: str) -> tuple[str, list[str]] | None:
    """The kind and tokens of a blank, known, transform or matrix line."""
    if not text:
        return "blank", []
    if text in KNOWN_LINES:
        return "heading", [text]
    if text.startswith("transform ("):
        return "transform", _transform(text)
    if _MATRIX.fullmatch(text):
        return "matrix_row", ["<dec>"] * len(text.split())
    return None


def shape_line(line: str, *, show_flag_names: bool = False) -> tuple[str, list[str]]:
    """Return the kind (one of `LINE_KINDS`) and tokens of one line of
    ``dumpsys input``; no text of the line is in them (see the module)."""
    text = line.strip()
    simple = _simple_line(text)
    if simple is not None:
        return simple
    row = _ROW_HEAD.match(text)
    if row is not None:
        rest = text[row.end() :]
        tokens = [f"row:{_small(int(row[1]))}", *_fields(rest, show=show_flag_names)]
        return "window_row", tokens
    label = _LABEL.match(text)
    if label is not None and "=" not in label[1]:
        rest = text[label.end() :].strip()
        tokens = [_label(label[1]), *_fields(rest, show=show_flag_names)]
        return ("fields" if "=" in rest else "heading"), tokens
    return ("fields" if "=" in text else "other"), _fields(text, show=show_flag_names)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _shape(
    lines: list[str], index: int, number: int, *, failing: bool, show: bool
) -> LineShape:
    line = lines[index]
    kind, tokens = shape_line(line, show_flag_names=show)
    return LineShape(number, kind, _indent(line), len(line), tuple(tokens), failing)


def _part(index: int, start: int | None) -> tuple[str, int]:
    """The part a line index is in and its number there."""
    if start is not None and index >= start:
        return DISPATCHER, index - start + 1
    return INPUT_READER, index + 1


def _around(lines: list[str], verdict: Verdict, *, show: bool) -> tuple[LineShape, ...]:
    """The failing line with up to `_CONTEXT` lines either side, in its part."""
    failing = verdict.line
    if failing is None:
        return ()
    start = verdict.start
    if start is not None and failing >= start:
        low, high = start, len(lines)
    else:
        low, high = 0, len(lines) if start is None else start
    first = max(low, failing - _CONTEXT)
    last = min(high, failing + _CONTEXT + 1)
    return tuple(
        _shape(lines, i, _part(i, start)[1], failing=i == failing, show=show)
        for i in range(first, last)
    )


def _outline(
    lines: list[str], verdict: Verdict, *, show: bool
) -> tuple[LineShape, ...]:
    """When no one line is to blame: the dispatcher's top-level lines (with
    no dispatcher header, the lines at indent 0; for the Viewport rules, the
    lines above the dispatcher that name a Viewport)."""
    start = verdict.start
    if verdict.reason is None:
        return ()
    if verdict.rule == "viewport_missing":
        top = len(lines) if start is None else start
        chosen = [i for i in range(top) if "viewport" in lines[i].lower()]
    elif start is None:
        chosen = [
            i for i, line in enumerate(lines) if line.strip() and _indent(line) == 0
        ]
    else:
        under = []
        for i in range(start + 1, len(lines)):
            if not lines[i].strip():
                continue
            if _indent(lines[i]) <= _indent(lines[start]):
                break
            under.append(i)
        level = min((_indent(lines[i]) for i in under), default=0)
        chosen = [start, *(i for i in under if _indent(lines[i]) == level)]
    return tuple(
        _shape(lines, i, _part(i, start)[1], failing=False, show=show)
        for i in chosen[:_OUTLINE_LINES]
    )


def _shown_display(display: int | None) -> int | None:
    return display if display is not None and -_SMALL <= display <= _SMALL else None


def _lists(verdict: Verdict) -> tuple[ListRows, ...]:
    def failed(span: ListSpan) -> bool:
        return verdict.line is not None and (
            verdict.line == span.head or verdict.line in span.lines
        )

    return tuple(
        ListRows(i, _shown_display(span.display), span.rows, failed(span))
        for i, span in enumerate(verdict.lists, start=1)
    )


def _row_form(line: str, after: str | None) -> tuple[bool, tuple[str, ...], bool]:
    """(quoted name, fields, followed by transform lines) of a row `line`,
    `after` being the list's next line (None: the last)."""
    _, tokens = shape_line(line)
    fields: list[str] = []
    quoted = False
    for token in tokens[1:]:
        key, equals, value = token.partition("=")
        if key == "name":
            quoted = value.startswith("<quoted:")
        elif equals:
            fields.append("<unknown_key>" if key.startswith("<unknown_key:") else key)
    transform = after is not None and after.strip().startswith("transform (")
    return quoted, tuple(fields), transform


def _forms(lines: list[str], verdict: Verdict) -> tuple[Form, ...]:
    """The forms of the rows of each list, in order of their first row."""
    forms: list[Form] = []
    for position, span in enumerate(verdict.lists, start=1):
        counts: dict[tuple[bool, tuple[str, ...], bool], int] = {}
        for i, at in enumerate(span.lines):
            if _ROW_HEAD.match(lines[at].strip()):
                after = lines[span.lines[i + 1]] if i + 1 < len(span.lines) else None
                form = _row_form(lines[at], after)
                counts[form] = counts.get(form, 0) + 1
        display = _shown_display(span.display)
        forms.extend(
            Form(position, display, quoted, fields, transform, rows)
            for (quoted, fields, transform), rows in counts.items()
        )
    return tuple(forms[:_MAX_FORMS])


def diagnose_tap_window(
    text: str, *, show_flag_names: bool = False, display_id: int = 0
) -> TapWindowDiagnosis:
    """Diagnose the tap window check on the whole text of ``dumpsys input``.

    The result holds no text of `text` (see the module); with
    `show_flag_names` an unknown flag name that is an upper-case word is
    shown.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    verdict = explain_input_windows(text, display_id)
    if verdict.rule is not None and verdict.rule not in RULES[verdict.reason or ""]:
        msg = "the reader refused with a rule outside RULES"  # a bug, not the text
        raise AssertionError(msg)
    part: str | None = None
    number: int | None = None
    if verdict.line is not None:
        part, number = _part(verdict.line, verdict.start)
    shapes = _around(lines, verdict, show=show_flag_names)
    if not shapes:
        shapes = _outline(lines, verdict, show=show_flag_names)
    return TapWindowDiagnosis(
        verdict.reason,
        verdict.rule,
        part,
        number,
        _lists(verdict),
        shapes,
        _forms(lines, verdict),
    )
