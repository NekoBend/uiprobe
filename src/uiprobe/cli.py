"""The ``uiprobe`` command line.

Results go to stdout; progress and warnings go to stderr. With ``--json``
every failure is one ``uiprobe.error/1`` object on stdout. Exit codes: 0 ok,
1 unexpected, 2 usage, 3 adb server, 4 device not chosen, 5 device unusable,
130 interrupted, 10-19 specific to uiprobe. A closed output pipe, on the
success path or while an error is being reported, ends with status 1 and
nothing more is written to the closed stream.

JSON is written with terminal and bidirectional control characters escaped as
``\\uXXXX``; other text stays readable.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import io
import os
import re
import stat
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TextIO, override

from uiprobe import __version__
from uiprobe._json import dumps
from uiprobe.crawl import Limits
from uiprobe.crawl_driver import CrawlResult, _check_device
from uiprobe.crawl_driver import crawl as crawl_run
from uiprobe.crawl_preview import check_package
from uiprobe.crawl_preview import preview as make_preview
from uiprobe.device import (
    DEFAULT_LAUNCH_TIMEOUT,
    DEFAULT_PRESS_MS,
    DEFAULT_SWIPE_MS,
    MAX_LAUNCH_TIMEOUT,
    MAX_PRESS_MS,
    PIN_WAIT_STOPPED,
    Device,
    DeviceDetails,
    DisplaySizeUnknownError,
    RefusedKeys,
    TextNotTypedError,
    _key_code,
    _text_pieces,
    device_details,
    devices,
)
from uiprobe.errors import (
    ERROR_SCHEMA,
    INTERNAL_ERROR,
    INTERRUPTED,
    USAGE_ERROR,
    NotEmulatorError,
    UiprobeError,
)
from uiprobe.guards import REASONS, Policy
from uiprobe.model import MAX_CANDIDATES, _describe
from uiprobe.pngfile import (
    PngFileError,
    PngInterruptedError,
    check_new_path,
    check_png,
    save_png,
)
from uiprobe.settings import (
    SETTINGS_FILE,
    IncompleteFileError,
    InterruptedFileError,
    Settings,
    SettingsError,
    _shown,
    load_settings,
    policy_from_settings,
    write_default_settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from uiprobe.adb import DeviceInfo
    from uiprobe.crawl_preview import Preview
    from uiprobe.guards import Decision, Detail
    from uiprobe.model import Element, Screen

__all__ = ["main", "run"]

FIND_SCHEMA = "uiprobe.find/1"
CRAWL_RUN_SCHEMA = "uiprobe.crawl_run/1"
# The limits of a pressing crawl from the command line: (lowest, highest,
# default, what). The defaults are small on purpose, so a bare --press ends
# soon; the settings file's [limits] are the preview's and are not used here.
_PRESS_LIMITS: dict[str, tuple[int, int, int, str]] = {
    "depth": (1, 50, 3, "how deep it taps on one path (does not end the run)"),
    "actions": (1, 10000, 30, "the most sent actions (launch, taps, BACK, restarts)"),
    "minutes": (1, 1440, 5, "the longest run in minutes"),
}
_PRESS_SETTLE = 1.0


def _press_only(args: argparse.Namespace) -> list[tuple[str, bool]]:
    """Each option that only a pressing run takes, and whether it was given."""
    return [
        ("--out", args.out is not None),
        ("--depth", args.depth is not None),
        ("--actions", args.actions is not None),
        ("--minutes", args.minutes is not None),
        ("--settle", args.settle is not None),
        ("--screenshots", args.screenshots),
        ("--allow-real-device", args.allow_real_device),
        ("--back-on-other-screens", args.back_on_other_screens),
        ("--no-tap-window-check", args.no_tap_window_check),
    ]


ACTION_SCHEMA = "uiprobe.action/1"
CRAWL_PREVIEW_SCHEMA = "uiprobe.crawl_preview/1"
INIT_SCHEMA = "uiprobe.init/1"
SCREENSHOT_SCHEMA = "uiprobe.screenshot/1"
_MAX_COORDINATE = 32767
_COORDINATE_RE = re.compile(r"[0-9]{1,5}")
_MAX_MS = 10000
_MAX_TIMEOUT_TEXT = 32
_TIMEOUT_TEXT = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_PLAIN_PATH_RE = re.compile(r"[A-Za-z0-9_./][A-Za-z0-9_./-]*")
# On Windows a backslash and a colon need no quoting in cmd or PowerShell, so a
# drive path such as C:\Users\me\uiprobe.toml is named as it is.
_PLAIN_WINDOWS_PATH_RE = re.compile(r"[A-Za-z0-9_./\\:][A-Za-z0-9_./\\:-]*")
_ON_WINDOWS = sys.platform == "win32"
_DIRECTIONS = ("up", "down", "left", "right")
_NOT_CHECKED = (
    " The command acts on the screen showing now; it does not check which app"
    " is in front."
)


class _UsageError(Exception):
    """The command line could not be parsed."""


class _GuidedUsageError(_UsageError):
    """A usage error that tells what to do in several lines.

    Text output prints the lines one under the other; JSON output puts them
    in the message, joined by newlines, and the named parts in ``details``.
    """

    def __init__(self, lines: Sequence[str], details: dict[str, object]) -> None:
        super().__init__("\n".join(lines))
        self.lines = tuple(lines)
        self.details = details


class _Parser(argparse.ArgumentParser):
    """An argument parser that reports errors instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


class _VersionAction(argparse.Action):
    """Print ``uiprobe VERSION`` through `_emit` and exit with status 0.

    argparse's own version action wraps the line to the terminal width and
    ignores a failed write; this one prints one line and lets a closed output
    end the run with status 1, like any other output.
    """

    @override
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        _emit(f"uiprobe {__version__}")
        parser.exit()


def _add_common_options(parser: argparse.ArgumentParser, *, in_command: bool) -> None:
    """Add the options every command shares.

    They are accepted before the command (``uiprobe -s X dump``, as with adb)
    and after it. The copies on the command have no default, so that a value
    given before the command is not overwritten when the command repeats none,
    and ``--serial`` has its own destination there so both are counted.
    """
    unset = argparse.SUPPRESS if in_command else None
    off = argparse.SUPPRESS if in_command else True
    serial_dest = "command_serial" if in_command else "serial"
    parser.add_argument(
        "-s",
        "--serial",
        dest=serial_dest,
        action="append",
        default=unset,
        help="device serial",
    )
    parser.add_argument("--adb", default=unset, help="path of the adb executable")
    parser.add_argument(
        "--adb-server", default=unset, help="adb server address, tcp:HOST:PORT"
    )
    parser.add_argument(
        "--no-start-server",
        dest="start_server",
        action="store_false",
        default=off,
        help="never start the adb server",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if in_command else False,
        help="print JSON",
    )


def _add_type_parser(add_parser: Callable[..., _Parser]) -> None:
    type_ = add_parser(
        "type",
        allow_abbrev=False,
        add_help=False,  # only --help: an unquoted text holding -h must not
        # end the run with the help and exit 0 while nothing is typed
        help="type text into the focused field",
        description=(
            "Type TEXT into the field that has focus, in whatever app is in"
            " front. Only printable ASCII (space to ~) is typed. A character"
            " outside ASCII, a newline, a tab or another control character,"
            " and empty text are usage errors and nothing is sent; to press"
            " Enter use the key command. The text is not written to the"
            " output and typing is not checked afterwards. Give -s, --adb and"
            " --adb-server before the command (uiprobe -s SERIAL type TEXT):"
            " after it they are refused, so that no word of the text is ever"
            " read as one of them."
        ),
    )
    type_.add_argument("--help", action="help", help="show this help message and exit")
    # Only the options that take no value; the device options go before type.
    type_.add_argument(
        "--no-start-server",
        dest="start_server",
        action="store_false",
        default=argparse.SUPPRESS,
        help="never start the adb server",
    )
    type_.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="print JSON"
    )
    type_.add_argument("text", metavar="TEXT", help="the text to type")


