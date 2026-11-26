"""The crawl command and the pressing run."""

from __future__ import annotations

import argparse  # noqa: TC003 (a runtime import in the former cli.py)
import contextlib
import sys
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Final, Literal

from uiprobe._json import dumps
from uiprobe.also_target import AlsoTargetError
from uiprobe.cli.commands import _single_serial
from uiprobe.cli.crawl_dry import (
    _check_plan_stream,
    _crawl_policy,
    _CrawlSetup,
    _preview_guidance,
    _preview_json,
    _preview_text,
)
from uiprobe.cli.output import _emit, _fail, _flush, _printable
from uiprobe.cli.parser import (
    _DEFAULT_LIMITS,
    _PRESS_LIMITS,
    _PRESS_SETTLE,
    _UsageError,
)
from uiprobe.crawl import Limits
from uiprobe.crawl_driver import CrawlResult, _check_device
from uiprobe.crawl_driver import crawl as crawl_run
from uiprobe.crawl_preview import preview as make_preview
from uiprobe.device import PIN_WAIT_STOPPED, Device
from uiprobe.errors import INTERRUPTED, NotEmulatorError, UiprobeError
from uiprobe.settings import (
    _DEFAULT_SCROLL_AMOUNT,
    _DEFAULT_SCROLL_INCIDENTS,
    _DEFAULT_SCROLLS_PER_SCREEN,
    Settings,
    _shown,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

    from uiprobe.crawl_driver import Progress
    from uiprobe.guards import Policy

CRAWL_RUN_SCHEMA = "uiprobe.crawl_run/1"


# Where a limit of a pressing run came from (the "limits_source" values).
_LimitSource = Literal["option", "file", "default"]
# Where BACK on screens of other apps came from (the
# "back_on_other_screens_source" values).
_BackSource = Literal["option", "file", "default"]


def _cmd_crawl(args: argparse.Namespace) -> int:
    if args.press:
        # Before anything else writes (a settings warning would go to stdout).
        _check_plan_stream(args)
    setup = _crawl_policy(args)  # checks the plan stream of press = true too
    if setup.press_source is not None:
        # Again right before the device is made: whatever wrote in between
        # (the settings warnings) may have closed the stream.
        _check_plan_stream(args)
        return _cmd_crawl_press(args, setup)
    settings_path = str(setup.settings.source)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
    )
    result = make_preview(device, args.package, setup.policy)
    if args.json:
        _emit(dumps(_preview_json(result, settings_path), indent=2))
    else:
        guidance = _preview_guidance(args, setup.settings, device)
        _emit(
            _preview_text(
                result, settings_path, dry_run=args.dry_run, guidance=guidance
            )
        )
    return 0


def _press_limits(
    args: argparse.Namespace, settings: Settings | None
) -> tuple[Limits, dict[str, _LimitSource]]:
    """Return the limits of a pressing run and where each one came from.

    Each of depth, actions and minutes is the option when it was given, else
    the settings file's [limits] value (every defaults version holds them),
    else `_DEFAULT_LIMITS`, the value `uiprobe init` writes.
    """
    given = {"depth": args.depth, "actions": args.actions, "minutes": args.minutes}
    from_file = (
        None
        if settings is None
        else {
            "depth": settings.depth,
            "actions": settings.actions,
            "minutes": settings.minutes,
        }
    )
    values: dict[str, int] = {}
    sources: dict[str, _LimitSource] = {}
    for name, (low, high, _what) in _PRESS_LIMITS.items():
        source: _LimitSource
        if given[name] is not None:
            value, source = given[name], "option"
        elif from_file is not None:
            value, source = from_file[name], "file"
        else:
            value, source = _DEFAULT_LIMITS[name], "default"
        if not low <= value <= high:
            msg = (
                f"the {name} limit must be from {low} to {high}, got {value}"
                f" ({_limit_source_text(name, source)})"
            )
            raise _UsageError(msg)
        values[name] = value
        sources[name] = source
    # The scroll values have no option: the file's [limits] value, else the
    # default (`uiprobe init` writes the same).
    scrolls = (
        {
            "scrolls_per_screen": _DEFAULT_SCROLLS_PER_SCREEN,
            "scroll_amount": _DEFAULT_SCROLL_AMOUNT,
            "scroll_incidents": _DEFAULT_SCROLL_INCIDENTS,
        }
        if settings is None
        else {
            "scrolls_per_screen": settings.scrolls_per_screen,
            "scroll_amount": settings.scroll_amount,
            "scroll_incidents": settings.scroll_incidents,
        }
    )
    return Limits(**values, **scrolls), sources


