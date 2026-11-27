"""Shared fixtures: a clean environment, network, process and file guards, fake adb.

The guards are one audit hook that is armed only while a test runs. The file
guard refuses to delete, rename or truncate anything outside the test's
``tmp_path`` (uiprobe only ever creates files), and refuses to open a file for
writing inside the repository. Its rules:

* Events judged: ``os.remove``, ``os.rename`` (also ``os.replace``),
  ``os.rmdir``, ``shutil.rmtree`` and ``os.truncate`` (also ``os.ftruncate``).
* Allowed: a path inside ``tmp_path``; a path inside a ``__pycache__`` or
  ``.pytest_cache`` directory under the repository (not anywhere else); and
  the unlink of a file directly inside ``tempfile.gettempdir()`` whose name is
  ``tmp`` plus eight characters of tempfile's own alphabet. That is the
  fallback of ``tempfile.TemporaryFile`` (so of ``capfd``) when the file system
  has no ``O_TMPFILE``: it creates a named file and unlinks it at once.
* Relative paths are resolved against the current directory, or, with a
  ``dir_fd``, against the directory that descriptor points to. A rename
  resolves its source with ``src_dir_fd`` and its destination with
  ``dst_dir_fd``, each on its own. A descriptor that cannot be turned into a
  path (no ``/proc/self/fd`` on the platform, a closed descriptor, a pipe) is
  refused, never allowed.
* ``os.remove``, ``os.rename`` and ``os.rmdir`` act on a symlink itself, so a
  symlink in the last component is not followed. ``os.truncate`` follows
  links, so its path is resolved in full. ``os.truncate`` with an integer
  descriptor is judged by the path the descriptor points to, else refused.
* ``open`` events (``open()``, ``io.open``, ``os.open``, ``Path.open`` ...):
  an open for writing (a mode with ``w``, ``a``, ``x`` or ``+``, or flags with
  ``O_WRONLY``, ``O_RDWR``, ``O_TRUNC``, ``O_APPEND`` or ``O_CREAT``) of a path
  inside the repository fails, unless the path is inside ``tmp_path`` or has a
  tool-cache component (``__pycache__``, ``.pytest_cache``, ``.ruff_cache``,
  ``.mypy_cache``, ``.hypothesis``, ``.coverage*``). Paths outside the
  repository are not checked here (pytest and tempfile live there). A relative
  name is taken against the current directory; the event carries no ``dir_fd``.
* While the guard is armed ``sys.dont_write_bytecode`` is True: importlib
  writes a ``.pyc`` by renaming ``X.pyc.<id>`` to ``X.pyc``, which the rename
  rule would refuse (outside the repository and ``tmp_path``) and importlib
  does not survive a ``RuntimeError``.

A descriptor is resolved through ``/proc/self/fd`` (Linux) or
``fcntl.F_GETPATH`` (macOS, where ``shutil.rmtree`` works through ``dir_fd``).
Windows needs neither: ``shutil.rmtree`` does not use ``dir_fd`` there. Where
neither works, a descriptor-relative operation is refused.
macOS: the F_GETPATH path is exercised through a fake fcntl only and is untested
on a real Mac (nobody has run it on macOS yet).
"""

from __future__ import annotations

import importlib
import ipaddress
import os
import re
import socket
import struct
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.fake_adb_server import FakeAdbServer
from tests.helpers import is_port_allowed, reset_allowed_ports
from tests.offline_check import offline_violation

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

ENVIRONMENT_VARIABLES = (
    "ADB_SERVER_SOCKET",
    "ANDROID_ADB_SERVER_PORT",
    "ANDROID_ADB_SERVER_ADDRESS",
    "ANDROID_SERIAL",
    "UIPROBE_ADB",
    "ANDROID_HOME",
    "ANDROID_SDK_ROOT",
)


_ADDRESS_VARIABLES = frozenset(
    {"ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_ADDRESS", "ANDROID_ADB_SERVER_PORT"}
)


class NetworkAccessBlockedError(RuntimeError):
    """Raised when a test tries to reach anything but a fake adb server."""


