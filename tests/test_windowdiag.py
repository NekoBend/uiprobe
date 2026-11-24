"""Tests for ``uiprobe diagnose tap-window``: the rule that refused, and no leak.

Every rule of `uiprobe.inputwindows.RULES` has a case below, made from the
captures in tests/data/inputwindows by one edit. The same cases are then
filled with identifier-like values (a serial, IMEI-like numbers, MAC and IP
addresses, accounts, phone numbers, package names, window titles with
field-like text, display ids, input device names) in window names, field
values, unknown keys, unknown flags, quoted values and lines outside the
lists. No output may hold any of them, and the report must not change when
those device-chosen values are changed (same lengths): a report that showed
any part of them would.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import pickle
import random
import re
import sys
import typing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from uiprobe import cli, crawl_driver
from uiprobe.crawl_driver import CrawlResult, _app_window, _Run
from uiprobe.device import Device
from uiprobe.inputwindows import (
    RULES,
    Unreadable,
    explain_input_windows,
    parse_input_windows,
    read_window_at,
    window_line_fields,
)
from uiprobe.packageuid import PackageUid
from uiprobe.windowdiag import (
    APP_CRAWL_TESTED,
    APP_TESTED,
    APP_UID_UNREADABLE,
    CAP,
    KNOWN_KEYS,
    LINE_KINDS,
    NOTE,
    NOTE_CRAWL_TESTED,
    NOTE_TESTED,
    NOTE_UID_UNREADABLE,
    NOTE_UID_UNREADABLE_ANY,
    NOTICE,
    NOTICE_SHOWN,
    SCHEMA,
    TRANSFORM_WORDS,
    TapWindowDiagnosis,
    diagnose_tap_window,
    shape_line,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.inputwindows import InputWindow

DATA = Path(__file__).parent / "data" / "inputwindows"
SERIAL = "emulator-5554"
# Python's default limit of digits int() converts; the number_too_long cases
# need it, whatever the host set.
DIGIT_LIMIT = 4300


@pytest.fixture(autouse=True)
def _digit_limit() -> Iterator[None]:
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(DIGIT_LIMIT)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(before)


def _capture(name: str) -> str:
    return (DATA / f"{name}.txt").read_text(encoding="utf-8")


API24 = _capture("api24")
API30 = _capture("api30")
API31 = _capture("api31")
API32 = _capture("api32")
API36 = _capture("api36")
SAMSUNG36 = _capture("samsung36")
CAPTURES = {name: _capture(name) for name in ("api24", "api30", "api31", "api32")}
CAPTURES |= {name: _capture(name) for name in ("api33", "api34", "api35", "api36")}
CAPTURES["samsung36"] = SAMSUNG36
IDENTITY = "      transform (ROT_0) (IDENTITY)"
TURNED = (
    "      transform (ROT_270) (ROTATE TRANSLATE)\n"
    "          0.0000  1.0000  0.0000\n"
    "          -1.0000  0.0000  320.0000\n"
    "          0.0000  0.0000  1.0000"
)


def _sub(text: str, old: str, new: str, count: int = 1) -> str:
    assert old in text, old
    return text.replace(old, new, count)


def _row(text: str, index: int) -> str:
    """The window line of row `index`."""
    found = [
        line for line in text.split("\n") if line.strip().startswith(f"{index}: name=")
    ]
    assert len(found) == 1, (index, len(found))
    return found[0]


def _edit_row(text: str, index: int, old: str, new: str) -> str:
    line = _row(text, index)
    assert old in line, (old, line)
    return text.replace(line, line.replace(old, new, 1))


def _after_row(text: str, index: int, old: str, new: str) -> str:
    """`text` with `old` right after row `index` replaced by `new`."""
    line = _row(text, index)
    return _sub(text, f"{line}\n{old}", f"{line}\n{new}")


def _viewports(text: str, old: str, new: str, count: int = -1) -> str:
    """`text` with `old` replaced by `new` in its first `count` Viewport lines
    (every one when -1)."""
    lines = text.split("\n")
    done = 0
    for i, line in enumerate(lines):
        if "Viewport" in line and old in line and done != count:
            lines[i] = line.replace(old, new)
            done += 1
    assert done, old
    return "\n".join(lines)


def _no_viewports(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if "Viewport" not in line)


# rule -> (text, display id); each one edit of a capture.
CASES: dict[str, tuple[str, int]] = {
    "no_dispatcher": (_sub(API36, "Input Dispatcher State:", "Input Dispatch:"), 0),
    "dispatcher_twice": (API36 + "\nInput Dispatcher State:\n", 0),
    "no_list": (_sub(API36, "    Windows:", "    Window list:"), 0),
    "lists_twice": (_sub(API24, "  Windows:", "  Windows: old\n  Windows:"), 0),
    "list_heading": (_sub(API24, "  Windows:", "  Windows: 7"), 0),
    "list_and_sections": (API24 + "\n  Display: 0", 0),
    "display_twice": (
        _sub(API36, "  Display: 0\n", "  Display: 00\n  Display: 0\n"),
        0,
    ),
    "display_missing": (_sub(API36, "  Display: 0\n", "  Display: 1\n"), 0),
    "no_rows": (API24, 5),
    "list_end": (
        _sub(API36, "  mMaximumObscuringOpacityForTouch: 0.800000", "  X: 1"),
        0,
    ),
    "stray_owner": (
        _sub(
            API36, "  DispatchEnabled: true", "  DispatchEnabled: true\n  P: ownerUid=1"
        ),
        0,
    ),
    "orphan_line": (_sub(API24, "  Windows:\n", "  Windows:\n    junk\n"), 0),
    "legacy_row_in_display_list": (
        _edit_row(API36, 0, ", globalScale=1,", ", globalScale=1, scale=1.000000,"),
        0,
    ),
    "form_mismatch": (
        _edit_row(API36, 2, ", displayId=0,", ", displayId=0, paused=false,"),
        0,
    ),
    "transform_mismatch": (_after_row(API36, 4, IDENTITY + "\n", ""), 0),
    "unknown_line": (_after_row(API36, 4, IDENTITY, IDENTITY + "\n      junk"), 0),
    "transform_shape": (
        _after_row(API36, 4, IDENTITY, IDENTITY + "\n          1.0000  0.0000  0.0000"),
        0,
    ),
    "row_shape": (_edit_row(API36, 3, ", displayId=0,", ", display=0,"), 0),
    "row_number": (_edit_row(API36, 3, "3: name=", "9: name="), 0),
    "field_twice": (_edit_row(API36, 3, ", ownerUid=", ", ownerUid=1, ownerUid="), 0),
    "display_mismatch": (_edit_row(API36, 3, ", displayId=0,", ", displayId=2,"), 0),
    "field_value": (_edit_row(API24, 0, "visible=true", "visible=yes"), 0),
    "field_missing": (_edit_row(API24, 0, ", ownerUid=10027", ""), 0),
    "unknown_flag": (_edit_row(API36, 3, "| SLIPPERY", "| SLIPPERY | SEM_FOO"), 0),
    "mixed_flags": (
        _sub(API36, ", inputConfig=", ", visible=true, inputConfig=", -1),
        0,
    ),
    "hex_form": (re.sub(r", windowScale=[^,]*", "", API30), 0),
    "portal_missing": (_sub(API31, ", portalToDisplayId=-1", "", -1), 0),
    "samsung_fields": (
        _edit_row(SAMSUNG36, 0, "samsungFlags=0x0", "samsungFlags=0x10"),
        0,
    ),
    # 0x1 is read only on a window the caller calls the app's; none here.
    "samsung_flags": (
        _edit_row(SAMSUNG36, 0, "samsungFlags=0x0", "samsungFlags=0x1"),
        0,
    ),
    "region_missing": (_edit_row(API36, 3, ", touchableRegion=<empty>", ""), 0),
    "region_format": (
        _edit_row(API36, 2, "[272,434][320,482]", "[272,434][320,482][1,1][2,2]"),
        0,
    ),
    "region_reversed": (
        _edit_row(API36, 2, "[272,434][320,482]", "[320,482][272,434]"),
        0,
    ),
    "display_transform_missing": (
        _sub(
            API36, "        transform (ROT_0) (IDENTITY)\n    Windows:", "    Windows:"
        ),
        0,
    ),
    "display_transform": (
        _sub(
            API36,
            "        transform (ROT_0) (IDENTITY)\n    Windows:",
            "        transform (ROT_90) (ROTATE)\n    Windows:",
        ),
        0,
    ),
    "row_rotated": (_after_row(API36, 0, IDENTITY, TURNED), 0),
    "row_orientation": (
        _edit_row(API32, 0, "displayOrientation=0", "displayOrientation=1"),
        0,
    ),
    "viewport_malformed": (_viewports(API36, "orientation=0", "orientation=7", 1), 0),
    "scaled": (
        _viewports(API36, "physicalFrame=[0, 0, 320", "physicalFrame=[0, 0, 640"),
        0,
    ),
    "viewport_missing": (_no_viewports(API36), 0),
    "viewport_disagree": (_viewports(API36, "orientation=0", "orientation=1", 1), 0),
    "rotated": (_viewports(API36, "orientation=0", "orientation=1"), 0),
    # More digits than int converts (Python's default limit is 4300).
    "number_too_long": (
        _viewports(API36, "displayId=0", "displayId=" + "0" * 4301, 1),
        0,
    ),
}
REASON_OF = {rule: reason for reason, rules in RULES.items() for rule in rules}


def test_every_rule_has_a_case():
    assert set(CASES) == set(REASON_OF)
    assert len(REASON_OF) == sum(len(rules) for rules in RULES.values())


@pytest.mark.parametrize("rule", sorted(CASES))
def test_each_rule_is_named_where_the_check_refuses(rule):
    text, display = CASES[rule]
    verdict = explain_input_windows(text, display)
    assert (verdict.reason, verdict.rule) == (REASON_OF[rule], rule)
    # The crawl's own reading refuses the same way, and names the rule.
    found = read_window_at(text, (1, 1), display)
    assert isinstance(found, Unreadable)
    assert (found, found.rule) == (REASON_OF[rule], rule)
    windows = parse_input_windows(text, display)
    if isinstance(windows, str):  # the Viewport rules are not the list's
        assert (windows, windows.rule) == (REASON_OF[rule], rule)


@pytest.mark.parametrize("name", sorted(CAPTURES))
def test_each_capture_reads(name):
    found = diagnose_tap_window(CAPTURES[name])
    assert found.readable
    assert (found.reason, found.rule, found.part, found.line) == (None,) * 4
    assert found.lines == ()
    rows = sum(
        1 for line in CAPTURES[name].split("\n") if re.match(r"\s*\d+: name=", line)
    )
    assert [(item.reached, item.read) for item in found.lists] == [(True, rows)]
    assert found.windows_read == rows
    shown = diagnose_tap_window(CAPTURES[name], show_device_text=True)
    assert sum(form.rows for form in shown.forms) == rows


def _failing(found):
    return next(shape for shape in found.lines if shape.failing)


def _rest(line: str) -> str:
    """The length tag of a window line after its row number."""
    text = line.strip()
    return f"<text:{len(text) - len(text.split(':', 1)[0]) - 2}>"


def test_the_default_report_holds_kinds_and_lengths_only():
    text, _ = CASES["unknown_flag"]
    lines = text.split("\n")
    header = lines.index("Input Dispatcher State:")
    at = lines.index(_row(text, 3))
    found = diagnose_tap_window(text)
    assert (found.part, found.line) == ("dispatcher", at - header + 1)
    assert found.line is not None
    assert [(s.number, s.kind, s.indent, s.length, s.tokens) for s in found.lines] == [
        (found.line + offset, kind, None, len(line), ())
        for offset, kind, line in zip(
            range(-2, 3),
            ("window_row", "transform", "window_row", "other", "other"),
            lines[at - 2 : at + 3],
            strict=True,
        )
    ]
    assert _failing(found).number == found.line
    assert [
        (i.position, i.per_display, i.asked, i.reached, i.read, i.failed)
        for i in found.lists
    ] == [(None, True, True, True, 3, True)]
    assert found.forms == ()
    shown = diagnose_tap_window(text, show_device_text=True)
    assert [s.indent for s in shown.lines] == [6, 6, 6, 6, 10]
    assert [i.position for i in shown.lists] == [1]


def test_with_show_device_text_a_refused_row_of_the_list_shows_its_flags():
    text, _ = CASES["unknown_flag"]
    failing = _failing(diagnose_tap_window(text, show_device_text=True))
    assert failing.tokens[0] == "row:<num:1>"
    assert (
        "inputConfig=NOT_FOCUSABLE|TRUSTED_OVERLAY|SLIPPERY|<unknown_flag:SEM_FOO>"
    ) in failing.tokens


def test_a_viewport_line_the_reader_refused_is_a_viewport_line():
    found = diagnose_tap_window(CASES["viewport_malformed"][0])
    assert (found.reason, found.rule, found.part, found.line) == (
        "rotation",
        "viewport_malformed",
        "input_reader",
        2,
    )
    # The reader matched the line's start (a Viewport line of display 0),
    # then stopped: it did not read the next one.
    assert [(s.number, s.kind) for s in found.lines] == [
        (1, "other"),
        (2, "viewport"),
        (3, "other"),
    ]


def test_the_viewport_lines_the_reader_read_show_their_parts():
    found = diagnose_tap_window(CASES["rotated"][0], show_device_text=True)
    assert (found.rule, found.part, found.line) == ("rotated", "input_reader", 2)
    read = [s for s in found.lines if s.kind == "viewport"]
    assert [s.number for s in read] == [2, 3]
    assert read[0].tokens == (
        "Viewport INTERNAL:",
        "displayId=<num>",
        "uniqueId=<text:25>",
        "port=<num>",
        "orientation=1",
        "logicalFrame=<list:4>",
        "physicalFrame=<list:4>",
        "deviceSize=<list:2>",
        "isActive=<list:1>",
    )
    assert all(s.tokens == () for s in diagnose_tap_window(CASES["rotated"][0]).lines)


def test_without_one_line_to_blame_the_outline_is_the_readers_headings():
    text = CASES["display_missing"][0]
    found = diagnose_tap_window(text)
    assert (found.rule, found.line) == ("display_missing", None)
    # No list was taken as display 0's: only the reader's header is a heading.
    assert [(s.number, s.kind) for s in found.lines] == [(1, "heading")]
    assert found.lists == ()
    assert found.display_list is None
    # With a list taken, its headings are the outline.
    missing = CASES["viewport_missing"][0]
    lines = missing.split("\n")
    header = lines.index("Input Dispatcher State:")
    headings = [
        i
        for i, line in enumerate(lines)
        if line.strip() in {"Input Dispatcher State:", "Display: 0", "Windows:"}
        or line.strip().startswith(("logicalSize=", "mMaximumObscuring"))
    ]
    outline = diagnose_tap_window(missing)
    assert [(s.number, s.kind) for s in outline.lines] == [
        (i - header + 1, "heading") for i in headings
    ]
    # Free text does not change the outline (only anchored headings count).
    word = _sub(missing, "  DispatchEnabled: true", "  DispatchEnabled: viewport")
    assert diagnose_tap_window(word).to_text() == outline.to_text()


def test_lists_of_several_displays_are_counted():
    second = (
        "  Display: 2\n    Windows:\n      0: name=x, id=1, displayId=2,"
        " inputConfig=0x0, alpha=1, frame=[0,0][1,1], globalScale=1,"
        " applicationInfo.name=, applicationInfo.token=<null>,"
        " touchableRegion=Q, ownerPid=1, ownerUid=1\n"
        "      transform (ROT_0) (IDENTITY)\n"
    )
    text = _sub(API36, "  Display: 0\n", second + "  Display: 0\n")
    found = diagnose_tap_window(text, show_device_text=True)
    assert (found.reason, found.rule) == ("region_format", "region_format")
    assert [
        (i.position, i.asked, i.reached, i.read, i.failed) for i in found.lists
    ] == [
        (1, False, True, 0, True),
        (2, True, False, 0, False),  # not reached: the other list refused first
    ]
    plain = diagnose_tap_window(text)
    assert [(i.asked, i.reached, i.failed) for i in plain.lists] == [
        (True, False, False)
    ]
    assert "the display's list: its Display section, not reached" in plain.to_text()


def test_no_display_number_is_shown():
    head, rest = API36.split("  Display: 0\n")
    other = rest.replace("displayId=0", "displayId=3")
    text = head + "  Display: 3\n" + other.rstrip("\n") + "\n  Display: 0\n" + rest
    found = diagnose_tap_window(text, show_device_text=True)
    assert [(i.asked, i.reached, i.read) for i in found.lists] == [
        (False, True, 13),
        (True, True, 13),
    ]
    plain = diagnose_tap_window(text)
    payload = json.loads(json.dumps(plain.to_dict()))
    assert payload["display_list"] == {
        "per_display": True,
        "reached": True,
        "read": 13,
        "failed": False,
    }
    assert "lists" not in payload
    assert plain.to_text() == diagnose_tap_window(API36).to_text()


VIEWPORT36 = API36.split("\n")[1]
VIEWPORT_SAMSUNG = SAMSUNG36.split("\n")[9]


def test_shapes_of_lines_the_reader_matched():
    row = _row(API24, 0)
    assert shape_line(row) == ("window_row", ["row:<num:1>", _rest(row)])
    assert shape_line(row, accepted=True) == (
        "window_row",
        [
            "row:0",
            "name=<quoted:28>",
            "displayId=<num>",
            "paused=false",
            "hasFocus=false",
            "hasWallpaper=false",
            "visible=true",
            "canReceiveKeys=false",
            "flags=<hex>",
            "type=<hex>",
            "layer=<num>",
            "frame=<rect>",
            "scale=<dec>",
            "touchableRegion=<rect>",
            "inputFeatures=<hex>",
            "ownerPid=<num>",
            "ownerUid=<num>",
            "dispatchingTimeout=<ms>",
        ],
    )
    assert shape_line("Input Dispatcher State:") == (
        "heading",
        ["Input Dispatcher State:"],
    )
    assert shape_line("  Display: 0") == ("heading", ["Display:", "0"])
    assert shape_line("  Display: 0", display_id=1) == (
        "heading",
        ["Display:", "<num:1>"],
    )
    assert shape_line("  Display: 00") == ("heading", ["Display:", "<num:2>"])
    assert shape_line("    Windows:") == ("heading", ["Windows:"])
    assert shape_line("  Connections:") == ("heading", ["Connections:"])
    assert shape_line("  mMaximumObscuringOpacityForTouch: 0.800000") == (
        "heading",
        ["mMaximumObscuringOpacityForTouch:", "<text:8>"],
    )
    assert shape_line("  Global monitors on display 0:") == (
        "heading",
        ["Global monitors on display", "<num:1>:"],
    )
    assert shape_line("    logicalSize=320x640") == (
        "heading",
        ["logicalSize=<text:7>"],
    )
    assert shape_line(" transform (ROT_90) (ROTATE TRANSLATE)") == (
        "transform",
        ["transform", "ROT_<num:2>", "ROTATE", "TRANSLATE"],
    )
    assert shape_line("  -1.0000  0.0000  320.0000") == (
        "matrix_row",
        ["<dec>", "<dec>", "<dec>"],
    )
    assert shape_line(VIEWPORT36) == ("other", [f"<text:{len(VIEWPORT36.strip())}>"])
    assert shape_line(VIEWPORT36, viewport=True) == (
        "viewport",
        [
            "Viewport INTERNAL:",
            "displayId=<num>",
            "uniqueId=<text:25>",
            "port=<num>",
            "orientation=0",
            "logicalFrame=<list:4>",
            "physicalFrame=<list:4>",
            "deviceSize=<list:2>",
            "isActive=<list:1>",
        ],
    )
    assert "densityDpi=<num>" in shape_line(VIEWPORT_SAMSUNG, viewport=True)[1]
    # Lines the reader does not match: their kind and length only.
    assert shape_line("  Some vendor line") == ("other", ["<text:16>"])
    assert shape_line("  DispatchEnabled: true") == ("other", ["<text:21>"])
    assert shape_line("") == ("blank", [])


def test_the_text_report_of_a_failure():
    text, _ = CASES["unknown_flag"]
    assert diagnose_tap_window(text).to_text().split("\n") == [
        "uiprobe diagnose tap-window: this output contains no device serial,"
        " app or window names, coordinates, accounts or network values;"
        " it read: dumpsys input",
        "result: not readable: line_format, rule unknown_flag at line 26 of the"
        " dispatcher part",
        "the display's list: its Display section, 3 windows read, the check"
        " refused here",
        "lines (number, kind, length):",
        "  24 window_row 351",
        "  25 transform 34",
        "> 26 window_row 357",
        "  27 other 35",
        "  28 other 33",
    ]


def test_the_json_report_of_a_readable_list():
    payload = diagnose_tap_window(API24).to_dict()
    assert payload == {
        "schema": SCHEMA,
        "notice": NOTICE,
        "show_device_text": False,
        "command": "dumpsys input",
        "readable": True,
        "reason": None,
        "rule": None,
        "part": None,
        "line": None,
        "windows_read": 7,
        "display_list": {
            "per_display": False,
            "reached": True,
            "read": 7,
            "failed": False,
        },
        "lines": [],
    }
    shown = diagnose_tap_window(API24, show_device_text=True).to_dict()
    assert shown["forms"] == [
        {
            "position": 1,
            "rows": 7,
            "recognised": True,
            "quoted": True,
            "transform": False,
            "fields": [
                "displayId",
                "paused",
                "hasFocus",
                "hasWallpaper",
                "visible",
                "canReceiveKeys",
                "flags",
                "type",
                "layer",
                "frame",
                "scale",
                "touchableRegion",
                "inputFeatures",
                "ownerPid",
                "ownerUid",
                "dispatchingTimeout",
            ],
        }
    ]


def test_the_samsung_form_reads_and_its_other_values_are_refused():
    assert diagnose_tap_window(SAMSUNG36).readable
    # As on main: a Samsung field with a value the phone did not print.
    text = _edit_row(API36, 3, ", alpha=1,", ", samsungFlags=0x10, alpha=1,")
    assert parse_input_windows(text) == "line_format"
    assert not diagnose_tap_window(text).readable


def test_a_name_is_only_its_length_even_when_it_is_a_known_word():
    for word in ("OK", "true", "false", "BLOCK_UNTRUSTED"):
        line = _row(API36, 3).replace("name=a8ea035 Taskbar,", f"name={word},")
        assert shape_line(line, accepted=True)[1][1] == f"name=<text:{len(word)}>"
    line = _row(API36, 3).replace(
        "applicationInfo.name=,", "applicationInfo.name=true, x=OK,"
    )
    assert "applicationInfo.name=<text:10>" in shape_line(line, accepted=True)[1]


def test_a_quoted_name_is_cut_where_the_reader_cuts_it():
    text = _edit_row(
        API24, 0, "name='Window{b24c1c0 u0 StatusBar}'", "name='My title, vendor=demo'"
    )
    found = diagnose_tap_window(text, show_device_text=True)
    assert found.readable  # the reader takes the whole quoted name
    tokens = shape_line(_row(text, 0), accepted=True)[1]
    assert tokens[1] == "name=<quoted:21>"
    assert not any(token.startswith("<unknown_key") for token in tokens)
    assert {(form.quoted, form.rows) for form in found.forms} == {(True, 7)}


def test_long_row_numbers_are_not_converted():
    # 4301 digits: past what int() converts; the reader says row_shape.
    long_row = _edit_row(API36, 3, "3: name=", "1" * 4301 + ": name=")
    found = diagnose_tap_window(long_row, show_device_text=True)
    assert (found.reason, found.rule) == ("line_format", "row_shape")
    assert _failing(found).tokens[0] == "row:<num:4301>"
    assert _failing(found).kind == "window_row"
    assert shape_line("  000000000001234: name=x") == (
        "window_row",
        ["row:<num:15>", "<text:6>"],
    )
    assert shape_line("  0012: name=x")[1][0] == "row:<num:4>"


@pytest.mark.parametrize(
    ("new", "token"),
    [
        ("inputConfig=0x1", "inputConfig=<unknown_flag:3>"),
        ("inputConfig=0x0", "inputConfig=0x0"),
    ],
)
def test_each_flags_field_has_its_own_number_rule(new, token):
    line = _row(API36, 3).replace(
        "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY | SLIPPERY", new
    )
    assert token in shape_line(line, accepted=True)[1]


def test_flags_and_feature_numbers_and_names():
    row = _row(API24, 0).replace("flags=0x81840048", "flags=0x123456789")
    assert "flags=<unknown_flag:11>" in shape_line(row, accepted=True)[1]
    row = _row(API36, 3).replace(", alpha=1,", ", alpha=1, inputFeatures=DROP_INPUT,")
    assert "inputFeatures=<unknown_flag:10>" in shape_line(row, accepted=True)[1]
    row = _row(API36, 3).replace("| SLIPPERY", "| SLIPPERY | ALT_FOCUSABLE_IM")
    tokens = shape_line(row, show_device_text=True, in_list=True)[1]
    assert (
        "inputConfig=NOT_FOCUSABLE|TRUSTED_OVERLAY|SLIPPERY"
        "|<unknown_flag:ALT_FOCUSABLE_IM>"
    ) in tokens


def test_a_matrix_row_has_three_columns():
    assert shape_line("  0.0 1.0") == ("other", ["<text:7>"])
    assert shape_line("  0.0 1.0 2.0")[0] == "matrix_row"


def test_counts_and_lengths_are_capped_when_rendered():
    long_end = _sub(
        API36, "  mMaximumObscuringOpacityForTouch: 0.800000", "  " + "x" * 12345
    )
    found = diagnose_tap_window(long_end)
    assert found.rule == "list_end"
    failing = _failing(found)
    assert (failing.kind, failing.length) == ("other", 12347)  # exact here
    assert f"> {found.line} other 9999+" in found.to_text()
    payload = json.loads(json.dumps(found.to_dict()))
    line = next(item for item in payload["lines"] if item["failing"])
    assert line == {
        "number": found.line,
        "failing": True,
        "kind": "other",
        "length": 9999,
    }
    shown = diagnose_tap_window(long_end, show_device_text=True)
    assert f"> {found.line} other 2 9999+" in shown.to_text()
    assert shape_line(" " * 20000 + "x") == ("other", ["<text:1>"])


# --- the reviewers' inputs ---

MARK = "ZX9SERIALMARK7"  # serial-shaped and upper-case, so showable
FRAGMENT = "status=OK"  # a known field and value


# Window lines whose name, applicationInfo.name or a quoted value holds a
# mark that ends a name (or lacks the one that ends it): never read.
AMBIGUOUS = {
    "bare_title_display_id": _edit_row(
        API36,
        3,
        "name=a8ea035 Taskbar,",
        f"name=X, displayId=0, inputConfig={MARK}, {FRAGMENT},",
    ),
    "bare_title_id": _edit_row(
        API36,
        3,
        "name=a8ea035 Taskbar,",
        f"name=X, id=5, inputConfig={MARK}, {FRAGMENT},",
    ),
    "app_name_token": _edit_row(
        API36,
        3,
        "applicationInfo.name=,",
        f"applicationInfo.name=X, applicationInfo.token=y, inputFeatures={MARK},"
        f" {FRAGMENT},",
    ),
    "app_name_without_token": _edit_row(
        _edit_row(API36, 3, ", applicationInfo.token=<null>", ""),
        3,
        "applicationInfo.name=,",
        f"applicationInfo.name=X, inputConfig={MARK}, {FRAGMENT},",
    ),
    "quoted_title_display_id": _edit_row(
        API24,
        0,
        "name='Window{b24c1c0 u0 StatusBar}'",
        f"name='X', displayId=0, flags={MARK}, {FRAGMENT}, z='",
    ),
    "quoted_title_only": (
        "Input Dispatcher State:\n  Windows:\n"
        f"    0: name='X, displayId=0, {FRAGMENT}, z={MARK}'\n  Connections:\n"
    ),
    "quoted_app_name_with_token": _edit_row(
        API36,
        3,
        "applicationInfo.name=, applicationInfo.token=<null>,",
        f"applicationInfo.name='X, applicationInfo.token=y, {FRAGMENT}, z={MARK}',",
    ),
    "viewport_quoted_unique_id": _sub(
        API24,
        "orientation=0, logicalFrame=",
        f"orientation=1, uniqueId='X, flags=SECURE, {FRAGMENT}, z={MARK}',"
        " logicalFrame=",
    ),
    "two_misplaced_quotes": f"FocusedWindow: id='x, name='{MARK}, {FRAGMENT}",
    "multiline_row_number": _edit_row(
        API36, 3, "name=a8ea035 Taskbar,", "name=a8ea035 T\n      7319: ignored,"
    ),
    "multiline_display": _edit_row(
        API36,
        3,
        "name=a8ea035 Taskbar,",
        "name=T\n  Display: 7319\n    Windows:\n      0: extra,",
    ),
}


@pytest.mark.parametrize("case", sorted(AMBIGUOUS))
def test_reviewer_inputs_show_nothing_of_the_device_text(case):
    text = AMBIGUOUS[case]
    found = diagnose_tap_window(text)  # never raises
    for output in (found.to_text(), json.dumps(found.to_dict())):
        assert MARK not in output
        assert FRAGMENT not in output
        assert "status" not in output
        assert "SECURE" not in output
        assert "7319" not in output
    assert all(not form.fields or "status" not in form.fields for form in found.forms)


def test_a_quoted_viewport_value_is_one_length():
    text = AMBIGUOUS["viewport_quoted_unique_id"]
    found = diagnose_tap_window(text, show_device_text=True)
    assert found.rule == "rotated"
    viewport = next(s for s in found.lines if s.kind == "viewport")
    quoted = f"X, flags=SECURE, {FRAGMENT}, z={MARK}"
    assert f"uniqueId=<quoted:{len(quoted)}>" in viewport.tokens


def test_flag_names_show_only_in_rows_of_a_found_list():
    lone = f"0: name=x, displayId=0, inputConfig={MARK}"
    found = diagnose_tap_window(lone, show_device_text=True)
    assert (found.rule, found.lists) == ("no_dispatcher", ())
    assert MARK not in found.to_text()
    assert MARK not in json.dumps(found.to_dict())
    outside = _sub(
        API36, "  DispatchEnabled: true", f"  DispatchEnabled: true\n  {lone}"
    )
    assert MARK not in diagnose_tap_window(outside, show_device_text=True).to_text()
    inside = _edit_row(API36, 3, "| SLIPPERY", f"| SLIPPERY | {MARK}")
    found = diagnose_tap_window(inside, show_device_text=True)
    assert f"<unknown_flag:{MARK}>" in found.to_text()


def test_a_name_that_fakes_a_whole_window_line_can_show_its_word():
    # Why the opt-in mode has its own notice.
    fake = "      3: name=x, id=1, displayId=0, inputConfig=SECRETWORD"
    text = _sub(API36, _row(API36, 3), _row(API36, 3) + "\n" + fake)
    found = diagnose_tap_window(text, show_device_text=True)
    assert "<unknown_flag:SECRETWORD>" in found.to_text()
    assert found.to_text().startswith(f"uiprobe diagnose tap-window: {NOTICE_SHOWN}")
    assert "SECRETWORD" not in diagnose_tap_window(text).to_text()


# --- no device values in any output, and no part of them ---

SECRETS = (
    "R5CT123ABC",  # a serial
    "356938035643809",  # IMEI-like
    "490154203237518",
    "02:00:5e:44:55:66",  # MAC
    "192.168.10.77",  # IPv4
    "fe80::1ff:fe23:4567:890a",  # IPv6
    "someone@example.com",  # an account
    "+81-90-5555-0199",  # a phone number
    "com.secretbank.wallet",  # a package
    "Transfer to Alice",  # a window title
    "4619827259835644672",  # a display uniqueId
    "sec_touchscreen",  # an input device name
)
PARTS = (
    "R5CT",
    "3569380",
    "4901542",
    "5e:44",
    "192.168",
    "fe80",
    "someone",
    "example",
    "5555-0199",
    "secretbank",
    "Alice",
    "Transfer",
    "46198272",
    "touchscreen",
    "status=OK",
)
TITLE = " ".join(SECRETS[:3] + SECRETS[6:10]) + ", status=OK"
APP_NAME = "com.secretbank.wallet R5CT123ABC, status=OK"
EXTRA = (
    ", sem_imei=356938035643809, macAddress=02:00:5e:44:55:66,"
    " ip=192.168.10.77, ipv6=fe80::1ff:fe23:4567:890a, account=someone@example.com,"
    " phone=+81-90-5555-0199, R5CT123ABC=490154203237518, owner=Transfer to Alice"
)
HEAD = (
    "  R5CT123ABC: someone@example.com",
    "  Device 356938035643809 serial=R5CT123ABC, name=sec_touchscreen",
    "  wlan0 192.168.10.77 02:00:5e:44:55:66 fe80::1ff:fe23:4567:890a",
    "  Account: Transfer to Alice +81-90-5555-0199 com.secretbank.wallet",
    f"  FocusedWindow: name='X, {FRAGMENT}, z={MARK}'",
    f"  {MARK}=1, {FRAGMENT}, owner={MARK}",
    f"  0: name={MARK}, displayId=0, inputConfig={MARK}, {FRAGMENT}",
)
READER = (
    "  Device 3: sec_touchscreen",
    "    Identifier: bus=0x0018, vendor=0x0000, uniqueId=4619827259835644672,"
    " descriptor=R5CT123ABC",
)
_NAME = re.compile(r"^(\s*\d+: name=)('?)(.*?)\2(, (?:id=\d+, )?displayId=)")
_APP = re.compile(r"applicationInfo\.name=[^,]*, applicationInfo\.token=")
# Words the reader reads in a window line (and the field names it knows):
# kept when values are changed, so the change does not change what the
# reader concludes.
PROTECTED = frozenset(
    {
        *(part for key in KNOWN_KEYS for part in key.split(".")),
        "name",
        "id",
        "displayId",
        "inputConfig",
        "flags",
        "visible",
        "ownerUid",
        "touchableRegion",
        "portalToDisplayId",
        "applicationInfo",
        "token",
        "scale",
        "globalScale",
        "windowScale",
        "displayOrientation",
        "samsungFlags",
        "pTouchableRegion",
        "1HXY",
        "uniqueId",
        "Display",
        "Windows",
        "transform",
        # The reader's words for a value, a transform and a list's end:
        # vocabulary that may show (see the guide), not what is checked here.
        "0x0",
        "null",
        "empty",
        "ROT",
        *TRANSFORM_WORDS,
        "MonitoringChannels",
        "Connections",
        "Viewport",
        "INTERNAL",
    }
)


def _same(text: str) -> str:
    return text


def _changed(text: str) -> str:
    """`text` with every word and number changed, lengths and the reader's
    words kept. Digits change by one fixed permutation of 1 to 9 (0 stays
    0), so numbers that were equal stay equal (a display and its rows) and
    no orientation or rotation turns 0 or stops being 0."""
    pick = random.Random(len(text))  # noqa: S311 - a test's fixed scrambling
    digits = list("123456789")
    while digits == list("123456789"):
        pick.shuffle(digits)
    permutation = str.maketrans("123456789", "".join(digits))

    def change(match: re.Match[str]) -> str:
        word = match[0]
        if word in PROTECTED:
            return word
        out = []
        for char in word:
            if char.isdigit():
                out.append(char.translate(permutation))
            elif char.isupper():
                out.append(pick.choice("ABCDEFGHJKMNPQRSTUVWXYZ"))
            else:
                out.append(pick.choice("abcdefghjkmnpqrstuvwxyz"))
        return "".join(out)

    return re.sub(r"[A-Za-z0-9]+", change, text)


def _hostile(text: str, chosen: Callable[[str], str]) -> str:
    """`text` with device-chosen values (passed through `chosen`) in names,
    applicationInfo.name, unknown fields and lines outside the lists, in ways
    that do not change what the check concludes."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _NAME.match(line):
            line = _NAME.sub(lambda m: f"{m[1]}{m[2]}{chosen(TITLE)}{m[2]}{m[4]}", line)
            app = f"applicationInfo.name={chosen(APP_NAME)}, applicationInfo.token="
            lines[i] = _APP.sub(lambda _m, app=app: app, line) + chosen(EXTRA)
        elif line.strip() == "Input Dispatcher State:":
            lines[i] = "\n".join([line, *(chosen(h) for h in HEAD)])
    filled = "\n".join(lines)
    return "\n".join(chosen(r) for r in READER) + "\n" + filled


