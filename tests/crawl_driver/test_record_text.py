"""Tests for record_text: password fields and texts kept out of the records.

The crawl itself must not change: ids, fingerprints, judging and the actions
sent use the real texts, only the writer hides them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import (
    SERIAL,
    TRANSPORT,
    Clock,
    lines,
    run,
)
from tests.fake_device import FakeAndroid
from tests.marker_screens import (
    HIDDEN_DESC,
    HIDDEN_HINT,
    HIDDEN_MARKER,
    OVERLAY,
    SCREENS,
    TEXT_MARKERS,
    WINDOW_MARKER,
)
from uiprobe.crawl import Limits
from uiprobe.crawl_tree import read_tree
from uiprobe.device import Device

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address, pin=True)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def cover_main(android: FakeAndroid, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a window titled with a marker over the first button once main shows."""
    original = android.show

    def show_then_cover(name: str) -> None:
        original(name)
        android.overlays.clear()
        if name == "main":
            android.overlays.append((WINDOW_MARKER, OVERLAY, (0, 180, 320, 260)))

    monkeypatch.setattr(android, "show", show_then_cover)


def every_text(folder: Path) -> str:
    """The text of every file of the run folder (a screenshot would be bytes)."""
    return "\n".join(
        path.read_bytes().decode("utf-8", "replace")
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    )


def reset(android: FakeAndroid) -> None:
    android.stack[:] = ["home"]
    android.inputs.clear()
    android.stopped.clear()
    android.overlays.clear()


def shape(folder: Path) -> dict[str, object]:
    """What must not depend on record_text: ids, fingerprints, reasons, actions."""
    transitions = []
    for record in lines(folder / "transitions.jsonl"):
        record.pop("recorded_at")
        action = record.get("action")
        if isinstance(action, dict):
            action.pop("element", None)
            record["action"] = action
        record.get("details", {}).pop("window", None)
        record.get("details", {}).pop("hidden", None)
        transitions.append(record)
    skips = []
    for record in lines(folder / "skipped.jsonl"):
        record.pop("recorded_at")
        record.pop("element", None)
        for detail in record.get("details", []):
            detail.pop("note", None)
            detail.pop("hidden", None)
        skips.append(record)
    return {
        "screens": sorted(p.name for p in (folder / "screens").iterdir()),
        "transitions": transitions,
        "skips": skips,
    }


def test_the_crawl_does_the_same_with_record_text_off(
    tmp_path: Path, device, android, clock, monkeypatch
):
    cover_main(android, monkeypatch)
    on = tmp_path / "on"
    run(device, clock, on, limits=Limits(actions=12), record_text=True)
    sent_on = [(i.kind, i.args, i.screen, i.point) for i in android.inputs]
    reset(android)
    cover_main(android, monkeypatch)
    off = tmp_path / "off"
    run(device, clock, off, limits=Limits(actions=12), record_text=False)
    sent_off = [(i.kind, i.args, i.screen, i.point) for i in android.inputs]
    assert sent_on == sent_off
    assert len(sent_on) > 3
    assert shape(on) == shape(off)
    # Both runs saw the covered tap, so the window case is in the comparison.
    reasons = [r.get("reason") for r in lines(off / "transitions.jsonl")]
    assert "covered_by_other" in reasons
    assert (
        json.loads((on / "run.json").read_text())["screens"]
        == json.loads((off / "run.json").read_text())["screens"]
    )


def test_with_record_text_off_no_marker_is_left_anywhere(
    tmp_path: Path, device, android, clock, monkeypatch, capsys
):
    cover_main(android, monkeypatch)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=12), record_text=False)
    captured = capsys.readouterr()
    found = every_text(out) + captured.out + captured.err
    for marker in (*TEXT_MARKERS, WINDOW_MARKER, HIDDEN_MARKER):
        assert marker not in found, marker
    assert HIDDEN_DESC not in found  # content_desc and hint are texts too
    assert HIDDEN_HINT not in found
    begin = json.loads((out / "run-start.json").read_text(encoding="utf-8"))
    assert begin["record_text"] is False
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "[text not recorded]" in summary
    covered = [
        r
        for r in lines(out / "transitions.jsonl")
        if r.get("reason") == "covered_by_other"
    ]
    assert covered
    assert covered[0]["details"] == {
        "window": "",
        "owner_uid": android.uid_of(OVERLAY),
        "hidden": ["window"],
    }


