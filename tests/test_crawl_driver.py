"""Tests for the crawl driver: a fake Android app, a fake clock, files in tmp_path.

Every device is a strict `FakeAndroid` behind a fake adb server, so an input
that is not a tap, the BACK key or a launcher launch fails the test. Nothing
sleeps: the clock and the pause are injected.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import re
import time
from dataclasses import dataclass, fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

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
from uiprobe import adb as adb_module
from uiprobe import crawl_driver, runlog
from uiprobe import device as device_module
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
from uiprobe.device import IDENTITY_COMMAND, Device, FocusedWindow
from uiprobe.errors import (
    AdbError,
    DeviceNotFoundError,
    LaunchFailedError,
    NotEmulatorError,
)
from uiprobe.guards import DEFAULT_DENY_EXCEPTIONS, Policy
from uiprobe.model import Bounds
from uiprobe.runlog import RunFolderError, RunWriter

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.fake_adb_server import FakeAdbServer
    from uiprobe.crawl import Action
    from uiprobe.model import Screen as ModelScreen

SERIAL = "emulator-5554"
TRANSPORT = 7
SHOP = "com.example.shop"
OTHER = "com.example.other"
BUTTON = "android.widget.Button"
LAUNCHER = "android.intent.category.LAUNCHER"
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


# BACK on screens of other apps allowed, as before it became a choice.
BACK_POLICY = Policy(back_on_other_screens=True)


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
    policy: Policy | None = None,
    restart: bool = True,
) -> CrawlResult:
    return crawl(
        device,
        SHOP,
        out,
        policy=policy or Policy(),
        limits=limits or Limits(),
        screenshots=screenshots,
        allow_real_device=allow_real_device,
        settle=settle,
        stop=stop,
        clock=clock.monotonic,
        sleep=clock.sleep,
        restart=restart,
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
    assert start["deny_except"] == list(DEFAULT_DENY_EXCEPTIONS)

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
    # The run is planned with restart, and only the first launch force-stops
    # (then starts, as one action).
    assert [start["restart"], *(s.get("restart") for s in starts)] == [
        True,
        True,
        *[None] * (len(starts) - 1),
    ]
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
    assert android.stopped == [SHOP]  # the first launch's force-stop only
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
    # the pin reads the identity, then the emulator check: nothing else
    assert commands(fake_adb) == [IDENTITY_COMMAND, "getprop ro.kernel.qemu"]


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
    launches = spy(monkeypatch, device, "_launch_component", check("launch"))
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

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)
    launches = spy(monkeypatch, device, "_launch_component", crowd)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, restart=False)
    assert result.stop_reason in STOP_REASONS
    kinds = [
        r["action"]["kind"] for r in lines(out / "transitions.jsonl") if "action" in r
    ]
    assert "relaunch" in kinds
    # A relaunch is a force-stop, then a plain start of the launcher activity
    # its gate read, sent one after the other.
    assert all(args == (SHOP, f"{SHOP}/.Main") for args, _ in launches)
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
    # stop() turned True in the pause after the second press: the run ended
    # there, so that press has no end line (its result is not read).
    assert len(transitions) == 5
    assert "action" in transitions[-1]
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


def test_max_duration_is_judged_on_the_injected_clock(
    tmp_path, device, clock, monkeypatch
):
    # uiprobe's own clock is the same fake clock here, so the run's end by it
    # and by the injected clock is the same moment, whatever the machine does.
    share_clock(monkeypatch, clock)
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


# --- how the app is started ---


def test_the_crawl_reads_the_launcher_before_every_start(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)
    # restart=False: android.stopped then counts relaunches only.
    run(device, clock, tmp_path / "run", restart=False)
    sent = commands(fake_adb)
    resolve = (
        f"cmd package resolve-activity --brief -c {LAUNCHER} {SHOP};"
        " echo uiprobe-exit:$?"
    )
    start = (
        f"am start -W -a android.intent.action.MAIN -c {LAUNCHER} -n {SHOP}/.Main;"
        " echo uiprobe-exit:$?"
    )
    stop = f"am force-stop {SHOP}; echo uiprobe-exit:$?"
    changing = [c for c in sent if c.startswith(("am ", "monkey", "cmd "))]
    assert stop in changing  # there was a relaunch
    # Each start comes right after its own launcher read: the first launch
    # (read, start) and each relaunch (force-stop, read, start).
    starts = [i for i, c in enumerate(changing) if c == start]
    assert starts
    assert all(changing[i - 1] == resolve for i in starts)
    assert changing.count(resolve) == len(starts)
    for index, command in enumerate(changing):
        if command == stop:
            assert changing[index + 1 : index + 3] == [resolve, start]
    assert not any("monkey" in c for c in sent)


def test_a_relaunch_starts_the_launcher_as_it_is_now(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    # The app's launcher changes from .Main to .Detail between the first
    # launch and the relaunch: the relaunch starts .Detail.
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    reads: list[str] = []

    def launcher(command: str):
        reads.append(command)
        name = ".Main" if len(reads) == 1 else ".Detail"
        return (
            "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1"
            f" isDefault=true\n{SHOP}/{name}\nuiprobe-exit:0\n"
        ).encode()

    monkeypatch.setattr(device, "tap", tap_then_cover)
    android.override(re.compile(r"^cmd package"), launcher)
    # restart=False: android.stopped then counts relaunches only.
    run(device, clock, tmp_path / "run", restart=False)
    starts = [c for c in commands(fake_adb) if c.startswith("am start")]
    assert len(starts) >= 2
    assert f"-n {SHOP}/.Main;" in starts[0]
    assert all(f"-n {SHOP}/.Detail;" in c for c in starts[1:])


@pytest.mark.parametrize("restart", [False, True], ids=["no-restart", "restart"])
def test_an_unreadable_launcher_ends_the_crawl_at_the_first_launch(
    tmp_path, device, android, clock, fake_adb, restart
):
    # The launcher is read right before the first start: when it cannot be
    # read, the start is not sent and the run ends with error. With restart
    # it is read in the gate of the force-stop too, so nothing at all is
    # sent: the app is not force-stopped either.
    android.override(re.compile(r"^cmd package"), b"No activity found\n")
    out = tmp_path / "run"
    with pytest.raises(LaunchFailedError) as info:
        run(device, clock, out, restart=restart)
    assert info.value.details == {
        "package": SHOP,
        "reason": "launcher_unreadable",
        "output": "No activity found",
    }
    assert not any(c.startswith(("am ", "input ")) for c in commands(fake_adb))
    assert android.stopped == []
    records = lines(out / "transitions.jsonl")
    assert len(records) == 2
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (
        True,
        "launcher_unreadable",
    )
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped" not in summary
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))[
        "stop_reason"
    ] == ("error")


def test_an_unreadable_launcher_at_a_relaunch_sends_no_start(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    # The launcher read before the relaunch's start fails: the app was
    # force-stopped, the start is not sent, and the run ends with error.
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    reads: list[str] = []

    def launcher(command: str):
        reads.append(command)
        if len(reads) == 1:
            return android._shell(command)
        return b"No activity found\n"

    monkeypatch.setattr(device, "tap", tap_then_cover)
    android.override(re.compile(r"^cmd package"), launcher)
    out = tmp_path / "run"
    with pytest.raises(LaunchFailedError) as info:
        # restart=False: android.stopped then counts relaunches only.
        run(device, clock, out, restart=False)
    assert info.value.details["reason"] == "launcher_unreadable"
    assert android.stopped == [SHOP]
    starts = [c for c in commands(fake_adb) if c.startswith("am start")]
    assert len(starts) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (
        True,
        "start_not_sent",
    )
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"


def share_clock(monkeypatch, clock) -> None:
    """Make uiprobe's own clock (the one the device checks right before a
    request goes out) the test's clock, so that a test can move both."""
    monkeypatch.setattr(
        device_module,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep),
    )


def cover_after_browse(monkeypatch, device, android) -> None:
    """Bring screens of another app over the app after the tap on Browse,
    so that the engine relaunches."""
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)


def start_requests(fake_adb: FakeAdbServer) -> list[int]:
    """The positions of the start requests in the server's request log."""
    return [
        i for i, r in enumerate(fake_adb.requests) if r.startswith("shell:am start ")
    ]


def device_requests_after(fake_adb: FakeAdbServer, index: int) -> list[str]:
    return [
        r for r in fake_adb.requests[index + 1 :] if r.startswith(("shell:", "exec:"))
    ]


@pytest.mark.parametrize("nth", [1, 2], ids=["first-launch", "relaunch"])
def test_a_start_whose_reply_does_not_come_ends_the_crawl(
    tmp_path, device, android, monkeypatch, clock, fake_adb, nth
):
    cover_after_browse(monkeypatch, device, android)
    shell = device._client.shell
    starts: list[str] = []

    def silent_start(serial, command, *, timeout=None, **pin):
        if command.startswith("am start"):
            starts.append(command)
            if len(starts) == nth:
                shell(serial, command, timeout=timeout, **pin)  # it went out
                raise AdbError(
                    "timed out reading output from the adb server",
                    details={"reason": "timeout", "stage": "output"},
                )
        return shell(serial, command, timeout=timeout, **pin)

    monkeypatch.setattr(device._client, "shell", silent_start)
    out = tmp_path / "run"
    with pytest.raises(LaunchFailedError) as info:
        # The first launch with its force-stop; a relaunch after a first
        # launch without one, so that cover_after_browse covers the app.
        run(device, clock, out, restart=nth == 1)
    assert info.value.details["reason"] == "start_timeout"
    # Nothing more after that start: no stop, start, input or read.
    assert device_requests_after(fake_adb, start_requests(fake_adb)[-1]) == []
    records = lines(out / "transitions.jsonl")
    last = records[-1]
    assert last["schema"] == "uiprobe.transition_end/1"
    assert (last["failed"], last["reason"], last["outcome"]) == (
        True,
        "start_timeout",
        "unknown",
    )
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if nth == 1 else "relaunch")
    assert last["action_id"] == started[-1]["action_id"]
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))[
        "stop_reason"
    ] == ("error")
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert f"the reply of am start -W for {SHOP} did not come within 10" in summary
    assert "whether the app started is unknown" in summary