def _secret_flags(text: str) -> str:
    return _sub(
        text, "| SEM_FOO", "| R5CT123ABC | someone@example.com | sec_touchscreen"
    )


HOSTILE: dict[str, tuple[Callable[[Callable[[str], str]], str], int]] = {
    rule: ((lambda c, t=text: _hostile(t, c)), display)
    for rule, (text, display) in CASES.items()
}
HOSTILE["unknown_flag"] = (
    lambda c: _secret_flags(_hostile(CASES["unknown_flag"][0], c)),
    0,
)
HOSTILE |= {
    f"readable_{name}": ((lambda c, t=text: _hostile(t, c)), 0)
    for name, text in CAPTURES.items()
}


def _no_secret(text: str, *, shown: frozenset[str] = frozenset()) -> None:
    """No secret or part of one in `text` (but the names in `shown`, as
    shown flag names), and no digit run longer than four."""
    for name in shown:
        text = text.replace(f"<unknown_flag:{name}>", "")
    for secret in (*SECRETS, *PARTS, MARK):
        assert secret not in text, secret
    assert max((len(run) for run in re.findall(r"\d+", text)), default=0) <= 4


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_no_device_value_reaches_the_report(case):
    build, display = HOSTILE[case]
    text = build(_same)
    for secret in SECRETS:
        assert secret in text  # the case holds every one of them
    found = diagnose_tap_window(text, display_id=display)
    expected = REASON_OF.get(case)
    if expected is not None:
        assert found.rule == case  # the values did not change the verdict
    else:
        assert found.readable
    _no_secret(found.to_text())
    _no_secret(json.dumps(found.to_dict()))


