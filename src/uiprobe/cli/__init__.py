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

import argparse  # noqa: TC003 (re-exported; the former cli.py imported it)
import contextlib
import errno
import io
import os
import re
import stat
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, NoReturn, TextIO, override

from uiprobe import __version__
from uiprobe._json import dumps
from uiprobe.cli.commands import (
    _COORDINATE_RE,
    _DIRECTIONS,
    _MAX_COORDINATE,
    _MAX_TIMEOUT_TEXT,
    _TIMEOUT_TEXT,
    ACTION_SCHEMA,
    FIND_SCHEMA,
    INIT_SCHEMA,
    SCREENSHOT_SCHEMA,
    _check_new_file,
    _cmd_devices,
    _cmd_dump,
    _cmd_find,
    _cmd_init,
    _cmd_key,
    _cmd_launch,
    _cmd_screenshot,
    _cmd_swipe,
    _cmd_tap,
    _cmd_type,
    _device_json,
    _devices_text,
    _find_text,
    _key_settings,
    _parse_ms,
    _parse_press_ms,
    _parse_timeout,
    _parse_xy,
    _single_serial,
    _swipe_arguments,
)
from uiprobe.cli.crawl_dry import (
    _MAX_RUN_NAME,
    _ON_WINDOWS,
    _PLAIN_PATH_RE,
    _PLAIN_WINDOWS_PATH_RE,
    CRAWL_PREVIEW_SCHEMA,
    _check_config_path,
    _check_plan_stream,
    _check_run_folder_leaf,
    _check_run_folder_parent,
    _crawl_policy,
    _crawl_settings,
    _CrawlSetup,
    _create_line,
    _decision_center,
    _detail_json,
    _detail_text,
    _file_press_out,
    _hint_path,
    _is_missing,
    _load_settings,
    _OutSource,
    _PressSource,
    _preview_guidance,
    _preview_json,
    _preview_text,
    _reasons_text,
    _refuse_press_only,
    _run_folder_parent,
    _run_stamp,
    _runs_dir_parent,
    _settings_warnings,
)
from uiprobe.cli.crawl_press import (
    _RESTART_PLAN,
    _SYSTEM_UI,
    CRAWL_RUN_SCHEMA,
    _cli_not_emulator,
    _cmd_crawl,
    _cmd_crawl_press,
    _limit_source_text,
    _LimitSource,
    _made_by_this_run,
    _press_because_text,
    _press_device_check,
    _press_hints,
    _press_json,
    _press_limits,
    _press_limits_text,
    _press_out_text,
    _press_plan_text,
    _press_result_text,
    _press_settle,
)
from uiprobe.cli.diagnose import (
    _DIAGNOSE_PACKAGE_MESSAGE,
    _DIAGNOSE_USAGE_MESSAGE,
    _cmd_diagnose,
    _crawl_owned,
    _diagnose_failure,
    _diagnose_on,
    _diagnose_package,
)
from uiprobe.cli.output import (
    _emit,
    _error_object,
    _fail,
    _flush,
    _is_closed_output,
    _is_pipe,
    _json_requested,
    _printable,
    _report,
)
from uiprobe.cli.parser import (
    _DEFAULT_LIMITS,
    _MAX_MS,
    _NOT_CHECKED,
    _PRESS_LIMITS,
    _PRESS_SETTLE,
    _add_common_options,
    _add_crawl_parser,
    _add_diagnose_parser,
    _add_launch_parser,
    _add_press_options,
    _add_type_parser,
    _bounded_int,
    _build_parser,
    _GuidedUsageError,
    _Parser,
    _press_only,
    _settle_seconds,
    _UsageError,
    _VersionAction,
)
from uiprobe.crawl import Limits
from uiprobe.crawl_driver import (
    CrawlResult,
    _app_window,
    _check_device,
)
from uiprobe.crawl_driver import (
    crawl as crawl_run,
)
from uiprobe.crawl_preview import (
    MAX_PACKAGE_LENGTH,
    check_package,
)
from uiprobe.crawl_preview import (
    preview as make_preview,
)
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
    depends_on_a_drive_folder,
    save_png,
)
from uiprobe.runlog import RunFolderError, _check_folder, _new_folder
from uiprobe.settings import (
    _DEFAULT_ACTIONS,
    _DEFAULT_DEPTH,
    _DEFAULT_MINUTES,
    SETTINGS_FILE,
    IncompleteFileError,
    InterruptedFileError,
    Settings,
    SettingsError,
    _folder_form_base,
    _shown,
    _without_trailing_separators,
    load_settings,
    policy_from_settings,
    write_default_settings,
)
from uiprobe.windowdiag import APP_CRAWL_TESTED, APP_UID_UNREADABLE, TapWindowDiagnosis

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


__all__ = ["main", "run"]


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
    "diagnose": _cmd_diagnose,
    "init": _cmd_init,
}


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
    nothing that holds them. The same replacement is made whenever the
    arguments hold the word ``diagnose``, whose arguments can name the
    device.
    """
    where = _command_index(argv)
    is_type = where is not None and argv[where] == "type"
    # Any argv naming diagnose: an unknown option before it (--ser SERIAL)
    # can make argparse take its value for the command and quote it.
    is_diagnose = not is_type and "diagnose" in argv
    if is_type and where is not None and _device_option_after_type(argv[where + 1 :]):
        raise _UsageError(_DEVICE_OPTION_AFTER_TYPE)
    try:
        args = _build_parser().parse_args(argv)
    except _UsageError:
        if not (is_type or is_diagnose):
            raise
    else:
        _single_serial(args)
        return args
    raise _UsageError(_DIAGNOSE_USAGE_MESSAGE if is_diagnose else _TYPE_USAGE_MESSAGE)


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
