"""Tests for the settings file: generator, `uiprobe init` and the strict reader."""

from __future__ import annotations

import errno
import io
import json
import os
import re
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from uiprobe import cli, settings
from uiprobe.device import DEVICE_WIDE_KEY_PREFIXES, DEVICE_WIDE_KEYS
from uiprobe.guards import (
    API36_BOTTOM_PX,
    API36_SIDE_PX,
    API36_TOP_PX,
    CONFIRM_WORDS,
    DEFAULT_DENY_WORDS,
)
from uiprobe.model import MIN_VISIBLE_PX
from uiprobe.settings import (
    FIXED_DEVICE_KEYS,
    SETTINGS_FILE,
    SettingsError,
    load_settings,
    render_default_settings,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.fake_adb_server import FakeAdbServer

FIXED_KEYS = frozenset(
    {
        "KEYCODE_POWER",
        "KEYCODE_SLEEP",
        "KEYCODE_SOFT_SLEEP",
        "KEYCODE_LOCK",
        "KEYCODE_CALL",
        "KEYCODE_ENDCALL",
        "KEYCODE_HEADSETHOOK",
        "KEYCODE_TV_POWER",
        "KEYCODE_AVR_POWER",
        "KEYCODE_STB_POWER",
    }
)


# The version 1 items, pinned. A built-in word added without an explicit higher
# tag lands in version 1 and fails the snapshot test below.
V1_DENY = (
    "購入",
    "買う",
    "支払",
    "決済",
    "注文",
    "課金",
    "定期購入",
    "登録",
    "送信",
    "投稿",
    "共有",
    "削除",
    "消去",
    "ログアウト",
    "サインアウト",
    "退会",
    "解約",
    "初期化",
    "リセット",
    "通報",
    "ブロック",
    "電話",
    "発信",
    "buy",
    "purchase",
    "pay",
    "checkout",
    "order",
    "subscribe",
    "register",
    "sign up",
    "send",
    "post",
    "share",
    "delete",
    "remove",
    "erase",
    "log out",
    "logout",
    "sign out",
    "signout",
    "unsubscribe",
    "reset",
    "report",
    "block",
    "call",
    "購読",
    "申込",
    "申し込",
    "予約",
    "契約",
    "送金",
    "振込",
    "振り込",
    "寄付",
    "招待",
    "公開",
    "アップロード",
    "返信",
    "同意",
    "インストール",
    "破棄",
    "クリア",
    "フォーマット",
    "アップグレード",
    "プレミアム",
    "無料体験",
    "チェックアウト",
    "check out",
    "submit",
    "subscription",
    "trial",
    "upgrade",
    "premium",
    "install",
    "uninstall",
    "clear",
    "wipe",
    "format",
    "discard",
    "transfer",
    "withdraw",
    "donate",
    "invite",
    "publish",
    "upload",
    "reply",
    "accept",
    "agree",
    "log off",
    "sign off",
    "無効",
    "強制停止",
    "ログイン",
    "サインイン",
    "disable",
    "force stop",
    "sign in",
    "log in",
    "login",
    "カート",
    "アカウント",
    "cart",
    "account",
)
V1_CONFIRM = (
    "OK",
    "はい",
    "確認",
    "確定",
    "続ける",
    "続行",
    "同意",
    "許可",
    "実行",
    "適用",
    "完了",
    "yes",
    "confirm",
    "continue",
    "proceed",
    "accept",
    "agree",
    "allow",
    "apply",
    "done",
)
V1_KEYS = (
    "KEYCODE_ACCESSIBILITY",
    "KEYCODE_ALL_APPS",
    "KEYCODE_ASSIST",
    "KEYCODE_AVR_INPUT",
    "KEYCODE_BOOKMARK",
    "KEYCODE_BRIGHTNESS_DOWN",
    "KEYCODE_BRIGHTNESS_UP",
    "KEYCODE_CALCULATOR",
    "KEYCODE_CALENDAR",
    "KEYCODE_CAMERA",
    "KEYCODE_CONTACTS",
    "KEYCODE_CONTEXTUAL_INSERT",
    "KEYCODE_CONTEXTUAL_QUERY",
    "KEYCODE_CONTEXTUAL_SEARCH",
    "KEYCODE_DICTATE",
    "KEYCODE_DO_NOT_DISTURB",
    "KEYCODE_DVR",
    "KEYCODE_EMOJI_PICKER",
    "KEYCODE_ENVELOPE",
    "KEYCODE_EXPLORER",
    "KEYCODE_FOCUS",
    "KEYCODE_GUIDE",
    "KEYCODE_KEYBOARD_BACKLIGHT_DOWN",
    "KEYCODE_KEYBOARD_BACKLIGHT_TOGGLE",
    "KEYCODE_KEYBOARD_BACKLIGHT_UP",
    "KEYCODE_LANGUAGE_SWITCH",
    "KEYCODE_MANNER_MODE",
    "KEYCODE_META_LEFT",
    "KEYCODE_META_RIGHT",
    "KEYCODE_MUSIC",
    "KEYCODE_MUTE",
    "KEYCODE_NOTIFICATION",
    "KEYCODE_PAIRING",
    "KEYCODE_PROFILE_SWITCH",
    "KEYCODE_RECENT_APPS",
    "KEYCODE_SCREENSHOT",
    "KEYCODE_SETTINGS",
    "KEYCODE_STB_INPUT",
    "KEYCODE_STYLUS_BUTTON_TAIL",
    "KEYCODE_SYSRQ",
    "KEYCODE_TV",
    "KEYCODE_TV_INPUT",
    "KEYCODE_VOICE_ASSIST",
    "KEYCODE_VOLUME_DOWN",
    "KEYCODE_VOLUME_MUTE",
    "KEYCODE_VOLUME_UP",
    "KEYCODE_WAKEUP",
    "KEYCODE_WINDOW",
)
V1_PREFIXES = (
    "KEYCODE_MEDIA_",
    "KEYCODE_SYSTEM_NAVIGATION_",
    "KEYCODE_TV_",
    "KEYCODE_VIDEO_APP_",
    "KEYCODE_FEATURED_APP_",
    "KEYCODE_DEMO_APP_",
    "KEYCODE_STEM_",
)


def write(tmp_path: Path, text: str | bytes, name: str = SETTINGS_FILE) -> Path:
    path = tmp_path / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8", newline="\n")
    return path


def default_file(tmp_path: Path) -> Path:
    return write(tmp_path, render_default_settings())


def edited(tmp_path: Path, old: str, new: str) -> Path:
    """Write the default file with the single text `old` changed to `new`."""
    text = render_default_settings()
    assert text.count(old) == 1, old
    return write(tmp_path, text.replace(old, new))


# -- the defaults and the round trip ----------------------------------------------


def test_the_defaults_round_trip(tmp_path):
    loaded = load_settings(default_file(tmp_path))
    assert loaded.source == tmp_path / SETTINGS_FILE
    assert loaded.format_version == 1
    assert loaded.defaults_version == 1
    assert loaded.press is False
    assert loaded.deny_words == DEFAULT_DENY_WORDS
    assert loaded.confirm_words == CONFIRM_WORDS
    assert loaded.tap_unlabeled is False
    assert loaded.skip_number_only is True
    assert loaded.skip_toggles is True
    assert loaded.refuse_dialog_confirm is True
    assert loaded.min_visible_px == MIN_VISIBLE_PX == 8
    assert loaded.edge_top_px == API36_TOP_PX
    assert loaded.edge_bottom_px == API36_BOTTOM_PX
    assert loaded.edge_side_px == API36_SIDE_PX
    assert (loaded.depth, loaded.actions, loaded.minutes) == (5, 300, 30)
    assert set(loaded.refused_keys) == DEVICE_WIDE_KEYS
    assert loaded.refused_key_prefixes == DEVICE_WIDE_KEY_PREFIXES
    assert loaded.warnings == ()


def test_the_rendered_file_layout():
    text = render_default_settings()
    assert text.startswith("# uiprobe settings")
    assert text.endswith("\n")
    assert "\r" not in text
    assert "format_version = 1\ndefaults_version = 1\n" in text
    assert "press = false\n" in text
    for table in ("[deny]", "[confirm]", "[rules]", "[limits]", "[keys]"):
        assert f"\n{table}\n" in text
    assert text.count("remove = []") == 3
    assert '    "購入",\n' in text  # Japanese stays readable


def test_the_fixed_keys_are_exactly_these():
    assert FIXED_DEVICE_KEYS == FIXED_KEYS
    assert FIXED_KEYS <= DEVICE_WIDE_KEYS


def test_the_fixed_keys_are_not_listed_in_the_file():
    text = render_default_settings()
    for key in FIXED_KEYS:
        assert f'"{key}"' not in text
    assert '"KEYCODE_VOLUME_UP"' in text
    for key in (
        "KEYCODE_ACCESSIBILITY",
        "KEYCODE_CONTEXTUAL_SEARCH",
        "KEYCODE_CONTEXTUAL_QUERY",
        "KEYCODE_CONTEXTUAL_INSERT",
    ):
        assert f'"{key}"' in text


def test_strings_are_escaped(monkeypatch, tmp_path):
    odd = 'quote" back\\slash \u00e9 \u65e5\u672c'
    monkeypatch.setattr(settings, "_DENY_WORDS", ((1, odd),))
    assert load_settings(default_file(tmp_path)).deny_words == (odd,)


def test_control_characters_are_escaped_in_toml_text():
    odd = 'tab\t del\x7f nul\x00 quote" back\\ \u65e5'
    text = settings._toml_string(odd)
    assert tomllib.loads(f"a = {text}") == {"a": odd}
    assert "\t" not in text
    assert "\x7f" not in text


# -- init -------------------------------------------------------------------------


def invoke(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_init_creates_the_file(tmp_path, capsys, monkeypatch, fake_adb: FakeAdbServer):
    monkeypatch.setenv("ADB_SERVER_SOCKET", fake_adb.address)
    target = tmp_path / "my.toml"
    code, out, err = invoke(capsys, "init", str(target))
    assert (code, err) == (0, "")
    assert out == f"{target}\n"
    data = target.read_bytes()
    assert data == render_default_settings().encode("utf-8")
    assert b"\r" not in data
    assert not data.startswith(b"\xef\xbb\xbf")
    assert load_settings(target).deny_words == DEFAULT_DENY_WORDS
    assert fake_adb.requests == []


@pytest.mark.parametrize(
    "options",
    [
        ["-s", "emulator-5554"],
        ["--adb", "/nowhere/adb"],
        ["--adb-server", "tcp:10.0.0.1:5037"],
        ["--no-start-server"],
    ],
)
def test_init_refuses_device_options_before_the_command(tmp_path, capsys, options):
    target = tmp_path / "never.toml"
    code, out, err = invoke(capsys, *options, "init", str(target))
    assert code == 2
    assert out == ""
    assert "init takes no device options" in err
    assert not target.exists()
    code, _, _ = invoke(capsys, "init", *options, str(target))
    assert code == 2
    assert not target.exists()


def test_init_tells_when_the_file_was_left_incomplete(tmp_path, capsys, monkeypatch):
    class Failing:
        def __str__(self) -> str:
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(settings, "render_default_settings", Failing)
    target = tmp_path / "u.toml"
    code, out, err = invoke(capsys, "init", str(target))
    assert code == 2
    assert out == ""
    assert "No space left on device" in err
    assert "may be incomplete" in err
    assert "delete it" in err
    assert "cannot be created" not in err
    assert target.exists()


def _break_buffered_write(monkeypatch, *, always: bool):
    """Shadow the create-only open with a real text stack over a failing FileIO.

    The stack is TextIOWrapper over BufferedWriter over a FileIO subclass whose
    write raises ENOSPC (on every call, or only on the first), so CPython's own
    flush and close-after-failed-flush behavior runs.
    """
    state: dict[str, Any] = {"failed": 0, "raw": []}

    class FailingFileIO(io.FileIO):
        def write(self, data):
            if always or not state["failed"]:
                state["failed"] += 1
                raise OSError(errno.ENOSPC, "No space left on device")
            return super().write(data)

    def fake_open(path, mode, *, encoding, newline):
        assert mode == "x"
        raw = FailingFileIO(path, "x")
        state["raw"].append(raw)
        return io.TextIOWrapper(
            io.BufferedWriter(raw), encoding=encoding, newline=newline
        )

    monkeypatch.setattr(settings, "open", fake_open, raising=False)
    return state


def _open_descriptors() -> set[int] | None:
    try:
        return {int(entry.name) for entry in Path("/proc/self/fd").iterdir()}
    except (OSError, ValueError):
        return None


@pytest.mark.parametrize("always", [True, False], ids=["always", "once"])
def test_a_failure_at_flush_or_close_leaves_an_incomplete_file(
    tmp_path, capsys, monkeypatch, always
):
    state = _break_buffered_write(monkeypatch, always=always)
    before = _open_descriptors()
    direct = tmp_path / "direct.toml"
    with pytest.raises(settings.IncompleteFileError):
        settings.write_default_settings(direct)
    assert state["failed"] >= 1
    assert direct.exists()
    assert all(raw.closed for raw in state["raw"])
    if before is not None:
        assert _open_descriptors() == before

    state["failed"] = 0
    target = tmp_path / "u.toml"
    code, out, err = invoke(capsys, "init", str(target))
    assert code == 2
    assert out == ""
    assert "No space left on device" in err
    assert "may be incomplete, so delete it" in err
    assert "cannot be created" not in err
    assert target.exists()
    assert all(raw.closed for raw in state["raw"])
    if before is not None:
        assert _open_descriptors() == before


def test_init_defaults_to_the_current_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = invoke(capsys, "init")
    assert code == 0
    assert out == f"{SETTINGS_FILE}\n"
    assert (tmp_path / SETTINGS_FILE).is_file()


def test_init_json(tmp_path, capsys):
    target = tmp_path / "x.toml"
    code, out, err = invoke(capsys, "init", "--json", str(target))
    assert (code, err) == (0, "")
    assert json.loads(out) == {"schema": "uiprobe.init/1", "path": str(target)}
    code, out, _ = invoke(capsys, "--json", "init", str(tmp_path / "y.toml"))
    assert code == 0
    assert json.loads(out)["schema"] == "uiprobe.init/1"


def test_init_never_overwrites(tmp_path, capsys):
    target = write(tmp_path, "my own file\n")
    before = target.read_bytes()
    code, out, err = invoke(capsys, "init", str(target))
    assert code == 2
    assert out == ""
    assert f"{target} already exists; uiprobe never overwrites it" in err
    assert target.read_bytes() == before
    code, out, _ = invoke(capsys, "init", "--json", str(target))
    assert code == 2
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.error/1"
    assert payload["error"]["code"] == "usage_error"
    assert target.read_bytes() == before


def test_init_refuses_a_directory(tmp_path, capsys):
    code, out, err = invoke(capsys, "init", str(tmp_path))
    assert (code, out) == (2, "")
    assert str(tmp_path) in err


def test_init_refuses_an_existing_link(tmp_path, capsys):
    target = write(tmp_path, "mine\n", "real.toml")
    link = tmp_path / "link.toml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available")
    code, _, _ = invoke(capsys, "init", str(link))
    assert code == 2
    assert target.read_text(encoding="utf-8") == "mine\n"


def test_init_refuses_a_dangling_link(tmp_path, capsys):
    target = tmp_path / "absent.toml"
    link = tmp_path / "link.toml"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are not available")
    code, out, _ = invoke(capsys, "init", str(link))
    assert (code, out) == (2, "")
    assert not target.exists()
    assert link.is_symlink()


def test_init_reports_a_missing_folder(tmp_path, capsys):
    code, out, err = invoke(capsys, "init", str(tmp_path / "no" / "such" / "f.toml"))
    assert (code, out) == (2, "")
    assert "cannot be created" in err


def test_init_help_mentions_no_device_options(capsys):
    code, out, _ = invoke(capsys, "init", "--help")
    assert code == 0
    assert "--serial" not in out
    assert "--adb" not in out


def test_write_default_settings_creates_only(tmp_path):
    target = tmp_path / SETTINGS_FILE
    settings.write_default_settings(target)
    with pytest.raises(FileExistsError):
        settings.write_default_settings(target)


@pytest.mark.parametrize(
    ("table", "snapshot"),
    [
        ("_DENY_WORDS", V1_DENY),
        ("_CONFIRM_WORDS", V1_CONFIRM),
        ("_REFUSE_KEYS", V1_KEYS),
        ("_REFUSE_PREFIXES", V1_PREFIXES),
    ],
)
def test_the_version_1_defaults_are_pinned(table, snapshot):
    version_1 = tuple(
        item for version, item in getattr(settings, table) if version == 1
    )
    assert version_1 == snapshot, (
        f"{table}: the version 1 items changed. A default added later must be"
        " tagged with the new DEFAULTS_VERSION in the _LATER_* map (see the"
        " comment above the tables in settings.py), not left at 1, or files"
        " written at version 1 would never receive it. Removing or reordering"
        " a version 1 item changes the pinned snapshot instead."
    )


@pytest.mark.parametrize(
    "table", ["_DENY_WORDS", "_CONFIRM_WORDS", "_REFUSE_KEYS", "_REFUSE_PREFIXES"]
)
def test_no_default_is_tagged_beyond_the_current_version(table):
    for version, item in getattr(settings, table):
        assert 1 <= version <= settings.DEFAULTS_VERSION, (table, version, item)


def test_a_default_from_a_future_version_is_not_applied(monkeypatch, tmp_path):
    path = default_file(tmp_path)
    later = settings.DEFAULTS_VERSION + 1
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (later, "gift card"))
    )
    assert "gift card" not in load_settings(path).deny_words


