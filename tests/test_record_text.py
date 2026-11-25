"""RunWriter and the tree command with password fields and record_text off."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from uiprobe import cli
from uiprobe.crawl import Limits, fingerprint_of
from uiprobe.crawl_tree import GATE_WARNING, read_tree
from uiprobe.model import Bounds, Element, Screen
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from pathlib import Path

PACKAGE = "com.example.app"
_WRITERS: list[RunWriter] = []


@pytest.fixture(autouse=True)
def _close_writers():
    yield
    while _WRITERS:
        with contextlib.suppress(RunFolderError):
            _WRITERS.pop().close()


def el(number, parent=None, cls="TextView", text="", desc="", hint="", flags=()):
    return Element(
        id=f"e{number}",
        parent=None if parent is None else f"e{parent}",
        class_name=f"android.widget.{cls}",
        bounds=Bounds(0, number * 10, 100, number * 10 + 10),
        text=text,
        content_desc=desc,
        hint=hint,
        flags=tuple(flags),
    )


def screen(elements, activity=".Main"):
    return Screen(
        serial="emulator-5554",
        captured_at=datetime(2026, 3, 1, 9, 31, tzinfo=UTC),
        package=PACKAGE,
        activity=activity,
        api_level=36,
        display=(1080, 2400),
        elements=tuple(elements),
    )


HOME = screen(
    [
        el(1, text="Plain title", hint="Plain hint"),
        el(2, cls="Button", text="Sign in", desc="Sign in button", flags=["clickable"]),
        el(
            3,
            cls="EditText",
            text="hunter2",
            desc="Password box",
            hint="Type it",
            flags=["clickable", "password"],
        ),
        el(4, cls="EditText", text="", flags=["password"]),
        el(5, cls="Button", desc="Only desc", flags=["clickable"]),
    ]
)
NEXT = screen([el(1, text="Next page")], ".Next")


def writer(path: Path, **more) -> RunWriter:
    path.mkdir(parents=True, exist_ok=True)
    made = RunWriter.create(
        path / "run",
        package=PACKAGE,
        limits=Limits(),
        deny_words=["delete"],
        serial="emulator-5554",
        **more,
    )
    _WRITERS.append(made)
    return made


def read(path: Path, name: str):
    return (path / "run" / name).read_text(encoding="utf-8")


def screen_file(path: Path, shown: Screen):
    return json.loads(read(path, f"screens/{shown.screen_id}.json"))


def records(path: Path):
    return [json.loads(x) for x in read(path, "transitions.jsonl").splitlines()]


def test_a_password_field_is_hidden_with_no_switch(tmp_path):
    w = writer(tmp_path)
    w.write_screen(HOME)
    data = screen_file(tmp_path, HOME)
    by_id = {e["id"]: e for e in data["elements"]}
    secret = by_id["e3"]
    assert secret["text"] == ""
    assert secret["hidden"] == ["text"]
    assert secret["content_desc"] == "Password box"
    assert secret["hint"] == "Type it"
    # an empty password text has nothing to hide, so nothing is named
    assert "text" not in by_id["e4"]
    assert "hidden" not in by_id["e4"]
    # other elements are written as before
    assert by_id["e1"]["text"] == "Plain title"
    assert "hidden" not in by_id["e1"]
    assert "hunter2" not in read(tmp_path, f"screens/{HOME.screen_id}.json")


def test_a_tapped_password_element_is_hidden_in_the_start_line(tmp_path):
    w = writer(tmp_path)
    w.write_screen(HOME)
    w.write_transition_start(1, "tap", HOME.screen_id, HOME.elements[2], (5, 35))
    w.write_transition_end(1, HOME.screen_id)
    element = records(tmp_path)[0]["action"]["element"]
    assert element["text"] == ""
    assert element["hidden"] == ["text"]
    assert element["content_desc"] == "Password box"
    assert "hunter2" not in read(tmp_path, "transitions.jsonl")


def test_a_mapping_element_is_hidden_the_same_way(tmp_path):
    w = writer(tmp_path, record_text=False)
    given = {"id": "e9", "class": "X", "text": "t", "hint": "h", "bounds": [0, 0, 1, 1]}
    w.write_transition_start(1, "tap", HOME.screen_id, given, (5, 5))
    element = records(tmp_path)[0]["action"]["element"]
    assert element["text"] == element["hint"] == ""
    assert element["hidden"] == ["text", "hint"]
    assert given["text"] == "t"  # the caller's mapping is not touched


def test_the_summary_names_password_fields_and_hidden_texts(tmp_path):
    w = writer(tmp_path)
    w.write_screen(HOME)
    w.finish("exhausted", screens=1, actions=0)
    summary = read(tmp_path, "summary.txt")
    assert '  "[password field]"' in summary or "  [password field]" in summary
    assert "hunter2" not in summary
    assert "Sign in" in summary
    assert "Only desc" in summary


def test_record_text_off_hides_every_text(tmp_path):
    w = writer(tmp_path / "x", record_text=False)
    w.write_screen(HOME)
    w.write_transition_start(1, "tap", HOME.screen_id, HOME.elements[1], (5, 25))
    w.write_transition_end(1, NEXT.screen_id)
    w.finish("exhausted", screens=1, actions=1)
    base = tmp_path / "x"
    data = screen_file(base, HOME)
    by_id = {e["id"]: e for e in data["elements"]}
    assert by_id["e1"]["text"] == by_id["e1"]["hint"] == ""
    assert by_id["e1"]["hidden"] == ["text", "hint"]
    assert by_id["e2"]["hidden"] == ["text", "content_desc"]
    assert by_id["e3"]["hidden"] == ["text", "content_desc", "hint"]
    assert by_id["e5"]["hidden"] == ["content_desc"]
    assert "hidden" not in by_id["e4"]
    assert by_id["e2"]["class"] == "android.widget.Button"
    assert by_id["e2"]["flags"] == ["clickable"]
    assert by_id["e2"]["bounds"] == [0, 20, 100, 30]
    assert json.loads(read(base, "run-start.json"))["record_text"] is False
    summary = read(base, "summary.txt")
    assert "[text not recorded]" in summary
    assert "[password field]" in summary
    for word in ("Sign in", "Plain title", "Only desc", "hunter2", "Type it"):
        assert word not in read(base, f"screens/{HOME.screen_id}.json")
        assert word not in summary
        assert word not in read(base, "transitions.jsonl")


def test_the_screen_id_and_fingerprints_do_not_depend_on_what_is_recorded(tmp_path):
    first = writer(tmp_path / "a")
    second = writer(tmp_path / "b", record_text=False)
    for made in (first, second):
        made.write_screen(HOME)
        made.write_skip(
            HOME.screen_id,
            fingerprint_of(HOME, HOME.elements[2]),
            ["password_field"],
            HOME.elements[2],
        )
    a = json.loads(read(tmp_path / "a", "skipped.jsonl"))
    b = json.loads(read(tmp_path / "b", "skipped.jsonl"))
    assert a["fingerprint"] == b["fingerprint"]
    assert a["element"]["hidden"] == ["text"]
    assert b["element"]["hidden"] == ["text", "content_desc", "hint"]
    for text in (read(tmp_path / "a", "skipped.jsonl"),):
        assert "hunter2" not in text
    assert "Password box" not in read(tmp_path / "b", "skipped.jsonl")


def test_a_skip_detail_note_that_holds_a_label_is_hidden_with_record_text_off(
    tmp_path,
):
    details = [
        {
            "reason": "confirm_button",
            "word": "delete",
            "field": "text",
            "element_id": "e2",
            "note": "Yes, really",
        }
    ]
    for name, flag in (("on", True), ("off", False)):
        w = writer(tmp_path / name, record_text=flag)
        w.write_skip(
            HOME.screen_id, "f-1", ["confirm_button"], HOME.elements[1], details
        )
    on = json.loads(read(tmp_path / "on", "skipped.jsonl"))
    off = json.loads(read(tmp_path / "off", "skipped.jsonl"))
    assert on["details"][0]["note"] == "Yes, really"
    assert off["details"][0]["note"] == ""
    assert off["details"][0]["hidden"] == ["note"]
    assert off["details"][0]["word"] == "delete"  # a configured word stays
    assert details[0]["note"] == "Yes, really"


def test_the_window_of_a_covered_tap_is_hidden_and_the_uid_stays(tmp_path):
    for name, flag in (("on", True), ("off", False)):
        w = writer(tmp_path / name, record_text=flag)
        w.write_transition_start(1, "tap", HOME.screen_id, HOME.elements[1], (5, 25))
        w.write_transition_end(
            1, not_sent="covered_by_other", details={"window": "Chat", "owner_uid": 5}
        )
    assert records(tmp_path / "on")[1]["details"] == {"window": "Chat", "owner_uid": 5}
    assert records(tmp_path / "off")[1]["details"] == {
        "window": "",
        "owner_uid": 5,
        "hidden": ["window"],
    }


def test_the_writer_refuses_a_screenshot_when_texts_are_not_recorded(tmp_path):
    w = writer(tmp_path, record_text=False)
    with pytest.raises(ValueError, match="screenshots show"):
        w.write_screenshot(HOME.screen_id, b"\x89PNG\r\n\x1a\nxxxx")
    assert not (tmp_path / "run" / "screens").exists()


def test_record_text_must_be_a_bool_or_none(tmp_path):
    with pytest.raises(TypeError, match="record_text"):
        writer(tmp_path, record_text="no")
    assert not (tmp_path / "run").exists()


def test_run_start_has_the_key_only_when_given(tmp_path):
    writer(tmp_path / "none")
    writer(tmp_path / "true", record_text=True)
    assert "record_text" not in json.loads(read(tmp_path / "none", "run-start.json"))
    assert json.loads(read(tmp_path / "true", "run-start.json"))["record_text"] is True


# -- the tree command reads what the writer wrote ------------------------------------


def folder_with(tmp_path: Path, *, record_text: bool) -> Path:
    w = writer(tmp_path, record_text=record_text)
    home = HOME.screen_id
    w.write_screen(HOME)
    w.write_screen(NEXT)
    for number, index, to in ((1, 1, NEXT.screen_id), (2, 2, HOME.screen_id)):
        w.write_transition_start(
            number, "tap", home, HOME.elements[index], (5, index * 10 + 5)
        )
        w.write_transition_end(number, to)
    w.write_transition_start(3, "tap", home, HOME.elements[4], (5, 55))
    w.write_transition_end(
        3, not_sent="covered_by_other", details={"window": "Chat", "owner_uid": 7}
    )
    w.finish("exhausted", screens=2, actions=3)
    return tmp_path / "run"


def test_the_tree_of_a_run_with_record_text_off_has_no_texts_and_no_warning(tmp_path):
    base = folder_with(tmp_path, record_text=False)
    tree = read_tree(base)
    assert tree.record_text is False
    assert GATE_WARNING not in tree.warnings
    assert tree.warnings == []
    rows = {r.element_id: r for r in tree.screens[HOME.screen_id].rows}
    assert rows["e2"].pressed
    assert rows["e5"].to == {"not_sent": "covered_by_other"}
    shown = json.dumps([[r.name, list(r.texts)] for r in rows.values()])
    for word in ("Sign in", "Only desc", "hunter2"):
        assert word not in shown


def test_the_tree_of_a_run_with_the_password_hidden_keeps_the_password_row(tmp_path):
    base = folder_with(tmp_path, record_text=True)
    tree = read_tree(base)
    assert tree.warnings == []
    rows = {r.element_id: r for r in tree.screens[HOME.screen_id].rows}
    assert rows["e3"].name == "[password field]"
    assert rows["e3"].hidden
    # the empty password field has no row, as before
    assert "e4" not in rows
    assert rows["e2"].name == "Sign in"
    assert rows["e2"].pressed
    assert rows["e5"].to == {"not_sent": "covered_by_other"}


@pytest.mark.parametrize("record_text", [True, False])
def test_the_tree_command_prints_no_secret(tmp_path, capsys, record_text):
    base = folder_with(tmp_path, record_text=record_text)
    assert cli.run(["tree", str(base)]) == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert ("Sign in" in out) is record_text


def edit_lines(base: Path, change) -> None:
    path = base / "transitions.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        change(row)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_the_gate_still_refuses_a_hidden_key_that_does_not_fit(tmp_path):
    cases = {
        "not a list": lambda e: e.update(hidden="text"),
        "empty": lambda e: e.update(hidden=[]),
        "unknown field": lambda e: e.update(hidden=["resource_id"]),
        "wrong order": lambda e: e.update(hidden=["hint", "text"], text="", hint=""),
        "not blanked": lambda e: e.update(hidden=["text"], text="still here"),
    }
    for number, (name, change) in enumerate(cases.items()):
        base = folder_with(tmp_path / str(number), record_text=False)

        def apply(row, change=change):
            element = row.get("action", {}).get("element")
            if element is not None and element["id"] == "e2":
                change(element)

        edit_lines(base, apply)
        assert GATE_WARNING in read_tree(base).warnings, name


def test_the_gate_refuses_hidden_details_that_do_not_fit(tmp_path):
    base = folder_with(tmp_path / "a", record_text=False)

    def wrong(row):
        if "details" in row:
            row["details"]["hidden"] = ["owner_uid"]

    edit_lines(base, wrong)
    assert GATE_WARNING in read_tree(base).warnings
    base = folder_with(tmp_path / "b", record_text=False)

    def not_blank(row):
        if "details" in row:
            row["details"]["window"] = "Chat"

    edit_lines(base, not_blank)
    assert GATE_WARNING in read_tree(base).warnings
