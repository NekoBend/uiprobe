"""The pressing crawl with ``Policy.except_phrases``: a row named exactly as a
phrase is pressed, the start line says so, and the tree reads the run."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import (
    SERIAL,
    SHOP,
    TRANSPORT,
    Clock,
    button,
    lines,
    run,
)
from tests.fake_device import El, FakeAndroid, Screen
from uiprobe.crawl_tree import GATE_WARNING, read_tree
from uiprobe.device import Device
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from pathlib import Path

PHRASED = Policy(except_phrases=("Quick Share",))


def screens() -> tuple[Screen, ...]:
    return (
        Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
        Screen(
            "main",
            f"{SHOP}/.Main",
            (
                El((0, 0, 320, 100), text="Shop"),
                button(200, "Quick Share", "done"),
                button(260, "Share", "done"),
                button(320, "Browse", "done"),
            ),
        ),
        Screen("done", f"{SHOP}/.Done", (El((0, 0, 320, 100), text="Done"),)),
    )


@pytest.fixture
def android(fake_adb) -> FakeAndroid:
    return FakeAndroid(
        fake_adb, SERIAL, screens(), start="home", transport_id=TRANSPORT
    )


@pytest.fixture
def device(android, fake_adb) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def taps(out: Path) -> list[dict]:
    return [
        r
        for r in lines(out / "transitions.jsonl")
        if r["schema"].startswith("uiprobe.transition_start")
        and r["action"]["kind"] == "tap"
    ]


@pytest.mark.usefixtures("android")
def test_the_row_is_pressed_and_marked_and_the_others_are_not(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out, policy=PHRASED)
    pressed = {r["action"]["element"]["text"]: r for r in taps(out)}
    assert set(pressed) == {"Quick Share", "Browse"}  # "Share" stays refused
    assert pressed["Quick Share"]["except_phrase"] == "Quick Share"
    assert "except_phrase" not in pressed["Browse"]
    start = json.loads((out / "run-start.json").read_text(encoding="utf-8"))
    assert start["deny_except_phrases"] == ["Quick Share"]
    skipped = lines(out / "skipped.jsonl")
    assert [s["reasons"] for s in skipped if "Quick Share" in json.dumps(s)] == []


@pytest.mark.usefixtures("android")
def test_the_tree_reads_a_run_with_the_mark(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out, policy=PHRASED)
    tree = read_tree(out)
    assert tree.warnings == []
    names = {
        row.name for node in tree.screens.values() for row in node.rows if row.pressed
    }
    assert {"Quick Share", "Browse"} <= names


@pytest.mark.usefixtures("android")
def test_without_phrases_nothing_is_marked_and_run_start_has_no_key(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out)
    assert all("except_phrase" not in r for r in lines(out / "transitions.jsonl"))
    start = json.loads((out / "run-start.json").read_text(encoding="utf-8"))
    assert "deny_except_phrases" not in start


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("bad", ["", 5, None])
def test_the_tree_gate_refuses_a_bad_mark(tmp_path, device, clock, bad):
    out = tmp_path / "run"
    run(device, clock, out, policy=PHRASED)
    path = out / "transitions.jsonl"
    changed = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if "except_phrase" in record:
            record["except_phrase"] = bad
        changed.append(json.dumps(record))
    path.write_text("\n".join(changed) + "\n", encoding="utf-8")
    assert GATE_WARNING in read_tree(out).warnings


@pytest.mark.usefixtures("android")
def test_the_tree_gate_refuses_a_mark_on_another_kind_of_action(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out)
    path = out / "transitions.jsonl"
    changed = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        record = json.loads(line)
        if number == 0:  # the launch's start line
            record["except_phrase"] = "Quick Share"
        changed.append(json.dumps(record))
    path.write_text("\n".join(changed) + "\n", encoding="utf-8")
    assert GATE_WARNING in read_tree(out).warnings
