"""Tests for ``uiprobe tap``."""

from __future__ import annotations

import json

import pytest

from tests.device.tap.helpers import (
    LONG_COMMAND,
    SHOP,
    TAP_COMMAND,
    invoke,
    sent_inputs,
)
from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
    assert_no_resend_after_drop,
)
from uiprobe import cli
from uiprobe.device import Device
from uiprobe.errors import (
    ElementNotFoundError,
    ElementNotVisibleError,
)


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
