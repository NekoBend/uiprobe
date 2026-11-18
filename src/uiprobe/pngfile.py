"""Check a PNG image in memory and save it into a new file.

`check_png` reads only the signature, the first chunk and the last chunk.
`save_png` is the only place where a screenshot reaches the disk, and it has
one create-only open and one write call:

* The file is opened create-only (mode ``xb``). An existing file, link or
  folder at the path is an error (`FileExistsError`) and is left untouched.
* Nothing is overwritten, renamed, truncated or deleted, also not after an
  error: a file that could not be completed stays as it is and the error names
  it.
* The data is flushed to the operating system and then fsynced before the
  function returns.
"""

from __future__ import annotations

import os
from typing import Final

from uiprobe.errors import UiprobeError

__all__ = ["PngFileError", "check_png", "save_png"]

_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_IHDR_LENGTH: Final = 13
# Length 0, type IEND, and the checksum of the type: the last 12 bytes of every
# complete PNG file.
_IEND: Final = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_MAX_SIDE: Final = 2**31 - 1


class PngFileError(UiprobeError):
    """The file could not be created or completed.

    ``details["path"]`` names the file. ``details["created"]`` is False when
    the file was never created, and True when it stays on the disk as far as
    it got.
    """

    code = "file_error"
    exit_code = 1


def check_png(data: bytes) -> tuple[int, int]:
    """Return the ``(width, height)`` of the PNG `data`.

    The signature, a well-formed first chunk (``IHDR``, 13 bytes of data, width
    and height from 1 to 2**31 - 1) and the closing ``IEND`` chunk must be
    present. Chunk checksums and the image data are not checked.

    Raises:
        TypeError: `data` is not bytes.
        ValueError: `data` is not such a PNG; the message says what is wrong.
    """
    if not isinstance(data, bytes):
        msg = f"data must be bytes, not {type(data).__name__}"
        raise TypeError(msg)
    if not data:
        msg = "the screenshot is empty"
        raise ValueError(msg)
    if not data.startswith(_SIGNATURE):
        msg = "the screenshot is not a PNG image"
        raise ValueError(msg)
    header = data[len(_SIGNATURE) : len(_SIGNATURE) + 8 + _IHDR_LENGTH]
    if (
        len(header) < 8 + _IHDR_LENGTH
        or int.from_bytes(header[:4], "big") != _IHDR_LENGTH
        or header[4:8] != b"IHDR"
    ):
        msg = "the PNG image has no well-formed header chunk"
        raise ValueError(msg)
    width = int.from_bytes(header[8:12], "big")
    height = int.from_bytes(header[12:16], "big")
    if not (0 < width <= _MAX_SIDE and 0 < height <= _MAX_SIDE):
        msg = f"the PNG image has an impossible size, {width}x{height}"
        raise ValueError(msg)
    if not data.endswith(_IEND) or len(data) < len(_SIGNATURE) + 8 + 17 + 12:
        msg = "the PNG image is cut off"
        raise ValueError(msg)
    return width, height


def save_png(path: str | os.PathLike[str], data: bytes) -> None:
    """Write `data` into the new file `path`.

    Raises:
        FileExistsError: Something is at `path` already; nothing was changed.
        TypeError: `data` is not bytes.
        ValueError: `data` is not a PNG, or `path` cannot be used (for example
            it holds a null character); nothing was created.
        PngFileError: The file could not be created (its parent is missing, or
            access is denied) or could not be completed. A file that was
            created stays on the disk.
    """
    check_png(data)
    created = False
    try:
        with open(path, "xb") as handle:  # noqa: PTH123
            created = True
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise  # only the open can say this; nothing was created
    except OSError as error:
        verb = "complete" if created else "create"
        msg = f"cannot {verb} {path}: {error.strerror or error}"
        details = {"path": str(path), "created": created}
        raise PngFileError(msg, details=details) from error
