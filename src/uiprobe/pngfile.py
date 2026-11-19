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
# Gray and gray with alpha must not have a palette. A palette holds 1 to 256
# colors, and an indexed image's at most 2**depth (two for a 1-bit image).
_NO_PALETTE: Final = frozenset({0, 4})
_MAX_COLORS: Final = 256
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


def _header(data: bytes) -> tuple[int, int, int, int]:
    """Check the IHDR chunk; return ``(width, height, color type, bit depth)``."""
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
    return width, height, color, depth


def _check_chunk(
    kind: bytes, length: int, color: int, colors: int, seen: set[str]
) -> None:
    """Check the order rules for one chunk between IHDR and IEND.

    `colors` is the most palette entries the image may have.
    """
    if kind == b"IHDR":
        msg = "the PNG image has a second header chunk"
        raise ValueError(msg)
    if kind == b"IDAT":
        if "after data" in seen:
            msg = "the PNG image data is split by other chunks"
            raise ValueError(msg)
        if color == _PALETTE and "PLTE" not in seen:
            msg = "the PNG image has no palette before its image data"
            raise ValueError(msg)
        seen.add("IDAT")
        return
    if "IDAT" in seen:
        seen.add("after data")
    if kind == b"PLTE":
        if (
            seen & {"PLTE", "IDAT"}
            or color in _NO_PALETTE
            or length % 3
            or not 3 <= length <= 3 * colors
        ):
            msg = "the PNG image has a misplaced or malformed palette"
            raise ValueError(msg)
        seen.add("PLTE")


def _walk_chunks(data: bytes, color: int, depth: int) -> None:
    """Check the chunks after IHDR up to the closing IEND at the end of `data`."""
    colors = min(1 << depth, _MAX_COLORS) if color == _PALETTE else _MAX_COLORS
    cut = "the PNG image is cut off"
    position = len(_SIGNATURE) + 12 + _IHDR_LENGTH
    seen: set[str] = set()
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
        _check_chunk(kind, length, color, colors, seen)
        position = end
    if length != 0 or end != len(data) or not data.endswith(_IEND):
        msg = "the PNG image does not end with its closing chunk"
        raise ValueError(msg)
    if "IDAT" not in seen:
        msg = "the PNG image has no image data"
        raise ValueError(msg)


def check_png(data: bytes) -> tuple[int, int]:
    """Return the ``(width, height)`` of the PNG `data`.

    Checked: the signature; a first chunk ``IHDR`` with 13 bytes of data, a
    width and height from 1 to 2**31 - 1, a bit depth that its color type
    allows, compression and filter method 0 and interlace method 0 or 1;
    every chunk's length (at most 2**31 - 1) and a four-letter type, each
    chunk fitting inside `data`; no second ``IHDR``; at most one ``PLTE``
    (1 to 256 colors, for color type 3 at most 2**depth), before the image
    data, required for color type 3 and refused for gray types; ``IDAT``
    chunks in one unbroken run, at least one; and an empty closing ``IEND``
    chunk exactly at the end. Chunk
    checksums (except the fixed one of ``IEND``), other chunks' order and
    contents, and the image data are not checked.

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
    width, height, color, depth = _header(data)
    _walk_chunks(data, color, depth)
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
        TypeError: `path` is not exactly a str (a subclass could make
            `open` take it as a descriptor).
        ValueError: `path` is empty, holds a null character, or on Windows
            names a stream of a file.
        FileExistsError: Something is at `path`, also a broken link.
        OSError: Whether something is at `path` cannot be told (access to a
            folder above it is denied, for example); `path` is not used.
    """
    if type(path) is not str:
        msg = f"path must be a str or an os.PathLike of str, not {type(path).__name__}"
        raise TypeError(msg)
    if not path:
        msg = "the path must not be empty"
        raise ValueError(msg)
    if "\0" in path:
        msg = "the path must not hold a null character"
        raise ValueError(msg)
    if _names_a_stream(path):
        msg = (
            "the path names a stream of a file (a colon after the drive);"
            " give a plain file name"
        )
        raise ValueError(msg)
    # os.path.lexists() answers False on any error, so an entry whose
    # metadata cannot be read would pass. Only a missing entry (or a parent
    # that is not a folder, so nothing can be there) lets the caller go on.
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return
    raise FileExistsError(errno.EEXIST, "file exists", path)


def save_png(path: str | os.PathLike[str], data: bytes) -> None:
    """Write `data` into the new file `path`.

    Raises:
        FileExistsError: Something is at `path` already, also a broken link;
            nothing was changed.
        TypeError: `path` is not a str or an os.PathLike of str (an int, a
            bool or bytes is refused), or `data` is not bytes. Text of a str
            subclass, such as a `StrEnum` member, is used as a plain str, so
            it is never taken as an open descriptor.
        ValueError: `data` is not a PNG, or `path` cannot be used (it is
            empty, holds a null character, or on Windows names a stream of a
            file); nothing was created.
        PngFileError: The file could not be created (its parent is missing, or
            access is denied), whether something is at `path` could not be
            told, or the file could not be completed. A file that was
            created stays on the disk.
        PngInterruptedError: Ctrl+C arrived after the file was created (a
            KeyboardInterrupt); the file stays, possibly incomplete.
    """
    path = os.fspath(path)  # refuses an int, which open() takes as a descriptor
    if isinstance(path, str):
        # A plain str: a subclass with __index__ would make open() take it as
        # a descriptor, and its other methods could differ as well.
        path = str.__str__(path)
    try:
        check_new_path(path)
    except FileExistsError:
        raise
    except OSError as error:
        msg = f"cannot check {path}: {error.strerror or error}"
        details = {"path": path, "created": False}
        raise PngFileError(msg, details=details) from error
    check_png(data)
    try:
        handle = open(path, "xb")  # noqa: PTH123, SIM115
    except FileExistsError:
        raise  # something appeared after the check; nothing was created
    except OSError as error:
        msg = f"cannot create {path}: {error.strerror or error}"
        details = {"path": path, "created": False}
        raise PngFileError(msg, details=details) from error
    interrupted = False
    try:
        with handle:
            try:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            except KeyboardInterrupt:
                interrupted = True
                raise
    except OSError as error:
        # A failing close must not hide a Ctrl+C that arrived while writing.
        if interrupted:
            raise PngInterruptedError(path) from error
        msg = f"cannot complete {path}: {error.strerror or error}"
        details = {"path": path, "created": True}
        raise PngFileError(msg, details=details) from error
    except KeyboardInterrupt as error:
        raise PngInterruptedError(path) from error
