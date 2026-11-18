"""Tests for the pure crawl engine (in-memory screen graphs, no device, no clock)."""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest

from uiprobe import crawl
from uiprobe.crawl import (
    Action,
    Back,
    CrawlEngine,
    Launch,
    Limits,
    Observation,
    Relaunch,
    Reread,
    SkippedElement,
    Stop,
    Tap,
    fingerprint_of,
)
from uiprobe.guards import Policy, judge
from uiprobe.model import Bounds, Element, Screen

PKG = "com.example.shop"
OTHER = "com.example.other"
PERMISSION = "com.google.android.permissioncontroller"
LAUNCHER = "com.example.launcher"
DISPLAY = (320, 640)
BUTTON = "android.widget.Button"
ROW = "android.widget.TextView"
POLICY = Policy()
# The classes as untyped callables, to pass arguments of a wrong type on purpose.
LooseLimits: Any = Limits
LooseObservation: Any = Observation
LooseTap: Any = Tap
CRAWL_PY = Path(crawl.__file__)


# -- building screens ----------------------------------------------------------


def button(
    ident,
    label,
    index,
    *,
    parent="e1",
    rid="",
    flags=("clickable",),
    bounds=None,
    package=PKG,
    cls=BUTTON,
):
    top = 60 + index * 60
    return Element(
        ident,
        parent,
        cls,
        Bounds(*(bounds or (40, top, 280, top + 40))),
        text=label,
        resource_id=rid or f"{package}:id/btn_{index}",
        package=package,
        flags=tuple(flags),
    )


def make_screen(
    labels, *, name="main", package=PKG, rid="", cls=BUTTON, display=DISPLAY
):
    root = Element(
        "e1",
        None,
        "android.widget.FrameLayout",
        Bounds(0, 0, *display),
        package=package,
    )
    elements = [root]
    for index, label in enumerate(labels):
        elements.append(
            button(f"e{index + 2}", label, index, package=package, rid=rid, cls=cls)
        )
    return Screen(
        serial="emulator-5554",
        captured_at=datetime(2026, 11, 18, tzinfo=UTC),
        package=package,
        activity=f".{name}",
        api_level=36,
        display=display,
        elements=tuple(elements),
    )


def clone(element, **changes):
    values = {f.name: getattr(element, f.name) for f in fields(Element)}
    values.update(changes)
    return Element(**values)


def seen(screen, *, now=0.0, fg=PKG, keyboard=False):
    return Observation(screen, fg, keyboard, now)


def on_screen(labels, *, name="main", now=0.0, keyboard=False, **kw):
    return seen(make_screen(labels, name=name, **kw), now=now, keyboard=keyboard)


def off_screen(*, now=0.0, package=LAUNCHER):
    return seen(
        make_screen(["Apps"], name="home", package=package), now=now, fg=package
    )


def engine(limits=None, *, started_at=0.0, policy=POLICY):
    return CrawlEngine(PKG, policy, limits or Limits(), started_at=started_at)


def started(limits=None, *, started_at=0.0, policy=POLICY):
    eng = engine(limits, started_at=started_at, policy=policy)
    assert eng.start() == Launch()
    return eng


# -- a phone made of a graph ---------------------------------------------------


@dataclass(frozen=True)
class Node:
    name: str
    buttons: tuple[tuple[str, str], ...] = ()  # (label, destination name)
    package: str = PKG


class Phone:
    """A back stack over a graph of screens; the launcher is off target."""

    def __init__(self, nodes, start="n0"):
        self.nodes = {node.name: node for node in nodes}
        self.start = start
        self.stack: list[str] = []
        self.visited: list[str] = []
        self.clock = 0.0
        self.keyboard = False

    def screen_id(self, name):
        node = self.nodes[name]
        labels = [label for label, _ in node.buttons]
        return make_screen(labels, name=name, package=node.package).screen_id

    def current(self):
        return self.nodes[self.stack[-1]] if self.stack else None

    def observe(self, *, dump_ok=True):
        node = self.current()
        if node is None:
            return seen(
                make_screen(["Apps"], name="home", package=LAUNCHER),
                now=self.clock,
                fg=LAUNCHER,
            )
        if not dump_ok:
            return Observation(None, node.package, keyboard_shown=False, now=self.clock)
        screen = make_screen(
            [label for label, _ in node.buttons], name=node.name, package=node.package
        )
        return seen(screen, now=self.clock, fg=node.package, keyboard=self.keyboard)

    def send(self, action):
        self.clock += 1.0
        if isinstance(action, Launch | Relaunch):
            self.stack = [self.start]
        elif isinstance(action, Back):
            if self.stack:
                self.stack.pop()
        elif isinstance(action, Tap):
            node = self.current()
            assert node is not None
            x, y = action.point
            for index, (_, dest) in enumerate(node.buttons):
                top = 60 + index * 60
                if 40 <= x < 280 and top <= y < top + 40:
                    if dest != node.name:  # a tap that stays pushes nothing
                        self.stack.append(dest)
                    break
            else:
                msg = f"tap at {action.point} hit no button"
                raise AssertionError(msg)
        else:
            msg = f"cannot send {action!r}"
            raise AssertionError(msg)
        node = self.current()
        self.visited.append(node.name if node else "launcher")


def drive(eng, phone, *, policy=POLICY, max_steps=3000, depth=None, watch=None):
    """Run the engine against the phone; check every tap; return the actions.

    ``watch(observation)`` is called after the engine has taken each
    observation, before its action is used.
    """
    log: list[Action] = []
    action: Action = eng.start()
    log.append(action)
    phone.send(action)
    for _ in range(max_steps):
        observation = phone.observe()
        action = eng.next(observation)
        if watch is not None:
            watch(observation)
        if isinstance(action, Stop):
            log.append(action)
            return log
        assert not isinstance(action, Reread)
        if isinstance(action, Tap):
            check_tap(action, observation, policy)
            if depth is not None:
                # the real back stack of the phone, not the engine's own count
                assert len(phone.stack) < depth
            fresh = phone.observe()
            assert eng.recheck(action, fresh)
        if isinstance(action, Back):
            node = phone.current()
            if node is not None and node.package == PKG and not phone.keyboard:
                # a navigation BACK is never pressed on the root of the stack
                assert len(phone.stack) > 1
        log.append(action)
        phone.send(action)
    msg = "the crawl did not stop"
    raise AssertionError(msg)


def check_tap(tap, observation, policy):
    screen = observation.screen
    assert screen is not None
    assert screen.package == PKG
    assert observation.foreground == PKG
    judgement = judge(screen, PKG, policy, foreground=observation.foreground)
    matching = [
        d
        for d in judgement.pressable
        if d.element.id == tap.element_id and d.point == tap.point
    ]
    assert len(matching) == 1
    assert tap.screen_id == screen.screen_id
    assert tap.fingerprint == fingerprint_of(screen, matching[0].element)


def small_graph():
    return [
        Node("n0", (("Item a", "n1"), ("Item b", "n2"))),
        Node("n1", (("Item a", "n3"),)),
        Node("n2", (("Item a", "n3"), ("Item b", "perm"))),
        Node("n3", (("Item a", "n1"), ("Item b", "n3"))),
        Node("perm", (("Allow", "n0"),), package=PERMISSION),
    ]


# -- exploring -----------------------------------------------------------------


def test_visits_every_reachable_screen_and_ends_exhausted_on_the_start() -> None:
    phone = Phone(small_graph())
    eng = engine()
    log = drive(eng, phone)
    assert log[-1] == Stop("exhausted")
    assert {"n0", "n1", "n2", "n3", "perm"} <= set(phone.visited) | {"n0"}
    records = eng.records
    assert records.screens_seen == 4  # the permission screen is another package
    assert records.actions_sent == len(log) - 1
    start = eng.start_screen_id
    assert start is not None
    backs = [t for t in records.transitions if t.kind == "back"]
    assert backs
    assert all(t.from_screen_id != start for t in backs)
    assert all(t.to_screen_id is not None for t in records.transitions)
    assert [t.action_id for t in records.transitions] == list(
        range(1, records.actions_sent + 1)
    )
    assert records.transitions[0].kind == "launch"
    assert records.transitions[0].from_screen_id is None


def test_tap_follows_document_order_and_goes_depth_first() -> None:
    phone = Phone(small_graph())
    log = drive(engine(), phone)
    taps = [a for a in log if isinstance(a, Tap)]
    assert taps[0].element_id == "e2"  # the first button of the start screen
    assert phone.visited[:3] == ["n0", "n1", "n3"]  # down the first branch first


def test_never_taps_a_permission_screen_and_presses_back_there() -> None:
    phone = Phone(small_graph())
    log = drive(engine(), phone)
    assert "perm" in phone.visited
    i = phone.visited.index("perm")  # visited[k] is the result of log[k]
    assert isinstance(log[i + 1], Back)


def test_the_tap_point_is_the_one_judge_gave_not_the_centre_of_the_bounds() -> None:
    eng = started()
    narrow = on_screen(["Item a"], display=(200, 640))
    tap = eng.next(narrow)
    assert isinstance(tap, Tap)
    assert narrow.screen is not None
    judgement = judge(narrow.screen, PKG, POLICY, foreground=PKG)
    assert tap.point == judgement.pressable[0].point == (120, 80)
    assert tap.point != (160, 80)  # the centre of the bounds (40, 60, 280, 100)


def test_start_screen_with_nothing_to_try_is_exhausted_without_back() -> None:
    eng = started()
    assert eng.next(on_screen([])) == Stop("exhausted")
    assert eng.next(on_screen(["Item a"])) == Stop("exhausted")  # stays stopped
    assert eng.records.actions_sent == 1


