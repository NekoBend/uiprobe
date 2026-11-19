"""Tests for the brief text view, ``uiprobe dump --brief``."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import pytest

from tests.test_cli import SERIAL, invoke
from uiprobe.model import BRIEF_LABEL_LIMIT, Bounds, Element, Screen

DATA = Path(__file__).parent / "data"
ROWS = [
    ("Network & internet", "Mobile, Wi-Fi, hotspot"),
    ("Connected devices", "Bluetooth, pairing"),
    ("Apps", "Assistant, recent apps, default apps"),
    ("Notifications", "Notification history, conversations"),
    ("Battery", "100%"),
    ("Storage", "40% used - 20 GB free"),
    ("Sound & vibration", "Volume, haptics, Do Not Disturb"),
    ("Display", "Dark theme, font size, brightness"),
    ("Wallpaper & style", "Colors, themed icons, app grid"),
    ("Accessibility", "Display size, text, interaction controls"),
    ("Security & privacy", "App security, device lock, biometrics"),
    ("Location", "On - 9 apps have access to location"),
    ("Passwords & accounts", "Saved passwords, autofill, synced accounts"),
    ("System", "Languages, gestures, time, backup"),
    ("About emulated device", "sdk_gphone64_x86_64"),
]


class Builder:
    """Collect elements in document order with ids e1, e2, ..."""

    def __init__(self) -> None:
        self.items: list[Element] = []

    def add(self, parent: str | None, cls: str, **kwargs) -> str:
        flags = tuple(kwargs.pop("flags", ()))
        element_id = f"e{len(self.items) + 1}"
        self.items.append(
            Element(
                id=element_id,
                parent=parent,
                class_name=f"android.widget.{cls}",
                bounds=Bounds(0, 0, 1080, 2400),
                package="com.example.settings",
                flags=flags,
                **kwargs,
            )
        )
        return element_id


def make_screen(elements: list[Element]) -> Screen:
    return Screen(
        serial=SERIAL,
        captured_at=datetime(2026, 1, 1, tzinfo=UTC),
        package="com.example.settings",
        activity=".Settings",
        api_level=36,
        display=(1080, 2400),
        elements=tuple(elements),
    )


def settings_elements() -> list[Element]:
    b = Builder()
    root = b.add(None, "FrameLayout")
    outer = b.add(root, "LinearLayout")
    bar = b.add(outer, "FrameLayout")
    b.add(bar, "ImageButton", flags=["clickable"])  # pressable icon, no label
    b.add(bar, "TextView", text="Settings")
    box = b.add(outer, "FrameLayout")
    inner = b.add(box, "LinearLayout")
    b.add(inner, "ImageView")
    b.add(inner, "EditText", hint="Search settings", flags=["clickable"])
    scroll = b.add(outer, "ScrollView", flags=["scrollable"])
    column = b.add(scroll, "LinearLayout")
    for title, summary in ROWS:
        row = b.add(column, "LinearLayout", flags=["clickable"])
        icon_box = b.add(row, "FrameLayout")
        b.add(icon_box, "ImageView")
        texts = b.add(row, "LinearLayout")
        b.add(texts, "TextView", text=title, resource_id="android:id/title")
        b.add(texts, "TextView", text=summary, resource_id="android:id/summary")
    return b.items


def settings_screen() -> Screen:
    return make_screen(settings_elements())


BRIEF = """\
com.example.settings/.Settings  (SID)
e4 ImageButton [tap]
e5 TextView "Settings"
e9 EditText hint="Search settings" [tap]
e10 ScrollView [scroll]
\u251c e12 LinearLayout labels="Network & internet Mobile, Wi-Fi, hotspot" [tap]
\u251c e18 LinearLayout labels="Connected devices Bluetooth, pairing" [tap]
\u251c e24 LinearLayout labels="Apps Assistant, recent apps, default apps" [tap]
\u251c e30 LinearLayout labels="Notifications Notification history, conversations" [tap]
\u251c e36 LinearLayout labels="Battery 100%" [tap]
\u251c e42 LinearLayout labels="Storage 40% used - 20 GB free" [tap]
\u251c e48 LinearLayout labels="Sound & vibration Volume, haptics, Do Not Disturb" [tap]
\u251c e54 LinearLayout labels="Display Dark theme, font size, brightness" [tap]
\u251c e60 LinearLayout labels="Wallpaper & style Colors, themed icons, app grid" [tap]
\u251c e66 LinearLayout labels="Accessibility Display size, text, interaction controls" [tap]
\u251c e72 LinearLayout labels="Security & privacy App security, device lock, biometrics" [tap]
\u251c e78 LinearLayout labels="Location On - 9 apps have access to location" [tap]
\u251c e84 LinearLayout labels="Passwords & accounts Saved passwords, autofill, synced ac..." [tap]
\u251c e90 LinearLayout labels="System Languages, gestures, time, backup" [tap]
\u2514 e96 LinearLayout labels="About emulated device sdk_gphone64_x86_64" [tap]
"""  # noqa: E501


def brief_text(screen: Screen) -> str:
    return screen.to_tree(brief=True)


def test_settings_screen_line_counts():
    screen = settings_screen()
    assert len(screen.elements) == 101
    assert len(screen.to_tree().splitlines()) == 102
    assert len(brief_text(screen).splitlines()) == 20


def test_brief_text_is_exact():
    screen = settings_screen()
    expected = BRIEF.replace("SID", screen.screen_id)
    assert brief_text(screen) == expected.rstrip("\n")


def test_brief_ids_are_the_ids_of_the_full_view():
    screen = settings_screen()
    full = {
        line.lstrip("\u2502\u251c\u2514 ").split(" ", 1)[0]: line.lstrip(
            "\u2502\u251c\u2514 "
        )
        for line in screen.to_tree().splitlines()[1:]
    }
    for line in brief_text(screen).splitlines()[1:]:
        element_id, class_name = line.lstrip("\u2502\u251c\u2514 ").split(" ")[:2]
        assert full[element_id].split(" ")[1] == class_name


def test_default_view_is_unchanged_and_lists_every_element():
    screen = settings_screen()
    lines = screen.to_tree().splitlines()
    assert lines[1] == "e1 FrameLayout"
    assert lines[2] == "\u2514 e2 LinearLayout"
    assert lines[4] == "  \u2502 \u251c e4 ImageButton [tap]"
    assert screen.to_tree() == screen.to_tree(brief=False)


def test_pressable_icon_without_a_label_is_kept():
    assert "e4 ImageButton [tap]" in brief_text(settings_screen())


def test_nesting_follows_the_nearest_kept_ancestor():
    b = Builder()
    root = b.add(None, "FrameLayout")
    layer = b.add(root, "LinearLayout")
    title = b.add(layer, "TextView", text="Title")
    deeper = b.add(title, "FrameLayout")
    b.add(deeper, "TextView", text="Sub")
    b.add(layer, "TextView", text="Next")
    assert brief_text(make_screen(b.items)).splitlines()[1:] == [
        'e3 TextView "Title"',
        '\u2514 e5 TextView "Sub"',
        'e6 TextView "Next"',
    ]


def test_row_with_a_pressable_labeled_descendant_is_not_folded():
    b = Builder()
    row = b.add(None, "LinearLayout", flags=["clickable"])
    b.add(row, "TextView", text="Wi-Fi")
    b.add(row, "Switch", text="On", flags=["clickable", "checkable"])
    assert brief_text(make_screen(b.items)).splitlines()[1:] == [
        "e1 LinearLayout [tap]",
        '\u251c e2 TextView "Wi-Fi"',
        '\u2514 e3 Switch "On" [tap]',
    ]


def test_row_with_its_own_label_is_not_folded():
    b = Builder()
    row = b.add(None, "LinearLayout", content_desc="Open", flags=["clickable"])
    b.add(row, "TextView", text="Wi-Fi")
    assert brief_text(make_screen(b.items)).splitlines()[1:] == [
        'e1 LinearLayout desc="Open" [tap]',
        '\u2514 e2 TextView "Wi-Fi"',
    ]


def test_nested_pressable_inside_a_folded_row_does_not_repeat_labels():
    b = Builder()
    row = b.add(None, "LinearLayout", flags=["clickable"])
    mid = b.add(row, "LinearLayout", flags=["clickable"])
    b.add(mid, "TextView", text="Only once")
    assert brief_text(make_screen(b.items)).splitlines()[1:] == [
        'e1 LinearLayout labels="Only once" [tap]',
        "\u2514 e2 LinearLayout [tap]",
    ]


def test_folded_label_uses_description_and_hint_and_is_escaped():
    b = Builder()
    row = b.add(None, "LinearLayout", flags=["clickable"])
    b.add(row, "TextView", text="Line one\nline\ttwo " + chr(0x202E))
    b.add(row, "ImageView", content_desc="Star")
    b.add(row, "TextView", text="   ")
    b.add(row, "EditText", hint="Name")
    line = brief_text(make_screen(b.items)).splitlines()[1]
    assert line == (
        'e1 LinearLayout labels="Line one\\nline\\ttwo \\u202e Star Name" [tap]'
    )


def test_labeled_descendant_of_an_unlabeled_row_with_blank_labels_stays():
    b = Builder()
    row = b.add(None, "LinearLayout", flags=["clickable"])
    b.add(row, "TextView", text="  ")
    assert brief_text(make_screen(b.items)).splitlines()[1:] == [
        "e1 LinearLayout [tap]",
        '\u2514 e2 TextView "  "',
    ]


def test_long_folded_label_is_cut_with_an_ellipsis():
    b = Builder()
    row = b.add(None, "LinearLayout", flags=["clickable"])
    b.add(row, "TextView", text="x" * 100)
    line = brief_text(make_screen(b.items)).splitlines()[1]
    found = re.search(r'labels="([^"]*)"', line)
    assert found is not None
    label = found.group(1)
    assert label == "x" * (BRIEF_LABEL_LIMIT - 3) + "..."
    assert len(label) == BRIEF_LABEL_LIMIT


def test_label_of_exactly_the_limit_is_not_cut():
    b = Builder()
    row = b.add(None, "LinearLayout", flags=["clickable"])
    b.add(row, "TextView", text="y" * BRIEF_LABEL_LIMIT)
    assert "..." not in brief_text(make_screen(b.items))


def test_element_with_only_a_hint_is_kept():
    b = Builder()
    root = b.add(None, "FrameLayout")
    b.add(root, "EditText", hint="Search")
    assert brief_text(make_screen(b.items)).splitlines()[1:] == [
        'e2 EditText hint="Search"'
    ]


def test_empty_screen_prints_only_the_header():
    screen = make_screen([])
    assert brief_text(screen) == screen.to_tree()
    assert len(brief_text(screen).splitlines()) == 1


def test_screen_without_anything_to_keep_prints_only_the_header():
    b = Builder()
    root = b.add(None, "FrameLayout")
    b.add(root, "View")
    assert len(brief_text(make_screen(b.items)).splitlines()) == 1


def test_deep_nesting_keeps_the_indent_of_kept_elements_only():
    b = Builder()
    parent = None
    for _ in range(200):
        parent = b.add(parent, "FrameLayout")
    b.add(parent, "TextView", text="Deep")
    assert brief_text(make_screen(b.items)).splitlines()[1:] == ['e201 TextView "Deep"']


def test_scroll_container_with_a_label_keeps_both_tags_and_labels():
    b = Builder()
    b.add(None, "ScrollView", text="List", flags=["scrollable"])
    assert brief_text(make_screen(b.items)).splitlines()[1:] == [
        'e1 ScrollView "List" [scroll]'
    ]


# --- command line ---


def node(element: Element, children: dict[str | None, list[Element]]) -> str:
    flags = {
        name: "true" if name in element.flags else "false"
        for name in (
            "checkable",
            "checked",
            "clickable",
            "long_clickable",
            "scrollable",
            "focusable",
            "focused",
            "selected",
            "password",
        )
    }
    attrs = (
        f'text="{escape(element.text)}" resource-id="{escape(element.resource_id)}" '
        f'class="{element.class_name}" package="{element.package}" '
        f'content-desc="{escape(element.content_desc)}" '
        f'hint="{escape(element.hint)}" '
        f'checkable="{flags["checkable"]}" checked="{flags["checked"]}" '
        f'clickable="{flags["clickable"]}" enabled="true" '
        f'focusable="{flags["focusable"]}" focused="{flags["focused"]}" '
        f'scrollable="{flags["scrollable"]}" '
        f'long-clickable="{flags["long_clickable"]}" '
        f'password="{flags["password"]}" selected="{flags["selected"]}" '
        f'bounds="[0,0][1080,2400]"'
    )
    inner = "".join(node(kid, children) for kid in children.get(element.id, []))
    return f"<node {attrs}>{inner}</node>"


def hierarchy_xml(elements: list[Element]) -> bytes:
    children: dict[str | None, list[Element]] = {}
    for element in elements:
        children.setdefault(element.parent, []).append(element)
    body = "".join(node(top, children) for top in children[None])
    return f'<?xml version="1.0"?><hierarchy rotation="0">{body}</hierarchy>'.encode()


@pytest.fixture
def phone(fake_adb):
    fake_adb.add_device(SERIAL, model="Pixel_8")
    fake_adb.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    fake_adb.on_shell(SERIAL, "getprop ro.kernel.qemu", b"1\n")
    fake_adb.on_shell(
        SERIAL,
        "dumpsys window",
        b"  mFocusedApp=ActivityRecord{1 u0 com.example.settings/.Settings t3}\n",
    )
    fake_adb.on_shell(
        SERIAL,
        re.compile(r"^uiautomator dump "),
        b"UI hierchary dumped to: /data/local/tmp/x.xml\n",
    )
    fake_adb.on_exec(SERIAL, re.compile(r"^cat "), hierarchy_xml(settings_elements()))
    return fake_adb


def test_dump_brief_matches_the_full_view_ids(phone, capsys):
    code, full, err = invoke(capsys, "dump", "--adb-server", phone.address)
    assert (code, err) == (0, "")
    code, brief, err = invoke(capsys, "dump", "--brief", "--adb-server", phone.address)
    assert (code, err) == (0, "")
    assert len(full.splitlines()) == 102
    assert len(brief.splitlines()) == 20
    assert brief.splitlines()[0] == full.splitlines()[0]
    assert 'e9 EditText hint="Search settings" [tap]' in brief
    assert "e9 EditText id=" not in brief
    assert 'e9 EditText hint="Search settings" [tap]' in full


def test_dump_without_brief_is_the_default_view(phone, capsys):
    _, out, _ = invoke(capsys, "dump", "--adb-server", phone.address)
    assert "labels=" not in out
    assert out.splitlines()[1] == "e1 FrameLayout"


@pytest.mark.parametrize(
    "argv",
    [("dump", "--brief", "--json"), ("--json", "dump", "--brief")],
)
def test_brief_with_json_is_a_usage_error(phone, capsys, argv):
    code, out, err = invoke(capsys, *argv, "--adb-server", phone.address)
    assert code == 2
    payload = json.loads(out)
    assert payload["error"]["code"] == "usage_error"
    assert "--brief" in payload["error"]["message"]
    assert "--json" in payload["error"]["message"]
    assert err == ""


def test_dump_json_is_unchanged_by_the_feature(phone, capsys):
    code, out, _ = invoke(capsys, "dump", "--json", "--adb-server", phone.address)
    assert code == 0
    assert len(json.loads(out)["elements"]) == 101
