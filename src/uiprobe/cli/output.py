"""Shared output helpers: printing, error objects and closed-stream handling."""

from __future__ import annotations

import errno
import os
import stat
import sys
from typing import TYPE_CHECKING, TextIO

from uiprobe._json import dumps
from uiprobe.errors import ERROR_SCHEMA

if TYPE_CHECKING:
    from collections.abc import Sequence


def _printable(text: str) -> str:
    """Escape characters that must not reach a terminal raw."""
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


def _flush(stream: TextIO) -> None:
    """Flush `stream`; a closed output raises ``BrokenPipeError`` as in `_emit`."""
    try:
        stream.flush()
    except OSError as exc:
        if isinstance(exc, BrokenPipeError) or not _is_closed_output(exc, stream):
            raise
        raise BrokenPipeError(errno.EPIPE, "the output was closed") from exc


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
