"""Typing text: the checks, the pieces it is sent in, and why typing stopped."""

from __future__ import annotations

from typing import Final

from uiprobe.device.failure import _input_failure
from uiprobe.device.select import PIN_NOT_PROVEN
from uiprobe.errors import (
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    InputFailedError,
    UiprobeError,
)

TYPE_PIECE_SIZE: Final = 256
"""Most characters of text sent in one ``input text`` command."""


class TextNotTypedError(ValueError):
    """The text has a character, or is empty, so that it is not typed."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = details or {}


def _check_text(text: str) -> str:
    """Return `text` when it can be typed; raise before anything is sent."""
    if type(text) is not str:  # `_plain_text` has made it one
        msg = f"text must be a plain str, not {type(text).__name__}"
        raise TypeError(msg)
    if not text:
        msg = "text is empty; there is nothing to type"
        raise TextNotTypedError(msg)
    for position, char in enumerate(text):
        code = ord(char)
        if 0x20 <= code <= 0x7E:
            continue
        label = f"U+{code:04X}"
        escaped = ascii(char)
        head = f"character {label} ({escaped}) at position {position}"
        if code > 0x7F:
            msg = f"{head} is not ASCII; this version types ASCII text only"
        elif char in "\n\t":
            msg = (
                f"{head} is not typed: a newline or tab can act as Enter or"
                " move the focus; to press Enter use uiprobe key enter"
            )
        else:
            msg = f"{head} is a control character; only printable ASCII is typed"
        details: dict[str, object] = {
            "character": label,
            "escaped": escaped,
            "position": position,
        }
        raise TextNotTypedError(msg, details)
    return text


def _single_quoted(piece: str) -> str:
    """`piece` in single quotes for the device shell, even a plain word."""
    return "'" + "'\"'\"'".join(piece.split("'")) + "'"


def _text_pieces(text: object) -> list[str]:
    """Split `text` into the pieces to send, after checking it.

    `text` is first made a plain str (see `_plain_text`); the check and the
    split read only that str.

    A piece has at most `TYPE_PIECE_SIZE` characters and never contains ``%``
    directly followed by ``s`` (the device turns that pair into a space). The
    pieces joined give `text` again.
    """
    plain = _check_text(_plain_text(text))
    pieces: list[str] = []
    current = ""
    for char in plain:
        if len(current) >= TYPE_PIECE_SIZE or (char == "s" and current[-1:] == "%"):
            pieces.append(current)
            current = ""
        current += char
    pieces.append(current)
    return pieces


def _plain_text(text: object) -> str:
    """Return `text` as a plain str with the same characters.

    A str subclass is read once here, by the base type; everything after this
    uses only the returned str, so a subclass cannot show one text to the
    check and another to the device.
    """
    if not isinstance(text, str):
        msg = f"text must be a str, not {type(text).__name__}"
        raise TypeError(msg)
    return str.__str__(text)


# Why typing stopped, by error class, in fixed words: what the device or adb
# said is never used, as it can repeat the text.
_TYPING_STOPPED_BECAUSE: Final = (
    (AdbServerUnavailableError, "the adb server cannot be reached"),
    (DeviceNotFoundError, "the device is gone"),
    (DeviceOfflineError, "the device is offline"),
    (DeviceUnauthorizedError, "the device has not authorized this computer"),
)


def _typing_stopped(
    kind: type[UiprobeError], piece: int, pieces: int, *, unsent: bool = False
) -> UiprobeError:
    """A new error of class `kind` for typing that stopped at `piece` (1-based).

    It holds only numbers and fixed words, never the text or anything the
    device or adb answered. `unsent` says the piece was refused before it was
    sent, because the pinned device has to be proven again
    (``details["reason"]`` is then ``pin_not_proven``).
    """
    reason = (
        "the device has to be proven again"
        if unsent
        else next(
            (words for cls, words in _TYPING_STOPPED_BECAUSE if issubclass(kind, cls)),
            "the adb request failed",
        )
    )
    done = piece - 1
    fate = (
        f"piece {piece} was not sent"
        if unsent
        else f"piece {piece} may have been typed in part or in full"
    )
    msg = (
        f"typing stopped at piece {piece} of {pieces} ({reason}); {done} earlier"
        f" piece{' was' if done == 1 else 's were'} reported typed, and {fate}"
    )
    details: dict[str, object] = {
        "command": "type",
        "request": "shell:input text <text omitted>",
        "piece": piece,
        "pieces": pieces,
        "pieces_done": done,
    }
    if unsent:
        details["reason"] = PIN_NOT_PROVEN
    return kind(msg, details=details)


def _piece_failure(output: str, piece: int, pieces: int) -> InputFailedError | None:
    """Judge the answer to one piece; None means exit status 0 and nothing else.

    The answer itself never goes into the error, not even the exit status:
    the device prints the status line, and a reply can repeat the text in it.
    """
    failure = _input_failure("type", {}, output)
    if failure is None:
        return None
    reason = failure.details["reason"]
    done = piece - 1
    details: dict[str, object] = {
        "command": "type",
        "reason": reason,
        "piece": piece,
        "pieces": pieces,
        "pieces_done": done,
    }
    what = {
        "no_exit_status": "the device did not report an exit status",
        "exit_status": "input reported an exit status other than 0",
        "unexpected_output": "input printed something besides its exit status",
    }[str(reason)]
    msg = (
        f"typing stopped at piece {piece} of {pieces}: {what}; {done} earlier"
        f" piece{' was' if done == 1 else 's were'} reported typed, and piece"
        f" {piece} may have been typed in part or in full"
    )
    return InputFailedError(msg, details=details)
