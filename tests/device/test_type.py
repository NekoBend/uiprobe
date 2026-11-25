"""Tests for ``Device.type_text`` and ``uiprobe type``.

Every device is a strict `FakeAndroid`: the command allowlist admits only
``input text`` with a quoted argument and the exit status marker, so any
other form fails the test.
"""

from __future__ import annotations

import json
import re
import shlex
import string
from typing import TYPE_CHECKING

import pytest

from tests.fake_adb_server import Fail
from tests.fake_device import INPUT_MARKER_SUFFIX, El, FakeAndroid, Screen
from uiprobe import cli
from uiprobe.device import TYPE_PIECE_SIZE, Device, TextNotTypedError
from uiprobe.errors import AdbError, InputFailedError, UiprobeError

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
        assert command.endswith(INPUT_MARKER_SUFFIX)
        words = shlex.split(command.removesuffix(INPUT_MARKER_SUFFIX))
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
    assert typed(fake_adb) == [f"input text {shlex.quote(text)}{INPUT_MARKER_SUFFIX}"]
    assert typed(fake_adb)[0].startswith("input text '")
    assert pieces(fake_adb) == [text]


def test_a_single_quote_uses_the_usual_escape(device, fake_adb):
    device.type_text("it's")
    assert typed(fake_adb) == ["input text 'it'\"'\"'s'" + INPUT_MARKER_SUFFIX]


def test_the_command_is_exactly_input_text_and_the_piece(device, fake_adb):
    device.type_text("hello world")
    device.type_text("hello")
    assert typed(fake_adb) == [
        "input text 'hello world'; echo uiprobe-exit:$?",
        "input text 'hello'; echo uiprobe-exit:$?",
    ]


@pytest.mark.parametrize("text", ["%s", "a%sb", "%%s", "%s%s", "x%%%sy", "s%s%"])
def test_percent_s_is_never_inside_one_piece(device, fake_adb, text):
    device.type_text(text)
    parts = pieces(fake_adb)
    assert all("%s" not in p for p in parts)
    assert all(parts)  # no empty piece
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
    before = list(fake_adb.requests)
    with pytest.raises(TextNotTypedError, match="not ASCII") as info:
        device.type_text(text)
    assert fake_adb.requests == before
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
    before = list(fake_adb.requests)
    with pytest.raises(TextNotTypedError, match="position 2") as info:
        device.type_text("ab" + char + "cd")
    assert fake_adb.requests == before
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
    before = list(fake_adb.requests)
    with pytest.raises(ValueError, match="empty"):
        device.type_text("")
    assert fake_adb.requests == before


@pytest.mark.parametrize("value", [None, 5, b"abc", ["a"]])
def test_a_non_str_is_a_type_error(device, fake_adb, value):
    before = list(fake_adb.requests)
    with pytest.raises(TypeError):
        device.type_text(value)
    assert fake_adb.requests == before


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
    assert fake_adb.requests == []


def test_cli_non_ascii_json_error_has_details(capsys, fake_adb):
    code, out, err = invoke(capsys, fake_adb, "type", "--json", "café")
    assert (code, err) == (2, "")
    error = json.loads(out)["error"]
    assert error["code"] == "usage_error"
    assert error["details"] == {
        "character": "U+00E9",
        "escaped": "'\\xe9'",
        "position": 3,
    }
    assert fake_adb.requests == []


@pytest.mark.parametrize(
    "text", ["a\nb", "a\tb", "a\x01b", "", "a" * 300 + "\xe9", "a" * 256 + "\n"]
)
def test_cli_refuses_without_any_request(capsys, fake_adb, text):
    code, _, err = invoke(capsys, fake_adb, "type", text)
    assert code == 2
    assert err
    assert fake_adb.requests == []  # not even the device list


def test_cli_sends_long_text_in_pieces(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "type", "a" * 600)
    assert code == 0
    assert out == "typed 600 characters\n"
    assert [len(p) for p in pieces(fake_adb)] == [256, 256, 88]


# --- errors while sending never carry the text ---

TYPED_TEXT = "hunter2-Secret"


def _no_text_anywhere(error: UiprobeError, text: str = TYPED_TEXT) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    dumped = json.dumps(error.to_dict()) + str(error) + repr(error.args)
    assert text not in dumped


def test_a_dropped_connection_error_leaves_the_text_out(device, android):
    android.drop_after_input(1)
    with pytest.raises(AdbError) as info:
        device.type_text(TYPED_TEXT)
    _no_text_anywhere(info.value)
    assert info.value.details == {
        "command": "type",
        "request": "shell:input text <text omitted>",
        "piece": 1,
        "pieces": 1,
        "pieces_done": 0,
    }
    assert info.value.message.startswith("typing stopped at piece 1 of 1")