FAILED_START = (
    b"Starting: Intent { act=android.intent.action.MAIN"
    b" cat=[android.intent.category.LAUNCHER] cmp=com.example.shop/.Main }\n"
    b"Error type 3\n"
    b"Error: Activity class {com.example.shop/com.example.shop.Main} does not"
    b" exist.\nuiprobe-exit:1\n"
)


@pytest.mark.parametrize("nth", [1, 2], ids=["first-launch", "relaunch"])
def test_a_launch_that_fails_and_ends_the_crawl_gets_its_end_line(
    tmp_path, device, android, monkeypatch, clock, nth
):
    cover_after_browse(monkeypatch, device, android)
    calls: list[str] = []

    def answer(command: str):
        calls.append(command)
        return FAILED_START if len(calls) == nth else android._shell(command)

    android.override(re.compile(r"^am start"), answer)
    out = tmp_path / "run"
    with pytest.raises(LaunchFailedError) as info:
        # The first launch with its force-stop; a relaunch after a first
        # launch without one, so that cover_after_browse covers the app.
        run(device, clock, out, restart=nth == 1)
    assert info.value.details["reason"] == "start_failed"
    records = lines(out / "transitions.jsonl")
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if nth == 1 else "relaunch")
    ends = [r for r in records if r["action_id"] == started[-1]["action_id"]]
    assert ends[-1] == {
        "schema": "uiprobe.transition_end/1",
        "recorded_at": ends[-1]["recorded_at"],
        "action_id": started[-1]["action_id"],
        "failed": True,
        "reason": "start_failed",
        "outcome": "unknown",
    }


SETTINGS = "com.android.settings"
SEARCH = "com.google.android.settings.intelligence"
LAUNCH_DATA = Path(__file__).parent / "data" / "launch"


def launch_capture(name: str) -> bytes:
    """A capture of tests/data/launch without its ``#`` header."""
    raw = (LAUNCH_DATA / name).read_bytes().splitlines(keepends=True)
    return b"".join(line for line in raw if not line.startswith(b"#"))


def test_a_relaunch_that_names_another_apps_activity_ends_with_relaunch_limit(
    tmp_path, fake_adb, clock
):
    # On an API 36 emulator, a tap on the Settings search bar opened a screen
    # of another package; after am force-stop, am start of Settings brought
    # that screen back and named its activity (the capture below).
    screens = (
        Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
        Screen(
            "settings",
            f"{SETTINGS}/.Settings",
            (
                El((0, 0, 320, 100), text="Settings"),
                button(200, "Search settings", "search"),
            ),
        ),
        Screen(
            "search",
            f"{SEARCH}/.modules.search.activity.SearchActivity",
            (El((0, 0, 320, 640), text="Search"),),
        ),
    )
    phone = FakeAndroid(fake_adb, SERIAL, screens, start="home", transport_id=TRANSPORT)
    device = Device(adb_server=fake_adb.address)
    # The device waits for the app after each start with the test's clock.
    device._clock = clock.monotonic
    device._sleep = clock.sleep
    starts: list[str] = []

    def answer(command: str):
        starts.append(command)
        if len(starts) == 1:
            return phone._shell(command)  # the first launch, as the fake does
        return launch_capture("emu36-start-other-activity.txt")

    phone.override(re.compile(r"^am start"), answer)
    out = tmp_path / "run"
    result = crawl(
        device,
        SETTINGS,
        out,
        policy=Policy(),
        limits=Limits(),
        settle=0.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result.stop_reason == "relaunch_limit"
    assert len(starts) == 4  # the launch and 3 relaunches; the 4th is not sent
    taps = [i for i in phone.inputs if i.kind == "tap"]
    assert [t.screen for t in taps] == ["settings"]  # none while off target
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert f"{SETTINGS} was not in front after launch ({SEARCH})" in summary
    records = lines(out / "transitions.jsonl")
    assert not [r for r in records if r.get("failed")]


RESOLVER_REPLY = (
    b"Starting: Intent { act=android.intent.action.MAIN"
    b" cat=[android.intent.category.LAUNCHER] cmp=com.example.shop/.Main }\n"
    b"Status: ok\nLaunchState: COLD\n"
    b"Activity: android/com.android.internal.app.ResolverActivity\n"
    b"TotalTime: 5\nWaitTime: 6\nComplete\nuiprobe-exit:0\n"
)


def test_a_start_handed_to_the_system_chooser_does_not_end_the_crawl(
    tmp_path, device, android, clock, monkeypatch
):
    # Synthetic: the start reports Android's own chooser (owner "android").
    # The device waits for the app with the test's clock (it does not come).
    monkeypatch.setattr(device, "_clock", clock.monotonic)
    monkeypatch.setattr(device, "_sleep", clock.sleep)
    android.override(re.compile(r"^am start"), RESOLVER_REPLY)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason in STOP_REASONS
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert f"{SHOP} was not in front after launch (android)" in summary
    assert not [r for r in lines(out / "transitions.jsonl") if r.get("failed")]


@pytest.mark.usefixtures("android")
def test_the_device_compares_the_epoch_the_crawl_approved(
    tmp_path, device, monkeypatch, clock, fake_adb
):
    # Something proves the pin again right after the gate approved a tap (in
    # the review, a caller value's hook did): the device compares the epoch
    # the gate approved, not a fresh one, and sends nothing.
    device.pin_transport()
    final_check = crawl_driver._Run.final_check
    moved: list[int] = []

    def approve_then_move(self, path: str, approved: int) -> bool:
        approves = final_check(self, path, approved)
        if approves and path == "tap" and not moved:
            moved.append(len(fake_adb.commands))
            self.device._epoch += 2  # as a new proof of the pin would
            self.device._proven = self.device._epoch
        return approves

    monkeypatch.setattr(crawl_driver._Run, "final_check", approve_then_move)
    out = tmp_path / "run"
    run(device, clock, out)
    assert moved
    after = [c for _, _, c in fake_adb.commands[moved[0] :]]
    assert not after or not after[0].startswith("input ")
    records = lines(out / "transitions.jsonl")
    first_tap = next(r for r in records if r.get("action", {}).get("kind") == "tap")
    end = next(
        r
        for r in records
        if r["action_id"] == first_tap["action_id"] and "action" not in r
    )
    assert (end.get("not_sent"), end.get("reason")) == (True, "device_reconnected")


# --- uiprobe's own deadline, set once ---


class Trusted:
    """uiprobe's own clock (`device.time.monotonic`), apart from the
    caller's clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.sleeps: list[tuple[float, float]] = []  # (when, how long)
        monkeypatch.setattr(
            device_module,
            "time",
            SimpleNamespace(monotonic=lambda: self.now, sleep=lambda _s: None),
        )

    def attach(self, device: Device) -> None:
        """Make the device's own clock and pauses (its waits) this clock."""
        device._clock = lambda: self.now

        def sleep(seconds: float) -> None:
            self.sleeps.append((self.now, seconds))
            self.now += seconds

        device._sleep = sleep


@pytest.mark.usefixtures("android")
def test_a_late_caller_clock_cannot_move_the_deadline_later(
    tmp_path, device, monkeypatch, clock, fake_adb
):
    # The run may last 60 s by uiprobe's own clock from its start. At the
    # first launch's gate the caller's clock says 59 s, but 61 s have passed:
    # the limit the device checks stays at 60 s, so the start is not sent.
    trusted = Trusted(monkeypatch)
    gate = crawl_driver._Run.launch_gate

    def late_gate(self):
        trusted.now = 1061.0
        clock.now = 1059.0
        return gate(self)

    monkeypatch.setattr(crawl_driver._Run, "launch_gate", late_gate)
    out = tmp_path / "run"
    # The first launch without a force-stop, whose gate is launch_gate.
    result = run(device, clock, out, limits=Limits(minutes=1), restart=False)
    assert result.stop_reason == "max_duration"
    assert start_requests(fake_adb) == []
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "time_limit")


@pytest.mark.usefixtures("android")
def test_the_deadline_is_taken_before_the_first_caller_clock_value(
    tmp_path, device, monkeypatch, fake_adb
):
    # The caller's first clock value took 2 s to come (uiprobe's clock went
    # from 1000 to 1002), and the caller's clock stands still after it: the
    # run's end by uiprobe's clock is 1060, read before that first value,
    # not 1062. At 1061 the first launch is not sent.
    trusted = Trusted(monkeypatch)
    calls: list[float] = []

    def caller_clock() -> float:
        calls.append(trusted.now)
        if len(calls) == 1:
            trusted.now = 1002.0
        return 1000.0

    gate = crawl_driver._Run.launch_gate

    def late_gate(self):
        trusted.now = 1061.0
        return gate(self)

    monkeypatch.setattr(crawl_driver._Run, "launch_gate", late_gate)
    result = crawl(
        device,
        SHOP,
        tmp_path / "run",
        policy=Policy(),
        limits=Limits(minutes=1),
        settle=0.0,
        clock=caller_clock,
        sleep=lambda _s: None,
        restart=False,  # the first launch's gate is launch_gate
    )
    assert result.stop_reason == "max_duration"
    assert start_requests(fake_adb) == []


