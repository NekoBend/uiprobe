"""The tree command and the rows found after scrolling."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.test_tree import (
    CLICK,
    PACKAGE,
    Folder,
    _close_writers,  # noqa: F401 - a fixture of the module this one builds on
    el,
    make_screen,
    run_tree,
)
from uiprobe import crawl_tree
from uiprobe.crawl_tree import GATE_WARNING, read_tree

if TYPE_CHECKING:
    from pathlib import Path


def listing(texts, *, activity=".List"):
    """A list: a scrollable container with one clickable row per text."""
    elements = [
        el(1, cls="FrameLayout"),
        el(2, 1, cls="RecyclerView", flags=("scrollable",)),
    ]
    for index, text in enumerate(texts):
        row = 3 + 2 * index
        elements.append(el(row, 2, cls="LinearLayout", flags=CLICK))
        elements.append(el(row + 1, row, text=text))
    return make_screen(activity, elements)


LEAF = make_screen(".Leaf", [el(1, cls="FrameLayout"), el(2, 1, text="Leaf")])


def scroll(folder: Folder, from_screen, after, *, to=None, read=True):
    """A sent scroll on `from_screen`, and the screen it read after it."""
    folder.action += 1
    folder.writer.write_transition_start(
        folder.action, "scroll", from_screen.screen_id, point=(5, 50), amount=3
    )
    folder.end((to or after).screen_id)
    if read:
        folder.writer.write_scroll_screen(folder.action, after)
    return folder.action


def tap_after(folder: Folder, screen, number, to, scroll_id):
    """A tap decided on the screen as a scroll's result showed it."""
    folder.action += 1
    folder.writer.write_transition_start(
        folder.action,
        "tap",
        screen.screen_id,
        screen.elements[number - 1],
        (5, 5),
        after_scroll=scroll_id,
    )
    folder.end(to)


def one_scroll(tmp_path: Path, *, texts=("A", "B", "C"), after_texts=("C", "D", "E")):
    """Rows A to C, A and B pressed, a scroll, then D pressed on what it showed."""
    folder = Folder(tmp_path)
    first = listing(texts)
    after = listing(after_texts)
    assert first.screen_id == after.screen_id
    folder.screen(first)
    folder.screen(LEAF)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.tap(first, 3, LEAF.screen_id)  # A
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    folder.tap(first, 5, LEAF.screen_id)  # B
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    folder.tap(first, 7, LEAF.screen_id)  # C
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    number = scroll(folder, first, after)
    # D, at the element id B has in the file
    tap_after(folder, after, 5, LEAF.screen_id, number)
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    folder.finish()
    return folder, first, after, number


def names(tree, sid):
    return [row.name for row in tree.screens[sid].rows if row.kind == "row"]


def test_the_rows_after_a_scroll_are_listed_under_the_same_screen_in_order(
    tmp_path,
):
    folder, first, _after, number = one_scroll(tmp_path)
    tree = read_tree(folder.path)
    assert tree.warnings == []
    rows = tree.screens[first.screen_id].rows
    assert [r.name for r in rows if r.kind == "row"] == ["A", "B", "C", "D", "E"]
    assert [r.after_scroll for r in rows if r.kind == "row"] == [
        None,
        None,
        None,
        number,
        number,
    ]


def test_a_tap_decided_after_a_scroll_marks_its_own_row_not_the_row_at_its_id(
    tmp_path,
):
    # D was pressed at the element id (e5) that row B has in the screen's file;
    # B has its own tap, and D's row is the pressed one among the new rows.
    folder, first, _after, _number = one_scroll(tmp_path)
    rows = {r.name: r for r in read_tree(folder.path).screens[first.screen_id].rows}
    assert rows["B"].pressed
    assert rows["B"].to == {"screen_id": LEAF.screen_id}
    assert rows["D"].pressed
    assert rows["D"].to == {"screen_id": LEAF.screen_id}
    assert not rows["E"].pressed
    assert rows["E"].to is None
    assert rows["B"].action_id != rows["D"].action_id


def test_the_repeated_row_is_shown_once_by_default(tmp_path):
    assert crawl_tree.SCROLLED_REPEATS == "once"
    folder, first, _after, _number = one_scroll(tmp_path)
    rows = read_tree(folder.path).screens[first.screen_id].rows
    assert [r.name for r in rows].count("C") == 1
    # the one C is the row of the screen's own file, and its press stays
    c = next(r for r in rows if r.name == "C")
    assert c.after_scroll is None
    assert c.pressed