def _interrupt_after_create(monkeypatch):
    """Shadow the create-only open with a stream whose write is Ctrl+C."""
    state: dict[str, Any] = {"handles": []}

    class Interrupting:
        def __init__(self, handle):
            self.handle = handle

        def write(self, _text):
            raise KeyboardInterrupt

        def flush(self):
            self.handle.flush()

        def close(self):
            self.handle.close()

    def fake_open(path, mode, *, encoding, newline):
        assert mode == "x"
        handle = open(path, mode, encoding=encoding, newline=newline)  # noqa: PTH123, SIM115
        state["handles"].append(handle)
        return Interrupting(handle)

    monkeypatch.setattr(settings, "open", fake_open, raising=False)
    return state


def test_an_interrupt_after_the_create_carries_the_path(tmp_path, monkeypatch):
    state = _interrupt_after_create(monkeypatch)
    target = tmp_path / "u.toml"
    with pytest.raises(KeyboardInterrupt) as info:
        settings.write_default_settings(target)
    assert isinstance(info.value, settings.InterruptedFileError)
    assert info.value.path == str(target)
    assert target.exists()
    assert all(handle.closed for handle in state["handles"])


def test_init_interrupted_after_the_create_text(tmp_path, capsys, monkeypatch):
    _interrupt_after_create(monkeypatch)
    target = tmp_path / "u.toml"
    code, out, err = invoke(capsys, "init", str(target))
    assert (code, out) == (130, "")
    assert err.startswith("uiprobe: error: interrupted")
    assert str(target) in err
    assert "may be incomplete, so delete it before running uiprobe init again" in err
    assert target.exists()


