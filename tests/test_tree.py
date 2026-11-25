"""The tree command: a run folder shown as a tree, from files only."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from uiprobe import cli
from uiprobe.crawl import Limits, fingerprint_of
from uiprobe.crawl_tree import CUT_WARNING, GATE_WARNING, TREE_SCHEMA, read_tree
from uiprobe.model import Bounds, Element, Screen
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from pathlib import Path

PACKAGE = "com.example.app"
OTHER_SCREEN = "s-aaaaaaaaaaaa"
LEGEND = "· = not pressed, so nothing below it was looked at"
_WRITERS: list[RunWriter] = []


@pytest.fixture(autouse=True)
def _close_writers():
    yield
    while _WRITERS:
        with contextlib.suppress(RunFolderError):
            _WRITERS.pop().close()


def el(number, parent=None, cls="TextView", text="", desc="", flags=(), **more):
    return Element(
        id=f"e{number}",
        parent=None if parent is None else f"e{parent}",
        class_name=f"android.widget.{cls}",
        bounds=Bounds(0, number * 10, 100, number * 10 + 10),
        text=text,
        content_desc=desc,
        flags=tuple(flags),
        **more,
    )


def make_screen(activity, elements, package=PACKAGE):
    return Screen(
        serial="emulator-5554",
        captured_at=datetime(2026, 3, 1, 9, 31, tzinfo=UTC),
        package=package,
        activity=activity,
        api_level=36,
        display=(1080, 2400),
        elements=tuple(elements),
    )


CLICK = ("clickable",)


def home_screen(extra=()):
    return make_screen(
        ".Home",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, text="Welcome"),
            el(3, 1, cls="LinearLayout", flags=CLICK),
            el(4, 3, text="Network"),
            el(5, 3, text="Mobile, Wi-Fi"),
            el(6, 1, cls="LinearLayout", flags=CLICK),
            el(7, 6, text="About"),
            el(8, 1, cls="ImageButton", desc="Menu", flags=CLICK),
            el(9, 1, cls="ImageButton", flags=CLICK),
            el(10, 1, cls="LinearLayout", flags=CLICK),
            el(11, 10, text="Wi-Fi"),
            el(12, 1, cls="LinearLayout", flags=CLICK),
            el(13, 12, text="Same"),
            el(14, 1, cls="LinearLayout", flags=CLICK),
            el(15, 14, text="Other app"),
            el(16, 1, cls="LinearLayout", flags=CLICK),
            el(17, 16, text="Broken"),
            el(18, 1, cls="LinearLayout", flags=CLICK),
            el(19, 18, text="Blocked"),
            *extra,
        ],
    )


NETWORK = make_screen(
    ".Network",
    [
        el(1, cls="FrameLayout"),
        el(2, 1, cls="LinearLayout", flags=CLICK),
        el(3, 2, text="Back home"),
        el(4, 1, cls="LinearLayout", flags=CLICK),
        el(5, 4, text="Info"),
    ],
)
ABOUT = make_screen(".About", [el(1, cls="FrameLayout"), el(2, 1, text="Version 1")])


class Folder:
    """A run folder written by the real RunWriter."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "run"
        self.writer = RunWriter.create(
            self.path,
            package=PACKAGE,
            limits=Limits(),
            deny_words=["delete"],
            serial="emulator-5554",
        )
        _WRITERS.append(self.writer)
        self.action = 0

    def screen(self, screen):
        self.writer.write_screen(screen)
        return screen.screen_id

    def start(self, kind, from_screen=None, element=None, **more):
        self.action += 1
        point = (5, 5) if kind == "tap" else None  # a tap always has its point
        self.writer.write_transition_start(
            self.action, kind, from_screen, element, point, **more
        )
        return self.action

    def end(self, to=None, **more):
        self.writer.write_transition_end(self.action, to, **more)

    def tap(self, screen, number, to=None, **more):
        element = screen.elements[number - 1]
        self.start("tap", screen.screen_id, element)
        self.end(to, **more)

    def skip(self, screen, number, *reasons):
        element = screen.elements[number - 1]
        self.writer.write_skip(
            screen.screen_id, fingerprint_of(screen, element), reasons, element
        )

    def finish(self):
        self.writer.finish("exhausted", screens=3, actions=self.action)


def full_run(tmp_path: Path, *, finish: bool = True) -> Folder:
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    network_id = folder.screen(NETWORK)
    about_id = folder.screen(ABOUT)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, network_id)
    folder.tap(NETWORK, 2, home_id)
    folder.tap(NETWORK, 4, about_id)
    folder.tap(home, 6, about_id)
    folder.tap(home, 12, home_id)
    folder.tap(home, 14, OTHER_SCREEN)
    folder.start("relaunch", OTHER_SCREEN, off_target_package="com.other.app")
    folder.end(home_id)
    folder.tap(home, 16, dump_failed=True)
    folder.tap(home, 18, not_sent="covered_by_other")
    folder.skip(home, 10, "toggle")
    folder.skip(home, 8, "deny_word", "screen_edge")
    if finish:
        folder.finish()
    return folder


