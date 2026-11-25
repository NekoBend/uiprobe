"""Tests for ``Device.scroll``: the geometry and a display that is not measured."""

from __future__ import annotations

import json
import random
import re

import pytest

from tests.device.gestures.helpers import (
    SERIAL,
    SHOP,
    SUFFIX,
    element,
    invoke,
    no_request,
    sent_inputs,
)
from tests.fake_adb_server import Fail, FakeAdbServer
from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
)
from uiprobe.device import (
    Device,
)
from uiprobe.errors import (
    ElementNotVisibleError,
    UiprobeError,
)
from uiprobe.model import Bounds, Element

# --- Device.scroll: geometry (display 320x640) ---


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ("up", ((160, 511), (160, 128))),
        ("down", ((160, 128), (160, 511))),
        ("left", ((255, 320), (64, 320))),
        ("right", ((64, 320), (255, 320))),
    ],
)
def test_scroll_the_whole_display(
    device, android, fake_adb, screen, direction, expected
):
    # inset 64 and 128, edge margins 16 and 32 do not bind: (64,128)-(256,512);
    # the far ends are the last pixels, 255 and 511
    assert screen.display == (320, 640)
    assert device.scroll(direction) == expected
    (a, b), (c, d) = expected
    assert sent_inputs(fake_adb) == [f"input swipe {a} {b} {c} {d} 300" + SUFFIX]
    assert len(android.inputs) == 1


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ("up", ((160, 259), (160, 140))),
        ("down", ((160, 140), (160, 259))),
        ("left", ((249, 200), (70, 200))),
        ("right", ((70, 200), (249, 200))),
    ],
)
def test_scroll_within_an_element(device, fake_adb, screen, direction, expected):
    # Pane (10,100,310,300): inset 60 and 40, giving (70,140)-(250,260);
    # last pixels 249 and 259
    assert device.scroll(direction, within=element(screen, "Pane")) == expected
    (a, b), (c, d) = expected
    assert sent_inputs(fake_adb) == [f"input swipe {a} {b} {c} {d} 300" + SUFFIX]


def test_scroll_keeps_away_from_the_display_edges(device, screen):
    # Corner (0,0,100,100): inset 20 gives (20,20)-(80,80); the top is lifted to
    # the 32 pixel margin and the left stays at 20 (margin 16); the far ends are
    # the last pixels, 79 in both directions
    corner = element(screen, "Corner")
    assert device.scroll("up", within=corner) == ((50, 79), (50, 32))
    assert device.scroll("left", within=corner) == ((79, 56), (20, 56))


def test_scroll_rounds_down(device, screen):
    # Offset (1,1,100,100): width and height 99, inset 19 (19.8 rounded down),
    # (20,20)-(81,81), top lifted to 32; centers (20+81)//2 and (32+81)//2;
    # far ends are the last pixels 80 and 80
    offset = element(screen, "Offset")
    assert device.scroll("up", within=offset) == ((50, 80), (50, 32))
    assert device.scroll("right", within=offset) == ((20, 56), (80, 56))


def edge_device(fake_adb, elements, display=(320, 640)):
    """A device whose one screen holds `elements`, with a dump taken."""
    FakeAndroid(
        fake_adb,
        SERIAL,
        (Screen("edge", f"{SHOP}/.Edge", elements),),
        start="edge",
        display=display,
        general_swipes=True,
    )
    device = Device(adb_server=fake_adb.address)
    return device, device.dump()


@pytest.mark.parametrize(
    ("bounds", "direction", "expected"),
    [
        # left inset 8 is lifted to the 16 pixel margin: (16,240)-(32,360)
        ((0, 200, 40, 400), "left", ((31, 300), (16, 300))),
        ((0, 200, 40, 400), "right", ((16, 300), (31, 300))),
        # right inset 312 is cut to 304: (288,240)-(304,360)
        ((280, 200, 320, 400), "left", ((303, 300), (288, 300))),
        ((280, 200, 320, 400), "right", ((288, 300), (303, 300))),
        # bottom inset 624 is cut to 608: (64,576)-(256,608)
        ((0, 560, 320, 640), "up", ((160, 607), (160, 576))),
        ((0, 560, 320, 640), "down", ((160, 576), (160, 607))),
        # top inset 16 is lifted to 32: (64,32)-(256,64)
        ((0, 0, 320, 80), "up", ((160, 63), (160, 32))),
        # a long list running below the display: visible part (0,300,320,640),
        # inset 64 and 68, giving (64,368)-(256,572)
        ((0, 300, 320, 1000), "up", ((160, 571), (160, 368))),
        ((0, 300, 320, 1000), "down", ((160, 368), (160, 571))),
    ],
)
def test_scroll_clamps_on_every_side(fake_adb, bounds, direction, expected):
    device, screen = edge_device(fake_adb, (El(bounds, text="Area"),))
    assert device.scroll(direction, within=element(screen, "Area")) == expected


