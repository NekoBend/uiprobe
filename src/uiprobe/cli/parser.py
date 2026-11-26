"""The argument parser and the usage errors it raises."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Final, NoReturn, override

from uiprobe import __version__
from uiprobe.cli.output import _emit
from uiprobe.device import DEFAULT_PRESS_MS, DEFAULT_SWIPE_MS, MAX_PRESS_MS
from uiprobe.settings import (
    _DEFAULT_ACTIONS,
    _DEFAULT_DEPTH,
    _DEFAULT_MINUTES,
    SETTINGS_FILE,
    _shown,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


# The limits of a pressing crawl from the command line: (lowest, highest,
# what). Each value is the option, else the settings file's [limits], else
# _DEFAULT_LIMITS, the values `uiprobe init` writes (see `_press_limits`).
_PRESS_LIMITS: dict[str, tuple[int, int, str]] = {
    "depth": (1, 50, "how deep it taps on one path (does not end the run)"),
    "actions": (1, 10000, "the most sent actions (launch, taps, BACK, restarts)"),
    "minutes": (1, 1440, "the longest run in minutes"),
}


_DEFAULT_LIMITS: Final[dict[str, int]] = {
    "depth": _DEFAULT_DEPTH,
    "actions": _DEFAULT_ACTIONS,
    "minutes": _DEFAULT_MINUTES,
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
        ("--no-restart", args.no_restart),
        ("--no-record-text", args.no_record_text),
        ("--quiet", args.quiet),
    ]


_MAX_MS = 10000


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
            "Start the app PACKAGE through its launcher activity (read with"
            " cmd package resolve-activity, started with am start -W) and wait"
            " until it is the foreground app. With --restart the app is"
            " stopped first (am force-stop), so it starts a fresh task; state"
            " it has not saved is lost. It starts the app on whatever device is"
            " selected. Exit 15 (launch_failed) when the launcher activity"
            " cannot be read (nothing is started then), the stop of --restart"
            " fails, am start does not report success, or the app is not in"
            " front within the timeout."
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
        help=(
            "how long the start may stay silent, then how long to wait for the"
            " app to come to the front (default 10)"
        ),
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
    _add_diagnose_parser(commands.add_parser)
    _add_tree_parser(commands.add_parser)
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


def _add_tree_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """The tree command: a run folder shown as a tree."""
    tree = add_parser(
        "tree",
        allow_abbrev=False,
        help="show a crawl run folder as a tree",
        description=(
            "Show what a crawl saw and pressed, as a tree of screens, from the"
            " files of its run folder. The command reads only that folder: it"
            " contacts no device and no adb server, and writes no file (except"
            " the one given with --html)."
        ),
    )
    tree.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    tree.add_argument(
        "--ids",
        action="store_true",
        help="add the screen id and activity to each screen",
    )
    tree.add_argument(
        "--why",
        action="store_true",
        help="add the reasons a row was not pressed",
    )
    tree.add_argument(
        "--html",
        metavar="OUT.html",
        default=None,
        help=(
            "write the tree as one self-contained HTML file with collapsible"
            " rows (no scripts) instead of printing it; the file must not exist"
            " and its folder must; cannot be combined with --json"
        ),
    )
    tree.add_argument("run_dir", metavar="RUN_DIR", help="the run folder")


def _add_diagnose_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    """The diagnose command, whose one check so far is tap-window."""
    diagnose = add_parser(
        "diagnose",
        allow_abbrev=False,
        help="tell why a check cannot read a device, without its values",
        description=(
            "Run one of uiprobe's device checks and tell why it cannot read"
            " the device, in a report that holds no device values, so that it"
            " can be shared. CHECK is tap-window."
        ),
    )
    checks = diagnose.add_subparsers(dest="check", required=True, metavar="CHECK")
    tap_window = checks.add_parser(
        "tap-window",
        allow_abbrev=False,
        help="why the window list of a tap cannot be read",
        description=(
            "Read dumpsys input once and run the crawl's tap window check on"
            " it (without a point). Print whether the window list reads or"
            " which rule refused it and at which line, the window lists found"
            " and how many windows were read, and for the lines around the"
            " failure their kind and length. No text of the device's"
            " output is printed: no serial, app or window names, coordinates,"
            " accounts or network values (unless --show-device-text). Nothing"
            " is sent to the device but that one read, and with --package the"
            " read of the app's uid (dumpsys package PKG) before it."
        ),
    )
    _add_common_options(tap_window, in_command=True)
    tap_window.add_argument(
        "--package",
        metavar="PKG",
        help=(
            "test the samsungFlags=0x1 windows as a crawl of PKG does: read"
            " PKG's uid first (dumpsys package PKG) and accept that value on"
            " PKG's own windows (the crawl's owner uid and title test); PKG is"
            " not printed"
        ),
    )
    tap_window.add_argument(
        "--show-device-text",
        action="store_true",
        help=(
            "also print the shapes of the lines: field names, values and flags"
            " from the device, where window names and app text can appear"
        ),
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
            " --press, or press = true in the settings file, runs a pressing"
            " crawl: it starts the app, presses what the rules allow, presses"
            " BACK and restarts the app, and records every step in a new run"
            " folder: --out DIR, or one inside runs_dir of the settings file."
            " It presses only on an emulator unless"
            " --allow-real-device is given, and stops at the settings file's"
            " [limits] unless --depth, --actions or --minutes say otherwise."
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
            "force the preview (the default): judge the screen showing now,"
            " also when the settings file says press = true; cannot be"
            " combined with --press"
        ),
    )
    crawl.add_argument(
        "--press",
        action="store_true",
        help=(
            "press for real: start the app and press what the rules allow;"
            " needs --out DIR, or runs_dir in the settings file; cannot be"
            " combined with --dry-run"
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
    """The options that only a pressing crawl (--press or press = true) takes."""
    crawl.add_argument(
        "--out",
        metavar="DIR",
        help="when pressing: the new run folder to record into (must not exist)",
    )
    for name, (low, high, what) in _PRESS_LIMITS.items():
        crawl.add_argument(
            f"--{name}",
            type=_bounded_int(f"--{name}", low, high),
            metavar="N",
            help=(
                f"when pressing: {what}, {low} to {high} (default: [limits]"
                f" {name} of the settings file; uiprobe init writes"
                f" {_DEFAULT_LIMITS[name]})"
            ),
        )
    crawl.add_argument(
        "--settle",
        type=_settle_seconds,
        metavar="SEC",
        help=f"when pressing: the pause after a tap or BACK (default {_PRESS_SETTLE})",
    )
    crawl.add_argument(
        "--screenshots",
        action="store_true",
        help="when pressing: also save a screenshot of each new screen",
    )
    crawl.add_argument(
        "--allow-real-device",
        action="store_true",
        help="when pressing: also press on a device that is not an emulator",
    )
    crawl.add_argument(
        "--back-on-other-screens",
        action="store_true",
        help=(
            "when pressing: press BACK on a screen of another app (by default the"
            " app is restarted instead and nothing goes to the other app)"
        ),
    )
    crawl.add_argument(
        "--no-tap-window-check",
        action="store_true",
        help=(
            "when pressing: do not read which window takes a tap at its point (by"
            " default a tap goes out only when a window of the app takes it)"
        ),
    )
    crawl.add_argument(
        "--no-restart",
        action="store_true",
        help=(
            "when pressing: start from the screen the app shows now (by default"
            " the app is stopped first with am force-stop, then its launcher"
            " activity is started; what it had not saved is lost)"
        ),
    )
    crawl.add_argument(
        "--no-record-text",
        action="store_true",
        help=(
            "when pressing: keep the texts of the screen out of the run folder"
            " (text, content description and hint are written as empty strings;"
            " password fields are always hidden); cannot be combined with"
            " --screenshots"
        ),
    )
    crawl.add_argument(
        "--quiet",
        action="store_true",
        help=(
            "when pressing: print no progress lines on stderr (by default one"
            " line goes out after an action, at most every 5 seconds); the"
            " result is printed as usual"
        ),
    )


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
