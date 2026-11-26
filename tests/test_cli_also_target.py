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
    code, text, err = crawl(
        capsys, fake_adb, "--press", *FAST, "--quiet", "--out", str(out)
    )
    assert (code, err) == (0, "")
    assert (
        f"also pressing in: {HELPER} (also_target in the settings file; system UI"
        " and the home app are refused; the controls of permission screens are"
        " never tapped, and they get BACK when BACK on other screens is on)"
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
        capsys, fake_adb, "--press", *FAST, "--quiet", "--out", str(here / "r")
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


def test_a_home_read_that_fails_is_a_usage_error_before_anything_is_pressed(
    fake_adb, android, capsys, here
):
    android.home = None
    settings_with(here, f'["{HELPER}"]')
    out = here / "r"
    code, _text, err = crawl(capsys, fake_adb, "--press", *FAST, "--out", str(out))
    assert code == 2
    assert (
        "also_target: could not read this device's home app, which also_target"
        " needs in order to refuse it; nothing was pressed"
    ) in err
    assert writes(fake_adb) == []
    assert not out.exists()
    code, _text, err = crawl(capsys, fake_adb, "--dry-run")
    assert code == 2
    assert "could not read this device's home app" in err


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
        " system UI and the home app are refused, the controls of permission"
        " screens are never tapped (they get BACK when back_on_other_screens"
        " is true)\n"
    ) in text
    assert load_settings(out).also_target == ()
    assert fake_adb.requests == []


# -- end to end: pressing in a listed app on the fake device --------------------------

E2E_OTHER = "com.example.other"
E2E_TREE = """\
com.example.shop (com.example.shop)  also target: com.example.helper
├─ Open helper
│  ├─ Settings
│  │  └─ Advanced
│  │     └─ (screen with no named rows)
│  ├─ Delete everything ·
│  ├─ About → see: Advanced
│  └─ Open other → com.example.other
├─ Visit other → com.example.other
├─ Camera → com.google.android.permissioncontroller
└─ Notes ·
· = not pressed, so nothing below it was looked at
→ PACKAGE = pressing it opened that app
→ see: X = it led to a screen already shown under X
(screen with no named rows) = the screen it led to has no row with a name (see --json)
left out of the tree (see --json): rows not sent: 1, texts outside rows: 4
"""


def e2e_screens():
    from tests.crawl_driver.helpers import button
    from tests.fake_device import El, Screen, permission_dialog

    return (
        Screen("home", "com.example.launcher/.Home", (El((0, 0, 320, 640)),)),
        Screen(
            "main",
            f"{SHOP}/.Main",
            (
                El((0, 0, 320, 100), text="Shop"),
                button(200, "Open helper", "helper"),
                button(260, "Visit other", "ext"),
                button(320, "Camera", "permission"),
                button(380, "Notes", "notes"),
            ),
        ),
        Screen("notes", f"{SHOP}/.Notes", (El((0, 0, 320, 100), text="Notes"),)),
        Screen(
            "helper",
            f"{HELPER}/.Main",
            (
                El((0, 0, 320, 100), text="Helper"),
                button(200, "Settings", "helper2"),
                button(260, "Delete everything", "helper"),
                button(320, "About", "helper3"),
                button(380, "Open other", "ext"),
            ),
        ),
        Screen(
            "helper2",
            f"{HELPER}/.Two",
            (El((0, 0, 320, 100), text="Two"), button(200, "Advanced", "helper3")),
        ),
        Screen("helper3", f"{HELPER}/.Three", (El((0, 0, 320, 100), text="About"),)),
        Screen("ext", f"{E2E_OTHER}/.One", (El((0, 0, 320, 640)),)),
        permission_dialog(),
    )


def test_end_to_end_pressing_in_a_listed_app_on_the_fake_device(fake_adb, capsys, here):
    android = FakeAndroid(
        fake_adb, SERIAL, e2e_screens(), start="home", transport_id=TRANSPORT
    )
    # a window of the listed app floats over the shop's "Notes" row
    android.overlays.append((f"{HELPER}/.Float", HELPER, (20, 380, 300, 420)))
    settings_with(here, f'["{HELPER}"]')
    out = here / "run"
    code, text, _err = crawl(
        capsys, fake_adb, "--press", *FAST, "--out", str(out), "--actions", "60"
    )
    assert code == 0, text
    taps = [(i.screen, i.point) for i in android.inputs if i.kind == "tap"]
    pressed = {s for s, _ in taps}
    # the listed app's rows are pressed
    assert {"helper", "helper2"} <= pressed
    assert ("helper", (160, 220)) in taps
    assert ("helper", (160, 340)) in taps
    # a deny word in the listed app is refused as in the shop
    assert ("helper", (160, 280)) not in taps
    # the unlisted app and the permission dialog get no tap and no BACK
    assert not [i for i in android.inputs if i.screen in {"ext", "permission"}]
    # the listed app's window over the shop's row: refused, nothing pressed there
    assert ("main", (160, 400)) not in taps
    assert "notes" not in pressed
    ends = [
        json.loads(line)
        for line in (out / "transitions.jsonl").read_text("utf-8").splitlines()
    ]
    assert any(r.get("reason") == "covered_by_other" for r in ends)
    starts = [r for r in ends if "action" in r]
    kinds = [r["action"]["kind"] for r in starts]
    assert "relaunch" in kinds  # the unlisted app and the dialog: relaunch
    named = {r["off_target"]["package"] for r in starts if "off_target" in r}
    assert named == {E2E_OTHER, "com.google.android.permissioncontroller"}
    # the listed app is never launched or stopped
    assert set(android.stopped) == {SHOP}
    assert not [r for r in fake_adb.requests if "am " in r and HELPER in r]
    # the home app was read once, and the run folder lists the package
    assert len(home_reads(fake_adb)) == 1
    start = json.loads((out / "run-start.json").read_text("utf-8"))
    assert start["also_target"] == [HELPER]
    # the tree of the run, as the pull request shows it
    code = cli.run(["tree", str(out)])
    tree = capsys.readouterr().out
    assert code == 0
    assert tree == E2E_TREE


def test_end_to_end_with_back_on_other_screens_the_unlisted_app_gets_back(
    fake_adb, capsys, here
):
    android = FakeAndroid(
        fake_adb, SERIAL, e2e_screens(), start="home", transport_id=TRANSPORT
    )
    settings_with(here, f'["{HELPER}"]')
    out = here / "run"
    code, _text, _err = crawl(
        capsys,
        fake_adb,
        "--press",
        *FAST,
        "--back-on-other-screens",
        "--out",
        str(out),
        "--actions",
        "60",
    )
    assert code == 0
    keys = [i for i in android.inputs if i.kind == "key"]
    assert any(i.screen == "ext" for i in keys)  # BACK, as before the setting
    assert not [i for i in android.inputs if i.kind == "tap" and i.screen == "ext"]