def test_each_clock_value_can_only_bring_the_end_earlier(
    tmp_path, device, monkeypatch, clock
):
    trusted = Trusted(monkeypatch)
    engine = CrawlEngine(SHOP, Policy(), Limits(minutes=1), started_at=1000.0)
    writer = RunWriter.create(
        tmp_path / "run", package=SHOP, limits=Limits(), deny_words=[], serial=SERIAL
    )
    _WRITERS.append(writer)
    readings = iter([1059.0, 1059.0, 1001.0])
    driver = crawl_driver._Run(
        device,
        SHOP,
        writer,
        engine,
        Policy(),
        emulator=True,
        screenshots=False,
        settle=0.0,
        deadline=1060.0,
        stop=None,
        clock=lambda: next(readings),
        sleep=clock.sleep,
        send_limit=1060.0,
    )
    driver.now()  # 59 s by the caller's clock at 1000: the end comes to 1001
    assert driver.send_by == 1001.0
    trusted.now = 1002.0
    driver.now()  # still 59 s at 1002 would say 1003: the end stays at 1001
    assert driver.send_by == 1001.0
    driver.now()  # an earlier caller value cannot move it later either
    assert driver.send_by == 1001.0
    assert driver.past()


def test_a_clock_value_too_large_for_a_float_is_a_range_error(tmp_path, device):
    with pytest.raises(ValueError, match="below 2\\*\\*40"):
        crawl(
            device,
            SHOP,
            tmp_path / "run",
            policy=Policy(),
            limits=Limits(),
            clock=lambda: 10**400,
        )
    assert not (tmp_path / "run").exists()


def test_a_policy_with_avoided_areas_crawls_as_before(tmp_path, device, android, clock):

    policy = Policy(avoid=(Bounds(0, 0, 10, 10),))
    result = run(device, clock, tmp_path / "run", policy=policy)
    assert result.stop_reason == "exhausted"
    assert (result.screens, result.actions) == (4, 7)
    kinds = [
        r["action"]["kind"]
        for r in lines(tmp_path / "run" / "transitions.jsonl")
        if "action" in r
    ]
    assert kinds == ["launch", "tap", "tap", "back", "back", "tap", "back"]
    assert android.stopped == [SHOP]  # the first launch's force-stop only


# --- uiprobe's own clock, read once per check ---


def test_the_shop_crawl_ends_as_on_main_with_scheduler_pauses_between_clock_reads(
    tmp_path, fake_adb, monkeypatch
):
    # uiprobe's own clock (the default `clock`), modeled as one time source
    # where 20 ms pass between any two reads (the scheduler), and settle
    # pauses that bring the run close to its minute. origin/main ends this
    # crawl exhausted with 4 screens and 7 actions (59.50 s in this model);
    # so must this one (59.80 s: it reads the clock a few more times, for
    # its checks before each send and observation): the clock is read once
    # per check, and no scheduling gap is taken off the run's time.
    FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)
    device = Device(adb_server=fake_adb.address)
    now = [1000.0]

    def monotonic() -> float:
        moment = now[0]
        now[0] += 0.02
        return moment

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(
        device_module, "time", SimpleNamespace(monotonic=monotonic, sleep=sleep)
    )
    device._clock = monotonic
    device._sleep = sleep
    result = crawl(
        device,
        SHOP,
        tmp_path / "run",
        policy=Policy(),
        limits=Limits(minutes=1),
        settle=9.80,
        sleep=sleep,
    )
    assert (result.stop_reason, result.screens, result.actions) == ("exhausted", 4, 7)
    assert now[0] - 1000.0 < 60.0


# --- main's behaviour kept ---

OTHER_REPLY = (
    b"Starting: Intent { act=android.intent.action.MAIN"
    b" cat=[android.intent.category.LAUNCHER] cmp=com.example.shop/.Main }\n"
    b"Status: ok\nLaunchState: COLD\nActivity: com.example.other/.One\n"
    b"TotalTime: 5\nWaitTime: 6\nComplete\nuiprobe-exit:0\n"
)


@pytest.mark.parametrize("back_on_other_screens", [False, True])
def test_a_start_that_hands_over_for_a_moment_is_waited_on(
    tmp_path, device, android, monkeypatch, clock, back_on_other_screens
):
    # The start names an activity of another app, whose screen is in front,
    # and the shop comes back to the front 2 s later. As on main, the launch
    # waits for it: no force-stop and no BACK to the other app before the
    # wait ends, and the crawl then taps the shop. The only force-stop is the
    # first launch's own, before its start.
    monkeypatch.setattr(device, "_clock", clock.monotonic)
    monkeypatch.setattr(device, "_sleep", clock.sleep)
    started: list[float] = []

    def handed_over(_command: str):
        started.append(clock.now)
        android.show("ext1")  # the other app's screen is in front
        return OTHER_REPLY

    original = device.foreground

    def comes_back() -> str | None:
        if started and clock.now >= started[0] + 2.0 and android.screen.name == "ext1":
            android.show("main")  # the shop comes back to the front
        return original()

    first_input: list[tuple[str, list[str]]] = []
    original_tap = device.tap
    original_key = device.key

    def tap(point, _send_by=None, _epoch=None):
        first_input.append(("tap", list(android.stopped)))
        return original_tap(point, _send_by=_send_by, _epoch=_epoch)

    def key(name, refused=None, _send_by=None, _epoch=None):
        first_input.append((f"key {android.screen.package}", list(android.stopped)))
        return original_key(name, refused=refused, _send_by=_send_by, _epoch=_epoch)

    android.override(re.compile(r"^am start"), handed_over)
    monkeypatch.setattr(device, "foreground", comes_back)
    monkeypatch.setattr(device, "tap", tap)
    monkeypatch.setattr(device, "key", key)
    policy = Policy(back_on_other_screens=back_on_other_screens)
    out = tmp_path / "run"
    run(device, clock, out, policy=policy, limits=Limits(actions=3))
    assert len(started) == 1  # one start, no relaunch
    assert first_input[0] == ("tap", [SHOP])  # the shop is tapped, no relaunch
    assert android.stopped == [SHOP]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "was not in front after launch" not in summary


def test_the_time_budget_starts_before_the_launcher_read(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    # As on main, the run's time starts right after the app's uid is read.
    # Making the run folder takes 56 s and the launcher read 5 s, so the
    # start would go out at 61 s of 60: it does not (counted from the
    # launcher read instead, it would go out at 56 s).
    share_clock(monkeypatch, clock)

    def slow_read(command: str):
        clock.now += 5.0
        return android._shell(command)

    original_create = crawl_driver.RunWriter.create

    def slow_create(*args, **kwargs):
        writer = original_create(*args, **kwargs)
        clock.now += 56.0
        return writer

    android.override(re.compile(r"^cmd package"), slow_read)
    monkeypatch.setattr(crawl_driver.RunWriter, "create", slow_create)
    result = run(device, clock, tmp_path / "run", limits=Limits(minutes=1))
    assert result.stop_reason == "max_duration"
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am start")]


@pytest.mark.parametrize("restart", [False, True], ids=["no-restart", "restart"])
def test_a_start_whose_launcher_read_ends_past_the_limit_is_not_sent(
    tmp_path, device, android, clock, fake_adb, restart
):
    # The injected clock moves only during the first launch's launcher read,
    # past the 1-minute limit (uiprobe's own clock does not move). The clock
    # is read after that read, before the last comparison: no start. With
    # restart that read is in the gate of the force-stop: nothing is sent.
    def slow_read(command: str):
        clock.now += 61.0
        return android._shell(command)

    android.override(re.compile(r"^cmd package"), slow_read)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(minutes=1), restart=restart)
    assert result.stop_reason == "max_duration"
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    assert android.stopped == []
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "time_limit")


