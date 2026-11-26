"""Save an HTML page into a new file.

`save_html` is the only place where the page of `uiprobe tree --html` reaches
the disk. It has one create-only open and one write call, as `pngfile.py` has
for a screenshot:

* The path must be a str or an os.PathLike of str. Anything at the path, also
  a link whose target is missing, is refused (`FileExistsError`) and left
  untouched; so is a path whose folder does not exist (nothing makes folders)
  and a path with a symbolic link or a Windows junction among the folders
  above the file (each existing component of the parent path is looked at, as
  written, so the file cannot land through a link). The file is then opened
  create-only (mode ``x``), which fails on POSIX for anything that appeared
  in between. What stays: another process with the same rights can swap a
  folder above the file for a link between the check and the open, and on
  Windows a link made at the file's own name in between is followed.
* Nothing is overwritten, renamed, truncated or deleted, also not after an
  error: a file that could not be completed stays as it is and the error names
  it.
* The data is flushed to the operating system and then fsynced before the
  function returns.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

from uiprobe.errors import UiprobeError
from uiprobe.pngfile import check_new_path

__all__ = [
    "HtmlFileError",
    "HtmlInterruptedError",
    "check_html_path",
    "save_html",
]


class HtmlFileError(UiprobeError):
    """The file could not be completed.

    ``details["path"]`` names the file, which stays on the disk as far as it
    got.
    """

    code = "file_error"
    exit_code = 1


class HtmlInterruptedError(KeyboardInterrupt):
    """Ctrl+C arrived after the file was created; `path` names it.

    The file stays on the disk and may be incomplete.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _plain(path: str | os.PathLike[str]) -> str:
    path = os.fspath(path)  # refuses an int, which open() takes as a descriptor
    if isinstance(path, str):
        # A plain str: a subclass with __index__ would make open() take it as
        # a descriptor.
        path = str.__str__(path)
    return path


def check_html_path(path: str | os.PathLike[str]) -> None:
    """Refuse a path that `save_html` would refuse, before anything is read.

    Raises:
        FileExistsError: Something is at `path`, also a broken link.
        FileNotFoundError: The folder of `path` does not exist.
        TypeError: `path` is not a str or an os.PathLike of str.
        ValueError: `path` is empty, holds a null character, on Windows names
            a stream of a file, or has a link or a junction above the file.
        OSError: Whether something is at `path` cannot be told.
    """
    path = _plain(path)
    check_new_path(path)
    folder = Path(path).parent
    if not folder.is_dir():
        raise FileNotFoundError(errno.ENOENT, "the folder does not exist", str(folder))
    _refuse_linked_parents(folder)


def _refuse_linked_parents(folder: Path) -> None:
    """Refuse a folder path with a link or a junction among its components.

    The path is read as written (made absolute, `..` kept): every prefix that
    exists is looked at with `lstat`, so a link is seen before it is followed.
    """
    absolute = folder if folder.is_absolute() else Path.cwd() / folder
    prefix = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        prefix /= part
        if part not in {".", ".."} and (prefix.is_symlink() or prefix.is_junction()):
            msg = f"{prefix} is a link or a junction; give a path through real folders"
            raise ValueError(msg)


def save_html(path: str | os.PathLike[str], data: str) -> None:
    """Write `data` into the new file `path` (UTF-8, newline ``\\n``).

    Raises:
        The errors of `check_html_path`, before anything is created.
        FileExistsError: Something appeared at `path` after the check.
        OSError: The file cannot be created (access is denied, for example).
        HtmlFileError: The file was created but could not be completed.
        HtmlInterruptedError: Ctrl+C arrived after the file was created.
    """
    path = _plain(path)
    check_html_path(path)
    interrupted = False
    try:
        handle = open(path, "x", encoding="utf-8", newline="\n")  # noqa: PTH123, SIM115
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
                raise HtmlInterruptedError(path) from error
            msg = f"cannot complete {path}: {error.strerror or error}"
            details = {"path": path, "created": True}
            raise HtmlFileError(msg, details=details) from error
    except HtmlInterruptedError:
        raise
    except KeyboardInterrupt as error:
        # Ctrl+C at any point after the open began: name the file when it is there.
        if os.path.lexists(path):
            raise HtmlInterruptedError(path) from error
        raise
