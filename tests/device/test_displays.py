"""Tests for `parse_active_display_physical_id` (``dumpsys display``).

The fixtures are written by hand in the shape of a two-display phone. All
values in them are invented.
"""

from __future__ import annotations

import pytest

from uiprobe.device.displays import parse_active_display_physical_id

ID_A = "1111111111111111111"
ID_B = "2222222222222222222"
ID_C = "3333333333333333333"


def viewport(display: int, unique: str, *, active: bool = True) -> str:
    return (
        f"DisplayViewport{{type=INTERNAL, valid=true, isActive={str(active).lower()},"
        f" displayId={display}, uniqueId='local:{unique}', physicalPort=7,"
        " orientation=0, densityDpi=400, xDpi=300.5, yDpi=300.25,"
        " logicalFrame=Rect(0, 0 - 10, 20), physicalFrame=Rect(0, 0 - 10, 20),"
        " deviceWidth=10, deviceHeight=20}"
    )


def logical(*pairs: tuple[int, str], enabled: tuple[bool, ...] = ()) -> str:
    out = [f"  Logical Displays: size={len(pairs)}"]
    for index, (number, unique) in enumerate(pairs):
        on = str(enabled[index] if enabled else index == 0).lower()
        out += [
            f"  Display {number}:",
            f"    mDisplayId={number}",
            f"    mPrimaryDisplayDevice=Built-in Screen(local:{unique})",
            f"    mIsEnabled={on}",
            "    mIsInTransition=false",
            f"    mLayerStack={number}",
            "    mHasContent=true",
            f'    mBaseDisplayInfo=DisplayInfo{{"Name", displayId {number}}}',
            "      mDisplayInfoChangeSource=DISPLAY_MANAGER",
            "  ",
        ]
    return "\n".join(out) + "\n"


def make(
    views: list[tuple[int, str]],
    logicals: list[tuple[int, str]],
    *,
    folded: bool = False,
    on: tuple[bool, ...] | None = None,
) -> str:
    """A ``dumpsys display`` text in the line structure of a two-display phone.

    Every id, name and number is invented. Folded, display 0 is switched off
    and display 1 is on, as seen on a real phone (the ids stay with their
    displays).
    """
    flags = on or tuple((n == 0) != folded for n, _ in views)
    line = ", ".join(viewport(n, u, active=flags[i]) for i, (n, u) in enumerate(views))
    enabled = on or tuple((n == 0) != folded for n, _ in logicals)
    return (
        "DISPLAY MANAGER (dumpsys display)\n"
        f"  mViewports=[{line}]\n"
        "  mPendingDeviceState=-1\n"
        "  \n" + logical(*logicals, enabled=enabled) + "  \n"
        "   Display Groups: size=1\n"
        "  mDisplayId=0\n"
        "  mDisplayId=1\n"
    )


ID_ALL = [(0, ID_A), (1, ID_B)]


def test_the_open_shape_gives_the_id_of_display_zero():
    assert parse_active_display_physical_id(make(ID_ALL, ID_ALL)) == ID_A


def test_the_folded_shape_gives_the_id_of_display_one():
    # Folded, display 0 is off and display 1 is on; the ids stay with them.
    assert parse_active_display_physical_id(make(ID_ALL, ID_ALL, folded=True)) == ID_B


def test_the_order_of_the_viewports_does_not_matter():
    views = [(1, ID_B), (0, ID_A)]
    assert parse_active_display_physical_id(make(views, views)) == ID_A
    assert parse_active_display_physical_id(make(views, views, folded=True)) == ID_B


def test_both_displays_on_gives_none():
    assert (
        parse_active_display_physical_id(make(ID_ALL, ID_ALL, on=(True, True))) is None
    )


def test_no_display_on_gives_none():
    text = make(ID_ALL, ID_ALL, on=(False, False))
    assert parse_active_display_physical_id(text) is None