def test_init_interrupted_after_the_create_json(tmp_path, capsys, monkeypatch):
    _interrupt_after_create(monkeypatch)
    target = tmp_path / "u.toml"
    code, out, err = invoke(capsys, "init", "--json", str(target))
    assert (code, err) == (130, "")
    error = json.loads(out)["error"]
    assert error["code"] == "interrupted"
    assert str(target) in error["message"]
    assert "may be incomplete" in error["message"]
    assert target.exists()


def test_a_failing_close_does_not_hide_the_interrupt(tmp_path, capsys, monkeypatch):
    class InterruptThenFull:
        def __init__(self, handle):
            self.handle = handle

        def write(self, text):
            return self.handle.write(text)

        def flush(self):
            raise KeyboardInterrupt

        def close(self):
            self.handle.close()
            raise OSError(errno.ENOSPC, "No space left on device")

    def fake_open(path, mode, *, encoding, newline):
        assert mode == "x"
        handle = open(path, mode, encoding=encoding, newline=newline)  # noqa: PTH123, SIM115
        return InterruptThenFull(handle)

    monkeypatch.setattr(settings, "open", fake_open, raising=False)
    target = tmp_path / "u.toml"
    code, out, err = invoke(capsys, "init", str(target))
    assert (code, out) == (130, "")
    assert err.startswith("uiprobe: error: interrupted")
    assert str(target) in err
    assert target.exists()


