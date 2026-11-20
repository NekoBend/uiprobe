"""The uid of an installed package, read from ``dumpsys package <name>``.

The package's own stanza holds it::

    Packages:
      Package [com.android.settings] (459bdd):
        appId=1000
        sharedUser=SharedUserSetting{e0f06a3 android.uid.system/1000}

API 24 and 33 print ``userId=N``, API 34 to 36 ``appId=N`` (seen on
emulators; the other versions were not checked here, so both are read). The
number is the app id, which is the uid of the app's processes and windows for
Android user 0; for another user the uid is ``user * 100000 + app id``.
``sharedUser=`` is there when the package shares its uid with other packages
(``android:sharedUserId``, such as ``android.uid.system`` for Settings).
Only stanzas headed exactly ``Package [<name>]`` are read, each up to the next
line at its own indent or less; a later ``Shared users:`` section repeats the
number under ``SharedUser [...]`` and is not read, nor are other packages'
stanzas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["PackageUid", "parse_package_uid"]

_NUMBER = re.compile(r"^(?:userId|appId)=(\d{1,10})(?:\s.*)?$")
_SHARED = re.compile(r"^sharedUser=\S")


@dataclass(frozen=True, slots=True)
class PackageUid:
    """The app id of a package and whether other packages share it."""

    uid: int
    shared: bool


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_package_uid(text: str, package: str) -> PackageUid | None:
    """Read `package`'s app id from the text of ``dumpsys package``.

    Returns None when no stanza of the package holds a number, or when its
    stanzas hold different numbers (a stanza of the same name under
    ``Hidden system packages:`` must agree).
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    header = re.compile(rf"^ *Package \[{re.escape(package)}\] \([0-9a-f]+\):$")
    numbers: set[int] = set()
    shared = False
    for start, line in enumerate(lines):
        if not header.match(line):
            continue
        level = _indent(line)
        stanza: list[str] = []
        for inner in lines[start + 1 :]:
            if not inner.strip():
                continue
            if _indent(inner) <= level:
                break
            stanza.append(inner)
        fields = min((_indent(inner) for inner in stanza), default=0)
        for inner in stanza:
            if _indent(inner) != fields:
                continue
            number = _NUMBER.match(inner.strip())
            if number is not None:
                numbers.add(int(number[1]))
            shared = shared or _SHARED.match(inner.strip()) is not None
    if len(numbers) != 1:
        return None
    return PackageUid(next(iter(numbers)), shared)
