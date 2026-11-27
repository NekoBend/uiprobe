"""Tests for the settings file: generator, `uiprobe init` and the strict reader."""

from __future__ import annotations

import dataclasses
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

from uiprobe import cli, guards, pngfile, settings
from uiprobe.device import DEVICE_WIDE_KEY_PREFIXES, DEVICE_WIDE_KEYS
from uiprobe.guards import (
    API36_BOTTOM_PX,
    API36_SIDE_PX,
    API36_TOP_PX,
    CONFIRM_WORDS,
    DEFAULT_DENY_EXCEPTIONS,
    DEFAULT_DENY_WORDS,
    IRREVERSIBLE_PHRASES,
)
from uiprobe.guards.constants import DIALOG_ONLY_EXACT_LABELS
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

CURRENT = settings.DEFAULTS_VERSION
NEXT = CURRENT + 1  # a defaults version that does not exist yet
DATA = Path(__file__).parent / "data"

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
V1_IRREVERSIBLE = ()  # the [irreversible] table came with version 2
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
# The version 2 items, pinned like version 1.
V2_DENY = ("やり直", "元に戻", "restart", "undo")
V2_IRREVERSIBLE = (
    "can't undo",
    "cannot undo",
    "cannot be undone",
    "can't be undone",
    "cannot be reversed",
    "can't be reversed",
    "cannot be recovered",
    "can't be recovered",
    "irreversible",
    "irreversibly",
    "permanently",
    "this action is permanent",
    "元に戻せません",
    "取り消せません",
    "元に戻すことはできません",
    "元に戻すことができません",
    "取り消すことはできません",
    "取り消すことができません",
    "復元できません",
    "完全に削除",
)
# The file that `uiprobe init` wrote at defaults version 1 (rendered at
# revision ff6f5f0, before version 2 existed), unchanged.
V1_FILE = DATA / "settings_v1.toml"
V2_FILE = DATA / "settings_v2.toml"
# The file that `uiprobe init` wrote at defaults version 3 (rendered at
# revision a7e8052, before version 4 existed), unchanged.
V3_FILE = DATA / "settings_v3.toml"
# The file that `uiprobe init` wrote at defaults version 4 (rendered at
# revision 9e322cd, before version 5 existed), unchanged.
V4_FILE = DATA / "settings_v4.toml"
V5_FILE = DATA / "settings_v5.toml"
# The file that `uiprobe init` wrote at defaults version 6 (rendered at
# revision 425ce46, before version 7 existed), unchanged.
V6_FILE = DATA / "settings_v6.toml"
# The file that `uiprobe init` wrote at defaults version 7 (rendered at
# revision 2710222, before version 8 existed), unchanged.
V7_FILE = DATA / "settings_v7.toml"
# The file that `uiprobe init` wrote at defaults version 8 (rendered at
# revision e48fff0, before version 9 existed), unchanged.
V8_FILE = DATA / "settings_v8.toml"
# The file that `uiprobe init` wrote at defaults version 9 (rendered at
# revision 3903e87, before version 10 existed), unchanged.
V9_FILE = DATA / "settings_v9.toml"
# The version 9 items: the confirm words that turn a setting on.
V9_CONFIRM = ("turn on", "enable", "オンにする", "有効にする")
# The file that `uiprobe init` wrote at defaults version 10 (rendered at
# revision 70bf78e, before version 11 existed), unchanged.
V10_FILE = DATA / "settings_v10.toml"
# The file that `uiprobe init` wrote at defaults version 11 (rendered at
# revision e2f7a9d, before version 12 existed), unchanged.
V11_FILE = DATA / "settings_v11.toml"
# The version 12 items: the confirm words that count only as a whole label.
V12_CONFIRM = ("on", "オン")
# The version 7 items, pinned like version 1 and 2: the state-changing words.
V7_DENY = (
    "切断",
    "接続を解除",
    "ペアリングを解除",
    "オフにする",
    "取り出",
    "マウント解除",
    "リンク解除",
    "forget",
    "disconnect",
    "unpair",
    "turn off",
    "revoke",
    "eject",
    "unmount",
    "unlink",
    "deactivate",
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
    assert loaded.defaults_version == CURRENT == 12
    assert loaded.press is False
    assert loaded.deny_words == DEFAULT_DENY_WORDS
    assert loaded.deny_exceptions == DEFAULT_DENY_EXCEPTIONS
    assert loaded.confirm_words == CONFIRM_WORDS
    assert loaded.irreversible_phrases == IRREVERSIBLE_PHRASES
    assert loaded.refuse_irreversible_screens is True
    assert loaded.tap_unlabeled is False
    assert loaded.skip_number_only is True
    assert loaded.skip_toggles is True
    assert loaded.refuse_dialog_confirm is True
    assert loaded.skip_two_target_rows is True
    assert loaded.skip_rows_with_buttons is False
    assert loaded.skip_navigate_up is True
    assert loaded.skip_sliders is True
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
    assert "format_version = 1\ndefaults_version = 12\n" in text
    assert "press = false\n" in text
    for table in (
        "[deny]",
        "[confirm]",
        "[irreversible]",
        "[rules]",
        "[limits]",
        "[keys]",
    ):
        assert f"\n{table}\n" in text
    assert text.count("remove = []") == 4
    assert "refuse_irreversible_screens = true\n" in text
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


def test_write_default_settings_refuses_a_descriptor(tmp_path):
    target = write(tmp_path, "mine\n", "mine.toml")
    fd = os.open(target, os.O_RDWR)
    try:
        with pytest.raises(TypeError):
            settings.write_default_settings(fd)  # ty: ignore[invalid-argument-type]
        os.fstat(fd)  # still open
    finally:
        os.close(fd)
    assert target.read_text(encoding="utf-8") == "mine\n"


@pytest.mark.parametrize("through", ["str", "path"])
def test_write_default_settings_uses_indexable_text_as_text(tmp_path, through):
    sentinel = write(tmp_path, "mine\n", "mine.toml")
    fd = os.open(sentinel, os.O_RDWR)

    class IndexableText(str):
        """Text that open() would take as the sentinel's descriptor."""

        __slots__ = ()

        def __index__(self) -> int:
            return fd

    class IndexablePath:
        def __fspath__(self) -> str:
            return IndexableText(target)

    target = str(tmp_path / SETTINGS_FILE)
    given = IndexableText(target) if through == "str" else IndexablePath()
    try:
        settings.write_default_settings(given)
        os.fstat(fd)  # still open
    finally:
        os.close(fd)
    assert sentinel.read_text(encoding="utf-8") == "mine\n"
    assert Path(target).read_text(encoding="utf-8") == (
        settings.render_default_settings()
    )


@pytest.mark.parametrize("path", [b"u.toml", 2.0, None])
def test_write_default_settings_refuses_what_is_not_text(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TypeError):
        settings.write_default_settings(path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["u.toml:extra", "u.toml::$DATA", "dir:x/u.toml"])
def test_on_windows_a_stream_path_is_refused(tmp_path, monkeypatch, name):
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    with pytest.raises(ValueError, match="stream"):
        settings.write_default_settings(tmp_path / name)
    assert list(tmp_path.iterdir()) == []


def test_on_windows_a_stream_of_an_existing_file_is_refused(tmp_path, monkeypatch):
    target = write(tmp_path, "mine\n", SETTINGS_FILE)
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    with pytest.raises(ValueError, match="stream"):
        settings.write_default_settings(f"{target}:extra")
    assert target.read_text(encoding="utf-8") == "mine\n"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name != "nt", reason="streams exist on Windows")
def test_on_this_windows_a_stream_of_an_existing_file_is_not_made(tmp_path):
    target = write(tmp_path, "mine\n", SETTINGS_FILE)
    stream = f"{target}:extra"
    with pytest.raises(ValueError, match="stream"):
        settings.write_default_settings(stream)
    with pytest.raises(FileNotFoundError):
        open(stream, "rb").close()  # noqa: PTH123
    assert target.read_text(encoding="utf-8") == "mine\n"


def test_an_os_error_inside_a_callers_interrupt_handler_is_a_write_failure(
    tmp_path, monkeypatch
):
    # Only the first write fails, so close() succeeds and the flush error is
    # the one raised; its context is the caller's KeyboardInterrupt.
    _break_buffered_write(monkeypatch, always=False)
    target = tmp_path / "u.toml"
    try:
        raise KeyboardInterrupt  # the caller's own handler
    except KeyboardInterrupt:
        with pytest.raises(settings.IncompleteFileError):
            settings.write_default_settings(target)
    assert target.exists()