def _reports(text: str, display: int) -> tuple[str, str]:
    found = diagnose_tap_window(text, display_id=display)
    return found.to_text(), json.dumps(found.to_dict())


# The reviewers' inputs as templates: what an app chooses passes `chosen`.
CHOSEN_CASES: dict[str, Callable[[Callable[[str], str]], str]] = {
    "bare_title_fields": lambda c: _edit_row(
        API36,
        3,
        "name=a8ea035 Taskbar,",
        "name=" + c(f"X, displayId=0, inputConfig={MARK}, {FRAGMENT}") + ",",
    ),
    "quoted_title_only": lambda c: (
        "Input Dispatcher State:\n  Windows:\n    0: name='"
        + c(f"X, displayId=0, {FRAGMENT}, z=Y")
        + "'\n  Connections:\n"
    ),
    "app_name_fields": lambda c: _edit_row(
        API36,
        3,
        "applicationInfo.name=,",
        "applicationInfo.name="
        + c(f"X, applicationInfo.token=y, inputFeatures={MARK}, {FRAGMENT}")
        + ",",
    ),
    "quoted_app_name": lambda c: _edit_row(
        API36,
        3,
        "applicationInfo.name=, applicationInfo.token=<null>,",
        "applicationInfo.name='" + c(f"X, applicationInfo.token=y, {FRAGMENT}") + "',",
    ),
    "viewport_unique_id": lambda c: _sub(
        API24,
        "orientation=0, logicalFrame=",
        "orientation=1, uniqueId='" + c("X, flags=SECURE, z=Y") + "', logicalFrame=",
    ),
    "focused_window": lambda c: _sub(
        API36,
        "  DispatchEnabled: true",
        "  DispatchEnabled: true\n  FocusedWindow: name='"
        + c(f"X, {FRAGMENT}, z=Y")
        + "'",
    ),
    "multiline_row_number": lambda c: _edit_row(
        API36, 3, "name=a8ea035 Taskbar,", "name=" + c("T\n      7319: ignored") + ","
    ),
    "multiline_display": lambda c: _edit_row(
        API36,
        3,
        "name=a8ea035 Taskbar,",
        "name=" + c("T\n  Display: 7319\n    Windows:\n      0: extra") + ",",
    ),
    "app_name_known_words": lambda c: _edit_row(
        API36,
        3,
        "applicationInfo.name=,",
        "applicationInfo.name=" + c("true, x=OK") + ",",
    ),
    "known_word_title": lambda c: _edit_row(
        API36, 3, "name=a8ea035 Taskbar,", "name=" + c("OK") + ","
    ),
}


