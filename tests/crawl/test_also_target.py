"""The crawl engine with ``Policy.also_target``: a listed package's screens are
target screens (depth, tried marks, the path), every other package is as before,
and an empty list changes nothing."""

from __future__ import annotations

import random

from tests.crawl.helpers import (
    LAUNCHER,
    OTHER,
    PERMISSION,
    PKG,
    Node,
    Phone,
    drive,
    engine,
    make_screen,
    random_graph,
    seen,
    started,
)
from uiprobe.crawl import Back, Limits, Relaunch, Stop, Tap
from uiprobe.guards import Policy

P = "com.example.helper"
LISTED = Policy(also_target=(P,))


def listed_graph(third: str = OTHER):
    """The shop opens the helper's screens (listed) and another app's (not)."""
    return [
        Node("n0", (("Open helper", "p0"), ("Open third", "q0"), ("Item", "n1"))),
        Node("n1", (("Detail", "n1"),)),
        Node("p0", (("Deeper", "p1"), ("Again", "p0")), package=P),
        Node("p1", (("Leaf", "p1"), ("Back to shop", "n0")), package=P),
        Node("q0", (("Never", "q0"),), package=third),
    ]


def packages_of(phone: Phone, log) -> list[str]:
    del log
    return [phone.nodes[name].package for name in phone.visited if name in phone.nodes]


# -- a listed package's screens are target screens -------------------------------


def moves(phone: Phone, eng) -> list[tuple[str, str | None, str | None]]:
    """The transitions by node name (screen ids differ with the package)."""
    names = {phone.screen_id(n): n for n in phone.nodes}
    return [
        (t.kind, names.get(t.from_screen_id), names.get(t.to_screen_id))
        for t in eng.records.transitions
    ]


def test_the_listed_packages_screens_are_pressed_as_the_targets_are():
    nodes = [n for n in listed_graph() if n.name != "q0"]
    nodes[0] = Node("n0", (("Open helper", "p0"), ("Item", "n1")))
    phone = Phone(nodes)
    eng = engine(Limits(depth=5), policy=LISTED)
    log = drive(eng, phone, policy=LISTED)
    assert log[-1] == Stop("exhausted")
    # the same graph with every node in the shop's package, no list: the very
    # same moves, so the helper's screens got depth, tried marks, the path and
    # BACK exactly as the shop's own
    same = Phone([Node(n.name, n.buttons) for n in nodes])
    plain = engine(Limits(depth=5))
    plain_log = drive(plain, same)
    assert moves(phone, eng) == moves(same, plain)
    assert kinds(log) == kinds(plain_log)
    pressed = {m[1] for m in moves(phone, eng) if m[0] == "tap"}
    assert {"p0", "p1"} <= pressed
    by_screen = {}
    for t in eng.records.transitions:
        if t.kind == "tap":
            by_screen.setdefault(t.from_screen_id, []).append(t.fingerprint)
    for fingerprints in by_screen.values():
        assert len(fingerprints) == len(set(fingerprints))  # tried once each
    ids = {name: phone.screen_id(name) for name in phone.nodes}
    assert len(by_screen[ids["p0"]]) == 2
    assert eng.depth_of(ids["n0"]) == 1
    assert eng.depth_of(ids["p0"]) == 2
    assert eng.depth_of(ids["p1"]) == 3
    assert not any(isinstance(a, Relaunch) for a in log)
    assert eng.records.screens_seen == plain.records.screens_seen


def test_the_depth_limit_counts_the_helpers_screens():
    phone = Phone(
        [
            Node("n0", (("Open helper", "p0"),)),
            Node("p0", (("Deeper", "p1"),), package=P),
            Node("p1", (("Leaf", "p1"),), package=P),
        ]
    )
    eng = engine(Limits(depth=2), policy=LISTED)
    log = drive(eng, phone, policy=LISTED, depth=2)
    assert log[-1] == Stop("exhausted")
    pressed = {t.from_screen_id for t in eng.records.transitions if t.kind == "tap"}
    assert pressed == {phone.screen_id("n0")}  # p0 is at depth 2: not pressed


def test_a_listed_screen_gets_navigation_back_like_a_target_screen():
    phone = Phone(
        [
            Node("n0", (("Open helper", "p0"),)),
            Node("p0", (("Leaf", "p1"),), package=P),
            Node("p1", (), package=P),
        ]
    )
    eng = engine(Limits(depth=5), policy=LISTED)
    log = drive(eng, phone, policy=LISTED)
    backs = [a for a in log if isinstance(a, Back)]
    assert len(backs) == 2  # p1 to p0 to n0, as in the shop
    assert log[-1] == Stop("exhausted")


def test_the_screen_and_the_foreground_must_both_name_the_listed_package():
    eng = started(policy=LISTED)
    helper = make_screen(["Open"], package=P)
    # the helper's dump with the shop in front is not a target screen
    assert not isinstance(eng.next(seen(helper, fg=PKG)), Tap)
    eng = started(policy=LISTED)
    assert isinstance(eng.next(seen(helper, fg=P)), Tap)


def test_a_listed_screen_is_not_a_target_without_the_list():
    helper = make_screen(["Open"], package=P)
    eng = started()
    assert isinstance(eng.next(seen(helper, fg=P)), Relaunch)