def test_scroll_edge_margin_rounds_down(fake_adb):
    # display 330 wide: 5% is 16.5, floored to 16 (not 17); inset 6 is lifted
    # to 16; right inset 24 stays: (16,240)-(24,360)
    device, screen = edge_device(
        fake_adb, (El((0, 200, 30, 400), text="Area"),), display=(330, 640)
    )
    area = element(screen, "Area")
    assert device.scroll("right", within=area) == ((16, 300), (23, 300))


def _inside(point, bounds):
    """The exclusive rule: left <= x < right and top <= y < bottom."""
    left, top, right, bottom = bounds
    return left <= point[0] < right and top <= point[1] < bottom


_DIRECTIONS = ("up", "down", "left", "right")


@pytest.mark.parametrize("direction", _DIRECTIONS)
@pytest.mark.parametrize(
    "bounds", [(100, 100, 108, 300), (100, 300, 200, 308)], ids=["8_wide", "8_high"]
)
def test_scroll_in_a_narrow_element_stays_inside_it(fake_adb, bounds, direction):
    device, screen = edge_device(fake_adb, (El(bounds, text="Narrow"),))
    start, end = device.scroll(direction, within=element(screen, "Narrow"))
    for point in (start, end):
        assert _inside(point, bounds)
        assert _inside(point, (0, 0, 320, 640))
    assert start != end


@pytest.mark.parametrize(
    ("bounds", "direction", "expected"),
    [
        # 8 wide (the least that is visible): the 20% inset is 1 on each
        # side, so x runs 101..106 (right edge 107 is exclusive), center 104
        ((100, 100, 108, 300), "left", ((106, 200), (101, 200))),
        ((100, 100, 108, 300), "right", ((101, 200), (106, 200))),
        # height 200 inset 40: y 140..260, last pixel 259, x center 104
        ((100, 100, 108, 300), "up", ((104, 259), (104, 140))),
        ((100, 100, 108, 300), "down", ((104, 140), (104, 259))),
        # 8 high: inset 1, y runs 301..306 (bottom 307 exclusive), center 304;
        # width 100 inset 20: x 120..180, center 150
        ((100, 300, 200, 308), "up", ((150, 306), (150, 301))),
        ((100, 300, 200, 308), "down", ((150, 301), (150, 306))),
        ((100, 300, 200, 308), "left", ((179, 304), (120, 304))),
        ((100, 300, 200, 308), "right", ((120, 304), (179, 304))),
    ],
)
def test_scroll_in_a_narrow_element_uses_the_first_and_last_pixel(
    fake_adb, bounds, direction, expected
):
    device, screen = edge_device(fake_adb, (El(bounds, text="Narrow"),))
    assert device.scroll(direction, within=element(screen, "Narrow")) == expected


@pytest.mark.parametrize(
    "bounds",
    [
        (100, 100, 101, 300),
        (100, 300, 200, 301),
        (100, 100, 107, 300),  # 7 wide
        (100, 300, 200, 307),  # 7 high
        (313, 100, 400, 300),  # 7 pixels show at the right display edge
        (100, 633, 200, 700),  # 7 pixels show at the bottom display edge
    ],
)
def test_scroll_within_a_sliver_is_refused(fake_adb, bounds):
    device, screen = edge_device(fake_adb, (El(bounds, text="Thin"),))
    thin = element(screen, "Thin")
    before = len(fake_adb.commands)
    for direction in _DIRECTIONS:
        with pytest.raises(ElementNotVisibleError) as info:
            device.scroll(direction, within=thin)
        assert info.value.details["reason"] == "too_small"
        assert info.value.details["element"] == thin.id
    assert len(fake_adb.commands) == before


