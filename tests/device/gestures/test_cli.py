"""Tests for the ``swipe``, ``scroll`` and ``key`` commands."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.device.gestures.helpers import (
    SHOP,
    SUFFIX,
    SWIPE_COMMAND,
    invoke,
    sent_inputs,
)
from tests.fake_device import (
    assert_no_resend_after_drop,
)
from uiprobe import cli
from uiprobe.device import (
    Device,
    DisplaySizeUnknownError,
    _parse_display_size,
)

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer


def test_cli_swipe_from_to(capsys, fake_adb, android):
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--from", "10", "20", "--to", "30", "40"
    )
    assert (code, err) == (0, "")
    assert out == "swiped 10,20 -> 30,40 in 300 ms\n"
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]
    assert android.dumps == 0


@pytest.mark.usefixtures("android")
def test_cli_swipe_ms(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--from", "0", "0", "--to", "5", "5", "--ms", "1500"
    )
    assert (code, out) == (0, "swiped 0,0 -> 5,5 in 1500 ms\n")
    assert sent_inputs(fake_adb) == ["input swipe 0 0 5 5 1500" + SUFFIX]


@pytest.mark.usefixtures("android")
def test_cli_swipe_json_for_points(capsys, fake_adb):
    code, out, err = invoke(
        capsys,
        fake_adb,
        "swipe",
        "--from",
        "10",
        "20",
        "--to",
        "30",
        "40",
        "--ms",
        "250",
        "--json",
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 250,
    }
    assert list(json.loads(out)) == ["schema", "action", "from", "to", "duration_ms"]


def test_cli_swipe_direction_without_selectors(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "swipe", "--direction", "up")
    assert (code, err) == (0, "")
    assert out == "swiped 160,511 -> 160,128 in 300 ms\n"
    assert sent_inputs(fake_adb) == ["input swipe 160 511 160 128 300" + SUFFIX]
    assert android.dumps == 0


def shell_requests(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands]


def test_cli_swipe_direction_without_selectors_requests(capsys, fake_adb, android):
    code, _, _ = invoke(capsys, fake_adb, "swipe", "--direction", "up")
    assert code == 0
    assert shell_requests(fake_adb) == [
        "dumpsys window displays",
        "input swipe 160 511 160 128 300" + SUFFIX,
    ]
    assert android.dumps == 0


API24_DISPLAYS = (
    "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
    "  Display: mDisplayId=0\n"
    "    init=320x640 160dpi cur=640x320 app=640x320 rng=320x296-640x616\n"
)


@pytest.mark.parametrize("display", [(320, 640), (640, 320)])
@pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
def test_scroll_display_matches_dump_path(fake_adb, android, display, direction):
    android.display = display
    fast = Device(adb_server=fake_adb.address).scroll_display(direction)
    assert android.dumps == 0
    slow_device = Device(adb_server=fake_adb.address)
    slow_device.dump()
    assert slow_device.scroll(direction) == fast


def test_scroll_display_landscape_points(fake_adb, android):
    android.override("dumpsys window displays", API24_DISPLAYS.encode())
    assert Device(adb_server=fake_adb.address).scroll_display("up") == (
        (320, 255),
        (320, 64),
    )


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"garbage\n",
        b"  Display: mDisplayId=0\n    init=1x1 cur=axb\n",
        b"  Display: mDisplayId=0\n    init=1x1 cur=0x640\n",
        b"  Display: mDisplayId=0\n    init=1x1 cur=99999x640\n",
        b"  Display: mDisplayId=0\n    init=320x640\n",
        b"  Display: mDisplayId=1\n    init=1x1 cur=320x640\n",
    ],
)
def test_cli_swipe_falls_back_to_the_dump(capsys, fake_adb, android, output):
    android.override("dumpsys window displays", output)
    code, out, err = invoke(capsys, fake_adb, "swipe", "--direction", "up")
    assert (code, err) == (0, "")
    assert out == "swiped 160,511 -> 160,128 in 300 ms\n"
    assert android.dumps == 1
    assert sent_inputs(fake_adb) == ["input swipe 160 511 160 128 300" + SUFFIX]


def test_scroll_display_unreadable_size_sends_no_input(fake_adb, android):
    android.override("dumpsys window displays", b"nothing useful\n")
    device = Device(adb_server=fake_adb.address)
    with pytest.raises(DisplaySizeUnknownError, match="display size is unknown"):
        device.scroll_display("up")
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_scroll_display_checks_duration_before_adb(fake_adb, android):
    android.override("dumpsys window displays", b"nothing useful\n")
    device = Device(adb_server=fake_adb.address)
    seen = len(fake_adb.commands)
    with pytest.raises(TypeError):
        device.scroll_display("up", duration_ms=False)
    assert len(fake_adb.commands) == seen
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_scroll_display_checks_direction_before_adb(fake_adb):
    device = Device(adb_server=fake_adb.address)
    seen = len(fake_adb.commands)
    with pytest.raises(TypeError):
        device.scroll_display(1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="direction must be one of"):
        device.scroll_display("sideways")
    assert len(fake_adb.commands) == seen


def test_scroll_display_too_small_display(fake_adb, android):
    android.override(
        "dumpsys window displays", b"  Display: mDisplayId=0\n init=1x1 cur=10x10\n"
    )
    device = Device(adb_server=fake_adb.address)
    with pytest.raises(ValueError, match="too small") as info:
        device.scroll_display("up")
    assert not isinstance(info.value, DisplaySizeUnknownError)
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_device_reuses_its_serial(fake_adb):
    device = Device(adb_server=fake_adb.address)
    device.scroll_display("up")
    device.scroll_display("down")
    assert fake_adb.requests.count("host:devices-l") == 1


# The head of `dumpsys window displays` on an API 36 emulator, in portrait.
API36_DISPLAYS = (
    "WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)\n"
    "  Display: mDisplayId=0 (organized)\n"
    "    init=320x640 160dpi mMinSizeOfResizeableTaskDp=220 cur=320x640"
    " app=320x640 rng=320x320-640x640\n"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (API24_DISPLAYS, (640, 320)),
        (API36_DISPLAYS, (320, 640)),
        (API24_DISPLAYS.replace("cur=640x320", "cur=320x640"), (320, 640)),
        (API24_DISPLAYS.replace("\n", "\r\n"), (640, 320)),
        ("  Display: mDisplayId=0 extra text\n    init=1x1 cur=10x20\n", (10, 20)),
        ("  Display: mDisplayId=0\n    init=1x1 cur=10x20", (10, 20)),
        (
            "  Display: mDisplayId=1\n    init=1x1 cur=99x99\n"
            "  Display: mDisplayId=0\n    init=1x1 cur=10x20\n"
            "  Display: mDisplayId=2\n    init=1x1 cur=77x77\n",
            (10, 20),
        ),
        (
            "  Display: mDisplayId=0\n  Display: mDisplayId=1\n    init=1x1 cur=9x9\n",
            None,
        ),
        ("  Display: mDisplayId=1\n    init=1x1 cur=9x9\n", None),
        ("  Display: mDisplayId=01\n    init=1x1 cur=9x9\n", None),
        ("  Display: mDisplayId=0\n    init=1x1 cur=9x9x\n", None),
        ("  Display: mDisplayId=0\n    init=1x1 cur=0x9\n", None),
        ("  Display: mDisplayId=0\n    init=1x1 cur=32769x9\n", None),
        ("  Display: mDisplayId=0\n    init=1x1 cur=32768x9\n", (32768, 9)),
        ("  Display: mDisplayId=0x1\n    init=1x1 cur=9x9\n", None),
        ("  Display: mDisplayId=0bad\n    init=1x1 cur=9x9\n", None),
        ("  Display: mDisplayId=0\n    init=1x1\n    cur=9x9\n", None),
        ("  Display: mDisplayId=0\n    init=1x1 app=9x9\n", None),
        ("  Display: mDisplayId=0\n    init=1x1 cur=9x9 cur=8x8\n", None),
        ("  Display: mDisplayId=0\n    init=1x1 cur=9x9 cur=9x9\n", (9, 9)),
        ("", None),
    ],
)
def test_parse_display_size(text, expected):
    assert _parse_display_size(text) == expected


@pytest.mark.usefixtures("android")
def test_cli_swipe_direction_json(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "left", "--ms", "400", "--json"
    )
    assert code == 0
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "swipe",
        "from": [255, 320],
        "to": [64, 320],
        "duration_ms": 400,
        "direction": "left",
    }
    assert list(json.loads(out)) == [
        "schema",
        "action",
        "from",
        "to",
        "duration_ms",
        "direction",
    ]


@pytest.mark.usefixtures("android")
def test_cli_swipe_direction_in_an_element(capsys, fake_adb):
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--direction", "down", "--text", "Pane"
    )
    assert (code, err) == (0, "")
    assert out == (
        'swiped 160,140 -> 160,259 in 300 ms on e2 TextView "Pane" id=pane\n'
    )
    assert sent_inputs(fake_adb) == ["input swipe 160 140 160 259 300" + SUFFIX]


@pytest.mark.usefixtures("android")
def test_cli_swipe_direction_in_an_element_json(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--id", "pane", "--json"
    )
    assert code == 0
    payload = json.loads(out)
    assert payload == {
        "schema": "uiprobe.action/1",
        "action": "swipe",
        "from": [160, 259],
        "to": [160, 140],
        "duration_ms": 300,
        "direction": "up",
        "element": {
            "id": "e2",
            "parent": "e1",
            "class": "android.widget.TextView",
            "text": "Pane",
            "resource_id": f"{SHOP}:id/pane",
            "bounds": [10, 100, 310, 300],
            "center": [160, 200],
        },
    }
    assert list(payload) == [
        "schema",
        "action",
        "from",
        "to",
        "duration_ms",
        "direction",
        "element",
    ]


def test_cli_swipe_no_match_sends_nothing(capsys, fake_adb, android):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Nope", "--json"
    )
    assert code == 12
    assert json.loads(out)["error"]["code"] == "element_not_found"
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_swipe_several_matches_send_nothing(capsys, fake_adb, android):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Twin", "--json"
    )
    assert code == 13
    assert json.loads(out)["error"]["code"] == "multiple_elements"
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_swipe_invisible_element_is_not_found(capsys, fake_adb, android):
    code, _, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Flat"
    )
    assert code == 12
    assert android.inputs == []


def test_cli_swipe_area_too_small_is_a_usage_error_and_sends_nothing(
    capsys, fake_adb, android
):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Short", "--json"
    )
    assert code == 2
    assert "too small to scroll" in json.loads(out)["error"]["message"]
    assert android.inputs == []


def test_cli_swipe_in_a_sole_sliver_is_too_small_and_sends_nothing(
    capsys, fake_adb, android
):
    # Thin is 1 pixel wide and the only match: found, then refused as too small.
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Thin", "--json"
    )
    assert code == 12
    error = json.loads(out)["error"]
    assert error["code"] == "element_not_visible"
    assert error["details"]["reason"] == "too_small"
    assert android.inputs == []


def test_cli_swipe_disabled_element_warns_on_stderr_only(capsys, fake_adb, android):
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Off"
    )
    assert code == 0
    assert out.startswith("swiped ")
    assert "warning" not in out
    assert err == "uiprobe: warning: e10 is disabled; scrolling it anyway\n"
    assert len(android.inputs) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ("--from", "1", "2"),
        ("--to", "1", "2"),
        ("--from", "1", "2", "--to", "3", "4", "--direction", "up"),
        ("--from", "1", "2", "--to", "3", "4", "--text", "Pane"),
        ("--text", "Pane"),
        ("--id", "pane"),
        ("--desc", "x"),
        (),
        ("--ms", "300"),
        ("--direction", "north"),
        ("--direction", "UP"),
        ("--direction", ""),
        ("--direction", "up", "--ms", "0"),
        ("--direction", "up", "--ms", "10001"),
        ("--direction", "up", "--ms", "-5"),
        ("--direction", "up", "--ms", "1.5"),
        ("--direction", "up", "--ms", "+5"),
        ("--direction", "up", "--ms", "0x10"),
        ("--direction", "up", "--ms", "1e3"),
        ("--direction", "up", "--ms", ""),
        ("--direction", "up", "--ms", " 5"),
        ("--direction", "up", "--ms", "007x"),
        ("--direction", "up", "--ms", "\u0665"),
        ("--from", "1", "2", "--to", "1", "2"),
        ("--from", "1", "2", "--to", "01", "002"),
        ("--from", "-1", "2", "--to", "3", "4"),
        ("--from", "1", "2", "--to", "32768", "4"),
        ("--from", "1", "2", "--to", "3", "1.5"),
        ("--from", "1", "2", "--to", "3"),
        ("--from", "1", "--to", "3", "4"),
        ("--from", "1", "2", "--to", "3", "4", "--ms", "0"),
    ],
)
def test_cli_swipe_usage_errors_do_not_contact_the_server(
    capsys, fake_adb, android, argv
):
    code, out, _ = invoke(capsys, fake_adb, "swipe", *argv, "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_cli_swipe_accepts_the_edges(capsys, fake_adb):
    code, out, _ = invoke(
        capsys,
        fake_adb,
        "swipe",
        "--from",
        "0",
        "32767",
        "--to",
        "32767",
        "0",
        "--ms",
        "10000",
    )
    assert (code, out) == (0, "swiped 0,32767 -> 32767,0 in 10000 ms\n")


def test_cli_swipe_input_failed(capsys, fake_adb, android):
    android.answer_input(status=1, output="boom\n")
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--from", "10", "20", "--to", "30", "40", "--json"
    )
    assert (code, err) == (14, "")
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.error/1"
    assert payload["error"]["code"] == "input_failed"
    assert payload["error"]["details"] == {
        "command": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 300,
        "reason": "exit_status",
        "exit_status": 1,
        "output": "boom",
    }
    assert len(android.inputs) == 1


def test_cli_swipe_does_not_resend_after_a_drop(capsys, fake_adb, android):
    android.drop_after_input(1)
    code, _, _ = invoke(
        capsys, fake_adb, "swipe", "--from", "10", "20", "--to", "30", "40", "--json"
    )
    assert code == 10
    assert len(android.inputs) == 1
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("android")
def test_cli_swipe_ctrl_c_is_130(capsys, fake_adb, monkeypatch):
    def interrupt(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Device, "swipe", interrupt)
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--from", "1", "2", "--to", "3", "4", "--json"
    )
    assert code == 130
    assert json.loads(out)["error"]["code"] == "interrupted"


def test_cli_key_with_an_alias(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "key", "back")
    assert (code, err) == (0, "")
    assert out == "pressed key KEYCODE_BACK\n"
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_BACK" + SUFFIX]
    assert [(i.kind, i.args) for i in android.inputs] == [("key", ("KEYCODE_BACK",))]


@pytest.mark.usefixtures("android")
def test_cli_key_alias_is_case_insensitive(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "key", "HOME")
    assert (code, out) == (0, "pressed key KEYCODE_HOME\n")


@pytest.mark.usefixtures("android")
def test_cli_key_with_a_keycode_name_json(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "key", "KEYCODE_F1", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "key",
        "key": "KEYCODE_F1",
    }
    assert list(json.loads(out)) == ["schema", "action", "key"]
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_F1" + SUFFIX]


@pytest.mark.parametrize(
    "name",
    [
        "4",
        "0",
        "",
        " back",
        "keycode_back",
        "KEYCODE_back",
        "KEYCODE_" + "A" * 41,
        "KEYCODE_BACK;reboot",
        "nonsense",
    ],
)
def test_cli_key_usage_errors_do_not_contact_the_server(
    capsys, fake_adb, android, name
):
    code, out, _ = invoke(capsys, fake_adb, "key", name, "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_cli_key_without_a_name_is_a_usage_error(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "key", "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_cli_key_number_message_says_why(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "key", "4", "--json")
    assert code == 2
    message = json.loads(out)["error"]["message"]
    assert "numbers are not accepted" in message
    assert "KEYCODE_" in message


def test_cli_key_input_failed(capsys, fake_adb, android):
    android.answer_input(status=1, output="boom\n")
    code, out, err = invoke(capsys, fake_adb, "key", "home", "--json")
    assert (code, err) == (14, "")
    payload = json.loads(out)
    assert payload["error"]["code"] == "input_failed"
    assert payload["error"]["details"] == {
        "command": "key",
        "key": "KEYCODE_HOME",
        "reason": "exit_status",
        "exit_status": 1,
        "output": "boom",
    }
    assert len(android.inputs) == 1


def test_cli_key_does_not_resend_after_a_drop(capsys, fake_adb, android):
    android.drop_after_input(1)
    code, _, _ = invoke(capsys, fake_adb, "key", "home", "--json")
    assert code == 10
    assert len(android.inputs) == 1
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("android")
def test_cli_key_ctrl_c_is_130(capsys, fake_adb, monkeypatch):
    def interrupt(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Device, "key", interrupt)
    code, out, _ = invoke(capsys, fake_adb, "key", "back", "--json")
    assert code == 130
    assert json.loads(out)["error"]["code"] == "interrupted"


@pytest.mark.parametrize("command", ["swipe", "key"])
def test_cli_help_says_the_foreground_app_is_not_checked(capsys, command):
    assert cli.run([command, "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "does not check which app is in front" in text


def test_cli_swipe_help_mentions_system_gestures(capsys):
    assert cli.run(["swipe", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "system gestures" in text
    assert "never resent" in text
