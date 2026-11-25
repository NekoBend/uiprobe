"""Tests for the crawl engine on random graphs and random observations."""

from __future__ import annotations

import random

from tests.crawl.helpers import (
    LAUNCHER,
    OTHER,
    PERMISSION,
    PKG,
    POLICY,
    Node,
    Phone,
    back_engine,
    check_tap,
    drive,
    engine,
    make_screen,
    random_graph,
    reachable_targets,
    seen,
    small_graph,
    started,
)
from uiprobe import crawl
from uiprobe.crawl import (
    Back,
    Launch,
    Limits,
    Observation,
    Relaunch,
    Reread,
    Stop,
    Tap,
)


def test_random_graphs_keep_every_rule_and_terminate() -> None:
    rng = random.Random(20261118)  # noqa: S311
    for _ in range(400):
        nodes = random_graph(rng)
        limits = Limits(
            depth=rng.randint(1, 6),
            actions=rng.randint(1, 80),
            minutes=rng.choice([1, 2, 30]),
        )
        phone = Phone(nodes)
        eng = engine(limits)
        log = drive(eng, phone, depth=limits.depth)
        stop = log[-1]
        assert isinstance(stop, Stop)
        assert stop.reason in {
            "exhausted",
            "max_actions",
            "max_duration",
            "relaunch_limit",
        }
        sent = [a for a in log if not isinstance(a, Stop)]
        assert len(sent) == eng.actions_sent <= limits.actions
        assert isinstance(sent[0], Launch)
        assert len([a for a in sent if isinstance(a, Launch)]) == 1
        assert not any(isinstance(a, Reread) for a in log)
        for action in sent:
            if isinstance(action, Tap):
                depth = eng.depth_of(action.screen_id)
                assert depth is not None
                assert depth < limits.depth  # nothing is pressed at the limit
        for transition in eng.records.transitions:
            if transition.kind == "tap":
                assert transition.fingerprint is not None
                assert transition.point is not None
        # no two taps of one (screen, fingerprint)
        keys = [
            (t.from_screen_id, t.fingerprint)
            for t in eng.records.transitions
            if t.kind == "tap"
            and t.action_id not in eng.replay_actions  # a replay presses again
        ]
        assert len(keys) == len(set(keys))
        for node in nodes:
            depth = eng.depth_of(phone.screen_id(node.name))
            assert depth is None or 1 <= depth <= limits.depth


class DialogPhone(Phone):
    """A phone that puts 0 to 2 permission dialogs over each screen a tap opens."""

    def __init__(self, nodes, rng):
        super().__init__(nodes)
        self.rng = rng

    def send(self, action):
        before = len(self.stack)
        super().send(action)
        top = self.current()
        if (
            isinstance(action, Tap)
            and top is not None
            and top.package == PKG
            and len(self.stack) > before
        ):
            self.stack += ("D2", "D1")[2 - self.rng.randint(0, 2) :]
            self.visited[-1] = self.stack[-1]


def test_random_graphs_with_dialogs_keep_the_engine_path_equal_to_the_stack() -> None:
    rng = random.Random(4242)  # noqa: S311
    for _ in range(300):
        nodes = [
            *random_graph(rng),
            Node("D1", (("Allow", "n0"),), package=PERMISSION),
            Node("D2", (("Allow", "n0"),), package=PERMISSION),
        ]
        limits = Limits(depth=rng.randint(1, 6), actions=rng.randint(10, 120))
        phone = DialogPhone(nodes, random.Random(rng.random()))  # noqa: S311
        eng = engine(limits)

        def watch(observation, eng=eng, phone=phone):
            if observation.foreground == PKG and observation.screen is not None:
                assert len(eng._path) == len(phone.stack)

        log = drive(eng, phone, depth=limits.depth, watch=watch)
        assert isinstance(log[-1], Stop)


def test_random_graphs_with_room_visit_everything_reachable() -> None:
    rng = random.Random(7)  # noqa: S311
    for _ in range(300):
        nodes = random_graph(rng, links_to_start=rng.random() < 0.5)
        phone = Phone(nodes)
        eng = back_engine(Limits(depth=40, actions=600, minutes=30))
        log = drive(eng, phone, depth=40)
        assert not any(isinstance(a, Relaunch) for a in log)
        assert log[-1] == Stop("exhausted")
        assert {"n0", *phone.visited} >= reachable_targets(nodes)


def test_random_graphs_give_the_same_actions_twice() -> None:
    rng = random.Random(99)  # noqa: S311
    for _ in range(100):
        nodes = random_graph(rng)
        limits = Limits(depth=rng.randint(1, 6), actions=rng.randint(1, 80))
        assert drive(engine(limits), Phone(nodes)) == drive(
            engine(limits), Phone(nodes)
        )


def test_the_same_observations_replayed_give_the_same_actions() -> None:
    phone = Phone(small_graph())
    first = engine()
    seen_observations = []
    actions = [first.start()]
    phone.send(actions[0])
    while not isinstance(actions[-1], Stop):
        observation = phone.observe()
        seen_observations.append(observation)
        action = first.next(observation)
        if isinstance(action, Tap):
            assert first.recheck(action, observation)
            first.tap_sent(action)
        actions.append(action)
        if not isinstance(action, Stop):
            phone.send(action)
    second = engine()
    replay = [second.start()]
    for observation in seen_observations:
        action = second.next(observation)
        if isinstance(action, Tap):
            assert second.recheck(action, observation)
            second.tap_sent(action)
        replay.append(action)
    assert replay == actions
    assert second.records == first.records


