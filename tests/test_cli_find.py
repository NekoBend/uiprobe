"""Tests for ``uiprobe find`` and the JSON-mode detection of the command line.

The device is a strict `FakeAndroid`: any command beyond the ones a dump needs
fails the test, so nothing is ever pressed or typed.
"""

from __future__ import annotations

import io
import json
import sys
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import cli

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
SCREEN_ID_PREFIX = "s-"

ROWS = tuple(
    El(
        (0, 10 * n, 320, 10 * n + 9),
        text=f"Row {n}",
        rid=f"{SHOP}:id/row",
        go="main",
    )
    for n in range(25)
)

SCREENS = (
    Screen(
        "main",
        f"{SHOP}/.Main",
        (
            El((10, 10, 150, 60), text="Login", rid=f"{SHOP}:id/login", go="main"),
            El((10, 70, 150, 120), text="\nLogin help", desc="help"),
            El((11, 130, 22, 151), text="  Odd\n", desc="odd"),
            El((10, 200, 100, 250), text="Twin", rid=f"{SHOP}:id/twin"),
            El((120, 200, 210, 250), text="Twin", rid=f"{SHOP}:id/twin"),
            El((203, 300, 80, 350), text="Inverted sideways"),
            El((80, 623, 203, 616), text="Inverted"),
            El((0, 700, 100, 760), text="Below the display"),
            El((300, 560, 400, 700), text="Cut", desc="cut"),
            El((10, 300, 100, 300), text="Flat"),
        ),
    ),
    Screen("rows", f"{SHOP}/.Rows", ROWS),
)


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")


@pytest.fixture
def rows(android: FakeAndroid) -> FakeAndroid:
    android.show("rows")
    return android


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def find_json(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, dict[str, Any]]:
    code, out, err = invoke(capsys, fake_adb, "find", "--json", *argv)
    assert err == ""
    return code, json.loads(out)


@pytest.mark.usefixtures("android")
def test_find_json_shape(fake_adb, capsys):
    code, payload = find_json(capsys, fake_adb, "--id", "login")
    assert code == 0
    assert list(payload) == ["schema", "screen_id", "criteria", "total", "elements"]
    assert payload["schema"] == "uiprobe.find/1"
    assert str(payload["screen_id"]).startswith(SCREEN_ID_PREFIX)
    assert payload["criteria"] == {"resource_id": "login"}
    assert payload["total"] == 1
    element = payload["elements"][0]
    assert element["center"] == [80, 35]
    assert element["bounds"] == [10, 10, 150, 60]
    assert "hint" not in element
    assert list(element)[-1] == "center"


@pytest.mark.usefixtures("android")
def test_find_json_screen_id_matches_dump(fake_adb, capsys):
    _, payload = find_json(capsys, fake_adb, "--text", "Login")
    code, out, _ = invoke(capsys, fake_adb, "dump", "--json")
    assert code == 0
    assert payload["screen_id"] == json.loads(out)["screen_id"]


@pytest.mark.usefixtures("android")
def test_find_criteria_use_the_json_names(fake_adb, capsys):
    _, payload = find_json(
        capsys, fake_adb, "--text", "Login help", "--id", "x", "--desc", "help"
    )
    assert payload["criteria"] == {
        "text": "Login help",
        "resource_id": "x",
        "content_desc": "help",
    }


@pytest.mark.usefixtures("android")
def test_find_text_is_stripped_on_both_sides(fake_adb, capsys):
    _, payload = find_json(capsys, fake_adb, "--text", "Login help")
    assert payload["total"] == 1
    _, payload = find_json(capsys, fake_adb, "--text", "\tOdd ")
    assert payload["total"] == 1


@pytest.mark.usefixtures("android")
def test_find_combines_criteria_with_and(fake_adb, capsys):
    _, payload = find_json(capsys, fake_adb, "--text", "Twin")
    assert payload["total"] == 2
    _, payload = find_json(capsys, fake_adb, "--text", "Twin", "--id", "twin")
    assert payload["total"] == 2
    _, payload = find_json(capsys, fake_adb, "--text", "Twin", "--desc", "help")
    assert payload["total"] == 0
    _, payload = find_json(capsys, fake_adb, "--text", "Login", "--desc", "help")
    assert payload["total"] == 0