def test_the_repeated_row_is_shown_as_seen_with_the_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl_tree, "SCROLLED_REPEATS", "as_seen")
    folder, first, _after, number = one_scroll(tmp_path)
    rows = read_tree(folder.path).screens[first.screen_id].rows
    assert [r.name for r in rows] == ["A", "B", "C", "C", "D", "E"]
    again = rows[3]
    assert again.after_scroll == number
    assert not again.pressed  # the one press of C is on the row of the file
    assert rows[2].pressed


def test_the_text_tree_lists_the_rows_in_order(tmp_path, capsys):
    folder, _first, _after, _number = one_scroll(tmp_path)
    code, out, err = run_tree(capsys, str(folder.path))
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "com.example.app (com.example.app)",
        "├─ A",
        "│  └─ (screen with no named rows)",
        "├─ B → see: A",
        "├─ C → see: A",
        "├─ D → see: A",
        "└─ E ·",
        "· = not pressed, so nothing below it was looked at",
        "→ see: X = it led to a screen already shown under X",
        "(screen with no named rows) = the screen it led to has no row with a name"
        " (see --json)",
        "left out of the tree (see --json): texts outside rows: 1",
    ]


def test_the_json_names_the_scroll_a_row_came_from(tmp_path, capsys):
    folder, first, _after, number = one_scroll(tmp_path)
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    elements = json.loads(out)["screens"][first.screen_id]["elements"]
    rows = {e["name"]: e for e in elements if e["kind"] == "row"}
    assert "after_scroll" not in rows["A"]
    assert rows["D"]["after_scroll"] == number
    assert rows["D"]["pressed"] is True


