"""Tests for ``Device.screenshot``, ``uiprobe screenshot`` and the PNG file writer.

Every device is a strict `FakeAndroid`, so a command other than the allowed
``screencap -p`` (exec) and ``wm size`` (shell) would be refused. Files are made
only under `tmp_path`.
"""

from __future__ import annotations

import builtins
import errno
import json
import re
import struct
import zlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.fake_device import BLACK_PNG, PNG, El, FakeAndroid, Screen
from tests.helpers import unused_address
from uiprobe import cli, pngfile
from uiprobe.device import Device
from uiprobe.errors import ScreenshotFailedError
from uiprobe.pngfile import PngFileError, check_png, save_png

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer

SERIAL = "emulator-5554"
SHOP = "com.example.shop"
SCREENS = (
    Screen("main", f"{SHOP}/.Main", (El((0, 0, 100, 40), text="Hello"),)),
    Screen("secret", f"{SHOP}/.Secret", protected=True),
)


def chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def make_png(width: int = 2, height: int = 3, pixels: bytes | None = None) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = pixels if pixels is not None else bytes(range(256)) * 2
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", raw)  # not valid image data; only the bytes matter here
        + chunk(b"IEND", b"")
    )


# Raw bytes that a text-mode copy or a CRLF conversion would change.
CRLF_PNG = make_png(pixels=b"\r\n\x1a\n\r\r\n\n\r" * 40 + b"\x00\x0d\x0a")


@pytest.fixture(autouse=True)
def android(fake_adb: FakeAdbServer) -> FakeAndroid:
    return FakeAndroid(fake_adb, SERIAL, SCREENS, start="main")


@pytest.fixture
def device(android: FakeAndroid, fake_adb: FakeAdbServer) -> Device:
    assert android.serial == SERIAL
    return Device(adb_server=fake_adb.address)


