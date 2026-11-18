"""Tests for device selection and the Device class."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

import pytest

import uiprobe
from tests.fake_adb_server import Fail, FakeAdbServer, Response, Stall, Trickle
from tests.helpers import unused_address
from uiprobe import adb
from uiprobe.adb import DeviceInfo, ServerAddress
from uiprobe.device import Device, device_details, devices, select_device
from uiprobe.errors import (
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    DumpError,
    MultipleDevicesError,
)

DATA = Path(__file__).parent / "data"
SERIAL = "emulator-5554"


def phone_server(phone: FakeAdbServer, *, default: bool) -> ServerAddress:
    """Point the default server at the fake (is_default keeps its meaning)."""
    return ServerAddress("127.0.0.1", phone.port, is_default=default)


def info(serial: str, state: str = "device") -> DeviceInfo:
    return DeviceInfo(serial=serial, state=state)


# --- select_device ---


def test_select_single_ready_device():
    chosen = select_device([info("a"), info("b", "offline")], environ={})
    assert chosen.serial == "a"


def test_select_requested_serial_argument_wins_over_environment():
    found = [info("a"), info("b")]
    assert select_device(found, "b", {"ANDROID_SERIAL": "a"}).serial == "b"


def test_select_uses_android_serial():
    found = [info("a"), info("b")]
    assert select_device(found, None, {"ANDROID_SERIAL": "b"}).serial == "b"


def test_select_empty_android_serial_counts_as_unset():
    assert select_device([info("a")], None, {"ANDROID_SERIAL": ""}).serial == "a"


def test_select_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("ANDROID_SERIAL", "b")
    assert select_device([info("a"), info("b")]).serial == "b"


def test_select_empty_serial_argument_is_a_request_not_unset():
    # `-s "$UNSET"` must not silently pick the only device or ANDROID_SERIAL.
    with pytest.raises(DeviceNotFoundError) as caught:
        select_device([info("a")], "", {"ANDROID_SERIAL": "a"})
    assert caught.value.details["serial"] == ""


@pytest.mark.parametrize("serial", [5554, b"a", ["a"]])
def test_select_rejects_a_serial_that_is_not_a_string(serial):
    with pytest.raises(TypeError, match="serial must be a string"):
        select_device([info("5554")], serial, {})


def test_select_requested_missing():
    with pytest.raises(DeviceNotFoundError) as caught:
        select_device([info("a", "offline")], "z", {})
    assert caught.value.details == {
        "serial": "z",
        "devices": [{"serial": "a", "state": "offline"}],
    }


@pytest.mark.parametrize(
    ("state", "error"),
    [("offline", DeviceOfflineError), ("unauthorized", DeviceUnauthorizedError)],
)
def test_select_requested_not_ready(state, error):
    with pytest.raises(error) as caught:
        select_device([info("a", state)], "a", {})
    assert caught.value.details == {"serial": "a", "state": state}


def test_select_requested_other_state_is_offline():
    with pytest.raises(DeviceOfflineError):
        select_device([info("a", "no permissions (udev)")], "a", {})


def test_select_multiple_ready():
    with pytest.raises(MultipleDevicesError) as caught:
        select_device([info("a"), info("b"), info("c", "offline")], None, {})
    assert caught.value.details == {"serials": ["a", "b"]}
    assert "--serial" in caught.value.message


def test_select_nothing_connected():
    with pytest.raises(DeviceNotFoundError) as caught:
        select_device([], None, {})
    assert caught.value.details == {"devices": []}


def test_select_only_unusable_devices_prefers_unauthorized():
    found = [info("a", "offline"), info("b", "unauthorized")]
    with pytest.raises(DeviceUnauthorizedError) as caught:
        select_device(found, None, {})
    assert caught.value.details == {
        "devices": [
            {"serial": "a", "state": "offline"},
            {"serial": "b", "state": "unauthorized"},
        ]
    }


def test_select_only_offline_devices():
    with pytest.raises(DeviceOfflineError):
        select_device([info("a", "offline")], None, {})


# --- Device and devices() ---


@pytest.fixture
def phone(fake_adb: FakeAdbServer) -> FakeAdbServer:
    fake_adb.add_device(SERIAL, model="Pixel_8")
    return fake_adb


def test_devices_lists_every_state(fake_adb):
    fake_adb.add_device("a", model="M")
    fake_adb.add_device("b", "unauthorized")
    found = devices(adb_server=fake_adb.address)
    assert [(d.serial, d.state, d.model) for d in found] == [
        ("a", "device", "M"),
        ("b", "unauthorized", None),
    ]


def test_device_selects_at_construction(phone):
    device = Device(adb_server=phone.address)
    assert device.serial == SERIAL
    assert device.info.model == "Pixel_8"


def test_device_fails_immediately_when_none_matches(phone):
    with pytest.raises(DeviceNotFoundError):
        Device("other", adb_server=phone.address)


def test_device_multiple_devices(phone):
    phone.add_device("emulator-5556")
    with pytest.raises(MultipleDevicesError):
        Device(adb_server=phone.address)
    assert Device("emulator-5556", adb_server=phone.address).serial == "emulator-5556"


def test_device_reports_unreachable_server():
    with pytest.raises(AdbServerUnavailableError):
        Device(adb_server=unused_address(), start_server=False)


def test_device_rejects_bad_server_address():
    with pytest.raises(ValueError, match="adb server argument"):
        Device(adb_server="localhost:5037")


def test_device_rejects_missing_adb_file(tmp_path):
    with pytest.raises(ValueError, match="adb argument"):
        devices(adb=tmp_path / "nope")


def test_device_rejects_non_string_arguments(phone):
    with pytest.raises(TypeError, match="adb_server must be a string"):
        Device(adb_server=5037)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="serial must be a string"):
        Device(5554, adb_server=phone.address)  # ty: ignore[invalid-argument-type]


def test_device_rejects_an_empty_server_address_and_serial(phone):
    with pytest.raises(ValueError, match="adb server argument"):
        Device(adb_server="")
    with pytest.raises(DeviceNotFoundError):
        Device("", adb_server=phone.address)


def test_a_stale_uiprobe_adb_does_not_break_an_explicit_server(
    phone, monkeypatch, tmp_path
):
    # adb is only ever run to start a default server nobody chose.
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    assert Device(adb_server=phone.address).serial == SERIAL
    assert [d.serial for d in devices(adb_server=phone.address)] == [SERIAL]
    monkeypatch.setenv("ADB_SERVER_SOCKET", phone.address)
    assert Device().serial == SERIAL


def test_a_stale_uiprobe_adb_does_not_break_no_start_server(
    phone, monkeypatch, tmp_path
):
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    assert Device(start_server=False).serial == SERIAL


def test_a_stale_uiprobe_adb_does_not_break_a_blocked_start(
    phone, monkeypatch, tmp_path
):
    # A non-empty ANDROID_ADB_SERVER_ADDRESS means adb is never started.
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "192.0.2.1")
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    assert Device().serial == SERIAL


def test_an_empty_android_adb_server_address_does_not_hide_a_stale_adb(
    phone, monkeypatch, tmp_path
):
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setenv("ANDROID_ADB_SERVER_ADDRESS", "")
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    with pytest.raises(ValueError, match="UIPROBE_ADB"):
        Device()


def test_a_stale_uiprobe_adb_still_fails_when_it_could_be_used(
    phone, monkeypatch, tmp_path
):
    monkeypatch.setenv("UIPROBE_ADB", str(tmp_path / "removed-sdk" / "adb"))
    monkeypatch.setattr(adb, "DEFAULT_SERVER", phone_server(phone, default=True))
    with pytest.raises(ValueError, match="UIPROBE_ADB"):
        Device()


def test_an_explicit_adb_argument_is_checked_even_when_unused(phone, tmp_path):
    with pytest.raises(ValueError, match="adb argument"):
        Device(adb_server=phone.address, adb=tmp_path / "nope")
    with pytest.raises(ValueError, match="adb argument"):
        devices(adb_server=phone.address, adb="")


def test_shell_decodes_with_replacement(phone):
    phone.on_shell(SERIAL, "echo hi", b"caf\xc3\xa9 \xff")
    assert Device(adb_server=phone.address)._shell("echo hi") == "café �"


def test_api_level_is_cached(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    device = Device(adb_server=phone.address)
    assert device.api_level() == 36
    assert device.api_level() == 36
    assert phone.requests.count("shell:getprop ro.build.version.sdk") == 1


def test_api_level_unknown_when_not_a_number(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"\n")
    device = Device(adb_server=phone.address)
    assert device.api_level() is None
    assert device.api_level() is None
    assert phone.requests.count("shell:getprop ro.build.version.sdk") == 1


@pytest.mark.parametrize(("output", "expected"), [(b"1\n", True), (b"\n", False)])
def test_is_emulator(phone, output, expected):
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", output)
    assert Device(adb_server=phone.address).is_emulator() is expected


def test_api_level_failure_is_none_and_not_remembered(phone):
    device = Device(adb_server=phone.address)
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", Fail("closed"))
    assert device.api_level() is None
    phone._shell[SERIAL].clear()
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"35\n")
    assert device.api_level() == 35


def test_api_level_ignores_absurdly_long_numbers(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"1" * 5000 + b"\n")
    assert Device(adb_server=phone.address).api_level() is None


# --- device_details ---


def test_device_details_uses_one_listing_for_every_device(phone):
    phone.add_device("R58M123", model="SM_G991")
    phone.add_device("off", "offline")
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", b"1\n")
    phone.on_shell("R58M123", "getprop ro.build.version.sdk", b"34\n")
    found = device_details(adb_server=phone.address)
    assert [(d.info.serial, d.api_level, d.emulator) for d in found] == [
        (SERIAL, 36, True),
        ("R58M123", 34, False),
        ("off", None, None),
    ]
    assert phone.requests.count("host:devices-l") == 1
    assert phone.requests.count("host:version") == 0


def test_device_details_properties_fail_independently(phone):
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", Fail("closed"))
    (details,) = device_details(adb_server=phone.address)
    assert (details.api_level, details.emulator) == (36, None)
    phone._shell[SERIAL].clear()
    phone.on_shell(SERIAL, "getprop ro.build.version.sdk", Fail("closed"))
    phone.on_shell(SERIAL, "getprop ro.kernel.qemu", b"1\n")
    (details,) = device_details(adb_server=phone.address)
    assert (details.api_level, details.emulator) == (None, True)


def test_device_details_survives_a_device_that_vanishes(phone):
    phone.fail_request("host:transport:" + SERIAL, "device offline")
    (details,) = device_details(adb_server=phone.address)
    assert (details.info.serial, details.api_level, details.emulator) == (
        SERIAL,
        None,
        None,
    )


# --- dump ---

LOGIN_XML = (DATA / "dump_login.xml").read_bytes()
DUMPSYS = b"""\
  mCurrentFocus=Window{4f2a1c u0 com.example.shop/com.example.shop.LoginActivity}
  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.LoginActivity t12}
