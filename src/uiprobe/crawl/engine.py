"""The crawl engine: decides the next action from observations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from uiprobe.crawl.actions import (
    _ANCESTOR,
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
)
from uiprobe.crawl.prints import _ids_unique, _Prints

# Detail at runtime too, so that typing.get_type_hints(SkippedElement) works.
from uiprobe.guards import _WORD_REASONS, MAX_ELEMENTS, Detail, judge
from uiprobe.model import Screen  # noqa: TC001

if TYPE_CHECKING:
    from uiprobe.guards import Policy
    from uiprobe.model import Element


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
