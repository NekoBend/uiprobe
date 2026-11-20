"""The uid of an installed package, read from ``dumpsys package <name>``.

The package's own stanza holds it::

    Packages:
      Package [com.android.settings] (459bdd):
        appId=1000
        sharedUser=SharedUserSetting{e0f06a3 android.uid.system/1000}

API 24 to 33 print ``userId=N``, API 34 to 36 ``appId=N`` (seen on
emulators at API 24, 26 and 28 to 36; both are read). The
number is the app id, which is the uid of the app's processes and windows for
Android user 0; for another user the uid is ``user * 100000 + app id``.
``sharedUser=`` is there when the package shares its uid with other packages
(``android:sharedUserId``, such as ``android.uid.system`` for Settings).
Only the one stanza headed exactly ``Package [<name>]`` inside the
``Packages:`` section is read, up to the next line at its own indent or less
(the next stanza, or the next section at indent 0); a dump that ends inside
the stanza (``dumpsys`` can stop part way, and a missing ``sharedUser=``
line would make a shared uid look unshared) is not read. A later ``Shared
users:`` section repeats the number under ``SharedUser [...]`` and is not
read, nor are other packages' stanzas or ``Hidden system packages:``. The
package must be installed for user 0: its stanza has a ``User 0: ...
installed=true ...`` line (as on every emulator checked). The stanza's header
must be at the stanza indent (two spaces, its fields at four); a header of
the package anywhere else in the section gives None, and so do a number of
100000 or more, two different numbers in the stanza (or a number line of
another form, such as ``appId=1 userId=2``), a ``sharedUser=`` line of
another form or at another indent, and a ``Shared users:`` entry with the
package's number while the stanza has no ``sharedUser=`` line.

Only a line whose key (the text before its first ``=``) is ``userId`` or
``appId`` claims a number, and only a line keyed ``sharedUser`` claims a
shared user: the stanza also prints text the app sets (``versionName=``, the
names of requested permissions), so ``versionName=Release (appId=7)`` or a
permission ``sharedUser.demo.permission.SYNC`` claims nothing. The system
printed those fields as lines of their own on every emulator checked, so
text inside a line of another key is not read as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["PackageUid", "parse_package_uid"]

_NUMBER = re.compile(r"^(?:userId|appId)=(\d{1,10})$")
# The keys of the stanza's number lines (a line keyed so claims a number).
_NUMBER_KEYS: Final = frozenset({"userId", "appId"})
_SHARED_KEY: Final = "sharedUser"
# Any claim of a number, anywhere in a line of the ``Shared users:`` section.
_CLAIM = re.compile(r"\b(?:userId|appId)=(\d+)")
_SHARED_USERS: Final = "Shared users:"
_SHARED_HEADER = re.compile(r"^  SharedUser \[[^\]]*\] \([0-9a-f]+\):$")
_SHARED = re.compile(
    r"^sharedUser=SharedUserSetting\{[0-9a-f]{1,16} [A-Za-z0-9_.]{1,255}/(\d{1,10})\}$"
)
_USER0 = re.compile(r"^User 0:(?: |$)")
_INSTALLED = re.compile(r"^User 0: (?:.* )?installed=true(?: |$)")
_SECTION: Final = "Packages:"
# The indents the emulators printed: a stanza's header under Packages:, and
# its fields.
_HEADER_INDENT: Final = 2
_FIELD_INDENT: Final = 4
# Android gives each user 100000 uids: an app id is below that (user 0's uid).
_PER_USER: Final = 100000


@dataclass(frozen=True, slots=True)
class PackageUid:
    """The app id of a package and whether other packages share it."""

    uid: int
    shared: bool


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _key(line: str) -> str | None:
    """The key of a stanza line (stripped): the text before its first ``=``,
    None when it has none."""
    key, sign, _ = line.partition("=")
    return key if sign else None


def _section(lines: list[str], name: str = _SECTION) -> list[str] | None:
    """The lines of the section `name`, with the line that ends it (a line at
    indent 0) when there is one; None unless exactly one section."""
    heads = [i for i, line in enumerate(lines) if line == name]
    if len(heads) != 1:
        return None
    found: list[str] = []
    for line in lines[heads[0] + 1 :]:
        found.append(line)
        if line.strip() and _indent(line) == 0:
            break
    return found


def _stanza(lines: list[str], package: str) -> list[str] | None:
    """The lines of `package`'s one stanza in the ``Packages:`` section, None
    when there is not exactly one or it is not ended by a line at its header's
    indent or less (a dump cut off inside the stanza)."""
    section = _section(lines)
    if section is None:
        return None
    header = re.compile(rf"^ +Package \[{re.escape(package)}\] \([0-9a-f]+\):$")
    heads = [i for i, line in enumerate(section) if header.match(line)]
    # A header of the package at another indent (inside another stanza) is
    # not its stanza, and makes the section unclear.
    if len(heads) != 1 or _indent(section[heads[0]]) != _HEADER_INDENT:
        return None
    level = _HEADER_INDENT
    stanza: list[str] = []
    for line in section[heads[0] + 1 :]:
        if not line.strip():
            continue
        if _indent(line) <= level:
            break
        stanza.append(line)
    else:
        return None  # the text ends inside the stanza
    if min((_indent(line) for line in stanza), default=0) != _FIELD_INDENT:
        return None
    return stanza


def _shared_numbers(lines: list[str]) -> set[int] | None:
    """The numbers the ``Shared users:`` section gives its shared users (none
    when there is no such section), None when it is there more than once."""
    if lines.count(_SHARED_USERS) == 0:
        return set()
    section = _section(lines, _SHARED_USERS)
    if section is None:
        return None
    return {
        int(claim)
        for line in section
        if not _SHARED_HEADER.match(line)
        for claim in _CLAIM.findall(line)
    }


def _number(stanza: list[str]) -> int | None:
    """The one app id the stanza claims, below 100000; None when no field line
    is ``userId=N`` or ``appId=N`` (nothing else on it), a line keyed
    ``userId`` or ``appId`` at any indent is of another form, or two such
    lines give different numbers. Text inside a line of another key (such as
    ``versionName=``, which the app sets) claims nothing."""
    keyed = [line for line in stanza if _key(line.strip()) in _NUMBER_KEYS]
    matches = [_NUMBER.match(line.strip()) for line in keyed]
    own = [line for line in keyed if _indent(line) == _FIELD_INDENT]
    claimed = {int(match[1]) for match in matches if match is not None}
    if not own or None in matches or len(claimed) != 1:
        return None
    uid = next(iter(claimed))
    return uid if uid < _PER_USER else None


def parse_package_uid(text: str, package: str) -> PackageUid | None:
    """Read `package`'s app id from the text of ``dumpsys package``.

    Returns None unless the ``Packages:`` section holds exactly one stanza of
    the package, headed at the stanza indent (two spaces) and ended by the
    next stanza or section (not cut off), with one number (``userId=`` or
    ``appId=``; two different ones are None) below 100000 (an app id, not a
    uid of another user), a ``User 0:`` line that says ``installed=true``
    and, when there is a line keyed ``sharedUser``, one of the form
    ``sharedUser=SharedUserSetting{<hash> <name>/<app id>}`` at the field
    indent (an empty, other or misplaced ``sharedUser=`` would hide that the
    uid is shared). A ``SharedUser [...]`` entry of the ``Shared users:``
    section with the package's number, while the stanza has no
    ``sharedUser=`` line, is a contradiction: None as well.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    stanza = _stanza(lines, package)
    uid = None if stanza is None else _number(stanza)
    if stanza is None or uid is None:
        return None
    fields = [line.strip() for line in stanza if _indent(line) == _FIELD_INDENT]
    users = [line for line in fields if _USER0.match(line)]
    # Every line keyed sharedUser claims a shared user; only one at the field
    # indent of the SharedUserSetting form is one.
    claims = [line for line in stanza if _key(line.strip()) == _SHARED_KEY]
    shared = [_SHARED.match(line) for line in fields if _key(line) == _SHARED_KEY]
    numbers = _shared_numbers(lines)
    if (
        len(users) != 1
        or not _INSTALLED.match(users[0])
        or len(claims) > 1
        or len(shared) != len(claims)
        or any(match is None or int(match[1]) != uid for match in shared)
        or numbers is None
        or (uid in numbers and not shared)
    ):
        return None
    return PackageUid(uid, bool(shared))
