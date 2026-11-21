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
from uiprobe.crawl_preview import MAX_PACKAGE_LENGTH, PACKAGE_PATTERN, check_package
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
from uiprobe.inputwindows import InputWindow, read_window_at
from uiprobe.model import (
    Bounds,
    Element,
    Screen,
    parse_uiautomator_dump,
    shorten_activity,
)
from uiprobe.packageuid import PackageUid, parse_package_uid
from uiprobe.pngfile import check_png
from uiprobe.windowdiag import TapWindowDiagnosis, diagnose_tap_window

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
OFFLINE_STATE: Final = "offline"
# The boot id is a random UUID the kernel makes at every boot, so it tells two
# emulators of one image (same serial number and build) apart, and one
# emulator before and after a restart. ``echo "$(cat ...)"`` prints exactly one
# line, empty when the file cannot be read.
IDENTITY_COMMAND: Final = (
    "getprop ro.kernel.qemu; getprop ro.serialno; getprop ro.build.fingerprint;"
    ' echo "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"'
)
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
# A pinned transport that the adb server renumbered: how long to wait for the
# device to come back, how often to look, and how many times one Device may
# move its pin.
# Commands that change the device (inputs, starting and stopping apps): their
# request never follows a renumbered transport.
_CHANGING_COMMANDS: Final = ("input ", "monkey ", "am ")
REPIN_WAIT: Final = 10.0
REPIN_POLL: Final = 0.5
MAX_REPINS: Final = 5
# Waits for the pinned device to come back (on its number or a new one) that
# one Device allows; the next one fails, so a device that keeps dropping out
# cannot keep a caller re-reading forever.
MAX_RECONNECTS: Final = 10
# details["reason"] when the wait for a renumbered transport was given up
# because the watch set with `Device.set_pin_watch` said so.
PIN_WAIT_STOPPED: Final = "pin_wait_stopped"
# details["reason"] when a request that changes the device was not sent
# because the device has to be proven again first (the pin itself is kept).
PIN_NOT_PROVEN: Final = "pin_not_proven"
# details["reason"] when a request that changes the device was not sent
# because the time it had to go out by had passed: checked with uiprobe's own
# clock on the selected connection, right before the request is written (the
# crawl's time limit); details["why"] is ``time_limit``.
SEND_STOPPED: Final = "send_stopped"


def _monotonic() -> float:
    """uiprobe's own clock for the last check before a request goes out.

    The crawl turns its time limit into a value of this clock before its last
    reads, so no code of the caller runs between that check and the send.
    """
    return time.monotonic()


class _Dispatch(NamedTuple):
    """What the crawl hands one call that changes the device: the value of
    `_monotonic` by which its requests must go out, and the `pin_epoch` it
    approved (None: none given)."""

    send_by: float | None
    epoch: int | None


def _dispatch(send_by: object, epoch: object) -> _Dispatch | None:
    """Check the private `_send_by` and `_epoch` arguments; they must be a
    built-in float and int (the crawl's own values), or None."""
    if send_by is None and epoch is None:
        return None
    if send_by is not None and type(send_by) is not float:
        msg = f"_send_by must be a float, not {type(send_by).__name__}"
        raise TypeError(msg)
    if epoch is not None and type(epoch) is not int:
        msg = f"_epoch must be an int, not {type(epoch).__name__}"
        raise TypeError(msg)
    return _Dispatch(send_by, epoch)


# Android gives each user this many uids: uid = user * 100000 + app id.
_UIDS_PER_USER: Final = 100000


class FocusedWindow(NamedTuple):
    """The window that has input focus: its owner package, title and hash.

    ``window`` is the hash ``dumpsys window`` prints for the window, which
    tells two windows with the same owner and title apart. ``user`` is the
    Android user it belongs to (the ``u<N>`` of ``Window{<hash> u<N> ...}``),
    or -1 when the window's owner uid (``mOwnerUid=``, user * 100000 + app
    id) names another user. ``owner_uid`` is that full uid (on the emulators
    checked, API 24, 30, 33 to 36, the focused app window's equals the app's
    uid), None when not read.
    """

    owner: str
    title: str
    window: str
    user: int = 0
    owner_uid: int | None = None


class DeviceIdentity(NamedTuple):
    """What `Device.identity` read: emulator or not, serial number, build and
    boot id.

    ``boot_id`` is the kernel's random boot id (a lowercase UUID), or ``""``
    when it could not be read.
    """

    emulator: bool
    serialno: str
    fingerprint: str
    boot_id: str = ""


DEFAULT_LAUNCH_TIMEOUT: Final = 10.0
LAUNCH_POLL_INTERVAL: Final = 0.5
MAX_LAUNCH_TIMEOUT: Final = 600.0
# An app is started with ``am start -W`` of its launcher activity, read first
# with ``cmd package resolve-activity --brief``. ``monkey -p PACKAGE -c
# android.intent.category.LAUNCHER 1`` is not used: it switched the system
# setting accelerometer_rotation (auto-rotate) from 0 to 1 on a Samsung API 36
# phone and on API 24 and 36 emulators, while ``am start -W`` left it as it was.
LAUNCHER_CATEGORY: Final = "android.intent.category.LAUNCHER"
MAIN_ACTION: Final = "android.intent.action.MAIN"
# The lines ``resolve-activity --brief`` prints before the component (captured
# on API 24 and 36 emulators and a Samsung API 36 phone:
# ``priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true``).
_RESOLVE_INFO = re.compile(
    r"priority=-?[0-9]{1,10} preferredOrder=-?[0-9]{1,10}"
    r" match=0x[0-9a-f]{1,8} specificIndex=-?[0-9]{1,10} isDefault=(?:true|false)"
)
# The class part of a component: dot-separated names of ASCII letters, digits,
# ``_`` and ``$``, optionally starting with ``.`` (short for the package).
_CLASS_NAME = re.compile(r"\.?[A-Za-z0-9_$]+(?:\.[A-Za-z0-9_$]+)*")
_MAX_CLASS_NAME: Final = 255
# The lines ``am start -W`` prints, as captured on API 24 and 36 emulators and
# a Samsung API 36 phone. The first line echoes the component (short form);
# a Warning line is one of the two captured ones (the app was running); the
# lines with times and the launch state say nothing about success.
_START_ECHO: Final = (
    "Starting: Intent {{ act=android.intent.action.MAIN"
    " cat=[android.intent.category.LAUNCHER] cmp={} }}"
)
_START_WARNINGS: Final = frozenset(
    {
        "Warning: Activity not started, intent has been delivered to currently"
        " running top-most instance.",
        "Warning: Activity not started, its current task has been brought to the front",
    }
)
# LaunchState only as seen (API 36): COLD, WARM, HOT, UNKNOWN (0); another
# value is unknown output until it is seen. ThisTime only on API 24.
_START_INFO = re.compile(
    r"(LaunchState): (?:COLD|WARM|HOT|UNKNOWN \(0\))"
    r"|(ThisTime|TotalTime|WaitTime): -?[0-9]{1,10}"
)
_START_OK: Final = "Status: ok"
SYSTEM_PACKAGE: Final = "android"
_START_DONE: Final = "Complete"
_START_ACTIVITY: Final = "Activity:"
# C0 and C1 control characters; a line keeps none after its CR LF end is cut.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# The silence ``am start -W`` may keep while it waits is the launch timeout,
# but at least this long (a read timeout of 0 would not wait at all).
START_MIN_WAIT: Final = 1.0
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