def _add_launch_parser(add_parser: Callable[..., _Parser]) -> None:
    launch = add_parser(
        "launch",
        allow_abbrev=False,
        help="start an app and wait until it is in front",
        description=(
            "Start the app PACKAGE through its launcher activity and wait until"
            " it is the foreground app. With --restart the app is stopped"
            " first (am force-stop), so it starts a fresh task; state it has"
            " not saved is lost. It starts the app on whatever device is"
            " selected. Exit 15 (launch_failed) when the stop of --restart"
            " fails, the app has no launcher activity, monkey prints something"
            " unexpected, or the app is not in front within the timeout."
        ),
    )
    _add_common_options(launch, in_command=True)
    launch.add_argument("package", metavar="PACKAGE", help="the app's package name")
    launch.add_argument(
        "--restart",
        action="store_true",
        help="stop the app first, so it starts a fresh task",
    )
    launch.add_argument(
        "--timeout",
        metavar="SECONDS",
        help="how long to wait for the app to come to the front (default 10)",
    )


def _build_parser() -> _Parser:
    parser = _Parser(prog="uiprobe", allow_abbrev=False)
    parser.add_argument(
        "--version",
        action=_VersionAction,
        nargs=0,
        default=argparse.SUPPRESS,
        help="print the version and exit",
    )
    _add_common_options(parser, in_command=False)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text in (
        ("devices", "list devices"),
        ("dump", "print the current screen"),
    ):
        command = commands.add_parser(name, allow_abbrev=False, help=text)
        _add_common_options(command, in_command=True)
        if name == "dump":
            command.add_argument(
                "--brief",
                action="store_true",
                help=(
                    "text view only: list just the elements with text, a"
                    " description, a hint, [tap] or [scroll] (not with --json)"
                ),
            )
    find = commands.add_parser(
        "find",
        allow_abbrev=False,
        help="find elements on the current screen",
        description=(
            "List the elements of the current screen that match all of --text,"
            " --id and --desc. Only visible elements count: at least 8 x 8"
            " pixels of the element must be on the display. An element that"
            " another element covers at its press point is listed without a"
            " center, because tap refuses it."
        ),
    )
    _add_common_options(find, in_command=True)
    find.add_argument("--text", help="the element's text (surrounding space ignored)")
    find.add_argument("--id", dest="resource_id", help="the resource id, full or short")
    find.add_argument("--desc", help="the element's content description")
    tap = commands.add_parser(
        "tap",
        allow_abbrev=False,
        help="tap or long-press an element or a point",
        description=(
            "Tap (or, with --long, press for 800 ms) the element chosen by"
            " --text, --id and --desc, or the point given with --xy. A tap holds"
            f" still for {DEFAULT_PRESS_MS} ms by default (visible in the Pointer"
            " location developer option on a Galaxy S25 Ultra at 60 Hz);"
            " --press-ms changes the hold"
            f" (0 sends a plain input tap; the cap of {MAX_PRESS_MS} ms stays below"
            " Android's usual default long-press time, but an app may use a"
            " shorter threshold). The"
            " command acts on the screen showing now; it does not check which"
            " app is in front. Two or more matches (also a thin one) exit with 13"
            " (multiple_elements). An element is pressed only when at least 8 x 8"
            " pixels of it are on the display and the topmost element at its"
            " press point that has a clickable flag, text, description or hint"
            " is the element itself or one of its descendants. No match on the"
            " display exits with 12 (element_not_found). A sole match under 8 x 8"
            " pixels, a covered element and one with no press target exit with"
            " 12 (element_not_visible: too_small, covered or no_target). In all"
            " these cases nothing is sent. A point given with --xy is pressed as"
            " given."
        ),
    )
    _add_common_options(tap, in_command=True)
    tap.add_argument("--text", help="the element's text (surrounding space ignored)")
    tap.add_argument("--id", dest="resource_id", help="the resource id, full or short")
    tap.add_argument("--desc", help="the element's content description")
    tap.add_argument(
        "--xy",
        nargs=2,
        metavar=("X", "Y"),
        help="press this point instead of an element (0 to 32767)",
    )
    tap.add_argument("--long", action="store_true", help="press for 800 ms")
    tap.add_argument(
        "--press-ms",
        metavar="N",
        action="append",
        help=(
            f"hold a tap still for N ms, 0 to {MAX_PRESS_MS} (default"
            f" {DEFAULT_PRESS_MS}; 0 sends a plain input tap); not with --long; every"
            " repeated value is checked and the last one is used"
        ),
    )
    swipe = commands.add_parser(
        "swipe",
        allow_abbrev=False,
        help="swipe between two points, or scroll the screen or an element",
        description=(
            "Swipe from the point given with --from to the point given with --to,"
            " or scroll in the direction the finger moves (--direction up, down,"
            " left or right) along the middle of the screen or, with --text,"
            " --id and --desc, of one element (inset 20 percent, and 5 percent"
            " away from the display edges). A swipe that does not move is"
            " refused; use tap." + _NOT_CHECKED + " A swipe from a screen edge can"
            " trigger system gestures. The input is sent once and never resent."
        ),
    )
    _add_common_options(swipe, in_command=True)
    swipe.add_argument(
        "--from",
        dest="from_xy",
        nargs=2,
        metavar=("X", "Y"),
        help="start point (0 to 32767); needs --to",
    )
    swipe.add_argument(
        "--to",
        dest="to_xy",
        nargs=2,
        metavar=("X", "Y"),
        help="end point (0 to 32767); needs --from",
    )
    swipe.add_argument(
        "--direction",
        help="scroll the way the finger moves: up, down, left or right",
    )
    swipe.add_argument("--text", help="scroll inside the element with this text")
    swipe.add_argument(
        "--id", dest="resource_id", help="scroll inside this resource id"
    )
    swipe.add_argument("--desc", help="scroll inside the element with this description")
    swipe.add_argument(
        "--ms",
        help=f"duration in ms, 1 to {_MAX_MS} (default {DEFAULT_SWIPE_MS})",
    )
    key = commands.add_parser(
        "key",
        allow_abbrev=False,
        help="press a key such as back or home",
        description=(
            "Press one key: an alias (back, home, enter, delete, forward_delete,"
            " tab, escape, menu, search, recents, up, down, left, right, center,"
            " page_up, page_down, space; any case) or a full key code name such"
            " as KEYCODE_F1. Keys that act on the whole device, not on the app in"
            " front (power, calls, sound, brightness, screenshots, assistants,"
            " keys that open other apps, for example KEYCODE_POWER or"
            " KEYCODE_VOLUME_UP), are refused as a usage error; the settings file"
            " can change that list, except for the power, sleep and call keys."
            " Numbers are refused, and so is a KEYCODE_ name"
            " made only of two or more digits (KEYCODE_26), because Android may"
            " read it as a number; KEYCODE_0 to KEYCODE_9 are accepted." + _NOT_CHECKED
        ),
    )
    _add_common_options(key, in_command=True)
    key.add_argument(
        "--config",
        metavar="PATH",
        help=(
            f"the settings file whose [keys] lists decide what is refused"
            f" (default ./{SETTINGS_FILE} when it exists, else the built-in lists)"
        ),
    )
    key.add_argument("name", metavar="NAME", help="an alias or a KEYCODE_ name")
    _add_type_parser(commands.add_parser)
    screenshot = commands.add_parser(
        "screenshot",
        allow_abbrev=False,
        help="save a screenshot as a new PNG file",
        description=(
            "Take a screenshot of the screen showing now and save it as a PNG"
            " file at PATH, which must not exist (an existing file, link or"
            " folder is a usage error and is left as it is). The image is"
            " checked before the file is made; a failed screenshot (exit 16,"
            " screenshot_failed) makes no file. A screen can forbid"
            " screenshots, for example a private browser tab; its structure can"
            " still be dumped. A screenshot can contain personal data."
        ),
    )
    _add_common_options(screenshot, in_command=True)
    screenshot.add_argument(
        "-o",
        "--output",
        required=True,
        metavar="PATH",
        help="the new PNG file to write (required)",
    )
    _add_launch_parser(commands.add_parser)
    _add_crawl_parser(commands.add_parser)
    init = commands.add_parser(
        "init",
        allow_abbrev=False,
        help=f"create the settings file {SETTINGS_FILE}",
        description=(
            f"Write the settings file with every default written out, to PATH"
            f" (default ./{SETTINGS_FILE}). An existing file is never"
            " overwritten. The command does not contact the adb server."
        ),
    )
    init.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    init.add_argument(
        "path",
        nargs="?",
        default=SETTINGS_FILE,
        metavar="PATH",
        help=f"where to create the file (default ./{SETTINGS_FILE})",
    )
    return parser


def _printable(text: str) -> str:
    """Escape characters that must not reach a terminal raw."""
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


def _add_crawl_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """The crawl command: the preview, and the pressing run with --press."""
    crawl = add_parser(
        "crawl",
        allow_abbrev=False,
        help="preview what a crawl would press, or crawl for real with --press",
        description=(
            "The preview is the default: crawl judges the screen showing now and"
            " lists what a crawl of PACKAGE would press and what it would skip,"
            " with the reasons. It presses nothing, does not start the app and"
            " writes no file on this computer. --dry-run forces the preview."
            " --press runs a pressing crawl: it starts the app, presses what the"
            " rules allow, presses BACK and restarts the app, and records every"
            " step in the new folder --out DIR. It presses only on an emulator"
            " unless --allow-real-device is given, and stops after a few actions"
            " unless --depth, --actions or --minutes say otherwise."
            f" The rules come from the settings file (./{SETTINGS_FILE}"
            " or --config PATH); without one the command stops. Create it with"
            " uiprobe init."
        ),
    )
    _add_common_options(crawl, in_command=True)
    crawl.add_argument("package", metavar="PACKAGE", help="the app's package name")
    crawl.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "force the preview (the default): judge the screen showing now;"
            " cannot be combined with --press"
        ),
    )
    crawl.add_argument(
        "--press",
        action="store_true",
        help=(
            "press for real: start the app and press what the rules allow;"
            " needs --out; cannot be combined with --dry-run"
        ),
    )
    _add_press_options(crawl)
    crawl.add_argument(
        "--deny",
        action="append",
        default=[],
        metavar="WORD",
        help="also never press elements containing WORD (repeatable)",
    )
    crawl.add_argument(
        "--config",
        metavar="PATH",
        help=f"the settings file (default ./{SETTINGS_FILE}; it must exist)",
    )
    crawl.add_argument(
        "--tap-unlabeled",
        action="store_true",
        help="allow pressing elements that have no text, description or hint",
    )


def _add_press_options(crawl: argparse.ArgumentParser) -> None:
    """The options that only a pressing crawl (--press) takes."""
    crawl.add_argument(
        "--out",
        metavar="DIR",
        help="with --press: the new run folder to record into (must not exist)",
    )
    for name, (low, high, default, what) in _PRESS_LIMITS.items():
        crawl.add_argument(
            f"--{name}",
            type=_bounded_int(f"--{name}", low, high),
            metavar="N",
            help=f"with --press: {what}, {low} to {high} (default {default})",
        )
    crawl.add_argument(
        "--settle",
        type=_settle_seconds,
        metavar="SEC",
        help=f"with --press: the pause after a tap or BACK (default {_PRESS_SETTLE})",
    )
    crawl.add_argument(
        "--screenshots",
        action="store_true",
        help="with --press: also save a screenshot of each new screen",
    )
    crawl.add_argument(
        "--allow-real-device",
        action="store_true",
        help="with --press: also press on a device that is not an emulator",
    )
    crawl.add_argument(
        "--back-on-other-screens",
        action="store_true",
        help=(
            "with --press: press BACK on a screen of another app (by default the"
            " app is restarted instead and nothing goes to the other app)"
        ),
    )
    crawl.add_argument(
        "--no-tap-window-check",
        action="store_true",
        help=(
            "with --press: do not read which window takes a tap at its point (by"
            " default a tap goes out only when a window of the app takes it)"
        ),
    )


def _single_serial(args: argparse.Namespace) -> str | None:
    serials: list[str] = [
        *(args.serial or []),
        *(getattr(args, "command_serial", None) or []),
    ]
    if len(serials) > 1:
        msg = "--serial may be given only once"
        raise _UsageError(msg)
    return serials[0] if serials else None


def _device_json(details: DeviceDetails) -> dict[str, object]:
    info = details.info
    entry: dict[str, object] = {"serial": info.serial, "state": info.state}
    if info.model is not None:
        entry["model"] = info.model
    if details.api_level is not None:
        entry["api_level"] = details.api_level
    if details.emulator is not None:
        entry["emulator"] = details.emulator
    return entry


def _devices_text(found: Sequence[DeviceInfo]) -> str:
    rows = [("SERIAL", "STATE", "MODEL")]
    rows.extend((d.serial, d.state, d.model or "") for d in found)
    rows = [tuple(_printable(cell) for cell in row) for row in rows]
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    lines = [f"{a:<{widths[0]}}  {b:<{widths[1]}}  {c}".rstrip() for a, b, c in rows]
    return "\n".join(lines)


def _cmd_devices(args: argparse.Namespace) -> int:
    options = {
        "adb_server": args.adb_server,
        "adb": args.adb,
        "start_server": args.start_server,
    }
    if args.json:
        payload = {
            "schema": "uiprobe.devices/1",
            "devices": [_device_json(d) for d in device_details(**options)],
        }
        _emit(dumps(payload))
    else:
        _emit(_devices_text(devices(**options)))
    return 0


def _cmd_dump(args: argparse.Namespace) -> int:
    if args.brief and args.json:
        msg = "--brief is a text view and cannot be combined with --json"
        raise _UsageError(msg)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    screen = device.dump()
    _emit(screen.to_json(indent=2) if args.json else screen.to_tree(brief=args.brief))
    return 0


def _find_text(screen: Screen, found: list[Element]) -> str:
    noun = "element matches" if len(found) == 1 else "elements match"
    lines = [f"{len(found)} {noun}  ({screen.screen_id})"]
    lines.extend(screen._describe_line(element) for element in found[:MAX_CANDIDATES])  # noqa: SLF001
    if len(found) > MAX_CANDIDATES:
        lines.append(f"... {len(found) - MAX_CANDIDATES} more not shown")
    return "\n".join(lines)


def _cmd_find(args: argparse.Namespace) -> int:
    criteria = {
        key: value
        for key, value in (
            ("text", args.text),
            ("resource_id", args.resource_id),
            ("content_desc", args.desc),
        )
        if value is not None
    }
    if not criteria:
        msg = "give at least one of --text, --id, --desc"
        raise _UsageError(msg)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    screen = device.dump()
    found = screen.find(text=args.text, resource_id=args.resource_id, desc=args.desc)
    if args.json:
        payload = {
            "schema": FIND_SCHEMA,
            "screen_id": screen.screen_id,
            "criteria": criteria,
            "total": len(found),
            "elements": [screen._element_dict(e) for e in found[:MAX_CANDIDATES]],  # noqa: SLF001
        }
        _emit(dumps(payload, indent=2))
    else:
        _emit(_find_text(screen, found))
    return 0


def _parse_xy(values: Sequence[str], option: str = "--xy") -> tuple[int, int]:
    """Read a point option (``--xy``, ``--from``, ``--to``) as two integers.

    Each is a plain decimal integer from 0 to 32767; `option` names the option
    in the error.
    """
    numbers: list[int] = []
    for value in values:
        number = int(value) if _COORDINATE_RE.fullmatch(value) else -1
        if not 0 <= number <= _MAX_COORDINATE:
            msg = (
                f"{option} needs two integers from 0 to {_MAX_COORDINATE}, "
                f"not {value!r}"
            )
            raise _UsageError(msg)
        numbers.append(number)
    return numbers[0], numbers[1]


def _parse_press_ms(values: list[str] | None, *, long: bool) -> int:
    """Read ``--press-ms``: plain decimal integers from 0 to 200, not with --long.

    The option may be repeated; every value must be valid and the last one wins.
    """
    if values is None:
        return DEFAULT_PRESS_MS
    if long:
        msg = "--press-ms cannot be combined with --long"
        raise _UsageError(msg)
    numbers: list[int] = []
    for value in values:
        number = int(value) if re.fullmatch(r"[0-9]{1,3}", value) else -1
        if not 0 <= number <= MAX_PRESS_MS:
            msg = f"--press-ms needs an integer from 0 to {MAX_PRESS_MS}, not {value!r}"
            raise _UsageError(msg)
        numbers.append(number)
    return numbers[-1]


