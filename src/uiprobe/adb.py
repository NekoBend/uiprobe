"""A small client for the local adb server.

This is the only module that opens a socket or starts a process. It connects
to one address (the adb server) and, when the server is not running and the
address is the default one, runs ``adb start-server`` once.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import re
import selectors
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from uiprobe.errors import (
    AdbError,
    AdbServerUnavailableError,
    DeviceNotFoundError,
    DeviceOfflineError,
    DeviceUnauthorizedError,
    UiprobeError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_SERVER",
    "AdbClient",
    "DeviceInfo",
    "ServerAddress",
    "find_adb",
    "parse_devices_l",
    "quote_args",
    "resolve_server_address",
]

DEFAULT_PORT: Final = 5037
MAX_OUTPUT_BYTES: Final = 64 * 1024 * 1024
START_SERVER_TIMEOUT: Final = 30.0
# How long to keep retrying the connection after `adb start-server`.
START_RETRY_SECONDS: Final = 5.0
START_RETRY_INTERVAL: Final = 0.1
MAX_INT_DIGITS: Final = 9

_HEX4 = re.compile(rb"[0-9a-fA-F]{4}")
# A token such as ``model:Pixel`` or ``some_future_key:x`` starts the fields
# of a ``devices -l`` line. The bracketed URL in a "no permissions" state
# starts with ``[`` and so never matches.
_FIELD_TOKEN = re.compile(r"[a-z][a-z0-9_]*:")
# adb prints this in place of the serial of a device that has none.
_NO_SERIAL = re.compile(r"\(no serial number\)(?=\s|$)")
_ADB_ADDRESS_VARIABLE = "ANDROID_ADB_SERVER_ADDRESS"
# Variables that choose the adb server address; a started server gets none.
_ADDRESS_VARIABLES: Final = frozenset(
    {"ADB_SERVER_SOCKET", _ADB_ADDRESS_VARIABLE, "ANDROID_ADB_SERVER_PORT"}
)


def start_blocked_by_environment() -> bool:
    """Whether a non-empty ``ANDROID_ADB_SERVER_ADDRESS`` forbids a start.

    An empty value counts as unset, like the other address variables.
    """
    return bool(os.environ.get(_ADB_ADDRESS_VARIABLE))


# A zone is a short interface name or index; checked before any lookup.
_ZONE_PATTERN: Final = re.compile(r"[0-9A-Za-z_-]+(?:\.[0-9A-Za-z_-]+)*")
_ZONE_MAX_LENGTH = 63
_ADDRESS_FORMAT = "expected tcp:HOST:PORT"
# Why the server was not started, for the message of AdbServerUnavailableError.
_START_HINTS: Final = {
    "disabled": "starting the server is disabled",
    "address_not_default": "adb start-server is not run for an address you chose",
    "adb_not_found": "adb was not found; install platform-tools or set UIPROBE_ADB",
    "android_adb_server_address_set": (
        "ANDROID_ADB_SERVER_ADDRESS is set, so adb start-server is not run; "
        "give the server address as ADB_SERVER_SOCKET=tcp:IP:PORT or --adb-server"
    ),
}


@dataclass(frozen=True, slots=True)
class ServerAddress:
    """Where the adb server listens.

    `is_default` is True only for `DEFAULT_SERVER`: it marks an address the
    user did not choose, which is the only case where uiprobe may start adb.
    """

    host: str
    port: int
    is_default: bool = False

    def __post_init__(self) -> None:
        # Normalizes "localhost"; the field is frozen, so set it directly.
        object.__setattr__(self, "host", _literal_host(self.host, "adb server address"))

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"tcp:{host}:{self.port}"


def _literal_host(host: str, source: str, *, bracketed: bool = False) -> str:
    """Return `host` as an IP address literal, mapping ``localhost`` to 127.0.0.1.

    A bracketed IPv6 literal may carry a zone (``fe80::1%eth0`` or
    ``fe80::1%3``); the zone is kept in the result. Host names are refused so
    that connecting never makes a DNS query.

    Raises:
        ValueError: `host` is not an IP address literal or ``localhost``, or
            its zone names an interface that does not exist.
    """
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    if host.lower() == "localhost" and not bracketed:
        return "127.0.0.1"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if bracketed and not isinstance(ip, ipaddress.IPv6Address):
        ip = None  # brackets are for IPv6 literals only
    if ip is None:
        msg = (
            f"{source}: host {host!r} is not accepted; use an IP address "
            "(IPv6 in brackets) or localhost, because host names are never resolved"
        )
        raise ValueError(msg)
    if (
        isinstance(ip, ipaddress.IPv6Address)
        and ip.scope_id
        and _scope_index(ip.scope_id) is None
    ):
        msg = f"{source}: host {host!r} names an unknown network interface"
        raise ValueError(msg)
    return str(ip)


def _start_server_environment() -> dict[str, str]:
    """Return the environment for ``adb start-server``: no address, mDNS off."""
    env = {k: v for k, v in os.environ.items() if k not in _ADDRESS_VARIABLES}
    env["ADB_MDNS"] = "0"
    return env


def _scope_index(zone: str) -> int | None:
    """Return the interface index for an IPv6 zone, or None if there is none."""
    if len(zone) > _ZONE_MAX_LENGTH or _ZONE_PATTERN.fullmatch(zone) is None:
        return None
    if zone.isdigit():
        return int(zone)
    try:
        return socket.if_nametoindex(zone)
    except (OSError, ValueError):
        return None


DEFAULT_SERVER = ServerAddress("127.0.0.1", DEFAULT_PORT, is_default=True)


def _new_socket(family: socket.AddressFamily) -> socket.socket:
    return socket.socket(family, socket.SOCK_STREAM)


def _parse_port(text: str, source: str) -> int:
    port = ascii_int(text)
    if port is None or not 1 <= port <= 65535:
        msg = f"{source}: invalid port {text!r} (expected 1-65535)"
        raise ValueError(msg)
    return port


def _parse_tcp_address(value: str, source: str) -> ServerAddress:
    """Parse ``tcp:HOST:PORT``; HOST may be a bracketed IPv6 literal."""
    prefix = "tcp:"
    rest = value[len(prefix) :] if value.startswith(prefix) else None
    if rest is None:
        msg = f"{source}: invalid address {value!r} ({_ADDRESS_FORMAT})"
        raise ValueError(msg)
    bracketed = False
    if rest.startswith("["):
        host, bracket, tail = rest[1:].partition("]")
        port_text = tail[1:] if bracket and tail.startswith(":") else ""
        bracketed = True
    else:
        host, _, port_text = rest.rpartition(":")
        if ":" in host:
            host = ""
    if not host or not port_text:
        msg = f"{source}: invalid address {value!r} ({_ADDRESS_FORMAT})"
        raise ValueError(msg)
    port = _parse_port(port_text, source)
    return ServerAddress(_literal_host(host, source, bracketed=bracketed), port)


def resolve_server_address(
    adb_server: str | None = None, environ: Mapping[str, str] | None = None
) -> ServerAddress:
    """Pick the adb server address.

    Order: the argument, ``ADB_SERVER_SOCKET``, ``ANDROID_ADB_SERVER_PORT``
    (port only, on 127.0.0.1), then `DEFAULT_SERVER`. Only empty environment
    values count as unset: an empty argument is a malformed address.

    Raises:
        TypeError: The argument is not a string.
        ValueError: A value is malformed; the message names its source.
    """
    env = os.environ if environ is None else environ
    if adb_server is not None:
        if not isinstance(adb_server, str):
            msg = f"adb_server must be a string, not {type(adb_server).__name__}"
            raise TypeError(msg)
        return _parse_tcp_address(adb_server, "adb server argument")
    if socket_value := env.get("ADB_SERVER_SOCKET"):
        return _parse_tcp_address(socket_value, "ADB_SERVER_SOCKET")
    if port_value := env.get("ANDROID_ADB_SERVER_PORT"):
        port = _parse_port(port_value, "ANDROID_ADB_SERVER_PORT")
        return ServerAddress("127.0.0.1", port)
    return DEFAULT_SERVER


def _existing_file(value: str | os.PathLike[str], source: str) -> Path:
    """Return the absolute path of an existing file, or raise `ValueError`.

    The path is made absolute so that the file that is run is the file that
    was checked: a bare name would be searched on PATH when it is executed.
    """
    path = Path(value)
    if not path.is_file():
        msg = f"{source}: not an existing file: {path}"
        raise ValueError(msg)
    return path.absolute()


def _search_path(path_value: str, name: str) -> Path | None:
    """Find the executable `name` in the absolute directories of `path_value`.

    Unlike `shutil.which`, this never looks in the current directory, which
    Windows searches first, and it skips relative and empty PATH entries.
    """
    for entry in path_value.split(os.pathsep):
        directory = Path(entry.strip('"'))
        if not entry or not directory.is_absolute():
            continue
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_adb(
    adb: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Locate the adb executable without running it.

    Order: the argument, ``UIPROBE_ADB``, ``adb`` on PATH (absolute entries
    only), then ``platform-tools/adb`` under ``ANDROID_HOME`` and
    ``ANDROID_SDK_ROOT``. The result is an absolute path.

    Returns:
        The path, or None when adb cannot be found.

    Raises:
        TypeError: The argument is neither a string nor a path.
        ValueError: The argument or ``UIPROBE_ADB`` is not an existing file.
            An empty argument is an error; an empty ``UIPROBE_ADB`` is unset.
    """
    env = os.environ if environ is None else environ
    if adb is not None:
        return _existing_file(adb, "adb argument")
    if configured := env.get("UIPROBE_ADB"):
        return _existing_file(configured, "UIPROBE_ADB")
    name = "adb.exe" if sys.platform == "win32" else "adb"
    if on_path := _search_path(env.get("PATH", ""), name):
        return on_path
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if root := env.get(variable):
            candidate = Path(root) / "platform-tools" / name
            if candidate.is_file():
                return candidate.absolute()
    return None


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One line of ``adb devices -l``."""

    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    device: str | None = None
    transport_id: int | None = None


def ascii_int(text: str) -> int | None:
    """Return `text` as an int when it is 1 to 9 ASCII digits, else None.

    The length limit keeps `int` away from the huge strings it refuses to parse
    (Python raises `ValueError` above 4300 digits).
    """
    if text.isascii() and text.isdigit() and len(text) <= MAX_INT_DIGITS:
        return int(text)
    return None


def _split_serial(line: str) -> tuple[str, list[str]] | None:
    """Split a ``devices -l`` line into the serial and the tokens after it."""
    if no_serial := _NO_SERIAL.match(line):
        return no_serial.group(), line[no_serial.end() :].split()
    parts = line.split()
    return (parts[0], parts[1:]) if parts else None


def _parse_device_line(line: str) -> DeviceInfo | None:
    head = _split_serial(line)
    if head is None:
        return None
    serial, tokens = head
    split = next(
        (i for i, t in enumerate(tokens) if _FIELD_TOKEN.match(t)), len(tokens)
    )
    state = " ".join(tokens[:split])
    if not state:
        return None
    fields = dict(t.split(":", 1) for t in tokens[split:] if ":" in t)
    return DeviceInfo(
        serial=serial,
        state=state,
        model=fields.get("model"),
        product=fields.get("product"),
        device=fields.get("device"),
        transport_id=ascii_int(fields.get("transport_id", "")),
    )


def parse_devices_l(text: str) -> list[DeviceInfo]:
    """Parse the output of ``host:devices-l``.

    The state may contain spaces (``no permissions (...)``): it is everything
    after the serial up to the first token that looks like a ``key:value``
    field, whatever the key. Unknown keys are ignored and lines that cannot be
    parsed are skipped. The serial of a device without one is the literal
    ``(no serial number)``.
    """
    devices = (_parse_device_line(line) for line in text.splitlines())
    return [device for device in devices if device is not None]


def quote_args(*args: str) -> str:
    """Join arguments into one device shell command line, quoted."""
    return shlex.join(args)


def _parse_hex4(raw: bytes) -> int | None:
    return int(raw, 16) if _HEX4.fullmatch(raw) else None


WAIT_SLICE: Final = 0.25
"""Longest single blocking wait on the adb socket, in seconds.