POOL_NAMES = ("a", "b", "c", "d")


def _fuzz_observation(rng, now):
    roll = rng.random()
    keyboard = rng.choice([None, False, False, True])
    if roll < 0.1:
        return Observation(None, rng.choice([PKG, None]), keyboard, now)
    if roll < 0.3:
        package = rng.choice([LAUNCHER, PERMISSION, OTHER])
        return seen(
            make_screen(["Apps"], name="home", package=package),
            now=now,
            fg=rng.choice([package, None]),
            keyboard=keyboard,
        )
    labels = [f"L{rng.randint(0, 2)}" for _ in range(rng.randint(0, 3))]
    return seen(
        make_screen(labels, name=rng.choice(POOL_NAMES)),
        now=now,
        fg=rng.choice([PKG, PKG, PKG, None, OTHER]),
        keyboard=keyboard,
    )


class _ModelStack:
    """The test's own idea of the real back stack, built from the actions it
    sends and the observations it feeds, never from the engine's path.

    ``stack`` is None after a landing the model cannot place (a navigation BACK
    onto a screen that is not below the top); a relaunch makes it known again.
    """

    OFF = "off"

    def __init__(self) -> None:
        self.stack: list[str] | None = []
        self.last: tuple[str, str | None, bool] | None = ("launch", None, False)

    def sent(self, kind, pressed_on=None, *, nav=False):
        self.last = (kind, pressed_on, nav)

    def observe(self, observation):
        screen = observation.screen
        if screen is None:
            return
        sid = screen.screen_id
        on_target = screen.package == PKG and observation.foreground == PKG
        last, self.last = self.last, None
        kind, pressed, nav = last if last is not None else (None, None, False)
        if kind in ("launch", "relaunch"):
            self.stack = []
        stack = self.stack
        if stack is None:
            return
        if kind == "tap":
            if not on_target or sid != pressed:
                stack.append(sid if on_target else self.OFF)
        elif kind == "back" and nav and len(stack) > 1:
            stack.pop()
        if on_target:
            self._land(stack, sid, kind, nav=nav)

    def _land(self, stack, sid, kind, *, nav):
        if not stack:
            stack.append(sid)
        elif kind is None or (kind == "back" and not nav):
            # the same frame read again (after a refusal, or a BACK that is not
            # navigation): the screen below the top means the top was left
            below = len(stack) > 1 and stack[-2] == sid
            if stack[-1] != sid and kind is not None and below:
                stack.pop()
            elif stack[-1] != sid:
                stack[-1] = sid
        elif kind != "tap" and stack[-1] != sid:
            if sid in stack:
                del stack[len(stack) - stack[::-1].index(sid) :]
            else:
                self.stack = None


def _report_or_drop(eng, tap, model, drops):
    """Report the allowed tap as sent, or (one time in five) drop it."""
    if drops.random() < 0.8:
        eng.tap_sent(tap)
        model.sent("tap", tap.screen_id)
        return True
    return False


def test_random_observation_sequences_never_raise_and_keep_the_rules() -> None:
    rng = random.Random(7_061_118)  # noqa: S311
    # A separate stream decides which allowed taps the driver drops, so the
    # observations drawn from ``rng`` do not shift.
    drops = random.Random(5_310_007)  # noqa: S311
    for _ in range(600):
        limits = Limits(
            depth=rng.randint(1, 5),
            actions=rng.randint(1, 60),
            minutes=rng.choice([1, 30]),
        )
        eng = started(limits)
        model = _ModelStack()
        now = 0.0
        relaunches = 0
        stopped = False
        pending = None
        for _step in range(400):
            now += rng.choice([0.0, 1.0, 5.0, 20.0])
            # the driver feeds the fresh screen of a refused recheck back
            observation, pending = pending or _fuzz_observation(rng, now), None
            action = eng.next(observation)
            model.observe(observation)
            if isinstance(action, Stop):
                stopped = True
                break
            if isinstance(action, Relaunch):
                relaunches += 1
                assert relaunches <= crawl.MAX_RELAUNCHES
                model.sent("relaunch")
            if isinstance(action, Back):
                screen = observation.screen
                on_target = (
                    screen is not None
                    and screen.package == PKG
                    and observation.foreground == PKG
                )
                navigation = on_target and observation.keyboard_shown is not True
                if navigation and model.stack is not None:
                    assert len(model.stack) > 1
                model.sent("back", nav=navigation)
            if isinstance(action, Tap):
                check_tap(action, observation, POLICY)
                assert len(eng._path) < limits.depth
                fresh = (
                    observation
                    if rng.random() < 0.5
                    else _fuzz_observation(rng, now + rng.choice([0.0, 30.0]))
                )
                allowed = eng.recheck(action, fresh)
                if allowed:
                    assert fresh.screen is not None
                    check_tap(action, fresh, POLICY)
                    assert fresh.keyboard_shown is not True
                    assert fresh.now - 0.0 < limits.minutes * 60
                    sent = _report_or_drop(eng, action, model, drops)
                    relaunches = 0 if sent else relaunches
                else:
                    assert eng.actions_sent <= limits.actions
                    if fresh.now >= limits.minutes * 60:
                        assert eng.next(fresh) == Stop("max_duration")
                        stopped = True
                        break
                    pending = fresh
                    continue
            assert eng.actions_sent <= limits.actions
        assert stopped
        assert eng.actions_sent <= limits.actions
