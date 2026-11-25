"""Tests for ``Device.key``."""

from __future__ import annotations

import pytest

from tests.device.gestures.helpers import (
    SUFFIX,
    no_request,
    sent_inputs,
)
from uiprobe.device import (
    KEY_ALIASES,
)

# --- Device.key ---


@pytest.mark.parametrize(("alias", "code"), sorted(KEY_ALIASES))
def test_key_aliases(device, android, fake_adb, alias, code):
    assert device.key(alias) == code
    assert sent_inputs(fake_adb) == [f"input keyevent {code}" + SUFFIX]
    assert [(i.kind, i.args) for i in android.inputs] == [("key", (code,))]


@pytest.mark.usefixtures("device")
def test_the_alias_table():
    assert dict(KEY_ALIASES) == {
        "back": "KEYCODE_BACK",
        "home": "KEYCODE_HOME",
        "enter": "KEYCODE_ENTER",
        "delete": "KEYCODE_DEL",
        "forward_delete": "KEYCODE_FORWARD_DEL",
        "tab": "KEYCODE_TAB",
        "escape": "KEYCODE_ESCAPE",
        "menu": "KEYCODE_MENU",
        "search": "KEYCODE_SEARCH",
        "recents": "KEYCODE_APP_SWITCH",
        "up": "KEYCODE_DPAD_UP",
        "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT",
        "right": "KEYCODE_DPAD_RIGHT",
        "center": "KEYCODE_DPAD_CENTER",
        "page_up": "KEYCODE_PAGE_UP",
        "page_down": "KEYCODE_PAGE_DOWN",
        "space": "KEYCODE_SPACE",
    }


def test_key_aliases_cannot_be_changed(device, fake_adb):
    assert isinstance(KEY_ALIASES, tuple)
    with pytest.raises(TypeError):
        KEY_ALIASES[0] = ("back", "KEYCODE_HOME")  # ty: ignore[invalid-assignment]
    for alias, code in KEY_ALIASES:
        assert device.key(alias) == code
    assert sent_inputs(fake_adb) == [
        f"input keyevent {c}" + SUFFIX for _, c in KEY_ALIASES
    ]


@pytest.mark.parametrize("alias", ["BACK", "Back", "bAcK", "Page_Down", "RECENTS"])
def test_key_aliases_are_case_insensitive(device, fake_adb, alias):
    code = device.key(alias)
    assert code == dict(KEY_ALIASES)[alias.lower()]
    assert sent_inputs(fake_adb) == [f"input keyevent {code}" + SUFFIX]


@pytest.mark.parametrize(
    "name", ["KEYCODE_F1", "KEYCODE_A", "KEYCODE_" + "A" * 40, "KEYCODE_F12"]
)
def test_key_accepts_a_keycode_name(device, fake_adb, name):
    assert device.key(name) == name
    assert sent_inputs(fake_adb) == [f"input keyevent {name}" + SUFFIX]


def test_key_back_reaches_the_device(device, android):
    assert device.key("KEYCODE_BACK") == "KEYCODE_BACK"
    assert [i.kind for i in android.inputs] == ["key"]


@pytest.mark.parametrize(
    "name",
    [
        "",
        " back",
        "back ",
        "ba ck",
        "back\n",
        "KEYCODE_back",
        "keycode_back",
        "KEYCODE_",
        "KEYCODE_26",
        "KEYCODE_224",
        "KEYCODE_" + "A" * 41,
        "KEYCODE_BACK;reboot",
        "KEYCODE_BACK reboot",
        "KEYCODE_BACK$(reboot)",
        "KEYCODE_Ä",
        "volume_up",
        "ÿ",
    ],
)
def test_key_rejects_a_bad_name(device, android, fake_adb, name):
    no_request(android, fake_adb, lambda: device.key(name), ValueError)


@pytest.mark.parametrize("name", ["4", "0", "04", "66", "٤"])
def test_key_refuses_numbers_and_says_why(device, android, fake_adb, name):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="KEYCODE_") as info:
        device.key(name)
    if name.isascii():
        assert "numbers are not accepted" in str(info.value)
        assert "digits" in str(info.value)
        assert "KEYCODE_" in str(info.value)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize("name", [None, 4, b"back", ["back"], ("back",), 1.5])
def test_key_rejects_a_non_str(device, android, fake_adb, name):
    no_request(android, fake_adb, lambda: device.key(name), TypeError)