def test_a_relaunch_that_lands_elsewhere_makes_that_screen_the_root() -> None:
    eng = started()
    assert isinstance(eng.next(on_screen(["Item a"], name="n0")), Tap)
    alt = on_screen(["Item b", "Item c"], name="alt")
    assert isinstance(eng.next(alt), Tap)  # alt is known at depth 2
    assert eng.depth_of(alt.screen.screen_id) == 2
    for expected in (Back(), Back(), Relaunch()):
        assert eng.next(off_screen()) == expected
    # the relaunch lands on alt: it is the root now, whatever depth it had
    second = eng.next(alt)
    assert isinstance(second, Tap)
    assert second.screen_id == alt.screen.screen_id
    assert eng.depth_of(alt.screen.screen_id) == 1
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(alt) == Stop("exhausted")  # the root: no BACK, no loop
    assert eng.records.actions_sent == 1 + 1 + 1 + 3 + 1 + 1


def test_a_relaunch_landing_on_a_different_screen_in_a_phone_does_not_loop() -> None:
    class Elsewhere(Phone):
        def send(self, action):
            super().send(action)
            if isinstance(action, Relaunch):
                self.stack = ["alt"]
                self.visited[-1] = "alt"

    nodes = [
        Node("n0", (("Item a", "trap"),)),
        Node("trap"),
        Node("alt", (("Item a", "leaf"),)),
        Node("leaf"),
    ]

    class Leaving(Elsewhere):
        def send(self, action):
            before = list(self.stack)
            super().send(action)
            if isinstance(action, Back) and before == ["n0", "trap"]:
                self.stack = []  # this BACK leaves the app
                self.visited[-1] = "launcher"

    phone = Leaving(nodes)
    log = drive(engine(), phone)
    assert log[-1] == Stop("exhausted")
    assert "leaf" in phone.visited
    assert len(log) < 20


def test_start_screen_is_the_first_target_screen_after_off_target() -> None:
    eng = started()
    assert isinstance(eng.next(off_screen()), Back)
    first = eng.next(on_screen(["Item a"], name="late"))
    assert isinstance(first, Tap)
    assert eng.depth_of(first.screen_id) == 1
    assert eng.start_screen_id == first.screen_id


# -- range ---------------------------------------------------------------------


def test_off_target_is_back_back_relaunch_then_relaunch_limit() -> None:
    eng = started()
    actions = [eng.next(off_screen()) for _ in range(3 * 3 + 3)]
    expected = [Back(), Back(), Relaunch()] * 3 + [
        Back(),
        Back(),
        Stop("relaunch_limit"),
    ]
    assert actions == expected
    assert eng.records.actions_sent == 1 + 3 * 3 + 2
    assert eng.next(off_screen()) == Stop("relaunch_limit")  # stays stopped


def test_a_sent_tap_resets_the_relaunch_count() -> None:
    eng = started()
    relaunches = 0
    for _ in range(2):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
        relaunches += 1
    tap = eng.next(on_screen(["Item a"]))
    assert isinstance(tap, Tap)
    # the tap led off target; three more relaunches fit before the limit
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
        relaunches += 1
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")
    assert relaunches == 5


def test_a_target_screen_alone_does_not_reset_the_relaunch_count() -> None:
    eng = started()
    for _ in range(3):
        assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
    # a target screen is seen between the relaunches, but no tap was sent
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_an_unsent_tap_does_not_reset_the_relaunch_count() -> None:
    eng = started()
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
    tap = eng.next(on_screen(["Item a"]))
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, on_screen(["Item changed"]))  # refused: not sent
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Stop("relaunch_limit")


def test_an_allowed_recheck_resets_the_relaunch_count() -> None:
    eng = started()
    for _ in range(3):
        for expected in (Back(), Back(), Relaunch()):
            assert eng.next(off_screen()) == expected
    screen = on_screen(["Item a"])
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, screen)
    for expected in (Back(), Back(), Relaunch()):
        assert eng.next(off_screen()) == expected


def test_a_target_screen_resets_the_back_count() -> None:
    eng = started()
    assert eng.next(off_screen()) == Back()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Relaunch()


@pytest.mark.parametrize(
    ("screen_package", "foreground"),
    [
        (PKG, None),
        (PKG, OTHER),
        (OTHER, PKG),
        (PERMISSION, PERMISSION),
        (OTHER, None),
    ],
)
def test_target_needs_dump_package_and_foreground_to_match(
    screen_package, foreground
) -> None:
    eng = started()
    screen = make_screen(["Item a"], package=screen_package)
    action = eng.next(Observation(screen, foreground, keyboard_shown=False, now=0.0))
    assert action == Back()
    assert eng.start_screen_id is None


def test_a_tap_that_opens_a_permission_dialog_is_answered_with_back() -> None:
    eng = started()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    dialog = seen(
        make_screen(["Allow", "Deny"], name="dialog", package=PERMISSION),
        fg=PERMISSION,
    )
    assert eng.next(dialog) == Back()
    assert eng.records.transitions[-1].to_screen_id == dialog.screen.screen_id


# -- keyboard ------------------------------------------------------------------


def test_keyboard_shown_is_back_once_then_keyboard_stuck() -> None:
    # Pinned reading: the BACK that hides the keyboard is not navigation, so it
    # may be sent on the start screen (see the module docstring).
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Stop("keyboard_stuck")


def test_the_keyboard_back_counts_as_an_action() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.actions_sent == 2
    eng = started(Limits(actions=2))
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(on_screen(["Item a"])) == Stop("max_actions")


def test_navigation_backs_do_not_count_toward_the_off_target_backs() -> None:
    eng = started()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    assert eng.next(on_screen([], name="leaf")) == Back()  # navigation BACK
    assert [eng.next(off_screen()) for _ in range(3)] == [Back(), Back(), Relaunch()]


def test_the_keyboard_back_does_not_count_toward_the_off_target_backs() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert [eng.next(off_screen()) for _ in range(3)] == [Back(), Back(), Relaunch()]


def test_the_keyboard_back_keeps_the_path() -> None:
    eng = started(Limits(depth=2))
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    deep = on_screen(["Item a"], name="deep", keyboard=True)
    assert eng.next(deep) == Back()
    # still the second screen of the path: nothing is pressed at the limit
    assert eng.next(on_screen(["Item a"], name="deep")) == Back()


def test_keyboard_gone_after_back_continues() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)


def test_keyboard_back_is_forgotten_after_a_good_reading() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    assert eng.next(on_screen(["Item b"], name="two", keyboard=True)) == Back()


def test_unknown_keyboard_state_counts_as_not_shown() -> None:
    eng = started()
    screen = make_screen(["Item a"])
    assert isinstance(eng.next(Observation(screen, PKG, None, 0.0)), Tap)


def test_keyboard_stuck_survives_a_failed_dump_between() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    assert eng.next(Observation(None, PKG, None, 1.0)) == Reread()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Stop("keyboard_stuck")


# -- dump failures -------------------------------------------------------------


def failed(now=0.0):
    return Observation(None, PKG, None, now)


def test_failed_dumps_ask_for_a_reread_and_stop_at_three() -> None:
    eng = started()
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Stop("dump_failed_repeatedly")
    assert eng.records.actions_sent == 1  # a re-read sends nothing


def test_a_good_dump_resets_the_failure_count() -> None:
    eng = started()
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    assert isinstance(eng.next(on_screen(["Item b"], name="two")), Tap)
    assert eng.stopped is None


def test_a_failed_dump_after_an_action_is_recorded_once_and_resolved_later() -> None:
    eng = started()
    tap = eng.next(on_screen(["Item a"]))
    assert isinstance(tap, Tap)
    assert eng.next(failed()) == Reread()
    assert eng.next(on_screen([], name="leaf")) == Back()
    first, second = eng.records.transitions[:2]
    assert (first.kind, first.to_screen_id, first.dump_failed) == (
        "launch",
        tap.screen_id,
        False,
    )
    assert second.kind == "tap"
    assert second.to_screen_id is None
    assert second.dump_failed is True
    assert eng.depth_of(make_screen([], name="leaf").screen_id) == 2  # a tap led there


# -- limits --------------------------------------------------------------------


def test_limits_validation() -> None:
    assert Limits() == Limits(depth=5, actions=300, minutes=30)
    for bad in (0, -1):
        for name in ("depth", "actions", "minutes"):
            with pytest.raises(ValueError, match="at least 1"):
                LooseLimits(**{name: bad})
    for bad in (True, False, 1.0, "3", None):
        for name in ("depth", "actions", "minutes"):
            with pytest.raises(TypeError):
                LooseLimits(**{name: bad})


def test_max_actions_counts_launch_taps_backs_and_relaunches() -> None:
    eng = started(Limits(actions=4))
    assert eng.next(off_screen()) == Back()  # 2
    assert eng.next(off_screen()) == Back()  # 3
    assert eng.next(off_screen()) == Relaunch()  # 4
    assert eng.next(off_screen()) == Stop("max_actions")
    eng = started(Limits(actions=3))
    assert isinstance(eng.next(on_screen(["Item a", "Item b"])), Tap)  # 2
    assert isinstance(eng.next(on_screen(["Item a"], name="two")), Tap)  # 3
    assert eng.next(on_screen(["Item a"], name="three")) == Stop("max_actions")


def test_the_launch_alone_can_use_the_action_limit() -> None:
    eng = started(Limits(actions=1))
    assert eng.next(on_screen(["Item a"])) == Stop("max_actions")