class ProcessLaunchBlockedError(RuntimeError):
    """Raised when a test starts a process other than ``adb start-server``."""


def _is_loopback_host(host: object) -> bool:
    """Return True for 127.0.0.0/8, ::1, and the name 'localhost'."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


class FileModificationBlockedError(RuntimeError):
    """Raised when a test deletes, renames or truncates a file outside tmp_path.

    Also for a write-mode open of a file inside the repository.
    """


class _GuardState:
    """What the audit hook needs; None while no test is running."""

    connections: list[tuple[object, ...]] | None = None
    processes: list[list[str]] | None = None
    tmp_root: Path | None = None
    blocked: list[str] | None = None


_STATE = _GuardState()

_CONNECT_EVENTS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})
_RESOLVE_EVENTS = frozenset({"socket.gethostbyname", "socket.gethostbyaddr"})
_SPAWN_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn"})
_FORBIDDEN_PROCESS_EVENTS = frozenset(
    {"os.system", "os.exec", "os.spawn", "os.fork", "os.forkpty"}
)
_FILE_PATH_EVENTS = frozenset({"os.remove", "os.rmdir", "shutil.rmtree", "os.truncate"})
# Events whose second argument is a `dir_fd` (for os.truncate it is the length,
# and os.rename has two dir_fds, in the third and fourth place).
_DIR_FD_EVENTS = frozenset({"os.remove", "os.rmdir", "shutil.rmtree"})
# pytest and the interpreter manage these on their own, under the repository.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_HOUSEKEEPING_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache"})
# `capfd` makes a `tempfile.TemporaryFile` inside the guard window. On Linux that
# uses O_TMPFILE (no unlink event); where the file system lacks it, tempfile
# creates a named file and unlinks it at once. It is looked up here, at import,
# because the first `gettempdir()` call creates and removes a probe file.
_TEMP_DIRECTORY = Path(tempfile.gettempdir()).resolve()
_TEMP_FILE_NAME = re.compile(r"tmp[a-z0-9_]{8}")  # prefix + _RandomNameSequence
# Tool caches inside the repository that a write-mode open may touch.
_CACHE_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis"}
)
_CACHE_PREFIXES = (".coverage",)  # .coverage and .coverage.<host>.<pid>.<random>
_WRITE_MODE_CHARACTERS = frozenset("wax+")
_WRITE_FLAGS = 0
for _flag_name in ("O_WRONLY", "O_RDWR", "O_TRUNC", "O_APPEND", "O_CREAT"):
    _WRITE_FLAGS |= getattr(os, _flag_name, 0)


def _refuse(error: Exception) -> Exception:
    """Record a blocked attempt, so the test fails even if the code swallows it."""
    if _STATE.blocked is not None:
        _STATE.blocked.append(str(error))
    return error


def _check_connection(connections: list[tuple[object, ...]], address: object) -> None:
    """Record `address`; allow only a loopback port of a fake adb server."""
    if not isinstance(address, tuple) or len(address) < 2:
        connections.append((address,))
        msg = f"network access blocked (not a TCP/UDP address): {address!r}"
        raise _refuse(NetworkAccessBlockedError(msg))
    connections.append(address)
    if not _is_loopback_host(address[0]):
        msg = f"network access blocked: {address!r}"
        raise _refuse(NetworkAccessBlockedError(msg))
    if not is_port_allowed(address[1]):
        msg = f"connection to a port that is not a fake adb server: {address!r}"
        raise _refuse(NetworkAccessBlockedError(msg))


def _check_name_lookup(host: object) -> None:
    if host is not None and not _is_loopback_host(host):
        msg = f"name resolution blocked: {host!r}"
        raise _refuse(NetworkAccessBlockedError(msg))


def split_windows_command_line(line: str) -> list[str]:
    """Split a Windows command line into arguments; the inverse of `list2cmdline`.

    On Windows `subprocess.Popen` joins a list of arguments into one string
    before it fires its audit event, so the guard sees the whole command line
    where POSIX shows the list. This undoes what `subprocess.list2cmdline`
    does: whitespace separates arguments outside quotes, a quote toggles
    quoting, and backslashes are literal unless they precede a quote (2n of
    them stand for n, and an odd count also makes the quote itself literal).
    It is not a full parser of the C runtime's rules: it has neither the
    special rule for the first argument (no escapes inside its quotes) nor the
    rule for a doubled quote inside a quoted argument.
    """
    arguments: list[str] = []
    current: list[str] = []
    started = False
    quoted = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\":
            end = index
            while end < len(line) and line[end] == "\\":
                end += 1
            count = end - index
            if end < len(line) and line[end] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                else:
                    quoted = not quoted
                end += 1
            else:
                current.append("\\" * count)
            started = True
            index = end
            continue
        if char == '"':
            quoted = not quoted
            started = True
        elif char in " \t" and not quoted:
            if started:
                arguments.append("".join(current))
                current, started = [], False
        else:
            current.append(char)
            started = True
        index += 1
    if started:
        arguments.append("".join(current))
    return arguments


def _command_of(argv: object) -> list[str]:
    """Return the argument list a process-launch audit event stands for."""
    if isinstance(argv, list | tuple):
        return [str(part) for part in argv]
    if sys.platform == "win32":
        return split_windows_command_line(str(argv))
    return [str(argv)]


def _is_under(program: object, tmp_root: Path) -> bool:
    """Return True if `program` is an absolute path that resolves under `tmp_root`.

    `program` may be a str, bytes or path. A relative or bare name is refused
    even when it would point under `tmp_root` from the test's current
    directory: the launch does not resolve it that way. ``subprocess.Popen``
    searches the ``PATH`` of the env it is given for a bare name, resolves a
    relative path against its own ``cwd=`` argument (on POSIX),
    ``os.posix_spawnp`` searches ``PATH``, and on Windows a relative name may be
    looked up in the application directory, the current directory, the system
    directories and ``PATH``. Only an absolute path names the same program for
    the guard and for the launch.
    """
    if not isinstance(program, str | bytes | os.PathLike):
        return False
    try:
        path = Path(os.fsdecode(program))
        return path.is_absolute() and path.resolve().is_relative_to(tmp_root)
    except (TypeError, ValueError, OSError):
        return False


def _check_process(
    processes: list[list[str]],
    tmp_root: Path,
    argv: object,
    env: object,
    executable: object = None,
    *,
    record: bool,
) -> None:
    """Allow only ``<adb under tmp_path> start-server`` with ``ADB_MDNS=0`` and no
    address variables.

    `executable` is the program the launch names apart from the command line
    (the ``executable`` argument of ``subprocess.Popen``, the path of
    ``os.posix_spawn``). It decides which program runs, so when it is given it
    must be under `tmp_root` as well. None means the program is the first
    argument.

    Both must be absolute paths: a relative or bare name is blocked, because
    the launch resolves it by searching ``PATH``, against ``cwd=`` (on
    POSIX) or in the Windows search directories, not reliably against the
    test process's current directory (see `_is_under`).

    `env` is None when the launch does not say (``posix_spawn`` after
    ``subprocess.Popen`` already checked it).
    """
    command = _command_of(argv)
    if record:
        processes.append(command)
    allowed = (
        len(command) == 2
        and command[1] == "start-server"
        and _is_under(command[0], tmp_root)
        and (executable is None or _is_under(executable, tmp_root))
    )
    if not allowed:
        msg = (
            f"process launch blocked (only `adb start-server` is allowed): {command!r}"
        )
        raise _refuse(ProcessLaunchBlockedError(msg))
    if record and not (isinstance(env, Mapping) and env.get("ADB_MDNS") == "0"):
        msg = f"adb start-server must run with ADB_MDNS=0: {command!r}"
        raise _refuse(ProcessLaunchBlockedError(msg))
    if record and isinstance(env, Mapping):
        leaked = sorted(set(env) & _ADDRESS_VARIABLES)
        if leaked:
            msg = (
                f"adb start-server must not get address variables {leaked}: {command!r}"
            )
            raise _refuse(ProcessLaunchBlockedError(msg))


def _load_fcntl() -> Any:  # noqa: ANN401
    """Return the ``fcntl`` module, or None (Windows). Loaded by name, typed Any."""
    try:
        return importlib.import_module("fcntl")
    except ImportError:
        return None


def _path_by_getpath(fd: int, fcntl: Any) -> Path | None:  # noqa: ANN401
    """Ask ``fcntl.F_GETPATH`` (macOS) for the path of `fd`; None if it cannot."""
    request = getattr(fcntl, "F_GETPATH", None)
    if request is None:
        return None
    try:
        raw = fcntl.fcntl(fd, request, b"\0" * 1024)
    except OSError:
        return None
    if not isinstance(raw, bytes):
        return None
    path = Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    return path if path.is_absolute() else None


def _path_of_descriptor(fd: int) -> Path | None:
    """Return the path a descriptor points at, or None when that cannot be told.

    Linux tells through /proc/self/fd, macOS through ``fcntl.F_GETPATH``. A
    descriptor of a pipe, a socket or a closed file gives no absolute path; the
    caller refuses then.
    """
    try:
        target = Path(f"/proc/self/fd/{fd}").readlink()
    except OSError:
        return _path_by_getpath(fd, _load_fcntl())
    return target if target.is_absolute() else None


def _descriptor(args: tuple[object, ...], index: int) -> int | None:
    """Return the descriptor at `args[index]`, None when absent (None or -1)."""
    if len(args) <= index:
        return None
    value = args[index]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _resolve_operand(
    target: object, dir_fd: int | None, *, follow_link: bool
) -> Path | None:
    """Return the absolute, resolved path an operand stands for; None if unknown.

    A relative name is taken against `dir_fd` when given, else the current
    directory. The last component is kept as it is unless `follow_link`,
    because remove, rename and rmdir act on a link itself and truncate acts on
    its target. An integer is a file descriptor of the file itself.
    """
    if isinstance(target, int) and not isinstance(target, bool):
        return _path_of_descriptor(target)
    if not isinstance(target, str | bytes | os.PathLike):
        return None
    try:
        given = Path(os.fsdecode(target))
        if dir_fd is not None and not given.is_absolute():
            base = _path_of_descriptor(dir_fd)
            if base is None:
                return None
            given = base / given
        if follow_link or given.name in {"", ".", ".."}:
            return given.resolve()
        return given.parent.resolve() / given.name
    except (TypeError, ValueError, OSError):
        return None


def _is_housekeeping(path: Path) -> bool:
    """Return True for a path inside __pycache__ or .pytest_cache of the repository."""
    try:
        parts = path.relative_to(_PROJECT_ROOT).parts
    except ValueError:
        return False
    return not _HOUSEKEEPING_DIRECTORIES.isdisjoint(parts)


def _is_tempfile_name(path: Path) -> bool:
    """Return True for a name tempfile makes, directly inside gettempdir()."""
    return path.parent == _TEMP_DIRECTORY and bool(_TEMP_FILE_NAME.fullmatch(path.name))


def _is_allowed_file_target(path: Path, tmp_root: Path, event: str) -> bool:
    """Return True if changing the resolved `path` is fine (see the docstring above)."""
    return (
        path.is_relative_to(tmp_root)
        or _is_housekeeping(path)
        or (event == "os.remove" and _is_tempfile_name(path))
    )


def _check_file_event(event: str, args: tuple[object, ...], tmp_root: Path) -> None:
    """Refuse to delete, rename or truncate a file outside `tmp_root`.

    uiprobe only ever creates new files; a test may clean up after itself
    inside its own tmp_path and nowhere else. A rename names two paths, each
    with its own `dir_fd`, and both must be allowed (the destination would be
    overwritten). An operand that cannot be resolved is refused.
    """
    if event == "os.rename":
        operands = [(args[0], _descriptor(args, 2)), (args[1], _descriptor(args, 3))]
    else:
        # The second argument of os.truncate is the length, not a descriptor.
        dir_fd = _descriptor(args, 1) if event in _DIR_FD_EVENTS else None
        operands = [(args[0], dir_fd)]
    for target, dir_fd in operands:
        path = _resolve_operand(target, dir_fd, follow_link=event == "os.truncate")
        if path is None or not _is_allowed_file_target(path, tmp_root, event):
            msg = f"file change blocked outside tmp_path: {event} {target!r}"
            raise _refuse(FileModificationBlockedError(msg))


def _opens_for_writing(mode: object, flags: object) -> bool:
    """Return True for an ``open`` event that can create, change or empty a file."""
    if isinstance(flags, int) and flags & _WRITE_FLAGS:
        return True
    return isinstance(mode, str) and not _WRITE_MODE_CHARACTERS.isdisjoint(mode)


def _is_cache_path(path: Path) -> bool:
    """Return True for a path inside a tool cache of the repository."""
    try:
        parts = path.relative_to(_PROJECT_ROOT).parts
    except ValueError:
        return False
    return any(
        part in _CACHE_NAMES or part.startswith(_CACHE_PREFIXES) for part in parts
    )


def _check_open_event(args: tuple[object, ...], tmp_root: Path) -> None:
    """Refuse to open a file of the repository for writing.

    Only paths inside the repository are judged (and a descriptor given to
    ``open`` is not a path); `tmp_root` and the tool caches are fine. A path
    that cannot be resolved is let by: this check is narrow on purpose, the
    static check on src/uiprobe is the main one.
    """
    if len(args) < 3 or not _opens_for_writing(args[1], args[2]):
        return
    target = args[0]
    if not isinstance(target, str | bytes | os.PathLike):
        return
    try:
        path = Path(os.fsdecode(target)).resolve()
    except (TypeError, ValueError, OSError):
        return
    if (
        path.is_relative_to(_PROJECT_ROOT)
        and not path.is_relative_to(tmp_root)
        and not _is_cache_path(path)
    ):
        msg = f"write blocked inside the repository: open {target!r} {args[1:]!r}"
        raise _refuse(FileModificationBlockedError(msg))


def _audit(event: str, args: tuple[object, ...]) -> None:
    """Audit hook: sees every socket, process and file-change call, even raw."""
    connections, processes, tmp_root = (
        _STATE.connections,
        _STATE.processes,
        _STATE.tmp_root,
    )
    if connections is None or processes is None or tmp_root is None:
        return
    if event in _CONNECT_EVENTS:
        _check_connection(connections, args[1])
    elif event == "socket.getaddrinfo" or event in _RESOLVE_EVENTS:
        _check_name_lookup(args[0])
    elif event == "socket.getnameinfo":
        sockaddr = args[0]
        _check_name_lookup(sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr)
    elif event in _SPAWN_EVENTS:
        # `subprocess` fires both events for one launch: record it only once.
        is_popen = event == "subprocess.Popen"
        env = args[3] if is_popen else None
        # Popen event: (executable, args, cwd, env); posix_spawn: (path, argv, env).
        _check_process(processes, tmp_root, args[1], env, args[0], record=is_popen)
    elif event in _FORBIDDEN_PROCESS_EVENTS:
        msg = f"process launch blocked: {event} {args!r}"
        raise _refuse(ProcessLaunchBlockedError(msg))
    elif event == "os.rename" or event in _FILE_PATH_EVENTS:
        _check_file_event(event, args, tmp_root)
    elif event == "open":
        _check_open_event(args, tmp_root)


# An audit hook cannot be removed: it is installed once and does nothing
# outside a test (see `_GuardState`).
sys.addaudithook(_audit)


def _bring_up_loopback() -> None:
    """Bring the 'lo' interface up on Linux (a fresh network namespace has it down).

    Under ``unshare -rn`` the loopback interface starts down, so even 127.0.0.1
    is unreachable. Setting IFF_UP needs no network access. Failure is ignored:
    outside such a namespace the interface is already up.
    """
    if not sys.platform.startswith("linux"):
        return
    import fcntl

    siocgifflags, siocsifflags, iff_up = 0x8913, 0x8914, 0x1
    request = struct.pack("16sh14x", b"lo", 0)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            flags = struct.unpack("16sh14x", fcntl.ioctl(sock, siocgifflags, request))[
                1
            ]
            if not flags & iff_up:
                fcntl.ioctl(
                    sock, siocsifflags, struct.pack("16sh14x", b"lo", flags | iff_up)
                )
    except OSError:
        return


def pytest_sessionstart(session: pytest.Session) -> None:  # noqa: ARG001
    """Stop at once when UIPROBE_TEST_REQUIRE_OFFLINE=1 and a route leads out."""
    if message := offline_violation(os.environ):
        pytest.exit(message, returncode=pytest.ExitCode.USAGE_ERROR)


@pytest.fixture(scope="session", autouse=True)
def _loopback_up() -> None:
    _bring_up_loopback()


@pytest.fixture(scope="session")
def _empty_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("empty-path")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, _empty_directory: Path) -> None:
    """Remove every variable that changes adb or device selection.

    PATH points at an empty directory, so no test can find (and run) a real
    adb; a test that needs one builds it in its own directory.
    """
    for name in ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", str(_empty_directory))


@pytest.fixture(autouse=True)
def connections() -> list[tuple[object, ...]]:
    """Every connection target a test tried, in order (filled by the guard)."""
    return []


@pytest.fixture(autouse=True)
def processes() -> list[list[str]]:
    """Every command line a test tried to start, in order (filled by the guard)."""
    return []


@pytest.fixture(autouse=True)
def blocked_attempts() -> list[str]:
    """Every attempt the guards refused; a test that leaves any fails."""
    return []


@pytest.fixture(autouse=True)
def _guards(
    connections: list[tuple[object, ...]],
    processes: list[list[str]],
    blocked_attempts: list[str],
    tmp_path: Path,
) -> Iterator[None]:
    """Arm the network, process and file-change guards for one test."""
    reset_allowed_ports()
    _STATE.connections = connections
    _STATE.processes = processes
    _STATE.tmp_root = tmp_path.resolve()
    _STATE.blocked = blocked = blocked_attempts
    bytecode_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = bytecode_before
        _STATE.connections = _STATE.processes = _STATE.tmp_root = None
        _STATE.blocked = None
        reset_allowed_ports()
    if blocked:
        pytest.fail("the guards blocked: " + "; ".join(blocked), pytrace=False)


@pytest.fixture
def fake_adb() -> Iterator[FakeAdbServer]:
    """Start a scripted fake adb server and stop it afterwards.

    A client that breaks the protocol, or a handler that crashes, fails the
    test at teardown instead of showing up as a vague "connection closed".
    """
    with FakeAdbServer() as server:
        yield server
    assert not server.protocol_errors, server.protocol_errors
    assert not server.handler_errors, server.handler_errors
    assert not server.unexpected, server.unexpected


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    call: pytest.CallInfo[None],
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Add the address and cause of an adb error to the failure report.

    An error such as `AdbServerUnavailableError` keeps its cause (the socket
    error, for example) in `details`, which pytest does not show; the report
    then names it. Tests talk only to fakes, so no device value reaches it.
    """
    report = yield
    try:
        lines = _error_details(call)
    except Exception:  # noqa: BLE001 - never turn a report into an internal error
        lines = []
    if report.failed and lines:
        report.sections.append(("uiprobe error details", "\n".join(lines)))
    return report


# The detail keys shown: the server address and the cause of a uiprobe error
# (a socket error, for example). Other keys can hold host paths.
_SHOWN_DETAILS = ("address", "reason")


def _error_details(call: pytest.CallInfo[None]) -> list[str]:
    """The shown details of each uiprobe error in the failure's cause chain."""
    from uiprobe.errors import UiprobeError

    excinfo = call.excinfo
    if excinfo is None:
        return []
    lines: list[str] = []
    seen: set[int] = set()
    error: BaseException | None = excinfo.value
    while error is not None and id(error) not in seen and len(seen) < 10:
        seen.add(id(error))
        if isinstance(error, UiprobeError):
            shown = {
                key: str(error.details[key])[:500]
                for key in _SHOWN_DETAILS
                if key in error.details
            }
            if shown:
                lines.append(f"{type(error).__name__}: {shown}")
        cause = error.__cause__
        if cause is None and not error.__suppress_context__:
            cause = error.__context__
        error = cause
    return lines
