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
  foreground package both equal the target package, or both equal one package
  of ``Policy.also_target`` (never a permission package, which stays off
  target); an unknown foreground (None) is not the target. A listed package's
  screens are target screens in every respect (judged with every guard, depth
  and tried marks, the path and BACK); the engine never launches it, a
  relaunch always starts the target app. Off target the engine sends nothing to
  the screen in front: it relaunches the app (the driver force-stops it, then
  starts it).
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
* Path replay (``Policy.replay_path``, on by default). A relaunch loses the
  path. The engine remembers, for each target screen reached by a sent tap
  one level below another, the first tap that led there (the parent's id, and
  the element's fingerprint, id, bounds and press point). The principle:
  replay presses only (a) rows of the recorded route, each verified step by
  step from the root, or (b) untried rows that may be pressed; if (a) cannot
  be verified, the route is given up and the crawl falls back to (b). A
  route is used only when its first tap was pressed on the root and every
  link is recorded (an unrooted or broken chain sets no goal). When a new
  root is seen after a relaunch, the
  goal is the deepest screen of the path just before it that still had an
  untried pressable element (a goal kept from a failed replay comes first),
  and the engine returns ordinary `Tap` actions for the recorded elements,
  each found by its fingerprint among the elements judge puts in
  ``pressable`` now, where exactly one must match, at the recorded bounds and
  press point: on the root whatever its id, then on each recorded child by its
  recorded id. These taps are rechecked, gated, sent, counted and
  limited like every tap (`CrawlEngine.is_replay` tells the driver, which
  records ``replay: true``). A replay is abandoned when the screen is not the
  one the route expects, the element is missing, not pressable or refused by
  the recheck, a tap lands elsewhere, the depth limit would be passed or the
  refusal bounds are reached; the crawl then goes on from the screen it is
  on, and a goal whose replay failed twice is dropped (a relaunch that cuts a
  replay short counts as one failure). The same switch makes the "already
  tried" marks also under a badge-free key, a screen id and a row fingerprint
  both computed without the elements whose resource id contains ``badge`` and
  what is below them (inside the row too), so a row tried with a badge is not
  pressed again without it; a screen that is not judged has no such key. The
  relaunch limit is unchanged.
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

A driver that lists packages in ``Policy.also_target`` must also read the
device's home app and refuse it (`uiprobe.also_target.check_home`), as
`uiprobe.crawl_driver.crawl` does; the engine has no device. The static rules
(system UI, fixed home apps, the target itself, format, duplicates) are checked
when the `Policy` and the `CrawlEngine` are built.

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

import hashlib  # noqa: F401
from dataclasses import dataclass, replace  # noqa: F401
from typing import TYPE_CHECKING, Final  # noqa: F401

from uiprobe.crawl.actions import (  # noqa: F401
    _ANCESTOR,
    _MAX_CLOCK,
    _OFF,
    _SECONDS_PER_MINUTE,
    _TOO_LARGE,
    BACK_LIMIT,
    CHANGED_BEFORE_PRESS,
    DUMP_FAILED_REPEATEDLY,
    EXHAUSTED,
    KEYBOARD_STUCK,
    LABEL_CHANGES,
    MAX_ACTIONS,
    MAX_DEPTH,
    MAX_DUMP_FAILURES,
    MAX_DURATION,
    MAX_REFUSALS,
    MAX_RELAUNCHES,
    MAX_VOLATILE,
    RELAUNCH_LIMIT,
    TAP_NOT_SENT,
    Action,
    Back,
    Launch,
    Limits,
    Observation,
    Records,
    Relaunch,
    Reread,
    SkippedElement,
    Stop,
    StopReason,
    Tap,
    Transition,
    _check_clock,
    _check_count,
)
from uiprobe.crawl.engine import (  # noqa: F401
    CrawlEngine,
    _Pending,
    _Plan,
    _State,
)
from uiprobe.crawl.prints import (  # noqa: F401
    _ids_unique,
    _part,
    _Prints,
    fingerprint_of,
)
from uiprobe.guards import _WORD_REASONS, MAX_ELEMENTS, Detail, judge  # noqa: F401
from uiprobe.model import Screen  # noqa: F401

if TYPE_CHECKING:
    from uiprobe.guards import Policy  # noqa: F401
    from uiprobe.model import Element  # noqa: F401

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
