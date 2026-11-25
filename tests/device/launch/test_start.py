"""Tests for the start: real outputs and the success rule."""

from __future__ import annotations

import re

import pytest

from tests.device.launch.helpers import (
    LAUNCHER,
    MARK,
    OK_36,
    RESOLVE,
    RESOLVE_PREFIX,
    SCREENS,
    SETTINGS,
    SHOP,
    START,
    capture,
    sent,
    start_of,
)
from tests.fake_adb_server import Reset
from tests.fake_device import FakeAndroid
from uiprobe.device import Device
from uiprobe.errors import AdbError, LaunchFailedError

# --- the start: real outputs ---


@pytest.mark.parametrize(
    ("resolve", "start", "marker"),
    [
        ("emu36-resolve-settings.txt", "emu36-start-after-force-stop.txt", None),
        ("emu36-resolve-settings.txt", "emu36-start-running.txt", None),
        ("emu24-resolve-settings.txt", "emu24-start-after-force-stop.txt", None),
        ("emu24-resolve-settings.txt", "emu24-start-running.txt", None),
        ("emu36-resolve-settings.txt", "emu36-start-warm.txt", None),
        ("emu36-resolve-settings.txt", "emu36-start-cold-again.txt", None),
        ("samsung36-resolve-settings.txt", "samsung36-start-running.txt", True),
        (
            "samsung36-resolve-settings.txt",
            "samsung36-start-after-force-stop.txt",
            True,
        ),
    ],
)
def test_real_successful_starts_are_accepted(
    device, android, fake_adb, resolve, start, marker
):
    marker_r = True if resolve.startswith("samsung") else None  # captured without
    android.override(re.compile(r"^cmd package"), capture(resolve, marker=marker_r))
    android.override(re.compile(r"^am start"), capture(start, marker=marker))
    android.show("homepage")  # what the scripted start brought to the front
    device.launch(SETTINGS)
    assert sent(fake_adb) == [
        RESOLVE_PREFIX + SETTINGS + MARK,
        start_of(f"{SETTINGS}/.Settings"),
    ]


@pytest.mark.parametrize(
    ("name", "marker", "status"),
    [
        ("emu36-start-no-such-class.txt", None, 1),
        ("emu24-start-no-such-class.txt", None, None),  # exit status 0 on API 24
        ("samsung36-start-no-such-class.txt", True, None),
    ],
)
def test_real_error_outputs_fail_whatever_the_exit_status(
    device, android, name, marker, status
):
    android.override(re.compile(r"^am start"), capture(name, marker=marker))
    android.show("settings")  # even with the app in front
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SETTINGS)
    details = info.value.details
    output = details["output"]
    assert details["reason"] == "start_failed"
    assert isinstance(output, str)
    assert output.startswith("Error: Activity class {com.android.settings/")
    assert output.endswith("} does not exist.")
    assert details.get("exit_status") == status
    assert info.value.exit_code == 15


def test_a_start_that_names_another_apps_activity_fails_a_launch(
    device, android, fake_adb
):
    # Captured on an API 36 emulator: Settings' search screen (another
    # package) stayed on top of the task after am force-stop.
    # The app does not come to the front during the wait that follows.
    android.override(
        re.compile(r"^am start"), capture("emu36-start-other-activity.txt")
    )
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SETTINGS, timeout=1)
    assert info.value.details == {
        "package": SETTINGS,
        "reason": "other_activity",
        "output": "Activity: com.google.android.settings.intelligence"
        "/.modules.search.activity.SearchActivity",
        "activity": "com.google.android.settings.intelligence"
        "/.modules.search.activity.SearchActivity",
    }
    assert info.value.exit_code == 15
    assert sent(fake_adb) == [
        RESOLVE_PREFIX + SETTINGS + MARK,
        start_of(f"{SETTINGS}/.Settings"),
    ]


def test_a_start_that_names_another_apps_activity_waits_for_the_app(
    device, android, fake_adb, clock, monkeypatch
):
    # The same reply, but the app comes to the front after 2 s: as with any
    # start, the launch waits for it and succeeds.
    android.override(
        re.compile(r"^am start"), capture("emu36-start-other-activity.txt")
    )
    original = device.foreground

    def comes_back() -> str | None:
        if clock.now >= 1002.0:
            android.show("settings")
        return original()

    monkeypatch.setattr(device, "foreground", comes_back)
    assert device.launch(SETTINGS) is None
    assert clock.pauses == [0.5, 0.5, 0.5, 0.5]
    assert sent(fake_adb) == [
        RESOLVE_PREFIX + SETTINGS + MARK,
        start_of(f"{SETTINGS}/.Settings"),
    ]


