"""``[rules] scroll_lists`` and the two options that override it for one run:
what is read and sent, and the plan in JSON. Scrolling is off by default."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import SERIAL, SHOP, TRANSPORT
from tests.crawl_driver.scroll_screens import screens
from tests.fake_device import FakeAndroid
from uiprobe import cli
from uiprobe.device import Device
from uiprobe.settings import SETTINGS_FILE, render_default_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

FAST = ("--settle", "0", "--quiet")
ON = "scroll_lists = true\n"
OFF = "scroll_lists = false\n"


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(
        fake_adb,
        SERIAL,
        screens(),
        start="home",
        transport_id=TRANSPORT,
        wheel=True,
    )


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_file(here: Path, *, scroll: bool) -> None:
    text = render_default_settings()
    assert text.count(OFF) == 1
    (here / SETTINGS_FILE).write_text(
        text.replace(OFF, ON) if scroll else text, encoding="utf-8", newline="\n"
    )


def crawl(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run(capsys, fake_adb, here: Path, *extra: str) -> tuple[int, str, str]:
    return crawl(capsys, fake_adb, "--press", *FAST, "--out", str(here / "run"), *extra)


def usage_reads(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands if c == "input"]


def wheels(android: FakeAndroid) -> list[tuple[str, ...]]:
    return [i.args for i in android.inputs if i.kind == "wheel"]


LINE = "scroll lists with the mouse wheel: "
TAIL = (
    "; an app's own view, or a window that appears just before the wheel, can react"
    " to it (see docs/safety.md)"
)
LIMITS = "at most 10 per screen, wheel amount 3"


def plan_line(text: str) -> str:
    [line] = [x for x in text.splitlines() if x.startswith(LINE)]
    return line.removeprefix(LINE)


def run_start(here: Path) -> dict[str, object]:
    return json.loads((here / "run" / "run-start.json").read_bytes())


# --- what is read and sent ----------------------------------------------------


def test_the_default_run_reads_no_usage_and_sends_no_wheel(
    fake_adb, android, capsys, here
):
    write_file(here, scroll=False)
    code, text, _err = run(capsys, fake_adb, here)
    assert code == 0
    assert usage_reads(fake_adb) == []
    assert wheels(android) == []
    assert [i for i in android.inputs if i.kind == "tap"]
    assert plan_line(text) == "no"
    begin = run_start(here)
    assert not {"scroll_lists", "scroll_input", "scrolls_per_screen"} & set(begin)


def test_the_file_key_alone_turns_the_wheel_after_one_usage_read(
    fake_adb, android, capsys, here
):
    write_file(here, scroll=True)
    code, text, _err = run(capsys, fake_adb, here)
    assert code == 0
    assert len(usage_reads(fake_adb)) == 1
    assert wheels(android)
    assert plan_line(text) == f"yes (settings file), {LIMITS}{TAIL}"
    begin = run_start(here)
    assert begin["scroll_lists"] is True
    assert begin["scroll_input"] is True


def test_the_option_turns_it_on_over_a_false_file(fake_adb, android, capsys, here):
    write_file(here, scroll=False)
    code, text, _err = run(capsys, fake_adb, here, "--scroll")
    assert code == 0
    assert len(usage_reads(fake_adb)) == 1
    assert wheels(android)
    assert plan_line(text) == f"yes (--scroll), {LIMITS}{TAIL}"


def test_the_option_over_a_true_file_names_the_option(fake_adb, android, capsys, here):
    write_file(here, scroll=True)
    code, text, _err = run(capsys, fake_adb, here, "--scroll")
    assert code == 0
    assert len(usage_reads(fake_adb)) == 1
    assert wheels(android)
    assert plan_line(text) == f"yes (--scroll), {LIMITS}{TAIL}"


def test_the_no_option_turns_it_off_over_a_true_file(fake_adb, android, capsys, here):
    write_file(here, scroll=True)
    code, text, _err = run(capsys, fake_adb, here, "--no-scroll")
    assert code == 0
    assert usage_reads(fake_adb) == []
    assert wheels(android) == []
    assert plan_line(text) == "no"
    assert "scroll_lists" not in run_start(here)


def test_the_no_option_with_a_false_file_sends_nothing_either(
    fake_adb, android, capsys, here
):
    write_file(here, scroll=False)
    code, text, _err = run(capsys, fake_adb, here, "--no-scroll")
    assert code == 0
    assert usage_reads(fake_adb) == []
    assert wheels(android) == []
    assert plan_line(text) == "no"


def test_the_plan_line_says_when_the_device_does_not_list_the_wheel(
    fake_adb, android, capsys, here
):
    android.input_usage = FakeAndroid.USAGE_OLD
    write_file(here, scroll=True)
    code, text, _err = run(capsys, fake_adb, here)
    assert code == 0
    assert plan_line(text) == "no (this device's input does not list the wheel scroll)"
    assert wheels(android) == []


# --- the plan in JSON ---------------------------------------------------------


@pytest.mark.parametrize(
    ("file_scroll", "argv", "value", "source"),
    [
        (False, [], False, "default"),
        (True, [], True, "file"),
        (False, ["--scroll"], True, "option"),
        (True, ["--scroll"], True, "option"),
        (True, ["--no-scroll"], False, "option"),
        (False, ["--no-scroll"], False, "option"),
    ],
)
def test_the_json_plan_names_the_source(
    fake_adb, android, capsys, here, file_scroll, argv, value, source
):
    del android
    write_file(here, scroll=file_scroll)
    code, text, _err = run(capsys, fake_adb, here, "--actions", "2", "--json", *argv)
    assert code == 0
    result = json.loads(text)
    assert result["scroll_lists"] is value
    assert result["scroll_lists_source"] == source
    assert result["scroll_input"] is (True if value else None)
    assert (
        result["scrolls_per_screen"],
        result["scroll_amount"],
        result["scroll_incidents"],
    ) == (10, 3, 2)
    assert ("scroll_lists" in run_start(here)) is value


def test_the_plan_says_when_the_device_does_not_list_the_wheel(
    fake_adb, android, capsys, here
):
    android.input_usage = FakeAndroid.USAGE_OLD
    write_file(here, scroll=False)
    code, text, _err = run(
        capsys, fake_adb, here, "--actions", "2", "--json", "--scroll"
    )
    assert code == 0
    result = json.loads(text)
    assert result["scroll_lists"] is False  # asked, but not possible here
    assert result["scroll_lists_source"] == "option"
    assert result["scroll_input"] is False
    assert len(usage_reads(fake_adb)) == 1
    assert wheels(android) == []
    begin = run_start(here)
    assert (begin["scroll_lists"], begin["scroll_input"]) == (True, False)


def test_the_run_uses_the_usage_read_the_plan_shows(
    fake_adb, android, capsys, here, monkeypatch
):
    # One read: the plan and the run agree even when the device would answer
    # differently the next time it was asked.
    write_file(here, scroll=True)
    answers = [True, False]
    asked: list[int] = []

    def supported(self: Device) -> bool:
        del self
        asked.append(1)
        return answers.pop(0)

    monkeypatch.setattr(Device, "_wheel_supported", supported)
    code, text, _err = run(capsys, fake_adb, here, "--actions", "40", "--json")
    assert code == 0
    assert asked == [1]
    assert json.loads(text)["scroll_input"] is True
    assert run_start(here)["scroll_input"] is True
    assert wheels(android)


# --- usage errors -------------------------------------------------------------

BOTH = "--scroll and --no-scroll cannot be combined"


@pytest.mark.parametrize("press", [["--press"], []])
@pytest.mark.parametrize("order", [0, 1])
def test_both_options_are_refused_before_anything_happens(
    fake_adb, android, capsys, here, press, order
):
    write_file(here, scroll=False)
    both = ["--scroll", "--no-scroll"]
    code, _text, err = crawl(
        capsys, fake_adb, *press, *both[:: 1 - 2 * order], "--out", str(here / "run")
    )
    assert code == 2
    assert err == f"uiprobe: error: {BOTH}\n"
    assert fake_adb.requests == []
    assert android.inputs == []
    assert not (here / "run").exists()


@pytest.mark.parametrize("option", ["--scroll", "--no-scroll"])
def test_both_options_are_press_only(fake_adb, android, capsys, here, option):
    write_file(here, scroll=False)
    code, _text, err = crawl(capsys, fake_adb, option)
    assert code == 2
    assert f"{option} is used only when the crawl presses" in err
    code, _text, err = crawl(capsys, fake_adb, "--dry-run", option)
    assert code == 2
    assert err.endswith(
        f"{option} is used only when the crawl presses, and --dry-run never presses\n"
    )
    assert fake_adb.requests == []
    assert android.inputs == []