def test_failed_dumps_do_not_count_as_actions() -> None:
    eng = started(Limits(actions=2))
    assert eng.next(failed()) == Reread()
    assert eng.next(failed()) == Reread()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)  # the 2nd action


def test_max_duration_uses_the_clock_in_the_observation() -> None:
    eng = started(Limits(minutes=2), started_at=1000.0)
    assert isinstance(eng.next(on_screen(["Item a"], now=1119.9)), Tap)
    assert eng.next(on_screen(["Item a"], name="two", now=1120.0)) == Stop(
        "max_duration"
    )


def test_max_duration_applies_to_a_failed_dump_too() -> None:
    eng = started(Limits(minutes=1), started_at=5.0)
    assert eng.next(failed(now=65.0)) == Stop("max_duration")


def test_depth_limits_each_path_only() -> None:
    nodes = [
        Node("n0", (("Item a", "c1"), ("Item b", "leaf"))),
        Node("c1", (("Item a", "c2"),)),
        Node("c2", (("Item a", "c3"),)),
        Node("c3", (("Item a", "c4"),)),
        Node("c4", (("Item a", "c5"),)),
        Node("c5", (("Item a", "c6"),)),
        Node("c6", (("Item a", "c7"),)),
        Node("c7"),
        Node("leaf"),
    ]
    phone = Phone(nodes)
    eng = engine(Limits(depth=5))
    log = drive(eng, phone)
    assert log[-1] == Stop("exhausted")
    assert "c4" in phone.visited  # the fifth screen of the path
    assert "c5" not in phone.visited
    assert "leaf" in phone.visited  # the crawl went on elsewhere
    assert eng.depth_of(phone.screen_id("c4")) == 5
    assert eng.depth_of(phone.screen_id("c5")) is None
    taps = [a for a in log if isinstance(a, Tap)]
    assert phone.screen_id("c4") not in {t.screen_id for t in taps}


def test_depth_limit_holds_for_the_real_path_through_a_shorter_known_depth() -> None:
    # "a2" is known at depth 2, but reaching it again from "x" (depth 4) is a
    # path of 5 screens: nothing may be pressed there.
    nodes = [
        Node("n0", (("Item a", "a2"),)),
        Node("a2", (("Item a", "b"), ("Item b", "c"))),
        Node("b", (("Item a", "x"),)),
        Node("x", (("Item a", "a2"),)),
        Node("c", (("Item a", "d"),)),
        Node("d", (("Item a", "e"),)),
        Node("e", (("Item a", "f"),)),
        Node("f"),
    ]

    class Tracking(Phone):
        deepest = 0

        def send(self, action):
            super().send(action)
            self.deepest = max(self.deepest, len(self.stack))

    phone = Tracking(nodes)
    log = drive(engine(Limits(depth=5)), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert phone.deepest <= 5


def test_depth_numbers_follow_the_path() -> None:
    nodes = [
        Node("n0", (("Item a", "n1"),)),
        Node("n1", (("Item a", "n2"),)),
        Node("n2"),
    ]
    phone = Phone(nodes)
    eng = engine()
    drive(eng, phone)
    depths = {name: eng.depth_of(phone.screen_id(name)) for name in ("n0", "n1", "n2")}
    assert depths == {"n0": 1, "n1": 2, "n2": 3}


def test_a_screen_with_unknown_depth_gets_the_limit_and_nothing_is_pressed() -> None:
    eng = started(Limits(depth=3))
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    assert eng.next(on_screen([], name="leaf")) == Back()
    stranger = on_screen(["Item a", "Item b"], name="stranger")
    assert eng.next(stranger) == Back()
    assert eng.depth_of(stranger.screen.screen_id) == 3


def test_the_smallest_known_depth_is_kept() -> None:
    eng = started(Limits(depth=5))
    first = eng.next(on_screen(["Item a", "Item b"]))
    assert isinstance(first, Tap)
    mid = on_screen(["Item a", "Item b"], name="mid")
    assert isinstance(eng.next(mid), Tap)  # mid is depth 2
    far = on_screen([], name="far")
    assert eng.next(far) == Back()  # far is depth 3
    assert eng.depth_of(far.screen.screen_id) == 3
    assert isinstance(eng.next(mid), Tap)  # back on mid: the second button
    assert eng.next(far) == Back()
    assert eng.depth_of(far.screen.screen_id) == 3
    assert eng.next(mid) == Back()
    start = on_screen(["Item a", "Item b"])
    tap = eng.next(start)
    assert isinstance(tap, Tap)  # the start screen's second button
    assert eng.next(far) == Back()  # reached from depth 1: far is now depth 2
    assert eng.depth_of(far.screen.screen_id) == 2


def test_dialogs_later_do_not_raise_the_known_depth_of_a_screen() -> None:
    eng = started(Limits(depth=5))
    root = on_screen(["Item a", "Item b"], name="root")
    assert isinstance(eng.next(root), Tap)
    shared = on_screen([], name="shared")
    assert eng.next(shared) == Back()  # shared is depth 2
    assert eng.depth_of(shared.screen.screen_id) == 2
    assert isinstance(eng.next(root), Tap)  # the second button
    mid = on_screen(["Item a"], name="mid")
    assert isinstance(eng.next(mid), Tap)  # mid is depth 2
    # the tap on mid opens shared with two dialogs over it: real length 3
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    assert eng.next(shared) == Back()
    assert eng.depth_of(shared.screen.screen_id) == 2  # not 3


# -- tried marks ---------------------------------------------------------------


def test_a_list_that_changes_text_keeps_different_rows_distinct() -> None:
    eng = started()

    def rows(*labels):
        return on_screen(list(labels), name="list", rid="rows:id/row", cls=ROW)

    first = eng.next(rows("Alpha", "Beta"))
    assert isinstance(first, Tap)
    assert first.element_id == "e2"
    assert eng.next(on_screen([], name="leaf")) == Back()
    second = eng.next(rows("Beta", "Gamma"))  # same screen id, Beta moved up
    assert isinstance(second, Tap)
    assert second.element_id == "e2"  # the same element id is not "tried"
    assert second.fingerprint != first.fingerprint
    assert eng.next(on_screen([], name="leaf")) == Back()
    third = eng.next(rows("Beta", "Gamma"))
    assert isinstance(third, Tap)
    assert third.element_id == "e3"
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(rows("Beta", "Gamma")) == Stop("exhausted")


def test_two_pressable_elements_with_one_fingerprint_try_only_the_first() -> None:
    eng = started()

    def twin():
        return on_screen(["Same", "Same"], rid="x:id/same")

    first = eng.next(twin())
    assert isinstance(first, Tap)
    assert first.element_id == "e2"
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(twin()) == Stop("exhausted")


def test_the_fingerprint_ignores_position_and_element_id_but_not_the_ancestors() -> (
    None
):
    screen = make_screen(["Item a"])
    moved = make_screen(["Item a"])
    element = screen.elements[1]
    shifted = clone(element, id="e9", bounds=Bounds(41, 7, 99, 99))
    assert fingerprint_of(screen, element) == fingerprint_of(moved, moved.elements[1])
    assert fingerprint_of(screen, element) == fingerprint_of(moved, shifted)
    other_parent = Element(
        "e1", None, "android.widget.LinearLayout", Bounds(0, 0, *DISPLAY), package=PKG
    )
    nested = Screen(
        screen.serial,
        screen.captured_at,
        screen.package,
        screen.activity,
        screen.api_level,
        screen.display,
        (other_parent, element),
    )
    assert fingerprint_of(nested, element) != fingerprint_of(screen, element)
    same_class = clone(other_parent, class_name=screen.elements[0].class_name)
    by_id = clone(same_class, resource_id="x:id/other_parent")
    for parent in (same_class, by_id):
        variant = Screen(
            screen.serial,
            screen.captured_at,
            screen.package,
            screen.activity,
            screen.api_level,
            screen.display,
            (parent, element),
        )
        assert (
            fingerprint_of(variant, element) == fingerprint_of(screen, element)
        ) == (parent is same_class)
    for field in ("text", "resource_id", "content_desc", "hint", "class_name"):
        changed = clone(element, **{field: "different"})
        assert fingerprint_of(screen, changed) != fingerprint_of(screen, element)


def test_fingerprints_cannot_be_confused_by_moving_text_between_fields() -> None:
    first = make_screen(["ab"])
    second = make_screen(["a"])
    left = first.elements[1]
    right = clone(left, text="a", content_desc="b")
    assert fingerprint_of(first, left) != fingerprint_of(second, right)


# -- skipped records -----------------------------------------------------------


def test_skipped_elements_are_recorded_once_with_their_reasons() -> None:
    eng = started()
    screen = on_screen(["Delete", "Item a"])
    assert isinstance(eng.next(screen), Tap)
    assert eng.next(on_screen([], name="leaf")) == Back()
    assert eng.next(screen) == Stop("exhausted")
    skipped = eng.records.skipped
    assert len(skipped) == 1
    assert "deny_word" in skipped[0].reasons
    assert skipped[0].screen_id == screen.screen.screen_id
    assert skipped[0].fingerprint == fingerprint_of(
        screen.screen, screen.screen.elements[1]
    )


# -- re-check ------------------------------------------------------------------


def planned_tap(labels=("Item a",)):
    eng = started()
    first = on_screen(list(labels))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    return eng, first, tap


def test_recheck_allows_the_same_element() -> None:
    eng, first, tap = planned_tap()
    assert eng.recheck(tap, first)
    assert eng.records.skipped == ()
    assert eng.actions_sent == 2


def _changed_screen(*, display=DISPLAY, **changes):
    screen = make_screen(["Item a"])
    return Screen(
        screen.serial,
        screen.captured_at,
        screen.package,
        screen.activity,
        screen.api_level,
        display,
        (screen.elements[0], clone(screen.elements[1], **changes)),
    )


@pytest.mark.parametrize(
    "fresh",
    [
        pytest.param(seen(_changed_screen(resource_id="x:id/y")), id="resource_id"),
        pytest.param(seen(_changed_screen(class_name=ROW)), id="class"),
        pytest.param(
            seen(_changed_screen(bounds=Bounds(40, 61, 280, 101))), id="bounds"
        ),
        # same centre, so judge gives the same point: only the bounds differ
        pytest.param(
            seen(_changed_screen(bounds=Bounds(30, 50, 290, 110))),
            id="bounds-same-point",
        ),
        # same bounds, but the display is narrower, so judge's point moves
        pytest.param(seen(_changed_screen(display=(200, 640))), id="point-moved"),
        pytest.param(
            seen(_changed_screen(flags=("clickable", "disabled"))), id="judge"
        ),
        pytest.param(seen(make_screen(["Item a"]), keyboard=True), id="keyboard"),
        pytest.param(seen(_changed_screen(flags=())), id="not-clickable"),
        pytest.param(seen(make_screen(["Item a"], name="other")), id="screen_id"),
        pytest.param(
            seen(make_screen(["Item a"], package=OTHER), fg=OTHER), id="package"
        ),
        pytest.param(seen(make_screen(["Item a"]), fg=OTHER), id="foreground"),
        pytest.param(seen(make_screen(["Item a"]), fg=None), id="foreground-unknown"),
        pytest.param(
            Observation(None, PKG, keyboard_shown=None, now=0.0), id="failed-dump"
        ),
        pytest.param(seen(make_screen([])), id="gone"),
    ],
)
def test_recheck_refuses_a_changed_screen_and_records_the_skip(fresh) -> None:
    eng, first, tap = planned_tap()
    assert not eng.recheck(tap, fresh)
    assert eng.actions_sent == 1  # the tap was not sent
    assert eng.records.skipped == (
        SkippedElement(tap.screen_id, tap.fingerprint, ("changed_before_press",)),
    )
    # the element is tried: the same screen does not give the same tap again
    assert eng.next(first) == Stop("exhausted")


@pytest.mark.parametrize(
    "changes",
    [{"text": "Other"}, {"content_desc": "d"}, {"hint": "h"}],
    ids=["text", "content_desc", "hint"],
)
def test_recheck_refuses_a_label_change_and_marks_the_element_volatile(
    changes,
) -> None:
    eng, first, tap = planned_tap()
    assert not eng.recheck(tap, seen(_changed_screen(**changes)))
    assert eng.actions_sent == 1
    assert eng.records.skipped == (
        SkippedElement(tap.screen_id, tap.fingerprint, ("changed_before_press",)),
        SkippedElement(tap.screen_id, tap.fingerprint, ("label_changes",)),
    )
    assert eng.next(first) == Stop("exhausted")


def test_recheck_refusal_is_not_a_transition_and_does_not_count() -> None:
    eng, _first, tap = planned_tap()
    assert not eng.recheck(tap, seen(_changed_screen(text="Other")))
    assert [t.kind for t in eng.records.transitions] == ["launch"]


def test_a_refused_tap_leaves_no_pending_transition_behind() -> None:
    eng, _first, tap = planned_tap()
    fresh = seen(_changed_screen(resource_id="x:id/y"))
    assert not eng.recheck(tap, fresh)
    second = eng.next(fresh)
    assert isinstance(second, Tap)
    assert eng.next(on_screen([], name="leaf")) == Back()
    transitions = eng.records.transitions
    assert [t.kind for t in transitions] == ["launch", "tap"]
    assert [t.action_id for t in transitions] == [1, 2]
    assert transitions[1].fingerprint == second.fingerprint


def test_refusals_alternating_between_two_screens_still_reach_the_cap() -> None:
    # The clock never moves and every read has a new label, so only the refusal
    # cap can end this; it counts across screens.
    def screen_a(i):
        return on_screen([f"Live {i}"], name="a", now=0.0)

    def screen_b(i):
        return on_screen([f"Live {i}"], name="b", now=0.0)

    eng = started()
    action = eng.next(screen_a(0))
    assert isinstance(action, Tap)
    assert eng.recheck(action, screen_a(0))  # sent: the count is zero
    action = eng.next(screen_b(0))
    assert isinstance(action, Tap)  # b is depth 2
    sent = eng.actions_sent
    for i in range(1, 20):
        fresh = (screen_a if i % 2 else screen_b)(i)
        assert not eng.recheck(action, fresh)
        action = eng.next(fresh)
        if not isinstance(action, Tap):
            break
    else:
        pytest.fail("the refusals never reached the cap")
    assert i <= crawl.MAX_REFUSALS
    assert isinstance(action, Back | Stop)
    assert eng.actions_sent <= sent + 1


def test_a_label_that_changes_on_every_read_is_tried_once_and_skipped() -> None:
    eng = started()
    steps = 0
    action = eng.next(on_screen(["Timer 0"]))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, on_screen([f"Timer {steps}x"]))
        action = eng.next(on_screen([f"Timer {steps}"]))
    assert steps == 1  # marked volatile: the later reads are skipped
    assert action == Stop("exhausted")  # the start screen
    assert eng.actions_sent == 1
    reasons = [r.reasons for r in eng.records.skipped]
    assert reasons.count(("label_changes",)) == 1


