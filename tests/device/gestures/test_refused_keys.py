"""Tests for the device-wide keys that ``Device.key`` refuses."""

from __future__ import annotations

import json

import pytest

from tests.device.gestures.helpers import (
    SUFFIX,
    invoke,
    sent_inputs,
)
from uiprobe.device import (
    _KEY_CODES,
    DEVICE_WIDE_KEY_PREFIXES,
    DEVICE_WIDE_KEYS,
    KEY_ALIASES,
)

# --- device-wide keys are refused ---

PREFIX_EXAMPLES = (
    "KEYCODE_MEDIA_PLAY_PAUSE",
    "KEYCODE_SYSTEM_NAVIGATION_DOWN",
    "KEYCODE_TV_INPUT_HDMI_1",
    "KEYCODE_VIDEO_APP_1",
    "KEYCODE_FEATURED_APP_4",
    "KEYCODE_DEMO_APP_2",
    "KEYCODE_STEM_3",
)


EXPECTED_PREFIXES = (
    "KEYCODE_MEDIA_",
    "KEYCODE_SYSTEM_NAVIGATION_",
    "KEYCODE_TV_",
    "KEYCODE_VIDEO_APP_",
    "KEYCODE_FEATURED_APP_",
    "KEYCODE_DEMO_APP_",
    "KEYCODE_STEM_",
)


# Written out here, independent of the module, so that deleting a name from
# DEVICE_WIDE_KEYS fails a test.
EXPECTED_REFUSED = (
    "KEYCODE_POWER",
    "KEYCODE_SLEEP",
    "KEYCODE_WAKEUP",
    "KEYCODE_SOFT_SLEEP",
    "KEYCODE_TV_POWER",
    "KEYCODE_AVR_POWER",
    "KEYCODE_STB_POWER",
    "KEYCODE_CALL",
    "KEYCODE_ENDCALL",
    "KEYCODE_HEADSETHOOK",
    "KEYCODE_VOLUME_UP",
    "KEYCODE_VOLUME_DOWN",
    "KEYCODE_VOLUME_MUTE",
    "KEYCODE_MUTE",
    "KEYCODE_BRIGHTNESS_UP",
    "KEYCODE_BRIGHTNESS_DOWN",
    "KEYCODE_SYSRQ",
    "KEYCODE_SCREENSHOT",
    "KEYCODE_NOTIFICATION",
    "KEYCODE_ASSIST",
    "KEYCODE_VOICE_ASSIST",
    "KEYCODE_ALL_APPS",
    "KEYCODE_SETTINGS",
    "KEYCODE_LANGUAGE_SWITCH",
    "KEYCODE_MANNER_MODE",
    "KEYCODE_PAIRING",
    "KEYCODE_LOCK",
    "KEYCODE_EMOJI_PICKER",
    "KEYCODE_KEYBOARD_BACKLIGHT_DOWN",
    "KEYCODE_KEYBOARD_BACKLIGHT_UP",
    "KEYCODE_KEYBOARD_BACKLIGHT_TOGGLE",
    "KEYCODE_RECENT_APPS",
    "KEYCODE_PROFILE_SWITCH",
    "KEYCODE_DO_NOT_DISTURB",
    "KEYCODE_META_LEFT",
    "KEYCODE_META_RIGHT",
    "KEYCODE_DICTATE",
    "KEYCODE_ACCESSIBILITY",
    "KEYCODE_CONTEXTUAL_SEARCH",
    "KEYCODE_CONTEXTUAL_QUERY",
    "KEYCODE_CONTEXTUAL_INSERT",
    "KEYCODE_CAMERA",
    "KEYCODE_FOCUS",
    "KEYCODE_EXPLORER",
    "KEYCODE_ENVELOPE",
    "KEYCODE_CONTACTS",
    "KEYCODE_CALENDAR",
    "KEYCODE_MUSIC",
    "KEYCODE_CALCULATOR",
    "KEYCODE_BOOKMARK",
    "KEYCODE_GUIDE",
    "KEYCODE_DVR",
    "KEYCODE_TV",
    "KEYCODE_TV_INPUT",
    "KEYCODE_WINDOW",
    "KEYCODE_STYLUS_BUTTON_TAIL",
    "KEYCODE_AVR_INPUT",
    "KEYCODE_STB_INPUT",
)


