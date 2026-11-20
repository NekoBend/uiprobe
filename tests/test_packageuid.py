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


def test_a_number_claimed_deeper_must_agree():
    # Another number anywhere in the stanza is a contradiction; the same one
    # deeper down is not.
    deeper = CALCULATOR24.replace(
        "    userId=10044\n", "    userId=10044\n    gids:\n      userId=5\n"
    )
    assert parse_package_uid(deeper, "com.android.calculator2") is None
    same = deeper.replace("userId=5", "userId=10044")
    assert parse_package_uid(same, "com.android.calculator2") == PackageUid(
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


# --- round 4 ---


@pytest.mark.parametrize(
    "line",
    [
        "    appId=10044 userId=10045",
        "    userId=10044 appId=10044",
        "    userId=10044x",
    ],
)
def test_a_number_line_of_another_form_is_none(line):
    text = CALCULATOR24.replace("    userId=10044", line)
    assert parse_package_uid(text, "com.android.calculator2") is None


def test_two_number_lines_that_disagree_are_none():
    text = CALCULATOR24.replace(
        "    userId=10044\n", "    userId=10044\n    appId=10045\n"
    )
    assert parse_package_uid(text, "com.android.calculator2") is None


@pytest.mark.parametrize("digits", ["1" * 4301, "0" * 4297 + "1000"])
def test_a_shared_user_number_too_long_to_read_is_none(digits):
    # More digits than `int` converts by default (4300); with leading zeros
    # the number could be the package's own.
    head, _, shared = SETTINGS24.partition("Shared users:")
    assert shared.count("userId=1000") == 1
    text = head + "Shared users:" + shared.replace("userId=1000", f"userId={digits}")
    assert parse_package_uid(text, "com.android.settings") is None


def test_a_shared_user_entry_with_the_number_needs_the_stanza_line():
    # API 24 SystemUI: the Shared users: section has SharedUser
    # [android.uid.systemui] with userId=10027.
    systemui24 = (
        "Packages:\n"
        "  Package [com.android.systemui] (1c54a76):\n"
        "    userId=10027\n"
        "    sharedUser=SharedUserSetting{b0b8303 android.uid.systemui/10027}\n"
        "    pkg=Package{1c50e3b com.android.systemui}\n"
        "    User 0: ceDataInode=114806 installed=true hidden=false"
        " suspended=false stopped=false notLaunched=false enabled=0\n"
        "Shared users:\n"
        "  SharedUser [android.uid.systemui] (b0b8303):\n"
        "    userId=10027\n"
    )
    package = "com.android.systemui"
    assert parse_package_uid(systemui24, package) == PackageUid(10027, shared=True)
    marker = "    sharedUser=SharedUserSetting{b0b8303 android.uid.systemui/10027}\n"
    missing = systemui24.replace(marker, "")
    assert parse_package_uid(missing, package) is None
    misplaced = systemui24.replace(marker, "      " + marker.lstrip())
    assert parse_package_uid(misplaced, package) is None
    # An entry with another number does not matter.
    other = missing[: missing.index("Shared users:")] + (
        "Shared users:\n  SharedUser [android.uid.other] (1):\n    userId=10099\n"
    )
    assert parse_package_uid(other, package) == PackageUid(10027, shared=False)


# --- round 5: text the app sets inside the stanza ---


@pytest.mark.parametrize(
    "line",
    [
        "    versionName=Release (appId=7)",
        "    versionName=x userId=10044",
        "    versionName=x userId=10099",
        "    versionName=x appId=10099 userId=1000",
        "    versionCode=7 minSdk=24 targetSdk=36",
    ],
)
def test_a_uid_written_inside_another_field_is_no_claim(line):
    text = CALCULATOR24.replace("    pkg=", f"{line}\n    pkg=")
    assert parse_package_uid(text, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )


def test_a_uid_written_inside_another_field_cannot_stand_in_for_the_real_one():
    # Without the real uid line, the same text gives no uid at all.
    text = CALCULATOR24.replace(
        "    userId=10044\n", "    versionName=x userId=10044\n"
    )
    assert parse_package_uid(text, "com.android.calculator2") is None


@pytest.mark.parametrize(
    "line",
    [
        "      sharedUser.demo.permission.SYNC: granted=true",
        "    sharedUser.demo.permission.SYNC: granted=true",
        "    requestedPermissions: sharedUser.demo.permission.SYNC",
        "    versionName=x sharedUser=SharedUserSetting{1 android.uid.system/1000}",
    ],
)
def test_text_holding_shared_user_is_no_marker(line):
    text = CALCULATOR24.replace("    pkg=", f"{line}\n    pkg=")
    assert parse_package_uid(text, "com.android.calculator2") == PackageUid(
        10044, shared=False
    )
    # Nor does it stand in for, or hide, the real marker of a shared uid.
    shared = SYSTEMUI36.replace("    pkg=", f"{line}\n    pkg=")
    assert parse_package_uid(shared, "com.android.systemui") == PackageUid(
        10182, shared=True
    )


def test_text_holding_shared_user_does_not_hide_a_shared_users_entry():
    # The marker is gone and the permission name holds the word: the entry in
    # Shared users: with the number still makes it None.
    marker = "    sharedUser=SharedUserSetting{d5f3dd5 android.uid.systemui/10182}\n"
    text = SYSTEMUI36.replace(
        marker, "    sharedUser.demo.permission.SYNC: granted=true\n"
    )
    assert parse_package_uid(text, "com.android.systemui") is None


@pytest.mark.parametrize(
    "line",
    [
        "      sharedUser=",  # a shared-user line at another indent
        "      sharedUser=SharedUserSetting{1 android.uid.system/10044}",
        "      appId=7",  # an exact uid line deeper down, another number
        "      userId=10044 x",  # a uid line deeper down of another form
        "    appId=10044 (x)",  # a uid line at the field indent, another form
        "    appId=10099",  # a second uid line (a line break in a text field)
    ],
)
def test_a_line_keyed_as_a_field_elsewhere_still_refuses(line):
    text = CALCULATOR24.replace("    pkg=", f"{line}\n    pkg=")
    assert parse_package_uid(text, "com.android.calculator2") is None