def _cmd_tap(args: argparse.Namespace) -> int:
    selected = (args.text, args.resource_id, args.desc)
    by_element = any(value is not None for value in selected)
    if args.xy is not None and by_element:
        msg = "--xy cannot be combined with --text, --id or --desc"
        raise _UsageError(msg)
    if args.xy is None and not by_element:
        msg = "give --xy X Y, or at least one of --text, --id, --desc"
        raise _UsageError(msg)
    xy = None if args.xy is None else _parse_xy(args.xy)
    long: bool = args.long
    press_ms = _parse_press_ms(args.press_ms, long=long)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    element: Element | None = None
    element_json: dict[str, object] | None = None
    if xy is None:
        # The activity and the API level are not used here: skip their reads.
        screen = device.dump(details=False)
        element = screen.find_one(
            text=args.text, resource_id=args.resource_id, desc=args.desc
        )
        point = screen.tap_point(element)
        element_json = screen._element_dict(element)  # noqa: SLF001
        if "disabled" in element.flags:
            _emit(
                f"uiprobe: warning: {element.id} is disabled; pressing it anyway",
                stream=sys.stderr,
            )
    else:
        point = xy
    pressed = device.tap(point, long=long, press_ms=press_ms)
    verb = "long-tapped" if long else "tapped"
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "long_tap" if long else "tap",
            "point": list(pressed),
        }
        if element_json is not None:
            payload["element"] = element_json
        _emit(dumps(payload))
    else:
        where = f"{pressed[0]},{pressed[1]}"
        if element is None:
            _emit(f"{verb} {where}")
        else:
            _emit(f"{verb} {_printable(_describe(element))} at {where}")
    return 0


def _parse_ms(value: str | None) -> int:
    """Read ``--ms``: a plain decimal integer from 1 to 10000."""
    if value is None:
        return DEFAULT_SWIPE_MS
    number = int(value) if re.fullmatch(r"[0-9]{1,5}", value) else 0
    if not 1 <= number <= _MAX_MS:
        msg = f"--ms needs an integer from 1 to {_MAX_MS}, not {value!r}"
        raise _UsageError(msg)
    return number


def _swipe_arguments(
    args: argparse.Namespace,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None, int, bool]:
    """Check the swipe options; return from, to, duration and by-element."""
    by_element = any(v is not None for v in (args.text, args.resource_id, args.desc))
    if (args.from_xy is None) != (args.to_xy is None):
        msg = "--from and --to must be given together"
        raise _UsageError(msg)
    if args.from_xy is not None and args.direction is not None:
        msg = "--from and --to cannot be combined with --direction"
        raise _UsageError(msg)
    if args.direction is None and by_element:
        msg = "--text, --id and --desc need --direction"
        raise _UsageError(msg)
    if args.direction is None and args.from_xy is None:
        msg = "give --from X Y --to X Y, or --direction up, down, left or right"
        raise _UsageError(msg)
    if args.direction is not None and args.direction not in _DIRECTIONS:
        msg = (
            f"--direction must be one of up, down, left, right, not {args.direction!r}"
        )
        raise _UsageError(msg)
    duration = _parse_ms(args.ms)
    if args.from_xy is None:
        return None, None, duration, by_element
    start = _parse_xy(args.from_xy, "--from")
    end = _parse_xy(args.to_xy, "--to")
    if start == end:
        msg = "--from and --to are the same point; a swipe must move (use tap)"
        raise _UsageError(msg)
    return start, end, duration, by_element


def _cmd_swipe(args: argparse.Namespace) -> int:
    start, end, duration, by_element = _swipe_arguments(args)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    element: Element | None = None
    element_json: dict[str, object] | None = None
    if start is None or end is None:
        if not by_element:
            # Only the display size is needed: skip the full dump.
            try:
                start, end = device.scroll_display(args.direction, duration_ms=duration)
            except DisplaySizeUnknownError:
                device.dump(details=False)
                start, end = device.scroll(args.direction, duration_ms=duration)
        else:
            screen = device.dump(details=False)
            element = screen.find_one(
                text=args.text, resource_id=args.resource_id, desc=args.desc
            )
            element_json = screen._element_dict(element)  # noqa: SLF001
            if "disabled" in element.flags:
                _emit(
                    f"uiprobe: warning: {element.id} is disabled; scrolling it anyway",
                    stream=sys.stderr,
                )
            start, end = device.scroll(
                args.direction, within=element, duration_ms=duration
            )
    else:
        start, end = device.swipe(start, end, duration_ms=duration)
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "swipe",
            "from": list(start),
            "to": list(end),
            "duration_ms": duration,
        }
        if args.direction is not None:
            payload["direction"] = args.direction
        if element_json is not None:
            payload["element"] = element_json
        _emit(dumps(payload))
    else:
        line = f"swiped {start[0]},{start[1]} -> {end[0]},{end[1]} in {duration} ms"
        if element is not None:
            line += f" on {_printable(_describe(element))}"
        _emit(line)
    return 0


def _load_settings(path: str) -> Settings:
    """Read the settings file `path`; warnings go to stderr, errors are usage errors."""
    try:
        settings = load_settings(path)
    except SettingsError as exc:
        raise _UsageError(_printable(str(exc))) from None
    for warning in settings.warnings:
        _emit(f"uiprobe: warning: {_printable(warning)}", stream=sys.stderr)
    return settings


def _is_missing(path: str) -> bool:
    """Tell whether nothing is at `path`; a broken link or entry counts as there."""
    try:
        Path(path).lstat()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except (OSError, ValueError):
        return False  # let the reader name the problem
    return False


def _check_config_path(path: str | None) -> None:
    """Refuse a ``--config`` value that ends with a path separator.

    A folder-like name such as ``rules.toml/`` would otherwise lose its slash
    when turned into a path and name the file ``rules.toml``.
    """
    if path is not None and path.endswith(("/", "\\") if _ON_WINDOWS else "/"):
        msg = (
            f"--config {_shown(path)}: a settings file path cannot end with "
            "a path separator"
        )
        raise _UsageError(msg)


def _key_settings(args: argparse.Namespace) -> Settings | None:
    """Return the settings for ``key``, or None to use the built-in lists."""
    _check_config_path(args.config)
    if args.config is not None:
        return _load_settings(args.config)
    if _is_missing(SETTINGS_FILE):
        return None
    return _load_settings(SETTINGS_FILE)


def _create_line(path: str) -> str:
    """Return the command that creates ``path``; a path needing quoting gives PATH."""
    pattern = _PLAIN_WINDOWS_PATH_RE if _ON_WINDOWS else _PLAIN_PATH_RE
    if pattern.fullmatch(str(path)) is None:
        return "uiprobe init PATH"
    return f"uiprobe init {path}"


def _crawl_settings(args: argparse.Namespace) -> Settings:
    """Return the settings for ``crawl``; no file is a usage error with guidance.

    An explicit ``--config`` path that is genuinely absent gets the same
    guidance, naming that path; an unreadable file or a broken link is left to
    the reader, which names the problem.
    """
    _check_config_path(args.config)
    path = SETTINGS_FILE if args.config is None else args.config
    if _is_missing(path):
        looked = f"./{SETTINGS_FILE}" if args.config is None else path
        create = "uiprobe init"
        if args.config is not None:
            # Name the looked-for path only when it needs no shell quoting.
            create = _create_line(path)
        # Keep a --dry-run or --press the caller gave: a file that says
        # press = true is refused without --dry-run, and --press stays visible.
        switch = " --dry-run" if args.dry_run else ""
        name = f"uiprobe crawl {args.package}{switch} --config PATH"
        if args.press:
            # A pressing run has more options (--out, limits, device) than a
            # line could repeat safely: name the one thing to add.
            name = "add --config PATH to the same command"
        raise _GuidedUsageError(
            (
                "crawl needs a settings file and none was found "
                f"(looked for {_shown(looked)})",
                f"create it with:    {create}",
                f"or name one with:  {name}",
            ),
            {"looked_for": looked, "create": create, "name_one": name},
        )
    return _load_settings(path)