def _check_text(text: str) -> str:
    """Return `text` when it can be typed; raise before anything is sent."""
    if type(text) is not str:  # `_plain_text` has made it one
        msg = f"text must be a plain str, not {type(text).__name__}"
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

    `text` is first made a plain str (see `_plain_text`); the check and the
    split read only that str.

    A piece has at most `TYPE_PIECE_SIZE` characters and never contains ``%``
    directly followed by ``s`` (the device turns that pair into a space). The
    pieces joined give `text` again.
    """
    plain = _check_text(_plain_text(text))
    pieces: list[str] = []
    current = ""
    for char in plain:
        if len(current) >= TYPE_PIECE_SIZE or (char == "s" and current[-1:] == "%"):
            pieces.append(current)
            current = ""
        current += char
    pieces.append(current)
    return pieces


def _plain_text(text: object) -> str:
    """Return `text` as a plain str with the same characters.

    A str subclass is read once here, by the base type; everything after this
    uses only the returned str, so a subclass cannot show one text to the
    check and another to the device.
    """
    if not isinstance(text, str):
        msg = f"text must be a str, not {type(text).__name__}"
        raise TypeError(msg)
    return str.__str__(text)


# Why typing stopped, by error class, in fixed words: what the device or adb
# said is never used, as it can repeat the text.
_TYPING_STOPPED_BECAUSE: Final = (
    (AdbServerUnavailableError, "the adb server cannot be reached"),
    (DeviceNotFoundError, "the device is gone"),
    (DeviceOfflineError, "the device is offline"),
    (DeviceUnauthorizedError, "the device has not authorized this computer"),
)


def _typing_stopped(
    kind: type[UiprobeError], piece: int, pieces: int, *, unsent: bool = False
) -> UiprobeError:
    """A new error of class `kind` for typing that stopped at `piece` (1-based).

    It holds only numbers and fixed words, never the text or anything the
    device or adb answered. `unsent` says the piece was refused before it was
    sent, because the pinned device has to be proven again
    (``details["reason"]`` is then ``pin_not_proven``).
    """
    reason = (
        "the device has to be proven again"
        if unsent
        else next(
            (words for cls, words in _TYPING_STOPPED_BECAUSE if issubclass(kind, cls)),
            "the adb request failed",
        )
    )
    done = piece - 1
    fate = (
        f"piece {piece} was not sent"
        if unsent
        else f"piece {piece} may have been typed in part or in full"
    )
    msg = (
        f"typing stopped at piece {piece} of {pieces} ({reason}); {done} earlier"
        f" piece{' was' if done == 1 else 's were'} reported typed, and {fate}"
    )
    details: dict[str, object] = {
        "command": "type",
        "request": "shell:input text <text omitted>",
        "piece": piece,
        "pieces": pieces,
        "pieces_done": done,
    }
    if unsent:
        details["reason"] = PIN_NOT_PROVEN
    return kind(msg, details=details)


def _piece_failure(output: str, piece: int, pieces: int) -> InputFailedError | None:
    """Judge the answer to one piece; None means exit status 0 and nothing else.

    The answer itself never goes into the error, not even the exit status:
    the device prints the status line, and a reply can repeat the text in it.
    """
    failure = _input_failure("type", {}, output)
    if failure is None:
        return None
    reason = failure.details["reason"]
    done = piece - 1
    details: dict[str, object] = {
        "command": "type",
        "reason": reason,
        "piece": piece,
        "pieces": pieces,
        "pieces_done": done,
    }
    what = {
        "no_exit_status": "the device did not report an exit status",
        "exit_status": "input reported an exit status other than 0",
        "unexpected_output": "input printed something besides its exit status",
    }[str(reason)]
    msg = (
        f"typing stopped at piece {piece} of {pieces}: {what}; {done} earlier"
        f" piece{' was' if done == 1 else 's were'} reported typed, and piece"
        f" {piece} may have been typed in part or in full"
    )
    return InputFailedError(msg, details=details)


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


def _is_component(text: str, package: str) -> bool:
    """Whether `text` is ``PACKAGE/CLASS`` with exactly `package` before the
    slash and a class name of the accepted shape and length after it."""
    owner, slash, name = text.partition("/")
    return (
        slash == "/"
        and owner == package
        and len(name) <= _MAX_CLASS_NAME
        and _CLASS_NAME.fullmatch(name) is not None
    )


def _launcher_component(output: str, package: str) -> str | None:
    """Read the component from ``cmd package resolve-activity --brief ...;
    echo uiprobe-exit:$?`` output.

    Accepted: zero or more lines of the `_RESOLVE_INFO` form, then exactly one
    line ``PACKAGE/CLASS`` of `package`, then the line ``uiprobe-exit:0``,
    last (empty lines and one CR at each line end are ignored). Anything
    else is None: ``No activity found``, another exit status, and output that
    ends early (no marker line, so a component cut off at the end of the
    output is never taken).
    """
    lines = [line for line in _lines(output) if line]
    if len(lines) < 2 or lines[-1] != f"{EXIT_MARKER}0":
        return None
    *info, last = lines[:-1]
    if not all(_RESOLVE_INFO.fullmatch(line) for line in info):
        return None
    return last if _is_component(last, package) else None


def _spellings(component: str) -> frozenset[str]:
    """The short (``.Name``) and full spellings of ``PACKAGE/CLASS``."""
    package, _, name = component.partition("/")
    if name.startswith("."):
        return frozenset({component, f"{package}/{package}{name}"})
    if name.startswith(package + "."):
        return frozenset({component, f"{package}/{name[len(package) :]}"})
    return frozenset({component})


class _StartFailure(NamedTuple):
    """Why ``am start -W`` output is not a success.

    ``line`` is the line to show (None: the first line that is not blank),
    ``exit_status`` the status when it was known and not 0, and ``other``
    the component of another package that ``Activity:`` named.
    """

    reason: str
    problem: str
    line: str | None = None
    exit_status: int | None = None
    other: str | None = None


def _start_failure(output: str, package: str, activity: str) -> _StartFailure | None:
    """Judge ``am start -W ... -n ACTIVITY; echo uiprobe-exit:$?`` output.

    None is success: no line holds a control character (after one CR at its
    end is cut), no line starts with ``Error``, the marker line is last with
    exit status 0, and the other lines are, each kind at most once: exactly
    one ``Starting: Intent { act=android.intent.action.MAIN
    cat=[android.intent.category.LAUNCHER] cmp=ACTIVITY }`` (in the short or
    the full spelling of `activity`), one of the two captured ``Warning:``
    lines, exactly one ``Status: ok``, exactly one ``Activity: PACKAGE/CLASS``
    of `package` (any class: a launcher activity may hand over to another
    one), ``LaunchState:``, ``ThisTime:``, ``TotalTime:``, ``WaitTime:`` and
    ``Complete``. Empty lines are ignored. API 24 prints its ``Error`` lines
    with exit status 0, so the status alone proves nothing. Output that is a
    success in every other way but names an activity of another package gives
    the reason ``other_activity``.
    """
    lines = [line for line in _lines(output) if line]
    control = next((line for line in lines if _CONTROL.search(line)), None)
    if control is not None:
        return _StartFailure(
            "unexpected_output", "printed a control character", control
        )
    marker = lines[-1] if lines and lines[-1].startswith(EXIT_MARKER) else None
    others = lines[:-1] if marker is not None else lines
    value = None if marker is None else marker[len(EXIT_MARKER) :]
    status = int(value) if value is not None and _EXIT_VALUE.fullmatch(value) else None
    failed = None if status in (None, 0) else status
    errors = [line for line in others if line.startswith("Error")]
    # API 24 and 36 print ``Error type 3`` and then the line that says why.
    error = next((line for line in errors if line.startswith("Error:")), None)
    error = error or next(iter(errors), None)
    if error is not None:
        return _StartFailure("start_failed", "reported an error", error, failed)
    if failed is not None:
        return _StartFailure(
            "start_failed", f"ended with exit status {failed}", None, failed
        )
    if status is None:
        return _StartFailure("unexpected_output", "reported no exit status")
    return _start_report_failure(others, package, activity)


def _start_line_kind(line: str, echoes: frozenset[str]) -> str | None:
    """The kind of one line of a start's report, or None when unknown."""
    if line in echoes:
        return "Starting"
    if line in _START_WARNINGS:
        return "Warning"
    if line in (_START_OK, _START_DONE):
        return line
    if line.startswith(_START_ACTIVITY):
        return _START_ACTIVITY
    info = _START_INFO.fullmatch(line)
    return None if info is None else info.group(1) or info.group(2)


def _start_report_failure(
    lines: list[str], package: str, activity: str
) -> _StartFailure | None:
    """Judge the lines of a start that reported exit status 0 and no error."""
    echoes = frozenset(_START_ECHO.format(c) for c in _spellings(activity))
    kinds: list[str] = []
    for line in lines:
        kind = _start_line_kind(line, echoes)
        if kind is None:
            what = (
                "printed a LaunchState value it does not know"
                if line.startswith("LaunchState:")
                else "printed an unknown line"
            )
            return _StartFailure("unexpected_output", what, line)
        if kind in kinds:
            return _StartFailure("unexpected_output", "printed a line twice", line)
        kinds.append(kind)
    missing = [k for k in ("Starting", _START_OK, _START_ACTIVITY) if k not in kinds]
    if missing:
        what = "the start line" if missing[0] == "Starting" else f"'{missing[0]}'"
        return _StartFailure("unexpected_output", f"did not print {what}")
    named = next(line for line in lines if line.startswith(_START_ACTIVITY))
    component = named.removeprefix(_START_ACTIVITY + " ")
    if component != named and _is_component(component, package):
        return None
    owner = component.partition("/")[0]
    # The system owner ``android`` (its chooser,
    # android/com.android.internal.app.ResolverActivity) has no dot, unlike an
    # app's package; it is accepted here, for the activity reported only.
    if (
        component != named
        and owner != package
        and len(owner) <= MAX_PACKAGE_LENGTH
        and (owner == SYSTEM_PACKAGE or PACKAGE_PATTERN.fullmatch(owner))
        and _is_component(component, owner)
    ):
        return _StartFailure(
            "other_activity", "named an activity of another app", named, None, component
        )
    return _StartFailure("unexpected_output", f"named no activity of {package}", named)


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


