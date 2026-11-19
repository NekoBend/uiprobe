"""Tests for the crawl driver: a fake Android app, a fake clock, files in tmp_path.

Every device is a strict `FakeAndroid` behind a fake adb server, so an input
that is not a tap, the BACK key or a launcher launch fails the test. Nothing
sleeps: the clock and the pause are injected.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

import pytest

import uiprobe
from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
    assert_input_kinds,
    assert_no_long_press,
    assert_no_press_inside,
    assert_no_text_input,
    assert_only_back_keys,
)
from uiprobe import crawl_driver, runlog
from uiprobe.crawl import (
    TAP_NOT_SENT,
    Back,
    CrawlEngine,
    Limits,
    Observation,
    Relaunch,
    Tap,
)
from uiprobe.crawl_driver import CrawlResult, crawl
from uiprobe.device import Device, DeviceIdentity, FocusedWindow
from uiprobe.errors import (
    AdbError,
    DeviceNotFoundError,
    LaunchFailedError,
    NotEmulatorError,
)
from uiprobe.guards import Policy
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.crawl import Action
    from uiprobe.model import Screen as ModelScreen

SERIAL = "emulator-5554"
TRANSPORT = 7
SHOP = "com.example.shop"
OTHER = "com.example.other"
BUTTON = "android.widget.Button"
STOP_REASONS = {
    "exhausted",
    "max_actions",
    "max_duration",
    "relaunch_limit",
    "keyboard_stuck",
    "dump_failed_repeatedly",
}


def button(top: int, text: str, go: str) -> El:
    return El((20, top, 300, top + 40), text=text, cls=BUTTON, go=go)


SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((0, 0, 320, 100), text="Shop"),
            button(200, "Browse", "browse"),
            button(260, "Help", "cart"),
            button(320, "Delete account", "main"),
        ),
    ),
    Screen(
        "browse",
        f"{SHOP}/.Browse",
        (El((0, 0, 320, 100), text="Items"), button(200, "Item", "detail")),
    ),
    Screen("detail", f"{SHOP}/.Detail", (El((0, 0, 320, 100), text="Detail"),)),
    Screen(
        "cart",
        f"{SHOP}/.Cart",
        (El((0, 0, 320, 100), text="Help page"),),
        protected=True,
    ),
    Screen("ext1", f"{OTHER}/.One", (El((0, 0, 320, 640)),)),
    Screen("ext2", f"{OTHER}/.Two", (El((0, 0, 320, 640)),)),
    Screen("ext3", f"{OTHER}/.Three", (El((0, 0, 320, 640)),)),
)


class Clock:
    """A fake clock that only the pause moves."""

    def __init__(self) -> None:
        self.now = 5000.0
        self.pauses: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def run(
    device: Device,
    clock: Clock,
    out: Path,
    *,
    limits: Limits | None = None,
    screenshots: bool = False,
    allow_real_device: bool = False,
    settle: float = 1.0,
    stop: Callable[[], bool] | None = None,
) -> CrawlResult:
    return crawl(
        device,
        SHOP,
        out,
        policy=Policy(),
        limits=limits or Limits(),
        screenshots=screenshots,
        allow_real_device=allow_real_device,
        settle=settle,
        stop=stop,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )


def lines(path: Path):
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def spy(
    monkeypatch: pytest.MonkeyPatch,
    device: Device,
    name: str,
    before: Callable[..., None],
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    """Wrap a device method; `before(number, *args, **kwargs)` runs first."""
    original = getattr(device, name)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        before(len(calls), *args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(device, name, wrapper)
    return calls


def commands(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands]


# --- a whole run ---


def test_a_small_app_is_crawled_end_to_end(tmp_path, device, android, clock):
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert isinstance(result, CrawlResult)
    assert result.out == out
    assert result.stop_reason == "exhausted"
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "exhausted"
    assert run_json["screens"] == result.screens == 4
    assert run_json["actions"] == result.actions
    start = json.loads((out / "run-start.json").read_text(encoding="utf-8"))
    assert start["package"] == SHOP
    assert start["device"] == {"serial": SERIAL}
    assert "delete" in start["deny"]

    transitions = lines(out / "transitions.jsonl")
    starts = transitions[0::2]
    ends = transitions[1::2]
    assert len(transitions) == 2 * len(starts) == 2 * result.actions
    assert [s["schema"] for s in starts] == ["uiprobe.transition_start/1"] * len(starts)
    assert [e["schema"] for e in ends] == ["uiprobe.transition_end/1"] * len(ends)
    ids = [s["action_id"] for s in starts]
    assert ids == list(range(1, result.actions + 1))
    assert [e["action_id"] for e in ends] == ids
    assert starts[0]["action"] == {"kind": "launch"}
    assert not any(e["dump_failed"] for e in ends)
    kinds = [s["action"]["kind"] for s in starts]
    assert set(kinds) == {"launch", "tap", "back"}
    for s in starts:
        if s["action"]["kind"] == "tap":
            assert s["action"]["element"]["text"] in {"Browse", "Help", "Item"}
            assert len(s["action"]["point"]) == 2
            assert s["from_screen_id"].startswith("s-")

    skipped = lines(out / "skipped.jsonl")
    assert any(
        row["element"]["text"] == "Delete account" and "deny_word" in row["reasons"][0]
        for row in skipped
    )
    screen_files = sorted(p.name for p in (out / "screens").iterdir())
    assert len(screen_files) == 4
    assert all(name.endswith(".json") for name in screen_files)
    for name in screen_files:
        shown = json.loads((out / "screens" / name).read_text(encoding="utf-8"))
        assert shown["app"]["package"] == SHOP
    assert (
        (out / "summary.txt")
        .read_text(encoding="utf-8")
        .startswith("Stopped: exhausted")
    )

    taps = [i for i in android.inputs if i.kind == "tap"]
    assert len(taps) == kinds.count("tap")
    assert_input_kinds(android, {"tap", "key"})
    assert_only_back_keys(android)
    assert_no_text_input(android)
    assert_no_long_press(android)
    assert_no_press_inside(android, "delete")
    assert android.stopped == []
    # Each tap and BACK waits 1 s in steps of at most 0.1 s (stop is polled).
    assert max(clock.pauses) <= crawl_driver.PAUSE_STEP
    assert sum(clock.pauses) == pytest.approx(1.0 * (result.actions - 1))


def test_crawl_run_is_exported():
    assert uiprobe.crawl_run is crawl
    assert uiprobe.CrawlResult is CrawlResult


# --- before anything is written or sent ---


def test_bad_arguments_refuse_before_any_command(tmp_path, device, fake_adb):
    out = tmp_path / "run"
    untyped = cast("Callable[..., object]", crawl)
    good = {"policy": Policy(), "limits": Limits()}
    bad = [
        (ValueError, {"package": "nodots"}),
        (TypeError, {"package": 5}),
        (TypeError, {"policy": None}),
        (TypeError, {"limits": {"depth": 1, "actions": 1, "minutes": 1}}),
        (TypeError, {"screenshots": 1}),
        (TypeError, {"allow_real_device": "yes"}),
        (ValueError, {"settle": -1.0}),
        (ValueError, {"settle": float("nan")}),
        (TypeError, {"settle": "1"}),
        (TypeError, {"stop": True}),
        (TypeError, {"clock": 1.0}),
        (TypeError, {"sleep": None}),
        (TypeError, {"out": 5}),
    ]
    for error, change in bad:
        arguments = {"package": SHOP, "out": out, **good, **change}
        package = arguments.pop("package")
        destination = arguments.pop("out")
        with pytest.raises(error):
            untyped(device, package, destination, **arguments)
    assert fake_adb.commands == []
    assert not out.exists()


def test_a_real_device_is_refused_with_no_input_and_no_folder(
    tmp_path, device, android, fake_adb, clock
):
    android.props["ro.kernel.qemu"] = "0"
    out = tmp_path / "run"
    with pytest.raises(NotEmulatorError) as caught:
        run(device, clock, out)
    assert caught.value.code == "not_emulator"
    assert caught.value.exit_code == 17
    assert caught.value.details == {"serial": SERIAL, "reason": "not_emulator"}
    assert not out.exists()
    assert android.inputs == []
    assert commands(fake_adb) == ["getprop ro.kernel.qemu"]


def test_a_real_device_runs_when_allowed(tmp_path, device, android, fake_adb, clock):
    android.props["ro.kernel.qemu"] = "0"
    out = tmp_path / "run"
    result = run(device, clock, out, allow_real_device=True)
    assert result.stop_reason == "exhausted"
    assert "getprop ro.kernel.qemu" not in commands(fake_adb)
    assert android.inputs


def test_an_unreadable_emulator_check_counts_as_a_real_device(
    tmp_path, device, android, monkeypatch, clock
):
    def unreadable() -> bool:
        raise AdbError("cannot read")

    monkeypatch.setattr(device, "is_emulator", unreadable)
    out = tmp_path / "run"
    with pytest.raises(NotEmulatorError) as caught:
        run(device, clock, out)
    assert caught.value.details["reason"] == "unreadable"
    assert caught.value.details["error"] == "adb_error"
    assert not out.exists()
    assert android.inputs == []


def test_an_existing_folder_is_refused_before_any_input(
    tmp_path, device, android, fake_adb, clock
):
    out = tmp_path / "run"
    out.mkdir()
    with pytest.raises(RunFolderError):
        run(device, clock, out)
    assert android.inputs == []
    assert not any(c.startswith(("monkey", "am ")) for c in commands(fake_adb))
    assert list(out.iterdir()) == []


# --- the protocol ---


def test_the_start_line_is_on_disk_before_the_input_is_sent(
    tmp_path, device, monkeypatch, clock
):
    out = tmp_path / "run"
    seen: list[tuple[str, dict]] = []

    def check(kind: str) -> Callable[..., None]:
        def before(_number: int, *args: object, **_kwargs: object) -> None:
            last = lines(out / "transitions.jsonl")[-1]
            seen.append((kind, last))
            if kind == "tap":
                assert last["action"]["point"] == list(cast("tuple[int, int]", args[0]))

        return before

    taps = spy(monkeypatch, device, "tap", check("tap"))
    keys = spy(monkeypatch, device, "key", check("key"))
    launches = spy(monkeypatch, device, "launch", check("launch"))
    result = run(device, clock, out)
    assert len(seen) == len(taps) + len(keys) + len(launches) == result.actions
    ids = []
    for kind, last in seen:
        assert last["schema"] == "uiprobe.transition_start/1"
        assert last["action"]["kind"] == {"key": "back"}.get(kind, kind)
        ids.append(last["action_id"])
    assert ids == list(range(1, result.actions + 1))
    assert all(args == ("back",) for args, _ in keys)


def test_a_screen_that_changes_before_the_press_is_not_pressed(
    tmp_path, device, android, clock
):
    # Dump 1 follows the launch; dump 2 is the re-read before the first tap.
    android.switch_before_dump(2, "cart")
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason in STOP_REASONS
    skipped = lines(out / "skipped.jsonl")
    refused = [r for r in skipped if r["reasons"] == ["changed_before_press"]]
    assert [r["element"]["text"] for r in refused] == ["Browse"]
    records = lines(out / "transitions.jsonl")
    starts = [r for r in records if "action" in r]
    browse = [
        s for s in starts if s["action"].get("element", {}).get("text") == "Browse"
    ]
    assert len(browse) == 1  # written, then closed as not sent
    ends = [r for r in records if r.get("action_id") == browse[0]["action_id"]]
    assert ends[-1]["not_sent"] is True
    # The dialog came up just before the dump: the focused window changed
    # across it, which the gate sees before the engine's re-check.
    assert ends[-1]["reason"] == "focus_changed"
    on_browse = [
        i
        for i in android.inputs
        if i.kind == "tap" and i.point is not None and 200 <= i.point[1] < 240
    ]
    assert not on_browse
    assert (out / "run.json").exists()


def test_a_relaunch_restarts_the_app(tmp_path, device, android, monkeypatch, clock):
    def crowd(number: int, *_args: object, **_kwargs: object) -> None:
        pass

    original = device.tap

    def tap_then_cover(point: tuple[int, int]) -> tuple[int, int]:
        result = original(point)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)
    launches = spy(monkeypatch, device, "launch", crowd)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason in STOP_REASONS
    kinds = [
        r["action"]["kind"] for r in lines(out / "transitions.jsonl") if "action" in r
    ]
    assert "relaunch" in kinds
    # A relaunch is a force-stop, then a plain launch, sent one after the other.
    assert all(kwargs.get("restart", False) is False for _, kwargs in launches)
    assert len(launches) == 1 + kinds.count("relaunch")
    assert android.stopped == [SHOP] * kinds.count("relaunch")
    for name in (out / "screens").iterdir():
        shown = json.loads(name.read_text(encoding="utf-8"))
        assert shown["app"]["package"] == SHOP


# --- stopping ---


def test_stop_between_actions_ends_with_interrupted(tmp_path, device, android, clock):
    def stop() -> bool:
        return len(android.inputs) >= 2  # after the second press

    out = tmp_path / "run"
    result = run(device, clock, out, stop=stop)
    assert result.stop_reason == "interrupted"
    transitions = lines(out / "transitions.jsonl")
    starts = [r for r in transitions if "action" in r]
    assert result.actions == len(starts) == 3  # launch and two presses
    assert len(transitions) == 6  # each start has its end
    assert len(android.inputs) == 2
    assert (
        json.loads((out / "run.json").read_text(encoding="utf-8"))["stop_reason"]
        == "interrupted"
    )


def test_stop_before_the_first_action_sends_nothing(tmp_path, device, android, clock):
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: True)
    assert result.stop_reason == "interrupted"
    assert (result.screens, result.actions) == (0, 0)
    assert android.inputs == []
    assert android.stopped == []
    assert not (out / "transitions.jsonl").exists()


def interrupt_at_tap(
    monkeypatch: pytest.MonkeyPatch,
    device: Device,
    number: int,
    flag: list[bool],
) -> None:
    def before(call: int, *_args: object, **_kwargs: object) -> None:
        if call == number:
            flag.append(True)
            raise KeyboardInterrupt

    spy(monkeypatch, device, "tap", before)


def test_an_interrupt_is_raised_again_without_a_stop(
    tmp_path, device, android, monkeypatch, clock
):
    out = tmp_path / "run"
    interrupt_at_tap(monkeypatch, device, 2, [])
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out)
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "interrupted"
    transitions = lines(out / "transitions.jsonl")
    assert transitions[-1]["schema"] == "uiprobe.transition_start/1"
    assert run_json["actions"] == transitions[-1]["action_id"]
    assert len([i for i in android.inputs if i.kind == "tap"]) == 1


def test_an_interrupt_with_stop_set_returns(tmp_path, device, monkeypatch, clock):
    out = tmp_path / "run"
    flag: list[bool] = []
    interrupt_at_tap(monkeypatch, device, 2, flag)
    result = run(device, clock, out, stop=lambda: bool(flag))
    assert result.stop_reason == "interrupted"
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "interrupted"
    assert run_json["actions"] == result.actions
    assert (out / "summary.txt").exists()


def test_an_interrupt_with_a_stop_that_says_no_is_raised(
    tmp_path, device, monkeypatch, clock
):
    out = tmp_path / "run"
    interrupt_at_tap(monkeypatch, device, 1, [])
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out, stop=lambda: False)
    assert (out / "run.json").exists()


# --- errors ---


def test_an_adb_error_mid_run_is_recorded_and_raised(
    tmp_path, device, monkeypatch, clock
):
    def fail(call: int, *_args: object, **_kwargs: object) -> None:
        if call == 2:
            raise AdbError("the adb server answered FAIL")

    spy(monkeypatch, device, "tap", fail)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert 'Note: "error: adb_error"' in summary
    transitions = lines(out / "transitions.jsonl")
    assert transitions[-1]["schema"] == "uiprobe.transition_start/1"
    assert run_json["actions"] == transitions[-1]["action_id"]


def test_an_unexpected_error_is_recorded_as_internal_error(
    tmp_path, device, monkeypatch, clock
):
    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(device, "keyboard_shown", fail)
    out = tmp_path / "run"
    with pytest.raises(RuntimeError, match="boom"):
        run(device, clock, out)
    assert "error: internal_error" in (out / "summary.txt").read_text(encoding="utf-8")
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"
    assert run_json["actions"] == 1


# --- screenshots, limits ---


def test_a_failed_screenshot_is_noted_and_the_crawl_goes_on(tmp_path, fake_adb, clock):
    android = FakeAndroid(
        fake_adb,
        SERIAL,
        SCREENS,
        start="home",
        secure_screencap="empty",
        transport_id=TRANSPORT,
    )
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    result = run(device, clock, out, screenshots=True)
    assert result.stop_reason == "exhausted"
    assert result.screens == 4
    pictures = sorted(p.name for p in (out / "screens").glob("*.png"))
    assert len(pictures) == 3
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert summary.count("no screenshot of s-") == 1
    assert "empty" in summary
    assert android.screens["cart"].protected


def test_max_duration_is_judged_on_the_injected_clock(tmp_path, device, clock):
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(minutes=1), settle=25.0)
    assert result.stop_reason == "max_duration"
    assert clock.pauses
    assert max(clock.pauses) <= crawl_driver.PAUSE_STEP
    assert clock.now >= 5000.0 + 60
    assert clock.now <= 5000.0 + 60 + crawl_driver.PAUSE_STEP  # no pause past it


def test_max_actions_stops_the_crawl(tmp_path, device, clock):
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=4))
    assert result.stop_reason == "max_actions"
    assert result.actions == 4


# --- a launch that leaves another app in front ---


def test_a_launch_that_is_not_in_front_does_not_end_the_crawl(
    tmp_path, device, android, monkeypatch, clock
):
    # `Device.launch` waits with the device's own clock; keep it off real time.
    monkeypatch.setattr(device, "_clock", clock.monotonic)
    monkeypatch.setattr(device, "_sleep", clock.sleep)
    android.launch_in_front = False
    out = tmp_path / "run"
    result = run(device, clock, out)
    # Nothing came to the front, so the engine sees an off-target screen and
    # presses BACK or relaunches; the crawl ends by its own rules, not by error.
    assert result.stop_reason in STOP_REASONS
    assert result.actions >= 1
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "was not in front after launch" in summary
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    assert starts[0]["action"] == {"kind": "launch"}
    # The off-target screen was answered with BACK or a relaunch, not ignored.
    later = {r["action"]["kind"] for r in starts[1:]}
    assert later & {"back", "relaunch"}
    assert result.stop_reason in {"relaunch_limit", "max_actions", "exhausted"}


def test_a_launch_that_cannot_find_the_app_still_ends_with_error(
    tmp_path, device, monkeypatch, clock
):
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise LaunchFailedError(
            "no launcher", details={"package": SHOP, "reason": "unexpected_output"}
        )

    monkeypatch.setattr(device, "launch", refuse)
    out = tmp_path / "run"
    with pytest.raises(LaunchFailedError):
        run(device, clock, out)
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"
    assert "error: launch_failed" in (out / "summary.txt").read_text(encoding="utf-8")


# --- failed dumps, skips, a bad clock ---


def end_lines(out: Path) -> list[dict[str, object]]:
    return [
        r
        for r in lines(out / "transitions.jsonl")
        if r["schema"] == "uiprobe.transition_end/1"
    ]


def test_a_failed_dump_is_recorded_and_read_again(tmp_path, device, android, clock):
    android.fail_dump(3)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "exhausted"
    ends = end_lines(out)
    failed = [e for e in ends if e["dump_failed"]]
    assert len(failed) == 1
    transitions = lines(out / "transitions.jsonl")
    ids = [t["action_id"] for t in transitions]
    assert ids == [i for n in range(1, result.actions + 1) for i in (n, n)]


def test_dumps_that_keep_failing_end_the_crawl(tmp_path, device, android, clock):
    android.fail_dump(3, 4, 5, 6, 7, 8)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "dump_failed_repeatedly"
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "dump_failed_repeatedly"


def test_an_adb_error_from_the_dump_is_not_a_failed_dump(
    tmp_path, device, monkeypatch, clock
):
    def broken() -> None:
        raise AdbError("gone")

    monkeypatch.setattr(device, "dump", broken)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"
    assert end_lines(out) == []


def test_skipped_rows_are_not_repeated(tmp_path, device, clock):
    out = tmp_path / "run"
    run(device, clock, out)
    rows = lines(out / "skipped.jsonl")
    keys = [(r["screen_id"], r["fingerprint"]) for r in rows]
    assert keys
    assert len(keys) == len(set(keys))


def test_the_row_of_a_screen_that_is_too_large_has_no_element():
    from uiprobe.crawl import SkippedElement
    from uiprobe.crawl_driver import _skipped_element

    item = SkippedElement("s-abc", "s-abc", ("screen_too_large",))
    assert _skipped_element(item, (), {}, package=SHOP, policy=Policy()) == {}


def test_a_skip_row_names_the_element_whose_reasons_match(tmp_path, fake_adb, clock):
    # Two identical unlabeled tiles share a fingerprint; the second one is
    # listed first in the dump but only the one at the top edge gets
    # screen_edge, so each row must name its own tile.
    screens = (
        Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
        Screen(
            "main",
            f"{SHOP}/.Main",
            (
                El((20, 200, 300, 240), cls=BUTTON, go="main"),
                El((20, 0, 300, 40), cls=BUTTON, go="main"),
            ),
        ),
    )
    FakeAndroid(fake_adb, SERIAL, screens, start="home", transport_id=TRANSPORT)
    out = tmp_path / "run"
    run(Device(adb_server=fake_adb.address), clock, out)
    rows = lines(out / "skipped.jsonl")
    by_reasons = {tuple(r["reasons"]): r for r in rows}
    assert set(by_reasons) == {("unlabeled", "screen_edge"), ("unlabeled",)}
    edge = by_reasons[("unlabeled", "screen_edge")]["element"]["bounds"]
    plain = by_reasons[("unlabeled",)]["element"]["bounds"]
    assert edge == [20, 0, 300, 40]
    assert plain == [20, 200, 300, 240]


def test_a_clock_that_returns_a_bad_value_leaves_no_folder(tmp_path, device, fake_adb):
    out = tmp_path / "run"
    bad = cast("Callable[[], float]", lambda: "x")
    with pytest.raises(TypeError, match="number of seconds"):
        crawl(
            device,
            SHOP,
            out,
            policy=Policy(),
            limits=Limits(),
            clock=bad,
            sleep=lambda _s: None,
        )
    assert not out.exists()
    assert not any(c.startswith(("monkey", "am ")) for c in commands(fake_adb))


# --- the engine's tap protocol: recheck, send once, then tap_sent ---


@dataclass(frozen=True)
class Event:
    """One call the driver made: to the engine, or a tap or dump on the device."""

    kind: str
    tap: Tap | None = None
    allowed: bool | None = None
    point: tuple[int, int] | None = None


class EngineLog:
    """The engines `crawl` built, and the calls in the order they were made."""

    def __init__(self) -> None:
        self.engines: list[CrawlEngine] = []
        self.events: list[Event] = []

    def proposed(self) -> list[tuple[int, Tap]]:
        """The taps next() returned, with the index of that event."""
        return [
            (index, event.tap)
            for index, event in enumerate(self.events)
            if event.kind == "next" and event.tap is not None
        ]

    def reported(self) -> list[Tap]:
        return [e.tap for e in self.events if e.kind == "tap_sent" and e.tap]


@pytest.fixture
def engine_log(monkeypatch: pytest.MonkeyPatch, device: Device) -> EngineLog:
    log = EngineLog()

    class RecordingEngine(CrawlEngine):
        def __init__(
            self, package: str, policy: Policy, limits: Limits, *, started_at: float
        ) -> None:
            super().__init__(package, policy, limits, started_at=started_at)
            log.engines.append(self)

        def next(self, observation: Observation) -> Action:
            action = super().next(observation)
            tap = action if isinstance(action, Tap) else None
            log.events.append(Event("next", tap=tap))
            return action

        def recheck(self, tap: Tap, observation: Observation) -> bool:
            allowed = super().recheck(tap, observation)
            log.events.append(Event("recheck", tap=tap, allowed=allowed))
            return allowed

        def tap_sent(self, tap: Tap) -> None:
            log.events.append(Event("tap_sent", tap=tap))
            super().tap_sent(tap)

    monkeypatch.setattr(crawl_driver, "CrawlEngine", RecordingEngine)

    def on_tap(_number: int, *args: object, **_kwargs: object) -> None:
        log.events.append(Event("send", point=cast("tuple[int, int]", args[0])))

    def on_dump(_number: int, *_args: object, **_kwargs: object) -> None:
        log.events.append(Event("dump"))

    spy(monkeypatch, device, "tap", on_tap)
    spy(monkeypatch, device, "dump", on_dump)
    return log


def test_an_allowed_tap_is_sent_once_and_reported_once(
    tmp_path, device, android, clock, engine_log
):
    result = run(device, clock, tmp_path / "run")
    assert result.stop_reason == "exhausted"
    events = engine_log.events
    proposed = engine_log.proposed()
    assert proposed
    for index, tap in proposed:
        # next() -> fresh read -> recheck(same object) -> send -> tap_sent(same
        # object) -> the next observation.
        assert events[index + 1].kind == "dump"
        recheck = events[index + 2]
        assert recheck.kind == "recheck"
        assert recheck.tap is tap
        assert recheck.allowed is True
        assert events[index + 3] == Event("send", point=tap.point)
        assert events[index + 4].kind == "tap_sent"
        assert events[index + 4].tap is tap
        assert events[index + 5].kind == "dump"
    reported = engine_log.reported()
    assert [id(t) for t in reported] == [id(t) for _, t in proposed]
    sent = [i for i in android.inputs if i.kind == "tap"]
    assert len(sent) == len(reported)
    engine = engine_log.engines[0]
    assert engine.actions_sent == result.actions
    assert not any(TAP_NOT_SENT in s.reasons for s in engine.records.skipped)


def test_a_refused_recheck_sends_nothing_and_reports_nothing(
    tmp_path, device, android, clock, engine_log
):
    # Dump 1 follows the launch; dump 2 is the re-read before the first tap.
    android.switch_before_dump(2, "cart")
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason in STOP_REASONS
    events = engine_log.events
    refused = [
        (index, e.tap)
        for index, e in enumerate(events)
        if e.kind == "recheck" and e.allowed is False
    ]
    assert len(refused) == 1
    index, tap = refused[0]
    assert tap is not None
    assert events[index + 1].kind == "next"  # no send, no report in between
    assert all(t is not tap for t in engine_log.reported())
    allowed = [e.tap for e in events if e.kind == "recheck" and e.allowed]
    assert [id(t) for t in engine_log.reported()] == [id(t) for t in allowed]
    sends = [e for e in events if e.kind == "send"]
    assert len(sends) == len(allowed)
    assert tap.point not in [e.point for e in sends]
    rows = lines(out / "skipped.jsonl")
    assert [r["reasons"] for r in rows if r["fingerprint"] == tap.fingerprint] == [
        ["changed_before_press"]
    ]
    assert not [r for r in rows if TAP_NOT_SENT in r["reasons"]]


def test_a_tap_whose_send_fails_is_not_reported_and_the_engine_releases_it(
    tmp_path, device, android, monkeypatch, clock, engine_log
):
    def fail(call: int, *_args: object, **_kwargs: object) -> None:
        if call == 2:
            raise AdbError("the adb server answered FAIL")

    spy(monkeypatch, device, "tap", fail)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    proposed = engine_log.proposed()
    assert len(proposed) == 2
    first, failed = proposed[0][1], proposed[1][1]
    assert [id(t) for t in engine_log.reported()] == [id(first)]
    assert [e.allowed for e in engine_log.events if e.kind == "recheck"] == [
        True,
        True,
    ]
    assert len([i for i in android.inputs if i.kind == "tap"]) == 1
    engine = engine_log.engines[0]
    counted = engine.actions_sent
    transitions = len(engine.records.transitions)
    # The start line of the failed tap is on disk with the next number; the
    # engine did not count it.
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    assert starts[-1]["action"]["kind"] == "tap"
    assert starts[-1]["action_id"] == counted + 1
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"
    assert run_json["actions"] == starts[-1]["action_id"]
    # The run ended at the failed send: the failed tap was never reported, so
    # the engine still counts only what was sent.
    assert all(t is not failed for t in engine_log.reported())
    assert engine.actions_sent == counted
    assert len(engine.records.transitions) == transitions


def test_the_clock_values_given_to_the_engine_stay_in_range(
    tmp_path, device, monkeypatch
):
    nows: list[float] = []
    real = Observation

    def observation(
        screen: ModelScreen | None,
        foreground: str | None,
        keyboard_shown: bool | None,  # noqa: FBT001 - the driver passes it so
        now: float,
    ) -> Observation:
        nows.append(now)
        return real(screen, foreground, keyboard_shown, now)

    monkeypatch.setattr(crawl_driver, "Observation", observation)
    result = crawl(
        device,
        SHOP,
        tmp_path / "run",
        policy=Policy(),
        limits=Limits(),
        settle=0.0,
        clock=time.monotonic,
        sleep=lambda _s: None,
    )
    assert result.stop_reason == "exhausted"
    assert nows
    assert all(0 <= now < 2**40 for now in nows)
    assert nows == sorted(nows)


def test_a_clock_out_of_range_leaves_no_folder(tmp_path, device, fake_adb):
    for number, value in enumerate((-1.0, float(2**40), float("inf"))):
        out = tmp_path / f"run{number}"
        with pytest.raises(ValueError, match="below 2"):
            crawl(
                device,
                SHOP,
                out,
                policy=Policy(),
                limits=Limits(),
                clock=lambda value=value: value,
                sleep=lambda _s: None,
            )
        assert not out.exists()
    assert not any(c.startswith(("monkey", "am ")) for c in commands(fake_adb))


def test_a_dump_with_duplicate_element_ids_is_recorded_as_a_failed_dump(
    tmp_path, device, monkeypatch, clock
):
    original = device.dump
    calls: list[int] = []

    def dump() -> ModelScreen:
        screen = original()
        calls.append(1)
        if len(calls) != 3:
            return screen
        # Dump 3 follows the first tap: give two elements the same id.
        first, second, *rest = screen.elements
        twin = replace(second, id=first.id)
        return replace(screen, elements=(first, twin, *rest))

    monkeypatch.setattr(device, "dump", dump)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "exhausted"
    ends = end_lines(out)
    assert ends[1]["action_id"] == 2
    assert ends[1]["dump_failed"] is True
    assert [e for e in ends if e["dump_failed"]] == [ends[1]]


# --- the gate: every action is judged right before it is sent ---


def _gate_run(
    tmp_path: Path,
    device: Device,
    clock: Clock,
    *,
    stop: Callable[[], bool] | None = None,
) -> tuple[crawl_driver._Run, CrawlEngine, Observation]:
    """A run after its launch, with the first observation, for direct sends."""
    device.pin_transport()
    engine = CrawlEngine(SHOP, Policy(), Limits(minutes=1), started_at=clock.now)
    writer = RunWriter.create(
        tmp_path / "run", package=SHOP, limits=Limits(), deny_words=[], serial=SERIAL
    )
    _WRITERS.append(writer)
    driver = crawl_driver._Run(
        device,
        SHOP,
        writer,
        engine,
        Policy(),
        identity=_identity(device),
        emulator=True,
        screenshots=False,
        settle=0.0,
        deadline=clock.now + 60,
        stop=stop,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    driver.send(engine.start(), None)
    return driver, engine, driver.observe()


_WRITERS: list[RunWriter] = []


def _identity(device: Device) -> DeviceIdentity:
    found = device.identity()
    assert found is not None
    return found


@pytest.fixture(autouse=True)
def _close_writers():
    yield
    while _WRITERS:
        with contextlib.suppress(RunFolderError):
            _WRITERS.pop().close()


def _not_sent_reason(tmp_path: Path) -> str | None:
    records = lines(tmp_path / "run" / "transitions.jsonl")
    return records[-1].get("reason") if records[-1].get("not_sent") else None


def _make_action(kind: str, engine: CrawlEngine, observation: Observation):
    if kind == "tap":
        action = engine.next(observation)
        assert isinstance(action, Tap)
        return action
    return {"back": Back(), "relaunch": Relaunch()}[kind]


SHADE = FocusedWindow("com.android.systemui", "NotificationShade", "2f7e017")
# Each condition, how to bring it about, and the reason it gives per kind.
_CONDITIONS = {
    "stopped": ("stop", None),
    "time_limit": ("clock", None),
    "foreground_unknown": ("foreground", None),
    "focus_unknown": ("focused_window", None),
    "focus_other": ("focused_window", SHADE),
    "keyboard_unknown": ("keyboard_shown", None),
    "keyboard_shown": ("keyboard_shown", True),
    "focus_title": (
        "focused_window",
        FocusedWindow(SHOP, f"{SHOP}/{SHOP}.Other", "a1b2c3"),
    ),
}
_CASES = [
    ("tap", "stopped", "stopped"),
    ("tap", "time_limit", "time_limit"),
    ("tap", "focus_unknown", "focus_unknown"),
    ("tap", "focus_other", "focus_other"),
    ("tap", "keyboard_unknown", "keyboard_unknown"),
    ("tap", "keyboard_shown", "keyboard_shown"),
    ("back", "stopped", "stopped"),
    ("back", "time_limit", "time_limit"),
    ("back", "foreground_unknown", "foreground_unknown"),
    ("back", "focus_unknown", "focus_unknown"),
    ("back", "focus_other", "changed_before_send"),
    ("back", "keyboard_unknown", "keyboard_unknown"),
    ("back", "keyboard_shown", "changed_before_send"),
    ("back", "focus_title", "changed_before_send"),
    ("relaunch", "focus_title", "changed_before_send"),
    ("relaunch", "stopped", "stopped"),
    ("relaunch", "time_limit", "time_limit"),
    ("relaunch", "foreground_unknown", "foreground_unknown"),
    ("relaunch", "focus_unknown", "focus_unknown"),
    ("relaunch", "focus_other", "changed_before_send"),
]


@pytest.mark.parametrize(("kind", "condition", "reason"), _CASES)
def test_the_gate_sends_nothing_when_a_condition_fails(
    tmp_path, device, android, clock, monkeypatch, kind, condition, reason
):
    stopping = []
    driver, engine, observation = _gate_run(
        tmp_path, device, clock, stop=lambda: bool(stopping)
    )
    action = _make_action(kind, engine, observation)
    inputs, stopped = len(android.inputs), list(android.stopped)
    how, value = _CONDITIONS[condition]
    if how == "stop":
        stopping.append(True)
    elif how == "clock":
        clock.now = driver.deadline
    else:
        monkeypatch.setattr(device, how, lambda: value)
    driver.send(action, observation)
    assert len(android.inputs) == inputs  # no tap, no key
    assert android.stopped == stopped  # no relaunch
    assert _not_sent_reason(tmp_path) == reason


@pytest.mark.parametrize("condition", ["stopped", "time_limit"])
def test_the_gate_holds_back_the_launch_too(
    tmp_path, device, android, clock, condition
):
    device.pin_transport()
    engine = CrawlEngine(SHOP, Policy(), Limits(minutes=1), started_at=clock.now)
    writer = RunWriter.create(
        tmp_path / "run", package=SHOP, limits=Limits(), deny_words=[], serial=SERIAL
    )
    _WRITERS.append(writer)
    driver = crawl_driver._Run(
        device,
        SHOP,
        writer,
        engine,
        Policy(),
        identity=_identity(device),
        emulator=True,
        screenshots=False,
        settle=0.0,
        deadline=clock.now + 60,
        stop=lambda: condition == "stopped",
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    if condition == "time_limit":
        clock.now += 60
    driver.send(engine.start(), None)
    assert android.screen.name == "home"  # nothing was launched
    assert _not_sent_reason(tmp_path) == condition


@pytest.mark.parametrize("kind", ["tap", "back", "relaunch"])
def test_the_gate_lets_an_unchanged_action_through(
    tmp_path, device, android, clock, kind
):
    driver, engine, observation = _gate_run(tmp_path, device, clock)
    action = _make_action(kind, engine, observation)
    inputs, stopped = len(android.inputs), len(android.stopped)
    driver.send(action, observation)
    assert len(android.inputs) + len(android.stopped) == inputs + stopped + 1
    assert _not_sent_reason(tmp_path) is None


# --- the device stays the one that was checked ---


def test_a_device_that_takes_over_the_serial_receives_nothing(
    tmp_path, device, android, fake_adb, clock, monkeypatch
):
    def swap(_number: int, *_args: object, **_kwargs: object) -> None:
        # Right after the emulator check: another device takes the serial.
        fake_adb.devices[:] = [
            {"serial": SERIAL, "state": "device", "transport_id": TRANSPORT + 1}
        ]

    original = device.is_emulator

    def check_then_swap() -> bool:
        answer = original()
        swap(0)
        return answer

    monkeypatch.setattr(device, "is_emulator", check_then_swap)
    before = len(fake_adb.requests)
    with pytest.raises(DeviceNotFoundError):
        run(device, clock, tmp_path / "run")
    after = fake_adb.requests[before:]
    selectors = [r for r in after if r.startswith("host:transport")]
    assert selectors
    assert set(selectors) == {f"host:transport-id:{TRANSPORT}"}
    assert android.inputs == []
    assert android.stopped == []
    assert not [c for _, _, c in fake_adb.commands if c.startswith(("monkey", "input"))]


def test_the_emulator_check_goes_through_the_pinned_transport(
    tmp_path, device, fake_adb, clock
):
    before = len(fake_adb.requests)
    run(device, clock, tmp_path / "run", limits=Limits(actions=1))
    first = [r for r in fake_adb.requests[before:] if r.startswith("host:transport")]
    assert first[0] == f"host:transport-id:{TRANSPORT}"
    assert all(r == f"host:transport-id:{TRANSPORT}" for r in first)


def test_a_real_device_allowed_is_pinned_too(
    tmp_path, device, fake_adb, android, clock
):
    android.props["ro.kernel.qemu"] = "0"
    before = len(fake_adb.requests)
    run(
        device,
        clock,
        tmp_path / "run",
        allow_real_device=True,
        limits=Limits(actions=1),
    )
    selectors = [
        r for r in fake_adb.requests[before:] if r.startswith("host:transport")
    ]
    assert selectors
    assert set(selectors) == {f"host:transport-id:{TRANSPORT}"}


def test_no_transport_id_means_nothing_is_written_or_sent(tmp_path, fake_adb, clock):
    android = FakeAndroid(fake_adb, SERIAL, SCREENS, start="home")  # no id listed
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    with pytest.raises(AdbError, match="no transport id"):
        run(device, clock, out)
    assert not out.exists()
    assert android.inputs == []
    assert fake_adb.commands == []


# --- the rest of the review's cases ---


def test_a_lost_reply_to_a_tap_is_not_resent(tmp_path, device, android, clock):
    android.drop_after_input(1)  # the device takes the tap, the reply is lost
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out)
    assert len([i for i in android.inputs if i.kind == "tap"]) == 1
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    assert [r["action"]["kind"] for r in starts].count("tap") == 1


def test_a_custom_deny_word_is_honoured(tmp_path, device, android, clock):
    out = tmp_path / "run"
    crawl(
        device,
        SHOP,
        out,
        policy=Policy(deny_words=("browse",)),
        limits=Limits(),
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    assert_no_press_inside(android, "browse")
    rows = lines(out / "skipped.jsonl")
    assert any(r.get("element", {}).get("text") == "Browse" for r in rows)


def test_screens_are_counted_when_observed(tmp_path, device, clock):
    screens = tmp_path / "run" / "screens"

    def stop() -> bool:  # as soon as the first screen is written
        return screens.is_dir() and any(screens.glob("*.json"))

    result = run(device, clock, tmp_path / "run", stop=stop)
    assert result.stop_reason == "interrupted"
    assert result.screens == len(list((tmp_path / "run" / "screens").glob("*.json")))
    assert result.screens == 1


def test_the_transition_end_comes_before_a_screenshot_that_fails(
    tmp_path, device, clock, monkeypatch
):
    def fail(*_args: object, **_kwargs: object) -> bytes:
        raise AdbError("the adb server answered FAIL")

    monkeypatch.setattr(device, "screenshot", fail)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out, screenshots=True)
    records = lines(out / "transitions.jsonl")
    assert records[0]["action"] == {"kind": "launch"}
    assert records[1]["action_id"] == 1
    assert "to_screen_id" in records[1]


def test_a_screenshot_of_another_app_is_not_saved(
    tmp_path, device, android, clock, monkeypatch
):
    original = device.screenshot

    def shot_then_cover() -> bytes:
        data = original()
        android.show("ext1")  # another app comes forward as the image is taken
        return data

    monkeypatch.setattr(device, "screenshot", shot_then_cover)
    out = tmp_path / "run"
    run(device, clock, out, screenshots=True, limits=Limits(actions=1))
    assert list((out / "screens").glob("*.png")) == []
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "the app was not in front" in summary


def test_an_interrupt_that_breaks_a_write_is_raised_even_with_stop_set(
    tmp_path, device, clock, monkeypatch
):
    stopping = []
    original = RunWriter.write_transition_start
    count = []

    def interrupted(self, *args, **kwargs):
        count.append(1)
        if len(count) == 2:
            stopping.append(True)
            monkeypatch.setattr(runlog, "_put", _interrupting_put)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RunWriter, "write_transition_start", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, tmp_path / "run", stop=lambda: bool(stopping))
    assert not (tmp_path / "run" / "run.json").exists()


def _interrupting_put(_handle, _data):
    raise KeyboardInterrupt


# --- round two: the device behind the pin, the order in the gate ---


def _after_first_tap(monkeypatch, device, then):
    original = device.tap

    def tap(point):
        result = original(point)
        then()
        return result

    monkeypatch.setattr(device, "tap", tap)


@pytest.mark.parametrize("change", ["server_restart", "fingerprint", "not_emulator"])
def test_a_different_device_behind_the_pin_receives_nothing_more(
    tmp_path, device, android, fake_adb, clock, monkeypatch, change
):
    def swap() -> None:
        if change == "server_restart":
            # A restarted server gave the same transport number to another device.
            fake_adb.devices[:] = [
                {"serial": "R58M1234", "state": "device", "transport_id": TRANSPORT}
            ]
        elif change == "fingerprint":
            android.props["ro.build.fingerprint"] = "other/build:36/X/9:user/k"
        else:
            android.props["ro.kernel.qemu"] = "0"

    _after_first_tap(monkeypatch, device, swap)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError) as info:
        run(device, clock, out)
    assert info.value.details["reason"] == "device_changed"
    assert len(android.inputs) == 1  # only the tap before the change
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"


def test_a_real_device_allowed_may_report_qemu_0_but_not_change(
    tmp_path, device, android, clock
):
    android.props["ro.kernel.qemu"] = "0"
    result = run(device, clock, tmp_path / "run", allow_real_device=True)
    assert result.stop_reason in STOP_REASONS


def test_nothing_runs_between_the_last_read_and_the_tap(
    tmp_path, device, clock, monkeypatch
):
    events: list[str] = []
    for name in ("dump", "foreground", "focused_window", "keyboard_shown", "tap"):
        original = getattr(device, name)

        def logged(*args, _name=name, _original=original, **kwargs):
            events.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(device, name, logged)

    def stop() -> bool:
        events.append("stop")
        return False

    run(device, clock, tmp_path / "run", stop=stop)
    taps = [i for i, e in enumerate(events) if e == "tap"]
    assert taps
    for index in taps:
        # dump, foreground, focused window, keyboard: then the tap, at once.
        assert events[index - 4 : index] == [
            "dump",
            "foreground",
            "focused_window",
            "keyboard_shown",
        ]


def test_a_stop_that_takes_long_cannot_slip_a_dialog_under_the_tap(
    tmp_path, device, android, clock
):
    covered = []

    def slow_stop() -> bool:
        # While the user's stop() "runs", a dialog comes up once a tap is due.
        if android.inputs and not covered:
            covered.append(True)
            android.show("cart")
        return False

    run(device, clock, tmp_path / "run", stop=slow_stop)
    assert covered
    assert not [i for i in android.inputs if i.kind == "tap" and i.screen == "cart"]


def test_a_stop_seen_once_is_remembered(tmp_path, device, android, clock):
    answers = iter([False, False, True])

    def flaky_stop() -> bool:
        return next(answers, False)

    result = run(device, clock, tmp_path / "run", stop=flaky_stop)
    assert result.stop_reason == "interrupted"
    assert android.inputs == []  # the launch went out, nothing was pressed


def test_a_stop_between_force_stop_and_start_sends_no_start(
    tmp_path, device, android, monkeypatch, clock
):
    original = device.tap

    def tap_then_cover(point: tuple[int, int]) -> tuple[int, int]:
        result = original(point)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)
    stopping = []
    stop_app = device.stop_app

    def stop_then_ask(package: str) -> None:
        stop_app(package)
        stopping.append(True)

    monkeypatch.setattr(device, "stop_app", stop_then_ask)
    launches = spy(monkeypatch, device, "launch", lambda *_a, **_k: None)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: bool(stopping))
    assert result.stop_reason == "interrupted"
    assert android.stopped == [SHOP]
    assert len(launches) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    relaunch = [r for r in records if r.get("action", {}).get("kind") == "relaunch"]
    end = [r for r in records if r.get("action_id") == relaunch[0]["action_id"]][-1]
    assert (end["not_sent"], end["reason"]) == (True, "start_not_sent")
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again" in summary


def test_a_back_that_is_not_sent_ends_the_run(
    tmp_path, device, android, clock, monkeypatch
):
    original_launch = device.launch

    def launch_into_another_app(*args, **kwargs):
        original_launch(*args, **kwargs)
        android.show("ext1")  # the engine will want BACK

    monkeypatch.setattr(device, "launch", launch_into_another_app)
    monkeypatch.setattr(device, "keyboard_shown", lambda: None)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "not_sent"
    assert not [i for i in android.inputs if i.kind == "key"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "back was not sent: keyboard_unknown" in summary
    assert android.stopped == []  # no relaunch followed


def test_a_clock_that_does_not_move_cannot_hold_the_pause(tmp_path, device, clock):
    driver, _, _ = _gate_run(tmp_path, device, clock)
    driver.settle = 1.0
    sleeps = []
    driver.sleep = sleeps.append  # the clock never moves
    driver.pause()
    assert 0 < len(sleeps) <= int(1.0 / crawl_driver.PAUSE_STEP) + 2


# --- round three ---


def test_a_window_that_takes_focus_during_the_dump_is_not_pressed(
    tmp_path, device, android, clock, monkeypatch
):
    out = tmp_path / "run"
    original_dump = device.dump
    popped = []

    def in_a_tap_gate() -> bool:
        # One more tap start line than taps sent: the gate is reading for it.
        path = out / "transitions.jsonl"
        records = lines(path) if path.exists() else []
        started = sum(r.get("action", {}).get("kind") == "tap" for r in records)
        sent = sum(i.kind == "tap" for i in android.inputs)
        return (
            started == sent + 1 and records[-1].get("action", {}).get("kind") == "tap"
        )

    def dump_then_popup(*args, **kwargs):
        gate = in_a_tap_gate()
        screen = original_dump(*args, **kwargs)
        if gate and android.inputs and not popped:  # once, after a press
            popped.append(True)
            android.focus_window = ("PopupWindow:9f8e7d", SHOP)  # the app's own
        return screen

    monkeypatch.setattr(device, "dump", dump_then_popup)
    run(device, clock, out)
    assert popped
    ends = [r for r in lines(out / "transitions.jsonl") if r.get("not_sent")]
    assert ends
    assert ends[0]["reason"] == "focus_changed"


def test_a_device_without_a_fingerprint_is_not_started(
    tmp_path, device, android, clock
):
    android.props["ro.build.fingerprint"] = ""
    out = tmp_path / "run"
    with pytest.raises(AdbError, match="fingerprint"):
        run(device, clock, out)
    assert not out.exists()
    assert android.inputs == []


@pytest.mark.usefixtures("android")
def test_a_transport_number_taken_before_the_run_is_refused(
    tmp_path, device, fake_adb, clock
):
    # Between making the Device and the crawl, a restarted server gave the
    # number to another device: nothing at all goes to that transport.
    fake_adb.devices[:] = [
        {"serial": "R58M1234", "state": "device", "transport_id": TRANSPORT}
    ]
    before = len(fake_adb.requests)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError):
        run(device, clock, out)
    assert not [r for r in fake_adb.requests[before:] if r.startswith("host:transport")]
    assert not out.exists()


def test_every_device_request_checks_the_listing_first(
    tmp_path, device, fake_adb, clock
):
    run(device, clock, tmp_path / "run", limits=Limits(actions=2))
    requests = fake_adb.requests
    for index, request in enumerate(requests):
        if request.startswith("host:transport-id:"):
            assert requests[index - 1] == "host:devices-l"


def test_a_stop_refusal_ends_the_run_without_another_read(
    tmp_path, device, android, clock
):
    def stop() -> bool:
        return bool(android.inputs)  # true from the first press on

    run(device, clock, tmp_path / "run", stop=stop)
    records = lines(tmp_path / "run" / "transitions.jsonl")
    assert records[-1].get("reason") in (None, "stopped")
    # The run ended right after the refusal or the press: no dump followed a
    # refused action.
    last_start = max(i for i, r in enumerate(records) if "action" in r)
    assert all("action" not in r for r in records[last_start + 1 :])
