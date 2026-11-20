"""The run folder writer: files only under tmp_path, bytes read back as bytes."""

from __future__ import annotations

import builtins
import contextlib
import errno
import json
import os
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from uiprobe import pngfile, runlog
from uiprobe.crawl import Limits
from uiprobe.errors import UiprobeError
from uiprobe.model import Bounds, Element, Screen
from uiprobe.runlog import RunFolderError, RunWriter

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\r\n\x00 data"
ID_A = "s-0123456789ab"
ID_B = "s-ba9876543210"
STARTED = datetime(2026, 3, 1, 9, 30, 15, tzinfo=timezone(timedelta(hours=9)))
BIDI = "a\u202eb\u2066c"
CONTROL = "x\x1b[2Jy\x9bz\x07"


class _NoOffset(tzinfo):
    """A time zone that knows no UTC offset: `tzinfo` is set, the offset is None."""

    def utcoffset(self, _dt):
        return None

    def dst(self, _dt):
        return None

    def tzname(self, _dt):
        return None


NO_OFFSET = datetime(2026, 3, 1, 9, 30, tzinfo=_NoOffset())


_WRITERS: list[RunWriter] = []


@pytest.fixture(autouse=True)
def _close_writers():
    yield
    while _WRITERS:
        with contextlib.suppress(RunFolderError):
            _WRITERS.pop().close()


def make(tmp_path: Path, name: str = "run", **overrides) -> RunWriter:
    arguments: dict[str, Any] = {
        "package": "com.example.app",
        "limits": Limits(),
        "deny_words": ["delete", "pay"],
        "serial": "emulator-5554",
        "started_at": STARTED,
    }
    arguments.update(overrides)
    writer = RunWriter.create(tmp_path / name, **arguments)
    _WRITERS.append(writer)
    return writer


def make_screen(
    package: str = "com.example.app", labels: tuple[str, ...] = ("OK",)
) -> Screen:
    elements = tuple(
        Element(
            id=f"e{number}",
            parent=None,
            class_name="android.widget.Button",
            bounds=Bounds(0, number * 10, 100, number * 10 + 10),
            text=label,
            flags=("clickable",),
        )
        for number, label in enumerate(labels, start=1)
    )
    return Screen(
        serial="emulator-5554",
        captured_at=datetime(2026, 3, 1, 9, 31, tzinfo=UTC),
        package=package,
        activity=None,
        api_level=34,
        display=(1080, 2400),
        elements=elements,
    )


def names(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def lines_of(path: Path) -> list[dict]:
    data = path.read_bytes()
    assert data.endswith(b"\n")
    return [json.loads(line) for line in data.decode("utf-8").split("\n")[:-1]]


# --- the folder ---


def test_an_existing_folder_is_never_used(tmp_path):
    target = tmp_path / "run"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"mine")
    with pytest.raises(RunFolderError) as raised:
        make(tmp_path)
    assert raised.value.details["path"] == str(target)
    assert names(target) == ["keep.txt"]
    assert (target / "keep.txt").read_bytes() == b"mine"


def test_an_existing_file_of_that_name_is_refused(tmp_path):
    (tmp_path / "run").write_bytes(b"mine")
    with pytest.raises(RunFolderError):
        make(tmp_path)
    assert (tmp_path / "run").read_bytes() == b"mine"


def test_a_missing_parent_is_not_created(tmp_path):
    with pytest.raises(RunFolderError):
        RunWriter.create(
            tmp_path / "missing" / "run",
            package="p",
            limits=Limits(),
            deny_words=[],
            serial="s",
        )
    assert names(tmp_path) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"package": 1},
        {"serial": None},
        {"deny_words": "delete"},
        {"deny_words": [1]},
        {"limits": 5},
        {"limits": {"depth": 1}},
        {"limits": {"depth": 0, "actions": 1, "minutes": 1}},
        {"deny_words": ["a\ud800"]},
        {"deny_exceptions": "border"},
        {"deny_exceptions": [1]},
        {"package": "p\ud800"},
        {"started_at": datetime(2026, 1, 1)},  # noqa: DTZ001
        {"started_at": NO_OFFSET},
        {"started_at": "2026-01-01T00:00:00+00:00"},
    ],
)
def test_wrong_arguments_write_nothing(tmp_path, overrides):
    with pytest.raises((TypeError, ValueError)):
        make(tmp_path, **overrides)
    assert names(tmp_path) == []


def test_run_start_json(tmp_path):
    writer = make(tmp_path)
    assert names(tmp_path / "run") == ["run-start.json"]
    assert writer.path == tmp_path / "run"
    assert json.loads((tmp_path / "run" / "run-start.json").read_bytes()) == {
        "schema": "uiprobe.run_start/1",
        "started_at": "2026-03-01T09:30:15+09:00",
        "package": "com.example.app",
        "limits": {"depth": 5, "actions": 300, "minutes": 30},
        "deny": ["delete", "pay"],
        "deny_except": [],
        "back_on_other_screens": False,
        "device": {"serial": "emulator-5554"},
    }


def test_run_start_json_lists_the_exception_words(tmp_path):
    make(tmp_path, deny_exceptions=("border", "automatically"))
    data = json.loads((tmp_path / "run" / "run-start.json").read_bytes())
    assert data["deny_except"] == ["border", "automatically"]


def test_limits_may_be_a_plain_dict_and_the_time_defaults_to_now(tmp_path):
    make(tmp_path, limits={"depth": 2, "actions": 9, "minutes": 1}, started_at=None)
    data = json.loads((tmp_path / "run" / "run-start.json").read_bytes())
    assert data["limits"] == {"depth": 2, "actions": 9, "minutes": 1}
    stamp = datetime.fromisoformat(data["started_at"])
    assert stamp.utcoffset() is not None


def _create(path) -> RunWriter:
    writer = RunWriter.create(
        path, package="p", limits=Limits(), deny_words=[], serial="s"
    )
    _WRITERS.append(writer)
    return writer