def _limit_source_text(name: str, source: str) -> str:
    """Where a limit came from, as the plan text says it."""
    return {"option": f"--{name}", "file": "settings file"}.get(source, "default")


def _press_back(args: argparse.Namespace, policy: Policy) -> tuple[bool, _BackSource]:
    """Whether the run presses BACK on screens of other apps, and why.

    An option decides when given (the parser refuses both together), else the
    settings file's ``back_on_other_screens`` (already in `policy`), else off.
    """
    if args.back_on_other_screens:
        return True, "option"
    if args.no_back_on_other_screens:
        return False, "option"
    if policy.back_on_other_screens:
        return True, "file"
    return False, "default"


def _back_plan_text(plan: dict[str, object]) -> str:
    """The value of the plan line about BACK on screens of other apps."""
    source = plan["back_on_other_screens_source"]
    if plan["back_on_other_screens"]:
        return (
            "yes (settings file)"
            if source == "file"
            else "yes (--back-on-other-screens)"
        )
    if source == "option":
        return "no (--no-back-on-other-screens)"
    return "no (restart the app)"


def _press_settle(args: argparse.Namespace) -> float:
    return _PRESS_SETTLE if args.settle is None else args.settle


# The least number of seconds between two progress lines.
PROGRESS_INTERVAL: Final = 5.0