def test_a_first_launch_whose_read_after_the_force_stop_ends_late_sends_no_start(
    tmp_path, device, android, clock, fake_adb
):
    # With restart the launcher is read twice: in the gate of the force-stop,
    # and after the force-stop, before the start. The second read ends past
    # the limit: the force-stop went out, the start does not.
    reads: list[str] = []

    def slow_second_read(command: str):
        reads.append(command)
        if len(reads) == 2:
            clock.now += 61.0
        return android._shell(command)

    android.override(re.compile(r"^cmd package"), slow_second_read)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(minutes=1))
    assert result.stop_reason == "max_duration"
    assert len(reads) == 2
    assert android.stopped == [SHOP]
    assert not [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (
        True,
        "start_not_sent",
    )


def test_a_launcher_unreadable_only_after_the_first_force_stop_sends_no_start(
    tmp_path, device, android, clock, fake_adb
):
    # The read in the gate of the force-stop works; the read after the
    # force-stop fails. The app was force-stopped, the start is not sent, the
    # launch ends as start_not_sent and the run with launch_failed.
    reads: list[str] = []

    def second_read_fails(command: str):
        reads.append(command)
        if len(reads) == 1:
            return android._shell(command)
        return b"No activity found\n"

    android.override(re.compile(r"^cmd package"), second_read_fails)
    out = tmp_path / "run"
    with pytest.raises(LaunchFailedError) as info:
        run(device, clock, out)
    assert info.value.details["reason"] == "launcher_unreadable"
    assert len(reads) == 2
    assert android.stopped == [SHOP]
    assert changing_commands(fake_adb) == [RESOLVE, FORCE_STOP, RESOLVE]
    records = lines(out / "transitions.jsonl")
    assert len(records) == 2
    assert records[0]["restart"] is True
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (
        True,
        "start_not_sent",
    )
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert (
        f"{SHOP} was force-stopped at the start of the run but not started"
        " (launcher_unreadable)" in summary
    )
    run_json = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_json["stop_reason"] == "error"


def test_a_relaunch_whose_launcher_read_ends_past_the_limit_sends_no_start(
    tmp_path, device, android, clock, fake_adb, monkeypatch
):
    # The same for a relaunch: force-stop, launcher read (the injected clock
    # crosses the limit during it), the clock, and no start.
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    reads: list[float] = []

    def slow_second_read(command: str):
        reads.append(clock.now)
        if len(reads) == 2:
            clock.now += 10_000.0  # far past the run's limit
        return android._shell(command)

    monkeypatch.setattr(device, "tap", tap_then_cover)
    android.override(re.compile(r"^cmd package"), slow_second_read)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, restart=False)
    assert result.stop_reason == "max_duration"
    assert android.stopped == [SHOP]  # the relaunch's force-stop went out
    starts = [c for _, _, c in fake_adb.commands if c.startswith("am start")]
    assert len(starts) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    relaunch = [r for r in records if r.get("action", {}).get("kind") == "relaunch"]
    end = [r for r in records if r.get("action_id") == relaunch[0]["action_id"]][-1]
    assert (end["not_sent"], end["reason"]) == (True, "start_not_sent")


def test_the_policy_copy_keeps_every_field():
    # One value per Policy field, none of them the default: a field added to
    # Policy fails here until it is listed, and a field the crawl's copy
    # leaves out comes back as its default and fails below.
    values = {
        "deny_words": ("zap",),
        "confirm_words": ("yes please",),
        "tap_unlabeled": True,
        "avoid": (Bounds(1, 2, 3, 4),),
        "skip_number_only": False,
        "skip_toggles": False,
        "refuse_dialog_confirm": False,
        "edges": (5, 6, 7, 8),
        "min_visible_px": 9,
        "irreversible_phrases": ("for good",),
        "skip_irreversible": False,
        "back_on_other_screens": True,
        "check_tap_window": False,
        "deny_exceptions": ("automatically",),
        "skip_two_target_rows": False,
        "skip_rows_with_buttons": True,
        "skip_navigate_up": False,
    }
    assert set(values) == {f.name for f in fields(Policy)}
    default = Policy()
    original = Policy(**values)
    copy = crawl_driver._plain_policy(original)
    for name, value in values.items():
        assert getattr(default, name) != value, name
        assert getattr(copy, name) == value, name


# --- a start that went out is recorded as sent ---


def _ends_of(records: list[dict], kind: str) -> list[dict]:
    """The end lines of the last action of `kind`."""
    start = [r for r in records if r.get("action", {}).get("kind") == kind][-1]
    return [
        r for r in records if r["action_id"] == start["action_id"] and "action" not in r
    ]


@pytest.mark.parametrize("end", ["time_limit", "stop"])
def test_a_first_start_that_went_out_is_not_recorded_as_unsent(
    tmp_path, device, android, monkeypatch, clock, end
):
    # am start was sent and its reply came; the time limit (or a stop) comes
    # while the launch waits for the app in front. The launch stays a sent
    # action (no not_sent end line); the run ends at the next step.
    share_clock(monkeypatch, clock)
    asked: list[bool] = []

    def start_then_end(command: str):
        reply = android._shell(command)
        if end == "time_limit":
            clock.now += 3600.0
        else:
            asked.append(True)
        return reply

    android.override(re.compile(r"^am start"), start_then_end)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(minutes=1), stop=lambda: bool(asked))
    expected = "max_duration" if end == "time_limit" else "interrupted"
    assert result.stop_reason == expected
    ends = _ends_of(lines(out / "transitions.jsonl"), "launch")
    assert not [e for e in ends if e.get("not_sent")]


@pytest.mark.parametrize("end", ["time_limit", "stop"])
def test_a_relaunch_start_that_went_out_is_not_recorded_as_unsent(
    tmp_path, device, android, monkeypatch, clock, end
):
    share_clock(monkeypatch, clock)
    cover_after_browse(monkeypatch, device, android)
    asked: list[bool] = []
    starts: list[str] = []

    def start_then_end(command: str):
        starts.append(command)
        reply = android._shell(command)
        if len(starts) == 2:  # the relaunch's start went out and was answered
            if end == "time_limit":
                clock.now += 3600.0
            else:
                asked.append(True)
        return reply

    android.override(re.compile(r"^am start"), start_then_end)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(
        device,
        clock,
        out,
        limits=Limits(minutes=1),
        stop=lambda: bool(asked),
        restart=False,
    )
    assert len(starts) == 2
    expected = "max_duration" if end == "time_limit" else "interrupted"
    assert result.stop_reason == expected
    ends = _ends_of(lines(out / "transitions.jsonl"), "relaunch")
    assert not [e for e in ends if e.get("not_sent")]  # no start_not_sent either
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "not started again" not in summary


# --- stop and the time limit right before a request ---


def test_a_stop_asked_during_the_relaunch_pin_check_sends_no_start(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    cover_after_browse(monkeypatch, device, android)
    asked: list[bool] = []
    original = Device.check_pin

    def check_then_stop(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if android.stopped:  # the relaunch's check, after its force-stop
            asked.append(True)

    monkeypatch.setattr(Device, "check_pin", check_then_stop)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, stop=lambda: bool(asked), restart=False)
    assert result.stop_reason == "interrupted"
    assert android.stopped == [SHOP]
    assert len(start_requests(fake_adb)) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    assert records[-1]["reason"] == "start_not_sent"
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again (stopped)" in summary


def test_a_stop_asked_during_the_gate_pin_check_sends_no_tap(
    tmp_path, device, android, monkeypatch, clock
):
    asked: list[bool] = []
    in_gate: list[bool] = []
    gate = crawl_driver._Run.gate

    def marked_gate(self, action, observation):
        in_gate.append(isinstance(action, Tap))
        try:
            return gate(self, action, observation)
        finally:
            in_gate.pop()

    original = Device.check_pin

    def check_then_stop(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if in_gate and in_gate[-1]:
            asked.append(True)

    monkeypatch.setattr(crawl_driver._Run, "gate", marked_gate)
    monkeypatch.setattr(Device, "check_pin", check_then_stop)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: bool(asked))
    assert asked
    assert result.stop_reason == "interrupted"
    assert android.inputs == []
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "stopped")


def test_a_start_is_not_sent_once_the_time_limit_passed_during_the_listing(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    share_clock(monkeypatch, clock)
    original = Device._check_listed
    crossed: list[bool] = []

    def listed_then_late(self: Device, *, strict: bool = False) -> None:
        original(self, strict=strict)
        if strict and not crossed:  # the first request that changes the device
            crossed.append(True)
            clock.now += 3600.0  # the listing took long

    monkeypatch.setattr(Device, "_check_listed", listed_then_late)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert crossed
    assert result.stop_reason == "max_duration"
    assert start_requests(fake_adb) == []
    assert android.screen.name == "home"
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "time_limit")


@pytest.mark.usefixtures("android")
def test_a_start_is_not_sent_once_the_time_limit_passed_selecting_the_transport(
    tmp_path, device, monkeypatch, clock, fake_adb
):
    # The adb server took long to acknowledge host:transport-id:N for the
    # start; the check on that connection, right before the service request,
    # holds it back.
    share_clock(monkeypatch, clock)
    original_listed = Device._check_listed
    original_send = adb_module._send_request
    armed: list[bool] = []

    def listed(self: Device, *, strict: bool = False) -> None:
        original_listed(self, strict=strict)
        if strict:
            armed.append(True)

    def send_request(sock, request: str):
        message = original_send(sock, request)
        if armed and request.startswith("host:transport-id:"):
            armed.clear()
            clock.now += 3600.0  # the acknowledgement came late
        return message

    monkeypatch.setattr(Device, "_check_listed", listed)
    monkeypatch.setattr(adb_module, "_send_request", send_request)
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "max_duration"
    assert start_requests(fake_adb) == []
    records = lines(out / "transitions.jsonl")
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, "time_limit")


def test_a_relaunch_start_is_not_sent_once_the_time_limit_passed(
    tmp_path, device, android, monkeypatch, clock, fake_adb
):
    share_clock(monkeypatch, clock)
    cover_after_browse(monkeypatch, device, android)
    original = Device._check_listed

    def listed_then_late(self: Device, *, strict: bool = False) -> None:
        original(self, strict=strict)
        if strict and android.stopped:  # the start after the force-stop
            clock.now += 3600.0

    monkeypatch.setattr(Device, "_check_listed", listed_then_late)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, restart=False)
    assert result.stop_reason == "max_duration"
    assert android.stopped == [SHOP]
    assert len(start_requests(fake_adb)) == 1  # the first launch only
    records = lines(out / "transitions.jsonl")
    assert records[-1]["reason"] == "start_not_sent"
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped but not started again (time_limit)" in summary


# --- a launch that leaves another app in front ---


