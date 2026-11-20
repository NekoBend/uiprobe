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
installed=true ...`` line (as on every emulator checked).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = ["PackageUid", "parse_package_uid"]

_NUMBER = re.compile(r"^(?:userId|appId)=(\d{1,10})(?:\s.*)?$")
_SHARED = re.compile(r"^sharedUser=\S")
_USER0 = re.compile(r"^User 0:(?: |$)")
_INSTALLED = re.compile(r"^User 0: (?:.* )?installed=true(?: |$)")
_SECTION: Final = "Packages:"


@dataclass(frozen=True, slots=True)
class PackageUid:
    """The app id of a package and whether other packages share it."""

    uid: int
    shared: bool


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _section(lines: list[str]) -> list[str] | None:
    """The lines of the ``Packages:`` section, with the line that ends it (a
    line at indent 0) when there is one; None unless exactly one section."""
    heads = [i for i, line in enumerate(lines) if line == _SECTION]
    if len(heads) != 1:
        return None
    found: list[str] = []
    for line in lines[heads[0] + 1 :]:
        found.append(line)
        if line.strip() and _indent(line) == 0:
            break
    return found


def _stanza(lines: list[str], package: str) -> list[str] | None:
    """The field lines of `package`'s one stanza in the ``Packages:`` section
    (those at the stanza's own field indent), None when there is not exactly
    one or it is not ended by a line at its header's indent or less (a dump
    cut off inside the stanza)."""
    section = _section(lines)
    if section is None:
        return None
    header = re.compile(rf"^ +Package \[{re.escape(package)}\] \([0-9a-f]+\):$")
    heads = [i for i, line in enumerate(section) if header.match(line)]
    if len(heads) != 1:
        return None
    level = _indent(section[heads[0]])
    stanza: list[str] = []
    for line in section[heads[0] + 1 :]:
        if not line.strip():
            continue
        if _indent(line) <= level:
            break
        stanza.append(line)
    else:
        return None  # the text ends inside the stanza
    fields = min((_indent(line) for line in stanza), default=0)
    return [line.strip() for line in stanza if _indent(line) == fields]


def parse_package_uid(text: str, package: str) -> PackageUid | None:
    """Read `package`'s app id from the text of ``dumpsys package``.

    Returns None unless the ``Packages:`` section holds exactly one stanza of
    the package, ended by the next stanza or section (not cut off), with one
    number (``userId=`` or ``appId=``; two different ones are None) and a
    ``User 0:`` line that says ``installed=true``.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    stanza = _stanza(lines, package)
    if stanza is None:
        return None
    numbers = {int(m[1]) for m in map(_NUMBER.match, stanza) if m is not None}
    users = [line for line in stanza if _USER0.match(line)]
    if len(numbers) != 1 or len(users) != 1 or not _INSTALLED.match(users[0]):
        return None
    shared = any(_SHARED.match(line) for line in stanza)
    return PackageUid(next(iter(numbers)), shared)
