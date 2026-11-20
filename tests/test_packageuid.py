"""Tests for reading a package's uid from ``dumpsys package <name>``.

The stanza lines are copied from emulators (API 24, 31 and 36): the header,
the uid, the shared user, ``pkg=`` and the ``User 0:`` line (with, on API 24
and 36, the first lines under it), in their order; the lines between them are
left out. For the API 24 calculator only the
header and ``userId=`` were captured; its ``pkg=`` and ``User 0:`` lines are
written in the API 24 form. The section lines around them
(``Permissions:``, ``Packages:``, ``Queries:``, ``Shared users:``,
``Dexopt state:``) are the ones those dumps print there.
"""

from __future__ import annotations

import pytest

from uiprobe.packageuid import PackageUid, parse_package_uid

SETTINGS24 = """\
Key Set Manager:
Packages:
  Package [com.android.settings] (a137442):
    userId=1000
    sharedUser=SharedUserSetting{1d5f23c android.uid.system/1000}
    pkg=Package{e4e9bc5 com.android.settings}
    User 0: ceDataInode=114786 installed=true hidden=false suspended=false \
stopped=false notLaunched=false enabled=0
      disabledComponents:
      enabledComponents:

Shared users:
  SharedUser [android.uid.system] (1d5f23c):
    userId=1000
"""
SYSTEMUI31 = """\
Packages:
  Package [com.android.systemui] (961e512):
    userId=10134
    sharedUser=SharedUserSetting{410df26 android.uid.systemui/10134}
    pkg=Package{c90c767 com.android.systemui}
    User 0: ceDataInode=123368 installed=true hidden=false suspended=false \
distractionFlags=0 stopped=false notLaunched=false enabled=0 instant=false \
virtual=false

Queries:
"""
SYSTEMUI36 = """\
Permissions:
Packages:
  Package [com.android.systemui] (959c157):
    appId=10182
    sharedUser=SharedUserSetting{d5f3dd5 android.uid.systemui/10182}
    pkg=Package{e2269ea com.android.systemui}
    User 0: ceDataInode=557209 deDataInode=393294 installed=true hidden=false \
suspended=false distractionFlags=0 stopped=false notLaunched=false enabled=0 \
instant=false virtual=false quarantined=false
      installReason=0
      dataDir=/data/user_de/0/com.android.systemui

Queries:
Shared users:
  SharedUser [android.uid.systemui] (d5f3dd5):
    appId=10182
"""
CALCULATOR24 = """\
Packages:
  Package [com.android.calculator2] (634ebb0):
    userId=10044
    pkg=Package{9f8e7d6 com.android.calculator2}
    User 0: ceDataInode=114790 installed=true hidden=false suspended=false \
stopped=false notLaunched=false enabled=0
Dexopt state:
"""


@pytest.mark.parametrize(
    ("text", "package", "expected"),
    [
        (SETTINGS24, "com.android.settings", PackageUid(1000, shared=True)),
        (SYSTEMUI31, "com.android.systemui", PackageUid(10134, shared=True)),
        (SYSTEMUI36, "com.android.systemui", PackageUid(10182, shared=True)),
        (CALCULATOR24, "com.android.calculator2", PackageUid(10044, shared=False)),
    ],
)
def test_the_package_stanza_gives_the_uid(text, package, expected):
    assert parse_package_uid(text, package) == expected


def test_another_package_stanza_before_it_is_not_read():
    other = CALCULATOR24.replace(
        "Packages:\n",
        "Packages:\n  Package [com.android.calculator2x] (1a):\n    userId=10099\n",
    )
    assert parse_package_uid(other, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )
    assert parse_package_uid(other, "com.android.calc") is None
    # the dots are not wildcards
    assert parse_package_uid(CALCULATOR24, "com.android.calculator") is None
    assert parse_package_uid(CALCULATOR24, "com-android-calculator2") is None


