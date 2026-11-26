"""A scripted adb server for tests.

It speaks the part of the adb server protocol that uiprobe uses, on a loopback
port, and records every request it receives. Behaviour is scripted from
Python: the device list, the output of shell and exec commands, requests that
fail, and connections that break in the middle of a response.
"""

from __future__ import annotations

import re
import socket
import socketserver
import struct
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast

from tests.helpers import allow_port

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import TracebackType

type Reply = bytes | Fail | Stall | Trickle | Reset
type Response = Reply | Callable[[str], Reply]
type Matcher = str | re.Pattern[str]
# An allowlist: one list for every kind, or a list per service kind.
type Allowlist = Sequence[Matcher] | Mapping[str, Sequence[Matcher]]
KINDS = ("shell", "exec")

OKAY = b"OKAY"
FAIL = b"FAIL"


@dataclass(frozen=True, slots=True)
class Fail:
    """A scripted FAIL answer carrying `message`."""

    message: str


@dataclass(frozen=True, slots=True)
class Stall:
    """Answer OKAY, send `prefix`, then go silent until the server stops."""

    prefix: bytes = b""


@dataclass(frozen=True, slots=True)
class Trickle:
    """Answer OKAY, wait `lead` s, then send `data` in `chunk_size` pieces.

    The pieces are `delay` s apart. `lead` is the silence of a slow command.
    """

    data: bytes
    chunk_size: int = 1
    delay: float = 0.0
    lead: float = 0.0


@dataclass(frozen=True, slots=True)
class Reset:
    """Reset the connection (RST) instead of answering.

    From a callable this is evaluated before OKAY, so a command can take
    effect on the device and then lose its connection before any reply.
    """


_HEADER = re.compile(rb"[0-9a-fA-F]{4}")


def frame(payload: bytes) -> bytes:
    """Return `payload` with its 4-hex-digit length prefix."""
    return f"{len(payload):04x}".encode("ascii") + payload