@pytest.mark.parametrize(
    "activity",
    [
        "com.example.other/.Main",
        "com.example.shopx/.Main",
        "android/com.android.internal.app.ResolverActivity",
    ],
)
def test_a_start_naming_another_app_fails_only_when_the_app_stays_behind(
    device, android, activity
):
    output = OK_36.replace("{activity}", activity).encode()
    android.override(re.compile(r"^am start"), output)
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP, timeout=1)  # the app never comes to the front
    assert info.value.details["reason"] == "other_activity"
    assert info.value.details["output"] == f"Activity: {activity}"
    android.show("main")
    assert device.launch(SHOP) is None  # in front: a success like any start


# --- the start: the success rule ---


def scripted_start(android: FakeAndroid, output: bytes) -> None:
    android.override(re.compile(r"^am start"), output)
    android.show("main")  # the foreground check alone would pass


@pytest.mark.parametrize(
    "activity",
    [f"{SHOP}/.Main", f"{SHOP}/.Other", f"{SHOP}/{SHOP}.Main", f"{SHOP}/.a.B$C"],
)
def test_the_reported_activity_may_have_another_class(device, android, activity):
    scripted_start(android, OK_36.replace("{activity}", activity).encode())
    device.launch(SHOP)


@pytest.mark.parametrize(
    ("output", "reason", "shown"),
    [
        # not a component of any package
        (
            OK_36.replace("{activity}", "shop/.Main"),
            "unexpected_output",
            "Activity: shop/.Main",
        ),
        (
            OK_36.replace("{activity}", "com.example.other/.Ma in"),
            "unexpected_output",
            "Activity: com.example.other/.Ma in",
        ),
        (
            OK_36.replace("{activity}", "com.example.other/"),
            "unexpected_output",
            "Activity: com.example.other/",
        ),
        (
            OK_36.replace("{activity}", SHOP),
            "unexpected_output",
            f"Activity: {SHOP}",
        ),
        (
            OK_36.replace("{activity}", f"{SHOP}/.Main extra"),
            "unexpected_output",
            f"Activity: {SHOP}/.Main extra",
        ),
        (
            OK_36.replace("Activity: {activity}", f"Activity:{SHOP}/.Main"),
            "unexpected_output",
            f"Activity:{SHOP}/.Main",
        ),
        # missing or repeated lines
        (
            OK_36.replace("Activity: {activity}\n", ""),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace(
                "Activity: {activity}\n",
                f"Activity: {SHOP}/.Main\nActivity: {SHOP}/.Main\n",
            ),
            "unexpected_output",
            f"Activity: {SHOP}/.Main",
        ),
        (
            OK_36.replace("Status: ok\n", ""),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: ok\nStatus: ok\n"),
            "unexpected_output",
            "Status: ok",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: timeout\n"),
            "unexpected_output",
            "Status: timeout",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: ok \n"),
            "unexpected_output",
            "Status: ok ",
        ),
        # Error lines fail whatever the exit status
        (
            OK_36.replace("Complete\n", "Complete\nError: it broke\n"),
            "start_failed",
            "Error: it broke",
        ),
        (
            OK_36.replace("Status: ok\n", "Error type 3\nStatus: ok\n"),
            "start_failed",
            "Error type 3",
        ),
        # the exit status and its marker
        (
            OK_36.replace("uiprobe-exit:0", "uiprobe-exit:1"),
            "start_failed",
            "Starting: Intent",
        ),
        (
            OK_36.replace("uiprobe-exit:0\n", ""),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36.replace("uiprobe-exit:0", "uiprobe-exit:x"),
            "unexpected_output",
            "Starting: Intent",
        ),
        (
            OK_36 + "more\n",
            "unexpected_output",
            "Starting: Intent",
        ),
        # an unknown line
        (
            OK_36.replace("Complete\n", "Complete\nhello\n"),
            "unexpected_output",
            "hello",
        ),
        (
            OK_36.replace("Status: ok\n", "Status: ok\x0bActivity: x\n"),
            "unexpected_output",
            "Status: ok\\x0bActivity: x",
        ),
        ("uiprobe-exit:0\n", "unexpected_output", ""),
        ("", "unexpected_output", ""),
    ],
)
def test_the_start_must_report_the_known_success(
    device, android, fake_adb, output, reason, shown
):
    scripted_start(android, output.replace("{activity}", f"{SHOP}/.Main").encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    details = info.value.details
    assert details["reason"] == reason
    assert details["package"] == SHOP
    output = details["output"]
    assert isinstance(output, str)
    assert output.startswith(shown)
    assert sent(fake_adb) == [RESOLVE, START]  # once, never again


@pytest.mark.parametrize(
    ("change", "shown"),
    [
        # the echoed component must be the one sent
        (
            (f"cmp={SHOP}/.Main }}", "cmp=com.example.other/.A }"),
            "Starting: Intent { act=android.intent.action.MAIN",
        ),
        ((f"cmp={SHOP}/.Main }}", f"cmp={SHOP}/.Other }}"), "Starting: Intent"),
        ((f"cmp={SHOP}/.Main }}", f"cmp={SHOP}/.Main"), "Starting: Intent"),
        (("Starting: Intent { act", "Starting: Intent {  act"), "Starting: Intent"),
        # no start line at all
        (("Starting:", "Started:"), "Started:"),
        # a Warning only in a captured form
        (("Status: ok\n", "Warning: task\nStatus: ok\n"), "Warning: task"),
        (
            (
                "Status: ok\n",
                "Warning: Activity not started, its current task has been"
                " brought to the front.\nStatus: ok\n",
            ),
            "Warning: Activity not started, its current task has been brought",
        ),
        # control characters left after the CR LF end is cut
        (("Status: ok\n", "Warning: task\rError: failed\nStatus: ok\n"), "Warning"),
        (("TotalTime: 5", "TotalTime: 5\x1b[2J"), "TotalTime: 5\\x1b"),
        (("Complete", "Complete\r\r"), "Complete\\r"),
        (("Complete", "Comp\x00lete"), "Comp\\x00lete"),
        (("Complete", "Complete\x85"), "Complete\\x85"),
        # the informational lines in their known shapes, each once
        (("TotalTime: 5", "TotalTime: soon"), "TotalTime: soon"),
        (("LaunchState: COLD", "LaunchState: cold"), "LaunchState: cold"),
        (("WaitTime: 6\n", "WaitTime: 6\nWaitTime: 6\n"), "WaitTime: 6"),
        (("Complete\n", "Complete\nComplete\n"), "Complete"),
        (("Status: ok\n", "Status: ok\nWarning: x\n"), "Warning: x"),
    ],
)
def test_the_start_reply_must_hold_only_the_known_lines(
    device, android, fake_adb, change, shown
):
    old, new = change
    output = OK_36.replace("{activity}", f"{SHOP}/.Main")
    assert old in output
    scripted_start(android, output.replace(old, new, 1).encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["reason"] == "unexpected_output"
    output_shown = info.value.details["output"]
    assert isinstance(output_shown, str)
    assert output_shown.startswith(shown)
    assert sent(fake_adb) == [RESOLVE, START]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Status: ok\n", "Warning: task\rError: failed\nStatus: ok\n"),
        ("Complete", "Error: x\x1b[2J"),
        ("Complete", "Complete\r\r"),
    ],
)
def test_a_control_character_fails_the_start_as_such(device, android, old, new):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main").replace(old, new, 1)
    scripted_start(android, output.encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["reason"] == "unexpected_output"
    assert "printed a control character" in info.value.message
    assert "\x1b" not in info.value.message
    assert "\r" not in info.value.message


@pytest.mark.parametrize(
    "warning",
    [
        "Warning: Activity not started, intent has been delivered to currently"
        " running top-most instance.",
        "Warning: Activity not started, its current task has been brought to the front",
    ],
)
def test_the_captured_warnings_are_accepted(device, android, warning):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main")
    output = output.replace("Status: ok", f"{warning}\nStatus: ok")
    scripted_start(android, output.encode())
    device.launch(SHOP)


@pytest.mark.parametrize(
    ("state", "ok"),
    [
        # only the values seen
        ("COLD", True),
        ("WARM", True),
        ("HOT", True),
        ("UNKNOWN (0)", True),
        ("RELAUNCH", False),
        ("hot", False),
        ("FAILED", False),
        ("UNKNOWN (-1)", False),
        ("UNKNOWN (1)", False),
        ("UNKNOWN", False),
        ("cold", False),
        ("COLD extra", False),
        ("UNKNOWN (x)", False),
        ("", False),
    ],
)
def test_the_launch_state_line_has_one_shape(device, android, state, ok):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main").replace(
        "LaunchState: COLD", f"LaunchState: {state}"
    )
    scripted_start(android, output.encode())
    if ok:
        device.launch(SHOP)
        return
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["reason"] == "unexpected_output"