def test_a_path_that_cannot_be_looked_up_is_not_taken_as_missing(
    tmp_path, capsys, monkeypatch
):
    target = tmp_path / SETTINGS_FILE
    real = os.lstat

    def lstat(path, *args, **kwargs):
        if os.fspath(path) == str(target):
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return real(path, *args, **kwargs)

    with monkeypatch.context() as scoped:  # os is global: keep the patch short
        scoped.setattr(pngfile.os, "lstat", lstat)
        with pytest.raises(PermissionError):
            settings.write_default_settings(target)
        code, out, err = invoke(capsys, "init", str(target))
    assert (code, out) == (2, "")
    assert "cannot be created" in err
    assert "Permission denied" in err
    assert list(tmp_path.iterdir()) == []


def test_elsewhere_a_colon_is_part_of_the_name(tmp_path, monkeypatch):
    monkeypatch.setattr(pngfile, "_WINDOWS", False)
    if os.name == "nt":
        pytest.skip("Windows names a stream with a colon")
    target = tmp_path / "u.toml:extra"
    settings.write_default_settings(target)
    assert target.is_file()


@pytest.mark.parametrize(
    ("path", "word"), [("", "empty"), ("u\x00.toml", "null")], ids=["empty", "null"]
)
def test_write_default_settings_refuses_an_empty_or_null_path(
    tmp_path, monkeypatch, path, word
):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=word):
        settings.write_default_settings(path)
    assert list(tmp_path.iterdir()) == []


def test_init_refuses_a_stream_path_on_windows(tmp_path, capsys, monkeypatch):
    target = write(tmp_path, "mine\n", SETTINGS_FILE)
    monkeypatch.setattr(pngfile, "_WINDOWS", True)
    code, out, err = invoke(capsys, "init", f"{target}:extra")
    assert (code, out) == (2, "")
    assert "names a stream of a file" in err
    assert target.read_text(encoding="utf-8") == "mine\n"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    ("table", "snapshot"),
    [
        ("_DENY_WORDS", V1_DENY),
        ("_DENY_EXCEPTIONS", ()),
        ("_CONFIRM_WORDS", V1_CONFIRM),
        ("_IRREVERSIBLE_PHRASES", V1_IRREVERSIBLE),
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
    ("table", "snapshot"),
    [
        ("_DENY_WORDS", V2_DENY),
        ("_DENY_EXCEPTIONS", ()),
        ("_CONFIRM_WORDS", ()),
        ("_IRREVERSIBLE_PHRASES", V2_IRREVERSIBLE),
        ("_REFUSE_KEYS", ()),
        ("_REFUSE_PREFIXES", ()),
    ],
)
def test_the_version_2_defaults_are_pinned(table, snapshot):
    version_2 = tuple(
        item for version, item in getattr(settings, table) if version == 2
    )
    assert version_2 == snapshot


@pytest.mark.parametrize(
    "table",
    [
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ],
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
        ("defaults_version = 12", "defaults_version = 0", "defaults_version"),
        ("defaults_version = 12", "defaults_version = 13", "defaults_version"),
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
    assert len(parts) == 5
    ends = ["remove = []"] * 4
    ends[index] = f"remove = {items}"
    text = "".join(part + end for part, end in zip(parts, ends, strict=False))
    return write(tmp_path, text + parts[4])


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


@pytest.mark.parametrize("table", ["deny", "confirm", "rules", "limits", "keys"])
def test_a_table_of_the_wrong_type_is_refused(tmp_path, table):
    text = render_default_settings()
    head, _, rest = text.partition(f"\n[{table}]\n")
    nxt = re.search(r"^\[", rest, re.MULTILINE)
    tail = rest[nxt.start() :] if nxt else ""
    head = head.replace("press = false\n", f"press = false\n{table} = false\n")
    path = write(tmp_path, head + "\n" + tail)
    refused(path, exact=f"[{table}]: must be a table")


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
    text = text.replace("remove = []", 'remove = ["permanently"]', 1)  # phrases
    text = text.replace("remove = []", 'remove = ["KEYCODE_CAMERA"]', 1)  # keys
    loaded = load_settings(write(tmp_path, text))
    assert "call" not in loaded.deny_words
    assert "yes" not in loaded.confirm_words
    assert "permanently" not in loaded.irreversible_phrases
    assert "irreversible" in loaded.irreversible_phrases
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
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", NEXT)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (NEXT, "gift card"))
    )
    monkeypatch.setattr(
        settings, "_CONFIRM_WORDS", (*settings._CONFIRM_WORDS, (NEXT, "go ahead"))
    )
    monkeypatch.setattr(
        settings, "_REFUSE_KEYS", (*settings._REFUSE_KEYS, (NEXT, "KEYCODE_NEWKEY"))
    )
    monkeypatch.setattr(
        settings,
        "_REFUSE_PREFIXES",
        (*settings._REFUSE_PREFIXES, (NEXT, "KEYCODE_NEW_")),
    )
    loaded = load_settings(path)  # a file of the current version
    assert loaded.deny_words[-1] == "gift card"
    assert loaded.confirm_words[-1] == "go ahead"
    assert loaded.refused_keys.count("KEYCODE_NEWKEY") == 1
    assert loaded.refused_key_prefixes[-1] == "KEYCODE_NEW_"
    assert loaded.defaults_version == CURRENT
    # a newly written file lists the new items and applies them once
    fresh = write(tmp_path, render_default_settings(), "fresh.toml")
    assert '"gift card"' in fresh.read_text(encoding="utf-8")
    assert load_settings(fresh).deny_words.count("gift card") == 1


def test_a_later_default_can_be_removed_in_advance(monkeypatch, tmp_path):
    text = render_default_settings().replace("remove = []", 'remove = ["gift card"]', 1)
    path = write(tmp_path, text)
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", NEXT)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (NEXT, "Gift Card"))
    )
    assert "Gift Card" not in load_settings(path).deny_words


def test_a_later_default_is_not_added_twice(monkeypatch, tmp_path):
    path = default_file(tmp_path)
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", NEXT)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (NEXT, "CALL"))
    )
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


@pytest.mark.parametrize(
    ("char", "escape"),
    [
        ("\x1b", "\\x1b"),
        ("\x9b", "\\x9b"),
        ("\u202e", "\\u202e"),
        ("\U000e0001", "\\U000e0001"),
    ],
)
def test_escapes_are_unambiguous(char, escape):
    assert settings._shown(f"a{char}2e") == f"a{escape}2e"


def test_a_real_escape_character_and_its_typed_form_differ(tmp_path):
    real = str(tmp_path / "missing\u202e.toml")
    typed = str(tmp_path / ("missing" + "\\" + "u202e.toml"))  # six characters
    assert real != typed
    with pytest.raises(SettingsError) as first:
        load_settings(real)
    with pytest.raises(SettingsError) as second:
        load_settings(typed)
    assert str(first.value) != str(second.value)
    # The expected text follows the code's own rule, so it holds on every OS.
    assert str(first.value).startswith(settings._shown(real) + ": ")
    assert str(second.value).startswith(settings._shown(typed) + ": ")
    assert settings._shown(real).endswith("missing\\u202e.toml")
    assert settings._shown(typed).endswith("missing\\\\u202e.toml")


def test_a_text_that_needs_no_escape_is_shown_as_it_is():
    windows = "C:\\Users\\me\\uiprobe.toml"
    assert settings._shown(windows) == windows
    assert settings._shown("naive \u00e9 \u65e5") == "naive \u00e9 \u65e5"
    # Backslash letters that are not an escape of ours are left alone.
    assert settings._shown("C:\\Users\\x\\u12\\Uabc") == "C:\\Users\\x\\u12\\Uabc"


def test_a_windows_style_path_is_shown_unchanged_in_the_error():
    path = "C:\\no such folder\\Users\\uiprobe.toml"
    with pytest.raises(SettingsError) as info:
        load_settings(path)
    assert str(info.value).startswith(path + ": ")


def test_no_two_texts_show_alike():
    texts = ["a\u202eb", "a\\u202eb", "a\\\\u202eb", "a\\x1bb", "a\x1bb", "a\\\x1bb"]
    assert len({settings._shown(text) for text in texts}) == len(texts)


def test_backslashes_are_doubled_when_an_escape_is_shown():
    assert settings._shown("C:\\a\x1bb") == "C:\\\\a\\x1bb"
    assert settings._shown("\\") == "\\"
    assert settings._shown("\\\x1b") == "\\\\\\x1b"


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
    loaded = load_settings(with_remove(tmp_path, 3, '["KEYCODE_MEDIA_PLAY"]'))
    assert len(loaded.warnings) == 1
    assert "KEYCODE_MEDIA_PLAY" in loaded.warnings[0]
    assert "prefix" in loaded.warnings[0]
    both = load_settings(
        with_remove(tmp_path, 3, '["KEYCODE_POWER", "KEYCODE_TV_POWER"]')
    )
    assert len(both.warnings) == 2
    assert all("always refused" in warning for warning in both.warnings)
    plain = load_settings(with_remove(tmp_path, 3, '["KEYCODE_VOLUME_UP"]'))
    assert plain.warnings == ()
    assert "KEYCODE_VOLUME_UP" not in plain.refused_keys


