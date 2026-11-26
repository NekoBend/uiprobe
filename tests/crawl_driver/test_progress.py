"""Tests for the ``on_progress`` callback of `crawl` (the library prints nothing)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import Clock, end_lines, lines, run
from uiprobe.crawl import Limits
from uiprobe.device import Device

if TYPE_CHECKING:
    from uiprobe.crawl_driver import Progress


def test_the_callback_runs_after_each_sent_action_with_its_end_line_written(
    tmp_path, device, clock
):
    out = tmp_path / "run"
    seen: list[tuple[Progress, int, int]] = []

    def on_progress(progress: Progress) -> None:
        records = lines(out / "transitions.jsonl")
        starts = sum(1 for r in records if "action" in r)
        seen.append((progress, starts, len(end_lines(out))))

    result = run(device, clock, out, limits=Limits(actions=4), on_progress=on_progress)
    assert result.stop_reason == "max_actions"
    assert seen
    # The first call is for the first launch, and the counts only grow.
    assert [p.actions for p, _, _ in seen] == [1, 2, 3, 4]
    for progress, starts, ends in seen:
        assert starts == ends  # the action's end line is on disk already
        assert progress.action_limit == 4
        assert progress.depth_limit == 5
        assert progress.minutes == 30
        assert progress.screens >= 1
        assert progress.depth >= 1
    # The elapsed time is the run's own clock (the settle pauses move it).
    elapsed = [p.elapsed for p, _, _ in seen]
    assert elapsed == sorted(elapsed)
    assert elapsed[-1] >= 3.0


def test_the_callback_is_not_called_once_the_run_has_stopped(
    tmp_path, device, android, clock
):
    stopped = [False]
    calls: list[Progress] = []

    def stop() -> bool:
        if len(android.inputs) >= 2:
            stopped[0] = True
        return stopped[0]

    def on_progress(progress: Progress) -> None:
        assert not stopped[0]
        calls.append(progress)

    result = run(device, clock, tmp_path / "run", stop=stop, on_progress=on_progress)
    assert result.stop_reason == "interrupted"
    assert 1 <= len(calls) <= 2


def test_a_run_that_stops_before_its_first_action_reports_nothing(
    tmp_path, device, clock
):
    calls: list[Progress] = []
    result = run(
        device, clock, tmp_path / "run", stop=lambda: True, on_progress=calls.append
    )
    assert result.stop_reason == "interrupted"
    assert calls == []


def test_a_callback_that_raises_ends_the_run_as_an_error(
    tmp_path, device, android, clock
):
    out = tmp_path / "run"

    def on_progress(progress: Progress) -> None:
        del progress
        msg = "the printer broke"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="the printer broke"):
        run(device, clock, out, on_progress=on_progress)
    summary = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert summary["stop_reason"] == "error"
    assert "error: internal_error" in (out / "summary.txt").read_text(encoding="utf-8")
    assert len(android.inputs) == 0  # nothing was pressed after it raised


def test_an_interrupt_in_the_callback_is_raised_again(tmp_path, device, clock):
    out = tmp_path / "run"

    def on_progress(progress: Progress) -> None:
        del progress
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(device, clock, out, on_progress=on_progress)
    summary = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert summary["stop_reason"] == "interrupted"


def test_the_library_prints_nothing_by_default(tmp_path, device, clock, capsys):
    result = run(device, clock, tmp_path / "run", limits=Limits(actions=3))
    assert result.actions == 3
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_the_callback_must_be_callable(tmp_path, device, clock):
    with pytest.raises(TypeError, match="on_progress must be callable"):
        run(device, clock, tmp_path / "run", on_progress="print")  # ty: ignore[invalid-argument-type]
    assert not (tmp_path / "run").exists()


class _CountingClock(Clock):
    """A clock that moves 2 s at every read, so a read the callback made
    would shift every later deadline check."""

    def monotonic(self) -> float:
        self.now += 2.0
        return self.now


def test_a_noop_callback_changes_nothing_that_is_sent(tmp_path, fake_adb, android):
    # The callback reads no clock: the same run with and without it sends the
    # same requests and writes the same transitions, here on a tight time
    # limit where one more clock read would end the run earlier.
    def one_run(out, on_progress) -> tuple[list[str], list[dict], str]:
        android.inputs.clear()
        android.stopped.clear()
        android.show("home")
        clock = _CountingClock()
        before = len(fake_adb.commands)
        result = run(
            Device(adb_server=fake_adb.address),
            clock,
            out,
            limits=Limits(minutes=1),
            on_progress=on_progress,
        )
        sent = [
            re.sub(r"uiprobe-[0-9a-f]+\.xml", "uiprobe-X.xml", c)
            for _, _, c in fake_adb.commands[before:]
        ]
        records = [
            {k: v for k, v in r.items() if k != "recorded_at"}
            for r in lines(out / "transitions.jsonl")
        ]
        return sent, records, result.stop_reason

    plain = one_run(tmp_path / "a", None)
    with_callback = one_run(tmp_path / "b", lambda _progress: None)
    assert plain[2] == "max_duration"  # the time limit is what ended it
    assert plain == with_callback
    assert any(c.startswith("input ") for c in plain[0])


def test_a_callback_is_not_called_while_a_transition_is_open(tmp_path, device, clock):
    out = tmp_path / "run"
    checked: list[int] = []

    def on_progress(progress: Progress) -> None:
        starts = sum(1 for r in lines(out / "transitions.jsonl") if "action" in r)
        assert starts == len(end_lines(out))
        checked.append(progress.actions)

    run(device, clock, out, limits=Limits(actions=4), on_progress=on_progress)
    assert checked
