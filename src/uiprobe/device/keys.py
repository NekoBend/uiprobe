"""Key names: the aliases, the device-wide keys uiprobe refuses, and `RefusedKeys`."""

from __future__ import annotations

import re
from typing import Final

# Key names `Device.key` accepts besides a full ``KEYCODE_`` name, as
# (alias, key code) pairs; a tuple so a caller cannot change them. A bare
# number is refused: on API 24-36 ``input keyevent 0`` typed the character 0
# into a focused field, so a number is not reliably read as a key code.
KEY_ALIASES: Final[tuple[tuple[str, str], ...]] = (
    ("back", "KEYCODE_BACK"),
    ("home", "KEYCODE_HOME"),
    ("enter", "KEYCODE_ENTER"),
    ("delete", "KEYCODE_DEL"),
    ("forward_delete", "KEYCODE_FORWARD_DEL"),
    ("tab", "KEYCODE_TAB"),
    ("escape", "KEYCODE_ESCAPE"),
    ("menu", "KEYCODE_MENU"),
    ("search", "KEYCODE_SEARCH"),
    ("recents", "KEYCODE_APP_SWITCH"),
    ("up", "KEYCODE_DPAD_UP"),
    ("down", "KEYCODE_DPAD_DOWN"),
    ("left", "KEYCODE_DPAD_LEFT"),
    ("right", "KEYCODE_DPAD_RIGHT"),
    ("center", "KEYCODE_DPAD_CENTER"),
    ("page_up", "KEYCODE_PAGE_UP"),
    ("page_down", "KEYCODE_PAGE_DOWN"),
    ("space", "KEYCODE_SPACE"),
)


_KEY_CODES: Final[dict[str, str]] = dict(KEY_ALIASES)


# Keys that act on the whole device, not on the app in front; `Device.key`
# refuses them before anything is sent. The names follow Android's KeyEvent
# constants; some exist only on newer Android versions.
# A name that does not exist on a device is still refused here.
DEVICE_WIDE_KEYS: Final[frozenset[str]] = frozenset(
    {
        # power and sleep
        "KEYCODE_POWER",
        "KEYCODE_SLEEP",
        "KEYCODE_WAKEUP",
        "KEYCODE_SOFT_SLEEP",
        "KEYCODE_TV_POWER",
        "KEYCODE_AVR_POWER",
        "KEYCODE_STB_POWER",
        # calls
        "KEYCODE_CALL",
        "KEYCODE_ENDCALL",
        "KEYCODE_HEADSETHOOK",
        # sound (the prefix KEYCODE_MEDIA_ covers global media playback)
        "KEYCODE_VOLUME_UP",
        "KEYCODE_VOLUME_DOWN",
        "KEYCODE_VOLUME_MUTE",
        "KEYCODE_MUTE",
        # screen
        "KEYCODE_BRIGHTNESS_UP",
        "KEYCODE_BRIGHTNESS_DOWN",
        "KEYCODE_SYSRQ",
        "KEYCODE_SCREENSHOT",
        # system UI and assistants (the prefix KEYCODE_SYSTEM_NAVIGATION_
        # covers the system navigation keys)
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
        # keys that open other apps (the prefixes KEYCODE_TV_,
        # KEYCODE_VIDEO_APP_, KEYCODE_FEATURED_APP_, KEYCODE_DEMO_APP_ and
        # KEYCODE_STEM_ cover television and watch keys)
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
    }
)


# The keys that stay refused whatever a settings file says: power, sleep and
# calls. They are always part of `DEVICE_WIDE_KEYS`, and the settings file
# cannot remove them.
FIXED_DEVICE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "KEYCODE_POWER",
        "KEYCODE_SLEEP",
        "KEYCODE_SOFT_SLEEP",
        "KEYCODE_LOCK",
        "KEYCODE_CALL",
        "KEYCODE_ENDCALL",
        "KEYCODE_HEADSETHOOK",
        "KEYCODE_TV_POWER",
        "KEYCODE_AVR_POWER",
        "KEYCODE_STB_POWER",
    }
)


# Name prefixes refused as well: global media playback, system navigation,
# television controls, and keys that launch an app assigned on a TV device or
# are handled by the system on a watch. Not refused on purpose: SEARCH, the
# channel keys, CAPTIONS, 3D_MODE and MACRO_n (they normally act on the app in
# front).
DEVICE_WIDE_KEY_PREFIXES: Final[tuple[str, ...]] = (
    "KEYCODE_MEDIA_",
    "KEYCODE_SYSTEM_NAVIGATION_",
    "KEYCODE_TV_",
    "KEYCODE_VIDEO_APP_",
    "KEYCODE_FEATURED_APP_",
    "KEYCODE_DEMO_APP_",
    "KEYCODE_STEM_",
)


_KEYCODE_NAME: Final = re.compile(r"KEYCODE_[A-Z0-9_]{1,40}")


# ``KEYCODE_26`` is no key name; Android before API 29 may read the digits as
# a key code, so two or more digits are refused.
_KEYCODE_DIGITS: Final = re.compile(r"KEYCODE_[0-9]{2,}")


