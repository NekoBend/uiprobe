"""``uiprobe crawl`` with ``[rules] also_target`` in the settings file: the
plan, the refusals (usage errors, exit 2), the notes and the run folder."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from tests.crawl_driver.helpers import SERIAL, SHOP, TRANSPORT
from tests.crawl_driver.test_also_target import HELPER, screens
from tests.fake_device import FakeAndroid
from uiprobe import cli
from uiprobe.also_target import HOME_COMMAND
from uiprobe.settings import SETTINGS_FILE, load_settings, render_default_settings

if TYPE_CHECKING:
    from pathlib import Path

    from tests.fake_adb_server import FakeAdbServer

FAST = ("--settle", "0")


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(
        fake_adb, SERIAL, screens(), start="home", transport_id=TRANSPORT
    )


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def settings_with(here: Path, listed: str | None) -> None:
    text = render_default_settings()
    if listed is not None:
        text = text.replace("[rules]\n", f"[rules]\nalso_target = {listed}\n", 1)
    (here / SETTINGS_FILE).write_text(text, encoding="utf-8", newline="\n")


def crawl(capsys, fake_adb, *argv: str) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, "crawl", SHOP, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def writes(fake_adb: FakeAdbServer) -> list[str]:
    return [
        r
        for r in fake_adb.requests
        if r.startswith(("shell:", "exec:"))
        and any(w in r for w in ("monkey", "am start", "force-stop", "input "))
    ]


def home_reads(fake_adb: FakeAdbServer) -> list[str]:
    return [r for r in fake_adb.requests if HOME_COMMAND in r]


def test_a_listed_package_is_pressed_and_named_in_the_plan_and_the_record(
    fake_adb, android, capsys, here
):
    settings_with(here, f'["{HELPER}"]')
    out = here / "run"
    code, text, err = crawl(capsys, fake_adb, "--press", *FAST, "--out", str(out))
    assert (code, err) == (0, "")
    assert (
        f"also pressing in: {HELPER} (also_target in the settings file; system UI,"
        " the home app and permission screens are never pressed)"
    ) in text.splitlines()
    assert [i for i in android.inputs if i.kind == "tap" and i.screen == "helper"]
    start = json.loads((out / "run-start.json").read_text("utf-8"))
    assert start["also_target"] == [HELPER]
    assert len(home_reads(fake_adb)) == 1


@pytest.mark.usefixtures("android")
def test_the_json_plan_has_the_list_only_when_there_is_one(fake_adb, capsys, here):
    settings_with(here, f'["{HELPER}"]')
    code, text, _err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "a"), "--json"
    )
    assert code == 0
    assert json.loads(text)["also_target"] == [HELPER]
    settings_with(here, None)
    code, text, _err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "b"), "--json"
    )
    assert code == 0
    assert "also_target" not in json.loads(text)
    assert "also pressing in" not in text


@pytest.mark.usefixtures("android")
def test_an_empty_list_sends_and_reads_nothing_extra(fake_adb, capsys, here):
    settings_with(here, "[]")
    code, text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "r")
    )
    assert (code, err) == (0, "")
    assert "also pressing in" not in text
    assert home_reads(fake_adb) == []
    start = json.loads((here / "r" / "run-start.json").read_text("utf-8"))
    assert "also_target" not in start


@pytest.mark.parametrize(
    ("listed", "reason"),
    [
        (
            "com.android.systemui",
            "it is the system UI (lock screen, notification shade)",
        ),
        ("com.android.launcher3", "it is a home app (launcher)"),
        ("com.google.android.apps.nexuslauncher", "it is a home app (launcher)"),
        ("com.sec.android.app.launcher", "it is a home app (launcher)"),
    ],
)
@pytest.mark.usefixtures("android")
def test_the_refusals_are_usage_errors_before_the_device_is_touched(
    fake_adb, capsys, here, listed, reason
):
    settings_with(here, f'["{HELPER}", "{listed}"]')
    out = here / "run"
    code, text, err = crawl(capsys, fake_adb, "--press", *FAST, "--out", str(out))
    assert code == 2
    assert text == ""
    assert f"also_target cannot list {listed}: {reason}; nothing was pressed" in err
    assert SETTINGS_FILE in err
    assert "[rules]" in err
    assert fake_adb.requests == []  # not even a device was asked
    assert not out.exists()


@pytest.mark.usefixtures("android")
def test_the_target_itself_is_a_settings_refusal(fake_adb, capsys, here):
    settings_with(here, f'["{SHOP}"]')
    code, text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "r")
    )
    assert code == 2
    assert text == ""
    assert (
        f"[rules] also_target lists {SHOP}, which is the target app itself;"
        " remove it from the list"
    ) in err
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_a_duplicate_and_a_bad_name_are_settings_errors(fake_adb, capsys, here):
    for listed, needle in (
        (f'["{HELPER}", "{HELPER}"]', "duplicate entry"),
        ('["nope"]', "not a valid package name"),
    ):
        settings_with(here, listed)
        code, _text, err = crawl(capsys, fake_adb, "--press", *FAST, "--out", "r")
        assert code == 2
        assert needle in err
        assert "[rules] also_target" in err
    assert fake_adb.requests == []


def test_the_devices_home_app_is_refused_with_a_usage_error(
    fake_adb, android, capsys, here
):
    android.home = "com.vendor.home"
    settings_with(here, '["com.vendor.home"]')
    out = here / "run"
    code, _text, err = crawl(capsys, fake_adb, "--press", *FAST, "--out", str(out))
    assert code == 2
    assert (
        "also_target cannot list com.vendor.home: it is the home app of this"
        " device; nothing was pressed"
    ) in err
    assert writes(fake_adb) == []
    assert not out.exists()
    assert len(home_reads(fake_adb)) == 1


def test_a_home_read_that_fails_is_a_note_after_the_run(
    fake_adb, android, capsys, here
):
    android.home = None
    settings_with(here, f'["{HELPER}"]')
    code, text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "r"), "--json"
    )
    assert code == 0  # the plan is on stderr with --json
    assert "also pressing in" in err
    notes = json.loads(text)["notes"]
    assert any("home app of this device could not be read" in n for n in notes)


def test_a_package_that_is_not_installed_is_a_usage_error(
    fake_adb, android, capsys, here
):
    android.not_installed = frozenset({HELPER})
    settings_with(here, f'["{HELPER}"]')
    out = here / "run"
    code, _text, err = crawl(capsys, fake_adb, "--press", *FAST, "--out", str(out))
    assert code == 2
    assert f"also_target lists {HELPER}, which is not installed" in err
    assert writes(fake_adb) == []
    assert not out.exists()


def test_the_refusal_is_shown_with_record_text_off_too(fake_adb, android, capsys, here):
    android.not_installed = frozenset({HELPER})
    settings_with(here, f'["{HELPER}"]')
    code, _text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(here / "r"), "--no-record-text"
    )
    assert code == 2
    assert "not installed" in err
    assert "unexpected" not in err


def test_a_dry_run_judges_the_listed_package_and_refuses_the_same_names(
    fake_adb, android, capsys, here
):
    android.show("main")
    android.show("helper")
    settings_with(here, f'["{HELPER}"]')
    code, text, err = crawl(capsys, fake_adb, "--dry-run")
    assert code == 0, err
    assert "outside_target_app" not in text
    assert "Settings" in text
    assert len(home_reads(fake_adb)) == 1
    settings_with(here, '["com.android.systemui"]')
    code, _text, err = crawl(capsys, fake_adb, "--dry-run")
    assert code == 2
    assert "system UI" in err


def test_a_dry_run_without_a_list_reads_no_home_app(fake_adb, android, capsys, here):
    android.show("main")
    settings_with(here, None)
    code, _text, err = crawl(capsys, fake_adb, "--dry-run")
    assert code == 0, err
    assert home_reads(fake_adb) == []


def test_init_writes_the_entry_commented_and_the_file_loads(
    tmp_path, capsys, fake_adb, monkeypatch
):
    monkeypatch.setenv("ADB_SERVER_SOCKET", fake_adb.address)
    out = tmp_path / SETTINGS_FILE
    code = cli.run(["init", str(out)])
    assert (code, capsys.readouterr().err) == (0, "")
    text = out.read_text("utf-8")
    assert (
        '# also_target = ["com.example.helper"]   # apps the crawl may also press in;'
        " system UI and the home app are refused, permission screens are never"
        " pressed\n"
    ) in text
    assert load_settings(out).also_target == ()
    assert fake_adb.requests == []