"""


def script_dump(
    server: FakeAdbServer,
    *,
    dumpsys: bytes = DUMPSYS,
    xml: bytes = LOGIN_XML,
    wm_size: Response = b"",
) -> None:
    server.on_shell(SERIAL, "wm size", wm_size)
    server.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    server.on_shell(SERIAL, "dumpsys window", dumpsys)
    server.on_shell(
        SERIAL,
        re.compile(r"uiautomator dump /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml$"),
        b"UI hierchary dumped to: /data/local/tmp/x.xml\n",
    )
    server.on_exec(
        SERIAL,
        re.compile(r"^cat /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml$"),
        xml,
    )


def dialog_xml(rotation: object = "0") -> bytes:
    """A dump that holds only a dialog rooted at [100,200][900,600]."""
    return (
        f'<?xml version="1.0"?><hierarchy rotation="{rotation}">'
        '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" '
        'package="com.android.systemui" content-desc="" bounds="[100,200][900,600]"/>'
        "</hierarchy>"
    ).encode()


PHYSICAL = b"Physical size: 1080x2400\n"


def test_dump_display_is_the_device_size_not_the_dialog_extent(phone):
    script_dump(phone, xml=dialog_xml(), wm_size=PHYSICAL)
    assert Device(adb_server=phone.address).dump().display == (1080, 2400)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        ("0", (1080, 2400)),
        ("1", (2400, 1080)),
        ("2", (1080, 2400)),
        ("3", (2400, 1080)),
        ("9", (1080, 2400)),
        ("x", (1080, 2400)),
        ("", (1080, 2400)),
    ],
)
def test_dump_display_follows_the_rotation(phone, rotation, expected):
    script_dump(phone, xml=dialog_xml(rotation), wm_size=PHYSICAL)
    assert Device(adb_server=phone.address).dump().display == expected


def test_dump_display_without_a_rotation_attribute_is_unrotated(phone):
    xml = dialog_xml().replace(b' rotation="0"', b"")
    script_dump(phone, xml=xml, wm_size=PHYSICAL)
    assert Device(adb_server=phone.address).dump().display == (1080, 2400)


def test_dump_display_prefers_the_override_size(phone):
    script_dump(
        phone,
        xml=dialog_xml("1"),
        wm_size=PHYSICAL + b"Override size: 720x1600\n",
    )
    assert Device(adb_server=phone.address).dump().display == (1600, 720)


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"garbage\n",
        b"Physical size: 1080x\n",
        b"Physical size: 0x2400\n",
        b"Physical size: 1080x240000\n",
        b"Physical size: \xd9\xa1080x2400\n",
        b"Physical size: 1080x2400\nPhysical size: 720x1600\n",
        b"Physical size: 1080x2400\nsomething else\n",
        b"Override size: 720x1600 extra\n",
        b"/system/bin/sh: wm: not found\n",
    ],
)
def test_dump_display_falls_back_when_wm_size_is_unusable(phone, output):
    script_dump(phone, xml=dialog_xml(), wm_size=output)
    assert Device(adb_server=phone.address).dump().display == (900, 600)


def test_dump_display_falls_back_when_wm_size_fails(phone):
    script_dump(phone, xml=dialog_xml(), wm_size=Fail("closed"))
    assert Device(adb_server=phone.address).dump().display == (900, 600)


def requests_of(server: FakeAdbServer, prefix: str) -> list[str]:
    return [r for r in server.requests if r.startswith(prefix)]


def test_dump_end_to_end(phone):
    script_dump(phone)
    screen = Device(adb_server=phone.address).dump()
    assert screen.serial == SERIAL
    assert screen.package == "com.example.shop"
    assert screen.activity == ".LoginActivity"
    assert screen.api_level == 36
    assert screen.display == (1080, 2400)
    assert [e.id for e in screen.elements][:2] == ["e1", "e2"]
    assert screen.elements[1].text == "ようこそ"
    assert screen.captured_at.utcoffset() is not None
    assert screen.captured_at.microsecond == 0
    assert isinstance(screen.captured_at, datetime)


def test_dump_removes_its_temporary_file(phone):
    script_dump(phone)
    Device(adb_server=phone.address).dump()
    (dump,) = requests_of(phone, "shell:uiautomator dump ")
    path = dump.rsplit(" ", 1)[1]
    assert path.startswith("/data/local/tmp/uiprobe-")
    assert requests_of(phone, "shell:rm ") == [f"shell:rm -f {path}"]
    assert requests_of(phone, "exec:") == [f"exec:cat {path}"]


def test_dump_uses_a_fresh_path_each_time(phone):
    script_dump(phone)
    device = Device(adb_server=phone.address)
    device.dump()
    device.dump()
    paths = {r.rsplit(" ", 1)[1] for r in requests_of(phone, "shell:rm ")}
    assert len(paths) == 2


def test_dump_removes_file_when_cat_fails(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), Fail("read failed"))
    with pytest.raises(uiprobe.AdbError):
        Device(adb_server=phone.address).dump()
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_dump_failure_keeps_the_original_error(phone):
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), b"ERROR: " + b"x" * 800)
    with pytest.raises(DumpError) as caught:
        Device(adb_server=phone.address).dump()
    output = caught.value.details["output"]
    assert isinstance(output, str)
    assert output.startswith("ERROR: x")
    assert len(output) == 500
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_dump_ignores_failing_cleanup(phone):
    script_dump(phone)
    phone.on_shell(SERIAL, re.compile(r"^rm "), Fail("rm refused"))
    # The scripts are matched in order: put the failing rm first.
    phone._shell[SERIAL].insert(0, phone._shell[SERIAL].pop())
    assert Device(adb_server=phone.address).dump().package == "com.example.shop"


def test_dump_cleanup_error_does_not_mask_dump_error(phone):
    phone.on_shell(SERIAL, re.compile(r"^rm "), Fail("rm refused"))
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), b"ERROR: no")
    with pytest.raises(DumpError):
        Device(adb_server=phone.address).dump()


def test_dump_bad_xml_is_a_dump_error(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), b"<hierarchy><node")
    with pytest.raises(DumpError):
        Device(adb_server=phone.address).dump()
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_dump_empty_hierarchy(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(SERIAL, re.compile(r"^cat "), b"<hierarchy rotation='0'/>")
    screen = Device(adb_server=phone.address).dump()
    assert (screen.package, screen.display, screen.elements) == ("", (0, 0), ())


def test_dump_activity_falls_back_to_focused_app(phone):
    script_dump(
        phone,
        dumpsys=b"  mCurrentFocus=null\n  mFocusedApp=ActivityRecord{1 u0 "
        b"com.example.shop/.Main t12}\n",
    )
    assert Device(adb_server=phone.address).dump().activity == ".Main"


def test_dump_activity_of_another_package_is_not_accepted(phone):
    script_dump(
        phone,
        dumpsys=b"  mCurrentFocus=Window{1 u0 com.other/com.other.Main}\n",
    )
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_unknown(phone):
    script_dump(phone, dumpsys=b"nothing useful\n")
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_lookup_failure_is_not_fatal(phone):
    script_dump(phone, dumpsys=b"")
    phone._shell[SERIAL].insert(0, (re.compile(r"^dumpsys "), Fail("no dumpsys")))
    assert Device(adb_server=phone.address).dump().activity is None


def dumpsys_with_focus(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def test_dump_activity_of_another_package_in_relative_form_is_not_accepted(phone):
    # The shade has focus; the focused app is another package. It must not be
    # reported for the dumped screen, and `.MainActivity` read against the
    # dumped package would name a class that does not exist.
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  mCurrentFocus=Window{9 u0 NotificationShade}",
            "  mFocusedApp=ActivityRecord{1 u0 com.other.app/.MainActivity t3}",
        ),
    )
    screen = Device(adb_server=phone.address).dump()
    assert screen.package == "com.example.shop"
    assert screen.activity is None


def test_dump_activity_in_the_dumped_package_stays_short(phone):
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  mCurrentFocus=Window{9 u0 Popup}",
            "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3}",
        ),
    )
    assert Device(adb_server=phone.address).dump().activity == ".Main"


REAL_APP = "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Real t3}"
FORGED_FOCUS = "  mCurrentFocus=Window{1 u0 com.example.shop/com.example.shop.Fake}"
FORGED_APP = "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Fake t3}"


def title_window(title: str) -> str:
    return f"    Window #1 Window{{7 u0 {title}}}:"


def activity_of(phone, *lines: str) -> str | None:
    script_dump(phone, dumpsys=dumpsys_with_focus(*lines))
    return Device(adb_server=phone.address).dump().activity


def test_dump_activity_prefers_the_system_record_over_a_window_title(phone):
    # The focused window's own title is shaped like a component.
    title = "com.example.shop/com.example.shop.Fake"
    assert (
        activity_of(phone, f"  mCurrentFocus=Window{{4f2a1c u0 {title}}}", REAL_APP)
        == ".Real"
    )


@pytest.mark.parametrize(
    "focus", ["  mCurrentFocus=Window{9 u0 PopupWindow:3f2a}", "  mCurrentFocus=null"]
)
def test_dump_activity_ignores_a_focus_line_forged_by_another_window(phone, focus):
    forged = f"x\n{FORGED_FOCUS}\n"
    assert activity_of(phone, title_window(forged), focus, REAL_APP) == ".Real"


@pytest.mark.parametrize(
    "focus", ["  mCurrentFocus=Window{9 u0 PopupWindow:3f2a}", "  mCurrentFocus=null"]
)
def test_dump_activity_with_a_forged_record_line_is_never_the_forgery(phone, focus):
    forged = f"x\n{FORGED_FOCUS}\n{FORGED_APP}\n"
    assert activity_of(phone, title_window(forged), focus, REAL_APP) is None


def test_dump_activity_does_not_hand_a_forged_record_over_to_the_window_title(phone):
    forged = f"x\n{FORGED_FOCUS}\n{FORGED_APP}\n"
    assert activity_of(phone, title_window(forged), REAL_APP) is None


REAL_FOCUS = "  mCurrentFocus=Window{4f2a1c u0 com.example.shop/com.example.shop.Real}"


def test_dump_activity_never_comes_from_a_window_title(phone):
    assert activity_of(phone, REAL_FOCUS) is None


@pytest.mark.parametrize(
    "app_line",
    [
        "  mFocusedApp=null",
        "  mFocusedApp=SomethingNew{1 u0 com.example.shop/.Fake t3}",
        None,
    ],
    ids=["null", "unrecognized", "absent"],
)
class TestForgedTitleWithoutAFocusedApp:
    def _lines(self, app_line):
        return [FORGED_FOCUS] + ([app_line] if app_line else [])

    def test_the_activity_manager_is_asked_and_wins(self, phone, app_line):
        script_dump(phone, dumpsys=dumpsys_with_focus(*self._lines(app_line)))
        phone.on_shell(
            SERIAL,
            "dumpsys activity activities",
            dumpsys_with_focus(
                "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Real t5}"
            ),
        )
        assert Device(adb_server=phone.address).dump().activity == ".Real"
        assert "shell:dumpsys activity activities" in phone.requests

    def test_no_usable_manager_record_gives_none(self, phone, app_line):
        script_dump(phone, dumpsys=dumpsys_with_focus(*self._lines(app_line)))
        phone.on_shell(
            SERIAL,
            "dumpsys activity activities",
            dumpsys_with_focus("  mResumedActivity: null"),
        )
        assert Device(adb_server=phone.address).dump().activity is None
        assert "shell:dumpsys activity activities" in phone.requests


@pytest.mark.parametrize(
    "line",
    [
        "mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3}",
        "    mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Ma in t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Ma\u202ein t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/ t3}",
        "  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.Main t3} x",
    ],
)
def test_dump_activity_needs_a_well_formed_line(phone, line):
    script_dump(phone, dumpsys=dumpsys_with_focus(line))
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_mismatch_between_focus_and_dumped_package(phone):
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  mCurrentFocus=Window{1 u0 com.bank/com.bank.Login}",
            "  mFocusedApp=ActivityRecord{1 u0 com.bank/.Login t3}",
        ),
    )
    assert Device(adb_server=phone.address).dump().activity is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "  mFocusedApp=ActivityRecord{110055750 u0 "
            "com.example.shop/.NexusLauncherActivity t2}",
            ".NexusLauncherActivity",
        ),
        (
            "  mFocusedApp=AppWindowToken{59b0789 token=Token{527f542 "
            "ActivityRecord{c10ff8d u0 com.example.shop/.Launcher t9}}}",
            ".Launcher",
        ),
    ],
)
def test_dump_activity_real_output_shapes(phone, line, expected):
    script_dump(phone, dumpsys=dumpsys_with_focus(line))
    assert Device(adb_server=phone.address).dump().activity == expected


def manager_activity(phone, *lines: str) -> str | None:
    script_dump(phone, dumpsys=dumpsys_with_focus("  mCurrentFocus=null"))
    phone.on_shell(SERIAL, "dumpsys activity activities", dumpsys_with_focus(*lines))
    return Device(adb_server=phone.address).dump().activity


REAL_RESUMED = (
    "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Main t5}",
    "    mResumedActivity: ActivityRecord{c1 u0 com.example.shop/.Main t5}",
)


def test_dump_activity_falls_back_to_the_activity_manager(phone):
    assert manager_activity(phone, *REAL_RESUMED) == ".Main"


def test_dump_activity_manager_of_another_package_is_not_accepted(phone):
    line = "  topResumedActivity=ActivityRecord{c1 u0 com.bank/.Login t5}"
    assert manager_activity(phone, line) is None


def test_dump_activity_manager_lines_that_disagree_are_not_accepted(phone):
    forged = "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Fake t5}"
    assert manager_activity(phone, forged, *REAL_RESUMED) is None


def test_dump_activity_manager_forgery_on_a_null_line_is_not_accepted(phone):
    forged = "  topResumedActivity=ActivityRecord{c1 u0 com.example.shop/.Fake t5}"
    assert manager_activity(phone, forged, "    mResumedActivity: null") is None


def test_dump_activity_never_hands_an_ambiguous_window_over_to_the_manager(phone):
    forged = f"x\n{FORGED_FOCUS}\n{FORGED_APP}\n"
    script_dump(phone, dumpsys=dumpsys_with_focus(title_window(forged), REAL_APP))
    phone.on_shell(
        SERIAL, "dumpsys activity activities", dumpsys_with_focus(*REAL_RESUMED)
    )
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_lines_must_start_with_the_field(phone):
    script_dump(
        phone,
        dumpsys=dumpsys_with_focus(
            "  title mCurrentFocus=Window{1 u0 com.bank/com.bank.Login}",
            "  see mFocusedApp=ActivityRecord{1 u0 com.bank/.Login t3}",
        ),
    )
    assert Device(adb_server=phone.address).dump().activity is None


def test_dump_activity_search_is_fast_on_a_hostile_title(phone):
    hostile = "  mCurrentFocus=Window{" + " a" * 300_000
    script_dump(
        phone,
        dumpsys=(hostile + "\n  mFocusedApp=" + " b" * 300_000 + "\n").encode(),
    )
    started = time.monotonic()
    assert Device(adb_server=phone.address).dump().activity is None
    assert time.monotonic() - started < 2


def test_dump_api_level_failure_does_not_lose_the_screen(phone):
    script_dump(phone)
    phone._shell[SERIAL].insert(
        0, ("getprop ro.build.version.sdk", Fail("getprop refused"))
    )
    screen = Device(adb_server=phone.address).dump()
    assert screen.api_level is None
    assert screen.package == "com.example.shop"
    assert screen.to_dict()["device"] == {"serial": SERIAL}


def slow_dump(server: FakeAdbServer, seconds: float) -> list[str]:
    """Make the dump command slow; return the commands the slow reply served.

    The fake server uses the first script that matches, so the slow reply is
    put in front (``first=True``) of the immediate one that `script_dump`
    registered.
    """
    served: list[str] = []

    def respond(command: str) -> Trickle:
        served.append(command)
        data = b"UI hierchary dumped to: /data/local/tmp/x.xml\n"
        return Trickle(data, chunk_size=len(data), lead=seconds)

    server.on_shell(
        SERIAL,
        re.compile(r"^uiautomator dump /data/local/tmp/uiprobe-"),
        respond,
        first=True,
    )
    return served


def test_a_slow_dump_outlasts_the_client_timeout(phone, monkeypatch):
    # The client's per-read timeout (10 s, shortened here) must not cut a dump
    # that is merely slow: uiautomator is silent until it has finished.
    monkeypatch.setitem(adb.AdbClient.__init__.__kwdefaults__, "timeout", 0.2)
    monkeypatch.setattr("uiprobe.device.DUMP_TIMEOUT", 5.0)
    script_dump(phone)
    served = slow_dump(phone, 0.6)
    started = time.perf_counter()
    assert Device(adb_server=phone.address).dump().package == "com.example.shop"
    assert len(served) == 1
    # perf_counter, not monotonic: Python 3.12's monotonic() moves in 15.6 ms
    # steps on Windows and read this 0.6 s dump as 0.594 s. The bound stays
    # below 0.6 s because the fake server's Event.wait (Trickle.lead) can end
    # slightly early on Windows timers; 0.5 s is still far above the 0.2 s
    # client timeout.
    assert time.perf_counter() - started >= 0.5


def test_a_dump_that_never_finishes_is_a_dump_error_not_an_adb_error(
    phone, monkeypatch
):
    monkeypatch.setattr("uiprobe.device.DUMP_TIMEOUT", 0.2)
    script_dump(phone)
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), Stall())
    phone._shell[SERIAL].insert(0, phone._shell[SERIAL].pop())
    with pytest.raises(DumpError) as caught:
        Device(adb_server=phone.address).dump()
    assert caught.value.code == "dump_failed"
    assert caught.value.exit_code == 11
    assert caught.value.details == {"reason": "timeout", "timeout_seconds": 0.2}
    assert isinstance(caught.value.__cause__, uiprobe.AdbError)
    assert caught.value.__cause__.details["stage"] == "output"
    assert len(requests_of(phone, "shell:rm -f ")) == 1


def test_a_handshake_timeout_during_the_dump_stays_an_adb_error(phone, monkeypatch):
    monkeypatch.setitem(adb.AdbClient.__init__.__kwdefaults__, "timeout", 0.2)
    script_dump(phone)
    device = Device(adb_server=phone.address)
    phone.stall(f"host:transport:{SERIAL}")
    with pytest.raises(uiprobe.AdbError) as caught:
        device.dump()
    assert not isinstance(caught.value, DumpError)
    assert caught.value.exit_code == 10
    assert caught.value.details["reason"] == "timeout"
    assert caught.value.details["request"] == f"host:transport:{SERIAL}"


def test_a_stalled_shell_okay_during_the_dump_stays_an_adb_error(phone, monkeypatch):
    # The shell service never answers OKAY: nothing has been read from
    # uiautomator yet, so this is a stalled handshake and not a slow dump.
    monkeypatch.setitem(adb.AdbClient.__init__.__kwdefaults__, "timeout", 0.2)
    monkeypatch.setattr("secrets.token_hex", lambda _nbytes: "0123456789abcdef")
    script_dump(phone)
    request = "shell:uiautomator dump /data/local/tmp/uiprobe-0123456789abcdef.xml"
    phone.stall(request)
    device = Device(adb_server=phone.address)
    with pytest.raises(uiprobe.AdbError) as caught:
        device.dump()
    assert not isinstance(caught.value, DumpError)
    assert caught.value.exit_code == 10
    assert caught.value.details["reason"] == "timeout"
    assert caught.value.details["request"] == request
    assert caught.value.details["timeout_seconds"] == 0.2
    assert "stage" not in caught.value.details


def test_other_adb_errors_during_the_dump_stay_adb_errors(phone):
    script_dump(phone)
    phone.on_shell(SERIAL, re.compile(r"^uiautomator dump "), Fail("closed"))
    phone._shell[SERIAL].insert(0, phone._shell[SERIAL].pop())
    with pytest.raises(uiprobe.AdbError) as caught:
        Device(adb_server=phone.address).dump()
    assert not isinstance(caught.value, DumpError)


def test_the_dump_command_gets_the_dump_timeout_and_other_commands_do_not(
    phone, monkeypatch
):
    script_dump(phone)
    device = Device(adb_server=phone.address)
    seen: dict[str, float | None] = {}
    real = device._client.shell

    def spy(serial, command, *, timeout=None):
        seen[command.split()[0]] = timeout
        return real(serial, command, timeout=timeout)

    monkeypatch.setattr(device._client, "shell", spy)
    device.dump()
    assert seen["uiautomator"] == 60.0
    assert seen["dumpsys"] is None
    assert seen["rm"] is None


def test_dump_rejects_hostile_xml_with_dump_error(phone):
    script_dump(phone)
    phone._exec[SERIAL].clear()
    phone.on_exec(
        SERIAL,
        re.compile(r"^cat "),
        b'<?xml version="1.0" encoding="bogus"?><hierarchy/>',
    )
    with pytest.raises(DumpError):
        Device(adb_server=phone.address).dump()


# --- argument types are checked before the adb server is contacted ---


@pytest.mark.parametrize("serial", [5554, b"emulator-5554", ["a"]])
def test_device_rejects_a_serial_that_is_not_a_string_before_any_io(
    serial, connections
):
    with pytest.raises(TypeError, match="serial must be a string"):
        Device(serial, adb_server=unused_address())
    assert connections == []


@pytest.mark.parametrize("start_server", [1, 0, "no", None])
def test_device_rejects_a_start_server_that_is_not_a_bool_before_any_io(
    start_server, connections
):
    with pytest.raises(TypeError, match="start_server must be a bool"):
        Device(adb_server=unused_address(), start_server=start_server)
    assert connections == []


@pytest.mark.parametrize("adb_server", [5037, b"tcp:127.0.0.1:1", ["x"]])
def test_device_rejects_an_adb_server_that_is_not_a_string_before_any_io(
    adb_server, connections
):
    with pytest.raises(TypeError, match="adb_server must be a string"):
        Device(adb_server=adb_server)
    assert connections == []


def test_device_rejects_a_bad_adb_server_value_before_any_io(connections):
    with pytest.raises(ValueError, match="invalid address"):
        Device(adb_server="127.0.0.1:1")
    assert connections == []


@pytest.mark.parametrize("function", [devices, device_details])
def test_listing_rejects_wrong_argument_types_before_any_io(function, connections):
    address = unused_address()
    with pytest.raises(TypeError, match="start_server must be a bool"):
        function(adb_server=address, start_server=1)
    with pytest.raises(TypeError, match="adb_server must be a string"):
        function(adb_server=5037)
    assert connections == []


def test_wrong_types_never_start_adb_or_reach_a_running_server(phone, processes):
    with pytest.raises(TypeError):
        Device(5554, adb_server=phone.address)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        devices(adb_server=phone.address, start_server="yes")  # ty: ignore[invalid-argument-type]
    assert phone.requests == []
    assert processes == []


# --- package exports ---


def test_package_exports():
    for name in uiprobe.__all__:
        assert hasattr(uiprobe, name)
    assert uiprobe.Device is Device
    assert issubclass(uiprobe.DumpError, uiprobe.UiprobeError)


def _after_xml_is_read(monkeypatch, action):
    """Run `action` once the XML has been read, before the metadata lookups."""
    real = Device._dump_xml

    def dump_xml(self):
        xml = real(self)
        action()
        return xml

    monkeypatch.setattr(Device, "_dump_xml", dump_xml)


def test_dump_keeps_the_screen_when_the_server_stops_after_the_xml(phone, monkeypatch):
    script_dump(phone)
    device = Device(adb_server=phone.address, start_server=False)
    _after_xml_is_read(monkeypatch, phone.stop)
    screen = device.dump()
    assert (screen.activity, screen.api_level) == (None, None)
    assert screen.package == "com.example.shop"
    assert len(screen.elements) > 0


@pytest.mark.parametrize("state", ["offline", "unauthorized"])
def test_dump_keeps_the_screen_when_the_device_changes_state_after_the_xml(
    phone, monkeypatch, state
):
    script_dump(phone)
    device = Device(adb_server=phone.address)

    def change_state():
        phone.devices[0]["state"] = state

    _after_xml_is_read(monkeypatch, change_state)
    screen = device.dump()
    assert (screen.activity, screen.api_level) == (None, None)
    assert len(screen.elements) > 0


def test_dump_keeps_the_screen_when_the_device_disappears_after_the_xml(
    phone, monkeypatch
):
    script_dump(phone)
    device = Device(adb_server=phone.address)
    _after_xml_is_read(monkeypatch, phone.devices.clear)
    screen = device.dump()
    assert (screen.activity, screen.api_level) == (None, None)
    assert len(screen.elements) > 0


def test_api_level_is_none_when_the_server_or_device_is_gone(phone):
    script_dump(phone)
    device = Device(adb_server=phone.address, start_server=False)
    later = Device(adb_server=phone.address, start_server=False)
    phone.devices[0]["state"] = "offline"
    assert device.api_level() is None
    phone.devices[0]["state"] = "device"
    assert device.api_level() == 36
    phone.stop()
    assert later.api_level() is None
