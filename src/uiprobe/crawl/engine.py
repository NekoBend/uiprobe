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
    MAX_REPLAY_FAILURES,
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
from uiprobe.guards import (
    _WORD_REASONS,
    MAX_ELEMENTS,
    PERMISSION_PACKAGES,
    Detail,
    check_also_target,
    judge,
)
from uiprobe.model import Screen  # noqa: TC001

if TYPE_CHECKING:
    from uiprobe.guards import Judgement, Policy
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
    replay: bool = False
    element_id: str | None = None
    bounds: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class _Plan:
    """The tap handed out last and what the element looked like."""

    tap: Tap
    element: Element
    below: tuple[Element, ...]
    shape: tuple[object, ...]
    labels: tuple[tuple[str, str, str], ...]
    # A tap that presses an element again to get back down (see `_Replay`).
    replay: bool = False
    # The badge-free (screen id, fingerprint) of the element, None with
    # `Policy.replay_path` off or on a screen that is not judged.
    bare: tuple[str, str] | None = None
    # Which marks this proposal added (a deadline cancel removes only those).
    new_full: bool = True
    new_bare: bool = False


@dataclass(frozen=True, slots=True)
class _Way:
    """A tap that led to a target screen one level deeper, as it was pressed.

    ``parent`` is the screen it was pressed on, ``from_root`` says that this was
    the root (path length 1). The element is remembered by its fingerprint, id,
    bounds and press point.
    """

    parent: str
    fingerprint: str
    element_id: str
    bounds: tuple[int, ...]
    point: tuple[int, int]
    from_root: bool


@dataclass(frozen=True, slots=True)
class _Replay:
    """A route back down to a goal screen after a relaunch.

    ``steps`` are the taps from the root, each a `_Way`; the first one was
    pressed on the root, which may have another id now. ``done`` counts the
    taps that landed where the route says. After step i the screen is the
    ``parent`` of step i + 1, after the last one the goal.
    """

    goal: str
    steps: tuple[_Way, ...]
    done: int = 0

    def expected_after(self, index: int) -> str:
        """The screen id that the tap of step ``index`` must land on."""
        if index + 1 == len(self.steps):
            return self.goal
        return self.steps[index + 1].parent


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
    bare_tried: set[tuple[str, str]]
    route: dict[str, _Way]
    untried: dict[str, int]
    prior: list[str]
    goal: str | None
    replay: _Replay | None
    replay_fails: dict[str, int]
    replay_dropped: set[str]
    replay_tap: Tap | None
    replay_ids: set[int]
    approved_bounds: tuple[int, ...] | None


class _BadgeFree:
    """A judged screen as it is without badges (``Policy.replay_path``).

    A badge is an element whose resource id contains ``badge`` (any case);
    the elements below one go with it. ``screen_id`` is the id of the screen
    without them, and `key_of` the (screen id, fingerprint) of an element
    computed without them, so a row that shows a badge inside it and the same
    row without one have the same key. Built only for a screen that is judged,
    and the elements are walked once.
    """

    def __init__(self, screen: Screen, prints: _Prints) -> None:
        badges = {e.id for e in screen.elements if "badge" in e.resource_id.lower()}
        self._kept: set[str] | None = None
        self._prints = prints
        self.screen_id = screen.screen_id
        if not badges:
            return
        children: dict[str | None, list[str]] = {}
        for element in screen.elements:
            children.setdefault(element.parent, []).append(element.id)
        stack = list(badges)
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in badges:
                    badges.add(child)
                    stack.append(child)
        kept = tuple(e for e in screen.elements if e.id not in badges)
        bare = replace(screen, elements=kept)
        self._kept = {e.id for e in kept}
        self._prints = _Prints(bare)
        self.screen_id = bare.screen_id

    def key_of(self, element: Element, fingerprint: str) -> tuple[str, str] | None:
        """The key of ``element`` (whose own fingerprint is ``fingerprint``), or
        None when the element is a badge or lies below one."""
        if self._kept is None:
            return (self.screen_id, fingerprint)
        if element.id not in self._kept:
            return None
        return (self.screen_id, self._prints.of(element))


