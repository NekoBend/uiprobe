"""Tests for the crawl driver's own deadline and clock, and the launcher read limits."""

from __future__ import annotations

import json
import re
from dataclasses import fields
from types import SimpleNamespace

import pytest

from tests.crawl_driver.helpers import (
    _WRITERS,
    FORCE_STOP,
    RESOLVE,
    SCREENS,
    SERIAL,
    SHOP,
    TRANSPORT,
    changing_commands,
    lines,
    run,
    share_clock,
    start_requests,
)
from tests.fake_device import (
    FakeAndroid,
)
from uiprobe import crawl_driver
from uiprobe.crawl import (
    CrawlEngine,
    Limits,
)
from uiprobe.crawl_driver import crawl
from uiprobe.device import Device
from uiprobe.device import pin as device_pin
from uiprobe.device import state as device_state
from uiprobe.errors import (
    LaunchFailedError,
)
from uiprobe.guards import Policy
from uiprobe.model import Bounds
from uiprobe.runlog import RunWriter

# --- uiprobe's own deadline, set once ---


class Trusted:
    """uiprobe's own clock (`device.time.monotonic`), apart from the
    caller's clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.sleeps: list[tuple[float, float]] = []  # (when, how long)
        fake_time = SimpleNamespace(monotonic=lambda: self.now, sleep=lambda _s: None)
        monkeypatch.setattr(device_pin, "time", fake_time)
        monkeypatch.setattr(device_state, "time", fake_time)

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

    fake_time = SimpleNamespace(monotonic=monotonic, sleep=sleep)
    monkeypatch.setattr(device_pin, "time", fake_time)
    monkeypatch.setattr(device_state, "time", fake_time)
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
        "skip_sliders": False,
        "replay_path": False,
        "also_target": ("com.example.helper",),
    }
    assert set(values) == {f.name for f in fields(Policy)}
    default = Policy()
    original = Policy(**values)
    copy = crawl_driver._plain_policy(original)
    for name, value in values.items():
        assert getattr(default, name) != value, name
        assert getattr(copy, name) == value, name
