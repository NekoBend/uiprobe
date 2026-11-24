"""Tests for the screen model and the uiautomator XML parser."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from uiprobe._json import dumps
from uiprobe.errors import (
    DumpError,
    ElementNotFoundError,
    ElementNotVisibleError,
    MultipleElementsError,
)
from uiprobe.model import (
    MAX_DEPTH,
    MIN_VISIBLE_PX,
    Bounds,
    Element,
    Screen,
    parse_uiautomator_dump,
    parse_uiautomator_xml,
    shorten_activity,
)

DATA = Path(__file__).parent / "data"
BIDI = "".join(chr(code) for code in (0x202E, 0x2066, 0x2028))
CAPTURED = datetime(2026, 11, 15, 18, 30, tzinfo=UTC)


def make_screen(
    elements: list[Element],
    *,
    activity: str | None = ".LoginActivity",
    api_level: int | None = 36,
) -> Screen:
    return Screen(
        serial="emulator-5554",
        captured_at=CAPTURED,
        package="com.example.shop",
        activity=activity,
        api_level=api_level,
        display=(1080, 2400),
        elements=tuple(elements),
    )


def make_element(element_id: str = "e1", **kwargs) -> Element:
    kwargs.setdefault("parent", None)
    kwargs.setdefault("class_name", "android.widget.TextView")
    kwargs.setdefault("bounds", Bounds(0, 0, 10, 10))
    return Element(id=element_id, **kwargs)


@pytest.fixture
def login_elements() -> list[Element]:
    return parse_uiautomator_xml((DATA / "dump_login.xml").read_bytes())


# --- Bounds ---


def test_bounds_parse_and_properties():
    bounds = Bounds.parse("[10,20][110,320]")
    assert bounds == Bounds(10, 20, 110, 320)
    assert (bounds.width, bounds.height, bounds.center) == (100, 300, (60, 170))
    assert bounds.to_list() == [10, 20, 110, 320]


@pytest.mark.parametrize("text", ["", "[1,2]", "[a,b][c,d]", "[0,0][1,1]x", "0,0,1,1"])
def test_bounds_parse_rejects_malformed(text):
    with pytest.raises(DumpError):
        Bounds.parse(text)


# --- XML parsing ---


def test_parse_login_dump(login_elements):
    assert [e.id for e in login_elements] == ["e1", "e2", "e3", "e4", "e5"]
    assert [e.parent for e in login_elements] == [None, "e1", "e1", "e1", "e4"]
    email = login_elements[2]
    assert email.class_name == "android.widget.EditText"
    assert email.resource_id == "com.example.shop:id/email"
    assert email.hint == "メールアドレス"
    assert email.package == "com.example.shop"
    assert email.flags == ("clickable", "focusable")
    assert login_elements[1].text == "ようこそ"
    assert login_elements[3].flags == ("clickable", "disabled")
    assert login_elements[4].bounds == Bounds(90, 1810, 150, 1870)


def test_parse_accepts_str_with_encoding_declaration():
    text = (DATA / "dump_login.xml").read_text(encoding="utf-8")
    assert len(parse_uiautomator_xml(text)) == 5


def test_parse_pre_order_with_deep_nesting():
    xml = (
        "<hierarchy><node class='a' bounds='[0,0][1,1]'>"
        "<node class='b' bounds='[0,0][1,1]'>"
        "<node class='c' bounds='[0,0][1,1]'/></node>"
        "<node class='d' bounds='[0,0][1,1]'/></node>"
        "<node class='e' bounds='[0,0][1,1]'/></hierarchy>"
    )
    elements = parse_uiautomator_xml(xml)
    assert [(e.id, e.class_name, e.parent) for e in elements] == [
        ("e1", "a", None),
        ("e2", "b", "e1"),
        ("e3", "c", "e2"),
        ("e4", "d", "e1"),
        ("e5", "e", None),
    ]


def nested(depth: int) -> str:
    node = "<node class='x' bounds='[0,0][1,1]'>"
    return "<hierarchy>" + node * depth + "</node>" * depth + "</hierarchy>"


def test_parse_accepts_the_deepest_allowed_nesting():
    # The hierarchy element counts as one level.
    assert len(parse_uiautomator_xml(nested(MAX_DEPTH - 1))) == MAX_DEPTH - 1


def test_parse_rejects_nesting_beyond_the_limit():
    with pytest.raises(DumpError, match="nested too deeply"):
        parse_uiautomator_xml(nested(MAX_DEPTH))


def test_parse_stops_at_the_depth_limit_before_reading_the_rest():
    # 100000 levels would be 3 MB of XML and a tree view of ~10^10 characters.
    with pytest.raises(DumpError, match="nested too deeply"):
        parse_uiautomator_xml(nested(100_000))


def test_parse_empty_hierarchy():
    assert parse_uiautomator_xml("<hierarchy rotation='0'/>") == []


def test_parse_flag_order_and_defaults():
    xml = (
        "<hierarchy><node bounds='[0,0][1,1]' password='true' selected='true' "
        "focused='true' focusable='true' scrollable='true' long-clickable='true' "
        "clickable='true' checked='true' checkable='true' enabled='false'/>"
        "</hierarchy>"
    )
    (element,) = parse_uiautomator_xml(xml)
    assert element.flags == (
        "checkable",
        "checked",
        "clickable",
        "long_clickable",
        "scrollable",
        "focusable",
        "focused",
        "selected",
        "password",
        "disabled",
    )
    assert element.class_name == ""
    assert element.text == ""


@pytest.mark.parametrize(
    "xml",
    [
        "",
        "not xml",
        "<hierarchy><node bounds='[0,0][1,1]'></hierarchy>",
        "<root/>",
        "<hierarchy><node class='a' bounds='oops'/></hierarchy>",
        "<hierarchy><node class='a'/></hierarchy>",
    ],
)
def test_parse_rejects_bad_documents(xml):
    with pytest.raises(DumpError):
        parse_uiautomator_xml(xml)


@pytest.mark.parametrize(
    "prefix",
    [
        "<!DOCTYPE hierarchy>",
        "<!doctype hierarchy [<!ENTITY x 'y'>]>",
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>',
        "<!DOCTYPE hierarchy SYSTEM 'file:///etc/passwd'>",
    ],
)
def test_parse_rejects_doctype_and_entity(prefix):
    with pytest.raises(DumpError, match="DOCTYPE"):
        parse_uiautomator_xml(prefix + "<hierarchy/>")


def test_parse_rejects_doctype_in_utf16_document():
    document = "<!DOCTYPE hierarchy><hierarchy/>".encode("utf-16")
    with pytest.raises(DumpError, match="DOCTYPE"):
        parse_uiautomator_xml(document)


def test_parse_node_limit(monkeypatch):
    # The limit counts every element, the document element included.
    monkeypatch.setattr("uiprobe.model.MAX_NODES", 4)
    xml = "<hierarchy>" + "<node class='x' bounds='[0,0][1,1]'/>" * 4 + "</hierarchy>"
    with pytest.raises(DumpError, match="too many"):
        parse_uiautomator_xml(xml)
    assert (
        len(
            parse_uiautomator_xml(
                xml.replace("<node class='x' bounds='[0,0][1,1]'/>", "", 1)
            )
        )
        == 3
    )


def test_parse_stops_at_the_node_limit_before_reading_the_rest(monkeypatch):
    # The garbage after the nodes would be a well-formedness error if the whole
    # document were parsed first; the limit must fire as soon as it is passed.
    monkeypatch.setattr("uiprobe.model.MAX_NODES", 3)
    monkeypatch.setattr("uiprobe.model._FEED_CHUNK", 16)
    xml = "<hierarchy>" + "<node class='x' bounds='[0,0][1,1]'/>" * 6 + "<<<oops"
    with pytest.raises(DumpError, match="too many"):
        parse_uiautomator_xml(xml)


def test_parse_counts_nodes_of_every_nesting_level(monkeypatch):
    monkeypatch.setattr("uiprobe.model.MAX_NODES", 3)
    with pytest.raises(DumpError, match="too many"):
        parse_uiautomator_xml(nested(3))
    assert len(parse_uiautomator_xml(nested(2))) == 2


def test_parse_accepts_a_document_split_across_many_chunks(monkeypatch):
    monkeypatch.setattr("uiprobe.model._FEED_CHUNK", 7)
    text = (DATA / "dump_login.xml").read_text(encoding="utf-8")
    assert len(parse_uiautomator_xml(text)) == 5
    assert len(parse_uiautomator_xml(text.encode("utf-8"))) == 5


@pytest.mark.parametrize(
    "document",
    [
        b'<?xml version="1.0" encoding="bogus"?><hierarchy/>',
        b'<?xml version="1.0" encoding="shift_jis"?><hierarchy/>',
        b'<?xml version="1.0" encoding="utf-7"?><hierarchy/>',
        b'<hierarchy><node bounds="[' + b"9" * 5000 + b',0][1,1]"/></hierarchy>',
        b'<hierarchy><node bounds="[-' + b"0" * 5000 + b',0][1,1]"/></hierarchy>',
    ],
    ids=["unknown-encoding", "shift-jis", "utf-7", "huge-number", "huge-zeros"],
)
def test_malformed_untrusted_input_is_always_a_dump_error(document):
    with pytest.raises(DumpError):
        parse_uiautomator_xml(document)


@pytest.mark.parametrize(
    "text",
    ["[" + "9" * 5000 + ",0][1,1]", "[1234567890,0][1,1]", "[\u0661,0][1,1]"],
    ids=["huge", "ten-digits", "non-ascii-digit"],
)
def test_bounds_parse_rejects_oversized_and_non_ascii_numbers(text):
    with pytest.raises(DumpError):
        Bounds.parse(text)


def test_bounds_parse_accepts_nine_digits_and_negative_numbers():
    assert Bounds.parse("[-999999999,0][999999999,1]") == Bounds(
        -999999999, 0, 999999999, 1
    )


# --- Element and Screen JSON ---


def test_element_to_dict_omits_empty_values():
    top = make_element("e1", class_name="android.widget.FrameLayout")
    assert top.to_dict() == {
        "id": "e1",
        "class": "android.widget.FrameLayout",
        "bounds": [0, 0, 10, 10],
    }
    child = make_element(
        "e2", parent="e1", text="x", resource_id="a:id/b", flags=("checked",)
    )
    assert child.to_dict() == {
        "id": "e2",
        "parent": "e1",
        "class": "android.widget.TextView",
        "text": "x",
        "resource_id": "a:id/b",
        "bounds": [0, 0, 10, 10],
        "flags": ["checked"],
    }
    assert "package" not in child.to_dict()


def test_element_to_dict_writes_every_optional_key():
    element = make_element(
        "e2",
        parent="e1",
        text="t",
        resource_id="p:id/r",
        content_desc="d",
        hint="h",
        package="p",
        flags=("clickable",),
    )
    assert element.to_dict() == {
        "id": "e2",
        "parent": "e1",
        "class": "android.widget.TextView",
        "text": "t",
        "resource_id": "p:id/r",
        "content_desc": "d",
        "hint": "h",
        "bounds": [0, 0, 10, 10],
        "flags": ["clickable"],
    }
    assert list(element.to_dict()) == [
        "id",
        "parent",
        "class",
        "text",
        "resource_id",
        "content_desc",
        "hint",
        "bounds",
        "flags",
    ]


def test_login_dump_elements_match_the_reference_example(login_elements):
    data = [element.to_dict() for element in login_elements]
    assert data[2] == {
        "id": "e3",
        "parent": "e1",
        "class": "android.widget.EditText",
        "resource_id": "com.example.shop:id/email",
        "hint": "メールアドレス",
        "bounds": [80, 600, 1000, 720],
        "flags": ["clickable", "focusable"],
    }
    assert data[4] == {
        "id": "e5",
        "parent": "e4",
        "class": "android.widget.ImageView",
        "resource_id": "com.example.shop:id/icon",
        "content_desc": "lock",
        "bounds": [90, 1810, 150, 1870],
    }


def test_element_to_dict_keeps_zero_bounds():
    data = make_element(bounds=Bounds(0, 0, 0, 0)).to_dict()
    assert data["bounds"] == [0, 0, 0, 0]


def test_screen_to_dict_shape(login_elements):
    data = make_screen(login_elements).to_dict()
    assert list(data) == [
        "schema",
        "captured_at",
        "device",
        "app",
        "screen_id",
        "display",
        "elements",
    ]
    assert data["schema"] == "uiprobe.screen/1"
    assert data["captured_at"] == "2026-11-15T18:30:00+00:00"
    assert data["device"] == {"serial": "emulator-5554", "api_level": 36}
    assert data["app"] == {"package": "com.example.shop", "activity": ".LoginActivity"}
    assert data["display"] == {"width": 1080, "height": 2400}
    assert isinstance(data["elements"], list)
    assert len(data["elements"]) == 5


def test_screen_to_dict_omits_unknown_values_and_keeps_empty_elements():
    data = make_screen([], activity=None, api_level=None).to_dict()
    assert data["device"] == {"serial": "emulator-5554"}
    assert data["app"] == {"package": "com.example.shop"}
    assert data["elements"] == []


def test_screen_to_json_keeps_non_ascii_and_indent(login_elements):
    screen = make_screen(login_elements)
    compact = screen.to_json()
    assert "ようこそ" in compact
    assert "\n" not in compact
    assert json.loads(compact) == screen.to_dict()
    assert "\n  " in screen.to_json(indent=2)


def test_captured_at_has_offset_and_seconds_precision():
    tokyo = datetime.fromisoformat("2026-11-15T18:30:00.123456+09:00")
    screen = Screen("s", tokyo, "p", None, None, (1, 1), ())
    assert screen.to_dict()["captured_at"] == "2026-11-15T18:30:00+09:00"


# --- screen_id ---


def test_screen_id_format_and_stability(login_elements):
    screen = make_screen(login_elements)
    assert screen.screen_id.startswith("s-")
    assert len(screen.screen_id) == 14
    changed = [
        Element(
            id=e.id,
            parent=e.parent,
            class_name=e.class_name,
            bounds=Bounds(1, 2, 3, 4),
            text="different " + e.text,
            resource_id=e.resource_id,
            flags=(),
        )
        for e in login_elements
    ]
    assert make_screen(changed).screen_id == screen.screen_id


def test_screen_id_golden_values():
    # Ids name stored screens (screens/<id>.json): the hash input must not change.
    element = make_element(class_name="c", resource_id="r")
    screen = Screen("s", CAPTURED, "p", ".A", None, (1, 1), (element,))
    assert screen.screen_id == "s-935cffdd4ec9"
    nameless = Screen("s", CAPTURED, "p", None, None, (1, 1), (element,))
    assert nameless.screen_id == "s-efc1c8c53db1"
    twice = Screen("s", CAPTURED, "p", ".A", None, (1, 1), (element, element))
    assert twice.screen_id == "s-296492f58fef"


def test_screen_id_depends_on_structure(login_elements):
    base = make_screen(login_elements).screen_id
    assert make_screen(login_elements[:-1]).screen_id != base
    assert make_screen(login_elements, activity=".Other").screen_id != base
    assert make_screen(login_elements, activity=None).screen_id != base
    renamed = [*login_elements[:-1], make_element("e5", resource_id="x:id/y")]
    assert make_screen(renamed).screen_id != base


# --- tree ---


def test_tree_matches_the_reference_example():
    elements = [
        make_element("e1", class_name="android.widget.FrameLayout"),
        make_element("e2", parent="e1", text="ようこそ"),
        make_element(
            "e3",
            parent="e1",
            class_name="android.widget.EditText",
            resource_id="com.example.shop:id/email",
            hint="メールアドレス",
            flags=("clickable", "focusable"),
        ),
        make_element(
            "e4",
            parent="e1",
            class_name="android.widget.Button",
            text="ログイン",
            resource_id="com.example.shop:id/login",
            flags=("clickable",),
        ),
    ]
    screen = make_screen(elements)
    assert screen.to_tree().splitlines() == [
        f"com.example.shop/.LoginActivity  ({screen.screen_id})",
        "e1 FrameLayout",
        '├ e2 TextView "ようこそ"',
        '├ e3 EditText id=email hint="メールアドレス" [tap]',
        '└ e4 Button "ログイン" id=login [tap]',
    ]


def test_tree_nesting_and_tags(login_elements):
    lines = make_screen(login_elements).to_tree().splitlines()
    assert lines[1:] == [
        "e1 FrameLayout",
        '├ e2 TextView "ようこそ"',
        '├ e3 EditText id=email hint="メールアドレス" [tap]',
        '└ e4 Button "ログイン" id=login [tap] [disabled]',
        '  └ e5 ImageView id=icon desc="lock"',
    ]


def test_tree_continuation_bars_and_multiple_top_level():
    elements = [
        make_element("e1"),
        make_element("e2", parent="e1"),
        make_element("e3", parent="e2"),
        make_element("e4", parent="e1"),
        make_element("e5"),
    ]
    lines = make_screen(elements, activity=None).to_tree().splitlines()
    assert lines[0].startswith("com.example.shop  (s-")
    assert lines[1:] == [
        "e1 TextView",
        "├ e2 TextView",
        "│ └ e3 TextView",
        "└ e4 TextView",
        "e5 TextView",
    ]


def test_tree_all_tags():
    element = make_element(
        flags=(
            "clickable",
            "long_clickable",
            "scrollable",
            "checked",
            "password",
            "disabled",
        )
    )
    line = make_screen([element]).to_tree().splitlines()[1]
    assert line.endswith("[tap] [long] [scroll] [checked] [password] [disabled]")


def test_tree_id_without_separator_is_shown_whole():
    line = make_screen([make_element(resource_id="plain")]).to_tree().splitlines()[1]
    assert line == "e1 TextView id=plain"


def test_tree_escapes_control_and_bidi_characters():
    hostile = 'a\nb\rc\x1b[2Jd\x85e\x7ff\u202eg\u2066h\u2028i"j\\k'
    element = make_element(
        text=hostile,
        content_desc=hostile,
        hint=hostile,
        resource_id="p:id/" + hostile,
        class_name="x.\x1b" + "Evil",
    )
    screen = Screen("s", CAPTURED, "pk\x1b", "ac\ntivity", None, (1, 1), (element,))
    tree = screen.to_tree()
    assert len(tree.splitlines()) == 2
    for char in tree:
        assert ord(char) >= 0x20 or char == "\n"
        assert not 0x7F <= ord(char) <= 0x9F
        assert char not in BIDI
    assert "\\u001b" in tree
    assert "\\n" in tree
    assert '\\"j\\\\k' in tree
    assert "\\u202e" in tree


def tree_line(**kwargs) -> str:
    return make_screen([make_element(**kwargs)]).to_tree().splitlines()[1]


@pytest.mark.parametrize(
    "char",
    ["\u200b", "\u2060", "\ufeff", "\u00ad", "\U000e0041", "\ue000", "\u0378"],
    ids=[
        "zwsp",
        "word-joiner",
        "bom",
        "soft-hyphen",
        "tag-char",
        "private",
        "unassigned",
    ],
)
def test_tree_escapes_invisible_characters(char):
    tree = make_screen(
        [make_element(text="lo" + char + "gin", resource_id="p:id/lo" + char + "gin")]
    ).to_tree()
    assert char not in tree
    assert "\\u" in tree.lower() or "\\U" in tree


def test_tree_keeps_ordinary_text_readable():
    line = tree_line(text="ログイン 😀 Café")
    assert line == 'e1 TextView "ログイン 😀 Café"'


def test_tree_escape_uses_long_form_outside_the_basic_plane():
    assert tree_line(text="\U000e0041") == 'e1 TextView "\\U000e0041"'


def test_tree_resource_id_cannot_forge_tags():
    line = tree_line(resource_id="com.bank:id/pay [tap] [password]")
    assert line == 'e1 TextView id="pay [tap] [password]"'


def test_tree_class_name_cannot_forge_tags():
    line = tree_line(class_name='android.widget.Button "Cancel" [tap]')
    assert line == 'e1 "Button \\"Cancel\\" [tap]"'


def test_tree_header_cannot_forge_a_screen_id():
    screen = Screen(
        "s", CAPTURED, "com.bank  (s-000000000000)", "a b", None, (1, 1), ()
    )
    header = screen.to_tree().splitlines()[0]
    assert header == f'"com.bank  (s-000000000000)"/"a b"  ({screen.screen_id})'


def test_tree_plain_names_stay_unquoted():
    line = tree_line(class_name="a.b$Inner", resource_id="p:id/my_id.2")
    assert line == "e1 b$Inner id=my_id.2"


def test_tree_with_no_package_keeps_its_old_shape():
    screen = Screen("s", CAPTURED, "", None, None, (1, 1), ())
    assert screen.to_tree() == f"  ({screen.screen_id})"


# --- find / find_one ---


@pytest.fixture
def searchable() -> Screen:
    return make_screen(
        [
            make_element(
                "e1", text="OK", resource_id="a:id/ok", content_desc="confirm"
            ),
            make_element(
                "e2",
                parent="e1",
                text="OK",
                resource_id="a:id/other",
                bounds=Bounds(0, 10, 10, 20),
            ),
            make_element(
                "e3",
                parent="e1",
                text="Cancel",
                resource_id="b:id/ok",
                bounds=Bounds(0, 20, 10, 30),
            ),
        ]
    )


def test_find_exact_match(searchable):
    assert [e.id for e in searchable.find(text="OK")] == ["e1", "e2"]
    assert searchable.find(text="ok") == []
    assert searchable.find(text="O") == []


def test_find_resource_id_full_or_short(searchable):
    assert [e.id for e in searchable.find(resource_id="ok")] == ["e1", "e3"]
    assert [e.id for e in searchable.find(resource_id="a:id/ok")] == ["e1"]


def test_find_combines_criteria(searchable):
    assert [e.id for e in searchable.find(text="OK", resource_id="ok")] == ["e1"]
    assert [e.id for e in searchable.find(desc="confirm", text="OK")] == ["e1"]
    assert searchable.find(desc="confirm", text="Cancel") == []


def test_find_requires_a_criterion(searchable):
    with pytest.raises(ValueError, match="at least one"):
        searchable.find()
    with pytest.raises(ValueError, match="at least one"):
        searchable.find_one()


def test_find_one_returns_the_match(searchable):
    assert searchable.find_one(text="Cancel").id == "e3"


def test_find_one_not_found(searchable):
    with pytest.raises(ElementNotFoundError) as info:
        searchable.find_one(text="missing", desc="x")
    assert info.value.details == {
        "criteria": {"text": "missing", "content_desc": "x"},
        "total": 0,
        "candidates": [],
    }


def test_find_one_multiple_lists_candidates(searchable):
    with pytest.raises(MultipleElementsError) as info:
        searchable.find_one(text="OK")
    details = info.value.details
    assert details["criteria"] == {"text": "OK"}
    assert details["total"] == 2
    assert details["candidates"] == [
        {
            "id": "e1",
            "class": "android.widget.TextView",
            "text": "OK",
            "resource_id": "a:id/ok",
            "content_desc": "confirm",
            "bounds": [0, 0, 10, 10],
            "center": [5, 5],
        },
        {
            "id": "e2",
            "parent": "e1",
            "class": "android.widget.TextView",
            "text": "OK",
            "resource_id": "a:id/other",
            "bounds": [0, 10, 10, 20],
            "center": [5, 15],
        },
    ]


def test_find_one_candidates_are_capped_at_twenty():
    screen = make_screen([make_element(f"e{n}", text="x") for n in range(1, 31)])
    with pytest.raises(MultipleElementsError) as info:
        screen.find_one(text="x")
    candidates = info.value.details["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 20
    assert info.value.details["total"] == 30


# --- selection rules: visibility, text, candidates ---

NBSP = "\u00a0"


def wide(text: str) -> str:
    """Return `text` in full-width letters (U+FF21 and up)."""
    return "".join(chr(ord(char) + 0xFEE0) for char in text)


def boxed(element_id: str, bounds: tuple[int, int, int, int], **kwargs) -> Element:
    return make_element(element_id, bounds=Bounds(*bounds), **kwargs)


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ((10, 20, 30, 60), (10, 20, 30, 60)),
        ((0, 0, 1080, 2400), (0, 0, 1080, 2400)),
        ((-50, -50, 40, 20), (0, 0, 40, 20)),
        ((1000, 2300, 1200, 2500), (1000, 2300, 1080, 2400)),
        ((-10, -10, 2000, 3000), (0, 0, 1080, 2400)),
        ((0, 0, 8, 8), (0, 0, 8, 8)),
        ((-50, -50, 8, 8), (0, 0, 8, 8)),
        ((1072, 2392, 1200, 2500), (1072, 2392, 1080, 2400)),
    ],
)
def test_visible_bounds_is_the_intersection_with_the_display(bounds, expected):
    screen = make_screen([])
    visible = screen.visible_bounds(boxed("e1", bounds))
    assert visible is not None
    assert visible.to_list() == list(expected)


@pytest.mark.parametrize(
    "bounds",
    [
        (80, 623, 203, 616),  # inverted top and bottom, seen on API 36
        (203, 600, 80, 700),  # inverted left and right
        (100, 100, 100, 200),  # zero width
        (100, 100, 200, 100),  # zero height
        (0, 0, 0, 0),
        (1080, 0, 1200, 100),  # starts at the right edge
        (0, 2400, 100, 2500),  # starts at the bottom edge
        (-100, 0, 0, 100),  # ends at the left edge
        (0, -100, 100, 0),  # ends at the top edge
        (2000, 3000, 2100, 3100),  # far outside
        (-300, -300, -200, -200),
        (-100, -100, -200, -200),  # inverted and outside
    ],
)
def test_elements_with_no_visible_part_are_not_visible(bounds):
    screen = make_screen([boxed("e1", bounds, text="x", resource_id="a:id/x")])
    assert screen.visible_bounds(screen.elements[0]) is None
    assert screen.find(text="x") == []
    assert screen.find(resource_id="x") == []
    with pytest.raises(ElementNotFoundError):
        screen.find_one(text="x")


def test_an_eight_pixel_edge_is_still_visible():
    # 8 pixels of the element show at the right and bottom display edges.
    screen = make_screen([boxed("e1", (1072, 2392, 1500, 2500), text="x")])
    assert [e.id for e in screen.find(text="x")] == ["e1"]


def test_a_one_pixel_edge_is_not_visible():
    screen = make_screen([boxed("e1", (1079, 2399, 1500, 2500), text="x")])
    assert screen.find(text="x") == []


def test_a_zero_sized_display_shows_nothing():
    screen = Screen("s", CAPTURED, "p", None, None, (0, 0), (make_element(text="x"),))
    assert screen.find(text="x") == []


def test_find_skips_invisible_elements_and_keeps_visible_duplicates():
    screen = make_screen(
        [
            boxed("e1", (80, 623, 203, 616), text="Go"),
            boxed("e2", (0, 3000, 100, 3100), text="Go"),
            boxed("e3", (0, 0, 100, 100), text="Go"),
        ]
    )
    assert screen.find_one(text="Go").id == "e3"


@pytest.mark.parametrize(
    ("element_text", "wanted", "matches"),
    [
        ("\nLogin", "Login", True),
        ("Login", "\n Login\t", True),
        (" \r\n Login \n", "Login", True),
        (f"{NBSP}Login", "Login", True),  # str.strip removes U+00A0
        ("Log in", "Login", False),
        ("Lo\ngin", "Login", False),
        ("login", "Login", False),
        ("Login", "Logi", False),
        ("Login now", "Login", False),
        ("", "", True),
        ("  ", "", True),
    ],
)
def test_text_is_compared_after_stripping_both_sides(element_text, wanted, matches):
    screen = make_screen([make_element(text=element_text)])
    assert bool(screen.find(text=wanted)) is matches


def test_resource_id_and_desc_are_not_stripped():
    screen = make_screen(
        [make_element(resource_id="a:id/ok", content_desc="confirm")],
    )
    assert screen.find(desc=" confirm") == []
    assert screen.find(desc="confirm\n") == []
    assert screen.find(resource_id="ok ") == []
    assert screen.find(resource_id=" ok") == []
    assert len(screen.find(desc="confirm", resource_id="ok")) == 1


def test_and_needs_every_given_criterion_to_match():
    screen = make_screen(
        [
            boxed("e1", (0, 0, 10, 10), text="A", resource_id="p:id/one"),
            boxed("e2", (0, 0, 10, 10), text="A", resource_id="p:id/two"),
            boxed("e3", (0, 0, 10, 10), text="B", resource_id="p:id/two"),
        ]
    )
    assert [e.id for e in screen.find(text="A")] == ["e1", "e2"]
    assert [e.id for e in screen.find(resource_id="two")] == ["e2", "e3"]
    assert [e.id for e in screen.find(text="A", resource_id="two")] == ["e2"]
    assert screen.find(text="B", resource_id="one") == []


def test_the_tap_point_is_the_center_of_the_visible_part():
    screen = make_screen(
        [
            boxed("e1", (10, 200, 21, 221), text="odd"),  # 15, 210 after the floor
            boxed("e2", (-100, -100, 100, 50), text="top-left"),  # visible 0,0,100,50
            boxed("e3", (1000, 2000, 5000, 9000), text="corner"),  # 1000,2000,1080,2400
        ]
    )
    assert screen._element_dict(screen.find_one(text="odd"))["center"] == [15, 210]
    assert screen._element_dict(screen.find_one(text="top-left"))["center"] == [50, 25]
    assert screen._element_dict(screen.find_one(text="corner"))["center"] == [
        1040,
        2200,
    ]


def test_the_center_is_floored_for_odd_sizes():
    screen = make_screen([boxed("e1", (0, 0, 9, 9), text="x")])
    assert screen._element_dict(screen.elements[0])["center"] == [4, 4]


def test_element_dict_has_no_center_without_a_visible_part():
    screen = make_screen([boxed("e1", (5, 5, 5, 9))])
    data = screen._element_dict(screen.elements[0])
    assert "center" not in data
    assert data["bounds"] == [5, 5, 5, 9]


def test_element_dict_omits_empty_values_and_adds_center_last():
    screen = make_screen([boxed("e1", (0, 0, 10, 20), flags=("clickable",))])
    assert screen._element_dict(screen.elements[0]) == {
        "id": "e1",
        "class": "android.widget.TextView",
        "bounds": [0, 0, 10, 20],
        "flags": ["clickable"],
        "center": [5, 10],
    }
    assert list(screen._element_dict(screen.elements[0]))[-1] == "center"


def test_describe_appends_the_center_to_the_tree_line():
    screen = make_screen([boxed("e1", (0, 0, 10, 20), text="OK", flags=("clickable",))])
    assert (
        screen._describe_line(screen.elements[0])
        == 'e1 TextView "OK" [tap] center=5,10'
    )
    hidden = make_screen([boxed("e1", (0, 0, 0, 0), text="OK")])
    assert hidden._describe_line(hidden.elements[0]) == 'e1 TextView "OK"'


@pytest.fixture
def shop() -> Screen:
    return make_screen(
        [
            boxed("e1", (0, 0, 1080, 2400), class_name="android.widget.FrameLayout"),
            boxed(
                "e2",
                (80, 100, 400, 200),
                parent="e1",
                text="Sign-in",
                resource_id="com.shop:id/signin",
                flags=("clickable",),
            ),
            boxed(
                "e3",
                (80, 300, 400, 400),
                parent="e1",
                text="Create account",
                content_desc="Register",
                flags=("clickable",),
            ),
            boxed("e4", (80, 500, 400, 600), parent="e1", text="Terms and policy"),
            boxed(
                "e5",
                (80, 700, 400, 800),
                parent="e1",
                text="Hidden",
                flags=("clickable",),
            ),
        ]
    )


def candidate_ids(error) -> list[object]:
    candidates: Any = error.details["candidates"]
    return [c["id"] for c in candidates]


@pytest.mark.parametrize(
    "criteria",
    [
        {"text": "SIGN"},  # case
        {"text": wide("SIGN")},  # full-width letters, NFKC
        {"resource_id": "SignIn"},
        {"resource_id": wide("SIGNIN")},
        {"desc": "regis"},
        {"text": wide("create")},
        {"text": " sign "},  # stripped before comparing
    ],
)
def test_not_found_suggests_partial_matches_after_nfkc_and_casefold(shop, criteria):
    with pytest.raises(ElementNotFoundError) as info:
        shop.find_one(**criteria)
    assert candidate_ids(info.value) in (["e2"], ["e3"])
    assert info.value.details["total"] == 1


def test_not_found_partial_match_is_per_criterion_on_its_own_field(shop):
    with pytest.raises(ElementNotFoundError) as info:
        shop.find_one(text="account", resource_id="nothing")
    assert candidate_ids(info.value) == ["e3"]
    with pytest.raises(ElementNotFoundError) as info:
        shop.find_one(text="account", desc="regis", resource_id="nothing")
    assert candidate_ids(info.value) == ["e3"]
    with pytest.raises(ElementNotFoundError) as info:
        # "account" is in a text, not in any resource id or description.
        shop.find_one(resource_id="account", desc="account")
    assert candidate_ids(info.value) == ["e2", "e3", "e5"]  # the clickable ones


def test_not_found_is_and_with_an_extra_criterion_that_misses(shop):
    with pytest.raises(ElementNotFoundError) as info:
        shop.find_one(text="Sign-in", desc="nope")
    # Nothing matches both, so the elements that partly match one are shown.
    assert candidate_ids(info.value) == ["e2"]


def test_u2011_is_not_the_ascii_hyphen_and_nfkc_maps_it_to_u2010():
    # U+2011 (non-breaking hyphen) is not the ASCII hyphen-minus: it never
    # matches exactly. NFKC turns it into U+2010 (hyphen), not into "-".
    screen = make_screen(
        [
            boxed("e1", (0, 0, 100, 50), text="Sign\u2011in"),
            boxed("e2", (0, 100, 100, 150), text="Other", flags=("clickable",)),
        ]
    )
    assert screen.find(text="Sign-in") == []
    assert screen.find(text="Sign\u2010in") == []
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="Sign\u2010in")
    assert candidate_ids(info.value) == ["e1"]
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="Sign-in")
    # NFKC does not fold U+2011 to the ASCII hyphen, so there is no partial
    # match either, and the clickable elements are suggested instead.
    assert candidate_ids(info.value) == ["e2"]


def test_not_found_falls_back_to_the_clickable_elements(shop):
    with pytest.raises(ElementNotFoundError) as info:
        shop.find_one(text="zzz")
    assert candidate_ids(info.value) == ["e2", "e3", "e5"]
    assert info.value.details["total"] == 3


def test_not_found_candidates_leave_out_invisible_elements():
    screen = make_screen(
        [
            boxed("e1", (0, 0, 10, 10), text="Item", flags=("clickable",)),
            boxed("e2", (0, 5000, 10, 5010), text="Item two", flags=("clickable",)),
            boxed("e3", (80, 623, 203, 616), text="Item three"),
        ]
    )
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="item")
    assert candidate_ids(info.value) == ["e1"]
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="zzz")
    assert candidate_ids(info.value) == ["e1"]


def test_not_found_with_nothing_to_suggest_has_an_empty_list():
    screen = make_screen([make_element(text="a")])
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="zzz")
    assert info.value.details["candidates"] == []
    assert info.value.details["total"] == 0


def test_not_found_ignores_an_empty_needle_for_partial_matching():
    screen = make_screen(
        [
            make_element("e1", text="a"),
            make_element("e2", text="b", flags=("clickable",)),
        ]
    )
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="zzz", desc="")
    # "" is part of every string; it must not make everything a candidate.
    assert candidate_ids(info.value) == ["e2"]


def test_not_found_candidates_are_capped_with_the_full_total():
    screen = make_screen(
        [make_element(f"e{n}", text=f"Item {n}") for n in range(1, 31)]
    )
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="item")
    assert len(candidate_ids(info.value)) == 20
    assert info.value.details["total"] == 30


def test_not_found_candidates_have_the_element_shape_plus_center(shop):
    with pytest.raises(ElementNotFoundError) as info:
        shop.find_one(resource_id="signi")
    assert info.value.details["candidates"] == [
        {
            "id": "e2",
            "parent": "e1",
            "class": "android.widget.TextView",
            "text": "Sign-in",
            "resource_id": "com.shop:id/signin",
            "bounds": [80, 100, 400, 200],
            "flags": ["clickable"],
            "center": [240, 150],
        }
    ]


def test_multiple_candidates_use_the_visible_center_and_exclude_hidden():
    screen = make_screen(
        [
            boxed("e1", (-100, 0, 100, 100), text="x"),
            boxed("e2", (0, 0, 0, 100), text="x"),
            boxed("e3", (1000, 2300, 2000, 3000), text="x"),
        ]
    )
    with pytest.raises(MultipleElementsError) as info:
        screen.find_one(text="x")
    details = info.value.details
    assert details["total"] == 2
    candidates: Any = details["candidates"]
    assert [c["center"] for c in candidates] == [[50, 50], [1040, 2350]]


def test_find_one_error_candidates_are_json_serializable(shop):
    with pytest.raises(ElementNotFoundError) as info:
        shop.find_one(text="zzz")
    assert json.loads(dumps(info.value.to_dict()))["error"]["details"]["total"] == 3


def test_near_misses_requires_a_criterion(shop):
    with pytest.raises(ValueError, match="at least one"):
        shop._near_misses()


def test_screen_id_changes_with_the_number_of_elements():
    one = make_screen([make_element("e1", class_name="c", resource_id="r")])
    two = make_screen(
        [
            make_element("e1", class_name="c", resource_id="r"),
            make_element("e2", class_name="c", resource_id="r"),
        ]
    )
    assert one.screen_id != two.screen_id


def _settings_top(*, badge: bool) -> Screen:
    """The shape of a Samsung Settings top page with and without its "New"
    badge: a row whose badge frame (with a view inside) comes and goes."""
    row = [
        make_element("e1", class_name="android.widget.FrameLayout"),
        make_element(
            "e2",
            parent="e1",
            class_name="android.widget.LinearLayout",
            resource_id="com.android.settings:id/row",
        ),
        make_element(
            "e3",
            parent="e2",
            class_name="android.widget.TextView",
            resource_id="android:id/title",
        ),
    ]
    frame = [
        make_element(
            "e4",
            parent="e2",
            class_name="android.widget.LinearLayout",
            resource_id="com.android.settings:id/badge_frame",
        ),
        make_element("e5", parent="e4", class_name="android.view.View"),
    ]
    after = [
        make_element(
            "e6",
            parent="e1",
            class_name="android.widget.LinearLayout",
            resource_id="com.android.settings:id/row",
        ),
    ]
    return make_screen([*row, *(frame if badge else []), *after], activity=".Settings")


def test_a_badge_that_comes_and_goes_keeps_the_screen_id():
    assert _settings_top(badge=True).screen_id == _settings_top(badge=False).screen_id


@pytest.mark.parametrize("resource_id", ["x:id/BADGE", "x:id/new_badge", "badge"])
def test_any_resource_id_holding_badge_is_left_out(resource_id):
    plain = make_screen([make_element("e1", class_name="c", resource_id="r")])
    marked = make_screen(
        [
            make_element("e1", class_name="c", resource_id="r"),
            make_element("e2", class_name="d", resource_id=resource_id),
            make_element("e3", parent="e2", class_name="v"),
            make_element("e4", parent="e3", class_name="w"),
        ]
    )
    assert plain.screen_id == marked.screen_id


def test_elements_after_a_badge_still_count():
    with_sibling = make_screen(
        [
            make_element("e1", class_name="c", resource_id="r"),
            make_element("e2", class_name="d", resource_id="x:id/badge"),
            make_element("e3", class_name="v"),  # a sibling, not inside it
        ]
    )
    without = make_screen([make_element("e1", class_name="c", resource_id="r")])
    assert with_sibling.screen_id != without.screen_id


def test_other_differences_still_change_the_screen_id():
    base = _settings_top(badge=False)
    changed = make_screen(
        [*base.elements, make_element("e7", class_name="android.widget.Switch")],
        activity=".Settings",
    )
    other_id = make_screen(
        [
            *base.elements[:-1],
            make_element(
                "e6",
                parent="e1",
                class_name="android.widget.LinearLayout",
                resource_id="com.android.settings:id/other_row",
            ),
        ],
        activity=".Settings",
    )
    assert len({base.screen_id, changed.screen_id, other_id.screen_id}) == 3


# --- shorten_activity ---


@pytest.mark.parametrize(
    ("package", "activity", "expected"),
    [
        ("com.a", "com.a.Main", ".Main"),
        ("com.a", "com.a.ui.Main", ".ui.Main"),
        ("com.a", "com.b.Main", "com.b.Main"),
        ("com.a", "com.ab.Main", "com.ab.Main"),
        ("com.a", ".Main", ".Main"),
        ("", "x.Main", "x.Main"),
    ],
)
def test_shorten_activity(package, activity, expected):
    assert shorten_activity(package, activity) == expected


@pytest.mark.parametrize(
    "child",
    ["<x/>", "<ns:x xmlns:ns='urn:a'/>", "<node/>", "<x><y/></x>"],
)
def test_parse_counts_unknown_and_namespaced_elements(monkeypatch, child):
    monkeypatch.setattr("uiprobe.model.MAX_NODES", 5)
    weight = len(re.findall(r"<[^/]", child))
    count = 5 // weight + 1
    with pytest.raises(DumpError, match="too many"):
        parse_uiautomator_xml("<hierarchy>" + child * count + "</hierarchy>")


def test_parse_element_limit_includes_the_root_and_allows_the_boundary(monkeypatch):
    monkeypatch.setattr("uiprobe.model.MAX_NODES", 4)
    parse_uiautomator_xml("<hierarchy>" + "<x/>" * 3 + "</hierarchy>")
    with pytest.raises(DumpError, match="too many"):
        parse_uiautomator_xml("<hierarchy>" + "<x/>" * 4 + "</hierarchy>")


def test_parse_unknown_elements_do_not_become_elements():
    xml = "<hierarchy><x/><node class='a' bounds='[0,0][1,1]'/><y/></hierarchy>"
    assert len(parse_uiautomator_xml(xml)) == 1


@pytest.mark.parametrize(
    ("attribute", "expected"),
    [
        (' rotation="0"', 0),
        (' rotation="1"', 1),
        (' rotation="2"', 2),
        (' rotation="3"', 3),
        (' rotation="4"', 0),
        (' rotation="-1"', 0),
        (' rotation="01"', 0),
        (' rotation=""', 0),
        (' rotation="\u0661"', 0),
        ("", 0),
    ],
)
def test_parse_dump_reads_the_rotation(attribute: str, expected: int) -> None:
    dump = parse_uiautomator_dump(f"<hierarchy{attribute}/>")
    assert (dump.elements, dump.rotation) == ([], expected)


_CONTROL_SAMPLES = [
    "\x7f",
    "\x80",
    "\x9b",
    "\x9f",
    "\u061c",
    "\u200e",
    "\u200f",
    "\u2028",
    "\u2029",
    "\u202a",
    "\u202e",
    "\u2066",
    "\u2069",
    "\x1b",
    "\x00",
]


@pytest.mark.parametrize("char", _CONTROL_SAMPLES)
@pytest.mark.parametrize("compact", [True, False])
def test_screen_to_json_escapes_control_characters(char, compact):
    text = f"a{char}2Jb"
    screen = make_screen([make_element(text=text, resource_id=text)])
    out = screen.to_json() if compact else screen.to_json(indent=2)
    assert char not in out
    assert f"\\u{ord(char):04x}" in out
    assert json.loads(out) == screen.to_dict()


def test_json_helper_keeps_readable_text_and_round_trips():
    data = {"ja": "ようこそ", "mixed": "ログイン\x9b2J\u2028", "n": [1, None]}
    out = dumps(data)
    assert "ようこそ" in out
    assert "ログイン" in out
    assert json.loads(out) == data
    assert dumps(data, indent=1).startswith("{\n ")


def test_screen_json_from_a_hostile_dump_has_no_raw_controls():
    xml = b"""<hierarchy rotation="0"><node index="0" text="&#x9b;2J" class="c"
      package="p" bounds="[0,0][1,1]"/></hierarchy>"""
    parsed = parse_uiautomator_dump(xml)
    screen = make_screen(list(parsed.elements))
    out = screen.to_json()
    assert "\x9b" not in out
    assert "\\u009b" in out


_ESCAPED_RANGES = [
    (0x7F, 0x9F),
    (0x061C, 0x061C),
    (0x200E, 0x200F),
    (0x2028, 0x2029),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
]
_ESCAPED_CODE_POINTS = [
    code for low, high in _ESCAPED_RANGES for code in range(low, high + 1)
]
# Neighbours of the ranges, and text, that must stay as they are in JSON.
_PLAIN_CHARS = ["\u00a0", "\u00a1", "\u200d", "\u2030", "\u206a", "\u3042", "a", "~"]


@pytest.mark.parametrize("code", _ESCAPED_CODE_POINTS, ids=hex)
def test_json_helper_escapes_every_unsafe_code_point(code):
    char = chr(code)
    data = {"k": f"a{char}b", char: [char]}
    out = dumps(data)
    assert char not in out
    assert f"\\u{code:04x}" in out
    assert out.count(f"\\u{code:04x}") == 3
    assert json.loads(out) == data
    assert json.loads(dumps(data, indent=2)) == data


@pytest.mark.parametrize("char", _PLAIN_CHARS, ids=lambda c: hex(ord(c)))
def test_json_helper_leaves_neighbouring_characters_alone(char):
    data = {"k": f"a{char}b"}
    out = dumps(data)
    assert f"a{char}b" in out
    assert "\\u" not in out
    assert json.loads(out) == data


@pytest.mark.parametrize("code", _ESCAPED_CODE_POINTS, ids=hex)
@pytest.mark.parametrize("compact", [True, False])
def test_screen_json_escapes_every_unsafe_code_point(code, compact):
    char = chr(code)
    screen = make_screen([make_element(text=f"a{char}b")])
    out = screen.to_json() if compact else screen.to_json(indent=2)
    assert char not in out
    assert f"a\\u{code:04x}b" in out
    assert json.loads(out) == screen.to_dict()


@pytest.mark.parametrize("char", _PLAIN_CHARS, ids=lambda c: hex(ord(c)))
def test_screen_json_keeps_neighbouring_characters(char):
    screen = make_screen([make_element(text=f"a{char}b")])
    out = screen.to_json()
    assert f"a{char}b" in out
    assert json.loads(out) == screen.to_dict()


@pytest.mark.parametrize("code", _ESCAPED_CODE_POINTS, ids=hex)
def test_tree_escapes_every_unsafe_code_point(code):
    char = chr(code)
    assert tree_line(text=f"a{char}b") == f'e1 TextView "a\\u{code:04x}b"'


def test_fallback_candidates_skip_disabled_clickable_elements():
    screen = make_screen(
        [
            boxed("e1", (0, 0, 10, 10), flags=("clickable", "disabled")),
            boxed("e2", (0, 10, 10, 20), flags=("clickable",)),
        ]
    )
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="zzz")
    assert candidate_ids(info.value) == ["e2"]


# --- the 8 x 8 pixel rule ---


def test_the_minimum_visible_size_is_eight_pixels():
    assert MIN_VISIBLE_PX == 8


@pytest.mark.parametrize(
    ("bounds", "visible"),
    [
        # inside the display, width and height each 7 and 8
        ((100, 100, 107, 200), False),
        ((100, 100, 108, 200), True),
        ((100, 100, 200, 107), False),
        ((100, 100, 200, 108), True),
        ((100, 100, 107, 107), False),
        ((100, 100, 108, 108), True),
        # cut by the display edges: 7 or 8 pixels remain
        ((-93, 100, 7, 200), False),  # left
        ((-92, 100, 8, 200), True),
        ((1073, 100, 1200, 200), False),  # right: 1073..1080 is 7
        ((1072, 100, 1200, 200), True),
        ((100, -93, 200, 7), False),  # top
        ((100, -92, 200, 8), True),
        ((100, 2393, 200, 2500), False),  # bottom: 2393..2400 is 7
        ((100, 2392, 200, 2500), True),
        # the corner needs both sides
        ((1073, 2392, 1200, 2500), False),
        ((1072, 2393, 1200, 2500), False),
        ((1072, 2392, 1200, 2500), True),
        # a 2 pixel row at the bottom and a thin strip of a WebView field
        ((0, 2398, 1080, 2500), False),
        ((0, 300, 1080, 302), False),
    ],
)
def test_an_element_is_visible_only_with_eight_by_eight_pixels(bounds, visible):
    screen = make_screen([boxed("e1", bounds, text="x")])
    assert (screen.visible_bounds(screen.elements[0]) is not None) is visible
    assert bool(screen.find(text="x")) is visible


def test_a_sliver_is_not_listed_and_gets_no_candidate_center():
    screen = make_screen(
        [
            boxed("e1", (0, 0, 100, 7), text="Go"),
            boxed("e2", (0, 100, 100, 200), text="Gone"),
        ]
    )
    assert screen.find(text="Go") == []
    with pytest.raises(ElementNotVisibleError) as sole:
        screen.find_one(text="Go")  # the only match: found, then too small
    assert sole.value.details["reason"] == "too_small"
    with pytest.raises(ElementNotFoundError) as info:
        screen.find_one(text="Gon")
    candidates = info.value.details["candidates"]
    assert isinstance(candidates, list)
    ids = [c["id"] for c in candidates]
    assert ids == ["e2"]  # partial matches and fallbacks are visible ones only


def test_require_visible_explains_why_not():
    screen = make_screen(
        [
            boxed("e1", (0, 3000, 100, 3100)),
            boxed("e2", (0, 2395, 100, 2500)),
            boxed("e3", (10, 10, 60, 60)),
        ]
    )
    with pytest.raises(ElementNotVisibleError) as gone:
        screen.require_visible(screen.elements[0])
    assert gone.value.details == {
        "element": "e1",
        "bounds": [0, 3000, 100, 3100],
        "reason": "no_visible_part",
    }
    with pytest.raises(ElementNotVisibleError) as thin:
        screen.require_visible(screen.elements[1])
    assert thin.value.details == {
        "element": "e2",
        "bounds": [0, 2395, 100, 2500],
        "reason": "too_small",
        "visible_size": [100, 5],
    }
    assert "100x5" in str(thin.value)
    assert screen.require_visible(screen.elements[2]).to_list() == [10, 10, 60, 60]


# --- hit_target and the cover check ---


def hit(screen: Screen, point: tuple[int, int]) -> str | None:
    target = screen.hit_target(point)
    return None if target is None else target.id


def stack(*elements: Element) -> Screen:
    return make_screen(list(elements))


def test_hit_target_is_the_last_meaningful_element_holding_the_point():
    screen = stack(
        boxed("e1", (0, 0, 200, 200), text="Back"),
        boxed("e2", (50, 50, 150, 150), text="Front"),
    )
    assert hit(screen, (100, 100)) == "e2"
    assert hit(screen, (10, 10)) == "e1"
    assert screen.hit_target((500, 500)) is None


def test_hit_target_edges_are_exclusive_on_the_right_and_bottom():
    screen = stack(boxed("e1", (10, 20, 30, 40), text="Box"))
    assert hit(screen, (10, 20)) == "e1"
    assert hit(screen, (29, 39)) == "e1"
    assert screen.hit_target((30, 39)) is None
    assert screen.hit_target((29, 40)) is None
    assert screen.hit_target((9, 20)) is None
    assert screen.hit_target((10, 19)) is None


def test_hit_target_uses_the_part_on_the_display():
    screen = stack(boxed("e1", (-100, -100, 20, 20), text="Corner"))
    assert hit(screen, (5, 5)) == "e1"
    # off the display is never a hit, even inside the raw bounds
    assert screen.hit_target((-5, -5)) is None


def test_a_sliver_on_top_covers_what_is_under_it():
    screen = stack(
        boxed("e1", (0, 0, 100, 100), text="Below"),
        boxed("e2", (0, 90, 100, 97), text="Sliver"),  # 7 pixels high
    )
    assert hit(screen, (50, 93)) == "e2"
    assert hit(screen, (50, 89)) == "e1"
    assert hit(screen, (50, 97)) == "e1"  # bottom is exclusive


def test_a_one_pixel_strip_on_the_center_row_covers_the_target():
    target = boxed("e1", (0, 0, 100, 100), text="Below")  # center (50, 50)
    strip = boxed("e2", (20, 50, 80, 51), text="Line")
    screen = stack(target, strip)
    assert screen.visible_bounds(strip) is None
    assert hit(screen, (50, 50)) == "e2"
    with pytest.raises(ElementNotVisibleError) as caught:
        screen.tap_point(target)
    assert caught.value.details["covered_by"] == "e2"


def test_an_empty_part_never_holds_a_point():
    screen = stack(
        boxed("e1", (0, 0, 100, 100), text="Below"),
        boxed("e2", (50, 10, 50, 90), text="Zero"),
        boxed("e3", (1080, 10, 1200, 90), text="Off"),
    )
    assert hit(screen, (50, 50)) == "e1"
    assert hit(screen, (1100, 50)) is None


@pytest.mark.parametrize(
    "meaningful",
    [
        {"flags": ("clickable",)},
        {"text": "x"},
        {"content_desc": "x"},
        {"hint": "x"},
        {"text": "\u00a0 x \n"},
    ],
)
def test_meaningful_elements_are_hit_targets(meaningful):
    screen = stack(boxed("e1", (0, 0, 100, 100), **meaningful))
    assert screen.elements[0].is_meaningful
    assert hit(screen, (50, 50)) == "e1"


@pytest.mark.parametrize(
    "plain",
    [
        {},
        {"text": " \t\n"},
        {"content_desc": "  "},
        {"hint": "\u00a0"},
        {"resource_id": "a:id/layer", "flags": ("focusable", "scrollable")},
    ],
)
def test_a_container_without_a_label_or_click_is_looked_through(plain):
    screen = stack(
        boxed("e1", (0, 0, 100, 100), text="Under"),
        boxed("e2", (0, 0, 100, 100), **plain),
    )
    assert not screen.elements[1].is_meaningful
    assert hit(screen, (50, 50)) == "e1"
    assert screen.tap_point(screen.elements[0]) == (50, 50)


def test_a_descendant_on_top_does_not_cover_its_ancestor():
    screen = stack(
        boxed("e1", (0, 0, 200, 100), flags=("clickable",)),
        boxed("e2", (50, 20, 150, 80), parent="e1", text="Label"),
        boxed("e3", (60, 30, 140, 70), parent="e2", content_desc="deep"),
    )
    assert hit(screen, (100, 50)) == "e3"
    assert screen.tap_point(screen.elements[0]) == (100, 50)


def test_a_sibling_label_on_top_covers_the_button():
    screen = stack(
        boxed("e1", (0, 0, 200, 100), flags=("clickable",), text="Buy"),
        boxed("e2", (50, 20, 150, 80), text="Sale"),
    )
    with pytest.raises(ElementNotVisibleError) as info:
        screen.tap_point(screen.elements[0])
    assert info.value.code == "element_not_visible"
    assert info.value.details == {
        "element": "e1",
        "bounds": [0, 0, 200, 100],
        "reason": "covered",
        "covered_by": "e2",
        "point": [100, 50],
    }
    assert "e1 is covered by e2" in str(info.value)
    assert screen.tap_point(screen.elements[1]) == (100, 50)


def test_a_later_window_on_top_covers_an_earlier_window():
    screen = stack(
        boxed("e1", (0, 0, 1080, 2400), flags=("clickable",)),
        boxed("e2", (0, 0, 1080, 2400), parent="e1", text="Home pane"),
        boxed("e3", (0, 0, 1080, 2400), text="Home", flags=("clickable",)),
        boxed("e4", (100, 100, 300, 200), text="Item", flags=("clickable",)),
    )
    # e4 is later in document order than the pane window e3, so it is on top
    # itself; e2 and e1 lie under e3 and are covered by it.
    assert screen.tap_point(screen.elements[3]) == (200, 150)
    with pytest.raises(ElementNotVisibleError) as info:
        screen.tap_point(screen.elements[1])
    assert info.value.details["covered_by"] == "e3"
    with pytest.raises(ElementNotVisibleError) as outer:
        screen.tap_point(screen.elements[0])
    assert outer.value.details["covered_by"] == "e3"


def test_a_launcher_item_under_a_full_screen_pane_is_covered():
    screen = stack(
        boxed("e1", (100, 100, 300, 200), text="App", flags=("clickable",)),
        boxed("e2", (0, 0, 1080, 2400), text="Home", flags=("clickable",)),
    )
    with pytest.raises(ElementNotVisibleError) as info:
        screen.tap_point(screen.elements[0])
    assert info.value.details["covered_by"] == "e2"
    assert screen.tap_point(screen.elements[1]) == (540, 1200)


def test_the_cover_is_decided_at_the_press_point_only():
    screen = stack(
        boxed("e1", (0, 0, 200, 100), text="Wide", flags=("clickable",)),
        boxed("e2", (0, 0, 40, 100), text="Corner"),  # not at the center
    )
    assert screen.tap_point(screen.elements[0]) == (100, 50)


def test_a_cover_edge_is_exclusive_at_the_press_point():
    # center of e1 is (100, 50); e2 ends at x=100, so it does not hold it
    screen = stack(
        boxed("e1", (0, 0, 200, 100), text="Wide"),
        boxed("e2", (0, 0, 100, 100), text="Left"),
    )
    assert screen.tap_point(screen.elements[0]) == (100, 50)
    wider = stack(
        boxed("e1", (0, 0, 200, 100), text="Wide"),
        boxed("e2", (0, 0, 101, 100), text="Left"),
    )
    with pytest.raises(ElementNotVisibleError):
        wider.tap_point(wider.elements[0])


def test_hit_target_none_refuses_an_element_with_no_press_target():
    # No meaningful element holds the point, so nothing there can receive the
    # press (the cover check): refused with the distinct reason no_target.
    plain = boxed("e1", (0, 0, 100, 100), resource_id="a:id/only")
    screen = stack(plain)
    assert screen.hit_target((50, 50)) is None
    with pytest.raises(ElementNotVisibleError) as caught:
        screen.tap_point(plain)
    assert caught.value.details == {
        "element": "e1",
        "bounds": [0, 0, 100, 100],
        "reason": "no_target",
        "point": [50, 50],
    }
    assert "nothing that can receive a press is at 50,50" in str(caught.value)
    assert "center" not in screen._element_dict(plain)


def test_find_one_counts_a_thin_match_as_a_second_match():
    big = boxed("e1", (0, 0, 100, 100), text="Item")
    thin = boxed("e2", (0, 200, 100, 207), text="Item")
    screen = stack(big, thin)
    assert screen.find(text="Item") == [big]
    with pytest.raises(MultipleElementsError) as caught:
        screen.find_one(text="Item")
    assert caught.value.details["total"] == 2
    candidates = caught.value.details["candidates"]
    assert isinstance(candidates, list)
    assert [c["id"] for c in candidates] == ["e1", "e2"]
    assert "center" in candidates[0]
    assert "center" not in candidates[1]


def test_find_one_with_a_sole_thin_match_is_too_small():
    thin = boxed("e1", (0, 200, 100, 207), text="Item")
    with pytest.raises(ElementNotVisibleError) as caught:
        stack(thin).find_one(text="Item")
    assert caught.value.details["reason"] == "too_small"
    assert caught.value.details["visible_size"] == [100, 7]


def test_a_plain_child_in_a_clickable_row_is_refused_naming_the_row():
    # The press would reach the row, not the unlabeled icon: refused, and the
    # message says that the ancestor receives it (not that something is on top).
    row = boxed("e1", (0, 0, 100, 100), flags=("clickable",))
    icon = boxed("e2", (10, 10, 90, 90), parent="e1", resource_id="a:id/icon")
    screen = stack(row, icon)
    with pytest.raises(ElementNotVisibleError) as caught:
        screen.tap_point(icon)
    assert caught.value.details["reason"] == "covered"
    assert caught.value.details["covered_by"] == "e1"
    assert "ancestor e1" in str(caught.value)
    assert "center" not in screen._element_dict(icon)


def test_a_plain_element_over_a_meaningful_one_is_still_refused():
    # The plain layer receives no press of its own, so it would reach the
    # button below: that is another element than the one asked for.
    screen = stack(
        boxed("e1", (0, 0, 100, 100), text="Button"),
        boxed("e2", (0, 0, 100, 100), resource_id="a:id/layer"),
    )
    with pytest.raises(ElementNotVisibleError) as info:
        screen.tap_point(screen.elements[1])
    assert info.value.details["covered_by"] == "e1"


def test_find_lists_a_covered_element_without_a_center():
    screen = stack(
        boxed("e1", (0, 0, 200, 100), text="Buy"),
        boxed("e2", (50, 20, 150, 80), text="Sale"),
    )
    assert [e.id for e in screen.find(text="Buy")] == ["e1"]
    assert "center" not in screen._element_dict(screen.elements[0])
    assert screen._element_dict(screen.elements[1])["center"] == [100, 50]
    assert "center=" not in screen._describe_line(screen.elements[0])
    assert screen._describe_line(screen.elements[1]).endswith("center=100,50")


def test_find_one_candidates_of_a_covered_element_have_no_center():
    screen = stack(
        boxed("e1", (0, 0, 200, 100), text="Buy"),
        boxed("e2", (50, 20, 150, 80), text="Buy"),
        boxed("e3", (50, 20, 150, 80), text="Other"),
    )
    with pytest.raises(MultipleElementsError) as info:
        screen.find_one(text="Buy")
    candidates = info.value.details["candidates"]
    assert isinstance(candidates, list)
    first, second = candidates
    assert "center" not in first
    assert "center" not in second  # e3 is on top of both
    assert info.value.details["total"] == 2


def test_a_parent_cycle_does_not_hang_the_descendant_walk():
    screen = stack(
        boxed("e1", (0, 0, 100, 100), text="A"),
        boxed("e2", (0, 0, 100, 100), text="B", parent="e3"),
        boxed("e3", (0, 0, 100, 100), text="C", parent="e2"),
    )
    with pytest.raises(ElementNotVisibleError) as info:
        screen.tap_point(screen.elements[0])
    assert info.value.details["covered_by"] == "e3"