def _checked(package: str, policy: Policy) -> Policy:
    """The target itself cannot be listed (the other static rules of
    ``Policy.also_target`` were checked when the policy was built)."""
    if policy.also_target:
        check_also_target(package, policy.also_target)
    return policy


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
            finite, or not below 2**40, or ``policy.also_target`` lists
            `package` itself (`AlsoTargetError`).
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
        self._policy = _checked(package, policy)
        # Packages that count as the target besides `package` (never launched).
        # A permission package never does: its screens stay off target (a
        # relaunch, as without the list), so nothing is sent to them either.
        self._also = frozenset(policy.also_target) - PERMISSION_PACKAGES
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
        # Path replay and the badge-free tried marks (`Policy.replay_path`).
        self._bare_tried: set[tuple[str, str]] = set()
        self._route: dict[str, _Way] = {}
        self._untried: dict[str, int] = {}
        self._prior: list[str] = []
        self._goal: str | None = None
        self._replay: _Replay | None = None
        self._replay_fails: dict[str, int] = {}
        self._replay_dropped: set[str] = set()
        self._replay_tap: Tap | None = None
        self._replay_ids: set[int] = set()
        self._approved_bounds: tuple[int, ...] | None = None
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
            bare_tried=set(self._bare_tried),
            route=dict(self._route),
            untried=dict(self._untried),
            prior=list(self._prior),
            goal=self._goal,
            replay=self._replay,
            replay_fails=dict(self._replay_fails),
            replay_dropped=set(self._replay_dropped),
            replay_tap=self._replay_tap,
            replay_ids=set(self._replay_ids),
            approved_bounds=self._approved_bounds,
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
        self._bare_tried = set(state.bare_tried)
        self._route = dict(state.route)
        self._untried = dict(state.untried)
        self._prior = list(state.prior)
        self._goal = state.goal
        self._replay = state.replay
        self._replay_fails = dict(state.replay_fails)
        self._replay_dropped = set(state.replay_dropped)
        self._replay_tap = state.replay_tap
        self._replay_ids = set(state.replay_ids)
        self._approved_bounds = state.approved_bounds

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
            # Only the marks this proposal added go; marks that were there
            # before it (a replay tap was tried earlier) stay.
            self._unmark_tried(tap.screen_id, tap.fingerprint, planned)
            return False
        if self._press_allowed(planned, observation):
            self._approved = tap
            self._approved_bounds = tuple(planned.element.bounds.to_list())
            return True
        self._mark_tried(tap.screen_id, tap.fingerprint, planned.bare)
        self._add_skip(tap.screen_id, tap.fingerprint, (CHANGED_BEFORE_PRESS,))
        if planned.replay:
            self._abandon_replay()
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
        replay = tap is self._replay_tap
        if replay:
            self._replay_ids.add(self._sent)
        self._pending = _Pending(
            self._sent,
            "tap",
            tap.screen_id,
            tap.fingerprint,
            tap.point,
            replay=replay,
            element_id=tap.element_id,
            bounds=self._approved_bounds,
        )
        self._approved_bounds = None
        self._relaunches = 0
        self._refused = 0
        self._label_run = 0
        self._landed.clear()

    @property
    def replay_actions(self) -> frozenset[int]:
        """The action ids of the taps that were path replay taps."""
        return frozenset(self._replay_ids)

    def is_replay(self, tap: Tap) -> bool:
        """Say whether `tap` is a path replay tap: one that presses an element
        again to get back down after a relaunch. The driver records it so. True
        only for the very `Tap` instance that `next` returned last as one."""
        return tap is self._replay_tap

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
        self._approved_bounds = None
        if unsent is not None and unsent is self._replay_tap:
            self._abandon_replay()
        self._replay_tap = None
        if unsent is not None:
            self._refused += 1
            self._add_skip(unsent.screen_id, unsent.fingerprint, (TAP_NOT_SENT,))

    def _is_target(self, screen: Screen, observation: Observation) -> bool:
        # The dump and the foreground both name the app: the target, or one of
        # the packages of `Policy.also_target`.
        return observation.foreground == screen.package and (
            screen.package == self._package or screen.package in self._also
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
        if pending is not None and kind == "tap":
            # A tap that stays on the screen it was pressed on pushes nothing.
            if not on_target or sid != pressed_on:
                self._path.append(sid if on_target else _OFF)
                if on_target and pressed_on is not None:
                    self._note_way(sid, pressed_on, pending)
            if pending.replay:
                self._replay_landed(sid, on_target=on_target)
        elif pending is not None and kind == "back":
            # Only a navigation BACK leaves the screen on top of the path; an
            # off-target BACK leaves the path alone (see _settle_after_dialogs).
            if pending.navigation and len(self._path) > 1:
                self._path.pop()
        elif kind in ("launch", "relaunch"):
            if self._path:
                self._prior = self._path
            self._path = []
            # A replay that a relaunch cuts short has failed once; the goal
            # stays until it is reached or dropped.
            self._abandon_replay()
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
            self._start_replay(sid)
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
        tap = self._next_tap(screen, sid, judgement, prints, refused_out=refused_out)
        if tap is not None:
            return tap
        if len(self._path) == 1:
            return self._finish(EXHAUSTED)
        return self._send_back(sid)

    def _next_tap(
        self,
        screen: Screen,
        sid: str,
        judgement: Judgement,
        prints: _Prints | None,
        *,
        refused_out: bool,
    ) -> Tap | None:
        """The next tap on this screen: the next step of a replay, else the
        first element not tried yet; None when there is none."""
        counting = self._policy.replay_path
        bare = _BadgeFree(screen, prints) if counting and prints is not None else None
        if self._replay is not None:
            replayed = self._replay_step(
                sid, judgement, prints, bare=bare, refused_out=refused_out
            )
            if replayed is not None:
                return replayed
        if counting:
            self._untried[sid] = 0
        chosen: _Plan | None = None
        others: set[str] = set()
        if (
            prints is not None
            and len(self._path) < self._limits.depth
            and not refused_out
        ):
            for decision in judgement.pressable:
                if decision.point is None:
                    continue
                fingerprint = prints.of(decision.element)
                key = (
                    None if bare is None else bare.key_of(decision.element, fingerprint)
                )
                if self._is_tried(sid, fingerprint, key):
                    continue
                shape = prints.shape(decision.element)
                if (sid, shape) in self._volatile:
                    continue
                if chosen is not None:
                    others.add(fingerprint)
                    continue
                chosen = _Plan(
                    Tap(sid, decision.element.id, fingerprint, decision.point),
                    decision.element,
                    prints.descendants(decision.element),
                    shape,
                    prints.labels(decision.element),
                    bare=key,
                )
                if not counting:
                    break
        if chosen is None:
            return None
        if counting:
            # The rows still untried here once this one is pressed: a
            # relaunch may go back down to this screen for them.
            others.discard(chosen.tap.fingerprint)
            self._untried[sid] = len(others)
        return self._send_tap(sid, chosen)

    def _send_tap(self, sid: str, plan: _Plan) -> Tap:
        tap = plan.tap
        # Nothing is counted and nothing is pending until tap_sent reports
        # that the tap was really sent.
        plan = replace(
            plan,
            new_full=(sid, tap.fingerprint) not in self._tried,
            new_bare=plan.bare is not None and plan.bare not in self._bare_tried,
        )
        self._mark_tried(sid, tap.fingerprint, plan.bare)
        self._planned = plan
        return tap

    # -- the badge-free tried marks (Policy.replay_path) ----------------------------
    # A row tried on a screen with a badge ("New") is the same row on that
    # screen without it, whose id differs. With the switch on, every mark is
    # also made under the badge-free key of the row (see `_BadgeFree`), and a
    # row counts as tried when either mark is there. With the switch off `bare`
    # is None, so only the full-id marks exist, as before. A separate switch for
    # this part would only have to change where `_next_tap` builds `_BadgeFree`.

    def _mark_tried(
        self, sid: str, fingerprint: str, bare: tuple[str, str] | None
    ) -> None:
        self._tried.add((sid, fingerprint))
        if bare is not None:
            self._bare_tried.add(bare)

    def _unmark_tried(self, sid: str, fingerprint: str, plan: _Plan) -> None:
        if plan.new_full:
            self._tried.discard((sid, fingerprint))
        if plan.new_bare and plan.bare is not None:
            self._bare_tried.discard(plan.bare)

    def _is_tried(
        self, sid: str, fingerprint: str, bare: tuple[str, str] | None
    ) -> bool:
        if (sid, fingerprint) in self._tried:
            return True
        if bare is not None and bare in self._bare_tried:
            # Skipped only because of the badge-free mark: keep the full one.
            self._tried.add((sid, fingerprint))
            return True
        return False

    # -- path replay (Policy.replay_path) -------------------------------------------
    # The principle: replay presses only (a) rows of the recorded route, each
    # verified step by step from the root, or (b) untried rows that may be
    # pressed. If (a) cannot be verified, the route is given up and the crawl
    # falls back to (b).

    def _note_way(self, sid: str, parent: str, pending: _Pending) -> None:
        """Remember the first tap that landed on target screen ``sid`` one level
        below ``parent``, with the element as it was pressed. A tap pressed on
        the root (path length 2 now, with the root at the bottom) says so; the
        root itself is never a child."""
        if not self._policy.replay_path:
            return
        if (
            pending.fingerprint is None
            or pending.element_id is None
            or pending.bounds is None
            or pending.point is None
        ):
            return
        if sid in self._route or sid in (self._root, parent):
            return
        self._route[sid] = _Way(
            parent,
            pending.fingerprint,
            pending.element_id,
            pending.bounds,
            pending.point,
            from_root=len(self._path) == 2 and self._path[0] == parent,
        )

    def _route_to(self, goal: str) -> tuple[_Way, ...] | None:
        """The taps from the root down to ``goal``, or None.

        Every link is verified: each screen of the chain has the tap that led
        to it from the screen before, and the first tap was pressed on the
        root. A chain that does not end at a root tap (unrooted), a loop and a
        route too long to press on at the goal give None.
        """
        ways: list[_Way] = []
        seen = {goal}
        here = goal
        while True:
            way = self._route.get(here)
            if way is None:
                return None  # unrooted: the chain breaks before a root tap
            ways.append(way)
            if way.from_root:
                break
            if way.parent in seen:
                return None
            seen.add(way.parent)
            here = way.parent
        # The goal is pressed on only while the path is shorter than the limit.
        if len(ways) + 1 >= self._limits.depth:
            return None
        ways.reverse()
        return tuple(ways)

    def _start_replay(self, root: str) -> None:
        """At a new root: choose a goal and the route down to it.

        A goal kept from an earlier replay that has not been reached or
        dropped comes first; else it is the deepest screen of the path just
        before the relaunch that still had a row to try. The root itself is
        never a goal: nothing needs replaying for it. A relaunch that lands
        directly on the goal (the new root has the goal's id) has reached it:
        the goal ends there and no route row is pressed.
        """
        if not self._policy.replay_path:
            return
        prior, self._prior = self._prior, []
        candidates = [] if self._goal is None else [self._goal]
        candidates.extend(reversed(prior[1:]))
        for sid in candidates:
            if sid in self._replay_dropped or self._untried.get(sid, 0) < 1:
                continue
            if sid == root:
                self._goal = None  # reached: go on with the untried rows
                return
            route = self._route_to(sid)
            if route is not None:
                self._goal = sid
                self._replay = _Replay(sid, route)
                return
        self._goal = None

    def _replay_step(
        self,
        sid: str,
        judgement: Judgement,
        prints: _Prints | None,
        *,
        bare: _BadgeFree | None,
        refused_out: bool,
    ) -> Tap | None:
        """The next tap of the route, or None after abandoning the replay.

        The element is found by its fingerprint among the elements that judge
        puts in ``pressable`` on this very screen: exactly one must match, at
        the recorded bounds and press point. The tap goes through `recheck`
        like every other.
        """
        replay = self._replay
        if replay is None:
            return None
        way = replay.steps[replay.done]
        before = None if replay.done == 0 else way.parent
        if (
            prints is None
            or refused_out
            or len(self._path) != replay.done + 1
            or len(self._path) >= self._limits.depth
            or (before is not None and sid != before)
        ):
            self._abandon_replay()
            return None
        found = [
            d
            for d in judgement.pressable
            if d.point is not None and prints.of(d.element) == way.fingerprint
        ]
        if len(found) == 1:
            decision = found[0]
            shape = prints.shape(decision.element)
            if (
                decision.point is not None
                and decision.point == way.point
                and tuple(decision.element.bounds.to_list()) == way.bounds
                and (sid, shape) not in self._volatile
            ):
                tap = self._send_tap(
                    sid,
                    _Plan(
                        Tap(sid, decision.element.id, way.fingerprint, decision.point),
                        decision.element,
                        prints.descendants(decision.element),
                        shape,
                        prints.labels(decision.element),
                        replay=True,
                        bare=None
                        if bare is None
                        else bare.key_of(decision.element, way.fingerprint),
                    ),
                )
                self._replay_tap = tap
                return tap
        self._abandon_replay()
        return None

    def _replay_landed(self, sid: str, *, on_target: bool) -> None:
        """A replay tap was sent and led to ``sid``: on the route, or not."""
        replay = self._replay
        if replay is None:
            return
        if not on_target or sid != replay.expected_after(replay.done):
            self._abandon_replay()
        elif replay.done + 1 == len(replay.steps):
            self._replay = None
            self._goal = None  # reached
        else:
            self._replay = replace(replay, done=replay.done + 1)

    def _abandon_replay(self) -> None:
        """Stop replaying; the crawl goes on as without a replay, from the
        screen it is on. The second failure for a goal drops it."""
        replay, self._replay = self._replay, None
        if replay is None:
            return
        failures = self._replay_fails.get(replay.goal, 0) + 1
        self._replay_fails[replay.goal] = failures
        if failures >= MAX_REPLAY_FAILURES:
            self._replay_dropped.add(replay.goal)
            self._goal = None

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
