"""Tests for tapping: `Device.tap`, `Screen.tap_point` and ``uiprobe tap``.

Every device is a strict `FakeAndroid`. An autouse fixture checks after each
test that every input command the server received is exactly one of the two
marker forms (the fake itself also admits bare forms for other tests). Tests
that must not press assert that `FakeAndroid.inputs` is empty.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import Reset
from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
    assert_no_resend_after_drop,
)
from uiprobe import cli
from uiprobe.device import Device
from uiprobe.errors import (
    AdbError,
    ElementNotFoundError,
    ElementNotVisibleError,
    InputFailedError,
    UiprobeError,
)
from uiprobe.model import Bounds, Element
from uiprobe.model import Screen as ScreenModel

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
TAP_COMMAND = "input tap 80 35; echo uiprobe-exit:$?"
LONG_COMMAND = "input swipe 80 35 80 35 800; echo uiprobe-exit:$?"
ADMITTED = (
    "input tap 1 2; echo uiprobe-exit:$?",
    "input swipe 1 2 1 2 800; echo uiprobe-exit:$?",
)
INPUT_REQUEST = re.compile(r"^shell:input\b")
MARKER_FORMS = (
    re.compile(r"^input tap [0-9]+ [0-9]+; echo uiprobe-exit:\$\?\Z"),
    re.compile(r"^input swipe ([0-9]+) ([0-9]+) \1 \2 800; echo uiprobe-exit:\$\?\Z"),
)

SCREENS = (
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((10, 10, 150, 60), text="Login", rid=f"{SHOP}:id/login", go="next"),
            El((10, 70, 150, 120), text="Hold", desc="menu", long_go="menu"),
            El((200, 200, 250, 250), text="Twin", rid=f"{SHOP}:id/twin"),
            El((250, 200, 300, 250), text="Twin", rid=f"{SHOP}:id/twin"),
            El((300, 560, 400, 700), text="Cut", desc="cut"),
            El((-20, 500, 100, 550), text="Negative", desc="neg"),
            El((203, 300, 80, 350), text="Inverted"),
            El((10, 400, 100, 400), text="Flat"),
            El((0, 700, 100, 760), text="Below"),
            El((10, 300, 150, 350), text="Off", enabled=False, go="next"),
            El((300, 10, 330, 60), text="Edge"),
            El((0, 150, 40, 190), text="Pair", rid=f"{SHOP}:id/ra"),
            El((50, 150, 90, 190), text="Pair", rid=f"{SHOP}:id/rb"),
            El((100, 150, 140, 190), text="Other", rid=f"{SHOP}:id/ra"),
            El((150, 150, 190, 190), text="Dd", desc="da"),
            El((200, 150, 240, 190), text="Dd", desc="db"),
            El((250, 150, 290, 190), text="De", desc="da"),
        ),
    ),
    Screen("next", f"{SHOP}/.Next", (El((0, 0, 320, 100), text="Next"),)),
    Screen("menu", f"{SHOP}/.Menu", (El((0, 0, 320, 100), text="Menu"),)),
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


def sent_inputs(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands if c.startswith("input")]


@pytest.fixture(autouse=True)
def _only_marker_inputs(fake_adb: FakeAdbServer):
    """After each test, every input command received had a marker form."""
    yield
    for command in sent_inputs(fake_adb):
        assert command.endswith("; echo uiprobe-exit:$?"), command
        assert any(form.fullmatch(command) for form in MARKER_FORMS), command


@pytest.fixture
def screen(device: Device) -> ScreenModel:
    return device.dump()


def element(screen: ScreenModel, text: str) -> Element:
    return next(e for e in screen.elements if e.text == text)


def tap_failure(device: Device, *, long: bool = False) -> InputFailedError:
    with pytest.raises(InputFailedError) as info:
        device.tap((80, 35), long=long)
    return info.value


# --- exact commands and what the device recorded ---


def test_tap_sends_the_exact_command_once(device, android, fake_adb):
    assert device.tap((80, 35)) == (80, 35)
    assert sent_inputs(fake_adb) == [TAP_COMMAND]
    assert [(i.kind, i.point) for i in android.inputs] == [("tap", (80, 35))]
    assert android.stack == ["main", "next"]


def test_long_press_sends_the_exact_command_once(device, android, fake_adb):
    assert device.tap([80, 35], long=True) == (80, 35)
    assert sent_inputs(fake_adb) == [LONG_COMMAND]
    assert [(i.kind, i.point) for i in android.inputs] == [("long_press", (80, 35))]


def test_a_long_press_reaches_the_long_click_target(device, android):
    device.tap((20, 80), long=True)
    assert android.stack == ["main", "menu"]


# --- Device.tap targets ---


def test_tap_with_an_element_uses_its_center(device, android, screen):
    assert device.tap(element(screen, "Login")) == (80, 35)
    assert [i.point for i in android.inputs] == [(80, 35)]


@pytest.mark.parametrize("text", ["Login", "Cut", "Negative", "Edge"])
def test_tap_with_an_element_presses_the_screen_tap_point(
    device, android, screen, text
):
    item = element(screen, text)
    assert device.tap(item) == screen.tap_point(item)
    assert [i.point for i in android.inputs] == [screen.tap_point(item)]


def test_tap_with_an_element_past_the_right_edge_uses_the_visible_part(
    device, android, screen
):
    # display 320x640, bounds (300,10,330,60): the visible part is (300,10,320,60)
    assert screen.display == (320, 640)
    assert element(screen, "Edge").bounds == Bounds(300, 10, 330, 60)
    assert device.tap(element(screen, "Edge")) == (310, 35)
    assert [i.point for i in android.inputs] == [(310, 35)]


def test_tap_with_an_element_cut_at_the_bottom_uses_the_visible_part(
    device, android, screen
):
    assert device.tap(element(screen, "Cut")) == (310, 600)
    assert [i.point for i in android.inputs] == [(310, 600)]


def test_tap_uses_the_int_value_of_an_int_subclass(device, fake_adb):
    class Odd(int):
        def __str__(self) -> str:
            return "7"

    assert device.tap((Odd(80), 35)) == (80, 35)
    assert sent_inputs(fake_adb) == [TAP_COMMAND]


@pytest.mark.parametrize("text", ["Inverted", "Flat", "Below"])
def test_tap_with_an_element_without_a_visible_part(
    device, android, fake_adb, text, screen
):
    before = len(fake_adb.requests)
    with pytest.raises(ElementNotVisibleError) as info:
        device.tap(element(screen, text))
    assert info.value.code == "element_not_visible"
    assert info.value.exit_code == 12
    assert info.value.details["element"] == element(screen, text).id
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_tap_with_an_element_of_an_older_dump_is_a_value_error(
    device, android, fake_adb
):
    old = element(device.dump(), "Login")
    latest = device.dump()
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.tap(old)
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert device.tap(element(latest, "Login")) == (80, 35)


def test_tap_with_a_hand_made_element_is_a_value_error(
    device, android, fake_adb, screen
):
    made = Element("e99", None, "X", Bounds(11, 11, 150, 60))
    twin = Element("e2", None, "Login", element(screen, "Login").bounds)
    before = len(fake_adb.requests)
    for bad in (made, twin):
        with pytest.raises(ValueError, match=r"screen\.tap_point"):
            device.tap(bad)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_tap_with_an_element_before_any_dump_is_a_value_error(
    device, android, fake_adb
):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.tap(Element("e1", None, "X", Bounds(0, 0, 10, 10)))
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_tap_with_an_element_of_another_device_is_a_value_error(
    device, android, fake_adb
):
    other = Device(adb_server=fake_adb.address)
    item = element(other.dump(), "Login")
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.tap(item)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_a_failed_dump_keeps_the_previous_screen_as_the_latest(device, android):
    screen = device.dump()
    android.override(re.compile(r"^uiautomator dump"), b"error\n")
    with pytest.raises(UiprobeError):
        device.dump()
    assert device.tap(element(screen, "Login")) == (80, 35)
    assert [i.point for i in android.inputs] == [(80, 35)]


def test_an_element_whose_point_is_out_of_range_is_a_value_error(fake_adb):
    # `wm size` over 32768 is not trusted, so the display falls back to the
    # widest top-level bounds, which can put the press point out of range.
    wide = Screen(
        "wide",
        f"{SHOP}/.Wide",
        (El((0, 0, 40000, 100), text="Top"), El((39000, 0, 40000, 10), text="Far")),
    )
    android = FakeAndroid(fake_adb, SERIAL, (wide,), start="wide", display=(40000, 640))
    device = Device(adb_server=fake_adb.address)
    screen = device.dump()
    far = element(screen, "Far")
    assert screen.tap_point(far) == (39500, 5)
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="32767"):
        device.tap(far)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_a_point_needs_no_dump(device, android):
    assert device.tap((5, 6)) == (5, 6)
    assert [i.point for i in android.inputs] == [(5, 6)]


@pytest.mark.parametrize(
    "target", [(True, 1), (1, False), (1.0, 2), ("1", "2"), (1, 2, 3), (1,), 5, None]
)
def test_tap_rejects_a_bad_target_type(device, android, fake_adb, target):
    before = len(fake_adb.requests)
    with pytest.raises(TypeError):
        device.tap(target)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize("long", [1, 0, None, "yes"])
def test_tap_rejects_a_non_bool_long(device, android, fake_adb, long):
    before = len(fake_adb.requests)
    with pytest.raises(TypeError):
        device.tap((1, 2), long=long)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize("point", [(-1, 0), (0, -1), (32768, 0), (0, 32768)])
def test_tap_rejects_a_coordinate_out_of_range(device, android, fake_adb, point):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="32767"):
        device.tap(point)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_tap_accepts_the_edges_of_the_range(device, android):
    assert device.tap((0, 32767)) == (0, 32767)
    assert [i.point for i in android.inputs] == [(0, 32767)]


# --- Screen.tap_point ---


def test_tap_point_matches_the_center_find_reports(screen):
    for found in screen.find(text="Cut"), screen.find(text="Login"):
        (item,) = found
        assert list(screen.tap_point(item)) == screen._element_dict(item)["center"]
    assert screen.tap_point(element(screen, "Cut")) == (310, 600)
    assert screen.tap_point(element(screen, "Negative")) == (50, 525)


def test_tap_point_rounds_the_center_down(screen):
    # In an empty part of the screen, where no other element is on top: each
    # element is itself the topmost meaningful one at its center.
    odd = Element("e99", None, "X", Bounds(161, 401, 300, 450), text="odd")
    small = Element("e98", None, "X", Bounds(160, 500, 169, 509), text="x")
    full = replace(screen, elements=(*screen.elements, odd, small))
    assert full.tap_point(odd) == (230, 425)
    assert full.tap_point(small) == (164, 504)


@pytest.mark.parametrize("text", ["Below", "Flat", "Inverted"])
def test_tap_point_without_a_visible_part(screen, text):
    with pytest.raises(ElementNotVisibleError) as info:
        screen.tap_point(element(screen, text))
    item = element(screen, text)
    assert info.value.details == {
        "element": item.id,
        "bounds": item.bounds.to_list(),
        "reason": "no_visible_part",
    }
    assert "center" not in screen._element_dict(item)


# --- reading the output ---


def test_a_nonzero_exit_status(device, android):
    android.answer_input(status=255, output="Error: bad\n")
    error = tap_failure(device)
    assert error.code == "input_failed"
    assert error.exit_code == 14
    assert error.details == {
        "command": "tap",
        "point": [80, 35],
        "reason": "exit_status",
        "exit_status": 255,
        "output": "Error: bad",
    }
    assert len(android.inputs) == 1


def test_a_missing_marker(device, android):
    android.answer_input(marker=False)
    error = tap_failure(device, long=True)
    assert error.details == {
        "command": "long_tap",
        "point": [80, 35],
        "reason": "no_exit_status",
    }
    assert len(android.inputs) == 1


def test_extra_output_before_a_zero_marker(device, android):
    android.answer_input(output="Usage: input ...\n")
    error = tap_failure(device)
    assert error.details == {
        "command": "tap",
        "point": [80, 35],
        "reason": "unexpected_output",
        "output": "Usage: input ...",
    }


def test_long_output_is_cut(device, android):
    android.answer_input(output="x" * 500 + "\n")
    error = tap_failure(device)
    assert error.details["output"] == "x" * 200


@pytest.mark.parametrize(
    "answer",
    [
        b"uiprobe-exit:\n",
        b"uiprobe-exit:x\n",
        b"uiprobe-exit:-1\n",
        b"uiprobe-exit:1 \n",
        b"uiprobe-exit:\xd9\xa0\n",  # an Arabic-Indic digit
        b"uiprobe-exit:0\nmore\n",  # the marker is not the last line
    ],
)
def test_a_marker_that_is_not_plain_decimal_is_no_exit_status(device, android, answer):
    android.override(re.compile(r"^input tap"), answer)
    error = tap_failure(device)
    assert error.details["reason"] == "no_exit_status"
    assert error.details["command"] == "tap"
    assert error.details["point"] == [80, 35]


@pytest.mark.parametrize("value", ["00", "000", "007", "9" * 5000])
def test_a_zero_padded_or_huge_marker_is_no_exit_status(device, android, value):
    android.override(re.compile(r"^input tap"), f"uiprobe-exit:{value}\n".encode())
    error = tap_failure(device)
    assert error.details["reason"] == "no_exit_status"


def test_output_is_stripped_after_the_cut(device, android):
    android.answer_input(output="x" * 199 + " y\n")
    assert tap_failure(device).details["output"] == "x" * 199


def test_empty_lines_are_ignored_and_a_trailing_cr_is_removed(device, android):
    android.override(re.compile(r"^input tap"), b"\r\nuiprobe-exit:0\r\n\r\n")
    assert device.tap((80, 35)) == (80, 35)
    android.override(re.compile(r"^input tap"), b"\n\nuiprobe-exit:0\n\n")
    assert device.tap((80, 35)) == (80, 35)


def test_a_whitespace_only_line_is_other_output(device, android):
    android.override(re.compile(r"^input tap"), b" \nuiprobe-exit:0\n")
    assert tap_failure(device).details["reason"] == "unexpected_output"


def test_a_second_marker_line_is_unexpected_output(device, android):
    android.override(re.compile(r"^input tap"), b"uiprobe-exit:0\r\nuiprobe-exit:0\r\n")
    assert tap_failure(device).details["reason"] == "unexpected_output"


# --- never resent ---


def test_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.tap((80, 35))
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == [TAP_COMMAND]
    assert android.dropped == [TAP_COMMAND]
    assert_no_resend_after_drop(android, fake_adb)


def test_no_resend_when_the_marker_is_missing(device, android, fake_adb):
    android.answer_input(marker=False)
    with pytest.raises(InputFailedError):
        device.tap((80, 35))
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == [TAP_COMMAND]


def test_no_resend_after_a_reset_reply(device, android, fake_adb):
    android.override(re.compile(r"^input tap"), Reset())
    with pytest.raises(UiprobeError):
        device.tap((80, 35))
    assert sent_inputs(fake_adb) == [TAP_COMMAND]


# --- the command line ---


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_tap_by_text(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "tap", "--text", "Login")
    assert (code, err) == (0, "")
    assert out == 'tapped e2 TextView "Login" id=login [tap] at 80,35\n'
    assert sent_inputs(fake_adb) == [TAP_COMMAND]
    assert [(i.kind, i.point) for i in android.inputs] == [("tap", (80, 35))]


@pytest.mark.parametrize(
    "selector",
    [
        ("--id", "login"),
        ("--id", f"{SHOP}:id/login"),
        ("--desc", "menu", "--text", "Hold"),
        ("--text", "Login", "--id", "login"),
    ],
)
def test_cli_tap_by_selectors(capsys, fake_adb, android, selector):
    code, _, err = invoke(capsys, fake_adb, "tap", *selector)
    assert (code, err) == (0, "")
    assert [i.kind for i in android.inputs] == ["tap"]


def test_cli_tap_and_semantics_text_and_id(capsys, fake_adb, android):
    for criteria in (("--text", "Pair"), ("--id", "ra")):
        code, _, _ = invoke(capsys, fake_adb, "tap", *criteria)
        assert code == 13
    assert android.inputs == []
    code, _, err = invoke(capsys, fake_adb, "tap", "--text", "Pair", "--id", "ra")
    assert (code, err) == (0, "")
    assert [(i.kind, i.point) for i in android.inputs] == [("tap", (20, 170))]


def test_cli_tap_and_semantics_desc_and_text(capsys, fake_adb, android):
    for criteria in (("--desc", "da"), ("--text", "Dd")):
        code, _, _ = invoke(capsys, fake_adb, "tap", *criteria)
        assert code == 13
    assert android.inputs == []
    code, _, err = invoke(capsys, fake_adb, "tap", "--desc", "da", "--text", "Dd")
    assert (code, err) == (0, "")
    assert [(i.kind, i.point) for i in android.inputs] == [("tap", (170, 170))]


@pytest.mark.parametrize(
    "criteria",
    [
        ("--text", "Login", "--id", "ra"),
        ("--text", "Login", "--id", "nothing"),
        ("--desc", "da", "--text", "Login"),
        ("--desc", "nothing", "--text", "Login"),
    ],
)
def test_cli_tap_combined_selectors_must_all_match(capsys, fake_adb, android, criteria):
    code, _, _ = invoke(capsys, fake_adb, "tap", *criteria)
    assert code == 12
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_tap_by_desc(capsys, fake_adb, android):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--desc", "menu")
    assert code == 0
    assert out == 'tapped e3 TextView "Hold" desc="menu" [long] at 80,95\n'
    assert android.inputs[0].point == (80, 95)


def test_cli_tap_presses_the_center_of_the_visible_part(capsys, fake_adb, android):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--desc", "cut", "--json")
    assert code == 0
    assert json.loads(out)["point"] == [310, 600]
    assert android.inputs[0].point == (310, 600)


def test_cli_tap_long(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "tap", "--desc", "menu", "--long")
    assert (code, err) == (0, "")
    assert out == 'long-tapped e3 TextView "Hold" desc="menu" [long] at 80,95\n'
    assert sent_inputs(fake_adb) == [
        "input swipe 80 95 80 95 800; echo uiprobe-exit:$?"
    ]
    assert [i.kind for i in android.inputs] == ["long_press"]
    assert android.stack == ["main", "menu"]


def test_cli_tap_xy_does_not_dump(capsys, fake_adb, android):
    dumps = android.dumps
    code, out, err = invoke(capsys, fake_adb, "tap", "--xy", "80", "35")
    assert (code, out, err) == (0, "tapped 80,35\n", "")
    assert android.dumps == dumps
    assert sent_inputs(fake_adb) == [TAP_COMMAND]


@pytest.mark.usefixtures("android")
def test_cli_tap_xy_long(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--xy", "80", "35", "--long")
    assert (code, out) == (0, "long-tapped 80,35\n")
    assert sent_inputs(fake_adb) == [LONG_COMMAND]


@pytest.mark.usefixtures("android")
def test_cli_tap_json_for_an_element(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "tap", "--id", "login", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "tap",
        "point": [80, 35],
        "element": {
            "id": "e2",
            "parent": "e1",
            "class": "android.widget.TextView",
            "text": "Login",
            "resource_id": f"{SHOP}:id/login",
            "bounds": [10, 10, 150, 60],
            "flags": ["clickable"],
            "center": [80, 35],
        },
    }
    assert list(json.loads(out)) == ["schema", "action", "point", "element"]


@pytest.mark.usefixtures("android")
def test_cli_tap_json_for_a_point(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--xy", "1", "2", "--long", "--json")
    assert code == 0
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "long_tap",
        "point": [1, 2],
    }


def test_cli_tap_text_escapes_app_text(capsys, fake_adb, android):
    shown = FakeAndroid(
        fake_adb,
        "emulator-5556",
        (Screen("s", f"{SHOP}/.S", (El((0, 0, 50, 50), text="a\u202eb"),)),),
        start="s",
    )
    code, out, _ = invoke(
        capsys, fake_adb, "-s", "emulator-5556", "tap", "--text", "a\u202eb"
    )
    assert code == 0
    assert "\u202e" not in out
    assert "\\u202e" in out
    assert len(shown.inputs) == 1
    assert android.inputs == []


def test_cli_tap_no_match_presses_nothing(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "tap", "--text", "Nope", "--json")
    assert (code, err) == (12, "")
    error = json.loads(out)["error"]
    assert error["code"] == "element_not_found"
    assert error["details"]["candidates"]
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_tap_several_matches_press_nothing(capsys, fake_adb, android):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--id", "twin", "--json")
    assert code == 13
    error = json.loads(out)["error"]
    assert error["code"] == "multiple_elements"
    assert error["details"]["total"] == 2
    assert len(error["details"]["candidates"]) == 2
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


@pytest.mark.parametrize("text", ["Below", "Flat", "Inverted"])
def test_cli_tap_invisible_element_is_not_found(capsys, fake_adb, android, text):
    code, _, _ = invoke(capsys, fake_adb, "tap", "--text", text)
    assert code == 12
    assert android.inputs == []


def test_cli_tap_selector_and_xy_is_a_usage_error(capsys, fake_adb, android):
    for selector in (("--text", "Login"), ("--id", "login"), ("--desc", "x")):
        code, out, _ = invoke(
            capsys, fake_adb, "tap", "--xy", "1", "2", *selector, "--json"
        )
        assert code == 2
        assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


def test_cli_tap_without_a_selector_is_a_usage_error(capsys, fake_adb, android):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.parametrize(
    "values",
    [
        ("-1", "5"),
        ("5", "-1"),
        ("32768", "5"),
        ("5", "32768"),
        ("+5", "5"),
        ("0x10", "5"),
        ("1.5", "5"),
        ("1e3", "5"),
        ("", "5"),
        (" 5", "5"),
        ("5 ", "5"),
        ("123456", "5"),
        ("\u0665", "5"),
    ],
)
def test_cli_tap_bad_xy_is_a_usage_error(capsys, fake_adb, android, values):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--json", "--xy", *values)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_cli_tap_xy_needs_two_values(capsys, fake_adb):
    code, _, _ = invoke(capsys, fake_adb, "tap", "--xy", "5")
    assert code == 2
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_cli_tap_xy_accepts_the_edges(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "tap", "--xy", "0", "32767")
    assert (code, out) == (0, "tapped 0,32767\n")


def test_cli_tap_input_failed(capsys, fake_adb, android):
    android.answer_input(status=1, output="boom\n")
    code, out, err = invoke(capsys, fake_adb, "tap", "--xy", "80", "35", "--json")
    assert (code, err) == (14, "")
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.error/1"
    assert payload["error"]["code"] == "input_failed"
    assert payload["error"]["details"] == {
        "command": "tap",
        "point": [80, 35],
        "reason": "exit_status",
        "exit_status": 1,
        "output": "boom",
    }
    assert len(android.inputs) == 1


def test_cli_tap_input_failed_text(capsys, fake_adb, android):
    android.answer_input(marker=False)
    code, out, err = invoke(capsys, fake_adb, "tap", "--xy", "80", "35")
    assert code == 14
    assert out == ""
    assert err.startswith("uiprobe: error: ")
    assert len(android.inputs) == 1


def test_cli_tap_does_not_resend_after_a_drop(capsys, fake_adb, android):
    android.drop_after_input(1)
    code, _, _ = invoke(capsys, fake_adb, "tap", "--xy", "80", "35", "--json")
    assert code == 10
    assert len(android.inputs) == 1
    assert_no_resend_after_drop(android, fake_adb)


def test_cli_tap_disabled_element_is_pressed_with_a_warning(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "tap", "--text", "Off", "--json")
    assert code == 0
    assert json.loads(out)["element"]["flags"] == ["clickable", "disabled"]
    assert err == "uiprobe: warning: e11 is disabled; pressing it anyway\n"
    assert [i.point for i in android.inputs] == [(80, 325)]


def test_cli_tap_disabled_element_text_mode_keeps_stdout_clean(
    capsys, fake_adb, android
):
    code, out, err = invoke(capsys, fake_adb, "tap", "--text", "Off")
    assert code == 0
    assert out.startswith("tapped e11 ")
    assert "warning" not in out
    assert "disabled" in err
    assert len(android.inputs) == 1


@pytest.mark.usefixtures("android")
def test_cli_tap_ctrl_c_is_130(capsys, fake_adb, monkeypatch):
    def interrupt(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Device, "tap", interrupt)
    code, out, _ = invoke(capsys, fake_adb, "tap", "--xy", "1", "2", "--json")
    assert code == 130
    assert json.loads(out)["error"]["code"] == "interrupted"


def test_cli_tap_help_says_the_foreground_app_is_not_checked(capsys):
    assert cli.run(["tap", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "does not check which app is in front" in text


def test_element_not_visible_shares_the_exit_code_of_not_found():
    assert ElementNotVisibleError.exit_code == ElementNotFoundError.exit_code == 12


# --- the fake itself ---


def test_strict_mode_rejects_other_compound_commands(device, android, fake_adb):
    device._shell("input tap 1 2; echo uiprobe-exit:$?")
    device._shell("input swipe 1 2 1 2 800; echo uiprobe-exit:$?")
    assert fake_adb.unexpected == []
    for command in (
        "input swipe 1 2 3 4 800; echo uiprobe-exit:$?",
        "input swipe 1 2 1 2 900; echo uiprobe-exit:$?",
        "input tap 1 2; echo uiprobe-exit:$?; reboot",
        "input tap 1 2; echo hi",
        "input tap 1 2;echo uiprobe-exit:$?",
        "input keyevent 4; echo uiprobe-exit:$?",
        "getprop ro.kernel.qemu; echo uiprobe-exit:$?",
    ):
        with pytest.raises(AdbError):
            device._shell(command)
    assert len(fake_adb.unexpected) == 7
    fake_adb.unexpected.clear()  # the fixture would otherwise fail this test
    # the server also logged the refused commands; the autouse fixture checks
    # only what a device could have run
    fake_adb.commands[:] = [c for c in fake_adb.commands if c[2] in ADMITTED]
    assert [i.kind for i in android.inputs] == ["tap", "long_press"]


def test_the_fake_answers_the_marker_by_default(device):
    assert device._shell("input tap 1 2; echo uiprobe-exit:$?") == "uiprobe-exit:0\n"


# --- the 8 x 8 pixel rule and the cover check ---

COVER_SERIAL = "emulator-5558"
COVER_SCREEN = Screen(
    "c",
    f"{SHOP}/.Cover",
    (
        El((20, 100, 300, 160), text="Pay", rid=f"{SHOP}:id/pay", go="c"),
        El((100, 110, 200, 150), text="Promo"),
        El((20, 300, 300, 360), text="Clear", rid=f"{SHOP}:id/clear", go="c"),
        El((0, 280, 320, 380)),  # an empty layer on top: looked through
        El((20, 633, 300, 700), text="Sliver", rid=f"{SHOP}:id/sliver", go="c"),
        El((20, 632, 300, 700), text="Eight", rid=f"{SHOP}:id/eight", go="c"),
    ),
)


@pytest.fixture
def cover(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, COVER_SERIAL, (COVER_SCREEN,), start="c")


def cover_invoke(capsys, fake_adb, *argv):
    return invoke(capsys, fake_adb, "-s", COVER_SERIAL, *argv)


def test_cli_tap_on_a_covered_element_exits_12_and_sends_nothing(
    capsys, fake_adb, cover
):
    code, out, err = cover_invoke(capsys, fake_adb, "tap", "--text", "Pay", "--json")
    assert (code, err) == (12, "")
    error = json.loads(out)["error"]
    assert error["code"] == "element_not_visible"
    assert error["details"] == {
        "element": "e2",
        "bounds": [20, 100, 300, 160],
        "reason": "covered",
        "covered_by": "e3",
        "point": [160, 130],
    }
    assert "e3" in error["message"]
    assert cover.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_tap_on_a_covered_element_names_the_cover_in_text(capsys, fake_adb, cover):
    code, out, err = cover_invoke(capsys, fake_adb, "tap", "--id", "pay")
    assert code == 12
    assert out == ""
    assert "element e2 is covered by e3 at 160,130" in err
    assert cover.inputs == []


def test_cli_tap_on_an_element_with_an_empty_layer_on_top_presses_it(
    capsys, fake_adb, cover
):
    code, out, _ = cover_invoke(capsys, fake_adb, "tap", "--text", "Clear")
    assert code == 0
    assert out.startswith("tapped e4 ")
    assert [i.point for i in cover.inputs] == [(160, 330)]


def test_cli_tap_xy_presses_a_covered_point_as_given(capsys, fake_adb, cover):
    code, out, _ = cover_invoke(capsys, fake_adb, "tap", "--xy", "160", "130")
    assert (code, out) == (0, "tapped 160,130\n")
    assert [i.point for i in cover.inputs] == [(160, 130)]


def test_cli_tap_on_a_sole_sliver_exits_12_too_small_and_sends_nothing(
    capsys, fake_adb, cover
):
    # 7 pixels of "Sliver" show at the bottom edge. It is the only match, so it
    # is found and then refused as too small (not "not found").
    code, out, _ = cover_invoke(capsys, fake_adb, "tap", "--text", "Sliver", "--json")
    assert code == 12
    error = json.loads(out)["error"]
    assert error["code"] == "element_not_visible"
    assert error["details"]["reason"] == "too_small"
    assert error["details"]["visible_size"] == [280, 7]
    assert cover.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_tap_on_eight_visible_pixels_presses_it(capsys, fake_adb, cover):
    code, _, _ = cover_invoke(capsys, fake_adb, "tap", "--text", "Eight")
    assert code == 0
    assert [i.point for i in cover.inputs] == [(160, 636)]


def test_device_tap_with_a_sliver_element_is_refused(fake_adb, cover):
    device = Device(COVER_SERIAL, adb_server=fake_adb.address)
    sliver = element(device.dump(), "Sliver")
    with pytest.raises(ElementNotVisibleError) as info:
        device.tap(sliver)
    assert info.value.details == {
        "element": sliver.id,
        "bounds": [20, 633, 300, 700],
        "reason": "too_small",
        "visible_size": [280, 7],
    }
    assert cover.inputs == []
    assert sent_inputs(fake_adb) == []


def test_device_tap_with_a_covered_element_is_refused(fake_adb, cover):
    device = Device(COVER_SERIAL, adb_server=fake_adb.address)
    pay = element(device.dump(), "Pay")
    with pytest.raises(ElementNotVisibleError) as info:
        device.tap(pay)
    assert info.value.details["reason"] == "covered"
    assert info.value.details["covered_by"] == "e3"
    assert cover.inputs == []
    assert sent_inputs(fake_adb) == []


def test_device_tap_presses_a_button_with_a_label_child_on_top(fake_adb, cover):
    device = Device(COVER_SERIAL, adb_server=fake_adb.address)
    latest = device.dump()
    button = Element("e2", "e1", "android.widget.Button", Bounds(20, 100, 300, 160))
    label = Element(
        "e3", "e2", "android.widget.TextView", Bounds(100, 110, 200, 150), text="Pay"
    )
    nested = replace(latest, elements=(latest.elements[0], button, label))
    device._latest_screen = nested
    assert device.tap(nested.elements[1]) == (160, 130)
    assert [i.point for i in cover.inputs] == [(160, 130)]


@pytest.mark.usefixtures("cover")
def test_cli_find_lists_a_covered_element_without_a_center(capsys, fake_adb):
    code, out, _ = cover_invoke(capsys, fake_adb, "find", "--text", "Pay")
    assert code == 0
    assert out.splitlines()[0].startswith("1 element matches")
    assert "center" not in out
    code, out, _ = cover_invoke(capsys, fake_adb, "find", "--text", "Pay", "--json")
    (found,) = json.loads(out)["elements"]
    assert found["id"] == "e2"
    assert "center" not in found
    code, out, _ = cover_invoke(capsys, fake_adb, "find", "--text", "Promo", "--json")
    (top,) = json.loads(out)["elements"]
    assert top["center"] == [150, 130]


# --- selector matches are counted without the 8-pixel rule ---

MATCH_SERIAL = "emulator-5559"
MATCH_SCREEN = Screen(
    "m",
    f"{SHOP}/.Match",
    (
        El((20, 100, 120, 200), text="Item"),  # 100 x 100
        El((20, 633, 120, 640), text="Item"),  # 100 x 7 at the bottom edge
        El((200, 100, 300, 200), text="Lonely"),
        El((200, 633, 300, 640), text="Lonely", rid=f"{SHOP}:id/lonely"),  # 7 high
        El((20, 300, 100, 380), rid=f"{SHOP}:id/isolated"),  # no label, no flag
    ),
)


@pytest.fixture
def matches(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, MATCH_SERIAL, (MATCH_SCREEN,), start="m")


def match_invoke(capsys, fake_adb, *argv):
    return invoke(capsys, fake_adb, "-s", MATCH_SERIAL, *argv)


def test_cli_tap_with_a_big_and_a_thin_match_is_ambiguous(capsys, fake_adb, matches):
    # origin/main raised multiple_elements here; the thin one must still count.
    code, out, _ = match_invoke(capsys, fake_adb, "tap", "--text", "Item", "--json")
    assert code == 13
    error = json.loads(out)["error"]
    assert error["code"] == "multiple_elements"
    assert error["details"]["total"] == 2
    big, thin = error["details"]["candidates"]
    assert big["center"] == [70, 150]
    assert "center" not in thin
    assert matches.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_swipe_with_a_big_and_a_thin_match_is_ambiguous(capsys, fake_adb, matches):
    code, out, _ = match_invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Item", "--json"
    )
    assert code == 13
    assert json.loads(out)["error"]["code"] == "multiple_elements"
    assert matches.inputs == []
    assert sent_inputs(fake_adb) == []


@pytest.mark.usefixtures("matches")
def test_find_lists_only_the_big_one_of_a_big_and_a_thin_match(capsys, fake_adb):
    code, out, _ = match_invoke(capsys, fake_adb, "find", "--text", "Item", "--json")
    assert code == 0
    assert [e["id"] for e in json.loads(out)["elements"]] == ["e2"]


def test_cli_tap_on_a_single_seven_pixel_match_exits_12_too_small(
    capsys, fake_adb, matches
):
    code, out, _ = match_invoke(capsys, fake_adb, "tap", "--id", "lonely", "--json")
    assert code == 12
    error = json.loads(out)["error"]
    assert error["code"] == "element_not_visible"
    assert error["details"]["reason"] == "too_small"
    assert error["details"]["visible_size"] == [100, 7]
    assert matches.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_tap_on_an_element_with_no_press_target_exits_12(capsys, fake_adb, matches):
    code, out, _ = match_invoke(capsys, fake_adb, "tap", "--id", "isolated", "--json")
    assert code == 12
    error = json.loads(out)["error"]
    assert error["code"] == "element_not_visible"
    assert error["details"] == {
        "element": "e6",
        "bounds": [20, 300, 100, 380],
        "reason": "no_target",
        "point": [60, 340],
    }
    assert "nothing that can receive a press" in error["message"]
    assert matches.inputs == []
    assert sent_inputs(fake_adb) == []


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