def test_a_run_without_scrolls_has_no_after_scroll_key(tmp_path, capsys):
    folder = Folder(tmp_path)
    first = listing(["A"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.finish()
    _, out, _ = run_tree(capsys, str(folder.path), "--json")
    assert "after_scroll" not in out


def test_a_scroll_that_was_not_sent_or_not_read_adds_no_rows(tmp_path):
    folder = Folder(tmp_path)
    first = listing(["A", "B"])
    after = listing(["B", "C"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.action += 1
    folder.writer.write_transition_start(
        folder.action, "scroll", first.screen_id, point=(5, 50), amount=3
    )
    folder.end(not_sent="covered_by_other")
    folder.action += 1
    folder.writer.write_transition_start(
        folder.action, "scroll", first.screen_id, point=(5, 50), amount=3
    )
    folder.end(dump_failed=True)
    # a file of a scroll that no line names is not read either
    folder.writer.write_scroll_screen(99, after)
    folder.finish()
    tree = read_tree(folder.path)
    assert names(tree, first.screen_id) == ["A", "B"]
    assert tree.warnings == []


def test_a_scroll_without_its_screen_file_adds_no_rows(tmp_path):
    folder = Folder(tmp_path)
    first = listing(["A", "B"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, listing(["B", "C"]), read=False)
    folder.finish()
    tree = read_tree(folder.path)
    assert names(tree, first.screen_id) == ["A", "B"]


def test_a_scroll_screen_of_another_package_fails_the_gate(tmp_path):
    folder = Folder(tmp_path)
    first = listing(["A", "B"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    foreign = make_screen(".List", listing(["B"]).elements, package="com.other")
    scroll(folder, first, foreign)
    folder.finish()
    assert GATE_WARNING in read_tree(folder.path).warnings


def test_a_changed_id_after_a_scroll_lists_its_rows_under_the_first_id(tmp_path):
    folder = Folder(tmp_path)
    first = listing(["A", "B", "C"])
    second = listing(["C", "D"])  # one row fewer: another screen id
    assert first.screen_id != second.screen_id
    folder.screen(first)
    folder.screen(second)  # first seen after the scroll
    folder.screen(LEAF)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.tap(first, 3, LEAF.screen_id)
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    number = scroll(folder, first, second)
    # D, decided on the second id
    tap_after(folder, second, 5, LEAF.screen_id, number)
    folder.start("back", LEAF.screen_id)
    folder.end(second.screen_id)
    folder.finish()
    tree = read_tree(folder.path)
    rows = tree.screens[first.screen_id].rows
    assert [r.name for r in rows if r.kind == "row"] == ["A", "B", "C", "D"]
    d = next(r for r in rows if r.name == "D")
    assert (d.after_scroll, d.pressed) == (number, True)
    assert d.to == {"screen_id": LEAF.screen_id}
    # the leaf is reached from the first id, through the row found by scrolling
    assert LEAF.screen_id not in tree.unreached


def test_scrolls_in_a_row_through_ids_all_list_under_the_first(tmp_path):
    folder = Folder(tmp_path)
    one = listing(["A", "B", "C"])
    two = listing(["C", "D"])
    three = listing(["D"])
    for screen in (one, two, three):
        folder.screen(screen)
    folder.start("launch")
    folder.end(one.screen_id)
    scroll(folder, one, two)
    scroll(folder, two, three)  # decided on the second id
    folder.finish()
    tree = read_tree(folder.path)
    assert names(tree, one.screen_id) == ["A", "B", "C", "D"]


def test_a_tap_on_a_row_in_hidden_text_runs_still_goes_to_a_row(tmp_path):
    # With the texts not recorded rows cannot be told apart by name; the rows
    # of a scroll's read are listed as seen.
    folder = Folder(tmp_path, record_text=False)
    first = listing(["A", "B"])
    after = listing(["B", "C"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, after)
    folder.finish()
    rows = read_tree(folder.path).screens[first.screen_id].rows
    # rows told apart by nothing but their structure: all four are listed
    assert [r.name for r in rows if r.kind == "row"] == ["[row: LinearLayout]"] * 4


# -- the transitions gate ---------------------------------------------------------


def log_of(folder: Folder) -> Path:
    return folder.path / "transitions.jsonl"


def rewrite(folder: Folder, edit) -> None:
    path = log_of(folder)
    lines = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    for line in lines:
        edit(line)
    path.write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )


def scroll_start(line):
    return line["schema"].endswith("start/1") and line["action"]["kind"] == "scroll"


def test_a_scroll_as_the_writer_writes_it_passes_the_gate(tmp_path):
    folder, _first, _after, _number = one_scroll(tmp_path)
    starts = [
        json.loads(line)
        for line in log_of(folder).read_text("utf-8").splitlines()
        if '"scroll"' in line
    ]
    assert len(starts) == 1
    assert starts[0]["action"] == {
        "kind": "scroll",
        "point": [5, 50],
        "amount": 3,
    }
    assert "from_screen_id" in starts[0]
    assert read_tree(folder.path).warnings == []


DAMAGES = [
    lambda line: line["action"].pop("amount"),  # a scroll needs its amount
    lambda line: line["action"].pop("point"),
    lambda line: line.pop("from_screen_id"),
    lambda line: line["action"].update(
        element={"id": "e1", "class": "x", "bounds": [0, 0, 1, 1]}
    ),
    lambda line: line["action"].update(amount=[1]),
    lambda line: line["action"].update(amount="3"),
    lambda line: line["action"].update(amount=1.5),
    lambda line: line["action"].update(amount=True),
    lambda line: line["action"].update(amount=-1),
    lambda line: line["action"].update(amount=0),
    lambda line: line["action"].update(amount=11),
    lambda line: line["action"].update(point=[-1, 5]),
    lambda line: line["action"].update(point=[5, -1]),
    lambda line: line["action"].update(end_point=[5, 10]),
    lambda line: line["action"].update(point=[5]),
    lambda line: line.update(replay=True),
    lambda line: line.update(restart=True),
    lambda line: line.update(after_scroll=3),
    lambda line: line.update(off_target={"package": "x"}),
    lambda line: line["action"].update(kind="Scroll"),
    lambda line: line["action"].update(kind="swipe"),
    lambda line: line["action"].update(direction="down"),
]


@pytest.mark.parametrize("damage", DAMAGES)
def test_a_scroll_not_as_written_fails_the_gate(tmp_path, damage):
    folder, _first, _after, _number = one_scroll(tmp_path)

    def edit(line):
        if scroll_start(line):
            damage(line)

    rewrite(folder, edit)
    tree = read_tree(folder.path)
    assert GATE_WARNING in tree.warnings


@pytest.mark.parametrize("kind", ["tap", "back", "launch"])
def test_an_amount_on_another_kind_fails_the_gate(tmp_path, kind):
    folder, _first, _after, _number = one_scroll(tmp_path)

    def edit(line):
        if line["schema"].endswith("start/1") and line["action"]["kind"] == kind:
            line["action"]["amount"] = 3

    rewrite(folder, edit)
    assert GATE_WARNING in read_tree(folder.path).warnings


def test_the_writer_refuses_amount_without_a_scroll_and_a_scroll_without_it(
    tmp_path,
):
    folder = Folder(tmp_path)
    with pytest.raises(ValueError, match="amount"):
        folder.writer.write_transition_start(1, "tap", None, point=(1, 2), amount=1)
    with pytest.raises(ValueError, match="amount"):
        folder.writer.write_transition_start(1, "scroll", None, point=(1, 9))
    with pytest.raises((TypeError, ValueError)):
        folder.writer.write_transition_start(
            1,
            "scroll",
            None,
            point=(1, 9),
            amount="3",  # ty: ignore[invalid-argument-type]
        )


def test_the_scroll_screen_file_is_written_once_per_scroll_and_never_over(tmp_path):
    folder = Folder(tmp_path)
    screen = listing(["A"])
    folder.writer.write_scroll_screen(3, screen)
    assert (folder.path / "screens" / "scroll-3.json").is_file()
    with pytest.raises(Exception, match=r"scroll-3\.json"):
        folder.writer.write_scroll_screen(3, screen)
    with pytest.raises(ValueError, match="action_id"):
        folder.writer.write_scroll_screen(0, screen)


def test_the_package_of_the_screen_rows_is_the_runs(tmp_path):
    folder, first, _after, _number = one_scroll(tmp_path)
    assert read_tree(folder.path).screens[first.screen_id].package == PACKAGE


@pytest.mark.parametrize("value", [0, -1, "3", 1.5, True, None, [1]])
def test_after_scroll_must_be_a_positive_number_on_a_tap(tmp_path, value):
    folder, _first, _after, _number = one_scroll(tmp_path)

    def edit(line):
        if line["schema"].endswith("start/1") and line["action"]["kind"] == "tap":
            line["after_scroll"] = value

    rewrite(folder, edit)
    assert GATE_WARNING in read_tree(folder.path).warnings


def test_a_tap_with_after_scroll_as_the_writer_writes_it_passes(tmp_path):
    folder, _first, _after, number = one_scroll(tmp_path)
    taps = [
        json.loads(line)
        for line in log_of(folder).read_text("utf-8").splitlines()
        if '"after_scroll"' in line
    ]
    assert [t["after_scroll"] for t in taps] == [number]
    assert read_tree(folder.path).warnings == []


def test_the_writer_gives_after_scroll_to_a_tap_only(tmp_path):
    folder = Folder(tmp_path)
    with pytest.raises(ValueError, match="after_scroll"):
        folder.writer.write_transition_start(1, "back", None, after_scroll=2)
    with pytest.raises(ValueError, match="action_id"):
        folder.writer.write_transition_start(
            1, "tap", None, point=(1, 2), after_scroll=0
        )


def test_a_tap_without_its_read_is_matched_by_the_fields_it_recorded(tmp_path):
    # The row itself has the text here, so the fields tell the reads apart
    # even when the start line does not name the scroll.
    def rows(texts):
        elements = [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="RecyclerView", flags=("scrollable",)),
        ]
        elements += [
            el(3 + i, 2, cls="Button", text=text, flags=CLICK)
            for i, text in enumerate(texts)
        ]
        return make_screen(".Rows", elements)

    folder = Folder(tmp_path)
    first, after = rows(["A", "B"]), rows(["B", "C"])
    folder.screen(first)
    folder.screen(LEAF)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, after)
    folder.tap(after, 4, LEAF.screen_id)  # C, at the element id of B in the file
    folder.finish()
    found = {r.name: r for r in read_tree(folder.path).screens[first.screen_id].rows}
    assert found["C"].pressed
    assert not found["B"].pressed


def test_the_largest_amount_and_a_zero_point_pass_the_gate(tmp_path):
    folder, _first, _after, _number = one_scroll(tmp_path)

    def edit(line):
        if scroll_start(line):
            line["action"].update(amount=10, point=[0, 0])

    rewrite(folder, edit)
    assert read_tree(folder.path).warnings == []


def test_after_scroll_must_name_a_scroll_that_was_sent(tmp_path):
    for reason in ("not_sent", "failed"):
        (tmp_path / reason).mkdir()
        folder = Folder(tmp_path / reason)
        first = listing(["A", "B"])
        folder.screen(first)
        folder.screen(LEAF)
        folder.start("launch")
        folder.end(first.screen_id)
        folder.action += 1
        folder.writer.write_transition_start(
            folder.action, "scroll", first.screen_id, point=(5, 50), amount=3
        )
        if reason == "not_sent":
            folder.end(not_sent="covered_by_other")
        else:
            folder.writer.write_transition_end(folder.action, failed="adb_error")
        number = folder.action
        tap_after(folder, first, 3, LEAF.screen_id, number)
        folder.finish()
        assert GATE_WARNING in read_tree(folder.path).warnings, reason


def test_two_identical_rows_that_show_up_together_stay_two(tmp_path):
    folder = Folder(tmp_path)
    first = listing(["A", "B"])
    after = listing(["C", "C"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, after)
    folder.finish()
    rows = read_tree(folder.path).screens[first.screen_id].rows
    assert [r.name for r in rows if r.kind == "row"] == ["A", "B", "C", "C"]


def test_a_row_that_was_listed_twice_before_hides_only_two_repeats(tmp_path):
    folder = Folder(tmp_path)
    first = listing(["A", "A"])
    after = listing(["A", "A", "A"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, after)
    folder.finish()
    rows = read_tree(folder.path).screens[first.screen_id].rows
    assert [(r.name, r.after_scroll is not None) for r in rows if r.kind == "row"] == [
        ("A", False),
        ("A", False),
        ("A", True),
    ]


def test_the_later_destination_of_a_repeated_row_is_kept(tmp_path):
    folder = Folder(tmp_path)
    first = listing(["A", "B", "C"])
    after = listing(["C", "D"])
    other = make_screen(".Other", [el(1, cls="FrameLayout"), el(2, 1, text="Other")])
    for screen in (first, LEAF, other):
        folder.screen(screen)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.tap(first, 7, LEAF.screen_id)  # C goes to the leaf
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    number = scroll(folder, first, after)
    tap_after(folder, after, 3, other.screen_id, number)  # C again: elsewhere
    folder.finish()
    rows = [
        r for r in read_tree(folder.path).screens[first.screen_id].rows if r.name == "C"
    ]
    assert len(rows) == 1  # shown once
    assert rows[0].to == {"screen_id": LEAF.screen_id}
    assert rows[0].also_to == [{"screen_id": other.screen_id}]


# -- a label that became clickable, and a scroll that ended on another page --------


def mixed(*, clickable):
    """A list with one clickable row (A) and a label (B) that is clickable only
    when asked: the same ids and classes, so the same screen id."""
    return make_screen(
        ".List",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="RecyclerView", flags=("scrollable",)),
            el(3, 2, cls="LinearLayout", flags=CLICK),
            el(4, 3, text="A"),
            el(5, 2, text="B", flags=CLICK if clickable else ()),
        ],
    )


def test_a_label_that_became_clickable_keeps_its_press_and_destination(tmp_path):
    folder = Folder(tmp_path)
    first, after = mixed(clickable=False), mixed(clickable=True)
    assert first.screen_id == after.screen_id
    folder.screen(first)
    folder.screen(LEAF)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.tap(first, 3, LEAF.screen_id)  # A
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    number = scroll(folder, first, after)
    tap_after(folder, after, 5, LEAF.screen_id, number)  # B, now a button
    folder.start("back", LEAF.screen_id)
    folder.end(first.screen_id)
    folder.finish()
    tree = read_tree(folder.path)
    assert tree.warnings == []
    rows = [r for r in tree.screens[first.screen_id].rows if r.name == "B"]
    # the label of the first read stays a text, and the button is its own row
    assert sorted(r.kind for r in rows) == ["row", "text"]
    button = next(r for r in rows if r.kind == "row")
    assert button.after_scroll == number
    assert button.pressed
    assert button.to == {"screen_id": LEAF.screen_id}
    assert button.action_id is not None
    text = next(r for r in rows if r.kind == "text")
    assert not text.pressed
    assert text.to is None


def test_a_label_that_stayed_a_label_is_still_shown_once(tmp_path):
    folder = Folder(tmp_path)
    first = mixed(clickable=False)
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, mixed(clickable=False))
    folder.finish()
    rows = read_tree(folder.path).screens[first.screen_id].rows
    assert [r.name for r in rows].count("B") == 1


def test_a_row_of_a_scroll_that_led_to_another_page_is_not_a_row_of_the_source(
    tmp_path,
):
    folder = Folder(tmp_path)
    first = listing(["A", "B"])
    page = make_screen(
        ".List",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="LinearLayout", flags=CLICK),
            el(3, 2, text="Detail row"),
        ],
    )
    assert page.screen_id != first.screen_id
    assert page.activity == first.activity
    folder.screen(first)
    folder.screen(page)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.action += 1
    folder.writer.write_transition_start(
        folder.action, "scroll", first.screen_id, point=(5, 50), amount=3
    )
    folder.end(page.screen_id, scroll_effect="navigation")
    folder.writer.write_scroll_screen(folder.action, page)
    folder.finish()
    tree = read_tree(folder.path)
    assert tree.warnings == []
    source = [r.name for r in tree.screens[first.screen_id].rows if r.kind == "row"]
    assert source == ["A", "B"]  # nothing of the page, and no after_scroll row
    assert all(r.after_scroll is None for r in tree.screens[first.screen_id].rows)
    # the page is its own screen, not part of the source's
    assert "Detail row" in [r.name for r in tree.screens[page.screen_id].rows]


def test_a_scroll_that_only_scrolled_still_adds_its_rows(tmp_path):
    # the control of the test above: the same writes with no navigation mark
    folder = Folder(tmp_path)
    first, after = listing(["A", "B"]), listing(["B", "C"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, after)
    folder.finish()
    rows = read_tree(folder.path).screens[first.screen_id].rows
    assert [r.name for r in rows if r.kind == "row"] == ["A", "B", "C"]


# -- the gate and the writer agree -------------------------------------------------


def two_lists(tmp_path):
    """Two screens, each with its own scroll: the second one's read is not the
    first one's screen."""
    folder = Folder(tmp_path)
    list_a = listing(["A", "B"])
    list_b = listing(["P", "Q"], activity=".Other")
    assert list_a.screen_id != list_b.screen_id
    for screen in (list_a, list_b, LEAF):
        folder.screen(screen)
    folder.start("launch")
    folder.end(list_a.screen_id)
    first = scroll(folder, list_a, listing(["B", "C"]))
    return folder, list_a, list_b, first


def test_a_tap_may_cite_a_scroll_read_of_its_own_screen(tmp_path):
    folder, list_a, _list_b, first = two_lists(tmp_path)
    tap_after(folder, listing(["B", "C"]), 3, LEAF.screen_id, first)
    folder.finish()
    assert read_tree(folder.path).warnings == []
    assert list_a.screen_id == listing(["B", "C"]).screen_id


def test_a_tap_may_not_cite_a_scroll_read_of_another_screen(tmp_path):
    folder, _list_a, list_b, first = two_lists(tmp_path)
    # a tap decided on the other screen names the scroll of the first one
    tap_after(folder, list_b, 3, LEAF.screen_id, first)
    folder.finish()
    assert GATE_WARNING in read_tree(folder.path).warnings


def test_a_tap_may_cite_a_scroll_whose_read_came_on_a_later_try(tmp_path):
    # the end line of that scroll names no screen (the dump failed), so nothing
    # says the read was another screen's
    folder = Folder(tmp_path)
    first = listing(["A", "B"])
    after = listing(["B", "C"])
    folder.screen(first)
    folder.screen(LEAF)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.action += 1
    folder.writer.write_transition_start(
        folder.action, "scroll", first.screen_id, point=(5, 50), amount=3
    )
    folder.end(dump_failed=True)
    folder.writer.write_scroll_screen(folder.action, after)
    number = folder.action
    tap_after(folder, after, 3, LEAF.screen_id, number)
    folder.finish()
    assert read_tree(folder.path).warnings == []


@pytest.mark.parametrize("amount", [0, 11, -1, 100])
def test_the_writer_refuses_the_amounts_the_gate_refuses(tmp_path, amount):
    folder = Folder(tmp_path)
    with pytest.raises(ValueError, match="amount must"):
        folder.writer.write_transition_start(
            1, "scroll", None, point=(1, 9), amount=amount
        )
    assert not (folder.path / "transitions.jsonl").exists() or not [
        line
        for line in (folder.path / "transitions.jsonl").read_text("utf-8").splitlines()
        if '"scroll"' in line
    ]


@pytest.mark.parametrize("amount", [1, 2, 10])
def test_the_amounts_the_gate_accepts_are_written(tmp_path, amount):
    folder = Folder(tmp_path)
    first = listing(["A"])
    folder.screen(first)
    folder.start("launch")
    folder.end(first.screen_id)
    folder.action += 1
    folder.writer.write_transition_start(
        folder.action, "scroll", first.screen_id, point=(5, 50), amount=amount
    )
    folder.end(first.screen_id)
    folder.finish()
    assert read_tree(folder.path).warnings == []


def test_the_writer_and_the_gate_share_the_one_limit():
    from uiprobe import crawl_tree, guards

    assert crawl_tree._MAX_AMOUNT == guards.MAX_SCROLL_AMOUNT == 10