def test_a_quoted_text_is_left_out_of_the_error_too(device, android):
    android.drop_after_input(1)
    with pytest.raises(AdbError) as info:
        device.type_text("it's-" + TYPED_TEXT)
    _no_text_anywhere(info.value)


@pytest.mark.parametrize(
    "echo",
    [
        TYPED_TEXT,
        f"'{TYPED_TEXT}'",
        f"input text {TYPED_TEXT}",
        "error: " + TYPED_TEXT[:7],
    ],
)
def test_an_adb_failure_that_repeats_the_text_is_not_shown(device, android, echo):
    android.override(re.compile(r"^input text "), Fail(echo))
    with pytest.raises(AdbError) as info:
        device.type_text(TYPED_TEXT)
    _no_text_anywhere(info.value, TYPED_TEXT[:7])
    assert "the adb request failed" in info.value.message


def test_cli_json_error_after_a_dropped_connection_has_no_text(
    capsys, fake_adb, android
):
    android.drop_after_input(1)
    code, out, err = invoke(capsys, fake_adb, "--json", "type", TYPED_TEXT)
    assert code == 10
    assert TYPED_TEXT not in out + err
    assert json.loads(out)["error"]["details"]["pieces_done"] == 0


def test_a_failure_after_the_first_piece_names_the_pieces(device, android):
    text = "a" * TYPE_PIECE_SIZE + "b" * TYPE_PIECE_SIZE + "c" * 10
    android.drop_after_input(2)
    with pytest.raises(AdbError) as info:
        device.type_text(text)
    sent = [e.args[1] for e in android.inputs if e.kind == "text"]
    assert sent == ["a" * TYPE_PIECE_SIZE, "b" * TYPE_PIECE_SIZE]  # c never sent
    details = info.value.details
    assert (details["piece"], details["pieces"], details["pieces_done"]) == (2, 3, 1)
    assert "1 earlier piece was reported typed" in info.value.message


# --- the device's answer to each piece ---


@pytest.mark.parametrize(
    ("answer", "reason", "extra"),
    [
        ({"status": 1}, "exit_status", {}),
        ({"status": 1000}, "exit_status", {}),
        ({"output": "input: permission denied\n"}, "unexpected_output", {}),
        ({"output": TYPED_TEXT + "\n"}, "unexpected_output", {}),
        (
            {"status": 255, "output": TYPED_TEXT + "\n"},
            "exit_status",
            {},
        ),
        ({"marker": False}, "no_exit_status", {}),
    ],
)
def test_a_piece_the_device_did_not_take_stops_typing(
    device, android, answer, reason, extra
):
    android.answer_input(**answer)
    with pytest.raises(InputFailedError) as info:
        device.type_text("a" * TYPE_PIECE_SIZE + TYPED_TEXT)
    _no_text_anywhere(info.value)
    assert info.value.details == {
        "command": "type",
        "reason": reason,
        "piece": 1,
        "pieces": 2,
        "pieces_done": 0,
        **extra,
    }
    assert len([e for e in android.inputs if e.kind == "text"]) == 1


def test_cli_device_failure_exits_14_without_the_text(capsys, fake_adb, android):
    android.answer_input(output="input: " + TYPED_TEXT + "\n")
    code, out, err = invoke(capsys, fake_adb, "--json", "type", TYPED_TEXT)
    assert (code, err) == (14, "")
    assert TYPED_TEXT not in out
    assert json.loads(out)["error"]["code"] == "input_failed"


# --- one plain str from the check to the device ---


class _TwoFaced(str):
    """Text whose iteration, slicing and splitting show other characters."""

    __slots__ = ()

    def __iter__(self):
        return iter("x'; reboot; '")

    def __getitem__(self, key):
        return "'"

    def split(self, *_args, **_kwargs):
        return ["'; reboot; '"]

    def __add__(self, other):
        return "'; reboot; '"

    def __len__(self) -> int:
        return 3


def test_a_str_subclass_is_read_once_as_plain_text(device, fake_adb):
    device.type_text(_TwoFaced("hello"))
    assert typed(fake_adb) == ["input text 'hello'; echo uiprobe-exit:$?"]


class _HidesItsText(str):
    """Text that shows only ASCII when iterated, but holds a non-ASCII one."""

    __slots__ = ()

    def __iter__(self):
        return iter("cafe")


def test_the_check_reads_the_plain_text_of_a_subclass(device, fake_adb):
    before = list(fake_adb.requests)
    with pytest.raises(TextNotTypedError, match="U\\+00E9"):
        device.type_text(_HidesItsText("café"))
    assert fake_adb.requests == before


# --- arguments argparse cannot place ---