_NUMBER: Final = re.compile(r"[0-9]+")


class RefusedKeys:
    """The key names `Device.key` refuses, in place of the built-in lists.

    ``keys`` are exact ``KEYCODE_`` names and ``prefixes`` are name prefixes (a
    key whose name starts with one is refused). The names must match
    ``KEYCODE_[A-Z0-9_]{1,40}``. The keys of `FIXED_DEVICE_KEYS` are refused
    whatever is given here. The settings file's lists come as
    ``RefusedKeys(frozenset(settings.refused_keys), settings.refused_key_prefixes,
    str(settings.source))``.

    ``source`` names where the lists come from (a settings file path); it only
    appears in the refusal message. Without it the message says "the
    refused-keys list", which makes no claim about a file.

    Raises:
        TypeError: A name or the source is not a str.
        ValueError: A name does not match the pattern.
    """

    __slots__ = ("_keys", "_prefixes", "_source")

    def __init__(
        self,
        keys: frozenset[str],
        prefixes: tuple[str, ...],
        source: str = "the refused-keys list",
    ) -> None:
        self._keys = frozenset(_exact_key_name(name) for name in keys)
        self._prefixes = tuple(_exact_key_name(name) for name in prefixes)
        if not isinstance(source, str):
            msg = f"source must be a str, not {type(source).__name__}"
            raise TypeError(msg)
        self._source = str.__str__(source)

    @property
    def keys(self) -> frozenset[str]:
        """The exact key names that are refused."""
        return self._keys

    @property
    def prefixes(self) -> tuple[str, ...]:
        """The name prefixes that are refused."""
        return self._prefixes

    @property
    def source(self) -> str:
        """Where the lists come from, as the refusal message names it."""
        return self._source

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RefusedKeys):
            return NotImplemented
        return (self._keys, self._prefixes, self._source) == (
            other._keys,
            other._prefixes,
            other._source,
        )

    def __hash__(self) -> int:
        return hash((self._keys, self._prefixes, self._source))

    def __repr__(self) -> str:
        return (
            f"RefusedKeys(keys={self._keys!r}, prefixes={self._prefixes!r},"
            f" source={self._source!r})"
        )


def _exact_key_name(name: object) -> str:
    if not isinstance(name, str):
        msg = f"a key name must be a str, not {type(name).__name__}"
        raise TypeError(msg)
    exact = str.__str__(name)
    if not _KEYCODE_NAME.fullmatch(exact):
        msg = f"not a key name or prefix: {exact[:60]!r}"
        raise ValueError(msg)
    return exact


def _refuse_device_wide(code: str, refused: RefusedKeys | None = None) -> str:
    """Return `code`, or raise when the key acts on the whole device.

    `refused` None means the built-in lists; the fixed keys are refused either way.
    """
    if refused is None:
        wide = code in DEVICE_WIDE_KEYS or code.startswith(DEVICE_WIDE_KEY_PREFIXES)
    elif code in FIXED_DEVICE_KEYS:
        wide = True
    elif code.startswith(refused.prefixes):
        prefix = next(item for item in refused.prefixes if code.startswith(item))
        msg = (
            f"{code} is refused because {refused.source} lists the prefix {prefix}; "
            f"changing that prefix affects every key that starts with it"
        )
        if code in refused.keys:
            msg += f"; {code} is also listed by name"
        raise ValueError(msg)
    elif code in refused.keys:
        msg = f"{code} is refused by {refused.source}; remove it there to send it"
        raise ValueError(msg)
    else:
        wide = False
    if wide:
        msg = (
            f"{code} acts on the whole device, not on the app in front; "
            "uiprobe does not send it"
        )
        raise ValueError(msg)
    return code


def _key_code(name: object, refused: RefusedKeys | None = None) -> str:
    """Return the key code name for `name`, an alias or a ``KEYCODE_`` name."""
    if not isinstance(name, str):
        msg = f"a key name must be a str, not {type(name).__name__}"
        raise TypeError(msg)
    # A str subclass can override __hash__ or startswith and slip past the
    # refusal; work on an exact str copy from here on.
    name = str.__str__(name)
    if _NUMBER.fullmatch(name):
        msg = (
            "key numbers are not accepted because input keyevent can read them "
            "as digits; use a KEYCODE_ name such as KEYCODE_BACK"
        )
        raise ValueError(msg)
    alias = _KEY_CODES.get(name.lower()) if name.isascii() else None
    if alias is not None:
        return _refuse_device_wide(alias, refused)
    if _KEYCODE_DIGITS.fullmatch(name):
        msg = (
            f"{name} is not a key name, and input keyevent may read the digits "
            "as a key code; use the real name such as KEYCODE_BACK"
        )
        raise ValueError(msg)
    if _KEYCODE_NAME.fullmatch(name):
        return _refuse_device_wide(name, refused)
    msg = (
        f"unknown key {name[:60]!r}: use one of {', '.join(_KEY_CODES)} "
        "or a KEYCODE_ name in upper case"
    )
    raise ValueError(msg)