def test_only_a_stanza_in_the_packages_section_counts():
    # Without the stanza's own line, the number under SharedUser is not used.
    text = SYSTEMUI36.replace("    appId=10182\n    sharedUser", "    sharedUser", 1)
    assert parse_package_uid(text, "com.android.systemui") is None
    # A stanza of the same name in another section is not read.
    moved = CALCULATOR24.replace("Packages:", "Hidden system packages:")
    assert parse_package_uid(moved, "com.android.calculator2") is None
    # Two Packages: sections, or two stanzas of the package, are not told apart.
    twice = CALCULATOR24 + CALCULATOR24
    assert parse_package_uid(twice, "com.android.calculator2") is None
    start, end = CALCULATOR24.index("  Package"), CALCULATOR24.index("Dexopt")
    stanza = CALCULATOR24[start:end]
    doubled = CALCULATOR24.replace(stanza, stanza + stanza)
    assert parse_package_uid(doubled, "com.android.calculator2") is None


def test_the_package_must_be_installed_for_user_0():
    absent = CALCULATOR24.replace("    User 0: ", "    User 10: ")
    assert parse_package_uid(absent, "com.android.calculator2") is None
    removed = CALCULATOR24.replace("installed=true", "installed=false")
    assert parse_package_uid(removed, "com.android.calculator2") is None
    deeper = CALCULATOR24.replace("    User 0: ", "      User 0: ")
    assert parse_package_uid(deeper, "com.android.calculator2") is None


def test_a_dump_cut_off_inside_the_stanza_is_none():
    # Cut right after appId= (before sharedUser=): the uid would look unshared.
    cut = SYSTEMUI36[: SYSTEMUI36.index("    sharedUser=")]
    assert parse_package_uid(cut, "com.android.systemui") is None
    # Cut at the end of the stanza, before the next section.
    whole = SYSTEMUI36[: SYSTEMUI36.index("Queries:")]
    assert parse_package_uid(whole, "com.android.systemui") is None


def test_missing_or_conflicting_numbers_are_none():
    assert parse_package_uid("", "com.android.settings") is None
    assert parse_package_uid("Dexopt state:\n", "com.android.deskclock") is None
    both = SETTINGS24.replace("    userId=1000\n", "    userId=1000\n    appId=1001\n")
    assert parse_package_uid(both, "com.android.settings") is None
    none = CALCULATOR24.replace("    userId=10044\n", "")
    assert parse_package_uid(none, "com.android.calculator2") is None


def test_only_the_stanza_level_is_read():
    deeper = CALCULATOR24.replace(
        "    userId=10044\n", "    userId=10044\n    gids:\n      userId=5\n"
    )
    assert parse_package_uid(deeper, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )
    crlf = CALCULATOR24.replace("\n", "\r\n")
    assert parse_package_uid(crlf, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )


# --- round 3 ---


@pytest.mark.parametrize(
    "line",
    [
        "    sharedUser=",
        "    sharedUser=android.uid.systemui",
        "    sharedUser=SharedUserSetting{d5f3dd5 android.uid.systemui/10183}",
        "    sharedUser=SharedUserSetting{d5f3dd5 android.uid.systemui/10182} x",
    ],
)
def test_a_malformed_shared_user_line_is_none(line):
    good = "    sharedUser=SharedUserSetting{d5f3dd5 android.uid.systemui/10182}"
    text = SYSTEMUI36.replace(good, line)
    assert text != SYSTEMUI36
    assert parse_package_uid(text, "com.android.systemui") is None


def test_a_package_header_inside_another_stanza_is_not_the_package():
    nested = (
        "Packages:\n"
        "  Package [example.other] (a):\n"
        "    appId=10044\n"
        "    Package [example.target] (b):\n"
        "      appId=10044\n"
        "      User 0: installed=true\n"
        "Queries:\n"
    )
    assert parse_package_uid(nested, "example.target") is None
    # A target stanza at the right indent, plus a nested header naming it.
    both = CALCULATOR24.replace(
        "    User 0:", "    Package [com.android.calculator2] (b):\n    User 0:"
    )
    assert parse_package_uid(both, "com.android.calculator2") is None


def test_a_number_of_another_user_is_none():
    other = CALCULATOR24.replace("userId=10044", "userId=1010044")
    assert parse_package_uid(other, "com.android.calculator2") is None
    edge = CALCULATOR24.replace("userId=10044", "userId=100000")
    assert parse_package_uid(edge, "com.android.calculator2") is None
    below = CALCULATOR24.replace("userId=10044", "userId=99999")
    assert parse_package_uid(below, "com.android.calculator2") == PackageUid(
        99999, shared=False
    )