def test_a_display_on_in_one_place_only_is_not_on():
    text = make(ID_ALL, ID_ALL).replace("mIsEnabled=true", "mIsEnabled=false", 1)
    assert parse_active_display_physical_id(text) is None
    text = make(ID_ALL, ID_ALL).replace("isActive=true", "isActive=false", 1)
    assert parse_active_display_physical_id(text) is None


def test_a_missing_active_flag_gives_none():
    text = make(ID_ALL, ID_ALL).replace(" isActive=false,", "")
    assert parse_active_display_physical_id(text) is None


def test_a_missing_enabled_flag_gives_none():
    text = make(ID_ALL, ID_ALL).replace("mIsEnabled", "mIsEnabledX")
    assert parse_active_display_physical_id(text) is None


def test_a_flag_that_is_not_true_or_false_gives_none():
    text = make(ID_ALL, ID_ALL).replace("mIsEnabled=false", "mIsEnabled=maybe")
    assert parse_active_display_physical_id(text) is None


def test_ids_that_disagree_give_none():
    assert parse_active_display_physical_id(make([(0, ID_A)], [(0, ID_B)])) is None


def test_a_missing_viewport_line_gives_none():
    text = make(ID_ALL, ID_ALL).replace("mViewports=", "mViewportz=")
    assert parse_active_display_physical_id(text) is None


def test_a_display_without_a_viewport_gives_none():
    assert parse_active_display_physical_id(make([(0, ID_A)], ID_ALL)) is None


def test_a_missing_logical_block_gives_none():
    text = make(ID_ALL, ID_ALL).replace("Logical Displays:", "Other:")
    assert parse_active_display_physical_id(text) is None


def test_a_display_without_a_device_line_gives_none():
    text = make(ID_ALL, ID_ALL).replace("mPrimaryDisplayDevice", "mOther")
    assert parse_active_display_physical_id(text) is None


@pytest.mark.parametrize("bad", ["abc", "12a45", "", "-5", "1.5", "\u0661\u0662"])
def test_ids_that_are_not_digits_give_none(bad):
    assert parse_active_display_physical_id(make([(0, bad)], [(0, bad)])) is None


def test_an_id_longer_than_20_digits_gives_none():
    long = "1" * 21
    assert parse_active_display_physical_id(make([(0, long)], [(0, long)])) is None


def test_extra_displays_are_read_when_exactly_one_is_on():
    three = [(0, ID_C), (1, ID_A), (2, ID_B)]
    assert parse_active_display_physical_id(make(three, three)) == ID_C
    assert (
        parse_active_display_physical_id(make(three, three, on=(False, False, True)))
        == ID_B
    )
    assert (
        parse_active_display_physical_id(make(three, three, on=(True, False, True)))
        is None
    )


def test_two_viewports_for_one_display_give_none():
    assert (
        parse_active_display_physical_id(make([(0, ID_A), (0, ID_A)], [(0, ID_A)]))
        is None
    )


def test_two_logical_blocks_for_one_display_give_none():
    assert (
        parse_active_display_physical_id(make([(0, ID_A)], [(0, ID_A), (0, ID_A)]))
        is None
    )


def test_crlf_line_endings_are_read():
    text = make(ID_ALL, ID_ALL, folded=True).replace("\n", "\r\n")
    assert parse_active_display_physical_id(text) == ID_B


def test_empty_text_gives_none():
    assert parse_active_display_physical_id("") is None


def _blocks(*blocks: list[str], indent: str = "    ") -> str:
    out = ["  Logical Displays: size=2"]
    for heading, lines in zip(("0", "1"), blocks, strict=False):
        out.append(f"  Display {heading}:")
        out += [indent + line for line in lines]
    return "\n".join(out) + "\n"


def _text(viewport_id: str, logical: str) -> str:
    views = f"{viewport(0, viewport_id)}, {viewport(1, ID_B, active=False)}"
    return f"  mViewports=[{views}]\n{logical}"


