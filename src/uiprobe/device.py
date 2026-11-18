"""One Android device: choosing it, running commands on it, dumping its screen."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, NamedTuple

from uiprobe.adb import (
    AdbClient,
    DeviceInfo,
    ascii_int,
    find_adb,
    quote_args,
    resolve_server_address,
    start_blocked_by_environment,
)
from uiprobe.crawl_preview import check_package
from uiprobe.errors import (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    DumpError,
    InputFailedError,
    LaunchFailedError,
    MultipleDevicesError,
    ScreenshotFailedError,
    UiprobeError,
)
from uiprobe.model import (
    Bounds,
    Element,
    Screen,
    parse_uiautomator_dump,
    shorten_activity,
)
from uiprobe.pngfile import check_png

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "DEVICE_WIDE_KEYS",
    "DEVICE_WIDE_KEY_PREFIXES",
    "FIXED_DEVICE_KEYS",
    "KEY_ALIASES",
    "TYPE_PIECE_SIZE",
    "Device",
    "DeviceDetails",
    "RefusedKeys",
    "TextNotTypedError",
    "device_details",
    "devices",
    "select_device",
]

READY_STATE: Final = "device"
DEFAULT_LAUNCH_TIMEOUT: Final = 10.0
LAUNCH_POLL_INTERVAL: Final = 0.5
MAX_LAUNCH_TIMEOUT: Final = 600.0
_EVENTS_INJECTED = re.compile(r"Events injected: ([0-9]+)")
_NO_ACTIVITIES: Final = "** No activities found"
_FOREGROUND_LIMIT: Final = 1000
DUMP_DIRECTORY: Final = "/data/local/tmp"
DUMP_MESSAGE_LIMIT: Final = 500
# `uiautomator dump` prints nothing until it finishes, and on an animating
# screen it can wait about 10 s for the UI to settle before it gives up. The
# client's 10 s silence limit would fire first, so the dump gets its own.
DUMP_TIMEOUT: Final = 60.0
# `screencap` prints nothing until the image is encoded, which a slow device
# can take longer than the client's 10 s silence limit.
SCREENSHOT_TIMEOUT: Final = 30.0
# Failures that leave optional metadata unknown instead of failing the call.
_OPTIONAL_ERRORS: Final = (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
)

_WM_SIZE_LINE: Final = re.compile(
    r"(Physical|Override) size: ([0-9]{1,5})x([0-9]{1,5})"
)
_MAX_DISPLAY_SIDE: Final = 32768
_DISPLAY_ZERO_LINE: Final = re.compile(r"\s*Display: mDisplayId=0(?:\s|$)")
_CURRENT_SIZE: Final = re.compile(r"cur=([0-9]{1,5})x([0-9]{1,5})")

# Input commands. The device-side exit status is read through a marker, because
# the adb ``shell:`` service does not return it. Measured on API 24-36: the
# status is 0 even for a nonexistent ``input`` subcommand, so 0 does not prove
# success by itself; failure also prints usage text or an exception.
EXIT_MARKER: Final = "uiprobe-exit:"
LONG_PRESS_MS: Final = 800
# A tap holds still this long by default (``input swipe X Y X Y 10``), so that
# the "Pointer location" developer option shows it; a plain ``input tap`` is
# barely visible there (measured on one device, a Galaxy S25 Ultra at 60 Hz:
# 10 ms visible, 6 ms not). The cap stays below Android's usual default
# long-press time (about 400 to 500 ms, longer with accessibility settings);
# an app may use its own shorter threshold.
DEFAULT_PRESS_MS: Final = 10
MAX_PRESS_MS: Final = 200
_EXIT_SUFFIX: Final = f"; echo {EXIT_MARKER}$?"
_MAX_COORDINATE: Final = 32767
_OUTPUT_LIMIT: Final = 200
_EXIT_VALUE: Final = re.compile(r"0|[1-9][0-9]{0,9}")

DEFAULT_SWIPE_MS: Final = 300
_MAX_SWIPE_MS: Final = 10000
# Seconds added to a swipe's own duration for the shell call's read timeout.
_SWIPE_SLACK: Final = 10.0
# A scroll starts and ends this share of the area's width (height) inside its
# left and right (top and bottom) edges, then keeps this share of the display
# away from the display edges, where the system gestures live.
SCROLL_INSET_PERCENT: Final = 20
SCROLL_EDGE_PERCENT: Final = 5
_DIRECTIONS: Final = frozenset({"up", "down", "left", "right"})

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


def _check_coordinate(value: object) -> int:
    """Return `value` when it is a usable coordinate, else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"a coordinate must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    value = int.__index__(value)  # an exact int, whatever the subclass overrides
    if not 0 <= value <= _MAX_COORDINATE:
        msg = f"a coordinate must be from 0 to {_MAX_COORDINATE}, not {value}"
        raise ValueError(msg)
    return value


def _press_point(target: object) -> tuple[int, int]:
    """Return the point to press for `target` (an ``(x, y)`` pair)."""
    if not isinstance(target, tuple | list):
        name = type(target).__name__
        msg = f"target must be an Element or an (x, y) pair, not {name}"
        raise TypeError(msg)
    if len(target) != 2:
        msg = f"a point needs exactly two coordinates, not {len(target)}"
        raise TypeError(msg)
    return _check_coordinate(target[0]), _check_coordinate(target[1])


def _check_duration(value: object) -> int:
    """Return `value` when it is a usable swipe duration in ms, else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"duration_ms must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    value = int.__index__(value)
    if not 1 <= value <= _MAX_SWIPE_MS:
        msg = f"duration_ms must be from 1 to {_MAX_SWIPE_MS}, not {value}"
        raise ValueError(msg)
    return value


def _check_press_ms(value: object) -> int:
    """Return `value` when it is a usable tap hold in ms (0 to 200), else raise."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"press_ms must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    value = int.__index__(value)
    if not 0 <= value <= MAX_PRESS_MS:
        msg = f"press_ms must be from 0 to {MAX_PRESS_MS}, not {value}"
        raise ValueError(msg)
    return value


def _scroll_rectangle(area: Bounds, display: tuple[int, int]) -> Bounds:
    """Shrink `area` and keep it away from the display edges (floor division)."""
    inset_x = area.width * SCROLL_INSET_PERCENT // 100
    inset_y = area.height * SCROLL_INSET_PERCENT // 100
    margin_x = display[0] * SCROLL_EDGE_PERCENT // 100
    margin_y = display[1] * SCROLL_EDGE_PERCENT // 100
    return Bounds(
        max(area.left + inset_x, margin_x),
        max(area.top + inset_y, margin_y),
        min(area.right - inset_x, display[0] - margin_x),
        min(area.bottom - inset_y, display[1] - margin_y),
    )


