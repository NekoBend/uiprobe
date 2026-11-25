"""Reading the failure of an `input` or `am` command from its output."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from uiprobe.errors import InputFailedError

if TYPE_CHECKING:
    from collections.abc import Mapping


# Input commands. The device-side exit status is read through a marker, because
# the adb ``shell:`` service does not return it. Measured on API 24-36: the
# status is 0 even for a nonexistent ``input`` subcommand, so 0 does not prove
# success by itself; failure also prints usage text or an exception.
EXIT_MARKER: Final = "uiprobe-exit:"


_EXIT_SUFFIX: Final = f"; echo {EXIT_MARKER}$?"


_OUTPUT_LIMIT: Final = 200


_EXIT_VALUE: Final = re.compile(r"0|[1-9][0-9]{0,9}")


def _escaped(text: str, limit: int = 200) -> str:
    """Return `text` cut to `limit` characters and escaped to printable ASCII."""
    return ascii(text[:limit])[1:-1]


def _lines(text: str) -> list[str]:
    """Split shell output at line feeds only, dropping one CR at each line end."""
    return [line.removesuffix("\r") for line in text.split("\n")]


def _escaped_line(text: str) -> str:
    """Return the first line of `text` that is not blank, cut and escaped.

    Spaces and tabs around it are removed; a line made only of them is blank.
    """
    lines = (line.strip(" \t") for line in _lines(text))
    return _escaped(next((line for line in lines if line), ""))


def _input_failure(
    command: str, where: Mapping[str, object], text: str
) -> InputFailedError | None:
    """Judge the output of an input command; None means the device reported 0.

    `where` names what was sent (``point``, or ``from``, ``to`` and
    ``duration_ms``, or ``key``) and goes into the details after ``command``.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    lines = [line for line in lines if line]
    details: dict[str, object] = {"command": command, **where}
    marker = lines[-1] if lines and lines[-1].startswith(EXIT_MARKER) else None
    others = lines[:-1] if marker is not None else lines
    value = None if marker is None else marker[len(EXIT_MARKER) :]
    if value is None or not _EXIT_VALUE.fullmatch(value):
        reason = "no_exit_status"
        others = lines
    elif int(value) != 0:
        reason = "exit_status"
        details["exit_status"] = int(value)
    elif others:
        reason = "unexpected_output"
    else:
        return None
    details["reason"] = reason
    output = "\n".join(others).strip()[:_OUTPUT_LIMIT].rstrip()
    if output:
        details["output"] = output
    messages = {
        "no_exit_status": "the device did not report an exit status for the input",
        "exit_status": f"input failed on the device with exit status {value}",
        "unexpected_output": "input printed a line other than the exit status marker",
    }
    return InputFailedError(messages[reason], details=details)
