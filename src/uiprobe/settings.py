"""The settings file: where uiprobe's no-press defaults live.

The defaults are written out in a TOML file, ``uiprobe.toml``, that
``uiprobe init`` creates and that users edit freely. This module holds the
file format, the generator (`render_default_settings`,
`write_default_settings`) and a strict reader (`load_settings`). Nothing else
in uiprobe reads the file yet.

What the file holds: the deny words, the confirm words, the rules (unlabeled,
number-only and toggle elements, confirm buttons in dialogs, the screen-edge
sizes, the smallest visible size), the crawl limits, ``press`` and the
device-wide keys that ``uiprobe key`` refuses. What it never holds: the
``--allow-real-device`` flag, permission screens, password fields, typing,
the target-app check, the re-read before a press and Ctrl+C.

Every default list item carries the defaults version that introduced it. The
file records the defaults version it was written with. The effective list is:

* the items listed in the file, then
* every default introduced after the file's defaults version, so that words
  added by a later uiprobe apply without editing the file, then
* minus the items under ``remove``.

A deny word is compared with the ``remove`` list after the normalization the
deny matching uses (`uiprobe.guards.normalize`: NFKC, case-folded, whitespace
runs made one space), so ``Login`` under ``remove`` also removes ``login``. A
confirm word is compared in the form the confirm matching uses, which also
strips surrounding punctuation and invisible characters, so ``OK!`` and ``OK``
are the same word. Duplicates in one list are judged the same way. Key names
are compared as exact strings; ``[keys] remove`` takes names out of both
``refuse`` and ``refuse_prefixes``, and a removed name that a remaining prefix
still covers stays refused and gives a warning. The fixed keys
(`FIXED_DEVICE_KEYS`) are always refused: they are added back whatever the
file says, and listing one under ``[keys] remove`` only adds a warning.

Reading is race-resistant (`_read_bytes`): the path is opened once, read-only
(O_NONBLOCK on POSIX, so a FIFO or device does not wait for a writer), and
every check and every byte comes from that descriptor: regular file, size at most
`MAX_SETTINGS_BYTES`, at most `MAX_SETTINGS_BYTES` + 1 bytes read, size and
times unchanged after the read, and the path still naming the same file
(st_dev, st_ino). A symbolic link is followed at the open. What remains: a path
switched before the open names another file, which is then fully checked; on
Windows st_ctime is the creation time, so a same-size rewrite that restores the
modification time is not detected there.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from uiprobe.device import DEVICE_WIDE_KEY_PREFIXES, DEVICE_WIDE_KEYS, FIXED_DEVICE_KEYS
from uiprobe.guards import (
    API36_BOTTOM_PX,
    API36_SIDE_PX,
    API36_TOP_PX,
    CONFIRM_WORDS,
    DEFAULT_DENY_WORDS,
    _check_word,
    _confirm_form,
    normalize,
)
from uiprobe.model import MIN_VISIBLE_PX

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "FIXED_DEVICE_KEYS",
    "SETTINGS_FILE",
    "IncompleteFileError",
    "InterruptedFileError",
    "Settings",
    "SettingsError",
    "load_settings",
    "render_default_settings",
    "write_default_settings",
]

SETTINGS_FILE: Final = "uiprobe.toml"
FORMAT_VERSION: Final = 1
DEFAULTS_VERSION: Final = 1
MAX_SETTINGS_BYTES: Final = 1024 * 1024

# Every default list item with the defaults version that introduced it. Each
# built-in item is version 1 unless the `_LATER_*` map of its table names a
# higher version. The built-in tuples stay the runtime defaults (the crawl
# policy and `key` read them while no settings file is used), so a later
# default goes into the built-in tuple as well.
# To add a default in a later version: raise DEFAULTS_VERSION, add the item to
# its built-in tuple and to the `_LATER_*` map of its table as
# {item: new version}. Without the map entry the item would be tagged 1 and
# fail the snapshot test in tests/test_settings.py, and files written at an
# older version would be given it only through the map's version.
_LATER_DENY: Final[dict[str, int]] = {}
_LATER_CONFIRM: Final[dict[str, int]] = {}
_LATER_KEYS: Final[dict[str, int]] = {}
_LATER_PREFIXES: Final[dict[str, int]] = {}
_DENY_WORDS: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_DENY.get(word, 1), word) for word in DEFAULT_DENY_WORDS
)
_CONFIRM_WORDS: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_CONFIRM.get(word, 1), word) for word in CONFIRM_WORDS
)
_REFUSE_KEYS: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_KEYS.get(key, 1), key)
    for key in sorted(DEVICE_WIDE_KEYS - FIXED_DEVICE_KEYS)
)
_REFUSE_PREFIXES: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_PREFIXES.get(prefix, 1), prefix) for prefix in DEVICE_WIDE_KEY_PREFIXES
)

_DEFAULT_DEPTH: Final = 5
_DEFAULT_ACTIONS: Final = 300
_DEFAULT_MINUTES: Final = 30

_KEY_NAME: Final = re.compile(r"KEYCODE_[A-Z0-9_]{1,40}")
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_UTF16_BOMS: Final = (b"\xff\xfe", b"\xfe\xff")

_TOP_KEYS: Final = frozenset({"format_version", "defaults_version", "press"})
_BOOL_RULES: Final = (
    "tap_unlabeled",
    "skip_number_only",
    "skip_toggles",
    "refuse_dialog_confirm",
)
# table -> key -> (lowest, highest) allowed value
_INT_RANGES: Final[dict[str, dict[str, tuple[int, int]]]] = {
    "rules": {
        "min_visible_px": (1, 64),
        "edge_top_px": (0, 500),
        "edge_bottom_px": (0, 500),
        "edge_side_px": (0, 500),
    },
    "limits": {
        "depth": (1, 50),
        "actions": (1, 10000),
        "minutes": (1, 1440),
    },
}
_TABLE_KEYS: Final[dict[str, frozenset[str]]] = {
    "deny": frozenset({"words", "remove"}),
    "confirm": frozenset({"words", "remove"}),
    "rules": frozenset(_BOOL_RULES) | frozenset(_INT_RANGES["rules"]),
    "limits": frozenset(_INT_RANGES["limits"]),
    "keys": frozenset({"refuse", "refuse_prefixes", "remove"}),
}


class SettingsError(ValueError):
    """The settings file cannot be read or holds an invalid value."""


class IncompleteFileError(OSError):
    """The settings file was created but could not be written completely."""


class InterruptedFileError(KeyboardInterrupt):
    """Ctrl+C arrived after the settings file was created; `path` names it."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


