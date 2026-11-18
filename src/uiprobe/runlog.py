"""Write one crawl run into a new folder.

`RunWriter` records what a crawl did: the settings it started with, every
action that was sent and where it led, every element that was not pressed, the
screens that were seen, and how the run ended. It does not talk to the device,
and nothing in the command line calls it yet.

Rules that hold for every file:

* The run folder is made with ``os.mkdir`` and must not exist; its parent must
  exist. Nothing is created silently and an existing folder is never reused.
* Every file is opened create-only (mode ``x``). No file is overwritten,
  renamed, truncated or deleted, also not after an error: a file that could not
  be completed stays as it is and the error names it.
* Only fixed names and screen ids (``s-`` and 12 lowercase hex digits) are ever
  used in a path. Text that comes from the app goes into file contents only.
* Text is UTF-8 with LF line ends on every platform. JSON goes through the
  project's JSON helper, which escapes control and bidirectional characters.
* Each line or file is flushed to the operating system and then fsynced before
  the method returns, so a line that was written before an action was sent is
  complete on disk if the program is killed right after. A run folder without
  ``run.json`` did not end normally.

A write that is under way can be marked with `RunWriter.critical`, so that a
Ctrl+C handler in the caller can wait until it is complete. This module
installs no signal handler.
"""

from __future__ import annotations

import contextlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final

from uiprobe._json import dumps
from uiprobe.crawl import Limits
from uiprobe.errors import UiprobeError
from uiprobe.model import Element, Screen, _name, _quote

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping, Sequence

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


def _check_screen_id(screen_id: object) -> str:
    if not isinstance(screen_id, str) or _SCREEN_ID.fullmatch(screen_id) is None:
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


def _element_dict(element: Element | Mapping[str, object] | None) -> dict[str, object]:
    if element is None:
        return {}
    if isinstance(element, Element):
        return element.to_dict()
    return dict(element)


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


def _make_dir(path: Path) -> None:
    """Make one new folder; its parent must exist and the folder must not."""
    os.mkdir(path)  # noqa: PTH102


def _put(handle: IO[Any], data: str | bytes) -> None:
    """Write `data` and push it to the disk before returning."""
    handle.write(data)
    handle.flush()
    os.fsync(handle.fileno())


def _label(element: Element) -> str:
    return element.text.strip() or element.content_desc.strip()