def _scroll_points(
    direction: str, area: Bounds, display: tuple[int, int]
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return the swipe points for `direction` in `area` of a `display`.

    Raises ValueError when the area is too small to scroll.
    """
    box = _scroll_rectangle(area, display)
    # Under 20 pixels the 5% margin floors to 0, and a far edge would be
    # one past the last pixel; such a display is not a real one.
    if min(display) < 20 or box.width < 2 or box.height < 2:
        msg = "the area is too small to scroll"
        raise ValueError(msg)
    middle_x, middle_y = box.center
    # Right and bottom are exclusive, so the last pixel is one before them.
    last_x, last_y = box.right - 1, box.bottom - 1
    match direction:
        case "up":
            return (middle_x, last_y), (middle_x, box.top)
        case "down":
            return (middle_x, box.top), (middle_x, last_y)
        case "left":
            return (last_x, middle_y), (box.left, middle_y)
        case _:
            return (box.left, middle_y), (last_x, middle_y)


def _check_direction(direction: str) -> None:
    if not isinstance(direction, str):
        msg = f"direction must be a str, not {type(direction).__name__}"
        raise TypeError(msg)
    if direction not in _DIRECTIONS:
        msg = "direction must be one of up, down, left, right"
        raise ValueError(msg)


class DisplaySizeUnknownError(ValueError):
    """`Device.scroll_display` could not read the display size."""


def _scroll_area(screen: Screen, within: Element | None) -> Bounds:
    """Return the area a scroll works in: the visible part of `within` or all."""
    if within is None:
        return Bounds(0, 0, screen.display[0], screen.display[1])
    if not any(e is within for e in screen.elements):
        msg = (
            "this element is not from the latest dump of this device; "
            "dump again and use an element of that screen"
        )
        raise ValueError(msg)
    return screen.require_visible(within)


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


TYPE_PIECE_SIZE: Final = 256
"""Most characters of text sent in one ``input text`` command."""


class TextNotTypedError(ValueError):
    """The text has a character, or is empty, so that it is not typed."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = details or {}


def _check_text(text: object) -> str:
    """Return `text` when it can be typed; raise before anything is sent."""
    if not isinstance(text, str):
        msg = f"text must be a str, not {type(text).__name__}"
        raise TypeError(msg)
    if not text:
        msg = "text is empty; there is nothing to type"
        raise TextNotTypedError(msg)
    for position, char in enumerate(text):
        code = ord(char)
        if 0x20 <= code <= 0x7E:
            continue
        label = f"U+{code:04X}"
        escaped = ascii(char)
        head = f"character {label} ({escaped}) at position {position}"
        if code > 0x7F:
            msg = f"{head} is not ASCII; this version types ASCII text only"
        elif char in "\n\t":
            msg = (
                f"{head} is not typed: a newline or tab can act as Enter or"
                " move the focus; to press Enter use uiprobe key enter"
            )
        else:
            msg = f"{head} is a control character; only printable ASCII is typed"
        details: dict[str, object] = {
            "character": label,
            "escaped": escaped,
            "position": position,
        }
        raise TextNotTypedError(msg, details)
    return text


def _single_quoted(piece: str) -> str:
    """`piece` in single quotes for the device shell, even a plain word."""
    return "'" + "'\"'\"'".join(piece.split("'")) + "'"


def _text_pieces(text: object) -> list[str]:
    """Split `text` into the pieces to send, after checking it.

    A piece has at most `TYPE_PIECE_SIZE` characters and never contains ``%``
    directly followed by ``s`` (the device turns that pair into a space). The
    pieces joined give `text` again.
    """
    pieces: list[str] = []
    current = ""
    for char in _check_text(text):
        if len(current) >= TYPE_PIECE_SIZE or (char == "s" and current[-1:] == "%"):
            pieces.append(current)
            current = ""
        current += char
    pieces.append(current)
    return pieces


def _without_text(error: UiprobeError, secrets_: tuple[str, ...]) -> UiprobeError:
    """Copy of `error` with the typed text left out of its message and details.

    An adb error carries the request that failed, and for typing that request
    holds the text, which can be a password.
    """

    def clean(value: object) -> object:
        if not isinstance(value, str):
            return value
        for secret in secrets_:
            value = "<text omitted>".join(value.split(secret))
        return value

    details = {key: clean(value) for key, value in error.details.items()}
    message = str(clean(error.message))
    return type(error)(message, details=details)


def _check_launch_timeout(timeout: object) -> float:
    """Return `timeout` as a float, read through the base type's own method.

    An int or float subclass cannot change the value checked here: the
    number is converted first, with ``int.__float__`` or ``float.__float__``,
    and the converted value is the one checked and used.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        msg = f"timeout must be a number, not {type(timeout).__name__}"
        raise TypeError(msg)
    # The real type decides, not a `__class__` that claims another one.
    try:
        if isinstance(timeout, int) and not issubclass(type(timeout), float):
            value = int.__float__(timeout)
        elif isinstance(timeout, float):
            value = float.__float__(timeout)
        else:  # an int that is also a float cannot be made; kept for the types
            value = float("nan")
    except OverflowError:
        value = float("inf")
    if not 0 <= value <= MAX_LAUNCH_TIMEOUT:  # also false for NaN
        msg = f"timeout must be from 0 to {MAX_LAUNCH_TIMEOUT:g} seconds, not {value!r}"
        raise ValueError(msg)
    return value


def _escaped(text: str, limit: int = 200) -> str:
    """Return `text` cut to `limit` characters and escaped to printable ASCII."""
    return ascii(text[:limit])[1:-1]


def _shown_foreground(name: str) -> str:
    """Return `name` escaped; past 1000 characters it is cut and ends in ``...``.

    A name cut this way is never equal to a package name, which has at most
    255 characters.
    """
    if len(name) <= _FOREGROUND_LIMIT:
        return _escaped(name, _FOREGROUND_LIMIT)
    return _escaped(name, _FOREGROUND_LIMIT) + "..."


def _lines(text: str) -> list[str]:
    """Split shell output at line feeds only, dropping one CR at each line end."""
    return [line.removesuffix("\r") for line in text.split("\n")]


def _escaped_line(text: str) -> str:
    """Return the first line of `text` that is not blank, cut and escaped.

    Spaces and tabs around it are removed; a line made only of them is blank.
    """
    lines = (line.strip(" \t") for line in _lines(text))
    return _escaped(next((line for line in lines if line), ""))


def _input_failure(
    command: str, where: Mapping[str, object], text: str
) -> InputFailedError | None:
    """Judge the output of an input command; None means the device reported 0.

    `where` names what was sent (``point``, or ``from``, ``to`` and
    ``duration_ms``, or ``key``) and goes into the details after ``command``.
    """
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    lines = [line for line in lines if line]
    details: dict[str, object] = {"command": command, **where}
    marker = lines[-1] if lines and lines[-1].startswith(EXIT_MARKER) else None
    others = lines[:-1] if marker is not None else lines
    value = None if marker is None else marker[len(EXIT_MARKER) :]
    if value is None or not _EXIT_VALUE.fullmatch(value):
        reason = "no_exit_status"
        others = lines
    elif int(value) != 0:
        reason = "exit_status"
        details["exit_status"] = int(value)
    elif others:
        reason = "unexpected_output"
    else:
        return None
    details["reason"] = reason
    output = "\n".join(others).strip()[:_OUTPUT_LIMIT].rstrip()
    if output:
        details["output"] = output
    messages = {
        "no_exit_status": "the device did not report an exit status for the input",
        "exit_status": f"input failed on the device with exit status {value}",
        "unexpected_output": "input printed a line other than the exit status marker",
    }
    return InputFailedError(messages[reason], details=details)


def _parse_wm_size(text: str) -> tuple[int, int] | None:
    """Parse ``wm size`` output; the override wins over the physical size.

    Returns None unless every non-blank line is a well-formed size line, none
    is repeated, and at least one is present.
    """
    sizes: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _WM_SIZE_LINE.fullmatch(line.strip())
        if match is None or match[1] in sizes:
            return None
        width, height = int(match[2]), int(match[3])
        if not (0 < width <= _MAX_DISPLAY_SIDE and 0 < height <= _MAX_DISPLAY_SIDE):
            return None
        sizes[match[1]] = (width, height)
    return sizes.get("Override") or sizes.get("Physical")


def _parse_display_size(text: str) -> tuple[int, int] | None:
    """Parse the current size of display 0 from ``dumpsys window displays``.

    The block starts at a line ``Display: mDisplayId=0`` that ends there or
    goes on after whitespace, and runs up to the next ``Display:`` line. The
    size is read only from the summary line of the block, the first line that
    starts (after indentation) with ``init=``, and is already rotated. Returns
    None for anything unexpected: no such block, no summary line before the
    next display, no ``cur=`` token on it, a malformed one, several with
    different values, or a side that is zero or over the bound `wm size`
    parsing uses.
    """
    inside = False
    for line in text.splitlines():
        if not inside:
            inside = _DISPLAY_ZERO_LINE.match(line) is not None
            continue
        if line.lstrip().startswith("Display:"):
            return None
        if not line.lstrip().startswith("init="):
            continue
        sizes: set[tuple[int, int]] = set()
        for token in line.split():
            if not token.startswith("cur="):
                continue
            match = _CURRENT_SIZE.fullmatch(token)
            if match is None:
                return None
            width, height = int(match[1]), int(match[2])
            if not (0 < width <= _MAX_DISPLAY_SIDE and 0 < height <= _MAX_DISPLAY_SIDE):
                return None
            sizes.add((width, height))
        if len(sizes) != 1:
            return None
        return sizes.pop()
    return None


_COMPONENT = r"([A-Za-z0-9_.$]{1,256})/([A-Za-z0-9_.$]{1,256})"
_RECORD = r"ActivityRecord\{[0-9a-f]{1,16} u[0-9]{1,3} " + _COMPONENT
_TASK_SUFFIX = r"(?: t-?[0-9]{1,9})?(?: [a-z]{1,16})?"
_MAX_DUMPSYS_LINE = 4096


class _Source(NamedTuple):
    """One place in a dump that names the focused or resumed activity."""

    claims: re.Pattern[str]  # any line that claims to be this field
    shape: re.Pattern[str]  # the exact shape a genuine line has


# Window titles and some activity-manager strings are chosen by apps and may
# hold newlines, so an app can print a line that imitates any field. A forged
# line can be added but a genuine one cannot be removed, so a source counts
# only when every line that claims its field is well formed and they all name
# the same component. A window title is never a source: the activity comes
# only from the system's activity records.
_FOCUSED_APP_SOURCE: Final = _Source(
    re.compile(r"  mFocusedApp="),
    re.compile(
        r"  mFocusedApp=(?:AppWindowToken\{[0-9a-f]{1,16} "
        r"token=Token\{[0-9a-f]{1,16} )?" + _RECORD + _TASK_SUFFIX + r"\}{1,3}"
    ),
)
# `dumpsys activity activities` also prints app-chosen strings. The field is
# written ``name=`` on some versions and ``name:`` on others, and its
# indentation varies, so any indentation is accepted.
_RESUMED_SOURCE: Final = _Source(
    re.compile(r"\s*(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:]"),
    re.compile(
        r"\s*(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:] ?"
        + _RECORD
        + _TASK_SUFFIX
        + r"\}"
    ),
)
_ABSENT: Final = object()


def _read_source(text: str, source: _Source) -> tuple[str, str] | object | None:
    """Return the component `source` names, None if unusable, or `_ABSENT`.

    `_ABSENT` means the source says nothing: no line claims the field, or the
    only line is not a record (``null``), so nothing forged can hide behind it.
    None means the source cannot be trusted: several different lines claim
    the field, or they name different components.
    """
    lines = {
        line.rstrip("\r") for line in text.split("\n") if source.claims.match(line)
    }
    if not lines:
        return _ABSENT
    found: set[tuple[str, str]] = set()
    for line in lines:
        match = source.shape.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
        if match is None:
            return _ABSENT if len(lines) == 1 else None
        found.add((match[1], match[2]))
    return found.pop() if len(found) == 1 else None


_BOOL = r"(true|false)"
# Both genuine shapes of the line that carries ``mInputShown``: API 24-32 print
# it at the end of one flag line, API 36 on a line of its own (API 33-35 were
# not captured, so any indentation is accepted for the own-line form).
_INPUT_SHOWN_SHAPES: Final = (
    re.compile(
        rf"  mShowRequested={_BOOL} mShowExplicitlyRequested={_BOOL}"
        rf" mShowForced={_BOOL} mInputShown={_BOOL}"
    ),
    re.compile(rf"[ \t]*mInputShown={_BOOL}"),
)


def _read_input_shown(text: str) -> bool | None:
    """Return the soft keyboard state from ``dumpsys input_method`` output.

    The output holds app-chosen strings that may contain newlines, so every
    line that contains ``mInputShown=`` claims the field. The result is a bool
    only when each claiming line is a genuine shape within `_MAX_DUMPSYS_LINE`
    and all of them agree; otherwise (or with no claiming line) it is None.
    """
    values: set[str] = set()
    for raw in text.split("\n"):
        line = raw.removesuffix("\r")
        if "mInputShown=" not in line:
            continue
        if len(line) > _MAX_DUMPSYS_LINE:
            return None
        for shape in _INPUT_SHOWN_SHAPES:
            match = shape.fullmatch(line)
            if match is not None:
                values.add(match[match.lastindex or 1])
                break
        else:
            return None
    if len(values) != 1:
        return None
    return values.pop() == "true"


def _brief(candidates: Sequence[DeviceInfo]) -> list[dict[str, object]]:
    return [{"serial": d.serial, "state": d.state} for d in candidates]


def _state_error(
    state: str, message: str, details: Mapping[str, object]
) -> DeviceUnauthorizedError | DeviceOfflineError:
    if state == "unauthorized":
        return DeviceUnauthorizedError(message, details=details)
    return DeviceOfflineError(message, details=details)


def _check_serial(serial: object) -> None:
    if serial is not None and not isinstance(serial, str):
        msg = f"serial must be a string, not {type(serial).__name__}"
        raise TypeError(msg)


def _select_requested(candidates: Sequence[DeviceInfo], serial: str) -> DeviceInfo:
    for candidate in candidates:
        if candidate.serial != serial:
            continue
        if candidate.state == READY_STATE:
            return candidate
        raise _state_error(
            candidate.state,
            f"device {serial!r} is {candidate.state}",
            {"serial": serial, "state": candidate.state},
        )
    raise DeviceNotFoundError(
        f"device {serial!r} is not connected",
        details={"serial": serial, "devices": _brief(candidates)},
    )


def select_device(
    candidates: Sequence[DeviceInfo],
    serial: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> DeviceInfo:
    """Choose the device to use from the adb device list.

    The requested serial is `serial`, else ``ANDROID_SERIAL`` (empty counts as
    unset). An empty `serial` argument is a request for a device named ``""``,
    which is not connected. Without a requested serial, only devices in the
    ``device`` state count.

    Raises:
        TypeError: `serial` is not a string.
        DeviceNotFoundError: The requested device is absent, or none is connected.
        MultipleDevicesError: More than one usable device and none requested.
        DeviceUnauthorizedError: The only candidates wait for authorization.
        DeviceOfflineError: The only candidates are in another unusable state.
    """
    env = os.environ if environ is None else environ
    _check_serial(serial)
    requested = serial if serial is not None else env.get("ANDROID_SERIAL") or None
    if requested is not None:
        return _select_requested(candidates, requested)
    ready = [c for c in candidates if c.state == READY_STATE]
    if len(ready) == 1:
        return ready[0]
    if len(ready) > 1:
        serials = [c.serial for c in ready]
        raise MultipleDevicesError(
            f"{len(ready)} devices are connected; choose one with --serial",
            details={"serials": serials},
        )
    if not candidates:
        raise DeviceNotFoundError("no device is connected", details={"devices": []})
    states = {c.state for c in candidates}
    raise _state_error(
        "unauthorized" if "unauthorized" in states else "",
        "no device is ready: "
        + ", ".join(f"{c.serial} is {c.state}" for c in candidates),
        {"devices": _brief(candidates)},
    )


def _make_client(
    adb_server: str | None, adb: str | os.PathLike[str] | None, *, start_server: bool
) -> AdbClient:
    """Build the client; look for the adb executable only if it can be used.

    adb is run only to start a server nobody chose an address for, so a stale
    ``UIPROBE_ADB`` must not break a command that never needs it. An adb path
    given as an argument is still checked, to catch typos.

    Wrong argument types raise `TypeError` here, before any connection.
    """
    if not isinstance(start_server, bool):
        msg = f"start_server must be a bool, not {type(start_server).__name__}"
        raise TypeError(msg)
    address = resolve_server_address(adb_server)
    may_start = (
        start_server and address.is_default and not start_blocked_by_environment()
    )
    adb_path = find_adb(adb) if may_start or adb is not None else None
    return AdbClient(address, adb_path=adb_path, start_server=start_server)


def devices(
    *,
    adb_server: str | None = None,
    adb: str | os.PathLike[str] | None = None,
    start_server: bool = True,
) -> list[DeviceInfo]:
    """List the devices the adb server knows, in every state."""
    return _make_client(adb_server, adb, start_server=start_server).devices()


@dataclass(frozen=True, slots=True)
class DeviceDetails:
    """A listed device with what could be learned about it.

    `api_level` and `emulator` are None for a device that is not ready and for a
    value that could not be read.
    """

    info: DeviceInfo
    api_level: int | None = None
    emulator: bool | None = None


def _getprop(client: AdbClient, serial: str, name: str) -> str | None:
    """Return a device property, or None when it cannot be read."""
    try:
        raw = client.shell(serial, quote_args("getprop", name))
    except UiprobeError:
        return None
    return raw.decode("utf-8", errors="replace").strip()


def device_details(
    *,
    adb_server: str | None = None,
    adb: str | os.PathLike[str] | None = None,
    start_server: bool = True,
) -> list[DeviceDetails]:
    """List the devices and ask each ready one for its API level and kind.

    One client and one device listing serve every query. A property that
    cannot be read is None; it never fails the listing.
    """
    client = _make_client(adb_server, adb, start_server=start_server)
    details: list[DeviceDetails] = []
    for info in client.devices():
        api_level: int | None = None
        emulator: bool | None = None
        if info.state == READY_STATE:
            level = _getprop(client, info.serial, "ro.build.version.sdk")
            api_level = None if level is None else ascii_int(level)
            qemu = _getprop(client, info.serial, "ro.kernel.qemu")
            emulator = None if qemu is None else qemu == "1"
        details.append(DeviceDetails(info, api_level, emulator))
    return details


def _activity_name(package: str, component: tuple[str, str]) -> str:
    """Write the focused activity relative to the dumped package.

    A relative name (``.Main``) is first expanded with the component's package,
    and the result is then shortened against the dumped `package`.
    """
    component_package, name = component
    if name.startswith("."):
        name = component_package + name
    return shorten_activity(package, name)


class Device:
    """One connected Android device.

    The device is chosen when the object is created (see `select_device`), so
    a missing or ambiguous device fails here and not at the first operation.

    Raises:
        TypeError: An argument has the wrong type.
        ValueError: An argument has a bad value.

    Both are raised before the adb server is contacted.
    """

    def __init__(
        self,
        serial: str | None = None,
        *,
        adb_server: str | None = None,
        adb: str | os.PathLike[str] | None = None,
        start_server: bool = True,
    ) -> None:
        _check_serial(serial)
        self._client = _make_client(adb_server, adb, start_server=start_server)
        self._info = select_device(self._client.devices(), serial)
        self._api_level: int | None = None
        self._api_level_known = False
        self._latest_screen: Screen | None = None
        # True when the latest screen's display came from `wm size`, False when
        # it is the extent of the dumped windows; always set with the screen.
        self._latest_display_measured = False
        # The clock and the pause of `launch`; tests replace them.
        self._clock: Callable[[], float] = time.monotonic
        self._sleep: Callable[[float], None] = time.sleep

    @property
    def serial(self) -> str:
        """The adb serial of this device."""
        return self._info.serial

    @property
    def info(self) -> DeviceInfo:
        """What ``adb devices -l`` reported when the device was chosen."""
        return self._info

    def _shell(self, command: str) -> str:
        """Run `command` on the device and return its output as text."""
        return self._shell_text(command)

    def _shell_text(self, command: str, *, timeout: float | None = None) -> str:
        raw = self._client.shell(self.serial, command, timeout=timeout)
        return raw.decode("utf-8", errors="replace")

    def tap(
        self,
        target: Element | tuple[int, int] | list[int],
        *,
        long: bool = False,
        press_ms: int = DEFAULT_PRESS_MS,
    ) -> tuple[int, int]:
        """Press one point of the screen showing now; return the point pressed.

        `target` is an `Element` or a point ``(x, y)`` (a tuple or list of two
        ints, each 0 to 32767). An element is accepted only when it is one of
        the elements of the `Screen` returned by the latest `dump` of this
        device (the same object, not an equal copy); the point is then
        ``screen.tap_point(element)``, the center of the part of the element
        that is on the display, the same point ``uiprobe tap`` presses and
        ``uiprobe find`` prints as ``center``. An element that has no visible
        part (under 8 x 8 pixels counts as none) is not pressed, and neither is
        one that is covered: the topmost meaningful element at the point must
        be the element or a descendant of it (see `Screen.tap_point`). A point
        ``(x, y)`` given directly is pressed as given, with no check, because
        the caller named the point.

        By default a tap holds still for `press_ms` = 10 ms and runs ``input
        swipe X Y X Y 10`` (the point does not move). A plain ``input tap`` is
        barely visible in Android's "Pointer location" developer option; a 10 ms
        hold is (measured on one device, a Galaxy S25 Ultra at 60 Hz: 10 ms
        visible, 6 ms not). `press_ms` is an int from 0 to 200 (a bool is not
        accepted); 0 runs the plain ``input tap X Y``. The upper bound stays below
        Android's usual default long-press time, about 400 to 500 ms (longer
        with accessibility settings); an app may use its own shorter threshold,
        and a longer hold could turn into a long press. With `long` a press of
        800 ms runs ``input swipe X Y X Y 800``; it cannot be combined with a
        `press_ms` other than the default (``ValueError``). Every form is
        followed by ``echo uiprobe-exit:$?`` to read the device-side exit
        status. The command is sent exactly once, in one shell call. It is
        never retried or resent, not even after a connection error (that error
        propagates unchanged), because a resend could press twice.

        ``input`` prints usage text or an exception when it fails, and its exit
        status is not always nonzero then, so success needs the marker line
        ``uiprobe-exit:0`` as the last line and no other line that is not
        empty (empty lines are ignored). `InputFailedError` does not prove
        that nothing was pressed (``input`` may have acted before it failed),
        so callers must not blindly repeat the tap. The app in front is not
        checked.

        Raises:
            TypeError: `target`, `long` or `press_ms` has the wrong type, or a
                coordinate is not an int (a bool is not accepted).
            ValueError: A coordinate is outside 0 to 32767, `press_ms` is
                outside 0 to 200, `long` is combined with a `press_ms` other
                than the default, or `target` is an
                `Element` that is not one of the elements of the latest
                `dump` of this device (none yet, an older dump, another
                device, or built by hand). Dump again, or pass
                ``screen.tap_point(element)`` as a point.
            ElementNotVisibleError: The element has no visible part on the
                display of the latest dump, is under 8 x 8 pixels there, or is
                covered by another meaningful element at its press point
                (``details["reason"]`` says which); nothing is sent.
            InputFailedError: The device did not report success. ``details``
                hold ``command`` (``"tap"`` or ``"long_tap"``), ``point``,
                ``reason`` (``no_exit_status``, ``exit_status`` or
                ``unexpected_output``), ``exit_status`` for the second, and
                ``output`` when there is other text.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.
        """
        if not isinstance(long, bool):
            msg = f"long must be a bool, not {type(long).__name__}"
            raise TypeError(msg)
        hold = _check_press_ms(press_ms)
        if long and hold != DEFAULT_PRESS_MS:
            msg = "press_ms cannot be combined with long=True (a long press is 800 ms)"
            raise ValueError(msg)
        if isinstance(target, Element):
            latest = self._latest_screen
            if latest is None or not any(e is target for e in latest.elements):
                msg = (
                    "this element is not from the latest dump of this device; "
                    "dump again and use an element of that screen, or pass "
                    "screen.tap_point(element) as a point"
                )
                raise ValueError(msg)
            x, y = _press_point(latest.tap_point(target))
        else:
            x, y = _press_point(target)
        point = (x, y)
        if long or hold > 0:
            words = ("input", "swipe", str(x), str(y), str(x), str(y))
            words = (*words, str(LONG_PRESS_MS if long else hold))
        else:
            words = ("input", "tap", str(x), str(y))
        output = self._shell_text(quote_args(*words) + _EXIT_SUFFIX)
        failure = _input_failure(
            "long_tap" if long else "tap", {"point": list(point)}, output
        )
        if failure is not None:
            raise failure
        return point

    def swipe(
        self,
        start: tuple[int, int] | list[int],
        end: tuple[int, int] | list[int],
        *,
        duration_ms: int = DEFAULT_SWIPE_MS,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Drag one finger from `start` to `end`; return both points.

        `start` and `end` are points ``(x, y)`` (a tuple or list of two ints,
        each 0 to 32767; a bool is not an int). `duration_ms` is an int from 1
        to 10000. A swipe that does not move (`start` equals `end`) is a press
        and is refused; use `tap`.

        The command is ``input swipe X1 Y1 X2 Y2 MS`` followed by ``echo
        uiprobe-exit:$?`` (see `tap` for how the result is read). It is sent
        exactly once, in one shell call whose read timeout is `duration_ms`
        / 1000 + 10 seconds, and it is never retried or resent, not even after
        a connection error (that error propagates unchanged), because a
        resend could swipe twice. `InputFailedError` does not prove that
        nothing moved, so callers must not blindly repeat the swipe. The app in
        front is not checked, and a swipe from a screen edge can trigger a
        system gesture.

        Raises:
            TypeError: A point or `duration_ms` has the wrong type, or a
                coordinate is not an int (a bool is not accepted).
            ValueError: A coordinate is outside 0 to 32767, `duration_ms` is
                outside 1 to 10000, or `start` equals `end`.
            InputFailedError: The device did not report success. ``details``
                hold ``command`` (``"swipe"``), ``from``, ``to``,
                ``duration_ms``, ``reason`` (``no_exit_status``,
                ``exit_status`` or ``unexpected_output``), ``exit_status`` for
                the second, and ``output`` when there is other text.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.
        """
        first = _press_point(start)
        last = _press_point(end)
        duration = _check_duration(duration_ms)
        if first == last:
            msg = "a swipe that does not move is a press; use tap"
            raise ValueError(msg)
        words = ("input", "swipe", *map(str, (*first, *last, duration)))
        output = self._shell_text(
            quote_args(*words) + _EXIT_SUFFIX,
            timeout=duration / 1000 + _SWIPE_SLACK,
        )
        where: dict[str, object] = {
            "from": list(first),
            "to": list(last),
            "duration_ms": duration,
        }
        failure = _input_failure("swipe", where, output)
        if failure is not None:
            raise failure
        return first, last

    def scroll(
        self,
        direction: str,
        *,
        within: Element | None = None,
        duration_ms: int = DEFAULT_SWIPE_MS,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Swipe along the middle of an area of the screen; return the points.

        `direction` is the way the finger moves: ``"up"`` (which shows content
        further down), ``"down"``, ``"left"`` or ``"right"``. The area comes
        from the screen of the latest `dump` of this device: the visible part
        of `within` (an element of that screen, the same object, as for
        `tap`), or the whole display without it. The area is shrunk by 20% of
        its width on the left and on the right and by 20% of its height at
        the top and at the bottom, then clamped to stay 5% of the display
        width (height) away from the left and right (top and bottom) display
        edges, so the gesture avoids the system gestures at the edges. Every
        share is rounded down. The rectangle is half-open (its right and
        bottom edges are exclusive, as for element bounds), so the swipe runs
        from the first to the last pixel of it along the middle (center
        rounded down) across the movement, and both ends are inside it. With
        the rectangle ``left, top, right, bottom``: ``"up"`` goes from
        ``(center_x, bottom - 1)`` to ``(center_x, top)``, ``"down"`` the
        reverse, ``"left"`` from ``(right - 1, center_y)`` to ``(left,
        center_y)``, ``"right"`` the reverse. A rectangle under 2 pixels wide
        or high is refused. Then `swipe` sends it, once, with its rules.

        Raises:
            TypeError: `direction` is not a str, `within` is not an `Element`
                or None, or `duration_ms` has the wrong type.
            ValueError: `direction` is not one of the four, there is no dump
                yet ("dump the screen first"), the display size of the latest
                dump is unknown (`wm size` could not be read; use `swipe`
                with explicit points), `within` is not an element of the
                latest dump of this device, the area is too small to scroll,
                or the points or `duration_ms` are not usable (see `swipe`).
            ElementNotVisibleError: `within` has no visible part (a part
                under 8 x 8 pixels counts as none). It is not checked for a
                cover.
            InputFailedError: As for `swipe`, and the device errors listed
                there (`AdbError`, `AdbServerUnavailableError`,
                `DeviceNotFoundError`, `DeviceOfflineError`,
                `DeviceUnauthorizedError`).

        All but the device errors are raised before anything is sent to the
        adb server.
        """
        _check_direction(direction)
        if within is not None and not isinstance(within, Element):
            msg = f"within must be an Element or None, not {type(within).__name__}"
            raise TypeError(msg)
        latest = self._latest_screen
        if latest is None:
            msg = "dump the screen first"
            raise ValueError(msg)
        if not self._latest_display_measured:
            msg = (
                "the display size is unknown (wm size could not be read), so "
                "the scroll area cannot be placed; use swipe with explicit points"
            )
            raise ValueError(msg)
        area = _scroll_area(latest, within)
        start, end = _scroll_points(direction, area, latest.display)
        return self.swipe(start, end, duration_ms=duration_ms)

    def scroll_display(
        self, direction: str, *, duration_ms: int = DEFAULT_SWIPE_MS
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Swipe along the middle of the whole display; return the points.

        The same gesture as `scroll` without `within`, but it needs no `dump`:
        it reads the current display size from ``dumpsys window displays``
        (one request: the ``cur=`` size of display 0, already rotated) and
        sends the swipe, so it makes two adb requests where a dump and a
        scroll make many more. `direction` and the geometry are those of
        `scroll`, with the whole display as the area. It does not change the
        latest dump of this device.

        Raises:
            TypeError: `direction` is not a str or `duration_ms` has the
                wrong type.
            ValueError: `direction` is not one of the four, the display size
                cannot be read (a `DisplaySizeUnknownError`; use `dump` and
                `scroll`, or `swipe` with explicit points), the display is
                too small to scroll, or `duration_ms` is not usable (see
                `swipe`).
            InputFailedError: As for `swipe`, and the device errors listed
                there (`AdbError`, `AdbServerUnavailableError`,
                `DeviceNotFoundError`, `DeviceOfflineError`,
                `DeviceUnauthorizedError`).

        All but the device errors are raised before any input is sent.
        """
        _check_direction(direction)
        _check_duration(duration_ms)
        size = _parse_display_size(self._shell("dumpsys window displays"))
        if size is None:
            msg = (
                "the display size is unknown (dumpsys window displays gave no "
                "usable size); dump and scroll, or use swipe with explicit points"
            )
            raise DisplaySizeUnknownError(msg)
        start, end = _scroll_points(direction, Bounds(0, 0, size[0], size[1]), size)
        return self.swipe(start, end, duration_ms=duration_ms)

    def key(self, name: str, *, refused: RefusedKeys | None = None) -> str:
        """Press one key; return the key code name that was sent.

        `name` is an alias from `KEY_ALIASES` (``back``, ``home``, ...;
        case-insensitive) or a full key code name matching
        ``KEYCODE_[A-Z0-9_]{1,40}``, which is sent as given (it is not
        upper-cased, so ``keycode_back`` is refused). A bare number is refused
        because ``input keyevent`` can read it as a digit and type it. A
        ``KEYCODE_`` name made only of two or more digits after ``KEYCODE_``
        (for example ``KEYCODE_26``) is refused too, because Android may read
        it as a number; ``KEYCODE_0`` to ``KEYCODE_9`` are the digit keys and
        are accepted. A key that acts on the whole device and not on the app
        in front (power, calls, sound, brightness, screenshots, assistants,
        keys that open other apps; see `DEVICE_WIDE_KEYS` and
        `DEVICE_WIDE_KEY_PREFIXES`) is refused. `refused` None uses those
        built-in lists; a `RefusedKeys` replaces them (for example with the
        lists of the settings file), except that the keys of
        `FIXED_DEVICE_KEYS` are refused whatever it holds.

        The command is ``input keyevent KEYCODE_...`` followed by ``echo
        uiprobe-exit:$?`` (see `tap` for how the result is read). It is sent
        exactly once, never retried or resent, not even after a connection
        error (that error propagates unchanged). `InputFailedError` does not
        prove that no key was pressed, so callers must not blindly repeat it.
        The app in front is not checked.

        Raises:
            TypeError: `name` is not a str, or `refused` is neither None nor
                a `RefusedKeys`.
            ValueError: `name` is empty, a number, a ``KEYCODE_`` name of
                two or more digits (``KEYCODE_26``), a key that acts on the
                whole device (`DEVICE_WIDE_KEYS`, `DEVICE_WIDE_KEY_PREFIXES`,
                or `refused`),
                or neither an alias nor a ``KEYCODE_`` name.
            InputFailedError: The device did not report success. ``details``
                hold ``command`` (``"key"``), ``key``, ``reason`` and, as for
                `tap`, ``exit_status`` and ``output``.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.
        """
        if refused is not None and not isinstance(refused, RefusedKeys):
            msg = f"refused must be a RefusedKeys or None, not {type(refused).__name__}"
            raise TypeError(msg)
        code = _key_code(name, refused)
        output = self._shell_text(quote_args("input", "keyevent", code) + _EXIT_SUFFIX)
        failure = _input_failure("key", {"key": code}, output)
        if failure is not None:
            raise failure
        return code

    def type_text(self, text: str) -> None:
        """Type `text` into the field that has focus, as printable ASCII.

        Only the characters 0x20 to 0x7E are typed. Anything else is refused
        before anything is sent: a character outside ASCII (it failed on every
        Android version tried: emulators at API 24, 30 and 36), a newline or
        tab (they can act as Enter or move the focus; press Enter with `key`),
        any other control character, and empty text. The text goes to whatever
        field has focus in whatever app is in front; nothing is checked
        afterwards, and the device's answer is not used (its exit status can
        be 0 when typing failed).

        The text is sent as ``input text`` with the argument quoted for the
        device shell (always in single quotes, a quote inside as ``'"'"'``),
        in pieces of at most `TYPE_PIECE_SIZE` characters; the text is split
        before it is quoted.
        ``input text`` turns the two characters ``%s`` into a space, so a piece
        never contains ``%`` directly followed by ``s``: the text is split
        between them, and the pieces are sent as separate commands, in order.
        Each command is exactly ``input text <quoted piece>``, sent once.

        Raises:
            TypeError: `text` is not a str.
            ValueError: `text` is empty or has a character that is not typed.
                The error is a `TextNotTypedError`; its ``details`` hold
                ``character`` (``U+XXXX``), ``escaped`` and ``position``.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.
        The errors raised while sending leave the text out of their message and
        details. An error after the first command can leave part of the text
        typed.
        """
        for piece in _text_pieces(text):
            quoted = _single_quoted(piece)
            try:
                self._shell(f"input text {quoted}")
            except UiprobeError as exc:
                raise _without_text(exc, (quoted,)) from None

    def launch(
        self,
        package: str,
        *,
        restart: bool = False,
        timeout: float = DEFAULT_LAUNCH_TIMEOUT,
    ) -> None:
        """Start the app `package` and wait until it is in front.

        With ``restart=True`` the command ``am force-stop PACKAGE; echo
        uiprobe-exit:$?`` is sent first. It stops the app, so the next launch
        starts a fresh task; state the app has not saved is lost. It must
        report exit status 0 and print nothing else (empty lines are ignored),
        or nothing more is sent.
        Then exactly ``monkey -p PACKAGE -c android.intent.category.LAUNCHER
        1`` is sent (no other monkey option, and always a count of 1). Monkey
        must print the line ``Events injected: 1`` exactly once. Each command
        is sent once and never resent, not even after a connection error.

        Then `foreground` is read about every 0.5 seconds until it equals
        `package`. It is read at least once, and no read starts after
        `timeout` seconds have passed: the timeout bounds when the last read
        may start, not the whole wait. A read that started in time still
        counts, so the wait can run past the timeout by the time of that read,
        and by how late a pause ends when the system wakes it late.

        A str subclass is used as a plain str with the same text.

        Raises:
            TypeError: `package` is not a str, `restart` is not a bool, or
                `timeout` is not a number (a bool is not).
            ValueError: `package` does not follow the Android package rule
                (see `uiprobe.crawl_preview.PACKAGE_PATTERN`), or `timeout` is
                not from 0 to 600 seconds. Both are raised before the adb
                server is contacted.
            LaunchFailedError: ``details`` hold ``package`` and ``reason``:
                ``stop_failed`` (``am force-stop`` did not report success;
                ``exit_status`` when it reported a status other than 0,
                ``output`` when it printed something else, escaped; with no
                status and no output neither is set; monkey was not sent),
                ``not_installed_or_no_launcher`` (monkey found no launcher
                activity), ``unexpected_output`` (monkey did not report exactly
                one injected event; ``output`` is its first line that is not
                blank, stripped, cut to 200 characters and escaped) or
                ``not_in_front`` (the app was not the foreground app in time;
                ``foreground`` is the last package seen (escaped; past 1000
                characters cut and ended with ``...``), or None, and ``timeout``
                the limit). The app may still have started.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        if isinstance(package, str):
            package = str.__str__(package)  # a plain str, whatever the subclass
        package = check_package(package)
        if not isinstance(restart, bool):
            msg = f"restart must be a bool, not {type(restart).__name__}"
            raise TypeError(msg)
        limit = _check_launch_timeout(timeout)
        details: dict[str, object] = {"package": package}
        if restart:
            stop = self._shell(quote_args("am", "force-stop", package) + _EXIT_SUFFIX)
            failure = _input_failure("force-stop", {}, stop)
            if failure is not None:
                details["reason"] = "stop_failed"
                if "exit_status" in failure.details:
                    details["exit_status"] = failure.details["exit_status"]
                output = failure.details.get("output")
                if isinstance(output, str):
                    details["output"] = _escaped(output)
                msg = (
                    f"am force-stop {package} did not report success, so the app"
                    " was not launched"
                )
                raise LaunchFailedError(msg, details=details)
        output = self._shell(
            quote_args(
                "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"
            )
        )
        lines = [line.strip(" \t") for line in _lines(output)]
        counts = [
            m.group(1) for line in lines if (m := _EVENTS_INJECTED.fullmatch(line))
        ]
        if counts != ["1"]:
            if any(line.startswith(_NO_ACTIVITIES) for line in lines):
                details["reason"] = "not_installed_or_no_launcher"
                msg = (
                    f"{package} has no launcher activity (not installed, or it has"
                    " none)"
                )
                raise LaunchFailedError(msg, details=details)
            first = _escaped_line(output)
            details["reason"] = "unexpected_output"
            details["output"] = first
            msg = (
                f"monkey did not report one injected event; its output began '{first}'"
            )
            raise LaunchFailedError(msg, details=details)
        deadline = self._clock() + limit
        while True:
            seen = self.foreground()
            if seen == package:
                return
            if self._clock() >= deadline:
                break
            self._sleep(max(0.0, min(LAUNCH_POLL_INTERVAL, deadline - self._clock())))
            if self._clock() > deadline:
                break  # no read starts after the deadline
        shown = None if seen is None else _shown_foreground(seen)
        details.update(reason="not_in_front", foreground=shown, timeout=limit)
        last = "unknown" if shown is None else shown
        msg = f"{package} was not in front after {limit:g} seconds (foreground: {last})"
        raise LaunchFailedError(msg, details=details)

    def api_level(self) -> int | None:
        """Return the Android API level, or None when it cannot be read.

        A failure of the adb server or the device is not an error here and is not
        remembered, so a later call tries again.
        """
        if not self._api_level_known:
            try:
                text = self._shell("getprop ro.build.version.sdk").strip()
            except _OPTIONAL_ERRORS:
                return None
            self._api_level = ascii_int(text)
            self._api_level_known = True
        return self._api_level

    def is_emulator(self) -> bool:
        """Return True only when the device is an emulator.

        ``getprop ro.kernel.qemu`` must print exactly ``1`` (surrounding
        whitespace and a CR are ignored). An empty answer, ``0`` or anything
        else is not an emulator. A failure is never read as an answer.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return self._shell("getprop ro.kernel.qemu").strip() == "1"

    def foreground(self) -> str | None:
        """Return the package of the app that has focus, or None when unknown.

        The package comes from the same trusted sources as the activity in
        `dump`; it is whatever package that source names, not filtered. None
        means the output does not tell it reliably (missing, malformed,
        forged or ambiguous).

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        component = self._trusted_component()
        return None if component is None else component[0]

    def keyboard_shown(self) -> bool | None:
        """Return whether the soft keyboard is shown, or None when unknown.

        Reads ``mInputShown`` from ``dumpsys input_method``. None means no line
        claims the field, a claiming line is malformed (an app may print one),
        or the claiming lines disagree.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return _read_input_shown(self._shell("dumpsys input_method"))

    def dump(self, *, details: bool = True) -> Screen:
        """Capture the current screen with ``uiautomator dump``.

        The returned `Screen` becomes this device's latest screen: `tap`
        accepts only elements of that screen. A failed dump keeps the previous
        one.

        With `details` True (the default) the activity (``dumpsys window``) and
        the API level (``getprop``) are read too. With `details` False those two
        reads are skipped and `Screen.activity` and `Screen.api_level` are None;
        the display size (``wm size``) and everything the visibility and cover
        checks need are still read. `Screen.screen_id` includes the activity, so
        it can differ from the id of a full dump of the same screen.

        Raises:
            TypeError: `details` is not a bool (nothing is sent).
            DumpError: uiautomator failed or timed out, or its XML is unusable.
        """
        if not isinstance(details, bool):
            msg = f"details must be a bool, not {type(details).__name__}"
            raise TypeError(msg)
        xml = self._dump_xml()
        parsed = parse_uiautomator_dump(xml)
        elements = parsed.elements
        tops = [e for e in elements if e.parent is None]
        package = tops[0].package if tops else ""
        measured = self._display_size(parsed.rotation)
        display = measured or (
            max((e.bounds.right for e in tops), default=0),
            max((e.bounds.bottom for e in tops), default=0),
        )
        activity = None
        if details:
            component = self._focused_component(package)
            if component is not None:
                activity = _activity_name(package, component)
        screen = Screen(
            serial=self.serial,
            captured_at=datetime.now(UTC).astimezone().replace(microsecond=0),
            package=package,
            activity=activity,
            api_level=self.api_level() if details else None,
            display=display,
            elements=tuple(elements),
        )
        self._latest_screen = screen
        self._latest_display_measured = measured is not None
        return screen

    def screenshot(self) -> bytes:
        """Capture the screen as PNG bytes with ``screencap -p``.

        The bytes are checked in memory before they are returned, without
        decoding them (see `uiprobe.pngfile.check_png`): the signature, a
        well-formed header chunk, chunks that fit, image data and the closing
        chunk at the end. Nothing is
        written to the device or to this computer, and `dump` state is not
        touched.

        A screen that forbids screenshots (``FLAG_SECURE``, for example a
        private browser tab) gives empty output on older Android, and on newer
        Android a mostly black image; the latter is a valid PNG and is
        returned as it is.

        Raises:
            ScreenshotFailedError: The output is empty or is not a complete
                PNG image.
        """
        data = self._client.exec_out(
            self.serial,
            quote_args("screencap", "-p"),
            timeout=SCREENSHOT_TIMEOUT,
        )
        try:
            check_png(data)
        except ValueError as error:
            reason = "empty" if not data else "invalid"
            raise ScreenshotFailedError(
                f"{error}; the screen may forbid screenshots (for example a "
                "private browser tab), and its structure can still be dumped "
                "with uiprobe dump",
                details={"reason": reason, "bytes": len(data)},
            ) from error
        return data

    def display_for_image(self, width: int, height: int) -> tuple[int, int] | None:
        """Return the display size that `dump` reports, turned like an image.

        The size comes from ``wm size``, as in `dump`. A screenshot has no
        rotation of its own, so the size is turned to the orientation of an
        image that is `width` x `height` pixels (a square image keeps the
        natural orientation). None means the size could not be read.
        """
        size = self._display_size(0)
        if size is None:
            return None
        if width != height and (width > height) != (size[0] > size[1]):
            return (size[1], size[0])
        return size

    def _dump_xml(self) -> bytes:
        path = f"{DUMP_DIRECTORY}/uiprobe-{secrets.token_hex(8)}.xml"
        try:
            output = self._run_uiautomator(path)
            if "dumped to" not in output:
                raise DumpError(
                    "uiautomator dump failed",
                    details={"output": output.strip()[:DUMP_MESSAGE_LIMIT]},
                )
            return self._client.exec_out(self.serial, quote_args("cat", path))
        finally:
            with contextlib.suppress(UiprobeError):
                self._shell(quote_args("rm", "-f", path))

    def _run_uiautomator(self, path: str) -> str:
        """Run ``uiautomator dump``.

        Only a timeout while reading its output, after the service answered
        OKAY, is a `DumpError`; a stall in the handshake stays an `AdbError`.
        """
        try:
            return self._shell_text(
                quote_args("uiautomator", "dump", path), timeout=DUMP_TIMEOUT
            )
        except AdbError as error:
            if (
                error.details.get("reason") != "timeout"
                or error.details.get("stage") != "output"
            ):
                raise
            raise DumpError(
                "uiautomator dump timed out",
                details={
                    "reason": "timeout",
                    "timeout_seconds": error.details.get("timeout_seconds"),
                },
            ) from error

    def _display_size(self, rotation: int) -> tuple[int, int] | None:
        """Return the current logical display size, or None when unreadable."""
        try:
            size = _parse_wm_size(self._shell("wm size"))
        except _OPTIONAL_ERRORS:
            return None
        if size is None:
            return None
        # `wm size` reports the natural orientation.
        return (size[1], size[0]) if rotation in {1, 3} else size

    def _trusted_component(self) -> tuple[str, str] | None:
        """Return the focused component from the trusted source, or None.

        ``dumpsys window`` is read first; only when it says nothing is
        ``dumpsys activity activities`` read. A source that speaks but cannot
        be trusted gives None with no fallback. Device errors propagate.
        """
        found = _read_source(self._shell("dumpsys window"), _FOCUSED_APP_SOURCE)
        if found is _ABSENT:
            found = _read_source(
                self._shell("dumpsys activity activities"), _RESUMED_SOURCE
            )
        return found if isinstance(found, tuple) else None

    def _focused_component(self, package: str) -> tuple[str, str] | None:
        """Return the focused component, only when it belongs to `package`.

        Best effort: any failure, or a result that is missing, ambiguous or of
        another package, gives None.
        """
        if not package:
            return None
        try:
            component = self._trusted_component()
        except _OPTIONAL_ERRORS:
            return None
        if component is None or component[0] != package:
            return None
        return component
