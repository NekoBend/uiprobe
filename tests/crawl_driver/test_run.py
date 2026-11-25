"""Tests for a whole crawl run: arguments, protocol, errors, screenshots, limits."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

import uiprobe
from tests.fake_device import (
    FakeAndroid,
    assert_input_kinds,
    assert_no_long_press,
    assert_no_press_inside,
    assert_no_text_input,
    assert_only_back_keys,
)
from uiprobe import crawl_driver
from uiprobe.crawl import (
    Limits,
)
from uiprobe.crawl_driver import CrawlResult, crawl
from uiprobe.device import IDENTITY_COMMAND, Device
from uiprobe.errors import (
    AdbError,
    NotEmulatorError,
)
from uiprobe.guards import DEFAULT_DENY_EXCEPTIONS, Policy
from uiprobe.runlog import RunFolderError

if TYPE_CHECKING:
    from collections.abc import Callable


from tests.crawl_driver.helpers import (
    SCREENS,
    SERIAL,
    SHOP,
    STOP_REASONS,
    TRANSPORT,
    commands,
    lines,
    run,
    share_clock,
    spy,
)

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
