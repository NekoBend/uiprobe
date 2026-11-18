"""Tests for the network guard in conftest.py."""

from __future__ import annotations

import _socket
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.conftest import (
    NetworkAccessBlockedError,
    ProcessLaunchBlockedError,
    _check_process,
    _command_of,
    _is_loopback_host,
    split_windows_command_line,
)
from tests.helpers import allow_port, recording_adb, unused_port

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

MDNS_OFF = {**os.environ, "ADB_MDNS": "0"}


@pytest.fixture(autouse=True)
def _expect_blocked_attempts(blocked_attempts: list[str]) -> Iterator[None]:
    """These tests block on purpose; the guard fixture would fail them."""
    yield
    blocked_attempts.clear()


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("192.0.2.1", False),
        ("8.8.8.8", False),
        ("example.com", False),
        ("::2", False),
        (None, False),
    ],
)
def test_is_loopback_host(host, expected):
    assert _is_loopback_host(host) is expected


def test_non_loopback_connect_is_blocked_before_any_packet(connections):
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkAccessBlockedError),
    ):
        sock.connect(("192.0.2.1", 5037))
    assert connections == [("192.0.2.1", 5037)]


def test_non_loopback_connect_ex_is_blocked():
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkAccessBlockedError),
    ):
        sock.connect_ex(("192.0.2.1", 5037))


def test_create_connection_to_non_loopback_is_blocked():
    with pytest.raises(NetworkAccessBlockedError):
        socket.create_connection(("192.0.2.1", 5037), timeout=0.2)


def test_name_resolution_of_remote_host_is_blocked():
    with pytest.raises(NetworkAccessBlockedError):
        socket.getaddrinfo("example.com", 80)


def test_udp_sendto_non_loopback_is_blocked():
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(NetworkAccessBlockedError),
    ):
        sock.sendto(b"x", ("192.0.2.1", 53))


def test_loopback_connect_is_allowed_and_recorded(connections):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        allow_port(port)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    assert ("127.0.0.1", port) in connections


def test_loopback_port_that_is_not_a_fake_adb_is_blocked(connections):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
            pytest.raises(NetworkAccessBlockedError, match="not a fake adb"),
        ):
            client.connect(("127.0.0.1", port))
    assert connections == [("127.0.0.1", port)]


def test_the_real_adb_port_can_never_be_allowed():
    with pytest.raises(ValueError, match="real adb"):
        allow_port(5037)
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client,
        pytest.raises(NetworkAccessBlockedError),
    ):
        client.connect(("127.0.0.1", 5037))


def test_the_port_of_a_fake_server_is_allowed(fake_adb, connections):
    socket.create_connection(("127.0.0.1", fake_adb.port), timeout=2).close()
    assert connections == [("127.0.0.1", fake_adb.port)]


def test_an_unused_port_is_allowed_so_that_refusal_can_be_tested():
    port = unused_port()
    with pytest.raises(ConnectionRefusedError):
        # Windows reports a refused connection only after its SYN retries,
        # about two seconds; a short timeout would end in TimeoutError first.
        socket.create_connection(("127.0.0.1", port), timeout=10)


def test_raw_socket_module_is_guarded(connections):
    # `_socket.socket` bypasses every Python-level wrapper; the audit hook does not.
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessBlockedError):
            sock.connect(("192.0.2.1", 9))
    finally:
        sock.close()
    assert connections == [("192.0.2.1", 9)]


def posix_call(owner: object, name: str) -> Callable[..., object]:
    """Return a POSIX-only function; type checkers on Windows do not know it."""
    return getattr(owner, name)


def posix_int(owner: object, name: str) -> int:
    """Return a POSIX-only constant; type checkers on Windows do not know it."""
    return int(getattr(owner, name))


@pytest.mark.skipif(
    not hasattr(socket.socket, "sendmsg"), reason="socket.sendmsg is POSIX only"
)
def test_sendmsg_to_remote_host_is_blocked():
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock,
        pytest.raises(NetworkAccessBlockedError),
    ):
        posix_call(sock, "sendmsg")([b"x"], [], 0, ("192.0.2.1", 53))


@pytest.mark.parametrize(
    "lookup",
    [
        lambda: socket.gethostbyname("example.invalid"),
        lambda: socket.gethostbyaddr("192.0.2.1"),
        lambda: socket.getnameinfo(("192.0.2.1", 80), 0),
    ],
)
def test_other_name_lookups_are_blocked(lookup):
    with pytest.raises(NetworkAccessBlockedError):
        lookup()


@pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="Unix domain sockets are unavailable"
)
def test_unix_socket_connect_is_blocked(tmp_path):
    with (
        socket.socket(posix_int(socket, "AF_UNIX"), socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkAccessBlockedError),
    ):
        sock.connect(str(tmp_path / "sock"))


def test_path_is_empty_so_no_real_adb_can_be_found():
    assert os.environ["PATH"]
    assert not any(os.scandir(os.environ["PATH"]))


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_adb_start_server_is_allowed_and_recorded(tmp_path, processes):
    script, log = recording_adb(tmp_path)
    subprocess.run([str(script), "start-server"], check=True, env=MDNS_OFF)
    assert processes == [[str(script), "start-server"]]
    assert log.read_text().splitlines() == ["start-server"]
    assert (tmp_path / "env.log").read_text().splitlines() == [
        "ADB_MDNS=0",
        "ADDRESS=||",
    ]


def _adb_under(tmp_path: Path) -> tuple[Path, Path | None]:
    """Return an adb path under `tmp_path` and the log it writes, if it can run.

    Where no recording script exists (Windows), the path is a placeholder file:
    a blocked launch is refused before anything starts, so it is never run.
    """
    if sys.platform == "win32":
        placeholder = tmp_path / "adb.exe"
        placeholder.write_bytes(b"")
        return placeholder, None
    return recording_adb(tmp_path)


@pytest.mark.parametrize("env", [None, {"ADB_MDNS": "1"}, {"PATH": "/bin"}])
def test_start_server_without_adb_mdns_off_is_blocked(tmp_path, env):
    script, log = _adb_under(tmp_path)
    with pytest.raises(ProcessLaunchBlockedError, match="ADB_MDNS=0"):
        subprocess.run([str(script), "start-server"], check=False, env=env)
    assert log is None or not log.exists()


def test_every_blocked_attempt_is_recorded(blocked_attempts):
    with pytest.raises(NetworkAccessBlockedError):
        socket.getaddrinfo("example.com", 80)
    with pytest.raises(ProcessLaunchBlockedError):
        os.system("true")  # noqa: S605, S607
    assert len(blocked_attempts) == 2


@pytest.mark.parametrize("arguments", [["devices"], [], ["start-server", "-a"]])
def test_any_other_adb_command_is_blocked(tmp_path, processes, arguments):
    script, log = _adb_under(tmp_path)
    with pytest.raises(ProcessLaunchBlockedError):
        subprocess.run([str(script), *arguments], check=False, env=MDNS_OFF)
    assert processes == [[str(script), *arguments]]
    assert log is None or not log.exists()


def test_adb_start_server_outside_the_test_directory_is_blocked(processes):
    with pytest.raises(ProcessLaunchBlockedError):
        subprocess.run(["/usr/bin/adb", "start-server"], check=False)
    assert processes == [["/usr/bin/adb", "start-server"]]


@pytest.mark.parametrize(
    "arguments",
    [
        ["adb", "start-server"],
        [r"C:\Program Files\adb\adb.exe", "start-server"],
        ["adb", 'say "hi"', "back\\slash\\", "", "a b\\\\"],
        ["C:\\tmp\\x y\\", "end"],
    ],
)
def test_windows_command_lines_are_split_into_the_original_arguments(arguments):
    line = subprocess.list2cmdline(arguments)
    assert split_windows_command_line(line) == arguments


def test_a_command_line_string_is_one_program_name_on_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _command_of("/usr/bin/adb start-server") == ["/usr/bin/adb start-server"]