SNIPPETS = (
    ", flags=0x8",
    ", inputConfig=SPY",
    ", ownerUid=10",
    ", touchableRegion=<empty>",
    ", displayId=0",
    ", id=5",
    "'",
    "''",
    ", applicationInfo.token=y",
    " | DO_NOT_PILFER",
    ", samsungFlags=0x10",
    "\n      9: x",
    "\n  Display: 2\n    Windows:",
    "\n      transform (ROT_90) (ROTATE)",
    "\nInput Dispatcher State:",
    "\n  Connections:",
    ": ",
    "=",
)


def test_the_report_never_raises():
    pick = random.Random(7)  # noqa: S311 - a fixed test fuzz
    texts = [*CAPTURES.values(), *(text for text, _ in CASES.values())]
    for _ in range(300):
        text = pick.choice(texts)
        for _ in range(pick.randint(1, 4)):
            at = pick.randrange(len(text) + 1)
            text = text[:at] + pick.choice(SNIPPETS) + text[at:]
        for show in (False, True):
            found = diagnose_tap_window(text, show_device_text=show)
            found.to_text()
            json.dumps(found.to_dict())
    found = diagnose_tap_window("FocusedWindow: id='x, name='y")
    assert found.rule == "no_dispatcher"


def test_show_device_text_shows_only_upper_case_words():
    build, _ = HOSTILE["unknown_flag"]
    text = build(_same)
    plain = diagnose_tap_window(text).to_text()
    shown = diagnose_tap_window(text, show_device_text=True).to_text()
    assert "R5CT123ABC" not in plain
    assert "<unknown_flag" not in plain  # the refused row is a length
    assert "|<unknown_flag:R5CT123ABC>|<unknown_flag:19>|<unknown_flag:15>" in shown
    assert shown.count("R5CT123ABC") == 1  # only the flag, not the title or keys
    row = _row(API36, 3)
    for name, visible in (("A" * 48, True), ("A" * 49, False), ("Ab", False)):
        line = row.replace("| SLIPPERY", f"| SLIPPERY | {name}")
        kind, tokens = shape_line(line, show_device_text=True, in_list=True)
        assert kind == "window_row"
        assert (f"|<unknown_flag:{name}>" in " ".join(tokens)) is visible, tokens
    assert shape_line("  flags=SECRETWORD", show_device_text=True, in_list=True) == (
        "other",
        ["<text:16>"],
    )