def test_removing_a_prefix_removes_it(tmp_path):
    loaded = load_settings(with_remove(tmp_path, 3, '["KEYCODE_MEDIA_"]'))
    assert "KEYCODE_MEDIA_" not in loaded.refused_key_prefixes
    assert loaded.warnings == ()


def test_a_later_default_is_not_restored_in_a_file_of_that_version(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", NEXT)
    monkeypatch.setattr(
        settings, "_DENY_WORDS", (*settings._DENY_WORDS, (NEXT, "gift card"))
    )
    text = render_default_settings().replace(
        f"defaults_version = {CURRENT}", f"defaults_version = {NEXT}"
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
    assert str(outcome[0]) == f"{fifo}: not a regular file"


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
        ("defaults_version = 12", f"defaults_version = {HUGE}", "defaults_version"),
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


# -- the irreversibility phrases and their switch -----------------------------------


def test_the_generated_file_lists_every_phrase():
    text = render_default_settings()
    table = text.split("[irreversible]")[1].split("[rules]")[0]
    for phrase in IRREVERSIBLE_PHRASES:
        assert f'    "{phrase}",\n' in table
    assert "phrases = [" in table


def test_a_missing_irreversible_table_or_switch_is_refused(tmp_path):
    text = render_default_settings()
    head, _, tail = text.partition("[irreversible]")
    _, _, rest = tail.partition("[rules]")
    refused(write(tmp_path, head + "[rules]" + rest), "irreversible")
    refused(
        edited(tmp_path, "refuse_irreversible_screens = true\n", ""),
        "refuse_irreversible_screens",
    )
    refused(
        edited(
            tmp_path,
            "refuse_irreversible_screens = true",
            "refuse_irreversible_screens = 1",
        ),
        "refuse_irreversible_screens",
    )


def test_a_phrase_is_removed_after_the_apostrophe_is_normalized(tmp_path):
    loaded = load_settings(with_remove(tmp_path, 2, '["Can\u2019t Undo"]'))
    assert "can't undo" not in loaded.irreversible_phrases
    assert "cannot undo" in loaded.irreversible_phrases


def test_an_empty_phrase_is_refused(tmp_path):
    refused(
        edited(tmp_path, '    "permanently",\n', '    "  ",\n'),
        "irreversible",
    )


# -- files written before defaults version 2 ---------------------------------------


def v1_text() -> str:
    text = V1_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 1\n" in text
    assert "[irreversible]" not in text
    assert "refuse_irreversible_screens" not in text
    assert '"undo"' not in text
    return text


def test_an_unchanged_version_1_file_loads_with_the_version_2_defaults(tmp_path):
    loaded = load_settings(write(tmp_path, v1_text()))
    assert loaded.defaults_version == 1
    assert loaded.refuse_irreversible_screens is True
    assert loaded.irreversible_phrases == IRREVERSIBLE_PHRASES
    assert set(loaded.deny_words) == set(DEFAULT_DENY_WORDS)
    for word in V2_DENY:
        assert word in loaded.deny_words
    assert loaded.confirm_words == CONFIRM_WORDS
    assert set(loaded.refused_keys) == DEVICE_WIDE_KEYS
    assert loaded.warnings == ()
    policy = settings.policy_from_settings(loaded)
    assert policy.skip_irreversible is True
    assert policy.irreversible_phrases == IRREVERSIBLE_PHRASES


def test_a_version_1_file_keeps_a_removed_new_word_out(tmp_path):
    text = v1_text().replace("remove = []", 'remove = ["UNDO"]', 1)  # deny
    loaded = load_settings(write(tmp_path, text))
    assert "undo" not in loaded.deny_words
    for word in ("restart", "やり直", "元に戻"):
        assert word in loaded.deny_words


def test_a_version_1_file_may_add_the_new_table_and_rule(tmp_path):
    text = v1_text().replace(
        "[rules]\n",
        '[irreversible]\nremove = ["permanently", "can not be undone"]\n\n'
        "[rules]\nrefuse_irreversible_screens = false\n",
    )
    loaded = load_settings(write(tmp_path, text))
    assert loaded.refuse_irreversible_screens is False
    assert "permanently" not in loaded.irreversible_phrases
    assert "cannot be undone" not in loaded.irreversible_phrases
    assert "irreversible" in loaded.irreversible_phrases
    listed = v1_text().replace(
        "[rules]\n", '[irreversible]\nphrases = ["no way back"]\n\n[rules]\n'
    )
    phrases = load_settings(write(tmp_path, listed)).irreversible_phrases
    assert phrases == ("no way back", *IRREVERSIBLE_PHRASES)


@pytest.mark.parametrize(
    ("added", "needles"),
    [
        ('[irreversible]\nwords = ["x"]\n', ("[irreversible] words", "unknown key")),
        ('[irreversible]\nphrases = "x"\n', ("[irreversible] phrases", "list")),
        ('[irreversible]\nphrases = ["  "]\n', ("[irreversible] phrases",)),
        ("[irreversible]\nremove = [1]\n", ("[irreversible] remove", "strings")),
        ("irreversible = 1\n", ("[irreversible]", "must be a table")),
        (
            "[rules]\nrefuse_irreversible_screens = 1\n",
            ("refuse_irreversible_screens", "true or false"),
        ),
        ("[rules]\nrefuse_everything = true\n", ("refuse_everything", "unknown")),
    ],
)
def test_a_version_1_file_is_still_read_strictly(tmp_path, added, needles):
    head, _, tail = v1_text().partition("[rules]\n")
    if added.startswith("[rules]\n"):
        text = head + added + tail
    elif added.startswith("[irreversible]"):
        text = head + added + "\n[rules]\n" + tail
    else:
        text = added + v1_text()
    refused(write(tmp_path, text), *needles)


def test_a_version_2_file_needs_the_new_table_and_rule(tmp_path):
    text = v1_text().replace("defaults_version = 1", "defaults_version = 2")
    refused(write(tmp_path, text), "[irreversible]", "missing")


# -- irreversibility phrases that match the same texts -------------------------------


def test_spellings_of_one_phrase_are_one_entry(tmp_path):
    for spelling in ("can not be undone", "CANNOT-BE-UNDONE", "cannot be undone"):
        loaded = load_settings(with_remove(tmp_path, 2, f'["{spelling}"]'))
        assert "cannot be undone" not in loaded.irreversible_phrases, spelling
        assert "can't be undone" in loaded.irreversible_phrases
    refused(
        edited(
            tmp_path,
            '    "cannot be undone",\n',
            '    "cannot be undone",\n    "can not be undone",\n',
        ),
        "[irreversible] phrases",
        "duplicate entry",
    )


@pytest.mark.parametrize(
    ("key", "items"),
    [
        ("phrases", '["-"]'),
        ("remove", '["_"]'),
        ("phrases", '["- _ . / :"]'),
        ("remove", '["\u200b-\u2014"]'),
    ],
)
def test_a_phrase_of_only_separators_is_refused_in_a_version_1_file(
    tmp_path, key, items
):
    head, _, tail = v1_text().partition("[rules]\n")
    text = head + f"[irreversible]\n{key} = {items}\n\n[rules]\n" + tail
    refused(
        write(tmp_path, text),
        f"[irreversible] {key}",
        "is empty after removing spaces",
    )


def separator_phrase(text: str, key: str) -> str:
    """`text` with one separator-only entry in `[irreversible]` `key`."""
    head, marker, table = text.partition("[irreversible]\n")
    assert marker
    if key == "phrases":
        old, new = '    "permanently",\n', '    "-",\n'
    else:
        old, new = "remove = []", 'remove = ["_"]'
    assert old in table.split("\n[", 1)[0]
    return head + marker + table.replace(old, new, 1)


@pytest.mark.parametrize("key", ["phrases", "remove"])
def test_a_phrase_of_only_separators_is_refused_in_a_version_2_file(tmp_path, key):
    # The pinned file that `uiprobe init` wrote at defaults version 2.
    path = write(tmp_path, separator_phrase(v2_text(), key))
    refused(path, f"[irreversible] {key}", "is empty after removing spaces")


@pytest.mark.parametrize("key", ["phrases", "remove"])
def test_a_phrase_of_only_separators_is_refused_in_a_version_3_file(tmp_path, key):
    path = write(tmp_path, separator_phrase(render_default_settings(), key))
    refused(path, f"[irreversible] {key}", "is empty after removing spaces")


def test_no_two_default_phrases_match_the_same_texts():
    keys = [guards._phrase_key(phrase) for phrase in IRREVERSIBLE_PHRASES]
    assert len(set(keys)) == len(keys)


# -- the exception words ([deny] except, no built-in words) --------------------------


def v2_text() -> str:
    text = V2_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 2\n" in text
    assert "except" not in text
    assert "[irreversible]" in text
    return text


def with_except(tmp_path: Path, items: str, text: str | None = None) -> Path:
    """Write the default file (or `text`) with `[deny] except` set to `items`."""
    text = render_default_settings() if text is None else text
    assert text.count("except = []\n") == 1
    return write(tmp_path, text.replace("except = []\n", f"except = {items}\n"))


def test_the_generated_file_has_an_empty_except_list():
    text = render_default_settings()
    table = text.split("[deny]")[1].split("[confirm]")[0]
    assert (
        table.index("words = [")
        < table.index("except = []")
        < table.index("remove = [")
    )
    assert tomllib.loads(text)["deny"]["except"] == []
    assert DEFAULT_DENY_EXCEPTIONS == ()


def test_the_generated_file_holds_the_version_2_values_plus_the_later_keys():
    # The values are those `uiprobe init` wrote at defaults version 2
    # (tests/data/settings_v2.toml), plus the empty except list and, with
    # version 3, the two row rules and, with version 4, the up-button and
    # slider rules and, with version 5, the replay rule and, with version 6, the
    # record_text key and, with version 7, the state-changing deny words and,
    # with version 10, the back_on_other_screens rule and, with version 11, the
    # scroll rule and its three limits (version 12 added two confirm words).
    current = tomllib.loads(render_default_settings())
    assert current["deny"].pop("except") == []
    assert current.pop("defaults_version") == 12
    assert current["rules"].pop("back_on_other_screens") is False
    assert current["rules"].pop("scroll_lists") is False
    assert current["limits"].pop("scrolls_per_screen") == 10
    assert current["limits"].pop("scroll_amount") == 3
    assert current["limits"].pop("scroll_incidents") == 2
    assert current.pop("record_text") is True
    assert current["rules"].pop("replay_path") is True
    assert current["rules"].pop("skip_two_target_rows") is True
    assert current["rules"].pop("skip_rows_with_buttons") is False
    assert current["rules"].pop("skip_navigate_up") is True
    assert current["rules"].pop("skip_sliders") is True
    words = current["deny"]["words"]
    assert tuple(words[-len(V7_DENY) :]) == V7_DENY
    del words[-len(V7_DENY) :]
    confirm = current["confirm"]["words"]
    assert tuple(confirm[-len(V12_CONFIRM) :]) == V12_CONFIRM
    del confirm[-len(V12_CONFIRM) :]
    assert tuple(confirm[-len(V9_CONFIRM) :]) == V9_CONFIRM
    del confirm[-len(V9_CONFIRM) :]
    older = tomllib.loads(v2_text())
    assert older.pop("defaults_version") == 2
    assert current == older


@pytest.mark.parametrize("version", [1, 2])
def test_an_unchanged_older_file_has_no_exceptions(tmp_path, version):
    text = v1_text() if version == 1 else v2_text()
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == version
    assert loaded.deny_exceptions == ()
    assert set(loaded.deny_words) == set(DEFAULT_DENY_WORDS)
    assert loaded.warnings == ()
    assert settings.policy_from_settings(loaded).deny_exceptions == ()


def test_the_except_list_may_be_left_out_of_a_current_file(tmp_path):
    text = render_default_settings().replace("except = []\n", "")
    assert load_settings(write(tmp_path, text)).deny_exceptions == ()


@pytest.mark.parametrize("version", [1, 2])
def test_an_older_file_may_add_the_except_list(tmp_path, version):
    text = v1_text() if version == 1 else v2_text()
    text = text.replace("remove = []", 'except = ["Automatically"]\nremove = []', 1)
    loaded = load_settings(write(tmp_path, text))
    assert loaded.deny_exceptions == ("Automatically",)
    policy = settings.policy_from_settings(loaded)
    assert not guards.word_matches(
        "Automatically", "call", exceptions=policy.deny_exceptions
    )


def test_remove_takes_an_exception_out(tmp_path):
    text = render_default_settings().replace("remove = []", 'remove = ["BORDER"]', 1)
    loaded = load_settings(with_except(tmp_path, '["border", "posture"]', text))
    assert loaded.deny_exceptions == ("posture",)
    assert loaded.deny_words == DEFAULT_DENY_WORDS


def test_a_listed_exception_equal_to_a_deny_word_is_refused(tmp_path):
    message = refused(
        with_except(tmp_path, '["border", "Call"]'),
        "[deny] except",
        "equals the deny word 'call'",
        "remove the deny word instead",
    )
    assert "'Call'" in message
    refused(with_except(tmp_path, '["auto-pay"]'), "[deny] except", "one word")


def test_a_later_builtin_exception_needs_a_new_defaults_version():
    assert settings._FIRST_EXCEPT > settings.DEFAULTS_VERSION
    assert settings._DENY_EXCEPTIONS == ()


def test_a_later_builtin_exception_equal_to_a_deny_word_is_dropped(
    monkeypatch, tmp_path
):
    # A built-in exception that a later version might add reaches an older
    # file automatically; when the file lists it as a deny word, it is
    # dropped with a warning (the deny word wins).
    monkeypatch.setattr(settings, "DEFAULTS_VERSION", NEXT)
    monkeypatch.setattr(
        settings, "_DENY_EXCEPTIONS", ((NEXT, "automatically"), (NEXT, "border"))
    )
    text = v2_text().replace("words = [\n", 'words = [\n    "Automatically",\n', 1)
    loaded = load_settings(write(tmp_path, text))
    assert "Automatically" in loaded.deny_words
    assert loaded.deny_exceptions == ("border",)
    assert loaded.warnings == (
        "[deny] the default exception 'automatically' is not applied because it"
        " equals the deny word 'Automatically'",
    )


@pytest.mark.parametrize(
    ("items", "needle"),
    [
        ('["auto matically"]', "one word of letters and digits"),
        ('["border!"]', "one word of letters and digits"),
        ('["  "]', "empty"),
        ('["Border", "border"]', "duplicate entry"),
        ('"border"', "list of strings"),
        ("[1]", "strings"),
    ],
)
def test_bad_exception_entries_are_refused(tmp_path, items, needle):
    refused(with_except(tmp_path, items), "[deny] except", needle)


def test_an_added_deny_word_keeps_blocking_over_an_exception(tmp_path):
    loaded = load_settings(with_except(tmp_path, '["automatically", "border"]'))
    policy = settings.policy_from_settings(loaded, extra_deny=["automatically"])
    assert policy.deny_exceptions == ("border",)
    assert guards.word_matches(
        "Automatically", "automatically", exceptions=policy.deny_exceptions
    )


@pytest.mark.parametrize("word", ["Pay\\ufe0f", "border\\u200b"])
def test_an_exception_with_an_invisible_character_is_refused_in_the_file(
    tmp_path, word
):
    refused(
        with_except(tmp_path, f'["{word}"]'), "[deny] except", "invisible character"
    )


# Every field of Settings at 4308851, in order (before [deny] except).
OLD_SETTINGS_FIELDS = (
    "source",
    "format_version",
    "defaults_version",
    "press",
    "deny_words",
    "confirm_words",
    "irreversible_phrases",
    "tap_unlabeled",
    "skip_number_only",
    "skip_toggles",
    "refuse_dialog_confirm",
    "refuse_irreversible_screens",
    "min_visible_px",
    "edge_top_px",
    "edge_bottom_px",
    "edge_side_px",
    "depth",
    "actions",
    "minutes",
    "refused_keys",
    "refused_key_prefixes",
    "warnings",
)


def test_settings_built_with_the_earlier_fields_still_work(tmp_path):
    loaded = load_settings(default_file(tmp_path))
    values = {name: getattr(loaded, name) for name in OLD_SETTINGS_FIELDS}
    by_keyword = settings.Settings(**values)
    by_position = settings.Settings(*values.values())
    assert by_keyword == by_position == loaded
    assert by_keyword.deny_exceptions == ()
    assert by_position.confirm_words == CONFIRM_WORDS
    assert settings.policy_from_settings(by_position).deny_exceptions == ()
    assert [f.name for f in dataclasses.fields(settings.Settings)] == [
        *OLD_SETTINGS_FIELDS,
        "deny_exceptions",
        "skip_two_target_rows",
        "skip_rows_with_buttons",
        "skip_navigate_up",
        "skip_sliders",
        "runs_dir",
        "replay_path",
        "record_text",
        "also_target",
        "back_on_other_screens",
        "scroll_lists",
        "scrolls_per_screen",
        "scroll_amount",
        "scroll_incidents",
        "dialog_exact_labels",
    ]


# -- the row rules (defaults version 3) ----------------------------------------------


@pytest.mark.parametrize("version", [1, 2])
def test_an_older_file_without_the_row_rules_loads_their_defaults(tmp_path, version):
    text = v1_text() if version == 1 else v2_text()
    assert "skip_two_target_rows" not in text
    assert "skip_rows_with_buttons" not in text
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == version
    assert loaded.skip_two_target_rows is True
    assert loaded.skip_rows_with_buttons is False
    assert loaded.warnings == ()
    policy = settings.policy_from_settings(loaded)
    assert (policy.skip_two_target_rows, policy.skip_rows_with_buttons) == (True, False)


@pytest.mark.parametrize("version", [1, 2])
def test_an_older_file_may_set_the_row_rules(tmp_path, version):
    text = v1_text() if version == 1 else v2_text()
    text = text.replace(
        "[rules]\n",
        "[rules]\nskip_two_target_rows = false\nskip_rows_with_buttons = true\n",
        1,
    )
    loaded = load_settings(write(tmp_path, text))
    assert (loaded.skip_two_target_rows, loaded.skip_rows_with_buttons) == (False, True)


def test_a_current_file_gives_the_row_rules_as_written(tmp_path):
    text = (
        render_default_settings()
        .replace("skip_two_target_rows = true", "skip_two_target_rows = false")
        .replace("skip_rows_with_buttons = false", "skip_rows_with_buttons = true")
    )
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == CURRENT
    assert (loaded.skip_two_target_rows, loaded.skip_rows_with_buttons) == (False, True)
    policy = settings.policy_from_settings(loaded)
    assert (policy.skip_two_target_rows, policy.skip_rows_with_buttons) == (False, True)


@pytest.mark.parametrize("key", ["skip_two_target_rows", "skip_rows_with_buttons"])
def test_a_current_file_needs_the_row_rules(tmp_path, key):
    text = render_default_settings()
    line = next(
        line for line in text.splitlines(keepends=True) if line.startswith(f"{key} =")
    )
    refused(write(tmp_path, text.replace(line, "")), f"[rules] {key}", "is missing")


def test_the_generated_file_writes_both_row_rules():
    text = render_default_settings()
    assert "defaults_version = 12\n" in text
    rules = text.split("[rules]\n")[1].split("[limits]")[0]
    assert "skip_two_target_rows = true\n" in rules
    assert "skip_rows_with_buttons = false\n" in rules
    assert tomllib.loads(text)["rules"]["skip_two_target_rows"] is True


def test_a_newer_defaults_version_is_refused_with_its_number(tmp_path):
    # What an older uiprobe says about a file that `uiprobe init` writes now
    # (the message of the defaults_version range check names both numbers).
    text = render_default_settings().replace(
        "defaults_version = 12", "defaults_version = 13"
    )
    refused(write(tmp_path, text), "defaults_version", "must be from 1 to 12, not 13")


def test_version_3_added_no_list_defaults():
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 3
        ] == []
    assert settings._LATER_RULES["skip_two_target_rows"] == 3
    assert settings._LATER_RULES["skip_rows_with_buttons"] == 3


# -- the up-button and slider rules (defaults version 4) -----------------------------

V4_RULES = ["skip_navigate_up", "skip_sliders"]


def v3_text() -> str:
    text = V3_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 3\n" in text
    assert "skip_two_target_rows = true\n" in text
    assert "skip_navigate_up" not in text
    assert "skip_sliders" not in text
    return text


def test_the_version_3_file_loads_as_the_current_defaults(tmp_path):
    loaded = load_settings(write(tmp_path, v3_text()))
    assert loaded.defaults_version == 3
    assert loaded.warnings == ()
    (tmp_path / "now").mkdir()
    current = load_settings(default_file(tmp_path / "now"))
    assert (
        dataclasses.replace(loaded, source=current.source, defaults_version=CURRENT)
        == current
    )


@pytest.mark.parametrize("version", [1, 2, 3])
def test_an_older_file_without_the_version_4_rules_loads_them_on(tmp_path, version):
    text = (v1_text, v2_text, v3_text)[version - 1]()
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == version
    assert (loaded.skip_navigate_up, loaded.skip_sliders) == (True, True)
    assert loaded.warnings == ()
    policy = settings.policy_from_settings(loaded)
    assert (policy.skip_navigate_up, policy.skip_sliders) == (True, True)


@pytest.mark.parametrize("key", V4_RULES)
@pytest.mark.parametrize("version", [1, 2, 3])
def test_an_older_file_may_turn_a_version_4_rule_off(tmp_path, version, key):
    text = (v1_text, v2_text, v3_text)[version - 1]()
    text = text.replace("[rules]\n", f"[rules]\n{key} = false\n", 1)
    loaded = load_settings(write(tmp_path, text))
    assert getattr(loaded, key) is False
    assert getattr(settings.policy_from_settings(loaded), key) is False
    other = next(k for k in V4_RULES if k != key)
    assert getattr(loaded, other) is True


@pytest.mark.parametrize("key", V4_RULES)
def test_a_version_4_file_needs_each_version_4_rule(tmp_path, key):
    text = render_default_settings()
    assert f"{key} = true\n" in text
    refused(
        write(tmp_path, text.replace(f"{key} = true\n", "")),
        f"[rules] {key}",
        "is missing",
    )


@pytest.mark.parametrize("key", V4_RULES)
def test_a_version_4_file_gives_each_version_4_rule_as_written(tmp_path, key):
    text = render_default_settings().replace(f"{key} = true", f"{key} = false")
    loaded = load_settings(write(tmp_path, text))
    assert getattr(loaded, key) is False
    assert getattr(settings.policy_from_settings(loaded), key) is False


@pytest.mark.parametrize("key", V4_RULES)
def test_a_version_4_rule_must_be_a_bool(tmp_path, key):
    text = render_default_settings().replace(f"{key} = true", f"{key} = 1")
    refused(write(tmp_path, text), f"[rules] {key}")


def test_the_generated_file_writes_the_version_4_rules_in_the_rules_table():
    text = render_default_settings()
    rules = text.split("[rules]\n")[1].split("[limits]")[0]
    assert "skip_navigate_up = true\n" in rules
    assert "skip_sliders = true\n" in rules


def test_version_4_added_no_list_defaults():
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 4
        ] == []
    for key in V4_RULES:
        assert settings._LATER_RULES[key] == 4
        assert settings._LATER_RULE_VALUES[key] is True


