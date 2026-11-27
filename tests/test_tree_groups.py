"""`uiprobe tree`: the text between the rows of a list, as groups in the JSON and
gaps in the HTML page. The text tree does not show it."""

from __future__ import annotations

import json
import re

import pytest

from tests.test_tree import (
    CLICK,
    LEGEND,
    LEGEND_DESC,
    Folder,
    _close_writers,  # noqa: F401 - a fixture of the module this one builds on
    el,
    make_screen,
    run_tree,
)
from tests.test_tree_scroll import LEAF, scroll
from uiprobe.crawl_tree import read_tree


def listing(spec, *, toolbar=False, footer=False, buttons=0, activity=".Groups"):
    """A screen whose list is one container with a child per item of `spec`:
    ("row", text), ("hold", text) (a plain container with a clickable in it),
    ("sep", text) (a plain container with a text), ("flat", text) (a plain text),
    ("deep", text) (a text three levels down), ("two", first, second) (two texts).
    `toolbar` puts a button above the list, `footer` a text below it."""
    elements = [el(1, cls="FrameLayout")]
    number = [1]

    def add(parent, **kwargs):
        number[0] += 1
        elements.append(el(number[0], parent, **kwargs))
        return number[0]

    if toolbar:
        add(1, cls="ImageButton", desc="Up", flags=CLICK)
    for tool in range(buttons):
        add(1, cls="ImageButton", desc=f"Tool {tool}", flags=CLICK)
    holder = add(1, cls="RecyclerView", flags=("scrollable",))
    for item in spec:
        kind, text = item[0], item[1]
        if kind == "row":
            row = add(holder, cls="LinearLayout", flags=CLICK)
            add(row, text=text)
        elif kind == "hold":
            box = add(holder, cls="LinearLayout")
            row = add(box, cls="Button", flags=CLICK)
            add(row, text=text)
        elif kind == "sep":
            box = add(holder, cls="LinearLayout")
            add(box, text=text)
        elif kind == "flat":
            add(holder, cls="TextView", text=text)
        elif kind == "deep":
            one = add(holder, cls="LinearLayout")
            two = add(one, cls="FrameLayout")
            add(two, text=text)
        elif kind == "many":  # 500 empty children, the text on the last one
            box = add(holder, cls="LinearLayout")
            for _ in range(499):
                add(box, cls="View")
            add(box, text=text)
        elif kind == "two":
            box = add(holder, cls="LinearLayout")
            add(box, text=text)
            add(box, text=item[2])
    if footer:
        add(1, cls="TextView", text="Footer")
    return make_screen(activity, elements)


def run_of(tmp_path, screen, *, record_text=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    folder = Folder(tmp_path, record_text=record_text)
    folder.screen(screen)
    folder.start("launch")
    folder.end(screen.screen_id)
    folder.finish()
    return folder


def groups_of(tree, sid):
    """name -> group, for every row and text of the screen."""
    return {r.name: r.group for r in tree.screens[sid].rows}


def tree_of(tmp_path, spec, **kwargs):
    screen = listing(spec, **kwargs)
    folder = run_of(tmp_path, screen)
    return read_tree(folder.path), screen.screen_id, folder


ROWS = [
    ("row", "A"),
    ("row", "B"),
    ("sep", "Privacy"),
    ("row", "C"),
    ("row", "D"),
]


def test_a_child_without_clickables_between_rows_is_a_separator(tmp_path):
    tree, sid, _ = tree_of(tmp_path, ROWS)
    assert groups_of(tree, sid) == {"A": 0, "B": 0, "Privacy": 1, "C": 1, "D": 1}
    assert tree.screens[sid].groups == [None, "Privacy"]


def json_of(capsys, folder):
    code, out, err = run_tree(capsys, str(folder.path), "--json")
    assert code == 0, err
    return json.loads(out)


def html_of(tmp_path, capsys, folder, *extra):
    out = tmp_path / "page" / "out.html"
    out.parent.mkdir()
    code, _, err = run_tree(capsys, str(folder.path), "--html", str(out), *extra)
    assert code == 0, err
    return out.read_text(encoding="utf-8")


def test_children_before_the_first_row_and_after_the_last_are_not_separators(
    tmp_path, capsys
):
    spec = [("sep", "Top"), ("row", "A"), ("row", "B"), ("row", "C"), ("sep", "End")]
    tree, sid, folder = tree_of(tmp_path, spec)
    assert tree.screens[sid].groups == []
    assert set(groups_of(tree, sid).values()) == {None}
    screen = json_of(capsys, folder)["screens"][sid]
    assert "groups" not in screen
    assert [e["group"] for e in screen["elements"]] == [None] * 5


def test_a_child_that_holds_a_clickable_is_not_a_separator(tmp_path):
    spec = [("row", "A"), ("hold", "B"), ("row", "C"), ("sep", "S"), ("row", "D")]
    tree, sid, _ = tree_of(tmp_path, spec)
    assert groups_of(tree, sid) == {"A": 0, "B": 0, "C": 0, "S": 1, "D": 1}
    assert tree.screens[sid].groups == [None, "S"]


def test_a_list_of_fewer_than_three_rows_has_no_separator(tmp_path):
    tree, sid, _ = tree_of(tmp_path, [("row", "A"), ("sep", "S"), ("row", "B")])
    assert tree.screens[sid].groups == []
    assert set(groups_of(tree, sid).values()) == {None}


def test_only_the_chosen_list_counts_when_lists_are_nested(tmp_path):
    # the outer list has five children that hold a clickable (one of them is a
    # card holding the inner list); the inner list has three and a separator
    elements = [
        el(1, cls="FrameLayout"),
        el(2, 1, cls="RecyclerView"),
        el(3, 2, cls="CardView"),
        el(4, 3, cls="RecyclerView"),
        el(5, 4, cls="Button", flags=CLICK),
        el(6, 5, text="x"),
        el(7, 4, cls="LinearLayout"),
        el(8, 7, text="inner"),
        el(9, 4, cls="Button", flags=CLICK),
        el(10, 9, text="y"),
        el(11, 4, cls="Button", flags=CLICK),
        el(12, 11, text="z"),
    ]
    for number, name in enumerate("ABCD"):
        elements.append(el(13 + 2 * number, 2, cls="Button", flags=CLICK))
        elements.append(el(14 + 2 * number, 13 + 2 * number, text=name))
    screen = make_screen(".Nested", elements)
    tree = read_tree(run_of(tmp_path, screen).path)
    assert tree.screens[screen.screen_id].groups == []
    assert groups_of(tree, screen.screen_id)["inner"] is None


def test_groups_are_numbered_in_order_and_rows_outside_the_list_have_none(tmp_path):
    spec = [
        ("row", "A"),
        ("sep", "One"),
        ("row", "B"),
        ("row", "C"),
        ("sep", "Two"),
        ("row", "D"),
    ]
    tree, sid, _ = tree_of(tmp_path, spec, toolbar=True, footer=True)
    assert groups_of(tree, sid) == {
        "Up": None,
        "A": 0,
        "One": 1,
        "B": 1,
        "C": 1,
        "Two": 2,
        "D": 2,
        "Footer": None,
    }
    assert tree.screens[sid].groups == [None, "One", "Two"]


def test_two_separators_in_a_row_are_two_groups_and_a_missing_text_is_none(tmp_path):
    spec = [("row", "A"), ("sep", ""), ("flat", "Head"), ("row", "B"), ("row", "C")]
    tree, sid, _ = tree_of(tmp_path, spec)
    assert tree.screens[sid].groups == [None, None, "Head"]
    assert groups_of(tree, sid) == {"A": 0, "Head": 2, "B": 2, "C": 2}


def test_the_text_of_a_separator_is_the_first_non_empty_text_in_it(tmp_path):
    spec = [
        ("row", "A"),
        ("deep", "Deep text"),
        ("row", "B"),
        ("two", "  ", "Second"),
        ("row", "C"),
        ("two", "First", "Second"),
        ("row", "D"),
    ]
    tree, sid, _ = tree_of(tmp_path, spec)
    assert tree.screens[sid].groups == [None, "Deep text", "Second", "First"]


def test_without_recorded_texts_a_separator_has_no_text(tmp_path, capsys):
    screen = listing(ROWS)
    folder = run_of(tmp_path, screen, record_text=False)
    tree = read_tree(folder.path)
    assert tree.record_text is False
    assert tree.screens[screen.screen_id].groups == [None, None]
    data = json_of(capsys, folder)["screens"][screen.screen_id]
    assert data["groups"] == [
        {"index": 0, "text": None},
        {"index": 1, "text": None},
    ]
    rows = [e for e in data["elements"] if e["kind"] == "row"]
    assert [e["group"] for e in rows] == [0, 0, 1, 1]
    assert all(e["name"].startswith("[row:") for e in rows)


def test_the_json_has_group_on_every_element_and_groups_only_with_separators(
    tmp_path, capsys
):
    folder = run_of(tmp_path, listing(ROWS, toolbar=True))
    screen = next(iter(json_of(capsys, folder)["screens"].values()))
    assert screen["groups"] == [
        {"index": 0, "text": None},
        {"index": 1, "text": "Privacy"},
    ]
    by_name = {e["name"]: e["group"] for e in screen["elements"]}
    assert by_name == {'"Up"': None, "A": 0, "B": 0, "Privacy": 1, "C": 1, "D": 1}
    assert all("group" in e for e in screen["elements"])
    plain = run_of(tmp_path / "plain", listing([("row", "A"), ("row", "B")]))
    other = next(iter(json_of(capsys, plain)["screens"].values()))
    assert "groups" not in other
    assert all(e["group"] is None for e in other["elements"])


def test_the_text_tree_does_not_show_the_separators(tmp_path, capsys):
    folder = run_of(tmp_path, listing(ROWS, toolbar=True, footer=True))
    _, text, _ = run_tree(capsys, str(folder.path))
    # Golden text, as origin/main draws this screen: the two texts outside rows
    # (the separator and the footer) are only counted in the last line.
    assert text.splitlines() == [
        "com.example.app (com.example.app)",
        '├─ "Up" ·',
        "├─ A ·",
        "├─ B ·",
        "├─ C ·",
        "└─ D ·",
        LEGEND,
        LEGEND_DESC,
        "left out of the tree (see --json): texts outside rows: 2",
    ]
    assert "Privacy" not in text


def test_the_html_has_a_gap_and_the_small_text_between_groups(tmp_path, capsys):
    page = html_of(tmp_path, capsys, run_of(tmp_path / "r", listing(ROWS)))
    assert page.count('class="between"') == 1
    assert '<li class="between">Privacy</li>' in page
    order = [page.index(f'<span class="name">{n}</span>') for n in "ABCD"]
    gap = page.index('<li class="between">')
    assert order[1] < gap < order[2]
    assert ".between{" in page
    assert not re.search(r'class="between"[^<]*·', page)


def test_the_html_gap_has_no_text_when_the_separator_has_none(tmp_path, capsys):
    spec = [("row", "A"), ("sep", ""), ("row", "B"), ("row", "C")]
    page = html_of(tmp_path, capsys, run_of(tmp_path / "r", listing(spec)))
    assert page.count('<li class="between"></li>') == 1


def test_the_html_writes_no_gap_for_a_screen_without_separators(tmp_path, capsys):
    spec = [("row", "A"), ("row", "B"), ("row", "C")]
    page = html_of(tmp_path, capsys, run_of(tmp_path / "r", listing(spec)))
    assert 'class="between"' not in page.split("</style>")[1]


def test_the_separator_text_is_escaped_and_the_page_stays_inside_the_csp(
    tmp_path, capsys
):
    bad = "<script>alert(1)</script> \"x\" & 'y' <img src=//e/x>"
    spec = [("row", "A"), ("sep", bad), ("row", "B"), ("row", "C")]
    page = html_of(tmp_path, capsys, run_of(tmp_path / "r", listing(spec)))
    body = page.split("</style>")[1]
    assert "<script" not in page.lower()
    assert "<img" not in page
    assert not re.search(r"<[^>]* on[a-z]+=", body)
    assert (
        '<li class="between">&lt;script&gt;alert(1)&lt;/script&gt; '
        "&quot;x&quot; &amp; &#x27;y&#x27; &lt;img src=//e/x&gt;</li>"
    ) in page
    assert (
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
    ) in page
    # the new CSS uses no url(), @import or other fetch
    css = page.split("<style>")[1].split("</style>")[0]
    assert "url(" not in css
    assert "@import" not in css


def test_the_html_gap_is_not_a_row(tmp_path, capsys):
    page = html_of(tmp_path, capsys, run_of(tmp_path / "r", listing(ROWS)))
    gap = re.search(r'<li class="between">[^<]*</li>', page)
    assert gap is not None
    assert "line" not in gap[0]
    assert "·" not in gap[0]
    assert "<details" not in gap[0]


def test_rows_after_a_scroll_continue_the_last_group_and_new_separators_add_groups(
    tmp_path,
):
    folder = Folder(tmp_path)
    first = listing(
        [("row", "A"), ("row", "B"), ("sep", "Privacy"), ("row", "C"), ("row", "D")]
    )
    second = listing(
        [("row", "D"), ("row", "E"), ("sep", "More"), ("row", "F"), ("row", "G")],
        activity=".Groups",
    )
    for screen in (first, second, LEAF):
        folder.screen(screen)
    folder.start("launch")
    folder.end(first.screen_id)
    number = scroll(folder, first, second)
    folder.finish()
    tree = read_tree(folder.path)
    node = tree.screens[first.screen_id]
    named = {r.name: (r.group, r.after_scroll) for r in node.rows}
    # D is a repeat and is not listed again; E continues group 1
    assert named["E"] == (1, number)
    assert named["More"] == (2, number)
    assert named["F"] == (2, number)
    assert named["A"] == (0, None)
    assert node.groups == [None, "Privacy", "More"]


def test_a_separator_text_seen_again_after_a_scroll_is_not_a_new_group(tmp_path):
    folder = Folder(tmp_path)
    first = listing(
        [("row", "A"), ("row", "B"), ("sep", "Privacy"), ("row", "C"), ("row", "D")]
    )
    # scrolled up a little: B, the separator again, C and D again, then E and F
    second = listing(
        [
            ("row", "B"),
            ("sep", "Privacy"),
            ("row", "C"),
            ("row", "D"),
            ("row", "E"),
            ("row", "F"),
        ]
    )
    for screen in (first, second):
        folder.screen(screen)
    folder.start("launch")
    folder.end(first.screen_id)
    number = scroll(folder, first, second)
    folder.finish()
    node = read_tree(folder.path).screens[first.screen_id]
    named = {r.name: (r.group, r.after_scroll) for r in node.rows}
    assert node.groups == [None, "Privacy"]
    assert named["E"] == (1, number)
    assert named["F"] == (1, number)


@pytest.mark.parametrize("record_text", [True, False])
def test_a_scroll_read_without_a_list_leaves_its_rows_outside_any_group(
    tmp_path, record_text
):
    folder = Folder(tmp_path, record_text=record_text)
    first = listing(ROWS)
    second = listing([("row", "X"), ("row", "Y")])
    folder.screen(first)
    folder.screen(second)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, second)
    folder.finish()
    node = read_tree(folder.path).screens[first.screen_id]
    later = [r for r in node.rows if r.after_scroll is not None]
    assert later
    assert {r.group for r in later} == {None}


def scrolled(tmp_path, first_spec, second_spec, **kwargs):
    """A run with one scroll from the first read to the second; the tree of it."""
    folder = Folder(tmp_path, record_text=kwargs.pop("record_text", None))
    first = listing(first_spec)
    second = listing(second_spec, **kwargs)
    folder.screen(first)
    folder.screen(second)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, second)
    folder.finish()
    return read_tree(folder.path).screens[first.screen_id]


def by_name(node):
    return {r.name: r.group for r in node.rows}


def test_rows_above_the_first_common_row_keep_the_group_before_a_matched_separator(
    tmp_path,
):
    first = [
        ("row", "A"),
        ("sep", "X"),
        ("row", "B"),
        ("row", "C"),
        ("sep", "Y"),
        ("row", "D"),
        ("row", "E"),
    ]
    second = [("row", "P"), ("sep", "X"), ("row", "B"), ("row", "C")]
    node = scrolled(tmp_path, first, second)
    assert node.groups == [None, "X", "Y"]
    assert by_name(node)["P"] == 0  # not the group of Y
    assert by_name(node)["A"] == 0
    assert by_name(node)["D"] == 2


def test_a_second_separator_after_a_scroll_stays_a_second_group(tmp_path):
    first = [("row", "A"), ("sep", "Today"), ("row", "B"), ("row", "C")]
    second = [
        ("row", "C"),
        ("row", "D"),
        ("sep", "Yesterday"),
        ("row", "E"),
        ("row", "F"),
    ]
    node = scrolled(tmp_path, first, second)
    assert node.groups == [None, "Today", "Yesterday"]
    named = by_name(node)
    assert (named["B"], named["C"], named["D"]) == (1, 1, 1)
    assert (named["E"], named["F"]) == (2, 2)


def test_a_read_that_shares_no_row_with_the_earlier_ones_has_no_groups(tmp_path):
    first = [("row", "A"), ("sep", "X"), ("row", "B"), ("row", "C")]
    second = [("row", "P"), ("row", "Q"), ("sep", "Z"), ("row", "R")]
    node = scrolled(tmp_path, first, second)
    assert node.groups == [None, "X"]
    named = by_name(node)
    assert (named["P"], named["Q"], named["R"], named["Z"]) == (None,) * 4


def test_without_recorded_texts_a_later_read_cannot_be_lined_up(tmp_path):
    node = scrolled(
        tmp_path, ROWS, [("row", "C"), ("row", "D"), ("row", "E")], record_text=False
    )
    later = [r for r in node.rows if r.after_scroll is not None]
    assert later
    assert {r.group for r in later} == {None}
    assert [r.group for r in node.rows if r.view_key is None] == [0, 0, 1, 1]


def test_a_read_whose_list_is_another_element_gives_no_groups(tmp_path):
    # the list shrinks to two rows, so four toolbar buttons are the biggest group
    first = [("row", n) for n in "AB"] + [("sep", "X")] + [("row", n) for n in "CDE"]
    second = [("row", "D"), ("row", "E")]
    node = scrolled(tmp_path, first, second, buttons=4)
    later = [r for r in node.rows if r.after_scroll is not None]
    assert {r.name for r in later} >= {"Tool 0", "Tool 3"}
    assert {r.group for r in later} == {None}
    assert node.groups == [None, "X"]


def test_many_separators_in_a_later_read_are_matched_in_one_pass(tmp_path):
    count = 1500
    first = [("row", "a0"), ("row", "a1")]
    second = [("row", "a1")]
    for n in range(count):
        first += [("sep", f"s{n}"), ("row", f"r{n}")]
        second += [("sep", f"s{n}"), ("row", f"r{n}")]
    node = scrolled(tmp_path, first, second)
    assert len(node.groups) == count + 1
    assert node.groups[1:] == [f"s{n}" for n in range(count)]
    named = by_name(node)
    assert (named["r0"], named[f"r{count - 1}"]) == (1, count)


def test_the_html_has_one_gap_line_for_each_separator_in_order(tmp_path, capsys):
    spec = [
        ("row", "A"),
        ("sep", "First"),
        ("sep", "Second"),
        ("row", "B"),
        ("row", "C"),
    ]
    page = html_of(tmp_path, capsys, run_of(tmp_path / "r", listing(spec)))
    assert re.findall(r'<li class="between">([^<]*)</li>', page) == ["First", "Second"]
    name_b = page.index('<span class="name">B</span>')
    assert page.index(">Second</li>") < name_b


def test_a_separator_with_many_children_keeps_the_text_on_the_last_one(tmp_path):
    spec = [("row", "A"), ("many", "Far text"), ("row", "B"), ("row", "C")]
    tree, sid, _ = tree_of(tmp_path, spec)
    assert tree.screens[sid].groups == [None, "Far text"]


def test_a_merged_cut_off_row_takes_the_group_of_the_version_it_stands_at(tmp_path):
    from uiprobe.model import Bounds

    bottom = 2400

    def shelf(items):
        """A list; items are ("row" | "sep", texts, top, bottom)."""
        elements = [
            el(1, cls="FrameLayout"),
            el(
                2,
                1,
                cls="RecyclerView",
                flags=("scrollable",),
                bounds=Bounds(0, 200, 1080, bottom),
            ),
        ]
        number = 3
        for kind, texts, top, end in items:
            box = Bounds(0, top, 1080, end)
            flags = CLICK if kind == "row" else ()
            elements.append(el(number, 2, cls="LinearLayout", flags=flags, bounds=box))
            for offset, text in enumerate(texts, 1):
                elements.append(el(number + offset, number, text=text, bounds=box))
            number += 1 + len(texts)
        return make_screen(".Shelf", elements)

    first = shelf(
        [
            ("row", ("A",), 200, 400),
            ("row", ("B",), 400, 600),
            ("sep", ("Today",), 600, 700),
            ("row", ("C",), 700, 900),
            ("row", ("Cut",), 2300, bottom),
        ]
    )
    after = shelf(
        [
            ("row", ("C",), 200, 400),
            ("row", ("Cut", "On"), 400, 700),
            ("row", ("D",), 700, 900),
        ]
    )
    folder = Folder(tmp_path)
    folder.screen(first)
    folder.screen(after)
    folder.start("launch")
    folder.end(first.screen_id)
    scroll(folder, first, after)
    folder.finish()
    node = read_tree(folder.path).screens[first.screen_id]
    cut = next(r for r in node.rows if r.name == "Cut")
    assert cut.texts == ("Cut", "On")  # the fuller version
    assert cut.after_scroll is not None
    assert by_name(node) == {"A": 0, "B": 0, "Today": 1, "C": 1, "Cut": 1, "D": 1}
    assert node.groups == [None, "Today"]


def test_an_anchored_group_takes_a_separator_text_seen_later(tmp_path):
    node = scrolled(
        tmp_path,
        [("row", "B"), ("row", "C"), ("row", "D")],
        [("row", "P"), ("sep", "X"), ("row", "B"), ("row", "C")],
    )
    assert node.groups == [None, "X"]
    assert by_name(node) == {"B": 1, "C": 1, "D": 1, "P": 0, "X": 1}


def test_a_short_first_read_does_not_disable_grouping(tmp_path):
    node = scrolled(
        tmp_path,
        [("row", "A"), ("row", "B")],
        [("row", "B"), ("sep", "X"), ("row", "C"), ("row", "D")],
    )
    assert node.groups == [None, "X"]
    named = by_name(node)
    assert named["A"] is None  # shares no row with the list
    assert (named["B"], named["C"], named["D"]) == (0, 1, 1)


def _long_screen(count):
    from uiprobe.crawl_tree import Row, RunTree, ScreenNode

    rows = [
        Row(
            element_id=f"e{n}",
            kind="row",
            class_name="x.Row",
            name=f"r{n}",
            texts=(f"r{n}",),
            hidden=False,
            name_from="text",
            group=0 if n % 2 == 0 else count - 1,
        )
        for n in range(2 * count)
    ]
    node = ScreenNode(
        "s-0", "p", None, rows, groups=[None, *[f"s{n}" for n in range(count - 1)]]
    )
    return RunTree(
        package="p",
        label=None,
        started_at=None,
        ended_at=None,
        stop_reason=None,
        actions=0,
        finished=True,
        record_text=True,
        root="s-0",
        screens={"s-0": node},
        unreached=[],
        warnings=[],
    )


def test_each_separator_gets_one_gap_line_and_only_the_html_makes_them():
    from uiprobe.cli.tree import _NO_OTHERS, _entries, _Others, _Tally

    tree = _long_screen(400)
    html = _entries(tree, "s-0", ids=False, why=False, tally=_Tally(), others=_Others())
    lines = sum(len(e.gap) for e in html if e.gap is not None)
    assert 0 < lines <= 399  # at most one per separator, not one per crossing
    for others in (None, _NO_OTHERS):
        plain = _entries(
            tree, "s-0", ids=False, why=False, tally=_Tally(), others=others
        )
        assert all(e.gap is None for e in plain)
