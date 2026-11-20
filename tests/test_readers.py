"""Tests of the read-only readers: foreground, keyboard_shown, is_emulator."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import Fail
from tests.fake_device import El, FakeAndroid, Screen, _hex
from uiprobe.device import Device, DeviceIdentity, FocusedWindow
from uiprobe.errors import AdbError, AdbServerUnavailableError

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
OTHER = "com.other.app"
APIS = [24, 26, 28, 29, 30, 31, 32, 36]

SCREENS = (
    Screen("main", f"{SHOP}/.Main", (El((0, 0, 320, 100), text="Hi"),)),
    Screen(
        "editing", f"{SHOP}/.Edit", (El((0, 0, 320, 100), text="Name"),), keyboard=True
    ),
)
OTHER_APP = f"  mFocusedApp=ActivityRecord{{1 u0 {OTHER}/.Main t3}}"
WINDOW = re.compile(r"^dumpsys window\Z")
INPUT_METHOD = re.compile(r"^dumpsys input_method\Z")


def make(
    fake_adb: FakeAdbServer,
    *,
    api: int = 36,
    start: str = "main",
    focused_app_line: bool = True,
) -> tuple[FakeAndroid, Device]:
    """A strict FakeAndroid (the default mode) and a Device on it."""
    android = FakeAndroid(
        fake_adb,
        SERIAL,
        SCREENS,
        start=start,
        api=api,
        focused_app_line=focused_app_line,
    )
    return android, Device(adb_server=fake_adb.address)


def assert_no_input(fake_adb: FakeAdbServer, android: FakeAndroid) -> None:
    assert android.inputs == []
    assert not [r for r in fake_adb.requests if re.match(r"(shell|exec):input\b", r)]


def input_method(*lines: str) -> str:
    return "\n".join(lines) + "\n"


# --- foreground ---


@pytest.mark.parametrize("focused_app_line", [True, False])
@pytest.mark.parametrize("api", APIS)
def test_foreground_returns_the_package_in_front(fake_adb, api, focused_app_line):
    android, device = make(fake_adb, api=api, focused_app_line=focused_app_line)
    assert device.foreground() == SHOP
    assert_no_input(fake_adb, android)


def test_foreground_falls_back_to_the_activity_source_when_window_is_absent(fake_adb):
    android, device = make(fake_adb, focused_app_line=False)
    assert device.foreground() == SHOP
    assert any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)
    assert_no_input(fake_adb, android)


def test_foreground_does_not_fall_back_when_window_source_is_present(fake_adb):
    android, device = make(fake_adb)
    assert device.foreground() == SHOP
    assert not any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)
    assert_no_input(fake_adb, android)


def test_foreground_names_another_package_without_filtering(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, (OTHER_APP + "\n").encode())
    assert device.foreground() == OTHER
    assert_no_input(fake_adb, android)


@pytest.mark.parametrize("api", [24, 36])
def test_foreground_forged_extra_line_gives_none(fake_adb, api):
    android, device = make(fake_adb, api=api)
    android.extra_dumpsys["window"] = OTHER_APP + "\n"
    assert device.foreground() is None
    assert not any(r.endswith("dumpsys activity activities") for r in fake_adb.requests)
    assert_no_input(fake_adb, android)


def test_foreground_malformed_lone_line_counts_as_absent(fake_adb):
    android, device = make(fake_adb, focused_app_line=False)
    android.extra_dumpsys["window"] = "  mFocusedApp=ActivityRecord{1 u0 bad t3}\n"
    assert device.foreground() == SHOP  # from the activity source
    assert_no_input(fake_adb, android)


def test_foreground_malformed_line_beside_another_gives_none(fake_adb):
    android, device = make(fake_adb)
    android.extra_dumpsys["window"] = "  mFocusedApp=ActivityRecord{1 u0 bad t3}\n"
    assert device.foreground() is None
    assert_no_input(fake_adb, android)


def test_foreground_none_when_nothing_names_it(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, b"nothing\n")
    android.override(re.compile(r"^dumpsys activity activities\Z"), b"nothing\n")
    assert device.foreground() is None
    assert_no_input(fake_adb, android)


def test_foreground_error_propagates(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, Fail("closed"))
    with pytest.raises(AdbError):
        device.foreground()
    assert_no_input(fake_adb, android)


def test_foreground_error_in_the_fallback_propagates(fake_adb):
    android, device = make(fake_adb, focused_app_line=False)
    android.override(re.compile(r"^dumpsys activity activities\Z"), Fail("closed"))
    with pytest.raises(AdbError):
        device.foreground()
    assert_no_input(fake_adb, android)


def test_foreground_unreachable_server_propagates(fake_adb):
    android, device = make(fake_adb)
    fake_adb.stop()
    with pytest.raises(AdbServerUnavailableError):
        device.foreground()
    assert_no_input(fake_adb, android)


# --- dump keeps its package filter ---


def test_dump_activity_still_requires_the_dumped_package(fake_adb):
    android, device = make(fake_adb)
    assert device.dump().activity == ".Main"
    android.override(WINDOW, (OTHER_APP + "\n").encode())
    screen = device.dump()
    assert screen.package == SHOP
    assert screen.activity is None
    assert device.foreground() == OTHER
    assert_no_input(fake_adb, android)


def test_dump_activity_error_is_still_unknown_but_foreground_raises(fake_adb):
    android, device = make(fake_adb)
    android.override(WINDOW, Fail("closed"))
    assert device.dump().activity is None
    with pytest.raises(AdbError):
        device.foreground()
    assert_no_input(fake_adb, android)


# --- keyboard_shown ---


@pytest.mark.parametrize("api", APIS)
def test_keyboard_shown_true_and_false_on_every_api(fake_adb, api):
    android, device = make(fake_adb, api=api, start="editing")
    assert device.keyboard_shown() is True
    android.show("main")
    assert device.keyboard_shown() is False
    assert_no_input(fake_adb, android)


OLD_SHOWN = (
    "  mShowRequested=true mShowExplicitlyRequested=true mShowForced=false"
    " mInputShown=true"
)
OLD_HIDDEN = (
    "  mShowRequested=false mShowExplicitlyRequested=false mShowForced=false"
    " mInputShown=false"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (OLD_SHOWN, True),
        (OLD_HIDDEN, False),
        ("      mInputShown=true", True),
        ("      mInputShown=false", False),
        ("    mInputShown=true", True),  # API 33-35 indentation is not captured
        ("  mIsInputViewShown=true mStatusIcon=0\n" + OLD_HIDDEN, False),
        (OLD_SHOWN + "\n" + OLD_SHOWN, True),  # the same value twice
    ],
)
def test_keyboard_shown_captured_lines(fake_adb, text, expected):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, (text + "\n").encode())
    assert device.keyboard_shown() is expected
    assert_no_input(fake_adb, android)


def test_keyboard_shown_ignores_is_input_view_shown(fake_adb):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, b"  mIsInputViewShown=true mStatusIcon=0\n")
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nothing here",
        OLD_SHOWN + "\n" + OLD_HIDDEN,  # disagreeing, old shape
        OLD_HIDDEN + "\n      mInputShown=true",  # disagreeing across shapes
        "      mInputShown=false\n      mInputShown=true",
        "    hintText=x\n  mShowRequested=true mShowExplicitlyRequested=true"
        " mShowForced=false mInputShown=true\n" + "      mInputShown=false",
        "      mInputShown=true\n    hintText=a mInputShown=true",  # inside hintText
        "    hintText=mInputShown=true",
        "      mInputShown=maybe",
        "      mInputShown=true x",
        "      mInputShown=True",
        OLD_SHOWN + " extra",
        OLD_SHOWN.replace("  mShow", "mShow", 1),  # wrong indentation
        " " * 5000 + "mInputShown=true",  # well-formed but over-long
        OLD_SHOWN.replace("  mShow", " " * 5000 + "mShow", 1),
        "      mInputShown=true " + "x" * 5000,  # over-long
        "      mInputShown=" + "true" * 2000,
    ],
)
def test_keyboard_shown_unknown(fake_adb, text):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, (text + "\n").encode())
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


FORGED = {
    "old": "  mShowRequested=false mShowExplicitlyRequested=false mShowForced=false"
    " mInputShown={}",
    "own": "      mInputShown={}",
}


@pytest.mark.parametrize("shape", ["old", "own"])
@pytest.mark.parametrize("api", [24, 36])
@pytest.mark.parametrize("keyboard", [True, False])
def test_keyboard_shown_forged_disagreeing_line_gives_none(
    fake_adb, api, keyboard, shape
):
    android, device = make(fake_adb, api=api, start="editing" if keyboard else "main")
    assert device.keyboard_shown() is keyboard
    forged = FORGED[shape].format("false" if keyboard else "true")
    android.extra_dumpsys["input_method"] = forged + "\n"
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


def test_keyboard_shown_forged_line_inside_hint_text_gives_none(fake_adb):
    android, device = make(fake_adb, start="editing")
    android.extra_dumpsys["input_method"] = "      hintText=a\n b mInputShown=false\n"
    assert device.keyboard_shown() is None
    assert_no_input(fake_adb, android)


def test_keyboard_shown_handles_crlf(fake_adb):
    android, device = make(fake_adb)
    for text, expected in ((OLD_SHOWN, True), ("      mInputShown=false", False)):
        android.override(INPUT_METHOD, (text + "\r\n  other\r\n").encode())
        assert device.keyboard_shown() is expected
    assert_no_input(fake_adb, android)


def test_keyboard_shown_error_propagates(fake_adb):
    android, device = make(fake_adb)
    android.override(INPUT_METHOD, Fail("closed"))
    with pytest.raises(AdbError):
        device.keyboard_shown()
    assert_no_input(fake_adb, android)


def test_keyboard_shown_unreachable_server_propagates(fake_adb):
    android, device = make(fake_adb)
    fake_adb.stop()
    with pytest.raises(AdbServerUnavailableError):
        device.keyboard_shown()
    assert_no_input(fake_adb, android)


# --- is_emulator ---

GETPROP = re.compile(r"^getprop ro\.kernel\.qemu\Z")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"1\n", True),
        (b"1\r\n", True),
        (b"  1 \r\n", True),
        (b"1", True),
        (b"", False),
        (b"\n", False),
        (b"0\n", False),
        (b"true\n", False),
        (b"11\n", False),
        (b"1 1\n", False),
        (b"01\n", False),
    ],
)
def test_is_emulator_strict_value(fake_adb, output, expected):
    android, device = make(fake_adb)
    android.override(GETPROP, output)
    assert device.is_emulator() is expected
    assert_no_input(fake_adb, android)


def test_is_emulator_default_fake_is_an_emulator(fake_adb):
    android, device = make(fake_adb)
    assert device.is_emulator() is True
    assert_no_input(fake_adb, android)


def test_is_emulator_error_propagates(fake_adb):
    android, device = make(fake_adb)
    android.override(GETPROP, Fail("closed"))
    with pytest.raises(AdbError):
        device.is_emulator()
    assert_no_input(fake_adb, android)


def test_is_emulator_unreachable_server_propagates(fake_adb):
    android, device = make(fake_adb)
    fake_adb.stop()
    with pytest.raises(AdbServerUnavailableError):
        device.is_emulator()
    assert_no_input(fake_adb, android)


# --- focused_window ---

# Captured on emulators (dumpsys window windows): the focused window's block
# head and, two lines below, the owner line the system writes.
SETTINGS = "com.android.settings/com.android.settings.Settings"
CAPTURED_BLOCKS = {
    24: (
        "5eed814",
        SETTINGS,
        "    mDisplayId=0 stackId=1 mSession=Session{ec14794 1855:1000} mClient=x",
        "    mOwnerUid=1000 mShowToOwnerOnly=true package=com.android.settings"
        " appop=NONE",
    ),
    30: (
        "ff01d0e",
        "NotificationShade",
        "    mDisplayId=0 rootTaskId=1 mSession=Session{c0af70d 631:u0a10157} mC=x",
        "    mOwnerUid=10157 showForAllUsers=true package=com.android.systemui"
        " appop=NONE",
    ),
    36: (
        "498ad4f",
        SETTINGS,
        "    mDisplayId=0 taskId=57 mSession=Session{9196a2d 2532:1000} mClient=x",
        "    mOwnerUid=1000 showForAllUsers=false package=com.android.settings"
        " appop=NONE",
    ),
}


def focus(window: str, title: str) -> str:
    return f"  mCurrentFocus=Window{{{window} u0 {title}}}\n"


def block(
    window: str, title: str, second: str, owner_line: str, number: int = 4
) -> str:
    return (
        f"  Window #{number} Window{{{window} u0 {title}}}:\n{second}\n{owner_line}\n"
    )


def owner_line(package: str) -> str:
    return f"    mOwnerUid=10150 showForAllUsers=false package={package} appop=NONE"


SECOND = "    mDisplayId=0 taskId=5 mSession=Session{1 2:3} mClient=x"
WINDOWS = re.compile(r"^dumpsys window windows\Z")


@pytest.mark.parametrize("api", APIS)
def test_focused_window_names_the_app_in_front(fake_adb, api):
    android, device = make(fake_adb, api=api)
    title = f"{SHOP}/{SHOP}.Main"
    expected = FocusedWindow(SHOP, title, _hex(title), 0, android.uid_of(SHOP))
    assert device.focused_window() == expected


@pytest.mark.parametrize("api", sorted(CAPTURED_BLOCKS))
def test_focused_window_reads_the_captured_owner_line(fake_adb, api):
    window, title, second, owner = CAPTURED_BLOCKS[api]
    android, device = make(fake_adb)
    android.override(WINDOW, focus(window, title).encode())
    android.override(WINDOWS, block(window, title, second, owner).encode())
    expected = (
        "com.android.systemui"
        if title == "NotificationShade"
        else "com.android.settings"
    )
    uid = int(owner.split("mOwnerUid=")[1].split(" ")[0])
    assert device.focused_window() == FocusedWindow(expected, title, window, 0, uid)
    assert_no_input(fake_adb, android)


def test_the_shade_belongs_to_the_system_ui(fake_adb):
    android, device = make(fake_adb)
    android.focus_window = ("NotificationShade", "com.android.systemui")
    assert device.focused_window() == FocusedWindow(
        "com.android.systemui",
        "NotificationShade",
        _hex("NotificationShade"),
        0,
        android.uid_of("com.android.systemui"),
    )


def test_a_title_shaped_like_the_target_does_not_make_it_the_owner(fake_adb):
    android, device = make(fake_adb)
    title = f"{SHOP}/{SHOP}.Main"
    android.override(WINDOW, focus("abc123", title).encode())
    android.override(
        WINDOWS, block("abc123", title, SECOND, owner_line(OTHER)).encode()
    )
    assert device.focused_window() == FocusedWindow(OTHER, title, "abc123", 0, 10150)


@pytest.mark.parametrize(
    ("window_text", "windows_text"),
    [
        ("  mCurrentFocus=null\n", block("abc", "x", SECOND, owner_line(SHOP))),
        ("", block("abc", "x", SECOND, owner_line(SHOP))),
        (
            focus("abc", "x") + focus("def", "y"),
            block("abc", "x", SECOND, owner_line(SHOP)),
        ),
        ("  mCurrentFocus=Window{zz u0 x}\n", ""),
        (focus("abc", "a" * 600), ""),
        (focus("abc", "x"), ""),
        (
            focus("abc", "x"),
            block("abc", "x", SECOND, owner_line(SHOP))
            + block("abc", "x", SECOND, owner_line(OTHER), number=9),
        ),
        (focus("abc", "x"), block("abc", "y", SECOND, owner_line(SHOP))),
        (focus("abc", "x"), block("abc", "x", SECOND, "    package=" + SHOP)),
        (focus("abc", "x"), block("abc", "x", owner_line(SHOP), SECOND)),
        (focus("abc", "x"), "  Window #4 Window{abc u0 x}:\n" + SECOND + "\n"),
    ],
    ids=[
        "null",
        "absent",
        "focus-lines-disagree",
        "bad-hash",
        "too-long",
        "no-block",
        "two-blocks",
        "title-differs",
        "owner-line-shape",
        "owner-line-not-two-below",
        "cut-off",
    ],
)
def test_focused_window_is_unknown_unless_everything_agrees(
    fake_adb, window_text, windows_text
):
    android, device = make(fake_adb)
    android.override(WINDOW, window_text.encode())
    android.override(WINDOWS, windows_text.encode())
    assert device.focused_window() is None
    assert_no_input(fake_adb, android)


def test_identity_reads_three_properties_and_the_boot_id_at_once(fake_adb):
    android, device = make(fake_adb)
    found = device.identity()
    assert found == DeviceIdentity(
        emulator=True,
        serialno=android.props["ro.serialno"],
        fingerprint=android.props["ro.build.fingerprint"],
        boot_id="3ea6a12b-8379-445f-852a-73a2c4c3ff17",
    )


@pytest.mark.parametrize(
    "boot_id",
    [
        None,  # the file cannot be read: the command prints an empty line
        "cat: /proc/sys/kernel/random/boot_id: Permission denied",
        "3EA6A12B-8379-445F-852A-73A2C4C3FF17",  # not the kernel's lowercase
        "3ea6a12b-8379-445f-852a-73a2c4c3ff1",
        "3ea6a12b8379445f852a73a2c4c3ff17",
    ],
)
def test_a_boot_id_that_is_not_a_uuid_is_unknown(fake_adb, boot_id):
    android, device = make(fake_adb)
    android.boot_id = boot_id
    found = device.identity()
    assert found is not None
    assert found.boot_id == ""
    assert found.fingerprint == android.props["ro.build.fingerprint"]


@pytest.mark.parametrize("answer", [b"", b"1\n", b"1\na\nb\n", b"1\na\nb\nc\nd\n"])
def test_identity_of_another_shape_is_unknown(fake_adb, answer):
    android, device = make(fake_adb)
    android.override(re.compile(r"^getprop ro\.kernel\.qemu;"), answer)
    assert device.identity() is None
