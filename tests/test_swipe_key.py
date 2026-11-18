"""Tests for ``Device.swipe``, ``Device.scroll``, ``Device.key`` and their commands.

Every device is a strict `FakeAndroid`. An autouse fixture checks after each
test that every input command the server received has one of the marker forms
that these commands and ``tap`` send. Tests that must not send anything assert
that `FakeAndroid.inputs` is empty and that the server saw no new request.
"""

from __future__ import annotations

import json
import random
import re

import pytest

from tests.fake_adb_server import Fail, FakeAdbServer, Reset
from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
    assert_no_resend_after_drop,
)
from uiprobe import cli
from uiprobe.adb import AdbClient
from uiprobe.device import (
    _KEY_CODES,
    DEVICE_WIDE_KEY_PREFIXES,
    DEVICE_WIDE_KEYS,
    KEY_ALIASES,
    Device,
)
from uiprobe.errors import (
    AdbError,
    ElementNotVisibleError,
    InputFailedError,
    UiprobeError,
)
from uiprobe.model import Bounds, Element
from uiprobe.model import Screen as ScreenModel

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
SUFFIX = "; echo uiprobe-exit:$?"
SWIPE_COMMAND = "input swipe 10 20 30 40 300" + SUFFIX
MARKER_FORMS = (
    re.compile(r"^input tap [0-9]+ [0-9]+; echo uiprobe-exit:\$\?\Z"),
    re.compile(r"^input swipe ([0-9]+) ([0-9]+) \1 \2 800; echo uiprobe-exit:\$\?\Z"),
    *FakeAndroid.SWIPE_ALLOWED,
    re.compile(r"^input keyevent KEYCODE_[A-Z0-9_]{1,40}; echo uiprobe-exit:\$\?\Z"),
)

# Display 320x640: 5% edge margins are 16 and 32 pixels.
SCREENS = (
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((10, 100, 310, 300), text="Pane", rid=f"{SHOP}:id/pane"),
            El((0, 0, 100, 100), text="Corner"),
            El((1, 1, 100, 100), text="Offset"),
            El((100, 100, 101, 300), text="Thin"),
            El((0, 0, 100, 40), text="Short"),
            El((10, 400, 100, 400), text="Flat"),
            El((200, 500, 250, 550), text="Twin", rid=f"{SHOP}:id/twin"),
            El((250, 500, 300, 550), text="Twin", rid=f"{SHOP}:id/twin"),
            El((10, 320, 150, 370), text="Off", enabled=False),
        ),
    ),
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main", general_swipes=True)


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


def sent_inputs(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands if c.startswith("input")]


@pytest.fixture(autouse=True)
def _only_marker_inputs(fake_adb: FakeAdbServer):
    """After each test, every input command received had a marker form."""
    yield
    for command in sent_inputs(fake_adb):
        assert any(form.fullmatch(command) for form in MARKER_FORMS), command


@pytest.fixture
def screen(device: Device) -> ScreenModel:
    return device.dump()


def element(screen: ScreenModel, text: str) -> Element:
    return next(e for e in screen.elements if e.text == text)


def swipe_args(android: FakeAndroid) -> list[tuple[str, ...]]:
    assert [i.kind for i in android.inputs] == ["swipe"] * len(android.inputs)
    return [i.args[1:] for i in android.inputs]


# --- Device.swipe ---


def test_swipe_sends_the_exact_command_once(device, android, fake_adb):
    assert device.swipe((10, 20), [30, 40]) == ((10, 20), (30, 40))
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]
    assert [(i.kind, i.args) for i in android.inputs] == [
        ("swipe", ("swipe", "10", "20", "30", "40", "300"))
    ]


def test_swipe_sends_the_given_duration(device, fake_adb):
    device.swipe((0, 32767), (32767, 0), duration_ms=10000)
    assert sent_inputs(fake_adb) == ["input swipe 0 32767 32767 0 10000" + SUFFIX]
    device.swipe((1, 2), (1, 3), duration_ms=1)
    assert sent_inputs(fake_adb)[-1] == "input swipe 1 2 1 3 1" + SUFFIX


def test_swipe_uses_the_int_value_of_an_int_subclass(device, fake_adb):
    class Odd(int):
        def __str__(self) -> str:
            return "7"

    assert device.swipe((Odd(10), 20), (30, Odd(40)), duration_ms=Odd(300)) == (
        (10, 20),
        (30, 40),
    )
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]