# -- the replay rule (defaults version 5) --------------------------------------------


def v4_text() -> str:
    text = V4_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 4\n" in text
    assert "skip_sliders = true\n" in text
    assert "replay_path" not in text
    return text


def test_the_version_4_file_loads_as_the_current_defaults(tmp_path):
    loaded = load_settings(write(tmp_path, v4_text()))
    assert loaded.defaults_version == 4
    assert loaded.warnings == ()
    (tmp_path / "now").mkdir()
    current = load_settings(default_file(tmp_path / "now"))
    assert (
        dataclasses.replace(loaded, source=current.source, defaults_version=CURRENT)
        == current
    )


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_an_older_file_without_replay_path_loads_it_on(tmp_path, version):
    text = (v1_text, v2_text, v3_text, v4_text)[version - 1]()
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == version
    assert loaded.replay_path is True
    assert loaded.warnings == ()
    assert settings.policy_from_settings(loaded).replay_path is True


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_an_older_file_may_turn_replay_path_off(tmp_path, version):
    text = (v1_text, v2_text, v3_text, v4_text)[version - 1]()
    text = text.replace("[rules]\n", "[rules]\nreplay_path = false\n", 1)
    loaded = load_settings(write(tmp_path, text))
    assert loaded.replay_path is False
    assert settings.policy_from_settings(loaded).replay_path is False


