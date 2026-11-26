"""The pressing crawl with ``Policy.also_target``: pressing in a listed package,
every guard on its screens, the refusals before anything is sent, and the
records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.crawl_driver.helpers import (
    OTHER,
    SERIAL,
    SHOP,
    TRANSPORT,
    Clock,
    button,
    commands,
    lines,
    run,
)
from tests.fake_device import (
    PERMISSION_PACKAGE,
    El,
    FakeAndroid,
    Screen,
    permission_dialog,
)
from uiprobe.also_target import (
    FIXED_HOME_APPS,
    HOME_COMMAND,
    SYSTEM_UI,
    AlsoTargetError,
)
from uiprobe.crawl import Limits
from uiprobe.device import Device
from uiprobe.errors import NotEmulatorError
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

HELPER = "com.example.helper"
LISTED = Policy(also_target=(HELPER,))
HOME_READ = f"{HOME_COMMAND}; echo uiprobe-exit:$?"


def screens(
    *extra: Screen, compose: bool = False, ask: bool = False
) -> tuple[Screen, ...]:
    """The shop opens the helper; the helper has rows under every guard.

    `compose` adds a row of the helper's second screen that opens a screen with
    the keyboard up, `ask` one that opens a permission dialog.
    """
    second = [El((0, 0, 320, 100), text="Two"), button(200, "Deeper", "helper3")]
    if compose:
        second.append(button(260, "Compose", "typing"))
    if ask:
        second.append(button(260, "Camera", "permission"))
    return (
        Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
        Screen(
            "main",
            f"{SHOP}/.Main",
            (
                El((0, 0, 320, 100), text="Shop"),
                button(200, "Open helper", "helper"),
                button(260, "Leave", "ext1"),
            ),
        ),
        Screen(
            "helper",
            f"{HELPER}/.Main",
            (
                El((0, 0, 320, 100), text="Helper"),
                button(200, "Settings", "helper2"),
                button(260, "Delete everything", "helper"),
                El(
                    (20, 320, 300, 360),
                    text="Wi-Fi",
                    cls="android.widget.Switch",
                    go="helper",
                    checkable=True,
                ),
                El(
                    (20, 380, 300, 420),
                    text="name",
                    cls="android.widget.EditText",
                    go="helper",
                ),
                button(440, "Visit", "ext1"),
            ),
        ),
        Screen("helper2", f"{HELPER}/.Two", tuple(second)),
        Screen("helper3", f"{HELPER}/.Three", (El((0, 0, 320, 100), text="Three"),)),
        Screen("ext1", f"{OTHER}/.One", (El((0, 0, 320, 640)),)),
        *extra,
    )


TYPING = Screen(
    "typing",
    f"{HELPER}/.Typing",
    (El((0, 0, 320, 100), text="Type"), button(200, "Go", "helper3")),
    keyboard=True,
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(
        fake_adb, SERIAL, screens(), start="home", transport_id=TRANSPORT
    )


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def tapped(android: FakeAndroid) -> list[tuple[str, tuple[int, int] | None]]:
    return [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]


def records(out: Path) -> list[dict]:
    return lines(out / "transitions.jsonl")


def run_listed(device, clock, out, **kwargs):
    return run(device, clock, out, policy=kwargs.pop("policy", LISTED), **kwargs)


# -- pressing in the listed package --------------------------------------------------


def test_the_crawl_presses_in_the_listed_package_under_every_guard(
    tmp_path, device, android, clock
):
    out = tmp_path / "run"
    result = run_listed(device, clock, out)
    assert result.stop_reason == "exhausted"
    taps = tapped(android)
    on_helper = [point for screen, point in taps if screen == "helper" and point]
    # pressed in the helper: Settings (200) and Visit (440); refused: Delete
    # (260, a deny word), the switch (320) and the text field (380)
    assert {p[1] for p in on_helper} == {220, 460}
    assert (tapped_screens := {s for s, _ in taps}) >= {"main", "helper", "helper2"}
    assert "helper3" not in tapped_screens  # a screen with no row
    skipped = lines(out / "skipped.jsonl")
    reasons = {
        reason
        for line in skipped
        for reason in line["reasons"]
        if line["screen_id"] in _helper_ids(out)
    }
    assert {"deny_word", "toggle", "text_input"} <= reasons
    assert "outside_target_app" not in reasons


def _helper_ids(out: Path) -> set[str]:
    ids = set()
    for path in (out / "screens").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["app"]["package"] == HELPER:
            ids.add(data["screen_id"])
    return ids


@pytest.mark.usefixtures("android")
def test_the_helpers_screens_are_saved_like_the_shops(tmp_path, device, clock):
    out = tmp_path / "run"
    result = run_listed(device, clock, out)
    packages = {}
    for path in (out / "screens").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        packages.setdefault(data["app"]["package"], set()).add(data["app"]["activity"])
    assert set(packages) == {SHOP, HELPER}
    assert len(packages[HELPER]) == 3
    assert result.screens == sum(len(v) for v in packages.values())
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert f"{HELPER}/" in summary
    start = json.loads((out / "run-start.json").read_text(encoding="utf-8"))
    assert start["also_target"] == [HELPER]


@pytest.mark.usefixtures("android")
def test_off_target_lines_are_written_for_unlisted_packages_only(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    run_listed(device, clock, out)
    starts = [r for r in records(out) if "action" in r]
    named = [r["off_target"]["package"] for r in starts if "off_target" in r]
    assert named  # the Leave and Share rows led to an unlisted app
    assert set(named) == {OTHER}
    assert not any(HELPER in json.dumps(r.get("off_target", {})) for r in starts)


def test_the_helper_is_never_launched_and_a_relaunch_starts_the_shop(
    tmp_path, device, android, fake_adb, clock
):
    out = tmp_path / "run"
    run_listed(device, clock, out)
    assert android.stopped
    assert set(android.stopped) == {SHOP}
    changing = [c for c in commands(fake_adb) if c.startswith(("am ", "monkey"))]
    assert changing
    assert not any(HELPER in c for c in changing)
    reads = [c for c in commands(fake_adb) if "resolve-activity" in c]
    assert not any(HELPER in c for c in reads)
    assert all(SHOP in c for c in reads if "category.LAUNCHER" in c)
    # the relaunches were decided on the helper's unlisted neighbour's screen
    kinds = [r["action"]["kind"] for r in records(out) if "action" in r]
    assert "relaunch" in kinds


# -- the refusals before anything is sent ----------------------------------------


def home_reads(fake_adb: FakeAdbServer) -> list[str]:
    return [c for c in commands(fake_adb) if c == HOME_READ]


@pytest.mark.usefixtures("android")
def test_an_empty_list_reads_and_sends_nothing_extra(tmp_path, device, fake_adb, clock):
    run(device, clock, tmp_path / "run")
    assert home_reads(fake_adb) == []
    assert not any(" -c android.intent.category.HOME" in c for c in commands(fake_adb))
    start = json.loads((tmp_path / "run" / "run-start.json").read_text("utf-8"))
    assert "also_target" not in start


@pytest.mark.usefixtures("android")
def test_a_list_reads_the_home_app_once_before_any_input(
    tmp_path, device, fake_adb, clock
):
    run_listed(device, clock, tmp_path / "run")
    assert len(home_reads(fake_adb)) == 1
    allc = commands(fake_adb)
    first_change = next(
        i for i, c in enumerate(allc) if c.startswith(("am ", "input "))
    )
    assert allc.index(HOME_READ) < first_change


@pytest.mark.usefixtures("android")
def test_the_listed_package_uid_is_read_once_at_the_start(
    tmp_path, device, fake_adb, clock
):
    run_listed(device, clock, tmp_path / "run")
    first = next(
        i for i, c in enumerate(commands(fake_adb)) if c.startswith("dumpsys package")
    )
    allc = commands(fake_adb)
    assert f"dumpsys package {SHOP}" in allc[first]
    assert any(c == f"dumpsys package {HELPER}" for c in allc[: first + 3])


@pytest.mark.usefixtures("android")
def test_the_systems_ui_is_refused_before_anything_is_sent(
    tmp_path, device, fake_adb, clock
):
    with pytest.raises(AlsoTargetError, match="system UI"):
        run(
            device,
            clock,
            tmp_path / "run",
            policy=Policy(also_target=(SYSTEM_UI,)),
        )
    assert fake_adb.commands == []
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("launcher", FIXED_HOME_APPS)
@pytest.mark.usefixtures("android")
def test_a_fixed_home_app_is_refused_before_anything_is_sent(
    tmp_path, device, fake_adb, clock, launcher
):
    with pytest.raises(AlsoTargetError, match="home app"):
        run(device, clock, tmp_path / "run", policy=Policy(also_target=(launcher,)))
    assert fake_adb.commands == []
    assert not (tmp_path / "run").exists()


@pytest.mark.usefixtures("android")
def test_the_target_itself_and_a_bad_name_are_refused_before_anything_is_sent(
    tmp_path, device, fake_adb, clock
):
    for listed in ((SHOP,), ("nope",), (HELPER, HELPER)):
        with pytest.raises(AlsoTargetError):
            run(device, clock, tmp_path / "run", policy=Policy(also_target=listed))
    assert fake_adb.commands == []


def test_the_devices_home_app_is_refused_after_one_read_and_nothing_else_is_sent(
    tmp_path, device, android, fake_adb, clock
):
    android.home = "com.vendor.home"
    with pytest.raises(AlsoTargetError) as info:
        run(
            device,
            clock,
            tmp_path / "run",
            policy=Policy(also_target=("com.vendor.home", HELPER)),
        )
    assert str(info.value) == (
        "also_target cannot list com.vendor.home: it is the home app of this"
        " device; nothing was pressed"
    )
    assert len(home_reads(fake_adb)) == 1
    assert not any(c.startswith(("am ", "input ")) for c in commands(fake_adb))
    assert not (tmp_path / "run").exists()


def test_a_failed_home_read_continues_with_the_fixed_list_and_a_note(
    tmp_path, device, android, fake_adb, clock
):
    android.home = None  # "No activity found"
    out = tmp_path / "run"
    result = run_listed(device, clock, out)
    assert result.stop_reason == "exhausted"
    assert len(home_reads(fake_adb)) == 1
    assert len(result.also_target_notes) == 1
    assert "could not be read" in result.also_target_notes[0]
    assert "com.android.launcher3" in result.also_target_notes[0]
    assert result.also_target_notes[0] in (out / "summary.txt").read_text("utf-8")
    assert tapped(android)


def test_a_package_that_is_not_installed_is_refused_at_the_start(
    tmp_path, device, android, fake_adb, clock
):
    # the home read and the shop's uid pass, the uid of the missing package
    # cannot be read
    missing = HELPER
    android.not_installed = frozenset({HELPER})
    with pytest.raises(AlsoTargetError, match="not installed"):
        run(device, clock, tmp_path / "run", policy=Policy(also_target=(missing,)))
    assert not any(c.startswith(("am ", "input ")) for c in commands(fake_adb))
    assert not (tmp_path / "run").exists()


def test_a_real_device_is_refused_before_the_home_is_read(
    tmp_path, device, android, fake_adb, clock
):
    android.props["ro.kernel.qemu"] = "0"
    with pytest.raises(NotEmulatorError):
        run_listed(device, clock, tmp_path / "run")
    assert home_reads(fake_adb) == []
    assert not any(c.startswith(("am ", "input ")) for c in commands(fake_adb))


# -- the window check -----------------------------------------------------------------


def test_the_window_at_a_tap_on_the_helpers_screen_must_be_the_helpers(
    tmp_path, device, android, clock
):
    # the focused window (and so the window the fake lists for the helper's
    # screens) has the shop's uid: not the helper's, so no tap is sent there
    android.focus_owner_uid = android.uid_of(SHOP)
    out = tmp_path / "run"
    run_listed(device, clock, out)
    screens_tapped = {s for s, _ in tapped(android)}
    assert "main" in screens_tapped
    assert "helper" not in screens_tapped
    reasons = {r["reason"] for r in records(out) if r.get("not_sent") and "reason" in r}
    assert "cover_unknown" in reasons


def test_a_window_of_the_helper_over_the_shops_tap_point_stays_refused(
    tmp_path, device, android, clock
):
    # a window of the helper (a floating button) takes touches over the shop's
    # "Open helper" row: the shop's tap there is not sent
    android.overlays.append(
        (f"{HELPER}/.Float", HELPER, (20, 200, 300, 240)),
    )
    out = tmp_path / "run"
    run_listed(device, clock, out)
    assert (
        "main",
        (160, 220),
    ) not in tapped(android)
    ends = [r for r in records(out) if r.get("not_sent")]
    assert any(r["reason"] == "covered_by_other" for r in ends)
    assert all(s != "helper" for s, _ in tapped(android))


def test_a_window_of_another_app_over_the_helpers_tap_point_is_refused(
    tmp_path, device, android, clock
):
    android.overlays.append((f"{OTHER}/.Float", OTHER, (20, 200, 300, 240)))
    out = tmp_path / "run"
    run_listed(device, clock, out)
    assert ("helper", (160, 220)) not in tapped(android)
    ends = [r for r in records(out) if r.get("not_sent")]
    assert any(r["reason"] == "covered_by_other" for r in ends)


def test_a_window_of_the_shop_over_the_helpers_tap_point_is_refused(
    tmp_path, device, android, clock
):
    android.overlays.append((f"{SHOP}/.Float", SHOP, (20, 200, 300, 240)))
    out = tmp_path / "run"
    run_listed(device, clock, out)
    assert ("helper", (160, 220)) not in tapped(android)


# -- the other guards ----------------------------------------------------------


def test_the_keyboard_on_a_helper_screen_gets_back_not_a_tap(tmp_path, fake_adb, clock):
    android = FakeAndroid(
        fake_adb,
        SERIAL,
        screens(TYPING, compose=True),
        start="home",
        transport_id=TRANSPORT,
    )
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    run_listed(device, clock, out)
    on_typing = [i for i in android.inputs if i.screen == "typing"]
    assert on_typing  # the crawl got there
    # BACK first, as on the shop's screens: nothing is tapped while it is up
    assert on_typing[0].kind == "key"


def test_the_depth_limit_counts_helper_screens(tmp_path, device, android, clock):
    out = tmp_path / "run"
    run_listed(device, clock, out, limits=Limits(depth=2))
    screens_tapped = {s for s, _ in tapped(android)}
    assert "main" in screens_tapped
    assert "helper" not in screens_tapped  # the helper is at depth 2


@pytest.mark.usefixtures("android")
def test_the_action_limit_counts_helper_actions(tmp_path, device, clock):
    result = run_listed(device, clock, tmp_path / "run", limits=Limits(actions=3))
    assert result.stop_reason == "max_actions"
    assert result.actions == 3


def test_permission_screens_are_never_pressed_even_when_listed(
    tmp_path, fake_adb, clock
):
    android = FakeAndroid(
        fake_adb,
        SERIAL,
        screens(permission_dialog(), ask=True),
        start="home",
        transport_id=TRANSPORT,
    )
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    run(
        device,
        clock,
        out,
        policy=Policy(also_target=(HELPER, PERMISSION_PACKAGE)),
    )
    # the dialog is a screen of a listed package, but never the app's: nothing
    # reaches it, not a tap and not a BACK; the app is relaunched instead
    assert not [i for i in android.inputs if i.screen == "permission"]
    kinds = [r["action"]["kind"] for r in records(out) if "action" in r]
    assert "relaunch" in kinds
    shown = {
        r["off_target"]["package"]
        for r in records(out)
        if "action" in r and "off_target" in r
    }
    assert PERMISSION_PACKAGE in shown  # still an off-target screen in the records
    assert PERMISSION_PACKAGE not in {
        json.loads(p.read_text("utf-8"))["app"]["package"]
        for p in (out / "screens").glob("*.json")
    }


# -- the tree of a run that listed a package ---------------------------------------


def _tree_text(out: Path) -> list[str]:
    from uiprobe.cli.tree import render_text
    from uiprobe.crawl_tree import read_tree

    return render_text(read_tree(out))


@pytest.mark.usefixtures("android")
def test_the_tree_draws_the_helpers_screens_under_the_row_that_opened_them(
    tmp_path, device, clock
):
    from uiprobe.crawl_tree import read_tree

    out = tmp_path / "run"
    run_listed(device, clock, out)
    tree = read_tree(out)
    assert tree.also_target == (HELPER,)
    assert tree.warnings == []
    assert {n.package for n in tree.screens.values()} == {SHOP, HELPER}
    assert tree.unreached == []
    text = _tree_text(out)
    assert text[0] == f"{SHOP} ({SHOP})  also target: {HELPER}"
    body = "\n".join(text)
    # the rows of the helper are drawn below "Open helper", in the same tree
    opened = next(i for i, line in enumerate(text) if "Open helper" in line)
    assert any("Settings" in line for line in text[opened + 1 :])
    assert "(screen not reached" not in body
    # `→ PACKAGE` only for the unlisted app: the Leave and Visit rows
    arrows = [line for line in text if " → " in line and "see:" not in line]
    assert any(OTHER in line for line in arrows)
    assert not any(HELPER in line for line in arrows)
    assert not any(" → " in line and "Open helper" in line for line in text)


@pytest.mark.usefixtures("android")
def test_the_trees_json_names_the_listed_packages_only_when_there_are_some(
    tmp_path, device, clock
):
    from uiprobe.crawl_tree import read_tree, tree_to_dict

    out = tmp_path / "run"
    run_listed(device, clock, out)
    data: Any = tree_to_dict(read_tree(out))
    assert data["run"]["also_target"] == [HELPER]
    for node in data["screens"].values():
        assert node["package"] in {SHOP, HELPER}
    rows = [
        e["to"]
        for node in data["screens"].values()
        for e in node["elements"]
        if e.get("to")
    ]
    assert {r["other_app"] for r in rows if "other_app" in r} == {OTHER}


@pytest.mark.usefixtures("android")
def test_a_run_without_a_list_has_no_extra_line_or_key(tmp_path, device, clock):
    from uiprobe.crawl_tree import read_tree, tree_to_dict

    out = tmp_path / "run"
    run(device, clock, out)
    tree = read_tree(out)
    assert tree.also_target == ()
    data: Any = tree_to_dict(tree)
    assert "also_target" not in data["run"]
    assert _tree_text(out)[0] == f"{SHOP} ({SHOP})"


@pytest.mark.usefixtures("android")
def test_the_transitions_check_still_refuses_a_foreign_screen_file(
    tmp_path, device, clock
):
    from uiprobe.crawl_tree import GATE_WARNING, read_tree

    out = tmp_path / "run"
    run_listed(device, clock, out)
    assert GATE_WARNING not in read_tree(out).warnings
    # the same folder with the list taken out of run-start.json: the helper's
    # screen files are another package's, which uiprobe never writes
    path = out / "run-start.json"
    start = json.loads(path.read_text("utf-8"))
    del start["also_target"]
    path.write_text(json.dumps(start), encoding="utf-8")
    assert GATE_WARNING in read_tree(out).warnings
    # a list that does not name the helper is as wrong
    start["also_target"] = ["com.example.other.helper"]
    path.write_text(json.dumps(start), encoding="utf-8")
    assert GATE_WARNING in read_tree(out).warnings


@pytest.mark.parametrize(
    "bad", [[], "com.example.helper", [1], [HELPER, HELPER], [SHOP], ["nope"], None]
)
@pytest.mark.usefixtures("android")
def test_a_bad_also_target_in_run_start_is_a_warning_and_the_gate_fails(
    tmp_path, device, clock, bad
):
    from uiprobe.crawl_tree import GATE_WARNING, read_tree

    out = tmp_path / "run"
    run_listed(device, clock, out)
    path = out / "run-start.json"
    start = json.loads(path.read_text("utf-8"))
    start["also_target"] = bad
    path.write_text(json.dumps(start), encoding="utf-8")
    tree = read_tree(out)
    assert tree.also_target == ()
    assert GATE_WARNING in tree.warnings
    assert any("also_target" in w for w in tree.warnings)