def test_a_swipe_that_does_not_move_is_not_a_press(device, android, fake_adb):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="use tap"):
        device.swipe((5, 5), (5, 5))
    with pytest.raises(ValueError, match="use tap"):
        device.swipe((5, 5), [5, 5], duration_ms=800)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


def test_the_fake_still_reads_a_marker_long_press_and_a_marker_key(device, android):
    device._shell("input swipe 1 2 1 2 800" + SUFFIX)
    device._shell("input swipe 1 2 3 4 800" + SUFFIX)
    device._shell("input keyevent KEYCODE_HOME" + SUFFIX)
    assert [(i.kind, i.args) for i in android.inputs] == [
        ("long_press", ("swipe", "1", "2", "1", "2", "800")),
        ("swipe", ("swipe", "1", "2", "3", "4", "800")),
        ("key", ("KEYCODE_HOME",)),
    ]


# --- validation: nothing reaches the server ---


def no_request(android, fake_adb, call, error):
    before = len(fake_adb.requests)
    with pytest.raises(error):
        call()
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


@pytest.mark.parametrize(
    "point",
    [True, 1.5, "1", None, (1, True), (1.0, 2), (1, "2"), (1,), (1, 2, 3), 7],
)
def test_swipe_rejects_a_bad_point_type(device, android, fake_adb, point):
    for first, second in ((point, (1, 2)), ((1, 2), point)):
        no_request(
            android,
            fake_adb,
            lambda a=first, b=second: device.swipe(a, b),
            TypeError,
        )


@pytest.mark.parametrize("point", [(-1, 5), (5, -1), (32768, 5), (5, 32768)])
def test_swipe_rejects_a_coordinate_out_of_range(device, android, fake_adb, point):
    for first, second in ((point, (1, 2)), ((1, 2), point)):
        no_request(
            android,
            fake_adb,
            lambda a=first, b=second: device.swipe(a, b),
            ValueError,
        )


@pytest.mark.parametrize("duration", [True, False, 1.5, "300", None])
def test_swipe_rejects_a_bad_duration_type(device, android, fake_adb, duration):
    no_request(
        android,
        fake_adb,
        lambda: device.swipe((1, 2), (3, 4), duration_ms=duration),
        TypeError,
    )


@pytest.mark.parametrize("duration", [0, -5, 10001])
def test_swipe_rejects_a_duration_out_of_range(device, android, fake_adb, duration):
    no_request(
        android,
        fake_adb,
        lambda: device.swipe((1, 2), (3, 4), duration_ms=duration),
        ValueError,
    )


# --- the timeout of the shell call ---


@pytest.mark.parametrize(
    ("duration", "expected"), [(300, 10.3), (5000, 15.0), (10000, 20.0)]
)
def test_swipe_read_timeout_follows_the_duration(
    device, monkeypatch, duration, expected
):
    seen: list[tuple[str, float | None]] = []
    original = AdbClient.shell

    def spy(self, serial, command, *, timeout=None):
        seen.append((command, timeout))
        return original(self, serial, command, timeout=timeout)

    monkeypatch.setattr(AdbClient, "shell", spy)
    device.swipe((1, 2), (3, 4), duration_ms=duration)
    assert len(seen) == 1
    assert seen[0][0].startswith("input swipe 1 2 3 4 ")
    assert seen[0][1] == pytest.approx(expected)


def test_key_uses_the_client_timeout(device, monkeypatch):
    seen: list[float | None] = []
    original = AdbClient.shell

    def spy(self, serial, command, *, timeout=None):
        seen.append(timeout)
        return original(self, serial, command, timeout=timeout)

    monkeypatch.setattr(AdbClient, "shell", spy)
    device.key("back")
    assert seen == [None]


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


# --- Device.key ---


@pytest.mark.parametrize(("alias", "code"), sorted(KEY_ALIASES))
def test_key_aliases(device, android, fake_adb, alias, code):
    assert device.key(alias) == code
    assert sent_inputs(fake_adb) == [f"input keyevent {code}" + SUFFIX]
    assert [(i.kind, i.args) for i in android.inputs] == [("key", (code,))]