def test_a_version_5_file_needs_replay_path(tmp_path):
    text = render_default_settings()
    assert "replay_path = true\n" in text
    refused(
        write(tmp_path, text.replace("replay_path = true\n", "")),
        "[rules] replay_path",
        "is missing",
    )


def test_a_version_5_file_gives_replay_path_as_written(tmp_path):
    text = render_default_settings().replace(
        "replay_path = true", "replay_path = false"
    )
    loaded = load_settings(write(tmp_path, text))
    assert loaded.replay_path is False
    assert settings.policy_from_settings(loaded).replay_path is False


def test_replay_path_must_be_a_bool(tmp_path):
    text = render_default_settings().replace("replay_path = true", "replay_path = 1")
    refused(write(tmp_path, text), "[rules] replay_path")


def test_the_generated_file_writes_replay_path_in_the_rules_table():
    text = render_default_settings()
    rules = text.split("[rules]\n")[1].split("[limits]")[0]
    assert "replay_path = true\n" in rules
    # the comment above it says what false returns to the earlier behaviour
    comment = rules.split("replay_path = true")[0]
    assert "returns both the path replay" in comment
    assert "when only a badge changed" in comment


def test_version_5_added_no_list_defaults():
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 5
        ] == []
    assert settings._LATER_RULES["replay_path"] == 5
    assert settings._LATER_RULE_VALUES["replay_path"] is True