def _duration_text(seconds: float) -> str:
    """Whole seconds as ``14s``, ``2m 14s`` or ``1h 02m 03s``."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _progress_text(progress: Progress) -> str:
    """The progress line, for example
    ``progress: 2m 14s of 30m, 37 of 300 actions, 12 screens, depth 3 of 5``."""
    screens = "screen" if progress.screens == 1 else "screens"
    return (
        f"progress: {_duration_text(progress.elapsed)} of {progress.minutes}m,"
        f" {progress.actions} of {progress.action_limit} actions,"
        f" {progress.screens} {screens},"
        f" depth {progress.depth} of {progress.depth_limit}"
    )


class _ProgressPrinter:
    """The ``on_progress`` callback of the command: a line on `stream`, the
    first time at once, then at most one every `PROGRESS_INTERVAL` seconds.

    It writes through `_emit` and flushes right away, as the other lines on
    stderr (warnings, notes) do, so a line is never cut by another. A line is
    only a convenience: when a write or flush fails (stderr closed, a broken
    pipe, any other OSError), the printer turns itself off for the rest of the
    run and the crawl goes on, so the result is unaffected. The interval
    counts from the end of the last write.
    """

    def __init__(
        self,
        stream: TextIO | None,
        *,
        clock: Callable[[], float] | None = None,
        interval: float = PROGRESS_INTERVAL,
    ) -> None:
        self._stream = stream
        self._clock = time.monotonic if clock is None else clock
        self._interval = interval
        self._last: float | None = None
        self._off = False

    def __call__(self, progress: Progress) -> None:
        stream = self._stream
        if stream is None or self._off:  # a closed stderr: nothing to write to
            return
        now = self._clock()
        if self._last is not None and now - self._last < self._interval:
            return
        try:
            _emit(_progress_text(progress), stream=stream)
            _flush(stream)
        except (OSError, ValueError):  # ValueError: a file closed under us
            self._off = True
            return
        self._last = self._clock()  # counted from the end of the write


def _cmd_crawl_press(args: argparse.Namespace, setup: _CrawlSetup) -> int:  # noqa: PLR0912, PLR0915 - one error path per kind of failure
    """Run a pressing crawl into its run folder and report how it ended."""
    limits, limits_source = _press_limits(args, setup.settings)
    settle = _press_settle(args)
    record_text = setup.settings.record_text and not args.no_record_text
    if args.screenshots and not record_text:
        origin = (
            "--no-record-text"
            if args.no_record_text
            else "record_text = false in "
            + _printable(_shown(str(setup.settings.source)))
        )
        msg = (
            f"--screenshots cannot be used with {origin}: a screenshot shows the"
            " text of the screen, so the run would record it after all"
            " (nothing was pressed or written)"
        )
        raise _UsageError(msg)
    back, back_source = _press_back(args, setup.policy)
    policy = replace(
        setup.policy,
        back_on_other_screens=back,
        check_tap_window=not args.no_tap_window_check,
    )
    assert setup.out is not None  # noqa: S101 - _crawl_policy sets it to press
    out: str = setup.out
    shown_out = _shown(out)
    device = Device(
        _single_serial(args),
        adb_server=args.adb_server,
        adb=args.adb,
        start_server=args.start_server,
        pin=True,
    )
    emulator = _press_device_check(device, allow_real_device=args.allow_real_device)
    # The identity read through the pin must agree before anything is shown
    # or written: first that it is still the same device, then (without
    # --allow-real-device) that this identity also says emulator, which every
    # later pin check requires.
    device.check_pin(emulator=False)
    if not args.allow_real_device:
        # The identity the pin check just verified (not a fresh read, which
        # could answer differently).
        found = device.pinned_identity
        if found is None or not found.emulator:
            reason = "unreadable" if found is None else "not_emulator"
            refusal = NotEmulatorError(
                "conflicting emulator answers",
                details={"serial": device.serial, "reason": reason},
            )
            raise _cli_not_emulator(device, refusal)
    # The plan says whether the device's own `input` usage lists the wheel
    # scroll, so it is read here for the plan; crawl_run reads it again itself
    # and decides on its own read (run-start.json records that one). Nothing is
    # read when the run was not asked to scroll.
    wheel = device._wheel_supported() if policy.scroll_lists else None  # noqa: SLF001
    plan: dict[str, object] = {
        "package": args.package,
        "device": {"serial": device.serial, "emulator": emulator},
        "press_source": setup.press_source,
        "limits": {
            "depth": limits.depth,
            "actions": limits.actions,
            "minutes": limits.minutes,
        },
        "limits_source": limits_source,
        "out": out,
        "out_source": setup.out_source,
        "back_on_other_screens": policy.back_on_other_screens,
        "back_on_other_screens_source": back_source,
        "check_tap_window": policy.check_tap_window,
        "screenshots": args.screenshots,
        "restart": not args.no_restart,
        "record_text": record_text,
        **({"also_target": list(policy.also_target)} if policy.also_target else {}),
        "scroll_lists": policy.scroll_lists and wheel is True,
        "scroll_input": wheel,
        "scrolls_per_screen": limits.scrolls_per_screen,
        "scroll_amount": limits.scroll_amount,
        "scroll_incidents": limits.scroll_incidents,
    }
    # The plan is out (and flushed) before the first request that changes the
    # device; with --json it goes to stderr, so stdout stays one JSON document.
    plan_stream = sys.stderr if args.json else sys.stdout
    if plan_stream is None:  # a closed stream at start: refuse before pressing
        msg = "the plan cannot be shown: " + ("stderr" if args.json else "stdout")
        raise _UsageError(msg + " is closed, so nothing was pressed")
    _emit(
        _press_plan_text(plan, settings=str(setup.settings.source)),
        stream=plan_stream,
    )
    _flush(plan_stream)
    try:
        result = crawl_run(
            device,
            args.package,
            out,
            policy=policy,
            limits=limits,
            screenshots=args.screenshots,
            # The caller's own choice: crawl_run checks the device again
            # through the pin, and every later pin check keeps requiring an
            # emulator unless --allow-real-device was given.
            allow_real_device=args.allow_real_device,
            settle=settle,
            restart=not args.no_restart,
            record_text=record_text,
            on_progress=None if args.quiet else _ProgressPrinter(sys.stderr),
        )
    except KeyboardInterrupt as exc:
        # crawl_run wrote run.json with "interrupted" when it still could.
        if not _made_by_this_run(exc):
            return _fail(INTERRUPTED, "interrupted", as_json=args.json)
        msg = f"interrupted; the run so far is in {shown_out}"
        return _fail(INTERRUPTED, msg, as_json=args.json, details={"out": out})
    except NotEmulatorError as exc:
        # The check inside crawl_run answered differently from the first one:
        # nothing was written or sent; say it in the command's words.
        refusal = _cli_not_emulator(device, exc)
        if not record_text:
            _hide_device_text(refusal)
        raise refusal from None
    except UiprobeError as exc:
        if _made_by_this_run(exc):
            exc.details.setdefault("out", out)
        if not record_text:
            _hide_device_text(exc)
        raise
    except Exception as exc:
        if record_text or isinstance(
            exc, _UsageError | AlsoTargetError | BrokenPipeError
        ):
            raise
        # Whatever an unexpected error says may hold text from the device.
        name = type(exc).__name__
        hidden = UiprobeError(
            f"unexpected {name} {_DEVICE_TEXT_HIDDEN}", details={"type": name}
        )
        if _made_by_this_run(exc):
            hidden.details["out"] = out
        raise hidden from None
    moves = result.repins  # as run.json counts them
    hints = _press_hints(
        result,
        package=args.package,
        back=policy.back_on_other_screens,
        file_says_back=setup.policy.back_on_other_screens,
    )
    if args.json:
        _emit(dumps(_press_json(plan, result, hints, moves), indent=2))
    else:
        _emit(_press_result_text(result, hints, moves))
    return 0


_DEVICE_TEXT_HIDDEN: Final = "(device text not shown: record_text = false)"
# Detail keys that carry text the device sent (a reply, a command's output, a
# window title, a line of a dumpsys listing, an error text it caused).
_DEVICE_TEXT_KEYS: Final = ("output", "message", "error", "window", "line", "title")


def _hide_device_text(error: UiprobeError) -> None:
    """Replace the device's own text in `error`, for a run that records no texts.

    The code, the exit status and uiprobe's own words stay; every value of a
    detail in `_DEVICE_TEXT_KEYS` is replaced, also where the message or
    another detail repeats it, by ``(device text not shown: record_text =
    false)``.
    """
    texts = sorted(
        {
            v
            for k in _DEVICE_TEXT_KEYS
            if isinstance(v := error.details.get(k), str) and v
        },
        key=len,
        reverse=True,
    )
    for text in texts:
        error.message = _DEVICE_TEXT_HIDDEN.join(error.message.split(text))
        for key, value in error.details.items():
            if isinstance(value, str):
                error.details[key] = _DEVICE_TEXT_HIDDEN.join(value.split(text))
    error.args = (error.message, *error.args[1:])


def _made_by_this_run(error: BaseException) -> bool:
    """Whether a failed run may name its run folder in the error.

    Only when `crawl_run` itself made the folder and wrote ``run-start.json``
    completely: it then sets ``_uiprobe_own_run_folder`` on the error it
    raises (see `uiprobe.crawl_driver._crawl_on`). Nothing is inferred from
    what is on the disk, which another program can change.
    """
    own = getattr(error, "_uiprobe_own_run_folder", False) is True
    # Read once: an error object raised again by a later run (a reused
    # KeyboardInterrupt, say) must not carry this run's folder into it.
    with contextlib.suppress(Exception):
        setattr(error, "_uiprobe_own_run_folder", False)  # noqa: B010
    return own


def _cli_not_emulator(device: Device, exc: NotEmulatorError) -> NotEmulatorError:
    """The refusal of a real device, in the command's words (same details)."""
    what = (
        "is not an emulator"
        if exc.details.get("reason") == "not_emulator"
        else "could not be shown to be an emulator (unreadable check)"
    )
    msg = (
        f"{_printable(str(device.serial))} {what}, so nothing was pressed"
        " and no folder was made; a pressing crawl presses for real: to run"
        " it on this device, add --allow-real-device"
    )
    return NotEmulatorError(msg, details=exc.details)