@pytest.mark.usefixtures("android")
def test_find_accepts_the_full_resource_id(fake_adb, capsys):
    _, payload = find_json(capsys, fake_adb, "--id", f"{SHOP}:id/login")
    assert payload["total"] == 1


@pytest.mark.parametrize(
    "text",
    ["Inverted sideways", "Inverted", "Below the display", "Flat"],
)
@pytest.mark.usefixtures("android")
def test_find_does_not_count_invisible_elements(fake_adb, capsys, text):
    code, payload = find_json(capsys, fake_adb, "--text", text)
    assert code == 0
    assert payload["total"] == 0
    assert payload["elements"] == []


def test_the_fake_dump_carries_the_inverted_bounds_seen_on_api_36(android):
    assert b"[80,623][203,616]" in android.render_xml()


@pytest.mark.usefixtures("android")
def test_find_center_is_that_of_the_visible_part(fake_adb, capsys):
    _, payload = find_json(capsys, fake_adb, "--desc", "cut")
    # bounds [300,560][400,700] on a 320x640 display: visible [300,560][320,640]
    assert payload["elements"][0]["bounds"] == [300, 560, 400, 700]
    assert payload["elements"][0]["center"] == [310, 600]
    _, payload = find_json(capsys, fake_adb, "--desc", "odd")
    assert payload["elements"][0]["center"] == [16, 140]  # floor of 16.5 and 140.5


@pytest.mark.usefixtures("android")
def test_find_with_no_match_exits_zero_with_an_empty_list(fake_adb, capsys):
    code, payload = find_json(capsys, fake_adb, "--text", "nothing like it")
    assert code == 0
    assert payload["total"] == 0
    assert payload["elements"] == []
    assert payload["criteria"] == {"text": "nothing like it"}


@pytest.mark.usefixtures("rows")
def test_find_lists_at_most_twenty_and_reports_the_total(fake_adb, capsys):
    code, payload = find_json(capsys, fake_adb, "--id", "row")
    assert code == 0
    assert payload["total"] == 25
    elements = payload["elements"]
    assert len(elements) == 20
    assert [e["text"] for e in elements] == [f"Row {n}" for n in range(20)]
    assert elements[0]["center"] == [160, 4]


@pytest.mark.usefixtures("android")
def test_find_text_output(fake_adb, capsys):
    code, out, err = invoke(capsys, fake_adb, "find", "--id", "login")
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[0].startswith("1 element matches  (s-")
    assert lines[1:] == ['e2 TextView "Login" id=login [tap] center=80,35']


@pytest.mark.usefixtures("android")
def test_find_text_output_for_several_and_none(fake_adb, capsys):
    code, out, _ = invoke(capsys, fake_adb, "find", "--text", "Twin")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("2 elements match  (s-")
    assert lines[1:] == [
        'e5 TextView "Twin" id=twin center=55,225',
        'e6 TextView "Twin" id=twin center=165,225',
    ]
    code, out, _ = invoke(capsys, fake_adb, "find", "--text", "zzz")
    assert code == 0
    assert out.splitlines()[0].startswith("0 elements match  (s-")
    assert len(out.splitlines()) == 1


@pytest.mark.usefixtures("rows")
def test_find_text_output_says_how_many_are_not_shown(fake_adb, capsys):
    code, out, _ = invoke(capsys, fake_adb, "find", "--id", "row")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("25 elements match")
    assert len(lines) == 1 + 20 + 1
    assert lines[-1] == "... 5 more not shown"


def test_find_escapes_terminal_controls_in_text_output(fake_adb, capsys):
    FakeAndroid(
        fake_adb,
        SERIAL,
        [Screen("main", f"{SHOP}/.Main", (El((0, 0, 50, 50), text="a\u202eb"),))],
        start="main",
    )
    code, out, _ = invoke(capsys, fake_adb, "find", "--text", "a\u202eb")
    assert code == 0
    assert "\u202e" not in out
    assert "\\u202e" in out


