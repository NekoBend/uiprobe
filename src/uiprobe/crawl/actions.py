"""The actions, observations and limits of a crawl, and its stop reasons."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Detail at runtime too, so that typing.get_type_hints(SkippedElement) works.
from uiprobe.guards import Detail  # noqa: TC001
from uiprobe.model import Screen

# A stop reason is a plain string so the driver can add its own (``interrupted``
# and ``error``); the engine produces only the six below.
StopReason = str

EXHAUSTED: Final = "exhausted"
MAX_ACTIONS: Final = "max_actions"
MAX_DURATION: Final = "max_duration"
RELAUNCH_LIMIT: Final = "relaunch_limit"
KEYBOARD_STUCK: Final = "keyboard_stuck"
DUMP_FAILED_REPEATEDLY: Final = "dump_failed_repeatedly"

# The reason recorded when a re-check refuses a press.
CHANGED_BEFORE_PRESS: Final = "changed_before_press"
# The reason recorded once for an element whose labels change on every read.
LABEL_CHANGES: Final = "label_changes"
# The reason recorded for a tap that next() handed out and the driver never
# reported as sent with `CrawlEngine.tap_sent`.
TAP_NOT_SENT: Final = "tap_not_sent"

# BACKs in a row off target before the app is relaunched (only with
# Policy.back_on_other_screens); relaunches in a row
# without a sent tap before the crawl stops; failed dumps in a row before
# the crawl stops.
BACK_LIMIT: Final = 2
MAX_RELAUNCHES: Final = 3
# The largest depth limit `Limits` accepts; it bounds the placeholder path an
# unpredicted BACK landing builds.
MAX_DEPTH: Final = 100
MAX_DUMP_FAILURES: Final = 3
# Refused re-checks in a row (all screens) before nothing more is pressed until
# an action is sent.
MAX_REFUSALS: Final = 3
# Volatile keys (elements whose labels change on every read) per screen id
# before that screen counts as having nothing left to press.
MAX_VOLATILE: Final = 10
# Path entries that are not a known target screen. Screen ids never hold a NUL.
# _OFF marks the landing of a tap or a dialog that is off target (settled when a
# target screen appears); _ANCESTOR stands for an unknown screen below the one
# on top (filled in by a resync to a known depth or to the limit).
_OFF: Final = "\x00off"
_ANCESTOR: Final = "\x00ancestor"
_TOO_LARGE: Final = ("screen_too_large",)

_SECONDS_PER_MINUTE: Final = 60
# Clock values must be below this (about 34 800 years in seconds), so a float
# holds them to well under a millisecond and the deadline arithmetic is sound.
_MAX_CLOCK: Final = 2**40


def _check_count(name: str, value: object, most: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an int, not {type(value).__name__}"
        raise TypeError(msg)
    if value < 1:
        msg = f"{name} must be at least 1, got {value}"
        raise ValueError(msg)
    if most is not None and value > most:
        msg = f"{name} must be at most {most}, got {value}"
        raise ValueError(msg)


def _check_clock(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{name} must be a number of seconds, not {type(value).__name__}"
        raise TypeError(msg)
    # NaN fails both comparisons; infinity and huge ints fail the second.
    if not 0 <= value < _MAX_CLOCK:
        msg = f"{name} must be at least 0 and below 2**40 seconds"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Limits:
    """The stop limits: screens deep on one path, actions sent, minutes.

    Raises (on construction):
        TypeError: a value is not an int.
        ValueError: a value is below 1, or `depth` is above `MAX_DEPTH`.
    """

    depth: int = 5
    actions: int = 300
    minutes: int = 30

    def __post_init__(self) -> None:
        _check_count("depth", self.depth, MAX_DEPTH)
        _check_count("actions", self.actions)
        _check_count("minutes", self.minutes)


@dataclass(frozen=True, slots=True)
class Observation:
    """What the caller saw after the last action.

    ``screen`` is None for a failed dump. ``foreground`` is the foreground
    package, None when unknown. ``keyboard_shown`` is None when unknown. ``now``
    is a monotonic clock in seconds, on the same clock as ``started_at``, at
    least 0 and below 2**40.

    Raises (on construction):
        TypeError: a field has the wrong type.
        ValueError: ``now`` is below 0, not finite, or not below 2**40.
    """

    screen: Screen | None
    foreground: str | None
    keyboard_shown: bool | None
    now: float

    def __post_init__(self) -> None:
        if self.screen is not None and not isinstance(self.screen, Screen):
            msg = "screen must be a Screen or None"
            raise TypeError(msg)
        if self.foreground is not None and not isinstance(self.foreground, str):
            msg = "foreground must be a string or None"
            raise TypeError(msg)
        if self.keyboard_shown is not None and not isinstance(
            self.keyboard_shown, bool
        ):
            msg = "keyboard_shown must be a bool or None"
            raise TypeError(msg)
        _check_clock("now", self.now)


@dataclass(frozen=True, slots=True)
class Launch:
    """Start the target app (only the first action)."""


@dataclass(frozen=True, slots=True)
class Tap:
    """Press ``point`` (x, y), the press point judge gave for the element."""

    screen_id: str
    element_id: str
    fingerprint: str
    point: tuple[int, int]

    def __post_init__(self) -> None:
        named = (
            ("screen_id", self.screen_id),
            ("element_id", self.element_id),
            ("fingerprint", self.fingerprint),
        )
        for name, value in named:
            if not isinstance(value, str) or not value:
                msg = f"{name} must be a non-empty string"
                raise TypeError(msg)
        point = self.point
        if (
            not isinstance(point, tuple)
            or len(point) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in point)
        ):
            msg = "point must be a tuple of two ints"
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class Back:
    """Press the BACK key."""


@dataclass(frozen=True, slots=True)
class Relaunch:
    """Start the target app again."""


@dataclass(frozen=True, slots=True)
class Reread:
    """Read the screen again; nothing is sent and no action is counted."""


@dataclass(frozen=True, slots=True)
class Stop:
    """End the crawl; ``reason`` is one of the stop reasons."""

    reason: StopReason

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            msg = "reason must be a non-empty string"
            raise TypeError(msg)


Action = Launch | Tap | Back | Relaunch | Reread | Stop


@dataclass(frozen=True, slots=True)
class Transition:
    """One action that was sent and where it led.

    ``action_id`` counts every sent action from 1 (the launch is 1). ``kind`` is
    ``launch``, ``tap``, ``back`` or ``relaunch``. ``from_screen_id`` is the
    screen the action was decided on (None for the launch). ``fingerprint`` and
    ``point`` are set for taps. ``to_screen_id`` is the first screen seen
    afterwards, or None when that dump failed (``dump_failed`` is then True).
    When the next action is withdrawn (`CrawlEngine.withdraw`), the screen
    seen before it does not count: ``to_screen_id`` is the screen of the
    fresh observation that `next` gets after the withdrawal (a driver's own
    record may keep the earlier one).
    """

    action_id: int
    kind: str
    from_screen_id: str | None
    to_screen_id: str | None
    fingerprint: str | None = None
    point: tuple[int, int] | None = None
    dump_failed: bool = False


@dataclass(frozen=True, slots=True)
class SkippedElement:
    """An element that was not pressed, and why (reasons of judge,
    ``changed_before_press``, ``label_changes`` or ``tap_not_sent``). A screen
    judge does not judge (``screen_too_large``) has one row for the whole
    screen, with the screen id as its fingerprint. ``details`` are the
    `Detail` objects of judge's decision for ``deny_word``,
    ``irreversible_screen`` and ``confirm_button`` (the matched word or
    phrase), from the first element seen with this fingerprint and these
    reasons; empty for every other reason, so such a record equals the one
    built from the first three fields alone."""

    screen_id: str
    fingerprint: str
    reasons: tuple[str, ...]
    details: tuple[Detail, ...] = ()


@dataclass(frozen=True, slots=True)
class Records:
    """What the engine has seen so far, as plain data.

    ``transitions`` are in the order the actions were sent; ``skipped`` has one
    entry per ``(screen_id, fingerprint, reasons)``, in the order first seen;
    ``screens_seen`` counts distinct target screens; ``actions_sent`` counts
    every sent action.
    """

    transitions: tuple[Transition, ...]
    skipped: tuple[SkippedElement, ...]
    screens_seen: int
    actions_sent: int
