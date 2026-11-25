"""Small helpers the crawl driver shares: policy parsing, windows, element records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from uiprobe.crawl import Back, Launch, Observation, Relaunch, Tap, _Prints
from uiprobe.crawl_driver.constants import _FIRST_APP_UID, _MAX_SECONDS, INTERRUPTED
from uiprobe.errors import DeviceNotFoundError, LaunchFailedError, UiprobeError
from uiprobe.guards import Policy, judge
from uiprobe.model import Bounds

if TYPE_CHECKING:
    from uiprobe.crawl import Action, SkippedElement
    from uiprobe.device import FocusedWindow
    from uiprobe.inputwindows import InputWindow
    from uiprobe.model import Element, Screen
    from uiprobe.packageuid import PackageUid


class _Ended(Exception):  # noqa: N818 - a signal inside the driver, not an error
    """The run ends before a screen is read: `stop` returned True after the
    pin check of an observation (``interrupted``), or uiprobe's own clock
    passed the run's time (``max_duration``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