def test_policy_replay_path_defaults_on_and_must_be_a_bool():
    from uiprobe.guards import Policy

    assert Policy().replay_path is True
    assert Policy().with_deny_words(["zzz"]).replay_path is True
    assert Policy(replay_path=False).with_deny_words(["zzz"]).replay_path is False
    with pytest.raises(TypeError, match="replay_path must be a bool"):
        Policy(replay_path=1)  # ty: ignore[invalid-argument-type]


def test_replay_path_comes_after_runs_dir_so_positional_settings_still_work(tmp_path):
    names = [f.name for f in dataclasses.fields(settings.Settings)]
    assert names[-10:] == [
        "runs_dir",
        "replay_path",
        "record_text",
        "also_target",
        "back_on_other_screens",
        "scroll_lists",
        "scrolls_per_screen",
        "scroll_amount",
        "scroll_incidents",
        "dialog_exact_labels",
    ]
    loaded = load_settings(default_file(tmp_path))
    # Built by position with every field up to runs_dir, as before version 5:
    # replay_path takes its default and runs_dir keeps its place.
    values = [getattr(loaded, name) for name in names[:-8]]
    by_position = settings.Settings(*values)
    assert by_position.replay_path is True
    assert by_position.record_text is True
    assert by_position.scroll_lists is False
    assert by_position.scrolls_per_screen == 10
    assert by_position.runs_dir == loaded.runs_dir
    assert by_position == loaded
    off = dataclasses.replace(loaded, replay_path=False)
    assert settings.policy_from_settings(off).replay_path is False


# -- the record_text key (defaults version 6) ----------------------------------------


def v5_text() -> str:
    text = V5_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 5\n" in text
    assert "replay_path = true\n" in text
    assert "record_text" not in text
    return text


def all_older_texts():
    return (v1_text, v2_text, v3_text, v4_text, v5_text)


def test_the_version_5_file_loads_as_the_current_defaults(tmp_path):
    loaded = load_settings(write(tmp_path, v5_text()))
    assert loaded.defaults_version == 5
    assert loaded.warnings == ()
    (tmp_path / "now").mkdir()
    current = load_settings(default_file(tmp_path / "now"))
    assert (
        dataclasses.replace(loaded, source=current.source, defaults_version=CURRENT)
        == current
    )


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_an_older_file_without_record_text_loads_it_on(tmp_path, version):
    loaded = load_settings(write(tmp_path, all_older_texts()[version - 1]()))
    assert loaded.defaults_version == version
    assert loaded.record_text is True
    assert loaded.warnings == ()


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_an_older_file_may_turn_record_text_off(tmp_path, version):
    text = "record_text = false\n" + all_older_texts()[version - 1]()
    loaded = load_settings(write(tmp_path, text))
    assert loaded.record_text is False


def test_the_generated_file_is_the_current_version_and_writes_record_text_on_top():
    text = render_default_settings()
    assert settings.DEFAULTS_VERSION == 12
    assert "defaults_version = 12\n" in text
    top = text.split("[deny]")[0]
    assert "record_text = true\n" in top
    assert tomllib.loads(text)["record_text"] is True


def test_a_version_6_file_needs_record_text(tmp_path):
    text = render_default_settings()
    assert text.count("record_text = true\n") == 1
    refused(
        write(tmp_path, text.replace("record_text = true\n", "")),
        "record_text",
        "is missing",
    )


def test_a_version_6_file_gives_record_text_as_written(tmp_path):
    text = render_default_settings().replace(
        "record_text = true", "record_text = false"
    )
    loaded = load_settings(write(tmp_path, text))
    assert loaded.record_text is False
    assert loaded.defaults_version == CURRENT


@pytest.mark.parametrize("value", ["1", '"false"', "0", "[]"])
def test_record_text_must_be_a_bool(tmp_path, value):
    text = render_default_settings().replace(
        "record_text = true", f"record_text = {value}"
    )
    refused(write(tmp_path, text), "record_text", "must be true or false")


def test_record_text_is_not_a_rules_key(tmp_path):
    text = render_default_settings().replace(
        "[rules]\n", "[rules]\nrecord_text = true\n", 1
    )
    refused(write(tmp_path, text), "rules")


def test_version_6_added_no_list_defaults():
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 6
        ] == []
    assert settings._LATER_TOP == {"record_text": 6}
    assert settings._FIRST_EXCEPT == 13


def test_record_text_is_not_a_policy_field():
    assert "record_text" not in {f.name for f in dataclasses.fields(guards.Policy)}


# -- the state-changing deny words (defaults version 7) ------------------------------


def v6_text() -> str:
    text = V6_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 6\n" in text
    assert "record_text = true\n" in text
    assert '"forget"' not in text
    return text


def test_the_version_7_defaults_are_pinned():
    version_7 = tuple(item for version, item in settings._DENY_WORDS if version == 7)
    assert version_7 == V7_DENY
    for table in (
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 7
        ] == []
    assert settings._FIRST_EXCEPT == 13


def test_the_generated_file_lists_the_version_7_words(tmp_path):
    loaded = load_settings(default_file(tmp_path))
    assert loaded.defaults_version == CURRENT
    for word in V7_DENY:
        assert word in loaded.deny_words


def test_a_version_6_file_gets_the_version_7_words(tmp_path):
    loaded = load_settings(write(tmp_path, v6_text()))
    assert loaded.defaults_version == 6
    assert loaded.warnings == ()
    for word in V7_DENY:
        assert word in loaded.deny_words
    assert set(loaded.deny_words) == set(DEFAULT_DENY_WORDS)
    (tmp_path / "now").mkdir()
    current = load_settings(default_file(tmp_path / "now"))
    assert (
        dataclasses.replace(loaded, source=current.source, defaults_version=CURRENT)
        == current
    )
    policy = settings.policy_from_settings(loaded)
    assert "forget" in policy.deny_words


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_an_older_file_gets_the_version_7_words(tmp_path, version):
    loaded = load_settings(write(tmp_path, all_older_texts()[version - 1]()))
    assert loaded.defaults_version == version
    for word in V7_DENY:
        assert word in loaded.deny_words


def test_a_version_6_file_keeps_a_removed_version_7_word_out(tmp_path):
    text = v6_text().replace("remove = []", 'remove = ["Forget"]', 1)  # deny
    loaded = load_settings(write(tmp_path, text))
    assert "forget" not in loaded.deny_words
    for word in V7_DENY:
        if word != "forget":
            assert word in loaded.deny_words


def test_a_version_7_file_without_a_version_7_word_does_not_get_it_back(tmp_path):
    text = render_default_settings().replace('    "forget",\n', "", 1)
    assert '"forget"' not in text
    loaded = load_settings(write(tmp_path, text))
    assert "forget" not in loaded.deny_words


def test_a_version_6_file_with_a_version_7_word_as_exception_names_both_remedies(
    tmp_path,
):
    text = v6_text().replace("except = []", 'except = ["forget"]', 1)
    message = refused(write(tmp_path, text), "[deny] except", "forget")
    assert "defaults version" in message
    assert "drop the word from [deny] except" in message
    assert "add it to [deny] remove" in message


def test_both_remedies_for_a_version_7_word_used_as_exception_load(tmp_path):
    dropped = load_settings(write(tmp_path, v6_text()))
    assert "forget" in dropped.deny_words
    assert "forget" not in dropped.deny_exceptions
    text = v6_text().replace("except = []", 'except = ["forget"]', 1)
    text = text.replace("remove = []", 'remove = ["forget"]', 1)  # deny
    kept = load_settings(write(tmp_path, text))
    assert "forget" not in kept.deny_words
    assert "forget" not in kept.deny_exceptions


# -- the confirm words that turn a setting on (defaults version 9) -------------------


def v8_text() -> str:
    text = V8_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 8\n" in text
    assert '"turn on"' not in text
    return text


def test_the_version_9_defaults_are_pinned():
    version_9 = tuple(item for version, item in settings._CONFIRM_WORDS if version == 9)
    assert version_9 == V9_CONFIRM
    assert settings.DEFAULTS_VERSION == 12
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 9
        ] == []
    assert settings._FIRST_EXCEPT == 13


def test_the_generated_file_lists_the_version_9_words(tmp_path):
    loaded = load_settings(default_file(tmp_path))
    assert loaded.defaults_version == 12
    for word in V9_CONFIRM:
        assert word in loaded.confirm_words


