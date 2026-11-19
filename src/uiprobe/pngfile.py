"""Check a PNG image in memory and save it into a new file.

`check_png` walks the chunk structure without decoding the image. `save_png`
is the only place where a screenshot reaches the disk, and it has one
create-only open and one write call:

* The path must be a str or an os.PathLike (an int is not taken as an open
  descriptor). On Windows a path that names a stream of a file
  (``shot.png:extra``) is refused, because creating a stream changes the file
  it belongs to.
* Anything at the path, also a link whose target is missing, is refused
  (`FileExistsError`) and left untouched. The file is then opened create-only
  (mode ``xb``), which fails on POSIX for anything that appeared in between.
  On Windows a link made between the check and the open is followed, and a
  link or junction in a parent folder redirects the file on every system.
* Nothing is overwritten, renamed, truncated or deleted, also not after an
  error: a file that could not be completed stays as it is and the error names
  it.
* The data is flushed to the operating system and then fsynced before the
  function returns.
"""

from __future__ import annotations

import errno
import os
import re
from typing import Final

from uiprobe.errors import UiprobeError

__all__ = ["PngFileError", "PngInterruptedError", "check_png", "save_png"]

_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_IHDR_LENGTH: Final = 13
# Length 0, type IEND, and the checksum of the type: the last 12 bytes of every
# complete PNG file.
_IEND: Final = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_MAX_SIDE: Final = 2**31 - 1
_MAX_CHUNK: Final = 2**31 - 1
# The bit depths each color type allows (PNG specification, IHDR).
_DEPTHS: Final = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}
_PALETTE: Final = 3
_WINDOWS: Final = os.name == "nt"
# A Windows drive at the start of a path: an optional ``\\?\`` or ``\\.\``
# prefix, then a letter and a colon. Any other colon names a stream.
_DRIVE: Final = re.compile(r"(?:[\\/]{2}[?.][\\/])?[A-Za-z]:")


class PngFileError(UiprobeError):
    """The file could not be created or completed.

    ``details["path"]`` names the file. ``details["created"]`` is False when
    the file was never created, and True when it stays on the disk as far as
    it got.
    """

    code = "file_error"
    exit_code = 1


class PngInterruptedError(KeyboardInterrupt):
    """Ctrl+C arrived after the file was created; `path` names it.

    The file stays on the disk and may be incomplete.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _header(data: bytes) -> tuple[int, int, int]:
    """Check the IHDR chunk; return ``(width, height, color type)``."""
    start = len(_SIGNATURE)
    header = data[start : start + 8 + _IHDR_LENGTH]
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
    depth, color, compression, filtering, interlace = header[16:21]
    if depth not in _DEPTHS.get(color, ()):
        msg = f"the PNG image has an invalid bit depth {depth} for color type {color}"
        raise ValueError(msg)
    if compression != 0 or filtering != 0 or interlace not in {0, 1}:
        msg = "the PNG image has an invalid compression, filter or interlace method"
        raise ValueError(msg)
    return width, height, color


def _walk_chunks(data: bytes, color: int) -> None:
    """Check the chunks after IHDR up to the closing IEND at the end of `data`."""
    cut = "the PNG image is cut off"
    position = len(_SIGNATURE) + 12 + _IHDR_LENGTH
    palette = image = False
    while True:
        if position + 12 > len(data):
            raise ValueError(cut)
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        if length > _MAX_CHUNK or not (kind.isascii() and kind.isalpha()):
            msg = "the PNG image has a malformed chunk"
            raise ValueError(msg)
        end = position + 12 + length
        if end > len(data):
            raise ValueError(cut)
        if kind == b"IEND":
            break
        if kind == b"PLTE":
            palette = True
        elif kind == b"IDAT":
            if color == _PALETTE and not palette:
                msg = "the PNG image has no palette before its image data"
                raise ValueError(msg)
            image = True
        position = end
    if end != len(data) or not data.endswith(_IEND):
        msg = "the PNG image does not end with its closing chunk"
        raise ValueError(msg)
    if not image:
        msg = "the PNG image has no image data"
        raise ValueError(msg)


def check_png(data: bytes) -> tuple[int, int]:
    """Return the ``(width, height)`` of the PNG `data`.

    Checked: the signature; a first chunk ``IHDR`` with 13 bytes of data, a
    width and height from 1 to 2**31 - 1, a bit depth that its color type
    allows, compression and filter method 0 and interlace method 0 or 1;
    every chunk's length (at most 2**31 - 1) and a four-letter type, each
    chunk fitting inside `data`; a ``PLTE`` chunk before the image data when
    the color type is 3; at least one ``IDAT`` chunk; and the closing ``IEND``
    chunk exactly at the end. Chunk checksums (except the fixed one of
    ``IEND``) and the image data are not checked.

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
    width, height, color = _header(data)
    _walk_chunks(data, color)
    return width, height


def _names_a_stream(path: str) -> bool:
    """Whether `path` uses Windows stream syntax (a colon after the drive)."""
    if not _WINDOWS:
        return False
    drive = _DRIVE.match(path)
    return ":" in path[drive.end() if drive else 0 :]


def check_new_path(path: str) -> None:
    """Refuse a path that `save_png` would refuse, before anything is written.

    Raises:
        TypeError: `path` is not a str.
        ValueError: `path` is empty, or on Windows names a stream of a file.
        FileExistsError: Something is at `path`, also a broken link.
    """
    if not isinstance(path, str):
        msg = f"path must be a str or an os.PathLike of str, not {type(path).__name__}"
        raise TypeError(msg)
    if not path:
        msg = "the path must not be empty"
        raise ValueError(msg)
    if _names_a_stream(path):
        msg = (
            "the path names a stream of a file (a colon after the drive);"
            " give a plain file name"
        )
        raise ValueError(msg)
    if os.path.lexists(path):
        raise FileExistsError(errno.EEXIST, "file exists", path)


def save_png(path: str | os.PathLike[str], data: bytes) -> None:
    """Write `data` into the new file `path`.

    Raises:
        FileExistsError: Something is at `path` already, also a broken link;
            nothing was changed.
        TypeError: `path` is not a str or an os.PathLike of str (an int is not
            taken as an open descriptor), or `data` is not bytes.
        ValueError: `data` is not a PNG, or `path` cannot be used (it is
            empty, holds a null character, or on Windows names a stream of a
            file); nothing was created.
        PngFileError: The file could not be created (its parent is missing, or
            access is denied) or could not be completed. A file that was
            created stays on the disk.
        PngInterruptedError: Ctrl+C arrived after the file was created (a
            KeyboardInterrupt); the file stays, possibly incomplete.
    """
    path = os.fspath(path)  # refuses an int, which open() takes as a descriptor
    check_new_path(path)
    check_png(data)
    try:
        handle = open(path, "xb")  # noqa: PTH123, SIM115
    except FileExistsError:
        raise  # something appeared after the check; nothing was created
    except OSError as error:
        msg = f"cannot create {path}: {error.strerror or error}"
        details = {"path": path, "created": False}
        raise PngFileError(msg, details=details) from error
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        # A failing close must not hide a Ctrl+C that arrived while writing.
        if isinstance(error.__context__, KeyboardInterrupt):
            raise PngInterruptedError(path) from error
        msg = f"cannot complete {path}: {error.strerror or error}"
        details = {"path": path, "created": True}
        raise PngFileError(msg, details=details) from error
    except KeyboardInterrupt as error:
        raise PngInterruptedError(path) from error
