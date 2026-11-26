"""``[rules] back_on_other_screens`` and the two options that override it for
one run: what is sent, the plan line and ``back_on_other_screens_source``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import OTHER, SCREENS, SERIAL, SHOP, TRANSPORT
from tests.fake_device import FakeAndroid
from uiprobe import cli
from uiprobe.device import Device
from uiprobe.settings import SETTINGS_FILE, render_default_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

FAST = ("--settle", "0", "--quiet")
ON = "back_on_other_screens = true\n"
OFF = "back_on_other_screens = false\n"
LINE = "BACK on screens of other apps: "
NOTE = (
    f"note: the restarts kept ending on screens of other apps ({OTHER} 4 times);"
    " --back-on-other-screens (or back_on_other_screens = true under [rules] in"
    " the settings file) presses BACK on such a screen (up to twice before it"
    " restarts the app), and that BACK reaches whichever window has the input"
    " focus, not the app being crawled"
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="home", transport_id=TRANSPORT)


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_file(here: Path, *, back: bool) -> None:
    text = render_default_settings()
    assert text.count(OFF) == 1
    (here / SETTINGS_FILE).write_text(
        text.replace(OFF, ON) if back else text, encoding="utf-8", newline="\n"
    )


def crawl(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run(capsys, fake_adb, here: Path, *extra: str) -> tuple[int, str, str]:
    return crawl(capsys, fake_adb, "--press", *FAST, "--out", str(here / "run"), *extra)


@pytest.fixture
def lands_on_other_app(monkeypatch, android: FakeAndroid) -> None:
    """Every start of the app ends on a screen of another app."""
    original = Device._launch_component

    def launch_then_cover(self: Device, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]
        android.show("ext1")

    monkeypatch.setattr(Device, "_launch_component", launch_then_cover)


def keys(android: FakeAndroid) -> list[object]:
    return [i for i in android.inputs if i.kind == "key"]


def plan_line(text: str) -> str:
    [line] = [x for x in text.splitlines() if x.startswith(LINE)]
    return line.removeprefix(LINE)


def run_start(here: Path) -> dict[str, object]:
    return json.loads((here / "run" / "run-start.json").read_bytes())


# --- what is sent -------------------------------------------------------------


@pytest.mark.usefixtures("lands_on_other_app")
def test_the_default_run_sends_nothing_to_the_other_app(
    fake_adb, android, capsys, here
):
    write_file(here, back=False)
    code, text, _err = run(capsys, fake_adb, here)
    assert code == 0
    assert "stopped: relaunch_limit" in text
    assert android.inputs == []
    assert plan_line(text) == "no (restart the app)"
    assert NOTE in text.splitlines()
    assert run_start(here)["back_on_other_screens"] is False


@pytest.mark.usefixtures("lands_on_other_app")
def test_the_file_key_alone_sends_back(fake_adb, android, capsys, here):
    write_file(here, back=True)
    code, text, _err = run(capsys, fake_adb, here)
    assert code == 0
    assert keys(android)
    assert plan_line(text) == "yes (settings file)"
    assert not [
        x
        for x in text.splitlines()
        if "--back-on-other-screens" in x and x.startswith("note:")
    ]
    assert run_start(here)["back_on_other_screens"] is True


@pytest.mark.usefixtures("lands_on_other_app")
def test_the_option_turns_it_on_over_a_false_file(fake_adb, android, capsys, here):
    write_file(here, back=False)
    code, text, _err = run(capsys, fake_adb, here, "--back-on-other-screens")
    assert code == 0
    assert keys(android)
    assert plan_line(text) == "yes (--back-on-other-screens)"


@pytest.mark.usefixtures("lands_on_other_app")
def test_the_option_over_a_true_file_names_the_option(fake_adb, android, capsys, here):
    write_file(here, back=True)
    _code, text, _err = run(capsys, fake_adb, here, "--back-on-other-screens")
    assert keys(android)
    assert plan_line(text) == "yes (--back-on-other-screens)"


@pytest.mark.usefixtures("lands_on_other_app")
def test_the_no_option_turns_it_off_over_a_true_file(fake_adb, android, capsys, here):
    write_file(here, back=True)
    code, text, _err = run(capsys, fake_adb, here, "--no-back-on-other-screens")
    assert code == 0
    assert android.inputs == []
    assert "stopped: relaunch_limit" in text
    assert plan_line(text) == "no (--no-back-on-other-screens)"
    assert NOTE in text.splitlines()  # BACK was off for this run
    assert run_start(here)["back_on_other_screens"] is False


@pytest.mark.usefixtures("android")
def test_the_no_option_with_a_false_file_changes_nothing_but_the_source(
    fake_adb, capsys, here
):
    write_file(here, back=False)
    code, text, _err = run(capsys, fake_adb, here, "--no-back-on-other-screens")
    assert code == 0
    assert plan_line(text) == "no (--no-back-on-other-screens)"


# --- the plan in JSON ---------------------------------------------------------


@pytest.mark.parametrize(
    ("file_back", "argv", "value", "source"),
    [
        (False, [], False, "default"),
        (True, [], True, "file"),
        (False, ["--back-on-other-screens"], True, "option"),
        (True, ["--back-on-other-screens"], True, "option"),
        (True, ["--no-back-on-other-screens"], False, "option"),
        (False, ["--no-back-on-other-screens"], False, "option"),
    ],
)
@pytest.mark.usefixtures("android")
def test_the_json_plan_names_the_source(
    fake_adb, capsys, here, file_back, argv, value, source
):
    write_file(here, back=file_back)
    code, text, _err = run(capsys, fake_adb, here, "--actions", "2", "--json", *argv)
    assert code == 0
    result = json.loads(text)
    assert result["back_on_other_screens"] is value
    assert result["back_on_other_screens_source"] == source
    assert run_start(here)["back_on_other_screens"] is value


# --- usage errors -------------------------------------------------------------

BOTH = "--back-on-other-screens and --no-back-on-other-screens cannot be combined"


@pytest.mark.parametrize("press", [["--press"], []])
@pytest.mark.parametrize("order", [0, 1])
def test_both_options_are_refused_before_anything_happens(
    fake_adb, android, capsys, here, press, order
):
    write_file(here, back=False)
    both = ["--back-on-other-screens", "--no-back-on-other-screens"]
    code, _text, err = crawl(
        capsys, fake_adb, *press, *both[:: 1 - 2 * order], "--out", str(here / "run")
    )
    assert code == 2
    assert err == f"uiprobe: error: {BOTH}\n"
    assert fake_adb.requests == []
    assert android.inputs == []
    assert not (here / "run").exists()


@pytest.mark.parametrize(
    "option", ["--back-on-other-screens", "--no-back-on-other-screens"]
)
def test_both_options_are_press_only(fake_adb, android, capsys, here, option):
    write_file(here, back=False)
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


def test_a_file_that_says_true_does_not_change_the_preview(
    fake_adb, android, capsys, here
):
    write_file(here, back=False)
    _code, plain, _err = crawl(capsys, fake_adb)
    write_file(here, back=True)
    code, with_key, _err = crawl(capsys, fake_adb)
    assert code == 0
    assert with_key == plain
    assert android.inputs == []
