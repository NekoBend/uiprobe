"""Tests for how the crawl driver starts the app: launcher, relaunch, force-stop."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_device import (
    El,
    FakeAndroid,
    Screen,
)
from uiprobe import crawl_driver
from uiprobe.crawl import (
    Limits,
)
from uiprobe.crawl_driver import crawl
from uiprobe.device import Device
from uiprobe.errors import (
    AdbError,
    LaunchFailedError,
)
from uiprobe.guards import Policy

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

from tests.crawl_driver.helpers import (
    FORCE_STOP,
    LAUNCHER,
    RESOLVE,
    SERIAL,
    SHOP,
    STOP_REASONS,
    TRANSPORT,
    button,
    changing_commands,
    commands,
    cover_after_browse,
    lines,
    run,
    share_clock,
    start_requests,
)

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


LAUNCH_DATA = Path(__file__).parent.parent / "data" / "launch"


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


START = (
    f"am start -W -a android.intent.action.MAIN -c {LAUNCHER} -n {SHOP}/.Main;"
    " echo uiprobe-exit:$?"
)


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