def test_a_huge_parser_message_is_capped_but_keeps_the_position(tmp_path):
    name = "a" * 500_000
    text = render_default_settings() + f"[{name}]\n[{name}]\n"
    path = write(tmp_path, text)
    message = refused(path, "could not be parsed")
    assert len(message) < 400
    assert re.search(r"\(at line \d+, column \d+\)$", message), message


# -- strict validation --------------------------------------------------------------


def refused(path: Path, *needles: str, exact: str | None = None) -> str:
    with pytest.raises(SettingsError) as info:
        load_settings(path)
    message = str(info.value)
    assert str(path) in message
    for needle in needles:
        assert needle in message, message
    assert isinstance(info.value, ValueError)
    if exact is not None:
        assert message == f"{path}: {exact}"
    return message


def test_a_missing_file_is_refused(tmp_path):
    refused(tmp_path / "none.toml", "no such file")


def test_invalid_toml_is_refused(tmp_path):
    refused(write(tmp_path, "format_version = \n"), "the TOML could not be parsed")


def test_an_unknown_top_level_key_is_refused(tmp_path):
    path = write(tmp_path, render_default_settings() + "\n[extra]\na = 1\n")
    refused(path, "extra", "unknown")
    path = edited(tmp_path, "press = false\n", "press = false\nspeed = 3\n")
    refused(path, "speed", "unknown")


@pytest.mark.parametrize(
    ("old", "new", "where"),
    [
        ("tap_unlabeled = false", "tap_unlabeled = false\nsurprise = 1", "surprise"),
        ("depth = 5", "depth = 5\nsurprise = 1", "surprise"),
        ("remove = []\n\n[confirm]", "remove = []\nextra = []\n\n[confirm]", "extra"),
    ],
)
def test_an_unknown_key_in_a_table_is_refused(tmp_path, old, new, where):
    refused(edited(tmp_path, old, new), where, "unknown key")


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("press = false", 'press = "no"', "press"),
        ("press = false", "press = 0", "press"),
        ("skip_toggles = true", "skip_toggles = 1", "skip_toggles"),
        ("tap_unlabeled = false", 'tap_unlabeled = "false"', "tap_unlabeled"),
        ("min_visible_px = 8", "min_visible_px = true", "min_visible_px"),
        ("depth = 5", "depth = false", "depth"),
        ("depth = 5", "depth = 5.0", "depth"),
        ("depth = 5", 'depth = "5"', "depth"),
        ("format_version = 1", "format_version = true", "format_version"),
    ],
)
def test_wrong_types_are_refused(tmp_path, old, new, key):
    refused(edited(tmp_path, old, new), key)


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("min_visible_px = 8", "min_visible_px = 0", "min_visible_px"),
        ("min_visible_px = 8", "min_visible_px = 65", "min_visible_px"),
        ("edge_top_px = 24", "edge_top_px = -1", "edge_top_px"),
        ("edge_top_px = 24", "edge_top_px = 501", "edge_top_px"),
        ("edge_bottom_px = 32", "edge_bottom_px = -5", "edge_bottom_px"),
        ("edge_side_px = 30", "edge_side_px = 9999", "edge_side_px"),
        ("depth = 5", "depth = 0", "depth"),
        ("depth = 5", "depth = 51", "depth"),
        ("actions = 300", "actions = 0", "actions"),
        ("actions = 300", "actions = 10001", "actions"),
        ("minutes = 30", "minutes = 0", "minutes"),
        ("minutes = 30", "minutes = 1441", "minutes"),
        ("minutes = 30", "minutes = -30", "minutes"),
    ],
)
def test_numbers_out_of_range_are_refused(tmp_path, old, new, key):
    refused(edited(tmp_path, old, new), key, "must be from")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("min_visible_px = 8", "min_visible_px = 1"),
        ("min_visible_px = 8", "min_visible_px = 64"),
        ("edge_top_px = 24", "edge_top_px = 0"),
        ("edge_top_px = 24", "edge_top_px = 500"),
        ("depth = 5", "depth = 1"),
        ("depth = 5", "depth = 50"),
        ("actions = 300", "actions = 10000"),
        ("minutes = 30", "minutes = 1440"),
    ],
)
def test_numbers_at_the_limits_are_accepted(tmp_path, old, new):
    load_settings(edited(tmp_path, old, new))


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ("format_version = 1", "format_version = 2", "format_version"),
        ("format_version = 1", "format_version = 0", "format_version"),
        ("defaults_version = 1", "defaults_version = 0", "defaults_version"),
        ("defaults_version = 1", "defaults_version = 2", "defaults_version"),
    ],
)
def test_versions_out_of_range_are_refused(tmp_path, old, new, key):
    refused(edited(tmp_path, old, new), key)


def test_a_newer_format_is_reported_before_unknown_keys(tmp_path):
    text = render_default_settings().replace("format_version = 1", "format_version = 2")
    refused(
        write(tmp_path, text + "newkey = 1\n"), "format_version", "must be 1, not 2"
    )


def test_a_missing_key_is_reported_as_missing(tmp_path):
    refused(edited(tmp_path, "depth = 5\n", ""), "[limits] depth", "is missing")
    refused(edited(tmp_path, "press = false\n", ""), "press", "is missing")
    text = render_default_settings()
    refused(write(tmp_path, text.replace("tap_unlabeled = false\n", "")), "is missing")


def test_unknown_key_names_are_shown_escaped(tmp_path):
    text = render_default_settings()
    bad_top = '"\\u001b[31mevil" = 1\n' + text
    bad_in_table = text.replace("[limits]\n", '[limits]\n"\\u001b[2Jx" = 1\n')
    for bad, name in ((bad_top, "evil"), (bad_in_table, "[2Jx")):
        with pytest.raises(SettingsError) as info:
            load_settings(write(tmp_path, bad))
        assert "\\x1b" in str(info.value)
        assert "\x1b" not in str(info.value)
        assert name in str(info.value)


def with_remove(tmp_path: Path, index: int, items: str) -> Path:
    """Write the default file with the `remove` list number `index` set."""
    parts = render_default_settings().split("remove = []")
    assert len(parts) == 4
    ends = ["remove = []"] * 3
    ends[index] = f"remove = {items}"
    text = parts[0] + ends[0] + parts[1] + ends[1] + parts[2] + ends[2] + parts[3]
    return write(tmp_path, text)