@pytest.mark.usefixtures("device")
def test_the_alias_table():
    assert dict(KEY_ALIASES) == {
        "back": "KEYCODE_BACK",
        "home": "KEYCODE_HOME",
        "enter": "KEYCODE_ENTER",
        "delete": "KEYCODE_DEL",
        "forward_delete": "KEYCODE_FORWARD_DEL",
        "tab": "KEYCODE_TAB",
        "escape": "KEYCODE_ESCAPE",
        "menu": "KEYCODE_MENU",
        "search": "KEYCODE_SEARCH",
        "recents": "KEYCODE_APP_SWITCH",
        "up": "KEYCODE_DPAD_UP",
        "down": "KEYCODE_DPAD_DOWN",
        "left": "KEYCODE_DPAD_LEFT",
        "right": "KEYCODE_DPAD_RIGHT",
        "center": "KEYCODE_DPAD_CENTER",
        "page_up": "KEYCODE_PAGE_UP",
        "page_down": "KEYCODE_PAGE_DOWN",
        "space": "KEYCODE_SPACE",
    }


def test_key_aliases_cannot_be_changed(device, fake_adb):
    assert isinstance(KEY_ALIASES, tuple)
    with pytest.raises(TypeError):
        KEY_ALIASES[0] = ("back", "KEYCODE_HOME")  # ty: ignore[invalid-assignment]
    for alias, code in KEY_ALIASES:
        assert device.key(alias) == code
    assert sent_inputs(fake_adb) == [
        f"input keyevent {c}" + SUFFIX for _, c in KEY_ALIASES
    ]


@pytest.mark.parametrize("alias", ["BACK", "Back", "bAcK", "Page_Down", "RECENTS"])
def test_key_aliases_are_case_insensitive(device, fake_adb, alias):
    code = device.key(alias)
    assert code == dict(KEY_ALIASES)[alias.lower()]
    assert sent_inputs(fake_adb) == [f"input keyevent {code}" + SUFFIX]


@pytest.mark.parametrize(
    "name", ["KEYCODE_F1", "KEYCODE_A", "KEYCODE_" + "A" * 40, "KEYCODE_F12"]
)
def test_key_accepts_a_keycode_name(device, fake_adb, name):
    assert device.key(name) == name
    assert sent_inputs(fake_adb) == [f"input keyevent {name}" + SUFFIX]


def test_key_back_reaches_the_device(device, android):
    assert device.key("KEYCODE_BACK") == "KEYCODE_BACK"
    assert [i.kind for i in android.inputs] == ["key"]


@pytest.mark.parametrize(
    "name",
    [
        "",
        " back",
        "back ",
        "ba ck",
        "back\n",
        "KEYCODE_back",
        "keycode_back",
        "KEYCODE_",
        "KEYCODE_26",
        "KEYCODE_224",
        "KEYCODE_" + "A" * 41,
        "KEYCODE_BACK;reboot",
        "KEYCODE_BACK reboot",
        "KEYCODE_BACK$(reboot)",
        "KEYCODE_Ä",
        "volume_up",
        "ÿ",
    ],
)
def test_key_rejects_a_bad_name(device, android, fake_adb, name):
    no_request(android, fake_adb, lambda: device.key(name), ValueError)


@pytest.mark.parametrize("name", ["4", "0", "04", "66", "٤"])
def test_key_refuses_numbers_and_says_why(device, android, fake_adb, name):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="KEYCODE_") as info:
        device.key(name)
    if name.isascii():
        assert "numbers are not accepted" in str(info.value)
        assert "digits" in str(info.value)
        assert "KEYCODE_" in str(info.value)
    assert len(fake_adb.requests) == before
    assert android.inputs == []


@pytest.mark.parametrize("name", [None, 4, b"back", ["back"], ("back",), 1.5])
def test_key_rejects_a_non_str(device, android, fake_adb, name):
    no_request(android, fake_adb, lambda: device.key(name), TypeError)


# --- device-wide keys are refused ---