def _press_device_check(device: Device, *, allow_real_device: bool) -> bool | None:
    """Whether the device is an emulator (None: unreadable); refuse a real one.

    Without `allow_real_device`, a device that is not shown to be an emulator
    is refused before anything is written or sent, as `crawl_run` would.
    """
    if not allow_real_device:
        try:
            _check_device(device, allow_real_device=False)
        except NotEmulatorError as exc:
            raise _cli_not_emulator(device, exc) from None
        return True
    try:
        return device.is_emulator()
    except UiprobeError as exc:
        if exc.details.get("reason") in ("device_changed", PIN_WAIT_STOPPED):
            raise
        return None


def _press_plan_text(plan: dict[str, object], *, settings: str) -> str:
    device = plan["device"]
    assert isinstance(device, dict)  # noqa: S101 - built above
    kind = {True: "emulator", False: "real device"}.get(
        device["emulator"], "emulator check unreadable"
    )
    return "\n".join(
        [
            f"pressing on {_printable(str(device['serial']))} ({kind}):"
            f" {_printable(str(plan['package']))}",
            _press_because_text(plan, settings),
            _press_limits_text(plan),
            _press_out_text(plan, settings),
            "BACK on screens of other apps: " + _back_plan_text(plan),
            "check the window at each tap point: "
            + ("yes" if plan["check_tap_window"] else "no"),
            _RESTART_PLAN[bool(plan["restart"])],
            *_also_target_plan(plan),
            *_press_scroll_text(plan),
            *(
                []
                if plan.get("record_text", True)
                else [
                    "texts of the screens will not be recorded (password fields"
                    " are never recorded); the run folder holds no screen text"
                ]
            ),
        ]
    )


