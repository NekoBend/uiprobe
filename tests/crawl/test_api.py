"""Tests for the crawl engine's API: validation, action types, imports, withdrawing."""

from __future__ import annotations

import ast
import importlib
import random
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, get_args

import pytest

from tests.crawl.helpers import (
    PKG,
    POLICY,
    Phone,
    drive,
    engine,
    make_screen,
    off_screen,
    on_screen,
    press,
    random_graph,
    seen,
    small_graph,
    started,
)
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
)

LooseObservation: Any = Observation


LooseTap: Any = Tap


CRAWL_DIR = Path(crawl.__file__).parent


CRAWL_FILES = sorted(
    CRAWL_DIR.rglob("*.py"), key=lambda path: path.relative_to(CRAWL_DIR).as_posix()
)


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
    with pytest.raises(ValueError, match="below 2"):
        CrawlEngine(PKG, POLICY, Limits(), started_at=float("inf"))
    with pytest.raises(ValueError, match="below 2"):
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
    # Every module of the package that defines classes, not the re-exports.
    defined = {
        name
        for path in CRAWL_FILES
        if path.name != "__init__.py"
        for name, value in vars(
            importlib.import_module(f"{crawl.__name__}.{path.stem}")
        ).items()
        if isinstance(value, type)
        and is_dataclass(value)
        and value.__module__ == f"{crawl.__name__}.{path.stem}"
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
        "_State",
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
    "uiprobe.crawl.actions",
    "uiprobe.crawl.engine",
    "uiprobe.crawl.prints",
    "uiprobe.guards",
    "uiprobe.model",
}


def test_crawl_py_imports_only_approved_modules_and_never_calls_replace() -> None:
    # Every file of the package: the modules inside it are approved, nothing else new.
    assert [path.relative_to(CRAWL_DIR).as_posix() for path in CRAWL_FILES] == [
        "__init__.py",
        "actions.py",
        "engine.py",
        "prints.py",
    ]
    for path in CRAWL_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
        assert imported <= APPROVED, path.name


# -- withdraw ------------------------------------------------------------------


def _state(eng):
    records = eng.records
    return (records, eng.actions_sent, eng.start_screen_id, list(eng._path))


def test_a_withdrawn_back_or_relaunch_is_chosen_again_on_the_same_frame() -> None:
    for observation, kind in (
        (on_screen([], name="B"), Back),
        (off_screen(), Relaunch),
    ):
        eng = started()
        press(eng, on_screen(["Go"], name="A"))
        before = _state(eng)
        action = eng.next(observation)
        assert isinstance(action, kind)
        eng.withdraw(action)
        assert _state(eng) == before
        assert eng.next(observation) == action


def test_a_withdrawn_tap_is_not_tried_and_is_chosen_again() -> None:
    eng = started()
    screen = on_screen(["Item a", "Item b"])
    before = _state(eng)
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    assert eng.recheck(tap, screen)
    eng.withdraw(tap)
    assert _state(eng) == before
    again = eng.next(screen)
    assert again == tap
    assert not [s for s in eng.records.skipped if "tap_not_sent" in s.reasons]


def test_only_the_last_unsent_action_can_be_withdrawn() -> None:
    eng = started()
    screen = on_screen(["Item a", "Item b"])
    tap = eng.next(screen)
    assert isinstance(tap, Tap)
    with pytest.raises(ValueError, match="not the unsent action"):
        eng.withdraw(Tap(tap.screen_id, tap.element_id, tap.fingerprint, tap.point))
    assert eng.recheck(tap, screen)
    eng.tap_sent(tap)
    with pytest.raises(ValueError, match="not the unsent action"):
        eng.withdraw(tap)  # it was sent
    back = eng.next(on_screen([], name="X"))
    assert back == Back()
    eng.withdraw(back)
    with pytest.raises(ValueError, match="not the unsent action"):
        eng.withdraw(back)  # once only
    stop = eng.next(seen(make_screen(["Item a"]), now=10**6))
    assert isinstance(stop, Stop)
    with pytest.raises(ValueError, match="not the unsent action"):
        eng.withdraw(stop)


def test_withdrawing_every_action_once_changes_nothing() -> None:
    # Driving with each action withdrawn once and decided again on the same
    # frame gives the same crawl as driving without withdraws.
    rng = random.Random(4242)  # noqa: S311
    for _ in range(150):
        nodes = random_graph(rng)
        limits = Limits(depth=rng.randint(1, 6), actions=60, minutes=30)
        plain = drive(engine(limits), Phone(nodes))

        eng = engine(limits)
        phone = Phone(nodes)
        log = [eng.start()]
        phone.send(log[0])
        for _ in range(3000):
            observation = phone.observe()
            action = eng.next(observation)
            if isinstance(action, Tap | Back | Relaunch):
                eng.withdraw(action)
                again = eng.next(observation)
                assert again == action
                action = again  # the instance the engine now expects
            log.append(action)
            if isinstance(action, Stop):
                break
            if isinstance(action, Tap):
                assert eng.recheck(action, phone.observe())
            phone.send(action)
            if isinstance(action, Tap):
                eng.tap_sent(action)
        assert log == plain


def test_the_snapshot_holds_every_field_a_decision_can_change() -> None:
    # withdraw restores every field listed in _State; a field added to the
    # engine and not to _State would survive a withdraw.
    eng = engine()
    drive(eng, Phone(small_graph()))
    fixed = {"_package", "_policy", "_limits", "_started_at", "_returned"}
    held = {f"_{f.name}" for f in fields(crawl._State)}
    assert set(vars(eng)) - fixed == held
    # and the values a snapshot keeps are copies, not the engine's own
    state = eng._snapshot()
    for f in fields(crawl._State):
        value = getattr(state, f.name)
        if isinstance(value, list | set | dict) or type(value).__name__ == "_Pending":
            assert value is not getattr(eng, f"_{f.name}"), f.name


def test_skip_records_keep_only_the_details_of_word_reasons() -> None:
    import typing

    eng = started()
    screen = on_screen(["42", "Delete", "Item a"])
    assert isinstance(eng.next(screen), Tap)
    by_reasons = {r.reasons: r for r in eng.records.skipped}
    number = by_reasons[("number_only",)]
    assert number.details == ()
    assert number == SkippedElement(
        number.screen_id, number.fingerprint, ("number_only",)
    )
    deny = by_reasons[("deny_word",)]
    assert [d.reason for d in deny.details] == ["deny_word"]
    assert deny.details[0].word == "delete"
    hints = typing.get_type_hints(SkippedElement)
    assert hints["details"] == tuple[crawl.Detail, ...]
