"""One Android device: choosing it, running commands on it, dumping its screen."""

from __future__ import annotations

import contextlib  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import secrets  # noqa: F401
import time  # noqa: F401
from collections.abc import Callable  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from datetime import (  # noqa: F401
    UTC,
    datetime,
)
from typing import (  # noqa: F401
    TYPE_CHECKING,
    Final,
    NamedTuple,
)

from uiprobe.adb import (  # noqa: F401
    AdbClient,
    DeviceInfo,
    ascii_int,
    find_adb,
    quote_args,
    resolve_server_address,
    start_blocked_by_environment,
)
from uiprobe.crawl_preview import (  # noqa: F401
    MAX_PACKAGE_LENGTH,
    PACKAGE_PATTERN,
    check_package,
)
from uiprobe.device.apps import (  # noqa: F401
    DEFAULT_LAUNCH_TIMEOUT,
    LAUNCH_POLL_INTERVAL,
    LAUNCHER_CATEGORY,
    MAIN_ACTION,
    SCREENSHOT_TIMEOUT,
    START_MIN_WAIT,
    _AppsMixin,
)
from uiprobe.device.failure import (  # noqa: F401
    _EXIT_SUFFIX,
    _EXIT_VALUE,
    _OUTPUT_LIMIT,
    EXIT_MARKER,
    _escaped,
    _escaped_line,
    _input_failure,
    _lines,
)
from uiprobe.device.focus import (  # noqa: F401
    _ABSENT,
    _BOOL,
    _COMPONENT,
    _CURRENT_FOCUS,
    _CURRENT_FOCUS_CLAIM,
    _CURRENT_FOCUS_EXACT,
    _CURRENT_FOCUS_FIELD,
    _CURRENT_FOCUS_NULL,
    _DISPLAY_HEADER_FIELD,
    _FOCUSED_APP_SOURCE,
    _INPUT_DISPLAY,
    _INPUT_SHOWN_SHAPES,
    _MAX_DUMPSYS_LINE,
    _RECORD,
    _RESUMED_EXACT,
    _RESUMED_FIELD,
    _RESUMED_SOURCE,
    _TASK_SUFFIX,
    _TOP_FOCUSED_DISPLAY,
    _TOP_FOCUSED_DISPLAY_FIELD,
    _TOP_FOCUSED_DISPLAY_ZERO,
    _UIDS_PER_USER,
    _WINDOW_DISPLAY,
    _WINDOW_HEAD,
    _WINDOW_HEAD_HASH,
    _WINDOW_ON_DISPLAY_ZERO,
    _WINDOW_OWNER,
    FocusedWindow,
    _display_zero_focused,
    _dump_lines,
    _names_a_display,
    _names_other_display,
    _read_current_focus,
    _read_focus_of_several,
    _read_input_shown,
    _read_resumed_of_several,
    _read_source,
    _read_window_owner,
    _several_displays,
    _Source,
    _window_focus,
    displays_seen,
    forget_displays,
)
from uiprobe.device.foreground import _ForegroundMixin
from uiprobe.device.gestures import (  # noqa: F401
    _DIRECTIONS,
    _MAX_COORDINATE,
    _MAX_SWIPE_MS,
    _SWIPE_SLACK,
    DEFAULT_PRESS_MS,
    DEFAULT_SWIPE_MS,
    LONG_PRESS_MS,
    MAX_PRESS_MS,
    MAX_WHEEL_AMOUNT,
    SCROLL_EDGE_PERCENT,
    SCROLL_INSET_PERCENT,
    DisplaySizeUnknownError,
    _check_coordinate,
    _check_direction,
    _check_duration,
    _check_press_ms,
    _press_point,
    _scroll_area,
    _scroll_points,
    _scroll_rectangle,
)
from uiprobe.device.input import _InputMixin
from uiprobe.device.keys import (  # noqa: F401
    _KEY_CODES,
    _KEYCODE_DIGITS,
    _KEYCODE_NAME,
    _NUMBER,
    DEVICE_WIDE_KEY_PREFIXES,
    DEVICE_WIDE_KEYS,
    FIXED_DEVICE_KEYS,
    KEY_ALIASES,
    RefusedKeys,
    _exact_key_name,
    _key_code,
    _refuse_device_wide,
)
from uiprobe.device.launch import (  # noqa: F401
    _CLASS_NAME,
    _CONTROL,
    _FOREGROUND_LIMIT,
    _MAX_CLASS_NAME,
    _RESOLVE_INFO,
    _START_ACTIVITY,
    _START_DONE,
    _START_ECHO,
    _START_INFO,
    _START_OK,
    _START_WARNINGS,
    MAX_LAUNCH_TIMEOUT,
    SYSTEM_PACKAGE,
    _check_launch_timeout,
    _home_package,
    _is_component,
    _launcher_component,
    _shown_foreground,
    _spellings,
    _start_failure,
    _start_line_kind,
    _start_report_failure,
    _StartFailure,
)
from uiprobe.device.parse import (  # noqa: F401
    _CURRENT_SIZE,
    _DISPLAY_ZERO_LINE,
    _MAX_DISPLAY_SIDE,
    _WM_SIZE_LINE,
    _activity_name,
    _parse_display_size,
    _parse_wm_size,
)
from uiprobe.device.pin import (  # noqa: F401
    _CHANGING_COMMANDS,
    MAX_RECONNECTS,
    MAX_REPINS,
    PIN_WAIT_STOPPED,
    REPIN_POLL,
    REPIN_WAIT,
    SEND_STOPPED,
    _monotonic,
    _PinMixin,
)
from uiprobe.device.reads import (  # noqa: F401
    DUMP_DIRECTORY,
    DUMP_MESSAGE_LIMIT,
    DUMP_TIMEOUT,
    _ReadsMixin,
)
from uiprobe.device.select import (  # noqa: F401
    _BOOT_ID,
    _OPTIONAL_ERRORS,
    IDENTITY_COMMAND,
    OFFLINE_STATE,
    PIN_NOT_PROVEN,
    READY_STATE,
    DeviceDetails,
    DeviceIdentity,
    _brief,
    _check_serial,
    _Dispatch,
    _dispatch,
    _getprop,
    _make_client,
    _parse_identity,
    _select_requested,
    _state_error,
    device_details,
    devices,
    select_device,
)
from uiprobe.device.text import (  # noqa: F401
    _TYPING_STOPPED_BECAUSE,
    TYPE_PIECE_SIZE,
    TextNotTypedError,
    _check_text,
    _piece_failure,
    _plain_text,
    _single_quoted,
    _text_pieces,
    _typing_stopped,
)
from uiprobe.errors import (  # noqa: F401
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
from uiprobe.inputwindows import (  # noqa: F401
    InputWindow,
    read_window_at,
)
from uiprobe.model import (  # noqa: F401
    Bounds,
    Element,
    Screen,
    parse_uiautomator_dump,
    shorten_activity,
)
from uiprobe.packageuid import (  # noqa: F401
    PackageUid,
    parse_package_uid,
)
from uiprobe.pngfile import check_png  # noqa: F401
from uiprobe.windowdiag import (  # noqa: F401
    TapWindowDiagnosis,
    diagnose_tap_window,
)

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


class Device(_PinMixin, _ReadsMixin, _ForegroundMixin, _InputMixin, _AppsMixin):
    """One connected Android device.

    The device is chosen when the object is created (see `select_device`), so
    a missing or ambiguous device fails here and not at the first operation.

    Raises:
        TypeError: An argument has the wrong type.
        ValueError: An argument has a bad value.

    Both are raised before the adb server is contacted.
    """