@pytest.mark.parametrize("name", ["format_version", "defaults_version", "press"])
def test_a_missing_top_level_key_is_refused(tmp_path, name):
    text = render_default_settings()
    lines = [line for line in text.split("\n") if not line.startswith(f"{name} =")]
    refused(write(tmp_path, "\n".join(lines)), name)


@pytest.mark.parametrize("table", ["deny", "confirm", "rules", "limits", "keys"])
def test_a_missing_table_is_refused(tmp_path, table):
    text = render_default_settings()
    head, _, rest = text.partition(f"\n[{table}]\n")
    nxt = re.search(r"^\[", rest, re.MULTILINE)
    tail = rest[nxt.start() :] if nxt else ""
    refused(write(tmp_path, head + "\n" + tail), f"[{table}]", "missing")


def test_a_missing_key_in_a_table_is_refused(tmp_path):
    refused(edited(tmp_path, "depth = 5\n", ""), "[limits] depth")
    refused(edited(tmp_path, "skip_toggles = true\n", ""), "skip_toggles")


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ('words = [\n    "購入",', 'words = [\n    "購入", "",', "[deny] words"),
        ('words = [\n    "購入",', 'words = [\n    "購入", "   ",', "[deny] words"),
        ('words = [\n    "購入",', 'words = [\n    "購入", "\\u200b",', "[deny] words"),
        (
            'words = [\n    "購入",',
            'words = [\n    "購入", "a\\u0001b",',
            "[deny] words",
        ),
        ('words = [\n    "購入",', 'words = [\n    "購入", 5,', "[deny] words"),
        ('words = [\n    "購入",', 'words = [\n    "購入", "購入",', "duplicate"),
        ('words = [\n    "購入",', 'words = [\n    "購入", "CALL",', "duplicate"),
        ('words = [\n    "OK",', 'words = [\n    "OK", "!!",', "[confirm] words"),
        ('words = [\n    "OK",', 'words = [\n    "OK", "ok",', "duplicate"),
    ],
)
def test_bad_words_are_refused(tmp_path, old, new, key):
    refused(edited(tmp_path, old, new), key)


def test_a_words_list_must_be_a_list(tmp_path):
    text = render_default_settings()
    head, _, rest = text.partition("[deny]\n")
    _, _, tail = rest.partition("remove = []")
    refused(write(tmp_path, f'{head}[deny]\nwords = "call"\nremove = []{tail}'), "list")


def test_bad_remove_entries_are_refused(tmp_path):
    first = render_default_settings().index("remove = []")
    text = render_default_settings()
    path = write(tmp_path, text[:first] + 'remove = [""]' + text[first + 11 :])
    refused(path, "[deny] remove")


@pytest.mark.parametrize(
    "name",
    [
        "power",
        "KEYCODE_",
        "keycode_back",
        "KEYCODE_back",
        "KEYCODE_A-B",
        "KEYCODE_" + "A" * 41,
    ],
)
def test_bad_key_names_are_refused(tmp_path, name):
    text = render_default_settings()
    old = '"KEYCODE_VOLUME_UP",'
    assert old in text
    refused(write(tmp_path, text.replace(old, f'"{name}",', 1)), "[keys] refuse")


def test_a_duplicate_key_is_refused(tmp_path):
    old = '    "KEYCODE_VOLUME_UP",\n'
    refused(edited(tmp_path, old, old * 2), "duplicate", "KEYCODE_VOLUME_UP")


def test_a_bad_prefix_is_refused(tmp_path):
    refused(edited(tmp_path, '"KEYCODE_MEDIA_",', '"media",'), "refuse_prefixes")


# -- removing and adding defaults ------------------------------------------------


def test_a_deleted_deny_word_is_not_applied(tmp_path):
    path = edited(tmp_path, '    "購入",\n', "")
    words = load_settings(path).deny_words
    assert "購入" not in words
    assert len(words) == len(DEFAULT_DENY_WORDS) - 1


def test_a_deleted_confirm_word_is_not_applied(tmp_path):
    path = edited(tmp_path, '    "OK",\n', "")
    assert "OK" not in load_settings(path).confirm_words


def test_a_deleted_key_is_not_refused(tmp_path):
    path = edited(tmp_path, '    "KEYCODE_VOLUME_UP",\n', "")
    loaded = load_settings(path)
    assert "KEYCODE_VOLUME_UP" not in loaded.refused_keys
    assert set(loaded.refused_keys) >= FIXED_KEYS


def test_a_deleted_prefix_is_not_refused(tmp_path):
    path = edited(tmp_path, '    "KEYCODE_STEM_",\n', "")
    assert "KEYCODE_STEM_" not in load_settings(path).refused_key_prefixes


def test_a_listed_word_can_be_one_the_defaults_lack(tmp_path):
    path = edited(tmp_path, '    "購入",\n', '    "購入",\n    "gift card",\n')
    words = load_settings(path).deny_words
    assert "gift card" in words
    assert words.index("gift card") == 1


def test_remove_lists_take_words_out(tmp_path):
    text = render_default_settings()
    text = text.replace("remove = []", 'remove = ["call"]', 1)  # deny
    text = text.replace("remove = []", 'remove = ["yes"]', 1)  # confirm
    text = text.replace("remove = []", 'remove = ["KEYCODE_CAMERA"]', 1)  # keys
    loaded = load_settings(write(tmp_path, text))
    assert "call" not in loaded.deny_words
    assert "yes" not in loaded.confirm_words
    assert "KEYCODE_CAMERA" not in loaded.refused_keys


def test_remove_compares_after_normalization(tmp_path):
    text = render_default_settings().replace("remove = []", 'remove = ["Sign In"]', 1)
    loaded = load_settings(write(tmp_path, text))
    assert "sign in" not in loaded.deny_words


def test_removing_a_prefix_under_keys(tmp_path):
    text = render_default_settings()
    head, _, tail = text.rpartition("remove = []")
    path = write(tmp_path, head + 'remove = ["KEYCODE_STEM_"]' + tail)
    assert "KEYCODE_STEM_" not in load_settings(path).refused_key_prefixes


def test_the_fixed_keys_are_always_refused(tmp_path):
    text = render_default_settings()
    head, _, tail = text.rpartition("remove = []")
    removed = ", ".join(f'"{key}"' for key in sorted(FIXED_KEYS))
    loaded = load_settings(write(tmp_path, f"{head}remove = [{removed}]{tail}"))
    assert set(loaded.refused_keys) >= FIXED_KEYS
    assert len(loaded.warnings) == len(FIXED_KEYS)
    for key in FIXED_KEYS:
        assert any(key in warning for warning in loaded.warnings)


