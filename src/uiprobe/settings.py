"""The settings file: where uiprobe's no-press defaults live.

The defaults are written out in a TOML file, ``uiprobe.toml``, that
``uiprobe init`` creates and that users edit freely. This module holds the
file format, the generator (`render_default_settings`,
`write_default_settings`) and a strict reader (`load_settings`). ``uiprobe crawl``
reads the file for its no-press defaults, through `policy_from_settings`, and
``uiprobe key`` reads the keys it refuses. ``press = true`` makes ``crawl``
press without ``--press`` (``--dry-run`` still previews), and a pressing crawl
takes the ``[limits]`` that no option overrides. The limits ``uiprobe init``
writes (`_DEFAULT_DEPTH`, `_DEFAULT_ACTIONS`, `_DEFAULT_MINUTES`) are also the
command line's defaults when no file value applies.

What the file holds: the deny words and their exception words, the confirm
words, the phrases that mark a
screen as irreversible, the rules (unlabeled,
number-only and toggle elements, two-target rows and rows with buttons,
toolbar up buttons, sliders,
confirm buttons in dialogs, irreversible
screens, the screen-edge
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

A file written at an older defaults version may leave out the tables and
rules that a later version added (`_LATER_TABLES`, `_LATER_RULES`): a missing
table counts as empty, so all of its defaults apply, and a missing rule takes
its default. Version 2 added the ``[irreversible]`` table, the rule
``refuse_irreversible_screens`` and four deny words. Version 3 added the
rules ``skip_two_target_rows`` (a missing one reads True) and
``skip_rows_with_buttons`` (a missing one reads False); a file written at
version 3 must have both, and an older uiprobe refuses a version 3 file
(its ``defaults_version`` is out of range there). Version 4 added the rules
``skip_navigate_up`` and ``skip_sliders`` (a missing one reads True); a file
written at version 4 must have both, and an older uiprobe refuses a version 4
file. Version 5 added the rule ``replay_path`` (a missing one reads True); a
file written at version 5 must have it, and an older uiprobe refuses a version 5
file. Version 6 added the top-level key ``record_text`` (a missing one reads
True); a file written at version 6 must have it, and an older uiprobe refuses a
version 6 file. It is about the run folder, not about judging: with false the
records of a pressing crawl hold no screen text (see `uiprobe.crawl_driver`).
Version 8 added the rule ``also_target``, a list of package names (empty by
default) that a pressing crawl may also press in (see `uiprobe.also_target`).
It is optional in every file, like ``[deny] except``, and ``uiprobe init``
writes it as a comment, so no file needs it; an older file reads as ``[]``.
Version 10 added the rule ``back_on_other_screens`` (a missing one reads False);
a file written at version 10 must have it, and an older uiprobe refuses a
version 10 file. With true, a pressing crawl presses BACK on a screen of another
app instead of restarting the app (``Policy.back_on_other_screens``).
Version 11 added the rule ``scroll_lists`` (a missing one reads False) and the
keys ``scrolls_per_screen``, ``scroll_amount`` and ``scroll_incidents`` of
``[limits]`` (a missing one reads its default, `DEFAULT_SCROLLS_PER_SCREEN`,
`DEFAULT_SCROLL_AMOUNT`, `DEFAULT_SCROLL_INCIDENTS`); a file written at
version 11 must have all four, and an older uiprobe refuses a version 11 file.
With true, a pressing crawl may turn a mouse wheel over a list (opt-in, off by
default; see `uiprobe.guards.scroll_target`).
Version 12 added the confirm words ``on`` and ``オン`` (no rules or keys): a
dialog button whose whole label is one of them is refused like the version 9
words, but a longer label that merely starts with one is not
(`DIALOG_ONLY_EXACT_LABELS`).

``[deny] except`` holds exception words (see `uiprobe.guards.word_matches`): a
deny-word hit that lies completely inside one of them, where it stands as a
whole word of a text, description or hint, does not count. The list is
optional in every file, like ``remove``, and has no default words, so it adds
no defaults and did not need a new defaults version: a file without it
decides exactly as before. An exception word written there excuses every deny
word, the built-in ones and the ones the file or ``--deny`` adds. Built-in
exception words (none at present) would excuse only built-in deny words; one
added in a later version would come with a new defaults version, tagged in
`_LATER_EXCEPT` from `_FIRST_EXCEPT` on. ``[deny] remove`` takes words out of
both ``words`` and ``except``.

An exception word must be one word of letters and digits without invisible
characters. One that equals a deny word in use (any form of it, see
`uiprobe.guards._forms`) would excuse that word wherever it stands alone, so
listing it under ``except`` is refused with a message saying to remove the
deny word instead. A built-in exception that a file receives automatically is
dropped instead when it equals a deny word of that file, with a warning (the
deny word wins).

A deny word is compared with the ``remove`` list after the normalization the
deny matching uses (`uiprobe.guards.normalize`: NFKC, case-folded, whitespace
runs made one space), so ``Login`` under ``remove`` also removes ``login``. A
confirm word is compared in the form the confirm matching uses, which also
strips surrounding punctuation and invisible characters, so ``OK!`` and ``OK``
are the same word. An irreversibility phrase is compared in the form that
decides what it matches (`uiprobe.guards._phrase_key`: normalized, curly
apostrophes made plain, spaces and separators removed), so ``can not be
undone`` under ``remove`` removes ``cannot be undone``. Duplicates in one list
are judged the same way. Key names are compared as exact strings; ``[keys]
remove`` takes names out of both ``refuse`` and ``refuse_prefixes``, and a
removed name that a remaining prefix still covers stays refused and gives a
warning. The fixed keys
(`FIXED_DEVICE_KEYS`) are always refused: they are added back whatever the
file says, and listing one under ``[keys] remove`` only adds a warning.

Reading is race-resistant (`_read_bytes`): the path is opened once, read-only
(O_NONBLOCK on POSIX, so a FIFO or device does not wait for a writer), and
every check and every byte comes from that descriptor: regular file, size at most
`MAX_SETTINGS_BYTES`, at most `MAX_SETTINGS_BYTES` + 1 bytes read, size and
times unchanged after the read, and the path still naming the same file
(st_dev, st_ino). The path is opened as given, not as `pathlib` normalises
it, so ``rules.toml/.`` fails on POSIX as it does for any program instead of
reading ``rules.toml``. A symbolic link is followed at the open. What remains: a path
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

from uiprobe.crawl_preview import check_package
from uiprobe.device import DEVICE_WIDE_KEY_PREFIXES, DEVICE_WIDE_KEYS, FIXED_DEVICE_KEYS
from uiprobe.guards import (
    _STATE_DENY_WORDS,
    API36_BOTTOM_PX,
    API36_SIDE_PX,
    API36_TOP_PX,
    CONFIRM_WORDS,
    DEFAULT_DENY_EXCEPTIONS,
    DEFAULT_DENY_WORDS,
    DEFAULT_SCROLL_AMOUNT,
    DEFAULT_SCROLL_INCIDENTS,
    DEFAULT_SCROLL_LISTS,
    DEFAULT_SCROLLS_PER_SCREEN,
    IRREVERSIBLE_PHRASES,
    MAX_SCROLL_AMOUNT,
    Policy,
    _check_word,
    _confirm_form,
    _deny_forms,
    _exception_conflict,
    _exception_key,
    _phrase_key,
    normalize,
)
from uiprobe.guards.constants import DIALOG_ONLY_EXACT_LABELS
from uiprobe.model import MIN_VISIBLE_PX
from uiprobe.pngfile import check_new_path

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

__all__ = [
    "FIXED_DEVICE_KEYS",
    "SETTINGS_FILE",
    "IncompleteFileError",
    "InterruptedFileError",
    "Settings",
    "SettingsError",
    "load_settings",
    "policy_from_settings",
    "render_default_settings",
    "write_default_settings",
]

SETTINGS_FILE: Final = "uiprobe.toml"
FORMAT_VERSION: Final = 1
DEFAULTS_VERSION: Final = 12
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
# older version would be given it only through the map's version. A new table
# or rule also goes into `_LATER_TABLES` or `_LATER_RULES` (with the value of a
# missing rule in `_LATER_RULE_VALUES`), so that older files still load.
# Version 2 added the start-over deny words, the [irreversible] table with all
# of its phrases and the rule refuse_irreversible_screens. Version 3 added the
# rules skip_two_target_rows and skip_rows_with_buttons (no list items).
# Version 4 added the rules skip_navigate_up and skip_sliders (no list items).
# Version 5 added the rule replay_path (no list items). Version 6 added the
# top-level key record_text (no list items; a missing one reads True).
# Version 7 added the state-changing deny words (forget, disconnect, ...).
# Version 8 added the rule also_target (a list of packages; optional in every
# file, a missing one reads as the empty list; `uiprobe init` writes it
# commented). Version 9 added the confirm words turn on, enable, オンにする
# and 有効にする (a dialog's "Turn on" button changes a device setting).
# Version 10 added the rule back_on_other_screens (no list items; a missing one
# reads False). Version 11 added the rule scroll_lists and the [limits] keys
# scrolls_per_screen, scroll_amount and scroll_incidents (no list items).
# Version 12 added the confirm words on and オン, which count only as the whole
# label of a button on a dialog (`DIALOG_ONLY_EXACT_LABELS`).
_LATER_DENY: Final[dict[str, int]] = {
    "やり直": 2,
    "元に戻": 2,
    "restart": 2,
    "undo": 2,
    **dict.fromkeys(_STATE_DENY_WORDS, 7),
}
# The [deny] except list has no built-in words. One added later starts at the
# next defaults version (raise DEFAULTS_VERSION with it); before that, the
# test that no default is tagged beyond the current version fails.
_FIRST_EXCEPT: Final = 13
# The defaults version that added the exact-label confirm words; one that a
# file older than this lists by hand keeps the plain matching of its entry.
_FIRST_EXACT: Final = 12
_EXACT_FORMS: Final = frozenset(_confirm_form(w) for w in DIALOG_ONLY_EXACT_LABELS)
_LATER_EXCEPT: Final[dict[str, int]] = {}
_LATER_CONFIRM: Final[dict[str, int]] = {
    "turn on": 9,
    "enable": 9,
    "オンにする": 9,
    "有効にする": 9,
    "on": 12,
    "オン": 12,
}
# The [irreversible] table came with version 2, so its items start there.
_FIRST_IRREVERSIBLE: Final = 2
_LATER_IRREVERSIBLE: Final[dict[str, int]] = {}
_LATER_KEYS: Final[dict[str, int]] = {}
_LATER_PREFIXES: Final[dict[str, int]] = {}
_DENY_WORDS: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_DENY.get(word, 1), word) for word in DEFAULT_DENY_WORDS
)
_DENY_EXCEPTIONS: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_EXCEPT.get(word, _FIRST_EXCEPT), word) for word in DEFAULT_DENY_EXCEPTIONS
)
_CONFIRM_WORDS: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_CONFIRM.get(word, 1), word) for word in CONFIRM_WORDS
)
_IRREVERSIBLE_PHRASES: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_IRREVERSIBLE.get(phrase, _FIRST_IRREVERSIBLE), phrase)
    for phrase in IRREVERSIBLE_PHRASES
)
_REFUSE_KEYS: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_KEYS.get(key, 1), key)
    for key in sorted(DEVICE_WIDE_KEYS - FIXED_DEVICE_KEYS)
)
_REFUSE_PREFIXES: Final[tuple[tuple[int, str], ...]] = tuple(
    (_LATER_PREFIXES.get(prefix, 1), prefix) for prefix in DEVICE_WIDE_KEY_PREFIXES
)

# The crawl limits: what `uiprobe init` writes under [limits] and what a
# pressing crawl uses when neither an option nor the file gives a value. The
# command line imports these, so both always agree.
_DEFAULT_DEPTH: Final = 5
_DEFAULT_ACTIONS: Final = 300
_DEFAULT_MINUTES: Final = 30
_DEFAULT_SCROLLS_PER_SCREEN: Final = DEFAULT_SCROLLS_PER_SCREEN
_DEFAULT_SCROLL_AMOUNT: Final = DEFAULT_SCROLL_AMOUNT
_DEFAULT_SCROLL_INCIDENTS: Final = DEFAULT_SCROLL_INCIDENTS

_KEY_NAME: Final = re.compile(r"KEYCODE_[A-Z0-9_]{1,40}")
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_UTF16_BOMS: Final = (b"\xff\xfe", b"\xfe\xff")

_TOP_KEYS: Final = frozenset(
    {"format_version", "defaults_version", "press", "runs_dir", "record_text"}
)
_BOOL_RULES: Final = (
    "tap_unlabeled",
    "skip_number_only",
    "skip_toggles",
    "refuse_dialog_confirm",
    "refuse_irreversible_screens",
    "skip_two_target_rows",
    "skip_rows_with_buttons",
    "skip_navigate_up",
    "skip_sliders",
    "replay_path",
    "back_on_other_screens",
    "scroll_lists",
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
        "scrolls_per_screen": (1, 100),
        "scroll_amount": (1, MAX_SCROLL_AMOUNT),
        "scroll_incidents": (1, 100),
    },
}
# Tables and keys that a later defaults version added, with that version. A
# file written at an older version may leave them out: a missing table reads
# as empty (its list defaults are then merged like any later default) and a
# missing rule takes the value in `_LATER_RULE_VALUES`. When present they are
# checked like every other table and key.
_LATER_TABLES: Final[dict[str, int]] = {"irreversible": 2}
_LATER_RULES: Final[dict[str, int]] = {
    "refuse_irreversible_screens": 2,
    "skip_two_target_rows": 3,
    "skip_rows_with_buttons": 3,
    "skip_navigate_up": 4,
    "skip_sliders": 4,
    "replay_path": 5,
    "back_on_other_screens": 10,
    "scroll_lists": 11,
}
# Keys of [limits] that a later version added, with that version (a missing one
# takes its default).
_LATER_LIMITS: Final[dict[str, int]] = {
    "scrolls_per_screen": 11,
    "scroll_amount": 11,
    "scroll_incidents": 11,
}
_LATER_LIMIT_VALUES: Final[dict[str, int]] = {
    "scrolls_per_screen": _DEFAULT_SCROLLS_PER_SCREEN,
    "scroll_amount": _DEFAULT_SCROLL_AMOUNT,
    "scroll_incidents": _DEFAULT_SCROLL_INCIDENTS,
}
# Top-level keys a later version added, with that version (a missing one reads
# True).
_LATER_TOP: Final[dict[str, int]] = {"record_text": 6}
_LATER_RULE_VALUES: Final[dict[str, bool]] = {
    "refuse_irreversible_screens": True,
    "skip_two_target_rows": True,
    "skip_rows_with_buttons": False,
    "skip_navigate_up": True,
    "skip_sliders": True,
    "replay_path": True,
    "back_on_other_screens": False,
    "scroll_lists": DEFAULT_SCROLL_LISTS,
}
_TABLE_KEYS: Final[dict[str, frozenset[str]]] = {
    "deny": frozenset({"words", "except", "remove"}),
    "confirm": frozenset({"words", "remove"}),
    "irreversible": frozenset({"phrases", "remove"}),
    "rules": frozenset(_BOOL_RULES) | frozenset(_INT_RANGES["rules"]) | {"also_target"},
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
    holds every key of `FIXED_DEVICE_KEYS`. ``deny_exceptions`` holds the
    exception words of ``[deny] except``; it is the last field and defaults to
    ``()``, so a Settings built with the fields of an earlier version, by
    keyword or by position, still works. ``skip_two_target_rows`` (default
    True) and ``skip_rows_with_buttons`` (default False), added with defaults
    version 3, and ``skip_navigate_up`` and ``skip_sliders`` (both default
    True), added with defaults version 4, ``replay_path`` (default True), added
    with defaults version 5, ``record_text`` (default True), added with
    defaults version 6, and ``runs_dir`` (default None, an
    optional key that needed no new defaults version), follow it for the same
    reason. ``also_target`` (default ``()``), added with defaults version 8,
    comes after it for the same reason, then ``back_on_other_screens`` (default
    False), added with defaults version 10, and last ``scroll_lists`` (default
    `DEFAULT_SCROLL_LISTS`, False) and ``scrolls_per_screen``,
    ``scroll_amount`` and ``scroll_incidents`` (defaults
    `DEFAULT_SCROLLS_PER_SCREEN`, `DEFAULT_SCROLL_AMOUNT` and
    `DEFAULT_SCROLL_INCIDENTS`), added with defaults version 11.
    """

    source: Path
    format_version: int
    defaults_version: int
    press: bool
    deny_words: tuple[str, ...]
    confirm_words: tuple[str, ...]
    irreversible_phrases: tuple[str, ...]
    tap_unlabeled: bool
    skip_number_only: bool
    skip_toggles: bool
    refuse_dialog_confirm: bool
    refuse_irreversible_screens: bool
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
    # Last and with a default, so that callers that build a Settings from the
    # fields of earlier versions (by keyword or position) keep working.
    deny_exceptions: tuple[str, ...] = ()
    skip_two_target_rows: bool = True
    skip_rows_with_buttons: bool = False
    skip_navigate_up: bool = True
    skip_sliders: bool = True
    # The optional top-level key runs_dir: the folder that gets the run
    # folders of a pressing crawl without --out (None: not set).
    runs_dir: str | None = None
    # Added with defaults version 5, after runs_dir so that a Settings built
    # by position with the earlier fields keeps working.
    replay_path: bool = True
    # Added with defaults version 6: False keeps screen text out of the
    # records of a pressing crawl. Not a Policy field: it does not judge.
    record_text: bool = True
    # Added with defaults version 8: the packages (besides the target app)
    # that a pressing crawl may also press in. Empty by default.
    also_target: tuple[str, ...] = ()
    # Added with defaults version 10: press BACK on a screen of another app
    # instead of restarting the app (``Policy.back_on_other_screens``).
    back_on_other_screens: bool = False
    # Added with defaults version 11: ``[rules] scroll_lists`` (off by default)
    # and the ``[limits]`` keys ``scrolls_per_screen``, ``scroll_amount`` and
    # ``scroll_incidents``.
    scroll_lists: bool = DEFAULT_SCROLL_LISTS
    scrolls_per_screen: int = DEFAULT_SCROLLS_PER_SCREEN
    scroll_amount: int = DEFAULT_SCROLL_AMOUNT
    scroll_incidents: int = DEFAULT_SCROLL_INCIDENTS
    # The version 12 words ``on`` and ``オン`` that a file older than version 12
    # lists by hand: they keep the matching of every other confirm word
    # (`Policy.legacy_confirm_words`). Empty for a file of version 12 or later.
    legacy_confirm_words: tuple[str, ...] = ()