@pytest.mark.parametrize("device_first", [False, True])
def test_a_device_line_belongs_to_its_own_display_block(device_first):
    # The viewport says display 0 is 2222...; its block says 1111....
    def block(number: int, unique: str, *, enabled: bool) -> list[str]:
        lines = [
            f"mDisplayId={number}",
            f"mPrimaryDisplayDevice=Screen(local:{unique})",
            f"mIsEnabled={str(enabled).lower()}",
        ]
        return [lines[1], lines[0], lines[2]] if device_first else lines

    blocks = _blocks(block(0, ID_A, enabled=True), block(1, ID_B, enabled=False))
    assert parse_active_display_physical_id(_text(ID_B, blocks)) is None
    assert parse_active_display_physical_id(_text(ID_A, blocks)) == ID_A


def test_a_nested_section_does_not_supply_a_missing_device_line():
    text = _text(
        ID_A,
        "  Logical Displays: size=2\n"
        "  Display 0:\n"
        "    mDisplayId=0\n"
        "    mIsEnabled=true\n"
        "    Other:\n"
        f"      mPrimaryDisplayDevice=Screen(local:{ID_A})\n"
        "  Display 1:\n"
        "    mDisplayId=1\n"
        f"    mPrimaryDisplayDevice=Screen(local:{ID_B})\n"
        "    mIsEnabled=false\n",
    )
    assert parse_active_display_physical_id(text) is None


def test_a_device_line_of_the_next_block_does_not_fill_a_block():
    text = _text(
        ID_B,
        "  Logical Displays: size=2\n"
        "  Display 0:\n"
        "    mDisplayId=0\n"
        "    mIsEnabled=true\n"
        "  Display 1:\n"
        "    mDisplayId=1\n"
        f"    mPrimaryDisplayDevice=Screen(local:{ID_B})\n"
        "    mIsEnabled=false\n",
    )
    assert parse_active_display_physical_id(text) is None


def test_a_huge_display_number_gives_none_not_an_error():
    huge = "9" * 4301
    good = make(ID_ALL, ID_ALL)
    for old, new in (
        ("displayId=0,", f"displayId={huge},"),
        ("mDisplayId=0\n", f"mDisplayId={huge}\n"),
        ("Display 0:", f"Display {huge}:"),
    ):
        assert parse_active_display_physical_id(good.replace(old, new, 1)) is None


def test_records_after_the_closing_bracket_are_ignored():
    good = make(ID_ALL, ID_ALL, folded=True)
    stale = viewport(0, ID_C, active=True)
    text = good.replace("]\n", f"], {stale}\n", 1)
    assert parse_active_display_physical_id(text) == ID_B
    only_one = make([(0, ID_A)], [(0, ID_A), (1, ID_B)]).replace(
        "]\n", f"], {viewport(1, ID_B, active=False)}\n", 1
    )
    assert parse_active_display_physical_id(only_one) is None


def test_a_list_that_is_not_closed_or_empty_gives_none():
    good = make(ID_ALL, ID_ALL)
    assert parse_active_display_physical_id(good.replace("}]\n", "}\n", 1)) is None
    assert (
        parse_active_display_physical_id(good.replace("}, Disp", "} Disp", 1)) is None
    )
    empty = "  mViewports=[]\n" + logical((0, ID_A))
    assert parse_active_display_physical_id(empty) is None


def test_mviewports_inside_a_nested_section_is_not_taken():
    good = make(ID_ALL, ID_ALL)
    nested = good.replace("  mViewports=", "  Other:\n    mViewports=", 1)
    assert parse_active_display_physical_id(nested) is None
    deeper = good.replace("  mViewports=", "   mViewports=", 1)
    assert parse_active_display_physical_id(deeper) is None


def test_a_second_mviewports_line_gives_none():
    good = make(ID_ALL, ID_ALL)
    line = good.split("\n")[1]
    assert parse_active_display_physical_id(good + line + "\n") is None