# --- the command line ---


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def phone(fake_adb: FakeAdbServer) -> FakeAdbServer:
    fake_adb.add_device(SERIAL, model="Pixel_8")
    build, _ = HOSTILE["unknown_flag"]
    fake_adb.on_shell(SERIAL, "dumpsys input", build(_same).encode())
    return fake_adb


def _sent(fake: FakeAdbServer) -> list[str]:
    return [command for _, kind, command in fake.commands if kind == "shell"]


def test_the_command_reads_dumpsys_input_once(phone, capsys):
    code, out, err = invoke(
        capsys, "diagnose", "tap-window", "--adb-server", phone.address
    )
    assert (code, err) == (0, "")
    assert _sent(phone) == ["dumpsys input"]
    lines = out.rstrip("\n").split("\n")
    assert lines[0].startswith(f"uiprobe diagnose tap-window: {NOTICE}")
    assert lines[0].endswith("it read: dumpsys input")
    assert lines[1].startswith("result: not readable: line_format, rule unknown_flag")
    _no_secret(out)
    assert SERIAL not in out


def test_the_command_prints_json(phone, capsys):
    argv = ["diagnose", "tap-window", "--json", "--show-device-text"]
    code, out, _ = invoke(capsys, *argv, "--adb-server", phone.address)
    assert code == 0
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.diagnose_tap_window/1"
    assert (payload["notice"], payload["show_device_text"]) == (NOTICE_SHOWN, True)
    assert (payload["readable"], payload["reason"], payload["rule"]) == (
        False,
        "line_format",
        "unknown_flag",
    )
    assert payload["part"] == "dispatcher"
    failing = [line for line in payload["lines"] if line["failing"]]
    assert [line["number"] for line in failing] == [payload["line"]]
    _no_secret(out, shown=frozenset({"R5CT123ABC"}))


@pytest.mark.parametrize("as_json", [False, True])
def test_a_device_error_names_only_its_code(fake_adb, capsys, as_json):
    fake_adb.add_device(SERIAL)
    argv = [
        "-s",
        "R5CT123ABC",
        "diagnose",
        "tap-window",
        "--adb-server",
        fake_adb.address,
    ]
    code, out, err = invoke(capsys, *argv, *(["--json"] if as_json else []))
    assert code == 4
    assert "R5CT123ABC" not in out + err
    if as_json:
        error = json.loads(out)["error"]
        assert error["code"] == "device_not_found"
        assert "details" not in error
    else:
        assert err.startswith(
            "uiprobe: error: the check could not run (device_not_found)"
        )


def _raising(error: Exception) -> Callable[..., object]:
    def diagnose(*_args: object, **_kwargs: object) -> object:
        raise error

    return diagnose


@pytest.mark.parametrize(
    ("error", "code", "status"),
    [
        # Raised while the device is read, not by the arguments.
        (ValueError("bad value R5CT123ABC"), "internal_error", 1),
        (RuntimeError("someone@example.com"), "internal_error", 1),
    ],
)
def test_an_unexpected_error_shows_no_text(
    phone, capsys, monkeypatch, error, code, status
):
    monkeypatch.setattr(Device, "diagnose_tap_window", _raising(error))
    argv = ["diagnose", "tap-window", "--json", "--adb-server", phone.address]
    got, out, _ = invoke(capsys, *argv)
    assert got == status
    payload = json.loads(out)
    assert payload["error"]["code"] == code
    assert str(error) not in out
    assert "R5CT123ABC" not in out
    assert "someone" not in out


def test_the_check_is_required(capsys):
    code, out, err = invoke(capsys, "diagnose")
    assert code == 2
    assert out == ""
    assert "uiprobe [-s SERIAL] diagnose tap-window" in err


def test_show_device_text_has_its_own_notice():
    text = HOSTILE["unknown_flag"][0](_same)
    shown = diagnose_tap_window(text, show_device_text=True)
    plain = diagnose_tap_window(text)
    first = shown.to_text().split("\n")[0]
    assert (
        first == f"uiprobe diagnose tap-window: {NOTICE_SHOWN}; it read: dumpsys input"
    )
    assert f"{NOTICE};" not in shown.to_text()  # the plain promise is not made
    assert shown.to_dict()["notice"] == NOTICE_SHOWN
    assert plain.to_dict()["notice"] == NOTICE
    assert plain.to_text().startswith(f"uiprobe diagnose tap-window: {NOTICE};")


# Window lines whose name or applicationInfo.name holds a flags field: the
# reader refuses each one (a read field twice), and no mode shows the word.


@pytest.mark.parametrize(
    "argv",
    [
        ["diagnose", "-s", "DEMO_SERIAL", "tap-window", "--json"],
        ["diagnose", "tap-window", "DEMO_SERIAL", "--json"],
        ["diagnose", "tap-window", "--bogus=DEMO_SERIAL"],
        ["diagnose", "DEMO_SERIAL"],
        [
            "-s",
            "DEMO_SERIAL",
            "diagnose",
            "tap-window",
            "--adb-server",
            "x DEMO_SERIAL",
        ],
    ],
)
def test_diagnose_arguments_are_never_echoed(capsys, argv):
    code, out, err = invoke(capsys, *argv)
    assert code == 2
    assert "DEMO_SERIAL" not in out + err
    shown = json.loads(out)["error"]["message"] if "--json" in argv else err
    assert "uiprobe [-s SERIAL] diagnose tap-window [-s SERIAL]" in shown


def test_the_command_reports_a_long_row_number(fake_adb, capsys):
    fake_adb.add_device(SERIAL)
    text = _edit_row(API36, 3, "3: name=", "1" * 4301 + ": name=")
    fake_adb.on_shell(SERIAL, "dumpsys input", text.encode())
    argv = ["diagnose", "tap-window", "--json", "--adb-server", fake_adb.address]
    code, out, _ = invoke(capsys, *argv)
    assert code == 0
    assert json.loads(out)["rule"] == "row_shape"


@pytest.mark.parametrize("written", ["0000001234", "12345", "-7", "7 ", chr(0x661)])
def test_a_display_section_shows_nothing_of_its_number(written):
    text = _sub(API36, "  Display: 0\n", f"  Display: {written}\n")
    found = diagnose_tap_window(text)
    assert found.rule == "display_missing"
    assert found.lists == ()
    assert "the display's list: none taken" in found.to_text()
    reports = {
        diagnose_tap_window(
            _sub(API36, "  Display: 0\n", f"  Display: {w}\n")
        ).to_text()
        for w in ("1234567890", "99999", "-8", "8 ", chr(0x662))
        if len(w) == len(written)
    }
    assert reports <= {found.to_text()}
    legacy = diagnose_tap_window(API24)
    assert "the display's list: the list of every display" in legacy.to_text()


def test_an_unknown_line_after_a_row_is_named_not_a_transform():
    text = _sub(API24, _row(API24, 0), _row(API24, 0) + "\n      vendor note")
    lines = text.split("\n")
    header = lines.index("Input Dispatcher State:")
    found = diagnose_tap_window(text)
    assert (found.reason, found.rule) == ("line_format", "unknown_line")
    assert found.line == lines.index("      vendor note") - header + 1
    assert read_window_at(text, (1, 1)) == "line_format"  # still refused
    # A real transform mismatch is still named as one.
    assert diagnose_tap_window(CASES["transform_mismatch"][0]).rule == (
        "transform_mismatch"
    )


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_unreadable_survives_copy_and_pickle(protocol):
    found = parse_input_windows("")
    assert isinstance(found, Unreadable)
    for made in (
        copy.copy(found),
        copy.deepcopy(found),
        pickle.loads(pickle.dumps(found, protocol=protocol)),  # noqa: S301
    ):
        assert isinstance(made, Unreadable)
        assert (made, made.rule) == ("no_list", "no_dispatcher")


def test_the_refusing_list_is_marked_even_at_a_line_outside_it():
    found = diagnose_tap_window(CASES["list_end"][0])
    assert found.rule == "list_end"
    assert [item.failed for item in found.lists] == [True]


def test_without_one_dispatcher_lines_count_from_the_top():
    text, _ = CASES["dispatcher_twice"]
    found = diagnose_tap_window(text)
    lines = text.split("\n")
    second = len(lines) - 1 - lines[::-1].index("Input Dispatcher State:")
    assert (found.part, found.line) == ("output", second + 1)
    assert "of the output part" in found.to_text()


@pytest.mark.parametrize(
    ("unknown", "rule", "named"),
    [
        ("rotation", None, False),
        ("rotation", "rotated", True),
        ("line_format", None, False),
    ],
)
def test_the_crawl_hint_names_diagnose_only_with_a_reader_rule(unknown, rule, named):
    result = CrawlResult(
        Path("run"),
        "not_sent",
        1,
        1,
        not_sent=(("cover_unknown", 1),),
        repin=True,
        cover_unknown=unknown,
        cover_rule=rule,
    )
    [hint] = cli._press_hints(result, package="com.example.shop", back=False)
    assert ("diagnose tap-window" in hint) is named
    assert (f"rule {rule}" in hint) is named


def test_the_opt_in_notice_says_device_text_appears():
    for words in (
        "text and numbers from the device",
        "field names, values and flags",
        "with or without line breaks",
        "serial or personal data",
        "read it before",
    ):
        assert words in NOTICE_SHOWN


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "before",
    [["--ser", "DEMO_SERIAL"], ["--adb-serverx", "DEMO_SERIAL"], ["-x", "DEMO_SERIAL"]],
)
def test_an_unknown_option_before_diagnose_is_not_echoed(capsys, before, as_json):
    argv = [*before, "diagnose", "tap-window", *(["--json"] if as_json else [])]
    code, out, err = invoke(capsys, *argv)
    assert code == 2
    assert "DEMO_SERIAL" not in out + err
    shown = json.loads(out)["error"]["message"] if as_json else err
    assert "uiprobe [-s SERIAL] diagnose tap-window [-s SERIAL]" in shown