def test_a_structure_that_changes_on_every_read_does_not_livelock() -> None:
    eng = started()
    steps = 0
    action = eng.next(on_screen(["Timer 0"], rid="x:id/r0"))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, on_screen(["Timer 0"], rid=f"x:id/s{steps}"))
        action = eng.next(on_screen(["Timer 0"], rid=f"x:id/r{steps}"))
    assert steps == 3  # MAX_REFUSALS refusals, then the screen counts as done
    assert action == Stop("exhausted")  # the start screen
    assert eng.actions_sent == 1


def test_refusals_on_an_inner_screen_end_with_back() -> None:
    eng = started()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)
    action = eng.next(on_screen(["Timer"], name="inner", rid="x:id/r0"))
    for i in range(3):
        assert isinstance(action, Tap)
        fresh = on_screen(["Timer"], name="inner", rid=f"x:id/s{i}")
        assert not eng.recheck(action, fresh)
        action = eng.next(on_screen(["Timer"], name="inner", rid=f"x:id/r{i + 1}"))
    assert action == Back()


def test_an_allowed_recheck_resets_the_refusal_count() -> None:
    eng = started()
    action = eng.next(on_screen(["Timer", "Item b"], rid="x:id/a0"))
    assert isinstance(action, Tap)
    assert not eng.recheck(action, on_screen(["Timer", "Item b"], rid="x:id/b0"))
    action = eng.next(on_screen(["Timer", "Item b"], rid="x:id/a1"))
    assert isinstance(action, Tap)
    assert not eng.recheck(action, on_screen(["Timer", "Item b"], rid="x:id/b1"))
    action = eng.next(on_screen(["Timer", "Item b"], rid="x:id/a2"))
    assert isinstance(action, Tap)
    same = on_screen(["Timer", "Item b"], rid="x:id/a2")
    assert eng.recheck(action, same)


def test_recheck_judge_decides_again_on_the_fresh_screen() -> None:
    eng, _, tap = planned_tap()
    fresh = seen(_changed_screen(flags=("clickable", "disabled")))
    assert fresh.screen is not None
    judgement = judge(fresh.screen, PKG, POLICY, foreground=PKG)
    assert not judgement.pressable
    assert not eng.recheck(tap, fresh)


def test_recheck_rejects_a_tap_the_engine_did_not_hand_out() -> None:
    eng, first, tap = planned_tap()
    other = Tap(tap.screen_id, tap.element_id, tap.fingerprint, (1, 1))
    with pytest.raises(ValueError, match="last"):
        eng.recheck(other, first)
    assert not eng.recheck(tap, seen(_changed_screen(text="Other")))
    with pytest.raises(ValueError, match="last"):
        eng.recheck(tap, first)  # a refusal is final


def _swapped_twins(screen):
    first, second = screen.elements[1], screen.elements[2]
    return Screen(
        screen.serial,
        screen.captured_at,
        screen.package,
        screen.activity,
        screen.api_level,
        screen.display,
        (
            screen.elements[0],
            clone(first, bounds=second.bounds),
            clone(second, bounds=first.bounds),
        ),
    )


def test_recheck_picks_the_first_pressable_twin() -> None:
    twin = on_screen(["Same", "Same"], rid="x:id/same")
    eng = started()
    tap = eng.next(twin)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, twin)
    # the twins swapped places: the first one now has the other one's bounds
    eng = started()
    tap = eng.next(twin)
    assert isinstance(tap, Tap)
    assert twin.screen is not None
    assert not eng.recheck(tap, seen(_swapped_twins(twin.screen)))


def test_a_second_recheck_after_an_allowed_one_is_rejected() -> None:
    eng, first, tap = planned_tap()
    assert eng.recheck(tap, first)
    with pytest.raises(ValueError, match="last"):
        eng.recheck(tap, seen(_changed_screen(text="Other")))
    assert eng.actions_sent == 2  # the sent tap stays counted


# -- determinism, actions, imports ---------------------------------------------