def test_a_launch_that_is_not_in_front_does_not_end_the_crawl(
    tmp_path, device, android, monkeypatch, clock
):
    # A launch waits with the device's own clock; keep it off real time.
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

    monkeypatch.setattr(device, "_launch_component", refuse)
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
    policy: Policy | None = None,
) -> tuple[crawl_driver._Run, CrawlEngine, Observation]:
    """A run after its launch, with the first observation, for direct sends."""
    policy = policy or Policy()
    device.pin_transport()
    engine = CrawlEngine(SHOP, policy, Limits(minutes=1), started_at=clock.now)
    writer = RunWriter.create(
        tmp_path / "run",
        package=SHOP,
        limits=Limits(),
        deny_words=[],
        serial=SERIAL,
        back_on_other_screens=policy.back_on_other_screens,
    )
    _WRITERS.append(writer)
    driver = crawl_driver._Run(
        device,
        SHOP,
        writer,
        engine,
        policy,
        emulator=True,
        app_uid=device.package_uid(SHOP),
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
    ("back", "focus_other", "focus_other"),
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
        emulator=True,
        app_uid=device.package_uid(SHOP),
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
        # Right after the emulator check: another device (another boot id)
        # takes the serial under a new transport number.
        fake_adb.devices[:] = [
            {"serial": SERIAL, "state": "device", "transport_id": TRANSPORT + 1}
        ]
        android.boot_id = "0f1e2d3c-4b5a-4968-8778-695a4b3c2d1e"

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
    # The new number was asked only for the identity, which did not match.
    assert set(selectors) <= {
        f"host:transport-id:{TRANSPORT}",
        f"host:transport-id:{TRANSPORT + 1}",
    }
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

    def tap(point, _send_by=None, _epoch=None):
        result = original(point, _send_by=_send_by, _epoch=_epoch)
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

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)
    stopping = []
    stop_app = device.stop_app

    def stop_then_ask(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        stop_app(package, _send_by=_send_by, _epoch=_epoch)
        stopping.append(True)

    monkeypatch.setattr(device, "stop_app", stop_then_ask)
    launches = spy(monkeypatch, device, "_launch_component", lambda *_a, **_k: None)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, stop=lambda: bool(stopping), restart=False)
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
    original_launch = device._launch_component

    def launch_into_another_app(*args, **kwargs):
        original_launch(*args, **kwargs)
        android.show("ext1")  # the engine will want BACK

    monkeypatch.setattr(device, "_launch_component", launch_into_another_app)
    monkeypatch.setattr(device, "keyboard_shown", lambda: None)
    out = tmp_path / "run"
    result = run(device, clock, out, policy=BACK_POLICY)
    assert result.stop_reason == "not_sent"
    assert not [i for i in android.inputs if i.kind == "key"]
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "back was not sent: keyboard_unknown" in summary
    assert android.stopped == [SHOP]  # the first launch's; no relaunch followed


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


# --- the two last gaps ---


@pytest.mark.parametrize("restart", [False, True], ids=["relaunch", "first-launch"])
def test_a_changed_identity_after_the_force_stop_sends_no_start(
    tmp_path, device, android, monkeypatch, clock, restart
):
    # The identity changes right after the force-stop (of a relaunch, or of
    # the first launch with restart): the pin check before the start raises
    # device_changed. The start was not sent, so the action is closed as
    # start_not_sent (not failed, not left open), then the error is raised.
    original = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    monkeypatch.setattr(device, "tap", tap_then_cover)
    stop_app = device.stop_app

    def stop_then_swap(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        stop_app(package, _send_by=_send_by, _epoch=_epoch)
        android.props["ro.build.fingerprint"] = "other/build:36/X/9:user/k"

    monkeypatch.setattr(device, "stop_app", stop_then_swap)
    launches = spy(monkeypatch, device, "_launch_component", lambda *_a, **_k: None)
    out = tmp_path / "run"
    with pytest.raises(DeviceNotFoundError) as info:
        # restart=False: android.stopped then counts relaunches only.
        run(device, clock, out, restart=restart)
    assert info.value.details["reason"] == "device_changed"
    assert android.stopped == [SHOP]
    # No start after the force-stop (without restart, the first launch's).
    assert len(launches) == (0 if restart else 1)
    records = lines(out / "transitions.jsonl")
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if restart else "relaunch")
    assert records[-1] == {
        "schema": "uiprobe.transition_end/1",
        "recorded_at": records[-1]["recorded_at"],
        "action_id": started[-1]["action_id"],
        "not_sent": True,
        "reason": "start_not_sent",
    }
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    what = (
        "at the start of the run but not started"
        if restart
        else "but not started again"
    )
    assert f"{SHOP} was force-stopped {what} (device_changed)" in summary
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "error"


@pytest.mark.parametrize("restart", [False, True], ids=["relaunch", "first-launch"])
def test_an_interrupt_after_the_force_stop_closes_the_start_as_not_sent(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # Ctrl+C arrives in the pin check between the force-stop and the start:
    # the start was not sent, the action is closed as start_not_sent, then
    # the interrupt is raised again (stop() does not ask to return).
    # Without restart the first force-stop is the relaunch's (the cover
    # brings it); with restart it is the first launch's.
    cover_after_browse(monkeypatch, device, android)
    original = Device.check_pin
    interrupted: list[bool] = []

    def check_then_interrupt(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if android.stopped and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt

    monkeypatch.setattr(Device, "check_pin", check_then_interrupt)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out, restart=restart)
    assert interrupted
    assert len(start_requests(fake_adb)) == (0 if restart else 1)
    records = lines(out / "transitions.jsonl")
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if restart else "relaunch")
    assert (records[-1]["action_id"], records[-1]["reason"]) == (
        started[-1]["action_id"],
        "start_not_sent",
    )
    assert records[-1]["not_sent"] is True
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "force-stopped" in summary
    assert "(interrupted)" in summary
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "interrupted"


