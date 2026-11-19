"""The pressing crawl: sends the engine's actions to a device and records them.

`crawl` joins three parts that stay separate elsewhere: `CrawlEngine` decides
every step, `Device` sends it, and `RunWriter` records it. It lives in its own
module, not in ``crawl.py``, because that module is the pure engine (no device,
clock or file), and the writer imports it, so the driver sits above both.

The command line does not use it yet; the function is alpha.

What is sent. Only what the engine returned, and for a tap only what
`CrawlEngine.recheck` allowed for that very `Tap` object, in the gate right
before it is sent: the first launch, a
relaunch (``force-stop`` then start), a tap at the point judge gave, and the
BACK key. Text, long presses, swipes and other keys are never sent. Every
action is sent once and never retried. A tap is reported to the engine with
`CrawlEngine.tap_sent` only after it was sent without an error; a refused tap,
or one whose send raised, is never reported, and the engine releases it on the
next `CrawlEngine.next` with a ``tap_not_sent`` skip row.

The loop (the protocol of the `CrawlEngine` docstring)::

    pin the device's transport; check it is an emulator (through that pin)
    launch; observe
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

The gate (`_Run.gate`) is the one place every action passes right before it
is sent. In order: `stop` and the time limit; the pin (`Device.check_pin`:
the same transport, serial and identity, else an error); then fresh reads,
for a tap the dump first. It refuses, with a ``not_sent`` end line and nothing
sent, when anything it needs cannot be read or has changed: for a tap the
engine's `CrawlEngine.recheck` on the fresh dump, a keyboard known to be
hidden and a focused window owned by the app; for BACK and relaunch the same
foreground and focused window (and for BACK keyboard) as the observation they
were chosen on. After the decision nothing else runs before the input. A
refused tap's fresh observation goes to `CrawlEngine.next`, which releases the
unreported tap; a refused BACK or relaunch ends the run with ``not_sent``,
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
from uiprobe.errors import (
    AdbError,
    DumpError,
    LaunchFailedError,
    NotEmulatorError,
    ScreenshotFailedError,
    UiprobeError,
)
from uiprobe.guards import Policy, judge
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from uiprobe.crawl import Action, SkippedElement
    from uiprobe.device import Device, DeviceIdentity, FocusedWindow
    from uiprobe.model import Element, Screen

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
    """

    out: Path
    stop_reason: str
    screens: int
    actions: int


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
NOT_SENT_FOCUS_OTHER: Final = "focus_other"
NOT_SENT_KEYBOARD_UNKNOWN: Final = "keyboard_unknown"
NOT_SENT_KEYBOARD_SHOWN: Final = "keyboard_shown"
NOT_SENT_CHANGED: Final = "changed_before_send"
NOT_SENT_REFUSED: Final = "changed_before_press"
NOT_SENT_START: Final = "start_not_sent"
# The stop reason of a run that ended because BACK or a relaunch was not sent.
STOP_NOT_SENT: Final = "not_sent"
MAX_DURATION: Final = "max_duration"  # the engine's reason for the time limit
# How often a pause looks at `stop` and the time limit.
PAUSE_STEP: Final = 0.1


