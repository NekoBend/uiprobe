"""Tests for the ``on_progress`` callback of `crawl` (the library prints nothing)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import end_lines, lines, run
from uiprobe.crawl import Limits

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
    assert seen[0][0].actions == 1
    assert [p.actions for p, _, _ in seen] == sorted({p.actions for p, _, _ in seen})
    for progress, starts, ends in seen:
        assert starts == ends  # the action's end line is on disk already
        assert progress.action_limit == 4
        assert progress.depth_limit == 5
        assert progress.minutes == 30
        assert progress.screens >= 1
        assert progress.depth >= 1
    assert seen[-1][0].elapsed > 0  # the settle pauses moved the run's clock


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