def _also_target_plan(plan: dict[str, object]) -> list[str]:
    """The plan line that names the listed packages; none with an empty list."""
    listed = plan.get("also_target")
    if not listed:
        return []
    assert isinstance(listed, list)  # noqa: S101 - built in _cmd_crawl_press
    names = ", ".join(_printable(str(name)) for name in listed)
    return [
        f"also pressing in: {names} (also_target in the settings file; system UI"
        " and the home app are refused; the controls of permission screens are"
        " never tapped, and they get BACK when BACK on other screens is on)"
    ]


def _press_scroll_text(plan: dict[str, object]) -> list[str]:
    """The plan line about scrolling lists (none for a plan without the key)."""
    if "scroll_lists" not in plan:
        return []
    if plan["scroll_lists"]:
        answer = (
            f"yes, at most {plan['scrolls_per_screen']} per screen,"
            f" wheel amount {plan['scroll_amount']}"
        )
    elif plan.get("scroll_input") is False:
        answer = "no (this device's input does not list the wheel scroll)"
    else:
        answer = "no"
    return [
        "scroll lists with the mouse wheel on screens with nothing left to press:"
        f" {answer}"
    ]


def _press_because_text(plan: dict[str, object], settings: str) -> str:
    """The plan line that says why the run presses."""
    if plan["press_source"] == "file":
        return f"pressing because: press = true in {_printable(_shown(settings))}"
    return "pressing because: --press"


def _press_out_text(plan: dict[str, object], settings: str) -> str:
    """The plan line with the run folder and where it came from."""
    source = (
        f"runs_dir in {_printable(_shown(settings))}"
        if plan["out_source"] == "file"
        else "--out"
    )
    return f"run folder: {_shown(str(plan['out']))} ({source})"


def _press_limits_text(plan: dict[str, object]) -> str:
    """The plan line with each limit and where it came from."""
    limits = plan["limits"]
    sources = plan["limits_source"]
    assert isinstance(limits, dict)  # noqa: S101 - built in _cmd_crawl_press
    assert isinstance(sources, dict)  # noqa: S101
    shown = ", ".join(
        f"{name} {limits[name]} ({_limit_source_text(name, sources[name])})"
        for name in _PRESS_LIMITS
    )
    return f"limits: {shown}"


# The plan line about the first action, by whether it force-stops the app.
_RESTART_PLAN: Final = {
    True: (
        "start: the app is stopped first (am force-stop), then its launcher"
        " activity is started; what it had not saved is lost (--no-restart"
        " starts from the screen it shows now)"
    ),
    False: (
        "start: the app is not stopped first; the crawl starts from the screen"
        " the app shows now"
    ),
}