@dataclass(frozen=True, slots=True)
class _Seen:
    """What the device showed: the observation plus the focused window."""

    observation: Observation
    focus: FocusedWindow | None


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
        identity: DeviceIdentity,
        emulator: bool,
        screenshots: bool,
        settle: float,
        deadline: float,
        stop: Callable[[], bool] | None,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self.device = device
        self.package = package
        self.writer = writer
        self.engine = engine
        self.policy = policy
        self.identity = identity
        self.emulator = emulator
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
        self._skips_written = 0

    # -- the loop --

    def run(self) -> str:
        """Run until a stop reason is known; return it."""
        if self.stopped():
            return INTERRUPTED
        engine = self.engine
        self.send(engine.start(), None)
        observation = self.observe()
        while True:
            if self.stopped():
                return INTERRUPTED
            action = engine.next(observation)
            self.write_skips(observation.screen)
            if isinstance(action, Stop):
                return action.reason
            if isinstance(action, Reread):
                observation = self.observe()
                continue
            refused, fresh = self.send(action, observation)
            if refused is not None and isinstance(action, Back | Relaunch):
                # The engine counted it as sent: rather than let it go on from
                # a wrong count, the run ends (alpha: the safe side).
                if self._stop_seen:
                    return INTERRUPTED
                if self.clock() >= self.deadline:
                    return MAX_DURATION
                self.notes.append(f"{_kind(action)} was not sent: {refused}")
                return STOP_NOT_SENT
            if fresh is not None:
                # A tap the gate did not send: the screen it re-read goes to
                # the engine, which releases the unreported tap.
                observation = fresh
                continue
            observation = self.observe()

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
        from_id = None if screen is None else screen.screen_id
        element: Element | None = None
        point: tuple[int, int] | None = None
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
            action_id, _kind(action), from_id, element, point
        )
        reason, seen = self.gate(action, observation)
        if seen is not None and isinstance(action, Tap):
            self.write_skips(screen, seen.observation.screen, tap=action)
        if reason is not None:
            self.writer.write_transition_end(action_id, not_sent=reason)
            if seen is not None and isinstance(action, Tap):
                self.record(seen)
                return reason, seen.observation
            return reason, None
        # Nothing runs between the gate's last read and the input: no user
        # code (stop() was asked first), no other read.
        self._open = action_id
        if isinstance(action, Tap):
            self.device.tap(action.point)
            self.engine.tap_sent(action)
            self.pause()
        elif isinstance(action, Back):
            self.device.key("back")
            self.pause()
        elif isinstance(action, Relaunch):
            return self.relaunch(action_id), None
        else:
            self.launch()
        return None, None

    def gate(
        self, action: Action, observation: Observation | None
    ) -> tuple[str | None, _Seen | None]:
        """Decide whether `action` may be sent now; return (reason, what was read).

        The one place every action passes right before it is sent. First `stop`
        and the time limit, then that the pinned transport still leads to the
        checked device (an error if not), then a fresh read: for a tap the dump
        first and then foreground, focused window and keyboard, so the reads
        nearest the press come last. Anything that cannot be read, or that
        differs from what the action was chosen on, is a refusal. After the
        decision nothing else runs before the input is sent.
        """
        reason = self.halt()
        if reason is not None:
            return reason, None
        self.device.check_pin(self.identity, emulator=self.emulator)
        if isinstance(action, Launch):
            return None, None
        screen = self.dump() if isinstance(action, Tap) else None
        foreground = self.device.foreground()
        focus = self.device.focused_window()
        keyboard = self.device.keyboard_shown()
        seen = _Seen(Observation(screen, foreground, keyboard, self.clock()), focus)
        if isinstance(action, Tap):
            allowed = self.engine.recheck(action, seen.observation)
            owner = None if focus is None else focus.owner
            checks = (
                (keyboard is None, NOT_SENT_KEYBOARD_UNKNOWN),
                (keyboard is True, NOT_SENT_KEYBOARD_SHOWN),
                (focus is None, NOT_SENT_FOCUS_UNKNOWN),
                (owner != self.package, NOT_SENT_FOCUS_OTHER),
                (not allowed, NOT_SENT_REFUSED),
            )
        else:
            before = observation
            back = isinstance(action, Back)
            checks = (
                (before is None, NOT_SENT_CHANGED),  # not reached: a launch only
                (foreground is None, NOT_SENT_FOREGROUND_UNKNOWN),
                (focus is None, NOT_SENT_FOCUS_UNKNOWN),
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
        return next((why for failed, why in checks if failed), None), seen

    def halt(self) -> str | None:
        """The reason not to send that does not depend on the screen, if any."""
        if self.stopped():
            return NOT_SENT_STOPPED
        if self.clock() >= self.deadline:
            return NOT_SENT_TIME_LIMIT
        return None

    def pause(self) -> None:
        """Wait `settle` seconds, but not past the time limit or a stop.

        The number of steps is bounded, so a clock that does not move cannot
        keep the pause going.
        """
        end = min(self.clock() + self.settle, self.deadline)
        for _ in range(int(self.settle / PAUSE_STEP) + 2):
            left = end - self.clock()
            if left <= 0 or self.stopped():
                return
            self.sleep(min(PAUSE_STEP, left))

    def launch(self) -> None:
        """Start the app; an app that is not in front afterwards is not fatal.

        ``not_in_front`` means monkey started something, but another package
        (a permission dialog, a splash screen) holds the focus. The engine
        handles an off-target screen, so the crawl observes and goes on. The
        action stays counted as sent. Any other launch failure is raised.
        """
        try:
            self.device.launch(self.package)
        except LaunchFailedError as error:
            if error.details.get("reason") != "not_in_front":
                raise
            seen = error.details.get("foreground")
            self.notes.append(f"{self.package} was not in front after launch ({seen})")

    def relaunch(self, action_id: int) -> str | None:
        """Force-stop the app, then, unless told to stop meanwhile, start it.

        The two commands are sent one after the other, and `stop` and the time
        limit are looked at again between them. When they end the run there,
        only the force-stop was sent: the start line gets a ``not_sent`` end
        (``start_not_sent``) and the summary says so. Returns that reason, or
        None when the app was started.
        """
        self.device.stop_app(self.package)
        reason = self.halt()
        if reason is not None:
            self._open = None
            self.writer.write_transition_end(action_id, not_sent=NOT_SENT_START)
            self.notes.append(
                f"{self.package} was force-stopped but not started again ({reason})"
            )
            return NOT_SENT_START
        self.launch()
        return None

    # -- reading --

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
        """
        self.device.check_pin(self.identity, emulator=self.emulator)
        screen = self.dump()
        foreground = self.device.foreground()
        if self._open is not None:
            action_id, self._open = self._open, None
            if screen is None:
                self.writer.write_transition_end(action_id, dump_failed=True)
            else:
                self.writer.write_transition_end(action_id, screen.screen_id)
        if screen is not None and self.on_target(screen, foreground):
            self.screens_seen.add(screen.screen_id)  # counted when observed
        keyboard = self.device.keyboard_shown()
        focus = self.device.focused_window()
        observation = Observation(screen, foreground, keyboard, self.clock())
        self.record(_Seen(observation, focus))
        return observation

    def record(self, seen: _Seen) -> None:
        """Remember the focus, count and write a target screen, then its image."""
        self.focus = seen.focus
        screen = seen.observation.screen
        if screen is None or not self.on_target(screen, seen.observation.foreground):
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
            self.writer.write_skip(item.screen_id, item.fingerprint, item.reasons, body)


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
) -> CrawlResult:
    """Crawl `package` on `device`, pressing what the guards allow, into `out`.

    This presses on the selected device. It starts the app, taps elements that
    `policy` allows, presses BACK and relaunches the app, and records every
    step in the new run folder `out` (see the run folder page). It is alpha
    and not in the command line yet.

    The order is: check the arguments, check the device, make the run folder,
    run. Nothing is written and no input is sent before the first two have
    passed (the device check itself reads one property). Unless
    `allow_real_device` is True, the device must answer that it is an
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
    another app (a permission dialog, the launcher) gets only its id in the
    transition lines.

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
        RunFolderError: `out` exists, its parent is missing, or a file cannot
            be written.
        UiprobeError: A device or adb error that prevents going on.
    """
    check_package(package)
    if not isinstance(policy, Policy):
        msg = f"policy must be a Policy, not {type(policy).__name__}"
        raise TypeError(msg)
    if not isinstance(limits, Limits):
        msg = f"limits must be a Limits, not {type(limits).__name__}"
        raise TypeError(msg)
    if not isinstance(out, str | os.PathLike):
        msg = f"out must be a str or a path, not {type(out).__name__}"
        raise TypeError(msg)
    _check_flag("screenshots", screenshots)
    _check_flag("allow_real_device", allow_real_device)
    _check_clock("settle", settle)
    if settle < 0:
        msg = f"settle must not be negative, got {settle}"
        raise ValueError(msg)
    if stop is not None:
        _check_callable("stop", stop)
    _check_callable("clock", clock)
    _check_callable("sleep", sleep)
    # Pin first, then check through the pinned transport: the device that was
    # checked is the only one that can receive anything afterwards. Its
    # identity is kept so that the gate can see a different device behind the
    # same transport number (a restarted adb server numbers anew).
    device.pin_transport()
    _check_device(device, allow_real_device=allow_real_device)
    identity = device.identity()
    if identity is None:
        msg = f"the identity of {device.serial} could not be read, so it cannot be kept"
        raise AdbError(msg, details={"serial": device.serial})
    started_at = clock()
    _check_clock("clock()", started_at)
    engine = CrawlEngine(package, policy, limits, started_at=started_at)
    writer = RunWriter.create(
        out,
        package=package,
        limits=limits,
        deny_words=policy.deny_words,
        serial=device.serial,
    )
    try:
        run = _Run(
            device,
            package,
            writer,
            engine,
            policy,
            identity=identity,
            emulator=not allow_real_device,
            screenshots=screenshots,
            settle=settle,
            deadline=started_at + limits.minutes * 60,
            stop=stop,
            clock=clock,
            sleep=sleep,
        )
        return _drive(run, writer)
    finally:
        with contextlib.suppress(RunFolderError):
            writer.close()


def _drive(run: _Run, writer: RunWriter) -> CrawlResult:
    """Run the crawl and end the run folder for every way it can end."""
    try:
        reason = run.run()
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
    if error_code is not None:
        notes.append(f"error: {error_code}")
    try:
        with writer.critical():
            writer.finish(reason, screens=screens, actions=actions, notes=notes)
    except RunFolderError:
        if not quiet:
            raise
        return False
    return True


def _result(run: _Run, writer: RunWriter, reason: str) -> CrawlResult:
    screens, actions = run.count()
    return CrawlResult(writer.path, reason, screens, actions)