@dataclass(frozen=True)
class Settings:
    """The effective settings: the file's values with the later defaults added.

    ``warnings`` holds notes about entries that are accepted but have no
    effect, such as a fixed key under ``[keys] remove``. ``refused_keys`` always
    holds every key of `FIXED_DEVICE_KEYS`.
    """

    source: Path
    format_version: int
    defaults_version: int
    press: bool
    deny_words: tuple[str, ...]
    confirm_words: tuple[str, ...]
    tap_unlabeled: bool
    skip_number_only: bool
    skip_toggles: bool
    refuse_dialog_confirm: bool
    min_visible_px: int
    edge_top_px: int
    edge_bottom_px: int
    edge_side_px: int
    depth: int
    actions: int
    minutes: int
    refused_keys: tuple[str, ...]
    refused_key_prefixes: tuple[str, ...]
    warnings: tuple[str, ...] = ()


# -- the generator -------------------------------------------------------------


def _toml_string(value: str) -> str:
    """Return `value` as a TOML basic string; non-ASCII text stays as it is."""
    out = ['"']
    for char in value:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif unicodedata.category(char) in {"Cc", "Cs"}:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _toml_list(name: str, values: Sequence[str]) -> list[str]:
    if not values:
        return [f"{name} = []"]
    return [f"{name} = [", *(f"    {_toml_string(value)}," for value in values), "]"]


def _current(items: Sequence[tuple[int, str]]) -> list[str]:
    """Return the items introduced up to `DEFAULTS_VERSION`."""
    return [value for version, value in items if version <= DEFAULTS_VERSION]


def _flag(value: bool) -> str:  # noqa: FBT001 - a plain formatter for TOML booleans
    return "true" if value else "false"