def _cmd_key(args: argparse.Namespace) -> int:
    settings = _key_settings(args)
    refused = (
        None
        if settings is None
        else RefusedKeys(
            frozenset(settings.refused_keys),
            settings.refused_key_prefixes,
            _shown(str(settings.source)),
        )
    )
    _key_code(args.name, refused)  # a bad name is a usage error before any contact
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    sent = device.key(args.name, refused=refused)
    if args.json:
        _emit(dumps({"schema": ACTION_SCHEMA, "action": "key", "key": sent}))
    else:
        _emit(f"pressed key {sent}")
    return 0


def _cmd_type(args: argparse.Namespace) -> int:
    text = str.__str__(args.text)
    _text_pieces(text)  # a bad text is a usage error before any server contact
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    device.type_text(text)
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "type",
            "length": len(text),
            "device": {"serial": device.serial},
            "captured_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        }
        _emit(dumps(payload))
    else:
        _emit(f"typed {len(text)} characters")
    return 0


def _parse_timeout(value: str | None) -> float:
    """Read ``--timeout``: a plain decimal number of seconds from 0 to 600.

    It has at most 32 characters. The range is checked on the exact decimal
    value, so a number just above 600 is refused even when it rounds to 600.
    """
    if value is None:
        return DEFAULT_LAUNCH_TIMEOUT
    shown = value if len(value) <= 40 else value[:40] + "..."
    msg = (
        "--timeout needs a plain decimal number of seconds from 0 to 600 (at"
        f" most {_MAX_TIMEOUT_TEXT} characters), not {shown!r}"
    )
    if len(value) > _MAX_TIMEOUT_TEXT or not _TIMEOUT_TEXT.fullmatch(value):
        raise _UsageError(msg)
    whole, _, fraction = value.partition(".")
    limit = int(MAX_LAUNCH_TIMEOUT)
    if int(whole) > limit or (int(whole) == limit and fraction.strip("0")):
        raise _UsageError(msg)
    return float(value)