PREFIX_EXAMPLES = (
    "KEYCODE_MEDIA_PLAY_PAUSE",
    "KEYCODE_SYSTEM_NAVIGATION_DOWN",
    "KEYCODE_TV_INPUT_HDMI_1",
    "KEYCODE_VIDEO_APP_1",
    "KEYCODE_FEATURED_APP_4",
    "KEYCODE_DEMO_APP_2",
    "KEYCODE_STEM_3",
)
EXPECTED_PREFIXES = (
    "KEYCODE_MEDIA_",
    "KEYCODE_SYSTEM_NAVIGATION_",
    "KEYCODE_TV_",
    "KEYCODE_VIDEO_APP_",
    "KEYCODE_FEATURED_APP_",
    "KEYCODE_DEMO_APP_",
    "KEYCODE_STEM_",
)
# Written out here, independent of the module, so that deleting a name from
# DEVICE_WIDE_KEYS fails a test.
EXPECTED_REFUSED = (
    "KEYCODE_POWER",
    "KEYCODE_SLEEP",
    "KEYCODE_WAKEUP",
    "KEYCODE_SOFT_SLEEP",
    "KEYCODE_TV_POWER",
    "KEYCODE_AVR_POWER",
    "KEYCODE_STB_POWER",
    "KEYCODE_CALL",
    "KEYCODE_ENDCALL",
    "KEYCODE_HEADSETHOOK",
    "KEYCODE_VOLUME_UP",
    "KEYCODE_VOLUME_DOWN",
    "KEYCODE_VOLUME_MUTE",
    "KEYCODE_MUTE",
    "KEYCODE_BRIGHTNESS_UP",
    "KEYCODE_BRIGHTNESS_DOWN",
    "KEYCODE_SYSRQ",
    "KEYCODE_SCREENSHOT",
    "KEYCODE_NOTIFICATION",
    "KEYCODE_ASSIST",
    "KEYCODE_VOICE_ASSIST",
    "KEYCODE_ALL_APPS",
    "KEYCODE_SETTINGS",
    "KEYCODE_LANGUAGE_SWITCH",
    "KEYCODE_MANNER_MODE",
    "KEYCODE_PAIRING",
    "KEYCODE_LOCK",
    "KEYCODE_EMOJI_PICKER",
    "KEYCODE_KEYBOARD_BACKLIGHT_DOWN",
    "KEYCODE_KEYBOARD_BACKLIGHT_UP",
    "KEYCODE_KEYBOARD_BACKLIGHT_TOGGLE",
    "KEYCODE_RECENT_APPS",
    "KEYCODE_PROFILE_SWITCH",
    "KEYCODE_DO_NOT_DISTURB",
    "KEYCODE_META_LEFT",
    "KEYCODE_META_RIGHT",
    "KEYCODE_DICTATE",
    "KEYCODE_CAMERA",
    "KEYCODE_FOCUS",
    "KEYCODE_EXPLORER",
    "KEYCODE_ENVELOPE",
    "KEYCODE_CONTACTS",
    "KEYCODE_CALENDAR",
    "KEYCODE_MUSIC",
    "KEYCODE_CALCULATOR",
    "KEYCODE_BOOKMARK",
    "KEYCODE_GUIDE",
    "KEYCODE_DVR",
    "KEYCODE_TV",
    "KEYCODE_TV_INPUT",
    "KEYCODE_WINDOW",
    "KEYCODE_STYLUS_BUTTON_TAIL",
    "KEYCODE_AVR_INPUT",
    "KEYCODE_STB_INPUT",
)
REFUSED = [*EXPECTED_REFUSED, *PREFIX_EXAMPLES]


def test_the_refused_sets_are_exactly_these():
    assert frozenset(EXPECTED_REFUSED) == DEVICE_WIDE_KEYS
    assert len(EXPECTED_REFUSED) == len(set(EXPECTED_REFUSED))
    assert DEVICE_WIDE_KEY_PREFIXES == EXPECTED_PREFIXES


def test_every_prefix_has_an_example():
    for prefix in DEVICE_WIDE_KEY_PREFIXES:
        assert any(name.startswith(prefix) for name in PREFIX_EXAMPLES)


@pytest.mark.parametrize("name", REFUSED)
def test_key_refuses_a_device_wide_key(device, android, fake_adb, name):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="whole device") as info:
        device.key(name)
    assert str(info.value).startswith(f"{name} acts on the whole device")
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


@pytest.mark.parametrize("name", REFUSED)
def test_cli_refuses_a_device_wide_key(capsys, fake_adb, android, name):
    code, out, _ = invoke(capsys, fake_adb, "key", name, "--json")
    assert code == 2
    error = json.loads(out)["error"]
    assert error["code"] == "usage_error"
    assert error["message"].startswith(f"{name} acts on the whole device")
    assert fake_adb.requests == []
    assert android.inputs == []


def test_an_alias_to_a_refused_key_is_refused(monkeypatch, device, android, fake_adb):
    monkeypatch.setitem(_KEY_CODES, "off", "KEYCODE_POWER")
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="whole device"):
        device.key("off")
    assert len(fake_adb.requests) == before
    assert android.inputs == []