def test_the_fixed_keys_come_back_when_the_list_is_empty(tmp_path):
    text = render_default_settings()
    start = text.index("refuse = [")
    end = text.index("refuse_prefixes")
    path = write(tmp_path, text[:start] + "refuse = []\n# prefixes\n" + text[end:])
    loaded = load_settings(path)
    assert set(loaded.refused_keys) == FIXED_KEYS


def test_a_later_default_applies_automatically(monkeypatch, tmp_path):
    path = default_file(tmp_path)
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", 2)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (2, "gift card"))
    )
    monkeypatch.setattr(
        settings, "_CONFIRM_WORDS", (*settings._CONFIRM_WORDS, (2, "go ahead"))
    )
    monkeypatch.setattr(
        settings, "_REFUSE_KEYS", (*settings._REFUSE_KEYS, (2, "KEYCODE_NEWKEY"))
    )
    monkeypatch.setattr(
        settings, "_REFUSE_PREFIXES", (*settings._REFUSE_PREFIXES, (2, "KEYCODE_NEW_"))
    )
    loaded = load_settings(path)  # a version 1 file
    assert loaded.deny_words[-1] == "gift card"
    assert loaded.confirm_words[-1] == "go ahead"
    assert loaded.refused_keys.count("KEYCODE_NEWKEY") == 1
    assert loaded.refused_key_prefixes[-1] == "KEYCODE_NEW_"
    assert loaded.defaults_version == 1
    # a newly written file lists the new items and applies them once
    fresh = write(tmp_path, render_default_settings(), "fresh.toml")
    assert '"gift card"' in fresh.read_text(encoding="utf-8")
    assert load_settings(fresh).deny_words.count("gift card") == 1


def test_a_later_default_can_be_removed_in_advance(monkeypatch, tmp_path):
    text = render_default_settings().replace("remove = []", 'remove = ["gift card"]', 1)
    path = write(tmp_path, text)
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", 2)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (2, "Gift Card"))
    )
    assert "Gift Card" not in load_settings(path).deny_words


def test_a_later_default_is_not_added_twice(monkeypatch, tmp_path):
    path = default_file(tmp_path)
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", 2)
    monkeypatch.setattr(settings, "_DENY_WORDS", (*settings._DENY_WORDS, (2, "CALL")))
    words = load_settings(path).deny_words
    assert sum(1 for word in words if word.casefold() == "call") == 1


# -- reading the file --------------------------------------------------------------


def test_a_utf8_bom_is_accepted(tmp_path):
    data = b"\xef\xbb\xbf" + render_default_settings().encode("utf-8")
    assert load_settings(write(tmp_path, data)).deny_words == DEFAULT_DENY_WORDS


def test_crlf_line_ends_are_accepted(tmp_path):
    data = render_default_settings().encode("utf-8").split(b"\n")
    assert load_settings(write(tmp_path, b"\r\n".join(data))).press is False


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_utf16_is_refused(tmp_path, encoding):
    data = render_default_settings().encode(encoding)
    if encoding != "utf-16":
        data = (b"\xff\xfe" if encoding == "utf-16-le" else b"\xfe\xff") + data
    refused(write(tmp_path, data), "UTF-16", "UTF-8")


def test_utf16_without_a_bom_is_refused(tmp_path):
    data = render_default_settings().encode("utf-16-le")
    refused(write(tmp_path, data), "UTF-16")


def test_invalid_utf8_is_refused(tmp_path):
    refused(
        write(tmp_path, b"press = false\n# \xff\xfe\n"), "not valid UTF-8", "line 2"
    )


def test_a_file_over_one_mebibyte_is_refused(tmp_path):
    data = render_default_settings().encode("utf-8") + b"#" * (1024 * 1024)
    refused(
        write(tmp_path, data), exact=f"larger than {settings.MAX_SETTINGS_BYTES} bytes"
    )


def test_a_directory_is_refused(tmp_path):
    refused(tmp_path, exact="not a regular file")