@pytest.mark.parametrize(
    "argv",
    [
        ["type", "one", "two-" + TYPED_TEXT],
        ["type", "-Q" + TYPED_TEXT],
        ["type", "--" + TYPED_TEXT],
        ["--json", "type", "one", TYPED_TEXT],
        ["type", "--json", "-Q" + TYPED_TEXT],
    ],
)
def test_argument_errors_of_type_never_show_the_arguments(capsys, fake_adb, argv):
    code, out, err = invoke(capsys, fake_adb, *argv)
    assert code == 2
    assert TYPED_TEXT not in out + err
    assert "give TEXT as one quoted argument" in out + err
    if "--json" in argv:
        assert err == ""
        assert json.loads(out)["error"]["code"] == "usage_error"
    else:
        assert out == ""
    assert fake_adb.requests == []


def test_other_commands_keep_their_argument_errors(capsys, fake_adb):
    code, _, err = invoke(capsys, fake_adb, "key", "enter", "extra")
    assert code == 2
    assert "unrecognized arguments: extra" in err


def test_the_exit_status_is_never_shown(device, android):
    android.answer_input(status=4242)
    with pytest.raises(InputFailedError) as info:
        device.type_text("4242")
    _no_text_anywhere(info.value, "4242")


# --- device options after type are refused ---


SECRET_WORD = "Word-" + TYPED_TEXT


@pytest.mark.parametrize(
    "words",
    [
        ["-s", SECRET_WORD],
        ["-s" + SECRET_WORD],
        ["--serial", SECRET_WORD],
        ["--serial=" + SECRET_WORD],
        ["--ser", SECRET_WORD],
        ["--adb", SECRET_WORD],
        ["--adb=" + SECRET_WORD],
        ["--ad", SECRET_WORD],
        ["--adb-server", SECRET_WORD],
        ["--adb-server=" + SECRET_WORD],
        ["--adb-s", SECRET_WORD],
    ],
)
def test_a_device_option_after_type_is_refused(capsys, fake_adb, words):
    for json_flag in ([], ["--json"]):
        code, out, err = invoke(capsys, fake_adb, *json_flag, "type", "first", *words)
        assert code == 2
        assert TYPED_TEXT not in out + err
        assert "go before the type command" in out + err
        assert "uiprobe -s SERIAL type TEXT" in out + err
    assert fake_adb.requests == []


def test_a_word_matching_the_attached_serial_is_not_used(capsys, fake_adb, android):
    code, out, err = invoke(capsys, fake_adb, "--json", "type", "first", "-s", SERIAL)
    assert (code, err) == (2, "")
    assert SERIAL not in out
    assert android.inputs == []
    assert fake_adb.requests == []


def test_after_double_dash_a_device_option_is_text(capsys, fake_adb):
    code, out, _ = invoke(capsys, fake_adb, "-s", SERIAL, "type", "--", "-s")
    assert (code, out) == (0, "typed 2 characters\n")
    assert pieces(fake_adb) == ["-s"]


def test_options_without_a_value_still_work_after_type(capsys, fake_adb):
    code, out, _ = invoke(
        capsys, fake_adb, "-s", SERIAL, "type", "hello", "--json", "--no-start-server"
    )
    assert code == 0
    assert json.loads(out)["length"] == 5


def test_a_lone_dash_is_the_command_word(capsys, fake_adb):
    code, _, err = invoke(capsys, fake_adb, "-", "type")
    assert code == 2
    assert "'-'" in err  # argparse names the invalid command, nothing hidden


def test_options_before_type_keep_their_values(capsys, fake_adb):
    code, _, err = invoke(capsys, fake_adb, "--adb", "missing-adb", "type", "hello")
    assert code == 2
    assert "missing-adb" in err


def test_h_inside_an_unquoted_text_is_not_help(capsys, fake_adb):
    code, out, err = invoke(
        capsys, fake_adb, "type", "--json", "Beforeword", "-h", "Afterword"
    )
    assert (code, err) == (2, "")
    message = json.loads(out)["error"]["message"]
    assert "uiprobe type --help" in message
    assert "Beforeword" not in message
    assert "Afterword" not in message
    assert fake_adb.requests == []


def test_type_help_is_double_dash_help(capsys, fake_adb):
    assert cli.run(["type", "--help"]) == 0
    assert "TEXT" in capsys.readouterr().out
    assert fake_adb.requests == []


@pytest.mark.parametrize(
    "argv",
    [
        ["find", "--text", "type", "--wrong"],
        ["-s", "type", "key", "enter", "--wrong"],
    ],
)
def test_type_as_a_value_keeps_other_errors_visible(capsys, fake_adb, argv):
    code, _, err = invoke(capsys, fake_adb, *argv)
    assert code == 2
    assert "unrecognized arguments: --wrong" in err
