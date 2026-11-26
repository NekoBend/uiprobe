"""The entry points of a pressing crawl: `crawl` and the loop around a `_Run`."""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from uiprobe.crawl import CrawlEngine, Limits, _check_clock
from uiprobe.crawl_driver.constants import (
    _OWN_CLOCK,
    INTERRUPTED,
    MAX_DURATION,
    NOT_SENT_STOPPED,
    NOT_SENT_TIME_LIMIT,
    STOP_ERROR,
)
from uiprobe.crawl_driver.helpers import Progress, _plain_policy, _seconds
from uiprobe.crawl_driver.run import _Run
from uiprobe.crawl_preview import check_package
from uiprobe.device import PIN_WAIT_STOPPED, _monotonic, forget_displays
from uiprobe.errors import DeviceNotFoundError, NotEmulatorError, UiprobeError
from uiprobe.guards import Policy
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from uiprobe.device import Device


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
    window with input focus at the last observation (None when unknown);
    ``other_apps`` the packages in front when the engine acted on a screen of
    another app (a BACK, a relaunch, or the stop at the relaunch limit), with
    how many times each, most frequent first and, for equal counts, first seen
    first.
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
    other_apps: tuple[tuple[str, int], ...] = ()


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
    record_text: bool = True,
    on_progress: Callable[[Progress], None] | None = None,
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
    `record_text` (default True) False keeps the texts of screens out of the
    run folder: every element's ``text``, ``content_desc`` and ``hint`` is
    written as ``""`` and named in ``hidden``, ``summary.txt`` shows
    ``[text not recorded]`` for labels, the window title of a
    ``covered_by_other`` end line is blanked, and ``run-start.json`` says
    ``record_text: false``. It is a setting of the records, not of judging:
    the engine, the ids and the gate use the real texts, and only the writer
    hides them (so it is a keyword here and not a `Policy` field). A password
    field's text is hidden whatever this says. It cannot be combined with
    `screenshots` (a picture shows the text): that is a `ValueError`.
    `on_progress`, when given, is called with a `Progress` after each sent
    action, once its end line is written and the screen after it was read
    (also for the first launch), never for an action that was not sent and
    never once the run has stopped. It runs in the crawl's own thread,
    between two actions and never between a gate's last comparison and the
    input (see the send rule in docs/safety.md), and it reads nothing from the
    device. The library prints nothing itself. An exception it raises ends
    the run like any other error of the loop (``error`` with the code
    ``internal_error``, then raised again), a `KeyboardInterrupt` like Ctrl+C;
    it is not swallowed.
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
        ValueError: `screenshots` and ``record_text=False`` are both set,
            `package` is not a valid package name, `settle` is
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
    _check_flag("record_text", record_text)
    if screenshots and not record_text:
        msg = (
            "screenshots cannot be combined with record_text=False: a screenshot"
            " shows the text of the screen"
        )
        raise ValueError(msg)
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
    if on_progress is not None:
        _check_callable("on_progress", on_progress)
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
            record_text=record_text,
            on_progress=on_progress,
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
    record_text: bool,
    on_progress: Callable[[Progress], None] | None,
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
        record_text=record_text,
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
            on_progress=on_progress,
            started_at=started_at,
            limits=limits,
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


def _ranked(counts: dict[str, int]) -> tuple[tuple[str, int], ...]:
    """The counts, most frequent first; equal counts keep their first-seen order."""
    return tuple(sorted(counts.items(), key=lambda item: -item[1]))


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
        other_apps=_ranked(run.other_apps),
    )
