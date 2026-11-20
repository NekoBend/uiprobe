"""Tests for ``uiprobe diagnose tap-window``: the rule that refused, and no leak.

Every rule of `uiprobe.inputwindows.RULES` has a case below, made from the
emulator captures in tests/data/inputwindows by one edit. The same cases are
then filled with identifier-like values (a serial, IMEI-like numbers, MAC
and IP addresses, accounts, phone numbers, package names, window titles,
display ids, input device names) in window names, field values, unknown
keys, unknown flags, headings and other lines, and no output may hold any of
them.
"""

from __future__ import annotations

import copy
import json
import pickle
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from uiprobe import cli
from uiprobe.crawl_driver import CrawlResult
from uiprobe.device import Device
from uiprobe.inputwindows import (
    RULES,
    Unreadable,
    explain_input_windows,
    parse_input_windows,
    read_window_at,
)
from uiprobe.windowdiag import (
    KNOWN_FLAGS,
    KNOWN_KEYS,
    KNOWN_LABELS,
    KNOWN_LINES,
    KNOWN_VALUES,
    LINE_KINDS,
    NOTICE,
    NOTICE_SHOWN,
    SCHEMA,
    TRANSFORM_WORDS,
    diagnose_tap_window,
    shape_line,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tests.fake_adb_server import FakeAdbServer

DATA = Path(__file__).parent / "data" / "inputwindows"
SERIAL = "emulator-5554"


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
    assert [item.rows for item in found.lists] == [
        sum(
            1 for line in CAPTURES[name].split("\n") if re.match(r"\s*\d+: name=", line)
        )
    ]
    assert sum(form.rows for form in found.forms) == found.lists[0].rows


def test_the_failing_line_is_numbered_from_the_dispatcher_header():
    text, _ = CASES["unknown_flag"]
    lines = text.split("\n")
    header = lines.index("Input Dispatcher State:")
    at = lines.index(_row(text, 3))
    found = diagnose_tap_window(text)
    assert (found.part, found.line) == ("dispatcher", at - header + 1)
    assert found.line is not None
    failing = [shape for shape in found.lines if shape.failing]
    assert [shape.number for shape in failing] == [found.line]
    assert [shape.number for shape in found.lines] == list(
        range(found.line - 2, found.line + 3)
    )
    assert failing[0].kind == "window_row"
    assert "inputConfig=NOT_FOCUSABLE|TRUSTED_OVERLAY|SLIPPERY|<unknown_flag:7>" in (
        failing[0].tokens
    )
    assert [item.failed for item in found.lists] == [True]


def test_a_viewport_line_is_numbered_from_the_top():
    found = diagnose_tap_window(CASES["viewport_malformed"][0])
    assert (found.reason, found.rule, found.part, found.line) == (
        "rotation",
        "viewport_malformed",
        "input_reader",
        2,
    )
    assert [s.number for s in found.lines] == [1, 2, 3]  # not into the dispatcher
    assert found.lines[1].tokens[0] == "Viewport INTERNAL:"
    assert "uniqueId=<text:25>" in found.lines[1].tokens


def test_without_one_line_to_blame_the_outline_is_shown():
    found = diagnose_tap_window(CASES["display_missing"][0])
    assert (found.rule, found.line) == ("display_missing", None)
    tokens = [shape.tokens for shape in found.lines]
    assert tokens[0] == ("Input Dispatcher State:",)
    assert ("Display:", "<num>") in tokens
    assert [item.display for item in found.lists] == [1]


def test_lists_of_several_displays_are_counted():
    second = (
        "  Display: 2\n    Windows:\n      0: name=x, id=1, displayId=2,"
        " inputConfig=0x0, alpha=1, frame=[0,0][1,1], globalScale=1,"
        " applicationInfo.name=, applicationInfo.token=<null>,"
        " touchableRegion=Q, ownerPid=1, ownerUid=1\n"
        "      transform (ROT_0) (IDENTITY)\n"
    )
    text = _sub(API36, "  Display: 0\n", second + "  Display: 0\n")
    found = diagnose_tap_window(text)
    assert (found.reason, found.rule) == ("region_format", "region_format")
    assert [(i.position, i.display, i.rows, i.failed) for i in found.lists] == [
        (1, 2, 1, True),
        (2, 0, 13, False),
    ]


def test_shapes_of_known_lines():
    row = _row(API24, 0)
    # Outside a found list a row shows no known value words, only tags.
    assert "paused=<text:5>" in shape_line(row)[1]
    assert shape_line(row, in_list=True) == (
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
    assert shape_line("  Display: 0") == ("heading", ["Display:", "<num>"])
    assert shape_line("    Windows:") == ("heading", ["Windows:"])
    assert shape_line(" transform (ROT_90) (ROTATE TRANSLATE)") == (
        "transform",
        ["transform", "ROT_90", "ROTATE", "TRANSLATE"],
    )
    assert shape_line("  -1.0000  0.0000  320.0000") == (
        "matrix_row",
        ["<dec>", "<dec>", "<dec>"],
    )
    assert shape_line("  Some vendor line") == ("other", ["<text:16>"])
    assert shape_line("  DispatchEnabled: true") == (
        "heading",
        ["DispatchEnabled:", "true"],
    )
    assert shape_line("  vendorKey=abc, flags=NOT_FOCUSABLE | Odd") == (
        "fields",
        ["<unknown_key:9>=<text:3>", "flags=NOT_FOCUSABLE|<unknown_flag:3>"],
    )
    assert shape_line("") == ("blank", [])


def test_the_text_report_of_a_failure():
    text, _ = CASES["unknown_flag"]
    report = diagnose_tap_window(text).to_text().split("\n")
    assert report[0] == (
        "uiprobe diagnose tap-window: this output contains no device serial,"
        " app or window names, coordinates, accounts or network values;"
        " it read: dumpsys input"
    )
    assert report[1] == (
        "result: not readable: line_format, rule unknown_flag at line 26 of the"
        " dispatcher part"
    )
    assert report[2:4] == [
        "lists:",
        "  list 1 (display 0): 13 window rows, the check refused here",
    ]
    assert report[4] == "lines (number, kind, indent, length: tokens):"
    assert report[7].startswith("> 26 window_row 6 ")
    assert report[7].endswith(
        ": row:3 name=<text:15> id=<num> displayId=<num>"
        " inputConfig=NOT_FOCUSABLE|TRUSTED_OVERLAY|SLIPPERY|<unknown_flag:7>"
        " alpha=<num> frame=<rect> globalScale=<num> applicationInfo.name=<blank>"
        " applicationInfo.token=<null> touchableRegion=<empty> ownerPid=<num>"
        " ownerUid=<num> dispatchingTimeout=<ms> token=<hex>"
        " touchOcclusionMode=BLOCK_UNTRUSTED"
    )


def test_the_json_report_of_a_readable_list():
    payload = diagnose_tap_window(API24).to_dict()
    assert payload == {
        "schema": SCHEMA,
        "notice": NOTICE,
        "command": "dumpsys input",
        "show_flag_names": False,
        "readable": True,
        "reason": None,
        "rule": None,
        "part": None,
        "line": None,
        "lists": [
            {
                "position": 1,
                "per_display": False,
                "display": None,
                "display_digits": 0,
                "rows": 7,
                "failed": False,
            }
        ],
        "lines": [],
        "forms": [
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
        ],
    }


# --- no device values in any output ---

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
# Parts that must not appear either (a match of a part would leak part of one).
PARTS = (
    "R5CT",
    "123ABC",
    "3569380",
    "4901542",
    "5e:44",
    "192.168",
    "10.77",
    "fe80",
    "890a",
    "someone",
    "example",
    "5555-0199",
    "secretbank",
    "wallet",
    "Alice",
    "Transfer",
    "46198272",
    "touchscreen",
    "status=OK",
)
# A single-line title with a field-like fragment that holds no mark ending a
# name, so the verdict does not change and the fragment stays in the name.
TITLE = " ".join(SECRETS[:3] + SECRETS[6:10]) + ", status=OK"
EXTRA = (
    ", sem_imei=356938035643809, macAddress=02:00:5e:44:55:66,"
    " ip=192.168.10.77, ipv6=fe80::1ff:fe23:4567:890a, account=someone@example.com,"
    " phone=+81-90-5555-0199, R5CT123ABC=490154203237518, owner=Transfer to Alice"
)
HEAD = (
    "  R5CT123ABC: someone@example.com\n"
    "  Device 356938035643809 serial=R5CT123ABC, name=sec_touchscreen\n"
    "  wlan0 192.168.10.77 02:00:5e:44:55:66 fe80::1ff:fe23:4567:890a\n"
    "  Account: Transfer to Alice +81-90-5555-0199 com.secretbank.wallet\n"
)
READER = (
    "  Device 3: sec_touchscreen\n"
    "    Identifier: bus=0x0018, vendor=0x0000, uniqueId=4619827259835644672,"
    " descriptor=R5CT123ABC\n"
    "  Viewport VIRTUAL: displayId=9, uniqueId=virtual:com.secretbank.wallet,"
    " port=<none>, orientation=0\n"
)
_NAME = re.compile(r"^(\s*\d+: name=)('?)(.*?)\2(, (?:id=\d+, )?displayId=)")


def _hostile(text: str) -> str:
    """`text` with secrets in names, values, unknown keys and other lines,
    in ways that do not change what the check concludes."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if _NAME.match(line):
            line = _NAME.sub(rf"\g<1>\g<2>{TITLE}\g<2>\g<4>", line)
            line = re.sub(
                r"applicationInfo\.name=[^,]*, applicationInfo\.token=",
                "applicationInfo.name=com.secretbank.wallet R5CT123ABC, status=OK,"
                " applicationInfo.token=",
                line,
            )
            lines[i] = line + EXTRA
        elif line.strip() == "Input Dispatcher State:":
            lines[i] = line + "\n" + HEAD.rstrip("\n")
        elif line.strip() == "INPUT MANAGER (dumpsys input)":
            lines[i] = line + "\n" + READER.rstrip("\n")
    filled = "\n".join(lines)
    return filled if "INPUT MANAGER" in text else READER + filled


def _secret_flags(text: str) -> str:
    """`text` with the unknown flag of the unknown_flag case replaced by
    secrets, one of them an upper-case word."""
    return _sub(
        text, "| SEM_FOO", "| R5CT123ABC | someone@example.com | sec_touchscreen"
    )


HOSTILE: dict[str, tuple[str, int]] = {
    rule: (_hostile(text), display) for rule, (text, display) in CASES.items()
}
HOSTILE["unknown_flag"] = (_secret_flags(HOSTILE["unknown_flag"][0]), 0)
HOSTILE |= {f"readable_{name}": (_hostile(text), 0) for name, text in CAPTURES.items()}


def _allowed_words() -> set[str]:
    words: set[str] = set()
    vocabularies = (
        KNOWN_KEYS,
        KNOWN_LABELS,
        KNOWN_LINES,
        KNOWN_VALUES,
        KNOWN_FLAGS,
        TRANSFORM_WORDS,
        LINE_KINDS,
        set(RULES),
        set(REASON_OF),
    )
    for vocabulary in vocabularies:
        for item in vocabulary:
            words.update(_pieces(item))
    template = (
        f"{NOTICE} {NOTICE_SHOWN} per_display display_digits show_flag_names"
        " output uiprobe diagnose tap-window it read dumpsys input result"
        " readable not rule at line of the dispatcher input_reader part lists"
        " list display every window rows check refused here none found lines"
        " number kind indent length tokens forms quoted bare name transform no"
        " fields display schema notice command reason position failed failing"
        " uiprobe.diagnose_tap_window/1 <unknown_key> > True False recognised 0x0"
        " rows of no recognised form the name's end cannot be told"
    )
    words.update(_pieces(template))
    return words


def _pieces(text: str) -> list[str]:
    return [p for p in re.split(r"[\s=|,;:()]+", text) if p]


ALLOWED = _allowed_words()
TAG = re.compile(r"<[a-z_]+>|<[a-z_]+|\d+\+?>|row|-?\d+\+?")


def _check_text(text: str, *, shown_flags: frozenset[str] = frozenset()) -> None:
    """No secret in `text`, no digit run longer than four, and every piece
    of it a known word, a tag or a number."""
    for secret in (*SECRETS, *PARTS):
        if secret in shown_flags:
            continue
        assert secret not in text.replace("<unknown_flag:R5CT123ABC>", ""), secret
    runs = [len(run) for run in re.findall(r"\d+", text)]
    assert max(runs, default=0) <= 4, text
    for piece in _pieces(text):
        if piece in ALLOWED or TAG.fullmatch(piece):
            continue
        assert piece in shown_flags or piece.removesuffix(">") in shown_flags, piece


def _strings(value: object) -> Iterator[str]:
    """Every string (keys too) and number in a JSON value, as text."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


@pytest.mark.parametrize("case", sorted(HOSTILE))
@pytest.mark.parametrize("show", [False, True], ids=["plain", "show_flag_names"])
def test_no_device_value_reaches_the_report(case, show):
    text, display = HOSTILE[case]
    for secret in SECRETS:
        assert secret in text  # the case holds every one of them
    expected = REASON_OF.get(case)
    found = diagnose_tap_window(text, show_flag_names=show, display_id=display)
    if expected is not None:
        assert found.rule == case  # the secrets did not change the verdict
    else:
        assert found.readable
    shown = (
        frozenset({"R5CT123ABC"}) if show and case == "unknown_flag" else frozenset()
    )
    _check_text(found.to_text(), shown_flags=shown)
    payload = json.loads(json.dumps(found.to_dict()))
    for item in _strings(payload):
        _check_text(item, shown_flags=shown)


def test_show_flag_names_shows_only_upper_case_words():
    text, _ = HOSTILE["unknown_flag"]
    plain = diagnose_tap_window(text).to_text()
    shown = diagnose_tap_window(text, show_flag_names=True).to_text()
    assert "R5CT123ABC" not in plain
    assert "|<unknown_flag:10>|<unknown_flag:19>|<unknown_flag:15>" in plain
    assert "|<unknown_flag:R5CT123ABC>|<unknown_flag:19>|<unknown_flag:15>" in shown
    assert shown.count("R5CT123ABC") == 1  # only the flag, not the title or keys
    row = _row(API36, 3)
    for name, visible in (("A" * 48, True), ("A" * 49, False), ("Ab", False)):
        line = row.replace("| SLIPPERY", f"| SLIPPERY | {name}")
        kind, tokens = shape_line(line, show_flag_names=True, in_list=True)
        assert kind == "window_row"
        assert (f"|<unknown_flag:{name}>" in " ".join(tokens)) is visible, tokens
    # Outside a window line's own flags fields no name is shown.
    assert shape_line("  flags=SECRETWORD", show_flag_names=True) == (
        "fields",
        ["flags=<unknown_flag:10>"],
    )


# --- the command line ---


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def phone(fake_adb: FakeAdbServer) -> FakeAdbServer:
    fake_adb.add_device(SERIAL, model="Pixel_8")
    text, _ = HOSTILE["unknown_flag"]
    fake_adb.on_shell(SERIAL, "dumpsys input", text.encode())
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
    _check_text(out)
    assert SERIAL not in out


def test_the_command_prints_json(phone, capsys):
    argv = ["diagnose", "tap-window", "--json", "--show-flag-names"]
    code, out, _ = invoke(capsys, *argv, "--adb-server", phone.address)
    assert code == 0
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.diagnose_tap_window/1"
    assert (payload["notice"], payload["show_flag_names"]) == (NOTICE_SHOWN, True)
    assert (payload["readable"], payload["reason"], payload["rule"]) == (
        False,
        "line_format",
        "unknown_flag",
    )
    assert payload["part"] == "dispatcher"
    failing = [line for line in payload["lines"] if line["failing"]]
    assert [line["number"] for line in failing] == [payload["line"]]
    for item in _strings(payload):
        _check_text(item, shown_flags=frozenset({"R5CT123ABC"}))


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


# --- round 2 ---


def test_show_flag_names_has_its_own_notice():
    text, _ = HOSTILE["unknown_flag"]
    shown = diagnose_tap_window(text, show_flag_names=True)
    plain = diagnose_tap_window(text)
    first = shown.to_text().split("\n")[0]
    assert (
        first == f"uiprobe diagnose tap-window: {NOTICE_SHOWN}; it read: dumpsys input"
    )
    assert f"{NOTICE};" not in shown.to_text()  # the plain promise is not made
    assert "verbatim" in NOTICE_SHOWN
    assert shown.to_dict()["notice"] == NOTICE_SHOWN
    assert plain.to_dict()["notice"] == NOTICE
    assert plain.to_text().startswith(f"uiprobe diagnose tap-window: {NOTICE};")


# Window lines whose name or applicationInfo.name holds a flags field: the
# reader refuses each one (a read field twice), and no mode shows the word.
FAKED_FLAGS = {
    "bare_title_input_config": _edit_row(
        API36, 3, "name=a8ea035 Taskbar,", "name=Bad, inputConfig=SECRETWORD,"
    ),
    "quoted_title_flags": _edit_row(
        API24,
        0,
        "name='Window{b24c1c0 u0 StatusBar}'",
        "name='T, flags=SECRETWORD, z=1'",
    ),
    "app_name_input_config": _edit_row(
        API36,
        3,
        "applicationInfo.name=,",
        "applicationInfo.name=X, inputConfig=SECRETWORD | NOT_VISIBLE,",
    ),
    "app_name_flags": _edit_row(
        _capture("api31"),
        0,
        "applicationInfo.name=,",
        "applicationInfo.name=X, flags=SECRETWORD,",
    ),
}


@pytest.mark.parametrize("case", sorted(FAKED_FLAGS))
def test_flag_names_show_only_from_the_real_flags_fields(case):
    text = FAKED_FLAGS[case]
    found = diagnose_tap_window(text, show_flag_names=True)
    assert found.rule == "field_twice"
    assert any(shape.failing for shape in found.lines)
    assert "SECRETWORD" not in found.to_text()
    assert "SECRETWORD" not in json.dumps(found.to_dict())


def test_a_name_that_fakes_a_whole_window_line_can_show_its_word():
    # Why the mode has its own notice: a name holding a line break can fake a
    # whole window line, which the reader cuts like any other.
    fake = "      3: name=x, id=1, displayId=0, inputConfig=SECRETWORD"
    text = _sub(API36, _row(API36, 3), _row(API36, 3) + "\n" + fake)
    found = diagnose_tap_window(text, show_flag_names=True)
    assert "<unknown_flag:SECRETWORD>" in found.to_text()
    assert found.to_text().startswith(f"uiprobe diagnose tap-window: {NOTICE_SHOWN}")
    assert "SECRETWORD" not in diagnose_tap_window(text).to_text()


def test_a_name_is_only_its_length_even_when_it_is_a_known_word():
    text = _edit_row(CASES["unknown_flag"][0], 3, "name=a8ea035 Taskbar,", "name=OK,")
    found = diagnose_tap_window(text)
    failing = next(shape for shape in found.lines if shape.failing)
    assert failing.tokens[:2] == ("row:3", "name=<text:2>")
    assert "OK" not in failing.tokens
    for word in ("true", "false", "None", "BLOCK_UNTRUSTED"):
        line = _row(API36, 3).replace("name=a8ea035 Taskbar,", f"name={word},")
        assert shape_line(line)[1][1] == f"name=<text:{len(word)}>"
    line = _row(API36, 3).replace(
        "applicationInfo.name=,", "applicationInfo.name=true, x=OK,"
    )
    assert "applicationInfo.name=<text:10>" in shape_line(line)[1]


def test_a_quoted_name_is_cut_where_the_reader_cuts_it():
    text = _edit_row(
        API24, 0, "name='Window{b24c1c0 u0 StatusBar}'", "name='My title, vendor=demo'"
    )
    found = diagnose_tap_window(text)
    assert found.readable  # the reader takes the whole quoted name
    tokens = shape_line(_row(text, 0))[1]
    assert tokens[1] == "name=<quoted:21>"
    assert not any(token.startswith("<unknown_key") for token in tokens)
    assert {(form.quoted, form.rows) for form in found.forms} == {(True, 7)}


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


def test_long_row_numbers_are_not_converted():
    # 4301 digits: past what int() converts; the reader says row_shape.
    long_row = _edit_row(API36, 3, "3: name=", "1" * 4301 + ": name=")
    found = diagnose_tap_window(long_row)
    assert (found.reason, found.rule) == ("line_format", "row_shape")
    failing = next(shape for shape in found.lines if shape.failing)
    assert failing.tokens[0] == "row:<num:4301>"
    assert shape_line("  000000000001234: name=x") == (
        "window_row",
        ["row:<num:15>", "<text:6>"],  # the reader cannot cut it: no fields
    )
    assert shape_line("  0012: name=x")[1][0] == "row:12"


def test_the_command_reports_a_long_row_number(fake_adb, capsys):
    fake_adb.add_device(SERIAL)
    text = _edit_row(API36, 3, "3: name=", "1" * 4301 + ": name=")
    fake_adb.on_shell(SERIAL, "dumpsys input", text.encode())
    argv = ["diagnose", "tap-window", "--json", "--adb-server", fake_adb.address]
    code, out, _ = invoke(capsys, *argv)
    assert code == 0
    assert json.loads(out)["rule"] == "row_shape"


@pytest.mark.parametrize(("written", "digits"), [("0000001234", 10), ("12345", 5)])
def test_a_long_display_number_is_hidden_not_every_display(written, digits):
    text = _sub(API36, "  Display: 0\n", f"  Display: {written}\n")
    found = diagnose_tap_window(text)
    assert found.rule == "display_missing"
    [item] = found.lists
    assert (item.per_display, item.display, item.display_digits) == (True, None, digits)
    assert f"list 1 (display <num:{digits}>): 13 window rows" in found.to_text()
    assert "every display" not in found.to_text()
    legacy = diagnose_tap_window(API24).lists[0]
    assert (legacy.per_display, legacy.display) == (False, None)


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


def test_counts_lengths_and_indents_are_capped():
    long_end = _sub(
        API36, "  mMaximumObscuringOpacityForTouch: 0.800000", "  " + "x" * 12345
    )
    found = diagnose_tap_window(long_end)
    assert found.rule == "list_end"
    failing = next(shape for shape in found.lines if shape.failing)
    assert failing.tokens == ("<text:9999+>",)
    assert f"> {found.line} other 2 9999+: <text:9999+>" in found.to_text()
    payload = json.loads(json.dumps(found.to_dict()))
    line = next(item for item in payload["lines"] if item["failing"])
    assert (line["length"], line["indent"]) == (9999, 2)
    deep = " " * 20000 + "x"
    assert shape_line(deep) == ("other", ["<text:1>"])
    _check_text(found.to_text())


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


def test_a_flag_of_another_field_is_marked_unknown():
    text = _edit_row(API36, 3, "| SLIPPERY", "| SLIPPERY | ALT_FOCUSABLE_IM")
    found = diagnose_tap_window(text, show_flag_names=True)
    assert found.rule == "unknown_flag"
    failing = next(shape for shape in found.lines if shape.failing)
    assert (
        "inputConfig=NOT_FOCUSABLE|TRUSTED_OVERLAY|SLIPPERY"
        "|<unknown_flag:ALT_FOCUSABLE_IM>"
    ) in failing.tokens
    assert "|<unknown_flag:16>" in " ".join(
        next(s for s in diagnose_tap_window(text).lines if s.failing).tokens
    )


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
    [hint] = cli._press_hints(result)
    assert ("diagnose tap-window" in hint) is named
    assert (f"rule {rule}" in hint) is named


# --- round 3: structural redaction ---

MARK = "ZX9SERIALMARK7"  # serial-shaped and upper-case, so showable
FRAGMENT = "status=OK"  # a known field and value


def _failing(found):
    return next(shape for shape in found.lines if shape.failing)


# Window lines whose name or applicationInfo.name holds a mark that ends a
# name (or lacks the one that ends it): the end cannot be told, so nothing
# after the row number is read.
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
}


@pytest.mark.parametrize("case", sorted(AMBIGUOUS))
@pytest.mark.parametrize("show", [False, True], ids=["plain", "show_flag_names"])
def test_a_name_whose_end_cannot_be_told_hides_the_whole_line(case, show):
    text = AMBIGUOUS[case]
    found = diagnose_tap_window(text, show_flag_names=show)
    assert found.reason is not None
    row = next(s for s in found.lines if s.kind == "window_row" and len(s.tokens) == 2)
    assert row.tokens[1].startswith("<text:")
    for output in (found.to_text(), json.dumps(found.to_dict())):
        assert MARK not in output
        assert FRAGMENT not in output
        assert "=OK" not in output


@pytest.mark.parametrize(
    ("line", "tokens"),
    [
        (
            f"  FocusedWindow: name='X, {FRAGMENT}, z={MARK}'",
            ["FocusedWindow:", "name=<quoted:30>"],
        ),
        (
            f"    displayId=0, name='X, {FRAGMENT}', dispatchingTimeout=5000ms",
            ["displayId=<num>", "name=<quoted:12>", "dispatchingTimeout=<ms>"],
        ),
        (f"  FocusedWindow: name='X's, {FRAGMENT}'", ["FocusedWindow:", "<text:21>"]),
        (f"  FocusedWindow: name=X, {FRAGMENT}", ["FocusedWindow:", "<text:17>"]),
        (
            f"  FocusedWindow: name='X', windowName='{FRAGMENT}'",
            ["FocusedWindow:", "<text:32>"],
        ),
        (f"  {FRAGMENT}, vendor=OK", ["status=<word:2>", "<unknown_key:6>=<word:2>"]),
        (
            f"  0: name=x, displayId=0, {FRAGMENT}",
            ["row:0", "name=<text:1>", "displayId=<num>", "status=<word:2>"],
        ),
    ],
)
def test_text_an_app_sets_on_other_lines_is_only_its_length(line, tokens):
    assert shape_line(line, show_flag_names=True)[1] == tokens


def test_flag_names_show_only_in_rows_of_a_found_list():
    lone = f"0: name=x, displayId=0, inputConfig={MARK}"
    found = diagnose_tap_window(lone, show_flag_names=True)
    assert (found.rule, found.lists) == ("no_dispatcher", ())
    assert MARK not in found.to_text()
    assert MARK not in json.dumps(found.to_dict())
    # Inside the dispatcher but outside every list: not shown either.
    outside = _sub(
        API36, "  DispatchEnabled: true", f"  DispatchEnabled: true\n  {lone}"
    )
    found = diagnose_tap_window(outside, show_flag_names=True)
    assert MARK not in found.to_text()
    # In a row of the found list, whose name's end can be told: shown.
    inside = _edit_row(API36, 3, "| SLIPPERY", f"| SLIPPERY | {MARK}")
    found = diagnose_tap_window(inside, show_flag_names=True)
    assert f"<unknown_flag:{MARK}>" in found.to_text()


def test_the_opt_in_notice_says_any_device_text_can_appear():
    assert "any device text" in NOTICE_SHOWN
    assert "with or without a line break" in NOTICE_SHOWN
    assert "serial" in NOTICE_SHOWN


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
        top and API36,
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
    found = diagnose_tap_window(text)
    assert found.rule == "row_shape"
    assert [(f.rows, f.recognised, f.fields) for f in found.forms] == [(1, False, ())]
    assert "1 rows of no recognised form" in found.to_text()
    payload = json.loads(json.dumps(found.to_dict()))
    assert payload["forms"][0]["recognised"] is False


@pytest.mark.parametrize(
    ("old", "new", "token"),
    [
        (
            "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY | SLIPPERY",
            "inputConfig=0x1",
            "inputConfig=<unknown_flag:3>",
        ),
        (
            "inputConfig=NOT_FOCUSABLE | TRUSTED_OVERLAY | SLIPPERY",
            "inputConfig=0x0",
            "inputConfig=0x0",
        ),
    ],
)
def test_each_flags_field_has_its_own_number_rule(old, new, token):
    line = _row(API36, 3).replace(old, new)
    assert token in shape_line(line, in_list=True)[1]


def test_flags_and_feature_numbers_and_names():
    assert (
        "flags=<unknown_flag:11>"
        in shape_line(
            _row(API24, 0).replace("flags=0x81840048", "flags=0x123456789"),
            in_list=True,
        )[1]
    )
    assert (
        "inputFeatures=<unknown_flag:10>"
        in shape_line(
            _row(API36, 3).replace(
                ", alpha=1,", ", alpha=1, inputFeatures=DROP_INPUT,"
            ),
            in_list=True,
        )[1]
    )


def test_a_matrix_row_has_three_columns():
    assert shape_line("  0.0 1.0") == ("other", ["<dec>", "<dec>"])
    assert shape_line("  0.0 1.0 2.0")[0] == "matrix_row"


HOSTILE_LINES = {
    "head_quoted": f"  FocusedWindow: name='X, {FRAGMENT}, z={MARK}'",
    "head_fields": f"  {MARK}=1, {FRAGMENT}, owner={MARK}",
    "head_label": f"  {MARK}: {MARK}",
    "outside_row": f"  0: name={MARK}, displayId=0, inputConfig={MARK}, {FRAGMENT}",
}


@pytest.mark.parametrize("show", [False, True], ids=["plain", "show_flag_names"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_marks_on_other_lines_never_show(case, show):
    text, display = CASES[case]
    extra = "\n".join(HOSTILE_LINES.values())
    if "Input Dispatcher State:\n" in text:
        text = text.replace(
            "Input Dispatcher State:\n", f"Input Dispatcher State:\n{extra}\n", 1
        )
    else:
        text = extra + "\n" + text
    found = diagnose_tap_window(text, show_flag_names=show, display_id=display)
    for output in (found.to_text(), json.dumps(found.to_dict())):
        assert MARK not in output
        assert FRAGMENT not in output
