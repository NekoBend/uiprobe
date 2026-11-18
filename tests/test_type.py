"""Tests for ``Device.type_text`` and ``uiprobe type``.

Every device is a strict `FakeAndroid`: the command allowlist admits only
``input text`` with a quoted argument, so any other form fails the test.
"""

from __future__ import annotations

import json
import shlex
import string
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import El, FakeAndroid, Screen
from uiprobe import cli
from uiprobe.device import TYPE_PIECE_SIZE, Device, TextNotTypedError
from uiprobe.errors import AdbError

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SCREENS = (Screen("main", "com.example.shop/.Main", (El((0, 0, 320, 640)),)),)
pytestmark = pytest.mark.usefixtures("android")
PRINTABLE = "".join(chr(c) for c in range(0x20, 0x7F))


@pytest.fixture
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


def typed(fake_adb: FakeAdbServer, start: int = 0) -> list[str]:
    """The ``input text`` commands received after the first `start`."""
    return [c for _, _, c in fake_adb.commands[start:] if c.startswith("input")]


def pieces(fake_adb: FakeAdbServer) -> list[str]:
    """The text of each ``input text`` command, split back as a shell would."""
    out = []
    for command in typed(fake_adb):
        words = shlex.split(command)
        assert words[:2] == ["input", "text"]
        assert len(words) == 3
        out.append(words[2])
    return out