def _separators() -> str:
    """The path separators of this platform: also a backslash on Windows."""
    return "/\\" if sys.platform == "win32" else "/"


def _folder_form_base(path: str) -> str | None:
    """Return `path` without a trailing separator or ``.`` part, if it has one.

    ``rules.toml/``, ``rules.toml/.`` and ``rules.toml/./`` are the folder
    form of ``rules.toml``; None means `path` has no such ending. On Windows a
    backslash counts as a separator too. The end is scanned by index, once.
    """
    separators = _separators()
    end = len(path)
    while end:
        if path[end - 1] in separators:
            end -= 1
        elif end >= 2 and path[end - 1] == "." and path[end - 2] in separators:
            end -= 2
        else:
            break
    return None if end == len(path) else path[:end]


def _without_trailing_separators(
    path: str,
    *,
    windows: bool | None = None,
    splitdrive: Callable[[str], tuple[str, str]] = os.path.splitdrive,
) -> str:
    """Return `path` without trailing separators, keeping a root (``/``, ``C:\\``).

    A path made only of separators is kept exactly as written: on POSIX
    ``//`` can be a root of its own, and on Windows it starts a UNC name.
    `windows` and `splitdrive` default to this platform's rules (tests pass
    Windows ones). The end is scanned by index, once.
    """
    if windows is None:
        windows = sys.platform == "win32"
    separators = "/\\" if windows else "/"
    if all(char in separators for char in path):
        return path
    drive = splitdrive(path)[0] if windows else ""
    keep = len(drive) + 1  # the drive and one separator: the root
    end = len(path)
    while end > keep and path[end - 1] in separators:
        end -= 1
    return path[:end]


