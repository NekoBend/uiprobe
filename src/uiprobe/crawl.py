"""A pure engine that decides every step of a pressing crawl.

The engine is given what the caller saw (an `Observation`) and answers with the
next action. It sends nothing, reads no device, no clock and no file, so the
same observations always give the same actions. The driver that talks to the
device and the command that starts it are separate from this module. Nothing
in the command line calls the engine yet.

Two kinds of rule are kept apart here.

SAFETY rules are what the engine guarantees: a tap is sent only for an element
that judge put in ``pressable`` for that exact observation, at judge's point,
on a target screen; `CrawlEngine.recheck` refuses any change of that element
or its descendants (listed under Re-check), of its screen id or of judge's
result for it, a shown keyboard and the deadline; no input
other than tap, BACK, launch and relaunch exists; by default nothing but a
relaunch is sent while a screen of another app is in front (BACK there only
with ``Policy.back_on_other_screens``); the action and time limits
hold; there is no 4th relaunch without a tap the driver reported as sent
(`CrawlEngine.tap_sent`); the crawl always terminates; ``next``, ``recheck``
and ``tap_sent`` never raise on valid arguments.

BEST EFFORT is the estimate of the path (the depth of each path, and no
navigation BACK on the real root), and so whether the depth limit, which
holds per path, matches the real back stack. A BACK on the real root only
leaves the app, which the off-target handling recovers (by default with a
relaunch), so a wrong estimate costs actions, not safety. The edge cases
where screen ids change are listed under the stated limits below.

* Range. A screen is the target only when the package of the dump and the
  foreground package both equal the target package; an unknown foreground
  (None) is not the target. Off target the engine sends nothing to the screen
  in front: it relaunches the app (the driver force-stops it, then starts it).
  Only with ``Policy.back_on_other_screens`` True does it press BACK off
  target, and then it relaunches after 2 BACKs in a row without a target
  screen. After 3 relaunches in a row it stops with ``relaunch_limit``; the 4th
  relaunch is never sent. Relaunches count as in a row until the driver reports
  with `CrawlEngine.tap_sent` that a tap allowed by `CrawlEngine.recheck` was
  sent; seeing a target screen, a tap handed out but dropped, a refused recheck
  or an allowed recheck that is not reported does not reset the count, so a
  cycle of relaunches (or of BACK, BACK, Relaunch) with no sent tap in between
  ends with ``relaunch_limit``. A permission dialog belongs to another package,
  so it is off target and gets a relaunch (BACK with
  ``back_on_other_screens``); an app that shows one at every start therefore
  ends with ``relaunch_limit`` (see Pressing).
* Limits. Depth 5 screens, 300 actions and 30 minutes by default
  (`Limits`). Every input counts as an action: the first launch, taps, BACKs
  and relaunches; a re-read sends nothing and does not count. The time limit
  uses the ``now`` the caller puts in each observation (seconds, monotonic).
  The engine keeps the stack of screens of the current path, and a tap is
  planned only while that stack is shorter than the depth limit: the depth
  limit prevents another tap on the current path, it does not stop the crawl.
  On a screen at the depth limit nothing is pressed and the engine sends BACK,
  except on the root (path length 1, which is at the limit when the depth is
  1), where it stops with ``exhausted`` when candidate selection is reached
  (a shown keyboard gets BACK first). The whole crawl stops at the action
  or time limit, at an exhausted root, or for one of the other stop reasons.
* Sent taps. A `Tap` from ``next`` is only a proposal: it is counted as an
  action, recorded as a transition and allowed to move the path only when the
  driver reports it with `CrawlEngine.tap_sent` after an allowed
  `CrawlEngine.recheck`. When ``next`` is called with a proposal still awaiting
  its recheck, or an allowed tap still awaiting its send report (dropped, or
  failed to send), that tap is released: not counted, no transition, path
  unchanged; it stays marked tried and is recorded as skipped with the reason
  ``tap_not_sent`` and counts toward ``MAX_REFUSALS`` like a refused re-check,
  so a driver that never sends a tap still ends the crawl. Both `recheck` and
  `tap_sent` accept only the very `Tap` instance ``next`` returned, never an
  equal one.
* Pressing. The engine presses only an element that
  `uiprobe.guards.judge` put in ``pressable`` for that exact observation, at
  the point judge gave; judge runs on every target observation where the
  engine considers a tap (a shown keyboard gets BACK first, a due relaunch is
  sent first, and when no tap is allowed the root stops with ``exhausted``),
  and again in the re-check. The only
  actions are `Launch`, `Tap`, `Back`, `Relaunch`, `Reread` and `Stop`: no text
  input, long press, other key or swipe can be built from them.
* Keyboard. When the keyboard is shown on a target screen, press BACK once and
  read again; if it is still shown, stop with ``keyboard_stuck``. An unknown
  keyboard state (None) counts as not shown (judge already refuses text
  fields).
* Dump failures. An observation without a screen, or with a screen whose
  element ids are not unique (judge refuses such a hierarchy), asks for a
  re-read; 3 failed dumps in a row stop with ``dump_failed_repeatedly``.
* Clock. ``now`` and ``started_at`` must be numbers of seconds at least 0 and
  below 2**40, so the deadline arithmetic in floats stays exact to well under
  a millisecond; other values are refused when the `Observation` or the
  engine is built, like any other invalid observation.
* The root is the bottom of the path stack (path length 1) after a launch or a
  relaunch: the first target screen seen then becomes the root, whatever its
  id (only a launch or a relaunch makes a root, however the path was popped).
  The driver must make a Relaunch really reset the task (force-stop, then
  start), not bring an existing task to the front: the depth limit holds only
  when the new root is at the bottom of the real back stack. A navigation
  BACK is never pressed on the root: when it has nothing left to try the crawl
  stops with ``exhausted``, and only the root can end the
  crawl that way. A screen with the root's id higher up the path (reached again
  by a tap) is not the root: with nothing left to press, or at the depth limit,
  it gets BACK like any other screen; if that BACK leaves the app, the
  off-target handling (a relaunch, or BACK, BACK, Relaunch with
  ``back_on_other_screens``) recovers. The one BACK allowed on
  the root is the one that hides a shown keyboard, which is not navigation.
  The rest of this item matters only with ``back_on_other_screens``, the
  only case where BACK is sent off target.
  Only BACKs sent while off target count toward the 2 BACKs before a
  relaunch, and a target screen resets that count. One tap can open a target
  screen with dialogs over it, so the BACK itself never pops; the mark is
  settled when the target screen appears. The tap leaves a mark for the
  off-target landing; when a target screen appears after off-target BACKs with
  that mark on top of the path, the mark is removed if the screen below it has
  the same screen id (the dialogs were over the pressed screen), and otherwise
  it becomes the new screen at that path length, with its smallest known depth
  updated.
* A tap that lands on the screen id it was pressed on does not push the path:
  the screen did not change, so the depth stays.
* When ``next`` gets an observation and no input was sent since the previous
  one (after a refused recheck), the screen is the same frame read again: a
  target screen equal to the screen under the top of the path pops the top,
  and another one replaces the top in place (same length, the root stays the
  root). It is never an unpredicted landing.
* A keyboard BACK, and an off-target BACK with no off-target mark on top of the
  path, are not navigation. The target screen they land on is the same frame
  read again: if its id equals the one below the top of the path the top is
  popped, otherwise the top is replaced in place (path length and root kept,
  smallest known depth updated). It is never an unpredicted landing, except in
  the deferred case below, so a
  screen that changes its id when the keyboard goes away neither resyncs the
  path nor gets a BACK on the root. A navigation BACK that lands on a target
  screen with the keyboard shown, and not on the screen the path predicts, is
  not placed yet and its landing bookkeeping waits: the screen seen after the
  keyboard BACK is that navigation BACK's landing (predicted when it is the
  screen under the one left).
* A BACK that does nothing. The engine keeps the screen ids that navigation
  BACKs landed on since the last sent tap. When a navigation BACK lands on the
  screen id it was pressed on, or on one already in that set and not the screen
  the path predicts under it, the next action is a Relaunch (it counts toward
  the relaunch limit). The exception keeps a path that passes one screen id
  twice from being cut short while it unwinds. The keyboard BACK is not
  a navigation BACK and an off-target BACK is not either.
* Tried marks are kept by ``(screen_id, fingerprint)``, never by element id.
  The fingerprint (`fingerprint_of`) is built from the element's own class,
  resource id, text, content description and hint, then the class, resource id,
  text, content description and hint of its descendants in document order, and
  the (class, resource id) of its ancestors, with no position or bounds. A row
  whose label sits in a child view is told apart by that label. When two
  pressable elements of one screen share a fingerprint, only the first in
  document order is tried.
* Depth. The root has depth 1. A screen reached by a tap from a screen of path
  length d gets depth d + 1; the smallest known depth per screen id is kept,
  and `CrawlEngine.depth_of` reports it as information only: the path after a
  launch or relaunch starts at the first target screen seen, not at its known
  depth. A screen reached by BACK onto a place the path does not predict keeps
  its known depth, and one with no known depth gets the depth limit, so nothing
  is pressed there and the engine goes back.
* Re-check before pressing. The driver reads the screen again
  right before a tap and asks `CrawlEngine.recheck`. The press is allowed only
  when the fresh screen is the target with the same screen id and unique
  element ids, the clock of the fresh observation is before the deadline, the
  keyboard is not shown, and the element with the planned element id is in
  judge's ``pressable`` for the fresh screen at exactly the planned point, and
  that element and each of its descendants are equal in every field the
  `uiprobe.model.Element` holds (id, parent, class, bounds, text, resource id,
  content description, hint, package and flags such as checked, disabled,
  selected or focused), and its fingerprint is the same. Another element that
  looks the same never stands in for it. After an allowed re-check the driver
  sends the tap and then reports it with `CrawlEngine.tap_sent`. At or past
  the deadline the reserved tap is cancelled (not counted, not recorded, not
  marked tried) and the next call of ``next`` returns ``max_duration``.
  Otherwise nothing is sent, the
  element is recorded as skipped with the reason ``changed_before_press`` and
  marked tried, and the action is not counted. After 3 refusals in a row
  (``MAX_REFUSALS``), counted across all screens (for example a structure that
  changes on every read, even when two screens alternate), nothing more is
  pressed until an action is actually sent (a tap reported with
  `CrawlEngine.tap_sent`, a BACK, a relaunch); an allowed re-check whose tap is
  not reported does not reset it. The count is not reset by anything else, so
  a clock that does not move cannot keep it going: on the root the engine
  stops with ``exhausted``, elsewhere it sends BACK. Refusals because only
  labels changed (below) are not counted here.
* Volatile elements. When a refusal happens because only labels changed (the
  fresh screen has the same screen id and an element with the same classes and
  resource ids for itself, its descendants and its ancestors, and the same
  bounds, but other text, description or hint), that structure with its bounds
  is marked volatile for that screen id. Candidates with the same structure and
  bounds are skipped for the rest of the crawl, and the skip is recorded once
  with the reason ``label_changes``. Other elements are still tried; rows of a
  list have other bounds and stay distinct. These refusals do not count toward
  ``MAX_REFUSALS``; they have their own bound: at most ``MAX_VOLATILE`` (10)
  volatile keys per screen id, after which that screen has nothing left to
  press (BACK, or ``exhausted`` on the root). A second bound counts label-only
  refusals in a row across all screens, reset only when an action is sent (a
  tap reported with `CrawlEngine.tap_sent`, a BACK or a relaunch); at
  ``MAX_VOLATILE`` the current screen has nothing left to press either, so a
  screen id that changes on every read cannot keep the crawl going on a frozen
  clock.
* Order. Candidates are tried in document order, depth first: tap the first
  untried candidate, and when none is left press BACK (or stop with
  ``exhausted`` on the root).

Stated limits (known, not fixed here):

* Depth is the engine's estimate of the real back stack, made from the screens
  it sees. An app that pushes several screens for one tap, clears its stack, or
  opens a new screen with the same screen_id can make the real stack differ.
* After a tap that landed off target (the mark on the path) and off-target
  BACKs, a target screen with another id than the pressed one is taken as a
  new screen under the dialogs. If it is the pressed screen shown again with
  other or added elements (so another id), the engine's path is one longer
  than the real stack, and the root may then get a BACK; the engine cannot tell
  the two apart.
* A tap that changes the id of the current screen without navigating (a toggle
  or an expand that adds elements) is counted as a new screen, so the path
  estimate can grow and the real root can get a BACK.
* A refusal where a label and the element's own bounds or a descendant's
  bounds (or a flag such as disabled) change together is counted as a label
  change only when the element's own bounds are the same; flags and descendant
  bounds are not part of that comparison, so such an element can be marked
  volatile for the rest of the crawl.
* Tried marks are global per (screen_id, fingerprint). When the depth limit
  binds, a screen reached first at a deep position can use up a button that
  would lead further from a shallower position, so some screens within the
  limit can stay unvisited (more actions would be needed to retry).
* The driver must make Relaunch really reset the task (force-stop, then
  start); see the root rules above.
* By default a screen of another app gets a relaunch, which starts again at
  the root, and tried marks keep every pressed element from being pressed
  again. When a tap opens a dialog of another app, the rest of the screen it
  was pressed on stays unvisited if the way back to it goes through tried
  elements. With ``Policy.back_on_other_screens`` the dialog gets BACK and
  that screen is tried further.

How a driver uses it::

    engine = CrawlEngine(package, policy, Limits(), started_at=clock())
    action = engine.start()                      # a Launch; send it
    send(action)
    observation = observe()                      # dump, foreground, keyboard
    while True:
        action = engine.next(observation)
        if isinstance(action, Stop):
            break
        if isinstance(action, Tap):
            fresh = observe()
            if not engine.recheck(action, fresh):
                observation = fresh              # not sent; decide from it
                continue
        if not isinstance(action, Reread):
            send(action)
            if isinstance(action, Tap):
                engine.tap_sent(action)          # only once it was really sent
        observation = observe()

An action that the driver did not send for a reason that says nothing about
the screen (the device had to be proven again) is given back with
`CrawlEngine.withdraw`, which restores the engine's whole state from before
the `next` call that chose it.

Everything the driver may want to write down is in `CrawlEngine.records`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

# Detail at runtime too, so that typing.get_type_hints(SkippedElement) works.
from uiprobe.guards import _WORD_REASONS, MAX_ELEMENTS, Detail, judge
from uiprobe.model import Screen

if TYPE_CHECKING:
    from uiprobe.guards import Policy
    from uiprobe.model import Element

__all__ = [
    "BACK_LIMIT",
    "CHANGED_BEFORE_PRESS",
    "DUMP_FAILED_REPEATEDLY",
    "EXHAUSTED",
    "KEYBOARD_STUCK",
    "LABEL_CHANGES",
    "MAX_ACTIONS",
    "MAX_DEPTH",
    "MAX_DUMP_FAILURES",
    "MAX_DURATION",
    "MAX_REFUSALS",
    "MAX_RELAUNCHES",
    "MAX_VOLATILE",
    "RELAUNCH_LIMIT",
    "TAP_NOT_SENT",
    "Action",
    "Back",
    "CrawlEngine",
    "Launch",
    "Limits",
    "Observation",
    "Records",
    "Relaunch",
    "Reread",
    "SkippedElement",
    "Stop",
    "StopReason",
    "Tap",
    "Transition",
    "fingerprint_of",
]

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


def _part(value: str) -> str:
    return f"{len(value)}:{value}"


def _ids_unique(screen: Screen) -> bool:
    """Whether no two elements share an id (judge raises ValueError if not)."""
    elements = screen.elements
    return len({element.id for element in elements}) == len(elements)


class _Prints:
    """Fingerprints of the elements of one screen."""

    def __init__(self, screen: Screen) -> None:
        self._elements = screen.elements
        self._by_id = {element.id: element for element in screen.elements}
        self._chains: dict[str, tuple[Element, ...]] = {}
        self._below: dict[str, list[Element]] | None = None

    def _chain(self, element: Element) -> tuple[Element, ...]:
        cached = self._chains.get(element.id)
        if cached is not None:
            return cached
        found: list[Element] = []
        seen = {element.id}
        parent = element.parent
        while parent is not None and parent not in seen:
            seen.add(parent)
            ancestor = self._by_id.get(parent)
            if ancestor is None:
                break
            found.append(ancestor)
            parent = ancestor.parent
        chain = tuple(reversed(found))
        self._chains[element.id] = chain
        return chain

    def descendants(self, element: Element) -> tuple[Element, ...]:
        """The elements below ``element``, in document order."""
        if self._below is None:
            below: dict[str, list[Element]] = {}
            for other in self._elements:
                for ancestor in self._chain(other):
                    below.setdefault(ancestor.id, []).append(other)
            self._below = below
        return tuple(self._below.get(element.id, ()))

    def shape(self, element: Element) -> tuple[object, ...]:
        """What stays the same when only labels change: classes, resource ids of
        the element, its descendants and its ancestors, and the element's bounds.
        """
        return (
            element.class_name,
            element.resource_id,
            tuple(element.bounds.to_list()),
            tuple((d.class_name, d.resource_id) for d in self.descendants(element)),
            tuple((a.class_name, a.resource_id) for a in self._chain(element)),
        )

    def labels(self, element: Element) -> tuple[tuple[str, str, str], ...]:
        """Text, description and hint of the element and its descendants."""
        return tuple(
            (e.text, e.content_desc, e.hint)
            for e in (element, *self.descendants(element))
        )

    def of(self, element: Element) -> str:
        parts = [
            element.class_name,
            element.resource_id,
            element.text,
            element.content_desc,
            element.hint,
        ]
        below = self.descendants(element)
        parts.append(str(len(below)))
        for d in below:
            parts.extend((d.class_name, d.resource_id, d.text, d.content_desc, d.hint))
        chain = self._chain(element)
        parts.append(str(len(chain)))
        for ancestor in chain:
            parts.append(ancestor.class_name)
            parts.append(ancestor.resource_id)
        digest = hashlib.sha256(
            "|".join(_part(p) for p in parts).encode("utf-8", "replace")
        )
        return "f-" + digest.hexdigest()[:32]


def fingerprint_of(screen: Screen, element: Element) -> str:
    """Return the fingerprint of an element of the screen.

    It is built from the element's own class, resource id, text, content
    description and hint, then the class, resource id, text, content
    description and hint of its descendants in document order, then the (class,
    resource id) of its ancestors from the top down. No position or bounds go
    in, and neither does the element id, so it survives a list that scrolls or
    changes its element count.
    """
    return _Prints(screen).of(element)


@dataclass(slots=True)
class _Pending:
    """An action that was handed out and whose result has not been seen."""

    action_id: int
    kind: str
    from_screen_id: str | None
    fingerprint: str | None = None
    point: tuple[int, int] | None = None
    keyboard: bool = False
    navigation: bool = False
    recorded: bool = False


@dataclass(frozen=True, slots=True)
class _Plan:
    """The tap handed out last and what the element looked like."""

    tap: Tap
    element: Element
    below: tuple[Element, ...]
    shape: tuple[object, ...]
    labels: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class _State:
    """Every field of `CrawlEngine` that a decision can change, copied.

    `CrawlEngine.withdraw` puts all of them back; a test checks that no
    field of the engine is missing here.
    """

    started: bool
    stop: Stop | None
    sent: int
    backs: int
    relaunches: int
    failures: int
    start_id: str | None
    root: str | None
    depths: dict[str, int]
    tried: set[tuple[str, str]]
    skipped: dict[tuple[str, str, tuple[str, ...]], SkippedElement]
    transitions: list[Transition]
    seen: set[str]
    pending: _Pending | None
    planned: _Plan | None
    approved: Tap | None
    volatile: set[tuple[str, tuple[object, ...]]]
    expired: bool
    keyboard_back: bool
    path: list[str]
    refused: int
    landed: set[str]
    relaunch_due: bool
    deferred: str | None
    volatile_count: dict[str, int]
    label_run: int


class CrawlEngine:
    """Decides the next action of a pressing crawl from observations.

    Call `start` once, then `next` with each observation. A `Tap` must be
    passed to `recheck` (with a fresh observation) before it is sent; when
    `recheck` returns False the tap is not sent, and `next` is called with the
    fresh observation instead. When it returns True, send the tap and report
    it with `tap_sent`. After a `Stop`, `next` returns the same stop.

    Raises (on construction):
        TypeError: `package` is not a string, or `started_at` is not a number.
        ValueError: `package` is empty, or `started_at` is below 0, not
            finite, or not below 2**40.
    """

    def __init__(
        self,
        package: str,
        policy: Policy,
        limits: Limits,
        *,
        started_at: float,
    ) -> None:
        if not isinstance(package, str):
            msg = "package must be a string"
            raise TypeError(msg)
        if not package:
            msg = "package must not be empty"
            raise ValueError(msg)
        _check_clock("started_at", started_at)
        self._package = package
        self._policy = policy
        self._limits = limits
        self._started_at = started_at
        self._started = False
        self._stop: Stop | None = None
        self._sent = 0
        self._backs = 0
        self._relaunches = 0
        self._failures = 0
        self._start_id: str | None = None
        self._root: str | None = None
        self._depths: dict[str, int] = {}
        self._tried: set[tuple[str, str]] = set()
        self._skipped: dict[tuple[str, str, tuple[str, ...]], SkippedElement] = {}
        self._transitions: list[Transition] = []
        self._seen: set[str] = set()
        self._pending: _Pending | None = None
        self._planned: _Plan | None = None
        self._approved: Tap | None = None
        self._volatile: set[tuple[str, tuple[object, ...]]] = set()
        self._expired = False
        self._keyboard_back = False
        self._path: list[str] = []
        self._refused = 0
        self._landed: set[str] = set()
        self._relaunch_due = False
        self._deferred: str | None = None
        self._volatile_count: dict[str, int] = {}
        self._label_run = 0
        # The action `next` returned last, with the state from before that
        # call, while it can still be withdrawn.
        self._returned: tuple[Action, _State] | None = None

    # -- what the driver can read ------------------------------------------------

    @property
    def records(self) -> Records:
        """The transitions, skipped elements and counts so far."""
        return Records(
            transitions=tuple(self._transitions),
            skipped=tuple(self._skipped.values()),
            screens_seen=len(self._seen),
            actions_sent=self._sent,
        )

    @property
    def actions_sent(self) -> int:
        """The number of actions counted against the action limit."""
        return self._sent

    @property
    def start_screen_id(self) -> str | None:
        """The id of the start screen, None until a target screen was seen."""
        return self._start_id

    @property
    def stopped(self) -> Stop | None:
        """The stop that ended the crawl, or None while it goes on."""
        return self._stop

    def depth_of(self, screen_id: str) -> int | None:
        """The smallest known depth of a screen, None when it is unknown."""
        return self._depths.get(screen_id)

    # -- the steps ----------------------------------------------------------------

    def start(self) -> Launch:
        """Return the first action, a `Launch` (it counts as one action).

        Raises:
            RuntimeError: The engine was started before.
        """
        if self._started:
            msg = "the engine was started already"
            raise RuntimeError(msg)
        self._started = True
        self._sent = 1
        self._pending = _Pending(1, "launch", None)
        return Launch()

    def next(self, observation: Observation) -> Action:
        """Return the next action for what was just seen.

        The engine's whole state from before this call is kept with a
        returned `Tap`, `Back` or `Relaunch`, so that `withdraw` can undo the
        call when the action was not sent.

        Raises:
            RuntimeError: `start` was not called.
        """
        if not self._started:
            msg = "call start() before next()"
            raise RuntimeError(msg)
        if self._stop is not None:
            return self._stop
        before = self._snapshot()
        action = self._decide(observation)
        withdrawable = isinstance(action, Tap | Back | Relaunch)
        self._returned = (action, before) if withdrawable else None
        return action

    def withdraw(self, action: Action) -> None:
        """Undo the `next` call that returned `action`, which was not sent.

        Call it when the driver did not send `action` for a reason that
        says nothing about the screen (the device had to be proven again),
        passing the very instance `next` returned, before any other call of
        `next`. The engine goes back to its whole state from before that
        `next` call: nothing is counted, recorded, marked tried or skipped
        for it, and `next` with the same observation returns an equal
        action. A tap reported with `tap_sent` was sent and cannot be
        withdrawn.

        Raises:
            ValueError: `action` is not the action `next` returned last, or it
                was reported as sent, or `next` was called since.
        """
        returned = self._returned
        if returned is None or returned[0] is not action:
            msg = "action is not the unsent action that next() returned last"
            raise ValueError(msg)
        self._restore(returned[1])
        self._returned = None

    def _snapshot(self) -> _State:
        """Every field a decision can change, each container copied."""
        return _State(
            started=self._started,
            stop=self._stop,
            sent=self._sent,
            backs=self._backs,
            relaunches=self._relaunches,
            failures=self._failures,
            start_id=self._start_id,
            root=self._root,
            depths=dict(self._depths),
            tried=set(self._tried),
            skipped=dict(self._skipped),
            transitions=list(self._transitions),
            seen=set(self._seen),
            pending=None if self._pending is None else replace(self._pending),
            planned=self._planned,
            approved=self._approved,
            volatile=set(self._volatile),
            expired=self._expired,
            keyboard_back=self._keyboard_back,
            path=list(self._path),
            refused=self._refused,
            landed=set(self._landed),
            relaunch_due=self._relaunch_due,
            deferred=self._deferred,
            volatile_count=dict(self._volatile_count),
            label_run=self._label_run,
        )

    def _restore(self, state: _State) -> None:
        self._started = state.started
        self._stop = state.stop
        self._sent = state.sent
        self._backs = state.backs
        self._relaunches = state.relaunches
        self._failures = state.failures
        self._start_id = state.start_id
        self._root = state.root
        self._depths = dict(state.depths)
        self._tried = set(state.tried)
        self._skipped = dict(state.skipped)
        self._transitions = list(state.transitions)
        self._seen = set(state.seen)
        self._pending = None if state.pending is None else replace(state.pending)
        self._planned = state.planned
        self._approved = state.approved
        self._volatile = set(state.volatile)
        self._expired = state.expired
        self._keyboard_back = state.keyboard_back
        self._path = list(state.path)
        self._refused = state.refused
        self._landed = set(state.landed)
        self._relaunch_due = state.relaunch_due
        self._deferred = state.deferred
        self._volatile_count = dict(state.volatile_count)
        self._label_run = state.label_run

    def _decide(self, observation: Observation) -> Action:
        """The body of `next`, after its checks."""
        self._drop_unsent_tap()
        was_keyboard_back = self._keyboard_back
        screen = observation.screen
        if screen is not None and not _ids_unique(screen):
            # An invalid hierarchy (judge refuses it) counts as a failed dump.
            screen = None
        if screen is None:
            self._note_failed_dump()
        else:
            self._failures = 0
            self._keyboard_back = False
        sid = None if screen is None else screen.screen_id
        on_target = screen is not None and self._is_target(screen, observation)
        if sid is not None:
            self._note_screen(
                sid,
                on_target=on_target,
                keyboard=observation.keyboard_shown is True,
            )
        limit = self._limit_reached(observation.now)
        if limit is not None:
            return self._finish(limit)
        if screen is None or sid is None:
            return self._after_failed_dump()
        if not on_target:
            return self._off_target(sid)
        return self._on_target(screen, sid, observation, was_keyboard_back)

    def recheck(self, tap: Tap, observation: Observation) -> bool:
        """Say whether `tap` may be sent, judging the fresh observation.

        Call it right after `next` returned the tap, with a screen read just
        before pressing, passing the very `Tap` instance `next` returned (an
        equal tap from elsewhere is refused). True: send the tap, then call
        `tap_sent`; nothing is counted or reset until then. False: do not send
        it, nothing is counted, and the next step comes from `next`. There are
        two kinds
        of refusal. At or past the deadline the reserved tap is cancelled: it is
        not recorded as skipped and not marked tried, and `next` stops with
        ``max_duration``. Any other refusal records the element as skipped
        (``changed_before_press``) and marks it tried; when the structure of the
        element is unchanged and only labels changed, it is also marked volatile
        (see the module docstring).

        Raises:
            ValueError: `tap` is not the instance `next` returned last.
        """
        planned = self._planned
        if planned is None or planned.tap is not tap:
            msg = "tap is not the tap that next() returned last"
            raise ValueError(msg)
        self._planned = None
        if self._past_deadline(observation.now):
            # Time is up: the reserved tap is cancelled, not refused, and the
            # next call of next() stops the crawl.
            self._expired = True
            self._tried.discard((tap.screen_id, tap.fingerprint))
            return False
        if self._press_allowed(planned, observation):
            self._approved = tap
            return True
        self._tried.add((tap.screen_id, tap.fingerprint))
        self._add_skip(tap.screen_id, tap.fingerprint, (CHANGED_BEFORE_PRESS,))
        if self._only_labels_changed(planned, observation):
            # A label-only refusal has its own bound (MAX_VOLATILE per screen
            # id) and does not count toward MAX_REFUSALS.
            self._label_run += 1
            key = (tap.screen_id, planned.shape)
            if key not in self._volatile:
                self._volatile.add(key)
                self._volatile_count[tap.screen_id] = (
                    self._volatile_count.get(tap.screen_id, 0) + 1
                )
                self._add_skip(tap.screen_id, tap.fingerprint, (LABEL_CHANGES,))
        else:
            self._refused += 1
        return False

    def tap_sent(self, tap: Tap) -> None:
        """Note that `tap` was really sent to the device.

        Call it once, right after sending a tap that `recheck` allowed and
        before the next observation, passing the very `Tap` instance `next`
        returned (an equal tap from elsewhere is refused). Only this counts the
        tap as an action, records its transition (settled by the next
        observation), lets it move the path, and ends the runs of relaunches,
        refused re-checks and label-only refusals and the watch for BACKs that
        go round in circles. A tap that was dropped, refused or failed to send
        must not be reported here; `next` releases it.

        Raises:
            ValueError: `tap` is not the instance `recheck` allowed last, or it
                was reported already, or `next` was called since.
        """
        if self._approved is None or self._approved is not tap:
            msg = "tap is not the tap that recheck() allowed last"
            raise ValueError(msg)
        self._approved = None
        self._returned = None  # sent: it can no longer be withdrawn
        self._sent += 1
        self._pending = _Pending(
            self._sent, "tap", tap.screen_id, tap.fingerprint, tap.point
        )
        self._relaunches = 0
        self._refused = 0
        self._label_run = 0
        self._landed.clear()

    # -- internals ----------------------------------------------------------------

    def _drop_unsent_tap(self) -> None:
        # A tap handed out by next() (rechecked or not) that was never reported
        # with tap_sent was not sent: it is not counted, has no transition and
        # leaves the path alone. It stays marked tried, so it is not picked
        # again forever, and it is recorded as skipped with TAP_NOT_SENT. It
        # counts toward MAX_REFUSALS, so a driver that never sends cannot keep
        # the crawl going without counted actions.
        unsent = self._approved
        if unsent is None and self._planned is not None:
            unsent = self._planned.tap
        self._planned = None
        self._approved = None
        if unsent is not None:
            self._refused += 1
            self._add_skip(unsent.screen_id, unsent.fingerprint, (TAP_NOT_SENT,))

    def _is_target(self, screen: Screen, observation: Observation) -> bool:
        return (
            screen.package == self._package and observation.foreground == self._package
        )

    def _past_deadline(self, now: float) -> bool:
        return now - self._started_at >= self._limits.minutes * _SECONDS_PER_MINUTE

    def _limit_reached(self, now: float) -> StopReason | None:
        if self._sent >= self._limits.actions:
            return MAX_ACTIONS
        if self._expired or self._past_deadline(now):
            return MAX_DURATION
        return None

    def _after_failed_dump(self) -> Action:
        if self._failures >= MAX_DUMP_FAILURES:
            return self._finish(DUMP_FAILED_REPEATEDLY)
        return Reread()

    def _finish(self, reason: StopReason) -> Stop:
        self._stop = Stop(reason)
        return self._stop

    def _note_failed_dump(self) -> None:
        self._failures += 1
        pending = self._pending
        if pending is not None and not pending.recorded:
            self._record(pending, None, dump_failed=True)

    def _record(self, pending: _Pending, to: str | None, *, dump_failed: bool) -> None:
        pending.recorded = True
        self._transitions.append(
            Transition(
                action_id=pending.action_id,
                kind=pending.kind,
                from_screen_id=pending.from_screen_id,
                to_screen_id=to,
                fingerprint=pending.fingerprint,
                point=pending.point,
                dump_failed=dump_failed,
            )
        )

    def _note_screen(self, sid: str, *, on_target: bool, keyboard: bool) -> None:
        deferred, self._deferred = self._deferred, None
        pending = self._pending
        if pending is not None and not pending.recorded:
            self._record(pending, sid, dump_failed=False)
        self._pending = None
        kind = None if pending is None else pending.kind
        pressed_on = None if pending is None else pending.from_screen_id
        if kind == "tap":
            # A tap that stays on the screen it was pressed on pushes nothing.
            if not on_target or sid != pressed_on:
                self._path.append(sid if on_target else _OFF)
        elif pending is not None and kind == "back":
            # Only a navigation BACK leaves the screen on top of the path; an
            # off-target BACK leaves the path alone (see _settle_after_dialogs).
            if pending.navigation and len(self._path) > 1:
                self._path.pop()
        elif kind in ("launch", "relaunch"):
            self._path = []
        if not on_target:
            return
        self._backs = 0
        self._seen.add(sid)
        if pending is None and self._path:
            # Nothing was sent since the last observation (a refused recheck):
            # this is the same frame read again, so a changed screen replaces
            # the top of the path in place and is never an unpredicted landing.
            self._same_frame(sid, may_pop=True)
            return
        if (
            pending is not None
            and kind == "back"
            and self._after_back(sid, pending, deferred, keyboard=keyboard)
        ):
            return
        self._place(sid, tapped=kind == "tap")

    def _after_back(
        self,
        sid: str,
        pending: _Pending,
        deferred: str | None,
        *,
        keyboard: bool,
    ) -> bool:
        """Settle a target screen seen after a BACK; True when it is done."""
        if pending.navigation:
            if keyboard and not (self._path and self._path[-1] == sid):
                # The landing shows the keyboard and is not the predicted
                # screen: the keyboard may have changed what is shown. Wait for
                # the screen after the keyboard BACK; that one is the landing.
                self._deferred = pending.from_screen_id
                return True
            self._note_landing(sid, pending.from_screen_id)
            return False
        if deferred is not None and pending.keyboard:
            self._note_landing(sid, deferred)
            self._place(sid, tapped=False)
            return True
        # An off-target BACK is not navigation either.
        if self._path and self._path[-1] != _OFF:
            self._same_frame(sid, may_pop=True)
            return True
        self._settle_after_dialogs(sid)
        return False

    def _same_frame(self, sid: str, *, may_pop: bool) -> None:
        """Take a target screen as the frame on top of the path, read again.

        With ``may_pop`` and the screen below the top having this id, the top
        was left: pop it. Otherwise the top is replaced in place (path length
        and root kept) and the smallest known depth is updated.
        """
        path = self._path
        if path[-1] == sid:
            pass
        elif may_pop and len(path) > 1 and path[-2] == sid:
            path.pop()
        else:
            path[-1] = sid
            if len(path) == 1:
                # The root replaced in place stays the root.
                self._root = sid
        depth = len(path)
        known = self._depths.get(sid)
        self._depths[sid] = depth if known is None else min(known, depth)

    def _settle_after_dialogs(self, sid: str) -> None:
        """Place a target screen that appeared after off-target BACKs.

        A tap that lands off target leaves a mark on the path, and an off-target
        BACK does not remove it, because one tap can open a target screen with
        dialogs over it. When a target screen shows up with that mark on top
        of the path, the mark is settled here. If the screen below the mark has
        the same id, the dialogs were over the screen that was pressed, so the
        mark goes away. Otherwise the mark becomes the new screen at that path
        length, and the smallest known depth of the screen is updated.
        """
        path = self._path
        if not path or path[-1] != _OFF:
            return
        if len(path) > 1 and path[-2] == sid:
            path.pop()
            return
        path[-1] = sid
        depth = len(path)
        known = self._depths.get(sid)
        self._depths[sid] = depth if known is None else min(known, depth)

    def _note_landing(self, sid: str, pressed_on: str | None) -> None:
        # A landing the path predicts (the screen under the one left) is a
        # normal step back, even when the same screen id was landed on before:
        # a cycle in the path repeats ids without going round.
        predicted = bool(self._path) and self._path[-1] == sid
        if sid == pressed_on or (sid in self._landed and not predicted):
            self._relaunch_due = True
        self._landed.add(sid)

    def _place(self, sid: str, *, tapped: bool) -> None:
        if not self._path:
            # The first target screen after a launch or relaunch is the root,
            # whatever its id and whatever depth it was known at. Nothing else
            # makes a root: an empty path elsewhere is an unpredicted landing.
            if self._start_id is None:
                self._start_id = sid
            self._depths[sid] = 1
            self._root = sid
            self._path = [sid]
        elif tapped:
            depth = len(self._path)
            known = self._depths.get(sid)
            self._depths[sid] = depth if known is None else min(known, depth)
        elif self._path[-1] != sid:
            # Reached by BACK onto a screen the path does not predict: its known
            # depth (or the limit) is the path length.
            if sid not in self._path:
                depth = self._depths.setdefault(sid, self._limits.depth)
                if sid != self._root:
                    # Only a launch or relaunch makes a root: a former root
                    # reached by BACK sits above the real root.
                    depth = max(depth, 2)
                self._path = [_ANCESTOR] * (depth - 1) + [sid]
            else:
                end = len(self._path) - self._path[::-1].index(sid)
                self._path = self._path[:end]

    def _send_back(
        self,
        sid: str,
        *,
        off_target: bool = False,
        keyboard: bool = False,
    ) -> Back:
        self._sent += 1
        if off_target:
            self._backs += 1
        self._refused = 0
        self._label_run = 0
        self._pending = _Pending(
            self._sent,
            "back",
            sid,
            keyboard=keyboard,
            navigation=not (off_target or keyboard),
        )
        return Back()

    def _off_target(self, sid: str) -> Action:
        # By default nothing goes to a screen of another app: a BACK would
        # reach the owner of the focused window, whichever app that is. The
        # relaunch force-stops and starts the target app only.
        if self._policy.back_on_other_screens and self._backs < BACK_LIMIT:
            return self._send_back(sid, off_target=True)
        return self._relaunch(sid)

    def _relaunch(self, sid: str) -> Action:
        if self._relaunches >= MAX_RELAUNCHES:
            return self._finish(RELAUNCH_LIMIT)
        self._relaunches += 1
        self._backs = 0
        self._refused = 0
        self._label_run = 0
        self._relaunch_due = False
        self._sent += 1
        self._pending = _Pending(self._sent, "relaunch", sid)
        return Relaunch()

    def _on_target(
        self,
        screen: Screen,
        sid: str,
        observation: Observation,
        was_keyboard_back: bool,  # noqa: FBT001
    ) -> Action:
        if observation.keyboard_shown is True and was_keyboard_back:
            return self._finish(KEYBOARD_STUCK)
        if self._relaunch_due:
            return self._relaunch(sid)
        if observation.keyboard_shown is True:
            self._keyboard_back = True
            return self._send_back(sid, keyboard=True)
        judgement = judge(
            screen, self._package, self._policy, foreground=observation.foreground
        )
        prints: _Prints | None = None
        if len(screen.elements) > MAX_ELEMENTS:
            # An oversized screen is not judged: one row for the whole screen,
            # with no fingerprint index built over its elements.
            self._add_skip(sid, sid, _TOO_LARGE)
        else:
            prints = _Prints(screen)
            for decision in judgement.skipped:
                self._add_skip(
                    sid,
                    prints.of(decision.element),
                    tuple(decision.reasons),
                    tuple(d for d in decision.details if d.reason in _WORD_REASONS),
                )
        refused_out = (
            self._refused >= MAX_REFUSALS
            or self._volatile_count.get(sid, 0) >= MAX_VOLATILE
            or self._label_run >= MAX_VOLATILE
        )
        if (
            prints is not None
            and len(self._path) < self._limits.depth
            and not refused_out
        ):
            for decision in judgement.pressable:
                if decision.point is None:
                    continue
                fingerprint = prints.of(decision.element)
                if (sid, fingerprint) in self._tried:
                    continue
                shape = prints.shape(decision.element)
                if (sid, shape) in self._volatile:
                    continue
                return self._send_tap(
                    sid,
                    _Plan(
                        Tap(sid, decision.element.id, fingerprint, decision.point),
                        decision.element,
                        prints.descendants(decision.element),
                        shape,
                        prints.labels(decision.element),
                    ),
                )
        if len(self._path) == 1:
            return self._finish(EXHAUSTED)
        return self._send_back(sid)

    def _send_tap(self, sid: str, plan: _Plan) -> Tap:
        tap = plan.tap
        # Nothing is counted and nothing is pending until tap_sent reports
        # that the tap was really sent.
        self._tried.add((sid, tap.fingerprint))
        self._planned = plan
        return tap

    def _add_skip(
        self,
        sid: str,
        fingerprint: str,
        reasons: tuple[str, ...],
        details: tuple[Detail, ...] = (),
    ) -> None:
        key = (sid, fingerprint, reasons)
        if key not in self._skipped:
            self._skipped[key] = SkippedElement(sid, fingerprint, reasons, details)

    def _only_labels_changed(self, plan: _Plan, observation: Observation) -> bool:
        """Whether the fresh screen has the planned element's structure and
        bounds with other labels, and nothing with the planned labels there."""
        screen = observation.screen
        if screen is None or not _ids_unique(screen):
            return False
        if not self._is_target(screen, observation):
            return False
        if observation.keyboard_shown is True:
            return False
        if screen.screen_id != plan.tap.screen_id:
            return False
        prints = _Prints(screen)
        changed = False
        for element in screen.elements:
            if prints.shape(element) != plan.shape:
                continue
            if prints.labels(element) == plan.labels:
                return False
            changed = True
        return changed

    def _press_allowed(self, plan: _Plan, observation: Observation) -> bool:
        """Whether the fresh screen still has the planned element, unchanged,
        pressable at the planned point."""
        tap = plan.tap
        screen = observation.screen
        if screen is None or not _ids_unique(screen):
            return False
        if not self._is_target(screen, observation):
            return False
        if observation.keyboard_shown is True:
            return False
        if screen.screen_id != tap.screen_id:
            return False
        judgement = judge(
            screen, self._package, self._policy, foreground=observation.foreground
        )
        # The planned element itself (by its id), not another one that looks
        # the same: judge must still put it in pressable at the same point, and
        # every field of it and of its descendants must be unchanged.
        for decision in judgement.pressable:
            fresh = decision.element
            if fresh.id != tap.element_id:
                continue
            prints = _Prints(screen)
            return (
                decision.point == tap.point
                and fresh == plan.element
                and prints.descendants(fresh) == plan.below
                and prints.of(fresh) == tap.fingerprint
            )
        return False
