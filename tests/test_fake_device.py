"""Tests of `FakeAndroid`, through the real adb client and `Device`."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_device import (
    BLACK_PNG,
    LAUNCHER_CATEGORY,
    PNG,
    El,
    FakeAndroid,
    Screen,
    assert_input_kinds,
    assert_inputs_only_in,
    assert_no_long_press,
    assert_no_press_inside,
    assert_no_resend_after_drop,
    assert_no_text_input,
    assert_only_back_keys,
    permission_dialog,
)
from uiprobe.device import Device
from uiprobe.errors import AdbError, DumpError

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
TRICKY = 'Tom & <Jerry> "x"\nnext'

SCREENS = (
    Screen("launcher", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((0, 0, 320, 100), text=TRICKY, rid=f"{SHOP}:id/title"),
            El((10, 110, 150, 160), text="Settings", go="settings", long_go="menu"),
            El((10, 200, 150, 250), text="Delete account", go="launcher"),
        ),
    ),
    Screen(
        "settings",
        f"{SHOP}/.Settings",
        (
            El(
                (10, 10, 300, 60),
                text="Name",
                go="editing",
                cls="android.widget.EditText",
            ),
        ),
    ),
    Screen(
        "editing",
        f"{SHOP}/.Settings",
        (El((10, 10, 300, 60), text="Name"),),
        keyboard=True,
    ),
    Screen("menu", f"{SHOP}/.Menu", (El((0, 0, 100, 100), text="Menu"),)),
    Screen(
        "secret", f"{SHOP}/.Pin", (El((0, 0, 100, 100), text="PIN"),), protected=True
    ),
    permission_dialog(),
)


@pytest.fixture
def android(fake_adb: FakeAdbServer, request: pytest.FixtureRequest) -> FakeAndroid:
    """The device, strict by default; a test can pass options with `device_options`."""
    options: dict[str, Any] = {"start": "main", **getattr(request, "param", {})}
    return FakeAndroid(fake_adb, SERIAL, SCREENS, **options)


def device_options(**options: object) -> pytest.MarkDecorator:
    ident = ",".join(f"{k}={v}" for k, v in options.items())
    return pytest.mark.parametrize("android", [options], indirect=True, ids=[ident])


LAX = device_options(strict=False)


@pytest.fixture
def device(fake_adb: FakeAdbServer, android: FakeAndroid) -> Device:
    del android
    return Device(adb_server=fake_adb.address)


def names(android: FakeAndroid) -> list[str]:
    return [e.screen for e in android.inputs]


def test_dump_reflects_the_screen(device):
    screen = device.dump()
    assert (screen.package, screen.activity, screen.api_level) == (SHOP, ".Main", 36)
    assert screen.display == (320, 640)
    assert [e.text for e in screen.elements][1] == TRICKY
    assert [e.text for e in screen.elements][2:] == ["Settings", "Delete account"]


@pytest.mark.parametrize(("api", "count"), [(24, 17), (32, 17), (36, 19)])
def test_xml_attribute_count_by_api(fake_adb, api, count):
    phone = FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", api=api)
    node = re.findall(rb"<node ([^>]*?)/?>", phone.render_xml())[1]
    assert len(re.findall(rb'([a-z-]+)="', node)) == count


@pytest.mark.parametrize("focused_app_line", [True, False])
@pytest.mark.parametrize("api", [24, 29, 30, 31, 32, 36])
def test_foreground_activity_is_read_from_every_dumpsys_shape(
    fake_adb, api, focused_app_line
):
    FakeAndroid(
        fake_adb,
        SERIAL,
        SCREENS,
        start="main",
        api=api,
        focused_app_line=focused_app_line,
    )
    assert Device(adb_server=fake_adb.address).dump().activity == ".Main"


@LAX
def test_getprop_and_wm_size(fake_adb, android):
    phone = Device(adb_server=fake_adb.address)
    assert phone.is_emulator()
    assert phone._shell("getprop ro.build.version.sdk") == "36\n"
    assert phone._shell("wm size") == "Physical size: 320x640\n"
    assert phone._shell("getprop no.such.prop") == "\n"
    assert android.api == 36


def test_tap_hits_the_topmost_element_and_follows_the_transition(device, android):
    device._shell("input tap 20 120")
    assert android.screen.name == "settings"
    assert device.dump().activity == ".Settings"


def test_tap_outside_any_target_or_on_a_covering_element_stays(fake_adb):
    cover = El((0, 100, 320, 300), text="scrim")
    covered = Screen("main", f"{SHOP}/.Main", (*SCREENS[1].elements, cover))
    phone = FakeAndroid(
        fake_adb, SERIAL, [covered, SCREENS[0], *SCREENS[2:]], start="main"
    )
    Device(adb_server=fake_adb.address)._shell("input tap 20 120")
    Device(adb_server=fake_adb.address)._shell("input tap 300 500")
    assert phone.stack == ["main"]


@LAX
def test_long_press_is_a_swipe_in_place(device, android):
    device._shell("input swipe 20 120 25 125 800")
    assert android.screen.name == "main"
    device._shell("input swipe 20 120 20 120 800")
    assert android.screen.name == "menu"
    assert [e.kind for e in android.inputs] == ["swipe", "long_press"]


def test_back_walks_the_stack_and_stops_at_the_root(device, android):
    device._shell("input tap 20 120")
    device._shell("input keyevent KEYCODE_BACK")
    assert android.screen.name == "main"
    device._shell("input keyevent 4")
    assert android.stack == ["main"]


def shown(device) -> str:
    found = re.search(r"mInputShown=(\w+)", device._shell("dumpsys input_method"))
    assert found
    return found[1]


def test_keyboard_shows_hides_on_back_and_is_in_dumpsys_input_method(device, android):
    device._shell("input tap 20 120")
    assert shown(device) == "false"
    device._shell("input tap 20 20")
    assert android.keyboard_visible
    assert shown(device) == "true"
    assert b"inputmethod.latin" not in android.render_xml()
    device._shell("input tap 20 400")  # lands on the keyboard, nothing follows
    assert android.screen.name == "editing"
    device._shell("input keyevent KEYCODE_BACK")
    assert (android.screen.name, android.keyboard_visible) == ("editing", False)
    assert shown(device) == "false"
    device._shell("input keyevent KEYCODE_BACK")
    assert android.screen.name == "settings"


@device_options(extra_packages=["com.unknown"])
def test_relaunch_by_monkey_and_am_start(device, android):
    android.show("launcher")
    assert "Events injected" in device._shell(
        f"monkey -p {SHOP} -c android.intent.category.LAUNCHER 1"
    )
    assert android.stack == ["main"]  # the task came back to the front
    android.show("launcher")
    device._shell(f"am start -n {SHOP}/.Main")
    assert android.screen.package == SHOP
    assert "No activities found" in device._shell("monkey -p com.unknown 1")
    assert "does not exist" in device._shell("am start -n com.unknown/.Main")
    assert android.screen.package == SHOP


@pytest.mark.parametrize(("api", "secure"), [(24, b""), (36, BLACK_PNG)])
def test_screencap_is_a_png_or_blanked_when_protected(fake_adb, api, secure):
    phone = FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", api=api)
    client = Device(adb_server=fake_adb.address)._client
    assert client.exec_out(SERIAL, "screencap -p") == PNG
    assert PNG.startswith(b"\x89PNG")
    phone.show("secret")
    assert client.exec_out(SERIAL, "screencap -p") == secure


def test_dump_fault_fails_only_the_chosen_attempt(device, android):
    android.fail_dump(2)
    device.dump()
    with pytest.raises(DumpError):
        device.dump()
    assert device.dump().package == SHOP
    assert android.dumps == 3


def test_another_app_and_a_dialog_come_to_the_front(device, android):
    android.switch_before_dump(1, "launcher")
    assert device.dump().package == "com.example.launcher"
    android.switch_before_dump(2, "permission")
    screen = device.dump()
    assert screen.package == "com.google.android.permissioncontroller"
    device._shell("input tap 160 330")  # the allow button goes back
    assert android.screen.name == "launcher"


def test_screen_changes_between_dump_and_press(device, android):
    android.switch_after_dump(1, "settings")
    device.dump()
    assert android.screen.name == "settings"
    device._shell("input tap 20 20")  # empty on screen "main", "Name" here
    assert android.inputs[-1].screen == "settings"
    assert android.screen.name == "editing"


@LAX
def test_inputs_are_recorded_with_the_screen_they_met(device, android):
    device._shell("input tap 20 120")
    device._shell("input keyevent KEYCODE_BACK")
    device._shell("input text hi")
    device._shell("input touchscreen tap 5 5")
    device._shell("input press")
    assert [(e.kind, e.screen) for e in android.inputs] == [
        ("tap", "main"),
        ("key", "settings"),
        ("text", "main"),
        ("unknown", "main"),
        ("unknown", "main"),
    ]
    assert android.inputs[0].point == (20, 120)


@LAX
def test_helper_assertions(device, android):
    device._shell("input tap 20 120")
    assert_no_press_inside(android, "delete")
    assert_only_back_keys(android)
    assert_no_text_input(android)
    assert_no_long_press(android)
    device._shell("input keyevent KEYCODE_BACK")
    device._shell("input tap 20 220")
    device._shell("input keyevent KEYCODE_HOME")
    device._shell("input text x")
    device._shell("input swipe 20 120 20 120 800")
    for check in [
        lambda: assert_no_press_inside(android, "Delete"),
        lambda: assert_only_back_keys(android),
        lambda: assert_no_text_input(android),
        lambda: assert_no_long_press(android),
    ]:
        with pytest.raises(AssertionError):
            check()


def test_strict_mode_refuses_commands_outside_the_allowlist(fake_adb, android):
    phone = Device(adb_server=fake_adb.address)
    phone.dump()
    phone._shell("input tap 20 120")
    assert fake_adb.unexpected == []
    with pytest.raises(AdbError):
        phone._shell("input text hello")
    assert fake_adb.unexpected == ["shell:input text hello"]
    assert android.inputs[-1].kind == "tap"  # the refused command never ran
    fake_adb.unexpected.clear()  # the fixture would otherwise fail this test


@LAX
def test_input_kind_and_package_assertions(device, android):
    device._shell("input tap 20 120")
    assert_input_kinds(android, {"tap", "key"})
    assert_inputs_only_in(android, SHOP)
    device._shell("input roll 1 1")
    with pytest.raises(AssertionError):
        assert_input_kinds(android, {"tap", "key"})
    android.show("launcher")
    device._shell("input tap 5 5")
    with pytest.raises(AssertionError):
        assert_inputs_only_in(android, SHOP)


def test_input_applied_then_connection_lost_is_never_resent(fake_adb, android, device):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device._shell("input tap 20 120")
    assert android.screen.name == "settings"  # the tap took effect
    assert android.dropped == ["input tap 20 120"]
    assert_no_resend_after_drop(android, fake_adb)
    device._shell("input tap 20 120")  # a resend
    with pytest.raises(AssertionError):
        assert_no_resend_after_drop(android, fake_adb)


def test_a_command_sent_before_the_drop_is_not_a_resend(fake_adb, android, device):
    device._shell("input tap 20 120")  # before the drop
    android.drop_after_input(2)
    with pytest.raises(AdbError):
        device._shell("input tap 20 120")  # the one that is dropped
    assert android.dropped == ["input tap 20 120"]
    assert_no_resend_after_drop(android, fake_adb)
    device._shell("input tap 20 120")  # after the drop
    with pytest.raises(AssertionError):
        assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("android")
def test_strict_mode_admits_only_what_uiprobe_sends(fake_adb):
    phone = Device(adb_server=fake_adb.address)
    assert phone.is_emulator()
    phone._shell("input keyevent KEYCODE_BACK")
    phone._shell("dumpsys input_method")
    assert fake_adb.unexpected == []
    for command in (
        "rm -f /sdcard/x",
        "input keyevent KEYCODE_HOME",
        "getprop ro.serialno",
        "cat /sdcard/x",
        "uiautomator dump /sdcard/x.xml",
    ):
        with pytest.raises(AdbError):
            phone._shell(command)
    assert len(fake_adb.unexpected) == 5
    fake_adb.unexpected.clear()


def test_override_wins_over_the_device_and_a_late_script_is_refused(
    fake_adb, android, device
):
    with pytest.raises(ValueError, match="shadows"):
        fake_adb.on_shell(SERIAL, "wm size", b"x")
    android.override("wm size", b"Physical size: 1x1\n")
    assert device._shell("wm size") == "Physical size: 1x1\n"


def test_window_title_has_the_full_class_name_and_no_ime_window_line(
    fake_adb,
):
    old = FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", api=24)
    assert f"{SHOP}/{SHOP}.Main" in old._dumpsys_window()
    new = FakeAndroid(fake_adb, "emulator-5556", SCREENS, start="editing", api=36)
    # Plain `dumpsys window` has no mInputMethodWindow line in any capture.
    assert "mInputMethodWindow" not in old._dumpsys_window()
    assert "mInputMethodWindow" not in new._dumpsys_window()
    new.keyboard_in_dump = True
    assert b"inputmethod.latin" in new.render_xml()


def kind_of(command: str) -> str:
    """The service kind uiprobe uses: ``exec`` for cat and screencap."""
    return "exec" if command.startswith(("cat ", "screencap")) else "shell"


def allowed(
    android: FakeAndroid, command: str, *extra: str, kind: str | None = None
) -> bool:
    patterns = android.allowed(extra)[kind or kind_of(command)]
    return any(p.search(command) for p in patterns)


TMP = "/data/local/tmp/uiprobe-0123456789abcdef.xml"
VALID = (
    f"uiautomator dump {TMP}",
    f"cat {TMP}",
    f"rm -f {TMP}",
    "wm size",
    "getprop ro.build.version.sdk",
    "getprop ro.kernel.qemu",
    "dumpsys window",
    "dumpsys activity activities",
    "dumpsys input_method",
    "input tap 20 120",
    "input swipe 20 120 20 120 800",
    "input keyevent KEYCODE_BACK",
    "input keyevent 4",
    f"monkey -p {SHOP} 1",
    f"monkey -p {SHOP} -c android.intent.category.LAUNCHER 1",
    f"am start -n {SHOP}/.Main",
    f"am start -n {SHOP}/{SHOP}.Main",
    "screencap -p",
)
INJECTIONS = (
    "; reboot",
    " && reboot",
    " || reboot",
    " | sh",
    " $(reboot)",
    " `reboot`",
    "\nreboot",
    "\n",
    " 'x'",
    ' "x"',
    "\\",
    " > /sdcard/x",
    " < /sdcard/x",
    " &",
)


def test_every_valid_command_is_admitted(android):
    assert [c for c in VALID if not allowed(android, c)] == []


@pytest.mark.parametrize("command", VALID)
@pytest.mark.parametrize("suffix", INJECTIONS)
def test_strict_mode_rejects_chains_and_quoting_in_every_command_family(
    android, command, suffix
):
    assert not allowed(android, command + suffix)
    # Injected into the last token as well, not only appended after a space.
    assert not allowed(android, command + suffix.lstrip(" "))


@pytest.mark.parametrize(
    "command",
    [
        f"am start -n {SHOP}/.Main;reboot",
        f"am start -n {SHOP}/.Main&&reboot",
        f"am start -n {SHOP}/.Main|reboot",
        f"am start -n {SHOP}/.Main$(reboot)",
        f"am start -n {SHOP}/.Main`reboot`",
        f"am start -n {SHOP}/.Main$Inner",
        f"am start -n {SHOP}/",
        f"am start -n {SHOP}/..Main",
        f"am start -n {SHOP}/.Main..X",
        "am start -n shop/.Main",
        f"monkey -p {SHOP};reboot 1",
        f"monkey -p {SHOP} -c a;b 1",
        "monkey -p com.a.b  1",
        "input tap 1 2;reboot",
        "input tap \u0661 2",
        "input swipe 1 2 1 3 800",
        "input keyevent KEYCODE_BACK;reboot",
        "input keyevent 4;reboot",
        f"cat {TMP};reboot",
        f"cat {TMP}\n",
        "cat /data/local/tmp/uiprobe-xyz.xml",
        "cat /data/local/tmp/../uiprobe-0123456789abcdef.xml",
        "wm size;reboot",
        "wm size\n",
        "dumpsys window;reboot",
    ],
)
def test_strict_mode_rejects_malformed_and_chained_commands(android, command):
    assert not allowed(android, command)


@pytest.mark.parametrize(
    "command",
    [
        "monkey -p com.attacker.app 1",
        "monkey -p com.attacker.app -c com.attacker.Category 1",
        f"monkey -p {SHOP} -c com.attacker.Category 1",
        f"monkey -p {SHOP} -c android.intent.category.LAUNCHER.x 1",
        f"monkey -p {SHOP}x 1",
        "am start -n com.attacker.app/.Main",
        f"am start -n {SHOP}/.Undeclared",
        f"am start -n {SHOP}/{SHOP}.Undeclared",
        f"am start -n {SHOP}x/.Main",
    ],
)
def test_strict_mode_refuses_undeclared_packages_components_and_categories(
    android, command
):
    assert not allowed(android, command)


def test_extra_packages_admit_only_the_missing_package_launches(android):
    extra = "com.example.missing"
    assert allowed(android, f"monkey -p {extra} 1", extra)
    assert allowed(android, f"am start -n {extra}/.Main", extra)
    assert not allowed(android, f"monkey -p {extra} 1")
    assert not allowed(android, "monkey -p com.attacker.app 1", extra)
    assert not allowed(android, f"monkey -p {extra} -c com.attacker.Category 1", extra)


def test_allowed_refuses_a_declared_package_as_extra(android):
    with pytest.raises(ValueError, match="already declares"):
        android.allowed([SHOP])


@pytest.mark.parametrize("extra", ["x;reboot", "x", "com.a b.c", "com.example.m\n", ""])
def test_allowed_refuses_an_extra_that_is_not_package_shaped(android, extra):
    with pytest.raises(ValueError, match="not a package name"):
        android.allowed([extra])


def test_allowed_accepts_a_valid_missing_package(android):
    assert android.allowed(["com.example.missing"])


@device_options(extra_packages=["com.example.missing"])
def test_a_configured_missing_package_gets_the_real_failure_output(fake_adb, android):
    extra = "com.example.missing"
    phone = Device(adb_server=fake_adb.address)
    before = list(android.stack)
    assert "No activities found" in phone._shell(f"monkey -p {extra} 1")
    assert "does not exist" in phone._shell(f"am start -n {extra}/.Main")
    assert android.stack == before
    assert fake_adb.unexpected == []


def test_strict_mode_refuses_an_unknown_package_and_category_end_to_end(
    fake_adb, android
):
    phone = Device(adb_server=fake_adb.address)
    android.show("launcher")
    for command in (
        "monkey -p com.attacker.app 1",
        f"monkey -p {SHOP} -c com.attacker.Category 1",
        "am start -n com.attacker.app/.Main",
    ):
        with pytest.raises(AdbError):
            phone._shell(command)
    assert len(fake_adb.unexpected) == 3
    assert android.stack[-1] == "launcher"  # nothing launched
    fake_adb.unexpected.clear()


@LAX
def test_monkey_honours_the_category(device, android):
    android.show("launcher")
    out = device._shell(f"monkey -p {SHOP} -c com.attacker.Category 1")
    assert "No activities found" in out
    assert android.stack[-1] == "launcher"
    out = device._shell(f"monkey -p {SHOP} -c {LAUNCHER_CATEGORY} 1")
    assert "Events injected: 1" in out
    assert android.screen.package == SHOP


def test_strict_mode_serves_no_chain_end_to_end(fake_adb, android):
    phone = Device(adb_server=fake_adb.address)
    android.show("launcher")
    for command in (
        f"am start -n {SHOP}/.Main;reboot",
        f"am start -n {SHOP}/.Main\nreboot",
        f"monkey -p {SHOP} 1 && reboot",
    ):
        with pytest.raises(AdbError):
            phone._shell(command)
    assert len(fake_adb.unexpected) == 3
    assert android.screen.name == "launcher"
    fake_adb.unexpected.clear()


@device_options(start="launcher")
def test_explicit_launch_starts_the_named_component_of_an_app_that_is_not_running(
    device, android
):
    device._shell(f"am start -n {SHOP}/.Menu")
    assert android.stack == ["launcher", "menu"]
    # The full class name names the same activity as the short one.
    android.stack[:] = ["launcher"]
    device._shell(f"am start -n {SHOP}/{SHOP}.Main")
    assert android.stack == ["launcher", "main"]


def test_explicit_launch_of_a_running_app_brings_its_top_screen_forward(
    device, android
):
    # Assumed, not captured: the component is not consulted when the app has
    # screens on the stack; the top one comes forward, the rest above it go.
    device._shell("input tap 20 120")
    device._shell("input tap 20 20")
    android.show("launcher")
    assert android.stack == ["main", "settings", "editing", "launcher"]
    device._shell(f"am start -n {SHOP}/.Main")
    assert android.stack == ["main", "settings", "editing"]


@LAX
def test_explicit_launch_of_an_unknown_activity_fails_even_for_a_running_app(
    device, android
):
    out = device._shell(f"am start -n {SHOP}/.Missing")
    assert "does not exist" in out
    assert android.stack == ["main"]


@LAX
def test_launch_of_a_missing_activity_fails_and_changes_nothing(device, android):
    android.show("launcher")
    before = list(android.stack)
    out = device._shell(f"am start -n {SHOP}/.Missing")
    assert f"Error: Activity class {{{SHOP}/{SHOP}.Missing}} does not exist." in out
    assert "Error type 3" in out
    assert android.stack == before
    out = device._shell(f"am start -n {SHOP}/other.pkg.Main")
    assert "does not exist" in out
    assert android.stack == before
    out = device._shell("am start -n com.example.launcher/.Menu")  # wrong app
    assert "does not exist" in out
    assert android.stack == before


def test_disabled_elements_do_not_follow_transitions(fake_adb):
    screens = (
        Screen(
            "a",
            f"{SHOP}/.A",
            (El((0, 0, 100, 100), text="Off", go="b", long_go="b", enabled=False),),
        ),
        Screen("b", f"{SHOP}/.B", ()),
    )
    phone = FakeAndroid(fake_adb, SERIAL, screens, start="a")
    adb = Device(adb_server=fake_adb.address)
    adb._shell("input tap 10 10")
    adb._shell("input swipe 10 10 10 10 800")
    assert phone.screen.name == "a"
    # The touches were delivered and recorded, and rendered as disabled.
    assert [(e.kind, e.screen) for e in phone.inputs] == [
        ("tap", "a"),
        ("long_press", "a"),
    ]
    assert b'enabled="false"' in phone.render_xml()


def test_a_disabled_element_still_takes_the_touch_from_what_is_below(fake_adb):
    screens = (
        Screen(
            "a",
            f"{SHOP}/.A",
            (
                El((0, 0, 100, 100), text="Under", go="b"),
                El((0, 0, 100, 100), text="Over", enabled=False),
            ),
        ),
        Screen("b", f"{SHOP}/.B", ()),
    )
    phone = FakeAndroid(fake_adb, SERIAL, screens, start="a")
    Device(adb_server=fake_adb.address)._shell("input tap 10 10")
    assert phone.screen.name == "a"


@pytest.mark.parametrize(
    "component",
    [
        "com.example.app/.Main;reboot",
        "com.example.app/.Main\nreboot",
        "com.example.app/.Main$Inner",
        "com.example.app/.Mainé",
        "com.example.app/.Main\n",
        "com.example.app",
        "com.example.app/",
        "com.example.app;reboot/.Main",
        "com.example.app -c com.attacker.Category/.Main",
        "com.exämple.app/.Main",
    ],
)
def test_a_malformed_declared_component_fails_at_setup(fake_adb, component):
    with pytest.raises(ValueError, match="not a package/class component"):
        FakeAndroid(fake_adb, SERIAL, (Screen("main", component),), start="main")


@pytest.mark.parametrize(
    "component", ["com.example.app/.Main", "com.example.app/com.example.app.Main"]
)
def test_short_and_full_class_spellings_are_declarable(fake_adb, component):
    phone = FakeAndroid(fake_adb, SERIAL, (Screen("main", component),), start="main")
    assert phone.screen.name == "main"
    assert any(p.search(f"am start -n {component}") for p in phone.allowed()["shell"])


def test_screens_are_read_only(fake_adb):
    phone = FakeAndroid(
        fake_adb, SERIAL, (Screen("main", "com.example.app/.Main"),), start="main"
    )
    with pytest.raises(TypeError):
        phone.screens["main"] = Screen("main", "com.example.app/.Main;reboot")  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        phone.screens["other"] = Screen("other", "com.example.app/.Other")  # ty: ignore[invalid-assignment]


def test_allowed_validates_the_screens_again(fake_adb):
    phone = FakeAndroid(
        fake_adb, SERIAL, (Screen("main", "com.example.app/.Main"),), start="main"
    )
    phone._screens["main"] = Screen("main", "com.example.app/.Main;reboot")
    with pytest.raises(ValueError, match="not a package/class component"):
        phone.allowed()


def _server_state(server):
    return (
        [dict(d) for d in server.devices],
        {k: list(v) for k, v in server._shell.items()},
        {k: list(v) for k, v in server._exec.items()},
        {k: {kind: list(m) for kind, m in v.items()} for k, v in server._rules.items()},
        set(server._open),
    )


MAIN = Screen("main", "com.example.app/.Main")


@pytest.mark.parametrize(
    ("screens", "start", "extra"),
    [
        ((MAIN,), "main", ["invalid"]),
        ((MAIN,), "main", ["com.example.app"]),
        ((Screen("main", "com.example.app/.Main;reboot"),), "main", []),
        ((MAIN, Screen("main", "com.example.app/.Other")), "main", []),
        ((MAIN,), "nowhere", []),
    ],
    ids=["invalid-extra", "declared-extra", "bad-component", "duplicate", "start"],
)
def test_a_refused_construction_leaves_the_server_unchanged(
    fake_adb, screens, start, extra
):
    before = _server_state(fake_adb)
    with pytest.raises(ValueError):  # noqa: PT011
        FakeAndroid(fake_adb, "d1", screens, start=start, extra_packages=extra)
    assert _server_state(fake_adb) == before
    assert before == ([], {}, {}, {}, set())
    phone = FakeAndroid(fake_adb, "d1", (MAIN,), start="main")
    client = Device(adb_server=fake_adb.address, serial="d1")._client
    assert client.shell("d1", "wm size")
    with pytest.raises(AdbError):
        client.shell("d1", "input text hello")
    assert fake_adb.unexpected == ["shell:input text hello"]
    assert phone.inputs == []
    fake_adb.unexpected.clear()


# --- keyboard output per API (observed on API 24-32 and 36 emulators) ---

OLD_LINE = (
    "  mShowRequested={shown} mShowExplicitlyRequested={explicit}"
    " mShowForced=false mInputShown={shown}\n"
)
NEW_BLOCK = (
    "    mVisibilityStateComputer:\n"
    "      mRequestedShowExplicitly={shown} mShowForced=false\n"
    "      mImeHiddenByDisplayPolicy=false\n"
    "      mInputShown={shown}\n"
    "      mLastImeTargetWindow=android.os.BinderProxy@f4cef99\n"
)


def keyboard_states(fake_adb: FakeAdbServer, api: int) -> tuple[str, str]:
    """Return the input_method text with the keyboard up and with it hidden."""
    phone = FakeAndroid(fake_adb, SERIAL, SCREENS, start="editing", api=api)
    up = phone._dumpsys_input_method()
    phone.show("main")
    return up, phone._dumpsys_input_method()


@pytest.mark.parametrize(
    ("api", "explicit_when_shown"),
    [
        (24, True),
        (26, True),
        (28, True),
        (29, True),
        (30, True),
        (31, False),
        (32, False),
    ],
)
def test_input_method_is_one_line_on_api_24_to_32(fake_adb, api, explicit_when_shown):
    shown, hidden = keyboard_states(fake_adb, api)
    explicit = _bool(explicit_when_shown)
    assert shown.startswith(OLD_LINE.format(shown="true", explicit=explicit))
    assert hidden.startswith(OLD_LINE.format(shown="false", explicit="false"))
    assert "mVisibilityStateComputer" not in shown + hidden


@pytest.mark.parametrize("api", [33, 34, 35, 36])  # 33-35 assumed to match 36
def test_input_method_is_a_block_on_api_36(fake_adb, api):
    shown, hidden = keyboard_states(fake_adb, api)
    assert shown.startswith(NEW_BLOCK.format(shown="true"))
    assert hidden.startswith(NEW_BLOCK.format(shown="false"))
    assert "mShowRequested" not in shown + hidden


@pytest.mark.parametrize("api", [24, 31, 36])
def test_is_input_view_shown_is_no_signal(fake_adb, api):
    for state in keyboard_states(fake_adb, api):
        assert "mIsInputViewShown=true" in state


def _bool(value: object) -> str:
    return "true" if value else "false"


# --- strict by default, scoped to the serial and the service kind ---


def test_strict_is_the_default_so_an_unexpected_command_fails(fake_adb, android):
    phone = Device(adb_server=fake_adb.address)
    with pytest.raises(AdbError):
        phone._shell("input text hello")
    assert fake_adb.unexpected == ["shell:input text hello"]
    assert android.inputs == []
    fake_adb.unexpected.clear()


def test_strict_false_serves_any_command(fake_adb):
    phone = FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", strict=False)
    Device(adb_server=fake_adb.address)._shell("input text hello")
    assert [e.kind for e in phone.inputs] == ["text"]
    assert fake_adb.unexpected == []


@pytest.mark.usefixtures("android")
def test_cat_and_screencap_need_exec_and_the_rest_needs_shell(fake_adb):
    client = Device(adb_server=fake_adb.address)._client
    assert client.exec_out(SERIAL, "screencap -p") == PNG
    assert (
        client.exec_out(SERIAL, "cat /data/local/tmp/uiprobe-0123456789abcdef.xml")
        == b""
    )
    assert fake_adb.unexpected == []
    with pytest.raises(AdbError):
        client.shell(SERIAL, "screencap -p")
    with pytest.raises(AdbError):
        client.shell(SERIAL, "cat /data/local/tmp/uiprobe-0123456789abcdef.xml")
    with pytest.raises(AdbError):
        client.exec_out(SERIAL, "wm size")
    assert fake_adb.unexpected == [
        "shell:screencap -p",
        "shell:cat /data/local/tmp/uiprobe-0123456789abcdef.xml",
        "exec:wm size",
    ]
    fake_adb.unexpected.clear()


@pytest.mark.usefixtures("android")
def test_the_allowlist_is_scoped_to_the_serial(fake_adb):
    other = "emulator-5556"
    client = Device(adb_server=fake_adb.address)._client
    fake_adb.add_device(other)  # a device the fake does not model
    assert client.shell(SERIAL, "wm size")
    with pytest.raises(AdbError):
        client.shell(other, "wm size")
    assert fake_adb.unexpected == ["shell:wm size (no allowlist for emulator-5556)"]
    fake_adb.unexpected.clear()


def test_two_devices_each_get_their_own_allowlist(fake_adb):
    one = FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")
    two = FakeAndroid(fake_adb, "emulator-5556", (SCREENS[0],), start="launcher")
    client = Device(adb_server=fake_adb.address, serial=SERIAL)._client
    assert client.shell(SERIAL, "wm size")
    assert client.shell("emulator-5556", "wm size")
    assert (one.serial, two.serial) == (SERIAL, "emulator-5556")
    assert client.shell(SERIAL, f"am start -n {SHOP}/.Settings") is not None
    assert not fake_adb.unexpected
    with pytest.raises(AdbError):
        client.shell(
            "emulator-5556", f"am start -n {SHOP}/.Settings"
        )  # not declared there
    assert fake_adb.unexpected == [f"shell:am start -n {SHOP}/.Settings"]
    fake_adb.unexpected.clear()


def test_a_lax_device_is_served_next_to_a_strict_one(fake_adb, android):
    lax = FakeAndroid(fake_adb, "emulator-5556", SCREENS, start="main", strict=False)
    client = Device(adb_server=fake_adb.address, serial=SERIAL)._client
    client.shell("emulator-5556", "input text hello")
    assert [e.kind for e in lax.inputs] == ["text"]
    assert fake_adb.unexpected == []
    with pytest.raises(AdbError):
        client.shell(SERIAL, "input text hello")
    assert android.inputs == []
    assert fake_adb.unexpected == ["shell:input text hello"]
    fake_adb.unexpected.clear()


def test_a_lax_device_created_first_stays_served(fake_adb):
    lax = FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", strict=False)
    FakeAndroid(fake_adb, "emulator-5556", SCREENS, start="main")
    client = Device(adb_server=fake_adb.address, serial=SERIAL)._client
    client.shell(SERIAL, "input text hello")
    assert [e.kind for e in lax.inputs] == ["text"]
    assert fake_adb.unexpected == []


def test_monkey_output_starts_with_the_preamble_monkey_prints(device):
    out = device._shell(f"monkey -p {SHOP} -c {LAUNCHER_CATEGORY} 1")
    lines = out.splitlines()
    assert lines[:5] == [
        "  bash arg: -p",
        f"  bash arg: {SHOP}",
        "  bash arg: -c",
        f"  bash arg: {LAUNCHER_CATEGORY}",
        "  bash arg: 1",
    ]
    assert lines[5] == f"args: [-p, {SHOP}, -c, {LAUNCHER_CATEGORY}, 1]"
    assert lines[6] == ' arg: "-p"'
    assert lines[10] == ' arg: "1"'
    assert lines[11:13] == [f'data="{SHOP}"', f'data="{LAUNCHER_CATEGORY}"']
    assert lines[13] == "Events injected: 1"
    assert lines[0] != "Events injected: 1"


def test_am_start_echoes_the_short_component(device):
    out = device._shell(f"am start -n {SHOP}/{SHOP}.Main")
    assert out == f"Starting: Intent {{ cmp={SHOP}/.Main }}\n"


# --- screen validation ---


def test_a_screen_named_back_is_refused(fake_adb):
    with pytest.raises(ValueError, match="reserved"):
        FakeAndroid(
            fake_adb, SERIAL, (Screen("back", "com.example.app/.A"),), start="back"
        )


def test_a_duplicate_screen_name_is_refused(fake_adb):
    screens = (Screen("a", "com.example.app/.A"), Screen("a", "com.example.app/.B"))
    with pytest.raises(ValueError, match="duplicate screen names"):
        FakeAndroid(fake_adb, SERIAL, screens, start="a")


def test_an_undeclared_start_is_refused(fake_adb):
    with pytest.raises(ValueError, match="'nope' is not a declared screen"):
        FakeAndroid(fake_adb, SERIAL, SCREENS, start="nope")


@pytest.mark.parametrize("field", ["go", "long_go"])
def test_an_undeclared_transition_target_is_refused(fake_adb, field):
    fields: dict[str, Any] = {field: "typo"}
    element = El((0, 0, 9, 9), **fields)
    screens = (Screen("a", "com.example.app/.A", (element,)),)
    with pytest.raises(ValueError, match="'typo' is not a declared screen"):
        FakeAndroid(fake_adb, SERIAL, screens, start="a")


def test_back_is_a_valid_transition_target(fake_adb):
    screens = (Screen("a", "com.example.app/.A", (El((0, 0, 9, 9), go="back"),)),)
    assert FakeAndroid(fake_adb, SERIAL, screens, start="a").screen.name == "a"


def test_show_and_the_dump_switches_refuse_an_undeclared_screen(android):
    with pytest.raises(ValueError, match="is not a declared screen"):
        android.show("nope")
    with pytest.raises(ValueError, match="is not a declared screen"):
        android.switch_before_dump(1, "nope")
    with pytest.raises(ValueError, match="is not a declared screen"):
        android.switch_after_dump(1, "nope")
    assert android.stack == ["main"]


# --- system packages ---

RESOLVER = "android/com.android.internal.app.ResolverActivity"


def test_a_single_segment_system_package_can_be_declared(fake_adb):
    screens = (
        Screen("main", f"{SHOP}/.Main"),
        Screen("chooser", RESOLVER, (El((0, 0, 50, 50), text="Open with"),)),
    )
    phone = FakeAndroid(fake_adb, SERIAL, screens, start="main")
    client = Device(adb_server=fake_adb.address)
    assert f"cmp={RESOLVER}" in client._shell(f"am start -n {RESOLVER}")
    assert phone.screen.name == "chooser"
    assert client.dump().package == "android"
    assert RESOLVER in phone._dumpsys_window()
    assert "Events injected" in client._shell("monkey -p android 1")


def test_a_nested_class_with_a_dollar_stays_refused(fake_adb):
    with pytest.raises(ValueError, match="not a package/class component"):
        FakeAndroid(
            fake_adb,
            SERIAL,
            (Screen("a", "android/com.android.internal.app.Resolver$Inner"),),
            start="a",
        )


# --- malformed commands in lax mode ---


@LAX
@pytest.mark.parametrize("command", ["input tap a b", "input text 'unbalanced"])
def test_malformed_input_is_recorded_as_unknown_not_a_crash(fake_adb, android, command):
    Device(adb_server=fake_adb.address)._shell(command)
    assert [e.kind for e in android.inputs] == ["unknown"]
    assert fake_adb.handler_errors == []


@LAX
def test_a_swipe_with_non_integer_coordinates_is_a_plain_swipe(fake_adb, android):
    Device(adb_server=fake_adb.address)._shell("input swipe a b a b 800")
    assert [(e.kind, e.point) for e in android.inputs] == [("swipe", None)]


@LAX
def test_a_malformed_non_input_command_answers_nothing(fake_adb, android):
    assert Device(adb_server=fake_adb.address)._shell("getprop 'oops") == ""
    assert android.inputs == []
    assert fake_adb.handler_errors == []