def policy_from_settings(
    settings: Settings,
    *,
    extra_deny: Iterable[str] = (),
    tap_unlabeled: bool | None = None,
) -> Policy:
    """Return the `Policy` that the effective `settings` describe.

    The deny words, their exception words and the confirm words, the
    unlabeled, number-only, toggle, row, up-button, slider and
    dialog-confirm rules, the edge sizes and the smallest visible size come
    from the settings.
    `extra_deny` adds deny words for this policy only (for example
    ``--deny``); an exception word equal to one of them is dropped, so the
    added word blocks as given.
    `tap_unlabeled` None keeps the file's value; a bool replaces it (the
    command line passes True to force it for one run).

    ``min_visible_px`` applies to the rules of `uiprobe.guards.judge`;
    ``Screen.tap_point``, ``Screen.find`` and ``Screen.visible_bounds`` keep the fixed
    `MIN_VISIBLE_PX`. The settings built from the file written by
    `render_default_settings` give a policy that decides exactly as ``Policy()``.

    Raises:
        TypeError: `extra_deny` is one string, not a collection of words.
        ValueError: A word in `extra_deny` is empty after normalization.
    """
    policy = Policy(
        deny_words=settings.deny_words,
        deny_exceptions=settings.deny_exceptions,
        confirm_words=settings.confirm_words,
        legacy_confirm_words=settings.legacy_confirm_words,
        irreversible_phrases=settings.irreversible_phrases,
        skip_irreversible=settings.refuse_irreversible_screens,
        tap_unlabeled=settings.tap_unlabeled
        if tap_unlabeled is None
        else tap_unlabeled,
        skip_number_only=settings.skip_number_only,
        skip_toggles=settings.skip_toggles,
        skip_two_target_rows=settings.skip_two_target_rows,
        skip_rows_with_buttons=settings.skip_rows_with_buttons,
        skip_navigate_up=settings.skip_navigate_up,
        skip_sliders=settings.skip_sliders,
        replay_path=settings.replay_path,
        also_target=settings.also_target,
        back_on_other_screens=settings.back_on_other_screens,
        scroll_lists=settings.scroll_lists,
        refuse_dialog_confirm=settings.refuse_dialog_confirm,
        edges=(
            settings.edge_top_px,
            settings.edge_bottom_px,
            settings.edge_side_px,
            settings.edge_side_px,
        ),
        min_visible_px=settings.min_visible_px,
    )
    return policy.with_deny_words(extra_deny)