@pytest.mark.parametrize("restart", [False, True], ids=["no-restart", "restart"])
def test_a_failed_write_of_the_not_sent_line_keeps_the_read_error(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # The adb connection is lost in the first launcher read, and the end line
    # that closes the launch cannot be written: the error raised is still the
    # read's, not the run folder's.
    def lost(package: str) -> str:
        del package
        raise AdbError("connection closed", details={})

    original = RunWriter.write_transition_end

    def refuse_not_sent(self, *args, **kwargs):
        if kwargs.get("not_sent") is not None:
            raise RunFolderError("disk full", details={"path": "x"})
        return original(self, *args, **kwargs)

    monkeypatch.setattr(device, "_start_component", lost)
    monkeypatch.setattr(RunWriter, "write_transition_end", refuse_not_sent)
    out = tmp_path / "run"
    with pytest.raises(AdbError, match="connection closed"):
        run(device, clock, out, restart=restart)
    assert not [c for c in commands(fake_adb) if c.startswith(("am ", "input "))]
    assert android.stopped == []
    assert len(lines(out / "transitions.jsonl")) == 1  # the end line failed
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "error"


@pytest.mark.parametrize("restart", [False, True], ids=["relaunch", "first-launch"])
def test_an_interrupt_in_the_last_comparison_before_the_start_closes_it(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # Ctrl+C arrives in the last comparison of the pin before the start,
    # after the force-stop went out: the start is not sent, the action is
    # closed as start_not_sent with the note, and the interrupt is raised.
    cover_after_browse(monkeypatch, device, android)
    final_check = crawl_driver._Run.final_check
    interrupted: list[bool] = []

    def interrupt_at_start(self, path: str, approved: int) -> bool:
        if path == "start" and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt
        return final_check(self, path, approved)

    monkeypatch.setattr(crawl_driver._Run, "final_check", interrupt_at_start)
    out = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out, restart=restart)
    assert interrupted
    assert android.stopped == [SHOP]
    assert len(start_requests(fake_adb)) == (0 if restart else 1)
    records = lines(out / "transitions.jsonl")
    started = [r for r in records if "action" in r]
    assert started[-1]["action"]["kind"] == ("launch" if restart else "relaunch")
    assert records[-1] == {
        "schema": "uiprobe.transition_end/1",
        "recorded_at": records[-1]["recorded_at"],
        "action_id": started[-1]["action_id"],
        "not_sent": True,
        "reason": "start_not_sent",
    }
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    what = (
        "at the start of the run but not started"
        if restart
        else "but not started again"
    )
    assert f"{SHOP} was force-stopped {what} (interrupted)" in summary
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "interrupted"


@pytest.mark.parametrize("restart", [False, True], ids=["no-restart", "restart"])
def test_a_lost_connection_in_the_first_launcher_read_closes_the_launch(
    tmp_path, device, android, monkeypatch, clock, fake_adb, restart
):
    # The adb connection is lost in the first launcher read, before anything
    # was sent: the launch is closed as not sent (adb_error), then the error
    # is raised.
    def lost(package: str) -> str:
        del package
        raise AdbError("connection closed", details={})

    monkeypatch.setattr(device, "_start_component", lost)
    out = tmp_path / "run"
    with pytest.raises(AdbError):
        run(device, clock, out, restart=restart)
    assert not [c for c in commands(fake_adb) if c.startswith(("am ", "input "))]
    assert android.stopped == []
    records = lines(out / "transitions.jsonl")
    assert len(records) == 2
    assert records[1] == {
        "schema": "uiprobe.transition_end/1",
        "recorded_at": records[1]["recorded_at"],
        "action_id": 1,
        "not_sent": True,
        "reason": "adb_error",
    }
    assert json.loads((out / "run.json").read_bytes())["stop_reason"] == "error"


@pytest.mark.parametrize("kind", ["tap", "back"])
def test_a_stop_seen_in_the_pause_sends_no_further_request(
    tmp_path, device, android, fake_adb, clock, monkeypatch, kind
):
    stopping = []
    marks: list[int] = []
    if kind == "back":
        original_launch = device._launch_component

        def launch_into_another_app(*args, **kwargs):
            original_launch(*args, **kwargs)
            android.show("ext1")  # the engine will press BACK

        monkeypatch.setattr(device, "_launch_component", launch_into_another_app)
    sender = device.tap if kind == "tap" else device.key

    def send_then_stop(*args, **kwargs):
        result = sender(*args, **kwargs)
        stopping.append(True)  # stop() turns True during the pause after it
        marks.append(len(fake_adb.requests))
        return result

    monkeypatch.setattr(device, kind if kind == "tap" else "key", send_then_stop)
    out = tmp_path / "run"
    result = run(
        device,
        clock,
        out,
        stop=lambda: bool(stopping),
        settle=1.0,
        policy=BACK_POLICY if kind == "back" else None,
    )
    assert result.stop_reason == "interrupted"
    assert fake_adb.requests[marks[0] :] == []  # not one request after it
    records = lines(out / "transitions.jsonl")
    assert "action" in records[-1]  # its start line has no end line


# --- screens of other apps ---


def _launch_under(monkeypatch, device, android, *names):
    """Make every launch end with `names` (screens of another app) in front."""
    original_launch = device._launch_component

    def launch_then_cover(*args, **kwargs):
        original_launch(*args, **kwargs)
        for name in names:
            android.show(name)

    monkeypatch.setattr(device, "_launch_component", launch_then_cover)


def _inputs_sent(fake_adb: FakeAdbServer) -> list[str]:
    return [r for r in fake_adb.requests if "input " in r]


def test_by_default_a_screen_of_another_app_gets_no_input(
    tmp_path, device, android, fake_adb, clock, monkeypatch
):
    _launch_under(monkeypatch, device, android, "ext1")
    out = tmp_path / "run"
    result = run(device, clock, out)
    assert result.stop_reason == "relaunch_limit"
    # not one input request reached the adb server, only relaunches of the app
    assert _inputs_sent(fake_adb) == []
    assert android.inputs == []
    assert android.stopped == [SHOP] * 4  # the first launch and 3 relaunches
    records = lines(out / "transitions.jsonl")
    starts = [r for r in records if "action" in r]
    assert [r["action"]["kind"] for r in starts] == [
        "launch",
        "relaunch",
        "relaunch",
        "relaunch",
    ]
    assert "off_target" not in starts[0]
    assert starts[0]["restart"] is True
    assert not any("restart" in start for start in starts[1:])
    for start in starts[1:]:
        assert start["off_target"] == {
            "package": OTHER,
            "foreground": OTHER,
            "focus": OTHER,
        }
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["back_on_other_screens"] is False


def test_with_back_on_other_screens_back_goes_to_the_other_app(
    tmp_path, device, android, fake_adb, clock, monkeypatch
):
    # Three screens of the other app over the shop: two BACKs leave one, so
    # every round is BACK, BACK, relaunch, as before the choice existed.
    _launch_under(monkeypatch, device, android, "ext1", "ext2", "ext3")
    out = tmp_path / "run"
    result = run(device, clock, out, policy=BACK_POLICY)
    assert result.stop_reason == "relaunch_limit"
    keys = [i for i in android.inputs if i.kind == "key"]
    assert len(keys) == 2 * 4
    assert {i.screen for i in keys} == {"ext3", "ext2"}
    assert len(_inputs_sent(fake_adb)) == 2 * 4
    assert android.stopped == [SHOP] * 4  # the first launch and 3 relaunches
    records = lines(out / "transitions.jsonl")
    starts = [r for r in records if "action" in r]
    assert [r["action"]["kind"] for r in starts] == [
        "launch",
        *(["back", "back", "relaunch"] * 3),
        "back",
        "back",
    ]
    for start in starts[1:]:
        assert start["off_target"] == {
            "package": OTHER,
            "foreground": OTHER,
            "focus": OTHER,
        }
    begin = json.loads((out / "run-start.json").read_bytes())
    assert begin["back_on_other_screens"] is True


def test_a_back_that_leaves_the_app_is_followed_by_a_relaunch(
    tmp_path, device, android, clock, monkeypatch
):
    # Browse replaces the shop's task, so it is the bottom of the real stack
    # while the engine counts it one above the root: its navigation BACK
    # leaves the app for the launcher. By default the launcher gets nothing.
    original_tap = device.tap

    def tap_then_replace(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original_tap(point, _send_by=_send_by, _epoch=_epoch)
        if android.screen.name == "browse":
            android.stack[:] = ["home", "browse"]
        return result

    monkeypatch.setattr(device, "tap", tap_then_replace)
    out = tmp_path / "run"
    run(device, clock, out, limits=Limits(actions=12))
    starts = [r for r in lines(out / "transitions.jsonl") if "action" in r]
    on_launcher = [
        i for i, r in enumerate(starts) if r.get("off_target", {}).get("package")
    ]
    assert on_launcher  # the BACK left the app
    first = on_launcher[0]
    assert starts[first - 1]["action"]["kind"] == "back"
    for i in on_launcher:
        assert starts[i]["action"]["kind"] == "relaunch"
        assert starts[i]["off_target"] == {
            "package": "com.example.launcher",
            "foreground": "com.example.launcher",
            "focus": "com.example.launcher",
        }
    # every key went to a screen of the shop, none to the launcher
    keys = [i for i in android.inputs if i.kind == "key"]
    assert keys
    assert all(i.screen in {"main", "browse", "detail", "cart"} for i in keys)


def _start_lines(tmp_path: Path) -> list[dict[str, object]]:
    return [r for r in lines(tmp_path / "run" / "transitions.jsonl") if "action" in r]


def test_a_back_on_the_apps_screen_is_not_sent_while_another_window_has_focus(
    tmp_path, device, android, clock, monkeypatch
):
    # The dump and the foreground name the app, but a window of System UI has
    # held the input focus since before the BACK was chosen: the BACK would go
    # to that window, so it is not sent.
    monkeypatch.setattr(device, "focused_window", lambda: SHADE)
    driver, _, observation = _gate_run(tmp_path, device, clock)
    assert observation.foreground == SHOP
    inputs = len(android.inputs)
    driver.send(Back(), observation)
    assert len(android.inputs) == inputs
    assert _not_sent_reason(tmp_path) == "focus_other"
    assert "off_target" not in _start_lines(tmp_path)[-1]


THIRD = FocusedWindow("com.example.third", "com.example.third/.Pop", "5d6e7f8")


@pytest.mark.parametrize("focus_moves", [False, True])
def test_an_opted_in_back_on_another_apps_screen_needs_no_app_focus(
    tmp_path, device, android, clock, monkeypatch, focus_moves
):
    # Dump, foreground and focus name three different apps: the start line
    # must take the focus owner from the focused window, where a BACK goes.
    driver, _, _ = _gate_run(tmp_path, device, clock, policy=BACK_POLICY)
    android.show("ext1")
    monkeypatch.setattr(device, "focused_window", lambda: THIRD)
    observation = driver.observe()
    if focus_moves:
        monkeypatch.setattr(device, "focused_window", lambda: SHADE)
    keys = len([i for i in android.inputs if i.kind == "key"])
    driver.send(Back(), observation)
    sent = len([i for i in android.inputs if i.kind == "key"]) - keys
    assert _start_lines(tmp_path)[-1]["off_target"] == {
        "package": OTHER,
        "foreground": OTHER,
        "focus": "com.example.third",
    }
    if focus_moves:
        assert sent == 0
        assert _not_sent_reason(tmp_path) == "changed_before_send"
    else:
        assert sent == 1
        assert _not_sent_reason(tmp_path) is None
    begin = json.loads((tmp_path / "run" / "run-start.json").read_bytes())
    assert begin["back_on_other_screens"] is True


def test_off_target_names_the_dump_and_the_foreground_apart(
    tmp_path, device, android, clock, monkeypatch
):
    # The dump shows the shop while another app is said to be in front.
    driver, _, _ = _gate_run(tmp_path, device, clock)
    monkeypatch.setattr(device, "foreground", lambda: OTHER)
    observation = driver.observe()
    assert observation.screen is not None
    assert observation.screen.package == SHOP
    driver.send(Relaunch(), observation)
    assert _start_lines(tmp_path)[-1]["off_target"] == {
        "package": SHOP,
        "foreground": OTHER,
        "focus": SHOP,
    }
    assert android.stopped == [SHOP]


def test_off_target_with_an_unknown_foreground_has_no_foreground(
    tmp_path, device, android, clock, monkeypatch
):
    driver, _, _ = _gate_run(tmp_path, device, clock)
    monkeypatch.setattr(device, "foreground", lambda: None)
    observation = driver.observe()
    driver.send(Relaunch(), observation)
    assert _start_lines(tmp_path)[-1]["off_target"] == {
        "package": SHOP,
        "focus": SHOP,
    }
    # the gate cannot read the foreground, so nothing was force-stopped
    assert _not_sent_reason(tmp_path) == "foreground_unknown"
    assert android.stopped == []


def test_a_relaunch_decided_on_the_apps_screen_has_no_off_target(
    tmp_path, device, android, clock
):
    driver, _, observation = _gate_run(tmp_path, device, clock)
    driver.send(Relaunch(), observation)
    assert "off_target" not in _start_lines(tmp_path)[-1]
    assert android.stopped == [SHOP]


def test_a_tap_whose_reads_end_past_the_limit_is_not_sent(
    tmp_path: Path, device, android, clock, monkeypatch
):
    # Each dump takes 19 s on the injected clock alone (uiprobe's own clock
    # does not move): the gate's own dump before the second tap crosses the
    # 1-minute limit, and the gate reads the clock after its reads, before
    # its last comparison, so that tap must not go out (as on main).
    original = Device.dump

    def slow_dump(self: Device, **kwargs: object) -> object:
        clock.now += 19.0
        return original(self, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(Device, "dump", slow_dump)
    out = tmp_path / "run"
    started = clock.now
    result = run(device, clock, out, limits=Limits(minutes=1))
    assert result.stop_reason == "max_duration"
    taps = [i for i in android.inputs if i.kind == "tap"]
    sent_at = [
        r for r in lines(out / "transitions.jsonl") if r.get("reason") == "time_limit"
    ]
    assert sent_at, "the late tap is closed as not sent"
    assert clock.now - started >= 60
    # Every tap that went out was decided and checked within the limit: the
    # first one (at 38 s), not the second (its reads end at 77 s).
    assert len(taps) == 1
    assert dict(result.not_sent).get("time_limit") == 1


def _starts(fake_adb: FakeAdbServer) -> list[str]:
    return [r for r in fake_adb.requests if r.startswith("shell:am start ")]


def test_a_launch_whose_pin_check_ends_past_the_limit_is_not_sent(
    tmp_path: Path, device, android, clock, monkeypatch, fake_adb
):
    assert android.serial == SERIAL
    original = Device.check_pin
    checks = [0]

    def slow_check(self: Device, **kwargs: object) -> None:
        original(self, **kwargs)  # ty: ignore[invalid-argument-type]
        checks[0] += 1
        if checks[0] == 1:
            clock.now += 61.0  # the first launch's pin check crosses the limit

    monkeypatch.setattr(Device, "check_pin", slow_check)
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(minutes=1))
    assert result.stop_reason == "max_duration"
    assert _starts(fake_adb) == []
    assert dict(result.not_sent) == {"time_limit": 1}


def test_a_restart_whose_pin_check_ends_past_the_limit_is_only_half_sent(
    tmp_path: Path, device, android, clock, monkeypatch, fake_adb
):
    original_tap = device.tap

    def tap_then_cover(
        point: tuple[int, int], _send_by: float | None = None, _epoch: int | None = None
    ) -> tuple[int, int]:
        result = original_tap(point, _send_by=_send_by, _epoch=_epoch)
        if not android.stopped and android.screen.name == "browse":
            for name in ("ext1", "ext2", "ext3"):
                android.show(name)
        return result

    original_check = Device.check_pin

    def slow_after_stop(self: Device, **kwargs: object) -> None:
        original_check(self, **kwargs)  # ty: ignore[invalid-argument-type]
        if android.stopped:
            clock.now += 3600.0  # the restart's pin check crosses the limit

    monkeypatch.setattr(device, "tap", tap_then_cover)
    monkeypatch.setattr(Device, "check_pin", slow_after_stop)
    out = tmp_path / "run"
    # restart=False: android.stopped then counts relaunches only.
    result = run(device, clock, out, restart=False)
    assert android.stopped == [SHOP]
    assert len(_starts(fake_adb)) == 1  # the first launch only
    assert result.stop_reason == "max_duration"
    ends = [r for r in lines(out / "transitions.jsonl") if r.get("not_sent")]
    assert ends[-1]["reason"] == "start_not_sent"


@pytest.mark.usefixtures("android")
def test_nothing_is_hashed_between_the_last_time_check_and_a_tap(
    tmp_path: Path, device, clock, monkeypatch
):
    # The screen id walks the whole screen; it must be computed before the
    # last look at the time, which is the device's own, on the tap's
    # connection right before the request is written.
    from uiprobe import model

    events: list[str] = []
    original_id = model.Screen.screen_id
    original_check = device_module._monotonic
    original_send = adb_module._send_request

    def recording_id(self: model.Screen) -> str:
        events.append("hash")
        return original_id.fget(self)

    def recording_check() -> float:
        events.append("check")
        return original_check()

    def recording_send(sock, request: str):
        if request.startswith("shell:input swipe"):
            events.append("tap")
        return original_send(sock, request)

    monkeypatch.setattr(model.Screen, "screen_id", property(recording_id))
    monkeypatch.setattr(device_module, "_monotonic", recording_check)
    monkeypatch.setattr(adb_module, "_send_request", recording_send)
    out = tmp_path / "run"
    crawl(
        device,
        SHOP,
        out,
        policy=Policy(),
        limits=Limits(actions=4),
        settle=1.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    taps = [i for i, e in enumerate(events) if e == "tap"]
    assert taps
    for index in taps:
        assert events[index - 1] == "check"  # the time, then at once the tap


# --- skip details: the matched word in skipped.jsonl ---

WORD_SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((0, 0, 320, 100), text="Shop"),
            button(200, "Browse", "warn"),
            button(260, "Delete account", "main"),
            button(320, "Automatically", "main"),
            El((20, 380, 300, 420), cls=BUTTON, go="main"),
        ),
    ),
    Screen(
        "warn",
        f"{SHOP}/.Warn",
        (
            El((0, 0, 320, 100), text="You can't undo this"),
            El((0, 100, 320, 150), text="Delete files?"),
            button(200, "Cancel", "main"),
            button(260, "OK", "main"),
        ),
    ),
)


