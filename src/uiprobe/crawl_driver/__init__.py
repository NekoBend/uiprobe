"""The pressing crawl: sends the engine's actions to a device and records them.

`crawl` joins three parts that stay separate elsewhere: `CrawlEngine` decides
every step, `Device` sends it, and `RunWriter` records it. It lives in its own
module, not in ``crawl/``, because that module is the pure engine (no device,
clock or file), and the writer imports it, so the driver sits above both.

``uiprobe crawl --press`` runs it from the command line; the function is alpha.

What is sent. Only what the engine returned, and for a tap only what
`CrawlEngine.recheck` allowed for that very `Tap` object, in the gate right
before it is sent: the first launch (by default ``force-stop`` then
start of the launcher activity, whose screen is up to the app; with
``restart=False`` only the start), a relaunch
(``force-stop`` then start; a launch is ``am start -W`` of the launcher
activity, read right before each start, never ``monkey``), a tap at the
point judge gave, and the BACK key. Text, long presses, swipes and other
keys are never sent. By default the engine never
returns BACK for a screen of another app (it relaunches instead);
``Policy.back_on_other_screens`` allows it. A BACK or
relaunch decided on such a screen names its package (and the foreground) as
``off_target`` in the start line. A package of ``Policy.also_target`` is not
another app in this sense: its screens are the target's own (the window at a tap
must belong to that package's uid), and it is never started or stopped. Every
action is sent once and never retried. A tap is reported to the engine with
`CrawlEngine.tap_sent` only after it was sent without an error; a refused tap,
or one whose send raised, is never reported, and the engine releases it on the
next `CrawlEngine.next` with a ``tap_not_sent`` skip row.

The loop (the protocol of the `CrawlEngine` docstring)::

    pin the device's transport; check it is an emulator (through that pin)
    launch (by default force-stop, then start, as one action); observe
    repeat:
        if stop() is true: end with "interrupted"
        action = engine.next(observation)
        Stop:    end with the engine's reason
        Reread:  observe again (nothing is sent, no transition line)
        other:   transition start line, then the gate, then the input once
                 (tap and BACK: then a pause of `settle`); for a tap
                 engine.tap_sent(tap) with the same object, only when the
                 send did not raise; then observe; the first observation
                 after a sent action gets the transition end line

The gate (`_Run.gate`) is the one place every action passes right before it is
sent. In order: `stop` and the time limit; the pin (`Device.check_pin`: the
same transport, serial and identity with its boot id, a renumbered transport
followed only as `Device.can_repin` allows, else an error); `stop` again;
then fresh reads, for a tap the dump first; then the clock again (the
caller's code, as on main); last the comparison of the pin. No code of the
caller runs between that comparison and the input: `stop`, `sleep` and the
run folder writes all come before the reads or after the input. The
force-stop of the first launch (the default, ``restart=True``) has the gate
of a relaunch's force-stop without the screen reads, as there is no
observation yet but with the launcher read of the first launch's gate, so
an app whose launcher cannot be read is not force-stopped
(`_Run.restart_gate`); then the first launch goes on exactly as a relaunch
(`_Run.relaunch`): `stop`, the time, the pin check, `stop`,
the launcher read, the time, the last comparison, the start. The time
limit is also turned into a value of uiprobe's own clock (`_Run.now`) and
checked by the device, with the pin, on the request's own connection right
before it is written. It refuses, with a ``not_sent`` end line and nothing
sent, when anything it needs cannot be read or has changed: for a tap the
engine's `CrawlEngine.recheck` on the fresh dump, a keyboard known to be hidden
and a focused window owned by the app; for BACK and relaunch the same
foreground and focused window (and for BACK keyboard) as the observation they
were chosen on, and for a BACK chosen on a screen of the app also a focused
window owned by the app (a BACK goes to the focused window). The table of
checks per action is in docs/python.md. After the decision nothing else runs
before the input. An action refused as ``device_reconnected`` (by the gate,
or because the device had to be proven again right before the input) is taken
back with `CrawlEngine.withdraw` and decided again on a fresh read. Otherwise a
refused tap's fresh observation goes to `CrawlEngine.next`, which releases the
unreported tap, and a refused BACK or relaunch ends the run with ``not_sent``,
because the engine already counted it. Every observation checks the pin too.

How a run ends. The engine's stop reason, ``not_sent``, ``interrupted`` (the ``stop``
callable returned True between two actions, or Ctrl+C arrived while it
returns True) or ``error``. `RunWriter.finish` then writes ``run.json`` last.

* An error of uiprobe or of the device that prevents going on (adb, device,
  input, launch, run folder) writes ``run.json`` with ``error`` and the error
  code in the summary, then is raised again unchanged. Any other exception
  does the same with the code ``internal_error``. A transition that has a
  start line and no end line is an action whose result is unknown (the input
  may or may not have been delivered).
* `KeyboardInterrupt`: ``run.json`` is written with ``interrupted`` when the
  writer still can (an interrupt that broke a write leaves it refusing). If
  ``stop`` is given and returns True at that moment and ``run.json`` was
  written, `crawl` returns normally with ``stop_reason == "interrupted"``;
  otherwise the interrupt is raised again, because a library never swallows
  an interrupt. A second
  interrupt while ``run.json`` is being written can leave the folder without
  it; the writing is marked with `RunWriter.critical` so a handler can wait.
* If ``run.json`` itself cannot be written while another error is being
  raised, the original error is the one raised.
"""