_CURRENT_FOCUS_CLAIM: Final = re.compile(r"\s*mCurrentFocus=")
# The hash and user sit at fixed places right after ``Window{``; the title is
# the rest of the record up to its final brace, so it may hold braces of its
# own (``Order {7}``) but cannot move the hash or the user.
_CURRENT_FOCUS: Final = re.compile(
    r"\s*mCurrentFocus=Window\{([0-9a-f]{1,16}) u([0-9]{1,3}) ([^\r\n]{1,512})\}\s*"
)
_WINDOW_HEAD: Final = re.compile(
    r"  Window #[0-9]{1,4} Window\{([0-9a-f]{1,16}) u([0-9]{1,3}) ([^\r\n]{1,512})\}:"
)
# Where a window block's header names its hash: any line that names a hash
# there counts as a header of that hash (a title that holds ``Window{<hash>``
# further on does not).
_WINDOW_HEAD_HASH: Final = re.compile(r"  Window #[0-9]+ Window\{([0-9a-f]+) ")
# The system writes this line; 24 has mShowToOwnerOnly, 30 and 36 showForAllUsers.
_WINDOW_OWNER: Final = re.compile(
    r"    mOwnerUid=(?P<uid>[0-9]{1,10})"
    r" (?:mShowToOwnerOnly|showForAllUsers)=(?:true|false)"
    r" package=(?P<package>[A-Za-z][A-Za-z0-9_]{0,254}"
    r"(?:\.[A-Za-z][A-Za-z0-9_]{0,254}){0,64})"
    r" appop=[A-Z_]{1,64}"
)


def _read_current_focus(text: str) -> tuple[str, str, str] | None:
    """The ``(hash, title, user)`` of the focused window, or None.

    Every line that claims ``mCurrentFocus`` must be well formed and all must
    name the same window; anything else is None.
    """
    lines = {
        line.rstrip("\r")
        for line in text.split("\n")
        if _CURRENT_FOCUS_CLAIM.match(line)
    }
    windows: set[tuple[str, str, str]] = set()
    for line in lines:
        match = (
            _CURRENT_FOCUS.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
        )
        if match is None:
            return None
        windows.add((match[1], match[3], match[2]))
    return windows.pop() if len(windows) == 1 else None


def _read_window_owner(
    text: str, window: str, title: str, user: str
) -> FocusedWindow | None:
    """The owner of window `window` from its block, or None (see focused_window)."""
    lines = [line.rstrip("\r") for line in text.split("\n")]
    heads = [
        index
        for index, line in enumerate(lines)
        if (named := _WINDOW_HEAD_HASH.match(line)) is not None and named[1] == window
    ]
    if len(heads) != 1 or heads[0] + 2 >= len(lines):
        return None
    index = heads[0]
    line = lines[index]
    head = _WINDOW_HEAD.fullmatch(line) if len(line) <= _MAX_DUMPSYS_LINE else None
    if head is None or head[1] != window or head[2] != user or head[3] != title:
        return None
    owner = _WINDOW_OWNER.fullmatch(lines[index + 2])
    if owner is None:
        return None
    # The owner uid tells the user too (user * 100000 + app id); when it
    # disagrees with u<N>, the user is not known: -1, which no check takes.
    known = int(user) if int(owner["uid"]) // _UIDS_PER_USER == int(user) else -1
    return FocusedWindow(owner["package"], title, window, known, int(owner["uid"]))


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


