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

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from uiprobe import cli
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
    assert shape_line(row) == (
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
        "readable": True,
        "reason": None,
        "rule": None,
        "part": None,
        "line": None,
        "lists": [{"position": 1, "display": None, "rows": 7, "failed": False}],
        "lines": [],
        "forms": [
            {
                "position": 1,
                "display": None,
                "rows": 7,
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
)
TITLE = " ".join(SECRETS[:3] + SECRETS[6:10])
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
                "applicationInfo.name=com.secretbank.wallet R5CT123ABC,"
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
        f"{NOTICE} uiprobe diagnose tap-window it read dumpsys input result"
        " readable not rule at line of the dispatcher input_reader part lists"
        " list display every window rows check refused here none found lines"
        " number kind indent length tokens forms quoted bare name transform no"
        " fields display schema notice command reason position failed failing"
        " uiprobe.diagnose_tap_window/1 <unknown_key> > True False"
    )
    words.update(_pieces(template))
    return words


def _pieces(text: str) -> list[str]:
    return [p for p in re.split(r"[\s=|,;:()]+", text) if p]


ALLOWED = _allowed_words()
TAG = re.compile(r"<[a-z_]+>|<[a-z_]+|\d+>|row|-?\d+")


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
    long_name = "A" * 49
    for name, visible in (("A" * 48, True), (long_name, False), ("Ab", False)):
        kind, tokens = shape_line(f"  flags={name}", show_flag_names=True)
        assert kind == "fields"
        assert (name in tokens[0]) is visible, tokens


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
        (ValueError("bad value R5CT123ABC"), "usage_error", 2),
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
    assert "CHECK" in err or "check" in err
