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
* a number: a line number, or a count, length or indent of at most four
  digits (`CAP`; more is shown as ``9999+`` in text and as 9999 in JSON
  numbers), or a row or display number of at most four digits; or a tag
  built from a constant and such a number (``<text:12>``, ``<num:7>``).

Text an app sets (a window's name, ``applicationInfo.name``, the names on
other lines, `TEXT_KEYS`) is shown only as its length, never as a known
word. The fields of a line are read only when where such text ends can be
told: in a window line the reader must cut the name and each mark that ends
a name (``, id=``, ``, displayId=``, ``, applicationInfo.name=``,
``, applicationInfo.token=``) may be there once; on another line the text
must be one quoted run with no other quote. Otherwise everything after the
row number (or after the line's label) is shown as one length. Known value
words (`KNOWN_VALUES`) are shown only in the rows of a window list the
reader found and as the value of a known label (``DispatchEnabled: true``);
elsewhere every value is a type tag or its length. Text an app sets that
spans lines can still look like other lines of ``dumpsys input``; then the
known words in it are shown as such, never other text.

The one exception is opt-in: with ``show_flag_names`` an unknown name in the
``flags=``, ``inputConfig=`` or ``inputFeatures=`` field of a row of a
window list the reader found, whose name's end can be told, is shown when it
is an upper-case word (`SHOWABLE_FLAG`), as ``<unknown_flag:NAME>``. A
window name can still fake such a row, so the name can be any upper-case
device text; the report then says so (`NOTICE_SHOWN`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from uiprobe.inputwindows import (
    _FLAG_NAMES,
    _INDEX,
    _INPUT_CONFIG_NAMES,
    _LINE,
    _MATRIX_ROW,
    RULES,
    Verdict,
    explain_input_windows,
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
    " including a window name with or without a line break, may appear as such"
    " a name, so this output may include a serial"
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
# The largest count, length or indent shown, and the most digits a row or
# display number is shown with; longer ones are not numbers a dispatcher
# prints.
CAP: Final = 9999
_SMALL_DIGITS: Final = 4

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
# The names of ``inputFeatures=`` seen in the captures (API 31 and 32 print
# names); the reader does not read the field.
INPUT_FEATURE_NAMES: Final = frozenset({"NO_INPUT_CHANNEL"})
# The fields whose value is a list of flag names (``A | B``), each with the
# names known in that field, so a name of one field in another is unknown.
FLAG_VOCABULARIES: Final = {
    "flags": _FLAG_NAMES,
    "inputConfig": _INPUT_CONFIG_NAMES,
    "inputFeatures": INPUT_FEATURE_NAMES,
}
KNOWN_FLAGS: Final = _FLAG_NAMES | _INPUT_CONFIG_NAMES | INPUT_FEATURE_NAMES
# Fields whose value is text an app sets (or a window's title): always shown
# as its length, never as a known word.
TEXT_KEYS: Final = frozenset(
    {"name", "applicationInfo.name", "windowName", "channelName"}
)
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

_APP_NAME_END: Final = ", applicationInfo.token="
# The marks that end a window's name or its applicationInfo.name: each may
# be in a window line once, or the end cannot be told.
_ROW_MARKS: Final = (", id=", ", displayId=", ", applicationInfo.name=", _APP_NAME_END)
# The most hex digits of a flags=0x... value the reader reads.
_FLAGS_HEX: Final = len("0x") + 8
_LABEL = re.compile(r"([A-Za-z][A-Za-z0-9 ()_-]*?):(?= |$)")
_KEY = re.compile(r"(?:^|, )([A-Za-z][A-Za-z0-9_.]*)=")
_TRANSFORM_LINE = re.compile(r"transform \(([^()]*)\) \(([^()]*)\)(.*)")
_DECIMAL = r"-?\d+\.\d+"
_MATRIX = re.compile(rf"{_DECIMAL}(?: +{_DECIMAL})*")
_DIGITS = re.compile(r"\d+")
_NUMBER_LIST = re.compile(r"\[(-?\d+(?:\.\d+)?(?:, ?-?\d+(?:\.\d+)?)*)\]")
_WORD = re.compile(r"[A-Z][A-Z0-9_]*")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
# (pattern, tag) for values of one simple type, tried in order.
_SIMPLE: Final = (
    (re.compile(r"-?\d+"), "<num>"),
    (re.compile(_DECIMAL), "<dec>"),
    (_HEX, "<hex>"),
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
    """One window list: its place among the lists (from 1), whether it is the
    list of one ``Display:`` section (``per_display``; False for the one list
    of every display, API 24 to 28), that display's number (None when it has
    more than four digits, or for the one list), how many digits the number
    has, how many window rows the list has and whether the check refused in
    it."""

    position: int
    per_display: bool
    display: int | None
    display_digits: int
    rows: int
    failed: bool


@dataclass(frozen=True, slots=True)
class Form:
    """Rows of one list that look alike: whether the name is quoted, which
    fields follow it (`KNOWN_KEYS`, an unknown one as ``<unknown_key>``),
    whether transform lines follow the row, and how many rows. A row whose
    name's end cannot be told is of no recognised form (``recognised``
    False, no fields)."""

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
    the line is after the dispatcher's part) and
    ``line`` the line number in it (from 1; the dispatcher's header is line 1
    of `DISPATCHER`) where the check refused, both None when no one line is
    to blame. ``show_flag_names`` says whether unknown flag names may be in
    it (and so which notice it carries).
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
        display = f"display <num:{_count(item.display_digits)}>"
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
            f" (the name's end cannot be told), {transform}"
        )
    name = "quoted name" if form.quoted else "bare name"
    return (
        f"list {form.position}: {_count(form.rows)} rows, {name}, {transform};"
        f" fields: {' '.join(form.fields)}"
    )


def _tag(name: str, number: int) -> str:
    """A tag of a constant `name` and a count, such as ``<text:12>``."""
    return f"<{name}:{_count(number)}>"


def _row_number(digits: str) -> str:
    """The token of a row number, from its digits (counted before any
    conversion): the number up to four digits, else ``row:<num:DIGITS>``."""
    if len(digits) <= _SMALL_DIGITS:
        return f"row:{int(digits)}"
    return "row:" + _tag("num", len(digits))


def _flag(name: str, known: frozenset[str], *, show: bool) -> str:
    if name in known:
        return name
    if show and SHOWABLE_FLAG.fullmatch(name):
        return f"<unknown_flag:{name}>"
    return _tag("unknown_flag", len(name))


def _flags(key: str, value: str, *, show: bool) -> str:
    """A ``A | B`` value of the flags field `key` as flag tokens joined by
    ``|``, each name checked against the names of that field."""
    known = FLAG_VOCABULARIES[key]
    return "|".join(
        _flag(name, known, show=show) if name else "<blank>"
        for name in value.split(" | ")
    )


def _text(value: str) -> str:
    """Text an app sets: its length, quoted or not."""
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return _tag("quoted", len(value) - 2)
    return _tag("text", len(value))


def _flag_value(key: str, value: str, *, show: bool) -> str:
    """The token of a flags field's value, by the rules of that field: only
    ``0x0`` is a number in ``inputConfig=`` (no names), up to eight hex
    digits in ``flags=``; any other value is read as names."""
    if key == "inputConfig":
        if value == "0x0":
            return "0x0"
    elif _HEX.fullmatch(value) and (key != "flags" or len(value) <= _FLAGS_HEX):
        return "<hex>"
    return _flags(key, value, show=show)


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
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return _tag("quoted", len(value) - 2)
    return _tag("word" if _WORD.fullmatch(value) else "text", len(value))


def _value(key: str | None, value: str, *, show: bool, words: bool) -> str:
    """The token of the value of field `key` (None: text before any field).

    Text an app sets is its length; a flags field gives flag tokens (`show`
    lets unknown names show); known value words are shown only with `words`
    (a row of a found list, or the value of a known label) and then only
    for a known field; anything else is a type tag or its length."""
    if key in TEXT_KEYS:
        return "<blank>" if value == "" else _text(value)
    if key is not None and key in FLAG_VOCABULARIES:
        return _flag_value(key, value, show=show)
    return _plain(value, words=words and (key is None or key in KNOWN_KEYS))


def _fields(text: str, *, show: bool = False, words: bool = False) -> list[str]:
    """The tokens of ``key=value, key=value`` text: each key (known, or
    ``<unknown_key:LEN>``) with its value's token; text before the first key
    as one value. ``applicationInfo.name`` runs to ``, applicationInfo.token=``
    when that follows, as the reader cuts it. Callers make sure that no text
    an app sets can be read as fields here (`_cut_row`, `_line_fields`)."""
    keys = list(_KEY.finditer(text))
    if not keys:
        return [_value(None, text, show=False, words=words)] if text else []
    tokens: list[str] = []
    if keys[0].start() > 0:
        tokens.append(_value(None, text[: keys[0].start()], show=False, words=False))
    skip_to = -1
    for i, key in enumerate(keys):
        if key.start() < skip_to:
            continue
        name = key[1]
        end = keys[i + 1].start() if i + 1 < len(keys) else len(text)
        if name == "applicationInfo.name":
            token = text.find(_APP_NAME_END, key.end())
            if token >= 0:
                end = skip_to = token
        shown = name if name in KNOWN_KEYS else _tag("unknown_key", len(name))
        value = _value(name, text[key.end() : end], show=show, words=words)
        tokens.append(f"{shown}={value}")
    return tokens


def _line_fields(text: str, *, words: bool) -> list[str]:
    """The tokens of a line that is no window row (after its label).

    A field an app sets (`TEXT_KEYS`) must be the only one, quoted, with no
    other quote in the line; then it is its length and the fields around it
    are read. Otherwise its end cannot be told, and the whole text is shown
    as its length."""
    keys = [key for key in _KEY.finditer(text) if key[1] in TEXT_KEYS]
    if not keys:
        return _fields(text, words=words)
    whole = [_tag("text", len(text))]
    key = keys[0]
    if len(keys) > 1 or text.count("'") != 2 or text[key.end() : key.end() + 1] != "'":
        return whole
    close = text.index("'", key.end() + 1)
    after = text[close + 1 :]
    if after and not after.startswith(", "):
        return whole
    tokens = [
        *_fields(text[: key.start()]),
        f"{key[1]}={_tag('quoted', close - key.end() - 1)}",
    ]
    return tokens + _fields(after.removeprefix(", ")) if after else tokens


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
    """The kind and tokens of a blank, known, transform or matrix line (a
    line of decimals that is not three is `other`)."""
    if not text:
        return "blank", []
    if text in KNOWN_LINES:
        return "heading", [text]
    if text.startswith("transform ("):
        return "transform", _transform(text)
    if _MATRIX.fullmatch(text):
        kind = "matrix_row" if _MATRIX_ROW.match(text) else "other"
        return kind, ["<dec>"] * len(text.split())
    return None


def _cut_row(text: str) -> tuple[bool, int, str] | None:
    """Where the name of a window line (stripped) ends, when that can be
    told: whether it is quoted, its length and the fields after it.

    The reader must cut it (`uiprobe.inputwindows`), and none of the marks
    that end a name or ``applicationInfo.name`` may be in the line twice
    (`_ROW_MARKS`); ``applicationInfo.name`` needs exactly one
    ``, applicationInfo.token=`` after it. Otherwise text an app sets could
    be read as fields, and None is returned."""
    match = _LINE.match(text)
    if match is None or any(text.count(mark) > 1 for mark in _ROW_MARKS):
        return None
    app = text.find(", applicationInfo.name=")
    token = text.find(_APP_NAME_END)
    if (app >= 0) != (token >= 0) or token < app:
        return None
    quoted = match["quoted"] is not None
    name = match["quoted"] if quoted else match["bare"]
    start = match.start("id") if match["id"] is not None else match.start("fields")
    return quoted, len(name), text[start:]


def _window_row(
    text: str, digits: str, rest: str, *, show: bool, words: bool
) -> list[str]:
    """The tokens of a window line (stripped `text`, its row number's
    `digits`, the `rest` after them): the name as its length and the fields
    after it, when the name's end can be told (`_cut_row`); else everything
    after the number as one length. Only there may unknown flag names
    show (`show`) and known value words (`words`)."""
    number = _row_number(digits)
    cut = _cut_row(text)
    if cut is None:
        return [number, _tag("text", len(rest))]
    quoted, length, meta = cut
    name = _tag("quoted" if quoted else "text", length)
    return [number, f"name={name}", *_fields(meta, show=show, words=words)]


def shape_line(
    line: str, *, show_flag_names: bool = False, in_list: bool = False
) -> tuple[str, list[str]]:
    """Return the kind (one of `LINE_KINDS`) and tokens of one line of
    ``dumpsys input``; no text of the line is in them (see the module).

    Unknown flag names show only with both `show_flag_names` and `in_list`
    (the line is a row of a window list the reader found), and only in a
    window line whose name's end can be told; known value words only in
    such a row (`in_list`) and as the value of a known label."""
    text = line.strip()
    simple = _simple_line(text)
    if simple is not None:
        return simple
    row = _INDEX.match(text)
    if row is not None:
        digits = text[: row.end() - 1]
        rest = text[row.end() :].removeprefix(" ")
        show = show_flag_names and in_list
        tokens = _window_row(text, digits, rest, show=show, words=in_list)
        return "window_row", tokens
    label = _LABEL.match(text)
    if label is not None and "=" not in label[1]:
        rest = text[label.end() :].strip()
        shown = _label(label[1])
        known = "=" not in rest and not shown.startswith("<label:")
        tokens = [shown, *_line_fields(rest, words=known)]
        return ("fields" if "=" in rest else "heading"), tokens
    return ("fields" if "=" in text else "other"), _line_fields(text, words=False)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


@dataclass(frozen=True, slots=True)
class _Dump:
    """The lines of the output, what the reader concluded, and the indices
    of the lines of the window lists it found (only rows there may show
    unknown flag names, and only with `show`)."""

    lines: list[str]
    verdict: Verdict
    listed: frozenset[int]
    show: bool


def _shape(dump: _Dump, index: int, *, failing: bool) -> LineShape:
    line = dump.lines[index]
    kind, tokens = shape_line(
        line, show_flag_names=dump.show, in_list=index in dump.listed
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


def _lists(verdict: Verdict) -> tuple[ListRows, ...]:
    rows: list[ListRows] = []
    for position, span in enumerate(verdict.lists, start=1):
        per_display = span.display is not None
        shown = span.display if span.digits <= _SMALL_DIGITS else None
        failed = verdict.list_head is not None and verdict.list_head == span.head
        rows.append(
            ListRows(position, per_display, shown, span.digits, span.rows, failed)
        )
    return tuple(rows)


def _row_form(line: str, after: str | None) -> tuple[bool, bool, tuple[str, ...], bool]:
    """(recognised, quoted name, fields, followed by transform lines) of a
    row `line`, `after` being the list's next line (None: the last). A row
    whose name's end cannot be told is not recognised (no fields)."""
    transform = after is not None and after.strip().startswith("transform (")
    cut = _cut_row(line.strip())
    if cut is None:
        return False, False, (), transform
    quoted, _, meta = cut
    fields: list[str] = []
    for token in _fields(meta):
        key, equals, _ = token.partition("=")
        if equals:
            fields.append("<unknown_key>" if key.startswith("<unknown_key:") else key)
    return True, quoted, tuple(fields), transform


def _forms(lines: list[str], verdict: Verdict) -> tuple[Form, ...]:
    """The forms of the rows of each list (the rows the reader counts), in
    order of their first row."""
    forms: list[Form] = []
    for position, span in enumerate(verdict.lists, start=1):
        counts: dict[tuple[bool, bool, tuple[str, ...], bool], int] = {}
        for i, at in enumerate(span.lines):
            if _INDEX.match(lines[at].strip()):
                after = lines[span.lines[i + 1]] if i + 1 < len(span.lines) else None
                form = _row_form(lines[at], after)
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
    dump = _Dump(lines, verdict, listed, show_flag_names)
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
        _lists(verdict),
        shapes,
        _forms(lines, verdict),
        show_flag_names=show_flag_names,
    )