# -- the generator -------------------------------------------------------------


def _toml_bool(value: bool) -> str:  # noqa: FBT001
    return "true" if value else "false"


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
        "# With true, crawl presses for real without --press (it still needs",
        "# --out DIR, or runs_dir below, and --allow-real-device on a device",
        "# that is not an emulator); --dry-run previews instead.",
        "press = false",
        "# With false, the records of a pressing crawl (screens, transitions,",
        "# summary) hold no screen text; texts that were there are written as",
        '# empty strings and named under "hidden". Screenshots then need true.',
        "# Password fields are always hidden, whatever this says.",
        "record_text = true",
        "# The folder that gets the run folders of a pressing crawl without --out",
        "# (relative to the folder of this file; it must exist):",
        '# runs_dir = "uiprobe-runs"',
        "",
        "[deny]",
        "# Never press an element that contains one of these words.",
        *_toml_list("words", _current(_DENY_WORDS)),
        "# Exception words, none by default. A deny word found inside one of these",
        "# words does not count where the word stands whole in a text: with",
        '# "automatically" here, "call" no longer refuses "Automatically". An',
        "# element holding the word is then pressed unless another deny word",
        '# matches, also "Bill my card automatically"; that is your',
        "# responsibility. A word here excuses every deny word, also the ones you",
        '# add. Any other word that holds a deny word is still refused. "remove"',
        "# also takes words out of this list.",
        *_toml_list("except", _current(_DENY_EXCEPTIONS)),
        "remove = []",
        "",
        "[confirm]",
        "# Labels of buttons that confirm what the screen asks.",
        "# A label counts when it equals a word or starts with one followed by a",
        '# character that is not a letter ("Yes, continue"). A word that ends in a',
        "# Japanese letter (kana, kanji) needs no character after it, so the word",
        '# "はい" also matches "はいる".',
        *_toml_list("words", _current(_CONFIRM_WORDS)),
        "remove = []",
        "",
        "[irreversible]",
        "# A screen whose text holds one of these phrases is refused as a whole:",
        "# nothing on it is pressed (see refuse_irreversible_screens below).",
        *_toml_list("phrases", _current(_IRREVERSIBLE_PHRASES)),
        "remove = []",
        "",
        "[rules]",
        "tap_unlabeled = false",
        "skip_number_only = true",
        "skip_toggles = true",
        "refuse_dialog_confirm = true",
        "refuse_irreversible_screens = true",
        "# Do not press an Android Settings row that has a second button (such as",
        "# the gear of a network row): pressing the row itself changes the",
        "# setting, for example the active network.",
        "skip_two_target_rows = true",
        "# Do not press any row that holds its own button. Turn on for vendor",
        "# Settings apps whose rows have no divider id; it also refuses list",
        "# rows of ordinary apps that hold a button (a star, a menu).",
        "skip_rows_with_buttons = false",
        "# Do not press the up button at the top left of a toolbar: the crawl",
        "# presses the rows of a sub-page first and goes back with BACK.",
        "skip_navigate_up = true",
        "# Do not press a slider (a seek bar, a rating bar), a row that holds",
        "# one, or anything whose press point lies on one: a tap moves it.",
        "skip_sliders = true",
        "# After a relaunch, press again the rows pressed before to get back down",
        "# to a screen that still has rows to try, and count a row seen with and",
        "# without a badge as one row. false returns both the path replay and the",
        '# "do not press the same row twice when only a badge changed" behaviour',
        "# to the earlier behaviour.",
        "replay_path = true",
        "# With true, a pressing crawl presses BACK on another app's screen (up to",
        "# twice before it restarts the app) instead of restarting at once; that",
        "# BACK reaches whichever window has the input focus, not necessarily the",
        "# app being crawled. --back-on-other-screens and --no-back-on-other-screens",
        "# override it for one run.",
        "back_on_other_screens = false",
        "# With true, a pressing crawl turns a mouse wheel over a list (content",
        "# further down comes up) on a screen with nothing left to press, to reach",
        "# the rows below the fold; at most scrolls_per_screen times per screen",
        "# (see the limits below), and only when the device lists the wheel scroll.",
        "# The wheel goes only to a ListView, ScrollView, RecyclerView or",
        "# NestedScrollView at a point where everything is a plain layout or label.",
        "# An app's own view of such a class can still react to the wheel (see",
        "# docs/safety.md). --scroll and --no-scroll override it for one run.",
        f"scroll_lists = {_toml_bool(DEFAULT_SCROLL_LISTS)}",
        '# also_target = ["com.example.helper"]   # apps the crawl may also press in; system UI and the home app are refused, the controls of permission screens are never tapped (they get BACK when back_on_other_screens is true)',  # noqa: E501
        f"min_visible_px = {MIN_VISIBLE_PX}",
        f"edge_top_px = {API36_TOP_PX}",
        f"edge_bottom_px = {API36_BOTTOM_PX}",
        f"edge_side_px = {API36_SIDE_PX}",
        "",
        "[limits]",
        "# The limits of a pressing crawl; --depth, --actions and --minutes",
        "# override them for one run.",
        f"depth = {_DEFAULT_DEPTH}",
        f"actions = {_DEFAULT_ACTIONS}",
        f"minutes = {_DEFAULT_MINUTES}",
        "# The most wheel scrolls sent on one screen (with scroll_lists).",
        f"scrolls_per_screen = {_DEFAULT_SCROLLS_PER_SCREEN}",
        "# The size of one wheel scroll, 1 to 10.",
        f"scroll_amount = {_DEFAULT_SCROLL_AMOUNT}",
        "# After this many scrolls that changed something or opened another",
        "# screen, the run stops scrolling.",
        f"scroll_incidents = {_DEFAULT_SCROLL_INCIDENTS}",
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
        FileExistsError: `path` already exists (a file, a directory or a link,
            also a link whose target is missing).
        IncompleteFileError: The file was created but writing, flushing or
            closing it failed; it stays on disk, incomplete, because uiprobe
            never deletes files.
        InterruptedFileError: Ctrl+C arrived after the file was created (a
            KeyboardInterrupt); the file stays, possibly incomplete.
        OSError: The file cannot be created, or whether something is at
            `path` cannot be told (access to a folder above it is denied, for
            example).
        TypeError: `path` is not a str or an os.PathLike of str (neither an
            int nor bytes is taken, so nothing is used as an open descriptor).
        ValueError: `path` is empty, holds a null character, or on Windows
            names a stream of a file (a colon after the drive), which would
            change the file the stream belongs to.
    """
    path = os.fspath(path)  # refuses an int, which open() takes as a descriptor
    if isinstance(path, str):
        # A str subclass can define __index__, and open() would then take it
        # as a descriptor; its text is used as a plain str instead.
        path = str.__str__(path)
    # The same checks as for a screenshot file. On POSIX the create-only open
    # already fails on any existing path, including a link whose target is
    # missing. On Windows it follows such a link and creates the target, so an
    # existing entry of any kind is refused first. A link made between this
    # check and the open is not caught on Windows; the docs say so.
    check_new_path(path)
    text = render_default_settings()
    handle = open(path, "x", encoding="utf-8", newline="\n")  # noqa: PTH123, SIM115
    interrupted = False
    try:
        try:
            print(text, end="", file=handle)
            handle.flush()
        except KeyboardInterrupt:
            interrupted = True
            raise
        finally:
            handle.close()
    except OSError as error:
        # A failing close must not hide a Ctrl+C that arrived while writing.
        # Only a Ctrl+C caught here counts: an OSError raised while a caller
        # handles its own KeyboardInterrupt is a write failure.
        if interrupted:
            raise InterruptedFileError(path) from error
        raise IncompleteFileError(error) from error
    except KeyboardInterrupt as error:
        raise InterruptedFileError(path) from error


# -- the reader ----------------------------------------------------------------


# What `_shown` writes for an escape; typed out in a name, it is the lookalike.
_ESCAPE_LOOKALIKE: Final = re.compile(r"\\(?:x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8})")


def _shown(text: str) -> str:
    """Escape characters that must not reach a terminal raw.

    The escapes are Python's: ``\\xNN``, ``\\uNNNN`` or ``\\UNNNNNNNN``, so a
    code point never reads as a shorter escape followed by digits. Text that
    needs no escape and holds nothing that reads like one is shown as it is, so
    a Windows path stays readable. Otherwise every backslash is doubled as
    well: then a character and the same characters typed out as an escape
    (``\\u202e``) show differently, and two different texts never show alike.
    """
    if text.isprintable() and _ESCAPE_LOOKALIKE.search(text) is None:
        return text
    parts = []
    for char in text:
        point = ord(char)
        if char == "\\":
            parts.append("\\\\")
        elif char.isprintable():
            parts.append(char)
        elif point < 0x100:
            parts.append(f"\\x{point:02x}")
        elif point < 0x10000:
            parts.append(f"\\u{point:04x}")
        else:
            parts.append(f"\\U{point:08x}")
    return "".join(parts)


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
        self,
        data: Mapping[str, object],
        name: str,
        allowed: frozenset[str],
        *,
        optional: bool = False,
    ) -> Mapping[str, object]:
        if name not in data and optional:
            return {}
        if name not in data:
            raise self.fail(f"[{name}]", "the table is missing")
        value = data[name]
        if not isinstance(value, dict):
            raise self.fail(f"[{name}]", "must be a table")
        for key in value:
            if key not in allowed:
                raise self.fail(f"[{name}] {key}", "unknown key")
        return value

    def boolean(
        self,
        table: Mapping[str, object],
        name: str,
        key: str,
        default: bool | None = None,  # noqa: FBT001 - the value of a missing key
    ) -> bool:
        value = table.get(key)
        if key not in table and default is not None:
            return default
        if key not in table:
            raise self.fail(self._where(name, key), "is missing")
        if not isinstance(value, bool):
            raise self.fail(self._where(name, key), "must be true or false")
        return value

    def integer(
        self,
        table: Mapping[str, object],
        name: str,
        key: str,
        low: int,
        high: int,
        default: int | None = None,
    ) -> int:
        value = table.get(key)
        where = self._where(name, key)
        if key not in table:
            if default is not None:
                return default
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


def _exception_identity(word: str) -> str:
    """Check an exception word as `Policy` does; return what makes two equal.

    The identity is `normalize`, as for a deny word, so that ``[deny] remove``
    compares both lists alike; for one word it is the exception's key.
    """
    identity = _word_identity(word)
    _exception_key(word)
    return identity


def _confirm_identity(word: str) -> str:
    """Check a confirm word; return the form the confirm matching compares."""
    _word_identity(word)
    form = _confirm_form(word)
    if not form:
        msg = f"confirm word is empty after cleaning: {word!r}"
        raise ValueError(msg)
    return form


def _phrase_identity(phrase: str) -> str:
    """Check a phrase as `Policy` does; return what makes two phrases equal.

    Two phrases are equal when they match the same texts: the same after
    normalization, with curly apostrophes read as plain ones and spaces and
    separators ignored (`uiprobe.guards._phrase_key`), so ``can not be undone``
    and ``cannot be undone`` are one phrase. A phrase with nothing left
    once spaces, separators and invisible characters are removed (``-``) is
    refused: it has no words to match.
    """
    _word_identity(phrase)
    key = _phrase_key(phrase)
    if not key:
        msg = (
            "phrase is empty after removing spaces, separators and invisible"
            f" characters: {phrase!r}"
        )
        raise ValueError(msg)
    return key


def _package_identity(name: str) -> str:
    """Check an ``also_target`` entry as the command's PACKAGE is checked
    (the format only); return what makes two entries equal (the name itself).

    The system UI and the home app are refused where the crawl starts
    (`uiprobe.also_target`), not here: a settings file only holds names.
    """
    try:
        return check_package(name)
    except ValueError as error:
        raise ValueError(_capped(_shown(str(error)))) from None


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


def _exceptions(
    reader: _Reader,
    listed: Sequence[str],
    merged: Sequence[str],
    deny_words: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the exception words in use and the warnings about dropped ones.

    An exception that equals a deny word in use is refused when the file lists
    it under ``except``; one that only came in as a later default is dropped,
    with a warning, so the deny word keeps blocking.

    Raises:
        SettingsError: A listed exception word equals a deny word.
    """
    listed_keys = {normalize(word) for word in listed}
    deny_forms = _deny_forms(deny_words)
    kept: list[str] = []
    warnings: list[str] = []
    for exception in merged:
        word = _exception_conflict(exception, deny_forms)
        if word is None:
            kept.append(exception)
        elif normalize(exception) in listed_keys:
            raise reader.fail(
                "[deny] except",
                f"{_value_text(exception)} equals the deny word {_value_text(word)};"
                " an exception cannot let its own deny word through: to let it"
                " through, remove the deny word instead. If this file was written"
                " by an older uiprobe, the deny word may be a default that a later"
                f" defaults version added (current: {DEFAULTS_VERSION}, which added"
                " forget, disconnect and others): drop the word from [deny] except"
                " (it is now a default deny word), or add it to [deny] remove",
            )
        else:
            warnings.append(
                f"[deny] the default exception {_value_text(exception)} is not"
                f" applied because it equals the deny word {_value_text(word)}"
            )
    return tuple(kept), tuple(warnings)


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