from __future__ import annotations

import contextlib  # noqa: F401
import os  # noqa: F401
import time  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from typing import TYPE_CHECKING, Final  # noqa: F401

from uiprobe.crawl import (  # noqa: F401
    Back,
    CrawlEngine,
    Launch,
    Limits,
    Observation,
    Relaunch,
    Reread,
    Stop,
    Tap,
    _check_clock,
    _ids_unique,
    _Prints,
)
from uiprobe.crawl_driver.constants import (  # noqa: F401
    _COVER_REASONS,
    _FIRST_ACTION,
    _FIRST_APP_UID,
    _HALF_RELAUNCH,
    _MAX_SECONDS,
    _OWN_CLOCK,
    _SEND_PATHS,
    FOCUS_CAUSE_SEVERAL_DISPLAYS,
    INTERRUPTED,
    MAX_DURATION,
    NOT_SENT_CHANGED,
    NOT_SENT_COVER_UNKNOWN,
    NOT_SENT_COVERED,
    NOT_SENT_FOCUS_CHANGED,
    NOT_SENT_FOCUS_OTHER,
    NOT_SENT_FOCUS_UNKNOWN,
    NOT_SENT_FOREGROUND_UNKNOWN,
    NOT_SENT_KEYBOARD_SHOWN,
    NOT_SENT_KEYBOARD_UNKNOWN,
    NOT_SENT_RECONNECTED,
    NOT_SENT_REFUSED,
    NOT_SENT_START,
    NOT_SENT_STOPPED,
    NOT_SENT_TIME_LIMIT,
    PAUSE_STEP,
    STOP_ERROR,
    STOP_NOT_SENT,
)
from uiprobe.crawl_driver.driver import (  # noqa: F401
    CrawlResult,
    _check_callable,
    _check_device,
    _check_flag,
    _crawl_on,
    _drive,
    _end,
    _mark_own_run_folder,
    _result,
    _stop_requested,
    crawl,
)
from uiprobe.crawl_driver.helpers import (  # noqa: F401
    _app_window,
    _bounds,
    _element_of,
    _Ended,
    _failure_word,
    _flag,
    _hide_password_notes,
    _ints,
    _kind,
    _plain_policy,
    _seconds,
    _Seen,
    _skipped_element,
    _unreadable_launcher,
    _words,
)
from uiprobe.crawl_driver.run import _Run  # noqa: F401
from uiprobe.crawl_preview import check_package  # noqa: F401
from uiprobe.device import (  # noqa: F401
    PIN_NOT_PROVEN,
    PIN_WAIT_STOPPED,
    SEND_STOPPED,
    _monotonic,
    displays_seen,
    forget_displays,
)
from uiprobe.errors import (  # noqa: F401
    DeviceNotFoundError,
    DumpError,
    LaunchFailedError,
    NotEmulatorError,
    ScreenshotFailedError,
    UiprobeError,
)
from uiprobe.guards import _WORD_REASONS, Policy, judge  # noqa: F401
from uiprobe.inputwindows import ROTATION, Unreadable  # noqa: F401
from uiprobe.model import Bounds  # noqa: F401
from uiprobe.runlog import RunFolderError, RunWriter  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import Callable  # noqa: F401
    from pathlib import Path  # noqa: F401

    from uiprobe.crawl import Action, SkippedElement  # noqa: F401
    from uiprobe.device import Device, FocusedWindow  # noqa: F401
    from uiprobe.inputwindows import InputWindow  # noqa: F401
    from uiprobe.model import Element, Screen  # noqa: F401
    from uiprobe.packageuid import PackageUid  # noqa: F401

__all__ = ["INTERRUPTED", "STOP_ERROR", "CrawlResult", "crawl"]