def test_a_permission_package_screen_stays_off_target_even_when_listed():
    permission = make_screen(["Allow"], package=PERMISSION)
    policy = Policy(also_target=(PERMISSION,))
    eng = started(policy=policy)
    # never the app's own: no tap and no BACK, the app is relaunched, as
    # without the list (the guard of `judge` is tested in tests/guards)
    assert isinstance(eng.next(seen(permission, fg=PERMISSION)), Relaunch)
    assert not eng.records.skipped
    back = Policy(also_target=(PERMISSION,), back_on_other_screens=True)
    eng = started(policy=back)
    assert isinstance(eng.next(seen(permission, fg=PERMISSION)), Back)  # off target


# -- unlisted packages are handled as before --------------------------------------


def test_an_unlisted_package_is_relaunched_not_pressed():
    phone = Phone(listed_graph())
    eng = engine(Limits(depth=5), policy=LISTED)
    log = drive(eng, phone, policy=LISTED)
    assert isinstance(log[-1], Stop)
    assert any(isinstance(a, Relaunch) for a in log)
    third = phone.screen_id("q0")
    assert all(
        t.from_screen_id != third for t in eng.records.transitions if t.kind == "tap"
    )


def test_with_back_on_other_screens_an_unlisted_package_gets_back_not_taps():
    policy = Policy(also_target=(P,), back_on_other_screens=True)
    phone = Phone(listed_graph())
    eng = engine(Limits(depth=5), policy=policy)
    log = drive(eng, phone, policy=policy)
    third = phone.screen_id("q0")
    assert all(
        t.from_screen_id != third for t in eng.records.transitions if t.kind == "tap"
    )
    assert any(isinstance(a, Back) for a in log)
    assert isinstance(log[-1], Stop)


def kinds(log) -> list[str]:
    return [type(a).__name__ for a in log]


def test_the_listed_package_unlisted_is_the_same_as_another_app():
    # With the list empty, the helper's screens act like any other app's: the
    # engine's sequence of actions equals that of a graph whose helper nodes are
    # in another package.
    nodes_p = listed_graph()
    nodes_o = [
        Node(
            n.name,
            n.buttons,
            package=OTHER if n.package == P or n.name in ("p0", "p1") else n.package,
        )
        for n in nodes_p
    ]
    log_p = drive(engine(Limits(depth=5)), Phone(nodes_p))
    log_o = drive(engine(Limits(depth=5)), Phone(nodes_o))
    assert kinds(log_p) == kinds(log_o)
    log_l = drive(engine(Limits(depth=5), policy=LISTED), Phone(nodes_p), policy=LISTED)
    assert kinds(log_l) != kinds(log_o)


# -- an empty list is the default ---------------------------------------------------


def graph_with_apps(rng: random.Random):
    """A random graph whose nodes are also in an unlisted app, the launcher
    and a permission package."""
    nodes = random_graph(rng)
    out = []
    for node in nodes:
        roll = rng.random()
        if node.name != "n0" and roll < 0.25:
            package = rng.choice([OTHER, P, LAUNCHER])
            out.append(Node(node.name, node.buttons, package=package))
        else:
            out.append(node)
    return out


def snapshot(eng, log):
    return (
        kinds(log),
        [
            (t.kind, t.from_screen_id, t.fingerprint, t.point, t.to_screen_id)
            for t in eng.records.transitions
        ],
        [(s.screen_id, s.fingerprint, s.reasons) for s in eng.records.skipped],
        eng.records.screens_seen,
        eng.records.actions_sent,
    )


def test_an_empty_list_equals_the_default_on_random_graphs():
    rng = random.Random(20261126)  # noqa: S311
    for _ in range(300):
        nodes = graph_with_apps(rng)
        limits = Limits(
            depth=rng.randint(1, 6),
            actions=rng.randint(1, 80),
            minutes=rng.choice([1, 2, 30]),
        )
        back = rng.random() < 0.3
        default = Policy(back_on_other_screens=back)
        empty = Policy(back_on_other_screens=back, also_target=())
        eng_a = engine(limits, policy=default)
        eng_b = engine(limits, policy=empty)
        log_a = drive(eng_a, Phone(nodes), policy=default)
        log_b = drive(eng_b, Phone(nodes), policy=empty)
        assert snapshot(eng_a, log_a) == snapshot(eng_b, log_b)
        assert log_a == log_b


def test_random_graphs_with_a_list_keep_every_rule_and_terminate():
    rng = random.Random(20261127)  # noqa: S311
    for _ in range(300):
        nodes = graph_with_apps(rng)
        limits = Limits(
            depth=rng.randint(1, 6),
            actions=rng.randint(1, 80),
            minutes=rng.choice([1, 2, 30]),
        )
        policy = Policy(also_target=(P,), back_on_other_screens=rng.random() < 0.3)
        phone = Phone(nodes)
        eng = engine(limits, policy=policy)
        log = drive(eng, phone, policy=policy)
        assert isinstance(log[-1], Stop)
        sent = [a for a in log if not isinstance(a, Stop)]
        assert len(sent) == eng.actions_sent <= limits.actions
        by_id = {phone.screen_id(n.name): n for n in phone.nodes.values()}
        for transition in eng.records.transitions:
            if transition.kind == "tap":
                node = by_id[transition.from_screen_id]
                assert node.package in (PKG, P)  # never another app's screen
        keys = [
            (t.from_screen_id, t.fingerprint)
            for t in eng.records.transitions
            if t.kind == "tap" and t.action_id not in eng.replay_actions
        ]
        assert len(keys) == len(set(keys))
