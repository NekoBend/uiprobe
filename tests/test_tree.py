"""The tree command: a run folder shown as a tree, from files only."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from uiprobe import cli
from uiprobe.crawl import Limits, fingerprint_of
from uiprobe.crawl_tree import TREE_SCHEMA, read_tree
from uiprobe.model import Bounds, Element, Screen
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from pathlib import Path

PACKAGE = "com.example.app"
OTHER_SCREEN = "s-aaaaaaaaaaaa"
LEGEND = "· = 押していないので、その下は見ていない行"
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
        self.writer.write_transition_start(
            self.action, kind, from_screen, element, **more
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
    "├─ [見出し] Welcome",
    "├─ Network",
    "│  ├─ Back home → (上に出た画面: 最初の画面)",
    "│  └─ Info",
    "│     └─ [見出し] Version 1",
    "├─ About → (上に出た画面: Info)",
    '├─ "Menu" ·',
    "├─ [icon: ImageButton] ·",
    "├─ Wi-Fi ·",
    "├─ Same → (同じ画面)",
    "├─ Other app → 別のアプリ",
    "├─ Broken → (読めなかった)",
    "└─ Blocked → (送らなかった: covered_by_other)",
    LEGEND,
]


def test_the_whole_tree(tmp_path, capsys):
    folder = full_run(tmp_path)
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert out.splitlines() == FULL_TEXT
    assert err == ""


def test_a_row_is_named_by_its_first_text_only(tmp_path, capsys):
    folder = full_run(tmp_path)
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "Mobile, Wi-Fi" not in out


def test_ids_add_the_screen_and_the_other_app(tmp_path, capsys):
    folder = full_run(tmp_path)
    code, out, _ = run_tree(capsys, str(folder.path), "--ids")
    lines = out.splitlines()
    assert code == 0
    assert lines[1] == f"├─ (画面 {home_screen().screen_id}  .Home)"
    assert f"│  ├─ (画面 {NETWORK.screen_id}  .Network)" in lines
    assert any(
        line.endswith(f"(上に出た画面: Info) {ABOUT.screen_id}") for line in lines
    )
    assert "├─ Other app → 別のアプリ (com.other.app)" in lines


def test_why_adds_the_reasons_of_unpressed_rows(tmp_path, capsys):
    folder = full_run(tmp_path)
    _, out, _ = run_tree(capsys, str(folder.path), "--why")
    lines = out.splitlines()
    assert '├─ "Menu" · [deny_word, screen_edge]' in lines
    assert "├─ Wi-Fi · [toggle]" in lines
    assert "├─ [icon: ImageButton] ·" in lines
    assert "├─ Same → (同じ画面)" in lines


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
    assert lines[-2] == "└─ (1 件は対応する行が見つからない)"
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
    assert by_name["Welcome"]["kind"] == "heading"
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
    assert f"(押した行からは辿れなかった画面 {ABOUT.screen_id}  .About)" in lines
    assert "└─ [見出し] Version 1" in lines
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    assert json.loads(out)["unreached"] == [ABOUT.screen_id]


def test_another_packages_screen_file_is_another_app(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = home_screen()
    home_id = folder.screen(home)
    foreign = make_screen(".Pick", [el(1, cls="FrameLayout")], package="com.other.app")
    foreign_id = folder.screen(foreign)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 3, foreign_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "├─ Network → 別のアプリ" in out.splitlines()
    assert "Pick" not in out
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    data = json.loads(out)
    assert data["unreached"] == []
    assert data["screens"][foreign_id]["package"] == "com.other.app"


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
    assert "├─ Network → (読めなかった)" in out.splitlines()
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
    assert "├─ Network → (読めなかった)" in out.splitlines()
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    run = json.loads(out)["run"]
    assert run["finished"] is False
    assert run["stop_reason"] is None
    assert run["actions"] == 1 + 1


def test_a_damaged_line_is_a_warning_and_the_rest_is_shown(tmp_path, capsys):
    folder = full_run(tmp_path)
    path = folder.path / "transitions.jsonl"
    path.write_bytes(path.read_bytes() + b"{not json\n")
    code, out, err = run_tree(capsys, str(folder.path))
    assert code == 0
    assert out.splitlines() == FULL_TEXT
    assert "transitions.jsonl line" in err
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    assert any("transitions.jsonl" in w for w in json.loads(out)["warnings"])


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
        "このランでは画面の文字を記録していません (record_text = false)"
    )
    assert "├─ [row: LinearLayout]" in out
    assert "├─ [icon: ImageButton] ·" in lines
    for word in ("Network", "Welcome", "About", "Menu", "見出し"):
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
        "├─ [見出し] [password field]",
        "└─ [見出し] Sign in",
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
    assert "[見出し] head\\x07\\nline" in out
    assert "(画面 " + sid + "  .Evil\\x1b[31m)" in out
    assert "別のアプリ (com.bad\\x1b]0;pwn)" in out
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
    assert "no package" in err


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


def test_the_command_does_not_contact_an_adb_server(tmp_path, capsys, fake_adb):
    folder = full_run(tmp_path)
    fake_adb.add_device("emulator-5554")
    for extra in ((), ("--json",), ("--ids", "--why")):
        code = cli.run(
            ["--adb-server", fake_adb.address, "tree", str(folder.path), *extra]
        )
        assert code == 0
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
