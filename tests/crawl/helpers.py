"""Shared helpers of the crawl engine tests: screens, a graph phone, drivers."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime

from uiprobe.crawl import (
    Action,
    Back,
    CrawlEngine,
    Launch,
    Limits,
    Observation,
    Relaunch,
    Reread,
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


# The engine as it was before BACK off target became a choice: BACK, BACK,
# then a relaunch on a screen of another app.
BACK_POLICY = Policy(back_on_other_screens=True)


def back_engine(limits=None, *, started_at=0.0):
    return engine(limits, started_at=started_at, policy=BACK_POLICY)


def back_started(limits=None, *, started_at=0.0):
    return started(limits, started_at=started_at, policy=BACK_POLICY)


def press(eng, observation):
    """Take a tap for the observation, recheck it on the same frame, send it."""
    tap = eng.next(observation)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, observation)
    eng.tap_sent(tap)
    return tap


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
        if isinstance(action, Tap):
            eng.tap_sent(action)
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


# -- dump failures -------------------------------------------------------------


def failed(now=0.0):
    return Observation(None, PKG, None, now)


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