def test_a_path_that_cannot_be_opened_and_is_not_a_file_says_so(tmp_path, monkeypatch):
    # Windows refuses to open a directory (PermissionError); a Unix socket
    # fails with ENXIO. Either way the message names the kind of path.
    def refuse_open(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(settings.os, "open", refuse_open)
    refused(tmp_path, exact="not a regular file")
    # A regular file that cannot be opened keeps the "cannot be read" text.
    refused(default_file(tmp_path), "cannot be read", "Permission denied")


def _open_fifo_for_writing(path: Path) -> None:
    os.close(os.open(path, os.O_WRONLY))


def test_a_fifo_is_refused_without_blocking(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs os.mkfifo")
    fifo = tmp_path / "pipe.toml"
    os.mkfifo(fifo)
    # This writer lets a wrongly attempted read end at once, so a regression
    # fails the assertion below instead of hanging the run.
    writer = threading.Thread(target=_open_fifo_for_writing, args=(fifo,), daemon=True)
    writer.start()
    refused(fifo, "not a regular file")
    # Release the writer if nothing opened the pipe for reading.
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    writer.join(timeout=5)
    os.close(reader)


def test_a_path_with_a_control_character_is_shown_escaped(tmp_path):
    with pytest.raises(SettingsError) as info:
        load_settings(str(tmp_path / "a\x1bb.toml"))
    assert "\x1b" not in str(info.value)


def test_a_path_with_nul_is_refused(tmp_path):
    with pytest.raises(SettingsError):
        load_settings(str(tmp_path / "a\x00b.toml"))


def test_the_module_exports():
    for name in settings.__all__:
        assert hasattr(settings, name)


# -- remove lists, versions, reading --------------------------------------------


def test_a_missing_remove_list_is_allowed(tmp_path):
    text = render_default_settings().replace("remove = []\n", "")
    assert load_settings(write(tmp_path, text)).deny_words == DEFAULT_DENY_WORDS


def test_confirm_words_are_compared_in_the_confirm_form(tmp_path):
    refused(
        edited(tmp_path, 'words = [\n    "OK",', 'words = [\n    "OK", "OK!",'),
        "duplicate",
    )
    loaded = load_settings(with_remove(tmp_path, 1, '["OK!"]'))
    assert "OK" not in loaded.confirm_words


def test_removing_a_key_that_a_prefix_covers_warns(tmp_path):
    loaded = load_settings(with_remove(tmp_path, 2, '["KEYCODE_MEDIA_PLAY"]'))
    assert len(loaded.warnings) == 1
    assert "KEYCODE_MEDIA_PLAY" in loaded.warnings[0]
    assert "prefix" in loaded.warnings[0]
    both = load_settings(
        with_remove(tmp_path, 2, '["KEYCODE_POWER", "KEYCODE_TV_POWER"]')
    )
    assert len(both.warnings) == 2
    assert all("always refused" in warning for warning in both.warnings)
    plain = load_settings(with_remove(tmp_path, 2, '["KEYCODE_VOLUME_UP"]'))
    assert plain.warnings == ()
    assert "KEYCODE_VOLUME_UP" not in plain.refused_keys


def test_removing_a_prefix_removes_it(tmp_path):
    loaded = load_settings(with_remove(tmp_path, 2, '["KEYCODE_MEDIA_"]'))
    assert "KEYCODE_MEDIA_" not in loaded.refused_key_prefixes
    assert loaded.warnings == ()


def test_a_later_default_is_not_restored_in_a_file_of_that_version(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", 2)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (2, "gift card"))
    )
    text = render_default_settings().replace(
        "defaults_version = 1", "defaults_version = 2"
    )
    assert '"gift card"' in text
    path = write(tmp_path, text.replace('    "gift card",\n', ""))
    assert "gift card" not in load_settings(path).deny_words
    assert "gift card" in load_settings(write(tmp_path, text, "kept.toml")).deny_words


def test_a_large_file_is_refused_before_it_is_read(tmp_path, monkeypatch):
    path = write(tmp_path, b"#" * (1024 * 1024 + 1))

    def must_not_read(*_args):
        raise AssertionError("the file was read")

    monkeypatch.setattr(settings.os, "read", must_not_read)
    refused(path, "larger than")


def _after_first_read(monkeypatch, action):
    """Run `action()` once, right after the reader's first os.read."""
    real = os.read
    state = {"calls": 0}

    def read(fd, size):
        data = real(fd, size)
        state["calls"] += 1
        if state["calls"] == 1:
            action()
        return data

    monkeypatch.setattr(settings.os, "read", read)
    return state


def test_a_file_changed_while_it_is_read_is_refused(tmp_path, monkeypatch):
    path = default_file(tmp_path)

    def change():
        path.write_text(render_default_settings() + "# changed\n", encoding="utf-8")

    _after_first_read(monkeypatch, change)
    refused(path, "changed while it was read")


def test_a_same_size_change_while_reading_is_refused(tmp_path, monkeypatch):
    path = default_file(tmp_path)

    def rewrite():
        info = path.stat()
        data = path.read_bytes()
        path.write_bytes(data.replace(b"press = false", b"press = true "))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))

    _after_first_read(monkeypatch, rewrite)
    refused(path, "changed while it was read")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="st_ctime is the creation time on Windows and does not change on a rewrite",
)
def test_a_same_size_rewrite_with_a_restored_mtime_is_refused(tmp_path, monkeypatch):
    path = default_file(tmp_path)

    def rewrite():
        info = path.stat()
        data = path.read_bytes()
        path.write_bytes(data.replace(b"press = false", b"press = true "))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        for _ in range(200):  # a coarse clock may need a few ticks to move ctime
            if path.stat().st_ctime_ns != info.st_ctime_ns:
                break
            time.sleep(0.01)
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        after = path.stat()
        assert after.st_mtime_ns == info.st_mtime_ns
        assert after.st_ctime_ns != info.st_ctime_ns

    _after_first_read(monkeypatch, rewrite)
    refused(path, "changed while it was read")


def test_a_fifo_gets_exactly_the_one_message(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs os.mkfifo")
    fifo = tmp_path / "p.toml"
    os.mkfifo(fifo)
    refused(fifo, exact="not a regular file")


def test_loading_a_fifo_returns_promptly(tmp_path):
    if sys.platform == "win32":
        pytest.skip("needs named pipes")
    fifo = tmp_path / "uiprobe.toml"
    os.mkfifo(fifo)
    outcome: list[object] = []

    def run():
        try:
            outcome.append(load_settings(fifo))
        except SettingsError as error:
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=10)
    if thread.is_alive():
        # Release the stuck open so the process can finish, then fail.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        thread.join(timeout=5)
        pytest.fail("loading a FIFO blocked")
    assert len(outcome) == 1
    assert isinstance(outcome[0], SettingsError)
    assert "not a regular file" in str(outcome[0])


def test_a_growing_file_stops_at_the_limit_and_is_refused(tmp_path, monkeypatch):
    limit = settings.MAX_SETTINGS_BYTES
    path = write(tmp_path, b"#" * (limit - 10))
    real = os.read
    total = {"bytes": 0}

    def growing(fd, size):
        data = real(fd, size)
        assert size <= limit + 1 - total["bytes"]
        total["bytes"] += len(data)
        with path.open("ab") as handle:
            handle.write(b"#" * 100_000)
        return data

    monkeypatch.setattr(settings.os, "read", growing)
    refused(path, "larger than")
    assert total["bytes"] == limit + 1


def test_a_file_that_grew_after_the_size_check_is_refused(tmp_path, monkeypatch):
    path = write(tmp_path, b"# a\n")
    _after_first_read(monkeypatch, lambda: path.write_bytes(b"# a\n# b\n"))
    refused(path, "changed while it was read")