# --- samsungFlags=0x1: the note, and --package ---

LAUNCHER = "com.example.launcher"  # the app of rows 4, 6 and 11 of SAMSUNG36
LAUNCHER_UID = 10002
# The launcher's own window (row 11), and a window of another uid (row 3).
OWN_ROW = 11
OTHER_ROW = 3
# A window of the launcher's uid whose title does not name it (row 6).
UNNAMED_ROW = 6


def _flagged(row: int) -> str:
    """SAMSUNG36 with ``samsungFlags=0x1`` on row `row`."""
    return _edit_row(SAMSUNG36, row, "samsungFlags=0x0", "samsungFlags=0x1")


def _package_stanza(package: str, uid: int, *, shared: bool = False) -> str:
    """A ``dumpsys package <package>`` output in the API 24 form."""
    shared_line = (
        f"    sharedUser=SharedUserSetting{{1d5f23c com.example.shared/{uid}}}\n"
        if shared
        else ""
    )
    return (
        "Packages:\n"
        f"  Package [{package}] (634ebb0):\n"
        f"    userId={uid}\n"
        f"{shared_line}"
        f"    pkg=Package{{9f8e7d6 {package}}}\n"
        "    User 0: ceDataInode=114790 installed=true hidden=false"
        " suspended=false stopped=false notLaunched=false enabled=0\n"
        "Dexopt state:\n"
    )


def _crawl_test(package: str, app: PackageUid) -> Callable[[InputWindow], bool]:
    """The crawl's test (`_Run.app_window` calls it with the run's package)."""
    return lambda window: _app_window(package, window, app)


@pytest.mark.parametrize("rule", sorted(CASES))
def test_the_note_is_given_only_for_samsung_flags(rule):
    text, display = CASES[rule]
    for shown in (False, True):
        found = diagnose_tap_window(text, display_id=display, show_device_text=shown)
        lines = found.to_text().split("\n")
        payload = found.to_dict()
        if rule != "samsung_flags":
            assert found.note is None
            assert "note" not in payload
            assert not any(line.startswith("note:") for line in lines)
            continue
        assert found.note == NOTE
        assert payload["note"] == NOTE
        assert lines[1].startswith("result: not readable: line_format, rule samsung")
        assert lines[2] == f"note: {NOTE}"
        assert sum(line.startswith("note:") for line in lines) == 1


def test_the_note_names_the_crawl_and_the_option():
    assert NOTE == (
        "a crawl accepts samsungFlags=0x1 on the target app's own window;"
        " diagnose is not told an app (add --package PKG to test that app's"
        " windows)"
    )
    for name in CAPTURES:
        assert "note" not in diagnose_tap_window(CAPTURES[name]).to_dict()


@pytest.mark.parametrize("shared", [False, True])
def test_with_the_crawls_test_the_apps_own_0x1_window_reads(shared):
    app = PackageUid(LAUNCHER_UID, shared=shared)
    owned = _crawl_test(LAUNCHER, app)
    own = diagnose_tap_window(_flagged(OWN_ROW), owned=owned)
    assert (own.readable, own.app_test, own.note) == (True, APP_TESTED, None)
    assert "note" not in own.to_dict()
    other = diagnose_tap_window(_flagged(OTHER_ROW), owned=owned)
    assert (other.reason, other.rule) == ("line_format", "samsung_flags")
    assert (other.app_test, other.note) == (APP_TESTED, NOTE_TESTED)
    assert other.to_text().split("\n")[2] == f"note: {NOTE_TESTED}"
    assert other.to_dict()["note"] == NOTE_TESTED
    # The title test of a shared uid: a window of the uid that does not name
    # the app is the app's only when the uid is its own.
    unnamed = diagnose_tap_window(_flagged(UNNAMED_ROW), owned=owned)
    assert unnamed.readable is not shared
    # The same verdicts as the crawl's own reader with the same test.
    for row in (OWN_ROW, OTHER_ROW, UNNAMED_ROW):
        verdict = explain_input_windows(_flagged(row), owned=owned)
        found = diagnose_tap_window(_flagged(row), owned=owned)
        assert (found.reason, found.rule) == (verdict.reason, verdict.rule)


def test_a_wrong_app_test_is_refused():
    found = diagnose_tap_window(SAMSUNG36)
    with pytest.raises(ValueError, match="app_test"):
        dataclasses.replace(found, app_test="maybe")


def _samsung_phone(fake: FakeAdbServer, row: int, package: str | None) -> None:
    fake.add_device(SERIAL, model="SM_TEST")
    fake.on_shell(SERIAL, "dumpsys input", _flagged(row).encode())
    if package is not None:
        stanza = _package_stanza(package, LAUNCHER_UID)
        fake.on_shell(SERIAL, f"dumpsys package {package}", stanza.encode())


TAP_WINDOW = ["diagnose", "tap-window"]


@pytest.mark.parametrize("as_json", [False, True])
def test_the_command_adds_the_note_for_samsung_flags(fake_adb, capsys, as_json):
    _samsung_phone(fake_adb, OWN_ROW, None)
    argv = [*TAP_WINDOW, "--adb-server", fake_adb.address]
    code, out, err = invoke(capsys, *argv, *(["--json"] if as_json else []))
    assert (code, err) == (0, "")
    assert _sent(fake_adb) == ["dumpsys input"]
    if as_json:
        payload = json.loads(out)
        assert (payload["rule"], payload["note"]) == ("samsung_flags", NOTE)
        assert payload["schema"] == SCHEMA
        return
    lines = out.rstrip("\n").split("\n")
    assert lines[1].startswith("result: not readable: line_format, rule samsung_flags")
    assert lines[2] == f"note: {NOTE}"


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    ("row", "readable", "note"),
    [(OWN_ROW, True, None), (OTHER_ROW, False, NOTE_CRAWL_TESTED)],
    ids=["own_window", "other_window"],
)
def test_package_accepts_0x1_only_on_the_apps_own_window(
    fake_adb, capsys, row, readable, note, as_json
):
    _samsung_phone(fake_adb, row, LAUNCHER)
    argv = [*TAP_WINDOW, "--package", LAUNCHER, "--adb-server", fake_adb.address]
    code, out, err = invoke(capsys, *argv, *(["--json"] if as_json else []))
    assert (code, err) == (0, "")
    # The existing read plus the app's uid, read first as the crawl does;
    # nothing that presses, types or starts anything.
    sent = _sent(fake_adb)
    assert sent == [f"dumpsys package {LAUNCHER}", "dumpsys input"]
    assert not any(c.startswith(("input ", "am ", "monkey", "cmd ")) for c in sent)
    # The package is the user's own input: it is not echoed, and the notice
    # (no app or window names) stays the plain one.
    assert LAUNCHER not in out
    assert "launcher" not in out
    if as_json:
        payload = json.loads(out)
        assert (payload["readable"], payload.get("note")) == (readable, note)
        assert payload["notice"] == NOTICE
        assert "package" not in payload
        return
    lines = out.rstrip("\n").split("\n")
    assert lines[0] == f"uiprobe diagnose tap-window: {NOTICE}; it read: dumpsys input"
    if readable:
        assert lines[1] == "result: readable"
        assert not any(line.startswith("note:") for line in lines)
    else:
        assert lines[1].startswith("result: not readable: line_format, rule samsung")
        assert lines[2] == f"note: {note}"


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    ("text", "rule", "note"),
    [
        (_flagged(OWN_ROW), "samsung_flags", NOTE_UID_UNREADABLE),
        (SAMSUNG36, None, NOTE_UID_UNREADABLE_ANY),
        (CASES["unknown_flag"][0], "unknown_flag", NOTE_UID_UNREADABLE_ANY),
    ],
    ids=["samsung_flags", "readable", "other_rule"],
)
def test_package_whose_uid_cannot_be_read_is_always_noted(
    fake_adb, capsys, text, rule, note, as_json
):
    fake_adb.add_device(SERIAL, model="SM_TEST")
    fake_adb.on_shell(SERIAL, "dumpsys input", text.encode())
    fake_adb.on_shell(SERIAL, f"dumpsys package {LAUNCHER}", b"")  # not installed
    argv = [*TAP_WINDOW, "--package", LAUNCHER, "--adb-server", fake_adb.address]
    code, out, err = invoke(capsys, *argv, *(["--json"] if as_json else []))
    assert (code, err) == (0, "")
    assert _sent(fake_adb) == [f"dumpsys package {LAUNCHER}", "dumpsys input"]
    assert LAUNCHER not in out
    if as_json:
        payload = json.loads(out)
        assert (payload["rule"], payload["note"]) == (rule, note)
        assert payload["notice"] == NOTICE
        return
    lines = out.rstrip("\n").split("\n")
    assert lines[1].startswith("result: readable" if rule is None else "result: not")
    assert lines[2] == f"note: {note}"
    assert sum(line.startswith("note:") for line in lines) == 1


def test_the_notes_name_the_test_that_was_applied():
    head = "a crawl accepts samsungFlags=0x1 on the target app's own window; "
    assert (
        head + "this window is not the given app's own window by the given owned test"
    ) == NOTE_TESTED
    assert (
        head + "this window is not the given app's own window by the crawl's"
        " owner uid and title test"
    ) == NOTE_CRAWL_TESTED
    assert NOTE_UID_UNREADABLE.endswith(
        "a crawl of it would refuse every tap with the tap window check on"
    )
    found = diagnose_tap_window(_flagged(OTHER_ROW), owned=lambda _window: False)
    assert (found.app_test, found.note) == (APP_TESTED, NOTE_TESTED)
    marked = dataclasses.replace(found, app_test=APP_CRAWL_TESTED)
    assert marked.note == NOTE_CRAWL_TESTED
    assert "crawl's owner uid" not in found.to_text()
    assert dataclasses.replace(marked, rule=None).note is None


def test_the_short_uid_note_is_the_given_sentence():
    assert NOTE_UID_UNREADABLE_ANY == (
        "the given app's uid could not be read (dumpsys package); no window"
        " counted as its own"
    )
    # Without --package, or with a uid that was read, no note but samsung_flags'.
    app = PackageUid(LAUNCHER_UID, shared=False)
    for owned in (None, _crawl_test(LAUNCHER, app)):
        assert diagnose_tap_window(SAMSUNG36, owned=owned).note is None


