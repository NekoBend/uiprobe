"""Tests for the window a crawl's tap would reach (fake adb server and device)."""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import FakeAndroid
from tests.test_crawl_driver import SCREENS, SERIAL, SHOP, TRANSPORT, Clock, lines, run
from tests.test_inputwindows import DISPLAY1
from tests.test_repin import Waits, listing, renumber
from uiprobe.crawl import Limits
from uiprobe.device import EXIT_MARKER, Device
from uiprobe.guards import Policy
from uiprobe.packageuid import PackageUid

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

BROWSE = (160, 220)  # the point the first tap of the fixture presses
OVERLAY = "com.example.chathead"


@pytest.fixture(params=[24, 31, 36], ids=["api24", "api31", "api36"])
def android(fake_adb: FakeAdbServer, request: pytest.FixtureRequest) -> FakeAndroid:
    return FakeAndroid(
        fake_adb,
        SERIAL,
        SCREENS,
        start="home",
        transport_id=TRANSPORT,
        api=request.param,
    )


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL  # the fake device answers first
    return Device(adb_server=fake_adb.address, pin=True)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _taps(android: FakeAndroid) -> list[tuple[int, int] | None]:
    return [i.point for i in android.inputs if i.kind == "tap"]


def test_window_at_reads_each_form(device, android):
    android.overlays.append(("ChatHead", OVERLAY, (140, 200, 180, 240)))
    found = device.window_at(BROWSE)
    assert not isinstance(found, str)
    assert (found.title, found.owner_uid) == ("ChatHead", android.uid_of(OVERLAY))
    found = device.window_at((10, 400))
    assert not isinstance(found, str)
    assert found.owner_uid == android.uid_of("com.example.launcher")
    assert device.package_uid(SHOP) == PackageUid(android.uid_of(SHOP), shared=False)