def random_graph(rng, *, links_to_start=True):
    count = rng.randint(2, 8)
    names = [f"n{i}" for i in range(count)]
    targets = names if links_to_start else names[1:]
    nodes = []
    for i, name in enumerate(names):
        if i > 0 and rng.random() < 0.2:
            nodes.append(
                Node(name, (("Allow", rng.choice(targets)),), package=PERMISSION)
            )
            continue
        buttons = tuple(
            (f"Item {'abcde'[k]}", rng.choice(targets))
            for k in range(rng.randint(0, 4))
        )
        nodes.append(Node(name, buttons))
    return nodes


def reachable_targets(nodes):
    by_name = {node.name: node for node in nodes}
    found = {"n0"}
    todo = ["n0"]
    while todo:
        node = by_name[todo.pop()]
        if node.package != PKG:
            continue
        for _, dest in node.buttons:
            if dest not in found:
                found.add(dest)
                todo.append(dest)
    return {name for name in found if by_name[name].package == PKG}


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
        eng = engine(Limits(depth=40, actions=600, minutes=30))
        log = drive(eng, phone, depth=40)
        assert not any(isinstance(a, Relaunch) for a in log)
        assert log[-1] == Stop("exhausted")
        assert {"n0", *phone.visited} >= reachable_targets(nodes)