def test_scroll_endpoints_are_inside_the_area_and_the_display():
    rng = random.Random(20240611)  # noqa: S311 - a fixed seed, not security
    checked = 0
    refusals: list[str] = []
    for _ in range(120):
        width, height = rng.randint(20, 1300), rng.randint(20, 2600)
        left, top = rng.randint(-20, width), rng.randint(-20, height)
        # small sizes are common, so the thin and the clamped cases occur
        right = left + rng.choice((1, 2, 3, 4, 5, 8, 12, 30, 100, 700))
        bottom = top + rng.choice((1, 2, 3, 4, 5, 8, 12, 30, 100, 1500))
        bounds = (left, top, right, bottom)
        shown = (max(left, 0), max(top, 0), min(right, width), min(bottom, height))
        with FakeAdbServer() as server:
            device, screen = edge_device(
                server, (El(bounds, text="Area"),), display=(width, height)
            )
            areas = [(None, (0, 0, width, height))]
            if shown[2] - shown[0] >= 8 and shown[3] - shown[1] >= 8:
                areas.append((element(screen, "Area"), shown))
            for within, area in areas:
                for direction in _DIRECTIONS:
                    try:
                        start, end = device.scroll(direction, within=within)
                    except ValueError as error:
                        refusals.append(str(error))
                        continue
                    checked += 1
                    case = (bounds, width, height, direction)
                    assert start != end, case
                    for point in (start, end):
                        assert _inside(point, area), case
                        assert _inside(point, (0, 0, width, height)), case
    assert checked > 200
    assert refusals
    assert all("too small to scroll" in text for text in refusals)


def test_scroll_refuses_a_display_under_20_pixels(fake_adb):
    device, _screen = edge_device(fake_adb, (), display=(19, 640))
    before = len(fake_adb.commands)
    with pytest.raises(ValueError, match="too small to scroll"):
        device.scroll("up")
    assert len(fake_adb.commands) == before


@pytest.mark.usefixtures("screen")
def test_scroll_passes_the_duration(device, fake_adb):
    device.scroll("up", duration_ms=1200)
    assert sent_inputs(fake_adb) == ["input swipe 160 511 160 128 1200" + SUFFIX]


def test_scroll_uses_the_latest_dump_display(device, fake_adb):
    device.dump()
    device.scroll("down")
    assert sent_inputs(fake_adb) == ["input swipe 160 128 160 511 300" + SUFFIX]


@pytest.mark.parametrize("text", ["Short"])
def test_scroll_in_an_area_that_is_too_small(device, android, fake_adb, screen, text):
    # Short (0,0,100,40) is lifted to the top margin 32 and cut to 32 at the
    # bottom, which leaves no height. (Thin, 1 pixel wide, is not visible at all
    # now; see test_scroll_within_a_sliver_is_refused.)
    item = element(screen, text)
    for direction in ("up", "left"):
        no_request(
            android,
            fake_adb,
            lambda d=direction, i=item: device.scroll(d, within=i),
            ValueError,
        )
    with pytest.raises(ValueError, match="too small to scroll"):
        device.scroll("up", within=item)


def test_scroll_of_an_element_without_a_visible_part(device, android, fake_adb, screen):
    flat = element(screen, "Flat")
    before = len(fake_adb.requests)
    with pytest.raises(ElementNotVisibleError) as info:
        device.scroll("up", within=flat)
    assert info.value.details["element"] == flat.id
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize("direction", ["", "UP", "north", " up", "up ", "forward"])
@pytest.mark.usefixtures("screen")
def test_scroll_rejects_a_bad_direction(device, android, fake_adb, direction):
    no_request(android, fake_adb, lambda: device.scroll(direction), ValueError)


@pytest.mark.parametrize("direction", [None, 1, b"up", ["up"]])
@pytest.mark.usefixtures("screen")
def test_scroll_rejects_a_direction_that_is_not_a_str(
    device, android, fake_adb, direction
):
    no_request(android, fake_adb, lambda: device.scroll(direction), TypeError)


@pytest.mark.usefixtures("screen")
def test_scroll_rejects_a_bad_duration(device, android, fake_adb):
    for duration, error in ((0, ValueError), (10001, ValueError), (True, TypeError)):
        no_request(
            android,
            fake_adb,
            lambda d=duration: device.scroll("up", duration_ms=d),
            error,
        )


def test_scroll_without_a_dump(device, android, fake_adb):
    no_request(android, fake_adb, lambda: device.scroll("up"), ValueError)
    with pytest.raises(ValueError, match="dump the screen first"):
        device.scroll("up")
    with pytest.raises(ValueError, match="dump the screen first"):
        device.scroll("up", within=Element("e1", None, "X", Bounds(0, 0, 10, 10)))


def test_scroll_with_an_element_of_an_older_dump(device, android, fake_adb):
    old = element(device.dump(), "Pane")
    latest = device.dump()
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.scroll("up", within=old)
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert device.scroll("up", within=element(latest, "Pane")) == (
        (160, 259),
        (160, 140),
    )


@pytest.mark.usefixtures("screen")
def test_scroll_with_an_element_of_another_device(device, android, fake_adb):
    other = Device(adb_server=fake_adb.address)
    item = element(other.dump(), "Pane")
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="dump again"):
        device.scroll("up", within=item)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_scroll_with_a_hand_made_element(device, android, fake_adb, screen):
    made = Element("e99", None, "Pane", element(screen, "Pane").bounds)
    no_request(android, fake_adb, lambda: device.scroll("up", within=made), ValueError)


