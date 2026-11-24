"""The pressing crawl: sends the engine's actions to a device and records them.

`crawl` joins three parts that stay separate elsewhere: `CrawlEngine` decides
every step, `Device` sends it, and `RunWriter` records it. It lives in its own
module, not in ``crawl.py``, because that module is the pure engine (no device,
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
``off_target`` in the start line. Every
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

import contextlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from uiprobe.crawl import (
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
from uiprobe.crawl_preview import check_package
from uiprobe.device import (
    PIN_NOT_PROVEN,
    PIN_WAIT_STOPPED,
    SEND_STOPPED,
    _monotonic,
    displays_seen,
    forget_displays,
)
from uiprobe.errors import (
    DeviceNotFoundError,
    DumpError,
    LaunchFailedError,
    NotEmulatorError,
    ScreenshotFailedError,
    UiprobeError,
)
from uiprobe.guards import _WORD_REASONS, Policy, judge
from uiprobe.inputwindows import ROTATION, Unreadable
from uiprobe.model import Bounds
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from uiprobe.crawl import Action, SkippedElement
    from uiprobe.device import Device, FocusedWindow
    from uiprobe.inputwindows import InputWindow
    from uiprobe.model import Element, Screen
    from uiprobe.packageuid import PackageUid

__all__ = ["INTERRUPTED", "STOP_ERROR", "CrawlResult", "crawl"]

# The two stop reasons the driver adds to the engine's six.
INTERRUPTED: Final = "interrupted"
STOP_ERROR: Final = "error"


@dataclass(frozen=True, slots=True)
class CrawlResult:
    """How a crawl ended: the run folder, the stop reason, and the counts.

    ``screens`` is the number of distinct screens of the target app that were
    seen, ``actions`` the number of actions whose start line was written (the
    last one may not have reached the device if the run ended right then).
    ``not_sent`` counts the actions closed as not sent, by reason; ``repins``
    the moves of the pinned transport in this run and ``repin`` whether moves
    were allowed (the boot id could be read); ``cover_unknown`` what the
    first ``cover_unknown`` refusal could not read and ``cover_rule`` the
    rule of the window list reader that made it (None when the reader did
    not refuse it, see `uiprobe.inputwindows.RULES`); ``last_screen_id`` the
    last screen that was read and ``last_foreground`` the app in front at the
    last read (None when unknown); ``last_focus`` the package that owned the
    window with input focus at the last observation (None when unknown).
    """

    out: Path
    stop_reason: str
    screens: int
    actions: int
    not_sent: tuple[tuple[str, int], ...] = ()
    repins: int = 0
    repin: bool = False
    cover_unknown: str | None = None
    last_screen_id: str | None = None
    last_foreground: str | None = None
    cover_rule: str | None = None
    last_focus: str | None = None


def _check_flag(name: str, value: object) -> None:
    if not isinstance(value, bool):
        msg = f"{name} must be a bool, not {type(value).__name__}"
        raise TypeError(msg)


def _check_callable(name: str, value: object) -> None:
    if not callable(value):
        msg = f"{name} must be callable, not {type(value).__name__}"
        raise TypeError(msg)


def _check_device(device: Device, *, allow_real_device: bool) -> None:
    """Refuse a device that is not an emulator, unless a real one is allowed."""
    if allow_real_device:
        return
    reason = "not_emulator"
    cause: str | None = None
    try:
        if device.is_emulator():
            return
    except UiprobeError as error:
        if isinstance(error, DeviceNotFoundError) and error.details.get("reason") in (
            "device_changed",
            PIN_WAIT_STOPPED,
        ):
            raise  # the pin itself failed: that is the error, not the check
        reason = "unreadable"
        cause = error.code
    detail = (
        "the device is not an emulator"
        if reason == "not_emulator"
        else "the emulator check could not be read, so it counts as a real device"
    )
    msg = (
        f"{detail}; a pressing crawl runs on emulators and test devices, "
        "pass allow_real_device=True to use this one"
    )
    details: dict[str, object] = {"serial": device.serial, "reason": reason}
    if cause is not None:
        details["error"] = cause
    raise NotEmulatorError(msg, details=details)


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


class _Ended(Exception):  # noqa: N818 - a signal inside the driver, not an error
    """The run ends before a screen is read: `stop` returned True after the
    pin check of an observation (``interrupted``), or uiprobe's own clock
    passed the run's time (``max_duration``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# The requests that change the device, each passing `_Run.final_check` right
# before it is sent: a tap, BACK, the force-stop of a relaunch (and of the
# first launch, by default), the first launch's start without a force-stop
# and the start after a force-stop (a relaunch's, and the first launch's by
# default).
_SEND_PATHS: Final = ("tap", "back", "force-stop", "launch", "start")
# The default clock of `crawl`: uiprobe's own (`time.monotonic`).
_OWN_CLOCK: Final = time.monotonic
_MAX_SECONDS: Final = 2**40