class _ConstantHash(str):
    __slots__ = ()

    def __hash__(self):
        return 0


class _NoStartswith(str):
    __slots__ = ()

    def startswith(self, *_args, **_kwargs):
        return False


@pytest.mark.parametrize(
    "name",
    [_ConstantHash("KEYCODE_POWER"), _NoStartswith("KEYCODE_MEDIA_PLAY")],
    ids=["constant-hash", "no-startswith"],
)
def test_a_str_subclass_cannot_bypass_the_refusal(device, android, fake_adb, name):
    before = len(fake_adb.requests)
    with pytest.raises(ValueError, match="whole device"):
        device.key(name)
    assert len(fake_adb.requests) == before
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_a_harmless_str_subclass_sends_the_plain_code(device, fake_adb):
    class Name(str):
        __slots__ = ()

    assert device.key(Name("back")) == "KEYCODE_BACK"
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_BACK" + SUFFIX]


def test_no_alias_maps_to_a_refused_key():
    for alias, code in KEY_ALIASES:
        assert code not in DEVICE_WIDE_KEYS, alias
        assert not code.startswith(DEVICE_WIDE_KEY_PREFIXES), alias


def test_device_wide_keys_cannot_be_changed():
    assert isinstance(DEVICE_WIDE_KEYS, frozenset)
    assert isinstance(DEVICE_WIDE_KEY_PREFIXES, tuple)
    assert not hasattr(DEVICE_WIDE_KEYS, "add")


@pytest.mark.parametrize(
    "name",
    [
        "KEYCODE_BACK",
        "KEYCODE_HOME",
        "KEYCODE_APP_SWITCH",
        "KEYCODE_MENU",
        "KEYCODE_SEARCH",
        "KEYCODE_ENTER",
        "KEYCODE_DEL",
        "KEYCODE_TAB",
        "KEYCODE_ESCAPE",
        "KEYCODE_SPACE",
        "KEYCODE_DPAD_UP",
        "KEYCODE_PAGE_DOWN",
        "KEYCODE_A",
        "KEYCODE_7",
    ],
)
def test_navigation_keys_stay_allowed(device, fake_adb, name):
    assert device.key(name) == name
    assert sent_inputs(fake_adb) == [f"input keyevent {name}" + SUFFIX]


# --- input_failed ---


def test_swipe_input_failed_details(device, android):
    android.answer_input(status=1, output="boom\n")
    with pytest.raises(InputFailedError) as info:
        device.swipe((10, 20), (30, 40), duration_ms=500)
    assert info.value.code == "input_failed"
    assert info.value.exit_code == 14
    assert info.value.details == {
        "command": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 500,
        "reason": "exit_status",
        "exit_status": 1,
        "output": "boom",
    }


def test_swipe_missing_marker(device, android):
    android.answer_input(marker=False)
    with pytest.raises(InputFailedError) as info:
        device.swipe((10, 20), (30, 40))
    assert info.value.details == {
        "command": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 300,
        "reason": "no_exit_status",
    }


@pytest.mark.usefixtures("screen")
def test_scroll_failure_reports_the_swipe(device, android):
    android.answer_input(output="usage: input\n")
    with pytest.raises(InputFailedError) as info:
        device.scroll("up")
    assert info.value.details == {
        "command": "swipe",
        "from": [160, 511],
        "to": [160, 128],
        "duration_ms": 300,
        "reason": "unexpected_output",
        "output": "usage: input",
    }


def test_key_input_failed_details(device, android):
    android.answer_input(status=255, output="Exception\n")
    with pytest.raises(InputFailedError) as info:
        device.key("home")
    assert info.value.details == {
        "command": "key",
        "key": "KEYCODE_HOME",
        "reason": "exit_status",
        "exit_status": 255,
        "output": "Exception",
    }


def test_key_extra_output_before_a_zero_marker(device, android):
    android.answer_input(output="hello\n")
    with pytest.raises(InputFailedError) as info:
        device.key("back")
    assert info.value.details["reason"] == "unexpected_output"
    assert len(android.inputs) == 1


# --- never resent ---


def test_swipe_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.swipe((10, 20), (30, 40))
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]
    assert android.dropped == [SWIPE_COMMAND]
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("screen")
def test_scroll_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.scroll("up")
    assert len(android.inputs) == 1
    assert len(sent_inputs(fake_adb)) == 1
    assert_no_resend_after_drop(android, fake_adb)