def _cmd_launch(args: argparse.Namespace) -> int:
    package = check_package(args.package)
    timeout = _parse_timeout(args.timeout)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    device.launch(package, restart=args.restart, timeout=timeout)
    if args.json:
        payload: dict[str, object] = {
            "schema": ACTION_SCHEMA,
            "action": "launch",
            "package": package,
            "device": {"serial": device.serial},
            "restart": args.restart,
            "captured_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        }
        _emit(dumps(payload))
    else:
        _emit(f"launched {package}")
    return 0


def _crawl_policy(args: argparse.Namespace) -> tuple[Policy, Settings]:
    """Check the crawl options, read the settings and build the policy.

    No device is contacted.
    """
    if args.press and args.dry_run:
        msg = "--press and --dry-run cannot be combined"
        raise _UsageError(msg)
    try:
        check_package(args.package)
    except ValueError as exc:
        raise _UsageError(_printable(str(exc))) from None
    for word in args.deny:
        try:
            Policy().with_deny_words([word])
        except ValueError:
            msg = f"--deny word is empty after normalization: {word!r}"
            raise _UsageError(msg) from None
    if not args.press:
        given = [option for option, value in _press_only(args) if value]
        if given:
            msg = f"{given[0]} is used only with --press"
            raise _UsageError(msg)
    elif args.out is None:
        msg = "--press needs --out DIR, the new folder to record the run into"
        raise _UsageError(msg)
    settings = _crawl_settings(args)
    if settings.press and not (args.press or args.dry_run):
        msg = (
            f"the settings file {_shown(str(settings.source))} says press = true,"
            " but a crawl presses only when --press is given: add --press and"
            " --out DIR to press, or --dry-run to preview"
        )
        raise _UsageError(msg)
    policy = policy_from_settings(
        settings,
        extra_deny=args.deny,
        tap_unlabeled=True if args.tap_unlabeled else None,
    )
    return policy, settings


def _detail_text(decision: Decision, detail: Detail) -> str:
    """Describe what one deny word matched, for example ``"pay" in text of e3``."""
    text = f'"{_printable(detail.word)}"'
    if detail.field:
        text += f" in {_printable(detail.field)}"
    if detail.element_id and detail.element_id != decision.element.id:
        text += f" of {_printable(detail.element_id)}"
    return text


def _reasons_text(decision: Decision) -> str:
    parts: list[str] = []
    for reason in decision.reasons:
        part: str = reason
        if reason in ("deny_word", "irreversible_screen", "confirm_button"):
            found = [
                _detail_text(decision, d)
                for d in decision.details
                if d.reason == reason and d.word
            ]
            if found:
                part += f" ({'; '.join(found)})"
        parts.append(part)
    return ", ".join(parts)


def _decision_center(screen: Screen, decision: Decision) -> tuple[int, int] | None:
    """Return the ``center`` to show for a decision, without scanning the screen.

    It is the point the judgement chose, left out when the element is not
    visible or is covered (as ``Screen.tap_point`` would refuse it). A screen
    that was too large to judge has no point; then the center of the visible
    part is shown, which costs one element only. That part comes from
    ``Screen.visible_bounds``, which always uses 8 pixels, even when the
    policy's ``min_visible_px`` is larger (the element is skipped anyway).
    """
    if "screen_too_large" in decision.reasons:
        visible = screen.visible_bounds(decision.element)
        return None if visible is None else visible.center
    if "not_visible" in decision.reasons or "covered" in decision.reasons:
        return None
    return decision.point


def _preview_text(result: Preview, settings_path: str, *, dry_run: bool) -> str:
    screen = result.screen
    shown = {True: "shown", False: "hidden", None: "unknown"}[result.keyboard_shown]
    foreground = "unknown" if result.foreground is None else result.foreground
    pressable = result.judgement.pressable
    skipped = result.judgement.skipped
    lines = [
        f"preview for {_printable(result.package)} on screen {screen.screen_id};"
        " nothing was pressed",
        f"settings: {_printable(_shown(settings_path))}",
        f"foreground: {_printable(foreground)}, keyboard: {shown}",
        f"would press ({len(pressable)}):",
    ]
    lines.extend(
        "  " + screen._describe_line_at(d.element, _decision_center(screen, d))  # noqa: SLF001
        for d in pressable
    )
    lines.append(f"would not press ({len(skipped)}):")
    lines.extend(
        "  "
        + screen._describe_line_at(d.element, _decision_center(screen, d))  # noqa: SLF001
        + ": "
        + _reasons_text(d)
        for d in skipped
    )
    # Guidance on how to switch to a pressing run (--press, or
    # press = true in the settings file, plus --allow-real-device on a real
    # device) is printed here once the pressing crawl exists. Until then no
    # command is printed, because every pressing command is refused.
    how = "--dry-run" if dry_run else "the default"
    lines.append(f"nothing was pressed: this was a preview ({how})")
    return "\n".join(lines)


def _detail_json(detail: Detail) -> dict[str, object]:
    return dict(detail.to_dict())


def _preview_json(result: Preview, settings_path: str) -> dict[str, object]:
    screen = result.screen
    payload: dict[str, object] = {
        "schema": CRAWL_PREVIEW_SCHEMA,
        "package": result.package,
        "screen_id": screen.screen_id,
        "captured_at": screen.captured_at.isoformat(timespec="seconds"),
        "settings": settings_path,
        "pressed": False,
        "mode": "preview",
    }
    if result.foreground is not None:
        payload["foreground"] = result.foreground
    if result.keyboard_shown is not None:
        payload["keyboard_shown"] = result.keyboard_shown
    payload["press"] = [
        screen._element_dict_at(d.element, _decision_center(screen, d))  # noqa: SLF001
        for d in result.judgement.pressable
    ]
    skip: list[dict[str, object]] = []
    for decision in result.judgement.skipped:
        center = _decision_center(screen, decision)
        entry = screen._element_dict_at(decision.element, center)  # noqa: SLF001
        entry["reasons"] = [r for r in REASONS if r in decision.reasons]
        entry["details"] = [_detail_json(d) for d in decision.details]
        skip.append(entry)
    payload["skip"] = skip
    return payload


def _cmd_crawl(args: argparse.Namespace) -> int:
    if args.press and (sys.stderr if args.json else sys.stdout) is None:
        # Before anything else writes (a settings warning would go to stdout).
        stream = "stderr" if args.json else "stdout"
        msg = f"the plan cannot be shown: {stream} is closed, so nothing was pressed"
        raise _UsageError(msg)
    policy, settings = _crawl_policy(args)
    if args.press:
        return _cmd_crawl_press(args, policy)
    settings_path = str(settings.source)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    result = make_preview(device, args.package, policy)
    if args.json:
        _emit(dumps(_preview_json(result, settings_path), indent=2))
    else:
        _emit(_preview_text(result, settings_path, dry_run=args.dry_run))
    return 0


def _bounded_int(option: str, low: int, high: int) -> Callable[[str], int]:
    """A converter that checks every occurrence of `option` as it is read.

    A wrong value is a usage error even when the option is given again later
    with a good one (argparse keeps only the last value).
    """

    def convert(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            msg = f"{option} must be a whole number, got {_shown(text)}"
            raise _UsageError(msg) from None
        if not low <= value <= high:
            msg = f"{option} must be from {low} to {high}, got {value}"
            raise _UsageError(msg)
        return value

    return convert


def _settle_seconds(text: str) -> float:
    """Check one ``--settle`` value as it is read (also refuses NaN)."""
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not 0 <= value <= 60:
        msg = f"--settle must be from 0 to 60 seconds, got {_shown(text)}"
        raise _UsageError(msg)
    return value


def _press_limits(args: argparse.Namespace) -> Limits:
    given = {"depth": args.depth, "actions": args.actions, "minutes": args.minutes}
    values: dict[str, int] = {}
    for name, (low, high, default, _what) in _PRESS_LIMITS.items():
        value = given[name]
        value = default if value is None else value
        if not low <= value <= high:
            msg = f"--{name} must be from {low} to {high}, got {value}"
            raise _UsageError(msg)
        values[name] = value
    return Limits(**values)


def _press_settle(args: argparse.Namespace) -> float:
    return _PRESS_SETTLE if args.settle is None else args.settle


def _cmd_crawl_press(args: argparse.Namespace, policy: Policy) -> int:
    """Run a pressing crawl into ``--out`` and report how it ended."""
    limits = _press_limits(args)
    settle = _press_settle(args)
    policy = replace(
        policy,
        back_on_other_screens=args.back_on_other_screens,
        check_tap_window=not args.no_tap_window_check,
    )
    out: str = args.out
    shown_out = _shown(out)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
        pin=True,
    )
    emulator = _press_device_check(device, allow_real_device=args.allow_real_device)
    # The identity read through the pin must agree before anything is shown
    # or written: first that it is still the same device, then (without
    # --allow-real-device) that this identity also says emulator, which every
    # later pin check requires.
    device.check_pin(emulator=False)
    if not args.allow_real_device:
        # The identity the pin check just verified (not a fresh read, which
        # could answer differently).
        found = device.pinned_identity
        if found is None or not found.emulator:
            reason = "unreadable" if found is None else "not_emulator"
            refusal = NotEmulatorError(
                "conflicting emulator answers",
                details={"serial": device.serial, "reason": reason},
            )
            raise _cli_not_emulator(device, refusal)
    plan: dict[str, object] = {
        "package": args.package,
        "device": {"serial": device.serial, "emulator": emulator},
        "limits": {
            "depth": limits.depth,
            "actions": limits.actions,
            "minutes": limits.minutes,
        },
        "out": out,
        "back_on_other_screens": policy.back_on_other_screens,
        "check_tap_window": policy.check_tap_window,
        "screenshots": args.screenshots,
    }
    # The plan is out (and flushed) before the first request that changes the
    # device; with --json it goes to stderr, so stdout stays one JSON document.
    plan_stream = sys.stderr if args.json else sys.stdout
    if plan_stream is None:  # a closed stream at start: refuse before pressing
        msg = "the plan cannot be shown: " + ("stderr" if args.json else "stdout")
        raise _UsageError(msg + " is closed, so nothing was pressed")
    _emit(_press_plan_text(plan), stream=plan_stream)
    _flush(plan_stream)
    existed = Path(out).exists() or Path(out).is_symlink()
    try:
        result = crawl_run(
            device,
            args.package,
            out,
            policy=policy,
            limits=limits,
            screenshots=args.screenshots,
            # The caller's own choice: crawl_run checks the device again
            # through the pin, and every later pin check keeps requiring an
            # emulator unless --allow-real-device was given.
            allow_real_device=args.allow_real_device,
            settle=settle,
        )
    except KeyboardInterrupt:
        # crawl_run wrote run.json with "interrupted" when it still could.
        if existed or not Path(out).exists():
            return _fail(INTERRUPTED, "interrupted", as_json=args.json)
        msg = f"interrupted; the run so far is in {shown_out}"
        return _fail(INTERRUPTED, msg, as_json=args.json, details={"out": out})
    except NotEmulatorError as exc:
        # The check inside crawl_run answered differently from the first one:
        # nothing was written or sent; say it in the command's words.
        raise _cli_not_emulator(device, exc) from None
    except UiprobeError as exc:
        if not existed and Path(out).exists():  # made by this run
            exc.details.setdefault("out", out)
        raise
    moves = result.repins  # as run.json counts them
    hints = _press_hints(result)
    if args.json:
        _emit(dumps(_press_json(plan, result, hints, moves), indent=2))
    else:
        _emit(_press_result_text(result, hints, moves))
    return 0


def _flush(stream: TextIO) -> None:
    """Flush `stream`; a closed output raises ``BrokenPipeError`` as in `_emit`."""
    try:
        stream.flush()
    except OSError as exc:
        if isinstance(exc, BrokenPipeError) or not _is_closed_output(exc, stream):
            raise
        raise BrokenPipeError(errno.EPIPE, "the output was closed") from exc


def _cli_not_emulator(device: Device, exc: NotEmulatorError) -> NotEmulatorError:
    """The refusal of a real device, in the command's words (same details)."""
    what = (
        "is not an emulator"
        if exc.details.get("reason") == "not_emulator"
        else "could not be shown to be an emulator (unreadable check)"
    )
    msg = (
        f"{_printable(str(device.serial))} {what}, so nothing was pressed"
        " and no folder was made; a pressing crawl presses for real: to run"
        " it on this device, add --allow-real-device"
    )
    return NotEmulatorError(msg, details=exc.details)


def _press_device_check(device: Device, *, allow_real_device: bool) -> bool | None:
    """Whether the device is an emulator (None: unreadable); refuse a real one.

    Without `allow_real_device`, a device that is not shown to be an emulator
    is refused before anything is written or sent, as `crawl_run` would.
    """
    if not allow_real_device:
        try:
            _check_device(device, allow_real_device=False)
        except NotEmulatorError as exc:
            raise _cli_not_emulator(device, exc) from None
        return True
    try:
        return device.is_emulator()
    except UiprobeError as exc:
        if exc.details.get("reason") in ("device_changed", PIN_WAIT_STOPPED):
            raise
        return None


def _press_plan_text(plan: dict[str, object]) -> str:
    device = plan["device"]
    limits = plan["limits"]
    assert isinstance(device, dict)  # noqa: S101 - built above
    assert isinstance(limits, dict)  # noqa: S101
    kind = {True: "emulator", False: "real device"}.get(
        device["emulator"], "emulator check unreadable"
    )
    return "\n".join(
        [
            f"pressing on {_printable(str(device['serial']))} ({kind}):"
            f" {_printable(str(plan['package']))}",
            f"limits: depth {limits['depth']}, actions {limits['actions']},"
            f" minutes {limits['minutes']}",
            f"run folder: {_shown(str(plan['out']))}",
            "BACK on screens of other apps: "
            + ("yes" if plan["back_on_other_screens"] else "no (restart the app)"),
            "check the window at each tap point: "
            + ("yes" if plan["check_tap_window"] else "no"),
        ]
    )


def _press_hints(result: CrawlResult) -> list[str]:
    """Advice for the endings a real device runs into; only those that apply."""
    hints: list[str] = []
    refused = dict(result.not_sent)
    if refused.get("cover_unknown"):
        hints.append(
            "the window at a tap point could not be read on this device"
            f" ({result.cover_unknown or 'unknown'}); --no-tap-window-check"
            " turns the check off, but then an overlay of another app over the"
            " point can receive a tap"
        )
    if not result.repin:
        hints.append(
            "the boot id of this device could not be read, so a renumbered"
            " adb connection ends the run instead of being followed, and so"
            " does any failed adb exchange with the device, even on the same"
            " connection"
        )
    if result.stop_reason == "dump_failed_repeatedly":
        front = result.last_foreground
        hints.append(
            "uiautomator dump kept failing; a screen that never settles (a value"
            " that changes every second, an animation) makes it fail, so avoid"
            f" that screen (app in front at the last read: {_printable(str(front))},"
            f" last screen read: {result.last_screen_id or 'none'})"
        )
    return hints


def _press_result_text(result: CrawlResult, hints: list[str], moves: int) -> str:
    refused = ", ".join(f"{reason} {count}" for reason, count in result.not_sent)
    lines = [
        f"stopped: {result.stop_reason}",
        f"actions: {result.actions} (started; those not sent included)",
        f"screens: {result.screens}",
        f"not sent: {refused or 'none'}",
        f"transport moves: {moves}",
        f"run folder: {_shown(str(result.out))}",
    ]
    lines.extend(f"note: {hint}" for hint in hints)
    return "\n".join(lines)


def _press_json(
    plan: dict[str, object], result: CrawlResult, hints: list[str], moves: int
) -> dict[str, object]:
    return {
        "schema": CRAWL_RUN_SCHEMA,
        **plan,
        "out": str(result.out),
        "stop_reason": result.stop_reason,
        "actions": result.actions,
        "screens": result.screens,
        "not_sent": dict(result.not_sent),
        "repins": moves,
        "repin": result.repin,
        "notes": hints,
    }


def _cmd_init(args: argparse.Namespace) -> int:
    path: str = args.path
    shown = _printable(path)
    if (
        args.serial is not None
        or args.adb is not None
        or args.adb_server is not None
        or not args.start_server
    ):
        msg = (
            "init takes no device options (-s, --adb, --adb-server, --no-start-server)"
        )
        raise _UsageError(msg)
    try:
        write_default_settings(path)
    except FileExistsError:
        msg = f"{shown} already exists; uiprobe never overwrites it"
        raise _UsageError(msg) from None
    except IncompleteFileError as exc:
        msg = (
            f"{shown} was created but could not be written completely:"
            f" {_printable(str(exc))}; the file may be incomplete, so delete it"
            " and run uiprobe init again"
        )
        raise _UsageError(msg) from None
    except (OSError, ValueError) as exc:
        msg = f"{shown} cannot be created: {_printable(str(exc))}"
        raise _UsageError(msg) from None
    if args.json:
        _emit(dumps({"schema": INIT_SCHEMA, "path": path}))
    else:
        _emit(shown)
    return 0


def _check_new_file(path: str) -> None:
    """Refuse what `save_png` would refuse before the device is asked.

    That is an empty path, anything at the path (also a broken link) and, on
    Windows, a path that names a stream of a file.
    """
    shown = _printable(path)
    if not path:
        msg = "-o PATH must not be empty"
        raise _UsageError(msg)
    try:
        check_new_path(path)
    except FileExistsError:
        msg = f"-o {shown}: already exists; give a new file name"
        raise _UsageError(msg) from None
    except ValueError as error:
        msg = f"-o {shown}: {error}"
        raise _UsageError(msg) from None
    except OSError as error:
        msg = f"cannot check {path}: {error.strerror or error}"
        details = {"path": path, "created": False}
        raise PngFileError(msg, details=details) from error


def _cmd_screenshot(args: argparse.Namespace) -> int:
    path = args.output
    _check_new_file(path)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    png = device.screenshot()
    captured_at = datetime.now(UTC).astimezone()
    width, height = check_png(png)
    # Asked before the file is made, so that nothing slow or failing comes
    # between saving and reporting the saved file.
    display = device.display_for_image(width, height) if args.json else None
    try:
        save_png(path, png)
    except FileExistsError:
        msg = f"-o {_printable(path)}: already exists; give a new file name"
        raise _UsageError(msg) from None
    if args.json:
        payload: dict[str, object] = {
            "schema": SCREENSHOT_SCHEMA,
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "device": {"serial": device.serial},
            "path": path,
            "bytes": len(png),
            "width": width,
            "height": height,
        }
        if display is not None:
            payload["display"] = {"width": display[0], "height": display[1]}
        _emit(dumps(payload, indent=2))
    else:
        _emit(f"saved {_printable(path)} ({width}x{height}, {len(png)} bytes)")
    return 0


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "devices": _cmd_devices,
    "dump": _cmd_dump,
    "find": _cmd_find,
    "tap": _cmd_tap,
    "swipe": _cmd_swipe,
    "key": _cmd_key,
    "type": _cmd_type,
    "launch": _cmd_launch,
    "screenshot": _cmd_screenshot,
    "crawl": _cmd_crawl,
    "init": _cmd_init,
}