def test_an_existing_folder_is_named_in_the_message(tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(RunFolderError, match="already exists; give a new folder"):
        make(tmp_path)


@pytest.mark.parametrize(
    "name",
    ["run:x", "run::$INDEX_ALLOCATION", "dir:x/run", "run:"],
    ids=["named", "type", "parent", "trailing"],
)
def test_on_windows_a_stream_path_is_refused(tmp_path, monkeypatch, name):
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    with pytest.raises(RunFolderError, match="names a stream") as raised:
        _create(tmp_path / name)
    assert raised.value.details["path"] == str(tmp_path / name)
    assert names(tmp_path) == []


@pytest.mark.parametrize("kind", ["file", "folder"])
def test_on_windows_a_stream_of_an_existing_entry_is_refused(
    tmp_path, monkeypatch, kind
):
    target = tmp_path / "base"
    if kind == "file":
        target.write_bytes(b"mine")
    else:
        target.mkdir()
    before = target.stat().st_mtime_ns
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    with pytest.raises(RunFolderError, match="names a stream"):
        _create(f"{target}:x")
    assert names(tmp_path) == ["base"]
    assert target.stat().st_mtime_ns == before
    if kind == "file":
        assert target.read_bytes() == b"mine"


@pytest.mark.skipif(os.name != "nt", reason="streams exist on Windows")
@pytest.mark.parametrize("kind", ["file", "folder"])
def test_on_this_windows_a_stream_run_folder_is_not_made(tmp_path, kind):
    target = tmp_path / "base"
    if kind == "file":
        target.write_bytes(b"mine")
    else:
        target.mkdir()
    stream = f"{target}:x"
    with pytest.raises(RunFolderError, match="names a stream"):
        _create(stream)
    with pytest.raises(FileNotFoundError):
        os.stat(stream)  # noqa: PTH116
    assert names(tmp_path) == ["base"]


@pytest.mark.skipif(os.name == "nt", reason="Windows names a stream with a colon")
def test_elsewhere_a_colon_is_part_of_the_folder_name(tmp_path):
    _create(tmp_path / "run:x").close()
    assert (tmp_path / "run:x" / "run-start.json").is_file()


def test_a_null_character_is_refused(tmp_path):
    with pytest.raises(RunFolderError, match="null"):
        _create(f"{tmp_path}{os.sep}r\x00un")
    assert names(tmp_path) == []


def test_a_broken_link_is_refused(tmp_path):
    link = tmp_path / "run"
    try:
        link.symlink_to(tmp_path / "missing", target_is_directory=True)
    except OSError:
        pytest.skip("this system cannot make a link here")
    target = link.readlink()
    with pytest.raises(RunFolderError, match="already exists"):
        _create(link)
    assert not (tmp_path / "missing").exists()
    assert link.is_symlink()
    assert link.readlink() == target


def test_a_path_that_cannot_be_looked_up_is_not_taken_as_missing(tmp_path, monkeypatch):
    target = tmp_path / "run"
    real = os.lstat
    made = []

    def lstat(path, *args, **kwargs):
        if os.fspath(path) == str(target):
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return real(path, *args, **kwargs)

    with monkeypatch.context() as scoped:  # os is global: keep the patch short
        scoped.setattr(pngfile.os, "lstat", lstat)
        scoped.setattr(runlog, "_make_dir", made.append)
        with pytest.raises(RunFolderError, match="cannot check") as raised:
            _create(target)
    assert raised.value.details == {"path": str(target)}
    assert "Permission denied" in raised.value.message
    assert made == []
    assert names(tmp_path) == []


# --- transitions ---


def test_transition_lines_are_complete_after_each_call(tmp_path):
    writer = make(tmp_path)
    path = tmp_path / "run" / "transitions.jsonl"
    writer.write_transition_start(1, "launch")
    first = lines_of(path)  # read before finish: a crash would leave this
    assert len(first) == 1
    assert first[0]["schema"] == "uiprobe.transition_start/1"
    assert first[0]["action_id"] == 1
    assert first[0]["action"] == {"kind": "launch"}
    assert "from_screen_id" not in first[0]
    datetime.fromisoformat(first[0]["recorded_at"])
    writer.write_transition_end(1, ID_A)
    writer.write_transition_start(
        2, "tap", from_screen_id=ID_A, element={"id": "e1"}, point=(5, 6)
    )
    writer.write_transition_end(2, dump_failed=True)
    found = lines_of(path)
    assert [line["schema"] for line in found] == [
        "uiprobe.transition_start/1",
        "uiprobe.transition_end/1",
        "uiprobe.transition_start/1",
        "uiprobe.transition_end/1",
    ]
    assert found[1]["to_screen_id"] == ID_A
    assert found[1]["dump_failed"] is False
    assert found[2]["from_screen_id"] == ID_A
    assert found[2]["action"] == {
        "kind": "tap",
        "point": [5, 6],
        "element": {"id": "e1"},
    }
    assert found[3]["dump_failed"] is True
    assert "to_screen_id" not in found[3]
    assert all(
        datetime.fromisoformat(line["recorded_at"]).utcoffset() is not None
        for line in found
    )


def test_a_transition_element_may_be_an_element(tmp_path):
    writer = make(tmp_path)
    element = make_screen().elements[0]
    writer.write_transition_start(1, "tap", ID_A, element, (1, 2))
    line = lines_of(tmp_path / "run" / "transitions.jsonl")[0]
    assert line["action"]["element"] == element.to_dict()


@pytest.mark.parametrize(
    "call",
    [
        lambda w: w.write_transition_start(0, "tap"),
        lambda w: w.write_transition_start(True, "tap"),  # noqa: FBT003
        lambda w: w.write_transition_start(1, 5),
        lambda w: w.write_transition_start(1, "tap", from_screen_id="s-ABC"),
        lambda w: w.write_transition_start(1, "tap", point=(1,)),
        lambda w: w.write_transition_end(1),
        lambda w: w.write_transition_end(1, "nope"),
        lambda w: w.write_transition_end(1, ID_A, dump_failed=True),
        lambda w: w.write_transition_end(1, ID_A, dump_failed=1),
        lambda w: w.write_transition_start(1, "tap\ud800"),
        lambda w: w.write_transition_start(1, "tap", element={"text": "a\ud800"}),
    ],
)
def test_bad_transition_arguments_write_nothing(tmp_path, call):
    writer = make(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        call(writer)
    assert names(tmp_path / "run") == ["run-start.json"]


# --- skips ---


def test_a_skip_is_written_once_per_screen_fingerprint_and_reasons(tmp_path):
    writer = make(tmp_path)
    path = tmp_path / "run" / "skipped.jsonl"
    element = {"id": "e3", "class": "android.widget.Button"}
    assert writer.write_skip(ID_A, "fp1", ["deny_word", "not_visible"], element)
    assert not writer.write_skip(ID_A, "fp1", ("deny_word", "not_visible"), element)
    assert writer.write_skip(ID_A, "fp1", ["not_visible", "deny_word"], element)
    assert writer.write_skip(ID_A, "fp2", ["deny_word", "not_visible"], element)
    assert writer.write_skip(ID_B, "fp1", ["deny_word", "not_visible"], element)
    found = lines_of(path)
    assert len(found) == 4
    assert found[0]["schema"] == "uiprobe.skip/1"
    assert found[0]["screen_id"] == ID_A
    assert found[0]["fingerprint"] == "fp1"
    assert found[0]["reasons"] == ["deny_word", "not_visible"]
    assert found[0]["element"] == element
    assert found[1]["reasons"] == ["not_visible", "deny_word"]
    datetime.fromisoformat(found[0]["recorded_at"])


@pytest.mark.parametrize(
    "arguments",
    [
        ("bad", "fp", ["r"], {}),
        (ID_A, 1, ["r"], {}),
        (ID_A, "fp", "reason", {}),
        (ID_A, "fp", [1], {}),
    ],
)
def test_bad_skip_arguments_write_nothing(tmp_path, arguments):
    writer = make(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        writer.write_skip(*arguments)
    assert names(tmp_path / "run") == ["run-start.json"]


# --- screens and pictures ---


def test_a_screen_is_written_the_first_time_only(tmp_path):
    writer = make(tmp_path)
    screen = make_screen()
    assert writer.write_screen(screen)
    path = tmp_path / "run" / "screens" / f"{screen.screen_id}.json"
    before = path.read_bytes()
    assert not writer.write_screen(screen)
    assert path.read_bytes() == before
    assert json.loads(before)["schema"] == "uiprobe.screen/1"
    assert before.decode("utf-8") == screen.to_json(indent=2) + "\n"
    assert names(tmp_path / "run" / "screens") == [path.name]


def test_a_screenshot_is_written_the_first_time_only(tmp_path):
    writer = make(tmp_path)
    assert writer.write_screenshot(ID_A, PNG)
    assert not writer.write_screenshot(ID_A, PNG + b"more")
    assert (tmp_path / "run" / "screens" / f"{ID_A}.png").read_bytes() == PNG


@pytest.mark.parametrize("data", [b"", b"GIF89a", b"\x89PNG", b"x" + PNG])
def test_a_non_png_creates_no_file(tmp_path, data):
    writer = make(tmp_path)
    with pytest.raises(ValueError, match="PNG"):
        writer.write_screenshot(ID_A, data)
    assert names(tmp_path / "run") == ["run-start.json"]
    assert writer.write_screenshot(ID_A, PNG)  # a refused one is not "seen"


def test_a_screenshot_must_be_bytes(tmp_path):
    writer = make(tmp_path)
    with pytest.raises(TypeError):
        writer.write_screenshot(ID_A, "png")  # ty: ignore[invalid-argument-type]
    assert names(tmp_path / "run") == ["run-start.json"]


BAD_IDS = [
    "",
    "s-",
    "s-0123456789a",
    "s-0123456789abc",
    "s-0123456789AB",
    "S-0123456789ab",
    "s-0123456789ag",
    "x-0123456789ab",
    "s-0123456789ab\n",
    " s-0123456789ab",
    "../s-0123456789ab",
    "s-0123456789ab/../x",
    "s-0123456789ab.png",
    "s-0123456789" + chr(0x661) + "b",  # an Arabic-Indic digit
    None,
    5,
]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_invalid_screen_ids_are_refused(tmp_path, bad):
    writer = make(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        writer.write_screenshot(bad, PNG)
    with pytest.raises((TypeError, ValueError)):
        writer.write_skip(bad, "fp", ["r"], {})
    if bad is not None:  # None means "no from screen" and is fine
        with pytest.raises((TypeError, ValueError)):
            writer.write_transition_start(1, "tap", from_screen_id=bad)
    with pytest.raises((TypeError, ValueError)):
        writer.write_transition_end(1, bad)
    assert names(tmp_path / "run") == ["run-start.json"]


def test_app_text_never_reaches_a_path(tmp_path):
    writer = make(tmp_path)
    screen = make_screen(package="../../evil/..\\x", labels=("../a", "con.txt"))
    writer.write_screen(screen)
    writer.write_skip(screen.screen_id, "../fp", ["r"], {"text": "../x"})
    writer.finish("done", screens=1, actions=1)
    pattern = re.compile(
        r"(screens/s-[0-9a-f]{12}\.json|screens|[a-z.-]+\.(json|jsonl|txt))"
    )
    assert names(tmp_path / "run") == sorted(
        {
            "run-start.json",
            "run.json",
            "summary.txt",
            "skipped.jsonl",
            "screens",
            f"screens/{screen.screen_id}.json",
        }
    )
    assert all(pattern.fullmatch(name) for name in names(tmp_path / "run"))
    assert names(tmp_path) == ["run", *(f"run/{n}" for n in names(tmp_path / "run"))]


# --- finish ---


def test_finish_writes_run_json_and_summary_once(tmp_path):
    writer = make(tmp_path)
    writer.write_screen(make_screen(labels=("OK", "Cancel")))
    ended = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    writer.finish("action_limit", screens=1, actions=7, ended_at=ended)
    folder = tmp_path / "run"
    assert json.loads((folder / "run.json").read_bytes()) == {
        "schema": "uiprobe.run/1",
        "ended_at": "2026-03-01T10:00:00+00:00",
        "stop_reason": "action_limit",
        "screens": 1,
        "actions": 7,
    }
    summary = (folder / "summary.txt").read_bytes().decode("utf-8")
    assert "com.example.app" in summary
    assert '  "OK"\n  "Cancel"\n' in summary
    before = {n: (folder / n).read_bytes() for n in names(folder) if "." in n}
    with pytest.raises(RunFolderError, match="finished"):
        writer.finish("again", screens=2, actions=8)
    assert {n: (folder / n).read_bytes() for n in before} == before


def test_finish_notes_go_into_the_summary_escaped(tmp_path):
    writer = make(tmp_path)
    writer.finish("error", screens=0, actions=1, notes=["error: adb_error", CONTROL])
    summary = (tmp_path / "run" / "summary.txt").read_text(encoding="utf-8")
    assert summary.startswith(
        'Stopped: error\nScreens: 0\nActions: 1\nNote: "error: adb_error"\nNote: "'
    )
    assert "\x1b" not in summary
    assert "\\u001b" in summary


def test_finish_refuses_notes_that_are_not_text(tmp_path):
    writer = make(tmp_path)
    with pytest.raises(TypeError):
        writer.finish("exhausted", screens=0, actions=1, notes="one note")
    with pytest.raises(TypeError):
        writer.finish("exhausted", screens=0, actions=1, notes=[5])  # ty: ignore[invalid-argument-type]
    assert not (tmp_path / "run" / "run.json").exists()


def test_a_run_that_did_not_finish_has_no_run_json(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    assert not (tmp_path / "run" / "run.json").exists()
    del writer


def test_nothing_is_written_after_finish(tmp_path):
    writer = make(tmp_path)
    writer.finish("done", screens=0, actions=0)
    with pytest.raises(RunFolderError):
        writer.write_transition_start(1, "launch")
    with pytest.raises(RunFolderError):
        writer.write_screen(make_screen())
    assert not (tmp_path / "run" / "transitions.jsonl").exists()


@pytest.mark.parametrize(
    "arguments",
    [("x", -1, 0), ("x", 0, -1), ("x", True, 0), ("x", 0, "1"), (5, 0, 0)],
)
def test_bad_finish_arguments_write_nothing(tmp_path, arguments):
    writer = make(tmp_path)
    reason, screens, actions = arguments
    with pytest.raises((TypeError, ValueError)):
        writer.finish(reason, screens=screens, actions=actions)
    with pytest.raises(ValueError, match="time zone"):
        writer.finish("x", screens=0, actions=0, ended_at=datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="time zone"):
        writer.finish("x", screens=0, actions=0, ended_at=NO_OFFSET)
    assert names(tmp_path / "run") == ["run-start.json"]
    writer.finish("x", screens=0, actions=0)  # still possible


def test_summary_without_labelled_buttons(tmp_path):
    writer = make(tmp_path)
    writer.write_screen(make_screen(labels=("",)))
    writer.finish("x", screens=1, actions=1)
    summary = (tmp_path / "run" / "summary.txt").read_text(encoding="utf-8")
    assert "(no labelled buttons)" in summary


# --- bytes on disk ---


def test_text_files_are_utf8_with_lf_only(tmp_path):
    writer = make(tmp_path, package="com.example.日本")
    screen = make_screen(labels=("確認", "line1\nline2"))
    writer.write_screen(screen)
    writer.write_transition_start(1, "tap", ID_A, {"text": "日本\r\n"}, (1, 2))
    writer.write_transition_end(1, ID_B)
    writer.write_skip(ID_A, "fp", ["r"], {"text": "a\r\nb"})
    writer.write_screenshot(ID_A, PNG)
    writer.finish("done", screens=1, actions=1)
    folder = tmp_path / "run"
    texts = [p for p in folder.rglob("*") if p.is_file() and p.suffix != ".png"]
    assert len(texts) == 6
    for path in texts:
        data = path.read_bytes()
        assert b"\r" not in data, path.name
        assert data.endswith(b"\n"), path.name
        data.decode("utf-8")
    assert b"\r\n" in (folder / "screens" / f"{ID_A}.png").read_bytes()
    assert "確認".encode() in (folder / "summary.txt").read_bytes()
    assert "日本".encode() in (folder / "run-start.json").read_bytes()


# --- escaping ---


def test_control_and_bidi_characters_are_escaped(tmp_path):
    writer = make(tmp_path, package=f"p{BIDI}")
    screen = make_screen(package=f"p{BIDI}", labels=(f"{BIDI}{CONTROL}",))
    writer.write_screen(screen)
    element = {"text": f"{BIDI}{CONTROL}"}
    writer.write_transition_start(1, "tap", ID_A, element)
    writer.write_skip(ID_A, f"fp{BIDI}", ["r"], element)
    writer.finish(f"stop{BIDI}{CONTROL}", screens=1, actions=1)
    folder = tmp_path / "run"
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8")
        for char in "\u202e\u2066\x1b\x9b\x07":
            assert char not in text, (path.name, hex(ord(char)))
    assert lines_of(folder / "transitions.jsonl")[0]["action"]["element"] == element
    assert "\\u202e" in (folder / "skipped.jsonl").read_text(encoding="utf-8")
    summary = (folder / "summary.txt").read_text(encoding="utf-8")
    assert "\\u202e" in summary
    assert "\\u001b" in summary
    assert "\\u009b" in summary


# --- failures ---


class _FailingHandle:
    """Writes half of the data, then fails like a full disk."""

    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()

    def write(self, data):
        self.handle.write(data[: len(data) // 2])
        self.handle.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self):
        self.handle.flush()

    def fileno(self):
        return self.handle.fileno()

    def close(self):
        self.handle.close()


def fail_on(monkeypatch, name: str) -> None:
    real = builtins.open

    def fake(path, *args, **kwargs):
        handle = real(path, *args, **kwargs)
        return _FailingHandle(handle) if Path(path).name == name else handle

    monkeypatch.setattr(runlog, "open", fake, raising=False)


def test_a_failed_file_stays_and_is_named(tmp_path, monkeypatch):
    writer = make(tmp_path)
    folder = tmp_path / "run"
    screen = make_screen()
    fail_on(monkeypatch, f"{screen.screen_id}.json")
    with pytest.raises(RunFolderError) as raised:
        writer.write_screen(screen)
    target = folder / "screens" / f"{screen.screen_id}.json"
    assert str(target) in str(raised.value)
    assert raised.value.details["path"] == str(target)
    assert isinstance(raised.value, UiprobeError)
    assert isinstance(raised.value.__cause__, OSError)
    assert target.is_file()
    assert len(target.read_bytes()) > 0  # the partial file stays
    partial = target.read_bytes()
    monkeypatch.undo()
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_skip(ID_A, "fp", ["r"], {})
    with pytest.raises(RunFolderError):
        writer.finish("done", screens=1, actions=1)
    assert not (folder / "run.json").exists()
    assert target.read_bytes() == partial


def test_a_failed_line_stays_and_later_lines_are_refused(tmp_path, monkeypatch):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    path = tmp_path / "run" / "transitions.jsonl"
    good = path.read_bytes()

    def broken(*_arguments):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(runlog, "_put", broken)
    with pytest.raises(RunFolderError, match=r"transitions\.jsonl"):
        writer.write_transition_end(1, ID_A)
    monkeypatch.undo()
    assert path.read_bytes() == good
    with pytest.raises(RunFolderError):
        writer.write_transition_start(2, "back")
    assert path.read_bytes() == good


def test_a_failure_in_the_first_file_names_it(tmp_path, monkeypatch):
    fail_on(monkeypatch, "run-start.json")
    with pytest.raises(RunFolderError, match=r"run-start\.json"):
        make(tmp_path)
    assert (tmp_path / "run" / "run-start.json").is_file()


def test_an_existing_file_is_never_opened_again(tmp_path):
    writer = make(tmp_path)
    existing = tmp_path / "run" / "transitions.jsonl"
    existing.write_bytes(b"mine\n")
    with pytest.raises(RunFolderError, match=r"transitions\.jsonl"):
        writer.write_transition_start(1, "launch")
    assert existing.read_bytes() == b"mine\n"


def test_an_existing_screens_folder_is_an_error(tmp_path):
    writer = make(tmp_path)
    (tmp_path / "run" / "screens").mkdir()
    with pytest.raises(RunFolderError):
        writer.write_screen(make_screen())
    assert names(tmp_path / "run" / "screens") == []


# --- critical ---


def test_critical_sets_and_clears_writing(tmp_path):
    writer = make(tmp_path)
    assert writer.writing is False
    with writer.critical():
        assert writer.writing is True
        with writer.critical():
            assert writer.writing is True
        assert writer.writing is True
    assert writer.writing is False


def test_critical_clears_writing_after_an_exception(tmp_path):
    writer = make(tmp_path)
    message = "boom"
    with pytest.raises(RuntimeError), writer.critical():  # noqa: PT012
        assert writer.writing is True
        raise RuntimeError(message)
    assert writer.writing is False


def test_writing_is_read_only(tmp_path):
    writer = make(tmp_path)
    with pytest.raises(AttributeError):
        writer.writing = True  # ty: ignore[invalid-assignment]


def test_writing_is_true_during_a_write_and_false_after_an_error(tmp_path, monkeypatch):
    writer = make(tmp_path)
    seen = []

    def spy(*_arguments):
        seen.append(writer.writing)
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(runlog, "_put", spy)
    with pytest.raises(RunFolderError):
        writer.write_transition_start(1, "launch")
    assert seen == [True]
    assert writer.writing is False


def test_the_context_manager_closes_the_line_files(tmp_path):
    with make(tmp_path) as writer:
        writer.write_transition_start(1, "launch")
        stream = writer._streams["transitions.jsonl"]
    assert stream.closed
    assert lines_of(tmp_path / "run" / "transitions.jsonl")[0]["action_id"] == 1


# --- edge cases ---


def test_a_folder_named_screens_can_be_used(tmp_path):
    writer = RunWriter.create(
        tmp_path / "screens",
        package="p",
        limits=Limits(),
        deny_words=[],
        serial="s",
        started_at=STARTED,
    )
    _WRITERS.append(writer)
    assert writer.write_screen(make_screen())
    assert names(tmp_path / "screens") == [
        "run-start.json",
        "screens",
        f"screens/{make_screen().screen_id}.json",
    ]


def test_a_relative_folder_stays_where_it_was_made(tmp_path, monkeypatch):
    (tmp_path / "here").mkdir()
    (tmp_path / "elsewhere" / "run").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "here")
    writer = RunWriter.create(
        "run", package="p", limits=Limits(), deny_words=[], serial="s"
    )
    _WRITERS.append(writer)
    monkeypatch.chdir(tmp_path / "elsewhere")
    writer.write_transition_start(1, "launch")
    assert names(tmp_path / "elsewhere" / "run") == []
    assert (tmp_path / "here" / "run" / "transitions.jsonl").exists()


def test_text_that_cannot_be_encoded_leaves_no_file(tmp_path):
    writer = make(tmp_path)
    bad = "x\ud800"
    with pytest.raises(ValueError, match="UTF-8"):
        writer.write_skip(ID_A, "f", ["r"], {"text": bad})
    with pytest.raises(ValueError, match="UTF-8"):
        writer.finish(bad, screens=0, actions=0)
    assert names(tmp_path / "run") == ["run-start.json"]
    # nothing was recorded as seen, and the writer is still usable
    assert writer.write_skip(ID_A, "f", ["r"], {"text": "ok"})
    writer.finish("done", screens=0, actions=0)


def test_a_failed_skip_is_not_remembered(tmp_path, monkeypatch):
    writer = make(tmp_path)

    def broken(*_arguments):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(runlog, "_put", broken)
    with pytest.raises(RunFolderError):
        writer.write_skip(ID_A, "f", ["r"], {"text": "x"})
    assert ID_A not in {key[0] for key in writer._skipped}


def test_writing_after_close_is_refused_clearly(tmp_path):
    with make(tmp_path) as writer:
        writer.write_transition_start(1, "launch")
    before = (tmp_path / "run" / "transitions.jsonl").read_bytes()
    with pytest.raises(RunFolderError, match="closed"):
        writer.write_transition_end(1, ID_A)
    assert (tmp_path / "run" / "transitions.jsonl").read_bytes() == before
    assert writer._failed is None


class _Recorder:
    """A file handle that logs write, flush and close under the file's name."""

    def __init__(self, handle, name, events, by_fd):
        self.handle = handle
        self.name = name
        self.events = events
        by_fd[handle.fileno()] = name

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def write(self, data):
        self.events.append((self.name, "write"))
        return self.handle.write(data)

    def flush(self):
        self.events.append((self.name, "flush"))
        self.handle.flush()

    def fileno(self):
        return self.handle.fileno()

    def close(self):
        self.events.append((self.name, "close"))
        self.handle.close()


def test_every_write_is_flushed_then_synced_per_file(tmp_path, monkeypatch):
    events: list[tuple[str, str]] = []
    by_fd: dict[int, str] = {}
    real_open = builtins.open
    real_fsync = os.fsync

    def recording_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return _Recorder(handle, Path(path).name, events, by_fd)

    def recording_fsync(fd):
        events.append((by_fd[fd], "fsync"))
        real_fsync(fd)

    monkeypatch.setattr(runlog, "open", recording_open, raising=False)
    monkeypatch.setattr(runlog.os, "fsync", recording_fsync)
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    writer.write_transition_end(1, ID_A)  # a second line on the open stream
    assert writer.write_skip(ID_A, "fp", ["r"], {"text": "x"})
    screen = make_screen()
    writer.write_screen(screen)
    writer.write_screenshot(screen.screen_id, PNG)
    writer.finish("done", screens=1, actions=1)
    step = ["write", "flush", "fsync"]
    expected = {
        "run-start.json": [*step, "close"],
        "transitions.jsonl": [*step, *step, "close"],
        "skipped.jsonl": [*step, "close"],
        f"{screen.screen_id}.json": [*step, "close"],
        f"{screen.screen_id}.png": [*step, "close"],
        "summary.txt": [*step, "close"],
        "run.json": [*step, "close"],
    }
    found = {name: [op for who, op in events if who == name] for name in expected}
    assert found == expected
    assert {who for who, _ in events} == set(expected)
    # run.json is the last file touched
    assert events[-len(expected["run.json"]) :] == [
        ("run.json", op) for op in expected["run.json"]
    ]


def test_a_failed_fsync_breaks_the_writer(tmp_path, monkeypatch):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")

    def broken(_fd):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(runlog.os, "fsync", broken)
    with pytest.raises(RunFolderError, match=r"transitions\.jsonl"):
        writer.write_transition_end(1, ID_A)
    monkeypatch.undo()
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_skip(ID_A, "fp", ["r"], {})
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_screenshot(ID_A, PNG)
    assert names(tmp_path / "run") == ["run-start.json", "transitions.jsonl"]


def test_run_start_records_back_on_other_screens(tmp_path):
    RunWriter.create(
        tmp_path / "run",
        package="com.example.app",
        limits=Limits(),
        deny_words=[],
        serial="emulator-5554",
        started_at=STARTED,
        back_on_other_screens=True,
    )
    start = json.loads((tmp_path / "run" / "run-start.json").read_bytes())
    assert start["back_on_other_screens"] is True
    loose: Any = RunWriter.create
    with pytest.raises(TypeError, match="back_on_other_screens must be a bool"):
        loose(
            tmp_path / "other",
            package="com.example.app",
            limits=Limits(),
            deny_words=[],
            serial="emulator-5554",
            back_on_other_screens=1,
        )
    assert not (tmp_path / "other").exists()


def test_run_start_and_run_json_record_the_repins(tmp_path):
    writer = make(tmp_path, repin=True)
    writer.finish("done", screens=0, actions=0, ended_at=STARTED, repins=2)
    folder = tmp_path / "run"
    start = json.loads((folder / "run-start.json").read_bytes())
    assert start["repin"] is True
    assert list(start)[-1] == "repin"
    end = json.loads((folder / "run.json").read_bytes())
    assert end["repins"] == 2
    other = make(tmp_path, "other")
    other.finish("done", screens=0, actions=0, ended_at=STARTED)
    assert "repin" not in json.loads(
        (tmp_path / "other" / "run-start.json").read_bytes()
    )
    assert "repins" not in json.loads((tmp_path / "other" / "run.json").read_bytes())
    loose: Any = RunWriter.create
    with pytest.raises(TypeError, match="repin must be a bool or None"):
        loose(
            tmp_path / "third",
            package="com.example.app",
            limits=Limits(),
            deny_words=[],
            serial="emulator-5554",
            repin=1,
        )
    with pytest.raises(ValueError, match="repins must not be negative"):
        make(tmp_path, "fourth").finish("done", screens=0, actions=0, repins=-1)


def test_a_not_sent_end_line_can_say_why_in_details(tmp_path):
    writer = make(tmp_path, check_tap_window=True)
    writer.write_transition_start(1, "tap", ID_A)
    writer.write_transition_end(
        1,
        not_sent="covered_by_other",
        details={"window": "ChatHead", "owner_uid": 10234},
    )
    end = lines_of(tmp_path / "run" / "transitions.jsonl")[-1]
    assert end["reason"] == "covered_by_other"
    assert end["details"] == {"window": "ChatHead", "owner_uid": 10234}
    assert list(end)[-1] == "details"
    start = json.loads((tmp_path / "run" / "run-start.json").read_bytes())
    assert start["check_tap_window"] is True
    with pytest.raises(ValueError, match="only for an action that was not sent"):
        writer.write_transition_end(2, ID_B, details={"x": "y"})
    loose: Any = writer.write_transition_end
    with pytest.raises(TypeError, match="details values must be str or int"):
        loose(3, not_sent="cover_unknown", details={"x": 1.5})
    with pytest.raises(TypeError, match="details values must be str or int"):
        loose(3, not_sent="cover_unknown", details={"x": True})


def test_a_start_line_names_the_screen_of_another_app(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(
        1,
        "relaunch",
        ID_A,
        off_target_package="com.example.launcher",
        off_target_foreground="com.example.launcher",
        off_target_focus="com.android.systemui",
    )
    writer.write_transition_start(2, "back", ID_B, off_target_package="com.example.app")
    writer.write_transition_start(3, "back", ID_B)
    first, second, third = lines_of(tmp_path / "run" / "transitions.jsonl")
    assert first["off_target"] == {
        "package": "com.example.launcher",
        "foreground": "com.example.launcher",
        "focus": "com.android.systemui",
    }
    assert list(first)[-1] == "off_target"
    assert second["off_target"] == {"package": "com.example.app"}
    assert "off_target" not in third
    with pytest.raises(ValueError, match="need off_target_package"):
        writer.write_transition_start(4, "back", ID_B, off_target_foreground="x.y")
    with pytest.raises(ValueError, match="need off_target_package"):
        writer.write_transition_start(4, "back", ID_B, off_target_focus="x.y")
    assert len(lines_of(tmp_path / "run" / "transitions.jsonl")) == 3


def test_keys_are_written_in_a_fixed_order(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "tap", ID_A, {"text": "x"}, (1, 2))
    writer.write_transition_end(1, ID_B)
    writer.write_skip(ID_A, "f", ["r"], {"text": "x"})
    writer.finish("done", screens=1, actions=1, ended_at=STARTED)
    folder = tmp_path / "run"
    start = json.loads((folder / "run-start.json").read_bytes())
    assert list(start) == [
        "schema",
        "started_at",
        "package",
        "limits",
        "deny",
        "deny_except",
        "back_on_other_screens",
        "device",
    ]
    first, second = lines_of(folder / "transitions.jsonl")
    assert list(first) == [
        "schema",
        "recorded_at",
        "action_id",
        "action",
        "from_screen_id",
    ]
    assert list(first["action"]) == ["kind", "point", "element"]
    assert list(second) == [
        "schema",
        "recorded_at",
        "action_id",
        "dump_failed",
        "to_screen_id",
    ]
    skip = lines_of(folder / "skipped.jsonl")[0]
    assert list(skip) == [
        "schema",
        "recorded_at",
        "screen_id",
        "fingerprint",
        "reasons",
        "element",
    ]
    end = json.loads((folder / "run.json").read_bytes())
    assert list(end) == ["schema", "ended_at", "stop_reason", "screens", "actions"]


def test_the_summary_lists_clickable_elements_only(tmp_path):
    writer = make(tmp_path)
    screen = make_screen(labels=("Press",))
    plain = Element(
        id="e9",
        parent=None,
        class_name="android.widget.TextView",
        bounds=Bounds(0, 0, 10, 10),
        text="Just text",
    )
    writer.write_screen(replace(screen, elements=(*screen.elements, plain)))
    writer.finish("done", screens=1, actions=0)
    text = (tmp_path / "run" / "summary.txt").read_text(encoding="utf-8")
    assert "Press" in text
    assert "Just text" not in text


# --- review fixes ---


def test_the_constructor_cannot_make_a_writer(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"mine")
    with pytest.raises(TypeError, match="create"):
        RunWriter(target)
    with pytest.raises(TypeError, match="create"):
        RunWriter(target, _token=object())
    assert names(target) == ["keep.txt"]


class _ScreenId(str, Enum):  # noqa: UP042 (formats as "_ScreenId.HOME")
    HOME = ID_A


class _Text(str):
    def __format__(self, spec):
        return "../escaped"


@pytest.mark.parametrize("bad", [_ScreenId.HOME, _Text(ID_A)])
def test_a_str_subclass_is_not_a_screen_id(tmp_path, bad):
    assert bad == ID_A  # the value matches; only the type is wrong
    writer = make(tmp_path)
    with pytest.raises(TypeError):
        writer.write_screenshot(bad, PNG)
    with pytest.raises(TypeError):
        writer.write_skip(bad, "fp", ["r"], {})
    with pytest.raises(TypeError):
        writer.write_transition_start(1, "tap", from_screen_id=bad)
    with pytest.raises(TypeError):
        writer.write_transition_end(1, bad)
    assert names(tmp_path) == ["run", "run/run-start.json"]
    assert writer.write_screenshot(ID_A, PNG)
    assert names(tmp_path / "run" / "screens") == [f"{ID_A}.png"]


class _CloseFails:
    """A line file whose close fails after closing the real file."""

    def __init__(self, handle):
        self.handle = handle

    def write(self, data):
        return self.handle.write(data)

    def flush(self):
        self.handle.flush()

    def fileno(self):
        return self.handle.fileno()

    def close(self):
        self.handle.close()
        raise OSError(errno.EIO, "I/O error")


def test_a_failed_stream_close_at_finish_leaves_no_run_json(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    real = writer._streams["transitions.jsonl"]
    writer._streams["transitions.jsonl"] = _CloseFails(real)  # ty: ignore[invalid-assignment]
    with pytest.raises(RunFolderError, match=r"transitions\.jsonl"):
        writer.finish("done", screens=0, actions=1)
    assert names(tmp_path / "run") == ["run-start.json", "transitions.jsonl"]
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.finish("done", screens=0, actions=1)


def test_a_failed_summary_leaves_no_run_json(tmp_path, monkeypatch):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    stream = writer._streams["transitions.jsonl"]
    fail_on(monkeypatch, "summary.txt")
    with pytest.raises(RunFolderError, match=r"summary\.txt"):
        writer.finish("done", screens=0, actions=1)
    assert stream.closed  # the line files were closed first
    assert not (tmp_path / "run" / "run.json").exists()


class _BufferedFailure:
    """Writes half a line, keeps the rest buffered, and fails to flush.

    Like a real buffered file, close() writes what is still buffered.
    """

    def __init__(self, handle):
        self.handle = handle
        self.pending = ""
        self.closed = False

    def write(self, data):
        half = len(data) // 2
        self.handle.write(data[:half])
        self.handle.flush()
        self.pending += data[half:]
        return len(data)

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    def fileno(self):
        return self.handle.fileno()

    def close(self):
        if not self.closed:
            self.closed = True
            self.handle.write(self.pending)
            self.handle.close()


def test_a_failed_stream_gets_no_bytes_after_the_error(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    path = tmp_path / "run" / "transitions.jsonl"
    stream = _BufferedFailure(writer._streams["transitions.jsonl"])
    writer._streams["transitions.jsonl"] = stream  # ty: ignore[invalid-assignment]
    with pytest.raises(RunFolderError, match=r"transitions\.jsonl"):
        writer.write_transition_end(1, ID_A)
    assert stream.closed
    assert "transitions.jsonl" not in writer._streams
    after_error = path.read_bytes()
    writer.close()
    assert path.read_bytes() == after_error


class _Watched(set):
    def __init__(self, writer, seen):
        super().__init__()
        self.writer = writer
        self.seen = seen

    def add(self, item):
        self.seen.append(self.writer.writing)
        super().add(item)


class _WatchedDict(dict):
    def __init__(self, writer, seen):
        super().__init__()
        self.writer = writer
        self.seen = seen

    def __setitem__(self, key, value):
        self.seen.append(self.writer.writing)
        super().__setitem__(key, value)


def test_writing_stays_true_until_the_write_is_recorded(tmp_path):
    writer = make(tmp_path)
    seen: list[bool] = []
    writer._skipped = _Watched(writer, seen)
    writer._pictures = _Watched(writer, seen)
    writer._screens = _WatchedDict(writer, seen)
    assert writer.write_skip(ID_A, "fp", ["r"], {})
    assert writer.write_screenshot(ID_A, PNG)
    assert writer.write_screen(make_screen())
    assert seen == [True, True, True]
    assert writer.writing is False


@pytest.mark.parametrize(
    "call",
    [
        lambda w: w.write_transition_start(1, "launch"),
        lambda w: w.write_skip(ID_A, "fp", ["r"], {}),
        lambda w: w.write_screenshot(ID_A, PNG),
        lambda w: w.write_screen(make_screen()),
        lambda w: w.finish("done", screens=0, actions=0),
    ],
)
def test_an_interrupt_during_a_write_breaks_the_writer(tmp_path, monkeypatch, call):
    writer = make(tmp_path)

    def interrupted(*_arguments):
        raise KeyboardInterrupt

    monkeypatch.setattr(runlog, "_put", interrupted)
    with pytest.raises(KeyboardInterrupt):
        call(writer)
    monkeypatch.undo()
    assert writer.writing is False
    assert writer._failed is not None
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_transition_start(2, "back")
    assert not (tmp_path / "run" / "run.json").exists()


def test_a_repeat_does_not_hide_a_failed_writer(tmp_path, monkeypatch):
    writer = make(tmp_path)
    screen = make_screen()
    assert writer.write_skip(ID_A, "fp", ["r"], {})
    assert writer.write_screen(screen)
    assert writer.write_screenshot(ID_A, PNG)

    def broken(*_arguments):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(runlog, "_put", broken)
    with pytest.raises(RunFolderError):
        writer.write_transition_start(1, "launch")
    monkeypatch.undo()
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_skip(ID_A, "fp", ["r"], {})
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_screen(screen)
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_screenshot(ID_A, PNG)


def test_a_repeat_does_not_hide_a_closed_writer(tmp_path):
    writer = make(tmp_path)
    screen = make_screen()
    assert writer.write_skip(ID_A, "fp", ["r"], {})
    assert writer.write_screen(screen)
    assert writer.write_screenshot(ID_A, PNG)
    writer.close()
    with pytest.raises(RunFolderError, match="closed"):
        writer.write_skip(ID_A, "fp", ["r"], {})
    with pytest.raises(RunFolderError, match="closed"):
        writer.write_screen(screen)
    with pytest.raises(RunFolderError, match="closed"):
        writer.write_screenshot(ID_A, PNG)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_refused_before_any_file_is_opened(tmp_path, number):
    writer = make(tmp_path)
    with pytest.raises(ValueError, match="not JSON compliant"):
        writer.write_transition_start(1, "tap", element={"x": number})
    with pytest.raises(ValueError, match="not JSON compliant"):
        writer.write_skip(ID_A, "fp", ["r"], {"nested": [{"x": number}]})
    assert names(tmp_path / "run") == ["run-start.json"]
    # nothing was recorded, and the writer is still usable
    assert writer.write_skip(ID_A, "fp", ["r"], {"x": 1.5})
    writer.finish("done", screens=0, actions=0)


# --- second review ---


class _InterruptOnAdd(set):
    """Raises KeyboardInterrupt on the first add only."""

    interrupted = False

    def add(self, item):
        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        super().add(item)


def test_an_interrupt_before_a_skip_is_recorded_breaks_the_writer(tmp_path):
    writer = make(tmp_path)
    writer._skipped = _InterruptOnAdd()
    with pytest.raises(KeyboardInterrupt):
        writer.write_skip(ID_A, "fp", ["r"], {})
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_skip(ID_A, "fp", ["r"], {})
    assert len(lines_of(tmp_path / "run" / "skipped.jsonl")) == 1
    assert writer.writing is False


def test_an_interrupt_after_run_json_breaks_the_writer(tmp_path, monkeypatch):
    writer = make(tmp_path)
    real = writer._write_new

    def then_interrupt(path, data, **kwargs):
        real(path, data, **kwargs)
        if path.name == "run.json":
            raise KeyboardInterrupt

    monkeypatch.setattr(writer, "_write_new", then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        writer.finish("done", screens=0, actions=0)
    monkeypatch.undo()
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.finish("done", screens=0, actions=0)


def test_an_argument_error_does_not_break_the_writer(tmp_path):
    writer = make(tmp_path)
    with pytest.raises(TypeError):
        writer.write_skip(ID_A, "fp", ["r"], {"x": object()})
    assert writer._failed is None
    assert writer.write_skip(ID_A, "fp", ["r"], {})


def test_a_screen_with_a_non_finite_number_writes_nothing(tmp_path):
    writer = make(tmp_path)
    screen = replace(make_screen(), display=(float("nan"), 2400))
    with pytest.raises(ValueError, match="not JSON compliant"):
        writer.write_screen(screen)
    assert names(tmp_path / "run") == ["run-start.json"]
    assert writer._failed is None
    assert writer.write_screen(make_screen())


def test_a_circular_mapping_is_reported_as_circular(tmp_path):
    writer = make(tmp_path)
    element: dict[str, object] = {}
    element["self"] = element
    with pytest.raises(ValueError, match="Circular") as raised:
        writer.write_transition_start(1, "tap", element=element)
    assert "JSON compliant" not in str(raised.value)
    assert names(tmp_path / "run") == ["run-start.json"]


class _FlushFailsCloseInterrupted:
    def __init__(self, handle):
        self.handle = handle

    def write(self, data):
        return self.handle.write(data)

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    def fileno(self):
        return self.handle.fileno()

    def close(self):
        self.handle.close()
        raise KeyboardInterrupt


def test_an_interrupt_while_dropping_a_failed_stream_keeps_the_failure(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    path = tmp_path / "run" / "transitions.jsonl"
    real = writer._streams["transitions.jsonl"]
    writer._streams["transitions.jsonl"] = _FlushFailsCloseInterrupted(real)  # ty: ignore[invalid-assignment]
    with pytest.raises(KeyboardInterrupt):
        writer.write_transition_end(1, ID_A)
    with pytest.raises(RunFolderError, match="earlier write failed") as raised:
        writer.write_transition_start(2, "back")
    assert raised.value.details["path"] == str(path)
    with pytest.raises(RunFolderError) as raised:
        writer.finish("done", screens=0, actions=1)
    assert raised.value.details["path"] == str(path)
    assert not (tmp_path / "run" / "run.json").exists()


class _CloseInterrupted:
    def __init__(self, handle):
        self.handle = handle

    def close(self):
        self.handle.close()
        raise KeyboardInterrupt


def test_an_interrupted_close_leaves_the_other_streams_reachable(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    writer.write_skip(ID_A, "fp", ["r"], {})
    skipped = writer._streams["skipped.jsonl"]
    real = writer._streams["transitions.jsonl"]
    writer._streams["transitions.jsonl"] = _CloseInterrupted(real)  # ty: ignore[invalid-assignment]
    with pytest.raises(KeyboardInterrupt):
        writer.close()
    assert real.closed
    assert writer._streams == {"skipped.jsonl": skipped}
    assert not skipped.closed
    writer.close()
    assert skipped.closed
    assert writer._streams == {}


def test_a_repeated_skip_on_a_failed_writer_reports_the_failure(tmp_path, monkeypatch):
    writer = make(tmp_path)
    assert writer.write_skip(ID_A, "fp", ["r"], {})

    def broken(*_arguments):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(runlog, "_put", broken)
    with pytest.raises(RunFolderError):
        writer.write_transition_start(1, "launch")
    monkeypatch.undo()
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_skip(ID_A, "fp", ["r"], {"x": object()})


# --- third review ---


class _CloseRaises:
    def __init__(self, handle, error):
        self.handle = handle
        self.error = error

    def close(self):
        self.handle.close()
        raise self.error


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_any_failed_stream_close_at_finish_is_remembered(tmp_path, error):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    path = tmp_path / "run" / "transitions.jsonl"
    real = writer._streams["transitions.jsonl"]
    writer._streams["transitions.jsonl"] = _CloseRaises(real, error)  # ty: ignore[invalid-assignment]
    with pytest.raises(type(error)):
        writer.finish("done", screens=0, actions=1)
    with pytest.raises(RunFolderError, match="earlier write failed") as raised:
        writer.finish("done", screens=0, actions=1)
    assert raised.value.details["path"] == str(path)
    assert names(tmp_path / "run") == ["run-start.json", "transitions.jsonl"]


class _InterruptBeforeClose:
    """Interrupts once when close is looked up, before close() starts."""

    def __init__(self, handle):
        self.handle = handle
        self.interrupted = False

    @property
    def close(self):
        if not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        return self.handle.close


def test_an_interrupt_before_a_close_keeps_that_stream_reachable(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    writer.write_skip(ID_A, "fp", ["r"], {})
    skipped = writer._streams["skipped.jsonl"]
    real = writer._streams["transitions.jsonl"]
    stream = _InterruptBeforeClose(real)
    writer._streams["transitions.jsonl"] = stream  # ty: ignore[invalid-assignment]
    with pytest.raises(KeyboardInterrupt):
        writer.close()
    assert writer._streams == {"transitions.jsonl": stream, "skipped.jsonl": skipped}
    assert not real.closed
    writer.close()
    assert real.closed
    assert skipped.closed
    assert writer._streams == {}


class _Unreadable(Mapping):
    """A mapping that fails as soon as it is read."""

    def __getitem__(self, key):
        raise LookupError(key)

    def __iter__(self):
        msg = "read"
        raise RuntimeError(msg)

    def __len__(self):
        return 1


def test_a_repeated_skip_does_not_read_the_element(tmp_path, monkeypatch):
    writer = make(tmp_path)
    assert writer.write_skip(ID_A, "fp", ["r"], {})
    assert writer.write_skip(ID_A, "fp", ["r"], _Unreadable()) is False

    def broken(*_arguments):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(runlog, "_put", broken)
    with pytest.raises(RunFolderError):
        writer.write_transition_start(1, "launch")
    monkeypatch.undo()
    with pytest.raises(RunFolderError, match="earlier write failed"):
        writer.write_skip(ID_A, "fp", ["r"], _Unreadable())


def test_a_skip_element_that_calls_back_gives_one_line(tmp_path):
    writer = make(tmp_path)
    results = []

    class _CallsBack(dict):
        def items(self):
            if not results:
                results.append(writer.write_skip(ID_A, "fp", ["r"], {}))
            return super().items()

    assert writer.write_skip(ID_A, "fp", ["r"], {"nested": _CallsBack(a=1)}) is False
    assert results == [True]
    assert len(lines_of(tmp_path / "run" / "skipped.jsonl")) == 1


def test_a_skip_element_that_closes_the_writer_is_refused(tmp_path):
    writer = make(tmp_path)

    class _WritesThenCloses(dict):
        def items(self):
            if not writer._skipped:
                writer.write_skip(ID_A, "fp", ["r"], {})
                writer.close()
            return super().items()

    with pytest.raises(RunFolderError, match="closed"):
        writer.write_skip(ID_A, "fp", ["r"], {"nested": _WritesThenCloses(a=1)})
    assert len(lines_of(tmp_path / "run" / "skipped.jsonl")) == 1


class _CloseLookupFails:
    """A handle whose close cannot even be looked up."""

    def __init__(self, error):
        self.error = error

    @property
    def close(self):
        raise self.error


def test_a_close_failure_stays_recorded_when_the_next_close_fails(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    writer.write_skip(ID_A, "fp", ["r"], {})
    path = tmp_path / "run" / "transitions.jsonl"
    real = writer._streams["transitions.jsonl"]
    skipped = writer._streams["skipped.jsonl"]
    failing = _CloseRaises(real, OSError(errno.EIO, "I/O error"))
    writer._streams["transitions.jsonl"] = failing  # ty: ignore[invalid-assignment]
    lookup = _CloseLookupFails(MemoryError())
    writer._streams["skipped.jsonl"] = lookup  # ty: ignore[invalid-assignment]
    with pytest.raises(MemoryError):
        writer.finish("done", screens=0, actions=1)
    skipped.close()
    writer._streams.clear()
    with pytest.raises(RunFolderError, match="earlier write failed") as raised:
        writer.finish("done", screens=0, actions=1)
    assert raised.value.details["path"] == str(path)
    assert "run.json" not in names(tmp_path / "run")


def test_folders_are_made_with_mode_0o700(tmp_path, monkeypatch):
    modes = []
    real = os.mkdir

    def spy(path, mode=0o777):
        modes.append(mode)
        real(path, mode)

    monkeypatch.setattr(runlog.os, "mkdir", spy)
    writer = make(tmp_path)
    writer.write_screen(make_screen())
    monkeypatch.undo()
    assert modes == [0o700, 0o700]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_only_the_owner_can_open_the_run_folders(tmp_path):
    writer = make(tmp_path)
    writer.write_screen(make_screen())
    for folder in (tmp_path / "run", tmp_path / "run" / "screens"):
        assert folder.stat().st_mode & 0o077 == 0


def test_each_record_is_encoded_once_and_strictly(tmp_path, monkeypatch):
    calls = []
    real = runlog.dumps

    def spy(value, **kwargs):
        calls.append((value.get("schema"), kwargs.get("allow_nan", True)))
        return real(value, **kwargs)

    monkeypatch.setattr(runlog, "dumps", spy)
    writer = make(tmp_path)
    writer.write_transition_start(1, "launch")
    writer.write_transition_end(1, ID_A)
    writer.write_skip(ID_A, "fp", ["r"], {})
    writer.write_screen(make_screen())
    writer.finish("done", screens=1, actions=1)
    assert calls == [
        ("uiprobe.run_start/1", False),
        ("uiprobe.transition_start/1", False),
        ("uiprobe.transition_end/1", False),
        ("uiprobe.skip/1", False),
        ("uiprobe.screen/1", False),
        ("uiprobe.run/1", False),
    ]


class _PathNamingAnother(Path):
    """A path whose own parts name one place and whose fspath names another."""

    other = ""

    def __fspath__(self) -> str:
        return self.other


def test_the_fspath_of_a_path_subclass_is_what_is_checked(tmp_path):
    existing = tmp_path / "exists"
    existing.mkdir()
    out = _PathNamingAnother(tmp_path / "new")
    out.other = str(existing)
    with pytest.raises(RunFolderError, match="already exists"):
        _create(out)
    assert names(tmp_path) == ["exists"]


class _ShapeShifter(str):
    """Text that would join, divide and convert to `elsewhere`."""

    __slots__ = ()
    elsewhere = ""

    def __add__(self, other):
        return self.elsewhere

    def __truediv__(self, other):
        return Path(self.elsewhere)

    def __str__(self) -> str:
        return self.elsewhere


def _record_checks_and_creation(monkeypatch):
    checked: list[object] = []
    made: list[object] = []
    real_check = runlog.check_new_path
    real_make = runlog._make_dir

    def check(path):
        checked.append(path)
        return real_check(path)

    def make_dir(path):
        made.append(path)
        return real_make(path)

    monkeypatch.setattr(runlog, "check_new_path", check)
    monkeypatch.setattr(runlog, "_make_dir", make_dir)
    return checked, made


def test_one_plain_str_goes_from_the_check_to_the_creation(tmp_path, monkeypatch):
    checked, made = _record_checks_and_creation(monkeypatch)
    expected = str(tmp_path / "run")
    _ShapeShifter.elsewhere = str(tmp_path / "elsewhere")
    writer = _create(_ShapeShifter(expected))
    assert checked == [expected]
    assert made == [expected]
    assert all(type(value) is str for value in [*checked, *made])
    assert writer.path == Path(expected)
    assert (tmp_path / "run" / "run-start.json").is_file()
    assert names(tmp_path) == ["run", "run/run-start.json"]


def test_a_relative_path_is_checked_as_given_and_as_joined(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checked, made = _record_checks_and_creation(monkeypatch)
    _create("run")
    joined = os.path.join(os.getcwd(), "run")  # noqa: PTH109, PTH118
    assert checked == ["run", joined]
    assert made == [joined]


def test_an_empty_path_is_refused_as_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RunFolderError, match="must not be empty"):
        _create("")
    assert names(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="the limit differs on Windows")
def test_a_path_that_passes_only_once_normalized_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    given = "./" * 2100 + "run"  # too long as given; "run" once normalized
    with pytest.raises(RunFolderError, match="cannot check"):
        _create(given)
    assert names(tmp_path) == []


def test_an_unreadable_working_folder_is_a_run_folder_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def getcwd():
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    with monkeypatch.context() as scoped:  # os is global: keep the patch short
        scoped.setattr(runlog.os, "getcwd", getcwd)
        with pytest.raises(RunFolderError, match="working folder") as raised:
            _create("run")
    assert raised.value.details == {"path": "run"}
    assert names(tmp_path) == []


@pytest.mark.parametrize("out", [b"run", 5, None])
def test_out_that_is_not_text_is_refused(tmp_path, monkeypatch, out):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError):
        _create(out)
    assert names(tmp_path) == []


@pytest.mark.parametrize(
    "path",
    ["C:run", "c:runs/x", "\\run", "/uiprobe-no-such-run", "\\runs\\x"],
    ids=[
        "drive",
        "drive-slash-inside",
        "root",
        "root-slash",
        "root-deep",
    ],
)
def test_on_windows_a_path_relative_to_a_drive_folder_is_refused(
    tmp_path, monkeypatch, path
):
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    monkeypatch.chdir(tmp_path)
    checked: list[str] = []

    def never_make(folder):
        pytest.fail(f"the folder {folder!r} was about to be made")

    # Nothing outside the test folder is looked up or made, whatever is there.
    monkeypatch.setattr(runlog, "check_new_path", checked.append)
    monkeypatch.setattr(runlog, "_make_dir", never_make)
    with pytest.raises(
        RunFolderError, match="give a full path with its drive"
    ) as raised:
        _create(path)
    assert raised.value.details == {"path": path}
    assert "relative to the working folder" in raised.value.message
    assert checked == [path]  # checked as given, then refused before joining


def test_on_windows_a_bare_drive_depends_on_its_folder(monkeypatch):
    # The working folder of the drive exists, so create says "already exists"
    # first on Windows; the rule itself still counts it.
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    assert pngfile.depends_on_a_drive_folder("C:")


@pytest.mark.parametrize(
    "path",
    [
        "C:\\runs\\x",
        "c:/runs/x",
        "\\\\server\\share\\x",
        "//server/share/x",
        "\\\\?\\C:\\x",
        "runs\\x",
        "runs/x",
    ],
)
def test_on_windows_other_forms_do_not_depend_on_a_drive_folder(monkeypatch, path):
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    assert not pngfile.depends_on_a_drive_folder(path)


@pytest.mark.parametrize("path", ["C:run", "\\run", "/run"])
def test_elsewhere_no_path_depends_on_a_drive_folder(monkeypatch, path):
    monkeypatch.setattr(pngfile, "_WINDOWS", False)
    assert not pngfile.depends_on_a_drive_folder(path)


@pytest.mark.skipif(os.name != "nt", reason="drive folders exist on Windows")
def test_on_this_windows_a_path_relative_to_a_drive_folder_is_refused(tmp_path):
    drive = os.path.splitdrive(str(tmp_path))[0]
    name = f"uiprobe-{tmp_path.name}"
    for path in (f"{drive}{name}", f"\\{name}", f"/{name}"):
        with pytest.raises(RunFolderError, match="give a full path with its drive"):
            _create(path)
        assert not os.path.lexists(path)


def test_a_relative_path_is_fixed_when_the_run_starts(tmp_path, monkeypatch):
    (tmp_path / "runs").mkdir()
    (tmp_path / "other").mkdir()
    monkeypatch.chdir(tmp_path)
    writer = _create(os.path.join("runs", "x"))  # noqa: PTH118
    monkeypatch.chdir(tmp_path / "other")
    writer.finish("exhausted", screens=0, actions=0)
    assert writer.path == tmp_path / "runs" / "x"
    assert names(tmp_path / "runs" / "x") == [
        "run-start.json",
        "run.json",
        "summary.txt",
    ]
    assert names(tmp_path / "other") == []


def test_a_not_sent_end_line_has_its_reason_and_no_destination(tmp_path):
    writer = make(tmp_path)
    writer.write_transition_start(1, "tap", ID_A, None, (5, 5))
    writer.write_transition_end(1, not_sent="focus_other")
    writer.close()
    end = lines_of(tmp_path / "run" / "transitions.jsonl")[1]
    assert set(end) == {"schema", "recorded_at", "action_id", "not_sent", "reason"}
    assert (end["not_sent"], end["reason"], end["action_id"]) == (
        True,
        "focus_other",
        1,
    )


@pytest.mark.parametrize(
    "kwargs", [{"dump_failed": True}, {"to_screen_id": ID_A}], ids=["dump", "screen"]
)
def test_a_not_sent_end_line_refuses_a_destination(tmp_path, kwargs):
    writer = make(tmp_path)
    with pytest.raises(ValueError, match="not sent"):
        writer.write_transition_end(1, not_sent="stopped", **kwargs)


def test_the_writer_counts_complete_start_lines(tmp_path):
    writer = make(tmp_path)
    assert writer.starts_written == 0
    writer.write_transition_start(1, "launch")
    writer.write_transition_end(1, not_sent="stopped")
    writer.write_transition_start(2, "back", ID_A)
    assert writer.starts_written == 2


# --- skip details ---

DENY_DETAIL = {
    "reason": "deny_word",
    "word": "delete",
    "field": "text",
    "element_id": "e3",
}


def test_a_skip_line_carries_its_details_between_reasons_and_element(tmp_path):
    writer = make(tmp_path)
    confirm = {
        "reason": "confirm_button",
        "note": "OK",
        "word": "pay",
        "field": "joined",
    }
    element = {"id": "e3", "class": "android.widget.Button"}
    assert writer.write_skip(
        ID_A, "fp1", ["deny_word", "confirm_button"], element, [DENY_DETAIL, confirm]
    )
    line = lines_of(tmp_path / "run" / "skipped.jsonl")[0]
    assert list(line) == [
        "schema",
        "recorded_at",
        "screen_id",
        "fingerprint",
        "reasons",
        "details",
        "element",
    ]
    assert line["details"] == [
        DENY_DETAIL,
        {"reason": "confirm_button", "word": "pay", "field": "joined", "note": "OK"},
    ]
    assert list(line["details"][1]) == ["reason", "word", "field", "note"]


def test_a_skip_line_without_details_has_no_details_key(tmp_path):
    writer = make(tmp_path)
    assert writer.write_skip(ID_A, "fp1", ["unlabeled"], {}, [])
    assert writer.write_skip(ID_A, "fp2", ["unlabeled"], {})
    for line in lines_of(tmp_path / "run" / "skipped.jsonl"):
        assert "details" not in line


def test_a_repeated_skip_keeps_the_first_details(tmp_path):
    writer = make(tmp_path)
    assert writer.write_skip(ID_A, "fp1", ["deny_word"], {}, [DENY_DETAIL])
    other = {**DENY_DETAIL, "word": "remove"}
    assert not writer.write_skip(ID_A, "fp1", ["deny_word"], {}, [other])
    found = lines_of(tmp_path / "run" / "skipped.jsonl")
    assert [line["details"] for line in found] == [[DENY_DETAIL]]


def test_device_text_in_skip_details_is_escaped_like_element_text(tmp_path):
    writer = make(tmp_path)
    text = "Delete\x1b[2J\u202e\u2028all"
    element = {"id": "e3", "text": text}
    detail = {"reason": "irreversible_screen", "word": "permanently", "note": text}
    assert writer.write_skip(ID_A, "fp1", ["irreversible_screen"], element, [detail])
    raw = (tmp_path / "run" / "skipped.jsonl").read_text(encoding="utf-8")
    for char in ("\x1b", "\u202e", "\u2028"):
        assert char not in raw
    line = json.loads(raw)
    assert line["details"][0]["note"] == text == line["element"]["text"]


@pytest.mark.parametrize(
    "details",
    [
        "deny_word",
        {"reason": "deny_word"},
        [("reason", "deny_word")],
        [{"word": "delete"}],
        [{"reason": "deny_word", "colour": "red"}],
        [{"reason": "deny_word", "word": 3}],
        [{"reason": "deny_word", "word": "a\ud800"}],
        None,
    ],
)
def test_bad_skip_details_write_nothing(tmp_path, details):
    writer = make(tmp_path)
    with pytest.raises((TypeError, ValueError)):
        writer.write_skip(ID_A, "fp1", ["deny_word"], {}, details)
    assert names(tmp_path / "run") == ["run-start.json"]
