"""Tests for the 8 x 8 pixel rule, the cover check and selector match counts."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.device.tap.helpers import (
    COVER_SERIAL,
    MATCH_SERIAL,
    element,
    invoke,
    sent_inputs,
)
from uiprobe.device import Device
from uiprobe.errors import (
    AdbError,
    ElementNotVisibleError,
)
from uiprobe.model import Bounds, Element

ADMITTED = (
    "input tap 1 2; echo uiprobe-exit:$?",
    "input swipe 1 2 1 2 10; echo uiprobe-exit:$?",
    "input swipe 1 2 1 2 800; echo uiprobe-exit:$?",
)


# --- the fake itself ---


def test_strict_mode_rejects_other_compound_commands(device, android, fake_adb):
    device._shell("input tap 1 2; echo uiprobe-exit:$?")
    device._shell("input swipe 1 2 1 2 800; echo uiprobe-exit:$?")
    device._shell("input swipe 1 2 1 2 10; echo uiprobe-exit:$?")
    assert fake_adb.unexpected == []
    for command in (
        "input swipe 1 2 3 4 800; echo uiprobe-exit:$?",
        "input swipe 1 2 1 2 900; echo uiprobe-exit:$?",
        "input swipe 1 2 1 2 201; echo uiprobe-exit:$?",
        "input swipe 1 2 1 2 0; echo uiprobe-exit:$?",
        "input tap 1 2; echo uiprobe-exit:$?; reboot",
        "input tap 1 2; echo hi",
        "input tap 1 2;echo uiprobe-exit:$?",
        "input keyevent 4; echo uiprobe-exit:$?",
        "getprop ro.kernel.qemu; echo uiprobe-exit:$?",
    ):
        with pytest.raises(AdbError):
            device._shell(command)
    assert len(fake_adb.unexpected) == 9
    fake_adb.unexpected.clear()  # the fixture would otherwise fail this test
    # the server also logged the refused commands; the autouse fixture checks
    # only what a device could have run
    fake_adb.commands[:] = [c for c in fake_adb.commands if c[2] in ADMITTED]
    assert [i.kind for i in android.inputs] == ["tap", "long_press", "tap"]


def test_the_fake_answers_the_marker_by_default(device):
    assert device._shell("input tap 1 2; echo uiprobe-exit:$?") == "uiprobe-exit:0\n"


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