def test_skip_lines_carry_the_details_of_word_reasons(tmp_path, fake_adb, clock):
    FakeAndroid(fake_adb, SERIAL, WORD_SCREENS, start="home", transport_id=TRANSPORT)
    out = tmp_path / "run"
    policy = Policy(deny_exceptions=("automatically",))
    run(Device(adb_server=fake_adb.address), clock, out, policy=policy)
    rows = lines(out / "skipped.jsonl")
    by_text = {r["element"].get("text", ""): r for r in rows}
    delete = by_text["Delete account"]
    assert delete["reasons"] == ["deny_word"]
    assert delete["details"] == [
        {
            "reason": "deny_word",
            "word": "delete",
            "field": "text",
            "element_id": delete["element"]["id"],
        }
    ]
    cancel = by_text["Cancel"]
    assert cancel["reasons"] == ["irreversible_screen"]
    assert [(d["reason"], d["word"], d["field"]) for d in cancel["details"]] == [
        ("irreversible_screen", "can't undo", "text")
    ]
    assert cancel["details"][0]["element_id"] != cancel["element"]["id"]
    ok = by_text["OK"]
    assert ok["reasons"] == ["irreversible_screen", "confirm_button"]
    assert [d["reason"] for d in ok["details"]] == [
        "irreversible_screen",
        "confirm_button",
    ]
    # The first deny word on the screen, "undo" in "You can't undo this".
    assert (ok["details"][1]["word"], ok["details"][1]["note"]) == ("undo", "ok")
    # Other reasons carry no details.
    unlabeled = by_text[""]
    assert unlabeled["reasons"] == ["unlabeled"]
    assert "details" not in unlabeled
    # The word excused by the exception is not refused and named nowhere.
    assert "Automatically" not in by_text
    assert all(d.get("word") != "call" for r in rows for d in r.get("details", []))


def test_the_result_names_the_owner_of_the_last_focus(
    tmp_path, device, clock, monkeypatch
):
    # A window of System UI holds the focus throughout (as a lock screen
    # would): the result says so, for the CLI's note.
    monkeypatch.setattr(device, "focused_window", lambda: SHADE)
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=4))
    assert result.last_focus == SHADE.owner == "com.android.systemui"


def test_the_result_names_the_app_as_the_last_focus_when_it_had_it(
    tmp_path, device, clock
):
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=4))
    assert result.last_focus == SHOP


@pytest.mark.parametrize("last_is_system_ui", [True, False])
def test_the_result_names_the_focus_of_the_last_observation_only(
    tmp_path, device, clock, monkeypatch, last_is_system_ui
):
    # The owner changes during the run: the result names the last one read,
    # not the first.
    app = FocusedWindow(SHOP, f"{SHOP}/.Main", "1a2b3c4")
    first, last = (app, SHADE) if last_is_system_ui else (SHADE, app)
    calls = [0]

    def focus() -> FocusedWindow:
        calls[0] += 1
        return first if calls[0] <= 2 else last

    monkeypatch.setattr(device, "focused_window", focus)
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=4))
    assert calls[0] > 2
    assert result.last_focus == last.owner


def test_skip_lines_name_the_row_reasons(tmp_path, fake_adb, clock, monkeypatch):
    from tests.test_crawl_preview import ROWS_SCREEN, rows_xml

    android = FakeAndroid(
        fake_adb, SERIAL, (ROWS_SCREEN,), start="rows", transport_id=TRANSPORT
    )
    monkeypatch.setattr(android, "render_xml", lambda: rows_xml(android))
    device = Device(adb_server=fake_adb.address)
    out = tmp_path / "run"
    run(device, clock, out, policy=Policy(skip_rows_with_buttons=True))
    rows = {r["element"]["id"]: r for r in lines(out / "skipped.jsonl")}
    assert rows["e2"]["reasons"] == ["two_target_row"]
    assert rows["e8"]["reasons"] == ["row_with_button"]
    # Not word reasons: the lines carry no details.
    assert "details" not in rows["e2"]
    assert "details" not in rows["e8"]
    # Exactly the gear (232..304 x 206..285) and the star (250..310 x
    # 310..370) were tapped; neither row.
    taps = {i.point for i in android.inputs if i.kind == "tap"}
    assert taps == {(268, 245), (280, 340)}