def run_tree(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.run(["tree", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


FULL_TEXT = [
    "com.example.app (com.example.app)",
    "├─ [text] Welcome",
    "├─ Network",
    "│  ├─ Back home → (screen shown above: first screen)",
    "│  └─ Info",
    "│     └─ [text] Version 1",
    "├─ About → (screen shown above: Info)",
    '├─ "Menu" ·',
    "├─ [icon: ImageButton] ·",
    "├─ Wi-Fi ·",
    "├─ Same → (same screen)",
    "├─ Other app → another app",
    "├─ Broken → (could not be read)",
    "└─ Blocked → (not sent: covered_by_other)",
    LEGEND,
]


def test_the_whole_tree(tmp_path, capsys):
    folder = full_run(tmp_path)
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert out.splitlines() == FULL_TEXT
    assert err == ""


def _foreign_script(text):
    return [
        c
        for c in text
        if "\u3040" <= c <= "\u30ff"
        or "\u3400" <= c <= "\u9fff"
        or "\uff00" <= c <= "\uffef"
    ]


def test_the_command_own_wording_is_not_japanese(tmp_path, capsys):
    folder = full_run(tmp_path)
    for extra in ((), ("--ids",), ("--why",), ("--json",)):
        _, out, _ = run_tree(capsys, str(folder.path), *extra)
        assert out
        assert _foreign_script(out) == []
    _edit_start(folder, record_text=False)
    for extra in ((), ("--json",)):
        _, out, _ = run_tree(capsys, str(folder.path), *extra)
        assert _foreign_script(out) == []


def test_a_row_is_named_by_its_first_text_only(tmp_path, capsys):
    folder = full_run(tmp_path)
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "Mobile, Wi-Fi" not in out


def test_ids_add_the_screen_and_the_other_app(tmp_path, capsys):
    folder = full_run(tmp_path)
    code, out, _ = run_tree(capsys, str(folder.path), "--ids")
    lines = out.splitlines()
    assert code == 0
    assert lines[1] == f"├─ (screen {home_screen().screen_id}  .Home)"
    assert f"│  ├─ (screen {NETWORK.screen_id}  .Network)" in lines
    assert any(
        line.endswith(f"(screen shown above: Info) {ABOUT.screen_id}") for line in lines
    )
    assert "├─ Other app → another app (com.other.app)" in lines


def test_why_adds_the_reasons_of_unpressed_rows(tmp_path, capsys):
    folder = full_run(tmp_path)
    _, out, _ = run_tree(capsys, str(folder.path), "--why")
    lines = out.splitlines()
    assert '├─ "Menu" · [deny_word, screen_edge]' in lines
    assert "├─ Wi-Fi · [toggle]" in lines
    assert "├─ [icon: ImageButton] ·" in lines
    assert "├─ Same → (same screen)" in lines


def test_a_fingerprint_that_matches_nothing_is_reported(tmp_path, capsys):
    folder = full_run(tmp_path, finish=False)
    folder.writer.write_skip(
        home_screen().screen_id,
        "f-" + "0" * 32,
        ("deny_word",),
        home_screen().elements[2],
    )
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert lines[-2] == "└─ (1 with no matching row)"
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    home = json.loads(out)["screens"][home_screen().screen_id]
    assert home["unmatched"] == [
        {"fingerprint": "f-" + "0" * 32, "reasons": ["deny_word"]}
    ]


def test_json_shape(tmp_path, capsys):
    folder = full_run(tmp_path)
    code, out, _ = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    home_id = home_screen().screen_id
    assert code == 0
    assert data["schema"] == TREE_SCHEMA == "uiprobe.crawl_tree/1"
    assert data["root"] == home_id
    assert data["run"]["package"] == PACKAGE
    assert data["run"]["stop_reason"] == "exhausted"
    assert data["run"]["finished"] is True
    assert data["run"]["record_text"] is True
    assert data["unreached"] == []
    assert data["warnings"] == []
    assert set(data["screens"]) == {home_id, NETWORK.screen_id, ABOUT.screen_id}
    home = data["screens"][home_id]
    assert home["activity"] == ".Home"
    assert home["unmatched"] == []
    by_name = {e["name"]: e for e in home["elements"]}
    network = by_name["Network"]
    assert network == {
        "element_id": "e3",
        "kind": "row",
        "class": "android.widget.LinearLayout",
        "name": "Network",
        "texts": ["Network", "Mobile, Wi-Fi"],
        "pressed": True,
        "action_id": 2,
        "to": {"screen_id": NETWORK.screen_id},
        "reasons": [],
    }
    assert by_name["Welcome"]["kind"] == "text"
    assert "pressed" not in by_name["Welcome"]
    assert by_name["Wi-Fi"]["pressed"] is False
    assert by_name["Wi-Fi"]["action_id"] is None
    assert by_name["Wi-Fi"]["reasons"] == ["toggle"]
    assert "to" not in by_name["Wi-Fi"]
    assert by_name['"Menu"']["texts"] == []
    assert by_name["[icon: ImageButton]"]["reasons"] == []
    assert by_name["Same"]["to"] == {"same_screen": True}
    assert by_name["Other app"]["to"] == {"other_app": "com.other.app"}
    assert by_name["Broken"]["to"] == {"not_read": True}
    blocked = by_name["Blocked"]
    assert blocked["to"] == {"not_sent": "covered_by_other"}
    assert blocked["pressed"] is False
    assert blocked["action_id"] == 10


def test_a_screen_of_the_app_is_a_node_even_if_no_pressed_row_led_to_it(
    tmp_path, capsys
):
    folder = Folder(tmp_path)
    home_id = folder.screen(home_screen())
    folder.screen(ABOUT)
    folder.start("launch")
    folder.end(home_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert f"(screen not reached from a pressed row {ABOUT.screen_id}  .About)" in lines
    assert "└─ [text] Version 1" in lines
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    assert json.loads(out)["unreached"] == [ABOUT.screen_id]


def test_a_screen_file_of_another_package_fails_the_gate(tmp_path, capsys):
    """uiprobe writes only the target app's screens; a tap pair whose
    destination file names another package, with no off_target line, must not
    show "another app"."""
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    foreign = make_screen(".Pick", [el(1, cls="FrameLayout")], package="com.other")
    foreign_id = folder.screen(foreign)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, foreign_id)
    folder.finish()
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert err == f"uiprobe: warning: {GATE_WARNING}\n"
    assert "├─ Network → (result unknown)" in out.splitlines()
    assert "another app" not in out
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    assert data["warnings"] == [GATE_WARNING]
    assert data["screens"][foreign_id]["package"] == "com.other"


def test_a_foreign_screen_file_fails_the_gate_even_when_no_tap_names_it(
    tmp_path, capsys
):
    folder = full_run(tmp_path)
    foreign = make_screen(".Pick", [el(1, cls="FrameLayout")], package="com.other")
    (folder.path / "screens" / f"{foreign.screen_id}.json").write_text(
        foreign.to_json(), encoding="utf-8"
    )
    _, out, err = run_tree(capsys, str(folder.path))
    assert err == f"uiprobe: warning: {GATE_WARNING}\n"
    assert "├─ Network → (result unknown)" in out.splitlines()
    assert "another app" not in out


def test_an_off_target_with_an_empty_package_is_no_evidence(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, OTHER_SCREEN)
    folder.start("relaunch", OTHER_SCREEN, off_target_package="")
    folder.end(home_id)
    folder.writer.close()
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "another app" not in out
    assert "├─ Network → (could not be read)" in out.splitlines()
    assert err == ""


def test_another_app_needs_the_off_target_line_right_after_the_end(tmp_path, capsys):
    path, _ = _raw_run(
        tmp_path,
        [
            ("S", 1, _tap_start),
            ("E", 1, _tap_end_other),
            ("S", 2, _relaunch_off),
        ],
    )
    _, out, _ = run_tree(capsys, str(path))
    assert "├─ Network → another app" in out.splitlines()
    # the same lines with the off_target of another screen: no evidence
    path2 = tmp_path / "two"
    path2.mkdir()

    def elsewhere(_sid, _tap):
        return {
            "action": {"kind": "relaunch"},
            "from_screen_id": "s-bbbbbbbbbbbb",
            "off_target": {"package": "com.other.app"},
        }

    path, _ = _raw_run(
        path2,
        [("S", 1, _tap_start), ("E", 1, _tap_end_other), ("S", 2, elsewhere)],
    )
    _, out, _ = run_tree(capsys, str(path))
    assert "├─ Network → (could not be read)" in out.splitlines()


def test_a_key_that_appears_twice_fails_the_gate(tmp_path, capsys):
    folder = full_run(tmp_path)
    data = _log(folder).read_bytes()
    data = data.replace(b'"action_id": 2,', b'"action_id": 2, "action_id": 2,', 1)
    _log(folder).write_bytes(data)
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert err == f"uiprobe: warning: {GATE_WARNING}\n"
    # the damaged line itself names no usable tap; the others are unknown
    assert "├─ About → (result unknown)" in out.splitlines()
    assert not any("→ (same screen)" in line for line in out.splitlines())


@pytest.mark.parametrize("kind", ["[1]", "{}", "5", "null", "true"])
def test_a_kind_that_is_not_a_string_fails_the_gate(tmp_path, capsys, kind):
    folder = full_run(tmp_path)
    data = (
        _log(folder)
        .read_bytes()
        .replace(b'"kind": "tap"', b'"kind": ' + kind.encode(), 1)
    )
    _log(folder).write_bytes(data)
    code, _, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert err == f"uiprobe: warning: {GATE_WARNING}\n"


@pytest.mark.parametrize("last", [b"{}", b"[1]", b'"text"', b"123", b'{"a": 1}'])
def test_a_last_line_that_is_json_but_the_wrong_shape_fails_the_gate(
    tmp_path, capsys, last
):
    folder = full_run(tmp_path)
    _log(folder).write_bytes(_log(folder).read_bytes() + last)  # no newline
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert err == f"uiprobe: warning: {GATE_WARNING}\n"
    assert CUT_WARNING not in err
    assert "├─ Network → (result unknown)" in out.splitlines()


def test_a_missing_screen_without_off_target_is_not_read(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, OTHER_SCREEN)
    folder.finish()
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "├─ Network → (could not be read)" in out.splitlines()
    assert err == ""


def test_a_run_without_run_json_still_shows(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.start("tap", home_id, home.elements[2])  # no end line: result unknown
    code, out, _ = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "├─ Network → (result unknown)" in out.splitlines()
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    run = json.loads(out)["run"]
    assert run["finished"] is False
    assert run["stop_reason"] is None
    assert run["actions"] == 1 + 1


def test_a_missing_skipped_file_is_not_an_error(tmp_path, capsys):
    folder = full_run(tmp_path)
    (folder.path / "skipped.jsonl").unlink()
    code, out, err = run_tree(capsys, str(folder.path), "--why")
    assert code == 0
    assert "├─ Wi-Fi ·" in out.splitlines()
    assert err == ""


def _edit_start(folder: Folder, **changes) -> None:
    path = folder.path / "run-start.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_record_text_false_shows_the_structure_only(tmp_path, capsys):
    folder = full_run(tmp_path)
    _edit_start(folder, record_text=False)
    code, out, _ = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert code == 0
    assert lines[0] == (
        "com.example.app (com.example.app)  "
        "screen texts were not recorded in this run (record_text = false)"
    )
    assert "├─ [row: LinearLayout]" in out
    assert "├─ [icon: ImageButton] ·" in lines
    for word in ("Network", "Welcome", "About", "Menu", "[text]"):
        assert word not in "\n".join(lines[1:])
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    assert data["run"]["record_text"] is False
    elements = data["screens"][home_screen().screen_id]["elements"]
    assert all(e["texts"] == [] and e["hidden"] is True for e in elements)
    assert all(e["kind"] == "row" for e in elements)


def test_record_text_true_or_absent_records_texts(tmp_path, capsys):
    folder = full_run(tmp_path)
    _edit_start(folder, record_text=True)
    _, out, _ = run_tree(capsys, str(folder.path))
    assert out.splitlines() == FULL_TEXT


def test_a_password_field_is_hidden(tmp_path, capsys):
    folder = Folder(tmp_path)
    screen = make_screen(
        ".Login",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="LinearLayout", flags=CLICK),
            el(3, 2, cls="EditText", text="hunter2", flags=("password",)),
            el(4, 1, cls="EditText", text="secret", flags=("password",)),
            el(5, 1, text="Sign in"),
        ],
    )
    sid = folder.screen(screen)
    folder.start("launch")
    folder.end(sid)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert out.splitlines()[1:4] == [
        "├─ [password field] ·",
        "├─ [text] [password field]",
        "└─ [text] Sign in",
    ]
    assert "hunter2" not in out
    assert "secret" not in out
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    assert "hunter2" not in out
    assert "secret" not in out
    elements = json.loads(out)["screens"][sid]["elements"]
    row = elements[0]
    assert (row["name"], row["texts"], row["hidden"]) == ("[password field]", [], True)


def test_device_strings_are_escaped(tmp_path, capsys):
    folder = Folder(tmp_path)
    screen = make_screen(
        ".Evil\x1b[31m",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="LinearLayout", flags=CLICK),
            el(3, 2, text="x\x1b[2Jy‮b"),
            el(4, 1, text="head\x07\nline"),
            el(5, 1, cls="LinearLayout", flags=CLICK),
            el(6, 5, text="Go"),
        ],
    )
    sid = folder.screen(screen)
    folder.start("launch")
    folder.end(sid)
    folder.tap(screen, 5, OTHER_SCREEN)
    folder.start("relaunch", OTHER_SCREEN, off_target_package="com.bad\x1b]0;pwn")
    folder.end(sid)
    folder.finish()
    code, out, err = run_tree(capsys, str(folder.path), "--ids", "--why")
    assert code == 0
    for text in (out, err):
        assert "\x1b" not in text
        assert "\x07" not in text
        assert "‮" not in text
    assert "x\\x1b[2Jy\\u202eb ·" in out
    assert "[text] head\\x07\\nline" in out
    assert "(screen " + sid + "  .Evil\\x1b[31m)" in out
    assert "another app (com.bad\\x1b]0;pwn)" in out
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    assert "\x1b" not in out
    assert "‮" not in out
    assert "\\u001b[2J" in out