def _read_bytes(
    path: str | os.PathLike[str], shown: str, limit: int | None = None
) -> bytes:
    """Read the file `path` (at most one byte more than `limit`, which is the
    current `MAX_SETTINGS_BYTES` unless the caller gives another; `uiprobe tree`
    reads its run folder files through here).

    The path is opened once, as given (a str is not normalised, so a trailing
    ``/.`` after a file fails as it does for any program); every check and
    every byte comes from that descriptor, never from the path again (the path
    is only stat-ed at the end to see that it still names the opened file).
    """

    def fail(text: str) -> SettingsError:
        return SettingsError(f"{shown}: {text}")

    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise fail("no such file: the link's target is missing") from None
        raise fail("no such file") from None
    except (OSError, ValueError) as error:
        # Windows cannot open a directory (PermissionError): name it as POSIX
        # does. Only the message is chosen here; no byte is read.
        try:
            kind = os.stat(path).st_mode  # noqa: PTH116 - the path as given
        except (OSError, ValueError):
            kind = stat.S_IFREG
        if not stat.S_ISREG(kind):
            raise fail("not a regular file") from None
        raise fail(f"cannot be read: {_shown(str(error))}") from None
    try:
        return _read_descriptor(fd, path, fail, limit)
    finally:
        _close_quietly(fd)