def test_the_crawl_and_diagnose_share_one_app_window_test(monkeypatch):
    # cli calls the very function the crawl's _Run.app_window calls.
    assert cli._app_window is crawl_driver._app_window
    calls: list[str] = []

    def spy(package: str, _window: InputWindow, _app: PackageUid) -> bool:
        calls.append(package)
        return True

    monkeypatch.setattr(crawl_driver, "_app_window", spy)
    run = _Run.__new__(_Run)
    run.package = LAUNCHER
    window = parse_input_windows(SAMSUNG36)
    assert not isinstance(window, str)
    assert run.app_window(window[0], PackageUid(LAUNCHER_UID, shared=False))
    assert calls == [LAUNCHER]


def test_package_runs_the_crawls_own_test(fake_adb, capsys, monkeypatch):
    calls: list[tuple[str, int]] = []
    original = crawl_driver._app_window

    def spy(package: str, window: InputWindow, app: PackageUid) -> bool:
        calls.append((package, app.uid))
        return original(package, window, app)

    # cli imported the function itself (the identity is checked above).
    monkeypatch.setattr(cli.diagnose, "_app_window", spy)
    _samsung_phone(fake_adb, OWN_ROW, LAUNCHER)
    argv = [*TAP_WINDOW, "--package", LAUNCHER, "--json"]
    code, out, _ = invoke(capsys, *argv, "--adb-server", fake_adb.address)
    assert code == 0
    assert json.loads(out)["readable"] is True
    assert calls
    assert set(calls) == {(LAUNCHER, LAUNCHER_UID)}


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "package",
    ["DEMO_PACKAGE", "com.", "com..DEMO_PACKAGE", "1com.DEMO_PACKAGE", "com.x" * 60],
)
def test_an_invalid_package_is_a_usage_error_not_echoed(
    fake_adb, capsys, package, as_json
):
    _samsung_phone(fake_adb, OWN_ROW, None)
    argv = [*TAP_WINDOW, "--package", package, "--adb-server", fake_adb.address]
    code, out, err = invoke(capsys, *argv, *(["--json"] if as_json else []))
    assert code == 2
    assert fake_adb.commands == []  # refused before the device is contacted
    assert "DEMO_PACKAGE" not in out + err
    assert package not in out + err
    shown = json.loads(out)["error"] if as_json else {"message": err}
    assert "--package is not a valid package name (it is not shown)" in str(shown)
    if as_json:
        assert shown["code"] == "usage_error"


def test_package_without_a_value_is_not_echoed(capsys):
    code, out, err = invoke(capsys, *TAP_WINDOW, "--json", "--package")
    assert code == 2
    message = json.loads(out)["error"]["message"]
    assert "[--show-device-text] [--package PKG]" in message
    assert err == ""


def test_device_diagnose_tap_window_checks_owned_first(fake_adb):
    _samsung_phone(fake_adb, OWN_ROW, None)
    device = Device(SERIAL, adb_server=fake_adb.address)
    with pytest.raises(TypeError, match="owned"):
        device.diagnose_tap_window(owned="x.y")  # ty: ignore[invalid-argument-type]
    assert _sent(fake_adb) == []
    app = PackageUid(LAUNCHER_UID, shared=False)
    found = device.diagnose_tap_window(owned=_crawl_test(LAUNCHER, app))
    assert (found.readable, found.app_test) == (True, APP_TESTED)
    assert _sent(fake_adb) == ["dumpsys input"]


def test_the_owned_parameters_of_the_diagnosis_resolve_at_run_time():
    for function in (diagnose_tap_window, Device.diagnose_tap_window):
        hints = typing.get_type_hints(function)
        assert "Callable" in str(hints["owned"])


def test_the_uid_unreadable_note_is_kept_by_replace():
    found = diagnose_tap_window(_flagged(OWN_ROW))
    marked = dataclasses.replace(found, app_test=APP_UID_UNREADABLE)
    assert marked.note == NOTE_UID_UNREADABLE
    assert marked.to_text().split("\n")[2] == f"note: {NOTE_UID_UNREADABLE}"


def _huge_rotation_after_top_row(text: str) -> str:
    top = _row(text, 0)
    return _sub(
        text, top, top + "\n      transform (ROT_" + "1" * 4301 + ") (IDENTITY)"
    )


def test_a_huge_rotation_in_one_rows_transform_is_refused_not_raised(fake_adb, capsys):
    # On main this list was line_format; reading the extra line's rotation
    # for the rule must not convert its digits.
    text = _huge_rotation_after_top_row(API24)
    assert parse_input_windows(text) == "line_format"
    assert read_window_at(text, (1, 1)) == "line_format"
    assert diagnose_tap_window(text).rule == "transform_mismatch"
    fake_adb.add_device(SERIAL)
    fake_adb.on_shell(SERIAL, "dumpsys input", text.encode())
    argv = ["diagnose", "tap-window", "--json", "--adb-server", fake_adb.address]
    code, out, _ = invoke(capsys, *argv)
    assert (code, json.loads(out)["rule"]) == (0, "transform_mismatch")


def _all_rows_huge_rotation() -> str:
    top = _row(API36, 0)
    return _sub(
        API36,
        f"{top}\n{IDENTITY}",
        f"{top}\n{IDENTITY}".replace("ROT_0", "ROT_" + "1" * 4301),
    )


# Digit runs too long for int (Python's default limit is 4300): main refuses
# them (rotation); the rule names it.
TOO_LONG = {
    "row_transform_rotation": _all_rows_huge_rotation(),
    "viewport_display_id": _sub(
        API36,
        "Viewport INTERNAL: displayId=0",
        "Viewport INTERNAL: displayId=" + "1" * 4301,
    ),
    "display_orientation": _sub(
        API32, "displayOrientation=0", "displayOrientation=" + "1" * 4301
    ),
}


@pytest.mark.parametrize("case", sorted(TOO_LONG))
def test_digit_runs_too_long_to_read_are_refused(case):
    text = TOO_LONG[case]
    found = read_window_at(text, (1, 1))
    assert isinstance(found, Unreadable)
    assert (found, found.rule) == ("rotation", "number_too_long")
    report = diagnose_tap_window(text)
    assert (report.reason, report.rule) == ("rotation", "number_too_long")
    assert "1111" not in report.to_text()


def test_a_failure_after_the_dispatcher_is_numbered_from_the_top():
    text = API24.rstrip("\n") + "\nInput Manager Policy State:\n  ownerUid=1\n"
    lines = text.split("\n")
    found = diagnose_tap_window(text)
    assert found.rule == "stray_owner"
    stray = lines.index("  ownerUid=1") + 1
    assert (found.part, found.line) == ("output", stray)
    # Its context is the lines after the dispatcher's part, not the list.
    after = lines.index("Input Manager Policy State:") + 1
    assert [shape.number for shape in found.lines] == list(range(after, len(lines) + 1))


def test_a_row_of_no_recognised_form_is_counted():
    text = "Input Dispatcher State:\n  Windows:\n    0:name=x\n  Connections:\n"
    found = diagnose_tap_window(text, show_device_text=True)
    assert found.rule == "row_shape"
    assert [(f.rows, f.recognised, f.fields) for f in found.forms] == [(1, False, ())]
    assert "1 rows of no recognised form" in found.to_text()
    payload = json.loads(json.dumps(found.to_dict()))
    assert payload["forms"][0]["recognised"] is False
    assert "forms" not in json.loads(json.dumps(diagnose_tap_window(text).to_dict()))


# --- round 5: numbers, spellings of a boundary, Viewport lines read ---

FAKE_SECTION = (
    "T\n  Display: 7319\n    Windows:\n"
    "      0: name=x, id=1, displayId=7319, inputConfig=0x0, alpha=1,"
    " frame=[0,0][1,1], globalScale=1, applicationInfo.name=,"
    " applicationInfo.token=<null>, touchableRegion=<empty>, ownerPid=1, ownerUid=1\n"
    "      transform (ROT_0) (IDENTITY)\n  MonitoringChannels:\n  X"
)
FAKE_VIEWPORT = (
    "T\n" + API24.split("\n")[1].replace("orientation=0", "orientation=3") + "\n    X"
)
DOUBLE_SPACE = (
    "applicationInfo.name=X, applicationInfo.token=y, trustedOverlay=true, tail=Y,"
    "  applicationInfo.token=<null>,"
)


def test_a_display_section_inside_a_name_shows_no_number():
    text = _sub(
        API36,
        "  DispatchEnabled: true",
        f"  DispatchEnabled: true\n  FocusedWindow: name='{FAKE_SECTION}'",
    )
    found = diagnose_tap_window(text)
    assert "7319" not in found.to_text()
    assert "7319" not in json.dumps(found.to_dict())


def test_a_viewport_line_inside_a_name_shows_no_orientation():
    text = _edit_row(
        API24, 0, "name='Window{b24c1c0 u0 StatusBar}'", f"name='{FAKE_VIEWPORT}'"
    )
    found = diagnose_tap_window(text)
    assert found.reason is not None
    assert "orientation=3" not in found.to_text()
    assert all(s.kind != "viewport" for s in found.lines)


def test_a_boundary_spelled_twice_refuses_the_cut():
    for token in ("  applicationInfo.token=", " applicationInfo.token ="):
        spelled = DOUBLE_SPACE.replace("  applicationInfo.token=", token)
        text = _edit_row(
            CASES["unknown_flag"][0],
            2,
            "applicationInfo.name=, applicationInfo.token=<null>,",
            spelled,
        )
        assert window_line_fields(_row(text, 2)) is None, token
        found = diagnose_tap_window(text, show_device_text=True)
        assert found.rule == "unknown_flag"  # row 2 was still read into a window
        row = next(s for s in found.lines if s.tokens[0] == "row:2")
        assert row.tokens == ("row:2", _rest(_row(text, 2)))
        assert any(not form.recognised for form in found.forms)


def test_a_quoted_value_is_read_whole_before_fields():
    note = 'vendorNote="x, trustedOverlay=true, tail=y"'
    row = _row(API36, 3).replace(", alpha=1,", f", {note}, alpha=1,")
    cut = window_line_fields(row)
    assert cut is not None
    assert ("vendorNote", '"x, trustedOverlay=true, tail=y"') in cut.fields
    tokens = shape_line(row, accepted=True)[1]
    assert "<unknown_key:10>=<quoted:30>" in tokens
    assert not any("trustedOverlay" in token for token in tokens)
    for bad in ('vendorNote="x, a=b', 'vendorNote="x" tail', "vendorNote=a'b"):
        assert window_line_fields(row.replace(note, bad)) is None


def test_the_forms_use_the_readers_transform_pattern():
    text = (
        "Input Dispatcher State:\n  Windows:\n    0: name=x\n"
        "    transform (broken\n  Connections:\n"
    )
    found = diagnose_tap_window(text, show_device_text=True)
    assert [form.transform for form in found.forms] == [False]


