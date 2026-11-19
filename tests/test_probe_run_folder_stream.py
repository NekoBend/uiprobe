"""Temporary probe: what does a stream path do as the run folder on Windows?

It runs only inside pytest's temporary folder and reports what it saw as
the skip reason (shown by ``-ra``). It is replaced by a real test before
the pull request is merged.
"""

# The probe calls os directly, as runlog does.
# ruff: noqa: PTH102, PTH110, PTH112, PTH116, PTH208

from __future__ import annotations

import os
import sys

import pytest

from uiprobe.crawl import Limits
from uiprobe.runlog import RunWriter


def _try(label: str, func) -> str:
    try:
        result = func()
    except Exception as error:  # noqa: BLE001 - a probe records anything
        return f"{label}: {type(error).__name__} errno={getattr(error, 'errno', None)}"
    return f"{label}: ok {result!r}"


@pytest.mark.skipif(sys.platform != "win32", reason="probe for Windows only")
def test_probe_stream_paths_as_run_folder(tmp_path):
    seen = []
    base = tmp_path / "base.txt"
    base.write_bytes(b"mine")
    folder = tmp_path / "dir"
    folder.mkdir()
    before = os.stat(base)
    cases = {
        "mkdir file:x": str(base) + ":x",
        "mkdir dir:x": str(folder) + ":x",
        "mkdir missing:x": str(tmp_path / "missing") + ":x",
    }
    for label, path in cases.items():
        seen.append(_try(label, lambda p=path: os.mkdir(p, 0o700)))
        seen.append(_try(f"  exists {label}", lambda p=path: os.path.exists(p)))
        seen.append(_try(f"  isdir {label}", lambda p=path: os.path.isdir(p)))
    after = os.stat(base)
    seen.append(f"base bytes={base.read_bytes()!r}")
    seen.append(f"base mtime same={before.st_mtime_ns == after.st_mtime_ns}")
    seen.append(f"listing={sorted(os.listdir(tmp_path))}")
    for name in ("base2.txt", "dir2"):
        target = tmp_path / name
        if name.startswith("base"):
            target.write_bytes(b"mine")
        else:
            target.mkdir()
        mtime = os.stat(target).st_mtime_ns
        seen.append(
            _try(
                f"RunWriter.create {name}:x",
                lambda t=target: RunWriter.create(
                    str(t) + ":x",
                    package="com.example.app",
                    limits=Limits(),
                    deny_words=[],
                    serial="emulator-5554",
                ).close(),
            )
        )
        seen.append(f"  {name} mtime same={os.stat(target).st_mtime_ns == mtime}")
    pytest.skip("PROBE " + " | ".join(seen))