REFUSED = [*EXPECTED_REFUSED, *PREFIX_EXAMPLES]


def test_the_refused_sets_are_exactly_these():
    assert frozenset(EXPECTED_REFUSED) == DEVICE_WIDE_KEYS
    assert len(EXPECTED_REFUSED) == len(set(EXPECTED_REFUSED))
    assert DEVICE_WIDE_KEY_PREFIXES == EXPECTED_PREFIXES


def test_every_prefix_has_an_example():
    for prefix in DEVICE_WIDE_KEY_PREFIXES:
        assert any(name.startswith(prefix) for name in PREFIX_EXAMPLES)


@pytest.mark.parametrize("name", REFUSED)
def test_key_refuses_a_device_wide_key(device, android, fake_adb, name):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="whole device") as info:
        device.key(name)
    assert str(info.value).startswith(f"{name} acts on the whole device")
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


@pytest.mark.parametrize("name", REFUSED)
def test_cli_refuses_a_device_wide_key(capsys, fake_adb, android, name):
    code, out, _ = invoke(capsys, fake_adb, "key", name, "--json")
    assert code == 2
    error = json.loads(out)["error"]
    assert error["code"] == "usage_error"
    assert error["message"].startswith(f"{name} acts on the whole device")
    assert fake_adb.requests == []
    assert android.inputs == []


def test_an_alias_to_a_refused_key_is_refused(monkeypatch, device, android, fake_adb):
    monkeypatch.setitem(_KEY_CODES, "off", "KEYCODE_POWER")
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="whole device"):
        device.key("off")
    assert len(fake_adb.requests) == before
    assert android.inputs == []


class _ConstantHash(str):
    __slots__ = ()

    def __hash__(self):
        return 0


class _NoStartswith(str):
    __slots__ = ()

    def startswith(self, *_args, **_kwargs):
        return False


@pytest.mark.parametrize(
    "name",
    [_ConstantHash("KEYCODE_POWER"), _NoStartswith("KEYCODE_MEDIA_PLAY")],
    ids=["constant-hash", "no-startswith"],
)
def test_a_str_subclass_cannot_bypass_the_refusal(device, android, fake_adb, name):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="whole device"):
        device.key(name)
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_a_harmless_str_subclass_sends_the_plain_code(device, fake_adb):
    class Name(str):
        __slots__ = ()

    assert device.key(Name("back")) == "KEYCODE_BACK"
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_BACK" + SUFFIX]


def test_no_alias_maps_to_a_refused_key():
    for alias, code in KEY_ALIASES:
        assert code not in DEVICE_WIDE_KEYS, alias
        assert not code.startswith(DEVICE_WIDE_KEY_PREFIXES), alias


def test_device_wide_keys_cannot_be_changed():
    assert isinstance(DEVICE_WIDE_KEYS, frozenset)
    assert isinstance(DEVICE_WIDE_KEY_PREFIXES, tuple)
    assert not hasattr(DEVICE_WIDE_KEYS, "add")


@pytest.mark.parametrize(
    "name",
    [
        "KEYCODE_BACK",
        "KEYCODE_HOME",
        "KEYCODE_APP_SWITCH",
        "KEYCODE_MENU",
        "KEYCODE_SEARCH",
        "KEYCODE_ENTER",
        "KEYCODE_DEL",
        "KEYCODE_TAB",
        "KEYCODE_ESCAPE",
        "KEYCODE_SPACE",
        "KEYCODE_DPAD_UP",
        "KEYCODE_PAGE_DOWN",
        "KEYCODE_A",
        "KEYCODE_7",
    ],
)
def test_navigation_keys_stay_allowed(device, fake_adb, name):
    assert device.key(name) == name
    assert sent_inputs(fake_adb) == [f"input keyevent {name}" + SUFFIX]