def test_a_link_back_to_the_start_screen_does_not_end_the_crawl() -> None:
    # The start screen higher up the path is not the root: it gets BACK like any
    # other screen, so the screen "n2" below it is still visited.
    nodes = [
        Node("n0", (("Item a", "n1"),)),
        Node("n1", (("Item a", "n0"), ("Item b", "n2"))),
        Node("n2"),
    ]
    phone = Phone(nodes)
    log = drive(engine(), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "n2" in phone.visited


def test_p1_buttons_that_stay_on_the_start_screen_do_not_hide_the_leaves() -> None:
    nodes = [
        Node(
            "n0",
            (
                ("One", "n0"),
                ("Two", "n0"),
                ("Three", "n0"),
                ("Four", "n0"),
                ("Five", "leaf1"),
                ("Six", "leaf2"),
            ),
        ),
        Node("leaf1"),
        Node("leaf2"),
    ]
    phone = Phone(nodes)
    eng = engine()
    log = drive(eng, phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert {"leaf1", "leaf2"} <= set(phone.visited)
    assert eng.depth_of(phone.screen_id("n0")) == 1


def test_p3_a_cycle_back_to_the_start_still_reaches_the_other_leaf() -> None:
    nodes = [
        Node("n0", (("Item a", "n1"), ("Item b", "leafB"))),
        Node("n1", (("Item a", "n2"),)),
        Node("n2", (("Item a", "n3"),)),
        Node("n3", (("Item a", "n0"),)),
        Node("leafB"),
    ]

    class Tracking(Phone):
        deepest = 0

        def send(self, action):
            super().send(action)
            self.deepest = max(self.deepest, len(self.stack))

    phone = Tracking(nodes)
    eng = engine(Limits(depth=5))
    log = drive(eng, phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "leafB" in phone.visited
    assert phone.deepest <= 5  # the real stack never passes the depth limit
    n0 = phone.screen_id("n0")
    n3 = phone.screen_id("n3")
    # the BACK on the upper n0 (path of 5, at the limit) was sent
    assert any(
        t.kind == "back" and t.from_screen_id == n0 and t.to_screen_id == n3
        for t in eng.records.transitions
    )


def test_a_back_that_returns_the_same_screen_leads_to_a_relaunch() -> None:
    eng = started()
    root = on_screen(["Item a"], name="root")
    assert isinstance(eng.next(root), Tap)
    stuck = on_screen([], name="stuck")
    assert eng.next(stuck) == Back()
    assert eng.next(stuck) == Relaunch()  # the BACK did nothing
    assert eng.next(root) == Stop("exhausted")
    assert eng.actions_sent == 4


def test_backs_that_alternate_between_two_screens_lead_to_a_relaunch() -> None:
    eng = started()
    root = on_screen(["Item a"], name="root")
    assert isinstance(eng.next(root), Tap)
    first = on_screen([], name="first")
    second = on_screen([], name="second")
    assert eng.next(first) == Back()
    assert eng.next(second) == Back()
    assert eng.next(first) == Back()
    assert eng.next(second) == Relaunch()  # second was landed on before
    assert eng.next(root) == Stop("exhausted")
    assert eng.actions_sent == 6


def test_a_sent_tap_clears_the_screens_that_backs_landed_on() -> None:
    eng = started()
    root = on_screen(["Item a", "Item b"], name="root")
    mid = on_screen(["Item a"], name="mid")
    leaf = on_screen([], name="leaf")
    assert isinstance(eng.next(root), Tap)
    assert eng.next(leaf) == Back()
    assert isinstance(eng.next(root), Tap)  # a tap: the set starts again
    assert isinstance(eng.next(mid), Tap)
    assert eng.next(leaf) == Back()
    assert eng.next(mid) == Back()
    assert eng.next(root) == Stop("exhausted")


def test_unwinding_a_path_that_repeats_a_screen_id_is_not_a_relaunch() -> None:
    # n0 -> m -> n0 (again) -> x: the BACKs land on n0, m, n0 in turn. The last
    # landing is the screen the path predicts, so it is a normal step back.
    nodes = [
        Node("n0", (("Item a", "m"), ("Item b", "x"))),
        Node("m", (("Item a", "n0"),)),
        Node("x"),
    ]
    phone = Phone(nodes)
    log = drive(engine(Limits(depth=6)), phone, depth=6)
    assert log[-1] == Stop("exhausted")
    assert not any(isinstance(a, Relaunch) for a in log)
    assert "x" in phone.visited


def test_a_landing_on_a_screen_landed_on_before_the_last_tap_is_not_a_relaunch() -> (
    None
):
    eng = started()
    root = on_screen(["Item a", "Item b"], name="root")
    mid = on_screen(["Item a"], name="mid")
    leaf = on_screen([], name="leaf")
    assert isinstance(eng.next(root), Tap)  # a
    assert eng.next(leaf) == Back()
    assert isinstance(eng.next(root), Tap)  # b; the BACK landed on root before
    assert isinstance(eng.next(mid), Tap)
    assert eng.next(leaf) == Back()
    # The BACK jumps to root, past mid, which the path does not predict. The tap
    # sent since cleared the set, so this is no repeat landing.
    assert eng.next(root) == Stop("exhausted")


def test_an_off_target_back_is_not_a_navigation_back() -> None:
    eng = started()
    root = on_screen(["Item a"], name="root")
    mid = on_screen(["Item a"], name="mid")
    leaf = on_screen([], name="leaf")
    assert isinstance(eng.next(root), Tap)
    assert isinstance(eng.next(mid), Tap)
    assert eng.next(leaf) == Back()
    assert eng.next(mid) == Back()  # a navigation BACK landed on mid
    assert eng.next(off_screen()) == Back()  # the app was left; an off-target BACK
    # lands on mid again: that is no repeat landing of a navigation BACK (no
    # Relaunch), and the same frame replaces the root in place, so nothing
    # more is pressed (its button was tried) and there is no BACK on the root
    assert eng.next(mid) == Stop("exhausted")


def test_dialogs_over_a_new_target_screen_place_it_at_its_true_depth() -> None:
    eng = started(Limits(depth=3))
    root = on_screen(["Item a"], name="root")
    assert isinstance(eng.next(root), Tap)
    # the tap opened a target screen with two dialogs over it
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    assert eng.next(off_screen(package=PERMISSION)) == Back()
    opened = on_screen(["Item a"], name="opened")
    # not the root: it takes the place of the mark the tap left, at depth 2
    assert eng.next(opened).__class__ is Tap
    assert eng.depth_of(opened.screen.screen_id) == 2


def test_a_tap_that_pushes_more_than_one_screen_keeps_the_depth_limit() -> None:
    chain = [Node(f"t{i}", (("Item a", f"t{i + 1}"),)) for i in range(1, 5)]
    nodes = [
        Node("n0", (("Item a", "T"), ("Item b", "leafR"))),
        Node("T", (("Item a", "t1"),)),
        *chain,
        Node("t5"),
        Node("leafR"),
        Node("D1", (("Allow", "n0"),), package=PERMISSION),
        Node("D2", (("Allow", "n0"),), package=PERMISSION),
    ]

    class Dialogs(Phone):
        def send(self, action):
            before = list(self.stack)
            super().send(action)
            if isinstance(action, Tap) and self.stack[-1] == "T" and before == ["n0"]:
                self.stack += ["D2", "D1"]  # two dialogs over the new screen
                self.visited[-1] = "D1"

    phone = Dialogs(nodes)
    log = drive(engine(Limits(depth=5)), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "leafR" in phone.visited  # the second button of the root was pressed
    assert phone.stack == ["n0"]  # and the crawl ended on the real root


def test_dialogs_over_the_pressed_screen_leave_no_extra_depth() -> None:
    # n1 links back to the start screen n0, and the app shows a permission
    # dialog whenever n0 is opened by a tap.
    nodes = [
        Node("n0", (("Item a", "n1"), ("Item b", "n2"))),
        Node("n1", (("Item a", "n0"), ("Item b", "z"))),
        Node("n2", (("Item a", "n3"),)),
        Node("n3", (("Item a", "n4"),)),
        Node("n4", (("Item a", "n5"),)),
        Node("n5"),
        Node("z"),
        Node("D1", (("Allow", "n0"),), package=PERMISSION),
    ]

    class Perm(Phone):
        def send(self, action):
            super().send(action)
            if isinstance(action, Tap) and self.stack[-1] == "n0":
                self.stack.append("D1")
                self.visited[-1] = "D1"

    phone = Perm(nodes)
    log = drive(engine(Limits(depth=5)), phone, depth=5)
    assert log[-1] == Stop("exhausted")
    assert "z" in phone.visited
    assert phone.stack == ["n0"]


@pytest.mark.parametrize("dialogs", [1, 2])
def test_a_reopened_screen_with_new_labels_keeps_the_real_stack_in_limit(
    dialogs: int,
) -> None:
    # T shows a feed whose label changes each time T is opened (same screen id
    # is kept, the fingerprint is new). T is first reached from n0 (depth 2),
    # and later from b (real depth 3) with dialogs over it.
    nodes = [
        Node("n0", (("Item a", "T"), ("Item b", "a"))),
        Node("a", (("Item a", "b"),)),
        Node("b", (("Item a", "T"),)),
        Node("T", (("Feed 0", "U"),)),
        Node("U", (("Item a", "V"),)),
        Node("V", (("Item a", "W"),)),
        Node("W", (("Item a", "X"),)),
        Node("X"),
        Node("D1", (("Allow", "n0"),), package=PERMISSION),
        Node("D2", (("Allow", "n0"),), package=PERMISSION),
    ]

    class Feed(Phone):
        opened = 0

        def send(self, action):
            before = list(self.stack)
            super().send(action)
            if isinstance(action, Tap) and self.stack[-1] == "T":
                self.opened += 1
                for name, nxt in (("T", "U"), ("U", "V"), ("V", "W"), ("W", "X")):
                    label = f"Feed {self.opened}"
                    self.nodes[name] = Node(name, ((label, nxt),))
                if before[-1:] == ["b"]:
                    self.stack += ["D2", "D1"][-dialogs:]
                    self.visited[-1] = self.stack[-1]

    phone = Feed(nodes)
    log = drive(engine(Limits(depth=5)), phone, depth=5)
    assert isinstance(log[-1], Stop)


def test_the_keyboard_back_is_not_a_navigation_back() -> None:
    eng = started()
    assert eng.next(on_screen(["Item a"], keyboard=True)) == Back()
    # the same screen again without the keyboard: no relaunch
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)


def test_the_keyboard_back_on_the_root_is_still_allowed() -> None:
    eng = started()
    root = on_screen(["Item a"], keyboard=True)
    assert eng.next(root) == Back()
    assert isinstance(eng.next(on_screen(["Item a"])), Tap)


def test_a_screen_with_the_roots_id_higher_up_gets_back_when_it_is_done() -> None:
    eng = started(Limits(depth=3))
    root = on_screen(["Item a"], name="root")
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    mid = on_screen(["Item a"], name="mid")
    assert isinstance(eng.next(mid), Tap)
    # mid links back to the root screen: path of 3, at the limit
    assert eng.next(root) == Back()


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
        actions.append(action)
        if not isinstance(action, Stop):
            phone.send(action)
    second = engine()
    replay = [second.start()]
    for observation in seen_observations:
        action = second.next(observation)
        if isinstance(action, Tap):
            assert second.recheck(action, observation)
        replay.append(action)
    assert replay == actions
    assert second.records == first.records


def test_next_needs_start_and_start_runs_once() -> None:
    eng = engine()
    with pytest.raises(RuntimeError, match="start"):
        eng.next(on_screen([]))
    eng.start()
    with pytest.raises(RuntimeError, match="already"):
        eng.start()


def test_constructor_and_observation_validation() -> None:
    with pytest.raises(ValueError, match="empty"):
        CrawlEngine("", POLICY, Limits(), started_at=0.0)
    with pytest.raises(TypeError):
        CrawlEngine(PKG, POLICY, Limits(), started_at=True)
    with pytest.raises(ValueError, match="finite"):
        CrawlEngine(PKG, POLICY, Limits(), started_at=float("inf"))
    with pytest.raises(ValueError, match="finite"):
        Observation(None, None, None, float("nan"))
    with pytest.raises(TypeError):
        LooseObservation("screen", None, None, 0.0)
    with pytest.raises(TypeError):
        LooseObservation(None, None, 1, 0.0)
    with pytest.raises(TypeError):
        LooseObservation(None, 3, None, 0.0)


def test_only_the_listed_action_types_exist() -> None:
    assert set(get_args(Action)) == {Launch, Tap, Back, Relaunch, Reread, Stop}
    assert {f.name for f in fields(Tap)} == {
        "screen_id",
        "element_id",
        "fingerprint",
        "point",
    }
    assert {f.name for f in fields(Stop)} == {"reason"}
    for kind in (Launch, Back, Relaunch, Reread):
        assert fields(kind) == ()
    defined = {
        name
        for name, value in vars(crawl).items()
        if isinstance(value, type)
        and is_dataclass(value)
        and value.__module__ == crawl.__name__
    }
    assert defined == {
        "Launch",
        "Tap",
        "Back",
        "Relaunch",
        "Reread",
        "Stop",
        "Limits",
        "Observation",
        "Transition",
        "SkippedElement",
        "Records",
        "_Pending",
        "_Plan",
    }
    words = ("text", "key", "swipe", "long", "press", "input", "scroll")
    for name in defined:
        assert not any(word in name.lower() for word in words)
    for kind in (Launch, Tap, Back, Relaunch, Reread, Stop):
        assert not any(
            w in f.name for f in fields(kind) for w in ("text", "key", "duration")
        )


def test_a_tap_holds_only_ints_for_the_point() -> None:
    assert Tap("s-1", "e2", "f-1", (1, 2)).point == (1, 2)
    for point in ((1.5, 2), (True, 2), (1,), (1, 2, 3), [1, 2], "ab"):
        with pytest.raises(TypeError):
            LooseTap("s-1", "e2", "f-1", point)
    with pytest.raises(TypeError):
        Tap("", "e2", "f-1", (1, 2))
    with pytest.raises(TypeError):
        Stop("")


def test_the_driver_may_use_its_own_stop_reasons() -> None:
    assert Stop("interrupted").reason == "interrupted"
    assert Stop("error").reason == "error"


APPROVED = {
    "__future__",
    "collections.abc",
    "dataclasses",
    "hashlib",
    "typing",
    "uiprobe.guards",
    "uiprobe.model",
}


def test_crawl_py_imports_only_approved_modules_and_never_calls_replace() -> None:
    tree = ast.parse(CRAWL_PY.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module)
            if node.module == "uiprobe":
                imported.update(f"uiprobe.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "replace"
    assert imported <= APPROVED


# -- same frame, singleton unknown path, deadline, descendants ----


def test_a_refused_tap_with_a_new_screen_id_is_not_a_back_on_the_root() -> None:
    eng = started()
    first = on_screen(["Item a"])
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    fresh = on_screen(["Item a", "Item b"])
    assert fresh.screen is not None
    assert first.screen is not None
    assert fresh.screen.screen_id != first.screen.screen_id
    assert not eng.recheck(tap, fresh)
    action = eng.next(fresh)
    assert isinstance(action, Tap)  # never a Back on the root
    assert action.screen_id == fresh.screen.screen_id
    assert eng.depth_of(fresh.screen.screen_id) == 1


def test_a_refused_tap_on_a_deeper_screen_keeps_its_depth() -> None:
    eng = started(Limits(depth=3))
    root = on_screen(["Go"], name="root")
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, root)
    mid = on_screen(["a"], name="mid")
    tap = eng.next(mid)
    assert isinstance(tap, Tap)
    fresh = on_screen(["a", "b"], name="mid")
    assert not eng.recheck(tap, fresh)
    action = eng.next(fresh)
    assert isinstance(action, Tap)
    assert fresh.screen is not None
    assert eng.depth_of(fresh.screen.screen_id) == 2
    assert eng.recheck(action, fresh)
    leaf = on_screen(["z"], name="leaf")
    assert isinstance(eng.next(leaf), Back)  # the depth limit 3 binds
    assert leaf.screen is not None
    assert eng.depth_of(leaf.screen.screen_id) == 3


def test_a_singleton_unknown_path_does_not_crash_the_recovery() -> None:
    eng = started()
    root = on_screen(["Go"], name="root")
    tap = eng.next(root)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, root)
    assert isinstance(eng.next(on_screen([], name="mid")), Back)
    assert isinstance(eng.next(on_screen([], name="unknown")), Back)
    assert isinstance(eng.next(on_screen([], name="mid")), Back)
    assert isinstance(eng.next(off_screen()), Back)  # off target
    action = eng.next(on_screen([], name="unknown"))  # must not raise
    assert isinstance(action, Back | Relaunch | Stop)


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


def test_random_observation_sequences_never_raise_and_keep_the_rules() -> None:
    rng = random.Random(7_061_118)  # noqa: S311
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
                    relaunches = 0
                    model.sent("tap", action.screen_id)
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


def _timed(minutes=1):
    eng = started(Limits(minutes=minutes))
    first = on_screen(["Item a"], now=59.0)
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    return eng, first, tap


def test_recheck_refuses_at_the_deadline_and_cancels_the_tap() -> None:
    eng, first, tap = _timed()
    at_deadline = on_screen(["Item a"], now=60.0)
    assert not eng.recheck(tap, at_deadline)
    assert eng.actions_sent == 1
    assert eng.records.skipped == ()
    assert [t.kind for t in eng.records.transitions] == ["launch"]
    assert eng.next(first) == Stop("max_duration")  # even on an earlier clock


def test_recheck_just_before_the_deadline_still_allows_the_tap() -> None:
    eng, _first, tap = _timed()
    assert eng.recheck(tap, on_screen(["Item a"], now=59.5))
    assert eng.actions_sent == 2


def test_a_deadline_cancelled_tap_is_not_marked_tried() -> None:
    eng, _first, tap = _timed()
    assert not eng.recheck(tap, on_screen(["Item a"], now=61.0))
    assert (tap.screen_id, tap.fingerprint) not in eng._tried


def _row_screen(*children, name="rows", rows=1):
    """Clickable containers whose label sits in a child TextView."""
    elements = [
        Element(
            "e1",
            None,
            "android.widget.FrameLayout",
            Bounds(0, 0, *DISPLAY),
            package=PKG,
        )
    ]
    for index in range(rows):
        top = 60 + index * 60
        row_id = f"e{len(elements) + 1}"
        elements.append(
            Element(
                row_id,
                "e1",
                "android.widget.LinearLayout",
                Bounds(0, top, 320, top + 50),
                resource_id=f"{PKG}:id/row",
                package=PKG,
                flags=("clickable",),
            )
        )
        label = children[index] if index < len(children) else children[-1]
        elements.append(
            Element(
                f"e{len(elements) + 1}",
                row_id,
                ROW,
                Bounds(10, top, 300, top + 40),
                text=label,
                resource_id=f"{PKG}:id/title",
                package=PKG,
            )
        )
    base = make_screen([], name=name)
    return Screen(
        base.serial,
        base.captured_at,
        PKG,
        base.activity,
        base.api_level,
        DISPLAY,
        tuple(elements),
    )


def test_a_changed_child_label_refuses_the_tap() -> None:
    eng = started()
    first = seen(_row_screen("Item a"))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, seen(_row_screen("Item b")))


def test_an_unchanged_child_label_passes_recheck() -> None:
    eng = started()
    first = seen(_row_screen("Item a"))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, seen(_row_screen("Item a")))