def test_a_version_8_file_gets_the_version_9_words(tmp_path):
    loaded = load_settings(write(tmp_path, v8_text()))
    assert loaded.defaults_version == 8
    assert loaded.warnings == ()
    for word in V9_CONFIRM:
        assert word in loaded.confirm_words
    assert set(loaded.confirm_words) == set(CONFIRM_WORDS)
    (tmp_path / "now").mkdir()
    current = load_settings(default_file(tmp_path / "now"))
    assert (
        dataclasses.replace(loaded, source=current.source, defaults_version=CURRENT)
        == current
    )
    policy = settings.policy_from_settings(loaded)
    assert "turn on" in policy.confirm_words


def test_a_version_8_file_keeps_a_removed_version_9_word_out(tmp_path):
    head, confirm = v8_text().split("[confirm]")
    text = head + "[confirm]" + confirm.replace("remove = []", 'remove = ["Enable"]', 1)
    loaded = load_settings(write(tmp_path, text))
    assert "enable" not in loaded.confirm_words
    for word in V9_CONFIRM:
        if word != "enable":
            assert word in loaded.confirm_words


def test_a_version_9_file_without_a_version_9_word_does_not_get_it_back(tmp_path):
    text = render_default_settings().replace('    "turn on",\n', "", 1)
    assert '"turn on"' not in text
    loaded = load_settings(write(tmp_path, text))
    assert "turn on" not in loaded.confirm_words


# -- the BACK rule (defaults version 10) ----------------------------------------------

BACK_COMMENT = (
    "# With true, a pressing crawl presses BACK on another app's screen (up to\n"
    "# twice before it restarts the app) instead of restarting at once; that\n"
    "# BACK reaches whichever window has the input focus, not necessarily the\n"
    "# app being crawled. --back-on-other-screens and --no-back-on-other-screens\n"
    "# override it for one run.\n"
    "back_on_other_screens = false\n"
)


def version_text(version: int) -> str:
    """The file `uiprobe init` wrote at `version` (the current one is rendered)."""
    if version == CURRENT:
        return render_default_settings()
    text = (DATA / f"settings_v{version}.toml").read_text(encoding="utf-8")
    assert f"defaults_version = {version}\n" in text
    if version < 9:
        assert "back_on_other_screens" not in text
    return text


@pytest.mark.parametrize("version", range(1, 13))
def test_every_defaults_version_reads_back_on_other_screens_off(tmp_path, version):
    loaded = load_settings(write(tmp_path, version_text(version)))
    assert loaded.defaults_version == version
    assert loaded.back_on_other_screens is False
    assert loaded.warnings == ()
    assert settings.policy_from_settings(loaded).back_on_other_screens is False


@pytest.mark.parametrize("version", range(1, 12))
def test_every_defaults_version_may_turn_it_on(tmp_path, version):
    text = version_text(version)
    on = "back_on_other_screens = true\n"
    if version >= 10:
        text = text.replace("back_on_other_screens = false\n", on)
    else:
        text = text.replace("[rules]\n", "[rules]\n" + on, 1)
    loaded = load_settings(write(tmp_path, text))
    assert loaded.back_on_other_screens is True
    assert settings.policy_from_settings(loaded).back_on_other_screens is True


def test_a_version_10_file_needs_back_on_other_screens(tmp_path):
    text = render_default_settings()
    refused(
        write(tmp_path, text.replace("back_on_other_screens = false\n", "")),
        "[rules] back_on_other_screens",
        "is missing",
    )


def test_back_on_other_screens_must_be_a_bool(tmp_path):
    text = render_default_settings().replace(
        "back_on_other_screens = false", "back_on_other_screens = 1"
    )
    refused(write(tmp_path, text), "[rules] back_on_other_screens")


def test_the_generated_file_writes_back_on_other_screens_with_its_comment():
    text = render_default_settings()
    rules = text.split("[rules]\n")[1].split("[limits]")[0]
    assert BACK_COMMENT in rules
    assert tomllib.loads(text)["rules"]["back_on_other_screens"] is False


def test_version_10_added_no_list_defaults():
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 10
        ] == []
    assert settings._LATER_RULES["back_on_other_screens"] == 10
    assert settings._LATER_RULE_VALUES["back_on_other_screens"] is False


# -- the scroll rule and its limits (defaults version 10) ---------------------------

SCROLL_COMMENT = (
    "# With true, a pressing crawl turns a mouse wheel over a list (content\n"
    "# further down comes up) on a screen with nothing left to press, to reach\n"
    "# the rows below the fold; at most scrolls_per_screen times per screen\n"
    "# (see the limits below), and only when the device lists the wheel scroll.\n"
    "# The wheel goes only to a ListView, ScrollView, RecyclerView or\n"
    "# NestedScrollView at a point where everything is a plain layout or label.\n"
    "# An app's own view of such a class can still react to the wheel (see\n"
    "# docs/safety.md). --scroll and --no-scroll override it for one run.\n"
    "scroll_lists = false\n"
)


def test_the_generated_file_writes_scroll_lists_off_and_its_limits():
    text = render_default_settings()
    parsed = tomllib.loads(text)
    assert parsed["rules"]["scroll_lists"] is False
    assert parsed["limits"]["scrolls_per_screen"] == 10
    assert parsed["limits"]["scroll_amount"] == 3
    assert parsed["limits"]["scroll_incidents"] == 2
    assert SCROLL_COMMENT in text.split("[rules]\n")[1].split("[limits]")[0]
    limits = text.split("[limits]\n")[1].split("[keys]")[0]
    assert "scrolls_per_screen = 10\n" in limits
    assert "scroll_amount = 3\n" in limits
    assert "scroll_incidents = 2\n" in limits


def test_every_default_of_scrolling_is_one_constant(monkeypatch):
    # The policy, the settings, the limits and the generated file all read the
    # constants of uiprobe.guards; the file's text follows them.
    from uiprobe.crawl import Limits

    assert guards.DEFAULT_SCROLL_LISTS is False
    assert guards.Policy().scroll_lists is guards.DEFAULT_SCROLL_LISTS
    fields = settings.Settings.__dataclass_fields__
    assert fields["scroll_lists"].default is guards.DEFAULT_SCROLL_LISTS
    assert settings._LATER_RULE_VALUES["scroll_lists"] is guards.DEFAULT_SCROLL_LISTS
    for name, constant in (
        ("scrolls_per_screen", guards.DEFAULT_SCROLLS_PER_SCREEN),
        ("scroll_amount", guards.DEFAULT_SCROLL_AMOUNT),
        ("scroll_incidents", guards.DEFAULT_SCROLL_INCIDENTS),
    ):
        assert fields[name].default == constant
        assert settings._LATER_LIMIT_VALUES[name] == constant
        assert getattr(Limits(), name) == constant
    assert (
        guards.DEFAULT_SCROLLS_PER_SCREEN,
        guards.DEFAULT_SCROLL_AMOUNT,
        guards.DEFAULT_SCROLL_INCIDENTS,
    ) == (10, 3, 2)
    monkeypatch.setattr(settings, "DEFAULT_SCROLL_LISTS", True)
    assert "scroll_lists = true\n" in render_default_settings()


@pytest.mark.parametrize("version", range(1, 11))
def test_an_older_file_loads_scrolling_off_with_its_defaults(tmp_path, version):
    loaded = load_settings(write(tmp_path, version_text(version)))
    assert loaded.defaults_version == version
    assert loaded.scroll_lists is False
    assert (loaded.scrolls_per_screen, loaded.scroll_amount) == (10, 3)
    assert loaded.scroll_incidents == 2
    assert loaded.warnings == ()
    assert settings.policy_from_settings(loaded).scroll_lists is False


@pytest.mark.parametrize("version", range(1, 11))
def test_an_older_file_may_turn_scrolling_on_and_set_its_limits(tmp_path, version):
    text = version_text(version)
    text = text.replace("[rules]\n", "[rules]\nscroll_lists = true\n", 1)
    text = text.replace(
        "[limits]\n",
        "[limits]\nscrolls_per_screen = 3\nscroll_amount = 2\nscroll_incidents = 1\n",
        1,
    )
    loaded = load_settings(write(tmp_path, text))
    assert loaded.scroll_lists is True
    assert (loaded.scrolls_per_screen, loaded.scroll_amount) == (3, 2)
    assert loaded.scroll_incidents == 1
    assert settings.policy_from_settings(loaded).scroll_lists is True


def test_the_version_10_file_loads_as_the_current_defaults(tmp_path):
    text = V10_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 10\n" in text
    assert "scroll_lists" not in text
    assert "scrolls_per_screen" not in text
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == 10
    assert loaded.warnings == ()
    (tmp_path / "now").mkdir()
    current = load_settings(default_file(tmp_path / "now"))
    assert (
        dataclasses.replace(loaded, source=current.source, defaults_version=CURRENT)
        == current
    )