A blocking socket call may not be interrupted by Ctrl+C on Windows until it
returns. Every wait is therefore made in slices of this length, so that the
interpreter regains control (and can raise KeyboardInterrupt) at least this
often. The logical timeout is kept by the helpers below, not by the slice.
"""

# What a non-blocking connect returns while the connection is being made.
# EINTR: a Python signal handler that does not raise ran, and the kernel keeps
# connecting in the background.
_CONNECT_PENDING: Final = frozenset(
    {getattr(errno, "WSAEWOULDBLOCK", errno.EWOULDBLOCK)}
    if sys.platform == "win32"
    else {errno.EINPROGRESS, errno.EALREADY, errno.EINTR}
)


def _os_error(code: int) -> OSError:
    """An `OSError` for the socket error `code`, with a readable message."""
    message = os.strerror(code)
    if sys.platform == "win32" and message.startswith("Unknown error"):
        # The C runtime does not know Winsock codes such as 10061.
        message = f"Winsock error {code}"
    return OSError(code, message)


def _recv(sock: socket.socket, size: int) -> bytes:
    """Like ``sock.recv(size)``, waiting in slices of `WAIT_SLICE`.

    The timeout is the one `sock` has: the silence allowed before data arrives.
    It is measured from the call, so each call (that is, each arrival of data)
    starts it again. `TimeoutError` is raised once it has passed, and `sock`
    has its own timeout again whatever happens.
    """
    timeout = sock.gettimeout()
    if timeout is not None and timeout <= 0:
        return sock.recv(size)  # non-blocking: nothing to slice
    deadline = None if timeout is None else time.perf_counter() + timeout
    try:
        while True:
            if deadline is None:
                piece = WAIT_SLICE
            else:
                piece = min(WAIT_SLICE, deadline - time.perf_counter())
                if piece <= 0:
                    msg = "timed out"
                    raise TimeoutError(msg)
            sock.settimeout(piece)
            try:
                return sock.recv(size)
            except TimeoutError as exc:
                if exc.errno is not None:  # a real ETIMEDOUT, not a slice ending
                    raise
    finally:
        sock.settimeout(timeout)


def _connect_sliced(
    sock: socket.socket,
    address: tuple[str, int] | tuple[str, int, int, int],
    timeout: float,
) -> None:
    """Connect `sock` to `address`, waiting in slices of `WAIT_SLICE`.

    Raises `TimeoutError` when the connection is not made within `timeout`
    seconds, or `OSError` when it fails. `sock` is left with `timeout`.
    """
    sock.settimeout(0.0)  # non-blocking
    try:
        status = sock.connect_ex(address)
        if status != 0 and status not in _CONNECT_PENDING:
            raise _os_error(status)
        if status != 0:
            deadline = time.perf_counter() + timeout
            selector = selectors.DefaultSelector()
            try:
                selector.register(sock, selectors.EVENT_WRITE)
                while True:
                    piece = min(WAIT_SLICE, deadline - time.perf_counter())
                    if piece <= 0:
                        msg = "timed out"
                        raise TimeoutError(msg)
                    # Not select.select: it raises ValueError for a descriptor
                    # of 1024 or more on POSIX. On Windows the selector's
                    # select() also reports the exceptional set (a failed
                    # connect) as writable.
                    if selector.select(piece):
                        break
            finally:
                selector.close()
            error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise _os_error(error)
    finally:
        sock.settimeout(timeout)


def _timeout_error(
    message: str, sock: socket.socket, request: str | None, *, stage: str | None = None
) -> AdbError:
    """Build the error for a timeout, reporting the timeout `sock` had.

    `stage` is "output" only for a timeout while reading a service's output
    (after its OKAY); a timeout in the request/OKAY handshake has no stage.
    """
    details: dict[str, object] = {
        "reason": "timeout",
        "timeout_seconds": sock.gettimeout(),
    }
    if request is not None:
        details["request"] = request
    if stage is not None:
        details["stage"] = stage
    return AdbError(message, details=details)


def _request_details(request: str | None) -> dict[str, object] | None:
    return None if request is None else {"request": request}


def _read_exact(sock: socket.socket, size: int, request: str | None = None) -> bytes:
    """Read exactly `size` bytes or raise `AdbError` (carrying `request`)."""
    chunks: list[bytes] = []
    remaining = size
    try:
        while remaining:
            chunk = _recv(sock, remaining)
            if not chunk:
                msg = "adb server closed the connection before the response ended"
                raise AdbError(msg, details=_request_details(request))
            chunks.append(chunk)
            remaining -= len(chunk)
    except TimeoutError as exc:
        message = "timed out waiting for the adb server"
        raise _timeout_error(message, sock, request) from exc
    except OSError as exc:
        msg = f"connection to the adb server failed: {exc}"
        raise AdbError(msg, details=_request_details(request)) from exc
    return b"".join(chunks)


def _read_to_eof(sock: socket.socket, request: str) -> bytes:
    """Read until the server closes the connection, bounded by the size limit."""
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := _recv(sock, 65536):
            total += len(chunk)
            if total > MAX_OUTPUT_BYTES:
                msg = f"output from the device is larger than {MAX_OUTPUT_BYTES} bytes"
                raise AdbError(msg, details={"request": request})
            chunks.append(chunk)
    except TimeoutError as exc:
        message = "timed out reading output from the adb server"
        raise _timeout_error(message, sock, request, stage="output") from exc
    except OSError as exc:
        msg = f"connection to the adb server failed: {exc}"
        raise AdbError(msg, details={"request": request}) from exc
    return b"".join(chunks)


def _read_length_prefixed(sock: socket.socket, request: str) -> bytes:
    length = _parse_hex4(_read_exact(sock, 4, request))
    if length is None:
        msg = "adb server sent an invalid length"
        raise AdbError(msg, details={"request": request})
    return _read_exact(sock, length, request)


def _send_request(sock: socket.socket, request: str) -> str | None:
    """Send `request` and read the status.

    Returns:
        None for OKAY, or the server's message for FAIL.

    Raises:
        AdbError: The status is unknown or the exchange broke.
    """
    payload = request.encode("utf-8")
    if len(payload) > 0xFFFF:
        msg = "request is too long for the adb protocol"
        raise ValueError(msg)
    try:
        sock.sendall(f"{len(payload):04x}".encode("ascii") + payload)
    except OSError as exc:
        msg = f"sending to the adb server failed: {exc}"
        raise AdbError(msg, details={"request": request}) from exc
    status = _read_exact(sock, 4, request)
    if status == b"OKAY":
        return None
    if status == b"FAIL":
        return _read_length_prefixed(sock, request).decode("utf-8", "replace")
    msg = "adb server sent an unknown status"
    raise AdbError(msg, details={"request": request, "status": status.hex()})


def _fail(request: str, message: str) -> AdbError:
    return AdbError(
        f"adb server refused the request: {message}",
        details={"request": request, "message": message},
    )


def _transport_error(serial: str, message: str) -> UiprobeError:
    lowered = message.lower()
    details = {"serial": serial, "message": message}
    if "not found" in lowered or "no device" in lowered:
        return DeviceNotFoundError(f"device {serial!r} not found", details=details)
    if "unauthorized" in lowered:
        return DeviceUnauthorizedError(
            f"device {serial!r} is unauthorized", details=details
        )
    if "offline" in lowered:
        return DeviceOfflineError(f"device {serial!r} is offline", details=details)
    return _fail(f"host:transport:{serial}", message)


class AdbClient:
    """Talks to the adb server at one address.

    Every request uses a new connection. When the server is down, the client
    runs ``adb start-server`` once, but only if `start_server` is True, the
    address is the default one, and `adb_path` is known.
    """

    def __init__(
        self,
        address: ServerAddress = DEFAULT_SERVER,
        *,
        adb_path: Path | None = None,
        start_server: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self._address = address
        self._adb_path = adb_path
        self._start_server = start_server
        self._timeout = timeout
        self._start_attempted = False

    @property
    def address(self) -> ServerAddress:
        """The adb server this client talks to."""
        return self._address

    def stop_starting_server(self) -> None:
        """Never start the adb server from now on: a request that cannot
        connect fails with `AdbServerUnavailableError` instead of going to a
        newly started server (which numbers its transports anew)."""
        self._start_server = False

    def server_version(self) -> int:
        """Return the adb server protocol version."""
        raw = self._host_query("host:version")
        version = _parse_hex4(raw)
        if version is None:
            msg = "adb server sent an invalid version"
            raise AdbError(msg, details={"request": "host:version"})
        return version

    def devices(self) -> list[DeviceInfo]:
        """List the devices the server knows (``host:devices-l``)."""
        text = self._host_query("host:devices-l").decode("utf-8", "replace")
        return parse_devices_l(text)

    def shell(
        self,
        serial: str,
        command: str,
        *,
        timeout: float | None = None,
        transport_id: int | None = None,
    ) -> bytes:
        """Run `command` on the device with the ``shell:`` service.

        `timeout` replaces the client's timeout for reading the command's
        output (the longest silence allowed); the handshake keeps the client's.
        `transport_id`, when given, selects the device by that transport
        (``host:transport-id:N``) instead of by `serial`, so the request goes
        to that very connection or fails; `serial` then only names it in
        errors.
        """
        return self._device_service(serial, f"shell:{command}", timeout, transport_id)

    def exec_out(
        self,
        serial: str,
        command: str,
        *,
        timeout: float | None = None,
        transport_id: int | None = None,
    ) -> bytes:
        """Run `command` with the binary-safe ``exec:`` service.

        `timeout` and `transport_id` are as for `shell`.
        """
        return self._device_service(serial, f"exec:{command}", timeout, transport_id)

    def _host_query(self, request: str) -> bytes:
        with self._connect() as sock:
            message = _send_request(sock, request)
            if message is not None:
                raise _fail(request, message)
            return _read_length_prefixed(sock, request)

    def _device_service(
        self,
        serial: str,
        service: str,
        read_timeout: float | None,
        transport_id: int | None = None,
    ) -> bytes:
        if transport_id is None:
            selector = f"host:transport:{serial}"
        elif isinstance(transport_id, bool) or not isinstance(transport_id, int):
            msg = f"transport_id must be an int, not {type(transport_id).__name__}"
            raise TypeError(msg)
        else:
            selector = f"host:transport-id:{transport_id:d}"
        with self._connect() as sock:
            message = _send_request(sock, selector)
            if message is not None:
                raise _transport_error(serial, message)
            message = _send_request(sock, service)
            if message is not None:
                raise _fail(service, message)
            if read_timeout is not None:
                sock.settimeout(read_timeout)
            return _read_to_eof(sock, service)

    def _open(self) -> socket.socket:
        # Connect to the literal address with the matching family. Unlike
        # `socket.create_connection`, this never calls getaddrinfo.
        host = self._address.host
        address: tuple[str, int] | tuple[str, int, int, int] = (
            host,
            self._address.port,
        )
        if "%" in host:
            literal, _, zone = host.partition("%")
            scope = _scope_index(zone)
            if scope is None:
                msg = f"adb server address: unknown network interface in {host!r}"
                raise ValueError(msg)
            address = (literal, self._address.port, 0, scope)
        sock = _new_socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        try:
            _connect_sliced(sock, address, self._timeout)
        except BaseException:
            sock.close()
            raise
        return sock

    def _connect(self) -> socket.socket:
        """Connect to the server, starting it first when allowed."""
        try:
            return self._open()
        except OSError as exc:
            reason = f"cannot connect to the adb server: {exc}"
        skipped = self._start_blocker()
        if skipped is None:
            start_reason = self._run_start_server()
            if start_reason is None:
                sock = self._retry_open()
                if sock is not None:
                    return sock
                start_reason = "the adb server did not come up after start-server"
            reason = start_reason
        raise self._unavailable(reason, skipped)

    def _unavailable(
        self, reason: str, skipped: str | None
    ) -> AdbServerUnavailableError:
        message = f"adb server is unavailable at {self._address}"
        details: dict[str, object] = {
            "address": str(self._address),
            "start_attempted": self._start_attempted,
            "reason": reason,
        }
        if skipped in _START_HINTS:
            details["start_skipped"] = skipped
            message += f" ({_START_HINTS[skipped]})"
        return AdbServerUnavailableError(message, details=details)

    def _start_blocker(self) -> str | None:
        """Return why ``adb start-server`` may not run now, or None if it may."""
        if not self._start_server:
            return "disabled"
        if not self._address.is_default:
            return "address_not_default"
        if start_blocked_by_environment():
            return "android_adb_server_address_set"
        if self._adb_path is None:
            return "adb_not_found"
        if self._start_attempted:
            return "already_attempted"
        return None

    def _run_start_server(self) -> str | None:
        """Run ``adb start-server`` once; return a reason when it failed.

        The child gets ``ADB_MDNS=0`` so a server uiprobe starts does not send
        mDNS queries onto the local network, and none of the variables that
        choose a server address.
        """
        self._start_attempted = True
        try:
            result = subprocess.run(
                [str(self._adb_path), "start-server"],
                check=False,
                capture_output=True,
                timeout=START_SERVER_TIMEOUT,
                env=_start_server_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"adb start-server could not run: {exc}"
        if result.returncode != 0:
            return f"adb start-server exited with status {result.returncode}"
        return None

    def _retry_open(self) -> socket.socket | None:
        deadline = time.monotonic() + START_RETRY_SECONDS
        while True:
            try:
                return self._open()
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(START_RETRY_INTERVAL)