def test_a_moved_child_refuses_the_tap() -> None:
    eng = started()
    first = seen(_row_screen("Item a"))
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    moved = _row_screen("Item a")
    child = moved.elements[2]
    moved = Screen(
        moved.serial,
        moved.captured_at,
        moved.package,
        moved.activity,
        moved.api_level,
        moved.display,
        (*moved.elements[:2], clone(child, bounds=Bounds(11, 60, 300, 100))),
    )
    assert not eng.recheck(tap, seen(moved))


def test_rows_labelled_only_by_children_get_different_fingerprints_and_all_run() -> (
    None
):
    screen = _row_screen("Wi-Fi", "Bluetooth", rows=2)
    rows = [e for e in screen.elements if "clickable" in e.flags]
    assert fingerprint_of(screen, rows[0]) != fingerprint_of(screen, rows[1])
    eng = started()
    observation = seen(screen)
    first = eng.next(observation)
    assert isinstance(first, Tap)
    assert eng.recheck(first, observation)
    # the tap led nowhere new: the same screen again, the second row is next
    second = eng.next(observation)
    assert isinstance(second, Tap)
    assert second.fingerprint != first.fingerprint


# -- same frame after a BACK that is not navigation -------------------


def test_a_keyboard_back_on_the_root_that_changes_the_screen_id_is_the_same_frame() -> (
    None
):
    eng = started()
    first = eng.next(on_screen(["Item a"], name="root", keyboard=True))
    assert first == Back()  # the keyboard BACK
    second = eng.next(on_screen(["Item a", "Item b"], name="root"))
    assert isinstance(second, Tap)  # not a BACK on the root
    assert len(eng._path) == 1
    assert second.screen_id == eng._path[0]


def test_a_keyboard_that_returns_after_every_relaunch_never_backs_out_of_the_root() -> (
    None
):
    eng = started()
    with_keyboard = on_screen(["Item a"], name="root", keyboard=True)
    full = on_screen(["Item a", "Item b"], name="root")
    gone = off_screen()
    state = "keyboard"
    kinds: list[str] = []
    action = eng.next(with_keyboard)
    for _ in range(40):
        kinds.append(type(action).__name__)
        if isinstance(action, Stop):
            break
        if isinstance(action, Launch | Relaunch):
            state = "keyboard"
        elif isinstance(action, Back):
            state = {"keyboard": "full", "full": "gone", "gone": "gone"}[state]
        current = {"keyboard": with_keyboard, "full": full, "gone": gone}[state]
        if isinstance(action, Tap):
            assert eng.recheck(action, current)
            state = "full"
            current = full
        action = eng.next(current)
    assert action == Stop("exhausted")
    assert kinds.count("Back") == 1  # only the one that hides the keyboard
    assert "Relaunch" not in kinds


def test_an_off_target_back_landing_without_a_mark_is_the_same_frame() -> None:
    eng = started()
    tap = eng.next(on_screen(["Item a"], name="root"))
    assert isinstance(tap, Tap)
    # the recheck sees a dialog and refuses; nothing was sent
    dialog = seen(
        make_screen(["Allow"], name="perm", package=PERMISSION), fg=PERMISSION
    )
    assert not eng.recheck(tap, dialog)
    assert eng.next(dialog) == Back()  # off-target BACK, no mark on the path
    after = eng.next(on_screen(["Item a", "Item b"], name="root"))
    assert isinstance(after, Tap)  # the root is still the root: no BACK
    assert len(eng._path) == 1


def test_a_keyboard_back_that_lands_on_the_screen_below_pops_the_top() -> None:
    eng = started()
    root = on_screen(["Item a"], name="root")
    assert isinstance(eng.next(root), Tap)
    mid = on_screen(["Item a"], name="mid", keyboard=True)
    assert eng.next(mid) == Back()  # keyboard BACK on mid, path is [root, mid]
    assert len(eng._path) == 2
    # the BACK went further than the keyboard: the root is shown, the top is gone
    assert eng.next(root) == Stop("exhausted")
    assert len(eng._path) == 1


def test_a_keyboard_back_landing_replaces_the_top_in_place_and_keeps_the_depth() -> (
    None
):
    eng = started(Limits(depth=4))
    steps = [
        on_screen(["Go", "Two"], name="R"),
        on_screen(["Home", "Next"], name="A"),
        on_screen(["Go", "Two", "Extra"], name="R", keyboard=True),
    ]
    real = 1
    for observation in steps:
        action = eng.next(observation)
        if isinstance(action, Tap):
            assert eng.recheck(action, observation)
            real += 1
    assert real == 3
    # the keyboard BACK; the screen comes back with the first row labels
    action = eng.next(on_screen(["Go", "Two"], name="R"))
    assert isinstance(action, Tap)
    assert len(eng._path) == 3  # the real stack: R, A, R
    real += 1
    # the tap opens a screen at the real depth 4, the limit: nothing is pressed
    action = eng.next(on_screen(["Deep"], name="A2"))
    assert not isinstance(action, Tap)
    assert real == 4


# -- volatile elements --------------------------------------------------


def _ticking(clock):
    base = _row_screen(f"Synced {clock} seconds ago")
    elements = [*base.elements, button("e9", "Other", 4)]
    return Screen(
        base.serial,
        base.captured_at,
        base.package,
        base.activity,
        base.api_level,
        base.display,
        tuple(elements),
    )


def test_a_ticking_row_does_not_end_the_crawl_before_the_other_button() -> None:
    eng = started()
    tapped: list[str] = []
    clock = 0
    for _ in range(10):
        action = eng.next(seen(_ticking(clock)))
        clock += 1
        if isinstance(action, Stop):
            break
        assert isinstance(action, Tap)
        allowed = eng.recheck(action, seen(_ticking(clock)))
        clock += 1
        if allowed:
            tapped.append(action.element_id)
    assert action == Stop("exhausted")
    assert tapped == ["e9"]
    reasons = [r.reasons for r in eng.records.skipped]
    assert reasons.count(("label_changes",)) == 1


def test_two_rows_labelled_by_children_are_both_tried_when_the_labels_change_too() -> (
    None
):
    # rows of a list share a structure but not their bounds: the first row's
    # label changes before the press, the second row is still tried
    eng = started()
    first = eng.next(seen(_row_screen("Wi-Fi", "Bluetooth", rows=2)))
    assert isinstance(first, Tap)
    assert not eng.recheck(first, seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2)))
    second = eng.next(seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2)))
    assert isinstance(second, Tap)
    assert second.element_id != first.element_id
    assert eng.recheck(second, seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2)))
    assert eng.next(seen(_row_screen("Wi-Fi 2", "Bluetooth", rows=2))) == Stop(
        "exhausted"
    )
    reasons = [r.reasons for r in eng.records.skipped]
    assert reasons.count(("label_changes",)) == 1


def _moving_ticker(index, clock):
    """One clickable counter that moves with ``index``; the screen id stays."""
    base = make_screen(["x"], name="mover", rid="x:id/b")
    button_at = clone(
        base.elements[1],
        text=f"T {clock}",
        bounds=Bounds(40, 60 + index * 20, 280, 100 + index * 20),
    )
    return Screen(
        base.serial,
        base.captured_at,
        base.package,
        base.activity,
        base.api_level,
        base.display,
        (base.elements[0], button_at),
    )


def test_label_only_refusals_do_not_use_up_the_refusal_count() -> None:
    # three ticking counters and an Other button: Other is still tapped
    def screen(clock):
        return on_screen(
            [f"Steps {clock}", f"Battery {clock}%", f"Time {clock}", "Other"]
        )

    eng = started()
    tapped = []
    clock = 0
    for _ in range(12):
        action = eng.next(screen(clock))
        clock += 1
        if isinstance(action, Stop):
            break
        assert isinstance(action, Tap)
        if eng.recheck(action, screen(clock)):
            tapped.append(action.element_id)
        clock += 1
    assert action == Stop("exhausted")
    assert tapped == ["e5"]