def _error_object(
    code: str, message: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    error: dict[str, object] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"schema": ERROR_SCHEMA, "error": error}


def _fail(
    kind: tuple[str, int],
    message: str,
    *,
    as_json: bool,
    details: dict[str, object] | None = None,
) -> int:
    """Report an error that has no `UiprobeError`; return its exit code."""
    code, status = kind
    return _report(_error_object(code, message, details), status, as_json=as_json)


def _is_pipe(stream: TextIO | None) -> bool:
    """Return True if `stream` is backed by a pipe; False when that cannot be told."""
    if stream is None:
        return False
    try:
        return stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode)
    except (AttributeError, OSError, ValueError):
        return False


def _is_closed_output(exc: OSError, stream: TextIO | None) -> bool:
    """Return True if `exc`, raised by writing or flushing `stream`, means the
    reader is gone.

    POSIX reports a closed pipe as ``BrokenPipeError`` (EPIPE). Windows reports
    the same condition as ``OSError`` with EINVAL, which Python does not turn
    into ``BrokenPipeError``. EINVAL is accepted only on Windows, only for
    errors raised while writing or flushing our own output (see `_emit` and
    `_flush_streams`), and only when the stream is a pipe: on a file or a
    console it is an ordinary unexpected error. (``os.fstat`` reports a Windows
    pipe as a FIFO.)
    """
    if isinstance(exc, BrokenPipeError):
        return True
    return sys.platform == "win32" and exc.errno == errno.EINVAL and _is_pipe(stream)


def _emit(text: str, *, stream: TextIO | None = None) -> None:
    """Print `text` and a newline; a closed output raises ``BrokenPipeError``.

    All command output must go through here: on Windows an EINVAL from a raw
    write cannot be told apart from an unrelated EINVAL, so only writes made
    here are recognised as a closed output.
    """
    target = sys.stdout if stream is None else stream
    try:
        print(text, file=target)
    except OSError as exc:
        if isinstance(exc, BrokenPipeError) or not _is_closed_output(exc, target):
            raise
        raise BrokenPipeError(errno.EPIPE, "the output was closed") from exc


def _report(error: dict[str, object], status: int, *, as_json: bool) -> int:
    if as_json:
        _emit(dumps(error))
    else:
        inner = error["error"]
        message = inner["message"] if isinstance(inner, dict) else ""
        _emit(f"uiprobe: error: {_printable(str(message))}", stream=sys.stderr)
    return status


def _json_requested(argv: Sequence[str]) -> bool:
    """Return True if ``--json`` is among the options, i.e. before any ``--``.

    Everything after ``--`` is an operand for the command, never an option.
    """
    options = argv[: argv.index("--")] if "--" in argv else argv
    return "--json" in options