def test_an_overlay_over_the_tap_point_refuses_the_tap(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # A window of another app takes touches right where the crawl would tap;
    # the app's window still has the focus.
    original = android.show

    def show_then_cover(name: str) -> None:
        original(name)
        if name == "main":
            android.overlays.append(("ChatHead", OVERLAY, (0, 180, 320, 260)))

    monkeypatch.setattr(android, "show", show_then_cover)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    records = lines(out / "transitions.jsonl")
    covered = [r for r in records if r.get("reason") == "covered_by_other"]
    assert covered
    assert covered[0]["details"] == {
        "window": "ChatHead",
        "owner_uid": android.uid_of(OVERLAY),
    }
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["check_tap_window"] is True


def test_an_overlay_elsewhere_does_not_stop_the_tap(
    tmp_path: Path, device, android, clock
):
    android.overlays.append(("FloatingMenu", OVERLAY, (272, 434, 320, 482)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)
    records = lines(out / "transitions.jsonl")
    assert not [r for r in records if r.get("reason") == "covered_by_other"]


def test_the_check_can_be_turned_off(tmp_path: Path, device, android, clock, fake_adb):
    android.overlays.append(("ChatHead", OVERLAY, (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(
        device,
        clock,
        out,
        limits=Limits(actions=3),
        policy=Policy(check_tap_window=False),
    )
    assert BROWSE in _taps(android)  # the tap went to the overlay's area
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["check_tap_window"] is False
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]


@pytest.mark.parametrize(
    "problem", ["no_list", "line_format", "region_format", "no_window"]
)
def test_a_window_that_cannot_be_read_refuses_the_tap(
    tmp_path: Path, device, android, clock, monkeypatch, problem
):
    monkeypatch.setattr(device, "window_at", lambda _point, **_kw: problem)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    records = lines(out / "transitions.jsonl")
    unknown = [r for r in records if r.get("reason") == "cover_unknown"]
    assert unknown
    assert unknown[0]["details"] == {"unreadable": problem}


def test_the_record_names_the_rule_that_refused_the_list(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # A read field twice in the top row (text a window title could hold):
    # the list is line_format, and the end line says which check refused.
    original = android._dumpsys_input

    def owner_twice() -> str:
        return original().replace("ownerUid=", "ownerUid=1, ownerUid=", 1)

    monkeypatch.setattr(android, "_dumpsys_input", owner_twice)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] == {"unreadable": "line_format", "rule": "field_twice"}
    assert (result.cover_unknown, result.cover_rule) == ("line_format", "field_twice")


def test_an_unknown_line_after_a_row_is_recorded_as_such(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # A line after the top row that is no transform line: on every form the
    # record names that line's rule, not transform_mismatch (API 24 rows have
    # no transform lines, so the rows differ in having lines after them).
    original = android._dumpsys_input

    def note_after_top_row() -> str:
        lines = original().split("\n")
        top = next(i for i, line in enumerate(lines) if line.strip().startswith("0: "))
        lines.insert(top + 1, " " * 8 + "vendor note")
        return "\n".join(lines)

    monkeypatch.setattr(android, "_dumpsys_input", note_after_top_row)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] == {
        "unreadable": "line_format",
        "rule": "unknown_line",
    }


@pytest.mark.usefixtures("_digit_limit")
def test_a_huge_rotation_after_a_row_is_refused_not_raised(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # A transform line with a rotation of thousands of digits after the top
    # row: the gate records a refusal (API 24 rows have no transform lines,
    # so the rows differ; on the other forms the extra line is unknown).
    original = android._dumpsys_input
    huge = " " * 8 + "transform (ROT_" + "1" * 4301 + ") (IDENTITY)"

    def huge_rotation() -> str:
        lines = original().split("\n")
        top = next(i for i, line in enumerate(lines) if line.strip().startswith("0: "))
        lines.insert(top + 1, huge)
        return "\n".join(lines)

    monkeypatch.setattr(android, "_dumpsys_input", huge_rotation)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] in (
        {"unreadable": "line_format", "rule": "transform_mismatch"},
        {"unreadable": "line_format", "rule": "unknown_line"},
    )


def test_an_app_uid_that_cannot_be_read_refuses_every_tap(
    tmp_path: Path, device, android, clock, monkeypatch
):
    monkeypatch.setattr(device, "package_uid", lambda _package: None)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    records = lines(out / "transitions.jsonl")
    unknown = [r for r in records if r.get("reason") == "cover_unknown"]
    assert unknown[0]["details"] == {"unreadable": "target_uid"}


def test_an_app_with_a_system_uid_needs_its_package_in_the_window_name(
    tmp_path: Path, device, android, clock
):
    # Settings-like: the app runs as uid 1000, which a system window over the
    # point shares. The window's name must name the app's package as well.
    android.uids[SHOP] = 1000
    android.uids["android"] = 1000
    android.overlays.append(("KeyguardScrim", "android", (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    records = lines(out / "transitions.jsonl")
    covered = [r for r in records if r.get("reason") == "covered_by_other"]
    assert covered[0]["details"] == {"window": "KeyguardScrim", "owner_uid": 1000}


def test_an_app_with_a_system_uid_taps_its_own_window(
    tmp_path: Path, device, android, clock
):
    android.uids[SHOP] = 1000
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)


def test_package_uid_reads_the_package_stanza(device, android):
    # The fake prints another package's stanza first and, for a shared uid, a
    # "Shared users:" section after; userId= below API 34, appId= from it.
    android.uids[SHOP] = 1000
    android.uids[SHOP + "x"] = 10123
    android.shared_users[SHOP] = "android.uid.system"
    assert device.package_uid(SHOP) == PackageUid(1000, shared=True)
    android.shared_users.clear()
    android.uids[SHOP] = 10500
    assert device.package_uid(SHOP) == PackageUid(10500, shared=False)


def _covered(out: Path) -> list[dict[str, object]]:
    return [r for r in lines(out / "transitions.jsonl") if r.get("not_sent")]


def test_a_package_sharing_an_app_uid_is_not_the_app(
    tmp_path: Path, device, android, clock
):
    # Two packages share the ordinary uid 10500 (android:sharedUserId); a
    # window of the other one over the point has the app's uid but does not
    # name the app.
    helper = "com.example.shophelper"
    for package in (SHOP, helper):
        android.uids[package] = 10500
        android.shared_users[package] = "com.example.shared"
    android.overlays.append(("HelperBubble", helper, (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    refused = _covered(out)
    assert refused[0]["reason"] == "covered_by_other"
    assert refused[0]["details"] == {"window": "HelperBubble", "owner_uid": 10500}


def test_an_app_sharing_its_uid_taps_its_own_window(
    tmp_path: Path, device, android, clock
):
    android.uids[SHOP] = 10500
    android.shared_users[SHOP] = "com.example.shared"
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)  # its window's title is SHOP/...


def test_a_system_uid_window_of_a_longer_package_name_is_not_the_app(
    tmp_path: Path, device, android, clock
):
    # Both run as uid 1000; the other package's name begins with the app's.
    longer = SHOP + "x"
    for package in (SHOP, longer):
        android.uids[package] = 1000
        android.shared_users[package] = "android.uid.system"
    title = f"{longer}/{longer}.Bubble"
    android.overlays.append((title, longer, (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert BROWSE not in _taps(android)
    refused = _covered(out)
    assert refused[0]["details"] == {"window": title, "owner_uid": 1000}


@pytest.mark.parametrize(
    ("orientation", "rule"),
    [(1, "rotated"), (3, "rotated"), (None, "viewport_missing")],
)
def test_a_rotated_or_unknown_display_refuses_every_tap(
    tmp_path: Path, device, android, clock, orientation, rule
):
    android.orientation = orientation
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert {r["reason"] for r in refused} == {"cover_unknown"}
    # The record names the rule that refused next to the reason.
    assert refused[0]["details"] == {"unreadable": "rotation", "rule": rule}


# Python's default limit of digits `int` converts (`_digit_limit` sets it):
# one more digit raises ValueError. All zeros: read as 0 it would pass.
LIMIT = 4300
OVERLONG = "0" * (LIMIT + 1)
LONGEST_ZERO = "0" * LIMIT  # still converts: read as 0, as before


@pytest.fixture
def _digit_limit() -> Iterator[None]:
    """Use Python's default int-to-str digit limit, whatever the host set."""
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(LIMIT)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(before)


_IDENTITY_ROW = "\n      transform (ROT_0) (IDENTITY)\n"


def _row_rotation(text: str, digits: str) -> str:
    """The first row's transform (API 31 on) saying ``ROT_<digits>``."""
    return text.replace(
        _IDENTITY_ROW, f"\n      transform (ROT_{digits}) (IDENTITY)\n", 1
    )


def _viewport_display(text: str, digits: str) -> str:
    """A copy of the Viewport line above it naming display `digits` (the real
    line still says display 0, orientation 0)."""
    line = next(line for line in text.split("\n") if "Viewport" in line)
    copy = line.replace("displayId=0,", f"displayId={digits},")
    return text.replace(line, f"{copy}\n{line}", 1)


def _display_orientation(text: str, digits: str) -> str:
    """The first row with ``displayOrientation=<digits>``."""
    return text.replace(", ownerUid=", f", displayOrientation={digits}, ownerUid=", 1)


def _display1(text: str, rotation: str, field: str) -> str:
    """API 36: the list of display 1 (`DISPLAY1`) above display 0's, its
    row's transform saying ``ROT_<rotation>`` and `field` added to its row."""
    block = DISPLAY1.replace(
        "      transform (ROT_0) (IDENTITY)\n",
        f"      transform (ROT_{rotation}) (IDENTITY)\n",
    ).replace(", ownerUid=10999", f"{field}, ownerUid=10999")
    return text.replace("  Display: 0\n", block + "  Display: 0\n", 1)


def _display1_rotation(text: str, digits: str) -> str:
    return _display1(text, digits, "")


def _display1_orientation(text: str, digits: str) -> str:
    return _display1(text, "0", f", displayOrientation={digits}")


def _skipped_row_rotation(text: str, digits: str) -> str:
    """API 31 (one list of every display in the fake): the last row (the
    wallpaper) on display 1, its transform saying ``ROT_<digits>``."""
    lines = text.split("\n")
    row = max(i for i, line in enumerate(lines) if "ImageWallpaper" in line)
    assert lines[row + 1] == "      transform (ROT_0) (IDENTITY)"
    lines[row] = lines[row].replace("displayId=0,", "displayId=1,")
    lines[row + 1] = f"      transform (ROT_{digits}) (IDENTITY)"
    return "\n".join(lines)


_EDITS = [
    *[(api, "row_rotation", _row_rotation) for api in (31, 36)],
    *[(api, "viewport_display", _viewport_display) for api in (24, 31, 36)],
    *[(api, "display_orientation", _display_orientation) for api in (24, 31, 36)],
    (36, "display1_rotation", _display1_rotation),
    (36, "display1_orientation", _display1_orientation),
    (31, "skipped_row_rotation", _skipped_row_rotation),
]
_OTHER_DISPLAY = {"display1_rotation", "display1_orientation", "skipped_row_rotation"}
_REFUSED = [
    pytest.param(api, edit, OVERLONG, id=f"{name}-api{api}-overlong")
    for api, name, edit in _EDITS
]
# A Viewport line of display 0 must be of the known form, whose displayId= has
# at most 10 digits: 4300 zeros there refused before too, so the control
# there is display 1 written with 4300 digits (another display: not read).
_PASSED = [
    pytest.param(
        api,
        edit,
        "0" * (LIMIT - 1) + "1" if name == "viewport_display" else LONGEST_ZERO,
        id=f"{name}-api{api}-longest",
    )
    for api, name, edit in _EDITS
] + [
    pytest.param(api, edit, "1", id=f"{name}-api{api}-one")
    for api, name, edit in _EDITS
    if name in _OTHER_DISPLAY
]


def _edited(android, monkeypatch, edit: Callable[[str, str], str], digits: str) -> None:
    """Make the fake print `dumpsys input` with `edit` applied."""
    original = android._dumpsys_input
    assert edit(original(), digits) != original()
    monkeypatch.setattr(android, "_dumpsys_input", lambda: edit(original(), digits))


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize(("android", "edit", "digits"), _REFUSED, indirect=["android"])
def test_a_number_too_long_to_read_in_the_window_list_refuses_every_tap(
    tmp_path: Path, device, android, clock, monkeypatch, edit, digits
):
    # On display 0 or on another display: such a number could be 0 or
    # display 0 (leading zeros), so it is never discarded.
    _edited(android, monkeypatch, edit, digits)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert {r["reason"] for r in refused} == {"cover_unknown"}
    assert refused[0]["details"] == {
        "unreadable": "rotation",
        "rule": "number_too_long",
    }


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize(("android", "edit", "digits"), _PASSED, indirect=["android"])
def test_a_readable_number_in_the_window_list_taps_as_before(
    tmp_path: Path, device, android, clock, monkeypatch, edit, digits
):
    # 4300 digits still read as before; another display's readable rotation
    # does not matter for display 0.
    _edited(android, monkeypatch, edit, digits)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)
    assert not _covered(out)


@pytest.mark.usefixtures("_digit_limit")
def test_a_shared_user_number_too_long_to_read_refuses_every_tap(
    tmp_path: Path, device, android, clock, monkeypatch, fake_adb
):
    android.uids[SHOP] = 10500
    android.shared_users[SHOP] = "com.example.shared"
    original = android._dumpsys_package

    def overlong(name: str) -> str:
        head, _, shared = original(name).partition("Shared users:")
        assert "=10500" in shared
        return head + "Shared users:" + shared.replace("=10500", f"={OVERLONG}")

    monkeypatch.setattr(android, "_dumpsys_package", overlong)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] == {"unreadable": "target_uid"}
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]


def test_a_device_away_during_the_window_read_is_device_reconnected(
    tmp_path: Path, device, android, clock, fake_adb, monkeypatch
):
    # The device drops out while the gate reads the window at the point and
    # comes back on its number, proven the same; the window read then shows
    # another app's window there. The pin moved during the gate, so the tap
    # is refused as device_reconnected, without the cover's details, and is
    # not sent.
    original = device.window_at
    first: list[tuple[int, int]] = []

    def away_once(point: tuple[int, int], **kwargs: object) -> object:
        if not first:
            first.append(point)
            x, y = point
            android.overlays.append(("ChatHead", OVERLAY, (x - 1, y - 1, x + 1, y + 1)))
            Waits(device, [renumber(fake_adb, TRANSPORT)])
            listing(fake_adb, (TRANSPORT, "offline"))
        return original(point, **kwargs)

    monkeypatch.setattr(device, "window_at", away_once)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=4))
    assert device.reconnects == 1
    assert first
    assert first[0] not in _taps(android)
    refused = _covered(out)
    assert refused[0]["reason"] == "device_reconnected"
    assert "details" not in refused[0]


# --- round 2: the uid read again, the focused window's user, the command ---


def test_an_app_uid_that_changes_during_the_run_refuses_the_tap(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # The app is installed again under another uid after the run began: the
    # uid read right before the window no longer matches the first one.
    original = device.package_uid
    reads = [0]

    def changed(package: str) -> object:
        reads[0] += 1
        if reads[0] > 1:
            android.uids[SHOP] = 10777
        return original(package)

    monkeypatch.setattr(device, "package_uid", changed)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] == {"unreadable": "target_uid"}
    assert reads[0] >= 2


def test_an_app_uid_that_can_no_longer_be_read_refuses_the_tap(
    tmp_path: Path, device, android, clock, monkeypatch
):
    original = device.package_uid
    reads = [0]

    def unreadable(package: str) -> object:
        reads[0] += 1
        return original(package) if reads[0] == 1 else None

    monkeypatch.setattr(device, "package_uid", unreadable)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    assert _covered(out)[0]["details"] == {"unreadable": "target_uid"}


def test_the_uid_is_read_again_before_each_window_read(
    tmp_path: Path, device, clock, fake_adb
):
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    commands = [c for _, _, c in fake_adb.commands]
    reads = [i for i, c in enumerate(commands) if c == "dumpsys input"]
    assert reads
    for index in reads:
        assert commands[index - 1] == f"dumpsys package {SHOP}"


def test_a_focused_window_of_another_user_refuses_every_tap(
    tmp_path: Path, device, android, clock, fake_adb
):
    # The app is in front for user 10 (a work profile): its windows there
    # have another uid, so nothing proves the window at the point is its.
    android.focus_user = 10
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] == {"unreadable": "target_user"}
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]


def test_the_crawl_taps_with_a_10_ms_swipe_from_the_point_to_itself(
    tmp_path: Path, device, android, clock, fake_adb
):
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)
    x, y = BROWSE
    sent = [c for _, _, c in fake_adb.commands if c.startswith("input ")]
    assert f"input swipe {x} {y} {x} {y} 10; echo {EXIT_MARKER}$?" in sent
    assert not [c for c in sent if c.startswith("input tap")]


def test_a_rotated_dump_refuses_the_tap_even_with_viewport_0(
    tmp_path: Path, device, android, clock, fake_adb
):
    # The input reader says orientation 0, but the gate's own dump says the
    # hierarchy is turned once: the tap is refused before the window read.
    android.dump_rotation = 1
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert {r["reason"] for r in refused} == {"cover_unknown"}
    assert refused[0]["details"] == {"unreadable": "rotation"}
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]
    # The window list was not read, so there is no reader rule to diagnose.
    assert (result.cover_unknown, result.cover_rule) == ("rotation", None)


def test_a_focused_window_whose_owner_uid_is_of_another_user_refuses(
    tmp_path: Path, device, android, clock, fake_adb
):
    # The record says u0, but the window's owner uid is user 10's
    # (10 * 100000 + 10150): the user is not known, so no tap goes out.
    android.focus_owner_uid = 1010150
    focus = device.focused_window()
    assert focus is not None
    assert focus.user == -1
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    assert _covered(out)[0]["details"] == {"unreadable": "target_user"}
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]


def test_a_focused_window_owned_by_another_uid_refuses(
    tmp_path: Path, device, android, clock, fake_adb
):
    # The focused window names the app's package but its owner uid (user 0)
    # is another app's: the uid read from dumpsys package is not the one the
    # app's window has, so the window at the point cannot be judged by it.
    android.focus_owner_uid = android.uid_of(SHOP) + 1
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert _taps(android) == []
    assert _covered(out)[0]["details"] == {"unreadable": "target_uid"}
    assert not [c for _, _, c in fake_adb.commands if c == "dumpsys input"]


def test_an_apostrophe_in_the_app_window_name_still_names_the_app(
    tmp_path: Path, device, android, clock
):
    # A system-uid app's own window over the point, titled with an apostrophe
    # (quoted on API 24 and 31, bare on 36): its package prefix is read.
    android.uids[SHOP] = 1000
    android.shared_users[SHOP] = "android.uid.system"
    android.overlays.append((f"{SHOP}/Today's Orders", SHOP, (0, 180, 320, 260)))
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)
    assert not _covered(out)