def test_json_before_the_command_works_too(tmp_path, capsys):
    folder = full_run(tmp_path)
    code = cli.run(["--json", "tree", str(folder.path)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["schema"] == TREE_SCHEMA


def test_no_run_start_is_an_error(tmp_path, capsys):
    folder = full_run(tmp_path)
    (folder.path / "run-start.json").unlink()
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 2
    assert out == ""
    assert "run-start.json is missing" in err
    code, out, _ = run_tree(capsys, str(folder.path), "--json")
    payload = json.loads(out)
    assert code == 2
    assert payload["schema"] == "uiprobe.error/1"
    assert payload["error"]["code"] == "usage_error"


def test_a_folder_that_does_not_exist_is_a_usage_error(tmp_path, capsys):
    code, _, err = run_tree(capsys, str(tmp_path / "nothing\x1b"))
    assert code == 2
    assert "\x1b" not in err
    assert "not a folder" in err


def test_a_run_start_without_a_package_is_an_error(tmp_path, capsys):
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "run-start.json").write_text("[]", encoding="utf-8")
    code, _, err = run_tree(capsys, str(tmp_path / "run"))
    assert code == 2
    assert "uiprobe.run_start/1" in err


def test_the_command_writes_nothing(tmp_path, capsys):
    folder = full_run(tmp_path)

    def snapshot():
        return {
            p.relative_to(tmp_path): (p.stat().st_size, p.read_bytes())
            for p in tmp_path.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    run_tree(capsys, str(folder.path))
    run_tree(capsys, str(folder.path), "--json", "--ids", "--why")
    assert snapshot() == before


def test_device_options_are_refused_without_contact(tmp_path, capsys, fake_adb):
    folder = full_run(tmp_path)
    for options in (
        ["--adb-server", fake_adb.address],
        ["-s", "emulator-5554"],
        ["--adb", "adb"],
        ["--no-start-server"],
    ):
        code = cli.run([*options, "tree", str(folder.path)])
        assert code == 2
        assert "tree takes no device options" in capsys.readouterr().err
    assert fake_adb.requests == []


def test_the_command_does_not_contact_an_adb_server(
    tmp_path, capsys, fake_adb, monkeypatch
):
    folder = full_run(tmp_path)
    monkeypatch.setenv("ANDROID_ADB_SERVER_PORT", fake_adb.address.rsplit(":", 1)[-1])
    for extra in ((), ("--json",), ("--ids", "--why")):
        assert cli.run(["tree", str(folder.path), *extra]) == 0
    capsys.readouterr()
    assert fake_adb.requests == []


def test_read_tree_is_the_library_form(tmp_path):
    folder = full_run(tmp_path)
    tree = read_tree(folder.path)
    assert tree.package == PACKAGE
    assert tree.root == home_screen().screen_id
    assert tree.root is not None
    assert [row.name for row in tree.screens[tree.root].rows][:3] == [
        "Welcome",
        "Network",
        "About",
    ]


# --- the rules of the folder: nothing outside it, nothing unbounded ---


def _link(path: Path, target: Path | str, *, target_is_directory: bool = False) -> None:
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError):
        pytest.skip("links cannot be made here")


def _outside_screen(tmp_path: Path) -> Path:
    """A folder outside the run with a screen file whose texts must not show."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = make_screen(
        ".Secret",
        [el(1, cls="FrameLayout"), el(2, 1, text="TOPSECRET")],
    )
    (outside / f"{secret.screen_id}.json").write_text(
        secret.to_json(), encoding="utf-8"
    )
    (outside / "transitions.jsonl").write_text("TOPSECRET\n", encoding="utf-8")
    return outside


def test_a_linked_screens_folder_is_not_followed(tmp_path, capsys):
    folder = full_run(tmp_path)
    outside = _outside_screen(tmp_path)
    for entry in (folder.path / "screens").iterdir():
        (outside / entry.name).write_bytes(entry.read_bytes())
    import shutil

    shutil.rmtree(folder.path / "screens")
    _link((folder.path / "screens"), outside, target_is_directory=True)
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "TOPSECRET" not in out + err
    assert "Welcome" not in out
    assert "screens is a link or not a folder" in err


def test_a_linked_screen_file_is_skipped(tmp_path, capsys):
    folder = full_run(tmp_path)
    outside = _outside_screen(tmp_path)
    home_id = home_screen().screen_id
    target = folder.path / "screens" / f"{home_id}.json"
    target.unlink()
    _link(target, outside / f"{ABOUT.screen_id}.json")
    code, out, err = run_tree(capsys, str(folder.path), "--json")
    assert code == 0
    assert "TOPSECRET" not in out + err
    data = json.loads(out)
    assert home_id not in data["screens"]
    assert any(f"screens/{home_id}.json is a link" in w for w in data["warnings"])


def test_a_link_inside_the_folder_is_refused_too(tmp_path, capsys):
    folder = full_run(tmp_path)
    home_id = home_screen().screen_id
    screens = folder.path / "screens"
    (screens / "copy.json").write_bytes((screens / f"{home_id}.json").read_bytes())
    (screens / f"{home_id}.json").unlink()
    _link((screens / f"{home_id}.json"), "copy.json")
    _, out, err = run_tree(capsys, str(folder.path))
    assert f"screens/{home_id}.json is a link" in err
    assert "Welcome" not in out


def test_a_linked_log_file_is_skipped(tmp_path, capsys):
    folder = full_run(tmp_path)
    outside = _outside_screen(tmp_path)
    log = folder.path / "transitions.jsonl"
    log.unlink()
    _link(log, outside / "transitions.jsonl")
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "TOPSECRET" not in out + err
    assert "transitions.jsonl is a link" in err


def test_a_linked_run_start_is_a_usage_error(tmp_path, capsys):
    folder = full_run(tmp_path)
    outside = _outside_screen(tmp_path)
    (outside / "run-start.json").write_text('{"package": "TOPSECRET"}')
    (folder.path / "run-start.json").unlink()
    _link((folder.path / "run-start.json"), outside / "run-start.json")
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 2
    assert "TOPSECRET" not in out + err
    assert "is a link" in err


def test_a_linked_run_json_is_skipped(tmp_path, capsys):
    folder = full_run(tmp_path)
    outside = _outside_screen(tmp_path)
    (outside / "run.json").write_text('{"stop_reason": "TOPSECRET"}')
    (folder.path / "run.json").unlink()
    _link((folder.path / "run.json"), outside / "run.json")
    code, out, err = run_tree(capsys, str(folder.path), "--json")
    assert code == 0
    assert "TOPSECRET" not in out + err
    assert json.loads(out)["run"]["finished"] is False


def test_a_fifo_is_not_opened(tmp_path, capsys):
    if sys.platform == "win32":
        pytest.skip("no FIFOs on Windows")
    folder = full_run(tmp_path)
    log = folder.path / "skipped.jsonl"
    log.unlink()
    os.mkfifo(log)
    code, _, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "skipped.jsonl is not a regular file" in err
    fifo_start = tmp_path / "other"
    fifo_start.mkdir()
    os.mkfifo(fifo_start / "run-start.json")
    code, _, err = run_tree(capsys, str(fifo_start))
    assert code == 2
    assert "is not a regular file" in err


def test_a_parent_cycle_does_not_loop(tmp_path, capsys):
    folder = Folder(tmp_path)
    screen = make_screen(
        ".Loop",
        [
            Element(
                id="e1",
                parent="e1",
                class_name="android.widget.LinearLayout",
                bounds=Bounds(0, 0, 10, 10),
                text="Self",
                flags=CLICK,
            ),
            Element(
                id="e2",
                parent="e3",
                class_name="android.widget.LinearLayout",
                bounds=Bounds(0, 0, 10, 10),
                text="Two",
                flags=CLICK,
            ),
            Element(
                id="e3",
                parent="e2",
                class_name="android.widget.TextView",
                bounds=Bounds(0, 0, 10, 10),
                text="Three",
            ),
        ],
    )
    sid = folder.screen(screen)
    folder.start("launch")
    folder.end(sid)
    folder.finish()
    code, out, _ = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "├─ Self ·" in out
    assert "└─ Two ·" in out


def test_a_file_over_the_limit_is_skipped(tmp_path, capsys, monkeypatch):
    from uiprobe import crawl_tree

    folder = full_run(tmp_path)
    monkeypatch.setattr(crawl_tree, "MAX_LOG_BYTES", 10)
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "transitions.jsonl is over 0 MiB, so it is skipped" in err
    assert "skipped.jsonl is over" in err
    assert "Welcome" in out or "Version" in out
    monkeypatch.setattr(crawl_tree, "MAX_FILE_BYTES", 10)
    code, _, err = run_tree(capsys, str(folder.path))
    assert code == 2
    assert "run-start.json is over" in err


def test_deep_nesting_and_bad_bytes_are_warnings(tmp_path, capsys):
    folder = full_run(tmp_path)
    home_id = home_screen().screen_id
    (folder.path / "screens" / f"{home_id}.json").write_bytes(b"[" * 200000)
    (folder.path / "run.json").write_bytes(b"\xff\xfe")
    log = folder.path / "skipped.jsonl"
    log.write_bytes(b'{"a": ' * 100000 + b"\n\xff\n")
    code, _, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert f"screens/{home_id}.json cannot be read" in err
    assert "run.json is not valid UTF-8 JSON" in err
    assert "skipped.jsonl line 1 is not valid UTF-8 JSON" in err
    assert "skipped.jsonl line 2 is not valid UTF-8 JSON" in err
    (folder.path / "run-start.json").write_bytes(b"[" * 200000)
    code, _, err = run_tree(capsys, str(folder.path))
    assert code == 2
    assert "run-start.json is not valid UTF-8 JSON" in err


def test_a_pressed_row_is_named_by_its_own_recorded_element(tmp_path, capsys):
    folder = Folder(tmp_path)
    old = make_screen(
        ".Home",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="Button", text="Old title", flags=CLICK),
            el(3, 1, cls="Button", text="Other", flags=CLICK),
        ],
    )
    sid = folder.screen(old)
    folder.start("launch")
    folder.end(sid)
    # The tap was decided when the same layout showed another text.
    folder.start(
        "tap",
        sid,
        {
            "id": "e2",
            "class": "android.widget.Button",
            "text": "New title",
            "bounds": [0, 0, 10, 10],
            "flags": ["clickable"],
        },
    )
    folder.end(sid)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "├─ New title → (same screen)" in out.splitlines()
    assert "Old title" not in out
    assert "└─ Other ·" in out.splitlines()  # unpressed: from the snapshot
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    names = [e["name"] for e in json.loads(out)["screens"][sid]["elements"]]
    assert names == ["New title", "Other"]


def test_another_app_is_not_inferred_from_an_unrelated_later_line(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, OTHER_SCREEN)  # no file for it, and no line decided on it
    folder.start("relaunch", "s-bbbbbbbbbbbb", off_target_package="com.other.app")
    folder.end(home_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "├─ Network → (could not be read)" in out.splitlines()
    assert "another app" not in out
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    elements = json.loads(out)["screens"][home.screen_id]["elements"]
    network = next(e for e in elements if e["name"] == "Network")
    assert network["to"] == {"not_read": True}


def test_a_start_line_without_an_end_line_is_unknown(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.start("tap", home_id, home.elements[2])
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "├─ Network → (result unknown)" in out.splitlines()
    assert "Network ·" not in out
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    elements = json.loads(out)["screens"][home_id]["elements"]
    network = next(e for e in elements if e["name"] == "Network")
    assert network["to"] == {"unknown": True}
    assert network["pressed"] is False
    assert network["action_id"] == 2


def test_unreached_lists_only_screens_no_tap_led_to(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.screen(NETWORK)
    folder.screen(ABOUT)
    folder.start("launch")
    folder.end(home_id)
    # Network is only reachable from an unreached screen: About leads to it.
    about_row = make_screen(
        ".About",
        [el(1, cls="FrameLayout"), el(2, 1, cls="LinearLayout", flags=CLICK)],
    )
    folder.screen(about_row)
    folder.tap(about_row, 2, NETWORK.screen_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    assert about_row.screen_id in data["unreached"]
    assert NETWORK.screen_id not in data["unreached"]
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "(screen not reached from a pressed row" in out
    assert "Back home" in out  # the Network screen is still shown, below


# --- the second round of rules ---


def _append(path: Path, *lines: dict) -> None:
    path.write_bytes(
        path.read_bytes() + b"".join(json.dumps(x).encode() + b"\n" for x in lines)
    )


def test_run_dir_itself_and_its_parents_may_be_links(tmp_path, capsys):
    folder = full_run(tmp_path)
    shortcut = tmp_path / "shortcut"
    _link(shortcut, folder.path, target_is_directory=True)
    code, out, err = run_tree(capsys, str(shortcut))
    assert code == 0
    assert out.splitlines() == FULL_TEXT
    assert err == ""
    above = tmp_path / "above"
    _link(above, tmp_path, target_is_directory=True)
    code, out, _ = run_tree(capsys, str(above / "run"))
    assert code == 0
    assert out.splitlines() == FULL_TEXT


def test_a_junction_for_screens_is_refused(tmp_path, capsys, monkeypatch):
    folder = full_run(tmp_path)
    real = os.path.isjunction
    monkeypatch.setattr(
        os.path, "isjunction", lambda p: str(p).endswith("screens") or real(p)
    )
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert "screens is a link or not a folder" in err
    assert "Welcome" not in out


def test_a_hard_linked_file_is_refused(tmp_path, capsys):
    folder = full_run(tmp_path)
    home_id = home_screen().screen_id
    os.link(folder.path / "screens" / f"{home_id}.json", tmp_path / "second-name")
    _, out, err = run_tree(capsys, str(folder.path))
    assert f"screens/{home_id}.json has more than one name" in err
    assert "Welcome" not in out
    os.link(folder.path / "transitions.jsonl", tmp_path / "log-name")
    _, _, err = run_tree(capsys, str(folder.path))
    assert "transitions.jsonl has more than one name" in err


def test_a_screen_is_opened_only_by_the_exact_listed_name(tmp_path, capsys):
    folder = full_run(tmp_path)
    home_id = home_screen().screen_id
    screens = folder.path / "screens"
    (screens / f"{home_id}.json").rename(screens / f"S-{home_id[2:]}.json")
    _, out, err = run_tree(capsys, str(folder.path))
    assert "Welcome" not in out
    assert f"a tap names the screen {home_id}, which has no readable file" in err


def test_the_read_stops_at_the_cap_even_if_the_listing_size_lies(
    tmp_path, capsys, monkeypatch
):
    from uiprobe import crawl_tree

    folder = full_run(tmp_path)
    path = folder.path / "transitions.jsonl"
    size = path.stat().st_size
    monkeypatch.setattr(crawl_tree, "MAX_LOG_BYTES", size - 1)
    real = os.lstat
    lying = real(path)

    class Lie:
        def __getattr__(self, name):
            return 0 if name == "st_size" else getattr(lying, name)

    monkeypatch.setattr(
        crawl_tree, "_checked", lambda p, _limit: Lie() if p == path else real(p)
    )
    _, _, err = run_tree(capsys, str(folder.path))
    assert "transitions.jsonl is over" in err


def test_a_file_of_exactly_the_cap_is_read(tmp_path):
    from uiprobe import crawl_tree

    path = tmp_path / "f"
    path.write_bytes(b"x" * 20)
    assert crawl_tree._read_bytes(path, 20) == b"x" * 20
    with pytest.raises(crawl_tree._UnreadableError):
        crawl_tree._read_bytes(path, 19)


def test_texts_are_capped_and_nesting_is_bounded(tmp_path, capsys):
    folder = Folder(tmp_path)
    elements = [el(1, cls="FrameLayout"), el(2, 1, cls="LinearLayout", flags=CLICK)]
    elements += [el(number, 2, text=f"t{number}") for number in range(3, 203)]
    chain = [el(300, 1, cls="LinearLayout", flags=CLICK, text="c0")]
    chain += [
        el(300 + n, 299 + n, cls="LinearLayout", flags=CLICK, text=f"c{n}")
        for n in range(1, 400)
    ]
    sid = folder.screen(make_screen(".Big", [*elements, *chain]))
    folder.start("launch")
    folder.end(sid)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    rows = json.loads(out)["screens"][sid]["elements"]
    assert len(rows) == 2 + 399
    assert all(len(r["texts"]) <= 50 for r in rows)
    assert len(rows[0]["texts"]) == 50
    assert rows[0]["name"] == "t3"


def test_skip_reasons_must_be_a_list_of_strings(tmp_path, capsys):
    folder = full_run(tmp_path)
    _append(
        folder.path / "skipped.jsonl",
        {
            "schema": "uiprobe.skip/1",
            "screen_id": home_screen().screen_id,
            "fingerprint": "f-x",
            "reasons": "toggle",
        },
        {
            "schema": "uiprobe.skip/1",
            "screen_id": home_screen().screen_id,
            "fingerprint": "f-y",
            "reasons": [1],
        },
    )
    code, out, err = run_tree(capsys, str(folder.path), "--json")
    assert code == 0
    assert err == ""
    warnings = json.loads(out)["warnings"]
    assert (
        warnings.count(
            "skipped.jsonl has a line whose reasons are not a list of strings"
        )
        == 2
    )
    assert json.loads(out)["screens"][home_screen().screen_id]["unmatched"] == []


def test_run_start_needs_its_schema(tmp_path, capsys):
    folder = full_run(tmp_path)
    _edit_start(folder, schema="something/1")
    code, _, err = run_tree(capsys, str(folder.path))
    assert code == 2
    assert "uiprobe.run_start/1" in err
    (folder.path / "run-start.json").write_text('{"package": "a.b"}')
    code, _, err = run_tree(capsys, str(folder.path))
    assert code == 2


def test_a_later_visit_to_the_screen_is_not_evidence_of_another_app(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, OTHER_SCREEN)  # the next line is not the relaunch
    folder.tap(home, 6, home_id)
    folder.start("relaunch", OTHER_SCREEN, off_target_package="com.other.app")
    folder.end(home_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "├─ Network → (could not be read)" in out.splitlines()
    assert "another app" not in out


def test_every_sent_tap_on_a_row_keeps_its_edge(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    network_id = folder.screen(NETWORK)
    about_id = folder.screen(ABOUT)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, network_id)
    folder.tap(home, 3, about_id)
    folder.tap(home, 3, about_id)  # the same ending again is not repeated
    folder.tap(home, 3, home_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert lines[2] == "├─ Network"
    assert lines.index("│  ├─ Info ·") < lines.index(
        "│  ├─ (pressed again → next screen)"
    )
    assert "│  │  └─ [text] Version 1" in lines
    assert "│  └─ (pressed again → (same screen))" in lines
    assert lines.count("│  ├─ (pressed again → next screen)") == 1
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    network = next(
        e for e in data["screens"][home_id]["elements"] if e["name"] == "Network"
    )
    assert network["to"] == {"screen_id": network_id}
    assert network["also_to"] == [{"screen_id": about_id}, {"same_screen": True}]
    assert network["action_id"] == 2
    assert data["unreached"] == []


def test_a_screen_a_tap_led_to_is_never_unreached(tmp_path, capsys):
    folder = Folder(tmp_path)
    home_id = folder.screen(home_screen())
    about_id = folder.screen(ABOUT)
    folder.start("launch")
    folder.end(home_id)
    missing = make_screen(".Gone", [el(1, cls="FrameLayout"), el(2, 1, flags=CLICK)])
    folder.tap(missing, 2, about_id)  # its own screen file is not in the folder
    folder.finish()
    _, out, err = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    assert about_id in data["screens"]
    assert data["unreached"] == []
    assert "a tap names the screen" in err or any(
        "a tap names the screen" in w for w in data["warnings"]
    )


def test_a_refused_tap_does_not_rename_the_row(tmp_path, capsys):
    folder = Folder(tmp_path)
    old = make_screen(
        ".Home",
        [el(1, cls="FrameLayout"), el(2, 1, cls="Button", text="Old", flags=CLICK)],
    )
    sid = folder.screen(old)
    folder.start("launch")
    folder.end(sid)
    folder.start(
        "tap",
        sid,
        {
            "id": "e2",
            "class": "android.widget.Button",
            "text": "New",
            "bounds": [0, 0, 1, 1],
        },
    )
    folder.end(not_sent="covered_by_other")
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "└─ Old → (not sent: covered_by_other)" in out.splitlines()
    assert "New" not in out


def test_a_missing_launch_snapshot_falls_back_to_the_first_tap(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end("s-cccccccccccc")  # the launch's screen has no file
    folder.tap(home, 3, home_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    assert data["root"] == home_id


def test_a_failed_end_counts_as_unknown(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.start("tap", home_id, home.elements[2])
    folder.end(failed="start_failed")
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "├─ Network → (result unknown)" in out.splitlines()


# --- the last round ---


def test_a_long_text_is_cut_in_the_text_tree_only(tmp_path, capsys):
    folder = Folder(tmp_path)
    long_text = "L" * 260
    screen = make_screen(
        ".Home",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, text=long_text),
            el(3, 1, cls="LinearLayout", flags=CLICK),
            el(4, 3, text="M" * 201),
            el(5, 1, cls="LinearLayout", flags=CLICK),
            el(6, 5, text="E" * 200),
        ],
    )
    sid = folder.screen(screen)
    folder.start("launch")
    folder.end(sid)
    folder.tap(screen, 3, sid)  # back to the root: "seen above" uses the name
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert f"├─ [text] {'L' * 200}…(+60 chars)" in lines
    assert f"├─ {'M' * 200}…(+1 chars) → (same screen)" in lines
    assert f"└─ {'E' * 200} ·" in lines
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    names = [e["name"] for e in json.loads(out)["screens"][sid]["elements"]]
    assert names == [long_text, "M" * 201, "E" * 200]


def test_a_cut_name_is_used_in_a_seen_above_reference(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = make_screen(
        ".Home",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="LinearLayout", flags=CLICK),
            el(3, 2, text="N" * 250),
            el(4, 1, cls="LinearLayout", flags=CLICK),
            el(5, 4, text="Again"),
        ],
    )
    other = make_screen(".Other", [el(1, cls="FrameLayout"), el(2, 1, text="Hi")])
    home_id = folder.screen(home)
    other_id = folder.screen(other)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 2, other_id)
    folder.tap(home, 4, other_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    cut = "N" * 200 + "…(+50 chars)"
    assert f"├─ {cut}" in out.splitlines()
    assert f"└─ Again → (screen shown above: {cut})" in out.splitlines()


def test_warnings_are_counted_and_only_twenty_are_kept(tmp_path, capsys):
    folder = full_run(tmp_path)
    log = folder.path / "skipped.jsonl"
    log.write_bytes(log.read_bytes() + b"[]\n" * 30)
    code, out, err = run_tree(capsys, str(folder.path), "--json")
    warnings = json.loads(out)["warnings"]
    assert code == 0
    assert len(warnings) == 21
    assert warnings[-1] == "and 10 more warnings"
    code, _, err = run_tree(capsys, str(folder.path))
    assert err.count("uiprobe: warning:") == 21


def test_a_screen_with_too_many_elements_is_not_matched(tmp_path, capsys):
    folder = Folder(tmp_path)
    elements = [el(1, cls="FrameLayout")]
    elements += [
        el(n, 1, cls="LinearLayout", flags=CLICK, text=f"r{n}") for n in range(2, 2003)
    ]
    screen = make_screen(".Huge", elements)
    sid = folder.screen(screen)
    folder.start("launch")
    folder.end(sid)
    folder.skip(screen, 5, "toggle")
    folder.skip(screen, 6, "deny_word")
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "└─ (2 not matched to rows: too many elements)" in out.splitlines()
    assert "[toggle]" not in out
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    node = json.loads(out)["screens"][sid]
    assert len(node["unmatched"]) == 2
    assert all(u["why"] == "too_many_elements" for u in node["unmatched"])
    assert all(e["reasons"] == [] for e in node["elements"])


def _raw_run(tmp_path: Path, lines: list) -> tuple[Path, str]:
    """A run folder whose transitions.jsonl is written by hand."""
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.finish()
    start = {"schema": "uiprobe.transition_start/1"}
    end = {"schema": "uiprobe.transition_end/1"}
    tap = {
        "kind": "tap",
        "point": [5, 5],
        "element": {
            "id": "e3",
            "class": "android.widget.LinearLayout",
            "bounds": [0, 0, 1, 1],
        },
    }
    table = {"S": start, "E": end}
    out = []
    for item in lines:
        if isinstance(item, bytes):
            out.append(item)
            continue
        kind, number, extra = item
        body = extra(home_id, tap)
        line = {
            **table[kind],
            "recorded_at": "2026-03-01T09:30:15+09:00",
            "action_id": number,
            **body,
        }
        out.append(json.dumps(line).encode())
    (folder.path / "transitions.jsonl").write_bytes(b"\n".join(out) + b"\n")
    return folder.path, home_id


def _tap_start(sid, tap):
    return {"action": tap, "from_screen_id": sid}


def _tap_end_other(_sid, _tap):
    return {"dump_failed": False, "to_screen_id": OTHER_SCREEN}


def _relaunch_off(_sid, _tap):
    return {
        "action": {"kind": "relaunch"},
        "from_screen_id": OTHER_SCREEN,
        "off_target": {"package": "com.other.app"},
    }


def test_the_clean_sequence_is_another_app(tmp_path, capsys):
    path, _ = _raw_run(
        tmp_path,
        [
            ("S", 1, _tap_start),
            ("E", 1, _tap_end_other),
            ("S", 2, _relaunch_off),
        ],
    )
    _, out, _ = run_tree(capsys, str(path))
    assert "├─ Network → another app" in out.splitlines()


def test_a_lone_surrogate_is_escaped_in_the_json_output(tmp_path, capsys):
    folder = full_run(tmp_path)
    home_id = home_screen().screen_id
    path = folder.path / "screens" / f"{home_id}.json"
    text = path.read_text(encoding="utf-8").replace("Welcome", "Wel\\ud800come")
    path.write_text(text, encoding="utf-8")
    code, out, _ = run_tree(capsys, str(folder.path), "--json")
    assert code == 0
    out.encode("utf-8")  # a lone surrogate would raise here
    assert "Wel\\ud800come" in out
    assert json.loads(out)["screens"][home_id]["elements"][0]["name"] == "Wel\ud800come"
    _, text_out, _ = run_tree(capsys, str(folder.path))
    text_out.encode("utf-8")


def test_the_settings_limit_is_read_when_the_reader_runs(tmp_path, monkeypatch):
    from uiprobe import settings

    path = tmp_path / "f"
    path.write_bytes(b"x" * 10)
    monkeypatch.setattr(settings, "MAX_SETTINGS_BYTES", 5)
    with pytest.raises(settings.SettingsError, match="larger than 5 bytes"):
        settings._read_bytes(path, "f")
    assert settings._read_bytes(path, "f", 10) == b"x" * 10


# --- the one rule for a trusted tap ---


def _no_action(_sid, _tap):
    return {}


def _tap_end_ok(sid, _tap):
    return {"dump_failed": False, "to_screen_id": sid}


def _tap_end_not_sent(_sid, _tap):
    return {"not_sent": True, "reason": "covered_by_other"}


def _relaunch_same_package(_sid, _tap):
    return {
        "action": {"kind": "relaunch"},
        "from_screen_id": OTHER_SCREEN,
        "off_target": {"package": PACKAGE},
    }


def _unknown_and_no_edge(tmp_path, capsys, lines, *, gated=True):
    path, home_id = _raw_run(tmp_path, lines)
    code, out, err = run_tree(capsys, str(path))
    assert code == 0
    assert "├─ Network → (result unknown)" in out.splitlines()
    assert err.splitlines() == ([f"uiprobe: warning: {GATE_WARNING}"] if gated else [])
    _, out, _ = run_tree(capsys, str(path), "--json")
    data = json.loads(out)
    network = next(
        e for e in data["screens"][home_id]["elements"] if e["name"] == "Network"
    )
    assert network["to"] == {"unknown": True}
    assert network["pressed"] is False
    return data


def test_off_target_naming_the_target_package_is_unknown(tmp_path, capsys):
    _unknown_and_no_edge(
        tmp_path,
        capsys,
        [
            ("S", 1, _tap_start),
            ("E", 1, _tap_end_other),
            ("S", 2, _relaunch_same_package),
        ],
        gated=False,
    )


# --- the all-or-nothing gate on transitions.jsonl ---


def _log(folder: Folder) -> Path:
    return folder.path / "transitions.jsonl"


def _edit(folder: Folder, edit) -> None:
    """Change the lines of transitions.jsonl: `edit` gets the list of byte lines
    (without the final newline) and returns the new list."""
    raw = _log(folder).read_bytes()
    lines = raw.split(b"\n")[:-1]
    _log(folder).write_bytes(b"\n".join(edit(lines)) + b"\n")


def _change(index: int, **changes):
    """An edit that sets keys of line `index` (None removes a key)."""

    def edit(lines):
        data = json.loads(lines[index])
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        return [*lines[:index], json.dumps(data).encode(), *lines[index + 1 :]]

    return edit


# line numbers (from 0) in full_run: 0 launch start, 1 its end, 2 start of the tap
# on Network, 3 its end, 4 and 5 the tap on Back home, ...
BAD_LOGS = {
    "a blank line": lambda ls: [*ls[:3], b"", *ls[3:]],
    "an extra key on a start line": _change(2, extra=1),
    "a bool as the action id": _change(2, action_id=True),
    "a string as the action id": _change(2, action_id="2"),
    "an action id of 0": _change(0, action_id=0),
    "a time that is not a time": _change(2, recorded_at="yesterday"),
    "a wrong schema name": _change(2, schema="uiprobe.transition_start/2"),
    "dump_failed as a string": _change(3, dump_failed="false"),
    "a missing action": _change(2, action=None),
    "an action that is not an object": _change(2, action="tap"),
    "an off_target on a tap": _change(2, off_target={"package": "com.other.app"}),
    "a restart on a tap": _change(2, restart=True),
    "a replay that is false": _change(2, replay=False),
    "a replay that is a string": _change(2, replay="true"),
    "a replay on a launch": _change(0, replay=True),
    "a replay on a relaunch": _change(14, replay=True),
    "a tap without from_screen_id": _change(2, from_screen_id=None),
    "an end before its start": lambda ls: [ls[0], ls[1], ls[3], ls[2], *ls[4:]],
    "two end lines": lambda ls: [*ls[:4], ls[3], *ls[4:]],
    "a repeated start line": lambda ls: [*ls[:3], ls[2], *ls[3:]],
    "an id that does not rise": _change(4, action_id=1),
    "a line that is not an object": lambda ls: [*ls[:4], b"[1]", *ls[4:]],
    "a damaged complete line": lambda ls: [*ls[:4], b"{broken", *ls[4:]],
    "an empty object": lambda ls: [*ls[:4], b"{}", *ls[4:]],
    "a start line without action next to an end": lambda ls: [
        *ls[:4],
        json.dumps(
            {
                "schema": "uiprobe.transition_start/1",
                "recorded_at": "2026-03-01T09:30:15+09:00",
                "action_id": 99,
            }
        ).encode(),
        *ls[4:],
    ],
}


@pytest.mark.parametrize("name", sorted(BAD_LOGS))
def test_any_line_off_the_shape_means_no_press_results(tmp_path, capsys, name):
    folder = full_run(tmp_path)
    _edit(folder, BAD_LOGS[name])
    home_id = home_screen().screen_id
    code, out, err = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    assert code == 0
    assert err == ""
    assert data["warnings"] == [GATE_WARNING]
    assert data["root"] == home_id
    assert data["unreached"] == sorted(
        [NETWORK.screen_id, ABOUT.screen_id], key=list(data["screens"]).index
    )
    for node in data["screens"].values():
        for element in node["elements"]:
            assert element.get("pressed", False) is False
            assert element.get("to", {"unknown": True}) == {"unknown": True}
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert err == f"uiprobe: warning: {GATE_WARNING}\n"
    assert out.count("(screen not reached from a pressed row") == 2
    for ending in ("→ (same screen)", "→ another app", "→ next screen", "(not sent"):
        assert ending not in out


def test_a_file_that_fails_still_shows_the_rows_that_were_tapped(tmp_path, capsys):
    folder = full_run(tmp_path)
    _edit(folder, BAD_LOGS["a blank line"])
    _, out, _ = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert "├─ Network → (result unknown)" in lines
    assert "├─ Same → (result unknown)" in lines
    assert "├─ Wi-Fi ·" in lines


def test_a_file_that_passes_shows_its_results(tmp_path, capsys):
    folder = full_run(tmp_path)
    code, out, err = run_tree(capsys, str(folder.path))
    assert (code, err) == (0, "")
    assert out.splitlines() == FULL_TEXT


def _replay_run(tmp_path: Path, *, replay: bool) -> Folder:
    """A run whose Network row is pressed again after a relaunch, as a path
    replay (or the same run with that tap written as an ordinary one)."""
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    network_id = folder.screen(NETWORK)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, network_id)
    folder.start("relaunch", network_id)
    folder.end(home_id)
    more = {"replay": True} if replay else {}
    folder.start("tap", home_id, home.elements[2], **more)
    folder.end(network_id)
    folder.tap(NETWORK, 2, home_id)
    folder.finish()
    return folder


def test_a_replay_tap_passes_the_gate_and_shows_as_an_ordinary_pressed_row(
    tmp_path, capsys
):
    (tmp_path / "plain").mkdir()
    (tmp_path / "flagged").mkdir()
    plain = _replay_run(tmp_path / "plain", replay=False)
    flagged = _replay_run(tmp_path / "flagged", replay=True)
    start = [
        json.loads(line)
        for line in _log(flagged).read_text(encoding="utf-8").splitlines()
        if '"replay"' in line
    ]
    assert [(s["action"]["kind"], s["replay"]) for s in start] == [("tap", True)]
    code, out, err = run_tree(capsys, str(plain.path))
    assert (code, err) == (0, "")
    code, replay_out, err = run_tree(capsys, str(flagged.path))
    assert (code, err) == (0, "")
    assert replay_out == out
    assert "├─ Network" in replay_out.splitlines()
    assert "result unknown" not in replay_out
    _, plain_json, _ = run_tree(capsys, str(plain.path), "--json")
    _, flagged_json, _ = run_tree(capsys, str(flagged.path), "--json")
    assert json.loads(flagged_json)["warnings"] == []
    assert json.loads(flagged_json)["screens"] == json.loads(plain_json)["screens"]


def test_an_empty_or_missing_log_passes(tmp_path, capsys):
    folder = full_run(tmp_path)
    _log(folder).write_bytes(b"")
    _, _, err = run_tree(capsys, str(folder.path))
    assert err == ""
    _log(folder).unlink()
    _, _, err = run_tree(capsys, str(folder.path))
    assert err == ""


def test_an_end_that_is_dump_failed_or_failed_passes(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch", restart=True)
    folder.end(home_id)
    folder.tap(home, 3, dump_failed=True)
    folder.start("relaunch", home_id)
    folder.end(failed="start_timeout")
    folder.writer.close()
    code, out, err = run_tree(capsys, str(folder.path))
    assert (code, err) == (0, "")
    assert "├─ Network → (could not be read)" in out.splitlines()


def test_a_final_start_line_with_no_end_is_only_that_tap_unknown(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    network_id = folder.screen(NETWORK)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, network_id)
    folder.tap(home, 6, home_id)
    folder.start("tap", home_id, home.elements[11])  # the run ended right here
    folder.writer.close()
    code, out, err = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert (code, err) == (0, "")
    assert "├─ Network" in lines
    assert "├─ About → (same screen)" in lines
    assert "├─ Same → (result unknown)" in lines


def _cut_run(tmp_path: Path, keep: int) -> Folder:
    """A run of three taps whose transitions.jsonl is cut `keep` bytes short of
    its end, as a crash in the middle of the last write leaves it."""
    folder = Folder(tmp_path)
    home = make_screen(
        ".Home",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="LinearLayout", flags=CLICK),
            el(3, 2, text="Network"),
            el(4, 1, cls="LinearLayout", flags=CLICK),
            el(5, 4, text="日本語の名前"),
        ],
    )
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.start(
        "tap",
        home_id,
        {
            "id": "e2",
            "class": "android.widget.LinearLayout",
            "text": "Network",
            "bounds": [0, 0, 1, 1],
        },
    )
    folder.end(home_id)
    folder.start(
        "tap",
        home_id,
        {
            "id": "e4",
            "class": "android.widget.LinearLayout",
            "text": "日本語の名前",
            "bounds": [0, 0, 1, 1],
        },
    )
    folder.end(home_id)
    folder.writer.close()
    data = _log(folder).read_bytes()
    _log(folder).write_bytes(data[: len(data) - keep])
    return folder


def test_a_last_end_line_cut_short_leaves_only_that_tap_unknown(tmp_path, capsys):
    folder = _cut_run(tmp_path, keep=30)
    code, out, err = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert code == 0
    assert err == f"uiprobe: warning: {CUT_WARNING}\n"
    assert "├─ Network → (same screen)" in lines
    assert "└─ 日本語の名前 → (result unknown)" in lines


def test_a_last_start_line_cut_short_is_ignored(tmp_path, capsys):
    folder = _cut_run(tmp_path, keep=0)
    data = _log(folder).read_bytes()
    last_start = data.rstrip(b"\n").rsplit(b"\n", 2)[0]  # up to the 3rd action's start
    cut = data.rindex(b'"action_id": 3, "action"')
    _log(folder).write_bytes(data[: cut + 20])
    assert not _log(folder).read_bytes().endswith(b"\n")
    code, out, err = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert code == 0
    assert err == f"uiprobe: warning: {CUT_WARNING}\n"
    assert "├─ Network → (same screen)" in lines
    assert "└─ 日本語の名前 ·" in lines
    assert last_start


def test_a_line_cut_in_the_middle_of_a_character_is_cut_short(tmp_path, capsys):
    folder = _cut_run(tmp_path, keep=0)
    data = _log(folder).read_bytes()
    cut = data.rindex("日本語".encode()) + 1  # inside the first character's bytes
    cut = (
        data.rindex(b'"action_id": 3, "action"')
        + data[data.rindex(b'"action_id": 3, "action"') :].index("日".encode())
        + 1
    )
    _log(folder).write_bytes(data[:cut])
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert err == f"uiprobe: warning: {CUT_WARNING}\n"
    assert "├─ Network → (same screen)" in out.splitlines()


def test_a_complete_last_line_without_its_newline_passes(tmp_path, capsys):
    folder = _cut_run(tmp_path, keep=0)
    data = _log(folder).read_bytes()
    _log(folder).write_bytes(data.rstrip(b"\n"))
    code, out, err = run_tree(capsys, str(folder.path))
    assert (code, err) == (0, "")
    assert "└─ 日本語の名前 → (same screen)" in out.splitlines()


def test_a_cut_line_that_is_not_last_fails_the_gate(tmp_path, capsys):
    folder = _cut_run(tmp_path, keep=0)
    data = _log(folder).read_bytes()
    lines = data.split(b"\n")
    lines[2] = lines[2][:40]  # a line cut short, with its newline after it
    _log(folder).write_bytes(b"\n".join(lines))
    _, _, err = run_tree(capsys, str(folder.path))
    assert err == f"uiprobe: warning: {GATE_WARNING}\n"


def test_off_target_does_not_make_a_screen_with_a_target_file_another_app(
    tmp_path, capsys
):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, home_id)
    folder.start("relaunch", home_id, off_target_package="com.other.app")
    folder.end(home_id)
    folder.writer.close()
    _, out, err = run_tree(capsys, str(folder.path))
    assert err == ""
    assert "├─ Network → (same screen)" in out.splitlines()
    assert "another app" not in out
