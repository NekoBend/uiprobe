"""Tests for the progress lines of ``uiprobe crawl --press`` (stderr only)."""

from __future__ import annotations

import io
import json
import re
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import SCREENS, SERIAL, SHOP, TRANSPORT
from tests.fake_device import FakeAndroid
from uiprobe import cli
from uiprobe.cli import crawl_press
from uiprobe.cli.crawl_press import (
    PROGRESS_INTERVAL,
    _duration_text,
    _progress_text,
    _ProgressPrinter,
)
from uiprobe.crawl_driver import Progress
from uiprobe.settings import SETTINGS_FILE, render_default_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

LINE = re.compile(
    r"^progress: (\d+h \d\dm \d\ds|\d+m \d\ds|\d+s) of 30m,"
    r" \d+ of \d+ actions, \d+ screens?, depth \d+ of 5$"
)


@pytest.fixture(autouse=True)
def _android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture(autouse=True)
def _here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / SETTINGS_FILE).write_text(render_default_settings(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)


def press(capsys, fake_adb, tmp_path, *argv):
    out = tmp_path / "run"
    args = ["--adb-server", fake_adb.address, "crawl", SHOP, "--press"]
    code = cli.run([*args, "--out", str(out), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def progress(**changes: float) -> Progress:
    values = {
        "elapsed": 134.9,
        "actions": 37,
        "action_limit": 300,
        "screens": 12,
        "depth": 3,
        "depth_limit": 5,
        "minutes": 30,
    }
    return Progress(**{**values, **changes})  # ty: ignore[invalid-argument-type]


# --- the format ---


def test_the_line_has_the_documented_format():
    assert _progress_text(progress()) == (
        "progress: 2m 14s of 30m, 37 of 300 actions, 12 screens, depth 3 of 5"
    )
    assert _progress_text(progress(elapsed=0.4, screens=1, actions=1, depth=1)) == (
        "progress: 0s of 30m, 1 of 300 actions, 1 screen, depth 1 of 5"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (59.9, "59s"), (60, "1m 00s"), (3599, "59m 59s"), (3723, "1h 02m 03s")],
)
def test_the_elapsed_time_text(seconds, text):
    assert _duration_text(seconds) == text


# --- the interval, with an injected clock ---


class _Ticks:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_first_call_prints_and_later_ones_wait_for_the_interval():
    stream = io.StringIO()
    ticks = _Ticks()
    printer = _ProgressPrinter(stream, clock=ticks)
    printer(progress(actions=1))  # the first action: always a line
    ticks.now += 4.9
    printer(progress(actions=2))  # too soon
    ticks.now += 0.1
    printer(progress(actions=3))  # 5.0 seconds after the first line
    ticks.now += 1
    printer(progress(actions=4))
    ticks.now += 4.0  # 5.0 s after the line of action 3
    printer(progress(actions=5))
    ticks.now += 0.5
    printer(progress(actions=6))  # too soon again
    shown = [line.split(",")[1].strip() for line in stream.getvalue().splitlines()]
    assert shown == ["1 of 300 actions", "3 of 300 actions", "5 of 300 actions"]
    assert PROGRESS_INTERVAL == 5.0


def test_a_closed_stream_prints_nothing_and_does_not_fail():
    _ProgressPrinter(None)(progress())


def test_a_failing_write_turns_progress_off_without_failing():
    class Broken(io.StringIO):
        writes = 0

        def write(self, text: str) -> int:
            del text
            type(self).writes += 1
            raise BrokenPipeError

    ticks = _Ticks()
    printer = _ProgressPrinter(Broken(), clock=ticks)
    printer(progress(actions=1))
    ticks.now += 60
    printer(progress(actions=2))
    assert Broken.writes == 1  # no second attempt: progress is off


def test_a_failing_flush_turns_progress_off_without_failing():
    class Broken(io.StringIO):
        def flush(self) -> None:
            raise OSError(5, "input/output error")

    _ProgressPrinter(Broken())(progress())


def test_the_interval_counts_from_the_end_of_the_write():
    stream = io.StringIO()
    reads = iter([100.0, 103.0, 106.0, 108.5, 111.0])
    printer = _ProgressPrinter(stream, clock=lambda: next(reads))
    printer(progress(actions=1))  # attempt at 100, the write ended at 103
    printer(progress(actions=2))  # 106: only 3 s after the end of the write
    printer(progress(actions=3))  # 108.5: 5.5 s after it, so it prints
    assert len(stream.getvalue().splitlines()) == 2


def test_each_line_is_flushed():
    class Counting(io.StringIO):
        flushes = 0

        def flush(self) -> None:
            type(self).flushes += 1
            super().flush()

    stream = Counting()
    _ProgressPrinter(stream)(progress())
    assert Counting.flushes >= 1


# --- the command ---


def test_the_command_prints_the_first_line_on_stderr_only(fake_adb, capsys, tmp_path):
    code, out, err = press(
        capsys, fake_adb, tmp_path, "--settle", "0", "--actions", "6"
    )
    assert code == 0
    assert "progress:" not in out
    lines = [line for line in err.splitlines() if line.startswith("progress:")]
    # The run is far shorter than 5 seconds: only the first action's line.
    assert len(lines) == 1
    assert LINE.match(lines[0])
    assert " 1 of 6 actions," in lines[0]
    assert out.startswith(f"pressing on {SERIAL}")  # the plan stays on stdout


def test_quiet_prints_no_progress_line(fake_adb, capsys, tmp_path):
    code, out, err = press(
        capsys, fake_adb, tmp_path, "--settle", "0", "--actions", "6", "--quiet"
    )
    assert (code, err) == (0, "")
    assert "stopped: max_actions" in out  # the result is as without --quiet


def test_json_keeps_stdout_one_document_and_progress_on_stderr(
    fake_adb, capsys, tmp_path
):
    code, out, err = press(
        capsys, fake_adb, tmp_path, "--settle", "0", "--actions", "6", "--json"
    )
    assert code == 0
    assert json.loads(out)["actions"] == 6  # nothing but the JSON on stdout
    assert "progress" not in out
    lines = err.splitlines()
    assert lines[0].startswith(f"pressing on {SERIAL}")
    assert sum(1 for line in lines if LINE.match(line)) == 1


def test_json_with_quiet_has_the_plan_only_on_stderr(fake_adb, capsys, tmp_path):
    code, out, err = press(
        capsys,
        fake_adb,
        tmp_path,
        "--settle",
        "0",
        "--actions",
        "6",
        "--json",
        "--quiet",
    )
    assert code == 0
    assert json.loads(out)["stop_reason"] == "max_actions"
    assert "progress" not in err


def test_the_line_comes_after_the_plan_and_before_the_result(
    fake_adb, capsys, tmp_path
):
    code, _out, err = press(
        capsys, fake_adb, tmp_path, "--settle", "0", "--actions", "3", "--json"
    )
    assert code == 0
    assert err.splitlines()[-1].startswith("progress:")
    assert err.index("run folder:") < err.index("progress:")


def test_a_later_line_comes_after_the_interval(fake_adb, capsys, tmp_path, monkeypatch):
    ticks = _Ticks()

    def three_seconds_per_action() -> float:
        ticks.now += 3.0
        return ticks.now

    monkeypatch.setattr(
        crawl_press,
        "_ProgressPrinter",
        lambda stream: _ProgressPrinter(stream, clock=three_seconds_per_action),
    )
    code, _out, err = press(
        capsys, fake_adb, tmp_path, "--settle", "0", "--actions", "8"
    )
    assert code == 0
    lines = [line for line in err.splitlines() if line.startswith("progress:")]
    assert len(lines) >= 2  # 0 s, 6 s, 12 s, ...: every second action
    assert all(LINE.match(line) for line in lines)
    actions = [int(line.split(",")[1].split()[0]) for line in lines]
    assert actions[0] == 1
    assert all(b - a == 2 for a, b in pairwise(actions))


def test_a_dry_run_refuses_quiet_like_the_other_pressing_options(fake_adb, capsys):
    argv = ["--adb-server", fake_adb.address, "crawl", SHOP, "--dry-run", "--quiet"]
    code = cli.run(argv)
    err = capsys.readouterr().err
    assert code == 2
    assert "--quiet is used only when the crawl presses, and --dry-run" in err


def test_a_preview_refuses_quiet(fake_adb, capsys):
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, "--quiet"])
    err = capsys.readouterr().err
    assert code == 2
    assert "--quiet is used only when the crawl presses: add --press" in err


def test_a_closed_stderr_does_not_stop_the_crawl_and_stdout_stays_json(
    fake_adb, capsys, tmp_path, monkeypatch
):
    class BrokenProgress(io.StringIO):
        def write(self, text: str) -> int:
            if text.startswith("progress:"):
                raise BrokenPipeError
            return super().write(text)

    err = BrokenProgress()
    monkeypatch.setattr("sys.stderr", err)
    argv = ["--adb-server", fake_adb.address, "--json", "crawl", SHOP, "--press"]
    code = cli.run([*argv, "--out", str(tmp_path / "run"), "--settle", "0"])
    out = capsys.readouterr().out
    assert code == 0
    result = json.loads(out)
    assert result["stop_reason"] == "exhausted"
    assert "progress:" not in err.getvalue()
    assert err.getvalue().startswith(f"pressing on {SERIAL}")
