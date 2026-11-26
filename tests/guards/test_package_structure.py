"""Tests for the structure of the guards package."""

from __future__ import annotations

import ast
import importlib
import inspect
import typing
from pathlib import Path

import pytest

from uiprobe import guards

GUARDS_DIR = Path(guards.__file__).parent


GUARDS_FILES = sorted(
    GUARDS_DIR.rglob("*.py"), key=lambda path: path.relative_to(GUARDS_DIR).as_posix()
)


# -- imports ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", GUARDS_FILES, ids=lambda path: path.relative_to(GUARDS_DIR).as_posix()
)
def test_guards_imports_only_allowed_modules(path: Path) -> None:
    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "re",
        "typing",
        "unicodedata",
        "uiprobe.model",
        "uiprobe.scrollgeom",
        "uiprobe.guards.constants",
        "uiprobe.guards.judge",
        "uiprobe.guards.matching",
        "uiprobe.guards.policy",
        "uiprobe.guards.scroll",
        "uiprobe.guards.tree",
        "uiprobe.guards.words",
    }
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module or "")
    assert imported <= allowed
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"open", "exec", "eval", "__import__"}


def test_the_guards_package_holds_exactly_the_known_files() -> None:
    assert [path.relative_to(GUARDS_DIR).as_posix() for path in GUARDS_FILES] == [
        "__init__.py",
        "constants.py",
        "judge.py",
        "matching.py",
        "policy.py",
        "scroll.py",
        "tree.py",
        "words.py",
    ]


def test_every_annotation_in_the_guards_package_resolves() -> None:
    # Names that were only imported for type checking in the single module;
    # a test may supply them, nothing else may be missing at run time.
    from collections.abc import Iterable, Sequence

    from uiprobe.guards.policy import Judgement
    from uiprobe.model import Element, Screen

    extra = {
        "Judgement": Judgement,
        "Iterable": Iterable,
        "Sequence": Sequence,
        "Element": Element,
        "Screen": Screen,
    }
    functions = []
    for name in ("constants", "judge", "matching", "policy", "scroll", "tree", "words"):
        module = importlib.import_module(f"uiprobe.guards.{name}")
        for value in vars(module).values():
            if getattr(value, "__module__", None) != module.__name__:
                continue
            if inspect.isclass(value):
                functions += [
                    member
                    for member in vars(value).values()
                    if inspect.isfunction(member)
                ]
            elif inspect.isfunction(value):
                functions.append(value)
    assert len(functions) > 100
    for function in functions:
        typing.get_type_hints(function, localns=extra)
