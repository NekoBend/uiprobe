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
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, TextIO, override

from uiprobe import __version__
from uiprobe._json import dumps
from uiprobe.crawl_preview import check_package
from uiprobe.crawl_preview import preview as make_preview
from uiprobe.device import (
    DEFAULT_SWIPE_MS,
    Device,
    DeviceDetails,
    RefusedKeys,
    _key_code,
    device_details,
    devices,
)
from uiprobe.errors import (
    ERROR_SCHEMA,
    INTERNAL_ERROR,
    INTERRUPTED,
    USAGE_ERROR,
    UiprobeError,
)
from uiprobe.guards import REASONS, Policy
from uiprobe.model import MAX_CANDIDATES, _describe
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
ACTION_SCHEMA = "uiprobe.action/1"
CRAWL_PREVIEW_SCHEMA = "uiprobe.crawl_preview/1"
INIT_SCHEMA = "uiprobe.init/1"
_MAX_COORDINATE = 32767
_COORDINATE_RE = re.compile(r"[0-9]{1,5}")
_MAX_MS = 10000
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
            " --text, --id and --desc, or the point given with --xy. The"
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
    crawl = commands.add_parser(
        "crawl",
        allow_abbrev=False,
        help="preview what a crawl would press (pressing is not available yet)",
        description=(
            "The preview is the default: crawl judges the screen showing now and"
            " lists what a crawl of PACKAGE would press and what it would skip,"
            " with the reasons. It presses nothing, does not start the app and"
            " writes no file on this computer. --dry-run forces the preview, even"
            " when the settings file says press = true. --press and press = true"
            " ask for a pressing run, which is not available yet and is refused."
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
            "ask for a pressing run (accepted, but not available yet);"
            " cannot be combined with --dry-run"
        ),
    )
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
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    screen = device.dump()
    _emit(screen.to_json(indent=2) if args.json else screen.to_tree())
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
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    long: bool = args.long
    element: Element | None = None
    element_json: dict[str, object] | None = None
    if xy is None:
        screen = device.dump()
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
    pressed = device.tap(point, long=long)
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
        screen = device.dump()
        if by_element:
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
            start, end = device.scroll(args.direction, duration_ms=duration)
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
        switch = " --dry-run" if args.dry_run else " --press" if args.press else ""
        name = f"uiprobe crawl {args.package}{switch} --config PATH"
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
    settings = _crawl_settings(args)
    if args.press and settings.press:
        msg = (
            "pressing crawls are not available yet; this version only previews"
            f" (--press is given and the settings file"
            f" {_shown(str(settings.source))} says press = true: replace"
            " --press with --dry-run, or remove --press and set"
            " press = false there)"
        )
        raise _UsageError(msg)
    if args.press:
        msg = (
            "pressing crawls are not available yet; this version only previews"
            " (run without --press)"
        )
        raise _UsageError(msg)
    if settings.press and not args.dry_run:
        msg = (
            "pressing crawls are not available yet; this version only previews"
            f" (the settings file {_shown(str(settings.source))} says"
            " press = true: add --dry-run, or set press = false there)"
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
    optional = {
        "word": detail.word,
        "field": detail.field,
        "element_id": detail.element_id,
        "note": detail.note,
    }
    entry: dict[str, object] = {"reason": detail.reason}
    entry.update({key: value for key, value in optional.items() if value})
    return entry


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
    policy, settings = _crawl_policy(args)
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


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "devices": _cmd_devices,
    "dump": _cmd_dump,
    "find": _cmd_find,
    "tap": _cmd_tap,
    "swipe": _cmd_swipe,
    "key": _cmd_key,
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


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    args = _build_parser().parse_args(argv)
    _single_serial(args)
    return args


def _guided_failure(exc: _GuidedUsageError, *, as_json: bool) -> int:
    """Report a usage error that has several lines; return its exit code."""
    if as_json:
        return _fail(USAGE_ERROR, str(exc), as_json=True, details=exc.details)
    for index, line in enumerate(exc.lines):
        prefix = "uiprobe: error: " if index == 0 else ""
        _emit(prefix + _printable(line), stream=sys.stderr)
    return USAGE_ERROR[1]


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
        return _fail(USAGE_ERROR, str(exc), as_json=as_json)
    if isinstance(exc, KeyboardInterrupt):
        msg = "interrupted"
        if isinstance(exc, InterruptedFileError):
            msg = (
                f"interrupted; {_printable(exc.path)} was created and may be"
                " incomplete, so delete it before running uiprobe init again"
            )
        return _fail(INTERRUPTED, msg, as_json=as_json)
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