def test_key_no_resend_after_a_dropped_connection(device, android, fake_adb):
    android.drop_after_input(1)
    with pytest.raises(AdbError):
        device.key("home")
    assert len(android.inputs) == 1
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_HOME" + SUFFIX]
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("screen")
def test_no_resend_when_the_marker_is_missing(device, android, fake_adb):
    android.answer_input(marker=False)
    with pytest.raises(InputFailedError):
        device.swipe((10, 20), (30, 40))
    with pytest.raises(InputFailedError):
        device.key("back")
    with pytest.raises(InputFailedError):
        device.scroll("left")
    assert len(android.inputs) == 3
    assert len(sent_inputs(fake_adb)) == 3


def test_no_resend_after_a_reset_reply(device, android, fake_adb):
    android.override(re.compile(r"^input (swipe|keyevent)"), Reset())
    with pytest.raises(UiprobeError):
        device.swipe((10, 20), (30, 40))
    with pytest.raises(UiprobeError):
        device.key("back")
    assert len(sent_inputs(fake_adb)) == 2


# --- the command line ---


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_swipe_from_to(capsys, fake_adb, android):
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--from", "10", "20", "--to", "30", "40"
    )
    assert (code, err) == (0, "")
    assert out == "swiped 10,20 -> 30,40 in 300 ms\n"
    assert sent_inputs(fake_adb) == [SWIPE_COMMAND]
    assert android.dumps == 0


@pytest.mark.usefixtures("android")
def test_cli_swipe_ms(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--from", "0", "0", "--to", "5", "5", "--ms", "1500"
    )
    assert (code, out) == (0, "swiped 0,0 -> 5,5 in 1500 ms\n")
    assert sent_inputs(fake_adb) == ["input swipe 0 0 5 5 1500" + SUFFIX]


@pytest.mark.usefixtures("android")
def test_cli_swipe_json_for_points(capsys, fake_adb):
    code, out, err = invoke(
        capsys,
        fake_adb,
        "swipe",
        "--from",
        "10",
        "20",
        "--to",
        "30",
        "40",
        "--ms",
        "250",
        "--json",
    )
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 250,
    }
    assert list(json.loads(out)) == ["schema", "action", "from", "to", "duration_ms"]


def test_cli_swipe_direction_without_selectors(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "swipe", "--direction", "up")
    assert (code, err) == (0, "")
    assert out == "swiped 160,511 -> 160,128 in 300 ms\n"
    assert sent_inputs(fake_adb) == ["input swipe 160 511 160 128 300" + SUFFIX]
    assert android.dumps == 1


@pytest.mark.usefixtures("android")
def test_cli_swipe_direction_json(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "left", "--ms", "400", "--json"
    )
    assert code == 0
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "swipe",
        "from": [255, 320],
        "to": [64, 320],
        "duration_ms": 400,
        "direction": "left",
    }
    assert list(json.loads(out)) == [
        "schema",
        "action",
        "from",
        "to",
        "duration_ms",
        "direction",
    ]


@pytest.mark.usefixtures("android")
def test_cli_swipe_direction_in_an_element(capsys, fake_adb):
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--direction", "down", "--text", "Pane"
    )
    assert (code, err) == (0, "")
    assert out == (
        'swiped 160,140 -> 160,259 in 300 ms on e2 TextView "Pane" id=pane\n'
    )
    assert sent_inputs(fake_adb) == ["input swipe 160 140 160 259 300" + SUFFIX]


@pytest.mark.usefixtures("android")
def test_cli_swipe_direction_in_an_element_json(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--id", "pane", "--json"
    )
    assert code == 0
    payload = json.loads(out)
    assert payload == {
        "schema": "uiprobe.action/1",
        "action": "swipe",
        "from": [160, 259],
        "to": [160, 140],
        "duration_ms": 300,
        "direction": "up",
        "element": {
            "id": "e2",
            "parent": "e1",
            "class": "android.widget.TextView",
            "text": "Pane",
            "resource_id": f"{SHOP}:id/pane",
            "bounds": [10, 100, 310, 300],
            "center": [160, 200],
        },
    }
    assert list(payload) == [
        "schema",
        "action",
        "from",
        "to",
        "duration_ms",
        "direction",
        "element",
    ]