# --- the first launch stops the app first (restart, the default) ---

RESOLVE = (
    f"cmd package resolve-activity --brief -c {LAUNCHER} {SHOP}; echo uiprobe-exit:$?"
)
START = (
    f"am start -W -a android.intent.action.MAIN -c {LAUNCHER} -n {SHOP}/.Main;"
    " echo uiprobe-exit:$?"
)
FORCE_STOP = f"am force-stop {SHOP}; echo uiprobe-exit:$?"


def changing_commands(fake_adb: FakeAdbServer) -> list[str]:
    """The starts and stops the device received, each with its launcher read."""
    return [
        c
        for c in commands(fake_adb)
        if c.startswith(("am ", "input ", "monkey")) or c == RESOLVE
    ]


def test_the_first_launch_force_stops_then_starts_as_one_action(
    tmp_path, device, android, clock, fake_adb
):
    android.show("browse")  # the app is open below its first screen
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=1))
    assert (result.stop_reason, result.actions) == ("max_actions", 1)
    # The launcher read of the gate, the force-stop, then, as a relaunch,
    # the launcher read again and the start (which uses the second read).
    assert changing_commands(fake_adb) == [RESOLVE, FORCE_STOP, RESOLVE, START]
    assert android.stopped == [SHOP]
    assert android.screen.name == "main"  # the app's first screen
    records = lines(out / "transitions.jsonl")
    assert len(records) == 2  # one action: one start line, one end line
    assert records[0] == {
        "schema": "uiprobe.transition_start/1",
        "recorded_at": records[0]["recorded_at"],
        "action_id": 1,
        "action": {"kind": "launch"},
        "restart": True,
    }
    assert records[1]["action_id"] == 1
    assert records[1]["to_screen_id"] == result.last_screen_id
    shown = json.loads((out / "screens" / f"{result.last_screen_id}.json").read_bytes())
    assert shown["app"]["activity"].endswith(".Main")
    assert json.loads((out / "run.json").read_bytes())["actions"] == 1
    assert json.loads((out / "run-start.json").read_bytes())["restart"] is True


def test_without_restart_the_first_launch_sends_only_the_start(
    tmp_path, device, android, clock, fake_adb
):
    android.show("browse")
    out = tmp_path / "run"
    result = run(device, clock, out, limits=Limits(actions=1), restart=False)
    assert (result.stop_reason, result.actions) == ("max_actions", 1)
    assert changing_commands(fake_adb) == [RESOLVE, START]
    assert android.stopped == []
    assert android.screen.name == "browse"  # the screen it showed
    records = lines(out / "transitions.jsonl")
    assert records[0]["action"] == {"kind": "launch"}
    assert "restart" not in records[0]
    assert json.loads((out / "run-start.json").read_bytes())["restart"] is False


@pytest.mark.parametrize("end", ["stop", "time_limit"])
def test_a_stop_or_the_time_limit_after_the_first_force_stop_sends_no_start(
    tmp_path, device, android, monkeypatch, clock, fake_adb, end
):
    share_clock(monkeypatch, clock)
    stopped_app: list[bool] = []
    stop_app = device.stop_app

    def stop_then_end(
        package: str, _send_by: float | None = None, _epoch: int | None = None
    ) -> None:
        stop_app(package, _send_by=_send_by, _epoch=_epoch)
        stopped_app.append(True)
        if end == "time_limit":
            clock.now += 3600.0

    monkeypatch.setattr(device, "stop_app", stop_then_end)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: end == "stop" and bool(stopped_app))
    assert result.stop_reason == ("interrupted" if end == "stop" else "max_duration")
    assert android.stopped == [SHOP]
    assert start_requests(fake_adb) == []
    assert result.actions == 1
    assert dict(result.not_sent) == {"start_not_sent": 1}
    records = lines(out / "transitions.jsonl")
    assert [r["action_id"] for r in records] == [1, 1]
    assert records[0]["restart"] is True
    assert (records[1]["not_sent"], records[1]["reason"]) == (True, "start_not_sent")
    why = "stopped" if end == "stop" else "time_limit"
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert (
        f"{SHOP} was force-stopped at the start of the run but not started ({why})"
        in summary
    )


@pytest.mark.usefixtures("android")
@pytest.mark.parametrize("end", ["stop", "time_limit"])
def test_a_stop_or_the_time_limit_before_the_first_force_stop_sends_nothing(
    tmp_path, device, monkeypatch, clock, fake_adb, end
):
    # Asked in the gate of the force-stop, after its pin check: nothing goes
    # out, and the launch is closed as not sent for that reason.
    share_clock(monkeypatch, clock)
    in_gate: list[bool] = []
    gate = crawl_driver._Run.restart_gate

    def marked_gate(self):
        in_gate.append(True)
        if end == "time_limit":
            clock.now += 3600.0
        return gate(self)

    monkeypatch.setattr(crawl_driver._Run, "restart_gate", marked_gate)
    out = tmp_path / "run"
    result = run(device, clock, out, stop=lambda: end == "stop" and bool(in_gate))
    assert in_gate
    assert result.stop_reason == ("interrupted" if end == "stop" else "max_duration")
    # Only the gate's launcher read, when stop() did not skip it.
    assert [c for c in changing_commands(fake_adb) if c != RESOLVE] == []
    records = lines(out / "transitions.jsonl")
    reason = "stopped" if end == "stop" else "time_limit"
    assert (records[-1]["not_sent"], records[-1]["reason"]) == (True, reason)
    assert "force-stopped" not in (out / "summary.txt").read_text(encoding="utf-8")


def test_a_bad_restart_value_is_a_type_error(tmp_path, device, android, fake_adb):
    loose: Any = crawl
    for value in ("yes", 1, None):
        with pytest.raises(TypeError, match="restart must be a bool"):
            loose(
                device,
                SHOP,
                tmp_path / "run",
                policy=Policy(),
                limits=Limits(),
                restart=value,
            )
    assert not (tmp_path / "run").exists()
    assert fake_adb.commands == []
    assert android.inputs == []


def test_restart_is_keyword_only():
    parameter = inspect.signature(crawl).parameters["restart"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is True


# --- the up button of a sub-page ---

UP = "android.widget.ImageButton"


def up_button() -> El:
    # The API 37 emulator's Settings records it at (16, 24, 72, 80), outside
    # the screen-edge areas; the fake renders its elements flat, so the rule
    # finds it here by its label.
    return El((16, 24, 72, 80), desc="Navigate up", cls=UP, go="back")


SUBPAGE_SCREENS = (
    Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
    Screen(
        "top",
        f"{SHOP}/.Top",
        (
            El((0, 0, 320, 100), text="Settings"),
            button(200, "Network", "network"),
        ),
    ),
    Screen(
        "network",
        f"{SHOP}/.Network",
        (
            up_button(),
            El((80, 24, 300, 80), text="Network"),
            button(200, "Wi-Fi", "wifi"),
            button(260, "Mobile", "mobile"),
        ),
    ),
    Screen(
        "wifi",
        f"{SHOP}/.Wifi",
        (
            up_button(),
            El((80, 24, 300, 80), text="Wi-Fi"),
            button(200, "Saved", "saved"),
        ),
    ),
    Screen(
        "saved", f"{SHOP}/.Saved", (up_button(), El((80, 24, 300, 80), text="Saved"))
    ),
    Screen(
        "mobile", f"{SHOP}/.Mobile", (up_button(), El((80, 24, 300, 80), text="Mobile"))
    ),
)


def crawl_subpages(
    fake_adb: FakeAdbServer, clock: Clock, out: Path, policy: Policy
) -> tuple[FakeAndroid, CrawlResult]:
    android = FakeAndroid(
        fake_adb, SERIAL, SUBPAGE_SCREENS, start="home", transport_id=TRANSPORT
    )
    device = Device(adb_server=fake_adb.address)
    result = run(device, clock, out, limits=Limits(actions=12), policy=policy)
    return android, result


def test_the_rows_of_a_subpage_are_tapped_before_its_up_button(
    tmp_path, fake_adb, clock
):
    android, result = crawl_subpages(fake_adb, clock, tmp_path / "run", Policy())
    taps = [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]
    # Never the up button (center 44, 52): every row of every sub-page, the
    # deepest first.
    assert taps == [
        ("top", (160, 220)),
        ("network", (160, 220)),
        ("wifi", (160, 220)),
        ("network", (160, 280)),
    ]
    assert (result.stop_reason, result.screens) == ("exhausted", 5)
    rows = {r["element"]["id"]: r for r in lines(tmp_path / "run" / "skipped.jsonl")}
    assert {tuple(r["reasons"]) for r in rows.values()} == {("navigate_up",)}


def test_with_the_up_rule_off_the_crawl_presses_up_and_stays_shallow(
    tmp_path, fake_adb, clock
):
    android, result = crawl_subpages(
        fake_adb, clock, tmp_path / "run", Policy(skip_navigate_up=False)
    )
    taps = [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]
    # As before the rule: the up button is the first candidate of the sub-page,
    # so it is pressed at once. It lands on the parent, which the engine takes
    # for a deeper screen; its BACK there leaves the app, and after the
    # relaunch every candidate is already tried: no row of a sub-page is
    # pressed and only 2 screens are seen (5 above).
    assert taps == [("top", (160, 220)), ("network", (44, 52))]
    assert result.stop_reason == "exhausted"
    assert result.screens == 2