@pytest.mark.parametrize(
    ("key", "table"),
    [
        ("scroll_lists", "rules"),
        ("scrolls_per_screen", "limits"),
        ("scroll_amount", "limits"),
        ("scroll_incidents", "limits"),
    ],
)
def test_a_current_file_needs_the_scroll_keys(tmp_path, key, table):
    text = render_default_settings()
    line = next(
        line for line in text.splitlines(keepends=True) if line.startswith(f"{key} =")
    )
    refused(write(tmp_path, text.replace(line, "")), f"[{table}] {key}", "is missing")


@pytest.mark.parametrize("value", ["1", '"true"', "0", "[]"])
def test_scroll_lists_must_be_a_bool(tmp_path, value):
    text = render_default_settings().replace(
        "scroll_lists = false", f"scroll_lists = {value}"
    )
    refused(write(tmp_path, text), "[rules] scroll_lists", "must be true or false")


@pytest.mark.parametrize(
    ("key", "default", "high"),
    [
        ("scrolls_per_screen", 10, 100),
        ("scroll_amount", 3, 10),
        ("scroll_incidents", 2, 100),
    ],
)
def test_the_scroll_limits_are_whole_numbers_with_their_range(
    tmp_path, key, default, high
):
    base = render_default_settings()
    for value in ("0", "-1", str(high + 1), "true", '"3"', "2.5"):
        text = base.replace(f"{key} = {default}", f"{key} = {value}")
        refused(write(tmp_path, text), f"[limits] {key}")
    for value in (1, high):
        text = base.replace(f"{key} = {default}", f"{key} = {value}")
        assert getattr(load_settings(write(tmp_path, text)), key) == value


def test_scroll_lists_is_not_a_limits_key_nor_a_limit_a_rules_key(tmp_path):
    text = render_default_settings()
    refused(
        write(tmp_path, text.replace("[limits]\n", "[limits]\nscroll_lists = true\n")),
        "limits",
    )
    refused(
        write(
            tmp_path,
            text.replace("[rules]\n", "[rules]\nscrolls_per_screen = 3\n", 1),
        ),
        "rules",
    )


def test_version_11_added_no_list_defaults():
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 11
        ] == []
    assert settings._LATER_RULES["scroll_lists"] == 11
    assert settings._LATER_LIMITS == {
        "scrolls_per_screen": 11,
        "scroll_amount": 11,
        "scroll_incidents": 11,
    }


def test_the_policy_carries_scroll_lists_off_by_default():
    assert guards.Policy().scroll_lists is False
    assert guards.Policy(scroll_lists=True).with_deny_words(["zzz"]).scroll_lists
    assert guards.Policy().with_deny_words(["zzz"]).scroll_lists is False
    with pytest.raises(TypeError, match="scroll_lists must be a bool"):
        guards.Policy(scroll_lists=1)  # ty: ignore[invalid-argument-type]


def test_the_limits_check_the_scroll_numbers():
    from uiprobe.crawl import Limits

    with pytest.raises(ValueError, match="scrolls_per_screen must be at least 1"):
        Limits(scrolls_per_screen=0)
    with pytest.raises(TypeError, match="scrolls_per_screen must be an int"):
        Limits(scrolls_per_screen=True)


def test_the_settings_docstring_names_the_version_of_the_scroll_fields():
    doc = " ".join((settings.Settings.__doc__ or "").split())
    assert "`DEFAULT_SCROLL_INCIDENTS`), added with defaults version 11." in doc
    assert "scroll_lists" in doc
    assert "defaults version 7" not in doc.split("scroll_lists")[1]


# -- the dialog exact labels on and オン (defaults version 12) ------------------------


def v11_text() -> str:
    text = V11_FILE.read_text(encoding="utf-8")
    assert "defaults_version = 11\n" in text
    assert '    "on",\n' not in text
    assert '    "オン",\n' not in text
    return text


def test_the_version_12_defaults_are_pinned():
    version_12 = tuple(item for version, item in settings._EXACT_LABELS)
    assert version_12 == V12_CONFIRM == DIALOG_ONLY_EXACT_LABELS
    assert {version for version, _ in settings._EXACT_LABELS} == {12}
    assert settings.DEFAULTS_VERSION == 12
    for table in (
        "_DENY_WORDS",
        "_DENY_EXCEPTIONS",
        "_CONFIRM_WORDS",
        "_IRREVERSIBLE_PHRASES",
        "_REFUSE_KEYS",
        "_REFUSE_PREFIXES",
    ):
        assert [
            item for version, item in getattr(settings, table) if version == 12
        ] == []
    assert settings._FIRST_EXCEPT == 13


def test_the_default_policy_has_the_labels_but_not_as_confirm_words():
    policy = guards.Policy()
    assert policy.dialog_exact_labels == ("on", "オン")
    assert "on" not in policy.confirm_words
    assert "オン" not in policy.confirm_words
    assert "on" not in CONFIRM_WORDS


def test_the_generated_file_lists_the_version_12_words(tmp_path):
    text = render_default_settings()
    assert "defaults_version = 12\n" in text
    assert '    "on",\n' in text
    assert '    "オン",\n' in text
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == 12
    assert loaded.dialog_exact_labels == V12_CONFIRM
    assert loaded.confirm_words == CONFIRM_WORDS
    policy = settings.policy_from_settings(loaded)
    assert policy.dialog_exact_labels == V12_CONFIRM
    assert policy.confirm_words == CONFIRM_WORDS


@pytest.mark.parametrize("version", range(1, 12))
def test_an_older_file_gets_the_version_12_labels_through_the_map(tmp_path, version):
    # The fixture of every version from 1 to 11 does not list the two words;
    # loading it adds them as labels newer than the file, and
    # `[confirm] remove` keeps one out.
    text = version_text(version)
    assert '    "on",\n' not in text
    assert '    "オン",\n' not in text
    loaded = load_settings(write(tmp_path, text))
    assert loaded.defaults_version == version
    assert loaded.warnings == ()
    assert loaded.dialog_exact_labels == V12_CONFIRM
    assert loaded.confirm_words == CONFIRM_WORDS
    head, confirm = text.split("[confirm]")
    removed = head + "[confirm]" + confirm.replace("remove = []", 'remove = ["ON"]', 1)
    kept = load_settings(write(tmp_path, removed))
    assert kept.dialog_exact_labels == ("オン",)


def test_a_version_12_file_removes_a_label_with_remove(tmp_path):
    text = render_default_settings()
    head, confirm = text.split("[confirm]")
    removed = (
        head + "[confirm]" + confirm.replace("remove = []", 'remove = ["オン"]', 1)
    )
    loaded = load_settings(write(tmp_path, removed))
    assert loaded.dialog_exact_labels == ("on",)
    assert "オン" not in loaded.confirm_words


def test_a_version_12_file_without_a_label_does_not_get_it_back(tmp_path):
    text = render_default_settings().replace('    "on",\n', "", 1)
    loaded = load_settings(write(tmp_path, text))
    assert loaded.dialog_exact_labels == ("オン",)


def test_the_version_11_file_loads_as_the_current_defaults(tmp_path):
    loaded = load_settings(write(tmp_path, v11_text()))
    assert loaded.defaults_version == 11
    assert loaded.warnings == ()
    (tmp_path / "now").mkdir()
    current = load_settings(default_file(tmp_path / "now"))
    assert (
        dataclasses.replace(loaded, source=current.source, defaults_version=CURRENT)
        == current
    )


def _with_confirm_word(text: str, word: str) -> str:
    head, confirm = text.split("[confirm]")
    listed = confirm.replace("words = [\n", f'words = [\n    "{word}",\n', 1)
    return head + "[confirm]" + listed


@pytest.mark.parametrize("version", range(1, 12))
@pytest.mark.parametrize("word", ["on", "ON", "オン"])
def test_an_older_file_keeps_a_hand_written_word_as_a_confirm_word(
    tmp_path, version, word
):
    text = _with_confirm_word(version_text(version), word)
    loaded = load_settings(write(tmp_path, text))
    assert word in loaded.confirm_words
    assert loaded.dialog_exact_labels == V12_CONFIRM
    policy = settings.policy_from_settings(loaded)
    assert word in policy.confirm_words
    assert policy.with_deny_words(["zzz"]).dialog_exact_labels == V12_CONFIRM


@pytest.mark.parametrize("word", ["on", "オン"])
def test_a_version_12_file_listing_a_label_makes_it_a_label_only(tmp_path, word):
    loaded = load_settings(write(tmp_path, render_default_settings()))
    assert word in loaded.dialog_exact_labels
    assert word not in loaded.confirm_words