def invoke(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def shoot(capsys, fake_adb: FakeAdbServer, path, *extra: str):
    return invoke(
        capsys, "screenshot", "-o", str(path), "--adb-server", fake_adb.address, *extra
    )


def exec_requests(server: FakeAdbServer) -> list[str]:
    return [r for r in server.requests if r.startswith("exec:")]


def script(android: FakeAndroid, data: bytes) -> None:
    android.override(re.compile(r"^screencap -p\Z"), data)


# --- check_png ---

BAD_IMAGES = {
    "empty": b"",
    "text": b"not an image",
    "html": b"<html><body>error</body></html>",
    "signature only": PNG[:8],
    "cut in the header": PNG[:20],
    "cut after the header": PNG[:33],
    "cut in the data": PNG[:-13],
    "cut in the last chunk": PNG[:-1],
    "newlines converted": PNG.replace(b"\r\n", b"\n"),
    "no signature": PNG[1:],
    "wrong first chunk": PNG[:12] + b"IHDX" + PNG[16:],
    "short header length": PNG[:8] + struct.pack(">I", 12) + PNG[12:],
    "long header length": PNG[:8] + struct.pack(">I", 14) + PNG[12:],
    "width 0": PNG[:16] + struct.pack(">I", 0) + PNG[20:],
    "height 0": PNG[:20] + struct.pack(">I", 0) + PNG[24:],
    "width too big": PNG[:16] + struct.pack(">I", 2**31) + PNG[20:],
    "height too big": PNG[:20] + struct.pack(">I", 2**32 - 1) + PNG[24:],
    "no closing chunk": PNG[:-12],
    "trailing bytes": PNG + b"\x00",
}


def test_the_fake_images_are_valid():
    assert check_png(PNG) == (1, 1)
    assert check_png(BLACK_PNG) == (1, 1)
    assert check_png(make_png(1080, 2400)) == (1080, 2400)
    assert check_png(CRLF_PNG) == (2, 3)


@pytest.mark.parametrize("name", sorted(BAD_IMAGES))
def test_a_broken_image_is_refused(name):
    with pytest.raises(ValueError, match=r"screenshot|PNG"):
        check_png(BAD_IMAGES[name])


def test_check_png_wants_bytes():
    with pytest.raises(TypeError):
        check_png("text")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        check_png(bytearray(PNG))  # ty: ignore[invalid-argument-type]


# --- Device.screenshot ---


def test_the_exec_request_is_exactly_screencap(device, fake_adb):
    assert device.screenshot() == PNG
    assert exec_requests(fake_adb) == ["exec:screencap -p"]
    assert [r for r in fake_adb.requests if r.startswith("shell:")] == []
    assert fake_adb.unexpected == []


def test_a_screenshot_does_not_become_the_latest_screen(device):
    device.screenshot()
    assert device._latest_screen is None


def test_the_bytes_come_back_unchanged(device, android):
    script(android, CRLF_PNG)
    assert device.screenshot() == CRLF_PNG
    assert b"\r\n" in CRLF_PNG


@pytest.mark.parametrize("name", sorted(BAD_IMAGES))
def test_a_bad_image_is_screenshot_failed(device, android, name):
    script(android, BAD_IMAGES[name])
    with pytest.raises(ScreenshotFailedError) as raised:
        device.screenshot()
    error = raised.value
    assert (error.code, error.exit_code) == ("screenshot_failed", 16)
    assert "may forbid screenshots" in error.message
    assert "private browser tab" in error.message
    assert "uiprobe dump" in error.message
    assert error.details["bytes"] == len(BAD_IMAGES[name])
    assert error.details["reason"] == ("empty" if name == "empty" else "invalid")


def test_a_protected_screen_is_empty_on_api_24_and_black_on_api_36(fake_adb):
    old = FakeAndroid(fake_adb, "old-1", SCREENS, start="secret", api=24)
    new = FakeAndroid(fake_adb, "new-1", SCREENS, start="secret", api=36)
    with pytest.raises(ScreenshotFailedError, match="empty"):
        Device("old-1", adb_server=fake_adb.address).screenshot()
    assert Device("new-1", adb_server=fake_adb.address).screenshot() == BLACK_PNG
    assert (old.secure_screencap, new.secure_screencap) == ("empty", "black")


def test_display_for_image_follows_the_orientation_of_the_image(device, android):
    assert android.display == (320, 640)
    assert device.display_for_image(1, 1) == (320, 640)
    assert device.display_for_image(1080, 2400) == (320, 640)
    assert device.display_for_image(2400, 1080) == (640, 320)
    assert device.dump().display == (320, 640)


def test_display_for_image_is_none_when_the_size_is_unreadable(device, android):
    android.override(re.compile(r"^wm size\Z"), b"unknown\n")
    assert device.display_for_image(10, 20) is None


# --- save_png ---


def test_save_png_writes_the_bytes_into_a_new_file(tmp_path):
    target = tmp_path / "shot.png"
    save_png(target, CRLF_PNG)
    assert target.read_bytes() == CRLF_PNG


def test_save_png_leaves_an_existing_file_alone(tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        save_png(target, PNG)
    assert target.read_bytes() == b"old"


@pytest.mark.parametrize("name", sorted(BAD_IMAGES))
def test_save_png_makes_no_file_for_a_bad_image(tmp_path, name):
    with pytest.raises(ValueError, match=r"screenshot|PNG"):
        save_png(tmp_path / "shot.png", BAD_IMAGES[name])
    assert list(tmp_path.iterdir()) == []


def test_save_png_names_a_file_it_could_not_create(tmp_path):
    target = tmp_path / "missing" / "shot.png"
    with pytest.raises(PngFileError) as raised:
        save_png(target, PNG)
    assert raised.value.details == {"path": str(target), "created": False}
    assert str(target) in raised.value.message
    assert not (tmp_path / "missing").exists()


def test_save_png_refuses_a_null_character_before_creating_anything(tmp_path):
    with pytest.raises(ValueError, match="null"):
        save_png(Path(str(tmp_path / "a") + "\x00b"), PNG)
    assert list(tmp_path.iterdir()) == []


class FailingHandle:
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


def fail_writes(monkeypatch) -> None:
    real = builtins.open
    monkeypatch.setattr(
        pngfile, "open", lambda *a, **k: FailingHandle(real(*a, **k)), raising=False
    )


def test_a_failed_write_leaves_the_file_and_names_it(tmp_path, monkeypatch):
    target = tmp_path / "shot.png"
    fail_writes(monkeypatch)
    with pytest.raises(PngFileError) as raised:
        save_png(target, PNG)
    assert raised.value.details == {"path": str(target), "created": True}
    assert str(target) in raised.value.message
    assert "No space left" in raised.value.message
    assert isinstance(raised.value.__cause__, OSError)
    assert target.read_bytes() == PNG[: len(PNG) // 2]


# --- uiprobe screenshot ---


def test_the_command_saves_the_png_and_says_so(capsys, fake_adb, tmp_path):
    target = tmp_path / "shot.png"
    code, out, err = shoot(capsys, fake_adb, target)
    assert (code, err) == (0, "")
    assert out == f"saved {target} (1x1, {len(PNG)} bytes)\n"
    assert target.read_bytes() == PNG
    assert exec_requests(fake_adb) == ["exec:screencap -p"]


def test_the_command_writes_the_bytes_unchanged(capsys, android, fake_adb, tmp_path):
    script(android, CRLF_PNG)
    target = tmp_path / "shot.png"
    code, out, _ = shoot(capsys, fake_adb, target)
    assert code == 0
    assert out == f"saved {target} (2x3, {len(CRLF_PNG)} bytes)\n"
    assert target.read_bytes() == CRLF_PNG


def test_the_json_shape(capsys, fake_adb, tmp_path):
    target = tmp_path / "shot.png"
    code, out, err = shoot(capsys, fake_adb, target, "--json")
    assert (code, err) == (0, "")
    payload = json.loads(out)
    assert list(payload) == [
        "schema",
        "captured_at",
        "device",
        "path",
        "bytes",
        "width",
        "height",
        "display",
    ]
    assert payload["schema"] == "uiprobe.screenshot/1"
    assert datetime.fromisoformat(payload["captured_at"]).utcoffset() is not None
    assert "." not in payload["captured_at"]
    assert payload["device"] == {"serial": SERIAL}
    assert payload["path"] == str(target)
    assert payload["bytes"] == len(PNG) == target.stat().st_size
    assert (payload["width"], payload["height"]) == (1, 1)
    assert payload["display"] == {"width": 320, "height": 640}


def test_the_json_display_follows_the_image_orientation(
    capsys, android, fake_adb, tmp_path
):
    script(android, make_png(640, 320))
    _, out, _ = shoot(capsys, fake_adb, tmp_path / "wide.png", "--json")
    payload = json.loads(out)
    assert (payload["width"], payload["height"]) == (640, 320)
    assert payload["display"] == {"width": 640, "height": 320}


def test_the_json_leaves_display_out_when_it_is_unknown(
    capsys, android, fake_adb, tmp_path
):
    android.override(re.compile(r"^wm size\Z"), b"")
    target = tmp_path / "shot.png"
    code, out, _ = shoot(capsys, fake_adb, target, "--json")
    assert code == 0
    payload = json.loads(out)
    assert "display" not in payload
    assert target.read_bytes() == PNG
    code, out, _ = shoot(capsys, fake_adb, tmp_path / "two.png")
    assert (code, out.startswith("saved ")) == (0, True)


@pytest.mark.parametrize("name", sorted(BAD_IMAGES))
def test_a_failed_screenshot_makes_no_file(capsys, android, fake_adb, tmp_path, name):
    script(android, BAD_IMAGES[name])
    target = tmp_path / "shot.png"
    code, out, err = shoot(capsys, fake_adb, target)
    assert (code, out) == (16, "")
    assert "may forbid screenshots" in err
    assert list(tmp_path.iterdir()) == []
    code, out, err = shoot(capsys, fake_adb, target, "--json")
    assert (code, err) == (16, "")
    payload = json.loads(out)
    assert payload["schema"] == "uiprobe.error/1"
    assert payload["error"]["code"] == "screenshot_failed"
    assert "uiprobe dump" in payload["error"]["message"]
    assert list(tmp_path.iterdir()) == []


def test_a_protected_screen_gives_exit_16_on_old_android_only(
    capsys, fake_adb, tmp_path
):
    FakeAndroid(fake_adb, "old-1", SCREENS, start="secret", api=24)
    FakeAndroid(fake_adb, "new-1", SCREENS, start="secret", api=36)
    code, _, err = shoot(capsys, fake_adb, tmp_path / "old.png", "-s", "old-1")
    assert (code, "may forbid screenshots" in err) == (16, True)
    assert not (tmp_path / "old.png").exists()
    code, _, _ = shoot(capsys, fake_adb, tmp_path / "new.png", "-s", "new-1")
    assert code == 0
    assert (tmp_path / "new.png").read_bytes() == BLACK_PNG


def test_an_existing_file_is_a_usage_error_and_stays(capsys, fake_adb, tmp_path):
    target = tmp_path / "shot.png"
    target.write_bytes(b"precious")
    before = target.stat()
    code, out, err = shoot(capsys, fake_adb, target)
    assert (code, out) == (2, "")
    assert "already exists" in err
    assert target.read_bytes() == b"precious"
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    code, out, err = shoot(capsys, fake_adb, target, "--json")
    assert (code, err) == (2, "")
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert target.read_bytes() == b"precious"
    assert exec_requests(fake_adb) == []  # found before the device was asked


def test_an_existing_folder_is_a_usage_error(capsys, fake_adb, tmp_path):
    target = tmp_path / "shots"
    target.mkdir()
    (target / "keep").write_bytes(b"x")
    code, _, err = shoot(capsys, fake_adb, target)
    assert (code, "already exists" in err) == (2, True)
    assert [p.name for p in target.iterdir()] == ["keep"]
    assert exec_requests(fake_adb) == []  # found before the device was asked


def test_a_link_is_a_usage_error_and_its_target_is_not_made(capsys, fake_adb, tmp_path):
    elsewhere = tmp_path / "elsewhere.png"
    link = tmp_path / "link.png"
    try:
        link.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    code, _, err = shoot(capsys, fake_adb, link)
    assert (code, "already exists" in err) == (2, True)
    assert not elsewhere.exists()
    assert exec_requests(fake_adb) == []  # found before the device was asked
    elsewhere.write_bytes(b"target")
    code, _, _ = shoot(capsys, fake_adb, link)
    assert code == 2
    assert exec_requests(fake_adb) == []
    assert elsewhere.read_bytes() == b"target"


def test_a_file_that_appears_meanwhile_is_still_refused(
    capsys, fake_adb, tmp_path, monkeypatch
):
    target = tmp_path / "shot.png"
    target.write_bytes(b"late")
    monkeypatch.setattr(cli, "_check_new_file", lambda _path: None)
    code, out, err = shoot(capsys, fake_adb, target)
    assert (code, out) == (2, "")
    assert "already exists" in err
    assert target.read_bytes() == b"late"


def test_o_is_required(capsys, fake_adb):
    code, out, err = invoke(capsys, "screenshot", "--adb-server", fake_adb.address)
    assert (code, out) == (2, "")
    assert "-o" in err
    code, out, _ = invoke(
        capsys, "screenshot", "--json", "--adb-server", fake_adb.address
    )
    assert code == 2
    assert json.loads(out)["error"]["code"] == "usage_error"
    assert exec_requests(fake_adb) == []


def test_an_empty_path_is_a_usage_error(capsys, fake_adb):
    code, _, err = shoot(capsys, fake_adb, "")
    assert (code, "must not be empty" in err) == (2, True)


def test_a_missing_folder_is_an_error_that_names_the_file(capsys, fake_adb, tmp_path):
    target = tmp_path / "missing" / "shot.png"
    code, out, err = shoot(capsys, fake_adb, target)
    assert (code, out) == (1, "")
    assert str(target) in err
    code, out, _ = shoot(capsys, fake_adb, target, "--json")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "file_error"
    assert error["details"] == {"path": str(target), "created": False}
    assert not (tmp_path / "missing").exists()


def test_a_write_failure_after_create_keeps_the_file_and_names_it(
    capsys, fake_adb, tmp_path, monkeypatch
):
    target = tmp_path / "shot.png"
    fail_writes(monkeypatch)
    code, out, err = shoot(capsys, fake_adb, target)
    assert (code, out) == (1, "")
    assert str(target) in err
    assert target.read_bytes() == PNG[: len(PNG) // 2]  # stays, not deleted
    code, out, _ = shoot(capsys, fake_adb, tmp_path / "two.png", "--json")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "file_error"
    assert error["details"] == {"path": str(tmp_path / "two.png"), "created": True}
    assert (tmp_path / "two.png").exists()


def test_odd_path_names_are_written_and_shown_as_given(capsys, fake_adb, tmp_path):
    # Backslashes are part of a name on POSIX and separators on Windows; the
    # path is passed on and printed exactly as it was typed.
    names = ["my shots\\a b.png", "日本語 画面.png", "dir\\sub\\shot.png"]
    for name in names:
        path = f"{tmp_path}/{name}"
        code, out, err = shoot(capsys, fake_adb, path)
        if code != 0:  # only where backslash separates folders, and one is missing
            assert Path("a\\b").name == "b"
            assert (code, "cannot create" in err) == (1, True)
            continue
        assert out == f"saved {path} (1x1, {len(PNG)} bytes)\n"
        assert Path(path).read_bytes() == PNG
        code, out, _ = shoot(capsys, fake_adb, path, "--json")
        assert code == 2
        assert json.loads(out)["error"]["code"] == "usage_error"


def test_a_windows_style_name_is_one_file_name_on_posix(capsys, fake_adb, tmp_path):
    if Path("a\\b").name != "a\\b":
        pytest.skip("backslash separates folders on this system")
    path = f"{tmp_path}/C:\\Users\\me\\shot.png"
    code, out, _ = shoot(capsys, fake_adb, path, "--json")
    assert code == 0
    assert json.loads(out)["path"] == path
    assert (tmp_path / "C:\\Users\\me\\shot.png").read_bytes() == PNG


def test_a_relative_path_is_used_as_given(capsys, fake_adb, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = shoot(capsys, fake_adb, "shot.png", "--json")
    assert code == 0
    assert json.loads(out)["path"] == "shot.png"
    assert (tmp_path / "shot.png").read_bytes() == PNG


def test_control_characters_in_the_path_are_escaped(capsys, fake_adb, tmp_path):
    path = f"{tmp_path}/a\nb\x1b[2Jc.png"
    code, out, _ = shoot(capsys, fake_adb, path)
    assert code == 0
    assert out.count("\n") == 1
    assert "\x1b" not in out
    assert "a\\nb\\x1b[2Jc.png" in out
    code, out, _ = shoot(capsys, fake_adb, path, "--json")
    assert code == 2
    assert "\x1b" not in out
    assert json.loads(out)["error"]["code"] == "usage_error"


def test_common_options_work_before_and_after_the_command(capsys, fake_adb, tmp_path):
    fake_adb.add_device("other-1")
    first = tmp_path / "one.png"
    code, out, _ = invoke(
        capsys,
        "-s",
        SERIAL,
        "--adb-server",
        fake_adb.address,
        "--no-start-server",
        "--json",
        "screenshot",
        "-o",
        str(first),
    )
    assert code == 0
    assert json.loads(out)["device"] == {"serial": SERIAL}
    second = tmp_path / "two.png"
    code, out, _ = invoke(
        capsys,
        "screenshot",
        "-o",
        str(second),
        "-s",
        SERIAL,
        "--adb-server",
        fake_adb.address,
        "--no-start-server",
        "--json",
    )
    assert code == 0
    assert json.loads(out)["device"] == {"serial": SERIAL}
    assert first.read_bytes() == second.read_bytes() == PNG


def test_two_devices_without_a_choice_make_no_file(capsys, fake_adb, tmp_path):
    fake_adb.add_device("other-1")
    code, out, err = shoot(capsys, fake_adb, tmp_path / "shot.png")
    assert (code, out) == (4, "")
    assert "choose one with --serial" in err
    assert list(tmp_path.iterdir()) == []


def test_no_server_gives_exit_3_and_no_file(capsys, tmp_path):
    target = tmp_path / "shot.png"
    code, _, _ = invoke(
        capsys,
        "screenshot",
        "-o",
        str(target),
        "--adb-server",
        unused_address(),
        "--no-start-server",
    )
    assert code == 3
    assert not target.exists()


def test_a_trailing_separator_makes_no_file_and_the_error_shows_it(
    capsys, fake_adb, tmp_path
):
    path = f"{tmp_path}/shots/"
    code, _, err = shoot(capsys, fake_adb, path)
    assert (code, "cannot create" in err) == (1, True)
    assert list(tmp_path.iterdir()) == []
