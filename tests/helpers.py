"""Helpers shared by the tests: loopback ports, unused addresses, a fake adb."""

from __future__ import annotations

import socket
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

ADB_DEFAULT_PORT = 5037

# The only loopback ports a test may connect to: those of a fake adb server and
# of ports reserved by `unused_port`. The network guard in conftest reads it.
_allowed_ports: set[int] = set()


def allow_port(port: int) -> None:
    """Let the current test connect to loopback `port`."""
    if port == ADB_DEFAULT_PORT:
        msg = "tests must never connect to the real adb server port"
        raise ValueError(msg)
    _allowed_ports.add(port)


def is_port_allowed(port: object) -> bool:
    """Return True when connecting to `port` is allowed."""
    return port in _allowed_ports


def reset_allowed_ports() -> None:
    """Forget every allowed port (called around each test)."""
    _allowed_ports.clear()


def unused_port() -> int:
    """Return a loopback port nothing listens on, and allow connecting to it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    allow_port(port)
    return port


def unused_address() -> str:
    """Return ``tcp:127.0.0.1:PORT`` for a port nothing listens on."""
    return f"tcp:127.0.0.1:{unused_port()}"


def recording_adb(tmp_path: Path, exit_code: int = 0) -> tuple[Path, Path]:
    """Create a fake adb script that appends its arguments to a log file.

    The script also writes ``ADB_MDNS=<value>`` and an ``ADDRESS=`` line, which
    marks the address variables that are set, to ``env.log`` next to the log, so
    a test can check
    the environment the child really got.

    Returns:
        The script and the log; the log does not exist until the script runs.
    """
    if sys.platform == "win32":
        msg = "the fake adb is a shell script"
        raise NotImplementedError(msg)
    log = tmp_path / "calls.log"
    script = tmp_path / "bin" / "adb"
    script.parent.mkdir()
    env_log = tmp_path / "env.log"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\n'
        f'echo "ADB_MDNS=$ADB_MDNS" >> "{env_log}"\n'
        f'echo "ADDRESS=${{ADB_SERVER_SOCKET+x}}|${{ANDROID_ADB_SERVER_ADDRESS+x}}'
        f'|${{ANDROID_ADB_SERVER_PORT+x}}" >> "{env_log}"\nexit {exit_code}\n'
    )
    script.chmod(0o755)
    return script, log
