"""Tests for the crawl driver: a fake Android app, a fake clock, files in tmp_path.

Every device is a strict `FakeAndroid` behind a fake adb server, so an input
that is not a tap, the BACK key or a launcher launch fails the test. Nothing
sleeps: the clock and the pause are injected.
"""

from __future__ import annotations

import json
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
from uiprobe.crawl import Limits
from uiprobe.crawl_driver import CrawlResult, crawl
from uiprobe.device import Device
from uiprobe.errors import AdbError, LaunchFailedError, NotEmulatorError
from uiprobe.guards import Policy
from uiprobe.runlog import RunFolderError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
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
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home")


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
    recheck: bool = True,
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
        recheck=recheck,
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
    assert clock.pauses == [1.0] * (result.actions - 1)


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
        (TypeError, {"recheck": None}),
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
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    assert not [
        s for s in starts if s["action"].get("element", {}).get("text") == "Browse"
    ]
    on_browse = [
        i
        for i in android.inputs
        if i.kind == "tap" and i.point is not None and 200 <= i.point[1] < 240
    ]
    assert not on_browse
    assert (out / "run.json").exists()


def test_without_recheck_the_screen_is_not_read_again(tmp_path, device, android, clock):
    first = run(device, clock, tmp_path / "with")
    dumps = android.dumps
    second = run(device, clock, tmp_path / "without", recheck=False)
    taps = sum(
        1
        for r in lines(tmp_path / "with" / "transitions.jsonl")
        if r.get("action", {}).get("kind") == "tap"
    )
    assert second.stop_reason == first.stop_reason == "exhausted"
    assert dumps - (android.dumps - dumps) == taps


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
    restarts = [kwargs["restart"] for _, kwargs in launches]
    assert restarts[0] is False
    assert restarts[1:]
    assert all(restarts[1:])
    assert len(restarts) == 1 + kinds.count("relaunch")
    assert android.stopped == [SHOP] * kinds.count("relaunch")
    for name in (out / "screens").iterdir():
        shown = json.loads(name.read_text(encoding="utf-8"))
        assert shown["app"]["package"] == SHOP


# --- stopping ---


def test_stop_between_actions_ends_with_interrupted(tmp_path, device, clock):
    calls = []

    def stop() -> bool:
        calls.append(1)
        return len(calls) > 3

    out = tmp_path / "run"
    result = run(device, clock, out, stop=stop)
    assert result.stop_reason == "interrupted"
    assert result.actions == 3
    transitions = lines(out / "transitions.jsonl")
    assert len(transitions) == 6
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
        fake_adb, SERIAL, SCREENS, start="home", secure_screencap="empty"
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
    assert set(clock.pauses) == {25.0}
    assert clock.now >= 5000.0 + 60


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
    assert _skipped_element(item, (), {}) == {}


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