def test_with_record_text_on_the_password_text_is_still_not_written(
    tmp_path: Path, device, android, clock, monkeypatch, capsys
):
    cover_main(android, monkeypatch)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=12), record_text=True)
    captured = capsys.readouterr()
    found = every_text(out) + captured.out + captured.err
    assert HIDDEN_MARKER not in found
    # Everything else is kept, the password field's description and hint too.
    for marker in ("MARKtextShop", "MARKbuttonText", "MARKpageTitle", WINDOW_MARKER):
        assert marker in found, marker
    assert HIDDEN_DESC in found
    assert HIDDEN_HINT in found
    page = next(
        json.loads(p.read_text(encoding="utf-8"))
        for p in (out / "screens").glob("*.json")
        if "MARKpageTitle" in p.read_text(encoding="utf-8")
    )
    field = next(e for e in page["elements"] if "password" in e.get("flags", []))
    assert field["text"] == ""
    assert field["hidden"] == ["text"]
    assert field["content_desc"] == HIDDEN_DESC
    assert field["hint"] == HIDDEN_HINT
    begin = json.loads((out / "run-start.json").read_text(encoding="utf-8"))
    assert begin["record_text"] is True


@pytest.mark.usefixtures("android")
def test_with_record_text_off_each_text_is_empty_and_named(
    tmp_path: Path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=12), record_text=False)
    elements = [
        e
        for p in (out / "screens").glob("*.json")
        for e in json.loads(p.read_text(encoding="utf-8"))["elements"]
    ]
    first = next(e for e in elements if len(e.get("hidden", [])) == 3)
    assert first["text"] == first["content_desc"] == first["hint"] == ""
    assert first["hidden"] == ["text", "content_desc", "hint"]
    # An element with only a text names only the text.
    assert any(
        e.get("hidden") == ["text"] and "content_desc" not in e for e in elements
    )
    assert first["class"]  # structure stays
    assert first["bounds"]
    starts = [
        r
        for r in lines(out / "transitions.jsonl")
        if r.get("action", {}).get("element")
    ]
    assert starts
    element = starts[0]["action"]["element"]
    assert element["text"] == element["content_desc"] == element["hint"] == ""
    assert element["hidden"] == ["text", "content_desc", "hint"]


def test_a_run_folder_with_record_text_off_reads_as_a_tree(
    tmp_path: Path, device, android, clock, monkeypatch
):
    cover_main(android, monkeypatch)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=12), record_text=False)
    tree = read_tree(out)
    assert tree.record_text is False
    assert tree.warnings == []
    assert tree.screens
    # The transitions gate accepted every line, tap elements with hidden and
    # the covered_by_other details with hidden: results are shown.
    pressed = [r for s in tree.screens.values() for r in s.rows if r.pressed]
    assert pressed
    covered = [
        r
        for s in tree.screens.values()
        for r in s.rows
        if r.to and r.to.get("not_sent") == "covered_by_other"
    ]
    assert covered


@pytest.mark.usefixtures("android")
def test_a_run_folder_with_record_text_on_keeps_the_password_row(
    tmp_path: Path, device, clock
):
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=12), record_text=True)
    tree = read_tree(out)
    assert tree.record_text is True
    assert tree.warnings == []
    rows = [r for s in tree.screens.values() for r in s.rows]
    assert any(r.name == "[password field]" and r.hidden for r in rows)
    assert not any(HIDDEN_MARKER in " ".join((r.name, *r.texts)) for r in rows)


def test_screenshots_with_record_text_off_are_refused_before_anything(
    tmp_path: Path, device, android, clock
):
    out = tmp_path / "run"
    with pytest.raises(ValueError, match="screenshot shows the text"):
        run(device, clock, out, screenshots=True, record_text=False)
    assert not out.exists()
    assert android.inputs == []
    assert android.stopped == []


@pytest.mark.usefixtures("android")
def test_record_text_must_be_a_bool(tmp_path: Path, device, clock):
    with pytest.raises(TypeError):
        run(device, clock, tmp_path / "run", record_text=1)  # ty: ignore[invalid-argument-type]