def test_cli_swipe_no_match_sends_nothing(capsys, fake_adb, android):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Nope", "--json"
    )
    assert code == 12
    assert json.loads(out)["error"]["code"] == "element_not_found"
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_swipe_several_matches_send_nothing(capsys, fake_adb, android):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Twin", "--json"
    )
    assert code == 13
    assert json.loads(out)["error"]["code"] == "multiple_elements"
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_swipe_invisible_element_is_not_found(capsys, fake_adb, android):
    code, _, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Flat"
    )
    assert code == 12
    assert android.inputs == []


def test_cli_swipe_area_too_small_is_a_usage_error_and_sends_nothing(
    capsys, fake_adb, android
):
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Short", "--json"
    )
    assert code == 2
    assert "too small to scroll" in json.loads(out)["error"]["message"]
    assert android.inputs == []


def test_cli_swipe_in_a_sole_sliver_is_too_small_and_sends_nothing(
    capsys, fake_adb, android
):
    # Thin is 1 pixel wide and the only match: found, then refused as too small.
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Thin", "--json"
    )
    assert code == 12
    error = json.loads(out)["error"]
    assert error["code"] == "element_not_visible"
    assert error["details"]["reason"] == "too_small"
    assert android.inputs == []


def test_cli_swipe_disabled_element_warns_on_stderr_only(capsys, fake_adb, android):
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--direction", "up", "--text", "Off"
    )
    assert code == 0
    assert out.startswith("swiped ")
    assert "warning" not in out
    assert err == "uiprobe: warning: e10 is disabled; scrolling it anyway\n"
    assert len(android.inputs) == 1