# The package of the system's lock screen and notification shade.
_SYSTEM_UI: Final = "com.android.systemui"


def _other_apps_text(other_apps: tuple[tuple[str, int], ...]) -> str:
    """The packages the run landed on, each with its count, as one list."""
    return ", ".join(
        f"{_printable(name)} {'once' if count == 1 else f'{count} times'}"
        for name, count in other_apps
    )


def _press_hints(
    result: CrawlResult, *, package: str, back: bool, file_says_back: bool = False
) -> list[str]:
    """Advice for the endings a real device runs into; only those that apply.

    `package` is the target app and `back` whether BACK was allowed on
    screens of other apps. `file_says_back` is whether the settings file turns
    it on, so that a run with BACK off for this run only (by
    ``--no-back-on-other-screens``) is not told to set what is already set.
    """
    hints: list[str] = []
    refused = dict(result.not_sent)
    if refused.get("cover_unknown"):
        unknown = result.cover_unknown or "unknown"
        # Only a refusal the window list reader made has a rule to diagnose.
        diagnose = (
            f", rule {result.cover_rule}; uiprobe diagnose tap-window shows where"
            " without printing device values"
            if result.cover_rule is not None
            else ""
        )
        hints.append(
            f"the window at a tap point could not be read on this device ({unknown}"
            f"{diagnose}); --no-tap-window-check turns the check off, but then an"
            " overlay of another app over the point can receive a tap"
        )
    if not result.repin:
        hints.append(
            "the boot id of this device could not be read, so a renumbered"
            " adb connection ends the run instead of being followed, and so"
            " does any failed adb exchange with the device, even on the same"
            " connection"
        )
    front = result.last_foreground
    if (
        result.stop_reason == "relaunch_limit"
        and not back
        and front is not None
        and front != package
        and result.other_apps
    ):
        how = (
            "without --no-back-on-other-screens, back_on_other_screens = true in"
            " the settings file"
            if file_says_back
            else "--back-on-other-screens (or back_on_other_screens = true under"
            " [rules] in the settings file)"
        )
        hints.append(
            "the restarts kept ending on screens of other apps"
            f" ({_other_apps_text(result.other_apps)}); {how} presses BACK on"
            " such a screen (up to twice before it restarts the app), and that"
            " BACK reaches whichever window has the input focus, not the app"
            " being crawled"
        )
    if (
        result.stop_reason == "relaunch_limit"
        and front == package
        and result.last_focus == _SYSTEM_UI
    ):
        hints.append(
            f"the app was in front, but a window of {_SYSTEM_UI} had the input"
            " focus: if the screen is off or locked, turn it on and unlock it;"
            " if the notification shade or another System UI window is open,"
            " close it; then run again"
        )
    if result.stop_reason == "dump_failed_repeatedly":
        hints.append(
            "uiautomator dump kept failing; a screen that never settles (a value"
            " that changes every second, an animation) makes it fail, so avoid"
            f" that screen (app in front at the last read: {_printable(str(front))},"
            f" last screen read: {result.last_screen_id or 'none'})"
        )
    return hints


def _press_result_text(result: CrawlResult, hints: list[str], moves: int) -> str:
    refused = ", ".join(f"{reason} {count}" for reason, count in result.not_sent)
    lines = [
        f"stopped: {result.stop_reason}",
        f"actions: {result.actions} (started; those not sent included)",
        f"screens: {result.screens}",
        f"not sent: {refused or 'none'}",
        f"transport moves: {moves}",
        f"run folder: {_shown(str(result.out))}",
    ]
    lines.extend(f"note: {hint}" for hint in hints)
    return "\n".join(lines)


def _press_json(
    plan: dict[str, object], result: CrawlResult, hints: list[str], moves: int
) -> dict[str, object]:
    return {
        "schema": CRAWL_RUN_SCHEMA,
        **plan,
        "out": str(result.out),
        "stop_reason": result.stop_reason,
        "actions": result.actions,
        "screens": result.screens,
        "not_sent": dict(result.not_sent),
        "repins": moves,
        "repin": result.repin,
        "other_apps": [
            {"package": name, "count": count} for name, count in result.other_apps
        ],
        "notes": hints,
    }