def _parse_identity(text: str) -> DeviceIdentity | None:
    """Read the answer to `IDENTITY_COMMAND`; None unless it has four lines."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != len(DeviceIdentity._fields):
        return None
    qemu, serialno, fingerprint, boot_id = (line.rstrip("\r").strip() for line in lines)
    if not _BOOT_ID.match(boot_id):
        boot_id = ""
    return DeviceIdentity(qemu == "1", serialno, fingerprint, boot_id)


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
        pin: bool = False,
    ) -> None:
        if isinstance(serial, str):
            serial = str.__str__(serial)  # a plain str from here on
        _check_serial(serial)
        if not isinstance(pin, bool):
            msg = f"pin must be a bool, not {type(pin).__name__}"
            raise TypeError(msg)
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
        # Set by `pin_transport`: every later request uses this transport, and
        # the identity read through it is what the device must keep.
        self._transport_id: int | None = None
        self._pinned_identity: DeviceIdentity | None = None
        self._repins: list[tuple[int, int]] = []
        self._pin_watch: Callable[[], bool] | None = None
        self._repinning = False  # inside a re-pin: no second one
        self._reconnects = 0  # times the device had to be proven again
        # Moves on every event that makes earlier reads doubtful: a failed
        # request on the pin, a wait, a move, a failed pin. `_proven` is its
        # value when the identity was last proven on the current number.
        self._epoch = 0
        self._proven = 0
        self._broken: tuple[str, dict[str, object]] | None = None  # failed pin
        # Requests that did not succeed, pinned or not; and how many launches
        # are past their first request (no adb server is started for them).
        self._failed_requests = 0
        self._launch_holds = 0
        if pin:
            # Right after the listing, so the identity is the chosen device's.
            self.pin_transport()

    @property
    def serial(self) -> str:
        """The adb serial of this device."""
        return self._info.serial

    @property
    def transport_id(self) -> int | None:
        """The transport every request uses, once `pin_transport` set it."""
        return self._transport_id

    def _pin(self, *, strict: bool = False) -> dict[str, int]:
        """The client keyword that selects the pinned transport, if any.

        While a transport is pinned, every request first checks with the adb
        server (``host:devices-l``) that the pinned transport id still lists
        this serial, so no request goes to a device that a restarted server
        gave the same number. The instant between that check and the request
        itself remains. See `_check_listed` for what else holds.
        """
        if self._broken is not None:
            raise self._broken_error()
        if self._repinning:
            # A request while the device is being proven (first or again;
            # from the watch, say): nothing goes out, and the proof fails.
            raise self._fail(self._changed("was asked for during a proof"))
        if self._transport_id is None:
            return {}
        succeeded = False
        try:
            self._check_listed(strict=strict)
            succeeded = True
        except BaseException as error:
            self._interrupted(error)
            raise
        finally:
            if not succeeded:
                self._epoch += 1  # whatever stopped the check, the proof is stale
        return {"transport_id": self._transport_id}

    def _call[T](
        self,
        send: Callable[..., T],
        *,
        strict: bool,
        changing: bool = False,
        dispatch: _Dispatch | None = None,
    ) -> T:
        """Send one request with the pin; note a lost transport at once.

        A request on the pinned transport that does not succeed, for any
        reason (the device went offline or away, the server is gone, the
        protocol broke, an interrupt), moves `pin_epoch` before the error
        leaves this method, so a caller
        that swallows the error cannot hide it: the next read proves the
        device again (without a pinned boot id it fails the pin instead) and
        the next input is refused.

        A request that `changing` marks (an input, a start or stop of an app)
        is checked once more on its own connection, after the transport was
        selected and right before the request is written: nothing happened to
        the pin since this method began (`pin_epoch`), and uiprobe's own clock
        has not reached the time it had to go out by. Only uiprobe's code runs
        there, never a caller's.
        """
        pin = self._pin(strict=strict)  # moves the epoch itself on a failure
        guard = self._dispatch_guard(self._epoch, dispatch) if changing else None
        succeeded = False
        try:
            result = send(pin) if guard is None else send(pin, guard)
            succeeded = True
        except BaseException as error:
            if pin:
                self._interrupted(error)
            raise
        finally:
            if not succeeded:
                self._failed_requests += 1  # a launch in progress stops on it
                if pin:
                    self._epoch += 1  # any way out but success, whatever the error
        return result

    def _time_up(self) -> DeviceNotFoundError:
        msg = f"nothing was sent to {self.serial}: the time limit had passed"
        details = {"serial": self.serial, "reason": SEND_STOPPED, "why": "time_limit"}
        return DeviceNotFoundError(msg, details=details)

    def _dispatch_guard(
        self, epoch: int, dispatch: _Dispatch | None
    ) -> Callable[[], None]:
        """The last check of a request that changes the device (see `_call`).

        The epoch compared is the one the caller approved (`dispatch`), else
        the one when the request began.
        """
        send_by = None if dispatch is None else dispatch.send_by
        if dispatch is not None and dispatch.epoch is not None:
            epoch = dispatch.epoch

        def guard() -> None:
            if self._epoch != epoch:
                msg = (
                    f"{self.serial} has to be proven again before an input"
                    f" (transport {self._transport_id}); nothing was sent"
                )
                details = {"serial": self.serial, "reason": PIN_NOT_PROVEN}
                raise DeviceNotFoundError(msg, details=details)
            if send_by is not None and _monotonic() >= send_by:
                raise self._time_up()

        return guard

    def _check_listed(self, *, strict: bool = False) -> None:
        """Hold every request to one rule; raise when it does not hold.

        The rule for a request that changes the device (`strict`: an input,
        a start or a stop of an app): it goes out only when the pinned number
        is listed, ready, for this serial, and nothing happened since the
        identity was last proven on that number (`pin_epoch` equals the proven
        epoch: no request failed, no wait began, the pin did not move).
        Otherwise nothing is sent: when a read could prove the device again
        (the cases below), it is refused with ``pin_not_proven`` and the pin
        is kept, so the caller reads again; else it is refused with
        ``device_changed`` and the pin fails.

        A read goes out on the same condition. When the number is gone from
        the listing, still lists this serial but offline, or something
        happened since the last proof, the device is proven again first, by
        waiting for it (`_repin`); that needs a pinned boot id. Without one
        the device cannot be proven again at all (another emulator of the
        same image behind the same number would read the same), so any of
        these fails the pin; when something happened since the last proof,
        it fails before the listing is read, so that an adb server that
        cannot be reached does not leave the pin stale. Anything else fails
        the pin too, and so does a request made while a wait is under way
        (from the watch, say).
        """
        if not self.can_repin and self._epoch != self._proven:
            # No boot id: nothing would tell the pinned device from another
            # emulator of the same image behind the same number, so a device
            # that has to be proven again cannot be.
            raise self._fail(self._changed("cannot be proven again without a boot id"))
        everything = self._client.devices()
        if any(
            d.serial == self.serial and d.state not in (READY_STATE, OFFLINE_STATE)
            for d in everything
        ):
            raise self._fail(self._changed("is not usable any more"))
        if (
            sum(d.serial == self.serial and d.state == READY_STATE for d in everything)
            > 1
        ):
            raise self._fail(self._changed("is listed more than once"))
        listed = [d for d in everything if d.transport_id == self._transport_id]
        same = len(listed) == 1 and listed[0].serial == self.serial
        ready = same and listed[0].state == READY_STATE
        if ready and self._epoch == self._proven:
            return
        if not self.can_repin:
            # Proven and nothing happened since, but the number is not listed
            # and ready (renumbered, offline): no boot id, so no wait.
            raise self._fail(self._changed())
        waitable = ready or not listed or (same and listed[0].state == OFFLINE_STATE)
        if strict:
            if not waitable:
                raise self._fail(self._changed())
            if self._reconnects >= MAX_RECONNECTS:
                # No proof is left to try: nothing will make this pin usable.
                raise self._fail(
                    self._changed(f"was away more than {MAX_RECONNECTS} times")
                )
            # Nothing is sent and the pin stays: the next read proves the
            # device again (the caller decides on that fresh read).
            msg = (
                f"{self.serial} has to be proven again before an input"
                f" (transport {self._transport_id}); nothing was sent"
            )
            details = {"serial": self.serial, "reason": PIN_NOT_PROVEN}
            raise DeviceNotFoundError(msg, details=details)
        if not waitable:
            raise self._fail(self._changed())
        self._repin()

    def _changed(
        self, why: str = "does not lead to that device any more"
    ) -> DeviceNotFoundError:
        msg = f"the transport pinned for {self.serial} ({self._transport_id}) {why}"
        details = {"serial": self.serial, "reason": "device_changed"}
        return DeviceNotFoundError(msg, details=details)

    def _fail(self, error: DeviceNotFoundError) -> DeviceNotFoundError:
        """Keep a failed pin, so that no later request goes anywhere.

        A caller may swallow the error (a best-effort read does); every later
        request raises it again instead of reaching a device that came back
        after the pin was given up. Only the message and the details are
        kept, not the exception and its frames.
        """
        if self._broken is None:
            self._broken = (str(error), dict(error.details))
            self._epoch += 1
        return error

    def _doubt[E: Exception](self, failure: E) -> E:
        """Note a request that changed the device but did not report success
        (it may have been cut off): the proof is stale. Return `failure`."""
        if self._transport_id is not None:
            self._epoch += 1
        return failure

    def _interrupted(self, error: BaseException) -> None:
        """Keep the pin as failed when an interrupt (not an error) left a
        pinned exchange: such a Device is not to be used again."""
        if not isinstance(error, Exception) and self._transport_id is not None:
            self._fail(self._changed("was interrupted while it was in use"))

    def _broken_error(self) -> DeviceNotFoundError:
        assert self._broken is not None  # noqa: S101 - only called when kept
        message, details = self._broken
        return DeviceNotFoundError(message, details=dict(details))

    def _same_device(self, found: DeviceIdentity | None) -> bool:
        """Whether `found` is the pinned device.

        A pinned boot id must be read again, the same; without one (only
        `check_pin` gets here then), the three other fields are compared and
        a boot id read later is ignored.
        """
        expected = self._pinned_identity
        if found is None or expected is None:
            return False
        if expected.boot_id:
            return found == expected
        return found._replace(boot_id="") == expected

    def _stop_if_watched(self) -> None:
        watch = self._pin_watch
        stopped = watch is not None and watch()
        if self._broken is not None:
            # A request inside the watch failed the pin (and may have been
            # swallowed there): the wait ends with that failure.
            raise self._broken_error()
        if stopped:
            msg = f"the wait for {self.serial} to come back was stopped"
            details = {"serial": self.serial, "reason": PIN_WAIT_STOPPED}
            raise self._fail(DeviceNotFoundError(msg, details=details))

    def _count_reconnect(self) -> None:
        if self._reconnects >= MAX_RECONNECTS:
            raise self._fail(
                self._changed(f"was away more than {MAX_RECONNECTS} times")
            )
        self._reconnects += 1
        self._epoch += 1

    def _repin(self) -> None:
        """Prove the device again, following it to a new number if needed.

        The adb server can renumber a device that went offline for a moment.
        Until `REPIN_WAIT` seconds after the start (an absolute deadline,
        asked before every listing and every probe), the server's listing is
        read every `REPIN_POLL` seconds. An entry of this serial that is
        neither ready nor offline fails the pin; when exactly one entry is
        ready, the identity is read through that number (not taken from the
        listing) and must be the pinned device's (`_same_device`). The watch
        and the deadline are asked once more, and only then does the pin move
        (a move to another number counts toward `MAX_REPINS`). Anything else,
        any error on the way included, fails the pin, which stays where it
        was. The whole wait, the watch included, cannot start a second one.
        """
        old = self._transport_id
        assert old is not None  # noqa: S101 - only called while pinned
        self._repinning = True
        try:
            self._count_reconnect()
            deadline = self._clock() + REPIN_WAIT
            while self._clock() < deadline:
                self._stop_if_watched()
                if self._clock() >= deadline:
                    break
                new = self._candidate(old)
                if new is not None and self._clock() < deadline:
                    found = self._identity_through(new)
                    if found is not None:
                        if not self._same_device(found):
                            raise self._fail(
                                self._changed("came back as another device")
                            )
                        self._stop_if_watched()
                        if self._clock() >= deadline:
                            break
                        self._move(old, new)
                        return
                now = self._clock()
                if now < deadline:
                    self._sleep(min(REPIN_POLL, deadline - now))
            raise self._fail(
                self._changed(f"did not come back within {REPIN_WAIT:g} s")
            )
        except BaseException as error:
            code = (
                error.code if isinstance(error, UiprobeError) else type(error).__name__
            )
            self._fail(self._changed(f"could not be proven again ({code})"))
            raise
        finally:
            self._repinning = False
            if self._proven != self._epoch:
                # Any way out but a proof: kept.
                self._fail(self._changed("could not be proven again"))

    def _candidate(self, old: int) -> int | None:
        """Read the listing once; return the one ready number of this serial,
        or None to keep waiting. An entry that rules the device out fails the
        pin: the old number under another serial, an entry of this serial
        that is neither ready nor offline, two ready entries."""
        everything = self._client.devices()
        if any(d.transport_id == old and d.serial != self.serial for d in everything):
            raise self._fail(self._changed("was given to another device"))
        entries = [d for d in everything if d.serial == self.serial]
        if any(d.state not in (READY_STATE, OFFLINE_STATE) for d in entries):
            raise self._fail(self._changed("is not usable any more"))
        ready = [d for d in entries if d.state == READY_STATE]
        if len(ready) > 1:
            raise self._fail(self._changed("is listed more than once"))
        return ready[0].transport_id if ready else None

    def _move(self, old: int, new: int) -> None:
        if new != old:
            if len(self._repins) >= MAX_REPINS:
                raise self._fail(
                    self._changed(f"was renumbered more than {MAX_REPINS} times")
                )
            self._repins.append((old, new))
            self._epoch += 1
        self._transport_id = new
        self._proven = self._epoch

    def _identity_through(self, transport: int) -> DeviceIdentity | None:
        """The identity read through `transport`, or None when that failed.

        The transport is passed to the one request; the pin itself is not
        touched, so nothing else can go to `transport` before it is proven.
        """
        try:
            raw = self._client.shell(
                self.serial, IDENTITY_COMMAND, transport_id=transport
            )
        except (AdbServerUnavailableError, DeviceUnauthorizedError):
            raise  # no use waiting: the wait fails with it
        except _OPTIONAL_ERRORS:
            self._epoch += 1
            return None
        return _parse_identity(raw.decode("utf-8", errors="replace"))

    def _first_proof(self) -> tuple[int, DeviceIdentity]:
        """The transport the device was chosen on and the identity read
        through it; nothing is published here."""
        transport = self._info.transport_id
        if transport is None:
            msg = (
                f"adb reported no transport id for {self.serial}, so the"
                " requests cannot be kept on the device that was chosen"
            )
            raise AdbError(msg, details={"serial": self.serial})
        listed = [d for d in self._client.devices() if d.transport_id == transport]
        if not (
            len(listed) == 1
            and listed[0].serial == self.serial
            and listed[0].state == READY_STATE
        ):
            msg = (
                f"the transport chosen for {self.serial} ({transport}) does"
                " not lead to that device any more"
            )
            details = {"serial": self.serial, "reason": "device_changed"}
            raise DeviceNotFoundError(msg, details=details)
        found = _parse_identity(
            self._client.shell(
                self.serial, IDENTITY_COMMAND, transport_id=transport
            ).decode("utf-8", errors="replace")
        )
        if found is None or not found.fingerprint:
            msg = (
                f"the identity of {self.serial} (ro.build.fingerprint) could"
                " not be read, so the device cannot be told apart later"
            )
            raise AdbError(msg, details={"serial": self.serial})
        return transport, found

    def pin_transport(self) -> int:
        """Send every later request to the transport this device was chosen on.

        The transport id is the one ``adb devices -l`` reported when the
        `Device` was made. Afterwards each request selects that transport
        (``host:transport-id:N``), not the serial, so a device that later takes
        the same serial never receives anything: the requests fail instead.
        Then the identity is read through that transport and kept
        (`pinned_identity`); `check_pin` compares against it. Calling it again
        keeps the first transport and identity.

        When the kept identity has a boot id, a transport that the adb server
        later renumbers is followed (see `can_repin`); without one, anything
        that makes the device be proven again fails the pin.

        Raises:
            AdbError: The adb server reported no transport id for this device,
                so it cannot be pinned (nothing is sent).
            DeviceNotFoundError: The server no longer lists that transport id
                with this serial (``details["reason"]`` is ``device_changed``);
                nothing is pinned and nothing is sent.
            AdbError: The identity could not be read, or has no build
                fingerprint, so the device could not be told apart later;
                nothing is pinned.
        """
        if self._broken is not None:
            raise self._broken_error()
        if self._transport_id is not None:
            return self._transport_id
        # The candidate stays local until it is proven; any way out but
        # success is kept as a failed pin (an interrupt included), so no
        # request of this Device goes out afterwards, not even by serial.
        succeeded = False
        try:
            # From here on a server that cannot be reached is a failure, not a
            # reason to start a new one (it would number its transports
            # anew), and no other request goes out while the first proof runs.
            self._client.stop_starting_server()
            self._repinning = True
            transport, found = self._first_proof()
            self._pinned_identity = found
            self._transport_id = transport
            self._proven = self._epoch
            succeeded = True
        except UiprobeError as error:
            self._broken = (str(error), dict(error.details))
            self._epoch += 1
            raise
        finally:
            self._repinning = False
            if not succeeded and self._broken is None:
                self._broken = (
                    f"pinning {self.serial} did not finish",
                    {"serial": self.serial, "reason": "device_changed"},
                )
                self._epoch += 1
        return self._transport_id

    @property
    def pinned_identity(self) -> DeviceIdentity | None:
        """The identity read when the transport was pinned, None before."""
        return self._pinned_identity

    @property
    def can_repin(self) -> bool:
        """Whether the device may be proven again: a renumbered transport
        followed, or the identity read again after a failed request.

        Only when the pinned identity has a boot id: without one, two
        emulators of one image cannot be told apart, so a renumbered
        transport ends the pin (``device_changed``) as it always did, and so
        does anything else that moves `pin_epoch` (a failed request): the
        next request fails the pin instead of reading the identity again.
        """
        identity = self._pinned_identity
        return identity is not None and bool(identity.boot_id)

    @property
    def repins(self) -> tuple[tuple[int, int], ...]:
        """Each move of the pin so far, as (old transport, new transport)."""
        return tuple(self._repins)

    @property
    def reconnects(self) -> int:
        """How many times the pinned device had to be proven again (a wait,
        also one that finds it back on the same number); at most
        `MAX_RECONNECTS`."""
        return self._reconnects

    @property
    def pin_epoch(self) -> int:
        """A number that moves on every event that makes earlier reads of
        the pinned device doubtful: a failed request, a wait, a move of the
        pin, a failed pin. A reader that sees the same value before and after
        its reads knows none of these happened in between."""
        return self._epoch

    @property
    def pin_watch(self) -> Callable[[], bool] | None:
        """The watch set with `set_pin_watch`, or None."""
        return self._pin_watch

    def set_pin_watch(self, watch: Callable[[], bool] | None) -> None:
        """Ask `watch` while waiting for a renumbered transport.

        When it returns True the wait ends with `DeviceNotFoundError` whose
        ``details["reason"]`` is ``pin_wait_stopped``. None removes it.
        """
        if watch is not None and not callable(watch):
            msg = "watch must be callable or None"
            raise TypeError(msg)
        self._pin_watch = watch

    @property
    def info(self) -> DeviceInfo:
        """What ``adb devices -l`` reported when the device was chosen."""
        return self._info

    def _shell(self, command: str) -> str:
        """Run `command` on the device and return its output as text."""
        return self._shell_text(command)

    def _extras(self, guard: Callable[[], None] | None) -> dict[str, object]:
        """uiprobe's own options of one shell request: the last check of a
        request that changes the device, and no server start mid-launch."""
        extras: dict[str, object] = {}
        if guard is not None:
            extras["_before_send"] = guard
        if self._launch_holds:
            extras["_no_start"] = True
        return extras

    def _shell_text(
        self,
        command: str,
        *,
        timeout: float | None = None,
        strict: bool = False,
        dispatch: _Dispatch | None = None,
    ) -> str:
        changing = command.startswith(_CHANGING_COMMANDS)
        strict = strict or changing
        raw = self._call(
            lambda pin, guard=None: self._client.shell(
                self.serial, command, timeout=timeout, **pin, **self._extras(guard)
            ),
            strict=strict,
            changing=changing,
            dispatch=dispatch,
        )
        return raw.decode("utf-8", errors="replace")

    def tap(
        self,
        target: Element | tuple[int, int] | list[int],
        *,
        long: bool = False,
        press_ms: int = DEFAULT_PRESS_MS,
        _send_by: float | None = None,
        _epoch: int | None = None,
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

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        a value of uiprobe's own clock by which the request must go out, and
        the `pin_epoch` the crawl approved, both checked on the selected
        connection right before the request is written (``send_stopped``,
        ``pin_not_proven``).
        """
        dispatch = _dispatch(_send_by, _epoch)
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
        output = self._shell_text(quote_args(*words) + _EXIT_SUFFIX, dispatch=dispatch)
        failure = _input_failure(
            "long_tap" if long else "tap", {"point": list(point)}, output
        )
        if failure is not None:
            raise self._doubt(failure)
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
            raise self._doubt(failure)
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

    def key(
        self,
        name: str,
        *,
        refused: RefusedKeys | None = None,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> str:
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

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        a value of uiprobe's own clock by which the request must go out, and
        the `pin_epoch` the crawl approved, both checked on the selected
        connection right before the request is written (``send_stopped``,
        ``pin_not_proven``).
        """
        dispatch = _dispatch(_send_by, _epoch)
        if refused is not None and not isinstance(refused, RefusedKeys):
            kind = type(refused).__name__
            msg = f"refused must be a RefusedKeys or None, not {kind}"
            raise TypeError(msg)
        code = _key_code(name, refused)
        output = self._shell_text(
            quote_args("input", "keyevent", code) + _EXIT_SUFFIX, dispatch=dispatch
        )
        failure = _input_failure("key", {"key": code}, output)
        if failure is not None:
            raise self._doubt(failure)
        return code

    def type_text(self, text: str) -> None:
        """Type `text` into the field that has focus, as printable ASCII.

        Only the characters 0x20 to 0x7E are typed. Anything else is refused
        before anything is sent: a character outside ASCII (it failed on every
        Android version tried: emulators at API 24, 30 and 36), a newline or
        tab (they can act as Enter or move the focus; press Enter with `key`),
        any other control character, and empty text. A str subclass is read
        once and used as a plain str with the same characters. The text goes
        to whatever field has focus in whatever app is in front; nothing is
        checked afterwards.

        The text is sent as ``input text`` with the argument quoted for the
        device shell (always in single quotes, a quote inside as ``'"'"'``),
        in pieces of at most `TYPE_PIECE_SIZE` characters; the text is split
        before it is quoted. ``input text`` turns the two characters ``%s``
        into a space, so a piece never contains ``%`` directly followed by
        ``s``: the text is split between them. Each piece is sent once, in
        order, as exactly ``input text <quoted piece>; echo uiprobe-exit:$?``,
        and must report exit status 0 and print nothing else, or typing stops.
        Exit status 0 does not prove that the text arrived.

        Raises:
            TypeError: `text` is not a str.
            ValueError: `text` is empty or has a character that is not typed.
                The error is a `TextNotTypedError`; its ``details`` hold
                ``character`` (``U+XXXX``), ``escaped`` and ``position``.
            InputFailedError: A piece did not report success; ``details``
                hold ``reason``, ``piece``, ``pieces`` and ``pieces_done``
                (no exit status: the device prints it, so it could repeat
                the text).
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.

        The first two are raised before anything is sent to the adb server.
        An error while sending is a new error of the same class whose message
        and ``details`` hold only fixed words and numbers (``piece``,
        ``pieces``, ``pieces_done``): never the text, and nothing the device or
        adb answered, which can repeat it. It does not chain to the error uiprobe
        caught (a caller's own exception being handled stays its context).
        Typing that stops can leave the earlier pieces typed, and the piece it
        stopped at typed in part or in full.
        """
        pieces = _text_pieces(text)  # from one plain str, checked
        for number, piece in enumerate(pieces, start=1):
            failure = self._type_piece(piece, number, len(pieces))
            if failure is not None:
                # outside any handler: no chained error to read
                raise self._doubt(failure)

    def _type_piece(self, piece: str, number: int, pieces: int) -> UiprobeError | None:
        """Send one piece; return the error to raise, or None when it worked.

        A failed request leaves only its error class here, so the error that
        `type_text` raises chains to nothing that holds the text.
        """
        command = f"input text {_single_quoted(piece)}{_EXIT_SUFFIX}"
        kind: type[UiprobeError] | None = None
        unsent = False
        output = ""
        try:
            output = self._shell_text(command)
        except UiprobeError as exc:
            kind = type(exc)
            unsent = exc.details.get("reason") == PIN_NOT_PROVEN
        if kind is not None:
            return _typing_stopped(kind, number, pieces, unsent=unsent)
        return _piece_failure(output, number, pieces)

    def launch(
        self,
        package: str,
        *,
        restart: bool = False,
        timeout: float = DEFAULT_LAUNCH_TIMEOUT,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> None:
        """Start the app `package` and wait until it is in front.

        Earlier versions started apps with ``monkey -p PACKAGE -c
        android.intent.category.LAUNCHER 1``, which turned auto-rotate on
        (``accelerometer_rotation`` 0 to 1; seen on API 24 and 36 emulators
        and a Samsung API 36 phone). ``am start`` does not change it, and
        ``monkey`` is never sent.

        First the launcher activity is read with ``cmd package
        resolve-activity --brief -c android.intent.category.LAUNCHER PACKAGE;
        echo uiprobe-exit:$?`` (see `launcher_activity`), before every start.
        The read goes out only to a device that needs no
        new proof, as a request that changes it would, and when anything
        happened to the pin during it (`pin_epoch` moved) nothing more is sent
        (`DeviceNotFoundError` with ``pin_not_proven``). When the activity
        cannot be read, nothing is sent that changes the device.
        With ``restart=True`` the command ``am force-stop PACKAGE; echo
        uiprobe-exit:$?`` is sent next. It stops the app, so the next launch
        starts a fresh task; state the app has not saved is lost. It must
        report exit status 0 and print nothing else (empty lines are ignored),
        or nothing more is sent. The launcher activity is then read again in
        the same way, and the start uses that second read (the launcher can
        change while the app stops); when it cannot be read, the start is not
        sent and the error says ``force_stopped`` (stopped, not started).
        Then exactly ``am start -W -a android.intent.action.MAIN -c
        android.intent.category.LAUNCHER -n PACKAGE/CLASS; echo
        uiprobe-exit:$?`` is sent. It succeeds only when no line holds a
        control character (after one CR at its end is cut), no line starts
        with ``Error``, the exit status is 0, and the other lines are, each
        at most once: exactly one ``Starting: Intent {
        act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER]
        cmp=PACKAGE/CLASS }`` naming the component sent (short or full
        spelling), one of the two ``Warning:`` lines seen when the app was
        running, exactly one ``Status: ok``, exactly one ``Activity:`` line
        naming an activity of `package` (its class may differ from the one
        started), ``LaunchState:``, ``ThisTime:``, ``TotalTime:``,
        ``WaitTime:`` and ``Complete``. The wait of ``-W`` may stay silent for
        `timeout` seconds (at least `START_MIN_WAIT`). Each command is sent
        once and never resent, not even after a connection error.

        Then `foreground` is read about every 0.5 seconds until it equals
        `package`. It is read at least once, and no read starts after
        `timeout` seconds have passed: the timeout bounds when the last read
        may start, not the whole wait. A read that started in time still
        counts, so the wait can run past the timeout by the time of that read,
        and by how late a pause ends when the system wakes it late.

        A start whose reply names an activity of another app (or of the
        system) is waited on in the same way: when the app comes to the front
        within `timeout`, the launch succeeds; otherwise it fails with
        ``other_activity``. A str subclass is used as a plain str with the
        same text.

        No request of a launch starts an adb server: it uses only the server
        that is already there (`AdbServerUnavailableError`, ``start_skipped``
        ``in_launch``, when it is gone). After its first request, a request
        that fails (also one a read swallows) ends the launch (`AdbError` with
        ``launch_interrupted`` when the error itself was swallowed).
        On a device that is not pinned (as ``uiprobe launch`` uses), each
        request selects the device by its serial. Accepted limit: when the
        device behind the serial is replaced between the launcher read and
        the start, the start uses the component read just before (it fails,
        or opens that component of the same package on the new device). A
        pinned device keeps its pin's rule.

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        `_send_by` is a value of uiprobe's own clock by which the stop and the
        start must go out, and `_epoch` the `pin_epoch` the crawl approved
        (both checked on the selected connection right before each request
        is written; ``send_stopped``, ``pin_not_proven``). The foreground
        reads after the start are bounded by `timeout` only.

        Raises:
            TypeError: `package` is not a str, `restart` is not a bool, or
                `timeout` is not a number (a bool is not).
            ValueError: `package` does not follow the Android package rule
                (see `uiprobe.crawl_preview.PACKAGE_PATTERN`), or `timeout` is
                not from 0 to 600 seconds. Both are raised before the adb
                server is contacted.
            LaunchFailedError: ``details`` hold ``package`` and ``reason``:
                ``launcher_unreadable`` (the launcher activity could not be
                read; ``output`` is the first line that is not blank, stripped,
                cut to 200 characters and escaped; nothing was sent, or, with
                ``force_stopped`` True, the read after the force-stop failed:
                the app was stopped and not started),
                ``stop_failed`` (``am force-stop`` did not report success;
                ``exit_status`` when it reported a status other than 0,
                ``output`` when it printed something else, escaped; with no
                status and no output neither is set; the start was not sent),
                ``start_failed`` (``am start`` printed a line starting with
                ``Error``, or ended with an exit status other than 0, then in
                ``exit_status``), ``other_activity`` (a reply that is a
                success in every other way named an activity of another app
                or of the system (``android``, its chooser), and the app was
                still not in front after the wait; ``activity`` is that
                component, escaped),
                ``unexpected_output`` (``am start`` did not report the success
                described above), these three with ``output``, the
                line at fault or else the first line that is not blank, cut to
                200 characters and escaped, ``start_timeout`` (the reply of
                ``am start -W`` did not come: it was silent for ``timeout``
                seconds, the read timeout used) or ``not_in_front`` (the app
                was not the foreground app in time; ``foreground`` is the last
                package seen (escaped; past 1000 characters cut and ended with
                ``...``), or None, and ``timeout`` the limit). After
                ``start_failed``, ``other_activity``, ``unexpected_output``,
                ``start_timeout`` and ``not_in_front`` the app may still have
                started.
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone, or has to be proven
                again (``pin_not_proven``, nothing sent).
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
        dispatch = _dispatch(_send_by, _epoch)
        self._launch(package, limit, restart=restart, dispatch=dispatch)

    def _launch(
        self,
        package: str,
        limit: float,
        *,
        restart: bool,
        dispatch: _Dispatch | None,
        activity: str | None = None,
    ) -> None:
        """`launch` after its arguments were checked.

        No request of the launch starts an adb server: it uses only a server
        that is already there, so a server that went away cannot be replaced
        by one that lists another device under the serial. After the first
        request, a request that fails (also one a read swallows) ends it.
        `activity`, when given, is a launcher activity the caller read with
        `_start_component` right before (the crawl: then its last checks,
        then the start, with nothing read in between).
        """
        failed = self._failed_requests
        self._launch_holds += 1
        try:
            self._launch_steps(
                package, limit, restart=restart, dispatch=dispatch, activity=activity
            )
        finally:
            self._launch_holds -= 1
        self._check_no_failure(failed)

    def _launch_component(
        self,
        package: str,
        activity: str,
        *,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> None:
        """`launch` of `package` (checked by the caller) that starts
        `activity`, read by the caller with `_start_component` right before,
        without reading it again (the crawl's first launch and relaunch)."""
        dispatch = _dispatch(_send_by, _epoch)
        self._launch(
            package,
            DEFAULT_LAUNCH_TIMEOUT,
            restart=False,
            dispatch=dispatch,
            activity=activity,
        )

    def _start_component(self, package: str) -> str:
        """The launcher read before a start: `launcher_activity` sent only to
        a device proven now, as a start would be, and refused
        (``pin_not_proven``) when anything happened to the pin during it."""
        epoch = self._epoch
        activity = self._launcher_activity(package, strict=True)
        if self._epoch != epoch:
            msg = (
                f"{self.serial} has to be proven again before an input"
                f" (transport {self._transport_id}); nothing was sent"
            )
            details = {"serial": self.serial, "reason": PIN_NOT_PROVEN}
            raise DeviceNotFoundError(msg, details=details)
        return activity

    def _start_component_after_stop(self, package: str) -> str:
        """`_start_component` after the app was force-stopped: every failure
        says so (``force_stopped``: stopped, not started)."""
        try:
            return self._start_component(package)
        except LaunchFailedError as error:
            msg = f"{package} was force-stopped but not started again: {error}"
            details = {**error.details, "force_stopped": True}
            raise LaunchFailedError(msg, details=details) from None
        except DeviceNotFoundError as error:
            if error.details.get("reason") != PIN_NOT_PROVEN:
                raise
            msg = (
                f"{package} was force-stopped, but the start was not sent: the"
                f" device has to be proven again"
            )
            details = {**error.details, "force_stopped": True}
            raise DeviceNotFoundError(msg, details=details) from None
        except UiprobeError as error:
            # Any other failure of the read (a transport error, a timeout)
            # also says that the app was stopped and not started.
            error.details = {**error.details, "force_stopped": True}
            raise

    def _check_no_failure(self, failed: int) -> None:
        """Raise when a request failed since `failed` was taken in a launch."""
        if self._failed_requests != failed:
            msg = (
                f"a request to {self.serial} failed during the launch; nothing"
                " more was sent"
            )
            raise AdbError(msg, details={"reason": "launch_interrupted"})

    def _launch_steps(
        self,
        package: str,
        limit: float,
        *,
        restart: bool,
        dispatch: _Dispatch | None,
        activity: str | None = None,
    ) -> None:
        """The launcher reads, the stop, the start and the foreground reads."""
        failed = self._failed_requests
        if activity is None:
            # The launcher is read before every start. With a restart it is
            # read before the stop (an app whose launcher cannot be read is
            # not stopped) and again after it, and the start uses the second
            # read: the launcher can change while the app stops.
            activity = self._start_component(package)
            failed = self._failed_requests
            if restart:
                self._force_stop(package, dispatch)
                self._check_no_failure(failed)
                activity = self._start_component_after_stop(package)
        elif restart:
            self._force_stop(package, dispatch)
        self._check_no_failure(failed)
        # A start that named an activity of another app is waited on like
        # any other: the app may come to the front in time.
        other = self._start(
            package, activity, limit, force_stopped=restart, dispatch=dispatch
        )
        details: dict[str, object] = {"package": package}
        deadline = self._clock() + limit
        while True:
            self._check_no_failure(failed)
            seen = self.foreground()
            if seen == package:
                return
            if self._clock() >= deadline:
                break
            self._sleep(max(0.0, min(LAUNCH_POLL_INTERVAL, deadline - self._clock())))
            if self._clock() > deadline:
                break  # no read starts after the deadline
        if other is not None:
            raise other
        shown = None if seen is None else _shown_foreground(seen)
        details.update(reason="not_in_front", foreground=shown, timeout=limit)
        last = "unknown" if shown is None else shown
        msg = f"{package} was not in front after {limit:g} seconds (foreground: {last})"
        raise LaunchFailedError(msg, details=details)

    def launcher_activity(self, package: str) -> str:
        """Return the launcher activity of `package` as ``PACKAGE/CLASS``.

        It sends ``cmd package resolve-activity --brief -c
        android.intent.category.LAUNCHER PACKAGE; echo uiprobe-exit:$?`` (a
        read: nothing on the device changes). The output must be zero or more
        lines like ``priority=0 preferredOrder=0 match=0x108000
        specificIndex=-1 isDefault=true``, then exactly one line
        ``PACKAGE/CLASS`` with exactly `package` before the slash and a class
        of dot-separated parts of ASCII letters, digits, ``_`` and ``$`` (it
        may start with ``.``; at most 255 characters), then the line
        ``uiprobe-exit:0`` last, so output cut short and another exit status
        are refused. Empty lines and one CR at each line end are ignored. The
        CLASS is returned as printed, often in the short form ``.Name``.

        Raises:
            TypeError, ValueError: `package` is not a valid package name
                (before the adb server is contacted).
            LaunchFailedError: Anything else was printed (``No activity
                found`` for a package that is not installed or has no
                launcher activity, another package, more lines);
                ``details["reason"]`` is ``launcher_unreadable`` and
                ``details["output"]`` the first line that is not blank,
                stripped, cut to 200 characters and escaped.
            AdbError: The adb protocol or the command failed.
        """
        if isinstance(package, str):
            package = str.__str__(package)
        return self._launcher_activity(check_package(package))

    def _launcher_activity(self, package: str, *, strict: bool = False) -> str:
        """Read the launcher activity; `strict` sends the read only to a device
        that needs no new proof (as a request that changes it)."""
        output = self._shell_text(
            quote_args(
                "cmd",
                "package",
                "resolve-activity",
                "--brief",
                "-c",
                LAUNCHER_CATEGORY,
                package,
            )
            + _EXIT_SUFFIX,
            strict=strict,
        )
        component = _launcher_component(output, package)
        if component is not None:
            return component
        first = _escaped_line(output)
        details: dict[str, object] = {
            "package": package,
            "reason": "launcher_unreadable",
            "output": first,
        }
        msg = (
            f"the launcher activity of {package} could not be read on this device,"
            f" so it was not started; the output began '{first}'"
        )
        raise LaunchFailedError(msg, details=details)

    def _start(
        self,
        package: str,
        activity: str,
        limit: float,
        *,
        force_stopped: bool,
        dispatch: _Dispatch | None = None,
    ) -> LaunchFailedError | None:
        """Send ``am start -W`` of `activity` once and require it to succeed.

        Return the ``other_activity`` error, not raised, when a reply that is
        a success in every other way named an activity of another app: the
        caller waits for the app to come to the front first.
        """
        command = (
            quote_args(
                "am",
                "start",
                "-W",
                "-a",
                MAIN_ACTION,
                "-c",
                LAUNCHER_CATEGORY,
                "-n",
                activity,
            )
            + _EXIT_SUFFIX
        )
        wait = max(limit, START_MIN_WAIT)
        details: dict[str, object] = {"package": package}
        try:
            output = self._shell_text(command, timeout=wait, dispatch=dispatch)
        except DeviceNotFoundError as error:
            if not force_stopped or error.details.get("reason") != PIN_NOT_PROVEN:
                raise
            msg = (
                f"{package} was force-stopped, but the start was not sent: the"
                f" device has to be proven again"
            )
            details = {**error.details, "force_stopped": True}
            raise DeviceNotFoundError(msg, details=details) from None
        except AdbError as error:
            if (
                error.details.get("reason") != "timeout"
                or error.details.get("stage") != "output"
            ):
                raise
            # The request failed, so the pin was already doubted.
            details.update(reason="start_timeout", timeout=wait)
            msg = (
                f"the reply of am start -W for {package} did not come within"
                f" {wait:g} seconds; whether the app started is unknown"
            )
            raise LaunchFailedError(msg, details=details) from None
        failure = _start_failure(output, package, activity)
        if failure is None:
            return None
        line = _escaped_line(output) if failure.line is None else _escaped(failure.line)
        details.update(reason=failure.reason, output=line)
        if failure.exit_status is not None:
            details["exit_status"] = failure.exit_status
        msg = f"am start of {package} {failure.problem}; its output: '{line}'"
        if failure.other is not None:
            # A complete, well-formed reply: nothing about the pin is in doubt.
            details["activity"] = _escaped(failure.other, _FOREGROUND_LIMIT)
            return LaunchFailedError(msg, details=details)
        raise self._doubt(LaunchFailedError(msg, details=details))

    def stop_app(
        self,
        package: str,
        *,
        _send_by: float | None = None,
        _epoch: int | None = None,
    ) -> None:
        """Send ``am force-stop PACKAGE`` once and require it to report success.

        It is the first half of ``launch(package, restart=True)``, for a caller
        that wants to decide between the stop and the start. State the app has
        not saved is lost.

        Raises:
            TypeError, ValueError: `package` is not a valid package name
                (before the adb server is contacted).
            LaunchFailedError: The stop did not report exit status 0 with no
                other output (``details["reason"]`` is ``stop_failed``).
            AdbError: The adb protocol or the command failed.

        `_send_by` and `_epoch` are for uiprobe's crawl, not part of the API:
        a value of uiprobe's own clock by which the request must go out, and
        the `pin_epoch` the crawl approved, both checked on the selected
        connection right before the request is written (``send_stopped``,
        ``pin_not_proven``).
        """
        dispatch = _dispatch(_send_by, _epoch)
        if isinstance(package, str):
            package = str.__str__(package)
        self._force_stop(check_package(package), dispatch)

    def _force_stop(self, package: str, dispatch: _Dispatch | None = None) -> None:
        stop = self._shell_text(
            quote_args("am", "force-stop", package) + _EXIT_SUFFIX, dispatch=dispatch
        )
        failure = _input_failure("force-stop", {}, stop)
        if failure is None:
            return
        self._doubt(failure)
        details: dict[str, object] = {"package": package, "reason": "stop_failed"}
        if "exit_status" in failure.details:
            details["exit_status"] = failure.details["exit_status"]
        output = failure.details.get("output")
        if isinstance(output, str):
            details["output"] = _escaped(output)
        msg = (
            f"am force-stop {package} did not report success, so the app was not"
            " launched"
        )
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

    def focused_window(self) -> FocusedWindow | None:
        """Return the window that has input focus: its owner and its title.

        ``mCurrentFocus`` in ``dumpsys window`` names the window (``Window{HASH
        u0 TITLE}``); its block in ``dumpsys window windows`` (``Window #N
        Window{HASH u0 TITLE}:``) has, two lines below, the line ``mOwnerUid=...
        package=OWNER``, which the system writes. The owner comes from that
        line, never from the title, which an app chooses. Captured in the same
        form on emulators at API 24, 30 and 36; the notification shade is then
        a window of ``com.android.systemui``.

        None means unknown: no or several ``mCurrentFocus`` values, ``null``,
        a malformed line, no block or several blocks with that hash (titles
        can hold newlines, so a forged block can be added but the genuine one
        cannot be removed), a block whose title differs, or an owner line of
        another shape. The two dumps are read one after the other, so a focus
        that changed in between gives None too.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        focus = _read_current_focus(self._shell("dumpsys window"))
        if focus is None:
            return None
        found = _read_window_owner(self._shell("dumpsys window windows"), *focus)
        # Read the focus again: a window that took it between the two dumps
        # would otherwise be given the old window's owner.
        if found is None:
            return None
        return (
            found
            if _read_current_focus(self._shell("dumpsys window")) == focus
            else None
        )

    def identity(self) -> DeviceIdentity | None:
        """Read whether this is an emulator, its serial number and build, at once.

        One command reads ``ro.kernel.qemu``, ``ro.serialno``,
        ``ro.build.fingerprint`` and ``/proc/sys/kernel/random/boot_id``. None
        means the answer did not have exactly those four lines. A boot id that
        is not a lowercase UUID is kept as ``""`` (not readable).

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return _parse_identity(self._shell(IDENTITY_COMMAND))

    def check_pin(self, *, emulator: bool) -> None:
        """Raise unless the pinned transport still leads to the same device.

        The adb server must still list the pinned transport id with this serial
        (a restarted server numbers its transports anew; a renumbered one is
        followed only as `can_repin` allows), and `identity` through it must
        be the pinned device (a pinned boot id read again and the same; a boot
        id that could no longer be read counts as a change; without a pinned
        boot id the three other fields); with `emulator` it must also be an
        emulator. Nothing is sent but these reads. A failure is kept: every
        later request of this `Device` raises it again.

        Raises:
            DeviceNotFoundError: The pinned transport is gone, lists another
                serial, or leads to a device whose identity differs
                (``details["reason"]`` is ``device_changed``).
            AdbError: `pin_transport` was not called, or the reads failed.
        """
        if type(emulator) is not bool:
            msg = f"emulator must be a bool, not {type(emulator).__name__}"
            raise TypeError(msg)
        expected = self._pinned_identity
        if self._transport_id is None or expected is None:
            msg = "the transport is not pinned"
            raise AdbError(msg, details={"serial": self.serial})
        # The read and the comparison are one step: any way out but a match
        # (an interrupt while the answer is parsed, say) makes the proof stale.
        matched = False
        try:
            found = self.identity()  # its request checks the listing first
            matched = self._same_device(found) and not (
                emulator and (found is None or not found.emulator)
            )
        except BaseException as error:
            self._interrupted(error)
            raise
        finally:
            if not matched:
                self._epoch += 1
        if not matched:
            msg = (
                f"the device pinned as {self.serial} (transport"
                f" {self._transport_id}) is not the one that was checked any more"
            )
            details = {"serial": self.serial, "reason": "device_changed"}
            raise self._fail(DeviceNotFoundError(msg, details=details))
        # No mark is set here: the identity read itself went out only with a
        # current proof (see `_check_listed`), which a proof had set.

    def window_at(self, point: tuple[int, int]) -> InputWindow | str:
        """Return the window a touch at `point` would reach, or why that is
        not known.

        Reads ``dumpsys input`` once and takes the topmost window of display
        0 that takes touches at `point` (see `uiprobe.inputwindows`). The
        string is one of ``no_list``, ``line_format``, ``region_format``,
        ``rotation`` (the display is rotated, or its orientation cannot be
        read), ``no_window`` (no window takes a touch there) or ``portal``
        (the window there forwards touches to another display); the first
        four are an `Unreadable` whose ``rule`` names the check that refused
        (see `diagnose_tap_window`).

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        return read_window_at(self._shell("dumpsys input"), point)

    def diagnose_tap_window(
        self, *, show_device_text: bool = False
    ) -> TapWindowDiagnosis:
        """Tell whether `window_at` can read this device's window list, and if
        not which rule refused it and where, with no text of the device's
        output in the result (see `uiprobe.windowdiag`).

        Reads ``dumpsys input`` once. With `show_device_text` the result
        also holds the shapes of the lines, which hold device text.

        Raises:
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        if not isinstance(show_device_text, bool):
            msg = "show_device_text must be a bool"
            raise TypeError(msg)
        text = self._shell("dumpsys input")
        return diagnose_tap_window(text, show_device_text=show_device_text)

    def package_uid(self, package: str) -> PackageUid | None:
        """Return the app id the system gave `package` and whether the package
        shares it, None when not readable.

        Reads ``userId=`` or ``appId=`` and ``sharedUser=`` from the
        package's own stanza of ``dumpsys package <package>`` (see
        `uiprobe.packageuid`); a package that is not installed, or whose
        stanzas disagree, gives None. The app id is the uid of the package's
        windows for Android user 0 only. It can change when the app is
        installed again.

        Raises:
            TypeError, ValueError: `package` is not a valid package name
                (before any request).
            AdbError: The adb protocol or the command failed.
            AdbServerUnavailableError: The adb server cannot be reached.
            DeviceNotFoundError: The device is gone.
            DeviceOfflineError: The device is offline.
            DeviceUnauthorizedError: The device has not authorized this host.
        """
        if isinstance(package, str):
            package = str.__str__(package)
        check_package(package)
        text = self._shell(quote_args("dumpsys", "package", package))
        return parse_package_uid(text, package)

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
            rotation=parsed.rotation,
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
        data = self._call(
            lambda pin: self._client.exec_out(
                self.serial,
                quote_args("screencap", "-p"),
                timeout=SCREENSHOT_TIMEOUT,
                **pin,
            ),
            strict=False,
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
            return self._call(
                lambda pin: self._client.exec_out(
                    self.serial, quote_args("cat", path), **pin
                ),
                strict=False,
            )
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