def _read_descriptor(
    fd: int,
    path: str | os.PathLike[str],
    fail: Callable[[str], SettingsError],
    cap: int | None = None,
) -> bytes:
    if cap is None:
        cap = MAX_SETTINGS_BYTES  # read now, so a reassignment still counts
    changed = "the file changed while it was read; try again"
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise fail("not a regular file")
        if before.st_size > cap:
            raise fail(f"larger than {cap} bytes")
        chunks: list[bytes] = []
        total = 0
        limit = cap + 1
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
    if total > cap:
        raise fail(f"larger than {cap} bytes")
    if total != before.st_size or _file_identity(after) != _file_identity(before):
        raise fail(changed)
    try:
        named = os.stat(path)  # noqa: PTH116 - the path as given, not Path()
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


def _runs_dir(parsed: dict[str, object], reader: _Reader) -> str | None:
    """Read the optional ``runs_dir``: a non-empty path that names a folder."""
    if "runs_dir" not in parsed:
        return None
    value = parsed["runs_dir"]
    if not isinstance(value, str):
        raise reader.fail("runs_dir", "must be a string (a folder path)")
    if not value:
        raise reader.fail("runs_dir", "must not be empty")
    if "\0" in value:
        raise reader.fail("runs_dir", "must not hold a null character")
    # Trailing separators only say "a folder", which runs_dir names anyway;
    # a "/." ending stays refused: in --config that form hid a file.
    value = _without_trailing_separators(value)
    base = _folder_form_base(value)
    if base is not None and value.endswith("."):
        hint = f"; write {_value_text(base)}" if base else ""
        raise reader.fail("runs_dir", f'must not end with "/."{hint}')
    return value