@pytest.mark.skipif(
    sys.platform == "win32", reason="creating symbolic links needs a privilege"
)
class TestSymlinkedSettings:
    def test_a_symlinked_settings_file_is_read(self, tmp_path):
        real = default_file(tmp_path)
        link = tmp_path / "link.toml"
        link.symlink_to(real)
        assert load_settings(link).deny_words == load_settings(real).deny_words
        assert load_settings(link).press == load_settings(real).press

    def test_an_aba_switch_gives_only_the_opened_files_bytes(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        file_a = default_file(tmp_path / "a")
        file_b = write(tmp_path / "b", "[deny]\nwords = ['x']\n")
        link = tmp_path / "link.toml"
        link.symlink_to(file_a)
        real_open, real_fstat = os.open, os.fstat
        fstats = {"n": 0}

        def open_then_switch(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            link.unlink()
            link.symlink_to(file_b)  # B right after the open
            return fd

        def fstat(fd):
            fstats["n"] += 1
            if fstats["n"] == 2:  # A again before the final path check
                link.unlink()
                link.symlink_to(file_a)
            return real_fstat(fd)

        monkeypatch.setattr(settings.os, "open", open_then_switch)
        monkeypatch.setattr(settings.os, "fstat", fstat)
        # The descriptor names A, and A is what the path names again at the
        # end: A's bytes, never B's.
        assert settings._read_bytes(link, "x") == file_a.read_bytes()

    def test_a_switch_to_another_file_is_refused(self, tmp_path, monkeypatch):
        file_a = default_file(tmp_path)
        file_b = write(tmp_path, "[deny]\nwords = ['x']\n", "b.toml")
        link = tmp_path / "link.toml"
        link.symlink_to(file_a)

        def switch():
            link.unlink()
            link.symlink_to(file_b)

        _after_first_read(monkeypatch, switch)
        refused(link, "changed while it was read")


@pytest.mark.skipif(
    sys.platform == "win32", reason="an open file cannot be deleted on Windows"
)
def test_a_path_replaced_after_the_read_is_refused(tmp_path, monkeypatch):
    path = default_file(tmp_path)
    content = path.read_bytes()

    def replace_file():
        path.unlink()
        path.write_bytes(content)  # same bytes, another file

    _after_first_read(monkeypatch, replace_file)
    refused(path, "changed while it was read")


@pytest.mark.skipif(
    sys.platform == "win32", reason="an open file cannot be deleted on Windows"
)
def test_a_path_removed_after_the_read_is_refused(tmp_path, monkeypatch):
    path = default_file(tmp_path)
    _after_first_read(monkeypatch, path.unlink)
    refused(path, "changed while it was read")


def _count_closes(monkeypatch):
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def counting_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    def counting_close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(settings.os, "open", counting_open)
    monkeypatch.setattr(settings.os, "close", counting_close)
    return opened, closed


@pytest.mark.parametrize(
    "case", ["ok", "large", "directory", "read-error", "changed", "missing"]
)
def test_the_descriptor_is_closed_on_every_path(tmp_path, monkeypatch, case):
    opened, closed = _count_closes(monkeypatch)
    path = default_file(tmp_path)
    if case == "large":
        path = write(tmp_path, b"#" * (1024 * 1024 + 1), "big.toml")
    elif case == "directory":
        path = tmp_path
    elif case == "missing":
        path = tmp_path / "none.toml"
    elif case == "read-error":

        def broken(_fd, _size):
            raise OSError(5, "input/output error")

        monkeypatch.setattr(settings.os, "read", broken)
    elif case == "changed":
        _after_first_read(monkeypatch, lambda: path.write_bytes(b"# changed\n"))
    if case == "ok":
        load_settings(path)
    else:
        with pytest.raises(SettingsError):
            load_settings(path)
    assert closed == opened
    no_open = case == "missing" or (case == "directory" and sys.platform == "win32")
    assert len(opened) == (0 if no_open else 1)


def test_a_failing_close_does_not_mask_the_refusal(tmp_path, monkeypatch):
    path = write(tmp_path, b"#" * (1024 * 1024 + 1), "big.toml")
    real_close = os.close

    def failing(fd):
        real_close(fd)
        raise OSError(5, "input/output error")

    monkeypatch.setattr(settings.os, "close", failing)
    refused(path, exact=f"larger than {settings.MAX_SETTINGS_BYTES} bytes")


def test_the_descriptor_is_closed_after_an_unexpected_error(tmp_path, monkeypatch):
    opened, closed = _count_closes(monkeypatch)
    path = default_file(tmp_path)

    def boom(_fd):
        raise RuntimeError("unexpected")

    with monkeypatch.context() as scoped:  # os is global: keep the patch short
        scoped.setattr(settings.os, "fstat", boom)
        with pytest.raises(RuntimeError):
            load_settings(path)
    assert closed == opened
    assert len(opened) == 1


def test_a_deeply_nested_array_is_a_settings_error(tmp_path):
    path = write(tmp_path, "a = " + "[" * 600 + "]" * 600 + "\n")
    refused(path, "the TOML could not be parsed")


@pytest.mark.usefixtures("_digit_limit")
def test_an_integer_with_too_many_digits_is_a_settings_error(tmp_path):
    path = write(tmp_path, "a = 1" + "0" * 4999 + "\n")
    refused(path, "the TOML could not be parsed")


def test_a_very_long_unknown_key_is_capped_in_the_message(tmp_path):
    key = "k" * 900_000
    path = edited(tmp_path, "[keys]\n", f"[keys]\n{key} = 1\n")
    message = refused(path, "[keys]", "unknown key")
    assert len(message) < 400


def test_a_very_long_bad_keycode_name_is_capped_in_the_message(tmp_path):
    name = "KEYCODE_" + "A" * 900_000
    path = with_list(tmp_path, "keys", "refuse", f'"{name}"')
    message = refused(path, "[keys] refuse")
    assert len(message) < 400


def test_a_utf16_bom_gets_its_own_message(tmp_path):
    data = b"\xff\xfe" + render_default_settings().encode("utf-16-le")
    refused(write(tmp_path, data), "the file is UTF-16")


# -- huge integers in messages ----------------------------------------------------

HUGE = "0x" + "f" * 5000


@pytest.fixture
def _digit_limit() -> Iterator[None]:
    """Use Python's default int-to-str digit limit, whatever the host set."""
    before = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        yield
    finally:
        sys.set_int_max_str_digits(before)


def _huge_cases() -> list[tuple[str, str, str]]:
    cases = [
        ("format_version = 1", f"format_version = {HUGE}", "format_version"),
        ("defaults_version = 1", f"defaults_version = {HUGE}", "defaults_version"),
    ]
    for old, key in [
        ("min_visible_px = 8", "min_visible_px"),
        ("edge_top_px = 24", "edge_top_px"),
        ("edge_bottom_px = 32", "edge_bottom_px"),
        ("edge_side_px = 30", "edge_side_px"),
        ("depth = 5", "depth"),
        ("actions = 300", "actions"),
        ("minutes = 30", "minutes"),
    ]:
        cases.append((old, f"{key} = {HUGE}", key))
    return cases


@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize(("old", "new", "key"), _huge_cases())
def test_a_huge_integer_is_refused_with_the_file_and_key(tmp_path, old, new, key):
    message = refused(edited(tmp_path, old, new), key, "an integer with")
    assert len(message) < 400


def with_list(tmp_path: Path, table: str, key: str, literal: str) -> Path:
    """Write the default file with `[table] key` set to the list `[literal]`."""
    text = render_default_settings()
    head, marker, tail = text.partition(f"[{table}]\n")
    assert marker
    section, bracket, rest = tail.partition("\n[")
    pattern = rf"^{key} = (\[\]|\[\n.*?^\])\n?"
    section, count = re.subn(pattern, "", section, flags=re.MULTILINE | re.DOTALL)
    assert count == 1, (table, key)
    new = f"{head}[{table}]\n{key} = [{literal}]\n{section}{bracket}{rest}"
    return write(tmp_path, new)


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("deny", "words"),
        ("deny", "remove"),
        ("confirm", "words"),
        ("confirm", "remove"),
        ("keys", "refuse"),
        ("keys", "refuse_prefixes"),
        ("keys", "remove"),
    ],
)
@pytest.mark.usefixtures("_digit_limit")
@pytest.mark.parametrize("literal", [HUGE, f"[{HUGE}]"])
def test_a_huge_integer_in_a_list_is_refused_with_the_file_and_key(
    tmp_path, table, key, literal
):
    path = with_list(tmp_path, table, key, literal)
    message = refused(path, f"[{table}] {key}", "must hold only strings")
    assert len(message) < 400