def test_find_needs_a_criterion_and_does_not_touch_the_device(fake_adb, capsys):
    code, out, err = invoke(capsys, fake_adb, "find")
    assert (code, out) == (2, "")
    assert "at least one" in err
    code, out, _ = invoke(capsys, fake_adb, "find", "--json")
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert fake_adb.requests == []


@pytest.mark.usefixtures("android")
def test_find_options_are_not_abbreviated(fake_adb, capsys):
    code, _, _ = invoke(capsys, fake_adb, "find", "--tex", "x")
    assert code == 2


@pytest.mark.usefixtures("android")
def test_find_serial_and_common_options_work_before_or_after(fake_adb, capsys):
    code, out, _ = invoke(capsys, fake_adb, "-s", SERIAL, "find", "--id", "login")
    assert code == 0
    assert out.startswith("1 element matches")
    code, _, _ = invoke(capsys, fake_adb, "find", "--id", "login", "-s", SERIAL)
    assert code == 0


@pytest.mark.usefixtures("android")
def test_find_with_an_unknown_serial_fails_like_dump(fake_adb, capsys):
    code, out, _ = invoke(capsys, fake_adb, "find", "--json", "-s", "nope", "--id", "a")
    assert code == 4
    assert json.loads(out)["error"]["code"] == "device_not_found"


def test_find_sends_only_what_a_dump_needs(android, fake_adb, capsys):
    code, _, _ = invoke(capsys, fake_adb, "find", "--text", "Login")
    assert code == 0
    assert fake_adb.unexpected == []
    assert android.inputs == []


# --- JSON mode and the "--" separator ---


def test_options_after_double_dash_do_not_turn_on_json(fake_adb, capsys):
    code, out, err = invoke(capsys, fake_adb, "dump", "--", "--json")
    assert (code, out) == (2, "")
    assert err.startswith("uiprobe: error: ")


def test_options_before_double_dash_still_turn_on_json(fake_adb, capsys):
    code, out, err = invoke(capsys, fake_adb, "--json", "dump", "--", "x")
    assert (code, err) == (2, "")
    assert json.loads(out)["error"]["code"] == "usage_error"
    code, out, err = invoke(capsys, fake_adb, "dump", "--json", "--", "x")
    assert (code, err) == (2, "")
    assert json.loads(out)["error"]["code"] == "usage_error"


def test_find_value_after_double_dash_is_not_an_option(fake_adb, capsys):
    code, out, err = invoke(capsys, fake_adb, "find", "--text", "x", "--", "--json")
    assert (code, out) == (2, "")
    assert err.startswith("uiprobe: error: ")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["dump", "--json"], True),
        (["--json", "dump"], True),
        (["dump"], False),
        (["dump", "--", "--json"], False),
        (["dump", "--json", "--", "--json"], True),
        (["--", "--json"], False),
        (["find", "--text=--json"], False),
        ([], False),
    ],
)
def test_json_requested_stops_at_double_dash(argv, expected):
    assert cli._json_requested(argv) is expected


class _Stream(io.TextIOWrapper):
    """A text stream that records the `reconfigure` calls instead of acting."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        super().__init__(io.BytesIO(), encoding="cp1252")
        self.calls = calls

    def reconfigure(self, **kwargs: object) -> None:  # type: ignore[override]
        self.calls.append(kwargs)


def test_stream_configuration_ignores_json_after_double_dash(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(sys, "stdout", _Stream(calls))
    monkeypatch.setattr(sys, "stderr", _Stream(calls))
    cli._configure_streams(["dump", "--", "--json"])
    assert calls
    assert all("encoding" not in call for call in calls)
    calls.clear()
    cli._configure_streams(["dump", "--json", "--", "x"])
    assert {"encoding": "utf-8", "errors": "backslashreplace"} in calls
