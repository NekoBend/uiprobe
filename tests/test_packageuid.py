"""Tests for reading a package's uid from ``dumpsys package <name>``.

The stanza lines are copied from emulators (API 24 and 36: the lines that
name a package, its uid and its shared user, in their order); the lines
between them are left out.
"""

from __future__ import annotations

import pytest

from uiprobe.packageuid import PackageUid, parse_package_uid

SETTINGS36 = """\
Permissions:
  Permission [android.permission.WRITE_SETTINGS_HOMEPAGE_DATA] (1c2d3e):
    uid=1000 gids=[] type=0 prot=signature|privileged
Packages:
  Package [com.android.settings] (459bdd):
    appId=1000
    sharedUser=SharedUserSetting{e0f06a3 android.uid.system/1000}
Queries:
Shared users:
  SharedUser [android.uid.system] (e0f06a3):
    appId=1000
"""
SETTINGS24 = """\
Packages:
  Package [com.android.settings] (4c104eb):
    userId=1000
    sharedUser=SharedUserSetting{8b80f4d android.uid.system/1000}
Shared users:
  SharedUser [android.uid.system] (8b80f4d):
    userId=1000
"""
CALCULATOR24 = """\
Packages:
  Package [com.android.calculator2] (634ebb0):
    userId=10044
Dexopt state:
"""


@pytest.mark.parametrize(
    ("text", "package", "expected"),
    [
        (SETTINGS36, "com.android.settings", PackageUid(1000, shared=True)),
        (SETTINGS24, "com.android.settings", PackageUid(1000, shared=True)),
        (CALCULATOR24, "com.android.calculator2", PackageUid(10044, shared=False)),
    ],
)
def test_the_package_stanza_gives_the_uid(text, package, expected):
    assert parse_package_uid(text, package) == expected


def test_another_package_stanza_before_it_is_not_read():
    other = (
        "Packages:\n"
        "  Package [com.android.calculator2x] (1a):\n"
        "    userId=10099\n"
        "  Package [com.android.calculator2] (634ebb0):\n"
        "    userId=10044\n"
    )
    assert parse_package_uid(other, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )
    assert parse_package_uid(other, "com.android.calc") is None
    # the dots are not wildcards
    assert parse_package_uid(CALCULATOR24, "com.android.calculator") is None
    assert parse_package_uid(CALCULATOR24, "com-android-calculator2") is None


def test_the_shared_users_section_is_not_read():
    # Without the stanza's own line, the number under SharedUser is not used.
    text = SETTINGS36.replace("    appId=1000\n    sharedUser", "    sharedUser", 1)
    assert parse_package_uid(text, "com.android.settings") is None


def test_missing_or_conflicting_numbers_are_none():
    assert parse_package_uid("", "com.android.settings") is None
    assert parse_package_uid("Dexopt state:\n", "com.android.deskclock") is None
    both = SETTINGS24.replace("    userId=1000\n", "    userId=1000\n    appId=1001\n")
    assert parse_package_uid(both, "com.android.settings") is None
    hidden = (
        CALCULATOR24
        + "Hidden system packages:\n"
        + "  Package [com.android.calculator2] (12ab):\n    userId=10045\n"
    )
    assert parse_package_uid(hidden, "com.android.calculator2") is None
    same = hidden.replace("10045", "10044")
    assert parse_package_uid(same, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )


def test_only_the_stanza_level_is_read():
    deeper = CALCULATOR24.replace(
        "    userId=10044\n", "    userId=10044\n    User 0:\n      userId=5\n"
    )
    assert parse_package_uid(deeper, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )
    crlf = CALCULATOR24.replace("\n", "\r\n")
    assert parse_package_uid(crlf, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )
