"""`uiprobe tree --html OUT.html`: the tree as one self-contained HTML file."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.test_tree import (
    LEGEND,
    LEGEND_ARROW,
    LEGEND_BACK,
    LEGEND_DESC,
    LEGEND_DISABLED,
    LEGEND_EMPTY,
    LEGEND_NO_CHANGE,
    LEGEND_SEE,
    LEGEND_TOGGLE,
    SUMMARY,
    Folder,
    _button_screen,
    _close_writers,  # noqa: F401 (an autouse fixture of test_tree)
    _edit_start,
    _popup_run,
    _skip_run,
    full_run,
    run_tree,
)
from uiprobe.cli.tree_html import (
    CSP,
    FOLD_OTHER,
    FOLD_POPUP,
    HFold,
    HPage,
    HRow,
    HSection,
    anchor_id,
    render_page,
)

LEGEND_POPUP = "[popup] = pressing it opened a popup; its rows are in the fold below it"
SUMMARY_HEAD = "left out of the tree (see --json): "
HTML_HEAD = "not shown as rows (in the folds below each screen, and in --json): "


class Page(HTMLParser):
    """The page as a list of elements with their text, for assertions."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.details: list[dict] = []
        self.tags: list[str] = []
        self.ids: list[str] = []
        self.hrefs: list[str] = []
        self.links: list[list[str]] = []
        self.summaries: list[dict] = []
        self.footer = ""
        self.in_footer = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "a":
            self.hrefs.append(a["href"])
            self.links.append([a["href"], ""])
        if tag == "footer":
            self.in_footer = True
        if tag == "details":
            node = {
                "open": "open" in a,
                "fold": a.get("class") == "fold",
                "depth": sum(1 for s in self.stack if s["tag"] == "details"),
                "summary": "",
            }
            self.details.append(node)
        else:
            node = {"tag": tag}
        node.setdefault("tag", tag)
        self.stack.append(node)
        if tag == "summary":
            self.summaries.append(node)
            node["text"] = ""

    def handle_endtag(self, tag):
        self.stack.pop()
        if tag == "footer":
            self.in_footer = False

    def handle_data(self, data):
        if self.in_footer:
            self.footer += data
        for node in self.stack:
            if node["tag"] == "summary":
                node["text"] += data
        if self.stack and self.stack[-1]["tag"] == "a":
            self.links[-1][1] += data


def write_html(tmp_path: Path, capsys, folder, *extra: str):
    out = tmp_path / "out.html"
    code, stdout, err = run_tree(capsys, str(folder.path), "--html", str(out), *extra)
    return code, stdout, err, out


def page_of(tmp_path, capsys, folder, *extra: str) -> tuple[str, Page]:
    code, _, err, out = write_html(tmp_path, capsys, folder, *extra)
    assert code == 0, err
    text = out.read_text(encoding="utf-8")
    return text, Page(text)


def test_first_level_is_open_and_everything_deeper_is_closed(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    _, page = page_of(out_dir, capsys, folder)
    summaries = [s["text"] for s in page.summaries]
    assert summaries[0] == "Network"
    network = page.details[0]
    assert network["open"] is True
    assert network["depth"] == 0
    # "Info" is below Network: a row with something under it, closed.
    info = next(
        d
        for d, s in zip(page.details, page.summaries, strict=True)
        if s["text"] == "Info"
    )
    assert info["open"] is False
    assert info["depth"] == 1
    for node, summary in zip(page.details, page.summaries, strict=True):
        if node["fold"]:
            assert node["open"] is False
        elif node["depth"] > 0:
            assert node["open"] is False, summary["text"]
        else:
            assert node["open"] is True, summary["text"]


def test_the_other_items_fold_is_closed_and_labelled(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    folds = [
        (d, s["text"])
        for d, s in zip(page.details, page.summaries, strict=True)
        if d["fold"]
    ]
    assert folds
    for node, label in folds:
        assert node["open"] is False
        assert label.startswith(FOLD_OTHER + " (")
    assert FOLD_OTHER == "Other items on this screen"
    for item in (
        "text outside rows: Welcome",
        "unnamed element: [icon: ImageButton]",
        "result of pressing Broken: → (could not be read)",
        "result of pressing Blocked: → (not sent: covered_by_other)",
    ):
        assert f">{item}<" in text
    assert "result of pressing Same" not in text  # its line says [no change]


def test_a_popup_row_gets_a_closed_fold_with_the_popup_rows(tmp_path, capsys):
    folder, _ = _popup_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    assert FOLD_POPUP == "Rows inside this popup"
    popup_folds = [
        d
        for d, s in zip(page.details, page.summaries, strict=True)
        if s["text"] == f"{FOLD_POPUP} (2)"
    ]
    assert len(popup_folds) == 2  # one under each row that opened the popup
    assert all(d["fold"] and not d["open"] for d in popup_folds)
    assert text.count("[popup]") >= 2
    assert ">OK<" in text.replace('<span class="name">', ">").replace("</span>", "<")
    # The popup's rows are not drawn as rows of the tree: their fold is closed.
    ask = next(
        d
        for d, s in zip(page.details, page.summaries, strict=True)
        if s["text"] == "Ask [popup]"
    )
    assert ask["open"] is True  # first level, with the fold below it


def test_unpressed_rows_are_grey_and_keep_the_dot_or_label(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, _ = page_of(out_dir, capsys, folder)
    assert (
        '<span class="line unpressed"><span class="name">Wi-Fi</span> [toggle]</span>'
        in text
    )
    assert (
        '<span class="line unpressed"><span class="name">&quot;Menu&quot;</span> ·'
        in text
    )
    # A pressed row is not grey and has no dot.
    assert (
        '<span class="line"><span class="name">Same</span> [no change]</span>' in text
    )
    assert ".unpressed{" in text


def test_see_links_point_at_an_existing_id(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    _, page = page_of(out_dir, capsys, folder)
    assert len(page.links) == 2
    assert sorted(label for _, label in page.links) == ["Info", "first screen"]
    assert len(page.ids) == len(set(page.ids))
    for href, _ in page.links:
        assert href.startswith("#")
        assert href[1:] in page.ids
    assert all(re.fullmatch(r"#[A-Za-z0-9_-]+", h) for h in page.hrefs)


def test_the_legend_and_the_summary_are_at_the_bottom(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    lines = [ln for ln in page.footer.splitlines() if ln.strip()]
    assert lines == [
        LEGEND,
        LEGEND_TOGGLE,
        LEGEND_NO_CHANGE,
        LEGEND_DESC,
        LEGEND_ARROW,
        LEGEND_SEE,
        LEGEND_EMPTY,
        SUMMARY.replace(SUMMARY_HEAD, HTML_HEAD),
    ]
    assert text.index("<footer>") > text.rindex('<span class="name">')
    assert text.index("</footer>") > text.index(HTML_HEAD)


def test_the_legend_has_a_line_for_each_mark_in_the_page(tmp_path, capsys):
    folder, _ = _popup_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    _, page = page_of(out_dir, capsys, folder)
    lines = [ln for ln in page.footer.splitlines() if ln.strip()]
    assert LEGEND in lines  # the unpressed popup rows are in the page
    assert LEGEND_POPUP in lines
    assert lines[-1].endswith("rows inside popups: 2")


def test_the_text_tree_is_not_printed_and_the_path_is(tmp_path, capsys):
    folder = full_run(tmp_path)
    code, out, err, target = write_html(tmp_path / "..", capsys, folder)
    assert code == 0
    assert out == f"{target}\n"
    assert err == ""
    assert "├─" not in out


def test_markup_in_names_is_escaped(tmp_path, capsys):
    folder = Folder(tmp_path)
    nasty = "<script>alert(1)</script> \"q\" & 's' <b>x</b>"
    home = _button_screen(".Home", [nasty, "Next"])
    nxt = _button_screen(".Next", ["Leaf"])
    home_id, next_id = folder.screen(home), folder.screen(nxt)
    folder.start("launch")
    folder.end(home_id)
    folder.tap(home, 2, next_id)
    folder.finish()
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    assert "<script" not in text.lower()
    assert "<b>" not in text
    assert (
        "&lt;script&gt;alert(1)&lt;/script&gt; &quot;q&quot; &amp; &#x27;s&#x27; "
        "&lt;b&gt;x&lt;/b&gt;"
    ) in text
    assert page.tags.count("script") == 0


def test_control_and_format_characters_are_shown_as_in_the_text_tree(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = _button_screen(".Home", ["a‮b\x1b[31m", "plain"])
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.finish()
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, _ = page_of(out_dir, capsys, folder)
    _, text_tree, _ = run_tree(capsys, str(folder.path))
    assert "a\\u202eb\\x1b[31m" in text_tree
    assert "a\\u202eb\\x1b[31m" in text
    assert "‮" not in text
    assert "\x1b" not in text


def test_the_page_is_self_contained(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    csp = (
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
    )
    assert csp in text
    assert CSP == "default-src 'none'; style-src 'unsafe-inline'"
    assert "<script" not in text.lower()
    assert "javascript:" not in text.lower()
    assert re.search(r"\son[a-z]+=", text) is None
    assert '<meta charset="utf-8">' in text
    assert 'name="viewport"' in text
    assert "<title>uiprobe tree: com.example.app</title>" in text
    for forbidden in (
        "<link",
        "<img",
        "<iframe",
        "<object",
        "<form",
        "src=",
        "@import",
        "url(",
    ):
        assert forbidden not in text
    assert all(h.startswith("#") for h in page.hrefs)


def test_record_text_false_has_the_placeholders_and_no_texts(tmp_path, capsys):
    folder = full_run(tmp_path)
    _edit_start(folder, record_text=False)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, _ = page_of(out_dir, capsys, folder)
    assert (
        "com.example.app (com.example.app)  "
        "screen texts were not recorded in this run (record_text = false)"
    ) in text
    assert "[row: LinearLayout]" in text
    for word in ("Network", "Welcome", "About", "Menu", "[text]"):
        assert word not in text


def test_a_password_field_stays_a_placeholder(tmp_path, capsys):
    from tests.test_tree import CLICK, el, make_screen

    folder = Folder(tmp_path)
    home = make_screen(
        ".Home",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, cls="EditText", text="hunter2", flags=(*CLICK, "password")),
        ],
    )
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.finish()
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, _ = page_of(out_dir, capsys, folder)
    assert "hunter2" not in text
    assert "[password field]" in text
    _, text_tree, _ = run_tree(capsys, str(folder.path))
    assert "[password field]" in text_tree


def test_ids_and_why_are_taken_from_the_options(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, _ = page_of(out_dir, capsys, folder, "--ids", "--why")
    assert "(screen s-" in text
    assert (
        '<span class="line unpressed"><span class="name">Wi-Fi</span>'
        " · [toggle]</span>" in text
    )
    assert "[toggle] = " not in text
    assert "[back] = " not in text


def test_an_existing_file_is_refused_and_left_alone(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    target = out_dir / "out.html"
    target.write_text("mine", encoding="utf-8")
    code, out, err = run_tree(capsys, str(folder.path), "--html", str(target))
    assert code == 2
    assert out == ""
    assert "already exists" in err
    assert target.read_text(encoding="utf-8") == "mine"
    assert [p.name for p in out_dir.iterdir()] == ["out.html"]


def test_a_link_is_refused_also_when_its_target_is_missing(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    gone = out_dir / "gone.html"
    link = out_dir / "link.html"
    try:
        link.symlink_to(gone)
    except OSError:
        pytest.skip("links cannot be made here")
    code, _, err = run_tree(capsys, str(folder.path), "--html", str(link))
    assert code == 2
    assert "already exists" in err
    assert not gone.exists()
    assert sorted(p.name for p in out_dir.iterdir()) == ["link.html"]


def test_a_missing_folder_is_refused_and_nothing_is_made(tmp_path, capsys):
    folder = full_run(tmp_path)
    target = tmp_path / "nope" / "out.html"
    code, out, err = run_tree(capsys, str(folder.path), "--html", str(target))
    assert code == 2
    assert out == ""
    assert "folder does not exist" in err
    assert not (tmp_path / "nope").exists()


def test_html_with_json_is_a_usage_error_and_writes_nothing(tmp_path, capsys):
    folder = full_run(tmp_path)
    target = tmp_path / "out.html"
    code, out, err = run_tree(capsys, str(folder.path), "--json", "--html", str(target))
    assert code == 2
    assert "--html cannot be combined with --json" in out + err
    assert not target.exists()


def test_a_bad_run_folder_writes_nothing(tmp_path, capsys):
    target = tmp_path / "out.html"
    code, _, err = run_tree(capsys, str(tmp_path / "missing"), "--html", str(target))
    assert code == 2
    assert err
    assert not target.exists()


def test_a_run_folder_the_text_tree_refuses_is_refused_here(tmp_path, capsys):
    folder = full_run(tmp_path)
    shot = folder.path / "run-start.json"
    moved = folder.path / "run-start.real"
    shot.replace(moved)
    try:
        shot.symlink_to(moved)
    except OSError:
        pytest.skip("links cannot be made here")
    target = tmp_path / "out.html"
    code, out, _ = run_tree(capsys, str(folder.path))
    code_html, _, _ = run_tree(capsys, str(folder.path), "--html", str(target))
    assert (code, code_html) == (2, 2)
    assert out == ""
    assert not target.exists()


def test_only_the_one_file_is_created(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    before = sorted(p.name for p in folder.path.rglob("*"))
    page_of(out_dir, capsys, folder)
    assert [p.name for p in out_dir.iterdir()] == ["out.html"]
    assert sorted(p.name for p in folder.path.rglob("*")) == before


def test_the_text_tree_is_unchanged_without_the_option(tmp_path, capsys):
    folder = full_run(tmp_path)
    _, out, _ = run_tree(capsys, str(folder.path))
    assert out.splitlines()[-1] == SUMMARY


def test_a_very_deep_tree_does_not_hit_the_recursion_limit():
    row = HRow("leaf")
    for number in range(5000):
        row = HRow(f"row {number}", children=[row])
    page = HPage("p", HSection("head", children=[row]), [], [], None)
    text = render_page(page)
    assert text.count("<details") == 5000


def test_anchor_ids_are_made_only_of_safe_characters():
    used: set[str] = set()
    first = anchor_id('s-"><script>', used)
    second = anchor_id('s-"><script>', used)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)
    assert first != second
    assert second in used


def test_a_fold_without_a_row_is_still_closed():
    page = HPage(
        "p",
        HSection("head", children=[HFold("F", ["a <b>"])]),
        [],
        [],
        None,
    )
    text = render_page(page)
    assert '<details class="fold">' in text
    assert "<details open" not in text
    assert "a &lt;b&gt;" in text


def _html_run(tmp_path):
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    return out_dir


def test_the_text_legend_and_summary_keep_their_wording(tmp_path, capsys):
    folder, _ = _popup_run(tmp_path)
    _, out, _ = run_tree(capsys, str(folder.path))
    lines = out.splitlines()
    assert "[popup] = pressing it opened a popup; its contents are in --json" in lines
    assert lines[-1].startswith(SUMMARY_HEAD)
    assert HTML_HEAD not in out


@pytest.mark.parametrize("through", ["parent", "grandparent", "relative"])
def test_a_linked_parent_is_refused_and_nothing_lands_behind_it(
    tmp_path, capsys, monkeypatch, through
):
    folder = full_run(tmp_path)
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("links cannot be made here")
    if through == "parent":
        target = str(link / "out.html")
    elif through == "grandparent":
        target = str(link / "sub" / "out.html")
    else:
        monkeypatch.chdir(tmp_path)
        target = "link/out.html"
    code, out, err = run_tree(capsys, str(folder.path), "--html", target)
    assert code == 2
    assert out == ""
    assert "link or a junction" in err
    assert sorted(p.name for p in real.rglob("*")) == ["sub"]


def test_a_real_parent_is_accepted(tmp_path, capsys):
    folder = full_run(tmp_path)
    out_dir = _html_run(tmp_path)
    code, _, err, out = write_html(out_dir, capsys, folder)
    assert code == 0, err
    assert out.is_file()


def test_the_text_path_does_no_html_work(tmp_path, capsys, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    runs = [full_run(tmp_path / "a"), _popup_run(tmp_path / "b")[0]]
    before = [run_tree(capsys, str(r.path))[1] for r in runs]

    def boom(*_args, **_kwargs):
        msg = "HTML work on the text path"
        raise AssertionError(msg)

    for name in (
        "_HtmlBuilder",
        "_Node",
        "_Others",
        "_left_out",
        "_left_out_lines",
        "_unmatched_lines",
        "render_page",
        "HRow",
        "HFold",
    ):
        monkeypatch.setattr(f"uiprobe.cli.tree.{name}", boom)
    after = [run_tree(capsys, str(r.path))[1] for r in runs]
    assert after == before
    assert all(after)


def test_duplicate_items_stay_separate_lines_and_match_the_count(tmp_path, capsys):
    from tests.test_tree import CLICK, el, make_screen

    folder = Folder(tmp_path)
    home = make_screen(
        ".Home",
        [
            el(1, cls="FrameLayout"),
            el(2, 1, text="Dup"),
            el(3, 1, text="Dup"),
            el(4, 1, cls="Button", text="Go", flags=CLICK),
        ],
    )
    home_id = folder.screen(home)
    folder.start("launch")
    folder.end(home_id)
    folder.finish()
    out_dir = _html_run(tmp_path)
    text, page = page_of(out_dir, capsys, folder)
    assert text.count(">text outside rows: Dup<") == 2
    assert f"{FOLD_OTHER} (2)" in [s["text"] for s in page.summaries]


def test_a_popup_row_keeps_its_further_results(tmp_path, capsys):
    folder = Folder(tmp_path)
    home = _button_screen(".Home", ["Ask"])
    dialog = _button_screen(".Dialog", ["OK"], popup=True)
    behind = _button_screen(".Behind", ["Deep"])
    ids = [folder.screen(s) for s in (home, dialog, behind)]
    folder.start("launch")
    folder.end(ids[0])
    folder.tap(home, 2, ids[1])
    folder.tap(dialog, 2, ids[2])
    folder.tap(dialog, 2, "s-aaaaaaaaaaaa")
    folder.start("relaunch", "s-aaaaaaaaaaaa", off_target_package="com.other.app")
    folder.end(ids[0])
    folder.finish()
    out_dir = _html_run(tmp_path)
    text, page = page_of(out_dir, capsys, folder)
    assert "(pressed again)</span> → com.other.app" in text
    assert LEGEND_ARROW in page.footer
    assert "→ PACKAGE" in page.footer


def test_ctrl_c_right_after_the_file_is_made_names_the_file(
    tmp_path, capsys, monkeypatch
):
    from uiprobe import htmlfile

    folder = full_run(tmp_path)
    out_dir = _html_run(tmp_path)
    target = out_dir / "out.html"

    def made_then_interrupted(path, *_args, **_kwargs):
        Path(path).touch()
        raise KeyboardInterrupt

    monkeypatch.setattr(htmlfile, "open", made_then_interrupted, raising=False)
    code, _, err = run_tree(capsys, str(folder.path), "--html", str(target))
    assert code == 130
    assert f"interrupted; {target} was created and may be incomplete" in err


def test_ctrl_c_before_anything_exists_names_no_file(tmp_path, capsys, monkeypatch):
    from uiprobe import htmlfile

    folder = full_run(tmp_path)
    out_dir = _html_run(tmp_path)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(htmlfile, "open", interrupted, raising=False)
    code, _, err = run_tree(capsys, str(folder.path), "--html", str(out_dir / "o.html"))
    assert code == 130
    assert "was created" not in err


@pytest.mark.parametrize("where", ["run/out.html", "run/screens/out.html"])
def test_an_output_inside_the_run_folder_is_refused(tmp_path, capsys, where):
    folder = full_run(tmp_path)
    target = tmp_path / where
    before = sorted(p.name for p in folder.path.rglob("*"))
    code, out, err = run_tree(capsys, str(folder.path), "--html", str(target))
    assert code == 2
    assert out == ""
    assert "inside the run folder" in err
    assert sorted(p.name for p in folder.path.rglob("*")) == before


def test_a_run_folder_given_by_a_link_still_counts_as_the_same_folder(tmp_path, capsys):
    folder = full_run(tmp_path)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(folder.path, target_is_directory=True)
    except OSError:
        pytest.skip("links cannot be made here")
    target = folder.path / "out.html"
    code, _, err = run_tree(capsys, str(alias), "--html", str(target))
    assert code == 2
    assert "inside the run folder" in err
    assert not target.exists()


def test_the_page_shows_kind_labels_in_place_of_the_dot_with_legend_lines(
    tmp_path, capsys
):
    folder = _skip_run(tmp_path, ("disabled", "toggle"), ("deny_word",))
    out_dir = _html_run(tmp_path)
    text, page = page_of(out_dir, capsys, folder)
    assert (
        '<span class="line unpressed"><span class="name">Row0</span>'
        " [toggle, disabled]</span>" in text
    )
    assert (
        '<span class="line unpressed"><span class="name">Row1</span> ·</span>' in text
    )
    lines = [ln for ln in page.footer.splitlines() if ln.strip()]
    assert lines == [LEGEND, LEGEND_TOGGLE, LEGEND_DISABLED]


def test_a_kind_label_in_a_popup_row_is_in_the_page_and_not_in_the_text_legend(
    tmp_path, capsys
):
    folder, _ = _popup_run(tmp_path, cancel_reasons=("navigate_up",))
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    assert (
        '<span class="line unpressed"><span class="name">Cancel</span> [back]</span>'
        in text
    )
    fold = text[text.index(FOLD_POPUP) :]
    assert "Cancel</span> [back]" in fold
    footer = [ln for ln in page.footer.splitlines() if ln.strip()]
    assert LEGEND_BACK in footer
    _, out, _ = run_tree(capsys, str(folder.path))
    assert "[back]" not in out
    assert LEGEND_BACK not in out.splitlines()


def _same_run(tmp_path, *results):
    from tests.test_tree import _same_row_run

    return _same_row_run(tmp_path, *results)


def test_the_html_has_the_label_and_no_fold_line_for_a_no_change_row(tmp_path, capsys):
    (tmp_path / "r").mkdir()
    folder = _same_run(tmp_path / "r", "same")
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    assert '<span class="name">Same</span> [no change]</span>' in text
    assert "result of pressing Same" not in text
    assert FOLD_OTHER not in text
    lines = [ln for ln in page.footer.splitlines() if ln.strip()]
    assert lines == [LEGEND, LEGEND_NO_CHANGE]


def test_the_html_keeps_the_fold_line_and_the_count_for_mixed_results(tmp_path, capsys):
    (tmp_path / "r").mkdir()
    folder = _same_run(tmp_path / "r", "same", "about")
    out_dir = tmp_path / "w"
    out_dir.mkdir()
    text, page = page_of(out_dir, capsys, folder)
    assert "[no change]" not in text
    assert ">result of pressing Same: → (same screen)<" in text
    lines = [ln for ln in page.footer.splitlines() if ln.strip()]
    assert lines[-1].startswith(HTML_HEAD + "rows that stayed on the same screen: 1")
