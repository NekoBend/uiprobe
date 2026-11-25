"""Tests for what the selector commands read and for the hold of a tap."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from uiprobe.device import Device
from uiprobe.errors import (
    ElementNotVisibleError,
    InputFailedError,
)

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.model import Element
    from uiprobe.model import Screen as ScreenModel
from tests.device.tap.helpers import (
    COVER_SERIAL,
    LONG_COMMAND,
    MARKER_FORMS,
    SHOP,
    TAP_COMMAND,
    element,
    invoke,
    sent_inputs,
)

PLAIN_TAP_COMMAND = "input tap 80 35; echo uiprobe-exit:$?"


# --- the selector commands read only what their checks need ---


def _commands(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands]


@pytest.mark.usefixtures("android")
def test_cli_tap_by_text_sends_exactly_the_commands_it_needs(capsys, fake_adb):
    code, _, err = invoke(capsys, fake_adb, "tap", "--text", "Login")
    assert (code, err) == (0, "")
    commands = _commands(fake_adb)
    assert [c.split(" ")[0] for c in commands] == [
        "uiautomator",
        "cat",
        "rm",
        "wm",
        "input",
    ]
    assert commands[3] == "wm size"
    assert commands[4] == TAP_COMMAND
    assert not any(c.startswith(("dumpsys window", "getprop")) for c in commands)


@pytest.mark.usefixtures("android")
def test_cli_dump_still_sends_the_activity_and_api_level_reads(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "dump", "--json")
    assert code == 0
    commands = _commands(fake_adb)
    assert any(c.startswith("dumpsys window") for c in commands)
    assert "getprop ro.build.version.sdk" in commands
    payload = json.loads(out)
    assert payload["app"]["activity"] == ".Main"
    assert payload["device"]["api_level"] == 36
    code, out, _ = invoke(capsys, fake_adb, "dump")
    assert out.splitlines()[0].startswith(f"{SHOP}/.Main  (s-")


def test_dump_without_details_skips_the_two_reads(device, fake_adb):
    screen = device.dump(details=False)
    assert (screen.activity, screen.api_level) == (None, None)
    assert screen.display == (320, 640)
    commands = _commands(fake_adb)
    assert not any(c.startswith(("dumpsys window", "getprop")) for c in commands)
    assert "wm size" in commands
    data = screen.to_dict()
    assert "activity" not in data["app"]
    assert "api_level" not in data["device"]


def test_dump_by_default_fills_activity_and_api_level(device):
    screen = device.dump()
    assert screen.activity == ".Main"
    assert screen.api_level == 36
    assert device.dump(details=True).activity == ".Main"


@pytest.mark.usefixtures("cover")
def test_a_light_dump_gives_the_same_tap_checks_as_a_full_one(fake_adb):
    device = Device(COVER_SERIAL, adb_server=fake_adb.address)
    light = device.dump(details=False)
    full = device.dump()

    def outcome(screen: ScreenModel, element: Element) -> object:
        try:
            return screen.tap_point(element)
        except ElementNotVisibleError as error:
            return error.details["reason"]

    results = [
        (outcome(light, a), outcome(full, b))
        for a, b in zip(light.elements, full.elements, strict=True)
    ]
    assert all(one == other for one, other in results)
    assert {"covered", "too_small"} <= {r for r, _ in results if isinstance(r, str)}


@pytest.mark.parametrize("details", ["False", 0, None])
def test_dump_details_must_be_a_bool(device, fake_adb, details):
    before = len(fake_adb.commands)
    with pytest.raises(TypeError, match="details must be a bool"):
        device.dump(details=details)
    assert len(fake_adb.commands) == before


# --- the hold of a tap (press_ms, --press-ms) ---


def test_default_tap_is_a_10_ms_hold_in_place(device, android, fake_adb):
    assert device.tap((80, 35)) == (80, 35)
    assert sent_inputs(fake_adb) == ["input swipe 80 35 80 35 10; echo uiprobe-exit:$?"]
    assert [(i.kind, i.point) for i in android.inputs] == [("tap", (80, 35))]
    assert android.stack == ["main", "next"]


def test_press_ms_zero_sends_the_plain_input_tap(device, android, fake_adb):
    assert device.tap((80, 35), press_ms=0) == (80, 35)
    assert sent_inputs(fake_adb) == [PLAIN_TAP_COMMAND]
    assert [(i.kind, i.point) for i in android.inputs] == [("tap", (80, 35))]


@pytest.mark.parametrize("ms", [1, 10, 50, 200])
def test_press_ms_is_sent_as_the_hold(device, android, fake_adb, ms):
    device.tap((80, 35), press_ms=ms)
    assert sent_inputs(fake_adb) == [
        f"input swipe 80 35 80 35 {ms}; echo uiprobe-exit:$?"
    ]
    assert [i.kind for i in android.inputs] == ["tap"]


def test_a_held_tap_with_an_element_presses_the_same_point(device, android, screen):
    assert device.tap(element(screen, "Login"), press_ms=30) == (80, 35)
    assert [i.point for i in android.inputs] == [(80, 35)]


def test_long_keeps_the_800_ms_command(device, fake_adb):
    device.tap((80, 35), long=True)
    assert sent_inputs(fake_adb) == [LONG_COMMAND]


def test_long_with_the_default_press_ms_is_still_long(device, fake_adb):
    device.tap((80, 35), long=True, press_ms=10)
    assert sent_inputs(fake_adb) == [LONG_COMMAND]


@pytest.mark.parametrize("ms", [0, 11, 200])
def test_long_with_another_press_ms_is_refused(device, fake_adb, ms):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="long"):
        device.tap((80, 35), long=True, press_ms=ms)
    assert len(fake_adb.requests) == before


@pytest.mark.parametrize("ms", [-1, 201, 800, 10**9])
def test_press_ms_out_of_range_is_refused_before_sending(device, fake_adb, ms):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="press_ms"):
        device.tap((80, 35), press_ms=ms)
    assert len(fake_adb.requests) == before


@pytest.mark.parametrize("ms", [True, False, 1.0, "10", None, [10]])
def test_press_ms_of_the_wrong_type_is_refused_before_sending(device, fake_adb, ms):
    before = len(fake_adb.requests)
    with pytest.raises(TypeError, match="press_ms"):
        device.tap((80, 35), press_ms=ms)  # type: ignore[arg-type]
    assert len(fake_adb.requests) == before


def test_press_ms_takes_the_int_value_of_an_int_subclass(device, fake_adb):
    class Odd(int):
        def __str__(self) -> str:
            return "7"

    device.tap((80, 35), press_ms=Odd(25))
    assert sent_inputs(fake_adb) == ["input swipe 80 35 80 35 25; echo uiprobe-exit:$?"]


def test_a_held_tap_failure_reports_the_tap_command(device, android):
    android.answer_input(status=1, output="Error\n")
    with pytest.raises(InputFailedError) as info:
        device.tap((80, 35), press_ms=40)
    assert info.value.details["command"] == "tap"
    assert len(android.inputs) == 1


def test_cli_tap_default_is_a_10_ms_hold(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "tap", "--xy", "80", "35")
    assert (code, out, err) == (0, "tapped 80,35\n", "")
    assert sent_inputs(fake_adb) == ["input swipe 80 35 80 35 10; echo uiprobe-exit:$?"]
    assert android.stack == ["main", "next"]


@pytest.mark.usefixtures("android")
def test_cli_tap_press_ms_zero_sends_the_plain_tap(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "tap", "--xy", "80", "35", "--press-ms", "0"
    )
    assert (code, out) == (0, "tapped 80,35\n")
    assert sent_inputs(fake_adb) == [PLAIN_TAP_COMMAND]


@pytest.mark.usefixtures("android")
def test_cli_tap_press_ms_50(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--id", "login", "--press-ms", "50")
    assert code == 0
    assert out == 'tapped e2 TextView "Login" id=login [tap] at 80,35\n'
    assert sent_inputs(fake_adb) == ["input swipe 80 35 80 35 50; echo uiprobe-exit:$?"]


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("ms", ["0", "10", "200"])
def test_cli_tap_json_does_not_change_with_press_ms(capsys, fake_adb, ms):
    code, out, err = invoke(
        capsys, fake_adb, "tap", "--xy", "1", "2", "--press-ms", ms, "--json"
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "tap",
        "point": [1, 2],
    }


@pytest.mark.usefixtures("android")
def test_cli_tap_long_is_unchanged_by_the_new_option(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--xy", "80", "35", "--long")
    assert (code, out) == (0, "long-tapped 80,35\n")
    assert sent_inputs(fake_adb) == [LONG_COMMAND]


@pytest.mark.parametrize("ms", ["0", "10", "20"])
def test_cli_tap_long_with_press_ms_is_a_usage_error(capsys, fake_adb, android, ms):
    code, out, _ = invoke(
        capsys, fake_adb, "tap", "--xy", "1", "2", "--long", "--press-ms", ms, "--json"
    )
    assert code == 2
    error = json.loads(out)["error"]
    assert error["code"] == "usage_error"
    assert "--press-ms cannot be combined with --long" in error["message"]
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.parametrize(
    "value",
    [
        "-1",
        "201",
        "1000",
        "1.5",
        "+5",
        "",
        " 5",
        "5 ",
        "0x10",
        "\N{ARABIC-INDIC DIGIT FIVE}",
        "ten",
    ],
)
def test_cli_tap_bad_press_ms_is_a_usage_error(capsys, fake_adb, android, value):
    code, out, _ = invoke(
        capsys, fake_adb, "tap", "--xy", "1", "2", "--press-ms", value, "--json"
    )
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.parametrize("values", [("201", "10"), ("10", "201")])
def test_cli_tap_every_repeated_press_ms_is_checked(capsys, fake_adb, android, values):
    code, out, _ = invoke(
        capsys,
        fake_adb,
        "tap",
        "--xy",
        "1",
        "2",
        "--press-ms",
        values[0],
        "--press-ms",
        values[1],
        "--json",
    )
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


def test_cli_tap_repeated_valid_press_ms_uses_the_last(capsys, fake_adb, android):
    code, _, _ = invoke(
        capsys, fake_adb, "tap", "--xy", "1", "2", "--press-ms", "5", "--press-ms", "7"
    )
    assert code == 0
    assert sent_inputs(fake_adb) == ["input swipe 1 2 1 2 7; echo uiprobe-exit:$?"]
    assert len(android.inputs) == 1


@pytest.mark.parametrize(
    ("command", "matches"),
    [
        ("input swipe 1 2 1 2 1", True),
        ("input swipe 1 2 1 2 200", True),
        ("input swipe 1 2 1 2 0", False),
        ("input swipe 1 2 1 2 01", False),
        ("input swipe 1 2 1 2 010", False),
        ("input swipe 1 2 1 2 201", False),
    ],
)
def test_held_tap_marker_pattern_is_canonical_one_to_200(command, matches):
    held = MARKER_FORMS[2]
    assert bool(held.match(f"{command}; echo uiprobe-exit:$?")) is matches
