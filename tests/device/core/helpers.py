"""Shared helpers of the device core tests: constants and the dump scripts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.fake_adb_server import FakeAdbServer, Response

DATA = Path(__file__).parents[2] / "data"


SERIAL = "emulator-5554"


# --- dump ---

LOGIN_XML = (DATA / "dump_login.xml").read_bytes()


DUMPSYS = b"""\
  mCurrentFocus=Window{4f2a1c u0 com.example.shop/com.example.shop.LoginActivity}
  mFocusedApp=ActivityRecord{1 u0 com.example.shop/.LoginActivity t12}
"""


def script_dump(
    server: FakeAdbServer,
    *,
    dumpsys: bytes = DUMPSYS,
    xml: bytes = LOGIN_XML,
    wm_size: Response = b"",
) -> None:
    server.on_shell(SERIAL, "wm size", wm_size)
    server.on_shell(SERIAL, "getprop ro.build.version.sdk", b"36\n")
    server.on_shell(SERIAL, "dumpsys window", dumpsys)
    server.on_shell(
        SERIAL,
        re.compile(r"uiautomator dump /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml$"),
        b"UI hierchary dumped to: /data/local/tmp/x.xml\n",
    )
    server.on_exec(
        SERIAL,
        re.compile(r"^cat /data/local/tmp/uiprobe-[0-9a-f]{16}\.xml$"),
        xml,
    )


def requests_of(server: FakeAdbServer, prefix: str) -> list[str]:
    return [r for r in server.requests if r.startswith(prefix)]