class RunWriter:
    """Writes the records of one crawl run into a new folder.

    Make one with `create`. Methods that write raise `RunFolderError` when a
    file cannot be completed, `ValueError` or `TypeError` for arguments that
    are wrong (nothing is written then).
    """

    def __init__(self, root: Path) -> None:
        """Use `create`; this only remembers the folder `create` made."""
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
    ) -> RunWriter:
        """Make the run folder `out` and write ``run-start.json`` into it.

        `limits` is a `crawl.Limits` or a dict with ``depth``, ``actions``
        and ``minutes``. `deny_words` are the words in use, written in the
        given order. `started_at` must carry a time zone (default: now).

        Raises:
            RunFolderError: `out` exists, its parent is missing, or the first
                file cannot be written. Nothing is written in the first two
                cases; no parent is ever created.
            TypeError, ValueError: an argument is wrong; nothing is written.
        """
        _check_text("package", package)
        _check_text("serial", serial)
        words = _check_texts("deny_words", deny_words)
        limit_values = _limits_dict(limits)
        when = _now() if started_at is None else started_at
        if when.tzinfo is None:
            msg = "started_at must have a time zone"
            raise ValueError(msg)
        path = Path(out).absolute()
        record: dict[str, object] = {
            "schema": RUN_START_SCHEMA,
            "started_at": _stamp(when),
            "package": package,
            "limits": limit_values,
            "deny": words,
            "device": {"serial": serial},
        }
        _encoded(dumps(record, indent=2))
        try:
            _make_dir(path)
        except OSError as error:
            msg = f"cannot make the run folder {path}: {error.strerror or error}"
            raise RunFolderError(msg, details={"path": str(path)}) from error
        writer = cls(path)
        writer._write_json("run-start.json", record)
        return writer

    # -- marking a write --

    @contextlib.contextmanager
    def critical(self) -> Generator[None]:
        """Mark the time while a line or file is being written.

        `writing` is True inside the block (blocks may nest). A Ctrl+C handler
        in the caller can look at it and postpone the interrupt until the
        write is complete. Nothing is installed here.
        """
        self._depth += 1
        try:
            yield
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

    # -- the records --

    def write_transition_start(
        self,
        action_id: int,
        kind: str,
        from_screen_id: str | None = None,
        element: Element | Mapping[str, object] | None = None,
        point: Sequence[int] | None = None,
    ) -> None:
        """Write the line that says an action is about to be sent.

        Call it before the action is sent. The line is complete on disk when
        this returns. `element` is an `Element` or its JSON form.
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
            action["element"] = _element_dict(element)
        line["action"] = action
        if from_screen_id is not None:
            line["from_screen_id"] = _check_screen_id(from_screen_id)
        self._append("transitions.jsonl", line)

    def write_transition_end(
        self,
        action_id: int,
        to_screen_id: str | None = None,
        *,
        dump_failed: bool = False,
    ) -> None:
        """Write the line that says where an action led.

        ``to_screen_id`` is required when ``dump_failed`` is False and must be
        None when it is True.
        """
        if not isinstance(dump_failed, bool):
            msg = "dump_failed must be a bool"
            raise TypeError(msg)
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
        self._append("transitions.jsonl", line)

    def write_skip(
        self,
        screen_id: str,
        fingerprint: str,
        reasons: Sequence[str],
        element: Element | Mapping[str, object],
    ) -> bool:
        """Write one element that was not pressed, once per screen, fingerprint
        and reasons. Return False, writing nothing, for a repeat."""
        checked = _check_screen_id(screen_id)
        _check_text("fingerprint", fingerprint)
        listed = tuple(_check_texts("reasons", reasons))
        body = _element_dict(element)
        key = (checked, fingerprint, listed)
        if key in self._skipped:
            return False
        self._append(
            "skipped.jsonl",
            {
                "schema": SKIP_SCHEMA,
                "recorded_at": _stamp(_now()),
                "screen_id": checked,
                "fingerprint": fingerprint,
                "reasons": list(listed),
                "element": body,
            },
        )
        self._skipped.add(key)
        return True

    def write_screen(self, screen: Screen) -> bool:
        """Write ``screens/<screen_id>.json`` the first time this id is seen.

        The content is the same as ``uiprobe dump --json``. Return False,
        writing nothing, when the id was seen before.
        """
        screen_id = _check_screen_id(screen.screen_id)
        if screen_id in self._screens:
            return False
        self._write_new(
            self._screen_path(screen_id, "json"),
            screen.to_json(indent=2) + "\n",
            in_screens=True,
        )
        self._screens[screen_id] = screen
        return True

    def write_screenshot(self, screen_id: str, png: bytes) -> bool:
        """Write ``screens/<screen_id>.png`` the first time this id is seen.

        Raises:
            ValueError: `png` does not start with the PNG signature; no file
                is made.
        """
        checked = _check_screen_id(screen_id)
        if not isinstance(png, bytes):
            msg = f"png must be bytes, not {type(png).__name__}"
            raise TypeError(msg)
        if not png.startswith(_PNG_SIGNATURE):
            msg = "not a PNG image"
            raise ValueError(msg)
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
    ) -> None:
        """End the run: write ``summary.txt``, then ``run.json``, once.

        ``run.json`` comes last, so a folder that has it is complete. A second
        call raises `RunFolderError`.
        """
        _check_text("stop_reason", stop_reason)
        screen_count = _check_number("screens", screens)
        action_count = _check_number("actions", actions)
        when = _now() if ended_at is None else ended_at
        if when.tzinfo is None:
            msg = "ended_at must have a time zone"
            raise ValueError(msg)
        self._check_usable()
        summary = _encoded(self._summary(stop_reason, screen_count, action_count))
        record: dict[str, object] = {
            "schema": RUN_SCHEMA,
            "ended_at": _stamp(when),
            "stop_reason": stop_reason,
            "screens": screen_count,
            "actions": action_count,
        }
        _encoded(dumps(record, indent=2))
        self._write_new(self._root / "summary.txt", summary)
        self._write_json("run.json", record)
        self._finished = True
        self.close()

    def close(self) -> None:
        """Close the open line files. It writes nothing and ends nothing."""
        with self.critical():
            first: tuple[str, OSError] | None = None
            for name, handle in self._streams.items():
                try:
                    handle.close()
                except OSError as error:
                    first = first or (name, error)
            self._streams.clear()
            self._closed = True
            if first is not None:
                raise self._broken(self._root / first[0], first[1]) from first[1]

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

    def _write_json(self, name: str, value: dict[str, object]) -> None:
        self._write_new(self._root / name, dumps(value, indent=2) + "\n")

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
            except OSError as error:
                raise self._broken(path, error) from error

    def _ensure_screens_folder(self, folder: Path) -> None:
        if not self._made_screens_folder:
            self._made_screens_folder = True
            _make_dir(folder)

    def _append(self, name: str, value: dict[str, object]) -> None:
        line = _encoded(dumps(value) + "\n")
        with self.critical():
            self._check_usable()
            path = self._root / name
            try:
                handle = self._streams.get(name)
                if handle is None:
                    handle = self._streams[name] = _create_text(path)
                _put(handle, line)
            except OSError as error:
                raise self._broken(path, error) from error

    def _summary(self, stop_reason: str, screens: int, actions: int) -> str:
        lines = [
            f"Stopped: {_name(stop_reason)}",
            f"Screens: {screens}",
            f"Actions: {actions}",
        ]
        for screen_id, screen in self._screens.items():
            head = _name(screen.package)
            if screen.activity is not None:
                head += "/" + _name(screen.activity)
            lines.extend(["", f"{head}  ({screen_id})"])
            labels = [
                _label(element)
                for element in screen.elements
                if "clickable" in element.flags and _label(element)
            ]
            lines.extend(f"  {_quote(label)}" for label in labels)
            if not labels:
                lines.append("  (no labelled buttons)")
        return "\n".join(lines) + "\n"