def test_a_screen_with_a_new_volatile_key_on_every_read_reaches_the_bound() -> None:
    eng = started()
    steps = 0
    action = eng.next(seen(_moving_ticker(0, 0)))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, seen(_moving_ticker(steps - 1, steps)))
        action = eng.next(seen(_moving_ticker(steps, steps)))
    assert steps == crawl.MAX_VOLATILE
    assert action == Stop("exhausted")
    assert eng.actions_sent == 1


def test_a_ticking_row_inside_a_list_does_not_block_the_other_rows() -> None:
    eng = started()
    first = eng.next(seen(_row_screen("T 0", "B", "C", rows=3)))
    assert isinstance(first, Tap)
    assert first.element_id == "e2"
    assert not eng.recheck(first, seen(_row_screen("T 1", "B", "C", rows=3)))
    taps = []
    for clock in range(2, 8):
        action = eng.next(seen(_row_screen(f"T {clock}", "B", "C", rows=3)))
        if isinstance(action, Stop):
            break
        assert isinstance(action, Tap)
        assert eng.recheck(action, seen(_row_screen(f"T {clock}x", "B", "C", rows=3)))
        taps.append(action.element_id)
    assert taps == ["e4", "e6"]
    assert action == Stop("exhausted")


def test_a_volatile_mark_belongs_to_one_screen_id() -> None:
    eng = started()
    tap = eng.next(seen(_row_screen("T 0", name="one")))
    assert isinstance(tap, Tap)
    assert not eng.recheck(tap, seen(_row_screen("T 1", name="one")))
    # the same structure on a screen with another id is its own element
    again = eng.next(seen(_row_screen("T 2", name="two")))
    assert isinstance(again, Tap)


# -- keyboard after a navigation BACK, same frame pops ----------------


def test_a_keyboard_after_a_navigation_back_does_not_resync_the_path() -> None:
    # Real stack: R, then A on top. BACK from A returns to R, which shows the
    # keyboard and an extra row while it is up (so another screen id).
    eng = started()
    root = on_screen(["Go", "Two"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    assert eng.next(on_screen([], name="A")) == Back()  # navigation BACK
    shown = on_screen(["Go", "Two", "Extra"], name="R", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK, not navigation
    action = eng.next(root)  # the screen under A: the predicted landing
    assert isinstance(action, Tap)
    assert action.element_id == "e3"  # "Two"
    assert eng.recheck(action, root)
    assert len(eng._path) == 1


def test_a_refused_recheck_reread_below_the_top_pops_instead_of_a_back() -> None:
    eng = started()
    root = on_screen(["Go", "Two"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    inner = eng.next(on_screen(["x"], name="A"))
    assert isinstance(inner, Tap)
    assert not eng.recheck(inner, root)  # A closed by itself; R is shown
    action = eng.next(root)
    assert isinstance(action, Tap)  # still the root: never a BACK
    assert action.element_id == "e3"
    assert len(eng._path) == 1  # A was popped, not replaced


def test_label_only_refusals_in_a_row_across_screens_are_bounded() -> None:
    # a frozen clock, another screen id on every next(), labels change at
    # every recheck: nothing is ever sent, yet the crawl stops
    def screen(index, tick):
        return seen(_row_screen(f"Synced {tick} s ago", name=f"s{index}"))

    eng = started()
    steps = 0
    action = eng.next(screen(0, 0))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, screen(steps - 1, steps))
        action = eng.next(screen(steps, steps))
    assert steps == crawl.MAX_VOLATILE
    assert action == Stop("exhausted")
    assert eng.actions_sent == 1


def test_label_only_refusals_in_a_row_on_an_inner_screen_end_with_a_back() -> None:
    eng = started()
    root = on_screen(["Go"], name="root")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)

    def screen(index, tick):
        return seen(_row_screen(f"Synced {tick} s ago", name=f"i{index}"))

    steps = 0
    action = eng.next(screen(0, 0))
    while isinstance(action, Tap):
        steps += 1
        assert steps < 50
        assert not eng.recheck(action, screen(steps - 1, steps))
        action = eng.next(screen(steps, steps))
    assert steps == crawl.MAX_VOLATILE
    assert action == Back()


def _deferred_base():
    eng = started()
    root = on_screen(["Go"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    assert eng.next(on_screen([], name="X")) == Back()  # navigation BACK
    return eng


def test_a_deferred_landing_where_back_did_nothing_gets_a_relaunch() -> None:
    # the BACK did nothing; the keyboard came up over X with an extra row
    eng = _deferred_base()
    shown = on_screen(["Extra"], name="X", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK
    assert eng.next(on_screen([], name="X")) == Relaunch()


def test_a_deferred_unpredicted_landing_is_resynced_to_the_depth_limit() -> None:
    eng = _deferred_base()
    shown = on_screen(["Y1", "Extra"], name="Y", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK
    assert eng.next(on_screen(["Y1"], name="Y")) == Back()
    assert len(eng._path) == eng._limits.depth


def test_off_target_after_the_keyboard_back_drops_the_deferral() -> None:
    eng = _deferred_base()
    shown = on_screen(["Y1", "Extra"], name="Y", keyboard=True)
    assert eng.next(shown) == Back()  # the keyboard BACK
    assert eng.next(off_screen()) == Back()  # off target: the deferral is gone
    assert eng._deferred is None
    # a later screen X is not taken as the deferred navigation BACK's landing
    assert eng.next(on_screen([], name="X")) != Relaunch()


def test_a_stuck_keyboard_after_a_deferred_landing_stops() -> None:
    # the navigation BACK lands on X with the keyboard shown; the keyboard BACK
    # leaves it shown, so the crawl stops instead of relaunching
    eng = _deferred_base()
    assert eng.next(on_screen([], name="X", keyboard=True)) == Back()
    assert eng.next(on_screen([], name="X", keyboard=True)) == Stop("keyboard_stuck")


def test_a_huge_clock_value_is_refused_not_raised() -> None:
    with pytest.raises(ValueError, match="too large"):
        Observation(None, PKG, keyboard_shown=False, now=10**400)
    with pytest.raises(ValueError, match="too large"):
        CrawlEngine(PKG, POLICY, Limits(), started_at=10**400)


def test_the_depth_limit_has_an_upper_bound() -> None:
    assert Limits(depth=crawl.MAX_DEPTH).depth == crawl.MAX_DEPTH
    with pytest.raises(ValueError, match="at most"):
        Limits(depth=crawl.MAX_DEPTH + 1)
    with pytest.raises(ValueError, match="at most"):
        Limits(depth=10**30)
    # an unpredicted landing at the largest depth still works
    eng = started(Limits(depth=crawl.MAX_DEPTH))
    root = on_screen(["Go"], name="R")
    go = eng.next(root)
    assert isinstance(go, Tap)
    assert eng.recheck(go, root)
    assert eng.next(on_screen([], name="X")) == Back()
    assert isinstance(eng.next(on_screen(["Z"], name="Z")), Back | Tap)


def test_an_oversized_screen_gets_one_skip_row_and_nothing_is_pressed() -> None:
    eng = started()
    big = on_screen([f"B{i}" for i in range(5001)], name="big")
    assert eng.next(big) == Stop("exhausted")
    rows = eng.records.skipped
    assert len(rows) == 1
    assert rows[0].reasons == ("screen_too_large",)
    assert rows[0].screen_id == big.screen.screen_id


def test_a_former_root_reached_by_back_is_not_a_root() -> None:
    eng = started()
    a = on_screen(["a1"], name="A")
    tap = eng.next(a)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, a)
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Back()
    assert eng.next(off_screen()) == Relaunch()
    c = on_screen(["c1", "c2"], name="C")
    tap = eng.next(c)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, c)
    assert eng.next(on_screen([], name="D")) == Back()  # navigation BACK
    # the BACK lands on A, the former root: it is above the real root
    assert eng.next(a) == Back()
    tap = eng.next(c)
    assert isinstance(tap, Tap)
    assert tap.fingerprint != fingerprint_of(c.screen, c.screen.elements[1])


def _root_replaced_then_unpredicted_landing(eng, root):
    # The root was replaced in place. Then root -> X -> Y (navigation BACK) and
    # an unpredicted landing Z is left by BACK onto the root: it stays the root.
    assert isinstance(eng.next(root), Tap)
    assert isinstance(eng.next(on_screen(["x1"], name="X")), Tap)
    assert eng.next(on_screen([], name="Y")) == Back()
    assert eng.next(on_screen([], name="Z")) == Back()
    last = eng.next(root)
    assert isinstance(last, Tap)  # the root still has its second element
    assert eng.recheck(last, root)
    assert eng.next(on_screen([], name="W")) == Back()
    assert eng.next(root) == Stop("exhausted")


def test_a_root_replaced_by_a_refused_recheck_stays_the_root() -> None:
    eng = started()
    first = on_screen(["r1", "r2"], name="A")
    tap = eng.next(first)
    assert isinstance(tap, Tap)
    fresh = on_screen(["r1", "r2"], name="B")
    assert not eng.recheck(tap, fresh)
    _root_replaced_then_unpredicted_landing(eng, fresh)


def test_a_root_replaced_by_the_keyboard_back_stays_the_root() -> None:
    eng = started()
    assert eng.next(on_screen(["r1", "r2"], name="R", keyboard=True)) == Back()
    _root_replaced_then_unpredicted_landing(eng, on_screen(["r1", "r2"], name="R2"))
