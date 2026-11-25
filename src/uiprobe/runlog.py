"""Write one crawl run into a new folder.

`RunWriter` records what a crawl did: the settings it started with, every
action that was sent and where it led, every element that was not pressed, the
screens that were seen, and how the run ended. It does not talk to the device,
and nothing in the command line calls it yet.

Rules that hold for every file:

* The run folder is made with ``os.mkdir`` and must not exist; its parent must
  exist. Nothing is created silently and an existing folder is never reused.
  The run folder and ``screens`` are made with mode 0o700, so that only their
  owner can open them: the files hold screen text, which may be personal.
* Every file is opened create-only (mode ``x``). No file is overwritten,
  renamed, truncated or deleted, also not after an error: a file that could not
  be completed stays as it is and the error names it.
* Only fixed names and screen ids (``s-`` and 12 lowercase hex digits) are ever
  used in a path. Text that comes from the app goes into file contents only.
* Text is UTF-8 with LF line ends on every platform. JSON goes through the
  project's JSON helper, which escapes control and bidirectional characters.
  NaN and infinite numbers are refused by the encoder before any file is
  opened.
* Each line or file is flushed to the operating system and then fsynced before
  the method returns, so a line that was written before an action was sent is
  complete on disk if the program is killed right after. ``run.json`` is
  written last, after the line files are closed and ``summary.txt`` is
  written. A run folder whose ``run.json`` is missing or is not complete JSON
  did not end normally. The reverse does not hold: ``run.json`` can look
  complete although `RunWriter.finish` failed (at the final fsync, for
  example); only a `finish` that returned proves a normal end.

A write that is under way, together with the writer's record of it, is
marked with `RunWriter.critical`, so that a Ctrl+C handler in the caller can
wait until it is complete. This module installs no signal handler. An
interrupt (`KeyboardInterrupt`, `SystemExit`, ...) that reaches a write
propagates as itself, without ``details["path"]``, and the writer then
refuses every later write, as after a failed write.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final

from uiprobe._json import dumps
from uiprobe.crawl import Limits
from uiprobe.errors import UiprobeError
from uiprobe.guards import _WORD_REASONS
from uiprobe.model import Element, Screen, _name, _quote
from uiprobe.pngfile import check_new_path, depends_on_a_drive_folder

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Sequence

__all__ = [
    "RUN_SCHEMA",
    "RUN_START_SCHEMA",
    "SKIP_SCHEMA",
    "TRANSITION_END_SCHEMA",
    "TRANSITION_START_SCHEMA",
    "RunFolderError",
    "RunWriter",
]

RUN_START_SCHEMA: Final = "uiprobe.run_start/1"
TRANSITION_START_SCHEMA: Final = "uiprobe.transition_start/1"
TRANSITION_END_SCHEMA: Final = "uiprobe.transition_end/1"
SKIP_SCHEMA: Final = "uiprobe.skip/1"
RUN_SCHEMA: Final = "uiprobe.run/1"

_SCREEN_ID: Final = re.compile(r"s-[0-9a-f]{12}")
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
# Passed by `RunWriter.create` only: a writer cannot be made for a folder that
# `create` did not make itself.
_CREATED: Final = object()


class RunFolderError(UiprobeError):
    """The run folder cannot be made or a file of it cannot be completed.

    ``details["path"]`` names the file or folder. After a failed write the
    file stays as far as it got, and the writer refuses every later write.
    """

    code = "run_folder_error"
    exit_code = 1


def _now() -> datetime:
    return datetime.now(UTC).astimezone()


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _check_moment(name: str, moment: datetime) -> datetime:
    if not isinstance(moment, datetime):
        msg = f"{name} must be a datetime, not {type(moment).__name__}"
        raise TypeError(msg)
    if moment.utcoffset() is None:
        msg = f"{name} must have a time zone with a UTC offset"
        raise ValueError(msg)
    return moment


def _check_screen_id(screen_id: object) -> str:
    """Return `screen_id` if it is a plain `str` of the screen id form.

    A subclass of `str` (a str-valued enum, for example) is refused: its
    formatting could differ from the value that was checked.
    """
    if type(screen_id) is not str:
        msg = f"a screen id must be a str, not {type(screen_id).__name__}"
        raise TypeError(msg)
    if _SCREEN_ID.fullmatch(screen_id) is None:
        msg = "a screen id is 's-' and 12 lowercase hex digits"
        raise ValueError(msg)
    return screen_id


def _check_text(name: str, value: object) -> str:
    if not isinstance(value, str):
        msg = f"{name} must be a str, not {type(value).__name__}"
        raise TypeError(msg)
    return _encoded(value)


def _encoded(text: str) -> str:
    """Return `text` after proving it can be written as UTF-8.

    Done before any file is opened, so text that cannot be encoded (a lone
    surrogate, for example) never leaves an empty or partial file.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        msg = "text cannot be written as UTF-8 (it holds a lone surrogate)"
        raise ValueError(msg) from error
    return text