def test_changed_values_change_digits_too():
    assert _changed("T 7319 0 orientation=3") != "T 7319 0 orientation=3"
    assert re.sub(r"[1-9]", "1", _changed("7319 0 3")) == "1111 0 1"
    assert len(set(_changed("7 7 7").split())) == 1  # equal numbers stay equal


CHOSEN_CASES |= {
    "display_section_in_a_name": lambda c: _sub(
        API36,
        "  DispatchEnabled: true",
        "  DispatchEnabled: true\n  FocusedWindow: name='" + c(FAKE_SECTION) + "'",
    ),
    "display_section_in_a_title": lambda c: _edit_row(
        API36, 3, "name=a8ea035 Taskbar,", "name=" + c(FAKE_SECTION) + ","
    ),
    "viewport_in_a_title": lambda c: _edit_row(
        API24,
        0,
        "name='Window{b24c1c0 u0 StatusBar}'",
        "name='" + c(FAKE_VIEWPORT) + "'",
    ),
    "app_name_double_space_token": lambda c: _edit_row(
        CASES["unknown_flag"][0],
        2,
        "applicationInfo.name=, applicationInfo.token=<null>,",
        "applicationInfo.name="
        + c("X, applicationInfo.token=y, trustedOverlay=true, tail=Y")
        + ",  applicationInfo.token=<null>,",
    ),
    "double_quoted_value": lambda c: _edit_row(
        API36,
        3,
        ", alpha=1,",
        ', vendorNote="' + c("x, trustedOverlay=true, tail=y") + '", alpha=1,',
    ),
}


# --- round 6: the default report holds no device characters ---

ZERO, ONE, THREE = chr(0x660), chr(0x661), chr(0x663)  # Arabic-Indic digits
_POOL = "abcXYZ019" + ZERO + ONE + THREE + "-+'\",=:|_.[]{}<>"


def _any_change(seed: int) -> Callable[[str], str]:
    """A function that replaces about half the characters of a text (not
    its whitespace) with others: letters, digits, Unicode digits, signs,
    quotes and delimiters; the length stays."""

    def change(text: str) -> str:
        pick = random.Random(seed * 7919 + len(text))  # noqa: S311 - a fixed fuzz
        return "".join(
            c if c.isspace() or pick.random() < 0.5 else pick.choice(_POOL)
            for c in text
        )

    return change


def _verdict(text: str, display: int) -> tuple[object, ...]:
    found = explain_input_windows(text, display)
    return (found.reason, found.rule, found.line)


# The reviewers' inputs of round 6 (astra-diag-5), as templates.
ONE_ROW = (
    "Input Dispatcher State:\n  Windows:\n    0: name={}, displayId=0,"
    " inputConfig=0x0, ownerUid=1, touchableRegion=<empty>, displayOrientation=1\n"
    "  Connections:\n"
)
REVIEW_6: dict[str, Callable[[Callable[[str], str]], str]] = {
    "bare_title_with_id": lambda c: ONE_ROW.format(c("Title, id=42")),
    "app_name_spaced_token": lambda c: _edit_row(
        CASES["unknown_flag"][0],
        2,
        "applicationInfo.name=, applicationInfo.token=<null>,",
        "applicationInfo.name="
        + c("X, applicationInfo.token=y, trustedOverlay=true, tail=Y")
        + ", applicationInfo.token =<null>,",
    ),
    "quoted_input_features": lambda c: _edit_row(
        _edit_row(API31, 3, "flags=", "flags=NEW_FLAG | "),
        2,
        ", ownerPid=",
        ', inputFeatures="' + c("x | NO_INPUT_CHANNEL | y") + '", ownerPid=',
    ),
    "unicode_zero_display": lambda c: (
        "Input Dispatcher State:\n  Display: 3\n    Windows:\n  Display: 3\n"
        "    Windows:\n  Display: " + c(ZERO) + "\n    Windows:\n"
    ),
    "display_sign": lambda c: _sub(
        API36, "  Display: 0\n", "  Display: " + c("-7") + "\n"
    ),
    "viewport_word": lambda c: (
        '  Device name: "' + c("viewport") + '"\n' + CASES["viewport_missing"][0]
    ),
    "multiline_row_in_title": lambda c: _edit_row(
        API36,
        3,
        "name=a8ea035 Taskbar,",
        "name=" + c("T\n      3: name=x, id=1, displayId=0, inputConfig=SPY") + ",",
    ),
}


# --- a closed vocabulary for the default report ---

REASONS = frozenset(RULES)
RULE_NAMES = frozenset(rule for rules in RULES.values() for rule in rules)
PART_NAMES = frozenset({"dispatcher", "input_reader", "output"})


def _alt(words: Iterable[str]) -> str:
    return "|".join(re.escape(word) for word in sorted(words))


_TEXT_LINES = (
    re.compile(
        re.escape(f"uiprobe diagnose tap-window: {NOTICE}; it read: dumpsys input")
    ),
    re.compile(
        rf"result: readable|result: not readable: (?:{_alt(REASONS)}),"
        rf" rule (?:{_alt(RULE_NAMES)})"
        rf"(?: at line \d+ of the (?:{_alt(PART_NAMES)}) part)?"
    ),
    re.compile(re.escape(f"note: {NOTE}")),
    re.compile(
        r"the display's list: (?:none taken|(?:the list of every display|its"
        r" Display section), (?:\d+\+? windows read|not reached)"
        r"(?:, the check refused here)?)"
    ),
    re.compile(r"lines \(number, kind, length\):"),
    re.compile(rf"[> ] \d+ (?:{_alt(LINE_KINDS)}) \d+\+?"),
)


def _closed_text(text: str) -> None:
    """Every line of a default text report is one of the fixed forms, built
    from fixed words, rule names, line kinds and numbers."""
    for line in text.split("\n"):
        assert any(form.fullmatch(line) for form in _TEXT_LINES), line


def _count_value(value: object, *, capped: bool) -> None:
    assert isinstance(value, int)
    assert not isinstance(value, bool)
    assert value >= 0
    if capped:
        assert value <= CAP


def _closed_json(payload: dict[str, object]) -> None:
    """A default JSON report holds only fixed keys, fixed strings and
    numbers uiprobe counted."""
    noted = payload["rule"] == "samsung_flags"
    assert set(payload) == {
        "schema",
        "notice",
        "show_device_text",
        "command",
        "readable",
        "reason",
        "rule",
        "part",
        "line",
        "windows_read",
        "display_list",
        "lines",
        *(["note"] if noted else []),
    }
    if noted:
        assert payload["note"] == NOTE
    assert (payload["schema"], payload["notice"], payload["command"]) == (
        SCHEMA,
        NOTICE,
        "dumpsys input",
    )
    assert payload["show_device_text"] is False
    assert isinstance(payload["readable"], bool)
    assert payload["reason"] is None or payload["reason"] in REASONS
    assert payload["rule"] is None or payload["rule"] in RULE_NAMES
    assert payload["part"] is None or payload["part"] in PART_NAMES
    if payload["line"] is not None:
        _count_value(payload["line"], capped=False)
    _count_value(payload["windows_read"], capped=True)
    chosen = payload["display_list"]
    if chosen is not None:
        assert isinstance(chosen, dict)
        assert set(chosen) == {"per_display", "reached", "read", "failed"}
        for key in ("per_display", "reached", "failed"):
            assert isinstance(chosen[key], bool)
        _count_value(chosen["read"], capped=True)
    lines = payload["lines"]
    assert isinstance(lines, list)
    for entry in lines:
        assert isinstance(entry, dict)
        assert set(entry) == {"number", "failing", "kind", "length"}
        _count_value(entry["number"], capped=False)
        assert isinstance(entry["failing"], bool)
        assert entry["kind"] in LINE_KINDS
        _count_value(entry["length"], capped=True)


def _closed_object(found: TapWindowDiagnosis) -> None:
    """The default Python object holds the same and nothing more."""
    assert found.reason is None or found.reason in REASONS
    assert found.rule is None or found.rule in RULE_NAMES
    assert found.part is None or found.part in PART_NAMES
    assert found.show_device_text is False
    assert found.app_test is None
    assert found.note == (NOTE if found.rule == "samsung_flags" else None)
    assert found.forms == ()
    assert len(found.lists) <= 1
    for item in found.lists:
        assert (item.position, item.asked) == (None, True)
        _count_value(item.read, capped=False)
    for shape in found.lines:
        assert (shape.indent, shape.tokens) == (None, ())
        assert shape.kind in LINE_KINDS
        _count_value(shape.number, capped=False)
        _count_value(shape.length, capped=False)


def _closed(text: str, display: int = 0) -> None:
    found = diagnose_tap_window(text, display_id=display)
    _closed_object(found)
    _closed_text(found.to_text())
    _closed_json(json.loads(json.dumps(found.to_dict())))


def _corpus() -> Sequence[tuple[str, int]]:
    """Every rule case, capture, hostile input and reviewer's input, with the
    values an app chooses as they are and changed."""
    texts = [*CASES.values(), *((text, 0) for text in CAPTURES.values())]
    texts += [(text, 0) for text in AMBIGUOUS.values()]
    for build, display in HOSTILE.values():
        texts += [(build(_same), display), (build(_changed), display)]
    for build in (*CHOSEN_CASES.values(), *REVIEW_6.values()):
        texts += [(build(_same), 0), (build(_changed), 0)]
        texts += [(build(_any_change(seed)), 0) for seed in range(3)]
    return texts


def test_the_default_report_uses_a_closed_vocabulary():
    corpus = _corpus()
    assert len(corpus) > 200
    for text, display in corpus:
        _closed(text, display)


@pytest.mark.parametrize("seed", range(4))
def test_mutated_outputs_keep_the_closed_vocabulary(seed):
    pick = random.Random(seed)  # noqa: S311 - a fixed test fuzz
    corpus = _corpus()
    for _ in range(150):
        text, display = pick.choice(corpus)
        for _ in range(pick.randint(1, 4)):
            at = pick.randrange(len(text) + 1)
            if pick.random() < 0.5:
                text = text[:at] + pick.choice(SNIPPETS) + text[at:]
            else:
                text = text[:at] + text[at + pick.randint(1, 30) :]
        _closed(text, display)
        shown = diagnose_tap_window(text, show_device_text=True, display_id=display)
        shown.to_text()  # never raises
        json.dumps(shown.to_dict())


def test_a_list_the_reader_entered_is_reached():
    text = "Input Dispatcher State:\n  Windows:\n    orphan\n  Connections:\n"
    found = diagnose_tap_window(text)
    assert (found.reason, found.rule) == ("line_format", "orphan_line")
    [item] = found.lists
    assert (item.reached, item.read, item.failed) == (True, 0, True)
    assert "the list of every display, 0 windows read, the check refused here" in (
        found.to_text()
    )