def _seconds(value: object) -> float:
    """A clock value turned into a built-in float at once, when it is received.

    Only the number is kept, so nothing of the caller's object (a method of a
    subclass, a finalizer) can run later.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"clock() must return a number of seconds, not {type(value).__name__}"
        raise TypeError(msg)
    try:
        seconds = float(value)
    except OverflowError:
        seconds = float("inf")  # refused below, as any value out of range
    if type(seconds) is not float:
        msg = "clock() must return a number of seconds"
        raise TypeError(msg)
    if not 0 <= seconds < _MAX_SECONDS:  # also false for NaN
        msg = "clock() must return at least 0 and below 2**40 seconds"
        raise ValueError(msg)
    return seconds


def _words(name: str, value: object) -> tuple[str, ...]:
    """A tuple of plain str read once from a policy field."""
    if not isinstance(value, tuple | list):
        msg = f"policy.{name} must be a tuple of str"
        raise TypeError(msg)
    words = tuple(value)
    if not all(isinstance(word, str) for word in words):
        msg = f"policy.{name} must hold only str"
        raise TypeError(msg)
    return tuple(str.__str__(word) for word in words)


def _ints(name: str, value: object) -> tuple[int, ...]:
    """A tuple of plain int read once from a policy field."""
    if not isinstance(value, tuple | list):
        msg = f"policy.{name} must be a tuple of int"
        raise TypeError(msg)
    numbers = tuple(value)
    if any(isinstance(n, bool) or not isinstance(n, int) for n in numbers):
        msg = f"policy.{name} must hold only int"
        raise TypeError(msg)
    return tuple(int.__int__(n) for n in numbers)


def _bounds(value: object) -> Bounds:
    """A fresh `Bounds` of built-in ints, its four sides read once."""
    if not isinstance(value, Bounds):
        msg = f"policy.avoid must hold Bounds, not {type(value).__name__}"
        raise TypeError(msg)
    sides = _ints("avoid", (value.left, value.top, value.right, value.bottom))
    return Bounds(*sides)


def _flag(name: str, value: object) -> bool:
    if not isinstance(value, bool):  # bool cannot be subclassed
        msg = f"policy.{name} must be a bool"
        raise TypeError(msg)
    return value


def _plain_policy(policy: Policy) -> Policy:
    """A `Policy` of the built-in class holding only plain values, read once.

    Every field is read here, at the start, so no accessor of a subclass and
    no method of a value the caller made runs while the crawl decides.
    """
    avoid = policy.avoid
    edges = policy.edges
    min_visible = policy.min_visible_px
    if isinstance(min_visible, bool) or not isinstance(min_visible, int):
        msg = "policy.min_visible_px must be an int"
        raise TypeError(msg)
    if avoid is not None and not isinstance(avoid, tuple | list):
        msg = "policy.avoid must be a tuple of bounds or None"
        raise TypeError(msg)
    bounds = None if avoid is None else tuple(_bounds(b) for b in tuple(avoid))
    return Policy(
        deny_words=_words("deny_words", policy.deny_words),
        confirm_words=_words("confirm_words", policy.confirm_words),
        tap_unlabeled=_flag("tap_unlabeled", policy.tap_unlabeled),
        avoid=bounds,
        skip_number_only=_flag("skip_number_only", policy.skip_number_only),
        skip_toggles=_flag("skip_toggles", policy.skip_toggles),
        refuse_dialog_confirm=_flag(
            "refuse_dialog_confirm", policy.refuse_dialog_confirm
        ),
        edges=None if edges is None else _ints("edges", edges),  # ty: ignore[invalid-argument-type]
        min_visible_px=int.__int__(min_visible),
        irreversible_phrases=_words(
            "irreversible_phrases", policy.irreversible_phrases
        ),
        skip_irreversible=_flag("skip_irreversible", policy.skip_irreversible),
        back_on_other_screens=_flag(
            "back_on_other_screens", policy.back_on_other_screens
        ),
        check_tap_window=_flag("check_tap_window", policy.check_tap_window),
        deny_exceptions=_words("deny_exceptions", policy.deny_exceptions),
        skip_two_target_rows=_flag("skip_two_target_rows", policy.skip_two_target_rows),
        skip_rows_with_buttons=_flag(
            "skip_rows_with_buttons", policy.skip_rows_with_buttons
        ),
        skip_navigate_up=_flag("skip_navigate_up", policy.skip_navigate_up),
        skip_sliders=_flag("skip_sliders", policy.skip_sliders),
    )


@dataclass(frozen=True, slots=True)
class _Seen:
    """What the device showed: the observation plus the focused window."""

    observation: Observation
    focus: FocusedWindow | None


def _app_window(package: str, window: InputWindow, app: PackageUid) -> bool:
    """Whether `window` belongs to the app (see `cover`): its owner uid
    is the app's, and its title names the app unless the app has a uid
    of its own (not shared, 10000 or more)."""
    uid, shared = app.uid, app.shared
    named = window.title == package or window.title.startswith(package + "/")
    return window.owner_uid == uid and (named or (not shared and uid >= _FIRST_APP_UID))


class _Run:
    """The state of one crawl: the engine, the writer and what is still open."""

    def __init__(
        self,
        device: Device,
        package: str,
        writer: RunWriter,
        engine: CrawlEngine,
        policy: Policy,
        *,
        emulator: bool,
        app_uid: PackageUid | None = None,
        screenshots: bool,
        settle: float,
        deadline: float,
        stop: Callable[[], bool] | None,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        send_limit: float | None = None,
        own_clock: bool = False,
        restart: bool = False,
    ) -> None:
        self.device = device
        self.package = package
        self.writer = writer
        self.engine = engine
        self.policy = policy
        self.emulator = emulator
        self.app_uid = app_uid  # read once at the start (check_tap_window)
        # Whether the first launch force-stops the app before it starts it.
        self.restart = restart
        # The launcher activity read in the gate of the start about to go
        # out (the first launch or a relaunch), used by that start only.
        self.start_component: str | None = None
        self.gate_details: dict[str, str | int] | None = None  # why, for cover
        # Why the focus counted as unknown, when not because it was unreadable.
        self.gate_focus_details: dict[str, str | int] | None = None
        self.not_sent: dict[str, int] = {}  # not_sent end lines by reason
        self.cover_unknown: str | None = None  # what the first cover_unknown missed
        self.cover_rule: str | None = None  # and the reader's rule, when it had one
        self.last_screen_id: str | None = None  # the last screen read
        self.last_foreground: str | None = None  # the app in front at the last read
        self.gate_screen_id: str | None = None  # the id of the gate's own dump
        self.screenshots = screenshots
        self.settle = settle
        self.deadline = deadline
        self.stop = stop
        self.clock = clock
        self.sleep = sleep
        self.notes: list[str] = []
        self.screens_seen: set[str] = set()  # target screens, when observed
        self.focus: FocusedWindow | None = None  # at the last observation
        self._stop_seen = False  # stop() once returned True: it stays so
        self._next_id = 0  # the action id of the next start line
        self._open: int | None = None  # sent action that has no end line yet
        # An action whose start line is written and that is in the gate: not
        # sent yet, so a run that ends there can close it as not sent.
        self.unsent: int | None = None
        # A refused tap and the screen it was chosen on, until the engine
        # releases it (see `run`).
        self.held: tuple[Tap, Screen | None] | None = None
        self.withdrawn = False  # the last action was taken back by the engine
        # Device.pin_epoch when the current observation began its reads, and
        # after the gate's own pin check: an input goes out only when nothing
        # happened to the pin since the observation it was chosen on.
        self.epoch_seen = device.pin_epoch
        self.gate_epoch = device.pin_epoch
        self.sent_epoch = device.pin_epoch  # when the last action was sent
        # The run's end as a value of uiprobe's own clock, set once at the
        # start; later values can only come earlier (`now`).
        self.send_limit = (
            _monotonic() + 3600.0 * 24 if send_limit is None else send_limit
        )
        # The time limit as a value of uiprobe's own clock (`now`), by
        # which the next input, stop or start must go out.
        self.send_by: float = self.send_limit
        # Whether `clock` is uiprobe's own (the default): then it is read
        # once per check and the end set at the start stays as it is.
        self.own_clock = own_clock
        # The pin epoch the last `final_check` approved; the device compares
        # it again right before the request is written.
        self.approved: int = device.pin_epoch
        self.repins_before = len(device.repins)  # moves made before this run
        # Why a wait for the device was stopped (a not_sent reason), once known.
        self.wait_stop: str | None = None
        self._skips_written = 0

    # -- the loop --

    def run(self) -> str:
        """Run until a stop reason is known; return it."""
        try:
            return self.run_loop()
        except _Ended as ended:
            if self.unsent is not None:  # in the gate: not sent
                why = (
                    NOT_SENT_TIME_LIMIT
                    if ended.reason == MAX_DURATION
                    else NOT_SENT_STOPPED
                )
                self.end_not_sent(self.unsent, why)
                self.unsent = None
            return ended.reason

    def run_loop(self) -> str:
        """`run`, which a stop seen inside an observation ends."""
        reason = self.first_launch()
        if reason is not None:
            return reason
        engine = self.engine
        observation = self.next_observation()
        while observation is not None:
            if self.stopped():
                return INTERRUPTED
            action = engine.next(observation)
            # A tap refused on reads an event crossed is released here: its
            # rows name elements of the screen it was chosen on.
            held, self.held = self.held, None
            if held is None:
                self.write_skips(observation.screen)
            else:
                self.write_skips(observation.screen, held[1], tap=held[0])
            if isinstance(action, Stop):
                return action.reason
            if isinstance(action, Reread):
                observation = self.observe()
                continue
            refused, fresh = self.send(action, observation)
            ending = self.ending(action, refused)
            if ending is not None:
                return ending
            # A tap the gate did not send: the screen it re-read goes to the
            # engine, which releases the unreported tap.
            observation = fresh if fresh is not None else self.next_observation()
        return INTERRUPTED

    def first_launch(self) -> str | None:
        """Send the first launch; the stop reason when the run ends there."""
        if self.stopped():
            return INTERRUPTED
        refused, _ = self.send(self.engine.start(), None)
        if refused == NOT_SENT_RECONNECTED:
            self.notes.append(f"launch was not sent: {refused}")
            return STOP_NOT_SENT
        if refused == _HALF_RELAUNCH:
            return STOP_NOT_SENT  # the summary already says what happened
        if refused is not None:  # otherwise only stop() or the time limit
            return INTERRUPTED if self._stop_seen else MAX_DURATION
        return None

    def next_observation(self) -> Observation | None:
        """Ask `stop` again, then read the screen; None when `stop` said so
        (after an action, before any read of the screen)."""
        if self.stopped():
            return None
        return self.observe()

    def send_input(self, action: Action, action_id: int) -> str | None:
        """Send a tap, BACK or the first launch; return why nothing was sent,
        or None when it was sent.

        ``device_reconnected``: the device has to be proven again first (the
        action is closed as the gate would have closed it, and taken back
        unless it is the first launch). ``time_limit``: the time limit had
        passed when the request was about to go out (checked by the device
        with uiprobe's own clock, see `now`);
        the action is closed as not sent and the run ends.
        """
        try:
            if isinstance(action, Tap):
                self.device.tap(
                    action.point, _send_by=self.send_by, _epoch=self.approved
                )
            elif isinstance(action, Back):
                self.device.key("back", _send_by=self.send_by, _epoch=self.approved)
            else:
                self.launch()
        except DeviceNotFoundError as error:
            reason = error.details.get("reason")
            if reason == SEND_STOPPED:
                self._open = None
                why = str(error.details.get("why", NOT_SENT_TIME_LIMIT))
                self.end_not_sent(action_id, why)
                return why
            if reason != PIN_NOT_PROVEN:
                raise
            self._open = None
            self.end_not_sent(action_id, NOT_SENT_RECONNECTED)
            if not isinstance(action, Launch):
                self.withdraw(action)
            return NOT_SENT_RECONNECTED
        return None

    def end_failed(self, action_id: int, error: Exception) -> None:
        """Close a launch or relaunch whose sending failed and ends the run.

        The end line has ``failed``, the reason and ``outcome: unknown``: the
        device may have acted on what was sent. A start whose reply did not
        come in time also gets a note in the summary.
        """
        self._open = None
        if isinstance(error, LaunchFailedError):
            reason = str(error.details.get("reason", error.code))
        elif isinstance(error, UiprobeError):
            reason = error.code
        else:
            reason = "internal_error"
        if reason == "start_timeout":
            seconds = (
                error.details.get("timeout")
                if isinstance(error, UiprobeError)
                else None
            )
            self.notes.append(
                f"the reply of am start -W for {self.package} did not come within"
                f" {seconds} seconds; whether the app started is unknown, so the"
                " run ended"
            )
        with contextlib.suppress(RunFolderError):
            self.writer.write_transition_end(action_id, failed=reason)

    def end_not_sent(
        self,
        action_id: int,
        reason: str,
        details: dict[str, str | int] | None = None,
    ) -> None:
        """Write a not_sent end line and count it for the result."""
        self.writer.write_transition_end(action_id, not_sent=reason, details=details)
        self.not_sent[reason] = self.not_sent.get(reason, 0) + 1
        if reason == NOT_SENT_COVER_UNKNOWN and details and self.cover_unknown is None:
            self.cover_unknown = str(details.get("unreadable", ""))
            rule = details.get("rule")
            self.cover_rule = None if rule is None else str(rule)

    def withdraw(self, action: Action) -> None:
        """Have the engine take back `action`, which was not sent."""
        self.engine.withdraw(action)
        self.withdrawn = True
        # Skip rows written for the undone call stay written; the writer
        # drops repeats when the engine reports them again.
        self._skips_written = min(self._skips_written, len(self.engine.records.skipped))

    def ending(self, action: Action, refused: str | None) -> str | None:
        """The stop reason when the run ends after `action`, or None to go on.

        Asked to stop or out of time (also when stop() said so in the pause
        after the input, whose start line then has no end line: its result is
        unknown, as the run folder page says): end now, without another
        request. A BACK or relaunch the gate held back: the engine already
        counted it, so rather than go on from a wrong count the run ends
        (alpha: the safe side).
        """
        if self.withdrawn:
            # Taken back by the engine: the crawl goes on from a fresh read.
            self.withdrawn = False
            return INTERRUPTED if self._stop_seen else None
        if refused == _HALF_RELAUNCH:
            return STOP_NOT_SENT  # the summary already says what happened
        if refused in (NOT_SENT_STOPPED, NOT_SENT_TIME_LIMIT, NOT_SENT_START):
            return INTERRUPTED if self._stop_seen else MAX_DURATION
        if refused is not None and isinstance(action, Back | Relaunch):
            self.notes.append(f"{_kind(action)} was not sent: {refused}")
            return STOP_NOT_SENT
        return INTERRUPTED if self._stop_seen else None

    def waiting_stopped(self) -> bool:
        """Whether a wait for the device should end: the time limit, or a
        `stop` that already returned True before the wait. `stop` itself is
        not called while the device is being proven (it may read the device,
        which is refused then); it is asked again after the wait. The first
        reason is kept for the ending."""
        if self.wait_stop is None:
            if self._stop_seen:
                self.wait_stop = NOT_SENT_STOPPED
            elif self.now() >= self.deadline:
                self.wait_stop = NOT_SENT_TIME_LIMIT
        return self.wait_stop is not None

    def now(self) -> float:
        """The run's clock, read once and kept as a built-in float.

        With uiprobe's own clock (the default) it is read once, and the run's
        end by it (`send_by`, which the device checks right before a request
        that changes it goes out) is the one set at the start. With a clock
        of the caller's, uiprobe's own clock is read first, and the time the
        caller's clock says is left, counted from that moment, can bring
        `send_by` earlier, never later.
        """
        if self.own_clock:
            return _monotonic()
        before = _monotonic()
        now = _seconds(self.clock())
        self.send_by = min(self.send_by, before + (self.deadline - now))
        return now

    def past(self) -> bool:
        """Whether the run's end by uiprobe's own clock has come."""
        return _monotonic() >= self.send_by

    def check_time(self) -> None:
        """End the run (``max_duration``) once its end by uiprobe's own clock
        has come (asked when an observation begins)."""
        if self.past():
            raise _Ended(MAX_DURATION)

    def final_check(self, path: str, approved: int) -> bool:
        """The last comparison before a request that changes the device.

        True when nothing happened to the pin since `approved` (the epoch the
        reads before it began at). The value is kept and handed to the device
        with the request, which compares it once more on the request's own
        connection. No code of the caller runs from here to the request.
        """
        if path not in _SEND_PATHS:
            msg = f"not a sending path: {path!r}"
            raise ValueError(msg)
        if not self.epoch_is(approved):
            return False
        self.approved = approved
        return True

    def epoch_is(self, approved: int) -> bool:
        """The comparison of `final_check` itself: the pin epoch is still
        `approved`."""
        return self.device.pin_epoch == approved

    def stopped(self) -> bool:
        """Whether `stop` asked to end; once it did, the answer stays True."""
        if not self._stop_seen and self.stop is not None and bool(self.stop()):
            self._stop_seen = True
        return self._stop_seen

    def count(self) -> tuple[int, int]:
        """The target screens observed and the start lines on disk."""
        return len(self.screens_seen), self.writer.starts_written

    # -- sending --

    def send(
        self, action: Action, observation: Observation | None
    ) -> tuple[str | None, Observation | None]:
        """Write the start line, pass the gate, then send the action once.

        Returns the reason the gate gave when it refused (None when the action
        was sent) and, for a refused tap, the observation it re-read.
        """
        self._next_id += 1
        action_id = self._next_id
        screen = None if observation is None else observation.screen
        self.write_start(action_id, action, observation)
        self.unsent = action_id
        reason, seen = self.gate(action, observation)
        self.unsent = None
        if seen is not None and reason != NOT_SENT_RECONNECTED:
            # A read the pin check kept: it names the last screen and app
            # (the id was computed in the gate, before its last comparison,
            # so no work of this size runs between it and the input).
            if self.gate_screen_id is not None:
                self.last_screen_id = self.gate_screen_id
            self.last_foreground = seen.observation.foreground
        if reason is not None:
            details = (
                self.gate_details
                if reason in _COVER_REASONS
                else self.gate_focus_details
                if reason == NOT_SENT_FOCUS_UNKNOWN
                else None
            )
            self.end_not_sent(action_id, reason, details)
            if reason == NOT_SENT_RECONNECTED and not isinstance(action, Launch):
                # Nothing was sent and nothing about the screen is known: the
                # engine takes the action back and decides again on a fresh
                # read.
                self.withdraw(action)
                return reason, None
            if seen is not None and isinstance(action, Tap):
                return reason, self.refused_tap(action, screen, seen)
            return reason, None
        # Nothing of the caller's runs between the gate's last comparison and
        # the input; a start uses the launcher activity its gate read.
        self._open = action_id
        self.sent_epoch = self.device.pin_epoch
        try:
            if isinstance(action, Relaunch) or (
                isinstance(action, Launch) and self.restart
            ):
                return self.relaunch(action_id, action), None
            refused = self.send_input(action, action_id)
        except Exception as error:
            # A launch or relaunch that failed ends the run; its end line says
            # so (a tap or BACK keeps a start line alone: result unknown).
            if isinstance(action, Launch | Relaunch) and self._open == action_id:
                self.end_failed(action_id, error)
            raise
        if refused is not None:
            return refused, None
        if isinstance(action, Tap):
            self.engine.tap_sent(action)
            if seen is not None:
                self.write_skips(screen, seen.observation.screen, tap=action)
        if isinstance(action, Tap | Back):
            self.pause()
        return None, None

    def write_start(
        self, action_id: int, action: Action, observation: Observation | None
    ) -> None:
        """Write the start line of `action`, before its gate."""
        screen = None if observation is None else observation.screen
        from_id = None if screen is None else screen.screen_id
        element: Element | None = None
        point: tuple[int, int] | None = None
        other: str | None = None  # the package of a screen of another app
        foreground: str | None = None
        focus: str | None = None
        if (
            isinstance(action, Back | Relaunch)
            and observation is not None
            and screen is not None
            and not self.on_target(screen, observation.foreground)
        ):
            # Decided on a screen of another app: say whose it was, so the run
            # folder tells where a BACK went or why the app was restarted.
            # The gate sends a BACK only while the focused window is the one
            # read here, so its owner is where that BACK goes.
            other = screen.package
            foreground = observation.foreground
            focus = None if self.focus is None else self.focus.owner
        if isinstance(action, Tap):
            from_id = action.screen_id
            point = action.point
            element = _element_of(screen, action.element_id)
        elif isinstance(action, Launch):
            from_id = None
        elif not isinstance(action, Back | Relaunch):  # Stop and Reread
            msg = f"not an input: {action!r}"
            raise TypeError(msg)
        self.writer.write_transition_start(
            action_id,
            _kind(action),
            from_id,
            element,
            point,
            off_target_package=other,
            off_target_foreground=foreground,
            off_target_focus=focus,
            restart=isinstance(action, Launch) and self.restart,
        )

    def cover(
        self,
        point: tuple[int, int],
        focus: tuple[FocusedWindow | None, ...],
        screen: Screen | None,
    ) -> tuple[str, dict[str, str | int]] | None:
        """Why a tap at `point` must not go out, from the window there, or
        None when the window that takes it belongs to the app.

        `focus` is the focused window as read before and after the dump: a
        user other than 0 refuses (``target_user``), and so does an owner uid
        (``mOwnerUid=``) other than the app's (``target_uid``).
        `screen` is the gate's own dump: a hierarchy rotated by the dump's
        ``rotation`` attribute refuses (``rotation``), even when the input
        reader's Viewport lines say 0; a dump that could not be read leaves
        this to the other checks (the engine's re-check refuses it).
        The app's uid is read again right before the window (the app may
        have been installed again under another uid): it must be readable
        and the same as at the start (``target_uid``).

        The window's owner uid must be the app's app id exactly, so only
        Android user 0 passes (another user's uid is user * 100000 + app id).
        When the app shares its uid with other packages (``sharedUser``) or
        has a uid below 10000 (a system uid such as Settings' 1000, which
        system windows have), the window's title must also name the app: be
        the package or begin with the package and ``/`` (as an activity's
        window does); a window of the app with another title is refused.
        The same test tells the reader which windows may print
        ``samsungFlags=0x1`` (`uiprobe.inputwindows.read_window_at`).
        """
        app = self.app_uid
        unknown = "target_uid" if app is None else self.uncovered(app, focus, screen)
        if app is None or unknown is not None:
            return NOT_SENT_COVER_UNKNOWN, {"unreadable": unknown or "target_uid"}

        def owned(window: InputWindow) -> bool:
            return self.app_window(window, app)

        found = self.device.window_at(point, owned=owned)
        if isinstance(found, Unreadable):  # the reason, and the check that refused
            return NOT_SENT_COVER_UNKNOWN, {
                "unreadable": str(found),
                "rule": found.rule,
            }
        if isinstance(found, str):
            return NOT_SENT_COVER_UNKNOWN, {"unreadable": found}
        if self.app_window(found, app):
            return None
        return NOT_SENT_COVERED, {"window": found.title, "owner_uid": found.owner_uid}

    def app_window(self, window: InputWindow, app: PackageUid) -> bool:
        return _app_window(self.package, window, app)

    def uncovered(
        self,
        app: PackageUid,
        focus: tuple[FocusedWindow | None, ...],
        screen: Screen | None,
    ) -> str | None:
        """What makes the window at a tap's point not worth reading (see
        `cover`), checked in this order, or None.

        `focus` are the focused window as read before and after the dump:
        each one's user must be 0 (``target_user``) and its owner uid, when
        read, the app's uid (``target_uid``).
        """
        users = {f.user for f in focus if f is not None}
        owners = [f.owner_uid for f in focus if f is not None]
        if users - {0}:
            return "target_user"
        if any(uid is not None and uid != app.uid for uid in owners):
            return "target_uid"
        if screen is not None and screen.rotation != 0:
            return ROTATION
        if self.device.package_uid(self.package) != app:
            return "target_uid"
        return None

    def refused_tap(
        self, tap: Tap, screen: Screen | None, seen: _Seen
    ) -> Observation | None:
        """Record what a refused tap's gate read; return it as the next
        observation, or None to read the screen again.

        The gate's reads began after its own pin check. If something happened
        to the pin during them they are mixed: they are not recorded, and the
        screen is read again from scratch.
        """
        if self.device.pin_epoch != self.gate_epoch:
            # The skip rows come from the screen the tap was chosen on, now
            # and when the engine releases the tap.
            self.write_skips(screen, tap=tap)
            self.held = (tap, screen)
            return None
        self.write_skips(screen, seen.observation.screen, tap=tap)
        self.record(seen, self.gate_epoch)
        if self.device.pin_epoch != self.gate_epoch:
            return None  # the screenshot's reads saw an event
        self.epoch_seen = self.gate_epoch
        return seen.observation

    def gate(
        self, action: Action, observation: Observation | None
    ) -> tuple[str | None, _Seen | None]:
        """Decide whether `action` may be sent now; return (reason, what was read).

        The one place every action passes right before it is sent. First `stop`
        and the time limit, then that the pinned transport still leads to the
        checked device (an error if not), then `stop` again (the pin check can
        wait for the device), then a fresh read: for a tap the dump
        first and then foreground, focused window and keyboard, so the reads
        nearest the press come last. Anything that cannot be read, or that
        differs from what the action was chosen on, is a refusal. After the
        decision nothing else runs before the input is sent.
        """
        now = self.now()  # the one time taken, before any read
        reason = self.halt(now)
        if reason is not None:
            return reason, None
        self.device.check_pin(emulator=self.emulator)
        if isinstance(action, Launch):
            return (self.restart_gate() if self.restart else self.launch_gate()), None
        # Asked again after the pin check, which can block for long (a wait
        # for the device); after it only the reads run before the input.
        if self.stopped():
            return NOT_SENT_STOPPED, None
        self.gate_epoch = self.device.pin_epoch
        tap = isinstance(action, Tap)
        # For a tap: the focused window before and after the dump must be the
        # same window of the app, so the dump shows that window (uiautomator
        # dumps the focused window: seen on API 24, 30 and 36 with the shade).
        before = self.device.focused_window() if tap else None
        screen = self.dump() if tap else None
        foreground = self.device.foreground()
        focus = self.device.focused_window()
        keyboard = self.device.keyboard_shown()
        self.gate_details = None
        self.gate_focus_details = None
        # With several displays the focus reading is best effort against
        # window titles that forge lines, so the check of the window at the
        # tap point is the guard: without it, a tap or BACK is not sent. Any
        # read of this device in the run that showed several displays counts
        # (the observation's too), and this reason comes before the others.
        guard = isinstance(action, Tap | Back) and not self.policy.check_tap_window
        unchecked = guard and displays_seen(self.device)
        if unchecked:
            before = focus = None
            self.gate_focus_details = {"cause": FOCUS_CAUSE_SEVERAL_DISPLAYS}
        seen = _Seen(Observation(screen, foreground, keyboard, now), focus)
        if isinstance(action, Tap):
            # The last read: the window that takes a touch at the point.
            cover = (
                self.cover(action.point, (before, focus), screen)
                if self.policy.check_tap_window
                else None
            )
            allowed = self.engine.recheck(action, seen.observation)
            owners = {None if f is None else f.owner for f in (before, focus)}
            checks = (
                (unchecked, NOT_SENT_FOCUS_UNKNOWN),
                (keyboard is None, NOT_SENT_KEYBOARD_UNKNOWN),
                (keyboard is True, NOT_SENT_KEYBOARD_SHOWN),
                (before is None or focus is None, NOT_SENT_FOCUS_UNKNOWN),
                (owners != {self.package}, NOT_SENT_FOCUS_OTHER),
                (before != focus, NOT_SENT_FOCUS_CHANGED),
                (
                    cover is not None,
                    NOT_SENT_COVER_UNKNOWN if cover is None else cover[0],
                ),
                (not allowed, NOT_SENT_REFUSED),
            )
            if cover is not None:
                self.gate_details = cover[1]
        else:
            before = observation
            back = isinstance(action, Back)
            # A BACK goes to the window with input focus. One decided on a
            # screen of the app (the keyboard BACK, a navigation BACK) is sent
            # only while the app owns that window; one decided on a screen of
            # another app exists only with Policy.back_on_other_screens.
            own_back = (
                back
                and before is not None
                and before.screen is not None
                and self.on_target(before.screen, before.foreground)
            )
            checks = (
                (unchecked, NOT_SENT_FOCUS_UNKNOWN),
                (before is None, NOT_SENT_CHANGED),  # not reached: a launch only
                (foreground is None, NOT_SENT_FOREGROUND_UNKNOWN),
                (focus is None, NOT_SENT_FOCUS_UNKNOWN),
                (
                    own_back and focus is not None and focus.owner != self.package,
                    NOT_SENT_FOCUS_OTHER,
                ),
                (back and keyboard is None, NOT_SENT_KEYBOARD_UNKNOWN),
                (
                    before is not None
                    and (
                        foreground != before.foreground
                        or focus != self.focus
                        or (back and keyboard != before.keyboard_shown)
                    ),
                    NOT_SENT_CHANGED,
                ),
            )
        # The engine re-checks with the same time the gate took first, which was
        # before the limit, so its refusal is never about the time.
        # Last, after every other check: if anything happened to the pin since
        # the observation the action was chosen on (or during these reads),
        # nothing read before can be trusted.
        # Every condition is evaluated to a reason first (the truth tests run
        # here), then nothing but one integer comparison follows.
        why = next((reason for failed, reason in checks if bool(failed)), None)
        self.gate_screen_id = None if screen is None else screen.screen_id
        # The time again, after the reads (they can take long) and before the
        # last comparison, as on main: the clock, which may be the caller's
        # code, runs before the comparison and nothing runs after it. The
        # device also checks the time with uiprobe's own clock right before
        # the input goes out.
        late = self.now() >= self.deadline
        # uiprobe's own check once more, after the caller's clock (which may
        # have read the device and shown several displays first) and before
        # the final comparison; its cause wins over the reasons found so far
        # (a changed pin or the time limit below still come first).
        if guard and displays_seen(self.device):
            self.gate_focus_details = {"cause": FOCUS_CAUSE_SEVERAL_DISPLAYS}
            why = NOT_SENT_FOCUS_UNKNOWN
        path = (
            "tap"
            if isinstance(action, Tap)
            else "back"
            if isinstance(action, Back)
            else "force-stop"
        )
        if not self.final_check(path, self.epoch_seen):
            return NOT_SENT_RECONNECTED, seen
        if late:
            return NOT_SENT_TIME_LIMIT, None
        return why, seen

    def launch_gate(self) -> str | None:
        """The end of the first launch's gate, after the pin check: `stop`
        again (the check can take long), the launcher read, the time again
        (the read can take long too), then the last comparison."""
        epoch = self.device.pin_epoch
        stopped = self.stopped()  # asked again after the pin check
        read = stopped or self.read_start_component(None)
        now = self.now()
        late = now >= self.deadline or self.past()
        if not read or not self.final_check("launch", epoch):
            return NOT_SENT_RECONNECTED
        if stopped:
            return NOT_SENT_STOPPED
        if late:
            return NOT_SENT_TIME_LIMIT
        return None

    def restart_gate(self) -> str | None:
        """The end of the gate of the first launch's force-stop, after the
        pin check: `stop` again (the check can take long), the launcher read,
        the time again (the read can take long too), then the last
        comparison, as in `launch_gate`.

        It is the gate of a relaunch's force-stop without the reads of the
        screen, which compare it with the observation it was chosen on: the
        first launch has none. The launcher activity is read here so that an
        app whose launcher cannot be read is not force-stopped: nothing is
        sent, and the run ends as a first launch without the force-stop does
        (`read_start_component`). The start does not use this read: the
        launcher activity is read again after the force-stop, right before
        the start, as for a relaunch.
        """
        epoch = self.device.pin_epoch
        stopped = self.stopped()  # asked again after the pin check
        read = stopped or self.read_start_component(None)
        self.start_component = None  # the start uses the read after the stop
        now = self.now()
        late = now >= self.deadline or self.past()
        if not read or not self.final_check("force-stop", epoch):
            return NOT_SENT_RECONNECTED
        if stopped:
            return NOT_SENT_STOPPED
        if late:
            return NOT_SENT_TIME_LIMIT
        return None

    def read_start_component(self, relaunch_id: int | None) -> bool:
        """Read the launcher activity for the start about to go out; False
        when the device had to be proven again during the read.

        When it cannot be read, the start is not sent: the action is closed
        (a first launch as not sent, a relaunch with `relaunch_id` as
        ``start_not_sent``) and `LaunchFailedError` (``launcher_unreadable``)
        ends the run. Any other error of uiprobe or of the device in the read
        of a first launch (`relaunch_id` None; an adb connection lost, a
        changed device) also closes it as not sent, with the error's word
        (`_failure_word`, ``adb_error`` for a lost connection), before it is
        raised: nothing was sent. In a relaunch, `relaunch` closes it.
        A wait for the device that was stopped (``pin_wait_stopped``) is
        closed by the driver's ending, with the reason that stopped it. When
        writing that end line fails (`RunFolderError`), the error of the read
        is the one raised.
        """
        self.start_component = None
        try:
            read = self.device._start_component  # noqa: SLF001 - internal to uiprobe
            self.start_component = read(self.package)
        except DeviceNotFoundError as error:
            reason = error.details.get("reason")
            if reason == PIN_NOT_PROVEN:
                return False
            if reason != PIN_WAIT_STOPPED and relaunch_id is None:
                self.close_unsent(_failure_word(error))
            raise
        except LaunchFailedError as error:
            if _unreadable_launcher(error):
                if relaunch_id is not None:
                    with contextlib.suppress(RunFolderError):
                        self.half_sent(relaunch_id, "launcher_unreadable")
                else:
                    self.close_unsent("launcher_unreadable")
            elif relaunch_id is None:
                self.close_unsent(_failure_word(error))
            raise
        except UiprobeError as error:
            if relaunch_id is None:
                self.close_unsent(_failure_word(error))
            raise
        return True

    def close_unsent(self, reason: str) -> None:
        """Close the action in the gate (if any) as not sent for `reason`.

        Called only while an error is being raised: a failure to write the
        end line (`RunFolderError`) is not raised, so that error stays the
        one raised.
        """
        if self.unsent is not None:
            action_id, self.unsent = self.unsent, None
            with contextlib.suppress(RunFolderError):
                self.end_not_sent(action_id, reason)

    def halt(self, now: float | None = None) -> str | None:
        """The reason not to send that does not depend on the screen, if any."""
        if self.stopped():
            return NOT_SENT_STOPPED
        if (self.now() if now is None else now) >= self.deadline:
            return NOT_SENT_TIME_LIMIT
        return None

    def pause(self) -> None:
        """Wait `settle` seconds, but not past the time limit or a stop.

        The number of steps is bounded, so a clock that does not move cannot
        keep the pause going.
        """
        end = min(self.now() + self.settle, self.deadline)
        for _ in range(int(self.settle / PAUSE_STEP) + 2):
            left = end - self.now()
            if left <= 0 or self.stopped():
                return
            self.sleep(min(PAUSE_STEP, left))

    def launch(self) -> None:
        """Start the app; an app that is not in front afterwards is not fatal.

        It starts the launcher activity its gate read (before the gate's last
        clock read and comparison; no component is kept between starts) with
        ``am start -W`` and waits for the app to come to the front. No code of
        the caller runs in that call; the start goes out only while the pin
        epoch is still the one the gate approved and the time limit has not
        passed.
        ``not_in_front`` means the start reported success, but
        another package (a permission dialog, a splash screen) holds the
        focus; ``other_activity`` means the start reported success with an
        activity of another app (on API 36, Settings' search screen of
        another package stays on top of its task after a force-stop) and the
        app was not in front after the same wait either. The
        engine handles an off-target screen, so the crawl observes and goes
        on. The action stays counted as sent. Any other launch failure,
        ``start_timeout`` included (whether the app started is unknown), is
        raised and ends the run.
        """
        component, self.start_component = self.start_component, None
        if component is None:  # every gate of a start reads it first
            msg = "no launcher activity was read for this start"
            raise RuntimeError(msg)
        try:
            self.device._launch_component(  # noqa: SLF001 - internal to uiprobe
                self.package, component, _send_by=self.send_by, _epoch=self.approved
            )
        except LaunchFailedError as error:
            reason = error.details.get("reason")
            if reason == "other_activity":
                # The start went to an activity of another app (one that was
                # on top of the app's task): that app is in front.
                seen = str(error.details.get("activity", "")).partition("/")[0]
            elif reason == "not_in_front":
                seen = error.details.get("foreground")
            else:
                raise
            self.notes.append(f"{self.package} was not in front after launch ({seen})")

    def relaunch(self, action_id: int, action: Launch | Relaunch) -> str | None:
        """Force-stop the app, then, unless told to stop meanwhile, start it.

        A relaunch, and the first launch when `restart` is set. The two
        commands are sent one after the other, and `stop` and the time
        limit are looked at again between them. When they end the run there,
        only the force-stop was sent: the start line gets a ``not_sent`` end
        (``start_not_sent``) and the summary says so. Returns that reason, or
        None when the app was started.
        """
        unsent = self.relaunch_stop(action_id, action)
        if unsent is not None:
            return unsent
        try:
            reason = self.relaunch_halt(action_id)
            # The last comparison before the start, inside the region: nothing
            # runs after it but leaving the region and the tests below.
            stale = reason == NOT_SENT_RECONNECTED or (
                reason is None and not self.final_check("start", self.sent_epoch)
            )
        except BaseException as error:
            # The force-stop went out and nothing has sent the start: an
            # error (a changed device in the pin check, say) or an interrupt
            # here, up to and in the last comparison, leaves no doubt about
            # the start, so the action is closed as start_not_sent (not as
            # failed, or left open) before it is raised again. A step that
            # already closed it is not repeated. `launch` is outside.
            if self._open == action_id:
                with contextlib.suppress(RunFolderError):
                    self.half_sent(action_id, _failure_word(error))
            raise
        if stale:
            # The device had to be proven again after the force-stop: the
            # start is not sent on the earlier decision.
            return self.half_sent(action_id, NOT_SENT_RECONNECTED)
        if reason is not None:
            self.half_sent(action_id, reason)
            return NOT_SENT_START
        try:
            self.launch()
        except DeviceNotFoundError as error:
            why = error.details.get("reason")
            if why == SEND_STOPPED:
                # The time limit passed right before the start went out.
                self.half_sent(action_id, str(error.details.get("why", "")))
                return NOT_SENT_START
            if why != PIN_NOT_PROVEN:
                raise
            # The force-stop went out, the start did not.
            return self.half_sent(action_id, NOT_SENT_RECONNECTED)
        return None

    def relaunch_stop(self, action_id: int, action: Launch | Relaunch) -> str | None:
        """Send the force-stop of a relaunch or of the first launch; why it
        was not sent, or None. A relaunch not sent because the device had to
        be proven again is taken back by the engine; the first launch is not
        (the run ends, as for a first launch without the force-stop)."""
        try:
            self.device.stop_app(
                self.package, _send_by=self.send_by, _epoch=self.approved
            )
        except DeviceNotFoundError as error:
            reason = error.details.get("reason")
            if reason == SEND_STOPPED:
                self._open = None  # nothing was sent: the time limit had passed
                why = str(error.details.get("why", NOT_SENT_TIME_LIMIT))
                self.end_not_sent(action_id, why)
                return why
            if reason != PIN_NOT_PROVEN:
                raise
            self._open = None  # nothing was sent
            self.end_not_sent(action_id, NOT_SENT_RECONNECTED)
            if isinstance(action, Relaunch):
                self.withdraw(action)
            return NOT_SENT_RECONNECTED
        return None

    def relaunch_halt(self, action_id: int) -> str | None:
        """Between the force-stop and the start: `stop`, the time, the pin
        check, `stop`, the launcher read and the time again; why not to start,
        or None."""
        try:
            # stop() may read the device, so it is inside the handler too.
            reason = self.halt()
            if reason is None:
                # The device behind the pin is checked again before the start,
                # then stop() (the check can block for long) and the time,
                # then, in `relaunch`, the last comparison of the pin.
                self.device.check_pin(emulator=self.emulator)
                if self.stopped():
                    reason = NOT_SENT_STOPPED
                elif not self.read_start_component(action_id):
                    reason = NOT_SENT_RECONNECTED
                elif self.now() >= self.deadline or self.past():
                    reason = NOT_SENT_TIME_LIMIT
        except DeviceNotFoundError as error:
            if error.details.get("reason") != PIN_WAIT_STOPPED:
                raise
            # The time limit (or a stop seen before) ended a wait for the
            # device: the start was not sent, which the record says before
            # the run ends.
            self.half_sent(action_id, self.wait_stop or NOT_SENT_STOPPED)
            raise
        return reason

    def half_sent(self, action_id: int, why: str) -> str:
        """Close a relaunch, or the first launch, whose force-stop went out
        and whose start did not; return `_HALF_RELAUNCH`."""
        self._open = None
        self.end_not_sent(action_id, NOT_SENT_START)
        # The first launch is always the run's first action.
        what = (
            "at the start of the run but not started"
            if action_id == _FIRST_ACTION
            else "but not started again"
        )
        self.notes.append(f"{self.package} was force-stopped {what} ({why})")
        return _HALF_RELAUNCH

    def dump(self) -> Screen | None:
        try:
            screen: Screen | None = self.device.dump()
        except DumpError:
            return None
        if screen is not None and not _ids_unique(screen):
            # The engine counts a screen whose element ids are not unique as
            # a failed dump (judge refuses it), so it is recorded as one.
            return None
        return screen

    def observe(self) -> Observation:
        """Read the device, close the open transition, then record the screen.

        The pin is checked first: a dump writes a file on the device and
        removes it, so even reading goes only to the device that was checked.
        Reads that an event on the pin crossed are read again from the pin
        check on (bounded: every event makes the next check prove the device
        again, which `MAX_RECONNECTS` limits), before the screen is written
        or handed on. The open transition is closed as soon as one dump and
        foreground read are not crossed by an event (or the dump failed,
        which the engine counts toward its limit). A failed dump is handed
        on as it is, and `stop` or the time limit end the rereading.
        """
        # A step boundary: `stop` and the run's end are looked at when an
        # observation begins (as before each send); a read in progress then
        # runs until it finishes.
        if self.stopped():
            raise _Ended(INTERRUPTED)
        self.check_time()
        while True:
            self.device.check_pin(emulator=self.emulator)
            self.epoch_seen = self.device.pin_epoch
            screen = self.dump()
            foreground = self.device.foreground()
            if screen is None or self.device.pin_epoch == self.epoch_seen:
                self.close_transition(screen, foreground)
            keyboard = self.device.keyboard_shown()
            focus = self.device.focused_window()
            if (
                screen is None
                or self.device.pin_epoch == self.epoch_seen
                or self.halt() is not None
            ):
                break
        if screen is not None:
            self.last_screen_id = screen.screen_id
        self.last_foreground = foreground
        observation = Observation(screen, foreground, keyboard, self.now())
        self.record(_Seen(observation, focus), self.epoch_seen)
        return observation

    def close_transition(self, screen: Screen | None, foreground: str | None) -> None:
        """Write the end line of the open transition and count the screen."""
        if self._open is not None:
            action_id, self._open = self._open, None
            if screen is None:
                self.writer.write_transition_end(action_id, dump_failed=True)
            else:
                self.writer.write_transition_end(action_id, screen.screen_id)
        if screen is not None and self.on_target(screen, foreground):
            self.screens_seen.add(screen.screen_id)  # counted when observed

    def record(self, seen: _Seen, epoch: int) -> None:
        """Remember the focus, count and write a target screen, then its image.

        Nothing is written when the pin's epoch is no longer `epoch` (the one
        the reads began at): reads that an event crossed may be mixed.
        """
        self.focus = seen.focus
        screen = seen.observation.screen
        if screen is None or not self.on_target(screen, seen.observation.foreground):
            return
        if self.device.pin_epoch != epoch:
            return
        self.screens_seen.add(screen.screen_id)
        if not self.writer.write_screen(screen) or not self.screenshots:
            return
        try:
            png = self.device.screenshot()
            after = self.device.foreground()
        except ScreenshotFailedError as error:
            reason = error.details.get("reason", "unknown")
            self.notes.append(f"no screenshot of {screen.screen_id}: {reason}")
            return
        if after != self.package:
            # Another app may have come forward while the image was taken.
            self.notes.append(
                f"no screenshot of {screen.screen_id}: the app was not in front"
            )
            return
        if self.device.pin_epoch != epoch:
            # An event crossed the screenshot's reads: the image is dropped.
            self.notes.append(
                f"no screenshot of {screen.screen_id}: the device was proven again"
            )
            return
        self.writer.write_screenshot(screen.screen_id, png)

    def on_target(self, screen: Screen, foreground: str | None) -> bool:
        # The engine's own rule: both the dump and the foreground name the app.
        return screen.package == self.package and foreground == self.package

    def write_skips(self, *screens: Screen | None, tap: Tap | None = None) -> None:
        """Write the engine's new skipped elements; `screens` hold the elements.

        `tap`, when given, is the tap the engine re-checked: a row about its
        screen and fingerprint names that very element.
        """
        skipped = self.engine.records.skipped
        new = skipped[self._skips_written :]
        self._skips_written = len(skipped)
        found: dict[str, dict[str, list[tuple[Element, tuple[str, ...]]]]] = {}
        for item in new:
            body = _skipped_element(
                item, screens, found, package=self.package, policy=self.policy, tap=tap
            )
            details = [d.to_dict() for d in item.details if d.reason in _WORD_REASONS]
            self.writer.write_skip(
                item.screen_id, item.fingerprint, item.reasons, body, details
            )


def _failure_word(error: BaseException) -> str:
    """The short word for an error that ended a launch: the launch failure's
    or the device error's ``reason`` when it has one (``launcher_unreadable``,
    ``device_changed``), else the error code (``adb_error`` for a lost adb
    connection); ``interrupted`` for Ctrl+C, ``internal_error`` otherwise."""
    if isinstance(error, KeyboardInterrupt):
        return INTERRUPTED
    if not isinstance(error, UiprobeError):
        return "internal_error"
    reason = error.details.get("reason")
    if isinstance(error, LaunchFailedError | DeviceNotFoundError) and isinstance(
        reason, str
    ):
        return reason
    return error.code


def _unreadable_launcher(error: Exception) -> bool:
    """Whether `error` is a launch that stopped at its launcher read."""
    return (
        isinstance(error, LaunchFailedError)
        and error.details.get("reason") == "launcher_unreadable"
    )


def _kind(action: Action) -> str:
    """The ``kind`` of an action in its start line."""
    if isinstance(action, Launch):
        return "launch"
    if isinstance(action, Relaunch):
        return "relaunch"
    if isinstance(action, Back):
        return "back"
    return "tap"


def _element_of(screen: Screen | None, element_id: str) -> Element | None:
    if screen is None:
        return None
    return next((e for e in screen.elements if e.id == element_id), None)


def _skipped_element(
    item: SkippedElement,
    screens: tuple[Screen | None, ...],
    found: dict[str, dict[str, list[tuple[Element, tuple[str, ...]]]]],
    *,
    package: str,
    policy: Policy,
    tap: Tap | None = None,
) -> Element | dict[str, object]:
    """Find the element a skip is about; a whole-screen row has none.

    Elements can share a fingerprint but differ in their skip reasons (two
    identical tiles, one at the screen edge). The element is the first one, in
    document order, whose decision from `judge` on that screen (foreground
    taken as `package`, as on every screen the engine judges) has exactly the
    row's reasons; when none has, the first one with the fingerprint.

    ``found`` caches, per screen id, the fingerprint to (element, reasons)
    list map, so one screen is fingerprinted and judged once per call. The row
    of an oversized screen has its screen id as fingerprint and no element;
    the engine builds no index over such a screen, so neither does the driver.
    """
    if item.fingerprint == item.screen_id:
        return {}
    if tap is not None and item.screen_id == tap.screen_id:
        # A row the re-check of this tap wrote: name the element it chose.
        for screen in screens:
            if screen is not None and screen.screen_id == tap.screen_id:
                chosen = _element_of(screen, tap.element_id)
                if (
                    chosen is not None
                    and _Prints(screen).of(chosen) == item.fingerprint
                ):
                    return chosen
    for screen in screens:
        if screen is None or screen.screen_id != item.screen_id:
            continue
        index = found.get(screen.screen_id)
        if index is None:
            prints = _Prints(screen)
            judgement = judge(screen, package, policy, foreground=package)
            reasons = {d.element.id: tuple(d.reasons) for d in judgement.decisions}
            index = {}
            for element in screen.elements:
                index.setdefault(prints.of(element), []).append(
                    (element, reasons.get(element.id, ()))
                )
            found[screen.screen_id] = index
        candidates = index.get(item.fingerprint)
        if candidates:
            wanted = tuple(item.reasons)
            for element, reasons_of in candidates:
                if reasons_of == wanted:
                    return element
            return candidates[0][0]
    return {}


def crawl(
    device: Device,
    package: str,
    out: str | os.PathLike[str],
    *,
    policy: Policy,
    limits: Limits,
    screenshots: bool = False,
    allow_real_device: bool = False,
    settle: float = 1.0,
    stop: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    restart: bool = True,
) -> CrawlResult:
    """Crawl `package` on `device`, pressing what the guards allow, into `out`.

    This presses on the selected device. It stops and starts the app (see
    `restart`), taps elements that `policy` allows, presses BACK and
    relaunches the app, and records every step in the new run folder `out`
    (see the run folder page). A screen of
    another app gets a relaunch, not BACK, unless
    ``policy.back_on_other_screens`` is True. It is alpha;
    ``uiprobe crawl --press`` runs it from the command line.

    The order is: check the arguments, check the device, make the run folder,
    run. Nothing is written and no input is sent before the first two have
    passed (the device check itself reads one property).

    `restart` (the default True) makes the first action force-stop the app
    (``am force-stop``) and then start its launcher activity (which screen
    the app then shows is up to the app); what the app had not saved is
    lost. It is one action of kind ``launch``, counted once, whose start
    line has ``restart: true``, and it passes the checks of a relaunch: the
    force-stop goes out only after `stop`, the time limit and the pin were
    checked and the launcher activity was read (when it cannot be read,
    nothing is sent), and the start only after they were checked and read
    again. When `stop` or the time limit ends the run between the two, the
    start is not sent (``start_not_sent``) and the summary says the app was
    stopped but not started. ``restart=False`` sends only the start, so the
    crawl begins on the screen the app shows now. Earlier versions behaved
    as ``restart=False``. Every launch and relaunch reads the app's launcher
    activity with ``cmd package resolve-activity`` right before its start
    and starts it with ``am start -W`` (`Device.launch`); when it cannot be
    read, the start is not sent and the run ends with `LaunchFailedError`
    (``launcher_unreadable``). With `restart` the launcher activity is also
    read before the force-stop: when that read fails, nothing is sent.
    Unless `allow_real_device` is True, the device must answer that it is an
    emulator; an answer that cannot be read counts as a real device.

    Every action passes the gate described in the module docstring right
    before it is sent; a tap is reported to the engine with
    `CrawlEngine.tap_sent` only after it was sent. The device's transport is
    pinned before the emulator check, so only the checked device receives
    anything. `settle` is the pause in seconds after a tap or BACK; it ends
    early at the time limit or when `stop` returns True. `stop` is called
    between actions, in the gate and during the pause; when it returns True
    the crawl ends with ``interrupted``.
    `clock` (monotonic seconds, at least 0 and below 2**40, as the engine
    requires) and `sleep` are replaceable for tests.
    `screenshots` also saves a screenshot of each new screen; a screen that
    cannot be captured is noted in the summary and the crawl goes on. The
    image is taken after the dump, so it can differ if the app changed
    between them.

    Only screens of the target app are written to ``screens/``; a screen of
    another app (a permission dialog, the launcher) gets its id in the
    transition lines and, on the start line of a BACK or relaunch decided on
    it, ``off_target`` with its package, the foreground and the focus owner.

    Ending is described in the module docstring: the engine's stop reason, or
    ``interrupted``, or ``error``. A `KeyboardInterrupt` is returned as
    ``interrupted`` only when `stop` returns True at that moment and
    ``run.json`` could be written, else it is raised again. Errors are raised
    again after ``run.json`` is written with ``error``.

    Returns:
        The `CrawlResult` with the run folder, the stop reason and the counts.

    Raises:
        TypeError: An argument has the wrong type.
        ValueError: `package` is not a valid package name, `settle` is
            negative, not finite or not below 2**40, or `clock` returned a
            value outside [0, 2**40) before the run folder was made.
        NotEmulatorError: The device is not an emulator (or the check cannot
            be read) and `allow_real_device` is False. Nothing was written.
        AdbError: The adb server reported no transport id for the device, so
            it cannot be pinned. Nothing was written.
        LaunchFailedError: A launch failed other than by not coming to the
            front in time (``launcher_unreadable`` among them: the start was
            not sent).
        RunFolderError: `out` exists, its parent is missing, or a file cannot
            be written.
        UiprobeError: A device or adb error that prevents going on.
    """
    # Arguments are turned into plain values once, here, and only those are
    # used: a subclass cannot run code (a device read, say) in the middle of
    # a check later. The callables (stop, clock, sleep) cannot be turned into
    # plain values; no caller code runs after the gate's last comparison.
    if isinstance(package, str):
        package = str.__str__(package)
    check_package(package)
    if not isinstance(policy, Policy):
        msg = f"policy must be a Policy, not {type(policy).__name__}"
        raise TypeError(msg)
    policy = _plain_policy(policy)
    if not isinstance(limits, Limits):
        msg = f"limits must be a Limits, not {type(limits).__name__}"
        raise TypeError(msg)
    limits = Limits(
        depth=int.__int__(limits.depth),
        actions=int.__int__(limits.actions),
        minutes=int.__int__(limits.minutes),
    )
    if not isinstance(out, str | os.PathLike):
        msg = f"out must be a str or a path, not {type(out).__name__}"
        raise TypeError(msg)
    _check_flag("screenshots", screenshots)
    _check_flag("allow_real_device", allow_real_device)
    _check_flag("restart", restart)
    _check_clock("settle", settle)
    settle = (
        float.__float__(settle)
        if isinstance(settle, float)
        else float(int.__int__(settle))
    )
    if settle < 0:
        msg = f"settle must not be negative, got {settle}"
        raise ValueError(msg)
    if stop is not None:
        _check_callable("stop", stop)
    _check_callable("clock", clock)
    _check_callable("sleep", sleep)
    # Pin first (unless Device(..., pin=True) already did, right when the
    # device was chosen), then check through the pinned transport: the device
    # that was checked is the only one that can receive anything afterwards.
    # The identity read at the pin lets the gate see a different device behind
    # the same transport number (a restarted adb server numbers anew), and its
    # boot id decides whether a renumbered transport may be followed.
    # Before the run exists, a wait for the device asks nothing (stop is not
    # called while the device is being proven); the caller's own watch is put
    # back at the end.
    caller_watch = device.pin_watch
    device.set_pin_watch(None)
    repins_before = len(device.repins)
    try:
        return _crawl_on(
            device,
            package,
            out,
            policy=policy,
            limits=limits,
            screenshots=screenshots,
            allow_real_device=allow_real_device,
            settle=settle,
            stop=stop,
            clock=clock,
            sleep=sleep,
            repins_before=repins_before,
            restart=restart,
        )
    finally:
        device.set_pin_watch(caller_watch)


def _crawl_on(
    device: Device,
    package: str,
    out: str | os.PathLike[str],
    *,
    policy: Policy,
    limits: Limits,
    screenshots: bool,
    allow_real_device: bool,
    settle: float,
    stop: Callable[[], bool] | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    repins_before: int,
    restart: bool,
) -> CrawlResult:
    """`crawl` after its arguments were checked: pin, check, run."""
    forget_displays(device)  # several displays seen count within one run
    device.pin_transport()
    _check_device(device, allow_real_device=allow_real_device)
    # The app's uid, which a tap's window must have (Policy.check_tap_window),
    # and whether other packages share it; None when it cannot be read: then
    # every tap is refused (cover_unknown) before any window is read.
    app_uid = device.package_uid(package) if policy.check_tap_window else None
    # The run's end by uiprobe's own clock, set once. With uiprobe's own
    # clock (the default) it is read once, for both. With a clock of the
    # caller's, uiprobe's is read before the caller's first value, and a
    # later caller value can bring the end earlier, never later.
    own_clock = clock is _OWN_CLOCK
    if own_clock:
        started_at = _monotonic()
        send_limit = started_at + limits.minutes * 60
    else:
        send_limit = _monotonic() + limits.minutes * 60
        started_at = _seconds(clock())
    engine = CrawlEngine(package, policy, limits, started_at=started_at)
    writer = RunWriter.create(
        out,
        package=package,
        limits=limits,
        deny_words=policy.deny_words,
        deny_exceptions=policy.deny_exceptions,
        serial=device.serial,
        back_on_other_screens=policy.back_on_other_screens,
        repin=device.can_repin,
        check_tap_window=policy.check_tap_window,
        restart=restart,
    )
    try:
        run = _Run(
            device,
            package,
            writer,
            engine,
            policy,
            emulator=not allow_real_device,
            app_uid=app_uid,
            screenshots=screenshots,
            settle=settle,
            deadline=started_at + limits.minutes * 60,
            stop=stop,
            clock=clock,
            sleep=sleep,
            send_limit=send_limit,
            own_clock=own_clock,
            restart=restart,
        )
        run.repins_before = repins_before  # moves made before this call
        # Asked only while the device waits for a renumbered transport.
        device.set_pin_watch(run.waiting_stopped)
        return _drive(run, writer)
    except BaseException as error:
        _mark_own_run_folder(error)
        raise
    finally:
        try:
            with contextlib.suppress(RunFolderError):
                writer.close()
        except BaseException as error:  # an interrupt while it closes
            _mark_own_run_folder(error)
            raise


def _mark_own_run_folder(error: BaseException) -> None:
    """Say on `error` that this run made its folder and wrote run-start.json.

    For the command line only: the name is private, not part of the
    documented API, and a literal, so that the source checks can see it
    touches no module. An error that refuses the attribute keeps its
    identity and stays unmarked.
    """
    with contextlib.suppress(Exception):
        setattr(error, "_uiprobe_own_run_folder", True)  # noqa: B010


def _drive(run: _Run, writer: RunWriter) -> CrawlResult:
    """Run the crawl and end the run folder for every way it can end."""
    try:
        reason = run.run()
    except DeviceNotFoundError as error:
        if error.details.get("reason") != PIN_WAIT_STOPPED:
            code = error.code
            _end(run, writer, STOP_ERROR, quiet=True, error_code=code)
            raise
        # The device was waited for and stop() or the time limit ended the
        # wait: the run ends as it would have between two actions. An action
        # still in the gate was not sent and is closed as such; one already
        # sent keeps its start line alone (its result is unknown).
        why = run.wait_stop or NOT_SENT_STOPPED
        reason = MAX_DURATION if why == NOT_SENT_TIME_LIMIT else INTERRUPTED
        if run.unsent is not None:
            with contextlib.suppress(RunFolderError):
                run.end_not_sent(run.unsent, why)
    except KeyboardInterrupt:
        # Returned only when stop() asks for it and run.json was written; a
        # run.json that could not be written (the interrupt hit a write and
        # the writer refuses more) leaves the interrupt to be raised again.
        returns = _stop_requested(run)
        ended = _end(run, writer, INTERRUPTED, quiet=True)
        if not (returns and ended):
            raise
        return _result(run, writer, INTERRUPTED)
    except Exception as error:
        code = error.code if isinstance(error, UiprobeError) else "internal_error"
        _end(run, writer, STOP_ERROR, quiet=True, error_code=code)
        raise
    _end(run, writer, reason, quiet=False)
    return _result(run, writer, reason)


def _stop_requested(run: _Run) -> bool:
    try:
        return run.stopped()
    except Exception:  # noqa: BLE001 - the interrupt is what gets handled here
        return False


def _end(
    run: _Run,
    writer: RunWriter,
    reason: str,
    *,
    quiet: bool,
    error_code: str | None = None,
) -> bool:
    """Write ``summary.txt`` and ``run.json``; return whether that worked.

    `quiet` keeps an earlier error: a failure to write is then reported only
    by the result, not raised.
    """
    screens, actions = run.count()
    notes = list(run.notes)
    repins = run.device.repins[run.repins_before :]
    notes.extend(
        f"transport {old} -> {new} for {run.device.serial}:"
        " same serial, identity and boot id"
        for old, new in repins
    )
    if error_code is not None:
        notes.append(f"error: {error_code}")
    try:
        with writer.critical():
            writer.finish(
                reason,
                screens=screens,
                actions=actions,
                notes=notes,
                repins=len(repins),
            )
    except RunFolderError:
        if not quiet:
            raise
        return False
    return True


def _result(run: _Run, writer: RunWriter, reason: str) -> CrawlResult:
    screens, actions = run.count()
    return CrawlResult(
        writer.path,
        reason,
        screens,
        actions,
        not_sent=tuple(sorted(run.not_sent.items())),
        repins=len(run.device.repins) - run.repins_before,
        repin=run.device.can_repin,
        cover_unknown=run.cover_unknown,
        cover_rule=run.cover_rule,
        last_screen_id=run.last_screen_id,
        last_foreground=run.last_foreground,
        last_focus=None if run.focus is None else run.focus.owner,
    )