def test_a_windows_command_line_string_is_split_before_it_is_checked(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert _command_of("/usr/bin/adb start-server") == ["/usr/bin/adb", "start-server"]


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_an_executable_override_outside_the_test_directory_is_blocked(
    tmp_path, processes
):
    # The command line names an adb under tmp_path, but `executable` is what runs.
    script, log = recording_adb(tmp_path)
    with pytest.raises(ProcessLaunchBlockedError):
        subprocess.run(
            [str(script), "start-server"],
            executable="/bin/true",
            check=False,
            env=MDNS_OFF,
        )
    assert processes == [[str(script), "start-server"]]
    assert not log.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell script")
def test_an_executable_override_inside_the_test_directory_is_allowed(tmp_path):
    script, log = recording_adb(tmp_path)
    subprocess.run(
        [str(script), "start-server"], executable=script, check=True, env=MDNS_OFF
    )
    assert log.read_text().splitlines() == ["start-server"]


def test_a_windows_executable_override_outside_the_test_directory_is_blocked(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sys, "platform", "win32")
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    line = subprocess.list2cmdline([str(adb), "start-server"])
    outside = Path(sys.executable).resolve()
    processes: list[list[str]] = []
    root = tmp_path.resolve()
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process(processes, root, line, MDNS_OFF, str(outside), record=True)
    # The same launch without an override, or with one under tmp_path, passes.
    _check_process(processes, root, line, MDNS_OFF, None, record=True)
    _check_process(processes, root, line, MDNS_OFF, str(adb), record=True)


def test_posix_spawn_path_outside_the_test_directory_is_blocked(tmp_path):
    adb = tmp_path / "adb"
    adb.write_bytes(b"")
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process(
            [],
            tmp_path.resolve(),
            [str(adb), "start-server"],
            None,
            "/bin/true",
            record=False,
        )


def test_other_ways_to_start_a_process_are_blocked():
    with pytest.raises(ProcessLaunchBlockedError):
        os.system("true")  # noqa: S605, S607
    with pytest.raises(ProcessLaunchBlockedError):
        os.execv("/bin/true", ["true"])  # noqa: S606


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is POSIX only")
def test_fork_is_blocked():
    with pytest.raises(ProcessLaunchBlockedError):
        posix_call(os, "fork")()


@pytest.mark.skipif(
    not hasattr(os, "posix_spawn"), reason="os.posix_spawn is POSIX only"
)
def test_posix_spawn_is_blocked():
    with pytest.raises(ProcessLaunchBlockedError):
        posix_call(os, "posix_spawn")("/bin/true", ["/bin/true"], {})


def _only_empty_path() -> dict[str, str]:
    """Return an env that passes the ADB_MDNS check but whose PATH is empty.

    The guard fixture points PATH at an empty directory outside tmp_path, so
    even a launch the guard wrongly allowed could not run a real program.
    """
    path = os.environ["PATH"]
    assert not any(os.scandir(path))
    # Built at call time: the module-level snapshot predates the guard fixture's
    # environment clean-up and could carry a developer's adb address variables.
    return {**os.environ, "ADB_MDNS": "0"}


@pytest.fixture
def no_real_launch(monkeypatch):
    """Make sure that no negative test below can start a real program.

    On Windows, CreateProcessW searches the directory of python.exe before the
    current directory, so an adb.exe beside python.exe would run if the guard
    regressed. The guard's audit event fires before _winapi.CreateProcess is
    called, so a stub that refuses to launch turns such a regression into a
    test failure instead of a real launch. On POSIX nothing is needed: PATH is
    empty and the placeholders are not executable.
    """
    if sys.platform == "win32":

        def refuse(*_args, **_kwargs):
            message = "a blocked launch reached CreateProcess"
            raise AssertionError(message)

        monkeypatch.setattr("_winapi.CreateProcess", refuse)


@pytest.mark.parametrize("executable", ["adb", "./adb"])
@pytest.mark.usefixtures("no_real_launch")
def test_a_relative_executable_is_blocked_even_if_it_exists_in_the_cwd(
    monkeypatch, tmp_path, processes, executable
):
    # The command line names an absolute adb under tmp_path, but Popen would
    # search PATH (empty here) for the bare name, or resolve "./adb" to a
    # non-executable placeholder, so nothing can run.
    script, log = _adb_under(tmp_path)
    (tmp_path / "adb").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProcessLaunchBlockedError):
        subprocess.run(
            [str(script), "start-server"],
            executable=executable,
            check=False,
            env=_only_empty_path(),
        )
    assert processes == [[str(script), "start-server"]]
    assert log is None or not log.exists()


@pytest.mark.parametrize("program", ["adb", "./adb"])
@pytest.mark.usefixtures("no_real_launch")
def test_a_relative_argv0_is_blocked_even_if_it_exists_in_the_cwd(
    monkeypatch, tmp_path, processes, program
):
    _script, log = _adb_under(tmp_path)
    (tmp_path / "adb").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProcessLaunchBlockedError):
        subprocess.run(
            [program, "start-server"],
            check=False,
            env=_only_empty_path(),
        )
    assert processes == [[program, "start-server"]]
    assert log is None or not log.exists()