def load_settings(path: str | os.PathLike[str]) -> Settings:
    """Read and validate the settings file `path`; return the effective values.

    The file must be a regular file of at most 1 MiB, strict UTF-8 (a UTF-8 BOM
    is accepted, UTF-16 is refused) and valid TOML. ``format_version`` and
    ``defaults_version`` and every table and key that `render_default_settings`
    writes must be present; only the ``remove`` lists and ``[deny] except``
    may be left out, and a file written at an older defaults version may leave
    out what a later version added (`_LATER_TABLES`, `_LATER_RULES`): the
    missing rule takes its default and the missing list its defaults. A
    listed exception word equal to a deny word in use is refused. Unknown
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
    # One string for the read and for `Settings.source`, so a PathLike that
    # answers differently each time cannot name two files.
    given = os.fspath(path)
    location = Path(given)
    reader = _Reader(location)
    # Open the path as given: Path() drops a trailing "/." (and "."
    # components), so "rules.toml/." would read rules.toml, which no other
    # program would. Messages about the read name the path as given.
    data = _read_bytes(given, _shown(given))
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
    runs_dir = _runs_dir(parsed, reader)
    record_text = reader.boolean(
        parsed,
        "",
        "record_text",
        True if defaults_version < _LATER_TOP["record_text"] else None,
    )

    deny = reader.table(parsed, "deny", _TABLE_KEYS["deny"])
    deny_listed = reader.strings(deny, "deny", "words", _word_identity)
    except_listed = reader.strings(
        deny,
        "deny",
        "except",
        _exception_identity,
        optional=True,
    )
    deny_removed = reader.strings(deny, "deny", "remove", _word_identity, optional=True)
    deny_words = _merge(
        deny_listed, deny_removed, _DENY_WORDS, defaults_version, normalize
    )
    deny_exceptions, except_warnings = _exceptions(
        reader,
        except_listed,
        _merge(
            except_listed, deny_removed, _DENY_EXCEPTIONS, defaults_version, normalize
        ),
        deny_words,
    )
    confirm = reader.table(parsed, "confirm", _TABLE_KEYS["confirm"])
    confirm_listed = reader.strings(confirm, "confirm", "words", _confirm_identity)
    confirm_removed = reader.strings(
        confirm, "confirm", "remove", _confirm_identity, optional=True
    )

    irreversible = reader.table(
        parsed,
        "irreversible",
        _TABLE_KEYS["irreversible"],
        optional=defaults_version < _LATER_TABLES["irreversible"],
    )
    phrase_listed = reader.strings(
        irreversible,
        "irreversible",
        "phrases",
        _phrase_identity,
        optional=defaults_version < _LATER_TABLES["irreversible"],
    )
    phrase_removed = reader.strings(
        irreversible, "irreversible", "remove", _phrase_identity, optional=True
    )

    rules = reader.table(parsed, "rules", _TABLE_KEYS["rules"])
    flags = {
        key: reader.boolean(
            rules,
            "rules",
            key,
            _LATER_RULE_VALUES[key]
            if defaults_version < _LATER_RULES.get(key, 1)
            else None,
        )
        for key in _BOOL_RULES
    }
    also_target = reader.strings(
        rules,
        "rules",
        "also_target",
        _package_identity,
        optional=True,  # written commented by `uiprobe init`, so never required
    )
    sizes = {
        key: reader.integer(rules, "rules", key, *bounds)
        for key, bounds in _INT_RANGES["rules"].items()
    }
    limits_table = reader.table(parsed, "limits", _TABLE_KEYS["limits"])
    limits = {
        key: reader.integer(
            limits_table,
            "limits",
            key,
            *bounds,
            default=_LATER_LIMIT_VALUES[key]
            if defaults_version < _LATER_LIMITS.get(key, 1)
            else None,
        )
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
    warnings = (
        except_warnings
        + tuple(
            f"[keys] remove lists {key}, which is always refused;"
            " the entry has no effect"
            for key in key_removed
            if key in FIXED_DEVICE_KEYS
        )
        + tuple(
            f"[keys] remove lists {key}, which a refused prefix still covers;"
            " the entry has no effect"
            for key in key_removed
            if key not in FIXED_DEVICE_KEYS and key.startswith(tuple(prefixes))
        )
    )

    return Settings(
        source=location,
        format_version=format_version,
        defaults_version=defaults_version,
        press=press,
        runs_dir=runs_dir,
        record_text=record_text,
        deny_words=deny_words,
        deny_exceptions=deny_exceptions,
        legacy_confirm_words=tuple(
            word
            for word in confirm_listed
            if defaults_version < _FIRST_EXACT and _confirm_form(word) in _EXACT_FORMS
        ),
        confirm_words=_merge(
            confirm_listed,
            confirm_removed,
            _CONFIRM_WORDS,
            defaults_version,
            _confirm_form,
        ),
        irreversible_phrases=_merge(
            phrase_listed,
            phrase_removed,
            _IRREVERSIBLE_PHRASES,
            defaults_version,
            _phrase_identity,
        ),
        tap_unlabeled=flags["tap_unlabeled"],
        skip_number_only=flags["skip_number_only"],
        skip_toggles=flags["skip_toggles"],
        refuse_dialog_confirm=flags["refuse_dialog_confirm"],
        refuse_irreversible_screens=flags["refuse_irreversible_screens"],
        skip_two_target_rows=flags["skip_two_target_rows"],
        skip_rows_with_buttons=flags["skip_rows_with_buttons"],
        skip_navigate_up=flags["skip_navigate_up"],
        skip_sliders=flags["skip_sliders"],
        replay_path=flags["replay_path"],
        also_target=also_target,
        back_on_other_screens=flags["back_on_other_screens"],
        scroll_lists=flags["scroll_lists"],
        scrolls_per_screen=limits["scrolls_per_screen"],
        scroll_amount=limits["scroll_amount"],
        scroll_incidents=limits["scroll_incidents"],
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