def render_default_settings() -> str:
    """Return the text of a settings file with every default written out."""
    lines = [
        '# uiprobe settings, generated by "uiprobe init". Edit it freely.',
        "#",
        "# Delete a word or a key to stop applying it; that is your responsibility.",
        "# Words and keys that a later uiprobe version adds apply automatically",
        "# even though this file does not list them, unless you list them under",
        '# "remove". The fixed keys (power, sleep and call keys) are always',
        "# refused; they are not listed here and cannot be removed.",
        "",
        f"format_version = {FORMAT_VERSION}",
        f"defaults_version = {DEFAULTS_VERSION}",
        "# Press elements for real (the crawl itself is not wired to this file yet).",
        "press = false",
        "",
        "[deny]",
        "# Never press an element that contains one of these words.",
        *_toml_list("words", _current(_DENY_WORDS)),
        "remove = []",
        "",
        "[confirm]",
        "# Labels of buttons that confirm what the screen asks.",
        *_toml_list("words", _current(_CONFIRM_WORDS)),
        "remove = []",
        "",
        "[rules]",
        "tap_unlabeled = false",
        "skip_number_only = true",
        "skip_toggles = true",
        "refuse_dialog_confirm = true",
        f"min_visible_px = {MIN_VISIBLE_PX}",
        f"edge_top_px = {API36_TOP_PX}",
        f"edge_bottom_px = {API36_BOTTOM_PX}",
        f"edge_side_px = {API36_SIDE_PX}",
        "",
        "[limits]",
        f"depth = {_DEFAULT_DEPTH}",
        f"actions = {_DEFAULT_ACTIONS}",
        f"minutes = {_DEFAULT_MINUTES}",
        "",
        "[keys]",
        "# Keys that act on the whole device; `uiprobe key` refuses them.",
        *_toml_list("refuse", _current(_REFUSE_KEYS)),
        "# Every key whose name starts with one of these is refused too.",
        *_toml_list("refuse_prefixes", _current(_REFUSE_PREFIXES)),
        "remove = []",
        "",
    ]
    return "\n".join(lines)


def write_default_settings(path: str | os.PathLike[str]) -> None:
    """Create the file `path` with the default settings; never overwrite one.

    Raises:
        FileExistsError: `path` already exists (a file, a directory or a link).
        IncompleteFileError: The file was created but writing, flushing or
            closing it failed; it stays on disk, incomplete, because uiprobe
            never deletes files.
        InterruptedFileError: Ctrl+C arrived after the file was created (a
            KeyboardInterrupt); the file stays, possibly incomplete.
        OSError: The file cannot be created.
    """
    text = render_default_settings()
    handle = open(path, "x", encoding="utf-8", newline="\n")  # noqa: PTH123, SIM115
    try:
        try:
            print(text, end="", file=handle)
            handle.flush()
        finally:
            handle.close()
    except OSError as error:
        # A failing close must not hide a Ctrl+C that arrived while writing.
        if isinstance(error.__context__, KeyboardInterrupt):
            raise InterruptedFileError(str(path)) from error
        raise IncompleteFileError(error) from error
    except KeyboardInterrupt as error:
        raise InterruptedFileError(str(path)) from error


# -- the reader ----------------------------------------------------------------


def _shown(text: str) -> str:
    """Escape characters that must not reach a terminal raw."""
    return "".join(
        char if char.isprintable() else f"\\x{ord(char):02x}" for char in text
    )


_SHOWN_LIMIT: Final = 60


def _value_text(value: object) -> str:
    """Describe a value from the file for a message; never raises, always short.

    TOML allows huge integers (hexadecimal, octal, binary), and Python refuses
    to turn one into decimal digits past its limit, so such an integer is
    described by its size instead.
    """
    try:
        text = repr(_shown(value)) if isinstance(value, str) else repr(value)
    except (ValueError, RecursionError):
        if isinstance(value, int):
            return f"an integer with {value.bit_length()} bits"
        return f"a value of type {type(value).__name__}"
    return _capped(_shown(text))


def _capped(text: str) -> str:
    if len(text) > _SHOWN_LIMIT:
        return text[:_SHOWN_LIMIT] + "..."
    return text


_PARSE_TAIL: Final = re.compile(r" \(at (?:line \d+, column \d+|end of document)\)\Z")


def _capped_parse_error(text: str) -> str:
    """Cap the parser's text but keep its "(at line L, column C)" tail."""
    tail = _PARSE_TAIL.search(text)
    if tail is None:
        return _capped(text)
    return _capped(text[: tail.start()]) + tail.group()