def render_devices(devices: list[dict[str, object]]) -> str:
    """Render device dicts the way ``host:devices-l`` prints them."""
    lines = []
    for device in devices:
        parts = [f"{device['serial']:<22}", str(device.get("state", "device"))]
        parts.extend(
            f"{key}:{device[key]}"
            for key in ("product", "model", "device", "transport_id")
            if key in device
        )
        lines.append(" ".join(parts))
    return "".join(f"{line}\n" for line in lines)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True

    def __init__(self, fake: FakeAdbServer, port: int) -> None:
        self.fake = fake
        super().__init__(("127.0.0.1", port), _Handler)

    def handle_error(self, request: object, client_address: object) -> None:
        """Keep a crashed handler's exception instead of printing it."""
        del request, client_address
        exc = sys.exception()
        self.fake.handler_errors.append(repr(exc))


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        fake = cast("_Server", self.server).fake
        # Without this, Nagle's algorithm may merge small writes into one
        # segment, and "chunked" replies would not really arrive in pieces.
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        fake._track(self.request, add=True)
        try:
            self._serve(fake)
        except OSError:
            return
        finally:
            fake._track(self.request, add=False)

    def _recv_exact(self, size: int) -> bytes | None:
        data = b""
        while len(data) < size:
            chunk = self.request.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _send(self, fake: FakeAdbServer, data: bytes) -> None:
        """Write `data`, in small pieces when the fake is told to trickle."""
        size = fake.chunk_size
        if size is None:
            self.request.sendall(data)
            return
        for start in range(0, len(data), size):
            self.request.sendall(data[start : start + size])
            fake._stop.wait(fake.chunk_delay)

    def _read_request(self, fake: FakeAdbServer) -> str | None:
        """Read one request; a framing violation is recorded and answered FAIL."""
        header = self._recv_exact(4)
        if header is None:
            return None
        if not _HEADER.fullmatch(header):
            return self._protocol_error(fake, f"invalid length header {header!r}")
        payload = self._recv_exact(int(header, 16))
        if payload is None:
            return None
        fake.wire.append(header + payload)
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return self._protocol_error(fake, f"request is not UTF-8: {payload!r}")

    def _protocol_error(self, fake: FakeAdbServer, problem: str) -> None:
        fake.protocol_errors.append(problem)
        self.request.sendall(FAIL + frame(b"protocol fault"))
        # Closing with unread input would send a RST that can destroy the FAIL
        # before the client reads it: finish the reply, then discard to EOF.
        self.request.shutdown(socket.SHUT_WR)
        while self.request.recv(4096):
            pass

    def _serve(self, fake: FakeAdbServer) -> None:
        bound: str | None = None
        while (request := self._read_request(fake)) is not None:
            fake.requests.append(request)
            if self._refused(fake, bound, request):
                return
            if self._scripted(fake, request):
                return
            if request.startswith(("host:transport:", "host:transport-id:")):
                bound = self._select(fake, request)
                if bound is None:
                    return
            elif request.startswith(("shell:", "exec:")) and bound is not None:
                self._answer_service(fake, bound, request)
                return
            elif request.startswith("host:"):
                self._answer_host(fake, request)
                return
            else:
                fake._note_unknown(f"request {request}")
                self._send(fake, FAIL + frame(b"unknown request"))
                return

    def _refused(self, fake: FakeAdbServer, bound: str | None, request: str) -> bool:
        """Judge a shell or exec command before any script can end the request.

        A script does not admit a command. Answers FAIL and returns True when
        strict mode refuses it, or when no transport was selected first.
        """
        if not request.startswith(("shell:", "exec:")):
            return False
        kind, _, command = request.partition(":")
        if bound is None:
            if not fake._note_unbound(request):
                return False
            message = b"no transport selected"
        elif fake._admit(bound, kind, command):
            return False
        else:
            message = b"command is not allowed by the test"
        self._send(fake, FAIL + frame(message))
        return True

    def _scripted(self, fake: FakeAdbServer, request: str) -> bool:
        """Apply per-request scripts; return True when the connection is done."""
        if request in fake._stalls:
            fake._stop.wait()
            return True
        if request in fake._resets:
            self._reset()
            return True
        if request in fake._raw:
            self._send(fake, fake._raw[request])
            return True
        if request in fake._fails:
            self._send(fake, FAIL + frame(fake._fails[request].encode()))
            return True
        return False

    def _reset(self) -> None:
        self.request.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        # Close here: the server would otherwise send a FIN (shutdown)
        # before the close, and the client would see an orderly EOF.
        self.request.close()

    def _answer_transport(
        self,
        fake: FakeAdbServer,
        serial: str,
        entry: dict[str, object] | None = None,
    ) -> bool:
        """Answer OKAY or FAIL for `serial`; `entry` is the listing entry that
        a transport id selected (its own state counts, not the first entry
        with that serial)."""
        match = entry or next((d for d in fake.devices if d["serial"] == serial), None)
        message = None
        if match is None:
            message = f"device '{serial}' not found"
        elif match.get("state", "device") == "offline":
            message = "device offline"
        elif match.get("state") == "unauthorized":
            message = "device unauthorized."
        if message is None:
            self._send(fake, OKAY)
            return True
        self._send(fake, FAIL + frame(message.encode()))
        return False

    def _select(self, fake: FakeAdbServer, request: str) -> str | None:
        """Answer a transport request; return the bound serial, or None on FAIL."""
        if request.startswith("host:transport-id:"):
            return self._answer_transport_id(
                fake, request.removeprefix("host:transport-id:")
            )
        serial = request.removeprefix("host:transport:")
        return serial if self._answer_transport(fake, serial) else None

    def _answer_transport_id(self, fake: FakeAdbServer, wanted: str) -> str | None:
        """Select the device listed with that ``transport_id``; return its serial.

        A missing id answers FAIL with the message adb prints for it.
        """
        match = next(
            (d for d in fake.devices if str(d.get("transport_id", "")) == wanted), None
        )
        if match is None:
            self._send(
                fake, FAIL + frame(f"no device with transport id '{wanted}'".encode())
            )
            return None
        serial = str(match["serial"])
        return serial if self._answer_transport(fake, serial, match) else None

    def _answer_host(self, fake: FakeAdbServer, request: str) -> None:
        if request == "host:version":
            body = f"{fake.version:04x}".encode("ascii")
        elif request == "host:devices-l":
            body = render_devices(fake.devices).encode("utf-8")
        else:
            fake._note_unknown(f"host service {request}")
            self._send(fake, FAIL + frame(b"unknown host service"))
            return
        self._send(fake, OKAY + frame(body))

    def _answer_service(self, fake: FakeAdbServer, serial: str, request: str) -> None:
        kind, _, command = request.partition(":")
        table = fake._shell if kind == "shell" else fake._exec
        reply: Response = b""
        for matcher, candidate in table.get(serial, []):
            if _matches(matcher, command):
                reply = candidate
                break
        # A callable is evaluated before OKAY, so it may answer with a FAIL.
        response = reply(command) if callable(reply) else reply
        if isinstance(response, Reset):
            self._reset()
            return
        if isinstance(response, Fail):
            self._send(fake, FAIL + frame(response.message.encode()))
            return
        # A real server answers OKAY as soon as the service is open and streams
        # the output afterwards, so a slow command is a silence after OKAY.
        self._send(fake, OKAY)
        if isinstance(response, Stall):
            self._send(fake, response.prefix)
            fake._stop.wait()
        elif isinstance(response, Trickle):
            fake._stop.wait(response.lead)
            for start in range(0, len(response.data), response.chunk_size):
                self.request.sendall(response.data[start : start + response.chunk_size])
                fake._stop.wait(response.delay)
        elif isinstance(response, bytes):
            self._send(fake, response)


