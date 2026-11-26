"""Tests for `parse_display_zero_physical_id` (``dumpsys display``).

The fixtures are written by hand in the shape of a two-display phone. All
values in them are invented.
"""

from __future__ import annotations

import pytest

from uiprobe.device.displays import parse_display_zero_physical_id

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
) -> str:
    """A ``dumpsys display`` text in the line structure of a two-display phone.

    Every id, name and number is invented. Folded, display 0 is switched off
    and display 1 is on, as seen on a real phone (the ids stay with their
    displays).
    """
    line = ", ".join(viewport(n, u, active=(n == 0) != folded) for n, u in views)
    enabled = tuple((n == 0) != folded for n, _ in logicals)
    return (
        "DISPLAY MANAGER (dumpsys display)\n"
        f"  mViewports=[{line}]\n"
        "  mPendingDeviceState=-1\n"
        "  \n" + logical(*logicals, enabled=enabled) + "  \n"
        "   Display Groups: size=1\n"
        "  mDisplayId=0\n"
        "  mDisplayId=1\n"
    )


def test_the_open_shape():
    text = make([(0, ID_A), (1, ID_B)], [(0, ID_A), (1, ID_B)])
    assert parse_display_zero_physical_id(text) == ID_A


def test_the_folded_shape_is_read():
    # Folded, display 0 is off and display 1 on; display 0 keeps its id.
    text = make([(0, ID_A), (1, ID_B)], [(0, ID_A), (1, ID_B)], folded=True)
    assert parse_display_zero_physical_id(text) == ID_A


def test_a_phone_whose_display_zero_is_the_other_device_is_read():
    text = make([(0, ID_B), (1, ID_A)], [(0, ID_B), (1, ID_A)])
    assert parse_display_zero_physical_id(text) == ID_B


def test_the_order_of_the_viewports_does_not_matter():
    text = make([(1, ID_B), (0, ID_A)], [(1, ID_B), (0, ID_A)])
    assert parse_display_zero_physical_id(text) == ID_A


def test_ids_that_disagree_give_none():
    assert parse_display_zero_physical_id(make([(0, ID_A)], [(0, ID_B)])) is None


def test_a_missing_viewport_line_gives_none():
    text = make([(0, ID_A)], [(0, ID_A)]).replace("mViewports=", "mViewportz=")
    assert parse_display_zero_physical_id(text) is None


def test_a_viewport_line_without_display_zero_gives_none():
    assert parse_display_zero_physical_id(make([(1, ID_B)], [(0, ID_A)])) is None


def test_a_missing_logical_block_gives_none():
    text = make([(0, ID_A)], [(0, ID_A)]).replace("Logical Displays:", "Other:")
    assert parse_display_zero_physical_id(text) is None


def test_a_logical_block_without_display_zero_gives_none():
    assert parse_display_zero_physical_id(make([(0, ID_A)], [(1, ID_B)])) is None


def test_a_display_zero_without_a_device_line_gives_none():
    text = make([(0, ID_A)], [(0, ID_A)]).replace("mPrimaryDisplayDevice", "mOther")
    assert parse_display_zero_physical_id(text) is None


@pytest.mark.parametrize("bad", ["abc", "12a45", "", "-5", "1.5", "١٢"])
def test_ids_that_are_not_digits_give_none(bad):
    assert parse_display_zero_physical_id(make([(0, bad)], [(0, bad)])) is None


def test_an_id_longer_than_20_digits_gives_none():
    long = "1" * 21
    assert parse_display_zero_physical_id(make([(0, long)], [(0, long)])) is None


def test_extra_displays_are_ignored():
    text = make([(0, ID_C), (1, ID_A), (2, ID_B)], [(0, ID_C), (1, ID_A), (2, ID_B)])
    assert parse_display_zero_physical_id(text) == ID_C


def test_two_viewports_for_display_zero_give_none():
    text = make([(0, ID_A), (0, ID_A)], [(0, ID_A)])
    assert parse_display_zero_physical_id(text) is None


def test_two_logical_blocks_for_display_zero_give_none():
    text = make([(0, ID_A)], [(0, ID_A), (0, ID_A)])
    assert parse_display_zero_physical_id(text) is None


def test_crlf_line_endings_are_read():
    text = make([(0, ID_A), (1, ID_B)], [(0, ID_A), (1, ID_B)]).replace("\n", "\r\n")
    assert parse_display_zero_physical_id(text) == ID_A


def test_empty_text_gives_none():
    assert parse_display_zero_physical_id("") is None