def test_an_unknown_launch_state_names_the_value(device, android):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main").replace(
        "LaunchState: COLD", "LaunchState: RELAUNCH"
    )
    scripted_start(android, output.encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["output"] == "LaunchState: RELAUNCH"
    assert "LaunchState value it does not know" in str(info.value)


def test_the_hot_start_of_settings_on_api_36_is_a_success(device, android):
    # The line "LaunchState: HOT" is verbatim from the reply of a settings36
    # emulator (API 36) on 2026-11-21, when Settings' task was already on top;
    # the other lines follow the captured API 36 shape.
    activity = f"{SETTINGS}/.homepage.SettingsHomepageActivity"
    output = (
        "Starting: Intent { act=android.intent.action.MAIN"
        f" cat=[{LAUNCHER}] cmp={SETTINGS}/.Settings }}\n"
        f"Status: ok\nLaunchState: HOT\nActivity: {activity}\n"
        "TotalTime: 0\nWaitTime: 3\nComplete\nuiprobe-exit:0\n"
    )
    android.override(re.compile(r"^am start"), output.encode())
    android.override(
        re.compile(r"^cmd package"),
        f"{SETTINGS}/.Settings\nuiprobe-exit:0\n".encode(),
    )
    android.show("homepage")
    device.launch(SETTINGS)


def test_the_echo_may_name_the_full_spelling_sent(device, android, fake_adb):
    # A full class name sent is echoed in the short form, and the reverse.
    output = OK_36.replace("{activity}", f"{SHOP}/.Main")
    scripted_start(android, output.encode())
    full_read = f"{SHOP}/{SHOP}.Main\nuiprobe-exit:0\n".encode()
    android.override(re.compile(r"^cmd package"), full_read)
    device.launch(SHOP)  # the launcher read gave the full spelling
    full = output.replace(f"cmp={SHOP}/.Main", f"cmp={SHOP}/{SHOP}.Main")
    scripted_start(android, full.encode())
    android.override(
        re.compile(r"^cmd package"), f"{SHOP}/.Main\nuiprobe-exit:0\n".encode()
    )
    device.launch(SHOP)
    starts = [c for c in sent(fake_adb) if c.startswith("am start")]
    assert starts == [start_of(f"{SHOP}/{SHOP}.Main"), START]


def test_a_non_zero_exit_status_is_reported(device, android):
    scripted_start(android, b"Starting: x\nuiprobe-exit:255\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details == {
        "package": SHOP,
        "reason": "start_failed",
        "output": "Starting: x",
        "exit_status": 255,
    }
    assert "exit status 255" in info.value.message


def test_the_start_accepts_crlf_and_empty_lines(device, android):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main").replace("\n", "\r\n\r\n")
    scripted_start(android, output.encode())
    device.launch(SHOP)


def test_the_start_failure_escapes_what_it_shows(device, android):
    scripted_start(android, b"Error: \x1b]0;x\x07 oops\nuiprobe-exit:0\n")
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["output"] == "Error: \\x1b]0;x\\x07 oops"
    assert "\x1b" not in info.value.message


@pytest.mark.parametrize("separator", ["\v", "\f", "\x1c", "\x85", "\u2028"])
def test_only_a_line_feed_ends_a_line(device, android, separator):
    output = OK_36.replace("{activity}", f"{SHOP}/.Main").replace(
        "\nStatus: ok", f"{separator}Status: ok"
    )
    scripted_start(android, output.encode())
    with pytest.raises(LaunchFailedError) as info:
        device.launch(SHOP)
    assert info.value.details["reason"] == "unexpected_output"


def test_the_fake_answers_the_api_24_and_36_shapes(fake_adb):
    for api, serial in ((24, "emulator-5556"), (36, "emulator-5558")):
        FakeAndroid(fake_adb, serial, SCREENS, start="launcher", api=api)
        phone = Device(serial, adb_server=fake_adb.address)
        phone.launch(SHOP)
        phone.launch(SHOP)  # running: a Warning line
        assert phone.foreground() == SHOP


def test_launch_does_not_resend_after_a_connection_error(device, android, fake_adb):
    android.override(re.compile(r"^am start"), Reset())
    before = len(fake_adb.commands)
    with pytest.raises(AdbError):
        device.launch(SHOP)
    assert sent(fake_adb, before) == [RESOLVE, START]