def invoke(
    capsys: pytest.CaptureFixture[str], fake_adb: FakeAdbServer, *argv: str
) -> tuple[int, str, str]:
    code = cli.run(["--adb-server", fake_adb.address, *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def requests(fake_adb: FakeAdbServer) -> list[str]:
    return [c for _, _, c in fake_adb.commands]


# --- Device.type_text ---


@pytest.mark.parametrize("char", PRINTABLE)
def test_every_printable_ascii_character_round_trips(device, fake_adb, char):
    assert device.type_text(char) is None
    assert pieces(fake_adb) == [char]


def test_special_characters_are_quoted(device, fake_adb):
    text = "' \" $ ` \\ ; & | ( ) < > { } ~ *?[]!#"
    device.type_text(text)
    assert pieces(fake_adb) == [text]
    assert typed(fake_adb)[0].startswith("input text '")


def test_the_whole_printable_range_in_one_command(device, fake_adb):
    device.type_text(PRINTABLE)
    assert pieces(fake_adb) == [PRINTABLE]


@pytest.mark.parametrize(
    "text",
    ["a>?@ABCDEFG", "a456789:;<=", "az{|}~", "a(", "a)", "a<", "a>", "a|"],
)
def test_measured_cases_are_sent_quoted_and_unchanged(device, fake_adb, text):
    device.type_text(text)
    assert typed(fake_adb) == [f"input text {shlex.quote(text)}"]
    assert typed(fake_adb)[0].startswith("input text '")
    assert pieces(fake_adb) == [text]


def test_a_single_quote_uses_the_usual_escape(device, fake_adb):
    device.type_text("it's")
    assert typed(fake_adb) == ["input text 'it'\"'\"'s'"]


def test_the_command_is_exactly_input_text_and_the_piece(device, fake_adb):
    device.type_text("hello world")
    device.type_text("hello")
    assert typed(fake_adb) == ["input text 'hello world'", "input text 'hello'"]
    assert not any("uiprobe-exit" in c for c in requests(fake_adb))


@pytest.mark.parametrize("text", ["%s", "a%sb", "%%s", "%s%s", "x%%%sy", "s%s%"])
def test_percent_s_is_never_inside_one_piece(device, fake_adb, text):
    device.type_text(text)
    parts = pieces(fake_adb)
    assert all("%s" not in p for p in parts)
    assert "".join(parts) == text
    assert len(parts) > 1


def test_percent_alone_is_one_piece(device, fake_adb):
    device.type_text("100%")
    device.type_text("%")
    assert pieces(fake_adb) == ["100%", "%"]


def test_percent_then_other_letters_stay_together(device, fake_adb):
    device.type_text("%a %S %5s")
    assert pieces(fake_adb) == ["%a %S %5s"]


def test_percent_s_split_is_between_the_two_characters(device, fake_adb):
    device.type_text("a%sb")
    assert pieces(fake_adb) == ["a%", "sb"]


def test_long_text_is_split_into_pieces(device, fake_adb):
    text = string.ascii_letters * 10
    assert len(text) > 2 * TYPE_PIECE_SIZE
    device.type_text(text)
    parts = pieces(fake_adb)
    assert TYPE_PIECE_SIZE == 256
    assert all(len(p) <= TYPE_PIECE_SIZE for p in parts)
    assert len(parts) == 3
    assert "".join(parts) == text


def test_text_of_exactly_one_piece_is_not_split(device, fake_adb):
    device.type_text("a" * TYPE_PIECE_SIZE)
    assert len(pieces(fake_adb)) == 1


def test_a_long_text_of_quotes_splits_the_raw_text(device, fake_adb):
    text = "'" * 300
    device.type_text(text)
    parts = pieces(fake_adb)
    assert parts == ["'" * 256, "'" * 44]
    assert max(len(c) for c in typed(fake_adb)) < 2000


def test_long_text_with_percent_s_at_the_boundary(device, fake_adb):
    text = "a" * 255 + "%s" + "b" * 10
    device.type_text(text)
    parts = pieces(fake_adb)
    assert all("%s" not in p for p in parts)
    assert "".join(parts) == text


def test_the_fake_records_the_typed_text(device, android):
    device.type_text("ab%scd")
    texts = [e.args[1] for e in android.inputs if e.kind == "text"]
    assert "".join(texts) == "ab%scd"


@pytest.mark.parametrize(
    ("text", "char", "position"),
    [
        ("café", "é", 3),
        ("テ", "テ", 0),
        ("a\U0001f600", "\U0001f600", 1),
        ("x\u202e", "\u202e", 1),
    ],
)
def test_non_ascii_is_refused_before_anything_is_sent(
    device, fake_adb, text, char, position
):
    before = len(fake_adb.commands)
    with pytest.raises(TextNotTypedError, match="not ASCII") as info:
        device.type_text(text)
    assert len(fake_adb.commands) == before
    error = info.value
    assert isinstance(error, TextNotTypedError)
    assert error.details == {
        "character": f"U+{ord(char):04X}",
        "escaped": ascii(char),
        "position": position,
    }


def test_the_non_ascii_message_is_exact(device):
    with pytest.raises(TextNotTypedError) as info:
        device.type_text("café")
    assert str(info.value) == (
        "character U+00E9 ('\\xe9') at position 3 is not ASCII; "
        "this version types ASCII text only"
    )


def test_the_first_offending_character_is_reported(device):
    with pytest.raises(TextNotTypedError) as info:
        device.type_text("abéè")
    assert info.value.details["position"] == 2


@pytest.mark.parametrize(
    "char", ["\n", "\t", "\r", "\x00", "\x01", "\x1b", "\x1f", "\x7f"]
)
def test_control_characters_are_refused_before_anything_is_sent(device, fake_adb, char):
    before = len(fake_adb.commands)
    with pytest.raises(TextNotTypedError, match="position 2") as info:
        device.type_text("ab" + char + "cd")
    assert len(fake_adb.commands) == before
    assert info.value.details["position"] == 2
    assert info.value.details["character"] == f"U+{ord(char):04X}"
    assert "not ASCII" not in str(info.value)


@pytest.mark.parametrize("char", ["\n", "\t"])
def test_newline_and_tab_point_to_the_enter_key(device, char):
    with pytest.raises(ValueError, match="uiprobe key enter"):
        device.type_text(char)


def test_the_other_control_message_does_not_mention_enter(device):
    with pytest.raises(ValueError, match="control character") as info:
        device.type_text("\x01")
    assert "enter" not in str(info.value)


def test_carriage_return_gets_the_control_message(device):
    with pytest.raises(ValueError, match="control character"):
        device.type_text("\r")


def test_empty_text_is_refused(device, fake_adb):
    before = len(fake_adb.commands)
    with pytest.raises(ValueError, match="empty"):
        device.type_text("")
    assert len(fake_adb.commands) == before


@pytest.mark.parametrize("value", [None, 5, b"abc", ["a"]])
def test_a_non_str_is_a_type_error(device, fake_adb, value):
    before = len(fake_adb.commands)
    with pytest.raises(TypeError):
        device.type_text(value)
    assert len(fake_adb.commands) == before


# --- uiprobe type ---


def test_cli_prints_the_count(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "type", "hello world")
    assert (code, out, err) == (0, "typed 11 characters\n", "")
    assert pieces(fake_adb) == ["hello world"]


def test_cli_json_has_length_and_not_the_text(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "type", "--json", "s3cret-Pass")
    assert code == 0
    assert err == ""
    assert "s3cret-Pass" not in out
    data = json.loads(out)
    assert data["schema"] == "uiprobe.action/1"
    assert data["action"] == "type"
    assert data["length"] == 11
    assert data["device"] == {"serial": SERIAL}
    assert isinstance(data["captured_at"], str)
    assert set(data) == {"schema", "action", "length", "device", "captured_at"}


def test_cli_text_output_never_shows_the_text(capsys, fake_adb):
    _, out, err = invoke(capsys, fake_adb, "type", "s3cret-Pass")
    assert "s3cret" not in out + err


def test_cli_accepts_a_text_after_double_dash(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "type", "--", "-5")
    assert code == 0
    assert out == "typed 2 characters\n"
    assert pieces(fake_adb) == ["-5"]


def test_cli_non_ascii_is_a_usage_error_with_no_request(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "type", "café")
    assert code == 2
    assert out == ""
    assert (
        "character U+00E9 ('\\xe9') at position 3 is not ASCII; "
        "this version types ASCII text only"
    ) in err
    assert requests(fake_adb) == []


def test_cli_non_ascii_json_error_has_details(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "type", "--json", "café")
    assert code == 2
    error = json.loads(out + err)["error"]
    assert error["code"] == "usage_error"
    assert error["details"] == {
        "character": "U+00E9",
        "escaped": "'\\xe9'",
        "position": 3,
    }
    assert requests(fake_adb) == []


@pytest.mark.parametrize("text", ["a\nb", "a\tb", "a\x01b", ""])
def test_cli_refuses_without_any_request(capsys, fake_adb, text):
    code, _, err = invoke(capsys, fake_adb, "type", text)
    assert code == 2
    assert err
    assert requests(fake_adb) == []


def test_cli_sends_long_text_in_pieces(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "type", "a" * 600)
    assert code == 0
    assert out == "typed 600 characters\n"
    assert [len(p) for p in pieces(fake_adb)] == [256, 256, 88]


# --- errors while sending never carry the text ---

TYPED_TEXT = "hunter2-Secret"


def test_a_dropped_connection_error_leaves_the_text_out(device, android):
    android.drop_after_input(1)
    with pytest.raises(AdbError) as info:
        device.type_text(TYPED_TEXT)
    dumped = json.dumps(info.value.to_dict()) + str(info.value)
    assert TYPED_TEXT not in dumped
    assert info.value.details["request"] == "shell:input text <text omitted>"
    assert info.value.__suppress_context__


def test_a_quoted_text_is_left_out_of_the_error_too(device, android):
    android.drop_after_input(1)
    with pytest.raises(AdbError) as info:
        device.type_text("it's-" + TYPED_TEXT)
    assert TYPED_TEXT not in json.dumps(info.value.to_dict())


def test_cli_json_error_after_a_dropped_connection_has_no_text(
    capsys, fake_adb, android
):
    android.drop_after_input(1)
    code, out, err = invoke(capsys, fake_adb, "--json", "type", TYPED_TEXT)
    assert code == 10
    assert TYPED_TEXT not in out + err
    assert "<text omitted>" in out


@pytest.mark.parametrize(("text", "drop"), [("ab%s", 2), ("e", 1)])
def test_a_short_piece_does_not_garble_the_error(device, android, text, drop):
    android.drop_after_input(drop)
    with pytest.raises(AdbError) as info:
        device.type_text(text)
    assert info.value.details["request"] == "shell:input text <text omitted>"
    assert info.value.message.count("<text omitted>") == 0
    assert "adb server" in info.value.message


def test_a_failure_after_the_first_piece_leaves_earlier_pieces_typed(device, android):
    android.drop_after_input(2)
    with pytest.raises(AdbError):
        device.type_text("a" * (TYPE_PIECE_SIZE + 44))
    assert len(android.inputs) == 2