@pytest.mark.usefixtures("screen")
def test_scroll_within_must_be_an_element(device, android, fake_adb):
    no_request(
        android,
        fake_adb,
        lambda: device.scroll("up", within=(1, 2)),  # type: ignore[arg-type]
        TypeError,
    )


# --- scroll needs a measured display ---

UNKNOWN_DISPLAY = "the display size is unknown (wm size could not be read)"


WM_SIZE_OK = b"Physical size: 320x640\n"


UIAUTOMATOR_DUMP = re.compile(r"^uiautomator dump ")


def _scroll_refused_without_input(android, fake_adb, call):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match=re.escape(UNKNOWN_DISPLAY)) as caught:
        call()
    assert str(caught.value) == (
        f"{UNKNOWN_DISPLAY}, so the scroll area cannot be placed; "
        "use swipe with explicit points"
    )
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []
    assert not [r for r in fake_adb.requests[before:] if "input swipe" in r]


@pytest.mark.parametrize(
    "output",
    [b"", b"Physical size: nonsense\n", b"/system/bin/sh: wm: not found\n"],
)
def test_scroll_refuses_when_wm_size_is_unreadable(device, android, fake_adb, output):
    android.override("wm size", output)
    device.dump()
    _scroll_refused_without_input(android, fake_adb, lambda: device.scroll("up"))


def test_scroll_refuses_when_wm_size_fails(device, android, fake_adb):
    android.override("wm size", Fail("closed"))
    device.dump()
    _scroll_refused_without_input(android, fake_adb, lambda: device.scroll("up"))


def test_scroll_with_an_element_refuses_when_wm_size_is_unreadable(
    device, android, fake_adb
):
    android.override("wm size", b"")
    pane = element(device.dump(), "Pane")
    _scroll_refused_without_input(
        android, fake_adb, lambda: device.scroll("up", within=pane)
    )


def test_scroll_works_again_after_a_dump_with_a_readable_wm_size(
    device, android, fake_adb
):
    android.override("wm size", b"")
    device.dump()
    _scroll_refused_without_input(android, fake_adb, lambda: device.scroll("up"))
    android.override("wm size", WM_SIZE_OK)
    device.dump()
    assert device.scroll("up") == ((160, 511), (160, 128))
    assert len(sent_inputs(fake_adb)) == 1


def test_a_failed_dump_keeps_the_screen_and_its_flag_together(device, android):
    # measured, then a failed dump: scrolling still works on the old screen
    device.dump()
    android.override(UIAUTOMATOR_DUMP, b"ERROR: boom\n")
    with pytest.raises(UiprobeError):
        device.dump()
    assert device.scroll("up") == ((160, 511), (160, 128))


def test_a_failed_dump_after_an_unmeasured_one_keeps_scroll_refused(
    device, android, fake_adb
):
    android.override("wm size", b"")
    device.dump()
    android.override("wm size", WM_SIZE_OK)
    android.override(UIAUTOMATOR_DUMP, b"ERROR: boom\n")
    with pytest.raises(UiprobeError):
        device.dump()
    _scroll_refused_without_input(android, fake_adb, lambda: device.scroll("up"))


@pytest.mark.parametrize("extra", [(), ("--text", "Pane")])
def test_cli_swipe_direction_with_an_unknown_display_is_a_usage_error(
    capsys, fake_adb, android, extra
):
    android.override("wm size", b"")
    android.override("dumpsys window displays", b"")
    code, out, err = invoke(capsys, fake_adb, "swipe", "--direction", "up", *extra)
    assert code == 2
    assert out == ""
    assert UNKNOWN_DISPLAY in err
    assert "use swipe with explicit points" in err
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_swipe_json_with_an_unknown_display(capsys, fake_adb, android):
    android.override("wm size", b"")
    android.override("dumpsys window displays", b"")
    code, out, _ = invoke(capsys, fake_adb, "swipe", "--direction", "up", "--json")
    assert code == 2
    assert UNKNOWN_DISPLAY in json.loads(out)["error"]["message"]
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


@pytest.mark.usefixtures("android")
def test_cli_swipe_direction_in_an_element_skips_activity_and_api_level(
    capsys, fake_adb
):
    code, _, err = invoke(
        capsys, fake_adb, "swipe", "--direction", "down", "--text", "Pane"
    )
    assert (code, err) == (0, "")
    commands = [c for _, _, c in fake_adb.commands]
    assert "wm size" in commands
    assert not any(c.startswith(("dumpsys window", "getprop")) for c in commands)