@pytest.mark.parametrize(
    "argv",
    [
        ("--from", "1", "2"),
        ("--to", "1", "2"),
        ("--from", "1", "2", "--to", "3", "4", "--direction", "up"),
        ("--from", "1", "2", "--to", "3", "4", "--text", "Pane"),
        ("--text", "Pane"),
        ("--id", "pane"),
        ("--desc", "x"),
        (),
        ("--ms", "300"),
        ("--direction", "north"),
        ("--direction", "UP"),
        ("--direction", ""),
        ("--direction", "up", "--ms", "0"),
        ("--direction", "up", "--ms", "10001"),
        ("--direction", "up", "--ms", "-5"),
        ("--direction", "up", "--ms", "1.5"),
        ("--direction", "up", "--ms", "+5"),
        ("--direction", "up", "--ms", "0x10"),
        ("--direction", "up", "--ms", "1e3"),
        ("--direction", "up", "--ms", ""),
        ("--direction", "up", "--ms", " 5"),
        ("--direction", "up", "--ms", "007x"),
        ("--direction", "up", "--ms", "\u0665"),
        ("--from", "1", "2", "--to", "1", "2"),
        ("--from", "1", "2", "--to", "01", "002"),
        ("--from", "-1", "2", "--to", "3", "4"),
        ("--from", "1", "2", "--to", "32768", "4"),
        ("--from", "1", "2", "--to", "3", "1.5"),
        ("--from", "1", "2", "--to", "3"),
        ("--from", "1", "--to", "3", "4"),
        ("--from", "1", "2", "--to", "3", "4", "--ms", "0"),
    ],
)
def test_cli_swipe_usage_errors_do_not_contact_the_server(
    capsys, fake_adb, android, argv
):
    code, out, _ = invoke(capsys, fake_adb, "swipe", *argv, "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_cli_swipe_accepts_the_edges(capsys, fake_adb):
    code, out, _ = invoke(
        capsys,
        fake_adb,
        "swipe",
        "--from",
        "0",
        "32767",
        "--to",
        "32767",
        "0",
        "--ms",
        "10000",
    )
    assert (code, out) == (0, "swiped 0,32767 -> 32767,0 in 10000 ms\n")


def test_cli_swipe_input_failed(capsys, fake_adb, android):
    android.answer_input(status=1, output="boom\n")
    code, out, err = invoke(
        capsys, fake_adb, "swipe", "--from", "10", "20", "--to", "30", "40", "--json"
    )
    assert (code, err) == (14, "")
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.error/1"
    assert payload["error"]["code"] == "input_failed"
    assert payload["error"]["details"] == {
        "command": "swipe",
        "from": [10, 20],
        "to": [30, 40],
        "duration_ms": 300,
        "reason": "exit_status",
        "exit_status": 1,
        "output": "boom",
    }
    assert len(android.inputs) == 1


def test_cli_swipe_does_not_resend_after_a_drop(capsys, fake_adb, android):
    android.drop_after_input(1)
    code, _, _ = invoke(
        capsys, fake_adb, "swipe", "--from", "10", "20", "--to", "30", "40", "--json"
    )
    assert code == 10
    assert len(android.inputs) == 1
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("android")
def test_cli_swipe_ctrl_c_is_130(capsys, fake_adb, monkeypatch):
    def interrupt(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Device, "swipe", interrupt)
    code, out, _ = invoke(
        capsys, fake_adb, "swipe", "--from", "1", "2", "--to", "3", "4", "--json"
    )
    assert code == 130
    assert json.loads(out)["error"]["code"] == "interrupted"


def test_cli_key_with_an_alias(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "key", "back")
    assert (code, err) == (0, "")
    assert out == "pressed key KEYCODE_BACK\n"
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_BACK" + SUFFIX]
    assert [(i.kind, i.args) for i in android.inputs] == [("key", ("KEYCODE_BACK",))]


@pytest.mark.usefixtures("android")
def test_cli_key_alias_is_case_insensitive(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "key", "HOME")
    assert (code, out) == (0, "pressed key KEYCODE_HOME\n")


@pytest.mark.usefixtures("android")
def test_cli_key_with_a_keycode_name_json(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "key", "KEYCODE_F1", "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "schema": "uiprobe.action/1",
        "action": "key",
        "key": "KEYCODE_F1",
    }
    assert list(json.loads(out)) == ["schema", "action", "key"]
    assert sent_inputs(fake_adb) == ["input keyevent KEYCODE_F1" + SUFFIX]


@pytest.mark.parametrize(
    "name",
    [
        "4",
        "0",
        "",
        " back",
        "keycode_back",
        "KEYCODE_back",
        "KEYCODE_" + "A" * 41,
        "KEYCODE_BACK;reboot",
        "nonsense",
    ],
)
def test_cli_key_usage_errors_do_not_contact_the_server(
    capsys, fake_adb, android, name
):
    code, out, _ = invoke(capsys, fake_adb, "key", name, "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_cli_key_without_a_name_is_a_usage_error(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "key", "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_cli_key_number_message_says_why(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "key", "4", "--json")
    assert code == 2
    message = json.loads(out)["error"]["message"]
    assert "numbers are not accepted" in message
    assert "KEYCODE_" in message


def test_cli_key_input_failed(capsys, fake_adb, android):
    android.answer_input(status=1, output="boom\n")
    code, out, err = invoke(capsys, fake_adb, "key", "home", "--json")
    assert (code, err) == (14, "")
    payload = json.loads(out)
    assert payload["error"]["code"] == "input_failed"
    assert payload["error"]["details"] == {
        "command": "key",
        "key": "KEYCODE_HOME",
        "reason": "exit_status",
        "exit_status": 1,
        "output": "boom",
    }
    assert len(android.inputs) == 1


def test_cli_key_does_not_resend_after_a_drop(capsys, fake_adb, android):
    android.drop_after_input(1)
    code, _, _ = invoke(capsys, fake_adb, "key", "home", "--json")
    assert code == 10
    assert len(android.inputs) == 1
    assert_no_resend_after_drop(android, fake_adb)


@pytest.mark.usefixtures("android")
def test_cli_key_ctrl_c_is_130(capsys, fake_adb, monkeypatch):
    def interrupt(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(Device, "key", interrupt)
    code, out, _ = invoke(capsys, fake_adb, "key", "back", "--json")
    assert code == 130
    assert json.loads(out)["error"]["code"] == "interrupted"


@pytest.mark.parametrize("command", ["swipe", "key"])
def test_cli_help_says_the_foreground_app_is_not_checked(capsys, command):
    assert cli.run([command, "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "does not check which app is in front" in text


def test_cli_swipe_help_mentions_system_gestures(capsys):
    assert cli.run(["swipe", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "system gestures" in text
    assert "never resent" in text


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
    code, out, err = invoke(capsys, fake_adb, "swipe", "--direction", "up", *extra)
    assert code == 2
    assert out == ""
    assert UNKNOWN_DISPLAY in err
    assert "use swipe with explicit points" in err
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []


def test_cli_swipe_json_with_an_unknown_display(capsys, fake_adb, android):
    android.override("wm size", b"")
    code, out, _ = invoke(capsys, fake_adb, "swipe", "--direction", "up", "--json")
    assert code == 2
    assert UNKNOWN_DISPLAY in json.loads(out)["error"]["message"]
    assert android.inputs == []
    assert sent_inputs(fake_adb) == []
