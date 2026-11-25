"""Stop reasons, not-sent reasons and limits the crawl driver shares."""

from __future__ import annotations

import time
from typing import Final

# The two stop reasons the driver adds to the engine's six.
INTERRUPTED: Final = "interrupted"

STOP_ERROR: Final = "error"

# Why an action whose start line was written was then not sent (the
# ``reason`` of its ``not_sent`` end line). Whatever cannot be read counts as
# a reason not to send.
NOT_SENT_STOPPED: Final = "stopped"

NOT_SENT_TIME_LIMIT: Final = "time_limit"

NOT_SENT_FOREGROUND_UNKNOWN: Final = "foreground_unknown"

NOT_SENT_FOCUS_UNKNOWN: Final = "focus_unknown"

# The ``cause`` in the details of a ``focus_unknown`` end line when the focus
# was read on a device with several displays and Policy.check_tap_window is
# off: the focus reading alone is not trusted there.
FOCUS_CAUSE_SEVERAL_DISPLAYS: Final = "several_displays_need_tap_window_check"

NOT_SENT_FOCUS_OTHER: Final = "focus_other"

NOT_SENT_FOCUS_CHANGED: Final = "focus_changed"

NOT_SENT_KEYBOARD_UNKNOWN: Final = "keyboard_unknown"

NOT_SENT_KEYBOARD_SHOWN: Final = "keyboard_shown"

NOT_SENT_CHANGED: Final = "changed_before_send"

NOT_SENT_REFUSED: Final = "changed_before_press"

NOT_SENT_START: Final = "start_not_sent"

# The pinned device was away (and came back, proven the same) after the
# observation the action was chosen on: what was read may be stale.
NOT_SENT_RECONNECTED: Final = "device_reconnected"

# A window of another app takes touches at the tap point, or which window
# does cannot be read (Policy.check_tap_window).
NOT_SENT_COVERED: Final = "covered_by_other"

NOT_SENT_COVER_UNKNOWN: Final = "cover_unknown"

_COVER_REASONS: Final = frozenset({NOT_SENT_COVERED, NOT_SENT_COVER_UNKNOWN})

# Android gives installed apps uids from 10000 on; below are system uids that
# several packages share (and system windows have).
_FIRST_APP_UID: Final = 10000

# What `_Run.relaunch` returns when it force-stopped the app and then did not
# start it because the device had to be proven again (not an end-line reason).
_HALF_RELAUNCH: Final = "relaunch_half_sent"

# The action id of the first launch: the run's first start line.
_FIRST_ACTION: Final = 1

# The stop reason of a run that ended because BACK or a relaunch was not sent.
STOP_NOT_SENT: Final = "not_sent"

MAX_DURATION: Final = "max_duration"  # the engine's reason for the time limit

# How often a pause looks at `stop` and the time limit.
PAUSE_STEP: Final = 0.1

# The requests that change the device, each passing `_Run.final_check` right
# before it is sent: a tap, BACK, the force-stop of a relaunch (and of the
# first launch, by default), the first launch's start without a force-stop
# and the start after a force-stop (a relaunch's, and the first launch's by
# default).
_SEND_PATHS: Final = ("tap", "back", "force-stop", "launch", "start")

# The default clock of `crawl`: uiprobe's own (`time.monotonic`).
_OWN_CLOCK: Final = time.monotonic

_MAX_SECONDS: Final = 2**40