def _json_text(value: dict[str, object], *, indent: int | None = None) -> str:
    """Return `value` as JSON text, in one pass of the project's JSON helper.

    NaN and infinite numbers raise `ValueError` from the encoder itself;
    other encoder errors (a circular mapping, a value JSON cannot hold) are
    raised as they come. Text that cannot be encoded as UTF-8 is refused.
    """
    return _encoded(dumps(value, indent=indent, allow_nan=False))


def _check_details(details: Mapping[str, str | int]) -> dict[str, object]:
    checked: dict[str, object] = {}
    for key, value in details.items():
        _check_text("details key", key)
        if isinstance(value, bool) or not isinstance(value, str | int):
            msg = f"details values must be str or int, not {type(value).__name__}"
            raise TypeError(msg)
        checked[key] = (
            _check_text("details value", value) if isinstance(value, str) else value
        )
    return checked


def _check_number(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    if value < 0:
        msg = f"{name} must not be negative, got {value}"
        raise ValueError(msg)
    return value


def _check_action_id(action_id: object) -> int:
    number = _check_number("action_id", action_id)
    if number < 1:
        msg = "action_id counts from 1"
        raise ValueError(msg)
    return number


def _check_texts(name: str, values: object) -> list[str]:
    if isinstance(values, str) or not hasattr(values, "__iter__"):
        msg = f"{name} must be a list of str, not {type(values).__name__}"
        raise TypeError(msg)
    return [_check_text(f"an item of {name}", item) for item in values]  # ty: ignore[not-iterable]


_SKIP_DETAIL_KEYS: Final = ("reason", "word", "field", "element_id", "note")


def _check_skip_details(
    details: object, reasons: tuple[str, ...]
) -> list[dict[str, str]]:
    """Check the details of a skip line; return them as plain dicts.

    Each item is a mapping with a ``reason`` and, optionally, ``word``,
    ``field``, ``element_id`` and ``note``; every value is a str that can be
    written as UTF-8. The keys are written in that order. The reason must be
    one of the line's `reasons` and one of ``deny_word``,
    ``irreversible_screen`` and ``confirm_button``.
    """
    if isinstance(details, str | Mapping) or not hasattr(details, "__iter__"):
        msg = f"details must be a list of mappings, not {type(details).__name__}"
        raise TypeError(msg)
    checked: list[dict[str, str]] = []
    for item in details:  # ty: ignore[not-iterable]
        if not isinstance(item, Mapping):
            msg = f"an item of details must be a mapping, not {type(item).__name__}"
            raise TypeError(msg)
        unknown = [key for key in item if key not in (*_SKIP_DETAIL_KEYS, "hidden")]
        if unknown or "reason" not in item:
            msg = (
                "an item of details needs a reason and may hold only"
                f" {', '.join(_SKIP_DETAIL_KEYS)}"
            )
            raise ValueError(msg)
        entry = {
            key: _check_text(f"details {key}", item[key])
            for key in _SKIP_DETAIL_KEYS
            if key in item
        }
        if "hidden" in item:
            if item["hidden"] != ["note"] or item.get("note") != "":
                msg = 'hidden may only be ["note"], with a note of ""'
                raise ValueError(msg)
            entry["hidden"] = ["note"]  # ty: ignore[invalid-assignment]
        if entry["reason"] not in _WORD_REASONS or entry["reason"] not in reasons:
            msg = (
                "the reason of a detail must be one of the line's reasons and one"
                " of deny_word, irreversible_screen and confirm_button"
            )
            raise ValueError(msg)
        checked.append(entry)
    return checked


_TEXT_FIELDS: Final = ("text", "content_desc", "hint")


def _hide_texts(data: dict[str, object], *, record_text: bool) -> dict[str, object]:
    """Blank the texts of an element's JSON form that must not be recorded.

    A field with the ``password`` flag always loses its ``text``; with
    `record_text` False ``text``, ``content_desc`` and ``hint`` go. A hidden
    field that held something is written as ``""`` and named in ``hidden``
    (in field order); a field that was empty stays as it was and is not named.
    """
    flags = data.get("flags")
    password = isinstance(flags, list | tuple | set | frozenset) and (
        "password" in flags
    )
    names = _TEXT_FIELDS if not record_text else ("text",) if password else ()
    hidden = [name for name in names if data.get(name)]
    for name in hidden:
        data[name] = ""
    if hidden:
        data["hidden"] = hidden
    return data


def _element_dict(
    element: Element | Mapping[str, object] | None, *, record_text: bool = True
) -> dict[str, object]:
    if element is None:
        return {}
    data = element.to_dict() if isinstance(element, Element) else dict(element)
    return _hide_texts(data, record_text=record_text)


def _check_point(point: Sequence[int] | None) -> list[int] | None:
    if point is None:
        return None
    values = list(point)
    if len(values) != 2:
        msg = "point must be (x, y)"
        raise ValueError(msg)
    return [_check_number("point", value) for value in values]


def _limits_dict(limits: Limits | dict[str, int]) -> dict[str, int]:
    """Read depth, actions and minutes from a `Limits` or a mapping."""
    if isinstance(limits, Limits):
        values: dict[str, object] = {
            "depth": limits.depth,
            "actions": limits.actions,
            "minutes": limits.minutes,
        }
    elif isinstance(limits, dict):
        values = dict(limits)
    else:
        msg = f"limits must be a Limits or a dict, not {type(limits).__name__}"
        raise TypeError(msg)
    checked = {
        key: _check_number(f"limits.{key}", values.get(key))
        for key in ("depth", "actions", "minutes")
    }
    for key, number in checked.items():
        if number < 1:
            msg = f"limits.{key} must be at least 1, got {number}"
            raise ValueError(msg)
    return checked


def _create_text(path: Path) -> IO[str]:
    """Open a new text file for writing; an existing file is an error."""
    return open(path, "x", encoding="utf-8", newline="\n")  # noqa: PTH123


def _create_binary(path: Path) -> IO[bytes]:
    """Open a new binary file for writing; an existing file is an error."""
    return open(path, "xb")  # noqa: PTH123


def _check_folder(path: str) -> None:
    """Run `check_new_path` on `path`; a refusal is a `RunFolderError`."""
    details = {"path": path}
    try:
        check_new_path(path)
    except FileExistsError as error:
        msg = (
            f"cannot make the run folder {path}: already exists; give a new folder name"
        )
        raise RunFolderError(msg, details=details) from error
    except ValueError as error:
        msg = f"cannot make the run folder {path}: {error}"
        raise RunFolderError(msg, details=details) from error
    except OSError as error:
        msg = f"cannot check {path}: {error.strerror or error}"
        raise RunFolderError(msg, details=details) from error


def _new_folder(out: object) -> str:
    """Return the absolute path of the new run folder `out`, checked.

    `out` is read once with `os.fspath` and kept as a plain str, so a str or
    `os.PathLike` subclass cannot hand over one spelling for the check and
    another for the creation. That spelling is checked as given; a relative
    one is then joined to the working folder (without normalizing it) and
    checked again. The caller makes the folder with exactly the returned str.

    Raises:
        TypeError: `out` is not a str or an `os.PathLike` of str.
        RunFolderError: a check refused the path, or the working folder
            cannot be read.
    """
    if not isinstance(out, str | os.PathLike):
        msg = f"out must be a str or an os.PathLike of str, not {type(out).__name__}"
        raise TypeError(msg)
    text = os.fspath(out)
    if not isinstance(text, str):
        msg = f"out must name a path as str, not {type(text).__name__}"
        raise TypeError(msg)
    text = str.__str__(text)  # a plain str, whatever the subclass
    _check_folder(text)
    if depends_on_a_drive_folder(text):
        msg = (
            f"cannot make the run folder {text}: on Windows give a full path"
            " with its drive (such as C:\\runs\\x) or a path relative to the"
            " working folder (such as runs\\x)"
        )
        raise RunFolderError(msg, details={"path": text})
    if os.path.isabs(text):  # noqa: PTH117 - Path would normalize the text
        return text
    try:
        folder = os.path.join(os.getcwd(), text)  # noqa: PTH109, PTH118
    except OSError as error:
        msg = (
            f"cannot make the run folder {text}: the working folder cannot be"
            f" read ({error.strerror or error})"
        )
        raise RunFolderError(msg, details={"path": text}) from error
    _check_folder(folder)
    return folder


def _make_dir(path: str | Path) -> None:
    """Make one new folder that only its owner can open.

    Its parent must exist and the folder must not. Mode 0o700 keeps other
    users out on POSIX (the umask can only narrow it); on Windows, Python
    3.12.4 and later give the folder access control for its owner, the
    Administrators group and SYSTEM only, and older 3.12 releases ignore the
    mode.
    """
    os.mkdir(path, 0o700)  # noqa: PTH102


def _put(handle: IO[Any], data: str | bytes) -> None:
    """Write `data` and push it to the disk before returning."""
    handle.write(data)
    handle.flush()
    os.fsync(handle.fileno())


def _label(element: Element, *, record_text: bool = True) -> str:
    if "password" in element.flags:
        return "[password field]"
    label = element.text.strip() or element.content_desc.strip()
    return label if label and record_text else "[text not recorded]" if label else ""


class RunWriter:
    """Writes the records of one crawl run into a new folder.

    Make one with `create`; calling the class directly raises `TypeError`.
    Methods that write raise `RunFolderError` when a file cannot be completed,
    `ValueError` or `TypeError` for arguments that are wrong (nothing is
    written then).
    """

    def __init__(self, root: Path, *, _token: object = None) -> None:
        """Use `create`; this only remembers the folder `create` made."""
        if _token is not _CREATED:
            msg = "make a RunWriter with RunWriter.create"
            raise TypeError(msg)
        self._root = root
        self._depth = 0
        self._failed: Path | None = None
        self._finished = False
        self._closed = False
        self._streams: dict[str, IO[str]] = {}
        self._screens: dict[str, Screen] = {}
        self._pictures: set[str] = set()
        self._skipped: set[tuple[str, str, tuple[str, ...]]] = set()
        self._made_screens_folder = False
        self._starts_written = 0
        self._record_text = True

    @classmethod
    def create(
        cls,
        out: str | os.PathLike[str],
        *,
        package: str,
        limits: Limits | dict[str, int],
        deny_words: Iterable[str],
        serial: str,
        started_at: datetime | None = None,
        back_on_other_screens: bool = False,
        repin: bool | None = None,
        check_tap_window: bool | None = None,
        deny_exceptions: Iterable[str] = (),
        restart: bool | None = None,
        record_text: bool | None = None,
    ) -> RunWriter:
        """Make the run folder `out` and write ``run-start.json`` into it.

        `limits` is a `crawl.Limits` or a dict with ``depth``, ``actions``
        and ``minutes``. `deny_words` are the words in use, written in the
        given order, and `deny_exceptions` the exception words in use
        (``Policy.deny_exceptions``), written as ``deny_except`` in the given
        order. `started_at` must carry a time zone (default: now).
        `back_on_other_screens` is written as given (the crawl's
        ``Policy.back_on_other_screens``). `repin`, when given, is written as
        ``repin``: whether a renumbered transport may be followed in this run
        (the device's boot id could be read). `check_tap_window`, when
        given, is written as ``check_tap_window`` (the crawl's
        ``Policy.check_tap_window``). `restart`, when given, is written as
        ``restart``: whether the crawl force-stops the app before its first
        start. `record_text`, when given, is written as ``record_text`` and
        decides whether the texts of screens are kept (see `RunWriter` and
        the run folder page): False writes every element's ``text``,
        ``content_desc`` and ``hint`` as ``""`` (named in ``hidden``), the
        label lines of ``summary.txt`` as ``[text not recorded]``, the
        ``window`` of a ``covered_by_other`` end line as ``""``, and refuses
        `write_screenshot`. None is True. A password field's text is
        hidden either way.

        Before anything is made, `out` gets the checks of
        `uiprobe.pngfile.check_new_path`: it must not be empty, hold a null
        character or, on Windows, name a stream of a file or folder (a colon
        after the drive), and nothing may be at it, a broken link included.
        `out` is read once with `os.fspath` and used as a plain str. It is
        checked as given, and a relative path again after it is joined to the
        working folder (not normalized); the folder is made with that same
        str. A path that would pass only after normalizing it is refused. On
        Windows a path relative to a drive's own folder (``C:run``, a drive
        without a separator after it, or ``\\run``, a separator without a
        drive) is refused, so the folder never depends on which folder or
        drive is current later; a relative path is fixed to the working
        folder when the run starts.

        Raises:
            RunFolderError: `out` fails those checks, is on Windows relative
                to a drive's own folder (``C:run``, ``\\run``), whether
                something is at it cannot be told (``cannot check``), the
                working folder cannot be read, its parent is missing, or the
                first file cannot be written. Nothing is written in any case
                but the last; no parent is ever created.
            TypeError, ValueError: an argument is wrong (`out` that is not a
                str or an os.PathLike of str is a `TypeError`); nothing is
                written.
        """
        _check_text("package", package)
        _check_text("serial", serial)
        if not isinstance(back_on_other_screens, bool):
            msg = "back_on_other_screens must be a bool"
            raise TypeError(msg)
        if repin is not None and not isinstance(repin, bool):
            msg = "repin must be a bool or None"
            raise TypeError(msg)
        if check_tap_window is not None and not isinstance(check_tap_window, bool):
            msg = "check_tap_window must be a bool or None"
            raise TypeError(msg)
        if restart is not None and not isinstance(restart, bool):
            msg = "restart must be a bool or None"
            raise TypeError(msg)
        if record_text is not None and not isinstance(record_text, bool):
            msg = "record_text must be a bool or None"
            raise TypeError(msg)
        words = _check_texts("deny_words", deny_words)
        exceptions = _check_texts("deny_exceptions", deny_exceptions)
        limit_values = _limits_dict(limits)
        when = _check_moment("started_at", _now() if started_at is None else started_at)
        folder = _new_folder(out)
        record: dict[str, object] = {
            "schema": RUN_START_SCHEMA,
            "started_at": _stamp(when),
            "package": package,
            "limits": limit_values,
            "deny": words,
            "deny_except": exceptions,
            "back_on_other_screens": back_on_other_screens,
            "device": {"serial": serial},
        }
        if repin is not None:
            record["repin"] = repin
        if check_tap_window is not None:
            record["check_tap_window"] = check_tap_window
        if restart is not None:
            record["restart"] = restart
        if record_text is not None:
            record["record_text"] = record_text
        text = _json_text(record, indent=2) + "\n"
        try:
            _make_dir(folder)  # the very str that was checked
        except OSError as error:
            msg = f"cannot make the run folder {folder}: {error.strerror or error}"
            raise RunFolderError(msg, details={"path": folder}) from error
        root = Path(folder)
        writer = cls(root, _token=_CREATED)
        writer._record_text = record_text is not False
        writer._write_new(root / "run-start.json", text)
        return writer

    # -- marking a write --

    @contextlib.contextmanager
    def critical(self) -> Generator[None]:
        """Mark the time while a line or file is being written.

        `writing` is True inside the block (blocks may nest). Each write
        method keeps the write and the writer's record of it (for example
        that a screen was seen) inside one block. A Ctrl+C handler in the
        caller can look at `writing` and postpone the interrupt until the
        write is complete. Nothing is installed here.

        An exception that is not an `Exception` (`KeyboardInterrupt`,
        `SystemExit`, ...) leaving the block marks the writer as failed: a
        write or its record may be incomplete, so every later write is
        refused. The exception itself propagates unchanged. This mark is best
        effort only: an asynchronous exception can arrive at any point,
        including after the mark would be made. The reliable way is the one
        above: postpone Ctrl+C while `writing` is True.
        """
        self._depth += 1
        try:
            yield
        except BaseException as error:
            if not isinstance(error, Exception) and self._failed is None:
                self._failed = self._root
            raise
        finally:
            self._depth -= 1

    @property
    def writing(self) -> bool:
        """True while a write is under way (see `critical`)."""
        return self._depth > 0

    @property
    def path(self) -> Path:
        """The run folder."""
        return self._root

    @property
    def starts_written(self) -> int:
        """How many transition start lines are complete on disk."""
        return self._starts_written

    # -- the records --

    def write_transition_start(
        self,
        action_id: int,
        kind: str,
        from_screen_id: str | None = None,
        element: Element | Mapping[str, object] | None = None,
        point: Sequence[int] | None = None,
        *,
        off_target_package: str | None = None,
        off_target_foreground: str | None = None,
        off_target_focus: str | None = None,
        restart: bool = False,
        replay: bool = False,
    ) -> None:
        """Write the line that says an action is about to be sent.

        Call it before the action is sent. The line is complete on disk when
        this returns. `element` is an `Element` (written in its
        ``uiprobe.screen/1`` form) or a mapping, written as given.

        `off_target_package` is given when the action was decided on a screen
        of another app: the package of that screen's dump. It is written as
        ``off_target`` with ``package`` and, when given,
        `off_target_foreground` (the package in front) as ``foreground`` and
        `off_target_focus` (the owner of the window with input focus, where a
        BACK goes) as ``focus``.

        `restart` is True for a launch that force-stops the app before it
        starts it: the line then has ``restart: true`` (the key is absent
        otherwise).

        `replay` is True for a tap that presses an element again to get back
        down to a screen after a relaunch (a path replay): the line then has
        ``replay: true`` (the key is absent otherwise). It needs `kind`
        ``tap``.

        Raises:
            TypeError: `restart` or `replay` is not a bool.
            ValueError: `off_target_foreground` or `off_target_focus` is given
                without `off_target_package`, or `replay` is True for a kind
                other than ``tap``.
        """
        line: dict[str, object] = {
            "schema": TRANSITION_START_SCHEMA,
            "recorded_at": _stamp(_now()),
            "action_id": _check_action_id(action_id),
        }
        action: dict[str, object] = {"kind": _check_text("kind", kind)}
        checked_point = _check_point(point)
        if checked_point is not None:
            action["point"] = checked_point
        if element is not None:
            action["element"] = _element_dict(element, record_text=self._record_text)
        line["action"] = action
        if from_screen_id is not None:
            line["from_screen_id"] = _check_screen_id(from_screen_id)
        if off_target_package is not None:
            other: dict[str, object] = {
                "package": _check_text("off_target_package", off_target_package)
            }
            if off_target_foreground is not None:
                other["foreground"] = _check_text(
                    "off_target_foreground", off_target_foreground
                )
            if off_target_focus is not None:
                other["focus"] = _check_text("off_target_focus", off_target_focus)
            line["off_target"] = other
        elif off_target_foreground is not None or off_target_focus is not None:
            msg = "off_target_foreground and off_target_focus need off_target_package"
            raise ValueError(msg)
        if not isinstance(restart, bool):
            msg = "restart must be a bool"
            raise TypeError(msg)
        if restart:
            line["restart"] = True
        if not isinstance(replay, bool):
            msg = "replay must be a bool"
            raise TypeError(msg)
        if replay:
            if action["kind"] != "tap":
                msg = "replay needs the kind tap"
                raise ValueError(msg)
            line["replay"] = True
        self._append("transitions.jsonl", _json_text(line) + "\n", start=True)

    def write_transition_end(
        self,
        action_id: int,
        to_screen_id: str | None = None,
        *,
        dump_failed: bool = False,
        not_sent: str | None = None,
        details: Mapping[str, str | int] | None = None,
        failed: str | None = None,
    ) -> None:
        """Write the line that says where an action led, or that it was not sent.

        ``to_screen_id`` is required when ``dump_failed`` is False and must be
        None when it is True. ``not_sent`` is the reason (a short word) when
        the action whose start line was written was then not sent; the line
        then has ``not_sent: true`` and ``reason``, and neither ``dump_failed``
        nor ``to_screen_id`` (both must be left at their defaults).
        `details`, only with `not_sent`, is written as ``details``: short
        words or numbers that say more about why (for example the window that
        would have taken a tap). ``failed`` is the reason (a short word) when
        sending the action failed and the run ends: the line then has
        ``failed: true``, ``reason`` and ``outcome: "unknown"`` (the device
        may have acted on it), and nothing else may be given.
        """
        if not isinstance(dump_failed, bool):
            msg = "dump_failed must be a bool"
            raise TypeError(msg)
        if failed is not None:
            _check_text("failed", failed)
            if dump_failed or to_screen_id is not None or not_sent or details:
                msg = "a failed action has only its reason"
                raise ValueError(msg)
            line_failed: dict[str, object] = {
                "schema": TRANSITION_END_SCHEMA,
                "recorded_at": _stamp(_now()),
                "action_id": _check_action_id(action_id),
                "failed": True,
                "reason": failed,
                "outcome": "unknown",
            }
            self._append("transitions.jsonl", _json_text(line_failed) + "\n")
            return
        if details is not None and not_sent is None:
            msg = "details are written only for an action that was not sent"
            raise ValueError(msg)
        if not_sent is not None:
            _check_text("not_sent", not_sent)
            if dump_failed or to_screen_id is not None:
                msg = "an action that was not sent has no destination"
                raise ValueError(msg)
            skipped: dict[str, object] = {
                "schema": TRANSITION_END_SCHEMA,
                "recorded_at": _stamp(_now()),
                "action_id": _check_action_id(action_id),
                "not_sent": True,
                "reason": not_sent,
            }
            if details:
                shown = _check_details(details)
                if not self._record_text and shown.get("window"):
                    shown["window"] = ""
                    shown["hidden"] = ["window"]
                skipped["details"] = shown
            self._append("transitions.jsonl", _json_text(skipped) + "\n")
            return
        line: dict[str, object] = {
            "schema": TRANSITION_END_SCHEMA,
            "recorded_at": _stamp(_now()),
            "action_id": _check_action_id(action_id),
            "dump_failed": dump_failed,
        }
        if dump_failed:
            if to_screen_id is not None:
                msg = "to_screen_id must be None when the dump failed"
                raise ValueError(msg)
        else:
            line["to_screen_id"] = _check_screen_id(to_screen_id)
        self._append("transitions.jsonl", _json_text(line) + "\n")

    def write_skip(
        self,
        screen_id: str,
        fingerprint: str,
        reasons: Sequence[str],
        element: Element | Mapping[str, object],
        details: Sequence[Mapping[str, str | list[str]]] = (),
    ) -> bool:
        """Write one element that was not pressed, once per screen, fingerprint
        and reasons. Return False, writing nothing, for a repeat.

        `details`, when not empty, is written as ``details``: a list of
        objects with ``reason`` and, when given, ``word``, ``field``,
        ``element_id`` and ``note`` (all strings; the form of
        `uiprobe.guards.Detail.to_dict`). A repeat keeps the first line's
        details.
        """
        checked = _check_screen_id(screen_id)
        _check_text("fingerprint", fingerprint)
        listed = tuple(_check_texts("reasons", reasons))
        explained = _check_skip_details(details, listed)
        if not self._record_text:
            for entry in explained:
                if entry.get("note"):
                    entry["note"] = ""
                    entry["hidden"] = ["note"]  # ty: ignore[invalid-assignment]
        key = (checked, fingerprint, listed)
        with self.critical():
            self._check_usable()
            if key in self._skipped:
                return False
            body = _element_dict(element, record_text=self._record_text)
            record: dict[str, object] = {
                "schema": SKIP_SCHEMA,
                "recorded_at": _stamp(_now()),
                "screen_id": checked,
                "fingerprint": fingerprint,
                "reasons": list(listed),
            }
            if explained:
                record["details"] = explained
            record["element"] = body
            line = _json_text(record)
            # Reading the element may have called back into this writer, which
            # may since have failed or been closed.
            self._check_usable()
            if key in self._skipped:
                return False
            self._append("skipped.jsonl", line + "\n")
            self._skipped.add(key)
        return True

    def write_screen(self, screen: Screen) -> bool:
        """Write ``screens/<screen_id>.json`` the first time this id is seen.

        The content is the same as ``uiprobe dump --json``. Return False,
        writing nothing, when the id was seen before.
        """
        screen_id = _check_screen_id(screen.screen_id)
        with self.critical():
            self._check_usable()
            if screen_id in self._screens:
                return False
            data = screen.to_dict()
            data["elements"] = [
                _hide_texts(dict(item), record_text=self._record_text)
                for item in data["elements"]  # ty: ignore[not-iterable]
            ]
            text = _json_text(data, indent=2) + "\n"
            self._write_new(self._screen_path(screen_id, "json"), text, in_screens=True)
            self._screens[screen_id] = screen
        return True

    def write_screenshot(self, screen_id: str, png: bytes) -> bool:
        """Write ``screens/<screen_id>.png`` the first time this id is seen.

        Raises:
            ValueError: `png` does not start with the PNG signature; no file
                is made.
        """
        checked = _check_screen_id(screen_id)
        if not self._record_text:
            msg = "screenshots show the screen's text; this run does not record it"
            raise ValueError(msg)
        if not isinstance(png, bytes):
            msg = f"png must be bytes, not {type(png).__name__}"
            raise TypeError(msg)
        if not png.startswith(_PNG_SIGNATURE):
            msg = "not a PNG image"
            raise ValueError(msg)
        with self.critical():
            self._check_usable()
            if checked in self._pictures:
                return False
            self._write_new(self._screen_path(checked, "png"), png, in_screens=True)
            self._pictures.add(checked)
        return True

    def finish(
        self,
        stop_reason: str,
        *,
        screens: int,
        actions: int,
        ended_at: datetime | None = None,
        notes: Sequence[str] = (),
        repins: int | None = None,
    ) -> None:
        """End the run once: close the line files, write ``summary.txt``,
        then ``run.json``.

        ``run.json`` comes last. If closing a line file or writing
        ``summary.txt`` fails, no ``run.json`` is made, so a missing or
        malformed ``run.json`` means the run did not end normally. The reverse
        does not hold: a failure at the final fsync of ``run.json`` raises
        `RunFolderError` after its bytes are already in the file. A second
        call raises `RunFolderError`.
        `notes` are extra lines for ``summary.txt``, written after the
        counts (for an error code or a screenshot that could not be
        taken); they are escaped like labels. `repins`, when given, is
        written to ``run.json`` as ``repins`` (how many times the pinned
        transport was followed to a new number).
        """
        _check_text("stop_reason", stop_reason)
        screen_count = _check_number("screens", screens)
        action_count = _check_number("actions", actions)
        repin_count = None if repins is None else _check_number("repins", repins)
        when = _check_moment("ended_at", _now() if ended_at is None else ended_at)
        self._check_usable()
        note_lines = _check_texts("notes", notes)
        summary = _encoded(
            self._summary(stop_reason, screen_count, action_count, note_lines)
        )
        record: dict[str, object] = {
            "schema": RUN_SCHEMA,
            "ended_at": _stamp(when),
            "stop_reason": stop_reason,
            "screens": screen_count,
            "actions": action_count,
        }
        if repin_count is not None:
            record["repins"] = repin_count
        text = _json_text(record, indent=2) + "\n"
        with self.critical():
            self._check_usable()
            self._close_streams()
            self._write_new(self._root / "summary.txt", summary)
            self._write_new(self._root / "run.json", text)
            self._finished = True
            self._closed = True

    def close(self) -> None:
        """Close the open line files. It writes nothing and ends nothing."""
        with self.critical():
            self._closed = True
            self._close_streams()

    def __enter__(self) -> RunWriter:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.close()

    # -- internals --

    def _check_usable(self) -> None:
        if self._failed is not None:
            msg = f"an earlier write failed and left {self._failed} incomplete"
            raise RunFolderError(msg, details={"path": str(self._failed)})
        if self._finished:
            msg = "the run is already finished"
            raise RunFolderError(msg, details={"path": str(self._root)})
        if self._closed:
            msg = "the writer is closed"
            raise RunFolderError(msg, details={"path": str(self._root)})

    def _broken(self, path: Path, error: OSError) -> RunFolderError:
        self._failed = path
        msg = f"cannot complete {path}: {error.strerror or error}"
        return RunFolderError(msg, details={"path": str(path)})

    def _screen_path(self, screen_id: str, suffix: str) -> Path:
        return self._root / "screens" / f"{screen_id}.{suffix}"

    def _close_streams(self) -> None:
        """Close the line files; the first failure breaks the writer."""
        first: tuple[str, OSError] | None = None
        while self._streams:
            # A handle leaves the cache only once its close() has returned or
            # raised: an interrupt before that leaves it, and the ones after
            # it, reachable for a later close().
            name, handle = next(iter(self._streams.items()))
            close = handle.close
            try:
                close()
            except OSError as error:
                if first is None:
                    first = (name, error)
                    # Record it now: a later failure while closing the others
                    # must not leave the writer usable.
                    self._failed = self._root / name
            except BaseException:
                if first is not None:
                    raise

                self._failed = self._root / name
                raise
            finally:
                self._streams.pop(name, None)
        if first is not None:
            raise self._broken(self._root / first[0], first[1]) from first[1]

    def _drop_stream(self, name: str) -> None:
        """Forget a line file after a failure and close it now.

        Closing may flush bytes that are still buffered; doing it here, before
        the error is raised, means nothing reaches the file afterwards. The
        caller has marked the writer as failed already, so an interrupt during
        this close cannot leave it usable.
        """
        handle = self._streams.pop(name, None)
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()

    def _write_new(
        self, path: Path, data: str | bytes, *, in_screens: bool = False
    ) -> None:
        if isinstance(data, str):
            _encoded(data)
        with self.critical():
            self._check_usable()
            try:
                if in_screens:
                    self._ensure_screens_folder(path.parent)
                with (
                    _create_binary(path)
                    if isinstance(data, bytes)
                    else _create_text(path)
                ) as handle:
                    _put(handle, data)
            except BaseException as error:
                self._failed = path
                if isinstance(error, OSError):
                    raise self._broken(path, error) from error
                raise

    def _ensure_screens_folder(self, folder: Path) -> None:
        if not self._made_screens_folder:
            self._made_screens_folder = True
            _make_dir(folder)

    def _append(self, name: str, line: str, *, start: bool = False) -> None:
        _encoded(line)
        with self.critical():
            self._check_usable()
            path = self._root / name
            try:
                handle = self._streams.get(name)
                if handle is None:
                    handle = self._streams[name] = _create_text(path)
                _put(handle, line)
                if start:  # counted inside the same marked block as the write
                    self._starts_written += 1
            except BaseException as error:
                self._failed = path  # before the cleanup, which may be interrupted
                self._drop_stream(name)
                if isinstance(error, OSError):
                    raise self._broken(path, error) from error
                raise

    def _summary(
        self, stop_reason: str, screens: int, actions: int, notes: Sequence[str]
    ) -> str:
        lines = [
            f"Stopped: {_name(stop_reason)}",
            f"Screens: {screens}",
            f"Actions: {actions}",
        ]
        lines.extend(f"Note: {_quote(note)}" for note in notes)
        for screen_id, screen in self._screens.items():
            head = _name(screen.package)
            if screen.activity is not None:
                head += "/" + _name(screen.activity)
            lines.extend(["", f"{head}  ({screen_id})"])
            labels = [
                label
                for element in screen.elements
                if "clickable" in element.flags
                and (label := _label(element, record_text=self._record_text))
            ]
            lines.extend(f"  {_quote(label)}" for label in labels)
            if not labels:
                lines.append("  (no labelled buttons)")
        return "\n".join(lines) + "\n"