# argparse quotes the words it cannot place ("unrecognized arguments: two"),
# and with the type command those words can be part of the text.
_TYPE_USAGE_MESSAGE = (
    "the arguments could not be read (they are not shown, as they can hold the"
    " text to type); give TEXT as one quoted argument, after -- when it starts"
    " with -; for how to use it see uiprobe type --help"
)
# Options before the command that take a value: their value is not a command.
_TOP_OPTIONS_WITH_VALUE = frozenset({"-s", "--serial", "--adb", "--adb-server"})
_DEVICE_OPTIONS = ("--serial", "--adb", "--adb-server")
_DEVICE_OPTION_AFTER_TYPE = (
    "-s, --serial, --adb and --adb-server go before the type command, as in"
    " uiprobe -s SERIAL type TEXT; after it they are refused (the value is not"
    " shown, as it can be part of the text to type). Give TEXT as one quoted"
    " argument, after -- when it starts with -"
)


def _command_index(argv: Sequence[str]) -> int | None:
    """Return where the command word is in `argv`, or None if there is none."""
    skip = False
    for index, word in enumerate(argv):
        if skip:
            skip = False
        elif word in _TOP_OPTIONS_WITH_VALUE:
            skip = True
        elif word == "--":
            return index + 1 if index + 1 < len(argv) else None
        elif word == "-" or not word.startswith("-"):
            return index
    return None


def _names_a_device_option(word: str) -> bool:
    """Whether argparse could read `word` as -s, --serial, --adb or --adb-server.

    That covers ``-s``, ``-sVALUE``, ``--serial=VALUE`` and every leading part
    of a long name (``--ser``, ``--adb-s``), whether or not argparse would
    accept the shortened form. A word with a space is read as text by
    argparse, so it is not one.
    """
    if " " in word:
        return False
    name = word.split("=", 1)[0]
    if name.startswith("-s"):
        return True
    return len(name) > 2 and any(option.startswith(name) for option in _DEVICE_OPTIONS)


def _device_option_after_type(words: Sequence[str]) -> bool:
    """Whether a device option comes in `words` before any ``--``."""
    for word in words:
        if word == "--":
            return False
        if _names_a_device_option(word):
            return True
    return False


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    """Read the command line.

    When the command is ``type``, a device option after it is refused before
    anything else, and an argparse error is replaced by a message that shows
    none of the arguments, raised outside the handler so that it chains to
    nothing that holds them.
    """
    where = _command_index(argv)
    is_type = where is not None and argv[where] == "type"
    if is_type and where is not None and _device_option_after_type(argv[where + 1 :]):
        raise _UsageError(_DEVICE_OPTION_AFTER_TYPE)
    try:
        args = _build_parser().parse_args(argv)
    except _UsageError:
        if not is_type:
            raise
    else:
        _single_serial(args)
        return args
    raise _UsageError(_TYPE_USAGE_MESSAGE)


def _guided_failure(exc: _GuidedUsageError, *, as_json: bool) -> int:
    """Report a usage error that has several lines; return its exit code."""
    if as_json:
        return _fail(USAGE_ERROR, str(exc), as_json=True, details=exc.details)
    for index, line in enumerate(exc.lines):
        prefix = "uiprobe: error: " if index == 0 else ""
        _emit(prefix + _printable(line), stream=sys.stderr)
    return USAGE_ERROR[1]


def _interrupt_failure(exc: KeyboardInterrupt, *, as_json: bool) -> int:
    """Report Ctrl+C, naming a file that was created and may be incomplete."""
    if isinstance(exc, InterruptedFileError):
        msg = (
            f"interrupted; {_printable(exc.path)} was created and may be"
            " incomplete, so delete it before running uiprobe init again"
        )
        return _fail(INTERRUPTED, msg, as_json=as_json)
    if isinstance(exc, PngInterruptedError):
        shown = _printable(exc.path)
        msg = f"interrupted; {shown} was created and may be incomplete"
        details: dict[str, object] = {"path": exc.path, "created": True}
        return _fail(INTERRUPTED, msg, as_json=as_json, details=details)
    return _fail(INTERRUPTED, "interrupted", as_json=as_json)


def _failure_code(exc: BaseException, *, as_json: bool) -> int:
    """Report `exc` the way the contract says; return the exit code."""
    if isinstance(exc, BrokenPipeError):
        # The reader of our output went away; there is nobody to tell.
        return INTERNAL_ERROR[1]
    if isinstance(exc, UiprobeError):
        return _report(exc.to_dict(), exc.exit_code, as_json=as_json)
    if isinstance(exc, _GuidedUsageError):
        return _guided_failure(exc, as_json=as_json)
    if isinstance(exc, _UsageError | TypeError | ValueError):
        details = exc.details if isinstance(exc, TextNotTypedError) else None
        return _fail(USAGE_ERROR, str(exc), as_json=as_json, details=details)
    if isinstance(exc, KeyboardInterrupt):
        return _interrupt_failure(exc, as_json=as_json)
    name = type(exc).__name__
    return _fail(
        INTERNAL_ERROR,
        f"unexpected {name}: {exc}",
        as_json=as_json,
        details={"type": name},
    )


def run(argv: Sequence[str]) -> int:
    """Run the command line `argv` (without the program name); return the exit code."""
    as_json = _json_requested(argv)
    try:
        args = _parse(argv)
        return _HANDLERS[args.command](args)
    except SystemExit as exc:
        # argparse exits for --help and --version; it never exits for an error
        # (see _Parser).
        return exc.code if isinstance(exc.code, int) else 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - last resort: never show a traceback
        try:
            return _failure_code(exc, as_json=as_json)
        except BrokenPipeError:
            # Reporting the error failed because its reader is gone. The rule
            # is the same as for a broken pipe on the success path: stop
            # writing and exit with status 1, whatever the error was. `main`
            # redirects the broken descriptor; `run` leaves descriptors alone.
            return INTERNAL_ERROR[1]


def _configure_streams(argv: Sequence[str]) -> None:
    """Make the output safe to read: UTF-8 JSON, and no crash on odd characters.

    JSON is exchanged as UTF-8 (RFC 8259), whatever the locale says; on Windows
    a pipe would otherwise carry the ANSI code page and break the JSON. Text
    output keeps the locale's encoding, which is what a terminal expects.
    """
    json_mode = _json_requested(argv)
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            if json_mode and stream is sys.stdout:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            else:
                stream.reconfigure(errors="backslashreplace")


def _discard_output(stream: TextIO) -> None:
    """Point `stream` at the null device so later writes cannot fail.

    This is what Python's documentation recommends after a broken pipe; it
    keeps the flush at interpreter shutdown from failing too.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, stream.fileno())
    except (AttributeError, OSError, ValueError):
        pass
    finally:
        os.close(devnull)


def _flush_streams(code: int) -> int:
    """Flush stdout and stderr now; discard the output of one whose reader is gone.

    Without this the buffered output fails to flush at interpreter shutdown,
    which ends the process with Python's own exit code 120 and a message. A
    stream that is None (its descriptor was closed at startup) has nothing to
    flush.
    """
    unexpected: OSError | None = None
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.flush()
        except OSError as exc:
            if not _is_closed_output(exc, stream):
                # Keep going: the other stream may still need discarding.
                unexpected = unexpected or exc
                continue
            _discard_output(stream)
            code = INTERNAL_ERROR[1]
        except ValueError:
            continue  # the stream was closed; nothing is left to lose
    if unexpected is not None:
        raise unexpected
    return code


def main() -> None:
    """Console-script entry point."""
    argv = sys.argv[1:]
    try:
        _configure_streams(argv)
        code = _flush_streams(run(argv))
    except KeyboardInterrupt:
        # Ctrl+C outside `run`: while the streams are set up or flushed.
        code = _interrupted(as_json=_json_requested(argv))
    sys.exit(code)


def _interrupted(*, as_json: bool) -> int:
    """Report an interrupt that `run` did not see; return its exit code.

    The exit code is always the interrupt's. Reporting and flushing are tried
    separately: when the report fails because the reader is gone, the
    buffered output must still be discarded, or the flush at interpreter
    shutdown fails and ends the process with Python's exit code 120.
    """
    # A second Ctrl+C or a gone reader: the exit code says it all.
    gone = (OSError, ValueError, KeyboardInterrupt)
    with contextlib.suppress(*gone):
        _fail(INTERRUPTED, "interrupted", as_json=as_json)
    with contextlib.suppress(*gone):
        _flush_streams(INTERRUPTED[1])
    return INTERRUPTED[1]