def _matches(matcher: Matcher, command: str) -> bool:
    if isinstance(matcher, str):
        return matcher == command
    return matcher.search(command) is not None


class FakeAdbServer:
    """A loopback adb server scripted from Python.

    Use it as a context manager. Unscripted shell and exec commands answer OKAY
    with empty output, as a real device does for a command that prints nothing.
    Strict mode (`allow_only`) is an allowlist: it refuses other commands, but
    a command on the list that has no script still answers with empty output.

    Attributes:
        version: Protocol version answered to ``host:version``.
        devices: Device dicts (`serial`, optional `state`, `product`, `model`,
            `device`, `transport_id`) rendered for ``host:devices-l``.
        requests: Every request received, in order.
        wire: The raw bytes of every request (length header and payload).
        chunk_size: When set, every server write is split into pieces of this
            size, `chunk_delay` seconds apart, to exercise short reads.
        protocol_errors: Requests that broke the framing rules (the fixture
            fails the test at teardown when there are any).
        handler_errors: Exceptions that crashed a connection handler.
        commands: Every shell and exec command on a bound transport as
            ``(serial, service, command)`` in arrival order (``service`` is
            ``"shell"`` or ``"exec"``), including those a script then resets,
            stalls or fails.
        unexpected: Commands refused by strict mode, and requests for services
            the fake does not implement while strict mode is on (see
            `allow_only`).
    """

    def __init__(self, *, port: int = 0) -> None:
        self.version = 41
        self.devices: list[dict[str, object]] = []
        self.requests: list[str] = []
        self.wire: list[bytes] = []
        self.chunk_size: int | None = None
        self.chunk_delay = 0.0
        self.protocol_errors: list[str] = []
        self.handler_errors: list[str] = []
        self.commands: list[tuple[str, str, str]] = []
        self.unexpected: list[str] = []
        self._open: set[str] = set()
        # Strict mode: serial (None = any device) -> service kind -> matchers.
        self._rules: dict[str | None, dict[str, list[Matcher]]] = {}
        self._local = threading.local()
        self._requested_port = port
        self._shell: dict[str, list[tuple[Matcher, Response]]] = {}
        self._exec: dict[str, list[tuple[Matcher, Response]]] = {}
        self._fails: dict[str, str] = {}
        self._raw: dict[str, bytes] = {}
        self._stalls: set[str] = set()
        self._resets: set[str] = set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def add_device(self, serial: str, state: str = "device", **fields: object) -> None:
        """Add a device to the list; a field given as None is left out."""
        given = {key: value for key, value in fields.items() if value is not None}
        self.devices.append({"serial": serial, "state": state, **given})

    def on_shell(
        self,
        serial: str,
        matcher: Matcher,
        response: Response,
        *,
        first: bool = False,
    ) -> None:
        """Script the answer to ``shell:`` commands that match `matcher`.

        A string matches the whole command; a pattern matches with `search`.
        Earlier scripts win; `first` puts this one in front of the others.
        Adding a script behind a catch-all (an empty pattern) raises, because
        it would never be reached.
        """
        self._add(self._shell, serial, matcher, response, first=first)

    def on_exec(
        self,
        serial: str,
        matcher: Matcher,
        response: Response,
        *,
        first: bool = False,
    ) -> None:
        """Script the answer to ``exec:`` commands that match `matcher`."""
        self._add(self._exec, serial, matcher, response, first=first)

    @staticmethod
    def _add(
        table: dict[str, list[tuple[Matcher, Response]]],
        serial: str,
        matcher: Matcher,
        response: Response,
        *,
        first: bool,
    ) -> None:
        scripts = table.setdefault(serial, [])
        if first:
            scripts.insert(0, (matcher, response))
            return
        if any(isinstance(m, re.Pattern) and not m.pattern for m, _ in scripts):
            msg = f"a catch-all script for {serial!r} already shadows {matcher!r}"
            raise ValueError(msg)
        scripts.append((matcher, response))

    def allow_only(
        self,
        patterns: Allowlist,
        *,
        serial: str | None = None,
        kinds: Sequence[str] = KINDS,
    ) -> None:
        """Turn on strict mode: only commands matching `patterns` are served.

        Any other shell or exec command is recorded in `unexpected`, answered
        with FAIL (so the client breaks at once), and fails the `fake_adb`
        fixture at teardown. So does a request for a service the fake does not
        implement. A command on the list that no script matches still answers
        OKAY with empty output.

        The list is checked before any request script, so a scripted reset,
        stall, raw answer or FAIL does not admit a shell or exec command; a
        command sent before a transport was selected is refused too. Scripts
        for other services (``host:...``, ``sync:``) are the test's explicit
        intent and are not checked.

        Args:
            patterns: Either one list of matchers, valid for every service
                kind in `kinds`, or a mapping from a service kind (``shell``
                or ``exec``) to its own list; a kind the mapping lacks admits
                nothing.
            serial: The device the list is for. A command for another device
                is refused unless that device has a list of its own (or this
                call left `serial` as None, which admits the list for any
                device). Calling again for the same `serial` replaces its list.
            kinds: The service kinds a plain list is valid for.

        Raises:
            ValueError: A service kind is not ``shell`` or ``exec``.
        """
        if isinstance(patterns, Mapping):
            per_kind = {kind: list(found) for kind, found in patterns.items()}
        else:
            per_kind = {kind: list(patterns) for kind in kinds}
        for kind in per_kind:
            if kind not in KINDS:
                msg = f"not a service kind: {kind!r}"
                raise ValueError(msg)
        with self._lock:
            self._rules[serial] = per_kind

    def allow_any(self, serial: str) -> None:
        """Serve every shell and exec command of `serial`, whatever `allow_only` says.

        For a device in lax mode that shares the server with strict devices.
        It does not turn strict mode on by itself.
        """
        with self._lock:
            self._open.add(serial)

    @property
    def admitted(self) -> int:
        """How many commands were recorded when this thread's command arrived.

        Read it from a script that answers a command: it is that command's
        1-based position in `commands`, fixed atomically with the recording
        (the length of `commands` could already have grown by then when
        several connections are open). Without an admitted command on this
        thread it is the current length.
        """
        with self._lock:
            return getattr(self._local, "admitted", len(self.commands))

    def _admit(self, serial: str, kind: str, command: str) -> bool:
        """Record a command; return False when strict mode refuses it."""
        with self._lock:
            self.commands.append((serial, kind, command))
            self._local.admitted = len(self.commands)
            if not self._rules or serial in self._open:
                return True
            for key in (None, serial):
                matchers = self._rules.get(key, {}).get(kind, ())
                if any(_matches(m, command) for m in matchers):
                    return True
            self.unexpected.append(f"{kind}:{command}")
            if serial not in self._rules and None not in self._rules:
                self.unexpected[-1] += f" (no allowlist for {serial})"
            return False

    def _note_unbound(self, request: str) -> bool:
        """In strict mode, record a command sent before a transport was chosen.

        Returns True when the request must be refused.
        """
        with self._lock:
            if not self._rules:
                return False
            self.unexpected.append(request)
            return True

    def _note_unknown(self, what: str) -> None:
        """In strict mode, keep an unimplemented request from passing silently."""
        with self._lock:
            if self._rules:
                self.unexpected.append(what)

    def fail_request(self, request: str, message: str) -> None:
        """Answer the exact `request` with FAIL and `message`.

        In strict mode a shell or exec `request` must still be admitted by
        `allow_only` (otherwise it is recorded in `unexpected` and refused);
        any other service is scripted unchecked.
        """
        self._fails[request] = message

    def send_raw(self, request: str, data: bytes) -> None:
        """Answer the exact `request` with `data`, then close the connection.

        In strict mode a shell or exec `request` must still be admitted by
        `allow_only` (otherwise it is recorded in `unexpected` and refused);
        any other service is scripted unchecked.
        """
        self._raw[request] = data

    def reset(self, request: str) -> None:
        """Reset the connection (RST, no orderly close) on the exact `request`.

        In strict mode a shell or exec `request` must still be admitted by
        `allow_only` (otherwise it is recorded in `unexpected` and refused);
        any other service is scripted unchecked.
        """
        self._resets.add(request)

    def stall(self, request: str) -> None:
        """Never answer the exact `request` (until the server stops).

        In strict mode a shell or exec `request` must still be admitted by
        `allow_only` (otherwise it is recorded in `unexpected` and refused);
        any other service is scripted unchecked.
        """
        self._stalls.add(request)

    @property
    def port(self) -> int:
        """The listening port."""
        if self._server is None:
            msg = "server is not running"
            raise RuntimeError(msg)
        return self._server.server_address[1]

    @property
    def address(self) -> str:
        """The server address in ``tcp:HOST:PORT`` form."""
        return f"tcp:127.0.0.1:{self.port}"

    def start(self) -> None:
        """Start listening in a background thread."""
        self._server = _Server(self, self._requested_port)
        allow_port(self.port)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.001},
            name="fake-adb",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the server and wait for every thread to end."""
        if self._server is None:
            return
        self._stop.set()
        self._server.shutdown()
        with self._lock:
            for sock in list(self._connections):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    continue
        self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        self._server = None
        self._thread = None

    def _track(self, sock: socket.socket, *, add: bool) -> None:
        with self._lock:
            if add:
                self._connections.add(sock)
            else:
                self._connections.discard(sock)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