class _Reader:
    """Validates one parsed file; every problem names the key and the file."""

    def __init__(self, path: Path) -> None:
        self.shown = _shown(str(path))

    def fail(self, where: str, text: str) -> SettingsError:
        return SettingsError(f"{self.shown}: {_capped(_shown(where))}: {text}")

    def table(
        self, data: Mapping[str, object], name: str, allowed: frozenset[str]
    ) -> Mapping[str, object]:
        value = data.get(name)
        if not isinstance(value, dict):
            raise self.fail(f"[{name}]", "the table is missing")
        for key in value:
            if key not in allowed:
                raise self.fail(f"[{name}] {key}", "unknown key")
        return value

    def boolean(self, table: Mapping[str, object], name: str, key: str) -> bool:
        value = table.get(key)
        if key not in table:
            raise self.fail(self._where(name, key), "is missing")
        if not isinstance(value, bool):
            raise self.fail(self._where(name, key), "must be true or false")
        return value

    def integer(
        self, table: Mapping[str, object], name: str, key: str, low: int, high: int
    ) -> int:
        value = table.get(key)
        where = self._where(name, key)
        if key not in table:
            raise self.fail(where, "is missing")
        if type(value) is not int:
            raise self.fail(where, "must be an integer")
        if low == high and value != low:
            raise self.fail(where, f"must be {low}, not {_value_text(value)}")
        if not low <= value <= high:
            raise self.fail(
                where, f"must be from {low} to {high}, not {_value_text(value)}"
            )
        return value

    def strings(
        self,
        table: Mapping[str, object],
        name: str,
        key: str,
        check: Callable[[str], str],
        *,
        optional: bool = False,
    ) -> tuple[str, ...]:
        """Return a list of valid, distinct strings (distinct after `check`)."""
        where = self._where(name, key)
        if key not in table and optional:
            return ()
        value = table.get(key)
        if key not in table:
            raise self.fail(where, "is missing")
        if not isinstance(value, list):
            raise self.fail(where, "must be a list of strings")
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise self.fail(
                    where, f"must hold only strings, found {_value_text(item)}"
                )
            try:
                identity = check(item)
            except ValueError as error:
                raise self.fail(where, _capped(_shown(str(error)))) from None
            if identity in seen:
                raise self.fail(where, f"duplicate entry {_value_text(item)}")
            seen.add(identity)
        return tuple(value)

    @staticmethod
    def _where(name: str, key: str) -> str:
        return f"[{name}] {key}" if name else key


def _word_identity(word: str) -> str:
    """Check a deny word as `Policy` does; return what makes two words equal."""
    _check_word(word)
    if any(unicodedata.category(char) == "Cc" for char in word):
        msg = f"word holds a control character: {word!r}"
        raise ValueError(msg)
    return normalize(word)


def _confirm_identity(word: str) -> str:
    """Check a confirm word; return the form the confirm matching compares."""
    _word_identity(word)
    form = _confirm_form(word)
    if not form:
        msg = f"confirm word is empty after cleaning: {word!r}"
        raise ValueError(msg)
    return form


def _key_identity(name: str) -> str:
    if not _KEY_NAME.fullmatch(name):
        msg = f"{name!r} does not match KEYCODE_[A-Z0-9_]{{1,40}}"
        raise ValueError(msg)
    return name


def _merge(
    listed: Sequence[str],
    removed: Sequence[str],
    versioned: Sequence[tuple[int, str]],
    file_version: int,
    identity: Callable[[str], str],
) -> tuple[str, ...]:
    """Return listed items, plus defaults newer than the file, minus `removed`."""
    items: dict[str, str] = {}
    for item in listed:
        items.setdefault(identity(item), item)
    for version, item in versioned:
        if file_version < version <= DEFAULTS_VERSION:
            items.setdefault(identity(item), item)
    gone = {identity(item) for item in removed}
    return tuple(item for key, item in items.items() if key not in gone)


# The flags of the one read-only open in `_read_bytes`. Only read-only names
# may appear here (the offline guard checks the definition by AST and the value
# at runtime). POSIX: O_NONBLOCK so that opening a FIFO or a device does not
# block (it has no effect on reading a regular file). Windows has no
# O_NONBLOCK. O_NOCTTY so that opening a terminal device cannot make it the
# controlling terminal. No O_NOFOLLOW: a settings file that is a symbolic link is fine,
# because the opened file is what gets validated.
_READ_FLAGS: int
if sys.platform == "win32":
    _READ_FLAGS = os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT
else:
    _READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOCTTY

_READ_CHUNK = 65536


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _close_quietly(fd: int) -> None:
    """Close `fd`; an error here must not hide a refusal in flight."""
    try:
        os.close(fd)
    except OSError:
        return  # nothing is lost by failing to close a read-only descriptor


def _read_bytes(path: Path, shown: str) -> bytes:
    """Read the settings file `path` (at most one byte more than the limit).

    The path is opened once; every check and every byte comes from that
    descriptor, never from the path again (the path is only stat-ed at the end
    to see that it still names the opened file).
    """

    def fail(text: str) -> SettingsError:
        return SettingsError(f"{shown}: {text}")

    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        raise fail("no such file") from None
    except (OSError, ValueError) as error:
        # Windows cannot open a directory (PermissionError): name it as POSIX
        # does. Only the message is chosen here; no byte is read.
        try:
            kind = path.stat().st_mode
        except (OSError, ValueError):
            kind = stat.S_IFREG
        if not stat.S_ISREG(kind):
            raise fail("not a regular file") from None
        raise fail(f"cannot be read: {_shown(str(error))}") from None
    try:
        return _read_descriptor(fd, path, fail)
    finally:
        _close_quietly(fd)


def _read_descriptor(
    fd: int, path: Path, fail: Callable[[str], SettingsError]
) -> bytes:
    changed = "the file changed while it was read; try again"
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise fail("not a regular file")
        if before.st_size > MAX_SETTINGS_BYTES:
            raise fail(f"larger than {MAX_SETTINGS_BYTES} bytes")
        chunks: list[bytes] = []
        total = 0
        limit = MAX_SETTINGS_BYTES + 1
        while total < limit:
            chunk = os.read(fd, min(_READ_CHUNK, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    except SettingsError:
        raise  # a refusal above is already worded; do not wrap it again
    except (OSError, ValueError) as error:
        raise fail(f"cannot be read: {_shown(str(error))}") from None
    if total > MAX_SETTINGS_BYTES:
        raise fail(f"larger than {MAX_SETTINGS_BYTES} bytes")
    if total != before.st_size or _file_identity(after) != _file_identity(before):
        raise fail(changed)
    try:
        named = path.stat()
    except FileNotFoundError:
        raise fail(changed) from None
    except (OSError, ValueError) as error:
        raise fail(f"cannot be read: {_shown(str(error))}") from None
    if (named.st_dev, named.st_ino) != (before.st_dev, before.st_ino):
        raise fail(changed)
    return b"".join(chunks)


def _decode(data: bytes, shown: str) -> str:
    if data.startswith(_UTF16_BOMS):
        msg = f"{shown}: the file is UTF-16; save it as UTF-8"
        raise SettingsError(msg)
    data = data.removeprefix(_UTF8_BOM)
    if b"\x00" in data:
        msg = f"{shown}: the file holds NUL; it looks like UTF-16, save it as UTF-8"
        raise SettingsError(msg)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        msg = f"{shown}: line {line} is not valid UTF-8; save the file as UTF-8"
        raise SettingsError(msg) from error


def load_settings(path: str | os.PathLike[str]) -> Settings:
    """Read and validate the settings file `path`; return the effective values.

    The file must be a regular file of at most 1 MiB, strict UTF-8 (a UTF-8 BOM
    is accepted, UTF-16 is refused) and valid TOML. ``format_version`` and
    ``defaults_version`` and every table and key that `render_default_settings`
    writes must be present; only the ``remove`` lists may be left out. Unknown
    tables or keys, wrong types, numbers out of range, words that deny matching
    rejects, key names that do not match ``KEYCODE_[A-Z0-9_]{1,40}`` and
    duplicate entries are refused.

    The path is opened once (read-only, ``O_NONBLOCK`` on Linux and macOS so a
    FIFO or device does not wait for a writer, plus ``O_NOCTTY``) and every
    check and byte comes from that descriptor: type and size are checked with
    ``fstat``, at most the limit plus one byte is read, and a second ``fstat``
    must match the first (including the change time, which on Windows is the
    creation time: a same-size rewrite with a restored modification time is
    caught on Linux and macOS when the file system records a newer change time,
    not on Windows). The path is stat-ed once more at the end and must still
    name the opened file. Limit: the path is resolved once, at the
    open, so a link or folder switched before it makes this read the file the
    path named at that moment (still fully checked).

    Raises:
        SettingsError: The file cannot be read or is invalid; the message
            names the file and the key.
    """
    location = Path(path)
    reader = _Reader(location)
    data = _read_bytes(location, reader.shown)
    text = _decode(data, reader.shown)
    try:
        parsed = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError) as error:
        detail = _capped_parse_error(_shown(str(error)))
        msg = f"{reader.shown}: the TOML could not be parsed: {detail}"
        raise SettingsError(msg) from None
    format_version = reader.integer(parsed, "", "format_version", 1, FORMAT_VERSION)
    for key in parsed:
        if key not in _TOP_KEYS and key not in _TABLE_KEYS:
            raise reader.fail(key, "unknown key or table")

    defaults_version = reader.integer(
        parsed, "", "defaults_version", 1, DEFAULTS_VERSION
    )
    press = reader.boolean(parsed, "", "press")

    deny = reader.table(parsed, "deny", _TABLE_KEYS["deny"])
    deny_listed = reader.strings(deny, "deny", "words", _word_identity)
    deny_removed = reader.strings(deny, "deny", "remove", _word_identity, optional=True)
    confirm = reader.table(parsed, "confirm", _TABLE_KEYS["confirm"])
    confirm_listed = reader.strings(confirm, "confirm", "words", _confirm_identity)
    confirm_removed = reader.strings(
        confirm, "confirm", "remove", _confirm_identity, optional=True
    )

    rules = reader.table(parsed, "rules", _TABLE_KEYS["rules"])
    flags = {key: reader.boolean(rules, "rules", key) for key in _BOOL_RULES}
    sizes = {
        key: reader.integer(rules, "rules", key, *bounds)
        for key, bounds in _INT_RANGES["rules"].items()
    }
    limits_table = reader.table(parsed, "limits", _TABLE_KEYS["limits"])
    limits = {
        key: reader.integer(limits_table, "limits", key, *bounds)
        for key, bounds in _INT_RANGES["limits"].items()
    }

    keys = reader.table(parsed, "keys", _TABLE_KEYS["keys"])
    key_listed = reader.strings(keys, "keys", "refuse", _key_identity)
    prefix_listed = reader.strings(keys, "keys", "refuse_prefixes", _key_identity)
    key_removed = reader.strings(keys, "keys", "remove", _key_identity, optional=True)
    refused = _merge(
        key_listed, key_removed, _REFUSE_KEYS, defaults_version, _key_identity
    )
    prefixes = _merge(
        prefix_listed, key_removed, _REFUSE_PREFIXES, defaults_version, _key_identity
    )
    warnings = tuple(
        f"[keys] remove lists {key}, which is always refused; the entry has no effect"
        for key in key_removed
        if key in FIXED_DEVICE_KEYS
    ) + tuple(
        f"[keys] remove lists {key}, which a refused prefix still covers;"
        " the entry has no effect"
        for key in key_removed
        if key not in FIXED_DEVICE_KEYS and key.startswith(tuple(prefixes))
    )

    return Settings(
        source=location,
        format_version=format_version,
        defaults_version=defaults_version,
        press=press,
        deny_words=_merge(
            deny_listed, deny_removed, _DENY_WORDS, defaults_version, normalize
        ),
        confirm_words=_merge(
            confirm_listed,
            confirm_removed,
            _CONFIRM_WORDS,
            defaults_version,
            _confirm_form,
        ),
        tap_unlabeled=flags["tap_unlabeled"],
        skip_number_only=flags["skip_number_only"],
        skip_toggles=flags["skip_toggles"],
        refuse_dialog_confirm=flags["refuse_dialog_confirm"],
        min_visible_px=sizes["min_visible_px"],
        edge_top_px=sizes["edge_top_px"],
        edge_bottom_px=sizes["edge_bottom_px"],
        edge_side_px=sizes["edge_side_px"],
        depth=limits["depth"],
        actions=limits["actions"],
        minutes=limits["minutes"],
        refused_keys=(*refused, *sorted(FIXED_DEVICE_KEYS - set(refused))),
        refused_key_prefixes=prefixes,
        warnings=warnings,
    )