def test_braces_in_the_focused_title_do_not_stop_the_tap(
    tmp_path: Path, device, android, clock
):
    android.focus_window = ("Order {7}", SHOP)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=3))
    assert BROWSE in _taps(android)
    assert not _covered(out)


# --- samsungFlags=0x1 (the Samsung form): read only on the app's window ---


def _samsung_rows(text: str, flagged: Callable[[str], bool]) -> str:
    """`text` (an API 36 form list) with the Samsung fields added to every
    window row, ``samsungFlags=0x1`` on the rows `flagged` picks and 0x0 on
    the others."""
    out = []
    for line in text.split("\n"):
        if ", inputConfig=" in line and ", ownerUid=" in line:
            value = "0x1" if flagged(line) else "0x0"
            line = re.sub(r"(, inputConfig=[^,]*)", rf"\1, samsungFlags={value}", line)
            line = re.sub(
                r"(, touchableRegion=(?:<empty>|\[[^\]]*\]\[[^\]]*\]))",
                r"\1, pTouchableRegion=<empty>, 1HXY=[0, 0, 0]",
                line,
            )
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize(
    ("flagged", "tapped", "uid"),
    [
        (SHOP, True, None),  # the app's own window, at the point
        (SHOP, True, 1000),  # a system uid: its title names the app
        ("StatusBar", False, None),  # another app's window, not at the point
        ("ChatHead", False, None),  # another app's window at the point
    ],
)
def test_samsung_flags_0x1_is_read_only_on_the_apps_window(
    tmp_path: Path, device, android, clock, monkeypatch, flagged, tapped, uid
):
    if android.api < 33:
        pytest.skip("the Samsung form is of API 36")
    if uid is not None:
        android.uids[SHOP] = uid
    original = android.show

    def show_then_cover(name: str) -> None:
        original(name)
        if name == "main" and flagged == "ChatHead":
            android.overlays.append(("ChatHead", OVERLAY, (0, 180, 320, 260)))

    monkeypatch.setattr(android, "show", show_then_cover)
    dumped = android._dumpsys_input
    monkeypatch.setattr(
        android,
        "_dumpsys_input",
        lambda: _samsung_rows(dumped(), lambda line: f" {flagged}" in line),
    )
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=3))
    if tapped:
        assert BROWSE in _taps(android)
        assert result.cover_unknown is None
        assert not _covered(out)
        return
    assert BROWSE not in _taps(android)
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] == {
        "unreadable": "line_format",
        "rule": "samsung_flags",
    }