@pytest.mark.parametrize("relative", ["adb", "./adb", "tools/adb"])
@pytest.mark.parametrize("as_executable", [False, True])
@pytest.mark.usefixtures("no_real_launch")
def test_a_relative_program_with_a_popen_cwd_inside_tmp_path_is_blocked(
    monkeypatch, tmp_path, processes, relative, as_executable
):
    # From the test's cwd (tmp_path) the relative name points at a placeholder
    # under tmp_path, but Popen resolves it against cwd=sub, where nothing
    # exists. A guard that resolved it against the test's cwd would allow it.
    script, log = _adb_under(tmp_path)
    (tmp_path / "adb").write_bytes(b"")
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "adb").write_bytes(b"")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(tmp_path)
    argv = [str(script) if as_executable else relative, "start-server"]
    with pytest.raises(ProcessLaunchBlockedError):
        subprocess.run(
            argv,
            executable=relative if as_executable else None,
            cwd=sub,
            check=False,
            env=_only_empty_path(),
        )
    assert processes == [argv]
    assert log is None or not log.exists()


@pytest.mark.skipif(
    not hasattr(os, "posix_spawn"), reason="os.posix_spawn is POSIX only"
)
@pytest.mark.parametrize("name", ["posix_spawn", "posix_spawnp"])
@pytest.mark.parametrize("relative", ["adb", "./adb", "tools/adb"])
def test_posix_spawn_with_a_relative_program_is_blocked(
    monkeypatch, tmp_path, name, relative
):
    # A non-executable placeholder: nothing can run even if the guard failed.
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "adb").write_bytes(b"")
    (tmp_path / "tools" / "adb").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    for path, argv in (
        (relative, [str(tmp_path / "adb"), "start-server"]),
        (str(tmp_path / "adb"), [relative, "start-server"]),
    ):
        with pytest.raises(ProcessLaunchBlockedError):
            posix_call(os, name)(path, argv, _only_empty_path())


@pytest.mark.parametrize(
    "convert", [str, os.fsencode, Path], ids=["str", "bytes", "path"]
)
@pytest.mark.parametrize(
    "relative", ["adb", "./adb", "tools/adb", "tools/../adb", "../adb"]
)
def test_check_process_blocks_relative_programs(
    monkeypatch, tmp_path, convert, relative
):
    adb = tmp_path / "adb"
    adb.write_bytes(b"")
    root = tmp_path.resolve()
    monkeypatch.chdir(tmp_path)
    command = [str(adb), "start-server"]
    # A relative executable, with an absolute argv[0].
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process(
            [], root, command, {"ADB_MDNS": "0"}, convert(relative), record=True
        )
    # A relative argv[0] of the same type (Popen passes it unconverted).
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process(
            [],
            root,
            [convert(relative), "start-server"],
            {"ADB_MDNS": "0"},
            record=True,
        )
    # The absolute form of the same executable is still allowed.
    _check_process([], root, command, {"ADB_MDNS": "0"}, convert(str(adb)), record=True)


@pytest.mark.parametrize("relative", ["adb", "./adb", "adb.exe", "bin/adb.exe"])
def test_check_process_blocks_relative_programs_in_a_windows_command_line(
    monkeypatch, tmp_path, relative
):
    monkeypatch.setattr(sys, "platform", "win32")
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    root = tmp_path.resolve()
    monkeypatch.chdir(tmp_path)
    line = subprocess.list2cmdline([relative, "start-server"])
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process([], root, line, {"ADB_MDNS": "0"}, None, record=True)
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process([], root, line, {"ADB_MDNS": "0"}, relative, record=True)
    absolute_line = subprocess.list2cmdline([str(adb), "start-server"])
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process(
            [], root, absolute_line, {"ADB_MDNS": "0"}, relative, record=True
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
@pytest.mark.parametrize("form", ["root_relative", "drive_relative"])
def test_check_process_blocks_windows_relative_path_forms(monkeypatch, tmp_path, form):
    # pathlib treats both forms as not absolute on Windows.
    adb = tmp_path / "adb.exe"
    adb.write_bytes(b"")
    root = tmp_path.resolve()
    # Resolving against the current directory must not make the forms look
    # like paths under tmp_path, so work from inside it.
    monkeypatch.chdir(tmp_path)
    if form == "root_relative":
        relative = str(tmp_path)[len(tmp_path.drive) :] + "\\adb.exe"
    else:
        relative = tmp_path.drive + "adb.exe"
    line = subprocess.list2cmdline([relative, "start-server"])
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process([], root, line, {"ADB_MDNS": "0"}, None, record=True)
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process(
            [], root, [relative, "start-server"], {"ADB_MDNS": "0"}, record=True
        )
    with pytest.raises(ProcessLaunchBlockedError):
        _check_process(
            [],
            root,
            [str(adb), "start-server"],
            {"ADB_MDNS": "0"},
            relative,
            record=True,
        )