@pytest.mark.parametrize("flagged", [True, False], ids=["0x1", "0x0"])
@pytest.mark.parametrize("shared", [False, True], ids=["system_uid", "shared_uid"])
def test_samsung_flags_0x1_on_an_off_point_window_of_the_apps_uid_is_refused(
    tmp_path: Path, device, android, clock, monkeypatch, shared, flagged
):
    # A window with the app's uid but another title, away from the point,
    # prints 0x1: the title test says it is not the app's, so no tap goes out.
    if android.api < 33:
        pytest.skip("the Samsung form is of API 36")
    other = "com.example.shophelper" if shared else "android"
    uid = 10500 if shared else 1000
    for package in (SHOP, other):
        android.uids[package] = uid
        if shared:
            android.shared_users[package] = "com.example.shared"
    android.overlays.append(("HelperPopup", other, (272, 434, 320, 482)))
    dumped = android._dumpsys_input
    monkeypatch.setattr(
        android,
        "_dumpsys_input",
        lambda: _samsung_rows(
            dumped(), lambda line: flagged and " HelperPopup," in line
        ),
    )
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=3))
    if not flagged:  # the same window with 0x0 does not stop the tap
        assert BROWSE in _taps(android)
        return
    assert _taps(android) == []
    refused = _covered(out)
    assert refused
    assert refused[0]["details"] == {
        "unreadable": "line_format",
        "rule": "samsung_flags",
    }
    assert (result.cover_unknown, result.cover_rule) == ("line_format", "samsung_flags")
